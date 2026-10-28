'use strict'

const path = require('node:path')
const childProcess = require('node:child_process')
const support = require('./native-support')

// Replace Node 18's undici-backed global fetch before user code runs. The iOS
// JITless runtime intentionally has no WebAssembly, while npm's bundled
// minipass-fetch is pure JS and uses the already validated http/https stack.
require('./fetch-compat').install(support.runtimeRoot())

// Node's Unix exec()/execSync() default is /bin/sh. iOS does not guarantee
// that path, while CodexApp already exposes a verified shell in $SHELL.
const shell = support.preferredShell()
if (shell) {
  const originalExec = childProcess.exec
  childProcess.exec = function (command, options, callback) {
    if (typeof options === 'function') return originalExec.call(this, command, { shell }, options)
    const next = options && typeof options === 'object' ? { ...options } : {}
    if (!next.shell) next.shell = shell
    return originalExec.call(this, command, next, callback)
  }
  const originalExecSync = childProcess.execSync
  childProcess.execSync = function (command, options) {
    const next = options && typeof options === 'object' ? { ...options } : {}
    if (!next.shell) next.shell = shell
    return originalExecSync.call(this, command, next)
  }
  for (const name of ['spawn', 'spawnSync']) {
    const original = childProcess[name]
    childProcess[name] = function (command, args, options) {
      let argv = args
      let opts = options
      if (!Array.isArray(argv)) { opts = argv; argv = [] }
      if (opts && opts.shell === true) opts = { ...opts, shell }
      return original.call(this, command, argv, opts)
    }
  }
  if (!process.env.npm_config_script_shell) process.env.npm_config_script_shell = shell
}

// Toolchain defaults are activated only when the independently managed Theos
// Runtime is present. Pure-JS npm remains usable without Theos/Python.
if (process.env.CA_THEOS_SDKROOT && process.env.CA_THEOS_CLANG) {
  process.env.SDKROOT ||= process.env.CA_THEOS_SDKROOT
  process.env.CC ||= process.env.CA_THEOS_CLANG
  process.env.CXX ||= process.env.CA_THEOS_CLANGXX || process.env.CA_THEOS_CLANG.replace(/clang(?:-16)?$/, m => m.startsWith('clang-') ? 'clang++-16' : 'clang++')
  process.env.MAKE ||= process.env.CA_THEOS_MAKE || 'make'
  process.env.IPHONEOS_DEPLOYMENT_TARGET ||= '15.0'
  const targetFlags = `-arch arm64 -isysroot ${process.env.CA_THEOS_SDKROOT} -miphoneos-version-min=15.0`
  process.env.CFLAGS = `${targetFlags} ${process.env.CFLAGS || ''}`.trim()
  process.env.CXXFLAGS = `${targetFlags} ${process.env.CXXFLAGS || ''}`.trim()
  process.env.LDFLAGS = `${targetFlags} -Wl,-no_fixup_chains ${process.env.LDFLAGS || ''}`.trim()
  process.env.GYP_DEFINES = `OS=ios target_arch=arm64 ${process.env.GYP_DEFINES || ''}`.trim()
}

// iOS can reject executable mmap from Documents/Application Support even after
// a .node has a valid signature. Native addons outside the current App bundle
// therefore run from a short-lived execution mirror under the managed Node
// Runtime. The project artifact remains in place; only the mmap image is copied.
const originalDlopen = process.dlopen
process.dlopen = function (module, filename, flags) {
  const requested = String(filename)
  if (path.extname(requested) !== '.node') return originalDlopen.call(this, module, filename, flags)
  const executable = support.stageAddonForLoad(requested)
  return originalDlopen.call(this, module, executable, flags)
}
