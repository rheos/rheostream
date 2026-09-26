#!/usr/bin/env bash
# Nightly pg_dumpall backup of the flagship Postgres, with verification and retention.
#
# Usage:
#   rheostream-pgdumpall.sh [run]            dump, verify, then apply retention
#   rheostream-pgdumpall.sh prune [--dry-run] [--keep FILE]...
#                                            apply retention only
#   rheostream-pgdumpall.sh verify FILE      check one dump file
#
# Configuration (environment; the cron file sets what differs from the defaults):
#   RHEO_BACKUP_COMPOSE_PROJECT  compose project label of the app (required for run)
#   RHEO_BACKUP_SERVICE          compose service name of Postgres  (default postgres)
#   RHEO_BACKUP_PGUSER           role pg_dumpall connects as       (default rheo)
#   RHEO_BACKUP_DIR              where dumps live      (default /root/rheostream-backups)
#   RHEO_BACKUP_KEEP_DAILY       daily dumps kept                  (default 14)
#   RHEO_BACKUP_KEEP_WEEKLY      Sunday dumps kept                 (default 8)
#   RHEO_BACKUP_MIN_BYTES        smallest acceptable dump file     (default 1000)
#   RHEO_BACKUP_LOG              syslog | stderr                   (default syslog)
#   RHEO_BACKUP_LOG_FILE         also append log lines to this file (default unset)
#
# Retention counts dumps; it never compares a dump's date with the clock. It keeps
# the newest dump of each of the KEEP_DAILY most recent days that have a dump, plus
# the newest dump of each of the KEEP_WEEKLY most recent Sundays (UTC) that have one.
# Whatever else happens, it never deletes: the dump this run just wrote, the newest
# good dump by name, or the most recently modified dump if it verifies. It deletes nothing
# when no good dump exists, and it only touches files named like
# pgdumpall-YYYYMMDDTHHMMSSZ.sql.gz. A failed dump exits before retention runs.
#
# Written for bash 3.2 as well as 5.x, so the retention test runs on macOS too.
set -euo pipefail

BACKUP_DIR="${RHEO_BACKUP_DIR:-/root/rheostream-backups}"
KEEP_DAILY="${RHEO_BACKUP_KEEP_DAILY:-14}"
KEEP_WEEKLY="${RHEO_BACKUP_KEEP_WEEKLY:-8}"
MIN_BYTES="${RHEO_BACKUP_MIN_BYTES:-1000}"
LOG_MODE="${RHEO_BACKUP_LOG:-syslog}"
LOG_FILE="${RHEO_BACKUP_LOG_FILE:-}"
LOG_TAG=rheostream-pgdumpall
NAME_RE='^pgdumpall-[0-9]{8}T[0-9]{6}Z\.sql\.gz$'
# pg_dumpall's last comment line; a dump cut off mid-stream does not have it.
COMPLETE_MARKER='-- PostgreSQL database cluster dump complete'

log() { # log LEVEL MESSAGE...
  local level="$1"; shift
  local line="$level: $*"
  if [ "$LOG_MODE" = syslog ] && command -v logger >/dev/null 2>&1; then
    local prio=user.info
    [ "$level" = error ] && prio=user.err
    logger -t "$LOG_TAG" -p "$prio" -- "$line" || true
    # An interactive run or an error still shows on stderr.
    if [ "$level" = error ] || [ -t 2 ]; then printf '%s\n' "$LOG_TAG $line" >&2; fi
  else
    printf '%s\n' "$LOG_TAG $line" >&2
  fi
  if [ -n "$LOG_FILE" ]; then
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$line" >>"$LOG_FILE" || true
  fi
}

die() { log error "$*"; exit 1; }

is_count() { case "$1" in '' | *[!0-9]*) return 1 ;; *) return 0 ;; esac; }

