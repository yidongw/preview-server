#!/usr/bin/env bash
set -euo pipefail

# Ensure homebrew binaries and global npm are available regardless of how
# this script is invoked (launchd runner has a minimal PATH)
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:$PATH"
# node_modules/.bin for globally installed packages (pm2)
NPM_GLOBAL=$(npm root -g 2>/dev/null)/..
export PATH="${NPM_GLOBAL}/bin:$PATH"

ACTION="$1"        # start | stop
PR_NUMBER="$2"     # e.g. 42
BRANCH="${3:-}"    # branch name (only needed for start)

REPO_PATH="/Users/xinjuan/git/carbon"
WORKTREE_BASE="/Users/xinjuan/preview/worktrees"
LOGS_PATH="/Users/xinjuan/preview/logs"
PORT=$((4000 + PR_NUMBER))
APP_NAME="erp-pr-${PR_NUMBER}"
WORKTREE="${WORKTREE_BASE}/pr-${PR_NUMBER}"
HOST_HEADER="erp-pr-${PR_NUMBER}.foxhole.bot"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

wait_for_port() {
  echo "[preview] Waiting for port ${PORT}..."
  for i in $(seq 1 45); do
    if nc -z localhost "$PORT" 2>/dev/null; then
      echo "[preview] Port ${PORT} is open"
      return 0
    fi
    sleep 2
  done
  echo "[preview] Warning: port ${PORT} never opened after 90s"
  return 1
}

# Hit the local server so Vite compiles its entry-point bundle before Caddy
# exposes the domain — browser never sees the "compiling…" cold start.
warmup_server() {
  echo "[preview] Warming up Vite (triggering initial compilation)..."
  for i in $(seq 1 15); do
    if curl -sf -o /dev/null --max-time 30 "http://localhost:${PORT}/"; then
      echo "[preview] Server warm"
      return 0
    fi
    sleep 2
  done
  echo "[preview] Warning: warmup timed out — Caddy route added anyway"
}

add_caddy_route() {
  curl -sf -X DELETE "http://localhost:2019/id/${APP_NAME}" 2>/dev/null || true
  curl -sf -X POST "http://localhost:2019/config/apps/http/servers/preview/routes" \
    -H "Content-Type: application/json" \
    -d "{
      \"@id\": \"${APP_NAME}\",
      \"match\": [{\"host\": [\"${HOST_HEADER}\"]}],
      \"handle\": [{\"handler\": \"reverse_proxy\", \"upstreams\": [{\"dial\": \"localhost:${PORT}\"}]}]
    }"
  echo "[preview] Live at https://${HOST_HEADER}"
}

