"""Thermik-Karte (Komp. D §6.2/§6.3) — erkannte Thermikzentren pro Tag
auf Regionen + Topo-Hintergrund.

Zeigt die detektierten Kreisflug-Phasen eines Tages als Punkte,
einfärbbar nach mittlerem Steigwert, maximaler Höhe oder Flugzeugtyp
(Gleitschirm / Segelflug / Drachen). pydeck (GPU) für tausende Punkte.
"""

from __future__ import annotations

import json

import numpy as np
import pydeck as pdk
import streamlit as st

from alptherm_icon.dashboard.data_loader import (
    list_thermal_days,
    load_regions,
    load_thermals,
    project_root,
)

st.set_page_config(page_title="Thermiken", page_icon="🔥", layout="wide")
st.title("🔥 Erkannte Thermikzentren")
st.caption(
    "Komp. D §6.2 — Kreisflug-Detektion auf OGN-Tracks. Punkt = ein "
    "detektierter Kreisflug, Position = Centroid."
)

AC_TYPE_LABEL = {1: "Segelflug", 6: "Drachen", 7: "Gleitschirm", None: "unbekannt"}
AC_TYPE_COLOR = {
    1: [30, 120, 220],    # Segelflug — blau
    7: [230, 120, 30],    # Gleitschirm — orange
    6: [120, 200, 60],    # Drachen — grün
    None: [150, 150, 150],
}


@st.cache_data(ttl=120)
def _days(_root: str) -> list[str]:
    return list_thermal_days(project_root())


@st.cache_data(ttl=120)
def _thermals_df(_root: str, day: str):
    """Cache the DataFrame directly — avoids the to_json/read_json
    round-trip (~600–1000 ms on 12 k thermals)."""
    return load_thermals(project_root(), day=day)


@st.cache_data(ttl=300)
def _regions_overlay_fc(_root: str):
    """Region outlines as a GeoJSON FeatureCollection dict (no fill,
    used only for the line overlay under the thermal points)."""
    gdf = load_regions(project_root(), simplify_deg=0.005, with_colors=False)
    return None if gdf is None else json.loads(gdf.to_json())


root = project_root()
days = _days(str(root))
if not days:
    st.warning(
        "Noch keine Thermik-Daten. Erst die Detektion laufen lassen:\n\n"
        "```\npython -m alptherm_icon.igc_pipeline detect --day YYYY-MM-DD\n```"
    )
    st.stop()

# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------
c1, c2, c3 = st.columns([1, 1.3, 1.5])
with c1:
    day = st.selectbox("Tag", days, index=len(days) - 1)
with c2:
    color_by = st.selectbox(
        "Einfärben nach",
        ["climb", "alt_top", "aircraft"],
        format_func={
            "climb": "Mittl. Steigwert (m/s)",
            "alt_top": "Max. Höhe (m)",
            "aircraft": "Flugzeugtyp",
        }.get,
    )
with c3:
    min_climb = st.slider("Min. Netto-Steigen (m/s)", -1.0, 3.0, 0.0, 0.25)

import pandas as pd

df = _thermals_df(str(root), day)
if df is None or df.empty:
    st.info(f"Keine Thermiken für {day}.")
    st.stop()

df = df[df["climb_rate_ms"] >= min_climb].copy()
st.markdown(
    f"**{len(df)} Thermiken** am {day} "
    f"(Filter Netto-Steigen ≥ {min_climb:.2f} m/s)"
)

# ---------------------------------------------------------------------------
# Per-point colour
# ---------------------------------------------------------------------------
def _ramp(values, vmin, vmax, lo_rgb, hi_rgb):
    """Linear RGB ramp lo→hi over [vmin, vmax]."""
    t = np.clip((values - vmin) / (vmax - vmin + 1e-9), 0, 1)
    return [
        [int(lo_rgb[k] + (hi_rgb[k] - lo_rgb[k]) * ti) for k in range(3)] for ti in t
    ]


if color_by == "climb":
    colors = _ramp(df["climb_rate_ms"].to_numpy(), 0.0, 2.5, [60, 60, 200], [220, 40, 40])
    legend = "blau = schwach · rot = stark (0–2,5 m/s)"
