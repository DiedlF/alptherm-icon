"""CLI for the M0 archive cronjob (plan §9).

Production flow — one command per cronjob::

    # Four times a day, one per ICON-D2 anchor we collect:
    python -m alptherm_icon.archive tier1 --init-hour 03
    python -m alptherm_icon.archive tier1 --init-hour 06
    python -m alptherm_icon.archive tier1 --init-hour 09
    python -m alptherm_icon.archive tier1 --init-hour 00

    # Once a day at ~12 UTC: diagnostic gut-day decision
    python -m alptherm_icon.archive trigger

    # Once a day at night: download every fired tier2_decision
    python -m alptherm_icon.archive download-pending

Convenience commands::

    python -m alptherm_icon.archive backfill --days 2      # 4 anchors/day
    python -m alptherm_icon.archive tier1 --init 2026052303  # explicit init
    python -m alptherm_icon.archive status                  # manifest summary
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path

from alptherm_icon import monitoring
from alptherm_icon.archive import manifest
from alptherm_icon.archive.archiver import (
    ArchiveRoot,
    archive_tier1,
    decide_tier2,
    download_pending_tier2,
)

ICON_D2_ANCHOR_HOURS: tuple[int, ...] = (0, 3, 6, 9)
DWD_PUBLISH_LATENCY = dt.timedelta(hours=2, minutes=30)


def _project_root() -> Path:
    here = Path.cwd().resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "pyproject.toml").exists():
            return candidate
    raise RuntimeError(f"no pyproject.toml at or above {here}")


def _parse_init(s: str) -> dt.datetime:
    """Parse YYYYMMDDHH into a UTC datetime."""
    return dt.datetime.strptime(s, "%Y%m%d%H").replace(tzinfo=dt.timezone.utc)


def _most_recent_available_init(
    hour: int, now: dt.datetime | None = None
) -> dt.datetime:
    """Most recent UTC date at which init hour ``hour`` should be on opendata.

    DWD takes ~2.5–3 h to publish a run, so we pick yesterday's slot when
    the current slot isn't ready yet.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if now < candidate + DWD_PUBLISH_LATENCY:
        candidate -= dt.timedelta(days=1)
    return candidate


def _resolve_init(args: argparse.Namespace) -> dt.datetime:
    """Resolve ``--init`` / ``--init-hour`` into one explicit init datetime."""
    if args.init:
        return _parse_init(args.init)
    if args.init_hour is not None:
        return _most_recent_available_init(args.init_hour)
    raise SystemExit("ERROR: provide --init YYYYMMDDHH or --init-hour HH")


