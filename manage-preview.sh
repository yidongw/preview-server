#!/usr/bin/env bash
set -euo pipefail

# Ensure homebrew binaries and global npm are available regardless of how
# this script is invoked (launchd runner has a minimal PATH)
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:$PATH"
# node_modules/.bin for globally installed packages (pm2)
NPM_GLOBAL=$(npm root -g 2>/dev/null)/..
export PATH="${NPM_GLOBAL}/bin:$PATH"

ACTION="$1"        # start | stop | reap
PR_NUMBER="${2:-0}" # e.g. 42 (not required for reap, which sweeps all)
BRANCH="${3:-}"    # branch name (only needed for start)

REPO_PATH="/Users/xinjuan/git/carbon"
WORKTREE_BASE="/Users/xinjuan/preview/worktrees"
LOGS_PATH="/Users/xinjuan/preview/logs"

# ERP — canonical ports 4000+N, blue-green temp 9000+N
PORT=$((4000 + PR_NUMBER))
APP_NAME="erp-pr-${PR_NUMBER}"
WORKTREE="${WORKTREE_BASE}/pr-${PR_NUMBER}"
HOST_HEADER="erp-pr-${PR_NUMBER}.foxhole.bot"
NEXT_PORT=$((PORT + 5000))
NEXT_APP="${APP_NAME}-next"

# MES — canonical ports 5000+N, blue-green temp 8000+N
MES_PORT=$((5000 + PR_NUMBER))
MES_APP_NAME="mes-pr-${PR_NUMBER}"
MES_HOST_HEADER="mes-pr-${PR_NUMBER}.foxhole.bot"
MES_NEXT_PORT=$((MES_PORT + 3000))
MES_NEXT_APP="${MES_APP_NAME}-next"

# Optional per-PR env overrides, layered on top of the global preview.env.
# Lets a single preview differ (e.g. CARBON_EDITION=cloud for one PR) without
# affecting every other preview. Create /Users/xinjuan/preview/preview.env.<PR>.
OVERRIDE_ENV="/Users/xinjuan/preview/preview.env.${PR_NUMBER}"

# Per-PR lock — prevents concurrent start/stop invocations from spawning
# duplicate pnpm builds (each can use up to 12 GB RAM).
LOCK_DIR="${LOGS_PATH}/locks/pr-${PR_NUMBER}"

# Global resource limits (override via preview.env or the environment).
MAX_BUILDS="${PREVIEW_MAX_BUILDS:-1}"
MAX_PREVIEWS="${PREVIEW_MAX_PREVIEWS:-10}"

# Repo previews are built from — used to query PR state when reaping leaked slots.
PREVIEW_REPO="${PREVIEW_REPO:-yidongw/carbon}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/preview-queues.sh
source "${SCRIPT_DIR}/lib/preview-queues.sh"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

release_preview_lock() {
  rm -rf "$LOCK_DIR"
}

# Kill orphaned build processes left behind when a previous deploy crashed
# or was cancelled mid-build.
cleanup_stale_builds() {
  [ -d "$WORKTREE" ] || return 0
  local killed=0 pid

  while IFS= read -r pid; do
    [ -z "$pid" ] && continue
    echo "[preview] Killing stale build pid=${pid}"
    kill "$pid" 2>/dev/null || true
    killed=$((killed + 1))
  done < <(pgrep -f "${WORKTREE}/apps/erp.*react-router/dev/bin.js build" 2>/dev/null || true)

  while IFS= read -r pid; do
    [ -z "$pid" ] && continue
    echo "[preview] Killing stale pnpm build pid=${pid}"
    kill "$pid" 2>/dev/null || true
    killed=$((killed + 1))
  done < <(pgrep -f "pnpm --dir ${WORKTREE}/apps/erp run build" 2>/dev/null || true)

  while IFS= read -r pid; do
    [ -z "$pid" ] && continue
    echo "[preview] Killing stale MES build pid=${pid}"
    kill "$pid" 2>/dev/null || true
    killed=$((killed + 1))
  done < <(pgrep -f "pnpm --dir ${WORKTREE}/apps/mes run build" 2>/dev/null || true)

  if [ "$killed" -gt 0 ]; then
    echo "[preview] Cleaned up ${killed} stale build process(es)"
  fi
}

