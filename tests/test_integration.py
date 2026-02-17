"""Integration test: full review pipeline with mocked AI and GitHub."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agent.models import FileDiff
from agent.reviewer import ReviewEngine


@pytest.fixture
def sample_files() -> list[FileDiff]:
    return [
        FileDiff(
            filename="src/cache.py",
            status="added",
            additions=30,
            deletions=0,
            patch=(
                "@@ -0,0 +1,10 @@\n"
                "+import redis\n"
                "+\n"
                "+class CacheService:\n"
                "+    def __init__(self, url: str):\n"
                "+        self.client = redis.from_url(url)\n"
                "+\n"
                "+    def get(self, key: str) -> str | None:\n"
                "+        return self.client.get(key)\n"
                "+\n"
                "+    def set(self, key: str, value: str, ttl: int = 3600):\n"
            ),
            language="python",
            token_count=80,
        ),
    ]


@pytest.mark.asyncio
async def test_full_pipeline_with_mocked_ai(sample_files):
    """End-to-end: AI returns structured review → engine parses and validates."""
    summary_response = {
        "purpose": "Add Redis caching layer",
        "changes": ["New CacheService class"],
        "key_files": ["src/cache.py"],
        "risk_areas": ["No connection error handling"],
        "test_coverage_note": "No tests added",
    }
    review_response = [
        {
            "line": 5,
            "severity": "warning",
            "category": "error_handling",
            "body": "Consider handling `redis.ConnectionError` in the constructor.",
        }
    ]

    mock_ai = AsyncMock()
    mock_ai.complete_json = AsyncMock(
        side_effect=[
            (summary_response, {"input_tokens": 500, "output_tokens": 100, "model": "test"}),
            (review_response, {"input_tokens": 300, "output_tokens": 50, "model": "test"}),
        ]
    )

    engine = ReviewEngine(ai=mock_ai, max_diff_tokens=120_000, suggest_logging=False)

    result = await engine.review(
        pr_number=42,
        repo="e6data/backend",
        head_sha="abc123",
        title="Add caching layer",
        body="This PR adds Redis caching.",
        author="dev",
        additions=30,
        deletions=0,
        files=sample_files,
        skipped_files=["package-lock.json (ignored)"],
    )

    assert result.pr_number == 42
    assert result.summary.purpose == "Add Redis caching layer"
    assert result.files_reviewed == 1
    assert len(result.comments) == 1
    assert result.comments[0].line == 5
    assert "error_handling" in result.comments[0].category.value
    assert "WARNING" in result.comments[0].body
    assert len(result.skipped_files) == 1


@pytest.mark.asyncio
async def test_ai_returns_empty_array(sample_files):
    """AI finding no issues should produce zero comments."""
    summary_response = {
        "purpose": "Clean code",
        "changes": ["Minor refactor"],
    }

    mock_ai = AsyncMock()
    mock_ai.complete_json = AsyncMock(
        side_effect=[
            (summary_response, {"input_tokens": 100, "output_tokens": 50, "model": "test"}),
            ([], {"input_tokens": 100, "output_tokens": 10, "model": "test"}),
        ]
    )

    engine = ReviewEngine(ai=mock_ai, suggest_logging=False)
    result = await engine.review(
        pr_number=1,
        repo="org/repo",
        head_sha="sha",
        title="Refactor",
        body=None,
        author="dev",
        additions=5,
        deletions=3,
        files=sample_files,
        skipped_files=[],
    )

    assert len(result.comments) == 0
    assert result.summary.purpose == "Clean code"


@pytest.mark.asyncio
async def test_invalid_line_numbers_filtered(sample_files):
    """AI returning invalid line numbers should be filtered out."""
    summary_response = {"purpose": "Test", "changes": ["Test"]}
    review_response = [
        {"line": 999, "severity": "warning", "category": "bug_risk", "body": "Ghost issue"},
        {"line": 5, "severity": "suggestion", "category": "best_practice", "body": "Real issue"},
    ]

    mock_ai = AsyncMock()
    mock_ai.complete_json = AsyncMock(
        side_effect=[
            (summary_response, {"input_tokens": 100, "output_tokens": 50, "model": "test"}),
            (review_response, {"input_tokens": 100, "output_tokens": 50, "model": "test"}),
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
        additions=10,
        deletions=0,
        files=sample_files,
        skipped_files=[],
    )

    # Line 999 should be filtered out, line 5 should remain
    assert len(result.comments) == 1
    assert result.comments[0].line == 5
