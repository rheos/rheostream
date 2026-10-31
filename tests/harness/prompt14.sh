#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
base="$root/deploy/compose.yaml"
override="$root/tests/harness/compose.prompt14.yaml"

head_sha() {
  git -C "$root" rev-parse HEAD
}

static_check() {
  test -f "$base"
  test -f "$override"
  test -f "$root/tests/fixtures/leads-current-handoff.json"
  grep -Fq 'RHEO_CORE_INTERNAL_API_URL: http://core:8100' "$override"
  grep -Fq 'RHEO_CORE_PUBLIC_URL: http://core:8000' "$override"
  if grep -Fq '8100:8100' "$override"; then
    echo "the acceptance stack must never publish Core 8100" >&2
    exit 1
  fi
  printf '%s\n' 'prompt14 static contract: ok'
}

prepare_env() {
  env_file=${1:?usage: prompt14.sh prepare-env /absolute/private.env}
  case "$env_file" in
    /*) ;;
    *) echo "private env path must be absolute" >&2; exit 2 ;;
  esac
  case "$env_file" in
    "$root"/*) echo "private env file must stay outside the checkout" >&2; exit 2 ;;
  esac
  sha=$(head_sha)
  short=$(printf '%s' "$sha" | cut -c1-12)
  project="rheo-stream-p14-$short"
  umask 077
  {
    printf 'RHEO_PROMPT14_PROJECT=%s\n' "$project"
    printf 'COMPOSE_PROJECT_NAME=%s\n' "$project"
    printf 'RHEO_PROMPT14_SOURCE_HEAD=%s\n' "$sha"
    printf 'RHEO_PROMPT14_CORE_IMAGE=rheo-stream-p14-core:%s\n' "$short"
    printf 'RHEO_PROMPT14_WEB_IMAGE=rheo-stream-p14-web:%s\n' "$short"
    printf 'RHEO_PROMPT14_CORE_PORT=8000\n'
    printf 'RHEO_PROMPT14_WEB_PORT=3000\n'
  } >"$env_file"
  chmod 600 "$env_file"
  printf 'private env prepared for %s at %s\n' "$project" "$env_file"
}

load_env() {
  env_file=${1:?private env file required}
  test -f "$env_file"
  # The helper created this file from fixed names and the current source head.
  # shellcheck disable=SC1090
  . "$env_file"
  current=$(head_sha)
  test "${RHEO_PROMPT14_SOURCE_HEAD:-}" = "$current" || {
    echo "private env source head does not match HEAD" >&2
    exit 2
  }
  case "${RHEO_PROMPT14_PROJECT:-}" in
    rheo-stream-p14-*) ;;
    *) echo "refusing non-Prompt-14 Compose project" >&2; exit 2 ;;
  esac
  export RHEO_PROMPT14_PROJECT COMPOSE_PROJECT_NAME RHEO_PROMPT14_SOURCE_HEAD
  export RHEO_PROMPT14_CORE_IMAGE RHEO_PROMPT14_WEB_IMAGE
  export RHEO_PROMPT14_CORE_PORT RHEO_PROMPT14_WEB_PORT
  # Only current-omitted chooses a different module set, explicitly.
  unset RHEO_PROMPT14_MODULES
}

compose() {
  docker compose --env-file "$env_file" -p "$RHEO_PROMPT14_PROJECT" \
    -f "$base" -f "$override" "$@"
}

save_config() {
  proof_dir=${2:?proof directory required}
  case "$proof_dir" in
    /*) ;;
    *) echo "proof directory must be absolute" >&2; exit 2 ;;
  esac
  mkdir -p "$proof_dir"
  compose config >"$proof_dir/compose-config.yaml"
  compose config --format json >"$proof_dir/compose-config.json"
  RHEO_PROMPT14_PROJECT="$RHEO_PROMPT14_PROJECT" \
    RHEO_PROMPT14_SOURCE_HEAD="$RHEO_PROMPT14_SOURCE_HEAD" \
    "$root/.venv/bin/python" "$root/tests/harness/prompt14_acceptance.py" \
      verify-compose "$proof_dir/compose-config.json" \
      >"$proof_dir/ownership-proof.txt"
  printf '%s\n' "$proof_dir"
}

# The audit browser: a throwaway Chrome profile holding one ordinary rheo_session
# cookie for the seeded owner on http://localhost. The session is minted in the
# project's core container and its secret goes through a pipe straight into the
# DevTools cookie setter: it is never printed, logged, written to disk, put in a
# URL or command line, or kept in a long-lived environment. The profile lives until
# browser-stop (or until browser-start fails, when the trap removes it).
browser_start() {
  state_dir=${1:?usage: prompt14.sh browser-start ENV STATE_DIR [--headless]}
  case "$state_dir" in
    /*) ;;
    *) echo "state directory must be absolute" >&2; exit 2 ;;
  esac
  case "$state_dir" in
    "$root"/*) echo "state directory must stay outside the checkout" >&2; exit 2 ;;
  esac
  if [ -e "$state_dir/browser.env" ]; then
    echo "an audit browser is already recorded there; run browser-stop first" >&2
    exit 2
  fi
  chrome="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  test -x "$chrome"
  headless=""
  if [ "${2:-}" = "--headless" ]; then
    headless="--headless=new"
  fi
  mkdir -p "$state_dir"
  chmod 700 "$state_dir"
  profile=$(mktemp -d "${TMPDIR:-/tmp}/rheo-audit-profile.XXXXXX")
  chrome_pid=""
  cleanup() {
    if [ -n "$chrome_pid" ]; then
      kill "$chrome_pid" 2>/dev/null || true
    fi
    rm -rf "$profile"
    rm -f "$state_dir/browser.env"
  }
  trap cleanup EXIT INT TERM
  "$chrome" --user-data-dir="$profile" --remote-debugging-port=0 \
    --no-first-run --no-default-browser-check $headless about:blank \
    >/dev/null 2>&1 &
  chrome_pid=$!
  tries=0
  while [ ! -s "$profile/DevToolsActivePort" ]; do
    tries=$((tries + 1))
    if [ "$tries" -gt 150 ]; then
      echo "the audit browser did not open its DevTools port" >&2
      exit 1
    fi
    sleep 0.2
  done
  port=$(head -n 1 "$profile/DevToolsActivePort")
  compose exec -T core /app/.venv/bin/python \
    /app/tests/harness/prompt14_acceptance.py mint-session \
    | node "$root/tests/harness/audit_browser.mjs" set-cookie "$port"
  {
    printf 'AUDIT_BROWSER_PID=%s\n' "$chrome_pid"
    printf 'AUDIT_BROWSER_PROFILE=%s\n' "$profile"
    printf 'AUDIT_BROWSER_PORT=%s\n' "$port"
  } >"$state_dir/browser.env"
  chmod 600 "$state_dir/browser.env"
  trap - EXIT INT TERM
  printf 'audit browser ready: DevTools port %s, profile %s\n' "$port" "$profile"
}

browser_stop() {
  state_dir=${1:?usage: prompt14.sh browser-stop STATE_DIR}
  test -f "$state_dir/browser.env"
  # The file holds only a pid, a profile path and a port that browser-start wrote.
  # shellcheck disable=SC1091
  . "$state_dir/browser.env"
  case "$AUDIT_BROWSER_PROFILE" in
    */rheo-audit-profile.*) ;;
    *) echo "refusing to remove an unexpected profile path" >&2; exit 2 ;;
  esac
  kill "$AUDIT_BROWSER_PID" 2>/dev/null || true
  tries=0
  while kill -0 "$AUDIT_BROWSER_PID" 2>/dev/null; do
    tries=$((tries + 1))
    if [ "$tries" -gt 50 ]; then
      kill -9 "$AUDIT_BROWSER_PID" 2>/dev/null || true
      break
    fi
    sleep 0.2
  done
  rm -rf "$AUDIT_BROWSER_PROFILE"
  rm -f "$state_dir/browser.env"
  test ! -e "$AUDIT_BROWSER_PROFILE"
  printf 'audit browser stopped; profile %s removed\n' "$AUDIT_BROWSER_PROFILE"
}

