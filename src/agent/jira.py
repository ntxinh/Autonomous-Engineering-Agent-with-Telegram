"""Async Jira REST client + ADF→markdown converter."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx


@dataclass
class Attachment:
    filename: str
    content_url: str
    mime: str
    local_path: Optional[Path] = None


@dataclass
class Issue:
    key: str
    title: str
    description: str            # markdown
    attachments: list[Attachment]


def _adf_to_md(node: Any, depth: int = 0) -> str:
    """Recursive ADF → markdown. Covers paragraph, heading, lists, code, link, mediaSingle, mention."""
    if node is None:
        return ""
    if isinstance(node, list):
        return "".join(_adf_to_md(n, depth) for n in node)

    t = node.get("type")
    content = node.get("content", [])

    if t == "doc":
        return _adf_to_md(content, depth)
    if t == "paragraph":
        return _adf_to_md(content, depth) + "\n\n"
    if t == "heading":
        level = node.get("attrs", {}).get("level", 1)
        return "#" * level + " " + _adf_to_md(content, depth).strip() + "\n\n"
    if t == "text":
        text = node.get("text", "")
        for mark in node.get("marks", []):
            mt = mark.get("type")
            if mt == "strong":
                text = f"**{text}**"
            elif mt == "em":
                text = f"*{text}*"
            elif mt == "code":
                text = f"`{text}`"
            elif mt == "link":
                href = mark.get("attrs", {}).get("href", "")
                text = f"[{text}]({href})"
        return text
    if t == "bulletList":
        return "".join(
            "  " * depth + "- " + _adf_to_md(item.get("content", []), depth + 1).rstrip() + "\n"
            for item in content
        ) + "\n"
    if t == "orderedList":
        return "".join(
            "  " * depth + f"{i+1}. " + _adf_to_md(item.get("content", []), depth + 1).rstrip() + "\n"
            for i, item in enumerate(content)
        ) + "\n"
    if t == "listItem":
        return _adf_to_md(content, depth)
    if t == "codeBlock":
        lang = node.get("attrs", {}).get("language", "")
        body = _adf_to_md(content, depth)
        return f"```{lang}\n{body}\n```\n\n"
    if t == "blockquote":
        inner = _adf_to_md(content, depth).strip()
        return "\n".join(f"> {line}" for line in inner.splitlines()) + "\n\n"
    if t == "hardBreak":
        return "\n"
    if t == "rule":
        return "\n---\n\n"
    if t == "mediaSingle" or t == "media":
        # leave a marker — actual image paths injected later from attachment list
        filename = node.get("attrs", {}).get("alt") or node.get("attrs", {}).get("id", "image")
        return f"![{filename}](.agent-attachments/{filename})\n\n"
    if t == "mention":
        return node.get("attrs", {}).get("text", "@user")
    # unknown → preserve children
    return _adf_to_md(content, depth)


def adf_to_markdown(adf: Any) -> str:
    if adf is None:
        return ""
    return _adf_to_md(adf).strip() + "\n"


class JiraClient:
    def __init__(self, base_url: str, email: str, token: str):
        self.base = base_url.rstrip("/")
        self.auth = (email, token)

    async def fetch_issue(self, key: str) -> Issue:
        async with httpx.AsyncClient(auth=self.auth, timeout=30) as c:
            r = await c.get(
                f"{self.base}/rest/api/3/issue/{key}",
                params={"fields": "summary,description,attachment"},
            )
            r.raise_for_status()
            data = r.json()

        fields = data["fields"]
        atts = [
            Attachment(
                filename=a["filename"],
                content_url=a["content"],
                mime=a.get("mimeType", "application/octet-stream"),
            )
            for a in (fields.get("attachment") or [])
        ]
        return Issue(
            key=key,
            title=fields["summary"],
            description=adf_to_markdown(fields.get("description")),
            attachments=atts,
        )

    async def download_attachments(self, issue: Issue, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(auth=self.auth, timeout=60, follow_redirects=True) as c:
            for a in issue.attachments:
                r = await c.get(a.content_url)
                r.raise_for_status()
                target = dest / a.filename
                target.write_bytes(r.content)
                a.local_path = target
