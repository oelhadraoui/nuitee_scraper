#!/usr/bin/env bash
# =============================================================================
# run_cycle.sh - one full scraping cycle (national + international in parallel)
#
# Each cycle writes into its own folder named after its START time:
#   output/runs/run_YYYY-MM-DD_HH-MM/
#       national/        prices.csv, prices_clean.csv, scraper.log, quality_report.json
#       international/   same
#       prices.csv, prices_clean.csv, quality_report.json, shortlist.csv  (merged)
#       cycle.log        this script's own log
#       STATUS           running | done | failed (+ start/end times)
#
# Cron calls this every minute under `flock -n`, so a new cycle starts as soon
# as the previous one has finished (both workers ended), and never overlaps.
# =============================================================================
set -uo pipefail

PROJECT_DIR="${PROJECT_DIR:-$HOME/nuitee-hotel-comparator}"
STALL_MINUTES="${STALL_MINUTES:-60}"   # kill a worker whose log hasn't moved for this long
cd "$PROJECT_DIR" || exit 1

START_HUMAN="$(date '+%Y-%m-%d %H:%M:%S %Z')"
RUN_NAME="run_$(date '+%Y-%m-%d_%H-%M')"
RUN_REL="output/runs/$RUN_NAME"          # path relative to project (= /app inside container)
RUN_DIR="$PROJECT_DIR/$RUN_REL"
mkdir -p "$RUN_DIR/national" "$RUN_DIR/international"
chmod -R a+rwX "$RUN_DIR"                # container user may differ from host user
ln -sfn "runs/$RUN_NAME" "$PROJECT_DIR/output/latest"

LOG="$RUN_DIR/cycle.log"
exec >>"$LOG" 2>&1
log() { echo "[$(date '+%F %T')] $*"; }
status() { printf 'state: %s\nstarted: %s\n%s\n' "$1" "$START_HUMAN" "${2:-}" >"$RUN_DIR/STATUS"; }

status running
log "=== cycle $RUN_NAME started"

COMPOSE=(docker compose --profile scopes)

declare -A PID
start_worker() {   # $1 = national | international  (runs in the main shell so `wait` works)
  local scope=$1 input
  [ "$scope" = national ] && input=input_morocco.json || input=input_international.json
  "${COMPOSE[@]}" run --rm -T --name "nuitee_${scope}_${RUN_NAME}" \
    -e STORAGE_OUTPUT_DIR="/app/$RUN_REL/$scope" \
    "$scope" run --input "$input" --only-offset all --run-id "${scope}_${RUN_NAME}" \
    </dev/null >>"$RUN_DIR/$scope/worker.stdout.log" 2>&1 &
  PID[$scope]=$!
}

# Watchdog: returns 0 if the worker's scraper.log went stale.
stalled() {
  local f="$RUN_DIR/$1/scraper.log"
  [ -f "$f" ] || f="$RUN_DIR/$1/worker.stdout.log"
  [ -n "$(find "$f" -mmin +"$STALL_MINUTES" 2>/dev/null)" ]
}

for s in national international; do
  start_worker "$s"
  log "started $s (pid ${PID[$s]})"
done

declare -A RC
while :; do
  running=0
  for s in national international; do
    [ -n "${RC[$s]:-}" ] && continue
    if kill -0 "${PID[$s]}" 2>/dev/null; then
      running=1
      if stalled "$s"; then
        log "WATCHDOG: $s log unchanged for ${STALL_MINUTES} min - stopping it"
        docker stop -t 30 "nuitee_${s}_${RUN_NAME}" >/dev/null 2>&1
      fi
    else
      wait "${PID[$s]}"; RC[$s]=$?
      log "$s finished (exit ${RC[$s]})"
    fi
  done
  [ $running -eq 0 ] && break
  sleep 60
done

# Merge both scopes + report + shortlist into the run folder.
log "merging national + international"
RUNENV=(-e STORAGE_OUTPUT_DIR="/app/$RUN_REL")
docker compose run --rm -T "${RUNENV[@]}" scraper merge "$RUN_REL/national" "$RUN_REL/international"
docker compose run --rm -T "${RUNENV[@]}" scraper report
docker compose run --rm -T "${RUNENV[@]}" scraper shortlist --output "$RUN_REL/shortlist.csv"

END_HUMAN="$(date '+%Y-%m-%d %H:%M:%S %Z')"
if [ "${RC[national]}" -eq 0 ] && [ "${RC[international]}" -eq 0 ]; then
  status done "finished: $END_HUMAN"
else
  status failed "finished: $END_HUMAN
national_exit: ${RC[national]}
international_exit: ${RC[international]}"
fi
log "=== cycle $RUN_NAME ended (national=${RC[national]} international=${RC[international]})"
sleep 60   # small pause so a crash-looping cycle can't hammer the sites