# Try to acquire the per-PR lock without blocking.
# Returns 0 if acquired, 1 if another live instance holds the lock.
try_preview_lock() {
  mkdir -p "${LOGS_PATH}/locks"

  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo $$ > "${LOCK_DIR}/pid"
    trap 'release_preview_lock' EXIT INT TERM
    cleanup_stale_builds
    return 0
  fi

  local lock_pid=""
  [ -f "${LOCK_DIR}/pid" ] && lock_pid=$(cat "${LOCK_DIR}/pid" 2>/dev/null || true)

  if [ -n "$lock_pid" ] && kill -0 "$lock_pid" 2>/dev/null; then
    echo "[preview] Deploy already in progress for PR #${PR_NUMBER} (pid ${lock_pid}), skipping duplicate invocation"
    return 1
  fi

  echo "[preview] Removing stale lock for PR #${PR_NUMBER} (pid ${lock_pid:-unknown})"
  rm -rf "$LOCK_DIR"
  cleanup_stale_builds

  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo $$ > "${LOCK_DIR}/pid"
    trap 'release_preview_lock' EXIT INT TERM
    return 0
  fi

  echo "[preview] Failed to acquire lock for PR #${PR_NUMBER}"
  return 1
}

# Block until the per-PR lock is available (used by stop).
wait_preview_lock() {
  mkdir -p "${LOGS_PATH}/locks"
  local waited=0
  local timeout=7200

  while true; do
    if mkdir "$LOCK_DIR" 2>/dev/null; then
      echo $$ > "${LOCK_DIR}/pid"
      trap 'release_preview_lock' EXIT INT TERM
      cleanup_stale_builds
      return 0
    fi

    local lock_pid=""
    [ -f "${LOCK_DIR}/pid" ] && lock_pid=$(cat "${LOCK_DIR}/pid" 2>/dev/null || true)

    if [ -n "$lock_pid" ] && ! kill -0 "$lock_pid" 2>/dev/null; then
      echo "[preview] Removing stale lock for PR #${PR_NUMBER} (pid ${lock_pid})"
      rm -rf "$LOCK_DIR"
      cleanup_stale_builds
      continue
    fi

    if [ "$waited" -eq 0 ]; then
      echo "[preview] Waiting for in-progress deploy on PR #${PR_NUMBER} (pid ${lock_pid:-unknown})..."
    fi

    if [ "$waited" -ge "$timeout" ]; then
      echo "[preview] Timed out waiting for lock on PR #${PR_NUMBER} after ${timeout}s"
      exit 1
    fi

    sleep 5
    waited=$((waited + 5))
  done
}

# Scan upward from candidate until a port with no listener is found.
find_free_port() {
  local candidate="$1"
  while nc -z localhost "$candidate" 2>/dev/null; do
    candidate=$((candidate + 1))
  done
  echo "$candidate"
}

wait_for_port() {
  local target="${1:-$PORT}"
  echo "[preview] Waiting for port ${target}..."
  for i in $(seq 1 45); do
    if nc -z localhost "$target" 2>/dev/null; then
      echo "[preview] Port ${target} is open"
      return 0
    fi
    sleep 2
  done
  echo "[preview] Warning: port ${target} never opened after 90s"
  return 1
}

# Send HTTP requests to the app so V8 JIT compiles hot paths and in-process
# caches initialize before real user traffic arrives.
warm_up() {
  local target="${1:-$PORT}"
  echo "[preview] Warming up on port ${target}..."
  local base_url="http://localhost:${target}"

  # Wait for the HTTP server to be accepting requests (TCP open doesn't guarantee this)
  for i in $(seq 1 30); do
    if curl -sf -o /dev/null "${base_url}/health"; then
      echo "[preview] Health check passed"
      break
    fi
    [ "$i" -eq 30 ] && echo "[preview] Warning: health check failed after 60s"
    sleep 2
  done

  # Hit the login page (first page users land on) multiple times for V8 JIT warmup.
  # Root (/) immediately redirects there, so warming it directly is what matters.
  for i in $(seq 1 5); do
    curl -sf -o /dev/null "${base_url}/login" || true
  done

  echo "[preview] Warm-up complete"
}

# ---------------------------------------------------------------------------
# ERP helpers
# ---------------------------------------------------------------------------

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

