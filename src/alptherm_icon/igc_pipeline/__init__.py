"""Komponente D — IGC-Validierungspipeline (plan §6).

Primary source WeGlide (REST API, api.weglide.org); OLC as fallback.
Detect circling phases per Richter 2011 (curvature on 2-min window,
exclude tow/motor-glider/wave/ridge), assign to region by circling
centroid, aggregate per (region, day, 30-min bin): N circles,
median & Q90 of v_climb, max altitude.

WeGlide ToS: read-only OK, request API key proactively to avoid
cloud-IP firewall; aggressive local caching of IGC files.
"""
