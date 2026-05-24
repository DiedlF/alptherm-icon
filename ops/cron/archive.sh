#!/usr/bin/env bash
# M0 archive wrapper for cron (plan §9.3 Sofortmaßnahmen).
#
# Subcommand-aware thin wrapper. The Python layer (see __main__.py) holds
# the actual logic; this script only sets up the venv, acquires a per-job
# lock, and routes stdout/stderr into a dated log.
#
# Usage:
#     archive.sh tier1 HH                # one of 00/03/06/09
#     archive.sh trigger                 # diagnostic gut-day decision
#     archive.sh download-pending        # nightly bulk Tier-2 fetch
#     archive.sh backfill N              # catch up last N days × 4 anchors
#     archive.sh status                  # manifest summary
#
# Recommended crontab (all times UTC; matches plan §9.3 table):
#
#     # Tier-1 sammlung — one per ICON-D2 anchor we keep (00/03/06/09 UTC).
#     # Each line fires ~30 min after DWD finishes publishing the run.
#     30 6  * * * /path/ops/cron/archive.sh tier1 03
#     30 9  * * * /path/ops/cron/archive.sh tier1 06
#     30 11 * * * /path/ops/cron/archive.sh tier1 09
#     30 15 * * * /path/ops/cron/archive.sh tier1 00
#     # Tier-2 diagnostic decision — uses today 09 UTC, decides for today 06 UTC.
#     # MUST fire AFTER the 09-UTC tier1 line above (the trigger reads its GRIBs).
#     0  12 * * * /path/ops/cron/archive.sh trigger
#     # Tier-2 nightly bulk download — any fired decision still without
#     # a tier2 row. Runs when bandwidth is free.
#     0  23 * * * /path/ops/cron/archive.sh download-pending

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <tier1 HH | trigger | download-pending | backfill N | status>" >&2
    exit 2
fi

SUBCMD="$1"
shift

REPO_ROOT="${ALPTHERM_ICON_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
VENV="${ALPTHERM_ICON_VENV:-$REPO_ROOT/.venv}"
LOCK_DIR="${ALPTHERM_ICON_LOCK_DIR:-$REPO_ROOT/data/archive/locks}"
LOG_DIR="${ALPTHERM_ICON_LOG_DIR:-$REPO_ROOT/data/archive/logs}"

mkdir -p "$LOCK_DIR" "$LOG_DIR"

# Per-subcommand lock so tier1 / trigger / download-pending don't block
# each other (only same-subcommand overlap is suppressed).
LOCK_FILE="$LOCK_DIR/${SUBCMD}.lock"
LOG_FILE="$LOG_DIR/archive-$(date -u +%Y%m%d).log"

if [[ ! -f "$VENV/bin/activate" ]]; then
    echo "[$(date -u +%FT%TZ)] FATAL: no venv at $VENV" | tee -a "$LOG_FILE"
    exit 2
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "[$(date -u +%FT%TZ)] previous '$SUBCMD' still holds $LOCK_FILE — skipping" \
        | tee -a "$LOG_FILE"
    exit 0
fi

cd "$REPO_ROOT"

case "$SUBCMD" in
    tier1)
        if [[ $# -lt 1 ]]; then
            echo "[$(date -u +%FT%TZ)] FATAL: tier1 needs an init hour (00/03/06/09)" \
                | tee -a "$LOG_FILE"
            exit 2
        fi
        HH="$1"
        echo "[$(date -u +%FT%TZ)] tier1 init-hour=$HH starting" | tee -a "$LOG_FILE"
        python -m alptherm_icon.archive tier1 --init-hour "$HH" >> "$LOG_FILE" 2>&1
        ;;
    trigger)
        echo "[$(date -u +%FT%TZ)] trigger starting" | tee -a "$LOG_FILE"
        python -m alptherm_icon.archive trigger >> "$LOG_FILE" 2>&1
        ;;
    download-pending)
        echo "[$(date -u +%FT%TZ)] download-pending starting" | tee -a "$LOG_FILE"
        python -m alptherm_icon.archive download-pending >> "$LOG_FILE" 2>&1
        ;;
    backfill)
        DAYS="${1:-2}"
        echo "[$(date -u +%FT%TZ)] backfill days=$DAYS starting" | tee -a "$LOG_FILE"
        python -m alptherm_icon.archive backfill --days "$DAYS" >> "$LOG_FILE" 2>&1
        ;;
    status)
        python -m alptherm_icon.archive status
        ;;
    *)
        echo "[$(date -u +%FT%TZ)] FATAL: unknown subcommand '$SUBCMD'" \
            | tee -a "$LOG_FILE"
        exit 2
        ;;
esac

rc=$?
echo "[$(date -u +%FT%TZ)] '$SUBCMD' finished rc=$rc" | tee -a "$LOG_FILE"
exit "$rc"
