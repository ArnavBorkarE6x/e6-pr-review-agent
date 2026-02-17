"""Tests for the logging suggestion feature."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agent.models import FileDiff, ReviewCategory, ReviewSeverity
from agent.reviewer import ReviewEngine, format_summary_body

JAVA_PATCH = (
    "@@ -0,0 +1,15 @@\n"
    "+package org.example.operators;\n"
    "+\n"
    "+import org.example.Chunk;\n"
    "+import org.example.Query;\n"
    "+\n"
    "+public class LimitOp extends Operator {\n"
    "+    private int m_limit;\n"
    "+    private int m_seen = 0;\n"
    "+\n"
    "+    public void process(Chunk chunk, Query query) {\n"
    "+        int chunkSize = chunk.getColumn(0).getSize();\n"
    "+        int remaining = m_limit - m_seen;\n"
    "+        if (remaining <= 0) { return; }\n"
    "+        this.m_parent.process(chunk, query);\n"
    "+    }\n"
)


@pytest.fixture
def java_file() -> list[FileDiff]:
    return [
        FileDiff(
            filename="src/main/java/org/example/operators/LimitOp.java",
            status="added",
            additions=15,
            deletions=0,
            patch=JAVA_PATCH,
            language="java",
            token_count=120,
        )
    ]


@pytest.mark.asyncio
async def test_logging_suggestions_returned(java_file):
    """Logging suggestions should appear as LOGGING category comments."""
    summary_response = {
        "purpose": "Add limit operator",
        "changes": ["New LimitOp class"],
    }
    review_response = []  # no code review issues
    logging_response = [
        {
            "line": 10,
            "level": "debug",
            "log_statement": 'LOG.debug("process() called: chunkSize={}, remaining={}", chunkSize, remaining);',
            "reason": "Helps trace chunk processing flow during debugging",
        },
        {
            "line": 13,
            "level": "info",
            "log_statement": 'LOG.info("Limit reached, discarding chunk");',
            "reason": "Important state change worth logging at INFO level",
        },
    ]

    mock_ai = AsyncMock()
    mock_ai.complete_json = AsyncMock(
        side_effect=[
            (summary_response, {"input_tokens": 200, "output_tokens": 50, "model": "test"}),
            (review_response, {"input_tokens": 200, "output_tokens": 10, "model": "test"}),
            (logging_response, {"input_tokens": 200, "output_tokens": 80, "model": "test"}),
        ]
    )

    engine = ReviewEngine(ai=mock_ai, suggest_logging=True)
    result = await engine.review(
        pr_number=10,
        repo="org/repo",
        head_sha="sha",
        title="Add limit operator",
        body=None,
        author="dev",
        additions=15,
        deletions=0,
        files=java_file,
        skipped_files=[],
    )

    # Should have 2 logging suggestions, 0 code review comments
    assert len(result.comments) == 2
    for c in result.comments:
        assert c.category == ReviewCategory.LOGGING
        assert c.severity == ReviewSeverity.SUGGESTION
        assert "LOGGING" in c.body
        assert "```suggestion" in c.body


@pytest.mark.asyncio
async def test_logging_disabled_no_suggestions(java_file):
    """When suggest_logging=False, no logging pass should run."""
    summary_response = {"purpose": "Test", "changes": ["Change"]}
    review_response = []

    mock_ai = AsyncMock()
    mock_ai.complete_json = AsyncMock(
        side_effect=[
            (summary_response, {"input_tokens": 100, "output_tokens": 50, "model": "test"}),
            (review_response, {"input_tokens": 100, "output_tokens": 10, "model": "test"}),
        ]
    )

    engine = ReviewEngine(ai=mock_ai, suggest_logging=False)
    result = await engine.review(
        pr_number=1,
        repo="org/repo",
        head_sha="sha",
        title="Test",
        body=None,
        author="dev",
        additions=15,
        deletions=0,
        files=java_file,
        skipped_files=[],
    )

    assert len(result.comments) == 0
    # AI should only be called twice (summary + review), not three times
    assert mock_ai.complete_json.call_count == 2


@pytest.mark.asyncio
async def test_logging_empty_response(java_file):
    """AI returning [] for logging should produce no logging comments."""
    summary_response = {"purpose": "Test", "changes": ["Change"]}
    review_response = []
    logging_response = []

    mock_ai = AsyncMock()
    mock_ai.complete_json = AsyncMock(
        side_effect=[
            (summary_response, {"input_tokens": 100, "output_tokens": 50, "model": "test"}),
            (review_response, {"input_tokens": 100, "output_tokens": 10, "model": "test"}),
            (logging_response, {"input_tokens": 100, "output_tokens": 10, "model": "test"}),
        ]
    )

    engine = ReviewEngine(ai=mock_ai, suggest_logging=True)
    result = await engine.review(
        pr_number=1,
        repo="org/repo",
        head_sha="sha",
        title="Test",
        body=None,
        author="dev",
        additions=15,
        deletions=0,
        files=java_file,
        skipped_files=[],
    )

    assert len(result.comments) == 0


@pytest.mark.asyncio
async def test_logging_invalid_lines_filtered(java_file):
    """Logging suggestions with invalid line numbers should be filtered."""
    summary_response = {"purpose": "Test", "changes": ["Change"]}
    review_response = []
    logging_response = [
        {
            "line": 999,
            "level": "info",
            "log_statement": 'LOG.info("ghost");',
            "reason": "This line doesn't exist",
        },
        {
            "line": 10,
            "level": "debug",
            "log_statement": 'LOG.debug("valid");',
            "reason": "Valid line",
        },
    ]

    mock_ai = AsyncMock()
    mock_ai.complete_json = AsyncMock(
        side_effect=[
            (summary_response, {"input_tokens": 100, "output_tokens": 50, "model": "test"}),
            (review_response, {"input_tokens": 100, "output_tokens": 10, "model": "test"}),
            (logging_response, {"input_tokens": 100, "output_tokens": 50, "model": "test"}),
        ]
    )

    engine = ReviewEngine(ai=mock_ai, suggest_logging=True)
    result = await engine.review(
        pr_number=1,
        repo="org/repo",
        head_sha="sha",
        title="Test",
        body=None,
        author="dev",
        additions=15,
        deletions=0,
        files=java_file,
        skipped_files=[],
    )

    # Line 999 should be filtered, line 10 kept
    assert len(result.comments) == 1
    assert result.comments[0].line == 10


@pytest.mark.asyncio
async def test_logging_uses_lightweight_model(java_file):
    """Logging suggestions should always use the lightweight model."""
    summary_response = {"purpose": "Test", "changes": ["Change"]}
    review_response = []
    logging_response = [
        {
            "line": 10,
            "level": "debug",
            "log_statement": 'LOG.debug("test");',
            "reason": "Test",
        },
    ]

    mock_ai = AsyncMock()
    mock_ai.complete_json = AsyncMock(
        side_effect=[
            (summary_response, {"input_tokens": 100, "output_tokens": 50, "model": "test"}),
            (review_response, {"input_tokens": 100, "output_tokens": 10, "model": "test"}),
            (logging_response, {"input_tokens": 100, "output_tokens": 50, "model": "test"}),
        ]
    )

    engine = ReviewEngine(ai=mock_ai, suggest_logging=True)
    await engine.review(
        pr_number=1,
        repo="org/repo",
        head_sha="sha",
        title="Test",
        body=None,
        author="dev",
        additions=15,
        deletions=0,
        files=java_file,
        skipped_files=[],
    )

    # Third call (logging) should have lightweight=True
    logging_call = mock_ai.complete_json.call_args_list[2]
    assert logging_call.kwargs.get("lightweight") is True


def test_summary_body_shows_logging_count():
    """Summary body should separately show review comments and logging suggestions."""
    from agent.models import PRSummary, ReviewComment, ReviewResult

    summary = PRSummary(purpose="Test", changes=["Change"])
    result = ReviewResult(
        pr_number=1,
        repo="org/repo",
        head_sha="sha",
        summary=summary,
        comments=[
            ReviewComment(path="a.py", line=1, body="issue", category=ReviewCategory.BUG_RISK),
            ReviewComment(path="a.py", line=2, body="log", category=ReviewCategory.LOGGING),
            ReviewComment(path="a.py", line=3, body="log2", category=ReviewCategory.LOGGING),
        ],
        files_reviewed=1,
        duration_seconds=5.0,
    )

    body = format_summary_body(summary, result)
    assert "1 review comment(s)" in body
    assert "2 logging suggestion(s)" in body


def test_summary_body_hides_logging_when_zero():
    """When there are no logging suggestions, don't show the count."""
    from agent.models import PRSummary, ReviewComment, ReviewResult

    summary = PRSummary(purpose="Test", changes=["Change"])
    result = ReviewResult(
        pr_number=1,
        repo="org/repo",
        head_sha="sha",
        summary=summary,
        comments=[
            ReviewComment(path="a.py", line=1, body="issue", category=ReviewCategory.BUG_RISK),
        ],
        files_reviewed=1,
        duration_seconds=5.0,
    )

    body = format_summary_body(summary, result)
    assert "1 review comment(s)" in body
    assert "logging" not in body.lower()


