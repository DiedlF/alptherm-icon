"""Liveness page (Plan §10.2 Ebene 1) — pro Job: läuft die Maschinerie?

Detail-View über die Heartbeat-Schicht, deutlich detaillierter als die
Mini-Tabelle auf der Landing-Page:

- Erwartete Frequenz pro Job (aus den Alerter-Thresholds)
- Aktueller Status mit Ampel-Coding
- ``last_extra``-Payload aufgeklappt (Job-spezifische Zahlen wie
  files_ok, trigger reason, packets_total)
"""

from __future__ import annotations

import datetime as dt

import streamlit as st

from alptherm_icon.dashboard.data_loader import (
    load_alerts,
    load_heartbeats,
    project_root,
)
from alptherm_icon.monitoring.alerter import DEFAULT_THRESHOLDS_S

st.set_page_config(page_title="Liveness", page_icon="🫀", layout="wide")
st.title("🫀 Liveness — läuft die Maschinerie?")
st.caption("Plan §10.2 Ebene 1 — die kritischste Ebene: hier droht Datenverlust.")


@st.cache_data(ttl=10)
def _load(_root_str: str):
    root = project_root()
    return {
        "heartbeats": load_heartbeats(root),
        "alerts": load_alerts(root),
        "loaded_at": dt.datetime.now(dt.timezone.utc),
    }


root = project_root()
data = _load(str(root))

if not data["heartbeats"]:
    st.info(
        "Noch keine Heartbeats. Der erste Cron-Run pro Job schreibt einen — "
        "Liveness wird sich automatisch füllen."
    )

heartbeats = {hb.job: hb for hb in data["heartbeats"]}
alert_by_job = {}
for a in data["alerts"].alerts:
    alert_by_job.setdefault(a.job, []).append(a)


def _format_threshold(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds / 60:.0f} min"
    if seconds < 24 * 3600:
        return f"{seconds / 3600:.0f} h"
    return f"{seconds / 86400:.1f} d"


def _status_badge(status: str, has_alert: bool) -> str:
    if has_alert:
        return "🔴 ALERT"
    return {
        "ok": "🟢 OK",
        "skip": "⚪ SKIP",
        "fail": "🟠 FAIL",
        "crash": "🔴 CRASH",
    }.get(status, f"⚪ {status}")


# Iterate over the *expected* job set, not just present heartbeats — so the
# absence of a job is visible (the "missing" alert reinforces this).
expected = sorted(DEFAULT_THRESHOLDS_S.keys())
for job in expected:
    hb = heartbeats.get(job)
    job_alerts = alert_by_job.get(job, [])
    has_alert = bool(job_alerts)
    threshold_label = _format_threshold(DEFAULT_THRESHOLDS_S[job])

    with st.container(border=True):
        cols = st.columns([2, 1, 2, 2])
        if hb is None:
            cols[0].markdown(f"**{job}** &nbsp; 🟡 MISSING")
        else:
            cols[0].markdown(
                f"**{job}** &nbsp; {_status_badge(hb.last_status, has_alert)}"
            )
        cols[1].markdown(f"Erwartet alle: `{threshold_label}`")
        if hb is not None:
            cols[2].markdown(
                f"**Letzter Versuch**  \n`{hb.last_attempt_utc}`"
            )
            cols[3].markdown(
                f"**Letzter OK**  \n`{hb.last_success_utc or '—'}`"
            )
        else:
            cols[2].markdown("Letzter Versuch  \n_(keiner)_")
            cols[3].markdown("Letzter OK  \n_(keiner)_")

        if has_alert:
            for a in job_alerts:
                st.warning(f"{a.kind}: {a.detail}")

        if hb is not None and hb.last_extra:
            with st.expander("Details (last_extra)", expanded=False):
                st.json(hb.last_extra)


# ---------------------------------------------------------------------------
# Unexpected jobs — anything heartbeat-wise NOT in DEFAULT_THRESHOLDS_S
# (e.g. when we add new jobs without bumping the alerter config).
# ---------------------------------------------------------------------------
unexpected = sorted(set(heartbeats) - set(DEFAULT_THRESHOLDS_S))
if unexpected:
    st.divider()
    st.subheader("Unbekannte Jobs (kein Threshold konfiguriert)")
    st.caption(
        "Diese Jobs schreiben Heartbeats, sind aber im Alerter nicht eingetragen — "
        "verlieren also kein Monitoring-Auge. `monitoring/alerter.py` ergänzen."
    )
    for job in unexpected:
        hb = heartbeats[job]
        st.text(f"{job}  status={hb.last_status}  last_attempt={hb.last_attempt_utc}")
