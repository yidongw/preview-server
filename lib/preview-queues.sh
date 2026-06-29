#!/usr/bin/env bash
# Queue and slot helpers for manage-preview.sh
#
# Required globals before sourcing:
#   LOGS_PATH, PR_NUMBER, APP_NAME, MAX_BUILDS, MAX_PREVIEWS
#   PREVIEW_REPO, SCRIPT_DIR  (only needed for reap_closed_previews)
#
# Optional test hooks (override before sourcing):
#   _count_live_previews, _preview_has_route

BUILD_QUEUE_DIR="${LOGS_PATH}/queues/build"
DEPLOY_QUEUE_DIR="${LOGS_PATH}/queues/deploy"
BUILD_SLOTS_DIR="${LOGS_PATH}/locks/build-slots"
QUEUE_TICKET=""
OUR_BUILD_SLOT=""

_preview_sleep() {
  if [ "${PREVIEW_TEST_MODE:-}" = "1" ]; then
    sleep 0.01
  else
    sleep "$1"
  fi
}

count_live_previews() {
  if declare -f _count_live_previews >/dev/null 2>&1; then
    _count_live_previews
    return
  fi
  # Fetch routes separately so pipefail doesn't cause || echo 0 to fire when
  # only curl fails (which would produce two zeros: one from node's catch block
  # and one from the fallback, making the result "0\n0" — an invalid integer).
  local routes
  routes=$(curl -sf http://localhost:2019/config/apps/http/servers/preview/routes 2>/dev/null) || true
  echo "$routes" | node -e "
    try { console.log(JSON.parse(require('fs').readFileSync(0,'utf8')).length); }
    catch { console.log(0); }
  " 2>/dev/null || echo 0
}

preview_has_slot() {
  if declare -f _preview_has_route >/dev/null 2>&1; then
    _preview_has_route
    return
  fi
  curl -sf "http://localhost:2019/id/${APP_NAME}" >/dev/null 2>&1
}

enqueue() {
  local queue_dir="$1"
  mkdir -p "$queue_dir"
  QUEUE_TICKET="${queue_dir}/$(date +%s)-$$-pr${PR_NUMBER}"
  echo $$ > "$QUEUE_TICKET"
}

dequeue() {
  [ -n "$QUEUE_TICKET" ] && rm -f "$QUEUE_TICKET"
  QUEUE_TICKET=""
}

cleanup_stale_queue_tickets() {
  local queue_dir="$1"
  local f pid
  for f in "$queue_dir"/*; do
    [ -f "$f" ] || continue
    pid=$(head -1 "$f" 2>/dev/null || true)
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && continue
    echo "[preview] Removing stale queue ticket $(basename "$f") (pid ${pid:-unknown})"
    rm -f "$f"
  done
}

queue_position() {
  local queue_dir="$1"
  local ticket_name
  ticket_name=$(basename "$QUEUE_TICKET")
  local pos=1 f
  for f in $(ls -1 "$queue_dir" 2>/dev/null | sort); do
    [ "$f" = "$ticket_name" ] && { echo "$pos"; return; }
    pos=$((pos + 1))
  done
  echo "?"
}

wait_for_queue_turn() {
  local queue_dir="$1"
  local label="$2"
  local ticket_name waited=0
  ticket_name=$(basename "$QUEUE_TICKET")

  while true; do
    cleanup_stale_queue_tickets "$queue_dir"
    local first
    first=$(ls -1 "$queue_dir" 2>/dev/null | sort | head -1)
    if [ "$first" = "$ticket_name" ]; then
      return 0
    fi
    if [ $((waited % 30)) -eq 0 ]; then
      echo "[preview] ${label} queue position $(queue_position "$queue_dir") for PR #${PR_NUMBER}"
    fi
    _preview_sleep 5
    waited=$((waited + 5))
  done
}

count_active_builds() {
  local count=0
  local slot
  for slot in "$BUILD_SLOTS_DIR"/*; do
    [ -d "$slot" ] || continue
    count=$((count + 1))
  done
  echo "$count"
}

cleanup_stale_build_slots() {
  local slot pid
  for slot in "$BUILD_SLOTS_DIR"/*; do
    [ -d "$slot" ] || continue
    pid=$(cat "${slot}/pid" 2>/dev/null || true)
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && continue
    echo "[preview] Removing stale build slot $(basename "$slot") (pid ${pid:-unknown})"
    rm -rf "$slot"
  done
}

acquire_global_build_lock() {
  mkdir -p "${LOGS_PATH}/locks" "$BUILD_QUEUE_DIR" "$BUILD_SLOTS_DIR"
  QUEUE_TICKET=""
  OUR_BUILD_SLOT=""
  enqueue "$BUILD_QUEUE_DIR"
  echo "[preview] Waiting for build slot (${MAX_BUILDS} max globally, PR #${PR_NUMBER})..."

  while true; do
    wait_for_queue_turn "$BUILD_QUEUE_DIR" "Build"
    cleanup_stale_build_slots

    if [ "$(count_active_builds)" -lt "$MAX_BUILDS" ]; then
      OUR_BUILD_SLOT="${BUILD_SLOTS_DIR}/$$-pr${PR_NUMBER}"
      if mkdir "$OUR_BUILD_SLOT" 2>/dev/null; then
        echo $$ > "${OUR_BUILD_SLOT}/pid"
        echo "$PR_NUMBER" > "${OUR_BUILD_SLOT}/pr"
        dequeue
        echo "[preview] Acquired build slot for PR #${PR_NUMBER} ($(count_active_builds)/${MAX_BUILDS})"
        return 0
      fi
    fi

    _preview_sleep 1
  done
}

release_global_build_lock() {
  if [ -n "${OUR_BUILD_SLOT:-}" ]; then
    rm -rf "$OUR_BUILD_SLOT"
    OUR_BUILD_SLOT=""
  fi
}

# Self-healing slot reclamation.
#
# A preview slot is meant to be freed by the `closed`-triggered GitHub Actions
# teardown, but that is unreliable: if the single self-hosted runner is busy or
# offline when a PR closes, the queued teardown job is dropped and the slot
# leaks permanently — eventually filling every slot and deadlocking all deploys
# (the operation that frees slots is itself stuck behind the full slots).
#
# To converge regardless, tear down any live preview whose PR is closed/merged.
# Only DEFINITIVE closed/merged states trigger teardown; gh/network failures
# leave the preview untouched, so a transient error never kills a live preview.
reap_closed_previews() {
  [ "${PREVIEW_TEST_MODE:-}" = "1" ] && return 0
  command -v gh >/dev/null 2>&1 || return 0

  local routes ids id pr state reaped=0
  routes=$(curl -sf "http://localhost:2019/config/apps/http/servers/preview/routes" 2>/dev/null) || return 0
  ids=$(printf '%s' "$routes" | node -e "
    try { for (const r of JSON.parse(require('fs').readFileSync(0,'utf8'))) if (r['@id']) console.log(r['@id']); }
    catch {}
  " 2>/dev/null) || return 0

  for id in $ids; do
    case "$id" in
      erp-pr-*) pr="${id#erp-pr-}" ;;
      *) continue ;;
    esac
    # Never reap the PR we're currently deploying.
    [ "$pr" = "${PR_NUMBER:-}" ] && continue

    state=$(gh pr view "$pr" --repo "$PREVIEW_REPO" --json state --jq '.state' 2>/dev/null || true)
    case "$state" in
      CLOSED | MERGED)
        echo "[preview] Reaping leaked preview for PR #${pr} (state=${state})"
        "${SCRIPT_DIR}/manage-preview.sh" stop "$pr" >/dev/null 2>&1 || true
        reaped=$((reaped + 1))
        ;;
    esac
  done

  [ "$reaped" -gt 0 ] && echo "[preview] Reaped ${reaped} closed-PR preview(s)"
  return 0
}

acquire_preview_slot() {
  if preview_has_slot; then
    echo "[preview] PR #${PR_NUMBER} already has a preview slot"
    return 0
  fi

  # Reclaim slots leaked by closed/merged PRs before queueing to wait.
  reap_closed_previews

  mkdir -p "$DEPLOY_QUEUE_DIR"
  QUEUE_TICKET=""
  enqueue "$DEPLOY_QUEUE_DIR"
  echo "[preview] Waiting for preview slot (${MAX_PREVIEWS} max, PR #${PR_NUMBER})..."

  local since_reap=0
  while true; do
    wait_for_queue_turn "$DEPLOY_QUEUE_DIR" "Deploy"

    local count
    count=$(count_live_previews)
    if [ "$count" -lt "$MAX_PREVIEWS" ]; then
      dequeue
      echo "[preview] Acquired preview slot for PR #${PR_NUMBER} ($((count + 1))/${MAX_PREVIEWS})"
      return 0
    fi

    echo "[preview] Preview slots full (${count}/${MAX_PREVIEWS}), waiting..."
    _preview_sleep 10
    # Re-check for PRs closed during the wait so a freed slot is reclaimed
    # without manual intervention, roughly once a minute.
    since_reap=$((since_reap + 10))
    if [ "$since_reap" -ge 60 ]; then
      reap_closed_previews
      since_reap=0
    fi
  done
}
