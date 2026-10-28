'use strict'

const fs = require('node:fs')
const path = require('node:path')
const crypto = require('node:crypto')
const { spawnSync } = require('node:child_process')
const { threadId } = require('node:worker_threads')

let cleanupRegistered = false

function runtimeRoot () {
  return process.env.CA_TOOL_NODE_RUNTIME_ROOT || path.resolve(path.dirname(process.execPath), '..')
}

function hasMachOCodeSignature (file) {
  let fd
  try {
    fd = fs.openSync(file, 'r')
    const stat = fs.fstatSync(fd)
    if (stat.size < 32) return false

    const readAt = (offset, length) => {
      if (!Number.isSafeInteger(offset) || offset < 0 || offset >= stat.size || length <= 0) return null
      const size = Math.min(length, stat.size - offset)
      const buffer = Buffer.alloc(size)
      const read = fs.readSync(fd, buffer, 0, size, offset)
      return read === size ? buffer : buffer.subarray(0, read)
    }

    const readThin = offset => {
      const header = readAt(offset, 32)
      if (!header || header.length < 28) return false
      const magicLE = header.readUInt32LE(0)
      let little = true
      let headerSize = 0
      if (magicLE === 0xfeedfacf) headerSize = 32
      else if (magicLE === 0xfeedface) headerSize = 28
      else {
        const magicBE = header.readUInt32BE(0)
        if (magicBE === 0xfeedfacf) { little = false; headerSize = 32 }
        else if (magicBE === 0xfeedface) { little = false; headerSize = 28 }
        else return false
      }
      const u32 = (buffer, position) => little ? buffer.readUInt32LE(position) : buffer.readUInt32BE(position)
      const ncmds = u32(header, 16)
      const sizeofcmds = u32(header, 20)
      if (ncmds > 65536 || sizeofcmds > 16 * 1024 * 1024) return false
      const commands = readAt(offset + headerSize, sizeofcmds)
      if (!commands || commands.length !== sizeofcmds) return false
      let command = 0
      for (let i = 0; i < ncmds; i++) {
        if (command + 8 > commands.length) return false
        const cmd = u32(commands, command)
        const cmdsize = u32(commands, command + 4)
        if (cmd === 0x1d) return true // LC_CODE_SIGNATURE
        if (cmdsize < 8 || command + cmdsize > commands.length) return false
        command += cmdsize
      }
      return false
    }

    if (readThin(0)) return true
    // Universal/fat Mach-O. iOS addons are normally thin arm64, but supporting
    // fat prebuilt modules here is cheap and avoids a false "unsigned" result.
    const fatHeader = readAt(0, 8)
    if (!fatHeader || fatHeader.length < 8) return false
    const fatMagic = fatHeader.readUInt32BE(0)
    if (fatMagic !== 0xcafebabe && fatMagic !== 0xcafebabf) return false
    const fat64 = fatMagic === 0xcafebabf
    const nfat = fatHeader.readUInt32BE(4)
    const stride = fat64 ? 32 : 20
    const table = readAt(8, Math.min(nfat, 128) * stride)
    if (!table) return false
    for (let i = 0; i < nfat && i < 128; i++) {
      const entry = i * stride
      if (entry + stride > table.length) break
      const sliceOffset = fat64 ? Number(table.readBigUInt64BE(entry + 8)) : table.readUInt32BE(entry + 8)
      if (readThin(sliceOffset)) return true
    }
    return false
  } catch {
    return false
  } finally {
    if (fd !== undefined) try { fs.closeSync(fd) } catch {}
  }
}

function appRoot () {
  return path.resolve(runtimeRoot(), '..', '..')
}

function nativeExecutionRoot () {
  return path.join(runtimeRoot(), 'lib', '.native-addons', `${process.pid}-${threadId}`)
}

function insideAppBundle (file) {
  const root = fs.realpathSync(appRoot())
  const resolved = fs.realpathSync(file)
  return resolved === root || resolved.startsWith(root + path.sep)
}

function registerExecutionCleanup () {
  if (cleanupRegistered) return
  cleanupRegistered = true
  const root = nativeExecutionRoot()
  process.once('exit', () => {
    try { fs.rmSync(root, { recursive: true, force: true }) } catch {}
  })
}

function stageAddonForLoad (file) {
  const source = fs.realpathSync(file)
  if (path.extname(source) !== '.node') throw new Error(`not a native addon: ${source}`)
  if (insideAppBundle(source)) return source

  // node-gyp already signs its output. Re-signing that same Mach-O with
  // codex-sign can fail with errSecCSBadObjectFormat / -67048 on iOS. Only sign
  // addons that truly have no LC_CODE_SIGNATURE; then copy the exact bytes into
  // the App bundle execution mirror so the signature/CDHash remains unchanged.
  if (!hasMachOCodeSignature(source)) signAddons([source])
  else process.stderr.write(`CodexApp node native: existing code signature, skip resign ${path.basename(source)}\n`)
  const stat = fs.statSync(source)
  const identity = `${source}\n${stat.size}\n${Math.trunc(stat.mtimeMs)}`
  const suffix = crypto.createHash('sha256').update(identity).digest('hex').slice(0, 20)
  const base = path.basename(source, '.node').replace(/[^A-Za-z0-9._-]+/g, '_') || 'addon'
  const root = nativeExecutionRoot()
  fs.mkdirSync(root, { recursive: true, mode: 0o755 })
  registerExecutionCleanup()
  const destination = path.join(root, `${base}-${suffix}.node`)
  if (!fs.existsSync(destination)) {
    const temporary = `${destination}.tmp-${process.pid}-${threadId}`
    try { fs.rmSync(temporary, { force: true }) } catch {}
    fs.copyFileSync(source, temporary)
    fs.chmodSync(temporary, 0o755)
    fs.renameSync(temporary, destination)
  }
  return destination
}

function preferredShell () {
  const shell = process.env.SHELL || process.env.CA_THEOS_BASH
  return shell && fs.existsSync(shell) ? shell : null
}

function signAddons (files) {
  const unique = [...new Set(files.map(file => {
    try { return fs.realpathSync(file) } catch { return null }
  }).filter(Boolean))]
  if (unique.length === 0) return true
  for (const file of unique) {
    if (path.extname(file) !== '.node') throw new Error(`refusing to sign non-.node target: ${file}`)
    const rootReal = path.dirname(file)
    const payload = Buffer.from(JSON.stringify([path.basename(file)]), 'utf8').toString('base64')
    const executable = path.join(appRoot(), 'CodexApp')
    const result = spawnSync(executable, ['-agent-sign-node-addons', rootReal, payload], {
      stdio: 'inherit',
      env: process.env
    })
    if (result.error) throw result.error
    if (result.status !== 0) throw new Error(`native addon signing failed (exit ${result.status}): ${file}`)
  }
  return true
}

function collectNodeAddons (start) {
  const result = []
  const stack = [{ dir: start, depth: 0 }]
  while (stack.length && result.length < 256) {
    const { dir, depth } = stack.pop()
    if (depth > 8) continue
    let entries
    try { entries = fs.readdirSync(dir, { withFileTypes: true }) } catch { continue }
    for (const entry of entries) {
      const full = path.join(dir, entry.name)
      if (entry.isDirectory()) stack.push({ dir: full, depth: depth + 1 })
      else if (entry.isFile() && entry.name.endsWith('.node')) result.push(full)
    }
  }
  return result
}

module.exports = { runtimeRoot, appRoot, nativeExecutionRoot, insideAppBundle, hasMachOCodeSignature, preferredShell, signAddons, stageAddonForLoad, collectNodeAddons }
