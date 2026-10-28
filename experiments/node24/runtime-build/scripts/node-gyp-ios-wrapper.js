'use strict'

const fs = require('node:fs')
const path = require('node:path')
const { spawnSync } = require('node:child_process')
const support = require('./native-support')

const root = support.runtimeRoot()
const realNodeGyp = path.join(root, 'lib', 'node_modules', 'npm', 'node_modules', 'node-gyp', 'bin', 'node-gyp.js')

if (!fs.existsSync(realNodeGyp)) {
  console.error(`CodexApp node-gyp: bundled node-gyp missing: ${realNodeGyp}`)
  process.exit(66)
}

const requested = process.argv.slice(2)
const versionOnly = requested.length === 1 && (requested[0] === '--version' || requested[0] === '-v')
if (!versionOnly && (!process.env.CA_THEOS_SDKROOT || !process.env.CA_THEOS_CLANG || !process.env.CA_THEOS_MAKE)) {
  console.error('CodexApp node-gyp: native addons require the Theos Runtime (clang/make/iPhoneOS SDK).')
  process.exit(69)
}

const env = { ...process.env }
env.npm_config_nodedir ||= path.join(root, 'devkit', 'node')
env.npm_config_arch ||= 'arm64'
env.npm_config_target_arch ||= 'arm64'
env.npm_config_build_from_source ||= 'true'
env.SDKROOT ||= env.CA_THEOS_SDKROOT
env.CC ||= env.CA_THEOS_CLANG
env.CXX ||= env.CA_THEOS_CLANGXX || env.CA_THEOS_CLANG.replace(/clang(?:-16)?$/, m => m.startsWith('clang-') ? 'clang++-16' : 'clang++')
env.MAKE ||= env.CA_THEOS_MAKE
env.IPHONEOS_DEPLOYMENT_TARGET ||= '15.0'
const targetFlags = `-arch arm64 -isysroot ${env.CA_THEOS_SDKROOT} -miphoneos-version-min=15.0`
env.CFLAGS = `${targetFlags} ${env.CFLAGS || ''}`.trim()
env.CXXFLAGS = `${targetFlags} ${env.CXXFLAGS || ''}`.trim()
env.LDFLAGS = `${targetFlags} -Wl,-no_fixup_chains ${env.LDFLAGS || ''}`.trim()
env.GYP_DEFINES = `OS=ios target_arch=arm64 ${env.GYP_DEFINES || ''}`.trim()

const result = spawnSync(process.execPath, [realNodeGyp, ...requested], {
  stdio: 'inherit',
  env
})
if (result.error) throw result.error
if (result.status !== 0) process.exit(result.status == null ? 70 : result.status)

const command = process.argv[2] || ''
if (command === 'build' || command === 'rebuild') {
  const addons = support.collectNodeAddons(path.join(process.cwd(), 'build'))
  if (addons.length) {
    support.signAddons(addons)
    console.log(`CodexApp node-gyp: signed ${addons.length} native addon(s)`)
  }
}
