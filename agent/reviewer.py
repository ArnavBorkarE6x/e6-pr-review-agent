"""Core review engine — orchestrates summary generation and per-file review.

Implements:
- Model cascade: lightweight model for trivial changes, primary for complex
- Comment validation: AI-suggested line numbers are verified against the diff
- Concurrent file reviews with bounded parallelism
"""

from __future__ import annotations

import asyncio
import logging
import time

from agent.ai_client import AIClient
from agent.diff_parser import (
    compress_diff_for_summary,
    find_closest_line,
    parse_patch_line_map,
)
from agent.models import (
    FileDiff,
    PRSummary,
    ReviewCategory,
    ReviewComment,
    ReviewResult,
    ReviewSeverity,
)
from agent.prompts import (
    LIGHTWEIGHT_REVIEW_PROMPT,
    REVIEW_PROMPT,
    SUMMARY_PROMPT,
    SYSTEM_PROMPT,
)

log = logging.getLogger(__name__)

LIGHTWEIGHT_THRESHOLD = 15  # max additions to use lightweight model
MAX_CONCURRENT_REVIEWS = 5

SEVERITY_BADGES = {
    ReviewSeverity.CRITICAL: ":rotating_light:",
    ReviewSeverity.WARNING: ":warning:",
    ReviewSeverity.SUGGESTION: ":bulb:",
    ReviewSeverity.NITPICK: ":mag:",
}


