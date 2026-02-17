"""Entrypoint for the GitHub Action.

Reads environment variables set by the workflow, runs the review pipeline,
and posts the results back to the PR. Exit code 0 = success, 1 = failure.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from agent.ai_client import AIClient
from agent.diff_parser import DEFAULT_IGNORE_PATTERNS
from agent.github_client import GitHubClient
from agent.reviewer import ReviewEngine, format_summary_body

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("e6data-review")

# ── Configuration from environment / Action inputs ──────────────────────

IGNORE_AUTHORS = {
    "dependabot[bot]",
    "renovate[bot]",
    "github-actions[bot]",
}


def _get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_env_required(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        log.error("Required environment variable %s is not set", name)
        sys.exit(1)
    return val


def _get_ignore_patterns() -> list[str]:
    extra = _get_env("INPUT_IGNORE_PATTERNS", "")
    patterns = list(DEFAULT_IGNORE_PATTERNS)
    if extra:
        patterns.extend(p.strip() for p in extra.split(",") if p.strip())
    return patterns


# ── Main ────────────────────────────────────────────────────────────────


async def run() -> None:
    github_token = _get_env_required("GITHUB_TOKEN")
    repo = _get_env_required("GITHUB_REPOSITORY")
    pr_number = int(_get_env_required("PR_NUMBER"))

    # AI config
    ai_provider = _get_env("INPUT_AI_PROVIDER", "anthropic")
    if ai_provider == "anthropic":
        api_key = _get_env_required("ANTHROPIC_API_KEY")
        model_primary = _get_env("INPUT_MODEL_PRIMARY", "claude-sonnet-4-5-20250929")
        model_light = _get_env("INPUT_MODEL_LIGHTWEIGHT", "claude-haiku-4-5-20251001")
    else:
        api_key = _get_env_required("OPENAI_API_KEY")
        model_primary = _get_env("INPUT_MODEL_PRIMARY", "gpt-4o")
        model_light = _get_env("INPUT_MODEL_LIGHTWEIGHT", "gpt-4o-mini")

    max_files = int(_get_env("INPUT_MAX_FILES", "50"))
    skip_drafts = _get_env("INPUT_SKIP_DRAFTS", "true").lower() == "true"
    max_diff_tokens = int(_get_env("INPUT_MAX_DIFF_TOKENS", "120000"))
    suggest_logging = _get_env("INPUT_SUGGEST_LOGGING", "true").lower() == "true"

    gh = GitHubClient(github_token)
    try:
        await _run_review(
            gh=gh,
            repo=repo,
            pr_number=pr_number,
            ai_provider=ai_provider,
            api_key=api_key,
            model_primary=model_primary,
            model_light=model_light,
            max_files=max_files,
            skip_drafts=skip_drafts,
            max_diff_tokens=max_diff_tokens,
            suggest_logging=suggest_logging,
        )
    finally:
        await gh.close()


async def _run_review(
    *,
    gh: GitHubClient,
    repo: str,
    pr_number: int,
    ai_provider: str,
    api_key: str,
    model_primary: str,
    model_light: str,
    max_files: int,
    skip_drafts: bool,
    max_diff_tokens: int,
    suggest_logging: bool = True,
) -> None:
    # ── Fetch PR metadata ───────────────────────────────────────────────
    pr = await gh.get_pr(repo, pr_number)
    author = pr["user"]["login"]
    head_sha = pr["head"]["sha"]
    title = pr["title"]
    body = pr.get("body")
    is_draft = pr.get("draft", False)

    log.info("PR #%d: %s by %s (%d files)", pr_number, title, author, pr["changed_files"])

    # ── Gatekeeper ──────────────────────────────────────────────────────
    if skip_drafts and is_draft:
        log.info("Skipping draft PR #%d", pr_number)
        return

    if author in IGNORE_AUTHORS or pr["user"].get("type") == "Bot":
        log.info("Skipping bot PR #%d by %s", pr_number, author)
        return

    # ── Fetch files ─────────────────────────────────────────────────────
    ignore_patterns = _get_ignore_patterns()
    files, skipped = await gh.get_pr_files(repo, pr_number, ignore_patterns, max_files)

    if not files:
        log.info("No reviewable files in PR #%d", pr_number)
        return

    # ── Run review engine ───────────────────────────────────────────────
    ai = AIClient(
        provider=ai_provider,
        api_key=api_key,
        model_primary=model_primary,
        model_lightweight=model_light,
    )
    engine = ReviewEngine(ai=ai, max_diff_tokens=max_diff_tokens, suggest_logging=suggest_logging)

    result = await engine.review(
        pr_number=pr_number,
        repo=repo,
        head_sha=head_sha,
        title=title,
        body=body,
        author=author,
        additions=pr.get("additions", 0),
        deletions=pr.get("deletions", 0),
        files=files,
        skipped_files=skipped,
    )

    # ── Post review to GitHub ───────────────────────────────────────────
    summary_body = format_summary_body(result.summary, result)

    await gh.post_review(
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        body=summary_body,
        comments=result.comments,
    )

    log.info(
        "Done: %d comments posted, %.1fs total",
        len(result.comments),
        result.duration_seconds,
    )


def main() -> None:
    try:
        asyncio.run(run())
    except Exception:
        log.exception("Review failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
