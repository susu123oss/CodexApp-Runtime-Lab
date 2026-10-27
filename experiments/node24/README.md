# Node 24 iOS Runtime Experiment

This directory is intentionally isolated from the existing Codex Runtime laboratory.

## Safety boundary

- Do not modify `Runtime/Codex/**`.
- Do not modify `Runtime/runtime-manifest.json`.
- Do not replace or delete existing Codex Runtime releases/assets.
- Do not publish this experiment as the stable `node` Runtime.
- All build output is emitted only as a GitHub Actions artifact.

## Source under test

- Upstream PR: nodejs-mobile/nodejs-mobile#158
- Source repository: ybaelli/nodejs-mobile
- Source commit: 4608d3c670cf37b383dbe631de4992cf23da46e0
- Node.js: 24.5.0
- Target: iPhoneOS arm64, JITless nodejs-mobile framework

The purpose of this branch is compatibility validation only. Promotion to Codex-MT's managed Node Runtime requires separate integration and device tests.
