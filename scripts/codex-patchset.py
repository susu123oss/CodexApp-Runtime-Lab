#!/usr/bin/env python3
"""Manage CodexApp's canonical OpenAI Codex iOS/TrollStore patchset."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATCH_ROOT = ROOT / "Runtime/Codex/Patches"
MANIFEST_PATH = PATCH_ROOT / "manifest.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest() -> dict:
    if not MANIFEST_PATH.is_file():
        raise SystemExit(f"missing patch manifest: {MANIFEST_PATH}")
    data = json.loads(MANIFEST_PATH.read_text())
    if data.get("schema") != 1 or not isinstance(data.get("patches"), list):
        raise SystemExit("unsupported or invalid Codex patch manifest")
    return data


def touched_files(path: Path) -> list[str]:
    text = path.read_text()
    pairs = re.findall(r"^diff --git a/(\S+) b/(\S+)$", text, re.M)
    return [right for _left, right in pairs]


def verify() -> dict:
    manifest = load_manifest()
    patches = sorted(manifest["patches"], key=lambda item: int(item["order"]))
    expected_orders = list(range(1, len(patches) + 1))
    actual_orders = [int(item["order"]) for item in patches]
    if actual_orders != expected_orders:
        raise SystemExit(f"patch orders must be contiguous: {actual_orders}")

    manifest_files = []
    for item in patches:
        path = PATCH_ROOT / item["file"]
        manifest_files.append(item["file"])
        if not path.is_file():
            raise SystemExit(f"missing patch: {path}")
        actual_hash = sha256(path)
        if actual_hash != item["sha256"]:
            raise SystemExit(
                f"patch SHA256 mismatch: {item['file']}\n"
                f"manifest={item['sha256']}\nactual={actual_hash}"
            )
        actual_sources = touched_files(path)
        expected_sources = item.get("source_files") or []
        if actual_sources != expected_sources:
            raise SystemExit(
                f"source map mismatch for {item['file']}\n"
                f"manifest={expected_sources}\nactual={actual_sources}"
            )
        for dependency in item.get("depends_on", []):
            if int(dependency) >= int(item["order"]):
                raise SystemExit(
                    f"invalid dependency for {item['file']}: {dependency} must precede {item['order']}"
                )

    disk_files = [path.name for path in sorted(PATCH_ROOT.glob("[0-9][0-9][0-9][0-9]-*.patch"))]
    if disk_files != manifest_files:
        raise SystemExit(
            "patch directory and manifest differ:\n"
            f"manifest={manifest_files}\nfiles={disk_files}"
        )

    for item in manifest.get("related_non_cli_patches", []):
        path = ROOT / item["file"]
        if not path.is_file():
            raise SystemExit(f"missing related runtime patch: {path}")
        if sha256(path) != item["sha256"]:
            raise SystemExit(f"related runtime patch SHA256 mismatch: {item['file']}")

    return manifest


def print_list(manifest: dict) -> None:
    upstream = manifest["canonical_upstream"]
    print(
        f"Codex patchset baseline: v{upstream['baseline_version']} "
        f"{upstream['baseline_commit']}"
    )
    for item in sorted(manifest["patches"], key=lambda entry: int(entry["order"])):
        sources = ", ".join(item["source_files"])
        print(f"{int(item['order']):04d}  {item['file']}")
        print(f"      {item['title']}")
        print(f"      source: {sources}")
        print(f"      sha256: {item['sha256']}")


def sync(dest: Path, manifest: dict) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    wanted = {item["file"] for item in manifest["patches"]}
    for stale in dest.glob("[0-9][0-9][0-9][0-9]-*.patch"):
        if stale.name not in wanted:
            stale.unlink()
    for item in sorted(manifest["patches"], key=lambda entry: int(entry["order"])):
        target = dest / item["file"]
        shutil.copy2(PATCH_ROOT / item["file"], target)
        if sha256(target) != item["sha256"]:
            raise RuntimeError(f"synced patch SHA256 mismatch: {target}")
        print(f"[codex-patchset] synced {item['file']}")


def patch_state(source: Path, patch: Path, env: dict[str, str] | None = None) -> tuple[str, str]:
    reverse = subprocess.run(
        ["git", "-C", str(source), "apply", "--cached", "--reverse", "--check", str(patch)],
        text=True,
        capture_output=True,
        env=env,
    )
    if reverse.returncode == 0:
        return "applied", "patch is already present in this source tree"
    forward = subprocess.run(
        ["git", "-C", str(source), "apply", "--cached", "--check", str(patch)],
        text=True,
        capture_output=True,
        env=env,
    )
    if forward.returncode == 0:
        applied = subprocess.run(
            ["git", "-C", str(source), "apply", "--cached", str(patch)],
            text=True,
            capture_output=True,
            env=env,
        )
        if applied.returncode != 0:
            detail = (applied.stderr or applied.stdout).strip()
            return "conflict", detail or "patch check passed but indexed apply failed"
        return "applicable", "patch can be applied cleanly"
    detail = (forward.stderr or forward.stdout or reverse.stderr or reverse.stdout).strip()
    return "conflict", detail or "patch neither applies nor reverses cleanly"


def upstream_changed_files(source: Path, baseline: str) -> tuple[set[str] | None, str | None]:
    baseline_probe = subprocess.run(
        ["git", "-C", str(source), "cat-file", "-e", f"{baseline}^{{commit}}"],
        text=True,
        capture_output=True,
    )
    if baseline_probe.returncode != 0:
        return None, "recorded baseline commit is not available in this candidate clone"

    relation = subprocess.run(
        ["git", "-C", str(source), "merge-base", "--is-ancestor", baseline, "HEAD"],
        text=True,
        capture_output=True,
    )
    relation_note = None
    if relation.returncode != 0:
        relation_note = "recorded baseline is not an ancestor of candidate HEAD; changed-file review is still shown but requires extra care"

    diff = subprocess.run(
        ["git", "-C", str(source), "diff", "--name-only", baseline, "HEAD", "--"],
        check=True,
        text=True,
        capture_output=True,
    )
    return {line.strip() for line in diff.stdout.splitlines() if line.strip()}, relation_note


def print_source_status(source: Path, manifest: dict, strict_semantic: bool = False) -> None:
    if not (source / ".git").exists():
        raise SystemExit(f"candidate source is not a Git worktree: {source}")
    head = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    baseline = manifest["canonical_upstream"]["baseline_commit"]
    print(f"[codex-patchset] source HEAD: {head}")
    print(f"[codex-patchset] recorded baseline: {baseline}")
    if head == baseline:
        print("[codex-patchset] source matches recorded baseline")
    else:
        print("[codex-patchset] source differs from recorded baseline; semantic re-review is required")

    changed_files, changed_note = upstream_changed_files(source, baseline)
    if changed_note:
        print(f"[codex-patchset] upstream diff warning: {changed_note}")

    patch_items = sorted(manifest["patches"], key=lambda entry: int(entry["order"]))
    conflicts = 0
    semantic_review = []
    blocked = False

    # Use a disposable Git index so every patch is checked against the state
    # produced by all preceding patches without modifying the candidate tree.
    # This matters for dependent patches such as 0009, which expects 0004.
    with tempfile.TemporaryDirectory(prefix="codex-patchset-index-") as temp_dir:
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = str(Path(temp_dir) / "index")
        subprocess.run(
            ["git", "-C", str(source), "read-tree", "HEAD"],
            check=True,
            text=True,
            capture_output=True,
            env=env,
        )

        for item in patch_items:
            touched = []
            if changed_files is not None:
                touched = sorted(set(item.get("source_files") or []) & changed_files)
                if touched:
                    semantic_review.append((item, touched))

            if blocked:
                state = "blocked"
                detail = "an earlier patch failed, so this dependent sequential state is not meaningful"
            else:
                state, detail = patch_state(source, PATCH_ROOT / item["file"], env)
                if state == "conflict":
                    conflicts += 1
                    blocked = True

            print(f"{int(item['order']):04d}  {state:<10}  {item['file']}")
            if state in {"conflict", "blocked"} and detail:
                print(f"      {detail.splitlines()[0]}")
            if touched:
                print(f"      upstream changed: {', '.join(touched)}")

    if changed_files is None:
        print("[codex-patchset] semantic risk: UNKNOWN (baseline diff unavailable)")
    elif semantic_review:
        print(
            f"[codex-patchset] semantic risk: REVIEW REQUIRED "
            f"({len(semantic_review)} patch(es) touch files changed by upstream)"
        )
        for item, touched in semantic_review:
            print(
                f"      {int(item['order']):04d} {item['file']}: "
                f"{', '.join(touched)}"
            )
    else:
        print("[codex-patchset] semantic risk: LOW (no patch-owned source files changed upstream)")

    if conflicts:
        raise SystemExit(
            f"{conflicts} patch(es) require rebase/adaptation before this source can be used for an upgrade"
        )
    print("[codex-patchset] sequential replay: PASS")
    if changed_files is None:
        print("[codex-patchset] candidate build gate: PASS WITH UNKNOWN SEMANTIC DIFF")
        print("[codex-patchset] stable promotion gate: MANUAL REVIEW REQUIRED")
    elif semantic_review:
        print("[codex-patchset] candidate build gate: PASS")
        print("[codex-patchset] stable promotion gate: MANUAL REVIEW REQUIRED")
        if strict_semantic:
            raise SystemExit(2)
    else:
        print("[codex-patchset] candidate build gate: PASS")
        print("[codex-patchset] stable promotion gate: STATIC REVIEW PASS")


def apply_patchset(source: Path, manifest: dict, allow_dirty: bool) -> None:
    git_dir = source / ".git"
    if not git_dir.exists():
        raise SystemExit(f"candidate source is not a Git worktree: {source}")
    if not allow_dirty:
        status = subprocess.run(
            ["git", "-C", str(source), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if status:
            raise SystemExit(
                "candidate source is dirty; refusing to apply patches. "
                "Use a disposable clean upgrade worktree or pass --allow-dirty explicitly."
            )

    for item in sorted(manifest["patches"], key=lambda entry: int(entry["order"])):
        patch = PATCH_ROOT / item["file"]
        print(f"[codex-patchset] check {item['file']}")
        checked = subprocess.run(
            ["git", "-C", str(source), "apply", "--check", str(patch)],
            text=True,
            capture_output=True,
        )
        if checked.returncode != 0:
            detail = (checked.stderr or checked.stdout).strip()
            raise SystemExit(
                f"patch does not apply cleanly: {item['file']}\n{detail}\n"
                "Stop here and rebase/adapt this patch against the new upstream source; do not skip it blindly."
            )
        subprocess.run(
            ["git", "-C", str(source), "apply", str(patch)],
            check=True,
        )
        print(f"[codex-patchset] applied {item['file']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verify")
    sub.add_parser("list")
    status_parser = sub.add_parser("status")
    status_parser.add_argument("--source", required=True)
    status_parser.add_argument(
        "--strict-semantic",
        action="store_true",
        help="exit 2 when upstream changed any source file owned by a patch; useful for stable-promotion CI gates",
    )
    sync_parser = sub.add_parser("sync")
    sync_parser.add_argument("--dest", required=True)
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("--source", required=True)
    apply_parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args()

    manifest = verify()
    if args.command == "verify":
        print(
            f"[codex-patchset] OK: {len(manifest['patches'])} Codex patches, "
            f"baseline {manifest['canonical_upstream']['baseline_commit']}"
        )
    elif args.command == "list":
        print_list(manifest)
    elif args.command == "status":
        print_source_status(
            Path(args.source).expanduser().resolve(),
            manifest,
            strict_semantic=args.strict_semantic,
        )
    elif args.command == "sync":
        sync(Path(args.dest).expanduser().resolve(), manifest)
    elif args.command == "apply":
        apply_patchset(Path(args.source).expanduser().resolve(), manifest, args.allow_dirty)


if __name__ == "__main__":
    main()
