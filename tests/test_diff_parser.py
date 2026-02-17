"""Tests for diff parsing and compression utilities."""

from __future__ import annotations

from agent.diff_parser import (
    compress_diff_for_summary,
    count_tokens,
    detect_language,
    extract_line_content,
    find_closest_line,
    is_binary,
    parse_patch_line_map,
    should_skip_file,
)
from agent.models import FileDiff


class TestDetectLanguage:
    def test_python(self):
        assert detect_language("src/main.py") == "python"

    def test_typescript(self):
        assert detect_language("components/App.tsx") == "typescript"

    def test_go(self):
        assert detect_language("cmd/server/main.go") == "go"

    def test_unknown_extension(self):
        assert detect_language("Makefile") == ""

    def test_nested_path(self):
        assert detect_language("a/b/c/d.java") == "java"


class TestIsBinary:
    def test_image(self):
        assert is_binary("logo.png") is True

    def test_source(self):
        assert is_binary("main.py") is False

    def test_font(self):
        assert is_binary("font.woff2") is True

    def test_database(self):
        assert is_binary("data.sqlite3") is True


class TestShouldSkipFile:
    def test_lock_file(self):
        assert should_skip_file("package-lock.json", ["package-lock.json"]) is True

    def test_glob_pattern(self):
        assert should_skip_file("styles.min.css", ["*.min.css"]) is True

    def test_no_match(self):
        assert should_skip_file("main.py", ["*.lock", "*.min.js"]) is False

    def test_nested_path(self):
        assert should_skip_file("vendor/go.sum", ["go.sum"]) is True


class TestParsePatchLineMap:
    def test_simple_addition(self):
        patch = "@@ -1,3 +1,4 @@\n line1\n+added_line\n line2\n line3\n"
        result = parse_patch_line_map(patch)
        assert 2 in result
        assert result[2] == "+added_line"

    def test_multiple_hunks(self):
        patch = (
            "@@ -1,3 +1,4 @@\n context\n+first_add\n context\n"
            "@@ -10,3 +11,4 @@\n context\n+second_add\n context\n"
        )
        result = parse_patch_line_map(patch)
        assert 2 in result
        assert 12 in result

    def test_deletion_only(self):
        patch = "@@ -1,3 +1,2 @@\n context\n-removed_line\n context\n"
        result = parse_patch_line_map(patch)
        assert len(result) == 0

    def test_mixed_changes(self):
        patch = "@@ -1,4 +1,5 @@\n ctx\n-old\n+new\n+extra\n ctx\n"
        result = parse_patch_line_map(patch)
        assert 2 in result  # +new
        assert 3 in result  # +extra
        assert len(result) == 2


class TestFindClosestLine:
    def test_exact_match(self):
        line_map = {10: "+code", 11: "+code", 12: "+code"}
        assert find_closest_line(11, line_map) == 11

    def test_close_above(self):
        line_map = {10: "+code", 12: "+code"}
        result = find_closest_line(11, line_map)
        assert result in (10, 12)

    def test_too_far(self):
        line_map = {10: "+code", 20: "+code"}
        assert find_closest_line(15, line_map) is None

    def test_empty_map(self):
        assert find_closest_line(10, {}) is None


class TestExtractLineContent:
    def test_strips_plus_prefix(self):
        line_map = {5: "+    int x = 10;"}
        assert extract_line_content(5, line_map) == "    int x = 10;"

    def test_missing_line_returns_none(self):
        line_map = {5: "+code"}
        assert extract_line_content(99, line_map) is None

    def test_empty_line(self):
        line_map = {3: "+"}
        assert extract_line_content(3, line_map) == ""


class TestCountTokens:
    def test_basic(self):
        assert count_tokens("Hello, world!") > 0

    def test_empty(self):
        assert count_tokens("") == 0


class TestCompressDiffForSummary:
    def test_fits_within_budget(self):
        files = [
            FileDiff(
                filename="main.py",
                status="modified",
                additions=5,
                deletions=2,
                patch="@@ -1,3 +1,4 @@\n context\n+added\n context",
            )
        ]
        result = compress_diff_for_summary(files, max_tokens=1000)
        assert "main.py" in result

    def test_overflow_listed(self):
        files = [
            FileDiff(
                filename=f"file{i}.py",
                status="modified",
                additions=100,
                patch="x" * 5000,
            )
            for i in range(20)
        ]
        result = compress_diff_for_summary(files, max_tokens=100)
        assert "additional file(s)" in result

    def test_removed_files_compact(self):
        files = [
            FileDiff(filename="old.py", status="removed"),
            FileDiff(filename="new.py", status="added", additions=10, patch="+line"),
        ]
        result = compress_diff_for_summary(files, max_tokens=5000)
        assert "Deleted files:" in result
        assert "old.py" in result
