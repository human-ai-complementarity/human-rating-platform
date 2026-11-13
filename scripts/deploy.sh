#!/usr/bin/env bash
set -euo pipefail

# ─── Deploy Render Services ──────────────────────────────────────────────────
#
# Triggers API and/or web deploys via the Render API, streams logs live during
# polling, and exits non-zero on failure/timeout.
#
# Usage:
#   ./scripts/deploy.sh
#   RENDER_DEPLOY_TARGET=api ./scripts/deploy.sh
#   RENDER_CLEAR_CACHE=do_not_clear ./scripts/deploy.sh
#
# Required env:
#   RENDER_API_KEY              Render API bearer token
#   RENDER_API_SERVICE_ID       API service ID (srv-...) when target includes api
#   RENDER_WEB_SERVICE_ID       Web service ID (srv-...) when target includes web
#
# Optional env:
#   RENDER_WORKER_SERVICE_ID        Pilot check worker service ID (srv-...); when set,
#                                   it deploys with every api deploy (same backend code)
#   RENDER_DEPLOY_TARGET            both | api | web | none (default: both)
#   RENDER_LOG_MODE                 tail | quiet (default: tail)
#   RENDER_COMMIT_ID                Git SHA to deploy (default: service branch head)
#   RENDER_DEPLOY_POLL_SECONDS      Poll interval (default: 10)
#   RENDER_DEPLOY_TIMEOUT_SECONDS   Max wait (default: 1200)
#   RENDER_CLEAR_CACHE              clear | do_not_clear (default: clear)
#   RENDER_ARTIFACT_DIR             Output dir (default: artifacts/render-deploy)
#   RENDER_API_URL                  API base (default: https://api.render.com)
#
# Exit codes:
#   0   All targeted services reached "live" (or target=none)
#   1   Deploy failure, timeout, missing deps, or trigger error
#
# Artifacts ($RENDER_ARTIFACT_DIR):
#   timeline.log              Status per poll tick
#   summary.txt               Deploy IDs + final result
#   {api,web}-deploy.json     Final deploy snapshot
#   {api,web}-events.json     Last 30 service events
#   {api,web}-logs.json       Bounded log window near failure/timeout
#   {api,web}-logs-tail.txt   Human-readable extracted log lines
#   {api,web}-predeploy-logs.json       Predeploy task-run log window for this deploy
#   {api,web}-predeploy-logs-tail.txt   Human-readable predeploy logs
# ───────────────────────────────────────────────────────────────────────────────

# ─── Helpers ──────────────────────────────────────────────────────────────────
require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "error: required command not found: $1" >&2
    exit 1
  fi
}

require_env() {
  local name="$1"
  if [ -z "${!name:-}" ]; then
    echo "error: required environment variable is missing: $name" >&2
    exit 1
  fi
}

require_int() {
  local name="$1"
  if ! printf '%s' "${!name}" | grep -Eq '^[0-9]+$'; then
    echo "error: ${name} must be a positive integer" >&2
    exit 1
  fi
}

now_iso_utc() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

uri_encode() {
  jq -rn --arg v "$1" '$v|@uri'
}

latest_log_timestamp() {
  local json_file="$1"
  jq -r '
    [((.logs // .result.logs // .entries // .data.logs // [])[]
      | (.timestamp // .time // .ts)
      | select(type=="string"))][-1] // empty
  ' "$json_file" 2>/dev/null || true
}

api_get() {
  local path="$1"
  curl -fsS \
    -H "Authorization: Bearer ${RENDER_API_KEY}" \
    -H "Accept: application/json" \
    "${RENDER_API_URL}${path}"
}

api_post() {
  local path="$1"
  local payload="$2"
  local output_file="$3"

  curl -sS \
    -o "$output_file" \
    -w "%{http_code}" \
    -H "Authorization: Bearer ${RENDER_API_KEY}" \
    -H "Accept: application/json" \
    -H "Content-Type: application/json" \
    -X POST \
    "${RENDER_API_URL}${path}" \
    -d "$payload"
}

service_id_for() {
  case "$1" in
    api)
      printf '%s\n' "${RENDER_API_SERVICE_ID:-}"
      ;;
    web)
      printf '%s\n' "${RENDER_WEB_SERVICE_ID:-}"
      ;;
    worker)
      printf '%s\n' "${RENDER_WORKER_SERVICE_ID:-}"
      ;;
    *)
      return 1
      ;;
  esac
}

