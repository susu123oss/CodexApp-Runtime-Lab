#!/usr/bin/env python3
"""Package and optionally publish an already-built local Codex iOS runtime.

Important: this script NEVER runs Cargo and NEVER dispatches GitHub Actions.
If the local CLI/host is unavailable or invalid it fails instead of rebuilding.
The DEB format is intentionally checked against CodexApp's extractor contract:
the payload member must be named ``data.tar.zst`` and contain a USTAR tree.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.util
import hashlib
import http.client
import json
import os
import plistlib
import re
import shutil
import subprocess
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PACKAGING_ROOT = ROOT / ".cache/codex-ios-runtime-src"
DEFAULT_BINARY = (
    DEFAULT_PACKAGING_ROOT
    / "build/ios-arm64/target-release-online/aarch64-apple-ios/release/codex"
)
DEFAULT_REPO = "susu123oss/CodexApp-Runtime"


def run(args: list[str], *, capture: bool = False, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        check=True,
        capture_output=capture,
        text=False,
        env=env,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_version(raw: str) -> str:
    version = raw.strip().lstrip("v")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}(?:-[0-9]+)?", version):
        raise SystemExit(f"invalid Codex Runtime version: {raw!r}")
    return version


def base_version(version: str) -> str:
    return version.split("-", 1)[0]


def shell_value(path: Path, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}=(.+)$", path.read_text(), re.M)
    if not match:
        raise SystemExit(f"missing {key} in {path}")
    return match.group(1).strip().strip("\"'")


def validate_packaging_root(packaging: Path, version: str) -> dict[str, str]:
    required = [
        packaging / "configuration/upstream.env",
        packaging / "configuration/version.txt",
        packaging / "packaging/DEBIAN/control",
        packaging / "packaging/codex.entitlements",
        packaging / "packaging/codex.launcher.c",
        packaging / "packaging/etc/codex/config.toml",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise SystemExit("missing packaging inputs:\n  " + "\n  ".join(missing))
    source_version = (packaging / "configuration/version.txt").read_text().strip().lstrip("v")
    if source_version != base_version(version):
        raise SystemExit(
            f"packaging source is Codex {source_version}, release is {version}; refusing version mix"
        )
    env_file = packaging / "configuration/upstream.env"
    return {
        "upstream_ref": shell_value(env_file, "UPSTREAM_REF"),
        "min_ios": shell_value(env_file, "MIN_IOS"),
    }


def validate_ios_arm64(path: Path, label: str, min_ios: str) -> None:
    if not path.is_file() or path.stat().st_size < 100 * 1024:
        raise SystemExit(f"{label} missing or unexpectedly small: {path}")
    build = run(["xcrun", "vtool", "-show-build", str(path)], capture=True).stdout.decode(
        "utf-8", "replace"
    )
    if not re.search(r"^\s*platform\s+(?:IOS|2)\s*$", build, re.M):
        raise SystemExit(f"{label} is not an iOS Mach-O: {path}")
    archs = run(["xcrun", "lipo", "-archs", str(path)], capture=True).stdout.decode().strip()
    if archs != "arm64":
        raise SystemExit(f"{label} architecture is {archs!r}; expected arm64")
    if label == "codex":
        match = re.search(r"^\s*minos\s+([0-9.]+)\s*$", build, re.M)
        if match:
            actual = tuple(int(part) for part in match.group(1).split("."))
            floor = tuple(int(part) for part in min_ios.split("."))
            if actual > floor:
                raise SystemExit(
                    f"codex minimum iOS {match.group(1)} exceeds packaging floor {min_ios}"
                )


def discover_host(packaging: Path, version: str, explicit: str) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    pattern = f"local-test-v{base_version(version)}-*/payload/codex-code-mode-host"
    candidates = [path for path in (packaging / "build").glob(pattern) if path.is_file()]
    if not candidates:
        raise SystemExit(
            f"no verified local host found for Codex {base_version(version)}; pass CODEX_RUNTIME_HOST/--host"
        )
    return max(candidates, key=lambda path: path.stat().st_mtime)


def host_ldid() -> Path:
    cached = ROOT / ".cache/ldid-host"
    if cached.is_file() and os.access(cached, os.X_OK):
        return cached
    candidate = shutil.which("ldid")
    if candidate:
        return Path(candidate)
    raise SystemExit(
        "host ldid unavailable. Provide/build .cache/ldid-host; iOS TheosRuntime/usr/bin/ldid is not a macOS executable."
    )


class InBuffer(ctypes.Structure):
    _fields_ = [("src", ctypes.c_void_p), ("size", ctypes.c_size_t), ("pos", ctypes.c_size_t)]


class OutBuffer(ctypes.Structure):
    _fields_ = [("dst", ctypes.c_void_p), ("size", ctypes.c_size_t), ("pos", ctypes.c_size_t)]


class HostZstd:
    def __init__(self) -> None:
        candidates = [
            ctypes.util.find_library("zstd"),
            "/usr/local/lib/libzstd.dylib",
            "/opt/homebrew/lib/libzstd.dylib",
        ]
        self.lib = None
        last_error = None
        for candidate in candidates:
            if not candidate:
                continue
            try:
                self.lib = ctypes.CDLL(candidate)
                break
            except OSError as exc:
                last_error = exc
        if self.lib is None:
            raise SystemExit(
                f"host libzstd unavailable ({last_error}); refusing to fall back to incompatible data.tar.gz"
            )
        self.lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
        self.lib.ZSTD_isError.restype = ctypes.c_uint
        self.lib.ZSTD_getErrorName.argtypes = [ctypes.c_size_t]
        self.lib.ZSTD_getErrorName.restype = ctypes.c_char_p
        self.lib.ZSTD_createCStream.restype = ctypes.c_void_p
        self.lib.ZSTD_freeCStream.argtypes = [ctypes.c_void_p]
        self.lib.ZSTD_initCStream.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.lib.ZSTD_initCStream.restype = ctypes.c_size_t
        self.lib.ZSTD_compressStream.argtypes = [ctypes.c_void_p, ctypes.POINTER(OutBuffer), ctypes.POINTER(InBuffer)]
        self.lib.ZSTD_compressStream.restype = ctypes.c_size_t
        self.lib.ZSTD_endStream.argtypes = [ctypes.c_void_p, ctypes.POINTER(OutBuffer)]
        self.lib.ZSTD_endStream.restype = ctypes.c_size_t
        self.lib.ZSTD_createDStream.restype = ctypes.c_void_p
        self.lib.ZSTD_freeDStream.argtypes = [ctypes.c_void_p]
        self.lib.ZSTD_initDStream.argtypes = [ctypes.c_void_p]
        self.lib.ZSTD_initDStream.restype = ctypes.c_size_t
        self.lib.ZSTD_decompressStream.argtypes = [ctypes.c_void_p, ctypes.POINTER(OutBuffer), ctypes.POINTER(InBuffer)]
        self.lib.ZSTD_decompressStream.restype = ctypes.c_size_t

    def check(self, code: int) -> int:
        if self.lib.ZSTD_isError(code):
            name = self.lib.ZSTD_getErrorName(code).decode("utf-8", "replace")
            raise RuntimeError(f"libzstd: {name}")
        return code

    def compress(self, source: Path, destination: Path, level: int = 9) -> None:
        stream = self.lib.ZSTD_createCStream()
        if not stream:
            raise RuntimeError("ZSTD_createCStream failed")
        try:
            self.check(self.lib.ZSTD_initCStream(stream, level))
            in_storage = ctypes.create_string_buffer(1024 * 1024)
            out_storage = ctypes.create_string_buffer(1024 * 1024)
            with source.open("rb") as src, destination.open("wb") as dst:
                for chunk in iter(lambda: src.read(len(in_storage)), b""):
                    ctypes.memmove(in_storage, chunk, len(chunk))
                    inp = InBuffer(ctypes.addressof(in_storage), len(chunk), 0)
                    while inp.pos < inp.size:
                        out = OutBuffer(ctypes.addressof(out_storage), len(out_storage), 0)
                        self.check(self.lib.ZSTD_compressStream(stream, ctypes.byref(out), ctypes.byref(inp)))
                        if out.pos:
                            dst.write(out_storage.raw[: out.pos])
                while True:
                    out = OutBuffer(ctypes.addressof(out_storage), len(out_storage), 0)
                    remaining = self.check(self.lib.ZSTD_endStream(stream, ctypes.byref(out)))
                    if out.pos:
                        dst.write(out_storage.raw[: out.pos])
                    if remaining == 0:
                        break
        finally:
            self.lib.ZSTD_freeCStream(stream)

    def decompress(self, source: Path, destination: Path) -> None:
        stream = self.lib.ZSTD_createDStream()
        if not stream:
            raise RuntimeError("ZSTD_createDStream failed")
        try:
            self.check(self.lib.ZSTD_initDStream(stream))
            in_storage = ctypes.create_string_buffer(1024 * 1024)
            out_storage = ctypes.create_string_buffer(1024 * 1024)
            last = 1
            with source.open("rb") as src, destination.open("wb") as dst:
                for chunk in iter(lambda: src.read(len(in_storage)), b""):
                    ctypes.memmove(in_storage, chunk, len(chunk))
                    inp = InBuffer(ctypes.addressof(in_storage), len(chunk), 0)
                    while inp.pos < inp.size:
                        out = OutBuffer(ctypes.addressof(out_storage), len(out_storage), 0)
                        last = self.check(self.lib.ZSTD_decompressStream(stream, ctypes.byref(out), ctypes.byref(inp)))
                        if out.pos:
                            dst.write(out_storage.raw[: out.pos])
            if last != 0:
                raise RuntimeError("zstd payload ended before frame completion")
        finally:
            self.lib.ZSTD_freeDStream(stream)


def sign_and_verify(path: Path, ldid: Path, entitlements: Path) -> None:
    run([str(ldid), "-S" + str(entitlements), "-Cadhoc", str(path)])
    parsed = plistlib.loads(run([str(ldid), "-e", str(path)], capture=True).stdout)
    for key in (
        "platform-application",
        "com.apple.private.security.no-sandbox",
        "com.apple.private.security.storage.AppBundles",
        "com.apple.private.security.storage.AppDataContainers",
        "com.apple.developer.kernel.extended-virtual-addressing",
    ):
        if parsed.get(key) is not True:
            raise RuntimeError(f"{path.name} missing entitlement: {key}")
    if parsed.get("com.apple.private.security.container-required") is not False:
        raise RuntimeError(f"{path.name} requires container-required=false")


def ar_header(name: str, size: int) -> bytes:
    name = (name + "/")[:16]
    return (
        f"{name:<16}{int(time.time()):<12}{0:<6}{0:<6}{0o100644:<8o}{size:<10}`\n"
    ).encode("ascii")


def append_member(output, name: str, *, path: Path | None = None, data: bytes | None = None) -> None:
    if (path is None) == (data is None):
        raise ValueError("exactly one of path/data is required")
    size = path.stat().st_size if path else len(data or b"")
    output.write(ar_header(name, size))
    if path:
        with path.open("rb") as stream:
            shutil.copyfileobj(stream, output, 1024 * 1024)
    else:
        output.write(data or b"")
    if size & 1:
        output.write(b"\n")


def ar_members(path: Path) -> dict[str, tuple[int, int]]:
    found: dict[str, tuple[int, int]] = {}
    with path.open("rb") as stream:
        if stream.read(8) != b"!<arch>\n":
            raise RuntimeError("invalid deb ar magic")
        while True:
            header = stream.read(60)
            if not header:
                break
            if len(header) != 60 or header[58:60] != b"`\n":
                raise RuntimeError("invalid deb ar header")
            name = header[:16].decode("ascii", "replace").strip().rstrip("/")
            size = int(header[48:58].decode("ascii").strip())
            offset = stream.tell()
            found[name] = (offset, size)
            stream.seek(size + (size & 1), os.SEEK_CUR)
    return found


def extract_ar_member(deb: Path, member: tuple[int, int], destination: Path) -> None:
    offset, size = member
    with deb.open("rb") as src, destination.open("wb") as dst:
        src.seek(offset)
        remaining = size
        while remaining:
            chunk = src.read(min(1024 * 1024, remaining))
            if not chunk:
                raise RuntimeError("short ar member")
            dst.write(chunk)
            remaining -= len(chunk)


def verify_prepared_runtime(deb: Path, sums: Path, verify_root: Path) -> str:
    if not deb.is_file() or not sums.is_file():
        raise SystemExit(f"prepared Codex Runtime assets are missing: {deb} / {sums}")
    digest = sha256_file(deb)
    expected_line = f"{digest}  {deb.name}"
    if expected_line not in sums.read_text().splitlines():
        raise RuntimeError("SHA256SUMS does not match prepared DEB")
    members = ar_members(deb)
    if "data.tar.zst" not in members:
        raise RuntimeError("CAArchiveExtractor preflight failed: missing data.tar.zst")
    if verify_root.exists():
        shutil.rmtree(verify_root)
    verify_root.mkdir(parents=True, exist_ok=True)
    verify_zst = verify_root / "data.tar.zst"
    verify_tar = verify_root / "data.tar"
    extract_ar_member(deb, members["data.tar.zst"], verify_zst)
    HostZstd().decompress(verify_zst, verify_tar)
    found_codex = found_host = False
    with tarfile.open(verify_tar, "r:") as archive:
        for member in archive.getmembers():
            leaf = Path(member.name).name
            if member.isfile() and leaf == "codex" and member.size > 5 * 1024 * 1024:
                if found_codex:
                    raise RuntimeError("duplicate codex in DEB")
                found_codex = True
            elif member.isfile() and leaf == "codex-code-mode-host" and member.size > 100 * 1024:
                if found_host:
                    raise RuntimeError("duplicate codex-code-mode-host in DEB")
                found_host = True
    if not found_codex or not found_host:
        raise RuntimeError("CAArchiveExtractor preflight failed: runtime executables missing")
    print("[codex-runtime] App extractor preflight: data.tar.zst OK")
    print(f"[codex-runtime] prepared SHA256: {digest}")
    return digest


def package_runtime(version: str, binary: Path, host: Path, packaging: Path, output: Path) -> tuple[Path, Path]:
    meta = validate_packaging_root(packaging, version)
    validate_ios_arm64(binary, "codex", meta["min_ios"])
    validate_ios_arm64(host, "codex-code-mode-host", meta["min_ios"])
    ldid = host_ldid()
    zstd = HostZstd()

    work = output / "work"
    if work.exists():
        shutil.rmtree(work)
    payload_root = work / "staging/var/jb"
    libexec = payload_root / "usr/libexec/codex"
    bin_dir = payload_root / "usr/bin"
    config_dir = payload_root / "etc/codex"
    debian = work / "DEBIAN"
    for path in (libexec, bin_dir, config_dir, debian):
        path.mkdir(parents=True, exist_ok=True)

    shutil.copy2(binary, libexec / "codex")
    shutil.copy2(host, libexec / "codex-code-mode-host")
    run(["xcrun", "--sdk", "iphoneos", "strip", "-xS", str(libexec / "codex")])
    sdk = subprocess.check_output(["xcrun", "--sdk", "iphoneos", "--show-sdk-path"], text=True).strip()
    launcher = bin_dir / "codex"
    run([
        "xcrun", "clang", "-target", f"arm64-apple-ios{meta['min_ios']}", "-isysroot", sdk,
        "-Os", "-fvisibility=hidden", '-DOG_PROGRAM="codex"', '-DOG_STATIC_PREFIX="/var/jb"',
        str(packaging / "packaging/codex.launcher.c"), "-Wl,-dead_strip", "-o", str(launcher),
    ])
    shutil.copy2(packaging / "packaging/etc/codex/config.toml", config_dir / "config.toml")

    entitlements = packaging / "packaging/codex.entitlements"
    for path in (launcher, libexec / "codex", libexec / "codex-code-mode-host"):
        os.chmod(path, 0o755)
        sign_and_verify(path, ldid, entitlements)

    installed_bytes = sum(path.stat().st_size for path in payload_root.rglob("*") if path.is_file())
    control = (packaging / "packaging/DEBIAN/control").read_text()
    replacements = {
        "@PACKAGE_ID@": "wiki.qaq.codex",
        "@VERSION@": version,
        "@ARCHITECTURE@": "iphoneos-arm64",
        "@INSTALLED_SIZE@": str((installed_bytes + 1023) // 1024),
        "@MIN_IOS@": meta["min_ios"],
        "@UPSTREAM@": f"codex@{meta['upstream_ref'][:12]}",
    }
    for key, value in replacements.items():
        control = control.replace(key, value)
    if re.search(r"@[A-Z_]+@", control):
        raise RuntimeError("control file still contains placeholders")
    (debian / "control").write_text(control)

    control_tar = work / "control.tar.gz"
    data_tar = work / "data.tar"
    data_zst = work / "data.tar.zst"
    with tarfile.open(control_tar, "w:gz", compresslevel=6) as archive:
        archive.add(debian / "control", arcname="./control")
    with tarfile.open(data_tar, "w", format=tarfile.USTAR_FORMAT) as archive:
        archive.add(work / "staging/var", arcname="./var")
    zstd.compress(data_tar, data_zst)

    deb = output / f"wiki.qaq.codex_{version}_iphoneos-arm64.deb"
    with deb.open("wb") as result:
        result.write(b"!<arch>\n")
        append_member(result, "debian-binary", data=b"2.0\n")
        append_member(result, "control.tar.gz", path=control_tar)
        append_member(result, "data.tar.zst", path=data_zst)

    digest = sha256_file(deb)
    sums = output / "SHA256SUMS"
    sums.write_text(f"{digest}  {deb.name}\n")
    verify_prepared_runtime(deb, sums, work / "app-extractor-preflight")
    print(f"[codex-runtime] local CLI: {binary}")
    print(f"[codex-runtime] local host: {host}")
    print(f"[codex-runtime] DEB: {deb} ({deb.stat().st_size} bytes)")
    print(f"[codex-runtime] SHA256: {digest}")
    return deb, sums


def github_token() -> str:
    proc = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        text=True,
        capture_output=True,
        check=True,
    )
    values = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    token = values.get("password", "")
    if not token:
        raise SystemExit("Git credential helper returned no GitHub token")
    return token


def request_json(url: str, token: str, *, method: str = "GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "CodexApp-Runtime-Publisher",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        if exc.code == 404:
            return 404, None
        raise RuntimeError(f"GitHub API {method} failed: HTTP {exc.code}: {raw}") from exc


def upload_asset(repo: str, release_id: int, path: Path, token: str) -> None:
    query = urllib.parse.urlencode({"name": path.name})
    connection = http.client.HTTPSConnection("uploads.github.com", timeout=600)
    try:
        connection.putrequest("POST", f"/repos/{repo}/releases/{release_id}/assets?{query}")
        connection.putheader("Accept", "application/vnd.github+json")
        connection.putheader("Authorization", f"Bearer {token}")
        connection.putheader("User-Agent", "CodexApp-Runtime-Publisher")
        connection.putheader("Content-Type", "application/octet-stream")
        connection.putheader("Content-Length", str(path.stat().st_size))
        connection.endheaders()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                connection.send(chunk)
        response = connection.getresponse()
        raw = response.read()
        if response.status not in (200, 201):
            raise RuntimeError(f"upload {path.name}: HTTP {response.status}: {raw.decode('utf-8', 'replace')}")
        uploaded = json.loads(raw)
        if int(uploaded.get("size", 0)) != path.stat().st_size:
            raise RuntimeError(f"uploaded size mismatch: {path.name}")
    finally:
        connection.close()
    print(f"[codex-runtime] uploaded {path.name} ({path.stat().st_size} bytes)")


def update_runtime_manifest(repo: str, version: str, deb: Path, target: str, token: str) -> None:
    """Publish the small manifest consumed by in-app update checks.

    This happens only after the release assets have been uploaded and verified.
    It intentionally uses the authenticated Contents API: normal app clients
    later read the raw file anonymously, avoiding GitHub Releases API quota.
    """
    api = f"https://api.github.com/repos/{repo}"
    manifest_path = "Runtime/runtime-manifest.json"
    encoded_path = urllib.parse.quote(manifest_path, safe="/")
    ref = urllib.parse.quote(target, safe="")
    status, remote = request_json(f"{api}/contents/{encoded_path}?ref={ref}", token)
    if status != 200 or not isinstance(remote, dict):
        raise RuntimeError(f"runtime manifest not found on {repo}@{target}: {manifest_path}")
    encoded = str(remote.get("content", "")).replace("\n", "")
    try:
        manifest = json.loads(base64.b64decode(encoded).decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("remote runtime manifest is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict):
        raise RuntimeError("remote runtime manifest root is not an object")
    runtimes = manifest.setdefault("runtimes", {})
    if not isinstance(runtimes, dict):
        raise RuntimeError("remote runtime manifest runtimes is not an object")

    tag = f"runtime-codex-v{version}"
    previous = runtimes.get("codex") if isinstance(runtimes.get("codex"), dict) else {}
    codex_entry = dict(previous)
    codex_entry.update({
        "archive_sha256": sha256_file(deb),
        "archive_url": f"https://github.com/{repo}/releases/download/{tag}/{deb.name}",
        "asset_name": deb.name,
        "download_size": deb.stat().st_size,
        "minimum_ios": "15.0",
        "release_tag": tag,
        "runtime_format": 1,
        "version": f"v{version}",
    })
    runtimes["codex"] = codex_entry
    manifest["schema"] = max(3, int(manifest.get("schema", 1) or 1))
    rendered = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode("utf-8")

    # Keep the application repository's copy aligned with the release metadata
    # as part of the same explicit local publication flow.
    local_manifest = ROOT / manifest_path
    local_manifest.parent.mkdir(parents=True, exist_ok=True)
    local_manifest.write_bytes(rendered)

    payload = {
        "message": f"Update Codex runtime manifest to v{version}",
        "content": base64.b64encode(rendered).decode("ascii"),
        "sha": remote["sha"],
        "branch": target,
    }
    request_json(f"{api}/contents/{encoded_path}", token, method="PUT", payload=payload)
    _, verify = request_json(f"{api}/contents/{encoded_path}?ref={ref}", token)
    verify_encoded = str((verify or {}).get("content", "")).replace("\n", "")
    verified = json.loads(base64.b64decode(verify_encoded).decode("utf-8"))
    codex = verified.get("runtimes", {}).get("codex", {})
    if codex.get("version") != f"v{version}" or codex.get("archive_sha256") != sha256_file(deb):
        raise RuntimeError("runtime manifest verification failed after upload")
    print(f"[codex-runtime] updated {manifest_path} -> v{version}")


def publish_runtime(repo: str, version: str, assets: list[Path], target: str) -> str:
    token = github_token()
    tag = f"runtime-codex-v{version}"
    api = f"https://api.github.com/repos/{repo}"
    encoded = urllib.parse.quote(tag, safe="")
    status, release = request_json(f"{api}/releases/tags/{encoded}", token)
    notes = (
        f"CodexApp iOS runtime package revision v{version}.\n\n"
        f"OpenAI Codex version: v{base_version(version)}\n"
        "Packaged from an already-built local iOS arm64 Codex CLI.\n"
        "The release path does not invoke Cargo or GitHub Actions.\n"
        "The DEB is preflighted against CodexApp's data.tar.zst extractor contract.\n"
    )
    payload = {
        "name": f"Codex Runtime v{version}",
        "body": notes,
        "draft": False,
        "prerelease": True,
    }
    if status == 404:
        payload.update({"tag_name": tag, "target_commitish": target})
        _, release = request_json(f"{api}/releases", token, method="POST", payload=payload)
        print(f"[codex-runtime] created release {tag}")
    else:
        _, release = request_json(
            f"{api}/releases/{release['id']}", token, method="PATCH", payload=payload
        )
        print(f"[codex-runtime] updating release {tag}")
    release_id = int(release["id"])
    _, existing = request_json(f"{api}/releases/{release_id}/assets?per_page=100", token)
    by_name = {asset["name"]: asset for asset in existing or []}
    for path in assets:
        old = by_name.get(path.name)
        if old:
            request_json(f"{api}/releases/assets/{old['id']}", token, method="DELETE")
        upload_asset(repo, release_id, path, token)
    _, verify = request_json(f"{api}/releases/tags/{encoded}", token)
    uploaded = {asset["name"]: int(asset.get("size", 0)) for asset in verify.get("assets", [])}
    for path in assets:
        if uploaded.get(path.name) != path.stat().st_size:
            raise RuntimeError(f"release verification failed: {path.name}")
    deb = next((path for path in assets if path.suffix == ".deb"), None)
    if deb is None:
        raise RuntimeError("release has no DEB asset for runtime manifest")
    update_runtime_manifest(repo, version, deb, target, token)
    print(f"[codex-runtime] verified release {verify['html_url']}")
    return verify["html_url"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True, help="package revision, e.g. 0.162.1-3")
    parser.add_argument("--binary", default=str(DEFAULT_BINARY))
    parser.add_argument("--host", default="")
    parser.add_argument("--packaging-root", default=str(DEFAULT_PACKAGING_ROOT))
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--target", default="main")
    parser.add_argument("--output-root", default=str(ROOT / ".cache/codex-runtime-release"))
    parser.add_argument("--publish", action="store_true", help="replace/create the GitHub prerelease after local preflight")
    parser.add_argument(
        "--reuse-prepared",
        action="store_true",
        help="publish the already prepared version-specific DEB without repackaging it",
    )
    args = parser.parse_args()

    version = normalize_version(args.version)
    output = Path(args.output_root).expanduser().resolve() / version
    output.mkdir(parents=True, exist_ok=True)
    if args.reuse_prepared:
        deb = output / f"wiki.qaq.codex_{version}_iphoneos-arm64.deb"
        sums = output / "SHA256SUMS"
        verify_prepared_runtime(deb, sums, output / "reuse-preflight")
        print(f"[codex-runtime] reusing prepared DEB: {deb}")
    else:
        packaging = Path(args.packaging_root).expanduser().resolve()
        binary = Path(args.binary).expanduser().resolve()
        host = discover_host(packaging, version, args.host)
        if not binary.is_file():
            raise SystemExit(
                f"local Codex binary not found: {binary}\n"
                "Refusing to rebuild automatically. Build Codex explicitly first, then rerun this release command."
            )
        deb, sums = package_runtime(version, binary, host, packaging, output)
    if args.publish:
        publish_runtime(args.repo, version, [deb, sums], args.target)
    else:
        print("[codex-runtime] prepare-only complete; no GitHub release was modified")


if __name__ == "__main__":
    main()
