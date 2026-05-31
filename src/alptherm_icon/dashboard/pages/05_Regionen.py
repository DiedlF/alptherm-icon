"""Regionen-Karte — interaktive Darstellung der ALPTHERM-Regionen.

Unterstützt zwei Quellen:
  • v2 (SOIUSA-basiert, plan §3.1 neuer Ansatz) — bevorzugt
  • v1 (HydroBASINS-Gerüst, plan §3.1 alter Ansatz) — Fallback

Zusätzliche optionale Layer:
  • HydroBASINS L8 Einzugsgebiete (geometrisches Baumaterial)
  • SOIUSA-Quellgruppen (orografische Sollstruktur aus OSM)
  • Domänenrand / Alpen-Perimeter als Linie
"""

from __future__ import annotations

import json

import folium
import streamlit as st
from streamlit_folium import st_folium

from alptherm_icon.dashboard.data_loader import (
    load_alpine_perimeter,
    load_domain_boundary,
    load_hydrobasins_for_display,
    load_regions,
    load_regions_v2,
    load_soiusa_groups,
    project_root,
)

st.set_page_config(page_title="Regionen", page_icon="🗺️", layout="wide")
st.title("🗺️ ALPTHERM-Regionen")

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

TERRAIN_PALETTE = {
    "alpine":       "#1565C0",
    "mittelgebirge": "#2E7D32",
    "flachland":    "#C8A063",
    "non_alpine":   "#9E9E9E",
}

HABITAT_PALETTE = {
    "alpine":  "#2e7d32",
    "vorland": "#c8a063",
}

BAND_PALETTE = {
    "whole":       "#9e9e9e",
    "flachland":   "#c8a063",
    "voralpen":    "#7cb342",
    "hochgebirge": "#455a64",
}

SIZE_PALETTE = {
    "<100":      "#bbbbbb",
    "100–500":   "#90caf9",
    "500–1500":  "#1e88e5",
    ">1500":     "#fb8c00",
}


# ---------------------------------------------------------------------------
# Cached loaders
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def _load_v2(_root: str, simplify_deg: float):
    from pathlib import Path
    gdf = load_regions_v2(Path(_root), simplify_deg=simplify_deg)
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
def _load_v1(_root: str, simplify_deg: float):
    from pathlib import Path
    gdf = load_regions(Path(_root), simplify_deg=simplify_deg)
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


@st.cache_data(ttl=600)
def _load_hydrobasins(_root: str, level: int, simplify_deg: float):
    from pathlib import Path
    gdf = load_hydrobasins_for_display(Path(_root), level=level, simplify_deg=simplify_deg)
    return None if gdf is None else json.loads(gdf.to_json())


@st.cache_data(ttl=600)
def _load_soiusa(_root: str, simplify_deg: float):
    from pathlib import Path
    gdf = load_soiusa_groups(Path(_root), simplify_deg=simplify_deg)
    return None if gdf is None else json.loads(gdf.to_json())


@st.cache_data(ttl=600)
def _load_perimeter_v2(_root: str):
    """Dissolved boundary of all alpine (terrain_type='alpine') v2 regions."""
    import shapely.geometry
    import shapely.wkb
    from pathlib import Path
    root = Path(_root)
    cache = root / "data" / "regions" / "alpine_v2_perimeter.wkb"
    src = root / "data" / "regions" / "alpine_v2_regions_annotated.geojson"
    if not src.exists():
        src = root / "data" / "regions" / "alpine_v2_regions.geojson"
    if not src.exists():
        return None
    if cache.exists() and cache.stat().st_mtime >= src.stat().st_mtime:
        return shapely.geometry.mapping(shapely.wkb.loads(cache.read_bytes()))
    import geopandas as gpd
    gdf = gpd.read_file(src)
    alpine = gdf[gdf.get("terrain_type", "alpine") == "alpine"]
    if alpine.empty:
        return None
    dissolved = alpine.geometry.union_all().simplify(0.004).buffer(0)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(shapely.wkb.dumps(dissolved))
    except OSError:
        pass
    return shapely.geometry.mapping(dissolved)


@st.cache_data(ttl=600)
def _load_perimeter_v1(_root: str):
    import shapely.geometry
    from pathlib import Path
    geom = load_alpine_perimeter(Path(_root))
    return None if geom is None else shapely.geometry.mapping(geom.boundary)