latest_deploy_id() {
  local service_id="$1"
  local response
  response="$(api_get "/v1/services/${service_id}/deploys?limit=1" 2>/dev/null || true)"
  printf '%s' "$response" | jq -r '.[0].deploy.id // empty' 2>/dev/null || true
}

deploy_status() {
  local service_id="$1"
  local deploy_id="$2"
  local response
  response="$(api_get "/v1/services/${service_id}/deploys/${deploy_id}" 2>/dev/null || true)"
  if [ -z "$response" ]; then
    printf '%s\n' "unknown"
    return 0
  fi
  printf '%s' "$response" | jq -r '.status // "unknown"' 2>/dev/null || printf '%s\n' "unknown"
}

deploy_created_at() {
  local service_id="$1"
  local deploy_id="$2"
  local response
  response="$(api_get "/v1/services/${service_id}/deploys/${deploy_id}" 2>/dev/null || true)"
  if [ -z "$response" ]; then
    return 0
  fi
  printf '%s' "$response" | jq -r '.createdAt // empty' 2>/dev/null || true
}

is_failure_status() {
  case "$1" in
    build_failed|pre_deploy_failed|update_failed|canceled)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

format_log_lines() {
  local json_file="$1"
  extract_log_lines "$json_file" \
    | awk -F '\t' '{
        ts = $1; $1 = ""; sub(/^\t/, "", $0)
        print (ts == "" ? "unknown_time" : ts) " " $0
      }'
}

