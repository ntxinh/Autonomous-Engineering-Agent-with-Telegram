"""Telegram command handlers. Dispatched from bot.py."""
from __future__ import annotations

import json
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import ContextTypes

from agent.queue import Job
from agent.session import Session

if TYPE_CHECKING:
    from agent.bot import BotState


def _owner_only(state: "BotState", update: Update) -> bool:
    chat = update.effective_chat
    if chat is None or chat.id != state.cfg.telegram_owner_chat_id:
        return False
    return True


HELP_TEXT = (
    "<b>Autonomous Agent — commands</b>\n"
    "/start &lt;TICKET&gt; &lt;backend|frontend|both&gt; — enqueue a ticket\n"
    "/status — current session summary\n"
    "/log [N] — tail last N lines of claude.log (default 50)\n"
    "/cancel — kill current Claude subprocess\n"
    "/queue — list queued jobs\n"
    "/confirm-frontend &lt;TICKET&gt; — after backend failure, run frontend anyway\n"
    "/skip &lt;TICKET&gt; — after backend failure, drop the frontend job\n"
    "/help — this message"
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def handle_start(state: "BotState", update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(state, update):
        return
    msg = update.effective_message
    assert msg is not None
    parts = shlex.split(msg.text or "")
    if len(parts) != 3:
        await msg.reply_text("Usage: /start <TICKET-ID> <backend|frontend|both>")
        return
    _, ticket, repo = parts
    ticket = ticket.strip().upper()
    repo = repo.strip().lower()
    if repo not in {"backend", "frontend", "both"}:
        await msg.reply_text("repo must be backend, frontend, or both")
        return

    chat_id = update.effective_chat.id  # type: ignore

    if repo == "both":
        pos1 = await state.queue.enqueue(Job(
            ticket=ticket, repo="backend",
            enqueued_by=chat_id, enqueued_at=_utcnow_iso(),
        ))
        pos2 = await state.queue.enqueue(Job(
            ticket=ticket, repo="frontend",
            enqueued_by=chat_id, enqueued_at=_utcnow_iso(),
            depends_on_ticket=ticket, depends_on_repo="backend",
        ))
        await msg.reply_text(
            f"Queued {ticket} backend (pos {pos1}) and frontend (pos {pos2}, depends on backend)."
        )
    else:
        pos = await state.queue.enqueue(Job(
            ticket=ticket, repo=repo,
            enqueued_by=chat_id, enqueued_at=_utcnow_iso(),
        ))
        await msg.reply_text(f"Queued {ticket} {repo} (pos {pos}).")


async def handle_status(state: "BotState", update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(state, update):
        return
    msg = update.effective_message
    assert msg is not None
    if state.current is None:
        await msg.reply_text("idle")
        return
    s = state.current
    txt = (
        f"<b>{s.id}</b>\n"
        f"ticket: {s.ticket}\n"
        f"repo: {s.repo}\n"
        f"branch: {s.branch}\n"
        f"state: {s.state}\n"
        f"step: {s.step or '-'}\n"
        f"started: {s.started_at}\n"
        f"pid: {s.pid or '-'}"
    )
    await msg.reply_html(txt)


async def handle_log(state: "BotState", update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(state, update):
        return
    msg = update.effective_message
    assert msg is not None
    parts = shlex.split(msg.text or "")
    n = state.cfg.log_tail_default
    if len(parts) >= 2:
        try:
            n = max(1, min(500, int(parts[1])))
        except ValueError:
            pass
    if state.current is None:
        await msg.reply_text("idle (no current session)")
        return
    log_file = state.cfg.sessions_dir / state.current.id / "claude.log"
    if not log_file.exists():
        await msg.reply_text("log file not yet created")
        return
    lines = log_file.read_text(errors="replace").splitlines()[-n:]
    body = "\n".join(lines) or "(empty)"
    # chunk under 4096 chars (HTML pre wrapper adds ~12)
    chunk_size = 3900
    for i in range(0, len(body), chunk_size):
        await msg.reply_html(f"<pre>{_html_escape(body[i:i+chunk_size])}</pre>")


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def handle_cancel(state: "BotState", update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(state, update):
        return
    msg = update.effective_message
    assert msg is not None
    if state.current is None:
        await msg.reply_text("nothing running")
        return
    session_id = state.current.id
    await state.cancel_current()
    await msg.reply_text(f"cancellation requested for {session_id}")


async def handle_queue(state: "BotState", update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(state, update):
        return
    msg = update.effective_message
    assert msg is not None
    items = await state.queue.list()
    if not items:
        await msg.reply_text("queue empty")
        return
    lines = [
        f"{i+1}. {j.ticket} [{j.repo}]"
        + (f" (depends on {j.depends_on_ticket} {j.depends_on_repo})"
           if j.depends_on_ticket else "")
        for i, j in enumerate(items)
    ]
    await msg.reply_text("\n".join(lines))


async def handle_confirm_frontend(state: "BotState", update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(state, update):
        return
    msg = update.effective_message
    assert msg is not None
    parts = shlex.split(msg.text or "")
    if len(parts) != 2:
        await msg.reply_text("Usage: /confirm-frontend <TICKET-ID>")
        return
    ticket = parts[1].strip().upper()
    items = await state.queue.list()
    target = next((j for j in items if j.ticket == ticket and j.repo == "frontend"), None)
    if target is None:
        await msg.reply_text(f"no pending frontend job for {ticket}")
        return
    # clear dependency
    await state.queue.remove_by_ticket(ticket, "frontend")
    await state.queue.enqueue(Job(
        ticket=ticket, repo="frontend",
        enqueued_by=update.effective_chat.id, enqueued_at=_utcnow_iso(),
    ))
    await msg.reply_text(f"frontend job for {ticket} unblocked")


async def handle_skip(state: "BotState", update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(state, update):
        return
    msg = update.effective_message
    assert msg is not None
    parts = shlex.split(msg.text or "")
    if len(parts) != 2:
        await msg.reply_text("Usage: /skip <TICKET-ID>")
        return
    ticket = parts[1].strip().upper()
    removed = await state.queue.remove_by_ticket(ticket)
    await msg.reply_text(f"removed {removed} job(s) for {ticket}")


async def handle_help(state: "BotState", update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(state, update):
        return
    msg = update.effective_message
    assert msg is not None
    await msg.reply_html(HELP_TEXT)


async def handle_text_reply(state: "BotState", update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Non-command messages routed as ask_user replies if one is pending."""
    if not _owner_only(state, update):
        return
    msg = update.effective_message
    assert msg is not None
    if state.current is None or state.current.pending_ask_id is None:
        await msg.reply_text("no question pending. use /help for commands")
        return
    ask_id = state.current.pending_ask_id
    delivered = await state.socket.deliver_reply(ask_id, msg.text or "")
    if delivered:
        state.current.pending_ask_id = None
        state.current.mark("running", "reply delivered")
        await msg.reply_text("reply delivered")
    else:
        await msg.reply_text("could not deliver reply (no waiter)")
