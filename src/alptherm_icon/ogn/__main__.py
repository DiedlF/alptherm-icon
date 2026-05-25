"""CLI for the OGN APRS-Stream daemon (plan §6.6 + §9.5).

Subcommands::

    # Start the long-running daemon (intended target: systemd unit)
    python -m alptherm_icon.ogn run

    # Inspect the rolling raw log + heartbeat
    python -m alptherm_icon.ogn status

    # Tail the live stream to stdout for ~N seconds without writing
    # (sanity-check connectivity + filter)
    python -m alptherm_icon.ogn probe --seconds 30
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import time
from pathlib import Path

from ogn.client import AprsClient

from alptherm_icon import monitoring
from alptherm_icon.archive.bbox import ALPEN_BBOX
from alptherm_icon.ogn.daemon import GeoFilter, OgnDaemon
from alptherm_icon.ogn.writer import raw_log_path


def _project_root() -> Path:
    here = Path.cwd().resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "pyproject.toml").exists():
            return candidate
    raise RuntimeError(f"no pyproject.toml at or above {here}")


def cmd_run(args: argparse.Namespace) -> int:
    root = _project_root()
    daemon = OgnDaemon(root=root)
    daemon.run()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    root = _project_root()
    today = dt.datetime.now(dt.timezone.utc).date()
    path = raw_log_path(root, today)
    print(f"today's raw log: {path}")
    if path.exists():
        size = path.stat().st_size
        print(f"  size: {size:,} bytes ({size / 1e6:.1f} MB compressed)")
    else:
        print("  (no file yet today)")
    # Recent days
    raw_dir = root / "data" / "ogn" / "raw"
    if raw_dir.exists():
        files = sorted(raw_dir.rglob("*.jsonl.gz"))
        if files:
            print(f"\narchive: {len(files)} day-files, total bytes:")
            total = 0
            for f in files[-7:]:
                s = f.stat().st_size
                total += s
                print(f"  {f.relative_to(root)}  {s:>10,} B")
            print(f"  (last-7 sum: {sum(f.stat().st_size for f in files[-7:]):,} B)")

    # Heartbeat
    try:
        hb = monitoring.read(root, "ogn-stream")
    except FileNotFoundError:
        print("\nheartbeat: (not yet written — daemon never ran?)")
        return 0
    print(f"\nheartbeat ogn-stream:")
    print(f"  status:        {hb.last_status}")
    print(f"  last_attempt:  {hb.last_attempt_utc}")
    print(f"  last_success:  {hb.last_success_utc or '—'}")
    print(f"  since:         {hb.since_utc}")
    for k, v in (hb.last_extra or {}).items():
        print(f"  {k}: {v}")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """Connect, log to stdout, disconnect after N seconds. No file output."""
    geofilter = GeoFilter.for_bbox(ALPEN_BBOX)
    print(f"connecting with filter {geofilter.to_aprs()} for {args.seconds}s…")
    client = AprsClient(aprs_user="N0CALL", aprs_filter=geofilter.to_aprs())
    client.connect(retries=3, wait_period=5)
    deadline = time.time() + args.seconds
    n = 0

    def on_line(line: str) -> None:
        nonlocal n
        n += 1
        if args.verbose or n <= 5:
            print(line)
        if time.time() > deadline:
            client.disconnect()

    def on_keepalive(client_: AprsClient) -> None:
        if time.time() > deadline:
            client_.disconnect()

    try:
        client.run(callback=on_line, timed_callback=on_keepalive, autoreconnect=False)
    except Exception as exc:  # noqa: BLE001
        print(f"probe ended: {exc!r}", file=sys.stderr)
    print(f"\nprobe complete: received {n} packets in {args.seconds}s")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    parser = argparse.ArgumentParser(prog="python -m alptherm_icon.ogn")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="start the long-running OGN daemon")
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="show heartbeat + raw-log inventory")
    p_status.set_defaults(func=cmd_status)

    p_probe = sub.add_parser(
        "probe",
        help="tail the stream to stdout for N seconds without writing files",
    )
    p_probe.add_argument("--seconds", type=int, default=15)
    p_probe.add_argument("--verbose", action="store_true", help="print every packet")
    p_probe.set_defaults(func=cmd_probe)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