case "${1:-}" in
  static-check)
    static_check
    ;;
  prepare-env)
    prepare_env "${2:-}"
    ;;
  config)
    load_env "${2:-}"
    save_config "$env_file" "${3:-}"
    ;;
  build)
    load_env "${2:-}"
    # Image tags name HEAD, so the tree must be exactly HEAD.
    "$root/.venv/bin/python" "$root/tests/harness/prompt14_acceptance.py" \
      require-clean "$root"
    save_config "$env_file" "${3:-}" >/dev/null
    compose build core worker web
    ;;
  up)
    # No worker service: the seed runs every worker visit itself, so the
    # approved-queued and executing fixtures stay durable for the audit.
    load_env "${2:-}"
    save_config "$env_file" "${3:-}" >/dev/null
    compose up -d --wait --no-build postgres core web
    ;;
  current-omitted)
    # Runtime recipe for handoff-unavailable-item-unavailable-project: recreate
    # only this project's core without the Current module loaded, so every
    # Current read the Leads viewer aggregate makes is unavailable.
    load_env "${2:-}"
    RHEO_PROMPT14_MODULES=relationships,leads
    export RHEO_PROMPT14_MODULES
    save_config "$env_file" "${3:-}" >/dev/null
    compose up -d --wait --no-build --no-deps core
    ;;
  current-restored)
    load_env "${2:-}"
    save_config "$env_file" "${3:-}" >/dev/null
    compose up -d --wait --no-build --no-deps core
    ;;
  core-stop)
    # Runtime recipe for approval-temporarily-unavailable: stop only this
    # project's core while web keeps serving the unchanged approval URL.
    load_env "${2:-}"
    save_config "$env_file" "${3:-}" >/dev/null
    compose stop core
    ;;
  core-start)
    load_env "${2:-}"
    save_config "$env_file" "${3:-}" >/dev/null
    compose up -d --wait --no-build --no-deps core
    ;;
  seed)
    load_env "${2:-}"
    save_config "$env_file" "${3:-}" >/dev/null
    compose exec -T core /app/.venv/bin/python \
      /app/tests/harness/prompt14_acceptance.py seed
    ;;
  urls)
    load_env "${2:-}"
    compose exec -T core /app/.venv/bin/python \
      /app/tests/harness/prompt14_acceptance.py urls
    ;;
  reset)
    load_env "${2:-}"
    save_config "$env_file" "${3:-}" >/dev/null
    compose down -v --remove-orphans
    ;;
  browser-start)
    load_env "${2:-}"
    browser_start "${3:-}" "${4:-}"
    ;;
  browser-stop)
    browser_stop "${2:-}"
    ;;
  *)
    echo "usage: prompt14.sh static-check | prepare-env ENV | config|build|up|seed|reset|current-omitted|current-restored|core-stop|core-start ENV PROOF_DIR | urls ENV | browser-start ENV STATE_DIR [--headless] | browser-stop STATE_DIR" >&2
    exit 2
    ;;
esac
