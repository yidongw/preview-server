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

# Send HTTP requests to the app so V8 JIT compiles hot paths and in-process
# caches initialize before real user traffic arrives.
warm_up() {
  echo "[preview] Warming up ${APP_NAME}..."
  for i in $(seq 1 10); do
    if curl -sf -o /dev/null "http://localhost:${PORT}/"; then
      echo "[preview] Warm-up complete"
      return 0
    fi
    sleep 2
  done
  echo "[preview] Warning: warm-up request failed after 20s"
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

# Build the app for production with sourcemaps so stack traces point to real
# source lines. PREVIEW_BUILD=1 is read by vite.config.ts to enable sourcemaps
# without affecting Vercel production deployments.
build_app() {
  echo "[preview] Building (production)..."
  PREVIEW_BUILD=1 NODE_OPTIONS="--max-old-space-size=12288" pnpm --dir "$WORKTREE/apps/erp" run build
  echo "[preview] Build complete"
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
    env.NODE_ENV = 'production';
    env.ERP_URL = 'https://erp-pr-${PR_NUMBER}.foxhole.bot';
    console.log(JSON.stringify(env));
  ")

  cat > "${LOGS_PATH}/${APP_NAME}.ecosystem.json" <<ECOSYSTEM
{
  "apps": [{
    "name": "${APP_NAME}",
    "script": "pnpm",
    "args": "run start",
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
# Always rebuilds so the bundle reflects the latest source.
# Only reinstalls deps or recompiles locales when those actually changed.
# ---------------------------------------------------------------------------

hot_update() {
  echo "[preview] Hot-updating PR #${PR_NUMBER} (branch: ${BRANCH})"

  git -C "$WORKTREE" fetch origin "${BRANCH}"

  PREV_HEAD=$(git -C "$WORKTREE" rev-parse HEAD)
  OLD_LOCK=$(git -C "$WORKTREE" rev-parse HEAD:pnpm-lock.yaml 2>/dev/null || echo "")

  git -C "$WORKTREE" reset --hard FETCH_HEAD

  NEW_HEAD=$(git -C "$WORKTREE" rev-parse HEAD)
  NEW_LOCK=$(git -C "$WORKTREE" rev-parse HEAD:pnpm-lock.yaml 2>/dev/null || echo "")

  if [ "$OLD_LOCK" != "$NEW_LOCK" ]; then
    echo "[preview] Dependencies changed, running pnpm install"
    pnpm --dir "$WORKTREE" install --prefer-offline
  else
    echo "[preview] Dependencies unchanged, skipping pnpm install"
  fi

  if git -C "$WORKTREE" diff "${PREV_HEAD}..${NEW_HEAD}" --name-only 2>/dev/null | grep -q "locales/"; then
    echo "[preview] Locales changed, recompiling"
    pnpm --dir "$WORKTREE" lingui:compile
  else
    echo "[preview] Locales unchanged, skipping lingui:compile"
  fi

  build_app

  build_ecosystem_json
  pm2 restart "$APP_NAME" --update-env
  wait_for_port
  warm_up

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

  build_app

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
  warm_up
  add_caddy_route
}

# ---------------------------------------------------------------------------
# Entrypoints
# ---------------------------------------------------------------------------

start_preview() {
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
