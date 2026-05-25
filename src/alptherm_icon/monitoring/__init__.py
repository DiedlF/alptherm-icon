"""Heartbeat / status layer for archive + OGN + future jobs (plan §10.1).

Sammel-Jobs schreiben strukturierte Status-JSONs in ``data/status/{job}.json``;
das Monitoring-Dashboard und Alerting lesen daraus. Strikt einseitig:
schreibende Komponenten kennen das Dashboard nicht, der Reader hängt nur
am Filesystem.
"""

from alptherm_icon.monitoring.heartbeat import (
    HeartbeatStatus,
    read,
    read_all,
    status_path,
    write,
)

__all__ = ["HeartbeatStatus", "read", "read_all", "status_path", "write"]
