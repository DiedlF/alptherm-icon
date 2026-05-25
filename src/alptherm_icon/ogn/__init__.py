"""OGN APRS-Stream-Mitschnitt (Plan §6.6 + §9.5).

Open Glider Network ist die dritte flüchtige Datenquelle neben ICON
und (optional) Live-Wetterstationen: ein durchgehender APRS-Stream
mit Positions-Beacons von Segelflugzeugen, Gleitschirmen und FLARM-
trägern. Es gibt **keine Langzeit-Historie** zum Nachladen — was nicht
mitgeschnitten wird, ist weg.

Architekturprinzipien (vgl. Plan §9.5):

1. *Rohdaten behalten, nicht live-aggregieren.* Jede APRS-Zeile wird
   unverändert in ein zeit-partitioniertes Tageslog geschrieben.
   Aircraft- *und* Receiver-Beacons, kein Typ-Filter. Parsen,
   Klassifizieren, Kreisflug-Detektion passieren in einer separaten
   Auswerteschicht — dieselbe Zwei-Stufen-Philosophie wie ICON
   (GRIB2 roh → Zarr) und IGC (Files cachen → Pipeline).

2. *Heartbeat statt Polling.* Der Live-Konsument schreibt seinen
   Status in ``data/status/ogn-stream.json``; Dashboard und Alerter
   lesen daraus, ohne den Stream selbst zu kennen.

3. *Geofilter serverseitig.* Nur die Alpen-Bbox abonnieren (Port
   14580, Range-Filter), nicht alles → reduziert das übertragene
   Volumen drastisch.

Der Logger ist als long-running Daemon gedacht (systemd, nicht cron) —
ein Cron-Restart würde die Aufzeichnung jede Minute unterbrechen.
"""

from alptherm_icon.ogn.writer import DailyRawLogWriter, raw_log_path

__all__ = ["DailyRawLogWriter", "raw_log_path"]
