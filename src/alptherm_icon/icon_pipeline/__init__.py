"""Komponente B — ICON-Datenpipeline (plan §4).

Pulls DWD ICON-D2 / ICON-EU GRIB2, filters to needed variables (see
§4.1), spatially extracts all grid points inside each region polygon,
interpolates to a uniform 100 m height grid, and aggregates to regional
profiles + spread measures (§4.3) per day per region.

Build a rolling own archive starting day 1 (§4.5) — DWD Open Data only
keeps a 2-day window.
"""
