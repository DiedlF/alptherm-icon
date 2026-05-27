"""Regionen-Karte (Komp. A v0) — interaktive Darstellung des ALPTHERM-
Regionsgerüsts auf einem Terrain-Basemap.

Zeigt die 833 Regionen-v1-Polygone (Plan §3.1 Stufe 1+2 + §3.1.1
Quer-Segmentierung) als farbcodiertes Overlay über OpenTopoMap. Drei
Farbschemata wählbar: Höhen-Habitat (alpine/vorland), Segment-Band,
Größenklasse.
"""

from __future__ import annotations

import folium
import streamlit as st
from streamlit_folium import st_folium

from alptherm_icon.dashboard.data_loader import load_regions, project_root

st.set_page_config(page_title="Regionen", page_icon="🗺️", layout="wide")
st.title("🗺️ ALPTHERM-Regionen")
st.caption(
    "Komp. A v0 — 833 Regionen alpenweit (HydroBASINS L8 + §3.1.1 "
    "Höhenband-Segmentierung). Hintergrund: OpenTopoMap."
)


@st.cache_data(ttl=300)
def _regions(_root_str: str, simplify_deg: float):
    gdf = load_regions(project_root(), simplify_deg=simplify_deg)
    if gdf is None:
        return None
    # Return GeoJSON-serialisable records — folium needs __geo_interface__
    # and cache_data can't pickle a live GeoDataFrame cheaply.
    return gdf.to_json()


root = project_root()

# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------
ctrl1, ctrl2, ctrl3 = st.columns([1, 1, 2])
with ctrl1:
    color_by = st.selectbox(
        "Färben nach",
        ["habitat_class", "band", "size_band"],
        format_func={
            "habitat_class": "Habitat (alpine/vorland)",
            "band": "Segment-Band",
            "size_band": "Größenklasse",
        }.get,
    )
with ctrl2:
    detail = st.select_slider(
        "Detailgrad",
        options=["grob (schnell)", "mittel", "fein (langsam)"],
        value="mittel",
    )
simplify_map = {"grob (schnell)": 0.006, "mittel": 0.003, "fein (langsam)": 0.0008}

geojson_str = _regions(str(root), simplify_map[detail])
if geojson_str is None:
    st.warning(
        "Noch kein Regions-GeoJSON. Erst die Komp.-A-Pipeline laufen lassen:\n\n"
        "```\npython -m alptherm_icon.regions alpine-v0\n"
        "python -m alptherm_icon.regions alpine-v0-dem\n"
        "python -m alptherm_icon.regions alpine-v0-ahd\n"
        "python -m alptherm_icon.regions alpine-v1\n```"
    )
    st.stop()

import json

fc = json.loads(geojson_str)
features = fc["features"]

# Derive a size_band on the fly (area_km2 already in properties).
for feat in features:
    a = feat["properties"].get("area_km2") or 0.0
    if a < 100:
        feat["properties"]["size_band"] = "<100"
    elif a < 500:
        feat["properties"]["size_band"] = "100–500"
    elif a < 1500:
        feat["properties"]["size_band"] = "500–1500"
    else:
        feat["properties"]["size_band"] = ">1500"

# Color palettes per scheme.
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
palette = palettes[color_by]


def _style(feature):
    val = feature["properties"].get(color_by, "")
    return {
        "fillColor": palette.get(val, "#999999"),
        "color": "#333333",
        "weight": 0.4,
        "fillOpacity": 0.45,
    }


# ---------------------------------------------------------------------------
# Build the folium map (OpenTopoMap basemap, centered on the Alps)
# ---------------------------------------------------------------------------
m = folium.Map(
    location=[46.5, 11.0],
    zoom_start=6,
    tiles=None,
)
folium.TileLayer(
    tiles="https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
    attr="© OpenTopoMap (CC-BY-SA), © OpenStreetMap-Mitwirkende",
    name="OpenTopoMap",
    max_zoom=17,
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

folium.LayerControl().add_to(m)

st.markdown(f"**{len(features)} Regionen** · Färbung: `{color_by}`")
st_folium(m, width=None, height=620, returned_objects=[])

# ---------------------------------------------------------------------------
# Legend + stats
# ---------------------------------------------------------------------------
with st.expander("Legende + Statistik", expanded=True):
    cols = st.columns(len(palette))
    counts: dict[str, int] = {}
    for feat in features:
        v = feat["properties"].get(color_by, "")
        counts[v] = counts.get(v, 0) + 1
    for col, (label, color) in zip(cols, palette.items()):
        col.markdown(
            f"<div style='display:flex;align-items:center;gap:6px'>"
            f"<div style='width:16px;height:16px;background:{color};"
            f"border:1px solid #333'></div><span>{label} "
            f"({counts.get(label, 0)})</span></div>",
            unsafe_allow_html=True,
        )

st.caption(
    "Stufe 3 (datengetriebene L9-Verfeinerung) folgt nach einer Saison "
    "ICON-Archiv — dann werden hochvariante Regionen weiter gesplittet (Plan §3.1)."
)