# Caddy's admin API does not support PUT on named-ID sub-paths when the key
# already exists (returns 409). DELETE + POST is the only reliable way to
# swap the upstream. The gap between the two calls is <20ms — acceptable for previews.
update_caddy_upstream() {
  local target_port="$1"
  curl -sf -X DELETE "http://localhost:2019/id/${APP_NAME}" 2>/dev/null || true
  curl -sf -X POST "http://localhost:2019/config/apps/http/servers/preview/routes" \
    -H "Content-Type: application/json" \
    -d "{\"@id\": \"${APP_NAME}\", \"match\": [{\"host\": [\"${HOST_HEADER}\"]}], \"handle\": [{\"handler\": \"reverse_proxy\", \"upstreams\": [{\"dial\": \"localhost:${target_port}\"}]}]}"
  echo "[preview] ${HOST_HEADER} → localhost:${target_port}"
}

# Build the ERP app for production with sourcemaps so stack traces point to real
# source lines. PREVIEW_BUILD=1 is read by vite.config.ts to enable sourcemaps
# without affecting Vercel production deployments.
build_app() {
  acquire_global_build_lock
  local build_failed=0
  echo "[preview] Building ERP (production)..."
  PREVIEW_BUILD=1 NODE_OPTIONS="--max-old-space-size=12288" pnpm --dir "$WORKTREE/apps/erp" run build || build_failed=1
  release_global_build_lock
  if [ "$build_failed" -ne 0 ]; then
    echo "[preview] ERP build failed"
    exit 1
  fi
  echo "[preview] ERP build complete"
}

