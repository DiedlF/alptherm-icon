"""Regionen-Karte — ALPTHERM-Regionen aus HydroBASINS-Einzugsgebieten.

Die Regionen sind HydroBASINS-Einzugsgebiete (Standard-Level 7) im Modellgebiet,
auf Wasserscheiden ausgerichtet. Optional als Hintergrund: der äußere Domänenrand.
"""

from __future__ import annotations

import json

import folium
import streamlit as st
from streamlit_folium import st_folium

from alptherm_icon.dashboard.data_loader import (
    load_domain_boundary,
    load_hydrobasins_for_display,
    project_root,
)

st.set_page_config(page_title="Regionen", page_icon="🗺️", layout="wide")
st.title("🗺️ ALPTHERM-Regionen (HydroBASINS)")

BASEMAPS = {
    "ESRI World Topo": {
        "tiles": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
        "attr": "Tiles © Esri — Esri, DeLorme, NAVTEQ, TomTom, USGS",
        "max_zoom": 19,
    },
    "OpenTopoMap": {
        "tiles": "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
        "attr": "© OpenTopoMap (CC-BY-SA), © OpenStreetMap-Mitwirkende",
        "max_zoom": 17,
    },
    "ESRI Imagery (Satellit)": {
        "tiles": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "attr": "Tiles © Esri — Source: Esri, Maxar, Earthstar Geographics",
        "max_zoom": 19,
    },
}

DISTINCT_PALETTE = [
    "#4e79a7", "#f28e2b", "#59a14f", "#e15759",
    "#b07aa1", "#76b7b2", "#edc948", "#ff9da7", "#9c755f",
]

SIZE_PALETTE = {
    "<100": "#bbbbbb",
    "100–500": "#90caf9",
    "500–1500": "#1e88e5",
    ">1500": "#fb8c00",
}

# Sequential ramp (YlGnBu) for continuous size colouring: small → large.
SIZE_GRADIENT = ["#ffffcc", "#a1dab4", "#41b6c4", "#2c7fb8", "#253494"]


def _ramp_color(t: float, stops: list[str] = SIZE_GRADIENT) -> str:
    """Interpolate a hex colour at position t∈[0,1] across ``stops``."""
    import math
    if not math.isfinite(t):
        return "#999999"
    t = min(max(t, 0.0), 1.0)
    seg = t * (len(stops) - 1)
    i = min(int(seg), len(stops) - 2)
    f = seg - i
    a = stops[i].lstrip("#")
    b = stops[i + 1].lstrip("#")
    rgb = [round(int(a[k:k+2], 16) + f * (int(b[k:k+2], 16) - int(a[k:k+2], 16))) for k in (0, 2, 4)]
    return "#%02x%02x%02x" % tuple(rgb)


# ---------------------------------------------------------------------------
# Cached loaders
# ---------------------------------------------------------------------------

@st.cache_data(ttl=600)
def _load_regions(_root: str, level: int, simplify_deg: float):
    from pathlib import Path
    gdf = load_hydrobasins_for_display(Path(_root), level=level, simplify_deg=simplify_deg)
    if gdf is None:
        return None
    fc = json.loads(gdf.to_json())
    for feat in fc["features"]:
        try:
            a = float(feat["properties"].get("area_km2") or 0.0)
        except (TypeError, ValueError):
            a = 0.0
        feat["properties"]["size_band"] = (
            "<100" if a < 100 else "100–500" if a < 500
            else "500–1500" if a < 1500 else ">1500"
        )
    return fc


@st.cache_data(ttl=600)
def _load_domain(_root: str):
    from pathlib import Path
    return load_domain_boundary(Path(_root))


@st.cache_data(ttl=600)
def _load_alps_perimeter(_root: str):
    """DEM-derived Alpine perimeter (alps-perimeter builder), if present."""
    import json as _json
    from pathlib import Path
    hits = sorted((Path(_root) / "data" / "regions").glob("alps_perimeter_dem_*.geojson"))
    if not hits:
        return None
    return _json.loads(hits[-1].read_text())


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------

root_str = str(project_root())
simplify_map = {"grob": 0.006, "mittel": 0.003, "fein": 0.001}

col_bm, col_lvl, col_col, col_det = st.columns([1.3, 1.0, 1.2, 1.0])
with col_bm:
    basemap_name = st.selectbox("Hintergrundkarte", list(BASEMAPS), index=0)
with col_lvl:
    level = st.selectbox("HydroBASINS-Level", [7, 8, 9], index=0)
with col_col:
    color_by = st.selectbox(
        "Färben nach",
        ["größe", "einzeln", "size_band"],
        format_func={
            "größe": "Größe (Verlauf)",
            "einzeln": "Einzeln (Graph-Coloring)",
            "size_band": "Größenklasse",
        }.get,
    )
with col_det:
    detail = st.select_slider("Detailgrad", ["grob", "mittel", "fein"], value="mittel")

simplify_deg = simplify_map[detail]
t1, t2 = st.columns(2)
with t1:
    show_domain = st.checkbox("Modelldomäne anzeigen", value=True)
with t2:
    show_alps = st.checkbox("Alpen-Perimeter (DEM)", value=True)

fc = _load_regions(root_str, level, simplify_deg)
domain_dict = _load_domain(root_str) if show_domain else None
alps_dict = _load_alps_perimeter(root_str) if show_alps else None


# ---------------------------------------------------------------------------
# Colour + style
# ---------------------------------------------------------------------------

