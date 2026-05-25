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


def config_from_env() -> AlerterConfig:
    """Build a config from environment variables for cron-friendly use.

    Supported variables:

    - ``ALPTHERM_NTFY_URL`` — full webhook URL (e.g. ``https://ntfy.sh/my-topic``)
    - ``ALPTHERM_ALERT_TITLE`` — overrides the default title
    """
    return AlerterConfig(
        webhook_url=os.environ.get("ALPTHERM_NTFY_URL") or None,
        webhook_title=os.environ.get("ALPTHERM_ALERT_TITLE", "ALPTHERM-ICON M0"),
    )
