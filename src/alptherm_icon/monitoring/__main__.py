"""CLI for the monitoring layer (Plan §10).

Two subcommands::

    # Show all heartbeats (one line per job, sorted)
    python -m alptherm_icon.monitoring status

    # Run the alerter once — fires alerts for stale / missing / stuck-fail
    # heartbeats. Intended for a 15-min cron tick.
    python -m alptherm_icon.monitoring alert [--dry-run]

The webhook target is taken from the environment so secrets stay out
of the crontab (``ALPTHERM_NTFY_URL``).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from alptherm_icon.monitoring import heartbeat as hb_mod
from alptherm_icon.monitoring.alerter import (
    AlerterConfig,
    check,
    config_from_env,
    deliver,
)


def _project_root() -> Path:
    here = Path.cwd().resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "pyproject.toml").exists():
            return candidate
    raise RuntimeError(f"no pyproject.toml at or above {here}")


def cmd_status(args: argparse.Namespace) -> int:
    root = _project_root()
    rows = hb_mod.read_all(root)
    if not rows:
        print(f"(no heartbeats under {root / 'data' / 'status'})")
        return 0
    print(f"{len(rows)} job(s) (alphabetical):")
    for hb in rows:
        ok = hb.last_success_utc or "—"
        print(
            f"  {hb.job:<18s} status={hb.last_status:<5s} "
            f"last_attempt={hb.last_attempt_utc}  last_ok={ok}  "
            f"since={hb.since_utc}"
        )
    return 0


def cmd_alert(args: argparse.Namespace) -> int:
    root = _project_root()
    config = config_from_env(root)
    if args.dry_run:
        config.webhook_url = None
    if args.test:
        # Skip the heartbeat check; send a canned alert so the operator
        # can confirm the webhook end-to-end.
        from alptherm_icon.monitoring.alerter import deliver_test

        ok = deliver_test(config)
        if not config.webhook_url:
            print("WARNING: no webhook configured — test message only printed locally.")
            print("Set ALPTHERM_NTFY_URL in data/monitoring.env to enable POST.")
        return 0 if ok else 2
    alerts = check(root, config)
    if not alerts:
        print("ok — no alerts")
        return 0
    ok = deliver(alerts, config)
    return 0 if ok else 2


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    parser = argparse.ArgumentParser(prog="python -m alptherm_icon.monitoring")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="show heartbeat overview")
    p_status.set_defaults(func=cmd_status)

    p_alert = sub.add_parser("alert", help="check thresholds and emit alerts")
    p_alert.add_argument(
        "--dry-run",
        action="store_true",
        help="evaluate alerts but never POST to the webhook",
    )
    p_alert.add_argument(
        "--test",
        action="store_true",
        help="send a canned test alert to verify the webhook is wired up",
    )
    p_alert.set_defaults(func=cmd_alert)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
