"""Regionen-Karte (Komp. A v0) — interaktive Darstellung des ALPTHERM-
Regionsgerüsts auf einem topografischen Basemap.

Zeigt die 833 Regionen-v1-Polygone (Plan §3.1 Stufe 1+2 + §3.1.1
Quer-Segmentierung) als farbcodiertes Overlay. Färbeschemata:
Höhen-Habitat, Segment-Band, Größenklasse, oder Einzelfärbung
(jede Region distinct via Graph-Coloring). Optionaler Alpen-Perimeter
als Linie.
"""

from __future__ import annotations

import json

import folium
import streamlit as st
from streamlit_folium import st_folium

from alptherm_icon.dashboard.data_loader import (
    load_alpine_perimeter,
    load_regions,
    project_root,
)

st.set_page_config(page_title="Regionen", page_icon="🗺️", layout="wide")
st.title("🗺️ ALPTHERM-Regionen")
st.caption(
    "Komp. A v0 — 833 Regionen alpenweit (HydroBASINS L8 + §3.1.1 "
    "Höhenband-Segmentierung)."
)

# 7-Farben-Palette für die Einzelfärbung (greedy graph coloring liefert ≤7).
DISTINCT_PALETTE = [
    "#4e79a7", "#f28e2b", "#59a14f", "#e15759",
    "#b07aa1", "#76b7b2", "#edc948", "#ff9da7", "#9c755f",
]

# Topografische Basemaps (XYZ-Tiles, kein Token nötig).
BASEMAPS = {
    "ESRI World Topo": {
        "tiles": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
        "attr": "Tiles © Esri — Esri, DeLorme, NAVTEQ, TomTom, USGS, ...",
        "max_zoom": 19,
    },
    "OpenTopoMap": {
        "tiles": "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
        "attr": "© OpenTopoMap (CC-BY-SA), © OpenStreetMap-Mitwirkende",
        "max_zoom": 17,
    },
    "ESRI Imagery (Satellit)": {
        "tiles": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "attr": "Tiles © Esri — Source: Esri, Maxar, Earthstar Geographics, ...",
        "max_zoom": 19,
    },
}


@st.cache_data(ttl=300)
def _regions_fc(_root_str: str, simplify_deg: float):
    """Return the GeoJSON FeatureCollection dict with size_band already
    derived. Cached → no JSON round-trip on rerun.
    """
    gdf = load_regions(project_root(), simplify_deg=simplify_deg)
    if gdf is None:
        return None
    fc = json.loads(gdf.to_json())
    for feat in fc["features"]:
        a = feat["properties"].get("area_km2") or 0.0
        feat["properties"]["size_band"] = (
            "<100" if a < 100 else "100–500" if a < 500
            else "500–1500" if a < 1500 else ">1500"
        )
    return fc


@st.cache_data(ttl=300)
def _perimeter_dict(_root_str: str):
    """Return the perimeter boundary as a GeoJSON-mappable dict."""
    import shapely.geometry

    geom = load_alpine_perimeter(project_root())
    return None if geom is None else shapely.geometry.mapping(geom.boundary)


root = project_root()

# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------
c1, c2, c3, c4 = st.columns([1.2, 1.2, 1, 1.2])
with c1:
    basemap_name = st.selectbox("Hintergrundkarte", list(BASEMAPS), index=0)
with c2:
    color_by = st.selectbox(
        "Färben nach",
        ["einzeln", "habitat_class", "band", "size_band"],
        format_func={
            "einzeln": "Einzeln (jede Region)",
            "habitat_class": "Habitat (alpine/vorland)",
            "band": "Segment-Band",
            "size_band": "Größenklasse",
        }.get,
    )
with c3:
    detail = st.select_slider(
        "Detailgrad", options=["grob", "mittel", "fein"], value="mittel"
    )
with c4:
    show_perimeter = st.checkbox("Alpen-Perimeter", value=True)

simplify_map = {"grob": 0.006, "mittel": 0.003, "fein": 0.0008}

