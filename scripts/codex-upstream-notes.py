#!/usr/bin/env python3
"""Generate a release-to-release OpenAI Codex CLI changelog for Lab releases."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


API_BASE = "https://api.github.com/repos/openai/codex"
TAG_RE = re.compile(r"^rust-v(\d+)\.(\d+)\.(\d+)$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_RE = re.compile(r"v?(\d+\.\d+\.\d+)-\d+")
CATEGORY_NAMES = {
    "new features": "新增功能与能力",
    "features": "新增功能与能力",
    "bug fixes": "问题修复与稳定性",
    "fixes": "问题修复与稳定性",
    "security": "安全更新",
    "performance": "性能优化",
    "documentation": "文档更新",
    "docs": "文档更新",
    "chores": "工程维护",
    "maintenance": "工程维护",
}
CATEGORY_ORDER = ("新增功能与能力", "问题修复与稳定性", "安全更新", "性能优化", "文档更新", "工程维护", "其他上游改进")
TICK = chr(96)
PROTECTED_RE = re.compile(
    re.escape(TICK) + r"[^" + re.escape(TICK) + r"]+" + re.escape(TICK) +
    r"|https?://[^\s)]+|(?<!\w)#[0-9]+|"
    r"(?<![A-Za-z0-9])amazon-bedrock(?:-runtime)?(?![A-Za-z0-9])|"
    r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b"
)


def api_json(url: str, timeout: int = 30, allow_404: bool = False):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "CodexApp-Laboratory-Changelog",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip() or os.environ.get("GH_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    last_error = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404 and allow_404:
                return None
            if error.code not in (429, 500, 502, 503, 504) or attempt == 3:
                detail = error.read().decode("utf-8", errors="replace")[:300]
                raise RuntimeError(f"GitHub API request failed: HTTP {error.code}: {detail}") from error
            last_error = error
        except (urllib.error.URLError, TimeoutError) as error:
            last_error = error
            if attempt == 3:
                raise RuntimeError(f"GitHub API request failed: {error}") from error
        time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"GitHub API request failed: {last_error}")


def translate_text(text: str, timeout: int = 30) -> str:
    del timeout
    return text.strip()


def version_tuple(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        raise ValueError(f"invalid upstream version: {value}")
    return tuple(map(int, match.groups()))


def previous_stable_tag_from_upstream(current_version: str) -> str:
    # The tags endpoint is small (unlike the releases endpoint, whose payload
    # includes every platform asset). Walk until the current tag is found and
    # choose the highest stable version below it.
    current_tag = f"rust-v{current_version}"
    candidates = []
    found_current = False
    for page in range(1, 11):
        tags = api_json(f"{API_BASE}/tags?per_page=100&page={page}")
        if not isinstance(tags, list) or not tags:
            break
        for item in tags:
            name = str((item or {}).get("name") or "")
            match = TAG_RE.fullmatch(name)
            if not match:
                continue
            version = ".".join(match.groups())
            if name == current_tag:
                found_current = True
            if version_tuple(version) < version_tuple(current_version):
                candidates.append((version_tuple(version), name))
        if found_current:
            break
    if not candidates:
        return ""
    return max(candidates, key=lambda item: item[0])[1]


def previous_upstream_tag(manifest_path: Path, patch_manifest_path: Path, current_version: str) -> str:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    current = ((manifest.get("runtimes") or {}).get("codex") or {}).get("version", "")
    match = VERSION_RE.fullmatch(str(current))
    if match:
        previous = match.group(1)
        if version_tuple(previous) < version_tuple(current_version):
            return f"rust-v{previous}"
        if version_tuple(previous) == version_tuple(current_version):
            # A new Lab revision can reuse the same upstream release. In that
            # case compare against the preceding official stable tag rather
            # than incorrectly reporting that this is the first changelog.
            previous_tag = previous_stable_tag_from_upstream(current_version)
            if previous_tag:
                return previous_tag

    patch_manifest = json.loads(patch_manifest_path.read_text(encoding="utf-8"))
    baseline = str((patch_manifest.get("canonical_upstream") or {}).get("baseline_version") or "")
    if baseline and version_tuple(baseline) < version_tuple(current_version):
        return f"rust-v{baseline}"
    return ""


def parse_release_sections(body: str) -> list[tuple[str, str]]:
    sections = []
    current = ""
    for line in body.splitlines():
        heading = re.match(r"^#{2,3}\s+(.+?)\s*$", line)
        if heading:
            name = heading.group(1).strip()
            if name.lower() == "changelog":
                break
            current = name
            continue
        if current and line.lstrip().startswith("- "):
            item = line.lstrip()[2:].strip()
            if item and not item.lower().startswith("full changelog:"):
                sections.append((current, item))
    return sections


def classify_commit(title: str) -> str:
    lower = title.lower()
    if re.search(r"\b(fix|prevent|correct|restore|retry|preserve|avoid|restrict|handle|resolve|keep|guard)\b", lower):
        return "问题修复与稳定性"
    if re.search(r"\b(add|support|enable|introduce|allow|expose|implement|provide|create|wire)\b", lower):
        return "新增功能与能力"
    if re.search(r"\b(perf|performance|optimi[sz]e|faster|reduce latency)\b", lower):
        return "性能优化"
    if re.search(r"\b(doc|docs|documentation)\b", lower):
        return "文档更新"
    if re.search(r"\b(update|remove|refactor|build|chore|bump|cleanup|clean up)\b", lower):
        return "工程维护"
    return "其他上游改进"


def commit_details(message: str) -> tuple[str, list[str]]:
    lines = message.splitlines()
    title = lines[0].strip() if lines else ""
    details = []
    paragraphs = []
    bullet_parts = []
    paragraph_parts = []
    active = True

    def flush_bullet():
        if bullet_parts:
            details.append(" ".join(bullet_parts).strip())
            bullet_parts.clear()

    def flush_paragraph():
        if paragraph_parts:
            paragraphs.append(" ".join(paragraph_parts).strip())
            paragraph_parts.clear()

    for raw in lines[1:]:
        line = raw.strip()
        if not line:
            flush_bullet()
            flush_paragraph()
            continue
        if line.startswith("## "):
            flush_bullet()
            flush_paragraph()
            heading = line[3:].strip().lower()
            if heading in {"testing", "tests", "changelog", "review", "notes"}:
                active = False
            continue
        if not active or line.startswith(("GitOrigin-RevId:", "(cherry picked from commit")):
            continue
        if line.startswith("- "):
            flush_paragraph()
            flush_bullet()
            bullet_parts.append(line[2:].strip())
        elif not line.startswith(("Co-authored-by:", "Signed-off-by:")):
            if bullet_parts:
                bullet_parts.append(line)
            else:
                paragraph_parts.append(line)

    flush_bullet()
    flush_paragraph()
    if details:
        return title, details[:4]
    return title, paragraphs[:2]


def self_test() -> None:
    parsed = parse_release_sections(
        "## New Features\n- Add " + TICK + "incremental_tools_legacy" + TICK + " support (#12)\n"
        "## Bug Fixes\n- Fix retry handling\n## Changelog\n- ignored"
    )
    assert len(parsed) == 2 and parsed[0][0] == "New Features"
    assert classify_commit("Add opt-in incremental tools") == "新增功能与能力"
    assert classify_commit("Restrict legacy incremental tools") == "问题修复与稳定性"
    title, details = commit_details("Add support (#1)\n\n## What changed\n- Keep " + TICK + "web_search" + TICK + " in tools\n\n## Testing\n- tests")
    assert title == "Add support (#1)" and details == ["Keep " + TICK + "web_search" + TICK + " in tools"]
    _, wrapped = commit_details(
        "Restrict provider scope (#2)\n\n## What changed\n"
        "Gate the feature on Amazon Bedrock and\n"
        "Amazon Bedrock Runtime instead of OpenAI.\n"
        "Responses Lite continues to use the existing flag."
    )
    assert len(wrapped) == 1 and "Responses Lite" in wrapped[0]
    print("[codex-upstream-notes] self-test: PASS")


def generate(args) -> str:
    match = TAG_RE.fullmatch(args.tag)
    if not match:
        raise ValueError(f"invalid OpenAI stable tag: {args.tag}")
    current_version = ".".join(match.groups())
    if not SHA_RE.fullmatch(args.sha):
        raise ValueError("upstream commit SHA must be 40 lowercase hexadecimal characters")

    previous_tag = previous_upstream_tag(args.manifest, args.patch_manifest, current_version)
    release_url = f"https://github.com/openai/codex/releases/tag/{args.tag}"
    release = api_json(f"{API_BASE}/releases/tags/{urllib.parse.quote(args.tag, safe='')}", allow_404=True)
    release_body = str((release or {}).get("body") or "")
    official_items = parse_release_sections(release_body)

    compare = None
    compare_url = ""
    if previous_tag and previous_tag != args.tag:
        compare_url = f"https://github.com/openai/codex/compare/{previous_tag}...{args.tag}"
        api_compare = (
            f"{API_BASE}/compare/{urllib.parse.quote(previous_tag, safe='')}..."
            f"{urllib.parse.quote(args.tag, safe='')}"
        )
        try:
            compare = api_json(api_compare)
        except RuntimeError:
            if not official_items:
                raise
    elif previous_tag == args.tag:
        lines = [
            "## 上游 Codex CLI 更新记录",
            "",
            f"- 上游版本：{args.tag}",
            f"- 上游提交：{args.sha}",
            f"- 上游发布页：{release_url}",
            "",
            "本次实验室修订沿用上一次发布的同一上游版本，没有新增上游 Codex CLI 提交。",
            "本次变化属于实验室补丁、构建或封装层调整，不会把旧版上游更新重复列为新功能。",
        ]
        return "\n".join(lines) + "\n"

    lines = [
        "## 上游 Codex CLI 更新记录",
        "",
        f"- 上游版本：{args.tag}",
        f"- 上游提交：{args.sha}",
        f"- 上游发布页：{release_url}",
    ]
    if previous_tag:
        lines.append(f"- 增量比较：{previous_tag} → {args.tag}")
        if compare_url:
            lines.append(f"- 完整差异：{compare_url}")
    else:
        lines.append("- 增量比较：首次记录，按上游官方发布说明整理")

    if official_items:
        grouped = {}
        for section, item in official_items:
            category = CATEGORY_NAMES.get(section.lower(), translate_text(section))
            grouped.setdefault(category, []).append(item)
        for category in CATEGORY_ORDER:
            items = grouped.get(category, [])
            if not items:
                continue
            lines.extend(["", f"### {category}"])
            for item in items:
                lines.append(f"- {translate_text(item)}")
    else:
        commits = (compare or {}).get("commits") or []
        grouped = {category: [] for category in CATEGORY_ORDER}
        for commit in commits:
            message = str(((commit.get("commit") or {}).get("message")) or "").strip()
            if not message:
                continue
            title, details = commit_details(message)
            if not title:
                continue
            category = classify_commit(title)
            grouped.setdefault(category, []).append((title, details))
        count = sum(len(items) for items in grouped.values())
        if not count:
            raise RuntimeError("no upstream release notes or comparable commits were found")
        for category in CATEGORY_ORDER:
            items = grouped.get(category, [])
            if not items:
                continue
            lines.extend(["", f"### {category}"])
            for title, details in items[:20]:
                lines.append(f"- **{translate_text(title)}**")
                for detail in details:
                    lines.append(f"  - {translate_text(detail)}")
            if len(items) > 20:
                lines.append(f"- 另有 {len(items) - 20} 条同类提交未展开，请查看完整差异链接。")

    if compare:
        ahead = compare.get("ahead_by")
        behind = compare.get("behind_by")
        changed_files = len(compare.get("files") or [])
        lines.extend([
            "",
            "### 上游差异统计",
            f"- 新增提交数：{ahead if isinstance(ahead, int) else '未提供'}",
            f"- 涉及文件数：{changed_files}",
        ])
        if compare.get("status") == "diverged" and isinstance(behind, int) and behind:
            lines.append(f"- 标签历史存在分叉：旧标签另有 {behind} 个提交不在本次新标签中；以上仅整理本次新增方向的提交。")
        if isinstance(compare.get("total_commits"), int) and len(compare.get("commits") or []) < compare["total_commits"]:
            lines.append("- GitHub Compare 返回的提交列表不完整；请通过完整差异链接查看全部提交。")

    lines.extend([
        "",
        "> 中文记录根据 OpenAI Codex 官方 Release 说明或提交记录自动翻译整理；命令、配置项、功能开关和代码标识保持上游原文。",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag")
    parser.add_argument("--sha")
    parser.add_argument("--manifest", type=Path, default=Path("Runtime/runtime-manifest.json"))
    parser.add_argument("--patch-manifest", type=Path, default=Path("Runtime/Codex/Patches/manifest.json"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    if not args.tag or not args.sha or not args.output:
        parser.error("--tag, --sha and --output are required unless --self-test is used")
    try:
        notes = generate(args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(notes, encoding="utf-8")
    except (OSError, ValueError, KeyError, RuntimeError, urllib.error.URLError, json.JSONDecodeError) as error:
        raise SystemExit(f"[codex-upstream-notes] BLOCKED: {error}")
    print(f"[codex-upstream-notes] wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
