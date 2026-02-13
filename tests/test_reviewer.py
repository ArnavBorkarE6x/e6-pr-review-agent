"""Tests for the review engine formatting and helpers."""

from __future__ import annotations

from agent.models import (
    PRSummary,
    ReviewCategory,
    ReviewComment,
    ReviewResult,
    ReviewSeverity,
)
from agent.reviewer import _safe_enum, format_summary_body


class TestSafeEnum:
    def test_valid_severity(self):
        result = _safe_enum("critical", ReviewSeverity, ReviewSeverity.SUGGESTION)
        assert result == ReviewSeverity.CRITICAL

    def test_invalid_severity(self):
        result = _safe_enum("unknown", ReviewSeverity, ReviewSeverity.SUGGESTION)
        assert result == ReviewSeverity.SUGGESTION

    def test_valid_category(self):
        result = _safe_enum("security", ReviewCategory, ReviewCategory.BEST_PRACTICE)
        assert result == ReviewCategory.SECURITY

    def test_case_insensitive(self):
        result = _safe_enum("BUG_RISK", ReviewCategory, ReviewCategory.BEST_PRACTICE)
        assert result == ReviewCategory.BUG_RISK


class TestFormatSummaryBody:
    def test_full_summary(self):
        summary = PRSummary(
            purpose="Fix null pointer in user service",
            changes=["Added null check", "Added test"],
            key_files=["user_service.py"],
            risk_areas=["Error path untested"],
            test_coverage_note="Tests added",
        )
        result = ReviewResult(
            pr_number=42,
            repo="e6data/backend",
            head_sha="abc123",
            summary=summary,
            comments=[
                ReviewComment(path="a.py", line=1, body="issue"),
            ],
            files_reviewed=3,
            duration_seconds=12.5,
        )

        body = format_summary_body(summary, result)
        assert "Automated PR Analysis" in body
        assert "Fix null pointer" in body
        assert "Added null check" in body
        assert "`user_service.py`" in body
        assert "Error path untested" in body
        assert "Tests added" in body
        assert "3 file(s) reviewed" in body
        assert "1 comment(s)" in body
        assert "12.5s" in body

    def test_skipped_files_collapsible(self):
        summary = PRSummary(purpose="test", changes=["change"])
        result = ReviewResult(
            pr_number=1,
            repo="org/repo",
            head_sha="abc",
            summary=summary,
            skipped_files=["a.lock", "b.min.js"],
            files_reviewed=1,
            duration_seconds=5.0,
        )
        body = format_summary_body(summary, result)
        assert "Skipped 2 file(s)" in body
        assert "`a.lock`" in body

    def test_minimal_summary(self):
        summary = PRSummary(purpose="Simple fix", changes=["One change"])
        result = ReviewResult(
            pr_number=1,
            repo="org/repo",
            head_sha="abc",
            summary=summary,
            files_reviewed=1,
            duration_seconds=3.0,
        )
        body = format_summary_body(summary, result)
        assert "Simple fix" in body
        assert "0 comment(s)" in body
