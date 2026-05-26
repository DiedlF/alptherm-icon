"""Monitoring dashboard (Plan §10).

Reiner Leser-Streamlit-App über die Sammel-Jobs (Plan §10.1):

- ``app.py`` — Landing-Page mit 1-Glance-Übersicht
- ``pages/01_Liveness.py`` — Heartbeat-Detail je Job (Plan §10.2 Ebene 1)
- ``pages/02_Bestand.py`` — Manifest + Storage + OGN-Inventory (Ebene 2)

Schreibt nie, triggert nie, hängt nur an den Filesystem-Artefakten der
Archive-, OGN- und Heartbeat-Schichten. Ein Crash hier kann die
Sammlung nie gefährden.
"""

from alptherm_icon.dashboard.data_loader import (
    AlertSummary,
    ManifestSummary,
    OgnDayStats,
    StorageStats,
    load_alerts,
    load_heartbeats,
    load_manifest_summary,
    load_ogn_inventory,
    load_storage,
    project_root,
)

__all__ = [
    "AlertSummary",
    "ManifestSummary",
    "OgnDayStats",
    "StorageStats",
    "load_alerts",
    "load_heartbeats",
    "load_manifest_summary",
    "load_ogn_inventory",
    "load_storage",
    "project_root",
]
