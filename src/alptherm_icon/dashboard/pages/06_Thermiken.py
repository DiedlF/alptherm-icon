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
    load_aircraft_track,
    load_hydrobasins_for_display,
    load_thermals,
    project_root,
)

REGION_LEVEL = 7  # HydroBASINS level used for background outlines + classification

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
def _track(_root: str, day: str, source_id: str):
    """Full GPS track of one aircraft on a day, as [[lon, lat], …]."""
    return load_aircraft_track(project_root(), day, source_id)


@st.cache_resource(ttl=600)
def _regions_gdf(_root: str, level: int):
    """The current HydroBASINS L{level} regions (geometry kept for sjoin)."""
    return load_hydrobasins_for_display(project_root(), level=level, simplify_deg=0.005)


@st.cache_data(ttl=300)
def _regions_overlay_fc(_root: str, level: int):
    """Region outlines as a GeoJSON FeatureCollection dict (line overlay)."""
    gdf = _regions_gdf(_root, level)
    return None if gdf is None else json.loads(gdf.to_json())


def _classify_to_regions(level: int, lons, lats):
    """Assign each (lon, lat) to its containing HydroBASINS L{level} basin →
    list of HYBAS_ID strings ('' if outside all basins)."""
    import geopandas as gpd

    gdf = _regions_gdf("", level)
    if gdf is None:
        return ["" for _ in lons]
    pts = gpd.GeoDataFrame(
        geometry=gpd.points_from_xy(lons, lats), crs="EPSG:4326"
    )
    joined = gpd.sjoin(pts, gdf[["HYBAS_ID", "geometry"]], how="left", predicate="within")
    joined = joined[~joined.index.duplicated(keep="first")]
    return [
        "" if v is None or (isinstance(v, float) and v != v) else str(int(v))
        for v in joined["HYBAS_ID"].reindex(range(len(lons)))
    ]


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
        ["climb", "alt_top", "n_turns", "aircraft"],
        format_func={
            "climb": "Mittl. Steigwert (m/s)",
            "alt_top": "Max. Höhe (m)",
            "n_turns": "Umdrehungen",
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
elif color_by == "n_turns":
    colors = _ramp(df["n_turns"].to_numpy(), 2, 12, [70, 160, 70], [150, 40, 160])
    legend = "grün = wenige · violett = viele Umdrehungen (2–12)"
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

# Tooltip display columns: low precision (0.1 for lift & turns, integers else) + time.
df["time_str"] = df["t_start"].dt.strftime("%H:%M")
df["climb_1"] = df["climb_rate_ms"].fillna(0).round(1)
df["n_turns_1"] = df["n_turns"].fillna(0).round(1)
df["alt_top_i"] = df["alt_top_m"].fillna(0).round().astype(int)
# Classify each thermal to the current HydroBASINS L7 basin (background regions).
df["region_l7"] = _classify_to_regions(REGION_LEVEL, df["lon_centroid"].tolist(), df["lat_centroid"].tolist())
# Mark thermals whose turn count is *estimated* (confined/undersampled reception).
method = df["method"] if "method" in df.columns else "turn"
df["turns_mark"] = ["≈" if m == "confined" else "" for m in (method if hasattr(method, "__iter__") else [method] * len(df))]

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

regions_fc = _regions_overlay_fc(str(root), REGION_LEVEL)
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

# Selection from a click on the previous run: which aircraft is picked?
# The map's widget key carries a nonce so the "Alle anzeigen" button can reset
# the selection by forcing a fresh pydeck widget (selection state can't be
# mutated directly once the widget exists).
st.session_state.setdefault("thermap_nonce", 0)
map_key = f"thermap_{st.session_state['thermap_nonce']}"
sel_state = st.session_state.get(map_key, {})
sel_objs = []
if isinstance(sel_state, dict):
    sel_objs = (sel_state.get("selection", {}) or {}).get("objects", {}).get("thermals", [])
sel_id = sel_objs[0].get("source_id") if sel_objs else None
sel_time = sel_objs[0].get("time_str") if sel_objs else None

if st.button("◻ Alle anzeigen", disabled=sel_id is None, help="Auswahl aufheben"):
    st.session_state["thermap_nonce"] += 1
    st.rerun()

# When a flight is selected, show only its thermals; otherwise show all.
scatter_df = df[df["source_id"] == sel_id] if sel_id else df

layers.append(
    pdk.Layer(
        "ScatterplotLayer",
        id="thermals",
        data=scatter_df[
            ["lon_centroid", "lat_centroid", "_r", "_g", "_b",
             "climb_1", "alt_top_i", "ac_label", "n_turns_1", "turns_mark", "region_l7",
             "source_id", "time_str"]
        ],
        get_position="[lon_centroid, lat_centroid]",
        get_fill_color="[_r, _g, _b, 200]",
        get_radius=600,
        radius_min_pixels=2,
        radius_max_pixels=8,
        pickable=True,
    )
)

# Selected aircraft → also draw its full day track.
track = _track(str(root), day, sel_id) if sel_id else None
has_track = track is not None and not track.empty
if has_track:
    path = track[["lon", "lat"]].values.tolist()
    layers.append(
        pdk.Layer(
            "PathLayer",
            data=[{"path": path}],
            get_path="path",
            get_color=[255, 230, 0],  # solid yellow for contrast
            width_min_pixels=4,
            get_width=8,
        )
    )

view = pdk.ViewState(latitude=46.8, longitude=10.5, zoom=6.2, pitch=0)
deck = pdk.Deck(
    map_style=None,
    layers=layers,
    initial_view_state=view,
    tooltip={
        "html": "<b>{ac_label}</b> · {time_str}<br/>Steigen: {climb_1} m/s<br/>"
        "Top: {alt_top_i} m · Umläufe: {turns_mark}{n_turns_1}<br/>"
        "ID: {source_id} · Region: {region_l7}"
    },
)
st.pydeck_chart(deck, key=map_key, on_select="rerun", selection_mode="single-object")

if sel_id:
    n_sel = len(scatter_df)
    track_note = f"{len(track)} Track-Punkte" if has_track else "kein Roh-Track gefunden"
    st.caption(
        f"🛩️ **{sel_id}** ({sel_time}) — nur dessen {n_sel} Thermiken + Track ({track_note}). "
        "Klick ins Leere zeigt wieder alle."
    )
    # Altitude profile + a linked track locator. Hovering the altitude curve
    # highlights the aircraft's position at that time on the locator (clientside,
    # so the basemap map above doesn't reset). All times UTC.
    if has_track:
        import altair as alt

        tdf = track.copy()
        tdf["t"] = pd.to_datetime(tdf["t"], utc=True).dt.tz_localize(None)
        ph = scatter_df[["t_start", "t_end"]].copy()
        ph["t_start"] = pd.to_datetime(ph["t_start"], utc=True).dt.tz_localize(None)
        ph["t_end"] = pd.to_datetime(ph["t_end"], utc=True).dt.tz_localize(None)

        hover = alt.selection_point(fields=["t"], nearest=True, on="pointerover", empty=False)

        phases = alt.Chart(ph).mark_rect(opacity=0.16, color="#e6a000").encode(
            x="t_start:T", x2="t_end:T"
        )
        alt_line = alt.Chart(tdf).mark_line(color="#555", strokeWidth=1.2).encode(
            x=alt.X("t:T", title="Zeit (UTC)"),
            y=alt.Y("alt_m:Q", title="Höhe (m ASL)", scale=alt.Scale(zero=False)),
        )
        sel_pts = alt.Chart(tdf).mark_point(size=1, opacity=0).encode(
            x="t:T", y="alt_m:Q"
        ).add_params(hover)
        alt_rule = alt.Chart(tdf).mark_rule(color="#d62728").encode(x="t:T").transform_filter(hover)
        alt_dot = alt.Chart(tdf).mark_point(color="#d62728", size=70, filled=True).encode(
            x="t:T", y="alt_m:Q",
            tooltip=[alt.Tooltip("t:T", title="UTC", format="%H:%M:%S"), alt.Tooltip("alt_m:Q", title="Höhe", format=".0f")],
        ).transform_filter(hover)
        profile = (phases + alt_line + sel_pts + alt_rule + alt_dot).properties(height=240)

        locator = (
            alt.Chart(tdf).mark_line(color="#bbb", strokeWidth=1).encode(
                x=alt.X("lon:Q", title=None, scale=alt.Scale(zero=False), axis=None),
                y=alt.Y("lat:Q", title=None, scale=alt.Scale(zero=False), axis=None),
            )
            + alt.Chart(tdf).mark_point(color="#d62728", size=90, filled=True).encode(
                x="lon:Q", y="lat:Q"
            ).transform_filter(hover)
        ).properties(height=240, title="Position (Cursor)")

        st.altair_chart(profile | locator, use_container_width=True)
else:
    st.caption(f"Legende: {legend} · Tipp: Punkt anklicken filtert auf dieses Flugzeug + zeigt seinen Tagestrack.")

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