check_config() {
  is_count "$KEEP_DAILY" && [ "$KEEP_DAILY" -ge 1 ] ||
    die "RHEO_BACKUP_KEEP_DAILY must be a whole number of at least 1, got '$KEEP_DAILY'"
  is_count "$KEEP_WEEKLY" || die "RHEO_BACKUP_KEEP_WEEKLY must be a whole number, got '$KEEP_WEEKLY'"
  is_count "$MIN_BYTES" || die "RHEO_BACKUP_MIN_BYTES must be a whole number, got '$MIN_BYTES'"
  [ -d "$BACKUP_DIR" ] || die "backup directory $BACKUP_DIR does not exist"
}

# verify_dump FILE: gzip stream intact, big enough, and pg_dumpall finished writing it.
verify_dump() {
  local f="$1" size
  [ -f "$f" ] || return 1
  gzip -t -- "$f" 2>/dev/null || return 1
  size=$(wc -c <"$f" | tr -d ' ')
  [ "$size" -ge "$MIN_BYTES" ] || return 1
  # Plain grep, not grep -q: -q can exit before tail finishes writing, and under
  # pipefail tail's SIGPIPE would then fail a good dump.
  gzip -dc -- "$f" | tail -n 5 | grep -F -- "$COMPLETE_MARKER" >/dev/null || return 1
}

# is_sunday YYYYMMDD (Sakamoto's day-of-week; 0 is Sunday). No GNU date needed.
is_sunday() {
  local y=$((10#${1:0:4})) m=$((10#${1:4:2})) d=$((10#${1:6:2})) t
  case $m in
    1) t=0 ;; 2) t=3 ;; 3) t=2 ;; 4) t=5 ;; 5) t=0 ;; 6) t=3 ;;
    7) t=5 ;; 8) t=1 ;; 9) t=4 ;; 10) t=6 ;; 11) t=2 ;; 12) t=4 ;;
    *) return 1 ;;
  esac
  [ "$m" -lt 3 ] && y=$((y - 1))
  [ $(((y + y / 4 - y / 100 + y / 400 + t + d) % 7)) -eq 0 ]
}

# Dump names in the backup dir, newest name first. Names sort in time order.
list_dumps() {
  local p n
  for p in "$BACKUP_DIR"/pgdumpall-*.sql.gz; do
    n=${p##*/}
    if [ -f "$p" ] && [[ $n =~ $NAME_RE ]]; then printf '%s\n' "$n"; fi
  done | sort -r
}

prune() {
  local dry_run=0 keep_paths="" arg
  while [ $# -gt 0 ]; do
    arg="$1"; shift
    case "$arg" in
      --dry-run) dry_run=1 ;;
      --keep)
        [ $# -gt 0 ] || die "--keep needs a file"
        keep_paths="$keep_paths$(basename -- "$1")
"
        shift ;;
      *) die "prune: unknown argument '$arg'" ;;
    esac
  done
  check_config

  local names keep="" days="" daily=0 weekly=0 f day newest_good="" newest_mtime_good=""
  names=$(list_dumps)
  if [ -z "$names" ]; then
    log info "retention: no dumps in $BACKUP_DIR, nothing to do"
    return 0
  fi

  for f in $names; do
    day=${f:10:8}
    case "
$days" in *"
$day
"*) continue ;; esac # already saw a newer dump for this day
    days="$days$day
"
    if [ "$daily" -lt "$KEEP_DAILY" ]; then keep="$keep$f
"; fi
    daily=$((daily + 1))
    if is_sunday "$day"; then
      if [ "$weekly" -lt "$KEEP_WEEKLY" ]; then keep="$keep$f
"; fi
      weekly=$((weekly + 1))
    fi
  done

  for f in $names; do
    if verify_dump "$BACKUP_DIR/$f"; then newest_good="$f"; break; fi
  done
  if [ -z "$newest_good" ]; then
    log error "retention: no dump in $BACKUP_DIR verifies; deleting nothing"
    return 3
  fi
  # The most recently written dump, in case a wrong clock misnamed it. Kept when it
  # verifies; only that one file is decompressed, not every dump.
  local newest_mtime=""
  for f in $names; do
    if [ -z "$newest_mtime" ] || [ "$BACKUP_DIR/$f" -nt "$BACKUP_DIR/$newest_mtime" ]; then
      newest_mtime="$f"
    fi
  done
  if [ "$newest_mtime" != "$newest_good" ] && verify_dump "$BACKUP_DIR/$newest_mtime"; then
    newest_mtime_good="$newest_mtime"
  fi
  keep="$keep$newest_good
$newest_mtime_good
$keep_paths"

  local deleted=0 kept=0
  for f in $names; do
    case "
$keep" in
      *"
$f
"*) kept=$((kept + 1)); continue ;;
    esac
    if [ "$dry_run" = 1 ]; then
      log info "retention: would delete $f"
    else
      rm -f -- "${BACKUP_DIR:?}/$f"
      log info "retention: deleted $f"
    fi
    deleted=$((deleted + 1))
  done
  local verb=deleted
  [ "$dry_run" = 1 ] && verb="would delete"
  log info "retention: kept $kept, $verb $deleted (daily $KEEP_DAILY, weekly $KEEP_WEEKLY, newest good $newest_good)"
}

