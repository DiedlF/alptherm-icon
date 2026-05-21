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

| Komp. | Aufgabe                       | Source           |
|-------|-------------------------------|------------------|
| A     | Regionsgeometrie + AHD        | `src/alptherm_icon/regions/`       |
| B     | ICON-Datenpipeline            | `src/alptherm_icon/icon_pipeline/` |
| C     | Modellkern (1D-Konvektion)    | `src/alptherm_icon/model/`         |
| D     | IGC-Validierungspipeline      | `src/alptherm_icon/igc_pipeline/`  |
| E     | Validierung + Parametertuning | `src/alptherm_icon/validation/`    |

A and B are independent. C depends on A + B. D is independent. E joins
C + D and cross-references ICON diagnostics (HBAS_SC, HTOP_DC).

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
│   ├── icon/            # ICON GRIB2 (rolling cache)
│   ├── igc/             # IGC-Files (cache)
│   └── aggregates/      # Flugaggregate (Parquet)
├── configs/regions/     # Region definitions
├── notebooks/           # Explorative Analyse
└── tests/
```

## Quickstart

```bash
pip install -e .[dev]
pytest
```

## References

See plan §10.2 for the full bibliography. Core papers:

- Liechti & Neininger (1994), *Technical Soaring* 18(3).
- Richter-Trummer (2011), BA Innsbruck — ALPTHERM verification on flight data.
- Whiteman (2000), *Mountain Meteorology* — physical guardrails for region cuts.

## License

TBD.
