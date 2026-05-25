"""Git + gh CLI operations. All subprocess; no GitPython dependency."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def _run(cmd: list[str], cwd: Path, env: dict | None = None) -> str:
    r = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, env=env,
    )
    if r.returncode != 0:
        raise GitError(f"{' '.join(cmd)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def slugify(title: str, max_len: int = 40) -> str:
    s = title.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:max_len].rstrip("-") or "untitled"


def branch_name(ticket: str, title: str) -> str:
    return f"agent/{ticket}-{slugify(title)}"


def preflight(repo: Path, base: str, branch: str, gh_env: dict) -> None:
    if not (repo / ".git").exists():
        raise GitError(f"{repo} is not a git repo")
    # working tree clean
    status = _run(["git", "status", "--porcelain"], repo)
    if status:
        raise GitError("working tree not clean")
    _run(["git", "fetch", "origin", base], repo)
    # branch must not exist locally or remotely
    local = subprocess.run(
        ["git", "rev-parse", "--verify", branch],
        cwd=repo, capture_output=True, text=True,
    )
    if local.returncode == 0:
        raise GitError(f"local branch {branch} already exists")
    remote_ls = _run(["git", "ls-remote", "--heads", "origin", branch], repo)
    if remote_ls:
        raise GitError(f"remote branch {branch} already exists")
    # gh auth
    gh_check = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, env=gh_env)
    if gh_check.returncode != 0:
        raise GitError(f"gh auth failed: {gh_check.stderr.strip()}")


def prepare_branch(repo: Path, base: str, branch: str) -> None:
    _run(["git", "fetch", "origin", base], repo)
    _run(["git", "switch", "--detach", f"origin/{base}"], repo)
    _run(["git", "switch", "-c", branch], repo)


def push_and_pr(
    repo: Path,
    branch: str,
    base: str,
    title: str,
    body: str,
    gh_env: dict,
) -> str:
    _run(["git", "push", "-u", "origin", branch], repo)
    # write body to temp file
    body_file = repo / ".agent-pr-body.tmp"
    body_file.write_text(body)
    try:
        url = _run(
            ["gh", "pr", "create", "--base", base, "--head", branch,
             "--title", title, "--body-file", str(body_file)],
            repo, env=gh_env,
        )
    finally:
        body_file.unlink(missing_ok=True)
    return url


def render_pr_body(ticket: str, title: str, summary: str, sid: str, branch: str,
                   base: str, jira_base: str) -> str:
    return f"""## Jira
[{ticket}]({jira_base}/browse/{ticket}) — {title}

## Summary
{summary or "_no summary captured_"}

## Session
- Agent session: `{sid}`
- Base: `{base}`
- Branch: `{branch}`

🤖 Generated via autonomous agent
"""