@pytest.mark.asyncio
async def test_logging_suggestion_block_format(java_file):
    """Logging suggestions should use GitHub suggestion block with original line."""
    summary_response = {"purpose": "Test", "changes": ["Change"]}
    review_response = []
    logging_response = [
        {
            "line": 10,
            "level": "debug",
            "log_statement": 'LOG.debug("process() called");',
            "reason": "Trace entry point",
        },
    ]

    mock_ai = AsyncMock()
    mock_ai.complete_json = AsyncMock(
        side_effect=[
            (summary_response, {"input_tokens": 100, "output_tokens": 50, "model": "test"}),
            (review_response, {"input_tokens": 100, "output_tokens": 10, "model": "test"}),
            (logging_response, {"input_tokens": 100, "output_tokens": 50, "model": "test"}),
        ]
    )

    engine = ReviewEngine(ai=mock_ai, suggest_logging=True)
    result = await engine.review(
        pr_number=1,
        repo="org/repo",
        head_sha="sha",
        title="Test",
        body=None,
        author="dev",
        additions=15,
        deletions=0,
        files=java_file,
        skipped_files=[],
    )

    assert len(result.comments) == 1
    body = result.comments[0].body
    # Should contain the GitHub suggestion block with original line + log statement
    assert "```suggestion" in body
    # Line 10 is "    public void process(Chunk chunk, Query query) {"
    assert "public void process" in body
    assert 'LOG.debug("process() called");' in body