extract_log_lines() {
  local json_file="$1"
  jq -r '
    (try (.logs // .result.logs // .entries // .data.logs) catch []) as $raw
    | (if ($raw | type) == "array" then $raw else [] end)
    | .[]
    | [
        (.timestamp // .time // .ts // ""),
        ((.message // .line // .text // .msg // "") | tostring)
      ]
    | @tsv
  ' "$json_file" 2>/dev/null || true
}

resolve_owner_id() {
  local service="$1"
  local service_id="$2"
  local owner_var="${service}_owner_id"
  local cached_owner="${!owner_var:-}"

  if [ -n "$cached_owner" ]; then
    if [ "$cached_owner" = "__unavailable__" ]; then
      return 1
    fi
    printf '%s\n' "$cached_owner"
    return 0
  fi

  local owner_id
  owner_id="$({
    api_get "/v1/services/${service_id}" \
      | jq -r '.ownerId // .service.ownerId // empty'
  } 2>/dev/null || true)"

  if [ -z "$owner_id" ]; then
    printf -v "$owner_var" '%s' "__unavailable__"
    return 1
  fi

  printf -v "$owner_var" '%s' "$owner_id"
  printf '%s\n' "$owner_id"
}

query_service_logs() {
  local owner_id="$1"
  local service_id="$2"
  local start_iso="$3"
  local end_iso="$4"
  local limit="$5"
  local output_file="$6"
  local task_run="${7:-}"

  local start_q
  start_q="$(uri_encode "$start_iso")"
  local end_q
  end_q="$(uri_encode "$end_iso")"
  local task_q=""
  if [ -n "$task_run" ]; then
    task_q="&taskRun=$(uri_encode "$task_run")"
  fi

  api_get "/v1/logs?ownerId=${owner_id}&resource=${service_id}${task_q}&startTime=${start_q}&endTime=${end_q}&direction=forward&limit=${limit}&type=build&type=app" >"$output_file"
}

capture_snapshots() {
  local service="$1"
  local service_id="$2"
  local deploy_id="$3"

  api_get "/v1/services/${service_id}/deploys/${deploy_id}" \
    >"${ARTIFACT_DIR}/${service}-deploy.json" || true
  api_get "/v1/services/${service_id}/events?limit=30" \
    >"${ARTIFACT_DIR}/${service}-events.json" || true
}

capture_failure_logs() {
  local service="$1"
  local service_id="$2"
  local deploy_start_iso="$3"
  local deploy_id="$4"

  local owner_id
  owner_id="$(resolve_owner_id "$service" "$service_id" || true)"
  if [ -z "$owner_id" ]; then
    return 0
  fi

  local end_iso
  end_iso="$(now_iso_utc)"

  local raw_file="${ARTIFACT_DIR}/${service}-logs.json"
  local tail_file="${ARTIFACT_DIR}/${service}-logs-tail.txt"
  : >"$tail_file"

  if ! query_service_logs "$owner_id" "$service_id" "$deploy_start_iso" "$end_iso" "500" "$raw_file" 2>/dev/null; then
    echo "unable to fetch deployment-scoped logs for ${service}" >"$tail_file"
    return 0
  fi

  format_log_lines "$raw_file" >"$tail_file" || true

  local predeploy_task_run=""
  local events_file="${ARTIFACT_DIR}/${service}-events.json"
  if [ -f "$events_file" ]; then
    predeploy_task_run="$(jq -r --arg d "$deploy_id" '
      .[]
      | select(.event.type == "pre_deploy_started")
      | select(.event.details.deployId == $d)
      | .event.details.deployCommandExecutionId // empty
    ' "$events_file" | head -n 1)"
  fi

  if [ -n "$predeploy_task_run" ]; then
    local pre_raw_file="${ARTIFACT_DIR}/${service}-predeploy-logs.json"
    local pre_tail_file="${ARTIFACT_DIR}/${service}-predeploy-logs-tail.txt"
    : >"$pre_tail_file"
    if query_service_logs "$owner_id" "$service_id" "$deploy_start_iso" "$end_iso" "500" "$pre_raw_file" "$predeploy_task_run" 2>/dev/null; then
      format_log_lines "$pre_raw_file" >"$pre_tail_file" || true
    else
      echo "unable to fetch predeploy task-run logs for ${service}" >"$pre_tail_file"
    fi
  fi
}

tail_live_logs() {
  local service="$1"
  local service_id="$2"
  local end_iso="$3"
  local deploy_start_iso="$4"

  if [ "$RENDER_LOG_MODE" != "tail" ]; then
    return 0
  fi

  local owner_id
  owner_id="$(resolve_owner_id "$service" "$service_id" || true)"
  if [ -z "$owner_id" ]; then
    return 0
  fi

  local cursor_var="${service}_log_cursor_at"
  local start_iso="${!cursor_var:-$deploy_start_iso}"
  local tmp_file
  tmp_file="$(mktemp)"

  if ! query_service_logs "$owner_id" "$service_id" "$start_iso" "$end_iso" "100" "$tmp_file" 2>/dev/null; then
    rm -f "$tmp_file"
    printf -v "$cursor_var" '%s' "$end_iso"
    return 0
  fi

  extract_log_lines "$tmp_file" \
    | awk -F '\t' -v svc="$service" -v now="$(now_iso_utc)" '
        $1 == "" && $2 == "" { next }
        NR <= 50 {
          ts = ($1 != "") ? $1 : now
          $1 = ""; sub(/^\t/, "", $0); gsub(/\r/, " ", $0)
          printf "[%s] %s %s\n", svc, ts, $0
        }
        NR == 51 {
          printf "[%s] ... log output truncated after 50 lines for this poll tick ...\n", svc
        }
      '

  local latest_ts
  latest_ts="$(latest_log_timestamp "$tmp_file")"
  rm -f "$tmp_file"
  if [ -n "$latest_ts" ]; then
    printf -v "$cursor_var" '%s' "$latest_ts"
  else
    printf -v "$cursor_var" '%s' "$end_iso"
  fi
}

capture_result() {
  local result="$1"
  echo "result=${result}" >>"$SUMMARY_FILE"
  for service in "${target_services[@]}"; do
    local service_id_var="${service}_service_id"
    local deploy_id_var="${service}_deploy_id"
    local state_var="${service}_state"
    local started_at_var="${service}_deploy_started_at"
    echo "${service}_status=${!state_var}" >>"$SUMMARY_FILE"
    capture_snapshots "$service" "${!service_id_var}" "${!deploy_id_var}"
    if [ "$result" != "success" ]; then
      capture_failure_logs "$service" "${!service_id_var}" "${!started_at_var}" "${!deploy_id_var}"
    fi
  done
}

trigger_deploy() {
  local service="$1"
  local service_id="$2"
  local before_id="$3"

  local trigger_body
  trigger_body="$(mktemp)"
  local payload
  if [ -n "${RENDER_COMMIT_ID:-}" ]; then
    payload="$(jq -cn --arg clear "$RENDER_CLEAR_CACHE" --arg commit "$RENDER_COMMIT_ID" '{clearCache: $clear, commitId: $commit}')"
  else
    payload="$(jq -cn --arg clear "$RENDER_CLEAR_CACHE" '{clearCache: $clear}')"
  fi

  local status_code
  status_code="$(api_post "/v1/services/${service_id}/deploys" "$payload" "$trigger_body")"

  if [ "$status_code" != "201" ] && [ "$status_code" != "202" ]; then
    echo "error: failed to trigger ${service} deploy (status=${status_code})" >&2
    cat "$trigger_body" >&2 || true
    rm -f "$trigger_body"
    exit 1
  fi

  local deploy_id
  deploy_id="$(jq -r '.id // empty' "$trigger_body" 2>/dev/null || true)"
  rm -f "$trigger_body"

  if [ -n "$deploy_id" ]; then
    printf '%s\n' "$deploy_id"
    return 0
  fi

  # Some 202 responses do not include an ID. Resolve by observing latest deploy ID change.
  for _ in $(seq 1 30); do
    deploy_id="$(latest_deploy_id "$service_id")"
    if [ -n "$deploy_id" ] && [ "$deploy_id" != "$before_id" ]; then
      printf '%s\n' "$deploy_id"
      return 0
    fi
    sleep 2
  done

  echo "error: could not resolve new deploy id for ${service}" >&2
  exit 1
}

# ─── Preflight ────────────────────────────────────────────────────────────────
require_cmd curl
require_cmd jq
require_cmd awk
require_env RENDER_API_KEY

RENDER_API_URL="${RENDER_API_URL:-https://api.render.com}"
RENDER_DEPLOY_POLL_SECONDS="${RENDER_DEPLOY_POLL_SECONDS:-10}"
RENDER_DEPLOY_TIMEOUT_SECONDS="${RENDER_DEPLOY_TIMEOUT_SECONDS:-1200}"
RENDER_CLEAR_CACHE="${RENDER_CLEAR_CACHE:-clear}"
RENDER_LOG_MODE="${RENDER_LOG_MODE:-tail}"
RENDER_DEPLOY_TARGET="${RENDER_DEPLOY_TARGET:-both}"
RENDER_COMMIT_ID="${RENDER_COMMIT_ID:-}"
ARTIFACT_DIR="${RENDER_ARTIFACT_DIR:-artifacts/render-deploy}"

case "$RENDER_DEPLOY_TARGET" in
  both)
    target_services=("api" "web")
    ;;
  api)
    target_services=("api")
    ;;
  web)
    target_services=("web")
    ;;
  none)
    target_services=()
    ;;
  *)
    echo "error: RENDER_DEPLOY_TARGET must be one of: both, api, web, none" >&2
    exit 1
    ;;