fc = _regions_fc(str(root), simplify_map[detail])
if fc is None:
    st.warning(
        "Noch kein Regions-GeoJSON. Erst die Komp.-A-Pipeline laufen lassen:\n\n"
        "```\npython -m alptherm_icon.regions alpine-v0\n"
        "python -m alptherm_icon.regions alpine-v0-dem\n"
        "python -m alptherm_icon.regions alpine-v0-ahd\n"
        "python -m alptherm_icon.regions alpine-v1\n```"
    )
    st.stop()

features = fc["features"]

palettes = {
    "habitat_class": {"alpine": "#2e7d32", "vorland": "#c8a063"},
    "band": {
        "whole": "#9e9e9e",
        "flachland": "#c8a063",
        "voralpen": "#7cb342",
        "hochgebirge": "#455a64",
    },
    "size_band": {
        "<100": "#bbbbbb",
        "100–500": "#90caf9",
        "500–1500": "#1e88e5",
        ">1500": "#fb8c00",
    },
}


def _fill_color(props) -> str:
    if color_by == "einzeln":
        return DISTINCT_PALETTE[int(props.get("color_idx", 0)) % len(DISTINCT_PALETTE)]
    return palettes[color_by].get(props.get(color_by, ""), "#999999")


def _style(feature):
    return {
        "fillColor": _fill_color(feature["properties"]),
        "color": "#333333",
        "weight": 0.4,
        "fillOpacity": 0.5 if color_by == "einzeln" else 0.45,
    }


# ---------------------------------------------------------------------------
# Map
# ---------------------------------------------------------------------------
bm = BASEMAPS[basemap_name]
m = folium.Map(location=[46.5, 11.0], zoom_start=6, tiles=None)
folium.TileLayer(
    tiles=bm["tiles"], attr=bm["attr"], name=basemap_name, max_zoom=bm["max_zoom"]
).add_to(m)

folium.GeoJson(
    fc,
    name="ALPTHERM-Regionen",
    style_function=_style,
    tooltip=folium.GeoJsonTooltip(
        fields=["region_id", "band", "habitat_class", "area_km2"],
        aliases=["Region", "Band", "Habitat", "Fläche (km²)"],
        localize=True,
    ),
    smooth_factor=1.0,
).add_to(m)

# Alpine perimeter as a bold line (dissolved alpine-region boundary).
if show_perimeter:
    peri_dict = _perimeter_dict(str(root))
    if peri_dict is not None:
        folium.GeoJson(
            peri_dict,
            name="Alpen-Perimeter (topographisch)",
            style_function=lambda _f: {"color": "#c62828", "weight": 2.5, "fillOpacity": 0},
        ).add_to(m)

folium.LayerControl().add_to(m)

label = "einzeln (Graph-Coloring)" if color_by == "einzeln" else f"`{color_by}`"
st.markdown(f"**{len(features)} Regionen** · Färbung: {label} · Karte: {basemap_name}")
st_folium(m, width=None, height=620, returned_objects=[])

# ---------------------------------------------------------------------------
# Legend
# ---------------------------------------------------------------------------
if color_by != "einzeln":
    with st.expander("Legende + Statistik", expanded=True):
        palette = palettes[color_by]
        counts: dict[str, int] = {}
        for feat in features:
            v = feat["properties"].get(color_by, "")
            counts[v] = counts.get(v, 0) + 1
        cols = st.columns(len(palette))
        for col, (label_, color) in zip(cols, palette.items()):
            col.markdown(
                f"<div style='display:flex;align-items:center;gap:6px'>"
                f"<div style='width:16px;height:16px;background:{color};"
                f"border:1px solid #333'></div><span>{label_} "
                f"({counts.get(label_, 0)})</span></div>",
                unsafe_allow_html=True,
            )
else:
    st.caption(
        "Einzelfärbung: benachbarte Regionen bekommen via Graph-Coloring "
        "garantiert unterschiedliche Farben (≤ 7 Farben genügen)."
    )

st.caption(
    "Roter Umriss = topographischer Alpen-Perimeter (gedissolvte Grenze der "
    "alpine-Regionen, 600 m-MSL-Schwelle, Plan §3.1.1). Der offizielle "
    "Alpenkonventions-Perimeter ist nicht frei abrufbar publiziert."
)