elif color_by == "alt_top":
    colors = _ramp(df["alt_top_m"].to_numpy(), 1000, 4000, [40, 60, 120], [240, 240, 80])
    legend = "dunkel = tief · hell = hoch (1000–4000 m)"
else:  # aircraft
    colors = [AC_TYPE_COLOR.get(t if t in AC_TYPE_COLOR else None) for t in df["aircraft_type"]]
    present = df["aircraft_type"].value_counts(dropna=False).to_dict()
    legend = " · ".join(
        f"{AC_TYPE_LABEL.get(t)}={n}" for t, n in present.items()
    )

df["_r"] = [c[0] for c in colors]
df["_g"] = [c[1] for c in colors]
df["_b"] = [c[2] for c in colors]
df["ac_label"] = df["aircraft_type"].map(lambda t: AC_TYPE_LABEL.get(t if t in AC_TYPE_LABEL else None))

# ---------------------------------------------------------------------------
# pydeck map — ESRI topo tiles + region outlines + thermal scatter
# ---------------------------------------------------------------------------
layers = []

tile_layer = pdk.Layer(
    "TileLayer",
    data="https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
    min_zoom=0,
    max_zoom=19,
    tile_size=256,
)
layers.append(tile_layer)

regions_fc = _regions_overlay_fc(str(root))
if regions_fc:
    layers.append(
        pdk.Layer(
            "GeoJsonLayer",
            data=regions_fc,
            stroked=True,
            filled=False,
            get_line_color=[80, 80, 80, 120],
            line_width_min_pixels=0.5,
        )
    )

layers.append(
    pdk.Layer(
        "ScatterplotLayer",
        data=df[
            ["lon_centroid", "lat_centroid", "_r", "_g", "_b",
             "climb_rate_ms", "alt_top_m", "ac_label", "n_turns", "region_id"]
        ],
        get_position="[lon_centroid, lat_centroid]",
        get_fill_color="[_r, _g, _b, 200]",
        get_radius=600,
        radius_min_pixels=2,
        radius_max_pixels=8,
        pickable=True,
    )
)

view = pdk.ViewState(latitude=46.8, longitude=10.5, zoom=6.2, pitch=0)
deck = pdk.Deck(
    map_style=None,
    layers=layers,
    initial_view_state=view,
    tooltip={
        "html": "<b>{ac_label}</b><br/>Steigen: {climb_rate_ms} m/s<br/>"
        "Top: {alt_top_m} m<br/>Umläufe: {n_turns}<br/>Region: {region_id}"
    },
)
st.pydeck_chart(deck)
st.caption(f"Legende: {legend}")

# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
with st.expander("Statistik", expanded=True):
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Thermiken", str(len(df)))
    pos = df[df["climb_rate_ms"] > 0]["climb_rate_ms"]
    s2.metric("Steigen Median (pos)", f"{pos.median():.2f} m/s" if len(pos) else "—")
    s3.metric("Steigen Q90 (pos)", f"{pos.quantile(0.9):.2f} m/s" if len(pos) else "—")
    s4.metric("Top-Höhe Median", f"{df['alt_top_m'].median():.0f} m")

    # Per aircraft type
    rows = []
    for t, grp in df.groupby("aircraft_type", dropna=False):
        p = grp[grp["climb_rate_ms"] > 0]["climb_rate_ms"]
        rows.append(
            {
                "Typ": AC_TYPE_LABEL.get(t if t in AC_TYPE_LABEL else None),
                "n": len(grp),
                "Steigen Median (pos)": round(p.median(), 2) if len(p) else None,
                "Steigen Q90 (pos)": round(p.quantile(0.9), 2) if len(p) else None,
                "Top-Höhe Median (m)": round(grp["alt_top_m"].median()),
            }
        )
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

st.caption(
    "Netto-Steigen = (Top − Basis) / Dauer über die ganze Kreisphase; "
    "Such-/Sink-Kreisen ergibt teils negatives Netto. Für Tuning (§7.6) "
    "wird auf positives Steigen bzw. Q90 gefiltert."
)