esac

# The pilot check worker runs the backend code, so it follows the api.
if [ -n "${RENDER_WORKER_SERVICE_ID:-}" ]; then
  for service in "${target_services[@]}"; do
    if [ "$service" = "api" ]; then
      target_services+=("worker")
      break
    fi
  done
fi

case "$RENDER_LOG_MODE" in
  tail|quiet)
    ;;
  *)
    echo "error: RENDER_LOG_MODE must be 'tail' or 'quiet'" >&2
    exit 1
    ;;
esac

require_int RENDER_DEPLOY_POLL_SECONDS
require_int RENDER_DEPLOY_TIMEOUT_SECONDS

if [ "${#target_services[@]}" -eq 0 ]; then
  mkdir -p "$ARTIFACT_DIR"
  SUMMARY_FILE="${ARTIFACT_DIR}/summary.txt"
  TIMELINE_FILE="${ARTIFACT_DIR}/timeline.log"
  echo "result=skipped" >"$SUMMARY_FILE"
  echo "target=none" >>"$SUMMARY_FILE"
  echo "reason=no services targeted" >>"$SUMMARY_FILE"
  echo "poll_time_utc statuses" >"$TIMELINE_FILE"
  echo "No targeted services. Skipping Render deploy."
  exit 0
fi

for service in "${target_services[@]}"; do
  if [ "$service" = "api" ]; then
    require_env RENDER_API_SERVICE_ID
  elif [ "$service" = "web" ]; then
    require_env RENDER_WEB_SERVICE_ID
  fi
