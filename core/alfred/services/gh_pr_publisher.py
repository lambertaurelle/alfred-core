"""GitHub Pull Request publisher with automatic fork fallback on origin push 403."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

_GITHUB_SLUG_RE = re.compile(
    r"(?:https?://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$"
)


def _extract_github_slug(remote_url: str) -> str:
    """Extract 'owner/repo' slug from a GitHub HTTPS or SSH remote URL."""
    match = _GITHUB_SLUG_RE.search(remote_url.strip())
    return match.group(1) if match else ""


def _create_pr_via_gh(
    runner: Callable[..., subprocess.CompletedProcess[str]],
    gh_bin: str,
    worktree: Path,
    env: dict[str, str],
    *,
    base_branch: str,
    head_ref: str,
    title: str,
    body: str,
    repo_slug: str = "",
    draft: bool = False,
) -> str | None:
    """Invoke 'gh pr create' and return the PR URL or None on failure."""
    cmd = [gh_bin, "pr", "create"]
    if repo_slug:
        cmd.extend(["--repo", repo_slug])
    cmd.extend(["--base", base_branch, "--head", head_ref, "--title", title, "--body", body])
    if draft:
        cmd.append("--draft")
    res = runner(cmd, cwd=worktree, env=env, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        return None
    return res.stdout.strip() or None


def _publish_via_fork(
    runner: Callable[..., subprocess.CompletedProcess[str]],
    gh_bin: str,
    worktree: Path,
    env: dict[str, str],
    *,
    branch: str,
    base_branch: str,
    title: str,
    body: str,
    origin_slug: str,
    draft: bool,
) -> str | None:
    """Fork upstream repo, push branch to fork remote, and open cross-repo GitHub PR."""
    fork_res = runner(
        [gh_bin, "repo", "fork", "--remote", "--remote-name", "fork", "--clone=false"],
        cwd=worktree, env=env, capture_output=True, text=True, check=False,
    )
    if fork_res.returncode != 0:
        return None
    fork_push = runner(
        ["git", "push", "-u", "fork", branch],
        cwd=worktree, env=env, capture_output=True, text=True, check=False,
    )
    if fork_push.returncode != 0:
        return None
    user_res = runner(
        [gh_bin, "api", "user", "--jq", ".login"],
        cwd=worktree, env=env, capture_output=True, text=True, check=False,
    )
    login = user_res.stdout.strip()
    if user_res.returncode != 0 or not login:
        return None
    return _create_pr_via_gh(
        runner, gh_bin, worktree, env,
        base_branch=base_branch,
        head_ref=f"{login}:{branch}",
        title=title,
        body=body,
        repo_slug=origin_slug,
        draft=draft,
    )


def publish_github_pr(
    worktree: Path,
    branch: str,
    base_branch: str,
    title: str,
    body: str,
    git_env: dict[str, str] | None = None,
    draft: bool = False,
    runner_fn: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    gh_locator_fn: Callable[[], str | None] | None = None,
) -> str | None:
    """Push branch and open a GitHub PR, falling back to a fork if origin push fails.

    Args:
        worktree: Isolated mono-repo git worktree path.
        branch: Local evolution branch name to publish.
        base_branch: Upstream target base branch (e.g. 'main').
        title: Pull Request title.
        body: Pull Request description body.
        git_env: Optional sanitized environment mapping for subprocess execution.
        draft: When True, open the Pull Request as a GitHub Draft PR.
        runner_fn: Optional subprocess runner test double.
        gh_locator_fn: Optional locator returning path to 'gh' CLI binary or None.

    Returns:
        Created GitHub Pull Request URL, or None when gh is missing or remote creation fails.
    """
    gh_bin = gh_locator_fn() if gh_locator_fn is not None else shutil.which("gh")
    if not gh_bin:
        return None
    runner = runner_fn or subprocess.run
    env = dict(os.environ) if git_env is None else dict(git_env)
    try:
        remote_chk = runner(
            ["git", "remote", "get-url", "origin"],
            cwd=worktree, env=env, capture_output=True, text=True, check=False,
        )
        if remote_chk.returncode != 0:
            return None
        origin_slug = _extract_github_slug(remote_chk.stdout)
        push_res = runner(
            ["git", "push", "-u", "origin", branch],
            cwd=worktree, env=env, capture_output=True, text=True, check=False,
        )
        if push_res.returncode == 0:
            return _create_pr_via_gh(
                runner, gh_bin, worktree, env,
                base_branch=base_branch, head_ref=branch, title=title, body=body, draft=draft,
            )
        return _publish_via_fork(
            runner, gh_bin, worktree, env,
            branch=branch, base_branch=base_branch, title=title, body=body,
            origin_slug=origin_slug, draft=draft,
        )
    except (OSError, subprocess.SubprocessError):
        return None