def _today_at(hour: int, now: dt.datetime | None = None) -> dt.datetime:
    """Today's UTC slot at ``hour:00`` — used by the trigger which always
    targets the *same day's* 06/09 UTC runs."""
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.replace(hour=hour, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_tier1(args: argparse.Namespace) -> int:
    root = _project_root()
    init = _resolve_init(args)
    print(f"tier1 run init={init:%Y-%m-%dT%H:%MZ}")
    record = archive_tier1(
        init=init,
        root=root,
        lead_max=args.lead_max,
        sleep_between_s=args.sleep,
        skip_zarr=args.skip_zarr,
        force=args.force,
    )
    if record is None:
        print("  (already recorded — use --force to re-download)")
        return 0
    print(
        f"  tier1: ok={record.files_ok}/{record.files_attempted} "
        f"404={record.files_404} err={record.files_error} "
        f"bytes={record.bytes_on_disk:,}"
    )
    return 0


def cmd_trigger(args: argparse.Namespace) -> int:
    root = _project_root()
    if args.decision_init:
        decision_init = _parse_init(args.decision_init)
    else:
        decision_init = _today_at(args.decision_hour)
    if args.target_init:
        target_init = _parse_init(args.target_init)
    else:
        target_init = _today_at(args.target_hour)
    print(
        f"tier2 trigger: decision={decision_init:%Y-%m-%dT%H:%MZ} "
        f"target={target_init:%Y-%m-%dT%H:%MZ}"
    )
    try:
        record = decide_tier2(
            decision_init=decision_init,
            target_init=target_init,
            root=root,
            force=args.force,
        )
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    if record is None:
        print("  (already decided — use --force to re-evaluate)")
        return 0
    trig = record.trigger or {}
    print(f"  fire={trig.get('fire')}  reason={trig.get('reason')}")
    return 0


def cmd_download_pending(args: argparse.Namespace) -> int:
    root = _project_root()
    written = download_pending_tier2(root=root, sleep_between_s=args.sleep)
    if not written:
        print("(nothing pending)")
        return 0
    for rec in written:
        print(
            f"  tier2 {rec.init_utc}: ok={rec.files_ok}/{rec.files_attempted} "
            f"404={rec.files_404} err={rec.files_error} "
            f"bytes={rec.bytes_on_disk:,}"
        )
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    """Catch up Tier 1 for the last ``--days`` × 4 anchor inits.

    Tier 2 backfill is not automated here — run ``trigger`` and
    ``download-pending`` explicitly for any date you want re-evaluated.
    """
    root = _project_root()
    today_anchors = [_most_recent_available_init(h) for h in ICON_D2_ANCHOR_HOURS]
    today_anchors.sort(reverse=True)  # newest first
    failures = 0
    for offset in range(args.days):
        for anchor in today_anchors:
            init = anchor - dt.timedelta(days=offset)
            print(f"--- backfill init={init:%Y-%m-%dT%H:%MZ} ---")
            try:
                archive_tier1(
                    init=init,
                    root=root,
                    lead_max=args.lead_max,
                    sleep_between_s=args.sleep,
                    skip_zarr=args.skip_zarr,
                    force=args.force,
                )
            except Exception as exc:  # noqa: BLE001 — keep trying others
                failures += 1
                print(f"  FAILED: {exc!r}", file=sys.stderr)
    return 1 if failures else 0


def cmd_status(args: argparse.Namespace) -> int:
    root = _project_root()
    paths = ArchiveRoot(root=root)
    rows = manifest.read_all(paths.manifest_path)
    if not rows:
        print(f"(no manifest at {paths.manifest_path})")
        return 0
    total_bytes = sum(r.get("bytes_on_disk", 0) for r in rows)
    by_tier: dict[str, int] = {}
    fired = 0
    for r in rows:
        by_tier[r["tier"]] = by_tier.get(r["tier"], 0) + 1
        if r["tier"] == "tier2_decision" and (r.get("trigger") or {}).get("fire"):
            fired += 1
    pending = manifest.pending_tier2_targets(paths.manifest_path)
    print(f"manifest: {paths.manifest_path}")
    print(f"records:  {len(rows)} ({by_tier})")
    print(f"tier2 fired: {fired}   pending downloads: {len(pending)}")
    print(f"on-disk:  {total_bytes:,} bytes ({total_bytes / 1e9:.2f} GB)")
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows[-10:]:
            extra = ""
            if r.get("trigger"):
                extra = f" trigger={r['trigger'].get('fire')}({r['trigger'].get('reason')})"
            if r.get("decision_init_utc"):
                extra += f" from={r['decision_init_utc']}"
            print(
                f"  {r['init_utc']} {r['tier']:<16s} "
                f"ok={r['files_ok']}/{r['files_attempted']} "
                f"bytes={r['bytes_on_disk']:,}{extra}"
            )
    if pending:
        print(f"\npending tier2 ({len(pending)}):")
        for p in pending:
            print(
                f"  target={p['init_utc']}  decided_from={p.get('decision_init_utc')}  "
                f"reason={(p.get('trigger') or {}).get('reason')}"
            )

    # Heartbeat overview — one line per job, sorted alphabetically.
    heartbeats = monitoring.read_all(root)
    if heartbeats:
        print(f"\nheartbeats ({len(heartbeats)} jobs):")
        for hb in heartbeats:
            success = hb.last_success_utc or "—"
            print(
                f"  {hb.job:<18s} status={hb.last_status:<5s} "
                f"last_attempt={hb.last_attempt_utc}  last_ok={success}  "
                f"since={hb.since_utc}"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    parser = argparse.ArgumentParser(prog="python -m alptherm_icon.archive")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def _io_opts(p: argparse.ArgumentParser) -> None:
        p.add_argument("--lead-max", type=int, default=48)
        p.add_argument("--sleep", type=float, default=0.1)
        p.add_argument("--skip-zarr", action="store_true")
        p.add_argument("--force", action="store_true")

    # tier1 ---------------------------------------------------------------
    p_t1 = sub.add_parser("tier1", help="archive Tier 1 for one ICON-D2 run")
    grp = p_t1.add_mutually_exclusive_group(required=True)
    grp.add_argument(
        "--init-hour",
        type=int,
        choices=ICON_D2_ANCHOR_HOURS,
        help="most-recent available init at this hour (00/03/06/09 UTC)",
    )
    grp.add_argument("--init", help="explicit init YYYYMMDDHH")
    _io_opts(p_t1)
    p_t1.set_defaults(func=cmd_tier1)

    # trigger -------------------------------------------------------------
    p_tr = sub.add_parser("trigger", help="evaluate the diagnostic gut-day trigger")
    p_tr.add_argument(
        "--decision-hour",
        type=int,
        default=9,
        help="today's UTC hour whose Tier-1 GRIBs evaluate the trigger (default: 9)",
    )
    p_tr.add_argument(
        "--target-hour",
        type=int,
        default=6,
        help="today's UTC hour whose Tier-2 profile will be downloaded if fired (default: 6)",
    )
    p_tr.add_argument("--decision-init", help="explicit decision init YYYYMMDDHH")
    p_tr.add_argument("--target-init", help="explicit target init YYYYMMDDHH")
    p_tr.add_argument("--force", action="store_true")
    p_tr.set_defaults(func=cmd_trigger)

    # download-pending ----------------------------------------------------
    p_dp = sub.add_parser(
        "download-pending",
        help="download Tier 2 for every fired tier2_decision still without a tier2 row",
    )
    p_dp.add_argument("--sleep", type=float, default=0.1)
    p_dp.set_defaults(func=cmd_download_pending)

    # backfill ------------------------------------------------------------
    p_bf = sub.add_parser(
        "backfill",
        help="catch up Tier 1 for all four daily anchors over the last N days",
    )
    p_bf.add_argument("--days", type=int, default=2)
    _io_opts(p_bf)
    p_bf.set_defaults(func=cmd_backfill)

    # status --------------------------------------------------------------
    p_st = sub.add_parser("status", help="summarize the manifest")
    p_st.add_argument("--json", action="store_true")
    p_st.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