done

mkdir -p "$ARTIFACT_DIR"
TIMELINE_FILE="${ARTIFACT_DIR}/timeline.log"
SUMMARY_FILE="${ARTIFACT_DIR}/summary.txt"

SCRIPT_START_ISO="$(now_iso_utc)"

{
  echo "started_at_utc=${SCRIPT_START_ISO}"
  echo "target=${RENDER_DEPLOY_TARGET}"
  echo "log_mode=${RENDER_LOG_MODE}"
  if [ -n "$RENDER_COMMIT_ID" ]; then
    echo "commit_id=${RENDER_COMMIT_ID}"
  fi
} >"$SUMMARY_FILE"

echo "poll_time_utc statuses" >"$TIMELINE_FILE"

# ─── Trigger ──────────────────────────────────────────────────────────────────
for service in "${target_services[@]}"; do
  service_id="$(service_id_for "$service")"
  before_id="$(latest_deploy_id "$service_id" || true)"
  deploy_id="$(trigger_deploy "$service" "$service_id" "$before_id")"

  printf -v "${service}_service_id" '%s' "$service_id"
  printf -v "${service}_deploy_id" '%s' "$deploy_id"
  deploy_started_at="$(deploy_created_at "$service_id" "$deploy_id")"
  if [ -z "$deploy_started_at" ]; then
    deploy_started_at="$SCRIPT_START_ISO"
  fi
  printf -v "${service}_deploy_started_at" '%s' "$deploy_started_at"
  printf -v "${service}_log_cursor_at" '%s' "$deploy_started_at"

  echo "${service}_deploy_id=${deploy_id}" >>"$SUMMARY_FILE"
  echo "${service}_deploy_started_at=${deploy_started_at}" >>"$SUMMARY_FILE"
  echo "Triggered ${service} deploy: ${deploy_id}"
done

# ─── Poll + Tail ──────────────────────────────────────────────────────────────
deadline_epoch="$(( $(date +%s) + RENDER_DEPLOY_TIMEOUT_SECONDS ))"

while true; do
  now_iso="$(now_iso_utc)"

  status_line=""
  all_live=true
  any_failed=false

  for service in "${target_services[@]}"; do
    service_id_var="${service}_service_id"
    deploy_id_var="${service}_deploy_id"
    state_var="${service}_state"

    service_id="${!service_id_var}"
    deploy_id="${!deploy_id_var}"
    state="$(deploy_status "$service_id" "$deploy_id")"
    printf -v "$state_var" '%s' "$state"

    if [ -n "$status_line" ]; then
      status_line="${status_line} "
    fi
    status_line="${status_line}${service}=${state}"

    if [ "$state" != "live" ]; then
      all_live=false
    fi
    if is_failure_status "$state"; then
      any_failed=true
    fi
  done

  echo "${now_iso} ${status_line}" | tee -a "$TIMELINE_FILE"

  for service in "${target_services[@]}"; do
    service_id_var="${service}_service_id"
    started_at_var="${service}_deploy_started_at"
    tail_live_logs "$service" "${!service_id_var}" "$now_iso" "${!started_at_var}" || true
  done

  if [ "$all_live" = true ]; then
    capture_result success
    echo "Render deploy completed successfully."
    exit 0
  fi

  if [ "$any_failed" = true ]; then
    capture_result failure
    echo "error: render deploy failed (${status_line})" >&2
    exit 1
  fi

  if [ "$(date +%s)" -ge "$deadline_epoch" ]; then
    capture_result timeout
    echo "error: timed out waiting for render deploys (${status_line})" >&2
    exit 1
  fi

  sleep "$RENDER_DEPLOY_POLL_SECONDS"
done
