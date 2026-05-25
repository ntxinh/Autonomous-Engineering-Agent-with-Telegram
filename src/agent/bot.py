"""Telegram bot orchestrator. Runs queue loop, spawns Claude per session,
brokers ask_user via socket server, creates PRs."""
from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from telegram import Bot
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

from agent import claude_runner, commands, git_ops
from agent.config import Config, load
from agent.jira import JiraClient
from agent.prompt import build_ctx, render
from agent.queue import Job, PersistentQueue
from agent.session import Session, make_sid
from agent.socket_server import SocketServer


@dataclass
class BotState:
    cfg: Config
    queue: PersistentQueue
    socket: SocketServer
    bot: Bot
    current: Optional[Session] = None
    current_proc_pid: Optional[int] = None

    async def cancel_current(self) -> None:
        if self.current is None:
            return
        if self.current.pending_ask_id:
            await self.socket.cancel_ask(self.current.pending_ask_id)
        if self.current_proc_pid:
            try:
                os.kill(self.current_proc_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        self.current.mark("cancelled", "operator cancel")


async def _tg_send(state: BotState, text: str, html: bool = False) -> None:
    try:
        await state.bot.send_message(
            chat_id=state.cfg.telegram_owner_chat_id,
            text=text,
            parse_mode="HTML" if html else None,
        )
    except Exception as e:
        # never crash the loop on Telegram errors
        print(f"[bot] telegram send failed: {e}")


async def _on_ask(state: BotState, sid: str, ask_id: str, question: str, urgency: str) -> None:
    if state.current is None or state.current.id != sid:
        return
    state.current.pending_ask_id = ask_id
    state.current.mark("awaiting_reply", "ask_user")
    badge = "❓" if urgency == "normal" else "❗"
    await _tg_send(
        state,
        f"<b>{badge} {state.current.ticket} asks:</b>\n{_escape(question)}\n\n"
        f"<i>Reply with a plain message to answer.</i>",
        html=True,
    )


async def _on_notify(state: BotState, sid: str, message: str, kind: str) -> None:
    if state.current is None or state.current.id != sid:
        return
    icon = {"progress": "•", "milestone": "🔵", "warning": "⚠️"}.get(kind, "•")
    await _tg_send(state, f"{icon} {message}")


async def _on_blocker(state: BotState, sid: str, reason: str) -> None:
    if state.current is None or state.current.id != sid:
        return
    state.current.mark("failed", "blocker")
    state.current.error = reason
    state.current.save()
    await _tg_send(state, f"🛑 <b>{sid}</b> blocked: {_escape(reason)}", html=True)


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _gh_env(cfg: Config) -> dict:
    env = dict(os.environ)
    env["GH_TOKEN"] = cfg.gh_token
    return env


async def _run_session(state: BotState, job: Job) -> None:
    cfg = state.cfg
    repo_cfg = cfg.repos.get(job.repo)
    if repo_cfg is None:
        await _tg_send(state, f"❌ unknown repo: {job.repo}")
        await state.queue.notify()
        return

    sid = make_sid(job.ticket, job.repo)
    session = Session(
        id=sid, ticket=job.ticket, repo=job.repo,
        repo_path=str(repo_cfg.path), branch="",
        base=repo_cfg.base_branch,
    )
    state.current = session
    session.mark("fetching", "jira")
    await _tg_send(state, f"▶️ started {session.id}")

    try:
        # Jira fetch
        jc = JiraClient(cfg.jira_base_url, cfg.jira_email, cfg.jira_api_token)
        issue = await jc.fetch_issue(job.ticket)
        att_dir = cfg.sessions_dir / sid / "attachments"
        await jc.download_attachments(issue, att_dir)

        # branch
        branch = git_ops.branch_name(job.ticket, issue.title)
        session.branch = branch
        session.mark("branching", branch)
        gh_env = await _gh_env(cfg)
        git_ops.preflight(repo_cfg.path, repo_cfg.base_branch, branch, gh_env)
        git_ops.prepare_branch(repo_cfg.path, repo_cfg.base_branch, branch)

        # symlink attachments into repo
        att_link = repo_cfg.path / ".agent-attachments"
        if att_link.exists() or att_link.is_symlink():
            att_link.unlink()
        att_link.symlink_to(att_dir)

        # write prompt
        ctx = build_ctx(
            issue=issue,
            repo_name=job.repo,
            repo_path=repo_cfg.path,
            stack=repo_cfg.stack,
            base=repo_cfg.base_branch,
            branch=branch,
            session_dir=cfg.sessions_dir / sid,
            attachments_in_repo=Path(".agent-attachments"),
        )
        prompt_text = render(ctx, cfg.agent_home / "templates")
        prompt_path = cfg.sessions_dir / sid / "prompt.md"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt_text)

        # MCP config
        mcp_cfg = claude_runner.write_mcp_config(
            session_dir=cfg.sessions_dir / sid,
            agent_home=cfg.agent_home,
            socket_path=cfg.socket_path,
            session_id=sid,
        )

        # spawn Claude
        env = claude_runner.build_env(
            claude_token=cfg.claude_oauth_token,
            session_id=sid,
            socket_path=cfg.socket_path,
        )
        # propagate PATH so 'uv' resolves inside Claude when launching MCP
        if "PATH" not in env:
            env["PATH"] = os.environ.get("PATH", "")

        async def pid_cb(pid: int) -> None:
            session.pid = pid
            state.current_proc_pid = pid
            session.save()

        session.mark("running", "claude")
        result = await claude_runner.run(
            prompt_path=prompt_path,
            mcp_config=mcp_cfg,
            repo_path=repo_cfg.path,
            session_dir=cfg.sessions_dir / sid,
            env=env,
            pid_callback=pid_cb,
        )

        if session.state == "cancelled":
            return
        if result.exit_code != 0:
            session.error = f"claude exited {result.exit_code}"
            session.mark("failed", "claude exit nonzero")
            await _tg_send(state, f"❌ {session.id} failed: claude exit {result.exit_code}")
            return

        # push + PR
        session.mark("pushing", "git push + gh pr")
        body = git_ops.render_pr_body(
            ticket=job.ticket,
            title=issue.title,
            summary=result.final_message,
            sid=sid,
            branch=branch,
            base=repo_cfg.base_branch,
            jira_base=cfg.jira_base_url,
        )
        url = git_ops.push_and_pr(
            repo=repo_cfg.path,
            branch=branch,
            base=repo_cfg.base_branch,
            title=f"{job.ticket}: {issue.title}",
            body=body,
            gh_env=gh_env,
        )
        session.pr_url = url
        (cfg.sessions_dir / sid / "pr.json").write_text(
            f'{{"url":"{url}","branch":"{branch}"}}'
        )
        session.mark("done", "pr created")
        await _tg_send(state, f"✅ {session.id} → {url}")

    except Exception as e:
        session.error = str(e)
        session.mark("failed", "exception")
        await _tg_send(state, f"❌ {session.id} failed: {_escape(str(e))}", html=True)
        # both-mode: if backend failed, prompt operator about frontend
        items = await state.queue.list()
        sib = next(
            (j for j in items
             if j.ticket == job.ticket and j.repo == "frontend"
             and j.depends_on_ticket == job.ticket),
            None,
        )
        if job.repo == "backend" and sib is not None:
            await _tg_send(
                state,
                f"Backend failed. Run frontend anyway? "
                f"<code>/confirm_frontend {job.ticket}</code> or "
                f"<code>/skip {job.ticket}</code>",
                html=True,
            )
    finally:
        state.current = None
        state.current_proc_pid = None
        # cleanup symlink
        try:
            link = (cfg.repos[job.repo].path / ".agent-attachments")
            if link.is_symlink():
                link.unlink()
        except Exception:
            pass
        await state.queue.notify()  # wake waiters (frontend dep may now be free)