@st.cache_data(ttl=600)
def _load_domain(_root: str):
    from pathlib import Path
    return load_domain_boundary(Path(_root))


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------

root = project_root()
root_str = str(root)

simplify_map = {"grob": 0.006, "mittel": 0.003, "fein": 0.001}

col_bm, col_src, col_col, col_det = st.columns([1.2, 1.1, 1.3, 1])
with col_bm:
    basemap_name = st.selectbox("Hintergrundkarte", list(BASEMAPS), index=0)
with col_src:
    region_source = st.radio(
        "Regionen",
        ["v2 (SOIUSA)", "v1 (HydroBASINS-Gerüst)", "Keine"],
        horizontal=True,
    )
with col_det:
    detail = st.select_slider("Detailgrad", ["grob", "mittel", "fein"], value="mittel")

simplify_deg = simplify_map[detail]

# Load the chosen region data to know which color fields exist.
fc = None
color_fields_available = ["einzeln", "size_band"]
if region_source == "v2 (SOIUSA)":
    fc = _load_v2(root_str, simplify_deg)
    if fc and fc["features"]:
        props = fc["features"][0]["properties"]
        if "terrain_type" in props:
            color_fields_available.insert(1, "terrain_type")
        if "soiusa_name" in props:
            color_fields_available.insert(2, "soiusa_name")
elif region_source == "v1 (HydroBASINS-Gerüst)":
    fc = _load_v1(root_str, simplify_deg)
    if fc and fc["features"]:
        props = fc["features"][0]["properties"]
        if "habitat_class" in props:
            color_fields_available.insert(1, "habitat_class")
        if "band" in props:
            color_fields_available.insert(2, "band")

color_labels = {
    "einzeln":      "Einzeln (Graph-Coloring)",
    "terrain_type": "Geländetyp",
    "soiusa_name":  "SOIUSA-Gruppe",
    "habitat_class":"Habitat (alpine/vorland)",
    "band":         "Segment-Band",
    "size_band":    "Größenklasse",
}

with col_col:
    color_by = st.selectbox(
        "Färben nach",
        color_fields_available,
        format_func=color_labels.get,
        disabled=(fc is None),
    )

# Additional layer toggles.
c1, c2, c3, c4 = st.columns(4)
with c1:
    show_hb = st.checkbox("HydroBASINS L8", value=True)
with c2:
    hb_level = st.selectbox("HydroBASINS-Level", [7, 8, 9], index=1, disabled=not show_hb)
with c3:
    show_soiusa = st.checkbox("SOIUSA-Quellgruppen", value=False)
with c4:
    show_perimeter = st.checkbox("Alpen-Perimeter", value=True)

# ---------------------------------------------------------------------------
# Data loading for optional layers
# ---------------------------------------------------------------------------

hb_fc = _load_hydrobasins(root_str, hb_level, simplify_deg + 0.001) if show_hb else None
soiusa_fc = _load_soiusa(root_str, simplify_deg) if show_soiusa else None

if region_source == "v2 (SOIUSA)":
    perimeter_dict = _load_perimeter_v2(root_str) if show_perimeter else None
    domain_dict = _load_domain(root_str)
else:
    perimeter_dict = _load_perimeter_v1(root_str) if show_perimeter else None
    domain_dict = None

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------

def _fill_color(props: dict) -> str:
    if color_by == "einzeln":
        return DISTINCT_PALETTE[int(props.get("color_idx", 0)) % len(DISTINCT_PALETTE)]
    if color_by == "terrain_type":
        return TERRAIN_PALETTE.get(props.get("terrain_type", ""), "#999999")
    if color_by == "soiusa_name":
        return DISTINCT_PALETTE[int(props.get("color_idx", 0)) % len(DISTINCT_PALETTE)]
    if color_by == "habitat_class":
        return HABITAT_PALETTE.get(props.get("habitat_class", ""), "#999999")
    if color_by == "band":
        return BAND_PALETTE.get(props.get("band", ""), "#999999")
    if color_by == "size_band":
        return SIZE_PALETTE.get(props.get("size_band", ""), "#999999")
    return "#999999"


def _region_style(feature):
    return {
        "fillColor": _fill_color(feature["properties"]),
        "color": "#333333",
        "weight": 0.6,
        "fillOpacity": 0.50,
    }


def _hb_style(_feature):
    return {
        "fillColor": "#78909C",
        "color": "#546E7A",
        "weight": 0.8,
        "fillOpacity": 0.10,
        "dashArray": "3 4",
    }


