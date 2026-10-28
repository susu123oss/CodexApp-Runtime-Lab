'use strict'

// Node 18's built-in fetch is backed by bundled undici. On the iOS JITless
// build WebAssembly is intentionally unavailable, and that undici path can
// terminate the process before user code can catch the error. npm itself ships
// a mature pure-JS HTTP fetch implementation (minipass-fetch) which uses
// node:http/node:https + zlib and therefore fits the already validated iOS
// runtime. Promote it to the global Web API surface instead of weakening the
// JITless baseline.

const path = require('node:path')
const { Blob, File } = require('node:buffer')
const { Readable } = require('node:stream')

function install (runtimeRoot) {
  const modules = path.join(runtimeRoot, 'lib', 'node_modules', 'npm', 'node_modules')
  const fetchImpl = require(path.join(modules, 'minipass-fetch'))
  const { Minipass } = require(path.join(modules, 'minipass'))
  const BaseRequest = fetchImpl.Request
  const Headers = fetchImpl.Headers
  const Response = fetchImpl.Response

  class FormDataCompat extends Minipass {
    constructor () {
      super()
      this._entries = []
      this._boundary = `----CodexNodeFormBoundary${process.pid.toString(16)}${Date.now().toString(16)}${Math.random().toString(16).slice(2)}`
      this._started = false
      // minipass-fetch detects this shape when calculating Content-Length.
      this._lengthRetrievers = []
    }

    append (name, value, filename) {
      this._entries.push(this._normalize(name, value, filename))
    }

    set (name, value, filename) {
      const key = String(name)
      this._entries = this._entries.filter(entry => entry.name !== key)
      this._entries.push(this._normalize(key, value, filename))
    }

    delete (name) {
      const key = String(name)
      this._entries = this._entries.filter(entry => entry.name !== key)
    }

    has (name) { return this._entries.some(entry => entry.name === String(name)) }
    get (name) { const entry = this._entries.find(item => item.name === String(name)); return entry ? entry.original : null }
    getAll (name) { return this._entries.filter(item => item.name === String(name)).map(item => item.original) }
    * entries () { for (const entry of this._entries) yield [entry.name, entry.original] }
    * keys () { for (const entry of this._entries) yield entry.name }
    * values () { for (const entry of this._entries) yield entry.original }
    [Symbol.iterator] () { return this.entries() }
    forEach (callback, thisArg) { for (const [name, value] of this.entries()) callback.call(thisArg, value, name, this) }
    get [Symbol.toStringTag] () { return 'FormData' }

    getBoundary () { return this._boundary }
    hasKnownLength () { return true }
    getLengthSync () {
      let size = 0
      for (const entry of this._entries) {
        size += Buffer.byteLength(this._header(entry))
        size += entry.size
        size += 2 // trailing CRLF
      }
      size += Buffer.byteLength(`--${this._boundary}--\r\n`)
      return size
    }

    pipe (destination, options) {
      if (!this._started) {
        this._started = true
        queueMicrotask(() => this._encode().catch(error => this.emit('error', error)))
      }
      return super.pipe(destination, options)
    }

    _normalize (name, value, filename) {
      const key = String(name)
      const blobLike = value && typeof value === 'object' &&
        typeof value.arrayBuffer === 'function' && typeof value.size === 'number'
      if (blobLike) {
        const resolvedName = filename !== undefined ? String(filename)
          : typeof value.name === 'string' && value.name ? value.name : 'blob'
        return {
          name: key,
          original: value,
          blob: value,
          filename: resolvedName,
          type: typeof value.type === 'string' && value.type ? value.type : 'application/octet-stream',
          size: Number(value.size) || 0,
        }
      }
      const buffer = Buffer.isBuffer(value) ? value
        : ArrayBuffer.isView(value) ? Buffer.from(value.buffer, value.byteOffset, value.byteLength)
          : Object.prototype.toString.call(value) === '[object ArrayBuffer]' ? Buffer.from(value)
            : Buffer.from(String(value))
      return { name: key, original: value, buffer, filename: filename === undefined ? null : String(filename), type: 'application/octet-stream', size: buffer.length }
    }

    _quoted (value) { return String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/[\r\n]/g, ' ') }
    _header (entry) {
      let disposition = `Content-Disposition: form-data; name="${this._quoted(entry.name)}"`
      if (entry.filename !== null && entry.filename !== undefined) disposition += `; filename="${this._quoted(entry.filename)}"`
      let header = `--${this._boundary}\r\n${disposition}\r\n`
      if (entry.blob || entry.filename !== null) header += `Content-Type: ${entry.type || 'application/octet-stream'}\r\n`
      return `${header}\r\n`
    }

    async _encode () {
      for (const entry of this._entries) {
        this.write(this._header(entry))
        if (entry.blob) this.write(Buffer.from(await entry.blob.arrayBuffer()))
        else this.write(entry.buffer)
        this.write('\r\n')
      }
      this.end(`--${this._boundary}--\r\n`)
    }
  }

  function isFormDataLike (body) {
    return body && typeof body === 'object' && body[Symbol.toStringTag] === 'FormData' && typeof body.entries === 'function'
  }

  function convertFormData (body) {
    if (!isFormDataLike(body) || body instanceof FormDataCompat) return body
    const converted = new FormDataCompat()
    for (const [name, value] of body.entries()) {
      if (value && typeof value === 'object' && typeof value.name === 'string') converted.append(name, value, value.name)
      else converted.append(name, value)
    }
    return converted
  }

  function convertWebStream (body) {
    if (!body || typeof body !== 'object' || typeof body.getReader !== 'function') return body
    if (typeof Readable.fromWeb !== 'function') return body
    const source = Readable.fromWeb(body)
    const out = new Minipass()
    source.on('error', error => out.emit('error', error)).pipe(out)
    return out
  }

  function normalizeInit (init) {
    if (!init || typeof init !== 'object' || !('body' in init)) return init
    const next = { ...init }
    next.body = convertWebStream(convertFormData(next.body))
    // WHATWG fetch requires duplex:'half' for streaming request bodies on newer
    // Node, but minipass-fetch does not need this flag. Accept it silently.
    delete next.duplex
    return next
  }

  class RequestCompat extends BaseRequest {
    constructor (input, init = {}) { super(input, normalizeInit(init) || {}) }
    clone () { return new RequestCompat(this) }
  }

  const fetchCompat = (input, init) => fetchImpl(input, normalizeInit(init))
  Object.defineProperties(fetchCompat, {
    name: { value: 'fetch' },
    Headers: { value: Headers }, Request: { value: RequestCompat }, Response: { value: Response },
    FetchError: { value: fetchImpl.FetchError }, AbortError: { value: fetchImpl.AbortError },
    __codexCompat: { value: Object.freeze({ engine: 'minipass-fetch', version: require(path.join(modules, 'minipass-fetch', 'package.json')).version }) },
  })

  if (!Headers.prototype.getSetCookie) {
    Object.defineProperty(Headers.prototype, 'getSetCookie', {
      configurable: true,
      value: function () {
        const raw = typeof this.raw === 'function' ? this.raw() : {}
        return Array.isArray(raw['set-cookie']) ? [...raw['set-cookie']] : []
      },
    })
  }
  if (!Response.json) {
    Response.json = (value, init = {}) => {
      const headers = new Headers(init.headers || {})
      if (!headers.has('content-type')) headers.set('content-type', 'application/json')
      return new Response(JSON.stringify(value), { ...init, headers })
    }
  }
  if (!Response.redirect) {
    Response.redirect = (url, status = 302) => {
      if (![301, 302, 303, 307, 308].includes(status)) throw new RangeError('Invalid redirect status')
      return new Response(null, { status, headers: { location: new URL(url).toString() } })
    }
  }

  const decodeDispositionValue = value => String(value || '').replace(/\\\"/g, '"').replace(/\\\\/g, '\\')
  const parseFormDataBody = async body => {
    const type = body.headers && body.headers.get('content-type') || ''
    const bytes = Buffer.from(await body.arrayBuffer())
    const result = new FormDataCompat()
    if (/^application\/x-www-form-urlencoded(?:;|$)/i.test(type)) {
      for (const [name, value] of new URLSearchParams(bytes.toString('utf8'))) result.append(name, value)
      return result
    }
    const boundaryMatch = /^multipart\/form-data\s*;[\s\S]*?boundary=(?:"([^"]+)"|([^;\s]+))/i.exec(type)
    if (!boundaryMatch) throw new TypeError(`Unsupported form data content type: ${type || 'unknown'}`)
    const boundary = Buffer.from(`--${boundaryMatch[1] || boundaryMatch[2]}`)
    let cursor = 0
    while (true) {
      const start = bytes.indexOf(boundary, cursor)
      if (start < 0) break
      let partStart = start + boundary.length
      if (bytes.slice(partStart, partStart + 2).equals(Buffer.from('--'))) break
      if (bytes.slice(partStart, partStart + 2).equals(Buffer.from('\r\n'))) partStart += 2
      const next = bytes.indexOf(boundary, partStart)
      if (next < 0) break
      let partEnd = next
      if (partEnd >= 2 && bytes.slice(partEnd - 2, partEnd).equals(Buffer.from('\r\n'))) partEnd -= 2
      const headerEnd = bytes.indexOf(Buffer.from('\r\n\r\n'), partStart)
      if (headerEnd < 0 || headerEnd >= partEnd) { cursor = next; continue }
      const rawHeaders = bytes.slice(partStart, headerEnd).toString('latin1')
      const payload = bytes.slice(headerEnd + 4, partEnd)
      const disposition = /^content-disposition:\s*form-data\s*;([^\r\n]*)/im.exec(rawHeaders)
      if (disposition) {
        const nameMatch = /(?:^|;)\s*name="((?:\\.|[^"])*)"/i.exec(disposition[1])
        const fileMatch = /(?:^|;)\s*filename="((?:\\.|[^"])*)"/i.exec(disposition[1])
        if (nameMatch) {
          const name = decodeDispositionValue(nameMatch[1])
          if (fileMatch) {
            const contentType = /^content-type:\s*([^\r\n]+)/im.exec(rawHeaders)
            const filename = decodeDispositionValue(fileMatch[1])
            const blob = new Blob([payload], { type: contentType ? contentType[1].trim() : 'application/octet-stream' })
            let file = blob
            if (typeof File === 'function') file = new File([payload], filename, { type: blob.type })
            else Object.defineProperty(file, 'name', { value: filename, configurable: true })
            result.append(name, file, filename)
          } else result.append(name, payload.toString('utf8'))
        }
      }
      cursor = next
    }
    return result
  }

  for (const proto of [BaseRequest.prototype, Response.prototype]) {
    if (!proto.bytes) Object.defineProperty(proto, 'bytes', { configurable: true, value: async function () { return new Uint8Array(await this.arrayBuffer()) } })
    if (!proto.formData) Object.defineProperty(proto, 'formData', { configurable: true, value: function () { return parseFormDataBody(this) } })
  }

  globalThis.fetch = fetchCompat
  globalThis.Headers = Headers
  globalThis.Request = RequestCompat
  globalThis.Response = Response
  globalThis.FormData = FormDataCompat
  if (typeof Blob === 'function') globalThis.Blob = Blob
  if (typeof File === 'function') globalThis.File = File
  return fetchCompat
}

module.exports = { install }
