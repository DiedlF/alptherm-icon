"""Streamlit landing page — 1-Glance Übersicht (Plan §10.2 Ebene 1+2 zusammengefasst).

Entry point for ``streamlit run src/alptherm_icon/dashboard/app.py``.
The ``pages/`` sub-directory next to this file gets auto-mounted by
Streamlit as additional sidebar entries.
"""

from __future__ import annotations

import datetime as dt

import streamlit as st

from alptherm_icon.dashboard.data_loader import (
    load_alerts,
    load_heartbeats,
    load_manifest_summary,
    load_ogn_inventory,
    load_storage,
    project_root,
)

# ---------------------------------------------------------------------------
# Page config — keep it tight; Streamlit's wide layout gives the heatmap room.
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="ALPTHERM-ICON M0 Monitor",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="expanded",
)


# Short TTL — dashboard is meant for "glance at it now and again". 10 s strikes
# the balance between freshness and not hammering the filesystem on reload.
@st.cache_data(ttl=10)
def _summary(root_str: str):
    root = project_root()
    return {
        "heartbeats": load_heartbeats(root),
        "alerts": load_alerts(root),
        "manifest": load_manifest_summary(root),
        "storage": load_storage(root),
        "ogn": load_ogn_inventory(root),
        "loaded_at": dt.datetime.now(dt.timezone.utc),
    }


root = project_root()
data = _summary(str(root))

# ---------------------------------------------------------------------------
# Header — title + refresh meta
# ---------------------------------------------------------------------------
title_col, refresh_col = st.columns([3, 1])
with title_col:
    st.title("📡 ALPTHERM-ICON M0 Monitor")
    st.caption(
        f"Project root: `{root}` · "
        f"Loaded {data['loaded_at']:%Y-%m-%d %H:%M:%S} UTC"
    )
with refresh_col:
    if st.button("↻ Refresh", use_container_width=True):
        _summary.clear()
        st.rerun()


# ---------------------------------------------------------------------------
# Top metrics — alert status, OGN, last tier-1, storage
# ---------------------------------------------------------------------------
st.subheader("Status auf einen Blick")

alerts = data["alerts"].alerts
heartbeats = {hb.job: hb for hb in data["heartbeats"]}
manifest = data["manifest"]
storage = data["storage"]
ogn = data["ogn"]

# Last tier-1 success: take the latest last_success across the four tier1 jobs.
tier1_jobs = [v for k, v in heartbeats.items() if k.startswith("tier1-")]
tier1_last_ok = max(
    (h.last_success_utc for h in tier1_jobs if h.last_success_utc),
    default=None,
)
ogn_hb = heartbeats.get("ogn-stream")

m1, m2, m3, m4 = st.columns(4)

with m1:
    if not alerts:
        st.metric("Alerts", "0", delta="ok", delta_color="off")
    else:
        st.metric("Alerts", str(len(alerts)), delta="kritisch", delta_color="inverse")

with m2:
    if ogn_hb is None:
        st.metric("OGN-Stream", "—", delta="kein Heartbeat", delta_color="inverse")
    else:
        ogn_today = next(
            (s for s in ogn if s.day == dt.datetime.now(dt.timezone.utc).date()),
            None,
        )
        size = f"{ogn_today.bytes_on_disk / 1e6:.1f} MB" if ogn_today else "0 B"
        delta_text = ogn_hb.last_status
        delta_color = "normal" if ogn_hb.last_status == "ok" else "inverse"
        st.metric("OGN heute", size, delta=delta_text, delta_color=delta_color)

with m3:
    if tier1_last_ok:
        st.metric("Letzter Tier-1 OK", tier1_last_ok, delta=None)
    else:
        st.metric("Letzter Tier-1 OK", "—", delta="noch nichts", delta_color="off")

with m4:
    free_gb = storage.disk_free_bytes / 1e9
    archive_gb = storage.archive_bytes / 1e9
    st.metric(
        f"Disk frei",
        f"{free_gb:.1f} GB",
        delta=f"archive: {archive_gb:.2f} GB",
        delta_color="off",
    )


# ---------------------------------------------------------------------------
# Alerts block — anything fired? Show full list, no truncation
# ---------------------------------------------------------------------------
if alerts:
    st.subheader("⚠ Offene Alerts")
    for a in alerts:
        kind_color = {
            "missing": "🟡",
            "stale": "🔴",
            "stuck-fail": "🟠",
        }.get(a.kind, "⚪")
        st.error(f"{kind_color} **{a.job}** — {a.kind}: {a.detail}")


# ---------------------------------------------------------------------------
# Heartbeats — single sortable table, color-coded
# ---------------------------------------------------------------------------
st.subheader("Heartbeats")
if data["heartbeats"]:
    rows = []
    for hb in data["heartbeats"]:
        last_attempt = hb.last_attempt_utc
        # Age in minutes for sorting / highlighting
        try:
            last_dt = dt.datetime.strptime(
                last_attempt, "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=dt.timezone.utc)
            age_min = (dt.datetime.now(dt.timezone.utc) - last_dt).total_seconds() / 60
        except Exception:  # noqa: BLE001
            age_min = float("nan")
        rows.append(
            {
                "Job": hb.job,
                "Status": hb.last_status,
                "Letzter Versuch (UTC)": hb.last_attempt_utc,
                "Letzter OK (UTC)": hb.last_success_utc or "—",
                "Alter (min)": round(age_min, 1) if age_min == age_min else None,
                "Seit": hb.since_utc,
            }
        )
    st.dataframe(rows, use_container_width=True, hide_index=True)
else:
    st.info("Noch keine Heartbeats geschrieben. Erster Cron-Run nach dem Heartbeat-Patch wird sie anlegen.")


# ---------------------------------------------------------------------------
# Manifest summary — quick view; full inventory is on the Bestand page
# ---------------------------------------------------------------------------
st.subheader("Archiv-Bestand")
if manifest.has_data:
    sm1, sm2, sm3, sm4 = st.columns(4)
    sm1.metric("Records gesamt", str(len(manifest.rows)))
    sm2.metric("Tier-1", str(manifest.by_tier.get("tier1", 0)))
    sm3.metric("Tier-2 fired", str(manifest.fired_decisions))
    sm4.metric("Pending Downloads", str(manifest.pending_downloads))
    st.caption(
        f"Gesamtgröße auf Disk: {manifest.total_bytes / 1e9:.2f} GB "
        f"(Details auf der **Bestand**-Seite)"
    )
else:
    st.info("Manifest ist leer — noch kein erfolgreicher Tier-1-Lauf.")


st.divider()
st.caption(
    "Reine Lese-Sicht über die Sammel-Jobs. Schreibt nie, triggert nie. "
    "Plan §10.1."
)
