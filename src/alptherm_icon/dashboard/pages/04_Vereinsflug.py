"""Vereinsflug-Tracker (Plan §10.3) — Watchlist + Karte + Ground-Truth-Werkzeug.

Doppelnutzen pro Plan:

1. *Als Feature*: unmittelbar attraktiv, macht das Projekt für den Verein
   sichtbar.
2. *Als Test-Instrument*: Vereinsflugzeuge sind kontrollierte Ground Truth
   — testet OGN-Abdeckung, Höhenreferenz, Device-DB-Zuordnung an Flügen,
   deren Wahrheit man kennt.

Die Watchlist wird aus ``data/watchlist.json`` (gitignored) gelesen; ein
Beispiel-File ``data/watchlist.example.json`` zeigt das Format.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pydeck as pdk
import streamlit as st

from alptherm_icon.dashboard.data_loader import (
    load_ogn_inventory,
    load_watchlist,
    load_watchlist_positions,
    project_root,
)

st.set_page_config(page_title="Vereinsflug", page_icon="🛩️", layout="wide")
st.title("🛩️ Vereinsflug — Tracker und Ground-Truth")
st.caption("Plan §10.3 — Watchlist auf OGN-Roh-Log + Live-Karte.")


@st.cache_data(ttl=60)
def _watchlist(_root_str: str):
    return load_watchlist(project_root())


@st.cache_data(ttl=120)
def _positions(_root_str: str, day_str: str | None):
    # First load ~50 s on a busy OGN day (full scan of ~25 M packets);
    # subsequent loads within the TTL are instant.
    day = dt.date.fromisoformat(day_str) if day_str else None
    return load_watchlist_positions(project_root(), day=day)


@st.cache_data(ttl=30)
def _inventory(_root_str: str):
    return [s.day for s in load_ogn_inventory(project_root())]


root = project_root()
watchlist = _watchlist(str(root))

# ---------------------------------------------------------------------------
# Empty-state — watchlist not yet set up
# ---------------------------------------------------------------------------
if not watchlist:
    st.info(
        "Keine Watchlist konfiguriert. Lege `data/watchlist.json` an "
        "(Beispiel: `data/watchlist.example.json`). Format:\n\n"
        "```json\n"
        "[\n"
        '  {"name": "D-1234 Discus", "ogn_name": "FLRDDDD24"},\n'
        '  {"name": "OE-9000 Duo Discus", "ogn_name": "ICA3F5AB7"}\n'
        "]\n```"
    )
    st.stop()


# ---------------------------------------------------------------------------
# Day selector — defaults to most recent day in inventory
# ---------------------------------------------------------------------------
inventory_days = _inventory(str(root))
if not inventory_days:
    st.warning("Kein OGN-Roh-Log vorhanden. Daemon läuft erstmals?")
    st.stop()

day_col, refresh_col = st.columns([1, 4])
with day_col:
    day_options = [d.isoformat() for d in inventory_days]
    selected_day = st.selectbox("Tag", day_options, index=len(day_options) - 1)
with refresh_col:
    if st.button("↻ Erneut scannen"):
        _positions.clear()

with st.spinner(f"Scanne OGN-Roh-Log für {len(watchlist)} Aircraft…"):
    positions = _positions(str(root), selected_day)

# ---------------------------------------------------------------------------
# Status-Tabelle
# ---------------------------------------------------------------------------
st.subheader("Status")

now = dt.datetime.now(dt.timezone.utc)
table_rows = []
map_rows = []
aircraft_type_label = {
    0: "(unbekannt)",
    1: "Segelflug",
    2: "Motor",
    3: "Heli",
    4: "Fallschirm",
    5: "Drop-Flieger",
    6: "Hängegleiter",
    7: "Gleitschirm",
    8: "Powered",
    9: "Jet",
    10: "UFO",
    11: "Balloon",
    12: "Airship",
    13: "UAV",
}

for pos in positions:
    last_seen_label = "—"
    age_min = None
    is_active = False
    if pos.last_seen_utc:
        age_min = (now - pos.last_seen_utc).total_seconds() / 60
        last_seen_label = pos.last_seen_utc.strftime("%H:%M:%SZ")
        is_active = age_min < 15  # last beacon < 15 min ago
    row = {
        "Aircraft": pos.entry.name,
        "OGN-ID": pos.entry.ogn_name,
        "Status": "🟢 in der Luft" if is_active else "⚪ ruhend",
        "Letzte Meldung (UTC)": last_seen_label,
        "Alter (min)": round(age_min, 1) if age_min is not None else None,
        "Höhe (m)": round(pos.altitude_m, 0) if pos.altitude_m is not None else None,
        "Steigen (m/s)": round(pos.climb_rate, 1) if pos.climb_rate is not None else None,
        "Speed (km/h)": round(pos.ground_speed_kmh, 0) if pos.ground_speed_kmh is not None else None,
        "Typ": aircraft_type_label.get(pos.aircraft_type or 0, str(pos.aircraft_type)),
        "Pakete heute": pos.packets_today,
    }
    if pos.entry.note:
        row["Aircraft"] = f"{pos.entry.name}  ({pos.entry.note})"
    table_rows.append(row)
    if pos.latitude is not None and pos.longitude is not None:
        map_rows.append(
            {
                "lat": pos.latitude,
                "lon": pos.longitude,
                "name": pos.entry.name,
                "altitude_m": pos.altitude_m,
                "is_active": is_active,
            }
        )

st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Karte (pydeck)
# ---------------------------------------------------------------------------
st.subheader("Karte")

if not map_rows:
    st.info(
        "Keine Positionen heute. Entweder hat keines der Aircraft heute "
        "gesendet, oder die OGN-IDs in `watchlist.json` stimmen nicht."
    )
else:
    df_map = pd.DataFrame(map_rows)
    df_map["color"] = df_map["is_active"].apply(
        lambda a: [60, 200, 60, 200] if a else [180, 180, 180, 180]
    )
    df_map["radius"] = df_map["is_active"].apply(lambda a: 600 if a else 300)

    midpoint = (
        float(df_map["lat"].mean()),
        float(df_map["lon"].mean()),
    )

    layer = pdk.Layer(
        "ScatterplotLayer",
        data=df_map,
        get_position="[lon, lat]",
        get_fill_color="color",
        get_radius="radius",
        pickable=True,
        opacity=0.85,
    )
    text_layer = pdk.Layer(
        "TextLayer",
        data=df_map,
        get_position="[lon, lat]",
        get_text="name",
        get_size=14,
        get_color=[0, 0, 0],
        get_alignment_baseline="bottom",
        get_pixel_offset=[0, -10],
    )
    view_state = pdk.ViewState(
        latitude=midpoint[0],
        longitude=midpoint[1],
        zoom=8,
        pitch=0,
    )
    deck = pdk.Deck(
        map_style=None,  # use default
        layers=[layer, text_layer],
        initial_view_state=view_state,
        tooltip={
            "html": "<b>{name}</b><br/>"
            "Lat/Lon: {lat:.4f}, {lon:.4f}<br/>"
            "Höhe: {altitude_m} m"
        },
    )
    st.pydeck_chart(deck)


st.divider()
st.caption(
    "Ground-Truth-Wert per Plan §10.3: jede Maschine deren Position hier nicht "
    "auftaucht, obwohl sie geflogen ist (Flugbuchabgleich), zeigt einen Funkschatten "
    "oder eine fehlende Device-DB-Zuordnung auf. Höhen-Abweichungen kalibrieren "
    "das Höhenreferenz-Caveat aus 6.6."
)