class ReviewEngine:
    """Runs the full review pipeline for a single PR."""

    def __init__(
        self,
        ai: AIClient,
        max_diff_tokens: int = 120_000,
    ) -> None:
        self.ai = ai
        self.max_diff_tokens = max_diff_tokens

    async def review(
        self,
        *,
        pr_number: int,
        repo: str,
        head_sha: str,
        title: str,
        body: str | None,
        author: str,
        additions: int,
        deletions: int,
        files: list[FileDiff],
        skipped_files: list[str],
    ) -> ReviewResult:
        """Run summary + per-file review. Returns a ReviewResult."""
        start = time.monotonic()
        total_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}

        # ── Summary ─────────────────────────────────────────────────────
        summary, usage = await self._generate_summary(
            title=title,
            body=body,
            author=author,
            additions=additions,
            deletions=deletions,
            changed_files=len(files) + len(skipped_files),
            files=files,
        )
        _merge_usage(total_usage, usage)

        # ── Per-file reviews ────────────────────────────────────────────
        comments = await self._review_files(
            title=title,
            purpose=summary.purpose,
            files=files,
            total_usage=total_usage,
        )

        elapsed = round(time.monotonic() - start, 2)
        log.info(
            "Review complete: %d files, %d comments, %.1fs",
            len(files),
            len(comments),
            elapsed,
        )

        return ReviewResult(
            pr_number=pr_number,
            repo=repo,
            head_sha=head_sha,
            summary=summary,
            comments=comments,
            skipped_files=skipped_files,
            files_reviewed=len(files),
            duration_seconds=elapsed,
            model_used=total_usage.get("model", ""),
        )

    # ── Summary ─────────────────────────────────────────────────────────

    async def _generate_summary(
        self,
        *,
        title: str,
        body: str | None,
        author: str,
        additions: int,
        deletions: int,
        changed_files: int,
        files: list[FileDiff],
    ) -> tuple[PRSummary, dict]:
        budget = min(self.max_diff_tokens // 3, 40_000)
        diff_text = compress_diff_for_summary(files, budget)

        prompt = SUMMARY_PROMPT.format(
            title=title,
            description=body or "(no description)",
            author=author,
            files_changed=changed_files,
            additions=additions,
            deletions=deletions,
            diff=diff_text,
        )

        try:
            data, usage = await self.ai.complete_json(SYSTEM_PROMPT, prompt, max_tokens=2048)
            if isinstance(data, dict):
                return PRSummary(**data), usage
        except Exception:
            log.exception("Summary generation failed, using fallback")

        return PRSummary(
            purpose=f"Changes in {changed_files} file(s): {title}",
            changes=[f"+{additions}/-{deletions} lines changed"],
        ), {}

    # ── Per-file review ─────────────────────────────────────────────────

    async def _review_files(
        self,
        *,
        title: str,
        purpose: str,
        files: list[FileDiff],
        total_usage: dict,
    ) -> list[ReviewComment]:
        sem = asyncio.Semaphore(MAX_CONCURRENT_REVIEWS)

        async def _one(f: FileDiff) -> list[ReviewComment]:
            async with sem:
                return await self._review_single_file(title, purpose, f, total_usage)

        tasks = [_one(f) for f in files if f.patch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        comments: list[ReviewComment] = []
        for r in results:
            if isinstance(r, Exception):
                log.warning("File review error: %s", r)
            else:
                comments.extend(r)
        return comments

    async def _review_single_file(
        self,
        title: str,
        purpose: str,
        file: FileDiff,
        total_usage: dict,
    ) -> list[ReviewComment]:
        use_lightweight = (
            file.additions <= LIGHTWEIGHT_THRESHOLD and file.deletions <= LIGHTWEIGHT_THRESHOLD
        )

        lang = file.language or "text"
        if use_lightweight:
            prompt = LIGHTWEIGHT_REVIEW_PROMPT.format(
                filename=file.filename, language=lang, patch=file.patch
            )
        else:
            prompt = REVIEW_PROMPT.format(
                title=title,
                purpose=purpose,
                filename=file.filename,
                language=lang,
                patch=file.patch,
            )

        try:
            data, usage = await self.ai.complete_json(
                SYSTEM_PROMPT, prompt, lightweight=use_lightweight, max_tokens=3000
            )
            _merge_usage(total_usage, usage)
        except Exception:
            log.exception("Review failed for %s", file.filename)
            return []

        if not isinstance(data, list):
            return []

        line_map = parse_patch_line_map(file.patch)
        comments: list[ReviewComment] = []

        for item in data:
            if not isinstance(item, dict):
                continue
            line = item.get("line")
            body = item.get("body", "").strip()
            if not line or not body:
                continue

            # Validate line exists in diff
            if line not in line_map:
                line = find_closest_line(line, line_map)
                if line is None:
                    log.debug("Skipping comment with invalid line in %s", file.filename)
                    continue

            severity = _safe_enum(
                item.get("severity", ""), ReviewSeverity, ReviewSeverity.SUGGESTION
            )
            category = _safe_enum(
                item.get("category", ""), ReviewCategory, ReviewCategory.BEST_PRACTICE
            )

            badge = SEVERITY_BADGES.get(severity, ":bulb:")
            cat_label = category.value.replace("_", " ").title()
            formatted = f"{badge} **{severity.value.upper()}** | {cat_label}\n\n{body}"

            comments.append(
                ReviewComment(
                    path=file.filename,
                    line=line,
                    body=formatted,
                    severity=severity,
                    category=category,
                )
            )

        return comments


# ── Helpers ─────────────────────────────────────────────────────────────


def _merge_usage(total: dict, usage: dict) -> None:
    for key in ("input_tokens", "output_tokens"):
        total[key] = total.get(key, 0) + usage.get(key, 0)
    if "model" in usage:
        total["model"] = usage["model"]


def _safe_enum(value: str, cls: type, default: object) -> object:  # type: ignore[type-arg]
    try:
        return cls(value.lower())
    except (ValueError, KeyError):
        return default


def format_summary_body(summary: PRSummary, result: ReviewResult) -> str:
    """Format the review summary as a markdown comment body."""
    lines = [
        "## :robot: Automated PR Analysis",
        "",
        "### Summary",
        f"**Purpose:** {summary.purpose}",
        "",
        "**Changes:**",
    ]
    for c in summary.changes:
        lines.append(f"- {c}")

    if summary.key_files:
        lines.append("")
        lines.append("**Key Files:**")
        for f in summary.key_files:
            lines.append(f"- `{f}`")

    if summary.risk_areas:
        lines.append("")
        lines.append(":warning: **Areas Requiring Attention:**")
        for r in summary.risk_areas:
            lines.append(f"- {r}")

    if summary.test_coverage_note:
        lines.append("")
        lines.append(f"**Tests:** {summary.test_coverage_note}")

    lines.extend(
        [
            "",
            "---",
            "<sub>"
            f":page_facing_up: {result.files_reviewed} file(s) reviewed "
            f"| :speech_balloon: {len(result.comments)} comment(s) "
            f"| :stopwatch: {result.duration_seconds}s"
            "</sub>",
            "",
            "<sub>:thumbsup: / :thumbsdown: on review comments helps us improve</sub>",
        ]
    )

    if result.skipped_files:
        lines.extend(
            [
                "",
                "<details>",
                f"<summary>Skipped {len(result.skipped_files)} file(s)</summary>",
                "",
            ]
        )
        for sf in result.skipped_files[:20]:
            lines.append(f"- `{sf}`")
        if len(result.skipped_files) > 20:
            lines.append(f"- ... and {len(result.skipped_files) - 20} more")
        lines.extend(["", "</details>"])

    return "\n".join(lines)
