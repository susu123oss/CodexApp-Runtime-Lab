#!/usr/bin/env python3
from __future__ import annotations

import gzip
import hashlib
import json
import os
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "NodeRuntime"
ARCHIVE = ROOT / "NodeRuntime.tar.gz"
METADATA = ROOT / "NodeRuntime.bundle.json"
FORMAT = 1


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def normalized(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    return info


def main() -> None:
    node = STAGE / "bin" / "node"
    info_path = STAGE / ".build-info.json"
    if not node.is_file() or not os.access(node, os.X_OK) or not info_path.is_file():
        raise SystemExit("NodeRuntime 未构建；先运行 scripts/build-node-runtime.py")
    info = json.loads(info_path.read_text(encoding="utf-8"))
    required = [
        "bin/node", "bin/npm", "bin/npx", "bin/corepack", "bin/pnpm", "bin/pnpx", "bin/yarn", "bin/yarnpkg", "bin/node-gyp",
        "lib/node_modules/npm/bin/npm-cli.js", "lib/node_modules/npm/bin/npx-cli.js",
        "lib/node_modules/corepack/dist/corepack.js", "lib/node_modules/corepack/dist/pnpm.js",
        "lib/node_modules/corepack/dist/yarn.js",
        "lib/codex/ios-bootstrap.js", "lib/codex/native-support.js", "lib/codex/fetch-compat.js", "lib/codex/node-gyp-ios.js",
        "devkit/node/include/node/node.h", "devkit/node/include/node/node_api.h",
        "devkit/node/include/node/common.gypi", "devkit/node/include/node/config.gypi",
    ]
    sign_paths = ["bin/node"]
    sign_paths_without_entitlements = []
    if info.get("framework_dynamic"):
        required.append("Frameworks/NodeMobile.framework/NodeMobile")
        # Keep the framework in legacy sign_paths so older App builds do not
        # start skipping it entirely. New installers subtract the no-entitlement
        # group from this list and sign the framework exactly once, correctly.
        sign_paths.append("Frameworks/NodeMobile.framework/NodeMobile")
        # The host App's privileged entitlements belong to the Node launcher,
        # not to a dynamically loaded framework. Python Runtime uses the same
        # split: CoreTrust-sign the framework without injecting App entitlements.
        sign_paths_without_entitlements.append("Frameworks/NodeMobile.framework/NodeMobile")
    for rel in required:
        if not (STAGE / rel).exists():
            raise SystemExit(f"NodeRuntime 缺少 {rel}")

    install = {
        "required_paths": required,
        "sign_paths": sign_paths,
        "sign_paths_without_entitlements": sign_paths_without_entitlements,
        "path_entries": ["bin"],
        "persistent_path_entries": ["Library/Application Support/CodexApp/Node/prefix/bin"],
        "environment": {"CA_TOOL_NODE_RUNTIME_ROOT": "${RUNTIME_ROOT}"},
        "probe": {
            "executable": "bin/node",
            "arguments": [
                "-e",
                (
                    "const p=require('./lib/node_modules/npm/package.json');"
                    "const c=require('./lib/node_modules/corepack/package.json');"
                    f"if(p.version!=='{info['npm_version']}'||c.version!=='{info['corepack_version']}')process.exit(43);"
                    "console.log('codex-node-runtime-ok '+process.version+' npm='+p.version+' corepack='+c.version)"
                ),
            ],
            "contains": "codex-node-runtime-ok",
        },
    }
    manifest = {
        "format": FORMAT,
        "runtime_id": "node",
        "minimum_ios": "15.0",
        "components": {
            "node": info["node_version"], "npm": info["npm_version"], "npx": info["npm_version"],
            "corepack": info["corepack_version"], "pnpm": "via-corepack", "yarn": "via-corepack",
            "node-gyp": info.get("node_gyp_version", "10.1.0"), "native_addons": "ios-arm64-napi",
            "fetch": "minipass-fetch-global",
        },
        "source_revision": info["source_revision"],
        "install": install,
    }
    (STAGE / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    entries = sorted((p for p in STAGE.rglob("*") if p.name != ".build-info.json"), key=lambda p: str(p.relative_to(STAGE)))
    with ARCHIVE.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT, dereference=False) as out:
                for path in entries:
                    out.add(path, arcname=str(path.relative_to(STAGE)), recursive=False, filter=normalized)
    # CAArchiveExtractor counts every 512-byte TAR header and each file's
    # 512-byte padding while enforcing expectedExpandedSize. npm contains
    # thousands of small files, so payload bytes alone undercount the TAR
    # stream by >1 MiB and cause an otherwise valid installation to fail.
    # The extractor stops at the first all-zero TAR trailer block.
    payload_bytes = sum(p.stat().st_size for p in entries if p.is_file() and not p.is_symlink())
    tar_bytes = 512 + sum(
        512 + ((p.stat().st_size + 511) // 512) * 512
        if p.is_file() and not p.is_symlink() else 512
        for p in entries
    )
    archive_sha = sha256_file(ARCHIVE)
    revision = hashlib.sha256(json.dumps({
        "archive_sha256": archive_sha, "format": FORMAT, "runtime_id": "node",
        "components": manifest["components"], "source_revision": info["source_revision"],
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    metadata = {
        "format": FORMAT, "runtime_id": "node", "minimum_ios": "15.0",
        "archive_sha256": archive_sha, "archive_bytes": ARCHIVE.stat().st_size,
        "tar_bytes": tar_bytes, "payload_bytes": payload_bytes,
        "revision": revision, "file_count": len(entries),
        "node_version": info["node_version"], "npm_version": info["npm_version"],
        "corepack_version": info["corepack_version"], "source_revision": info["source_revision"],
        "components": manifest["components"], "install": install,
    }
    METADATA.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"[node-runtime] archive={ARCHIVE.stat().st_size} tar={tar_bytes} payload={payload_bytes} revision={revision[:12]}")


if __name__ == "__main__":
    main()
