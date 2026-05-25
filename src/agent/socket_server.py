"""Unix-domain-socket server that brokers messages between the MCP server (in
Claude's process tree) and the orchestrator.

Frame format: one JSON object per line. See spec §4.2.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional


AskCb = Callable[[str, str, str, str], Awaitable[None]]    # (sid, ask_id, question, urgency)
NotifyCb = Callable[[str, str, str], Awaitable[None]]      # (sid, message, kind)
BlockerCb = Callable[[str, str], Awaitable[None]]          # (sid, reason)


@dataclass
class PendingAsk:
    ask_id: str
    sid: str
    future: asyncio.Future


class SocketServer:
    def __init__(
        self,
        path: Path,
        on_ask: AskCb,
        on_notify: NotifyCb,
        on_blocker: BlockerCb,
    ):
        self.path = path
        self.on_ask = on_ask
        self.on_notify = on_notify
        self.on_blocker = on_blocker
        self._server: Optional[asyncio.AbstractServer] = None
        # ask_id → (writer, future)
        self._pending: dict[str, tuple[asyncio.StreamWriter, asyncio.Future]] = {}

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.path.unlink()
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.path))
        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        os.chmod(self.path.parent, stat.S_IRWXU)          # 0700

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        # cancel outstanding asks
        for _, (_, fut) in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(RuntimeError("socket server stopped"))
        self._pending.clear()
        self.path.unlink(missing_ok=True)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    frame = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                ftype = frame.get("type")
                sid = frame.get("session_id", "")
                if ftype == "ask":
                    ask_id = frame.get("id") or str(uuid.uuid4())
                    fut: asyncio.Future = asyncio.get_running_loop().create_future()
                    self._pending[ask_id] = (writer, fut)
                    try:
                        await self.on_ask(sid, ask_id, frame.get("question", ""),
                                          frame.get("urgency", "normal"))
                    except Exception as e:
                        if not fut.done():
                            fut.set_exception(e)
                    try:
                        reply = await fut
                        writer.write((json.dumps({"v": 1, "id": ask_id, "reply": reply}) + "\n").encode())
                    except Exception as e:
                        writer.write((json.dumps({"v": 1, "id": ask_id, "error": str(e)}) + "\n").encode())
                    finally:
                        self._pending.pop(ask_id, None)
                        await writer.drain()
                elif ftype == "notify":
                    try:
                        await self.on_notify(sid, frame.get("message", ""), frame.get("kind", "milestone"))
                    except Exception:
                        pass
                elif ftype == "blocker":
                    try:
                        await self.on_blocker(sid, frame.get("reason", ""))
                    except Exception:
                        pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def deliver_reply(self, ask_id: str, reply: str) -> bool:
        entry = self._pending.get(ask_id)
        if not entry:
            return False
        _, fut = entry
        if not fut.done():
            fut.set_result(reply)
        return True

    async def cancel_ask(self, ask_id: str) -> bool:
        entry = self._pending.get(ask_id)
        if not entry:
            return False
        _, fut = entry
        if not fut.done():
            fut.set_exception(RuntimeError("cancelled"))
        return True

    def has_pending(self, sid: str) -> Optional[str]:
        for ask_id, (_, _) in self._pending.items():
            # we don't track sid per ask currently; serial mode → at most one
            return ask_id
        return None
