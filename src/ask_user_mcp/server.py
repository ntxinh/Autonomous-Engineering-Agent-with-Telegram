"""FastMCP stdio server exposing ask_user, notify_user, report_blocker.
Connects back to the orchestrator over a Unix domain socket."""
from __future__ import annotations

import json
import os
import socket
import time
import uuid
from typing import Literal

from fastmcp import FastMCP

SOCKET_PATH = os.environ.get("AGENT_BOT_SOCKET", "")
SESSION_ID = os.environ.get("AGENT_SESSION_ID", "")

mcp = FastMCP("ask-user")


def _connect_with_retry() -> socket.socket:
    """Open UDS connection. Retry with exp backoff (5 attempts) for orchestrator restarts."""
    delay = 0.5
    last_err: Exception | None = None
    for _ in range(5):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(SOCKET_PATH)
            return s
        except (FileNotFoundError, ConnectionRefusedError) as e:
            last_err = e
            time.sleep(delay)
            delay = min(delay * 2, 8.0)
    raise RuntimeError(f"operator channel unreachable: {last_err}")


def _send_and_wait(frame: dict) -> str:
    s = _connect_with_retry()
    try:
        s.sendall((json.dumps(frame) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                raise RuntimeError("operator channel closed before reply")
            buf += chunk
        line = buf.split(b"\n", 1)[0]
        resp = json.loads(line.decode("utf-8"))
        if "error" in resp:
            raise RuntimeError(f"operator: {resp['error']}")
        return resp.get("reply", "")
    finally:
        s.close()


def _send_oneway(frame: dict) -> None:
    s = _connect_with_retry()
    try:
        s.sendall((json.dumps(frame) + "\n").encode("utf-8"))
    finally:
        s.close()


@mcp.tool()
def ask_user(question: str, urgency: Literal["normal", "high"] = "normal") -> str:
    """Ask the human operator a clarifying question via Telegram and wait for reply.

    Use only when truly blocked. Prefer reasonable assumptions documented in
    code or PR description over interrupting the operator.
    """
    frame = {
        "v": 1, "type": "ask", "session_id": SESSION_ID,
        "id": str(uuid.uuid4()), "question": question, "urgency": urgency,
    }
    return _send_and_wait(frame)


@mcp.tool()
def notify_user(
    message: str,
    kind: Literal["progress", "milestone", "warning"] = "milestone",
) -> None:
    """Send a one-line status to the operator on Telegram. Milestones only."""
    _send_oneway({
        "v": 1, "type": "notify", "session_id": SESSION_ID,
        "message": message, "kind": kind,
    })


@mcp.tool()
def report_blocker(reason: str) -> None:
    """Mark session as blocked and halt. Operator must intervene with /cancel."""
    _send_oneway({
        "v": 1, "type": "blocker", "session_id": SESSION_ID,
        "reason": reason,
    })


def main() -> None:
    if not SOCKET_PATH or not SESSION_ID:
        raise SystemExit("AGENT_BOT_SOCKET and AGENT_SESSION_ID must be set")
    mcp.run()


if __name__ == "__main__":
    main()
