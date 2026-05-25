#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== mise =="
mise current || true

echo "== deps =="
mise exec -- uv run python -c "import telegram, httpx, fastmcp, jinja2; print('deps ok')"

echo "== gh =="
gh auth status

echo "== jira =="
: "${JIRA_BASE_URL:?env not loaded}"
: "${JIRA_EMAIL:?env not loaded}"
: "${JIRA_API_TOKEN:?env not loaded}"
curl -fsS -u "$JIRA_EMAIL:$JIRA_API_TOKEN" \
  "$JIRA_BASE_URL/rest/api/3/myself" >/dev/null
echo "jira ok"

echo "== telegram =="
: "${TELEGRAM_BOT_TOKEN:?env not loaded}"
curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" >/dev/null
echo "telegram ok"

echo "== claude =="
command -v claude >/dev/null && echo "claude on PATH" || echo "claude NOT on PATH"

echo "doctor: ok"
