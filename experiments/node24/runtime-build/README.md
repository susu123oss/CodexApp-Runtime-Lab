# Node 24 Runtime build inputs

This directory mirrors only the CodexApp Runtime packaging scripts needed for an isolated test candidate. The packaging workflow reuses the already successful NodeMobile.framework artifact from run 40596037615, runs Node configure/header staging only (no V8 compilation), then assembles and validates the complete portable-v1 NodeRuntime. It is intentionally separate from the production Codex-MT repository and Runtime catalog. The workflow uploads a 14-day test artifact and does not publish a Release or change the production manifest.