build_ecosystem_json_for() {
  local target_port="$1"
  local app_name="$2"
  local env_json
  env_json=$(node -e "
    const fs = require('fs');
    // Global preview.env first, then optional per-PR override (override wins).
    const files = ['/Users/xinjuan/preview/preview.env', '${OVERRIDE_ENV}'];
    const env = {};
    for (const file of files) {
      let content;
      try { content = fs.readFileSync(file, 'utf8'); } catch (e) { continue; }
      for (const line of content.split('\n')) {
        const m = line.match(/^([A-Z0-9_]+)=(.*)\$/);
        if (m) env[m[1]] = m[2].replace(/^\"|\"$/g,'').replace(/^'|'\$/g,'');
      }
    }
    env.PORT = '${target_port}';
    env.HOST = '0.0.0.0';
    env.NODE_ENV = 'production';
    env.ERP_URL = 'https://erp-pr-${PR_NUMBER}.foxhole.bot';
    env.MES_URL = 'https://mes-pr-${PR_NUMBER}.foxhole.bot';
    const bypassEmail = 'bypass@mail.com';
    const existing = env.DEV_BYPASS_EMAIL || '';
    const emails = existing.split(',').map((e) => e.trim()).filter(Boolean);
    if (!emails.some((e) => e.toLowerCase() === bypassEmail)) {
      emails.push(bypassEmail);
    }
    env.DEV_BYPASS_EMAIL = emails.join(',');
    console.log(JSON.stringify(env));
  ")

  cat > "${LOGS_PATH}/${app_name}.ecosystem.json" <<ECOSYSTEM
{
  "apps": [{
    "name": "${app_name}",
    "script": "pnpm",
    "args": "run start",
    "cwd": "${WORKTREE}/apps/erp",
    "out_file": "${LOGS_PATH}/${app_name}.log",
    "error_file": "${LOGS_PATH}/${app_name}-err.log",
    "env": ${env_json}
  }]
}
ECOSYSTEM
}

build_ecosystem_json() {
  build_ecosystem_json_for "$PORT" "$APP_NAME"
}

# ---------------------------------------------------------------------------
# MES helpers
# ---------------------------------------------------------------------------

add_mes_caddy_route() {
  curl -sf -X DELETE "http://localhost:2019/id/${MES_APP_NAME}" 2>/dev/null || true
  curl -sf -X POST "http://localhost:2019/config/apps/http/servers/preview/routes" \
    -H "Content-Type: application/json" \
    -d "{
      \"@id\": \"${MES_APP_NAME}\",
      \"match\": [{\"host\": [\"${MES_HOST_HEADER}\"]}],
      \"handle\": [{\"handler\": \"reverse_proxy\", \"upstreams\": [{\"dial\": \"localhost:${MES_PORT}\"}]}]
    }"
  echo "[preview] MES live at https://${MES_HOST_HEADER}"
}

update_mes_caddy_upstream() {
  local target_port="$1"
  curl -sf -X DELETE "http://localhost:2019/id/${MES_APP_NAME}" 2>/dev/null || true
  curl -sf -X POST "http://localhost:2019/config/apps/http/servers/preview/routes" \
    -H "Content-Type: application/json" \
    -d "{\"@id\": \"${MES_APP_NAME}\", \"match\": [{\"host\": [\"${MES_HOST_HEADER}\"]}], \"handle\": [{\"handler\": \"reverse_proxy\", \"upstreams\": [{\"dial\": \"localhost:${target_port}\"}]}]}"
  echo "[preview] ${MES_HOST_HEADER} → localhost:${target_port}"
}

build_mes_app() {
  acquire_global_build_lock
  local build_failed=0
  echo "[preview] Building MES (production)..."
  PREVIEW_BUILD=1 NODE_OPTIONS="--max-old-space-size=12288" pnpm --dir "$WORKTREE/apps/mes" run build || build_failed=1
  release_global_build_lock
  if [ "$build_failed" -ne 0 ]; then
    echo "[preview] MES build failed"
    exit 1
  fi
  echo "[preview] MES build complete"
}

build_mes_ecosystem_json_for() {
  local target_port="$1"
  local app_name="$2"
  local env_json
  env_json=$(node -e "
    const fs = require('fs');
    // Global preview.env first, then optional per-PR override (override wins).
    const files = ['/Users/xinjuan/preview/preview.env', '${OVERRIDE_ENV}'];
    const env = {};
    for (const file of files) {
      let content;
      try { content = fs.readFileSync(file, 'utf8'); } catch (e) { continue; }
      for (const line of content.split('\n')) {
        const m = line.match(/^([A-Z0-9_]+)=(.*)\$/);
        if (m) env[m[1]] = m[2].replace(/^\"|\"$/g,'').replace(/^'|'\$/g,'');
      }
    }
    env.PORT = '${target_port}';
    env.HOST = '0.0.0.0';
    env.NODE_ENV = 'production';
    env.ERP_URL = 'https://erp-pr-${PR_NUMBER}.foxhole.bot';
    env.MES_URL = 'https://mes-pr-${PR_NUMBER}.foxhole.bot';
    const bypassEmail = 'bypass@mail.com';
    const existing = env.DEV_BYPASS_EMAIL || '';
    const emails = existing.split(',').map((e) => e.trim()).filter(Boolean);
    if (!emails.some((e) => e.toLowerCase() === bypassEmail)) {
      emails.push(bypassEmail);
    }
    env.DEV_BYPASS_EMAIL = emails.join(',');
    console.log(JSON.stringify(env));
  ")

  cat > "${LOGS_PATH}/${app_name}.ecosystem.json" <<ECOSYSTEM
{
  "apps": [{
    "name": "${app_name}",
    "script": "pnpm",
    "args": "run start",
    "cwd": "${WORKTREE}/apps/mes",
    "out_file": "${LOGS_PATH}/${app_name}.log",
    "error_file": "${LOGS_PATH}/${app_name}-err.log",
    "env": ${env_json}
  }]
}
ECOSYSTEM
}

build_mes_ecosystem_json() {
  build_mes_ecosystem_json_for "$MES_PORT" "$MES_APP_NAME"
}

# ---------------------------------------------------------------------------
# Hot update: zero-downtime blue-green swap for ERP + MES
#
# 1. Fetch + reset worktree
# 2. Reinstall deps if lockfile changed
# 3. Recompile locales if changed
# 4. Build ERP, then MES (sequential: global build lock limits concurrency)
# 5. Blue-green swap ERP
# 6. Blue-green swap MES
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
  build_mes_app

  # --- Blue-green swap ERP ---
  pm2 delete "$NEXT_APP" 2>/dev/null || true
  NEXT_PORT=$(find_free_port "$NEXT_PORT")
  build_ecosystem_json_for "$NEXT_PORT" "$NEXT_APP"
  pm2 start "${LOGS_PATH}/${NEXT_APP}.ecosystem.json"
  wait_for_port "$NEXT_PORT"
  warm_up "$NEXT_PORT"

  update_caddy_upstream "$NEXT_PORT"

  pm2 stop "$APP_NAME" 2>/dev/null || true
  pm2 delete "$APP_NAME" 2>/dev/null || true

  build_ecosystem_json_for "$PORT" "$APP_NAME"
  pm2 start "${LOGS_PATH}/${APP_NAME}.ecosystem.json"
  wait_for_port "$PORT"
  warm_up "$PORT"
  update_caddy_upstream "$PORT"

  pm2 stop "$NEXT_APP" 2>/dev/null || true
  pm2 delete "$NEXT_APP" 2>/dev/null || true

  # --- Blue-green swap MES ---
  pm2 delete "$MES_NEXT_APP" 2>/dev/null || true
  MES_NEXT_PORT=$(find_free_port "$MES_NEXT_PORT")
  build_mes_ecosystem_json_for "$MES_NEXT_PORT" "$MES_NEXT_APP"
  pm2 start "${LOGS_PATH}/${MES_NEXT_APP}.ecosystem.json"
  wait_for_port "$MES_NEXT_PORT"
  warm_up "$MES_NEXT_PORT"

  update_mes_caddy_upstream "$MES_NEXT_PORT"

  pm2 stop "$MES_APP_NAME" 2>/dev/null || true
  pm2 delete "$MES_APP_NAME" 2>/dev/null || true

  build_mes_ecosystem_json_for "$MES_PORT" "$MES_APP_NAME"
  pm2 start "${LOGS_PATH}/${MES_APP_NAME}.ecosystem.json"
  wait_for_port "$MES_PORT"
  warm_up "$MES_PORT"
  update_mes_caddy_upstream "$MES_PORT"

  pm2 stop "$MES_NEXT_APP" 2>/dev/null || true
  pm2 delete "$MES_NEXT_APP" 2>/dev/null || true

  echo "[preview] Hot update complete"
}

# ---------------------------------------------------------------------------
# Cold start: fresh worktree (PR opened / first run)
# ---------------------------------------------------------------------------

cold_start() {
  echo "[preview] Cold start PR #${PR_NUMBER} on ports ERP:${PORT} MES:${MES_PORT} (branch: ${BRANCH})"

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
  build_mes_app

  acquire_preview_slot

  set -a
  # shellcheck source=/dev/null
  source /Users/xinjuan/preview/preview.env
  # shellcheck source=/dev/null
  [ -f "$OVERRIDE_ENV" ] && source "$OVERRIDE_ENV"
  PORT=$((4000 + PR_NUMBER))
  MES_PORT=$((5000 + PR_NUMBER))
  HOST=0.0.0.0
  set +a

  # Start ERP
  pm2 stop "$APP_NAME" 2>/dev/null || true
  pm2 delete "$APP_NAME" 2>/dev/null || true
  build_ecosystem_json
  pm2 start "${LOGS_PATH}/${APP_NAME}.ecosystem.json"

  # Start MES
  pm2 stop "$MES_APP_NAME" 2>/dev/null || true
  pm2 delete "$MES_APP_NAME" 2>/dev/null || true
  build_mes_ecosystem_json
  pm2 start "${LOGS_PATH}/${MES_APP_NAME}.ecosystem.json"

  wait_for_port "$PORT"
  warm_up "$PORT"
  add_caddy_route

  wait_for_port "$MES_PORT"
  warm_up "$MES_PORT"
  add_mes_caddy_route
}

# ---------------------------------------------------------------------------
# Entrypoints
# ---------------------------------------------------------------------------

start_preview() {
  if ! try_preview_lock; then
    exit 0
  fi

  if [ -d "$WORKTREE" ] && git -C "$WORKTREE" rev-parse HEAD >/dev/null 2>&1 \
     && pm2 show "$APP_NAME" 2>/dev/null | grep -q "online"; then
    hot_update
  else
    cold_start
  fi
}

stop_preview() {
  wait_preview_lock

  echo "[preview] Stopping PR #${PR_NUMBER}"

  # Remove Caddy routes
  curl -sf -X DELETE "http://localhost:2019/id/${APP_NAME}" 2>/dev/null || true
  curl -sf -X DELETE "http://localhost:2019/id/${MES_APP_NAME}" 2>/dev/null || true

  # Stop ERP processes
  pm2 stop "$APP_NAME" 2>/dev/null || true
  pm2 delete "$APP_NAME" 2>/dev/null || true
  pm2 stop "$NEXT_APP" 2>/dev/null || true
  pm2 delete "$NEXT_APP" 2>/dev/null || true

  # Stop MES processes
  pm2 stop "$MES_APP_NAME" 2>/dev/null || true
  pm2 delete "$MES_APP_NAME" 2>/dev/null || true
  pm2 stop "$MES_NEXT_APP" 2>/dev/null || true
  pm2 delete "$MES_NEXT_APP" 2>/dev/null || true

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
  reap)  reap_closed_previews ;;
  *) echo "Usage: $0 start|stop <pr-number> [branch] | reap"; exit 1 ;;
esac
