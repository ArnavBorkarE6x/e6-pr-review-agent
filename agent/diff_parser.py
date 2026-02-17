"""Diff parsing, file filtering, and token-aware compression."""

from __future__ import annotations

import fnmatch
import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

import tiktoken

if TYPE_CHECKING:
    from agent.models import FileDiff

# ── Language detection ──────────────────────────────────────────────────

_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".java": "java",
    ".kt": "kotlin",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".scala": "scala",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".swift": "swift",
    ".sh": "bash",
    ".sql": "sql",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".xml": "xml",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".tf": "hcl",
    ".proto": "protobuf",
    ".dart": "dart",
    ".lua": "lua",
    ".php": "php",
    ".r": "r",
    ".ex": "elixir",
    ".exs": "elixir",
}

_BINARY_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".svg",
        ".webp",
        ".bmp",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".otf",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".pptx",
        ".pyc",
        ".class",
        ".o",
        ".so",
        ".dll",
        ".exe",
        ".dylib",
        ".db",
        ".sqlite",
        ".sqlite3",
    }
)

DEFAULT_IGNORE_PATTERNS = [
    "*.lock",
    "*.min.js",
    "*.min.css",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "go.sum",
    "Cargo.lock",
    "*.pb.go",
    "*.generated.*",
    "*.snap",
    "__snapshots__/*",
    "*.map",
]

_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

_encoder: tiktoken.Encoding | None = None


def _get_encoder() -> tiktoken.Encoding:
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.get_encoding("cl100k_base")
    return _encoder


def count_tokens(text: str) -> int:
    return len(_get_encoder().encode(text, disallowed_special=()))


def detect_language(filename: str) -> str:
    ext = PurePosixPath(filename).suffix.lower()
    return _EXT_TO_LANG.get(ext, "")


def is_binary(filename: str) -> bool:
    ext = PurePosixPath(filename).suffix.lower()
    return ext in _BINARY_EXTENSIONS


def should_skip_file(filename: str, ignore_patterns: list[str]) -> bool:
    """Return True if the file matches any ignore glob pattern."""
    name = PurePosixPath(filename).name
    for pattern in ignore_patterns:
        if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(filename, pattern):
            return True
    return False


def parse_patch_line_map(patch: str) -> dict[int, str]:
    """Parse a unified diff and return {new_file_line_number: diff_line} for added lines.

    Used to validate that AI-suggested line numbers actually exist in the diff.
    """
    line_map: dict[int, str] = {}
    current_line = 0

    for raw_line in patch.splitlines():
        hunk_match = _HUNK_HEADER_RE.match(raw_line)
        if hunk_match:
            current_line = int(hunk_match.group(1))
            continue

        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            line_map[current_line] = raw_line
            current_line += 1
        elif raw_line.startswith("-") and not raw_line.startswith("---"):
            # Deleted lines don't advance the new-file counter
            continue
        else:
            # Context line
            current_line += 1

    return line_map


def extract_line_content(line: int, line_map: dict[int, str]) -> str | None:
    """Return the source content of a diff line (without the leading '+')."""
    raw = line_map.get(line)
    if raw is None:
        return None
    # Strip the leading '+' from the diff line
    if raw.startswith("+"):
        return raw[1:]
    return raw


def find_closest_line(target: int, line_map: dict[int, str]) -> int | None:
    """Find the closest valid diff line within 3 lines of the target."""
    if not line_map:
        return None
    if target in line_map:
        return target
    for offset in range(1, 4):
        if target + offset in line_map:
            return target + offset
        if target - offset in line_map:
            return target - offset
    return None


def compress_diff_for_summary(files: list[FileDiff], max_tokens: int) -> str:
    """Build a combined diff string for the summary prompt within a token budget.

    Strategy:
    1. Sort files by additions descending (most important first).
    2. Consolidate removed files into a compact list.
    3. Iteratively add patches until budget is reached.
    4. List remaining files as overflow.
    """
    removed_files = [f for f in files if f.status == "removed"]
    active_files = sorted(
        [f for f in files if f.status != "removed"],
        key=lambda f: f.additions,
        reverse=True,
    )

    parts: list[str] = []
    used_tokens = 0
    overflow: list[str] = []

    if removed_files:
        removed_list = "Deleted files: " + ", ".join(f.filename for f in removed_files)
        used_tokens += count_tokens(removed_list)
        parts.append(removed_list)

    for f in active_files:
        entry = f"--- {f.filename} ({f.status}, +{f.additions}/-{f.deletions})\n{f.patch}"
        entry_tokens = count_tokens(entry)

        if used_tokens + entry_tokens > max_tokens:
            overflow.append(f.filename)
            continue

        parts.append(entry)
        used_tokens += entry_tokens

    if overflow:
        tail = ", ".join(overflow[:10])
        suffix = " ...]" if len(overflow) > 10 else "]"
        parts.append(f"\n[{len(overflow)} additional file(s) not shown: {tail}{suffix}")

    return "\n\n".join(parts)
