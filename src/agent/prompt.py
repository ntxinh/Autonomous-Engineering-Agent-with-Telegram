"""Render the per-session prompt fed to Claude."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from agent.jira import Issue


@dataclass
class PromptCtx:
    ticket: str
    title: str
    description: str
    attachments: list[dict]   # [{"path": "...", "mime": "..."}]
    repo_name: str
    repo_path: str
    stack: str
    base: str
    branch: str
    session_dir: str


def render(ctx: PromptCtx, templates_dir: Path) -> str:
    env = Environment(
        loader=FileSystemLoader(templates_dir),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    tpl = env.get_template("prompt.md.j2")
    return tpl.render(**ctx.__dict__)


def build_ctx(
    issue: Issue,
    repo_name: str,
    repo_path: Path,
    stack: str,
    base: str,
    branch: str,
    session_dir: Path,
    attachments_in_repo: Path,
) -> PromptCtx:
    atts = [
        {"path": str(attachments_in_repo / a.filename), "mime": a.mime}
        for a in issue.attachments
        if a.local_path is not None
    ]
    return PromptCtx(
        ticket=issue.key,
        title=issue.title,
        description=issue.description,
        attachments=atts,
        repo_name=repo_name,
        repo_path=str(repo_path),
        stack=stack,
        base=base,
        branch=branch,
        session_dir=str(session_dir),
    )
