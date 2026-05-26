"""Bestand page (Plan §10.2 Ebene 2) — was habe ich gesammelt?

Kumulative Archiv-Sicht:
- Kalender-Heatmap: pro Tag & Init-Hour, ob ein Tier-1-Lauf vollständig /
  lückenhaft / fehlend ist (Lücken springen sofort ins Auge).
- Tier-2-Historie: getriggerte Tage, Fire-Reasons, korreliert mit der
  reinen Trigger-Rate über die Saison.
- Speicherverbrauch + Wachstumsrate.
- OGN-Tagesvolumen.
"""

from __future__ import annotations

import datetime as dt

import altair as alt
import pandas as pd
import streamlit as st

from alptherm_icon.dashboard.data_loader import (
    load_manifest_summary,
    load_ogn_inventory,
    load_storage,
    project_root,
)

st.set_page_config(page_title="Bestand", page_icon="📦", layout="wide")
st.title("📦 Bestand — was habe ich gesammelt?")
st.caption("Plan §10.2 Ebene 2 — kumulative Archiv-Sicht.")


@st.cache_data(ttl=10)
def _load(_root_str: str):
    root = project_root()
    return {
        "manifest": load_manifest_summary(root),
        "storage": load_storage(root),
        "ogn": load_ogn_inventory(root),
    }


root = project_root()
data = _load(str(root))
manifest = data["manifest"]
storage = data["storage"]
ogn = data["ogn"]


# ---------------------------------------------------------------------------
# Speicher-Übersicht
# ---------------------------------------------------------------------------
st.subheader("Speicher")
sc1, sc2, sc3, sc4 = st.columns(4)
sc1.metric("Tier-1 GRIBs", f"{storage.grib_bytes / 1e9:.2f} GB")
sc2.metric("Zarr", f"{storage.zarr_bytes / 1e9:.2f} GB")
sc3.metric("OGN raw", f"{storage.ogn_bytes / 1e9:.2f} GB")
sc4.metric(
    "Disk frei",
    f"{storage.disk_free_bytes / 1e9:.1f} GB",
    delta=f"von {storage.disk_total_bytes / 1e9:.0f} GB",
    delta_color="off",
)


# ---------------------------------------------------------------------------
# Tier-1 calendar heatmap — (date × init-hour) → completeness
# ---------------------------------------------------------------------------
st.subheader("Tier-1 Kalender")
st.caption(
    "Pro (Datum, Init-Hour): Vollständigkeit als ok-Anteil von files_attempted. "
    "Dunkelgrün = vollständig, gelb = teilweise, rot = leer (z.B. außerhalb DWD-Fenster)."
)

if manifest.has_data:
    tier1_rows = [r for r in manifest.rows if r["tier"] == "tier1"]
    if tier1_rows:
        df = pd.DataFrame(
            [
                {
                    "date": r["init_utc"][:10],
                    "init_hour": int(r["init_utc"][11:13]),
                    "ok_frac": (
                        r["files_ok"] / r["files_attempted"]
                        if r["files_attempted"]
                        else 0.0
                    ),
                    "files_ok": r["files_ok"],
                    "files_attempted": r["files_attempted"],
                    "bytes_on_disk": r["bytes_on_disk"],
                }
                for r in tier1_rows
            ]
        )
        chart = (
            alt.Chart(df)
            .mark_rect()
            .encode(
                x=alt.X("date:O", title="Datum (UTC)", sort="ascending"),
                y=alt.Y(
                    "init_hour:O",
                    title="Init-Hour",
                    sort=alt.SortField("init_hour"),
                ),
                color=alt.Color(
                    "ok_frac:Q",
                    title="ok-Anteil",
                    scale=alt.Scale(domain=[0, 1], range=["#cc4444", "#44aa44"]),
                ),
                tooltip=[
                    alt.Tooltip("date:O", title="Datum"),
                    alt.Tooltip("init_hour:O", title="Init UTC"),
                    alt.Tooltip("ok_frac:Q", title="ok-Anteil", format=".0%"),
                    alt.Tooltip("files_ok:Q", title="files_ok"),
                    alt.Tooltip("files_attempted:Q", title="attempted"),
                    alt.Tooltip("bytes_on_disk:Q", title="bytes", format=","),
                ],
            )
            .properties(height=200)
        )
        st.altair_chart(chart, use_container_width=True)
    else:
        st.info("Noch keine Tier-1-Rows im Manifest.")
