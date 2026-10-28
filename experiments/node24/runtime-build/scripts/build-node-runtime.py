#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import posixpath
import shutil
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache"
RECIPE = ROOT / "Runtime" / "Recipes" / "node.json"
STAGE = ROOT / "NodeRuntime"


def run(args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    print("[node-runtime] $", " ".join(args), flush=True)
    subprocess.run(args, cwd=cwd, env=env, check=True)


def output(args: list[str], *, cwd: Path | None = None) -> str:
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def safe_extract(archive: Path, destination: Path) -> Path:
    with tarfile.open(archive, "r:gz") as tf:
        members = tf.getmembers()
        roots = set()
        for member in members:
            parts = Path(member.name).parts
            if not parts or member.name.startswith("/") or ".." in parts:
                raise SystemExit(f"unsafe source archive member: {member.name}")
            roots.add(parts[0])
        if len(roots) != 1:
            raise SystemExit(f"unexpected source archive roots: {sorted(roots)}")
        root = next(iter(roots))
        root_prefix = root + "/"
        for member in members:
            if member.issym():
                if member.linkname.startswith("/"):
                    raise SystemExit(f"unsafe absolute source symlink: {member.name} -> {member.linkname}")
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(member.name), member.linkname))
                if resolved != root and not resolved.startswith(root_prefix):
                    raise SystemExit(f"source symlink escapes root: {member.name} -> {member.linkname}")
            elif member.islnk():
                if member.linkname.startswith("/"):
                    raise SystemExit(f"unsafe absolute source hardlink: {member.name} -> {member.linkname}")
                resolved = posixpath.normpath(member.linkname)
                if resolved != root and not resolved.startswith(root_prefix):
                    raise SystemExit(f"source hardlink escapes root: {member.name} -> {member.linkname}")
        tf.extractall(destination)
        return destination / root


def github_repo_slug(repository: str) -> str:
    value = repository.strip()
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    if value.endswith(".git"):
        value = value[:-4]
    if value.count("/") != 1 or not all(value.split("/")):
        raise SystemExit(f"unsupported nodejs-mobile repository URL: {repository}")
    return value


def ensure_source(repository: str, commit: str, expected_node: str, expected_npm: str, expected_corepack: str, clean: bool) -> Path:
    source = CACHE / f"nodejs-mobile-{commit[:12]}"
    if clean and source.exists():
        shutil.rmtree(source)
    if not source.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        archive = CACHE / f"nodejs-mobile-{commit}.tar.gz"
        if not archive.is_file() or not tarfile.is_tarfile(archive):
            repo_slug = github_repo_slug(repository)
            urls = [
                f"https://codeload.github.com/{repo_slug}/tar.gz/{commit}",
                f"https://gh-proxy.org/https://github.com/{repo_slug}/archive/{commit}.tar.gz",
                f"https://ghproxy.net/https://github.com/{repo_slug}/archive/{commit}.tar.gz",
            ]
            ok = False
            for index, url in enumerate(urls, 1):
                tmp = archive.with_suffix(archive.suffix + f".part{index}")
                resume = tmp.is_file() and tmp.stat().st_size > 0
                print(
                    f"[node-runtime] source {index}/{len(urls)}: {url}"
                    + (f" (resume={tmp.stat().st_size} bytes)" if resume else ""),
                    flush=True,
                )
                proc = subprocess.run([
                    "/usr/bin/curl", "--http1.1", "-L", "--fail", "--retry", "2", "--retry-all-errors",
                    "--connect-timeout", "15", "--speed-time", "45", "--speed-limit", "1024",
                    *(["-C", "-"] if resume else []),
                    "-o", str(tmp), url,
                ])
                if proc.returncode == 0 and tmp.is_file() and tarfile.is_tarfile(tmp):
                    tmp.replace(archive)
                    ok = True
                    break
                tmp.unlink(missing_ok=True)
            if not ok:
                raise SystemExit("nodejs-mobile fixed source archive download failed")
        extract_root = CACHE / f".nodejs-mobile-extract-{commit[:12]}"
        if extract_root.exists():
            shutil.rmtree(extract_root)
        extract_root.mkdir(parents=True)
        extracted = safe_extract(archive, extract_root)
        extracted.replace(source)
        shutil.rmtree(extract_root, ignore_errors=True)

    version_h = (source / "src" / "node_version.h").read_text(encoding="utf-8", errors="replace")
    expected_macros = {
        "NODE_MAJOR_VERSION": expected_node.split(".")[0],
        "NODE_MINOR_VERSION": expected_node.split(".")[1],
        "NODE_PATCH_VERSION": expected_node.split(".")[2],
    }
    for macro, value in expected_macros.items():
        if f"#define {macro} {value}" not in version_h:
            raise SystemExit(f"source version mismatch: expected {macro}={value}")
    npm = json.loads((source / "deps" / "npm" / "package.json").read_text(encoding="utf-8"))["version"]
    corepack = json.loads((source / "deps" / "corepack" / "package.json").read_text(encoding="utf-8"))["version"]
    if npm != expected_npm or corepack != expected_corepack:
        raise SystemExit(f"package manager version mismatch: npm={npm} corepack={corepack}")
    print(f"[node-runtime] source verified commit={commit[:12]} node={expected_node} npm={npm} corepack={corepack}", flush=True)
    return source


