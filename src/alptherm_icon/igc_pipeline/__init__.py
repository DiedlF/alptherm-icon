"""Komponente D — IGC-Validierungspipeline (plan §6).

Primary source WeGlide (REST API, api.weglide.org); OLC as fallback.
Detect circling phases per Richter 2011 (curvature on 2-min window,
exclude tow/motor-glider/wave/ridge), assign to region by circling
centroid, aggregate per (region, day, 30-min bin): N circles,
median & Q90 of v_climb, max altitude.

WeGlide ToS: read-only OK, request API key proactively to avoid
cloud-IP firewall; aggressive local caching of IGC files.
"""

from alptherm_icon.igc_pipeline.circling import (
    CirclingParams,
    ThermalPhase,
    detect_thermals,
)
from alptherm_icon.igc_pipeline.ogn_tracks import assemble_tracks
from alptherm_icon.igc_pipeline.track import Fix, Track

__all__ = [
    "CirclingParams",
    "Fix",
    "ThermalPhase",
    "Track",
    "assemble_tracks",
    "detect_thermals",
]
