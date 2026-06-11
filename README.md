# alptherm-icon

Reimplementation of the ALPTHERM / Regtherm thermal forecasting model
(Liechti & Neininger 1993; Liechti 2002) on top of DWD ICON-D2 / ICON-EU,
validated against IGC flight data (WeGlide).

Standalone research project. Long-term goal is to feed
[skyview](../skyview/) with calibrated, region-scale thermal forecasts —
but only after Komp. E (parameter tuning) reaches the success metrics in
§7.5 of the implementation plan. Re-evaluate skyview integration after M6.

## Components

Per `docs/Implementierungsplan_ALPTHERM_ICON.pdf` (lives in the skyview
repo for now):

| Komp. | Aufgabe                       | Source           | Status |
|-------|-------------------------------|------------------|--------|
| A     | Regionsgeometrie + AHD        | `src/alptherm_icon/regions/`       | working |
| B     | ICON-Datenpipeline + Archiv   | `src/alptherm_icon/icon_pipeline/`, `archive/` | operational |
| C     | Modellkern (1D-Konvektion)    | `src/alptherm_icon/model/`         | v0.4 — calibrated |
| D     | OGN/IGC-Validierungspipeline  | `src/alptherm_icon/igc_pipeline/`, `ogn/`  | working |
| E     | Validierung + Parametertuning | `src/alptherm_icon/validation/`    | **not started** |

A and B are independent. C depends on A + B. D is independent. E joins
C + D and cross-references ICON diagnostics (HBAS_SC, HTOP_DC).

## Status (Stand)

- **A — Regions / AHD.** Regions are HydroBASINS catchments (default **level 7**,
  watershed-aligned); AHD (`S_G(z)`/`V_a(z)`) per region from the Copernicus DEM. A
  DEM-derived single Alpine perimeter is available (`regions alps-perimeter`,
  high-elevation outline with interior valleys filled). Earlier orographic-grouping
  experiments (SOIUSA/AVE) were dropped in favour of plain HydroBASINS.
- **B — ICON pipeline + archive.** DWD ICON-D2 surface/profile fetch with correct
  **de-averaging** of the mean-since-init flux fields (DWD DB Reference §7.1.1) and
  the downward-positive sign convention; daily archive mirrored to Hetzner S3 (WORM
  raw + reproducible Zarr).
- **C — Model kernel.** Versioned: v0.1 bulk mixed-layer → v0.2 ICON fluxes → v0.3
  bin-wise parcel theory (eqs 11–19) → **v0.4 calibrated** against Liechti's Fig-4
  subsidence threshold and Table-2 onset/base. CLI `model run … --model {bulk,parcel}`.
  See `docs/komp_c_alptherm_spec.md`.
- **D — OGN/IGC pipeline.** Live OGN capture (immutable daily raw logs, §9.5) →
  **clean-track layer** (GPS-time order, per-second receiver-median dedup, jump
  rejection) → circle detection (heading integral **+ confinement+climb** for
  directional/undersampled reception) → daily thermal parquets. A **clean-track
  cache** (`igc_pipeline cache-clean`) makes re-runs ~15× faster.
- **E — Validation / tuning.** Stub only — day-type classification and the parameter
  fit (ΔT₀, P₀, entrain/detrain, …) against IGC max heights + ICON diagnostics
  (plan §7) are not built yet. Main open work toward the M6 success metrics.

A **Streamlit dashboard** (`src/alptherm_icon/dashboard/`) visualises regions
(+ DEM perimeter), the ICON archive, and detected thermals (per-flight focus,
altitude profile, cleaned tracks).

## Pilot region

Start: Inntal / Steinberge (plan §10.5 — Richter 2011 reference data
exists for this area). See `configs/regions/inntal_steinberge.geojson`
(placeholder bbox — refine with Pfafstetter HydroBASINS catchments).

## Stack

Python 3.11+. Open source throughout:

- **Numerik:** numpy, scipy, xarray
- **Geo:** geopandas, rasterio, shapely, pyproj
- **Regionalisierung:** pysal (spopt: max-p-regions, skater), HydroBASINS / EU-Hydro
- **NWP:** cfgrib (eccodes), herbie
- **IGC:** aerofiles, weglide-python-client
- **Optimization:** scikit-optimize, optuna
- **Viz:** matplotlib, plotly, folium
- **Storage:** NetCDF (xarray), Parquet (flight aggregates)

## Layout

Deviates slightly from plan §10.3 — uses a single `alptherm_icon` package
under `src/` (src-layout) instead of flat top-level component dirs. Same
component split, cleaner imports.

```
alptherm-icon/
├── src/alptherm_icon/
│   ├── regions/         # Komp. A
│   ├── icon_pipeline/   # Komp. B
│   ├── model/           # Komp. C
│   ├── igc_pipeline/    # Komp. D
│   └── validation/      # Komp. E
├── data/                # Caches + outputs (gitignored)
│   ├── dem/             # Copernicus DEM (statisch)
│   ├── regions/         # GeoJSON-Polygone + AHD-NetCDFs
│   ├── basins/          # HydroBASINS shapefiles
│   ├── icon/            # ICON GRIB2 (rolling cache)
│   ├── ogn/raw/         # immutable daily OGN logs (WORM, §9.5)
│   ├── ogn/clean/       # derived clean-track cache (regenerable)
│   ├── thermals/        # detected thermals per day (Parquet)
│   └── model/           # Komp. C output (CBL / parcel NetCDF)
├── configs/regions/     # Region definitions
├── notebooks/           # Explorative Analyse
└── tests/
```

## Quickstart

```bash
pip install -e .[dev]
pytest

# Komp. A — regions + perimeter
python -m alptherm_icon.regions alps-perimeter            # DEM-derived Alpine outline

# Komp. B — fetch ICON-D2 (de-averaged surface fluxes + profile)
python -m alptherm_icon.icon_pipeline fetch <region> --init YYYYMMDDHH

# Komp. C — run the convection kernel
python -m alptherm_icon.model run <region> --init YYYYMMDDHH --model parcel

# Komp. D — OGN thermal detection (uses/creates the clean-track cache)
python -m alptherm_icon.igc_pipeline cache-clean --day YYYY-MM-DD   # optional pre-warm
python -m alptherm_icon.igc_pipeline detect     --day YYYY-MM-DD

# Dashboard
streamlit run src/alptherm_icon/dashboard/app.py
```

## References

See plan §10.2 for the full bibliography. Core papers:

- Liechti & Neininger (1994), *Technical Soaring* 18(3).
- Richter-Trummer (2011), BA Innsbruck — ALPTHERM verification on flight data.
- Whiteman (2000), *Mountain Meteorology* — physical guardrails for region cuts.

## License

TBD.
