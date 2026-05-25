"""Load and validate config.toml + .env vars."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RepoCfg:
    name: str
    path: Path
    base_branch: str
    stack: str


@dataclass(frozen=True)
class Config:
    sessions_dir: Path
    socket_path: Path
    queue_file: Path
    log_file: Path
    agent_home: Path
    repos: dict[str, RepoCfg]
    notify_level: str
    log_tail_default: int
    parse_mode: str
    retain_success_days: int
    idle_nudge_minutes: int

    # secrets from env
    telegram_bot_token: str
    telegram_owner_chat_id: int
    claude_oauth_token: str
    jira_base_url: str
    jira_email: str
    jira_api_token: str
    gh_token: str


REQUIRED_ENV = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_OWNER_CHAT_ID",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "JIRA_BASE_URL",
    "JIRA_EMAIL",
    "JIRA_API_TOKEN",
    "GH_TOKEN",
)


def _xp(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve()


def load(config_path: Path | None = None) -> Config:
    config_path = config_path or Path(__file__).resolve().parents[2] / "config.toml"
    with open(config_path, "rb") as f:
        raw = tomllib.load(f)

    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"missing env vars: {', '.join(missing)}")

    repos = {
        name: RepoCfg(
            name=name,
            path=_xp(r["path"]),
            base_branch=r["base_branch"],
            stack=r["stack"],
        )
        for name, r in raw["repos"].items()
    }

    return Config(
        sessions_dir=_xp(raw["paths"]["sessions_dir"]),
        socket_path=_xp(raw["paths"]["socket_path"]),
        queue_file=_xp(raw["paths"]["queue_file"]),
        log_file=_xp(raw["paths"]["log_file"]),
        agent_home=_xp(raw["paths"]["agent_home"]),
        repos=repos,
        notify_level=raw["telegram"]["notify_level"],
        log_tail_default=raw["telegram"]["log_tail_default"],
        parse_mode=raw["telegram"]["parse_mode"],
        retain_success_days=raw["session"]["retain_success_days"],
        idle_nudge_minutes=raw["session"]["idle_nudge_minutes"],
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        telegram_owner_chat_id=int(os.environ["TELEGRAM_OWNER_CHAT_ID"]),
        claude_oauth_token=os.environ["CLAUDE_CODE_OAUTH_TOKEN"],
        jira_base_url=os.environ["JIRA_BASE_URL"].rstrip("/"),
        jira_email=os.environ["JIRA_EMAIL"],
        jira_api_token=os.environ["JIRA_API_TOKEN"],
        gh_token=os.environ["GH_TOKEN"],
    )