run() {
  [ $# -eq 0 ] || die "run takes no arguments"
  check_config
  local project="${RHEO_BACKUP_COMPOSE_PROJECT:-}"
  local service="${RHEO_BACKUP_SERVICE:-postgres}"
  local pguser="${RHEO_BACKUP_PGUSER:-rheo}"
  [ -n "$project" ] || die "RHEO_BACKUP_COMPOSE_PROJECT is not set"

  # One run at a time. flock is on every Linux box; the test on macOS skips it.
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$BACKUP_DIR/.lock"
    flock -n 9 || die "another backup run holds $BACKUP_DIR/.lock"
  fi

  local containers count c
  containers=$(docker ps -q \
    --filter "label=com.docker.compose.project=$project" \
    --filter "label=com.docker.compose.service=$service" \
    --filter status=running) || die "docker ps failed"
  count=$(printf '%s\n' "$containers" | grep -c . || true)
  [ "$count" = 1 ] || die "expected exactly one running $service container for project $project, found $count"
  c="$containers"

  local ts final partial
  ts=$(date -u +%Y%m%dT%H%M%SZ)
  final="$BACKUP_DIR/pgdumpall-$ts.sql.gz"
  # The partial name does not match NAME_RE, so retention never sees it.
  partial="$BACKUP_DIR/.pgdumpall-$ts.sql.gz.partial"
  [ ! -e "$final" ] || die "$final already exists"
  umask 077
  trap 'rm -f -- "$partial"' EXIT

  log info "dump: starting pg_dumpall from container $c into $final"
  if ! docker exec "$c" pg_dumpall -U "$pguser" | gzip >"$partial"; then
    die "dump: pg_dumpall or gzip failed; kept older dumps, removed the partial file"
  fi
  if ! verify_dump "$partial"; then
    die "dump: the new dump did not verify (gzip test, size >= $MIN_BYTES bytes, completion marker); kept older dumps"
  fi
  mv -- "$partial" "$final"
  trap - EXIT
  log info "dump: wrote $final ($(wc -c <"$final" | tr -d ' ') bytes, verified)"

  prune --keep "$final"
}

main() {
  local cmd="${1:-run}"
  [ $# -gt 0 ] && shift
  case "$cmd" in
    run) run "$@" ;;
    prune) prune "$@" ;;
    verify)
      [ $# -eq 1 ] || die "verify takes one file"
      if verify_dump "$1"; then log info "verify: $1 is good"; else die "verify: $1 failed"; fi ;;
    -h | --help) sed -n '2,29p' "$0" ;;
    *) die "unknown command '$cmd' (run, prune, verify)" ;;
  esac
}

main "$@"
