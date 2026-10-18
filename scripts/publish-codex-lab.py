#!/usr/bin/env python3
"""Promote a verified Codex-MT candidate DEB to an isolated Lab repository.

The canonical Stable runtime repository is deliberately not configurable here.
Only the independent Lab repository can be changed.  Promotion is explicit;
the Candidate build does not call this script automatically.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


LAB_REPO = "susu123oss/CodexApp-Runtime-Lab"
LAB_BRANCH = "main"
MANIFEST_PATH = "Runtime/runtime-manifest.json"
CANDIDATE_TAG = re.compile(r"^codex-candidate-v(\d+\.\d+\.\d+)-([0-9a-f]{12})$")
DEB_NAME = re.compile(r"^wiki\.qaq\.codex_(\d+\.\d+\.\d+-\d+)_iphoneos-arm64\.deb$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")
MIN_DEB_BYTES = 5 * 1024 * 1024


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_candidate(deb: Path, sums: Path, candidate_tag: str) -> tuple[str, str]:
    candidate_match = CANDIDATE_TAG.fullmatch(candidate_tag)
    asset_match = DEB_NAME.fullmatch(deb.name)
    if not candidate_match or not asset_match:
        raise ValueError("candidate tag or DEB filename is not canonical")
    version = asset_match.group(1)
    if version.rsplit("-", 1)[0] != candidate_match.group(1):
        raise ValueError("candidate upstream version does not match the DEB")
    if not deb.is_file() or deb.stat().st_size < MIN_DEB_BYTES:
        raise ValueError("candidate DEB is missing or unexpectedly small")
    if not sums.is_file():
        raise ValueError("candidate SHA256SUMS is missing")
    rows = [row.strip() for row in sums.read_text(encoding="utf-8").splitlines() if row.strip()]
    if len(rows) != 1:
        raise ValueError("SHA256SUMS must contain exactly one asset")
    match = re.fullmatch(r"([a-f0-9]{64})\s+(.+)", rows[0])
    if not match or match.group(2) != deb.name:
        raise ValueError("SHA256SUMS filename or checksum has an invalid format")
    actual = sha256_file(deb)
    if actual != match.group(1):
        raise ValueError("candidate SHA256SUMS does not match the DEB")
    with deb.open("rb") as stream:
        if stream.read(8) != b"!<arch>\n":
            raise ValueError("candidate DEB does not have a Debian ar header")
    return version, actual


def api(url: str, token: str, *, method: str = "GET", payload: dict | None = None) -> tuple[int, object]:
    raw = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=raw, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "CodexApp-Laboratory-Publisher",
        **({"Content-Type": "application/json"} if raw is not None else {}),
    })
    try:
        with urllib.request.urlopen(request, timeout=60) as result:
            body = result.read()
            return result.status, json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return 404, None
        raise RuntimeError(f"GitHub API {method} failed: HTTP {exc.code} ({url})") from exc


def upload_asset(release_id: int, path: Path, token: str) -> None:
    filename = urllib.parse.quote(path.name, safe="")
    conn = http.client.HTTPSConnection("uploads.github.com", timeout=600)
    try:
        conn.putrequest("POST", f"/repos/{LAB_REPO}/releases/{release_id}/assets?name={filename}")
        conn.putheader("Authorization", f"Bearer {token}")
        conn.putheader("Accept", "application/vnd.github+json")
        conn.putheader("User-Agent", "CodexApp-Laboratory-Publisher")
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(path.stat().st_size))
        conn.endheaders()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                conn.send(chunk)
        response = conn.getresponse()
        raw = response.read()
        if response.status != 201:
            raise RuntimeError(f"Lab release asset upload failed: HTTP {response.status}")
        result = json.loads(raw)
        if result.get("name") != path.name or result.get("size") != path.stat().st_size:
            raise RuntimeError("Lab release uploaded asset name/size mismatch")
    finally:
        conn.close()


def base_manifest() -> dict:
    return {
        "schema": 3,
        "source_name": "CodexApp Laboratory",
        "source_format": 1,
        "download_proxy_prefixes": ["https://gh-proxy.org/", "https://ghproxy.net/"],
        "runtimes": {},
    }


def version_parts(version: str) -> tuple[int, int, int, int]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)-(\d+)", version)
    if not match:
        raise ValueError(f"invalid lab runtime version: {version}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def prepare_manifest(old: dict, version: str, deb: Path, digest: str, candidate_tag: str) -> dict:
    if not isinstance(old, dict) or not isinstance(old.get("runtimes"), dict):
        raise ValueError("Lab manifest has invalid structure")
    current = old["runtimes"].get("codex")
    if current is not None and not isinstance(current, dict):
        raise ValueError("Lab manifest codex entry is invalid")
    current = current or {}
    current_version = current.get("version")
    if isinstance(current_version, str):
        if version_parts(f"v{version}") <= version_parts(current_version):
            raise ValueError("Lab catalog version must increase; never overwrite/downgrade an existing catalog entry")
    release_tag = f"runtime-codex-v{version}"
    codex = dict(current)
    codex.update({
        "archive_sha256": digest,
        "candidate_tag": candidate_tag,
        "archive_url": f"https://github.com/{LAB_REPO}/releases/download/{release_tag}/{deb.name}",
        "asset_name": deb.name,
        "display_name": "Codex · 实验室版",
        "download_size": deb.stat().st_size,
        "group": "core",
        "handler": "codex",
        "minimum_ios": "15.0",
        "optional": False,
        "order": 10,
        "release_tag": release_tag,
        "runtime_format": 1,
        "summary": "实验室 Codex CLI 与 Code Mode Host；需用户手动选择通道并确认安装。",
        "version": f"v{version}",
    })
    new = dict(old)
    new["runtimes"] = dict(old["runtimes"])
    new["runtimes"]["codex"] = codex
    new["schema"] = max(3, int(old.get("schema", 1) or 1))
    return new


def publish(deb: Path, sums: Path, candidate_tag: str, token: str) -> None:
    version, digest = verify_candidate(deb, sums, candidate_tag)
    root = f"https://api.github.com/repos/{LAB_REPO}"
    status, repository = api(root, token)
    if status != 200 or not isinstance(repository, dict):
        raise RuntimeError(f"Lab repository {LAB_REPO} is not accessible; create it first")
    if repository.get("full_name") != LAB_REPO or repository.get("private"):
        raise RuntimeError("Lab repository must exist and be public for anonymous CodexApp downloads")
    if repository.get("default_branch") != LAB_BRANCH:
        raise RuntimeError("Lab repository default branch must be main")

    manifest_api = f"{root}/contents/{MANIFEST_PATH}"
    status, record = api(f"{manifest_api}?ref={LAB_BRANCH}", token)
    if status == 404:
        existing = base_manifest()
        content_sha = None
    elif status == 200 and isinstance(record, dict):
        content_sha = record.get("sha")
        existing = json.loads(base64.b64decode(str(record.get("content", ""))).decode("utf-8"))
    else:
        raise RuntimeError("Lab manifest could not be read")

    # Preflight the entire next manifest *before* creating a public Release.
    next_manifest = prepare_manifest(existing, version, deb, digest, candidate_tag)
    release_tag = f"runtime-codex-v{version}"
    status, old_release = api(f"{root}/releases/tags/{release_tag}", token)
    if status == 200:
        raise RuntimeError(f"Lab release {release_tag} already exists; never replace immutable assets")
    if status != 404:
        raise RuntimeError("Lab release existence could not be verified")

    notes = (
        f"CodexApp Laboratory Runtime v{version}\n\n"
        f"Source Candidate: {candidate_tag}\n"
        f"DEB SHA256: {digest}\n\n"
        "Experimental channel only. Does not update CodexApp-Runtime Stable. "
        "Users must select the Laboratory channel and confirm installation.\n"
    )
    _, created = api(f"{root}/releases", token, method="POST", payload={
        "tag_name": release_tag,
        "target_commitish": LAB_BRANCH,
        "name": f"Codex Laboratory v{version}",
        "body": notes,
        "draft": False,
        "prerelease": True,
    })
    if not isinstance(created, dict) or not isinstance(created.get("id"), int):
        raise RuntimeError("Lab release creation returned an invalid response")
    release_id = created["id"]
    upload_asset(release_id, deb, token)
    upload_asset(release_id, sums, token)
    _, verified = api(f"{root}/releases/{release_id}/assets?per_page=100", token)
    assets = {item.get("name"): item.get("size") for item in verified or []}
    if assets.get(deb.name) != deb.stat().st_size or assets.get(sums.name) != sums.stat().st_size:
        raise RuntimeError("Lab release asset preflight failed; catalog remains unchanged")

    manifest_bytes = (json.dumps(next_manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    payload = {
        "message": f"lab(runtime): publish v{version} from {candidate_tag}",
        "branch": LAB_BRANCH,
        "content": base64.b64encode(manifest_bytes).decode("ascii"),
    }
    if content_sha:
        payload["sha"] = content_sha
    api(manifest_api, token, method="PUT", payload=payload)
    _, checked = api(f"{manifest_api}?ref={LAB_BRANCH}", token)
    actual = json.loads(base64.b64decode(str(checked.get("content", ""))).decode("utf-8"))
    if actual.get("runtimes", {}).get("codex", {}).get("archive_sha256") != digest:
        raise RuntimeError("Lab manifest verification failed after publication")
    print(f"[lab] published {release_tag} to {LAB_REPO}; Stable repository untouched")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deb", type=Path, required=True)
    parser.add_argument("--sums", type=Path, required=True)
    parser.add_argument("--candidate-tag", required=True)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    try:
        version, digest = verify_candidate(args.deb, args.sums, args.candidate_tag)
        print(f"[lab] verified v{version} / sha256={digest}")
        if args.verify_only:
            return 0
        token = os.environ.get("CODEX_LAB_RELEASE_TOKEN", "").strip()
        if not token:
            raise ValueError("CODEX_LAB_RELEASE_TOKEN is not configured")
        publish(args.deb, args.sums, args.candidate_tag, token)
    except (ValueError, OSError, RuntimeError) as error:
        print(f"[lab] BLOCKED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
