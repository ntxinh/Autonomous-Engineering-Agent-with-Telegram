"""Spawn Claude Code as a subprocess. Stream stdout (JSONL stream-json) into
session log files. Returns exit code and final summary text."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional


CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")


@dataclass
class RunResult:
    exit_code: int
    final_message: str          # last assistant message content (used for PR summary)


ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "USER", "SHELL", "TERM")


def build_env(claude_token: str, session_id: str, socket_path: Path) -> dict:
    env = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
    env["CLAUDE_CODE_OAUTH_TOKEN"] = claude_token
    env["AGENT_SESSION_ID"] = session_id
    env["AGENT_BOT_SOCKET"] = str(socket_path)
    return env


def write_mcp_config(session_dir: Path, agent_home: Path, socket_path: Path,
                     session_id: str) -> Path:
    cfg = {
        "mcpServers": {
            "ask_user": {
                "command": "uv",
                "args": ["run", "--project", str(agent_home), "ask-user-mcp"],
                "env": {
                    "AGENT_BOT_SOCKET": str(socket_path),
                    "AGENT_SESSION_ID": session_id,
                },
            }
        }
    }
    path = session_dir / ".mcp.json"
    path.write_text(json.dumps(cfg, indent=2))
    return path


async def run(
    prompt_path: Path,
    mcp_config: Path,
    repo_path: Path,
    session_dir: Path,
    env: dict,
    pid_callback: Optional[Callable[[int], Awaitable[None]]] = None,
    stdout_cb: Optional[Callable[[str], Awaitable[None]]] = None,
) -> RunResult:
    """Spawn Claude, stream stdout to claude.jsonl + claude.log. Capture final summary."""
    prompt_text = prompt_path.read_text()

    cmd = [
        CLAUDE_BIN,
        "-p", prompt_text,
        "--output-format", "stream-json",
        "--verbose",
        "--mcp-config", str(mcp_config),
        "--permission-mode", "acceptEdits",
        "--add-dir", str(repo_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(repo_path),
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    if pid_callback and proc.pid:
        await pid_callback(proc.pid)

    jsonl_path = session_dir / "claude.jsonl"
    log_path = session_dir / "claude.log"
    final_message = ""

    session_dir.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("a", encoding="utf-8") as jf, log_path.open("a", encoding="utf-8") as lf:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace")
            jf.write(line)
            jf.flush()
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                lf.write(line)
                lf.flush()
                continue
            # extract human-readable text
            etype = evt.get("type")
            if etype == "assistant":
                msg = evt.get("message", {})
                for block in msg.get("content", []):
                    if block.get("type") == "text":
                        text = block.get("text", "")
                        lf.write(text + "\n")
                        final_message = text
                    elif block.get("type") == "tool_use":
                        name = block.get("name", "?")
                        lf.write(f"[tool_use:{name}]\n")
            elif etype == "user":
                # tool results
                lf.write("[tool_result]\n")
            elif etype == "result":
                lf.write(f"[result: {evt.get('subtype', '')}]\n")
            lf.flush()
            if stdout_cb:
                await stdout_cb(line)

    rc = await proc.wait()
    return RunResult(exit_code=rc, final_message=final_message[:500])
