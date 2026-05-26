"""Inhalt page (Plan §10.2 Ebene 3) — was sagen die Daten?

Thermik-relevante Diagnostik aus dem laufenden Archiv:

- Spatial-max-Zeitreihen der vier konvektions-relevanten Tier-1-Vars
  (cape_ml, asob_s, htop_dc, tot_prec) über die letzten N Tage Forecast-
  Horizont. Zeigt, welche Tage konvektiv aktiv sind und wo der
  Tier-2-Trigger feuert.
- OGN-Tagesgang: distinct aircraft IDs pro UTC-Stunde, korreliert mit
  CAPE-Peak — direkte Flugaktivitäts-Validierung des Trigger.
"""

from __future__ import annotations

import datetime as dt

import altair as alt
import streamlit as st

from alptherm_icon.dashboard.data_loader import (
    load_ogn_hourly_activity,
    load_ogn_inventory,
    load_zarr_timeseries,
    project_root,
)

st.set_page_config(page_title="Inhalt", page_icon="📈", layout="wide")
st.title("📈 Inhalt — was sagen die Daten?")
st.caption("Plan §10.2 Ebene 3 — Thermik-relevante Diagnostik.")


@st.cache_data(ttl=30)
def _ts(_root_str: str, days: int):
    return load_zarr_timeseries(project_root(), days_back=days)


@st.cache_data(ttl=30)
def _ogn(_root_str: str, day_str: str | None):
    day = dt.date.fromisoformat(day_str) if day_str else None
    return load_ogn_hourly_activity(project_root(), day=day)


@st.cache_data(ttl=30)
def _ogn_days(_root_str: str):
    return [s.day for s in load_ogn_inventory(project_root())]


root = project_root()

# ---------------------------------------------------------------------------
# Tier-1 Spatial-Max-Zeitreihen — vier konvektive Indikatoren
# ---------------------------------------------------------------------------
st.subheader("Tier-1 Konvektions-Indikatoren — Bbox-Spatial-Max")

cfg_col, _ = st.columns([1, 4])
with cfg_col:
    days_back = st.slider(
        "Forecast-Tage zurück",
        min_value=1,
        max_value=5,
        value=3,
        help="Anzahl Tage Vorhersage-Horizont zurück vom neuesten Zarr-Eintrag.",
    )

df_ts = _ts(str(root), days_back)

if df_ts.empty:
    st.info(
        "Noch keine Zarr-Daten unter `data/archive/zarr/tier1.zarr`. "
        "Der erste Tier-1-Run schreibt sie."
    )
else:
    # One row of 2 charts × 2 = compact grid that scales with wide layout.
    metric_specs = [
        ("cape_ml", "CAPE_ML (J/kg) — Konvektionsenergie", "#e15759"),
        ("asob_s", "ASOB_S (W/m²) — Avg Netto-SW seit Init", "#f1c232"),
        ("htop_dc", "HTOP_DC (m MSL) — Trockenkonv.-Top", "#76a5af"),
        ("tot_prec", "TOT_PREC (mm) — Akk. Niederschlag", "#3d85c6"),
    ]
    cols = st.columns(2)
    for i, (var, label, color) in enumerate(metric_specs):
        sub = df_ts[df_ts["variable"] == var]
        with cols[i % 2]:
            if sub.empty:
                st.warning(f"{label}: keine Daten")
                continue
            chart = (
                alt.Chart(sub)
                .mark_line(point={"size": 30}, color=color)
                .encode(
                    x=alt.X("time:T", title="Valid Time (UTC)"),
                    y=alt.Y("value:Q", title=label),
                    tooltip=[
                        alt.Tooltip("time:T", title="UTC"),
                        alt.Tooltip("value:Q", title=var, format=",.1f"),
                    ],
                )
                .properties(height=240)
            )
            st.altair_chart(chart, use_container_width=True)
    st.caption(
        "Spatial-Max über die Alpen-Bbox (5°O–17°O, 43.5°N–49°N) pro Vollstunden-Lead. "
        "Duplikate aus überlappenden Forecast-Horizonten werden mit first-wins entfernt."
    )


# ---------------------------------------------------------------------------
# OGN-Tagesgang — distinct aircraft per UTC hour
# ---------------------------------------------------------------------------
st.subheader("OGN-Tagesgang — Flugaktivität")

ogn_days = _ogn_days(str(root))
if not ogn_days:
    st.info("Noch keine OGN-Tagesfiles. Daemon läuft erstmals.")
else:
    day_col, _ = st.columns([1, 4])
    with day_col:
        # Default: today (most recent)
        day_options = [d.isoformat() for d in ogn_days]
        selected_day = st.selectbox("Tag", day_options, index=len(day_options) - 1)

    df_ogn = _ogn(str(root), selected_day)
    if df_ogn["n_packets"].sum() == 0:
        st.warning(f"Keine OGN-Pakete für {selected_day} im Roh-Log gefunden.")
    else:
        # Two charts side-by-side: aircraft count + packet rate
        c1, c2 = st.columns(2)
        with c1:
            aircraft_chart = (
                alt.Chart(df_ogn)
                .mark_bar(color="#6aa84f")
                .encode(
                    x=alt.X("hour:O", title="UTC-Stunde"),
                    y=alt.Y("n_aircraft:Q", title="distinct Aircraft-IDs"),
                    tooltip=["hour", "n_aircraft", "n_packets"],
                )
                .properties(height=260, title="Aktive Aircraft pro Stunde")
            )
            st.altair_chart(aircraft_chart, use_container_width=True)
        with c2:
            packet_chart = (
                alt.Chart(df_ogn)
                .mark_bar(color="#a64d79")
                .encode(
                    x=alt.X("hour:O", title="UTC-Stunde"),
                    y=alt.Y("n_packets:Q", title="OGN-Pakete"),
                    tooltip=["hour", "n_packets"],
                )
                .properties(height=260, title="Pakete pro Stunde")
            )
            st.altair_chart(packet_chart, use_container_width=True)
        total_packets = int(df_ogn["n_packets"].sum())
        peak_hour = int(df_ogn.loc[df_ogn["n_aircraft"].idxmax(), "hour"])
        peak_aircraft = int(df_ogn["n_aircraft"].max())
        st.caption(
            f"**{total_packets:,}** Pakete heute, Peak um **{peak_hour:02d} UTC** "
            f"mit **{peak_aircraft:,}** distinkten Aircraft-IDs. "
            "Inkl. ADS-B-Verkehrsflugzeuge — Filterung nach Aircraft-Typ ist eine "
            "Aufgabe der Auswerteschicht (§9.5)."
        )

st.divider()
st.caption(
    "Phase 3 (offen): Vereinsflug-Tracker auf einer eigenen Seite, "
    "Korrelations-Panel Trigger-Tage × OGN-Aktivität für die ganze Saison. "
    "Siehe Plan §10.3 + §10.4 Weiterführende Ideen."
)
