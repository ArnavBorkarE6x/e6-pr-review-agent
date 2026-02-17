"""GitHub API client for the Action environment.

Uses the GITHUB_TOKEN provided automatically by GitHub Actions —
no App registration, no JWT, no installation tokens needed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from agent.diff_parser import (
    count_tokens,
    detect_language,
    is_binary,
    should_skip_file,
)
from agent.models import FileDiff, ReviewComment

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


class GitHubClient:
    """Thin async wrapper around the GitHub REST API."""

    def __init__(self, token: str) -> None:
        self._http = httpx.AsyncClient(
            base_url=GITHUB_API,
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    # ── Read PR data ────────────────────────────────────────────────────

    async def get_pr(self, repo: str, pr_number: int) -> dict:
        """Fetch full PR metadata."""
        resp = await self._http.get(f"/repos/{repo}/pulls/{pr_number}")
        resp.raise_for_status()
        return resp.json()

    async def get_pr_files(
        self,
        repo: str,
        pr_number: int,
        ignore_patterns: list[str],
        max_files: int = 50,
    ) -> tuple[list[FileDiff], list[str]]:
        """Fetch changed files for a PR with filtering.

        Returns (reviewable_files, skipped_filenames).
        """
        files: list[FileDiff] = []
        skipped: list[str] = []
        page = 1

        while True:
            resp = await self._http.get(
                f"/repos/{repo}/pulls/{pr_number}/files",
                params={"per_page": 100, "page": page},
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break

            for f in batch:
                filename = f["filename"]

                if is_binary(filename):
                    skipped.append(f"{filename} (binary)")
                    continue
                if should_skip_file(filename, ignore_patterns):
                    skipped.append(f"{filename} (ignored)")
                    continue

                patch = f.get("patch", "")
                if not patch:
                    skipped.append(f"{filename} (no diff)")
                    continue

                files.append(
                    FileDiff(
                        filename=filename,
                        status=f["status"],
                        additions=f.get("additions", 0),
                        deletions=f.get("deletions", 0),
                        patch=patch,
                        previous_filename=f.get("previous_filename"),
                        language=detect_language(filename),
                        token_count=count_tokens(patch),
                    )
                )

            page += 1
            if len(files) + len(skipped) >= max_files * 3:
                break

        if len(files) > max_files:
            overflow = files[max_files:]
            skipped.extend(f"{f.filename} (over limit)" for f in overflow)
            files = files[:max_files]

        return files, skipped

    # ── Cleanup old reviews ─────────────────────────────────────────────

    MARKER = "<!-- e6data-review-agent -->"

    async def cleanup_previous_reviews(self, repo: str, pr_number: int) -> None:
        """Delete all comments from previous runs of this agent.

        Removes:
        1. PR issue comments with our marker (summary comments)
        2. PR review comments (inline) from the bot user
        """
        deleted = 0

        # 1. Delete old summary comments (issue comments with our marker)
        page = 1
        while True:
            resp = await self._http.get(
                f"/repos/{repo}/issues/{pr_number}/comments",
                params={"per_page": 100, "page": page},
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            for comment in batch:
                if self.MARKER in (comment.get("body") or ""):
                    del_resp = await self._http.delete(
                        f"/repos/{repo}/issues/comments/{comment['id']}"
                    )
                    if del_resp.status_code == 204:
                        deleted += 1
            page += 1

        # 2. Delete old inline review comments from the bot
        page = 1
        while True:
            resp = await self._http.get(
                f"/repos/{repo}/pulls/{pr_number}/comments",
                params={"per_page": 100, "page": page},
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            for comment in batch:
                user = comment.get("user", {})
                user_login = user.get("login", "")
                # Bot comments via GITHUB_TOKEN come from github-actions[bot]
                if user_login == "github-actions[bot]" or user.get("type") == "Bot":
                    del_resp = await self._http.delete(
                        f"/repos/{repo}/pulls/comments/{comment['id']}"
                    )
                    if del_resp.status_code == 204:
                        deleted += 1
            page += 1

        if deleted:
            log.info("Cleaned up %d comments from previous review run", deleted)

    # ── Write review ────────────────────────────────────────────────────

    async def post_review(
        self,
        repo: str,
        pr_number: int,
        head_sha: str,
        body: str,
        comments: list[ReviewComment],
    ) -> int:
        """Submit a pull request review with inline comments.

        Posts the summary as an issue comment (deletable on re-run)
        and inline comments as a PR review.
        """
        # Post summary as issue comment with marker
        marked_body = f"{self.MARKER}\n{body}"
        resp = await self._http.post(
            f"/repos/{repo}/issues/{pr_number}/comments",
            json={"body": marked_body},
        )
        resp.raise_for_status()

        # Post inline comments as a review (if any)
        review_id = 0
        if comments:
            review_comments = [
                {"path": c.path, "line": c.line, "side": c.side, "body": c.body} for c in comments
            ]
            payload: dict = {
                "commit_id": head_sha,
                "event": "COMMENT",
                "body": "",
                "comments": review_comments,
            }
            resp = await self._http.post(
                f"/repos/{repo}/pulls/{pr_number}/reviews",
                json=payload,
            )
            resp.raise_for_status()
            review_id = resp.json().get("id", 0)

        log.info("Posted summary + review %d with %d inline comments", review_id, len(comments))
        return review_id

    async def close(self) -> None:
        await self._http.aclose()