def install_intel_ios_host_arch_fix(source: Path, helper_text: str) -> str:
    """Fix Node 22+ host generator architecture without touching the iOS target."""
    if platform.machine().lower() not in {"x86_64", "amd64"}:
        return helper_text
    node_gyp = source / "node.gyp"
    if not node_gyp.is_file() or "'target_name': 'node_js2c'" not in node_gyp.read_text(encoding="utf-8"):
        return helper_text

    fixer = source / "tools" / "codex_fix_intel_host_arch.py"
    fixer.write_text(
        """#!/usr/bin/env python3
from pathlib import Path
import re
import sys

root = Path(sys.argv[1])
count = 0
pattern = re.compile(r"(\\t-arch \\\\\n\\t)(?:arm64|i386)( \\\\\n)")
for path in root.rglob("*.host.mk"):
    value = path.read_text(encoding="utf-8")
    fixed, replacements = pattern.subn(r"\\1x86_64\\2", value)
    if replacements:
        path.write_text(fixed, encoding="utf-8")
        count += replacements
if count == 0:
    raise SystemExit("no host architecture flags patched")
print(f"[node-runtime] patched {count} host -arch flags to x86_64")
""",
        encoding="utf-8",
    )
    fixer.chmod(0o755)
    # Keep the generated helper deliberately simple: host Makefiles are the
    # only files touched, and only the token immediately following `-arch` is
    # rewritten. This avoids changing V8 target-architecture defines.
    fixer.write_text(
        """#!/usr/bin/env python3
from pathlib import Path
import sys

root = Path(sys.argv[1])
count = 0
for path in root.rglob("*.host.mk"):
    value = path.read_text(encoding="utf-8")
    lines = value.splitlines(keepends=True)
    replacements = 0
    for index in range(1, len(lines)):
        if not lines[index - 1].strip().startswith("-arch"):
            continue
        fields = lines[index].strip().split()
        token = fields[0] if fields else ""
        if token not in {"arm64", "i386"}:
            continue
        lines[index] = lines[index].replace(token, "x86_64", 1)
        replacements += 1
    if replacements:
        path.write_text("".join(lines), encoding="utf-8")
        count += replacements
if count == 0:
    raise SystemExit("no host architecture flags patched")
print(f"[node-runtime] patched {count} host -arch flags to x86_64")
""",
        encoding="utf-8",
    )
    fixer.chmod(0o755)

    marker = "python3 tools/codex_fix_intel_host_arch.py out"
    if marker in helper_text:
        return helper_text
    needle = '    --without-node-snapshot\n  make -j"${NODE_RUNTIME_JOBS:-4}"'
    if needle not in helper_text:
        raise SystemExit("nodejs-mobile iOS arm64 build stanza changed; cannot install Intel host fix")
    replacement = '    --without-node-snapshot\n  python3 tools/codex_fix_intel_host_arch.py out\n  make -j"${NODE_RUNTIME_JOBS:-4}"'
    print("[node-runtime] enabling Intel macOS host-tool architecture fix", flush=True)
    return helper_text.replace(needle, replacement, 1)


