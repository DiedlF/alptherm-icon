#!/usr/bin/env bash
# OGN thermal evaluation wrapper (plan §6.2 + §6.3).
#
# Thin shell wrapper around ``python -m alptherm_icon.igc_pipeline``.
# Activates the venv, acquires a per-subcommand flock, and routes
# stdout/stderr into a dated log — same conventions as archive.sh.
#
# Usage:
#     eval.sh eval-day YYYY-MM-DD   # stage 1+2: detect + assign for one day
#     eval.sh eval-day yesterday    # convenience: yesterday in UTC
#     eval.sh backfill [N]          # all/last-N unprocessed days
#     eval.sh reassign              # replay region join on existing parquets
#
# Recommended systemd timer (runs eval.sh eval-day yesterday at 20:30 UTC):
#   → see ops/systemd/alptherm-eval.timer
#
# The hour window is 07–18 UTC (covers the full convective day including
# early morning gliders and late evening wave; filters pre-dawn ADS-B noise).
# All days are evaluated regardless of the Tier-2 trigger — we want the
# complete dataset including low-activity days (see plan §8.1).

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <eval-day YYYY-MM-DD|yesterday | backfill [N] | reassign>" >&2
    exit 2
fi

SUBCMD="$1"
shift

REPO_ROOT="${ALPTHERM_ICON_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
VENV="${ALPTHERM_ICON_VENV:-$REPO_ROOT/.venv}"
LOCK_DIR="${ALPTHERM_ICON_LOCK_DIR:-$REPO_ROOT/data/archive/locks}"
LOG_DIR="${ALPTHERM_ICON_LOG_DIR:-$REPO_ROOT/data/archive/logs}"

mkdir -p "$LOCK_DIR" "$LOG_DIR"

LOCK_FILE="$LOCK_DIR/eval-${SUBCMD}.lock"
LOG_FILE="$LOG_DIR/eval-$(date -u +%Y%m%d).log"

if [[ ! -f "$VENV/bin/activate" ]]; then
    echo "[$(date -u +%FT%TZ)] FATAL: no venv at $VENV" | tee -a "$LOG_FILE"
    exit 2
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "[$(date -u +%FT%TZ)] previous 'eval-${SUBCMD}' still holds $LOCK_FILE — skipping" \
        | tee -a "$LOG_FILE"
    exit 0
fi

cd "$REPO_ROOT"

case "$SUBCMD" in
    eval-day)
        if [[ $# -lt 1 ]]; then
            echo "usage: $0 eval-day YYYY-MM-DD|yesterday" >&2
            exit 2
        fi
        DAY="$1"
        if [[ "$DAY" == "yesterday" ]]; then
            DAY=$(date -u -d "yesterday" +%Y-%m-%d)
        fi
        echo "[$(date -u +%FT%TZ)] eval-day $DAY starting" | tee -a "$LOG_FILE"
        python -m alptherm_icon.igc_pipeline detect \
            --day "$DAY" \
            --hour-lo 7 --hour-hi 18 \
            >> "$LOG_FILE" 2>&1
        ;;

    backfill)
        DAYS="${1:-}"
        DAYS_ARG=""
        if [[ -n "$DAYS" ]]; then
            DAYS_ARG="--days $DAYS"
        fi
        echo "[$(date -u +%FT%TZ)] backfill ${DAYS:+last $DAYS days} starting" \
            | tee -a "$LOG_FILE"
        # shellcheck disable=SC2086
        python -m alptherm_icon.igc_pipeline backfill $DAYS_ARG >> "$LOG_FILE" 2>&1
        ;;

    reassign)
        echo "[$(date -u +%FT%TZ)] reassign starting" | tee -a "$LOG_FILE"
        python -m alptherm_icon.igc_pipeline reassign >> "$LOG_FILE" 2>&1
        ;;

    *)
        echo "[$(date -u +%FT%TZ)] FATAL: unknown subcommand '$SUBCMD'" \
            | tee -a "$LOG_FILE"
        exit 2
        ;;
esac

rc=$?
echo "[$(date -u +%FT%TZ)] 'eval-${SUBCMD}' finished rc=$rc" | tee -a "$LOG_FILE"
exit "$rc"
