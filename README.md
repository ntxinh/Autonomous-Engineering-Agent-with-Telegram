# Autonomous Engineering Agent

Telegram-controlled Claude Code that implements Jira tickets in `workspace`.

## Setup

1. `cp .env.example .env` and fill in all secrets (mode 0600).
2. `mise install && mise exec -- uv sync`
3. `./scripts/doctor.sh` — verify all integrations.
4. Install systemd unit:
   ```bash
   mkdir -p ~/.config/systemd/user
   cp systemd/agent-bot.service ~/.config/systemd/user/agent-bot.service
   systemctl --user daemon-reload
   systemctl --user enable --now agent-bot
   ```
5. Tail: `journalctl --user -u agent-bot -f`

## Usage (Telegram)

- `/start ABC-123 backend`
- `/start ABC-123 frontend`
- `/start ABC-123 both`
- `/status`, `/log`, `/cancel`, `/queue`, `/help`
- Reply with a plain message to answer a pending `ask_user` question.

## Files

- `config.toml` — paths, repo map, base branches
- `.env` — secrets (gitignored)
- `sessions/<sid>/` — per-run artifacts (logs, prompt, attachments)
- `queue.json` — persistent FIFO
