"""Sanity tests for region polygon loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from alptherm_icon.regions.polygon import load_region

PILOT_GEOJSON = Path(__file__).resolve().parents[1] / "configs" / "regions" / "inntal_steinberge.geojson"


def test_load_pilot_region_by_name() -> None:
    geom, props = load_region(PILOT_GEOJSON, name="inntal_steinberge")
    assert geom.is_valid
    assert not geom.is_empty
    # Roughly the Lower Inn valley / Steinberge area
    minx, miny, maxx, maxy = geom.bounds
    assert 11.0 < minx < 12.0
    assert 47.0 < miny < 48.0
    assert 12.0 < maxx < 13.0
    assert 47.0 < maxy < 48.0
    assert props["status"] == "placeholder"


def test_load_region_missing_name_raises() -> None:
    with pytest.raises(ValueError, match="no feature with name"):
        load_region(PILOT_GEOJSON, name="does_not_exist")