def framework_binary(framework: Path) -> Path:
    path = framework / "NodeMobile"
    if not path.is_file():
        raise SystemExit(f"missing NodeMobile framework binary: {path}")
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--reuse-framework", action="store_true", help="reuse an existing NodeMobile.framework and only rebuild the launcher/package")
    parser.add_argument("--jobs", type=int, default=max(1, min(4, os.cpu_count() or 2)))
    args = parser.parse_args()

    recipe = json.loads(RECIPE.read_text(encoding="utf-8"))
    source_info = recipe["source"]["nodejs_mobile"]
    commit = source_info["commit"]
    source = ensure_source(
        source_info["repository"],
        commit,
        source_info["node_version"],
        source_info["npm_version"],
        source_info["corepack_version"],
        args.clean,
    )
    # Upstream's iOS helper otherwise consumes every host CPU. Keep it bounded
    # like the other Codex-MT Runtime builds so local development stays usable.
    helper = source / "tools" / "ios_framework_prepare.sh"
    text = helper.read_text(encoding="utf-8")
    text = text.replace("make -j$(getconf _NPROCESSORS_ONLN)", 'make -j"${NODE_RUNTIME_JOBS:-4}"')
    text = install_intel_ios_host_arch_fix(source, text)
    helper.write_text(text, encoding="utf-8")
    helper.chmod(0o755)
    env = os.environ.copy()
    env["NODE_RUNTIME_JOBS"] = str(max(1, args.jobs))
    framework = source / "out_ios_arm64" / "iphoneos-arm64" / "Release-iphoneos" / "NodeMobile.framework"
    if not args.reuse_framework or not (framework / "NodeMobile").is_file():
        run(["bash", "tools/ios_framework_prepare.sh", "arm64"], cwd=source, env=env)
    fw_binary = framework_binary(framework)
    file_desc = output(["file", str(fw_binary)])
    dynamic = "dynamically linked shared library" in file_desc
    static = "current ar archive" in file_desc
    if not dynamic and not static:
        raise SystemExit(f"unsupported NodeMobile.framework linkage: {file_desc}")

    if STAGE.exists():
        shutil.rmtree(STAGE)
    (STAGE / "bin").mkdir(parents=True)
    (STAGE / "lib" / "node_modules").mkdir(parents=True)
    if dynamic:
        shutil.copytree(framework, STAGE / "Frameworks" / "NodeMobile.framework", symlinks=True)

    launcher = ROOT / "scripts" / "node-runtime-main.cpp"
    node = STAGE / "bin" / "node"
    cmd = [
        "xcrun", "--sdk", "iphoneos", "clang++", "-std=c++17", "-O2", "-arch", "arm64",
        "-miphoneos-version-min=15.0", str(launcher), "-F" + str(framework.parent),
        "-framework", "NodeMobile", "-framework", "CoreFoundation",
        "-Wl,-rpath,@executable_path/../Frameworks", "-o", str(node),
    ]
    run(cmd)
    node.chmod(0o755)
    arch = output(["file", str(node)])
    if "arm64" not in arch or "Mach-O" not in arch:
        raise SystemExit(f"unexpected Node executable: {arch}")

    for name in ("npm", "npx", "corepack", "pnpm", "pnpx", "yarn", "yarnpkg", "node-gyp"):
        (STAGE / "bin" / name).symlink_to("node")
    shutil.copytree(source / "deps" / "npm", STAGE / "lib" / "node_modules" / "npm", symlinks=True)
    shutil.copytree(source / "deps" / "corepack", STAGE / "lib" / "node_modules" / "corepack", symlinks=True)
    # node-gyp's bundled GYP only maps Python sys.platform=darwin to the Apple
    # make generator flavor. The managed Python runtime reports iOS as "ios",
    # and without this mapping GYP emits ELF/Linux linker flags (-soname,
    # --start-group) that ld64 rejects. Keep OS=ios from config.gypi while using
    # the mac/Apple linker command templates (-bundle, dynamic_lookup).
    gyp_common = STAGE / "lib" / "node_modules" / "npm" / "node_modules" / "node-gyp" / "gyp" / "pylib" / "gyp" / "common.py"
    gyp_text = gyp_common.read_text(encoding="utf-8")
    flavor_before = '    flavors = {\n        "cygwin": "win",\n        "win32": "win",\n        "darwin": "mac",\n    }\n'
    flavor_after = '    flavors = {\n        "cygwin": "win",\n        "win32": "win",\n        "darwin": "mac",\n        "ios": "mac",\n    }\n'
    if flavor_after not in gyp_text:
        if flavor_before not in gyp_text:
            raise SystemExit("node-gyp GYP GetFlavor table changed; cannot add iOS linker compatibility")
        gyp_text = gyp_text.replace(flavor_before, flavor_after, 1)
        gyp_common.write_text(gyp_text, encoding="utf-8")
    codex_lib = STAGE / "lib" / "codex"
    codex_lib.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "node-ios-bootstrap.js", codex_lib / "ios-bootstrap.js")
    shutil.copy2(ROOT / "scripts" / "node-native-support.js", codex_lib / "native-support.js")
    shutil.copy2(ROOT / "scripts" / "node-fetch-compat.js", codex_lib / "fetch-compat.js")
    shutil.copy2(ROOT / "scripts" / "node-gyp-ios-wrapper.js", codex_lib / "node-gyp-ios.js")

    # Reuse nodejs-mobile's own header packer so the devkit exactly matches the
    # Node/V8/libuv/OpenSSL sources that produced NodeMobile.framework.  node-gyp
    # can then build offline against this runtime instead of downloading desktop
    # Node headers that are wrong for iOS.
    run(["bash", "tools/copy_libnode_headers.sh", "ios"], cwd=source, env=env)
    devkit_source = source / "out_ios" / "libnode"
    devkit = STAGE / "devkit" / "node"
    if not (devkit_source / "include" / "node" / "node.h").is_file():
        raise SystemExit(f"nodejs-mobile devkit headers missing: {devkit_source}")
    shutil.copytree(devkit_source, devkit, symlinks=True)

    node_gyp_version = json.loads((source / "deps" / "npm" / "node_modules" / "node-gyp" / "package.json").read_text(encoding="utf-8"))["version"]

    build_info = {
        "source_revision": commit,
        "node_version": source_info["node_version"],
        "npm_version": source_info["npm_version"],
        "corepack_version": source_info["corepack_version"],
        "node_gyp_version": node_gyp_version,
        "framework_dynamic": dynamic,
        "framework_file": file_desc,
    }
    (STAGE / ".build-info.json").write_text(json.dumps(build_info, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"[node-runtime] staged {STAGE} node={source_info['node_version']} npm={source_info['npm_version']} dynamic_framework={dynamic}")


if __name__ == "__main__":
    main()
