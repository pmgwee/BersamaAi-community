#!/usr/bin/env bash
# Trusted-VM runner for the 3-hour news digest. The pipeline reads its
# OpenRouter key from the local .env; the secret is never placed in git.
set -uo pipefail

cd "$(dirname "$0")"

exec 9>../news-digest.lock
if ! flock -n 9; then
  echo "news digest already running; skipping overlap"
  exit 0
fi

PYTHON="./.venv/bin/python"
LLM_AUTH_MODE="$("$PYTHON" -c 'from dotenv import dotenv_values; print((dotenv_values().get("LLM_AUTH_MODE") or "api").strip().lower())')"
if [ "$LLM_AUTH_MODE" = "codex" ]; then
  CODEX_BIN="${CODEX_CLI_PATH:-$HOME/.local/bin/codex}"
  if ! "$CODEX_BIN" login status; then
    echo "Codex OAuth login missing; run: codex login --device-auth" >&2
    exit 1
  fi
  export CODEX_CLI_PATH="$CODEX_BIN"
elif [ "$("$PYTHON" -c 'from dotenv import dotenv_values; print("yes" if (dotenv_values().get("LLM_API_KEY") or "").strip() else "no")')" != "yes" ]; then
  echo "LLM_API_KEY is missing from .env; add an OpenRouter API key" >&2
  exit 1
fi

GITHUB_TOKEN="$("$PYTHON" -c 'from dotenv import dotenv_values; print(dotenv_values().get("GITHUB_TOKEN", ""))')"
export GITHUB_TOKEN

git_vm() {
  if [ -n "$GITHUB_TOKEN" ]; then
    GIT_ASKPASS="$PWD/git-askpass-token.sh" GIT_TERMINAL_PROMPT=0 git "$@"
  else
    git "$@"
  fi
}

# These modules do not load .env themselves when invoked directly. Load it in
# the same Python process, without asking a shell to parse secret values.
"$PYTHON" -c 'from dotenv import load_dotenv; load_dotenv(); import runpy; runpy.run_module("pipeline.engagement", run_name="__main__")'
"$PYTHON" -c 'from dotenv import load_dotenv; load_dotenv(); import runpy; runpy.run_module("pipeline.preferences", run_name="__main__")'
"$PYTHON" -m pipeline.main --mode news
run_status=$?

# Keep the VM checkout pullable and preserve news/engagement state in main.
git add state/news_seen.json state/github_stars.json state/posted_log.jsonl \
        state/posted_log_share.jsonl state/engagement.jsonl state/preferences.json \
        state/activity_baseline.json state/processed.json 2>/dev/null || true
if ! git diff --staged --quiet; then
  git config user.email "pipeline-vm@bersama.ai"
  git config user.name "bersama-ai-pipeline"
  git commit -m "chore: pipeline state $(date -u +%FT%TZ)"
  git_vm fetch --depth=50 origin main
  if git_vm pull --rebase origin main; then
    git_vm push origin main || echo "warning: state push failed; VM branch remains ahead" >&2
  else
    echo "warning: state rebase failed; aborting rebase and retaining local commit" >&2
    git rebase --abort || true
  fi
fi

exit "$run_status"