def _soiusa_style(_feature):
    return {
        "fillColor": "transparent",
        "color": "#e65100",
        "weight": 1.8,
        "fillOpacity": 0,
        "dashArray": "6 4",
    }


# ---------------------------------------------------------------------------
# Build tooltip field lists from available properties
# ---------------------------------------------------------------------------

def _tooltip_fields(sample_props: dict, source: str) -> tuple[list, list]:
    if source == "v2 (SOIUSA)":
        candidates = [
            ("region_id",    "Region-ID"),
            ("terrain_type", "Geländetyp"),
            ("soiusa_name",  "SOIUSA-Gruppe"),
            ("soiusa_code",  "SOIUSA-Code"),
            ("n_basins",     "# Basins"),
            ("area_km2",     "Fläche (km²)"),
            ("mean_elev_m",  "Mittl. Höhe (m)"),
        ]
    else:
        candidates = [
            ("region_id",    "Region-ID"),
            ("band",         "Band"),
            ("habitat_class","Habitat"),
            ("area_km2",     "Fläche (km²)"),
            ("mean_elev_m",  "Mittl. Höhe (m)"),
        ]
    fields   = [f for f, _ in candidates if f in sample_props]
    aliases  = [a for f, a in candidates if f in sample_props]
    return fields, aliases


# ---------------------------------------------------------------------------
# Map
# ---------------------------------------------------------------------------

bm = BASEMAPS[basemap_name]
m = folium.Map(location=[46.8, 11.5], zoom_start=6, tiles=None)
folium.TileLayer(
    tiles=bm["tiles"], attr=bm["attr"], name=basemap_name, max_zoom=bm["max_zoom"]
).add_to(m)

# HydroBASINS layer — subtle outlines showing the building blocks.
if show_hb and hb_fc is not None:
    hb_tooltip_fields = [
        f for f in ("HYBAS_ID", "PFAF_ID", "area_km2", "MAIN_BAS")
        if f in (hb_fc["features"][0]["properties"] if hb_fc["features"] else {})
    ]
    folium.GeoJson(
        hb_fc,
        name=f"HydroBASINS L{hb_level}",
        style_function=_hb_style,
        tooltip=folium.GeoJsonTooltip(
            fields=hb_tooltip_fields or ["HYBAS_ID"],
            aliases=[f.replace("_", " ") for f in (hb_tooltip_fields or ["HYBAS_ID"])],
            localize=True,
        ),
        smooth_factor=1.5,
    ).add_to(m)
elif show_hb and hb_fc is None:
    st.sidebar.warning(
        f"HydroBASINS L{hb_level} noch nicht heruntergeladen. "
        "Erst `python -m alptherm_icon.regions alpine-v2` ausführen."
    )

# SOIUSA source groups — orographic should-be structure as orange dashed outlines.
if show_soiusa and soiusa_fc is not None:
    sp = soiusa_fc["features"][0]["properties"] if soiusa_fc["features"] else {}
    soiusa_fields = [f for f in ("soiusa_name", "name_de", "soiusa_code", "osm_id") if f in sp]
    folium.GeoJson(
        soiusa_fc,
        name="SOIUSA-Quellgruppen (OSM)",
        style_function=_soiusa_style,
        tooltip=folium.GeoJsonTooltip(
            fields=soiusa_fields or ["name"],
            aliases=[f.replace("_", " ").title() for f in (soiusa_fields or ["name"])],
        ),
        smooth_factor=1.5,
    ).add_to(m)
elif show_soiusa and soiusa_fc is None:
    st.sidebar.info(
        "soiusa_groups.geojson nicht gefunden. "
        "Erst `python -m alptherm_icon.regions soiusa-groups` ausführen."
    )

# ALPTHERM regions — the main coloured layer.
if fc is not None:
    features = fc["features"]
    sp = features[0]["properties"] if features else {}
    tip_fields, tip_aliases = _tooltip_fields(sp, region_source)
    folium.GeoJson(
        fc,
        name=f"ALPTHERM-Regionen ({region_source})",
        style_function=_region_style,
        tooltip=folium.GeoJsonTooltip(
            fields=tip_fields,
            aliases=tip_aliases,
            localize=True,
        ),
        smooth_factor=1.0,
    ).add_to(m)

