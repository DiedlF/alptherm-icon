"""Heartbeat-based alerter (Plan §10.4).

Reads ``data/status/*.json`` and emits an alert for each job whose
last attempt is older than its configured staleness threshold, or
which is *expected* but has never produced a heartbeat at all.

Alert delivery is pluggable via webhook URL — by default ntfy.sh-style
plain-text POSTs (the same simple wire format also works for
self-hosted ntfy, Telegram bot endpoints, and most chat webhooks).
Without a webhook the alerter prints to stdout so the cron log
still captures the gap.

Designed to be invoked from cron every 15 min. Stateless — the only
persistent state is the heartbeat files themselves.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from alptherm_icon.monitoring.heartbeat import HeartbeatStatus, read_all

log = logging.getLogger(__name__)

# Default staleness budgets per job (in seconds). Cron jobs that run
# once a day get ~26 h to allow for restart delays / DWD outages; the
# OGN stream which emits keepalives every ~240 s gets a much tighter
# 30 min budget so a silent socket trips alarm quickly.
DEFAULT_THRESHOLDS_S: dict[str, int] = {
    "tier1-00": 26 * 3600,
    "tier1-03": 26 * 3600,
    "tier1-06": 26 * 3600,
    "tier1-09": 26 * 3600,
    "tier2-decision": 26 * 3600,
    "tier2-download": 26 * 3600,
    "ogn-stream": 30 * 60,
}


@dataclass
class Alert:
    """One staleness or missing-heartbeat alert."""

    job: str
    kind: str  # "missing" | "stale" | "stuck-fail"
    detail: str
    last_attempt_utc: str | None = None
    last_success_utc: str | None = None
    age_seconds: float | None = None

    def to_text(self) -> str:
        bits = [f"[{self.kind.upper()}] {self.job}", self.detail]
        if self.age_seconds is not None:
            bits.append(f"age={self.age_seconds / 60:.1f} min")
        if self.last_success_utc:
            bits.append(f"last_ok={self.last_success_utc}")
        return " — ".join(bits)


@dataclass
class AlerterConfig:
    """Knobs for one alerter run. Sensible defaults built in."""

    thresholds_s: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS_S))
    webhook_url: str | None = None
    webhook_title: str = "ALPTHERM-ICON M0"
    fail_streak_alert_s: int = 6 * 3600
    """A job stuck in 'fail' status for longer than this also raises an alert,
    even if its last_attempt is fresh — distinguishes "tier1 silently 404-ing
    every day" from "tier1 hasn't run at all"."""


def _parse_iso(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def check(
    root: Path,
    config: AlerterConfig | None = None,
    now: dt.datetime | None = None,
) -> list[Alert]:
    """Walk the heartbeats and return all current alerts.

    Three alert kinds:

    - ``missing`` — job is in ``thresholds_s`` but has no heartbeat file.
    - ``stale`` — heartbeat exists but ``last_attempt_utc`` is too old.
    - ``stuck-fail`` — heartbeat is fresh but the job has been in
      ``fail`` status for longer than ``fail_streak_alert_s``.
    """
    config = config or AlerterConfig()
    now = now or dt.datetime.now(dt.timezone.utc)

    known: dict[str, HeartbeatStatus] = {hb.job: hb for hb in read_all(root)}
    alerts: list[Alert] = []

    for job, threshold_s in config.thresholds_s.items():
        hb = known.get(job)
        if hb is None:
            alerts.append(
                Alert(
                    job=job,
                    kind="missing",
                    detail=f"no heartbeat file under data/status/ (job never ran?)",
                )
            )
            continue
        last_attempt = _parse_iso(hb.last_attempt_utc)
        age = (now - last_attempt).total_seconds()
        if age > threshold_s:
            alerts.append(
                Alert(
                    job=job,
                    kind="stale",
                    detail=(
                        f"last attempt {age / 3600:.1f} h ago, "
                        f"threshold {threshold_s / 3600:.1f} h"
                    ),
                    last_attempt_utc=hb.last_attempt_utc,
                    last_success_utc=hb.last_success_utc,
                    age_seconds=age,
                )
            )
            continue
        # Fresh heartbeat — but check if it's stuck in 'fail'.
        if hb.last_status == "fail":
            since = _parse_iso(hb.since_utc)
            stuck_for = (now - since).total_seconds()
            if stuck_for > config.fail_streak_alert_s:
                alerts.append(
                    Alert(
                        job=job,
                        kind="stuck-fail",
                        detail=(
                            f"status=fail for {stuck_for / 3600:.1f} h "
                            f"(streak start {hb.since_utc})"
                        ),
                        last_attempt_utc=hb.last_attempt_utc,
                        last_success_utc=hb.last_success_utc,
                        age_seconds=stuck_for,
                    )
                )

    return alerts


def deliver(alerts: list[Alert], config: AlerterConfig) -> bool:
    """Send the alerts. Returns True iff delivery succeeded (or nothing to send).

    Print-only when no webhook is configured (cron captures it via 2>&1).
    POSTs plain-text body when ``config.webhook_url`` is set — the wire
    format works for ntfy.sh and self-hosted ntfy out of the box; other
    receivers can wrap their own URLs.
    """
    if not alerts:
        return True
    body = "\n".join(a.to_text() for a in alerts)
    print(f"--- {len(alerts)} alert(s) ---")
    print(body)

    if not config.webhook_url:
        return True  # local-only delivery counts as success

    req = urllib.request.Request(
        config.webhook_url,
        data=body.encode("utf-8"),
        headers={
            "Title": config.webhook_title,
            "Priority": "high",
            "Tags": "warning,alptherm",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if 200 <= resp.status < 300:
                return True
            log.warning("alerter: webhook %s returned %d", config.webhook_url, resp.status)
            return False
    except urllib.error.URLError as exc:
        log.error("alerter: webhook delivery failed: %r", exc)
        return False


def _load_env_file(path: Path) -> dict[str, str]:
    """Read a tiny ``KEY=VALUE``-per-line env file. No quoting, no
    expansion — keeps secrets out of the crontab without pulling
    python-dotenv as a dependency.
    """
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            out[key] = value
    return out


def config_from_env(root: Path | None = None) -> AlerterConfig:
    """Build a config, preferring values from ``data/monitoring.env``
    over process-level environment variables. The file format is a
    simple ``KEY=VALUE`` per line (see ``data/monitoring.env.example``).

    Supported keys:

    - ``ALPTHERM_NTFY_URL`` — full webhook URL (e.g. ``https://ntfy.sh/topic``)
    - ``ALPTHERM_ALERT_TITLE`` — overrides the default title

    Resolution order: env file > process env. Keeps secrets out of the
    crontab while still letting one-shot CLI invocations override.
    """
    file_env: dict[str, str] = {}
    if root is not None:
        file_env = _load_env_file(root / "data" / "monitoring.env")

    def _resolve(key: str, default: str | None = None) -> str | None:
        if key in file_env:
            return file_env[key]
        return os.environ.get(key, default)

    return AlerterConfig(
        webhook_url=_resolve("ALPTHERM_NTFY_URL") or None,
        webhook_title=_resolve("ALPTHERM_ALERT_TITLE", "ALPTHERM-ICON M0") or "ALPTHERM-ICON M0",
    )


def deliver_test(config: AlerterConfig) -> bool:
    """Send a single canned alert so the user can verify wiring.

    Useful right after setting up ``data/monitoring.env`` — does not
    consult the heartbeats at all.
    """
    now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return deliver(
        alerts=[
            Alert(
                job="alerter",
                kind="test",
                detail=f"webhook reachable at {now_iso}",
            )
        ],
        config=config,
    )
