"""Session state: dataclass + persisted JSON."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

State = Literal[
    "queued", "fetching", "branching", "running",
    "awaiting_reply", "pushing", "done", "failed", "cancelled",
]


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_sid(ticket: str, repo: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ticket}-{repo}-{ts}"


@dataclass
class Session:
    id: str
    ticket: str
    repo: str            # "backend" | "frontend"
    repo_path: str
    branch: str
    base: str
    state: State = "queued"
    step: str = ""
    started_at: str = field(default_factory=_utcnow_iso)
    ended_at: Optional[str] = None
    pid: Optional[int] = None
    pending_ask_id: Optional[str] = None
    pr_url: Optional[str] = None
    error: Optional[str] = None
    # both-mode linkage
    depends_on: Optional[str] = None  # session id of backend
    paired_repo: Optional[str] = None  # "frontend" or "backend"

    @property
    def dir(self) -> Path:
        from agent.config import load
        return load().sessions_dir / self.id

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / "session.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        os.replace(tmp, path)

    def mark(self, state: State, step: str = "") -> None:
        self.state = state
        self.step = step
        if state in {"done", "failed", "cancelled"}:
            self.ended_at = _utcnow_iso()
        self.save()

    @classmethod
    def load(cls, sid: str, sessions_dir: Path) -> "Session":
        path = sessions_dir / sid / "session.json"
        return cls(**json.loads(path.read_text()))

    @classmethod
    def find_active(cls, sessions_dir: Path) -> list["Session"]:
        active_states = {"running", "awaiting_reply", "pushing", "fetching", "branching"}
        out = []
        for sid_dir in sessions_dir.glob("*"):
            sj = sid_dir / "session.json"
            if not sj.exists():
                continue
            try:
                s = cls(**json.loads(sj.read_text()))
                if s.state in active_states:
                    out.append(s)
            except Exception:
                continue
        return out