# Alpine/SOIUSA perimeter as a bold red line.
if perimeter_dict is not None:
    folium.GeoJson(
        perimeter_dict,
        name="Alpen-Perimeter",
        style_function=lambda _f: {"color": "#c62828", "weight": 2.0, "fillOpacity": 0},
    ).add_to(m)

# Outer model domain boundary.
if domain_dict is not None:
    folium.GeoJson(
        domain_dict,
        name="Modelldomäne",
        style_function=lambda _f: {"color": "#6a1b9a", "weight": 1.5,
                                    "fillOpacity": 0, "dashArray": "8 5"},
    ).add_to(m)

folium.LayerControl(collapsed=False).add_to(m)

# ---------------------------------------------------------------------------
# Status caption and map render
# ---------------------------------------------------------------------------

if fc is None and region_source != "Keine":
    if region_source == "v2 (SOIUSA)":
        st.warning(
            "Noch kein v2-Regions-GeoJSON vorhanden. Pipeline ausführen:\n\n"
            "```\npython -m alptherm_icon.regions soiusa-groups\n"
            "python -m alptherm_icon.regions alpine-v2\n"
            "python -m alptherm_icon.regions alpine-v2-dem\n"
            "python -m alptherm_icon.regions alpine-v2-ahd --edges\n```"
        )
    else:
        st.warning(
            "Noch kein v1-Regions-GeoJSON. Pipeline ausführen:\n\n"
            "```\npython -m alptherm_icon.regions alpine-v0\n"
            "python -m alptherm_icon.regions alpine-v0-dem\n"
            "python -m alptherm_icon.regions alpine-v0-ahd\n"
            "python -m alptherm_icon.regions alpine-v1\n```"
        )

n_regions = len(fc["features"]) if fc else 0
n_hb = len(hb_fc["features"]) if hb_fc else 0
status_parts = []
if fc:
    label = "einzeln (Graph-Coloring)" if color_by in ("einzeln", "soiusa_name") else f"`{color_by}`"
    status_parts.append(f"**{n_regions} Regionen** ({region_source}) · Färbung: {label}")
if hb_fc:
    status_parts.append(f"**{n_hb} HydroBASINS L{hb_level}**")
if status_parts:
    st.markdown(" · ".join(status_parts))

st_folium(m, width=None, height=650, returned_objects=[])

# ---------------------------------------------------------------------------
# Legend
# ---------------------------------------------------------------------------

palette_map = {
    "terrain_type":  TERRAIN_PALETTE,
    "habitat_class": HABITAT_PALETTE,
    "band":          BAND_PALETTE,
    "size_band":     SIZE_PALETTE,
}

if color_by in palette_map and fc is not None:
    features = fc["features"]
    palette = palette_map[color_by]
    counts: dict[str, int] = {}
    for feat in features:
        v = feat["properties"].get(color_by, "")
        counts[v] = counts.get(v, 0) + 1

    with st.expander("Legende + Statistik", expanded=True):
        cols = st.columns(max(len(palette), 1))
        for col, (lbl, color) in zip(cols, palette.items()):
            col.markdown(
                f"<div style='display:flex;align-items:center;gap:6px'>"
                f"<div style='width:14px;height:14px;background:{color};"
                f"border:1px solid #333;border-radius:2px'></div>"
                f"<span>{lbl} ({counts.get(lbl, 0)})</span></div>",
                unsafe_allow_html=True,
            )
elif color_by in ("einzeln", "soiusa_name") and fc is not None:
    st.caption("Benachbarte Regionen bekommen via Graph-Coloring unterschiedliche Farben (≤ 9 Farben).")

with st.expander("Layer-Erklärung"):
    st.markdown(
        "| Layer | Stil | Bedeutung |\n"
        "|---|---|---|\n"
        "| **ALPTHERM-Regionen** | farbige Füllung | Die thermischen Modellregionen |\n"
        f"| **HydroBASINS L{hb_level}** | graue gestrichelte Umrisse | Geometrisches Baumaterial (Einzugsgebiete) |\n"
        "| **SOIUSA-Quellgruppen** | orangefarbene gestrichelte Linie | Orografische Sollstruktur (OSM mountain_range) |\n"
        "| **Alpen-Perimeter** | roter Umriss | Grenze alpine/nicht-alpine Regionen |\n"
        "| **Modelldomäne** | lila gestrichelt | Äußere Grenze des Modellgebiets (Donau/Schwarzwald) |\n"
    )
