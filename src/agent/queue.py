"""Persistent FIFO queue backed by queue.json. Async-friendly."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


@dataclass
class Job:
    ticket: str
    repo: str                       # "backend" | "frontend"
    enqueued_by: int                # telegram chat_id (for audit)
    enqueued_at: str
    depends_on_ticket: Optional[str] = None
    depends_on_repo: Optional[str] = None


class PersistentQueue:
    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()
        self._cond = asyncio.Condition(self._lock)
        self._items: list[Job] = self._load()

    def _load(self) -> list[Job]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text())
        return [Job(**r) for r in raw]

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(j) for j in self._items], indent=2))
        os.replace(tmp, self.path)

    async def enqueue(self, job: Job) -> int:
        async with self._cond:
            self._items.append(job)
            self._persist()
            pos = len(self._items)
            self._cond.notify_all()
        return pos

    async def dequeue(self) -> Job:
        async with self._cond:
            while True:
                for i, job in enumerate(self._items):
                    if job.depends_on_ticket is None:
                        self._items.pop(i)
                        self._persist()
                        return job
                    sibling_pending = any(
                        (s.ticket == job.depends_on_ticket and s.repo == job.depends_on_repo)
                        for s in self._items
                    )
                    if not sibling_pending:
                        self._items.pop(i)
                        self._persist()
                        return job
                await self._cond.wait()

    async def list(self) -> list[Job]:
        async with self._lock:
            return list(self._items)

    async def remove_by_ticket(self, ticket: str, repo: Optional[str] = None) -> int:
        async with self._cond:
            before = len(self._items)
            self._items = [
                j for j in self._items
                if not (j.ticket == ticket and (repo is None or j.repo == repo))
            ]
            removed = before - len(self._items)
            if removed:
                self._persist()
                self._cond.notify_all()
        return removed

    async def notify(self) -> None:
        """Wake dequeue waiters when a dependency just finished."""
        async with self._cond:
            self._cond.notify_all()
