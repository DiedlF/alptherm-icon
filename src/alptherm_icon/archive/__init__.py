"""ICON-D2 archive cronjob (plan M0, §9).

DWD Open Data only keeps a rolling 2-day window of GRIB2 files. Every
good thermal day not captured natively is unrecoverable for later
validation. This package implements the two-tier mitigation from §9.2:

- Tier 1 (unconditional): ~15 surface/diagnostic variables every day.
- Tier 2 (gut-day-triggered): full model-level T/QV/U/V/W/HHL profiles
  whenever the same run's CAPE / radiation / precip suggest a usable
  thermal day.

The archive is deliberately decoupled from the rest of the pipeline:
it runs from day 1, before Komp. C exists, and writes raw GRIB2 plus
a daily Zarr stack that later pipeline stages can read from instead
of going back to opendata.dwd.de.
"""