@pytest.mark.asyncio
async def test_logging_level_emojis(java_file):
    """Each log level should have the correct emoji in the comment."""
    summary_response = {"purpose": "Test", "changes": ["Change"]}
    review_response = []
    logging_response = [
        {
            "line": 10,
            "level": "error",
            "log_statement": 'LOG.error("fail");',
            "reason": "Error path",
        },
        {"line": 11, "level": "warn", "log_statement": 'LOG.warn("risky");', "reason": "Warn path"},
        {"line": 12, "level": "info", "log_statement": 'LOG.info("ok");', "reason": "Info path"},
        {
            "line": 13,
            "level": "debug",
            "log_statement": 'LOG.debug("trace");',
            "reason": "Debug path",
        },
    ]

    mock_ai = AsyncMock()
    mock_ai.complete_json = AsyncMock(
        side_effect=[
            (summary_response, {"input_tokens": 100, "output_tokens": 50, "model": "test"}),
            (review_response, {"input_tokens": 100, "output_tokens": 10, "model": "test"}),
            (logging_response, {"input_tokens": 100, "output_tokens": 80, "model": "test"}),
        ]
    )

    engine = ReviewEngine(ai=mock_ai, suggest_logging=True)
    result = await engine.review(
        pr_number=1,
        repo="org/repo",
        head_sha="sha",
        title="Test",
        body=None,
        author="dev",
        additions=15,
        deletions=0,
        files=java_file,
        skipped_files=[],
    )

    assert len(result.comments) == 4
    assert "`ERROR`" in result.comments[0].body
    assert "`WARN`" in result.comments[1].body
    assert "`INFO`" in result.comments[2].body
    assert "`DEBUG`" in result.comments[3].body