build_ecosystem_json() {
  ENV_JSON=$(node -e "
    const fs = require('fs');
    const lines = fs.readFileSync('/Users/xinjuan/preview/preview.env','utf8').split('\n');
    const env = {};
    for (const line of lines) {
      const m = line.match(/^([A-Z0-9_]+)=(.*)\$/);
      if (m) env[m[1]] = m[2].replace(/^\"|\"$/g,'').replace(/^'|'\$/g,'');
    }
    env.PORT = '${PORT}';
    env.HOST = '0.0.0.0';
    env.NODE_ENV = 'development';
    env.ERP_URL = 'https://erp-pr-${PR_NUMBER}.foxhole.bot';
    console.log(JSON.stringify(env));
  ")

  cat > "${LOGS_PATH}/${APP_NAME}.ecosystem.json" <<ECOSYSTEM
{
  "apps": [{
    "name": "${APP_NAME}",
    "script": "pnpm",
    "args": "run dev:app",
    "cwd": "${WORKTREE}/apps/erp",
    "out_file": "${LOGS_PATH}/${APP_NAME}.log",
    "error_file": "${LOGS_PATH}/${APP_NAME}-err.log",
    "env": ${ENV_JSON}
  }]
}
ECOSYSTEM
}

# ---------------------------------------------------------------------------
# Hot update: worktree already exists (PR synchronize / reopened)
# Only reinstall deps or recompile locales when those actually changed.
# Vite's file watcher sees the git reset and triggers HMR in the browser
# automatically — no server restart needed for source-only changes.
# ---------------------------------------------------------------------------

hot_update() {
  echo "[preview] Hot-updating PR #${PR_NUMBER} (branch: ${BRANCH})"

  git -C "$WORKTREE" fetch origin "${BRANCH}"

  # Capture old state for diffing before we move HEAD
  PREV_HEAD=$(git -C "$WORKTREE" rev-parse HEAD)
  OLD_LOCK=$(git -C "$WORKTREE" rev-parse HEAD:pnpm-lock.yaml 2>/dev/null || echo "")

  git -C "$WORKTREE" reset --hard FETCH_HEAD

  NEW_HEAD=$(git -C "$WORKTREE" rev-parse HEAD)
  NEW_LOCK=$(git -C "$WORKTREE" rev-parse HEAD:pnpm-lock.yaml 2>/dev/null || echo "")

  DEPS_CHANGED=false
  if [ "$OLD_LOCK" != "$NEW_LOCK" ]; then
    echo "[preview] Dependencies changed, running pnpm install"
    pnpm --dir "$WORKTREE" install --prefer-offline
    DEPS_CHANGED=true
  else
    echo "[preview] Dependencies unchanged, skipping pnpm install"
  fi

  if git -C "$WORKTREE" diff "${PREV_HEAD}..${NEW_HEAD}" --name-only 2>/dev/null | grep -q "locales/"; then
    echo "[preview] Locales changed, recompiling"
    pnpm --dir "$WORKTREE" lingui:compile
  else
    echo "[preview] Locales unchanged, skipping lingui:compile"
  fi

  if [ "$DEPS_CHANGED" = true ]; then
    # node_modules changed → Vite's module cache is stale, must restart
    echo "[preview] Restarting dev server (deps changed)"
    build_ecosystem_json
    pm2 restart "$APP_NAME" --update-env
    wait_for_port
    warmup_server
  else
    # Source-only change: Vite's watcher picks up the git reset via HMR.
    # No restart needed — browser gets changes automatically.
    echo "[preview] Source-only change — Vite HMR will propagate to browser"
  fi

  echo "[preview] Hot update complete"
}

# ---------------------------------------------------------------------------
# Cold start: fresh worktree (PR opened / first run)
# ---------------------------------------------------------------------------

cold_start() {
  echo "[preview] Cold start PR #${PR_NUMBER} on port ${PORT} (branch: ${BRANCH})"

  git -C "$REPO_PATH" worktree prune 2>/dev/null || true
  if [ -d "$WORKTREE" ]; then
    git -C "$REPO_PATH" worktree remove "$WORKTREE" --force 2>/dev/null || true
    rm -rf "$WORKTREE"
  fi

  git -C "$REPO_PATH" fetch origin "${BRANCH}"
  git -C "$REPO_PATH" worktree add "$WORKTREE" "FETCH_HEAD"

  pnpm --dir "$WORKTREE" install --prefer-offline

  # Compile locale catalogs (.mjs files are gitignored, absent in fresh worktrees)
  pnpm --dir "$WORKTREE" lingui:compile

  set -a
  # shellcheck source=/dev/null
  source /Users/xinjuan/preview/preview.env
  PORT=$((4000 + PR_NUMBER))
  HOST=0.0.0.0
  set +a

  pm2 stop "$APP_NAME" 2>/dev/null || true
  pm2 delete "$APP_NAME" 2>/dev/null || true

  build_ecosystem_json
  pm2 start "${LOGS_PATH}/${APP_NAME}.ecosystem.json"

  wait_for_port

  # Warm up Vite before exposing via Caddy so the first browser hit is fast
  warmup_server

  add_caddy_route
}

# ---------------------------------------------------------------------------
# Entrypoints
# ---------------------------------------------------------------------------

start_preview() {
  # If the worktree exists and the PM2 process is running, do a fast hot-update
  # instead of a full teardown+rebuild. This is the PR synchronize case.
  if [ -d "$WORKTREE" ] && git -C "$WORKTREE" rev-parse HEAD >/dev/null 2>&1 \
     && pm2 show "$APP_NAME" 2>/dev/null | grep -q "online"; then
    hot_update
  else
    cold_start
  fi
}

stop_preview() {
  echo "[preview] Stopping PR #${PR_NUMBER}"

  curl -sf -X DELETE "http://localhost:2019/id/${APP_NAME}" 2>/dev/null || true

  pm2 stop "$APP_NAME" 2>/dev/null || true
  pm2 delete "$APP_NAME" 2>/dev/null || true

  git -C "$REPO_PATH" worktree prune 2>/dev/null || true
  if [ -d "$WORKTREE" ]; then
    git -C "$REPO_PATH" worktree remove "$WORKTREE" --force 2>/dev/null || true
    rm -rf "$WORKTREE"
  fi

  echo "[preview] PR #${PR_NUMBER} torn down"
}

case "$ACTION" in
  start) start_preview ;;
  stop)  stop_preview ;;
  *) echo "Usage: $0 start|stop <pr-number> [branch]"; exit 1 ;;
esac