def _area(props: dict) -> float:
    try:
        return float(props.get("area_km2") or 0.0)
    except (TypeError, ValueError):
        return 0.0


# Log-area range across the loaded regions, for the continuous size ramp.
import math as _math
_areas = [_area(f["properties"]) for f in fc["features"]] if fc else []
_logs = [_math.log10(a) for a in _areas if a > 0]
_LOG_LO, _LOG_HI = (min(_logs), max(_logs)) if _logs else (0.0, 1.0)


def _fill_color(props: dict) -> str:
    if color_by == "größe":
        a = _area(props)
        if a <= 0 or _LOG_HI <= _LOG_LO:
            return "#999999"
        t = (_math.log10(a) - _LOG_LO) / (_LOG_HI - _LOG_LO)
        return _ramp_color(t)
    if color_by == "size_band":
        return SIZE_PALETTE.get(props.get("size_band", ""), "#999999")
    return DISTINCT_PALETTE[int(props.get("color_idx", 0)) % len(DISTINCT_PALETTE)]


def _region_style(feature):
    return {
        "fillColor": _fill_color(feature["properties"]),
        "color": "#333333",
        "weight": 0.6,
        "fillOpacity": 0.50,
    }


# ---------------------------------------------------------------------------
# Map
# ---------------------------------------------------------------------------

bm = BASEMAPS[basemap_name]
m = folium.Map(location=[46.8, 11.5], zoom_start=6, tiles=None)
folium.TileLayer(
    tiles=bm["tiles"], attr=bm["attr"], name=basemap_name, max_zoom=bm["max_zoom"]
).add_to(m)

if fc is not None:
    sp = fc["features"][0]["properties"] if fc["features"] else {}
    tip = [f for f in ("HYBAS_ID", "PFAF_ID", "area_km2", "MAIN_BAS") if f in sp]
    folium.GeoJson(
        fc,
        name=f"HydroBASINS L{level}",
        style_function=_region_style,
        tooltip=folium.GeoJsonTooltip(
            fields=tip or ["HYBAS_ID"],
            aliases=[f.replace("_", " ") for f in (tip or ["HYBAS_ID"])],
            localize=True,
        ),
        smooth_factor=1.0,
    ).add_to(m)

if alps_dict is not None:
    folium.GeoJson(
        alps_dict,
        name="Alpen-Perimeter (DEM)",
        style_function=lambda _f: {"color": "#c62828", "weight": 2.2, "fillOpacity": 0},
    ).add_to(m)

if domain_dict is not None:
    folium.GeoJson(
        domain_dict,
        name="Modelldomäne",
        style_function=lambda _f: {"color": "#6a1b9a", "weight": 1.5,
                                    "fillOpacity": 0, "dashArray": "8 5"},
    ).add_to(m)

folium.LayerControl(collapsed=False).add_to(m)

# ---------------------------------------------------------------------------
# Status + render
# ---------------------------------------------------------------------------

if fc is None:
    st.warning(
        f"HydroBASINS L{level} noch nicht heruntergeladen. Erst ausführen:\n\n"
        "```\npython -c \"from pathlib import Path; "
        "from alptherm_icon.regions.basins import fetch_hydrobasins; "
        f"fetch_hydrobasins(Path('data/basins'), 'eu', {level})\"\n```"
    )
else:
    label = {"einzeln": "einzeln (Graph-Coloring)", "größe": "Größe (Verlauf)"}.get(color_by, "Größenklasse")
    st.markdown(f"**{len(fc['features'])} Regionen** · HydroBASINS L{level} · Färbung: {label}")

st_folium(m, width=None, height=650, returned_objects=[])

# ---------------------------------------------------------------------------
# Legend
# ---------------------------------------------------------------------------

if color_by == "size_band" and fc is not None:
    counts: dict[str, int] = {}
    for feat in fc["features"]:
        v = feat["properties"].get("size_band", "")
        counts[v] = counts.get(v, 0) + 1
    with st.expander("Legende + Statistik", expanded=True):
        cols = st.columns(len(SIZE_PALETTE))
        for col, (lbl, color) in zip(cols, SIZE_PALETTE.items()):
            col.markdown(
                f"<div style='display:flex;align-items:center;gap:6px'>"
                f"<div style='width:14px;height:14px;background:{color};"
                f"border:1px solid #333;border-radius:2px'></div>"
                f"<span>{lbl} km² ({counts.get(lbl, 0)})</span></div>",
                unsafe_allow_html=True,
            )
elif color_by == "größe" and fc is not None:
    lo, hi = round(10 ** _LOG_LO), round(10 ** _LOG_HI)
    bar = "".join(
        f"<div style='flex:1;height:14px;background:{_ramp_color(i / 9)}'></div>" for i in range(10)
    )
    with st.expander("Legende: Fläche (km², log-skaliert)", expanded=True):
        st.markdown(
            f"<div style='display:flex;align-items:center;gap:8px'>"
            f"<span>{lo}</span><div style='display:flex;flex:1;max-width:320px;"
            f"border:1px solid #333;border-radius:2px;overflow:hidden'>{bar}</div>"
            f"<span>{hi}</span></div>",
            unsafe_allow_html=True,
        )
elif color_by == "einzeln" and fc is not None:
    st.caption("Benachbarte Einzugsgebiete bekommen via Graph-Coloring unterschiedliche Farben (≤ 9 Farben).")
