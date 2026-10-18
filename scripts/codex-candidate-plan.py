#!/usr/bin/env python3
"""Resolve the next OpenAI Codex stable candidate without mutating the repo.

The candidate identity is the upstream stable version plus a digest of the
canonical CodexApp patch/build gate inputs. GitHub Actions uses that identity
to avoid rebuilding the same candidate every day while still rebuilding when
our patchset or packaging gate changes.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RECIPE_PATH = ROOT / "Runtime/Recipes/codex.json"
MANIFEST_PATH = ROOT / "Runtime/Codex/Patches/manifest.json"
PATCH_ROOT = MANIFEST_PATH.parent
API_BASE = "https://api.github.com/repos/openai/codex"
STABLE_TAG = re.compile(r"^rust-v(\d+)\.(\d+)\.(\d+)$")
SHA = re.compile(r"^[a-f0-9]{40}$")


def request_json(url: str, timeout: int) -> object:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "CodexApp-candidate-plan",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except (urllib.error.URLError, http.client.IncompleteRead, TimeoutError) as error:
            last_error = error
            if attempt == 2:
                raise
            time.sleep(1 << attempt)
    raise RuntimeError(f"GitHub request failed: {last_error}")


def latest_stable(releases: object) -> tuple[str, str]:
    if not isinstance(releases, list):
        raise ValueError("OpenAI releases response is not an array")
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for release in releases:
        if not isinstance(release, dict):
            continue
        tag = str(release.get("tag_name") or "")
        match = STABLE_TAG.fullmatch(tag)
        if not match or release.get("draft") or release.get("prerelease"):
            continue
        candidates.append((tuple(map(int, match.groups())), tag))
    if not candidates:
        raise ValueError("no stable rust-vX.Y.Z release found")
    version_tuple, tag = max(candidates)
    return ".".join(map(str, version_tuple)), tag


def resolve_tag_sha(tag: str, timeout: int) -> str:
    encoded = urllib.parse.quote(tag, safe="")
    ref = request_json(f"{API_BASE}/git/ref/tags/{encoded}", timeout)
    if not isinstance(ref, dict):
        raise ValueError(f"invalid tag ref for {tag}")
    target = ref.get("object")
    if not isinstance(target, dict):
        raise ValueError(f"missing tag object for {tag}")
    if target.get("type") == "tag":
        tag_object = request_json(f"{API_BASE}/git/tags/{target.get('sha', '')}", timeout)
        if not isinstance(tag_object, dict) or not isinstance(tag_object.get("object"), dict):
            raise ValueError(f"invalid annotated tag object for {tag}")
        target = tag_object["object"]
    sha = str(target.get("sha") or "")
    if not SHA.fullmatch(sha):
        raise ValueError(f"could not resolve {tag} to a commit")
    return sha


def patch_digest(manifest: dict) -> str:
    digest = hashlib.sha256()
    inputs = [
        MANIFEST_PATH,
        ROOT / "scripts/codex-candidate-plan.py",
        ROOT / "scripts/codex-patchset.py",
        ROOT / "scripts/publish-codex-runtime.py",
        ROOT / ".github/workflows/codex-runtime-candidate.yml",
    ]
    for item in sorted(manifest["patches"], key=lambda entry: int(entry["order"])):
        inputs.append(PATCH_ROOT / item["file"])
    for path in inputs:
        if not path.is_file():
            raise ValueError(f"candidate identity input is missing: {path}")
        relative = path.relative_to(ROOT).as_posix().encode()
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def version_tuple(version: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError(f"invalid semantic version: {version}")
    return tuple(map(int, version.split(".")))  # type: ignore[return-value]


def plan(timeout: int) -> dict[str, object]:
    recipe = json.loads(RECIPE_PATH.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    source = recipe.get("source") or {}
    current_version = str(source.get("version") or "")
    current_commit = str(source.get("commit") or "")
    baseline = str((manifest.get("canonical_upstream") or {}).get("baseline_commit") or "")
    if not SHA.fullmatch(current_commit):
        raise ValueError("Runtime/Recipes/codex.json has an invalid source commit")
    if not SHA.fullmatch(baseline):
        raise ValueError("patch manifest has an invalid baseline commit")

    # GitHub's latest-release endpoint excludes drafts and prereleases. Prefer
    # the small single-release payload over downloading the full release list,
    # which can be tens of megabytes for openai/codex because every release has
    # many platform assets.
    latest_release = request_json(f"{API_BASE}/releases/latest", timeout)
    candidate_version, candidate_tag = latest_stable([latest_release])
    candidate_sha = resolve_tag_sha(candidate_tag, timeout)
    fingerprint = patch_digest(manifest)
    short_fingerprint = fingerprint[:12]

    return {
        "version": candidate_version,
        "upstream_tag": candidate_tag,
        "upstream_sha": candidate_sha,
        "current_version": current_version,
        "current_commit": current_commit,
        "baseline_commit": baseline,
        "patch_digest": fingerprint,
        "candidate_tag": f"codex-candidate-v{candidate_version}-{short_fingerprint}",
        "runtime_version": f"{candidate_version}-0",
        "is_newer": version_tuple(candidate_version) > version_tuple(current_version),
    }


def write_github_output(path: Path, result: dict[str, object]) -> None:
    mapping = {
        "version": result["version"],
        "upstream_tag": result["upstream_tag"],
        "upstream_sha": result["upstream_sha"],
        "current_version": result["current_version"],
        "current_commit": result["current_commit"],
        "baseline_commit": result["baseline_commit"],
        "patch_digest": result["patch_digest"],
        "candidate_tag": result["candidate_tag"],
        "runtime_version": result["runtime_version"],
        "is_newer": str(bool(result["is_newer"])).lower(),
    }
    with path.open("a", encoding="utf-8") as output:
        for key, value in mapping.items():
            output.write(f"{key}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    try:
        result = plan(args.timeout)
    except (
        OSError,
        ValueError,
        KeyError,
        urllib.error.URLError,
        http.client.IncompleteRead,
        json.JSONDecodeError,
    ) as error:
        raise SystemExit(f"candidate planning failed: {error}")
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.github_output:
        write_github_output(args.github_output, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