else:
    st.info("Manifest ist leer.")


# ---------------------------------------------------------------------------
# Tier-2 Trigger-Historie
# ---------------------------------------------------------------------------
st.subheader("Tier-2 Trigger-Historie")

if manifest.has_data:
    t2_rows = [r for r in manifest.rows if r["tier"] == "tier2_decision"]
    if t2_rows:
        df = pd.DataFrame(
            [
                {
                    "target_init": r["init_utc"],
                    "decision_init": r.get("decision_init_utc") or "—",
                    "fire": (r.get("trigger") or {}).get("fire", False),
                    "reason": (r.get("trigger") or {}).get("reason", "—"),
                    "cape_max": (r.get("trigger") or {})
                    .get("metrics", {})
                    .get("cape_max"),
                    "rad_max": (r.get("trigger") or {})
                    .get("metrics", {})
                    .get("rad_max"),
                    "htop_dc_max_m": (r.get("trigger") or {})
                    .get("metrics", {})
                    .get("htop_dc_max_m"),
                    "precip_window_mm": (r.get("trigger") or {})
                    .get("metrics", {})
                    .get("precip_window_mm"),
                }
                for r in t2_rows
            ]
        )
        df = df.sort_values("target_init", ascending=False)
        st.dataframe(df, use_container_width=True, hide_index=True)

        # Plot CAPE / Rad over time for the fired decisions
        fire_df = df[df["fire"]].copy()
        if not fire_df.empty:
            st.markdown("**Trigger-Metriken über die Tage (fired-only):**")
            fire_long = fire_df.melt(
                id_vars=["target_init"],
                value_vars=["cape_max", "rad_max", "htop_dc_max_m"],
                var_name="metric",
                value_name="value",
            )
            chart = (
                alt.Chart(fire_long)
                .mark_line(point=True)
                .encode(
                    x=alt.X("target_init:T", title="Target Init (UTC)"),
                    y=alt.Y("value:Q", title="Wert"),
                    color=alt.Color("metric:N", title="Metrik"),
                    tooltip=["target_init", "metric", "value"],
                )
                .properties(height=240)
            )
            st.altair_chart(chart, use_container_width=True)
    else:
        st.info("Noch kein Tier-2-Decision-Record. Wird heute um 12 UTC geschrieben.")
else:
    st.info("Manifest ist leer.")


# ---------------------------------------------------------------------------
# OGN Tagesvolumen
# ---------------------------------------------------------------------------
st.subheader("OGN Tagesvolumen")
if ogn:
    df = pd.DataFrame(
        [{"day": s.day.isoformat(), "MB": s.bytes_on_disk / 1e6} for s in ogn]
    )
    chart = (
        alt.Chart(df)
        .mark_bar()
        .encode(
            x=alt.X("day:O", title="Tag", sort="ascending"),
            y=alt.Y("MB:Q", title="komprimiert (MB)"),
            tooltip=["day", alt.Tooltip("MB:Q", format=".1f")],
        )
        .properties(height=200)
    )
    st.altair_chart(chart, use_container_width=True)
    total_gb = sum(s.bytes_on_disk for s in ogn) / 1e9
    st.caption(f"Gesamt: {total_gb:.3f} GB über {len(ogn)} Tag(e)")
else:
    st.info("Noch keine OGN-Tagesfiles. Der Daemon schreibt das aktuelle Tagesfile fortlaufend.")
