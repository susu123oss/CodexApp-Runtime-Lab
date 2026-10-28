# Node 24 Runtime build inputs

This directory mirrors only the CodexApp Runtime packaging scripts needed for an isolated test candidate. It is intentionally separate from the production Codex-MT repository and Runtime catalog. The workflow builds NodeMobile from the pinned source commit, assembles the full portable-v1 NodeRuntime, validates archive metadata and uploads a 14-day test artifact. It does not publish a Release or change the production manifest.
