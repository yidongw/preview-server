#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/preview-queues-test.XXXXXX")"
export PREVIEW_TEST_MODE=1

TESTS=0
FAILURES=0

cleanup() {
  jobs -p 2>/dev/null | xargs kill 2>/dev/null || true
  wait 2>/dev/null || true
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

assert_eq() {
  local expected="$1"
  local actual="$2"
  local message="${3:-}"
  TESTS=$((TESTS + 1))
  if [ "$expected" = "$actual" ]; then
    return 0
  fi
  echo "FAIL: ${message}"
  echo "  expected: ${expected}"
  echo "  actual:   ${actual}"
  FAILURES=$((FAILURES + 1))
  return 0
}

source_queues() {
  # shellcheck source=../lib/preview-queues.sh
  source "${ROOT}/lib/preview-queues.sh"
}

test_fifo_queue_order() {
  export LOGS_PATH="${TMP_ROOT}/fifo"
  export MAX_BUILDS=1
  export MAX_PREVIEWS=10
  mkdir -p "${LOGS_PATH}/queues/build"
  source_queues

  echo 1 > "${BUILD_QUEUE_DIR}/1000-1-pr42"
  export PR_NUMBER=43
  export APP_NAME="erp-pr-43"
  QUEUE_TICKET="${BUILD_QUEUE_DIR}/1001-2-pr43"
  echo 2 > "$QUEUE_TICKET"
  assert_eq "2" "$(queue_position "$BUILD_QUEUE_DIR")" "second ticket should be position 2"
}

test_single_build_slot_blocks_second() {
  export LOGS_PATH="${TMP_ROOT}/single-build"
  export MAX_BUILDS=1
  export MAX_PREVIEWS=10
  mkdir -p "$LOGS_PATH"

  (
    export PR_NUMBER=10
    export APP_NAME="erp-pr-10"
    source_queues
    acquire_global_build_lock
    sleep 2
    release_global_build_lock
  ) &
  local worker=$!
  sleep 0.1

  export PR_NUMBER=11
  export APP_NAME="erp-pr-11"
  source_queues
  enqueue "$BUILD_QUEUE_DIR"
  local waiting
  waiting=$(ls -1 "$BUILD_QUEUE_DIR" 2>/dev/null | wc -l | tr -d ' ')
  assert_eq "1" "$waiting" "second PR should remain queued while build slot is taken"

  kill "$worker" 2>/dev/null || true
  wait "$worker" 2>/dev/null || true
}

test_max_builds_allows_two_concurrent() {
  export LOGS_PATH="${TMP_ROOT}/max-builds"
  export MAX_BUILDS=2
  export MAX_PREVIEWS=10
  mkdir -p "$LOGS_PATH"

  export PR_NUMBER=20
  export APP_NAME="erp-pr-20"
  source_queues
  acquire_global_build_lock

  (
    export PR_NUMBER=21
    export APP_NAME="erp-pr-21"
    source_queues
    acquire_global_build_lock
    sleep 1
    release_global_build_lock
  ) &
  local worker=$!
  sleep 0.2

  assert_eq "2" "$(count_active_builds)" "two build slots should be active"

  release_global_build_lock
  wait "$worker" 2>/dev/null || true
}

test_max_builds_blocks_third() {
  export LOGS_PATH="${TMP_ROOT}/max-builds-block"
  export MAX_BUILDS=2
  export MAX_PREVIEWS=10
  mkdir -p "$LOGS_PATH"

  export PR_NUMBER=30
  export APP_NAME="erp-pr-30"
  source_queues
  acquire_global_build_lock

  (
    export PR_NUMBER=31
    export APP_NAME="erp-pr-31"
    source_queues
    acquire_global_build_lock
    sleep 2
    release_global_build_lock
  ) &
  local worker=$!
  sleep 0.1

  export PR_NUMBER=32
  export APP_NAME="erp-pr-32"
  enqueue "$BUILD_QUEUE_DIR"
  assert_eq "2" "$(count_active_builds)" "two slots should be full"
  assert_eq "1" "$(ls -1 "$BUILD_QUEUE_DIR" | wc -l | tr -d ' ')" "third PR should wait in queue"

  release_global_build_lock
  kill "$worker" 2>/dev/null || true
  wait "$worker" 2>/dev/null || true
}

test_deploy_queue_waits_at_capacity() {
  export LOGS_PATH="${TMP_ROOT}/deploy-capacity"
  export MAX_BUILDS=1
  export MAX_PREVIEWS=2
  LIVE_PREVIEW_COUNT=2
  _count_live_previews() { echo "$LIVE_PREVIEW_COUNT"; }
  _preview_has_route() { return 1; }
  mkdir -p "$LOGS_PATH"

  export PR_NUMBER=40
  export APP_NAME="erp-pr-40"
  source_queues
  enqueue "$DEPLOY_QUEUE_DIR"
  assert_eq "1" "$(ls -1 "$DEPLOY_QUEUE_DIR" | wc -l | tr -d ' ')" "deploy waiter should stay queued when full"

  LIVE_PREVIEW_COUNT=1
  acquire_preview_slot
  assert_eq "0" "$(ls -1 "$DEPLOY_QUEUE_DIR" 2>/dev/null | wc -l | tr -d ' ')" "deploy ticket should dequeue once slot opens"
}

test_existing_route_skips_deploy_queue() {
  export LOGS_PATH="${TMP_ROOT}/existing-route"
  export MAX_BUILDS=1
  export MAX_PREVIEWS=1
  _count_live_previews() { echo 1; }
  _preview_has_route() { return 0; }
  mkdir -p "$LOGS_PATH"

  export PR_NUMBER=50
  export APP_NAME="erp-pr-50"
  source_queues
  acquire_preview_slot
  assert_eq "0" "$(ls -1 "$DEPLOY_QUEUE_DIR" 2>/dev/null | wc -l | tr -d ' ')" "existing route should not enqueue deploy wait"
}

test_stale_queue_ticket_removed() {
  export LOGS_PATH="${TMP_ROOT}/stale-ticket"
  export MAX_BUILDS=1
  export MAX_PREVIEWS=10
  export PR_NUMBER=60
  export APP_NAME="erp-pr-60"
  mkdir -p "${LOGS_PATH}/queues/build"
  source_queues

  echo 999999 > "${BUILD_QUEUE_DIR}/dead-ticket"
  cleanup_stale_queue_tickets "$BUILD_QUEUE_DIR"
  assert_eq "0" "$(ls -1 "$BUILD_QUEUE_DIR" 2>/dev/null | wc -l | tr -d ' ')" "stale queue ticket should be removed"
}

test_stale_build_slot_removed() {
  export LOGS_PATH="${TMP_ROOT}/stale-slot"
  export MAX_BUILDS=1
  export MAX_PREVIEWS=10
  export PR_NUMBER=70
  export APP_NAME="erp-pr-70"
  mkdir -p "${LOGS_PATH}/locks/build-slots/dead-slot"
  source_queues

  echo 999999 > "${BUILD_SLOTS_DIR}/dead-slot/pid"
  cleanup_stale_build_slots
  assert_eq "0" "$(count_active_builds)" "stale build slot should be removed"
}

test_release_frees_build_slot() {
  export LOGS_PATH="${TMP_ROOT}/release-slot"
  export MAX_BUILDS=1
  export MAX_PREVIEWS=10
  export PR_NUMBER=80
  export APP_NAME="erp-pr-80"
  mkdir -p "$LOGS_PATH"
  source_queues

  acquire_global_build_lock
  assert_eq "1" "$(count_active_builds)" "slot should be held during build"
  release_global_build_lock
  assert_eq "0" "$(count_active_builds)" "slot should be released after build"
}

test_evicts_oldest_preview_when_full() {
  export LOGS_PATH="${TMP_ROOT}/evict-oldest"
  export MAX_BUILDS=1
  export MAX_PREVIEWS=2
  local evicted_pr=""
  local live_count=2
  _count_live_previews() { echo "$live_count"; }
  _preview_has_route() { return 1; }
  _evict_oldest_preview() {
    evicted_pr="$1"
    live_count=$((live_count - 1))
    rm -f "$PREVIEW_SLOTS_DIR"/*-pr"${evicted_pr}" 2>/dev/null || true
  }
  mkdir -p "$LOGS_PATH"

  export PR_NUMBER=100
  export APP_NAME="erp-pr-100"
  source_queues
  mkdir -p "$PREVIEW_SLOTS_DIR"

  # Two older previews already deployed; PR 55 is oldest.
  touch "${PREVIEW_SLOTS_DIR}/1000000000-pr55"
  touch "${PREVIEW_SLOTS_DIR}/2000000000-pr66"

  acquire_preview_slot

  assert_eq "55" "$evicted_pr" "should evict the oldest preview (PR #55)"
  assert_eq "0" "$(ls -1 "$DEPLOY_QUEUE_DIR" 2>/dev/null | wc -l | tr -d ' ')" "deploy ticket should be dequeued after acquiring slot"
  assert_eq "2" "$(ls -1 "$PREVIEW_SLOTS_DIR" 2>/dev/null | wc -l | tr -d ' ')" "PR 55 record gone; PR 66 and PR 100 records remain"
}

test_record_unrecord_preview_slot() {
  export LOGS_PATH="${TMP_ROOT}/record-slot"
  export MAX_BUILDS=1
  export MAX_PREVIEWS=10
  export PR_NUMBER=200
  export APP_NAME="erp-pr-200"
  mkdir -p "$LOGS_PATH"
  source_queues

  record_preview_slot
  assert_eq "1" "$(ls -1 "$PREVIEW_SLOTS_DIR" 2>/dev/null | wc -l | tr -d ' ')" "slot record should exist after record"

  unrecord_preview_slot
  assert_eq "0" "$(ls -1 "$PREVIEW_SLOTS_DIR" 2>/dev/null | wc -l | tr -d ' ')" "slot record should be gone after unrecord"
}

test_no_eviction_when_no_tracked_previews() {
  export LOGS_PATH="${TMP_ROOT}/evict-fallback"
  export MAX_BUILDS=1
  export MAX_PREVIEWS=1
  local live_count=1
  _count_live_previews() { echo "$live_count"; }
  _preview_has_route() { return 1; }
  mkdir -p "$LOGS_PATH"

  export PR_NUMBER=300
  export APP_NAME="erp-pr-300"
  source_queues

  # No slot records exist — evict_oldest_preview should return 1.
  local result=0
  evict_oldest_preview || result=$?
  assert_eq "1" "$result" "evict_oldest_preview should return 1 when no tracked previews"
}

echo "Running preview queue tests..."
test_fifo_queue_order
test_single_build_slot_blocks_second
test_max_builds_allows_two_concurrent
test_max_builds_blocks_third
test_deploy_queue_waits_at_capacity
test_existing_route_skips_deploy_queue
test_stale_queue_ticket_removed
test_stale_build_slot_removed
test_release_frees_build_slot
test_evicts_oldest_preview_when_full
test_record_unrecord_preview_slot
test_no_eviction_when_no_tracked_previews

echo
echo "Tests run: ${TESTS}, failures: ${FAILURES}"
if [ "$FAILURES" -ne 0 ]; then
  exit 1
fi
echo "All tests passed."