async def _queue_loop(state: BotState) -> None:
    while True:
        job = await state.queue.dequeue()
        try:
            await _run_session(state, job)
        except Exception as e:
            await _tg_send(state, f"❌ orchestrator error: {_escape(str(e))}", html=True)


def _build_app(state: BotState) -> Application:
    app = (
        Application.builder()
        .token(state.cfg.telegram_bot_token)
        .build()
    )

    def bind(fn):
        async def wrapped(update, context):
            await fn(state, update, context)
        return wrapped

    app.add_handler(CommandHandler("start", bind(commands.handle_start)))
    app.add_handler(CommandHandler("status", bind(commands.handle_status)))
    app.add_handler(CommandHandler("log", bind(commands.handle_log)))
    app.add_handler(CommandHandler("cancel", bind(commands.handle_cancel)))
    app.add_handler(CommandHandler("queue", bind(commands.handle_queue)))
    app.add_handler(CommandHandler("confirm_frontend", bind(commands.handle_confirm_frontend)))
    app.add_handler(CommandHandler("skip", bind(commands.handle_skip)))
    app.add_handler(CommandHandler("help", bind(commands.handle_help)))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bind(commands.handle_text_reply)))
    return app


async def run_async() -> None:
    cfg = load()
    cfg.sessions_dir.mkdir(parents=True, exist_ok=True)
    cfg.socket_path.parent.mkdir(parents=True, exist_ok=True)

    queue = PersistentQueue(cfg.queue_file)
    bot = Bot(token=cfg.telegram_bot_token)
    state = BotState(cfg=cfg, queue=queue, socket=None, bot=bot)  # type: ignore

    socket = SocketServer(
        path=cfg.socket_path,
        on_ask=lambda sid, aid, q, u: _on_ask(state, sid, aid, q, u),
        on_notify=lambda sid, m, k: _on_notify(state, sid, m, k),
        on_blocker=lambda sid, r: _on_blocker(state, sid, r),
    )
    state.socket = socket
    await socket.start()

    # crash recovery
    for s in Session.find_active(cfg.sessions_dir):
        s.error = "orchestrator restart, pid lost"
        s.mark("failed", "restart")
        await _tg_send(state, f"♻️ {s.id} marked failed after restart")

    app = _build_app(state)
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    queue_task = asyncio.create_task(_queue_loop(state))

    stop = asyncio.Event()

    def _signal_handler() -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    try:
        await stop.wait()
    finally:
        queue_task.cancel()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await socket.stop()


def run() -> None:
    asyncio.run(run_async())
