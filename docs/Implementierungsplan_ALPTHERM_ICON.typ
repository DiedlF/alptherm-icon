// =============================================================================
// Implementierungsplan: ALPTHERM-Reimplementierung für den Alpenraum
// auf Basis von ICON-D2/EU mit Validierung gegen WeGlide/OLC IGC-Daten
//
// Kompilieren:  typst compile Implementierungsplan_ALPTHERM_ICON.typ
// Live-Preview: typst watch  Implementierungsplan_ALPTHERM_ICON.typ
// =============================================================================

#set document(title: "Implementierungsplan ALPTHERM-ICON")
#set page(
  paper: "a4",
  margin: (x: 2cm, y: 2cm),
  numbering: "1",
)
#set text(font: ("Libertinus Serif", "DejaVu Sans"), size: 10pt, lang: "de")
#set par(justify: true, leading: 0.65em)

// DWD/ICON-Variablennamen (GROSSBUCHSTABEN_MIT_UNTERSTRICH) automatisch als
// Raw-Text rendern — verhindert ungewollte Subscript-Interpretation des "_".
// Greift NICHT in Math-Ausdrücken ($...$), wo Subscripts gewollt sind.
#show regex("\b[A-Z][A-Z0-9]*(_[A-Z0-9]+)+\b"): it => raw(it.text)

// --- Farbschema ---
#let accent     = rgb("#1a3a5c")
#let accent2    = rgb("#2d5a87")
#let zebra      = rgb("#f7f9fc")
#let headfill   = rgb("#e8eef5")
#let notefill   = rgb("#f9f4e8")
#let notestroke = rgb("#d4b870")

// --- Überschriften-Styling ---
#set heading(numbering: none)
#show heading.where(level: 1): it => [
  #v(0.6em)
  #text(fill: accent, weight: "bold", size: 15pt)[#it.body]
  #v(0.2em)
]
#show heading.where(level: 2): it => [
  #v(0.3em)
  #text(fill: accent2, weight: "bold", size: 12pt)[#it.body]
]
#show heading.where(level: 3): it => [
  #v(0.2em)
  #text(fill: rgb("#333333"), weight: "bold", size: 10.5pt)[#it.body]
]

// --- Info-Box (entspricht der goldenen "note"-Box im PDF) ---
#let note(body) = block(
  fill: notefill,
  stroke: 0.5pt + notestroke,
  inset: 8pt,
  radius: 2pt,
  width: 100%,
  text(size: 9.5pt, fill: rgb("#444444"), body),
)

// --- Code-/Formel-Block ---
#let formula(body) = block(
  fill: rgb("#f4f4f4"),
  stroke: 0.5pt + rgb("#dddddd"),
  inset: 6pt,
  radius: 2pt,
  width: 100%,
  text(font: "DejaVu Sans Mono", size: 9pt, body),
)

// --- Tabellen-Helfer: Kopfzeile fett auf hellblau, Zebra-Streifen ---
#let tbl(columns: auto, header: (), ..rows) = {
  let datarows = rows.pos()
  table(
    columns: columns,
    stroke: none,
    align: left + horizon,
    inset: (x: 7pt, y: 6pt),
    // Kopfzeile = Zeile 0, danach Zebra
    fill: (_, row) => if row == 0 { headfill }
                      else if calc.even(row) { zebra }
                      else { white },
    table.hline(stroke: 0.8pt + accent),
    table.header(
      ..header.map(c => text(weight: "bold", fill: accent, size: 8.5pt, c))
    ),
    table.hline(stroke: 0.6pt + accent),
    ..datarows.flatten().map(c => text(size: 8.5pt, c)),
    table.hline(stroke: 0.8pt + accent),
  )
}

// =============================================================================
// TITEL
// =============================================================================
#align(left)[
  #text(size: 20pt, weight: "bold", fill: accent)[Implementierungsplan]
  #v(0.2em)
  #text(size: 11pt, style: "italic", fill: rgb("#555555"))[
    Reimplementierung des ALPTHERM/Regtherm-Modellansatzes für den Alpenraum
    auf Basis von ICON-D2 / ICON-EU, Validierung gegen IGC-Streckenflugdaten
  ]
]
#v(0.5em)
#line(length: 100%, stroke: 0.5pt + rgb("#cccccc"))

// =============================================================================
= 1. Zielsetzung und Abgrenzung
// =============================================================================
Ziel des Projekts ist die Reimplementierung eines Thermikprognose-Modells im Sinne
von Liechti & Neininger (1993) für den Alpenraum. Die Originalphysik des Modells —
Volumeneffekt, Area-Height-Distribution, 1D-Energiebilanz pro Region — wird
beibehalten, jedoch werden alle 1994 noch empirisch parametrisierten Größen
(Strahlungstransmission, Energieaufteilung, Bodentemperatur, großräumige Subsidenz)
durch direkte ICON-Diagnostiken ersetzt. Das Modell agiert damit faktisch als
topographisch hochaufgelöster Subgrid-Konvektionspostprozessor über ICON.

*Nicht Ziel:*
- Ein operationelles Produkt auf Augenhöhe mit XC Therm (30 Jahre Tuning fehlen).
- Reverse Engineering der XC-Therm-internen Parameter.
- Eine eigene NWP-Komponente — ICON liefert die meteorologische Basis.

// =============================================================================
= 2. Systemarchitektur
// =============================================================================
Das Gesamtsystem zerfällt in fünf locker gekoppelte Komponenten, die je für sich
entwickelt und getestet werden können:

#tbl(
  columns: (auto, 1fr, 1fr, 1fr),
  header: ([Komponente], [Aufgabe], [Input], [Output]),
  ([A. Region & AHD], [Geometrie pro Region], [Copernicus DEM 30m], [Polygone + AHD-Profile]),
  ([B. ICON-Pipeline], [NWP-Daten ziehen & aufbereiten], [DWD Open Data], [Regionsprofile (NetCDF)]),
  ([C. Modellkern], [1D-Konvektion pro Region], [Profile + AHD], [v(z,t), Basis, Top]),
  ([D. IGC-Pipeline], [Flugdaten beschaffen & auswerten], [WeGlide API / OLC], [Kreisflug-Statistik]),
  ([E. Validierung & Tuning], [Bias-Analyse, Parameter-Fit], [C + D + ICON-Diagnostik], [Kalibrierte Parameter]),
)

Komponenten A und B sind reine Daten-Pipelines und unabhängig voneinander lauffähig.
C hängt von A und B ab. D ist unabhängig. E integriert C und D mit einer
Cross-Reference auf ICON-Diagnostiken aus B (HBAS_SC, HTOP_DC) für die
Basishöhen-Validierung.

// =============================================================================
= 3. Komponente A — Regionsgeometrie und AHD
// =============================================================================
Liechtis Schlüsselparameter ist die Area-Height-Distribution: pro 100m-Höhenschicht
das atmosphärische Restvolumen $V_a (z)$ und die horizontal projizierte
Geländeoberfläche $S_G (z)$ (Heizfläche). Beide werden aus einem DEM deterministisch
berechnet.

== 3.1 Regionsdefinition
Es existiert *keine publizierte Spezifikation*, wie Liechti die ALPTHERM/Regtherm-Regionen
konkret geschnitten hat — die Abgrenzung wurde vermutlich manuell-meteorologisch nach
jahrzehntelanger Erfahrung vorgenommen. Das REGTHERM-2001-Paper beschreibt nur die
Kopplung zwischen Regionen, nicht deren Definition. Die Regionsdefinition wird daher
selbst entwickelt, gestützt auf drei Referenzstränge: hydrologische Einzugsgebiete,
datengetriebene Regionalisierung und Talwind-Meteorologie.

=== Eckwerte:
- Ziel: ca. 20–30 Regionen für den gesamten Alpenraum (vergleichbar Granularität alte Regtherm-Einteilung).
- Regionsgröße: 500–1500 km² im Gebirge (Liechtis Empfehlung), Untergrenze durch ICON-Auflösung gesetzt (siehe Level-Wahl).
- Speicherung: GeoJSON-Polygone in EPSG:4326, plus Repräsentationspunkt (Centroid).

=== HydroBASINS-Level-Wahl:
HydroBASINS-Level haben keine feste Fläche, sondern eine Größenverteilung
(Pfafstetter-Subdivision: jeder Knoten teilt topologisch in 9). Relevanter Bereich:

#tbl(
  columns: (auto, auto, 1fr),
  header: ([Level], [Mittlere Fläche (grob)], [Einordnung]),
  ([7], [~1.500–5.000 km²], [Obergrenze, eher zu grob (Gebirgsgruppen)]),
  ([8], [~500–1.500 km²], [Liechtis Zielgröße — Haupttäler (BASIS)]),
  ([9], [~150–500 km²], [XC-Therm-Richtung — Seitentäler (Verfeinerung)]),
  ([10], [~50–150 km²], [Einzelne Talkessel — meist zu fein]),
  ([11–12], [~13–130 km²], [Zu kleinteilig für 1D-Annahme]),
)

*Trend Alptherm $->$ XC Therm:* Das alte Alptherm/Regtherm nutzte ~31 österreichische
Regionen (≈ Level 7–8); XC Therm rechnet heute >1300 europäische Regionen (≈ Level
9–10). Diese Verkleinerung wurde aber erst durch bessere Trägermodelle sinnvoll — die
1D-Annahme gilt nur, solange die Region intern halbwegs homogen konvektiv ist.

#note[
  *ICON-Auflösung als physikalische Untergrenze:* Wird feiner geschnitten als die
  effektive Input-Auflösung, gewinnt man keine Information — man verteilt denselben
  Gitterpunkt auf mehrere Regionen (Scheingenauigkeit). ICON-D2 (2,2 km): Untergrenze
  ~100–250 km² für 20–50 Gitterpunkte je Region $->$ spricht für Level 9. ICON-EU
  (7 km): 100 km² wären nur ~2 Punkte $->$ für EU-gestützte Tage auf Level 8 bleiben
  bzw. Level-9-Regionen für die EU-Statistik wieder aggregieren.
]

*Empfehlung — Level 8 als Anker, Level 9 als Verfeinerungsreservoir:* Stufe 1 startet
auf Level 8 (Liechti-Granularität, robust für beide ICON-Modelle). Stufe 3 verfeinert
selektiv auf Level 9, aber nur wo die Varianzanalyse es rechtfertigt. Da HydroBASINS
hierarchisch genestet ist, ist das Splitten trivial: Eine Level-8-Region zerfällt durch
Anhängen einer Pfafstetter-Ziffer exakt in ihre Level-9-Kinder — ein reines
Dissolve/Un-dissolve über die PFAF_ID, kein eigener Schnittalgorithmus nötig. Im
Alpenvorland kehrt sich die Logik um: dort eher gröber (Level 7–8), da die Thermik
großräumig homogen ist und Level 9 künstlich viele fast identische Regionen erzeugen
würde.

=== Dreistufiges Verfahren:
*Stufe 1 — Geometrisches Gerüst (Einzugsgebiete).* Talwindsysteme folgen
Einzugsgebieten; die natürlichste Regionsgrenze ist daher die topographische
Wasserscheide. Basis: HydroBASINS (Pfafstetter-codiert, hierarchisch) bzw. EU-Hydro
(Copernicus, feiner für Europa), zugeschnitten auf die Zielgröße. Liefert physikalisch
motivierte, räumlich zusammenhängende Startregionen ohne Tuning.

*Stufe 2 — Manuelle meteorologische Korrektur.* Hauptkamm-Teilung erzwingen
(Nord-/Zentral-/Südalpen trennen). Keine Region darf über den Alpenhauptkamm schneiden,
da Nord- und Südseite gegenläufige thermische Tagesgänge haben. Talverzweigungen als
natürliche Grenzen. Dies ist die einzige Stufe mit bewusst eingebrachtem
meteorologischem Erfahrungswissen.

*Stufe 3 — Datengetriebene Verfeinerung.* Regionen mit dauerhaft hoher
regionsinterner Varianz (HBAS_SC, Windrichtung) werden identifiziert und entlang
topographischer Strukturen gesplittet (vgl. Abschnitt 7.3). Werkzeug: räumliche
Clustering-Verfahren mit Kontiguitäts-Constraint (PySAL/spopt: max_p_regions, skater,
region_k_means). Attribute: mittlere Geländehöhe, Exposition, saisonale ICON-Statistik.
Normale Cluster-Verfahren (k-means) sind ungeeignet, da sie keine zusammenhängenden
Gebiete garantieren.

#note[
  *Historische Daten für Stufe 3 — wichtige Entkopplung:* Der DWD Open Data Server hält
  nur ein rollierendes 2-Tage-Fenster vor; ein Langzeitarchiv der GRIB2-Läufe existiert
  öffentlich nicht. Stufe 3 benötigt jedoch _keine_ echten Vorhersagen, sondern nur die
  räumliche _Klimatologie_ der Streuungsmuster. Stufe 3 wird daher von der operationellen
  Pipeline (Komp. B) entkoppelt und auf einen bereits existierenden Archiv-/Reanalyse-
  Datensatz gestützt (siehe Anhang 12.4). Damit ist kein monatelanges Vorab-Sammeln
  nötig — Stufe 3 ist sofort startbar.
]

#note[
  *Physikalische Leitplanken (Whiteman; Zardi & Whiteman):* Eine Region umfasst ein
  zusammenhängendes Talwindsystem; Becken und Täler werden getrennt behandelt
  (beckenspezifischer Volumeneffekt); nicht über Hauptkämme schneiden.
]

== 3.1.1 Sonderfall Alpenrand — Quer-Segmentierung langer Täler
HydroBASINS-Einzugsgebiete am Alpenrand (z.B. Iller, Lech, Salzach) erstrecken sich vom
Hauptkamm bis tief ins Vorland. Als _eine_ Region behandelt würden sie
Hochgebirgsthermik (3000 m, steile Heizflächen, später Onset, hohe Basis) mit
Flachland-Konvektion (500–700 m, früher Onset, feuchtere Böden) vermischen —
meteorologisch unzulässig. Das Einzugsgebiet ist die richtige Logik _längs_ der
Talachse (Talwind), aber die falsche _quer_ dazu (thermische Homogenität). Lösung: ein
zusätzlicher Schnitt nach Geländecharakter.

- *Relief-/Höhen-Segmentierung:* Lange Einzugsgebiete in Hochgebirgs-, Voralpen- und Flachlandsegment zerlegen. Indikator: Reliefenergie (max−min Höhe im gleitenden Fenster) plus mittlere Höhe.
- *Alpenkonvention-Perimeter als physiographische Maske:* Die etablierte, frei verfügbare Alpenraum-Abgrenzung (alternativ Alpine Space) schneidet genau dort, wo das physikalische Regime wechselt.
- *Vorland als eigene Regionsklasse:* Das Alpenvorland wird mit-vorhergesagt, aber mit anderem Modellverhalten (siehe Kasten).

#note[
  *Alpenvorland-Modellklasse:* Im Flachland ist die Thermik annähernd horizontal
  homogen — Liechtis AHD-Volumeneffekt ist dort nicht gültig. Vorland-Regionen werden
  daher als vereinfachtes Mixed-Layer-Modell auf Basis derselben ICON-Wärmeströme
  (ASHFL_S, ALHFL_S) geführt, ohne Talvolumen-Logik. Liefert trotzdem Onset, CBL-Höhe
  und Steigwerte. Die Regionsgeometrie im Vorland kann gröber sein.
]

*Wichtig — Talwind-Konnektivität bewahren:* Ein Quer-Schnitt zertrennt ein
zusammenhängendes Talwindsystem. Der Talwind transportiert Feuchte und Luftmasse vom
Vorland ins Gebirge (genau der Regtherm-Kopplungsmechanismus, siehe 5.5). Die Segmente
werden daher geometrisch getrennt, aber in der Kopplungsstufe als vertikal benachbarte,
gekoppelte Regionen wieder verbunden. Jeder Schnitt entspricht so einer physikalischen
Grenze: Wasserscheide (längs), Alpenrand (Regimewechsel), Reliefstufe (Gültigkeit des
Volumeneffekts).

== 3.2 AHD-Berechnung
Input: Copernicus DEM GLO-30 (~30m). Pro Region:
- Höhenklassen von Talgrund bis höchste Erhebung in 100m-Schritten.
- $S_G (z)$ = Anteil der DEM-Pixel in der jeweiligen Höhenklasse, multipliziert mit Regionsfläche.
- $V_a (z)$ = Differenz zwischen Maximalvolumen einer 100m-Schicht und dem von Topographie eingenommenen Volumen.
- Ergänzend: mittlere Hangneigung und Exposition pro Höhenklasse — Erweiterungs-Hook für Richters Kritik an steilen Heizflächen.

=== Zusätzlich — Schwellenhöhen des Regionsgraphen (für die Kopplung, 5.5):
Neben den AHD-Profilen je Region wird pro Regions_nachbarschaft_ (Kante des
Nachbarschaftsgraphen) die effektive Schwellenhöhe bestimmt: die niedrigste Stelle im
DEM entlang der gemeinsamen Polygongrenze, relativ zu den Talböden beider Regionen.
Daraus leitet sich der Grenztyp (offener Übergang / Talverzweigung / Pass / Hauptkamm)
und später der Durchlässigkeitsfaktor ab. Einmalig berechnet, als Kanten-Attribut
gespeichert — die Datengrundlage für die differenzierte Kopplung in Phase 2.

== 3.3 Aufwand
Geschätzt 1–2 Wochen. Stack: Python mit _rasterio_, _geopandas_, _xarray_. Output ist
ein Pickle/NetCDF pro Region mit AHD-Profilen, wird einmal berechnet und danach statisch
genutzt.

// =============================================================================
= 4. Komponente B — ICON-Datenpipeline
// =============================================================================
Bezugsquelle: DWD Open Data unter _https:\/\/opendata.dwd.de/weather/nwp/_. ICON-D2
(2.2 km) für Tag 0–2, ICON-EU (7 km) für Tag 2–5. Initialisierungen alle 3h,
Forecast-Schritte stündlich.

== 4.1 Variablenliste
#tbl(
  columns: (auto, auto, 1fr),
  header: ([Kategorie], [Variable], [Verwendung im Modell]),
  ([Initialprofile], [T, QV, P, U, V, W, HHL], [Vertikale T/q-Profile, Wind, großräumige Subsidenz]),
  ([], [W_SO (4 Layer)], [Bodenfeuchte $->$ ersetzt 'Evap=const']),
  ([Strahlung (akk.)], [ASOB_S, ATHB_S], [Netto-Strahlungsbilanz statt Liechti-Formeln]),
  ([], [ASWDIR_S, ASWDIFD_S], [Direkte/diffuse Komponenten optional]),
  ([Wärmeströme], [ASHFL_S, ALHFL_S], [Sensibler/latenter Wärmestrom direkt]),
  ([Bewölkung], [CLCT_MOD, CLCH, CLCM, CLCL], [Bewölkungseinfluss bereits in Strahlung]),
  ([Bodennahe Größen], [T_2M, TD_2M, T_G, RELHUM_2M], [Bodentemperatur, Feuchte, $T_S$ statt δ·P]),
  ([Konvektions-Diag.], [HBAS_SC, HTOP_SC], [Cu-Basis (Tuning-Target für Cu-Tage)]),
  ([], [HBAS_CON, HTOP_CON], [Hochreichende Konv. $->$ Ausschlusskriterium]),
  ([], [HTOP_DC], [Trockenkonvektion (Blue-Day Validierung)]),
  ([], [CAPE_ML, CIN_ML, LCL_ML, HZEROCL], [Zusätzliche Diagnostik]),
  ([Grenzschicht], [HPBL, TKE], [Cross-Check für eigenes CBL]),
  ([Statisch], [HSURF, SOILTYP, PLCOV, ROOTDP], [Bodencharakteristik pro Region]),
)

#note[
  *ICON-D2 vs. ICON-EU Verfügbarkeit:* Die Namen oben folgen der allgemeinen
  ICON-Konvention. Für ICON-D2 publiziert DWD Open Data einen Teil davon nicht;
  im Code (Komp. B Archiv) werden folgende Substitute verwendet:

  #tbl(
    columns: (auto, auto, 1fr),
    header: ([Plan-Name (ICON-EU)], [ICON-D2], [Substitut / Bemerkung]),
    ([HPBL], [—], [*MH* (Mixed Layer Depth, m AGL) — bei konvektiver Tages-Grenzschicht $approx$ HPBL]),
    ([HBAS_CON, HTOP_CON], [—], [kein direktes Pendant; hochreichende Konv. wird über TOT_PREC + CAPE_ML klassifiziert]),
    ([LCL_ML], [—], [kein Pendant; HBAS_SC trägt vergleichbare Information (Cu-Basis $approx$ LCL)]),
    ([TKE], [✓], [nur als Modelllevel-Profil (3D), nicht als Surface-Diagnostik — wandert in Tier 2]),
    ([CIN_ML, HZEROCL], [✓], [verfügbar wie aufgelistet, wandern in Tier 1]),
  )

  An Tagen, an denen ICON-EU zum Einsatz kommt (Tag 2–5), gelten die
  originalen Variablennamen unverändert.
]

== 4.2 Pipeline-Schritte
- Download via HTTP, GRIB2-Format, Filter auf benötigte Variablen (reduziert Volumen drastisch — voller Lauf wäre > 100 GB/Tag).
- Räumliche Extraktion: *alle* Gitterpunkte innerhalb des Region-Polygons (nicht nur ein Referenzpunkt — siehe 4.3).
- Vertikale Interpolation auf einheitliches Höhengitter (100m, passend zur AHD-Schichtung).
- Akkumulierte Größen (Strahlung, Wärmeströme) auf Stundenraten differenzieren.
- Output: NetCDF pro Tag und Region mit aggregierten Profilen, diagnostischen Werten und Streuungsmaßen als Zeitreihe.

== 4.3 Regionale Aggregation statt Einzelpunkt
Ein einzelner Referenzpunkt repräsentiert die Region schlecht — gerade bei räumlich
fleckigen, nichtlinearen Feldern wie Bewölkung und Konvektionsdiagnostik. Stattdessen
wird über alle Gitterpunkte im Polygon aggregiert, wobei *nicht nur die zentrale
Tendenz, sondern auch die Streuung* behalten wird:

#tbl(
  columns: (auto, auto, auto, 1fr),
  header: ([Variable], [Aggregat], [Zusätzlich], [Begründung]),
  ([Strahlung / Wärmeströme], [Flächenmittel, höhenstratifiziert], [P(z)-Profil je Höhenklasse], [Extensive Flüsse; Mittel ist physikalisch korrekt; höhenaufgelöst direkt in AHD einspeisbar]),
  ([Bewölkung (CLCT_MOD)], [Median], [Q25, Q75], [Robust gegen einzelne überschießende Zellen]),
  ([Konvektion (HBAS_SC, HTOP_DC)], [Median der gültigen Punkte], [Anteil gültiger Punkte], [Gültigkeitsanteil = Auslöse-/Bedeckungsindikator, den ein Einzelpunkt nicht liefern kann]),
  ([Profile (T(z), q(z))], [nach Höhenband getrennt], [Tal- vs. Hochlagenprofil], [Mitteln über versch. HSURF verschmiert Inversionen — diese sind für die Obergrenze entscheidend]),
  ([Wind (U, V)], [Vektormittel], [Richtungs- & Geschw.-Varianz], [Hohe Varianz = mögliche Konvergenz = potenziell exzellente Thermik; Streuung ist das Signal]),
)

#note[
  *Schlüsselgröße "Anteil aktiver Konvektion":* Der Prozentsatz der Gitterpunkte mit
  gültigem HBAS_SC ist ein Bedeckungs- und Auslöseindikator, der prinzipiell nicht aus
  einem Einzelpunkt ableitbar ist, aber direkt mit der Flugbarkeit korreliert. Wird als
  eigenständiges Feature mitgeführt.
]

== 4.4 Auflösungsabhängigkeit der Statistik
- *ICON-D2 (2.2 km):* ca. 200 Gitterpunkte in einer 1000-km²-Region — ausreichend für robuste Perzentile und Varianzmaße.
- *ICON-EU (7 km):* nur ca. 20 Punkte — Statistik dünn, daher auf Median + Gültigkeitsanteil beschränken.

== 4.5 Eigenes Archiv ab Projektbeginn aufbauen
Da der DWD Open Data Server nur ein 2-Tage-Fenster vorhält, sollte ab Tag 1 des Projekts
ein eigener Cron-Job die benötigten ~15 Variablen für die Alpen-Bounding-Box täglich
abgreifen und persistent speichern. Bis Komponente C steht, ist so bereits eine eigene
Saison beisammen — ohne Abhängigkeit von Drittarchiven. Für die Historie davor dienen
die externen Archive aus Anhang 12.4.

== 4.6 Operationeller Lauf-Rhythmus
ICON-D2 (2.2 km) liefert 8 Läufe/Tag (00, 03, 06, 09, 12, 15, 18, 21 UTC), je +48 h,
1-h-Schritt. ICON-EU (7 km) liefert +120 h aus den Läufen 00/06/12/18 UTC und +30 h aus
03/09/15/21 UTC. ICON-D2-RUC (seit 2024, stündlich, nur +14 h) ist für Tagesthermik zu
kurzfristig. Segelflug-relevant ist die Konvektion ca. 09–18 Uhr Lokalzeit (07–16 UTC im
Sommer).

Nicht alle 8 D2-Läufe werden verarbeitet — aufeinanderfolgende Läufe unterscheiden sich
oft nur marginal. Empfohlen ist ein gestaffelter Rhythmus mit 3–4 Modellrechnungen des
ALPTHERM-Kerns pro Tag:

#tbl(
  columns: (auto, auto, auto, 1fr),
  header: ([Zweck], [Lauf], [Lokalzeit (Sommer)], [Liefert]),
  ([Vorausblick Tag 2–5], [ICON-EU 00 UTC], [~04–05 Uhr fertig], [Tagesauswahl, Reiseentscheidung]),
  ([Haupt-Tagesprognose (Anker)], [ICON-D2 03 UTC], [~07 Uhr fertig], [Verbindliche Prognose für heute]),
  ([Vormittags-Update], [ICON-D2 06 UTC], [~10 Uhr fertig], [Korrektur vor dem Start]),
  ([Optional Mittags-Update], [ICON-D2 09 UTC], [~13 Uhr fertig], [Anpassung Nachmittagsthermik]),
)

=== Verfügbarkeitslatenz:
Die Open-Data-Bereitstellung hinkt der Initialisierung um ca. 2,5–3 h hinterher. Der
03-UTC-D2-Lauf ist also gegen 05:30–06:00 UTC abrufbar, fertig prozessiert ca. 06:30 UTC
(08:30 Lokalzeit) — passend für die Morgenprognose. Wer früher dran sein will, nutzt den
00-UTC-Lauf.

=== Konsistenz der Prognose (UX):
Mehrfaches Neurechnen lässt die Prognose zwischen Läufen springen. Zwei Gegenmaßnahmen:
(a) den Anker-Lauf (03 UTC) als "offizielle Tagesprognose" fixieren und spätere Läufe
nur als "Update" kennzeichnen, oder (b) leichte zeitliche Glättung über die letzten 2
Läufe.

== 4.7 Werkzeuge
_eccodes_ / _cfgrib_ für GRIB2-Decoding, _xarray_ für die Datenstruktur, _requests_ oder
_aiohttp_ für parallele Downloads. Speicherplatz pro Saison (April–September) pro Region
grob geschätzt ca. 200 MB.

// =============================================================================
= 5. Komponente C — Modellkern (1D-Konvektion)
// =============================================================================
Liechtis Physik bleibt erhalten, aber stark aufgeräumt. Folgende Größen kommen ab sofort
aus ICON und werden *nicht mehr* intern parametrisiert:

=== 5.1 Was aus ICON kommt
- P (Gesamtstrahlungsbilanz) = ASOB_S + ATHB_S
- $P_"sens"$ = ASHFL_S, $P_"lat"$ = ALHFL_S (Vorzeichenkonvention beachten)
- $T_S$ = T_G (kein δ·P mehr)
- Großräumige Subsidenz = W aus ICON
- Initial- und Boundary-Profile T, q aus ICON-Modellleveln

=== 5.2 Was im Modell verbleibt (Liechti-Kern)
Pro Zeitschritt Δt (vorgeschlagen 2 min, wie Original):
- Sensible Wärme pro Schicht: $H_"sens" = P_"sens" dot Δt dot S_G (z)$
- ΔT-Bestimmung mit Zwei-Regime-Logik (linear bzw. gesättigt bei $ΔT_0$)
- Paketmasse: $m_p = H_"sens" \/ (c_p dot ΔT)$
- Auftriebsenergie schichtweise gegen aktuelles T(z)-Profil
- Vertikalgeschwindigkeit $v = (2E\/m)^0.5$
- Entrainment/Detrainment mit Koeffizienten $E_(n 0)$, $D_(c 0)$
- Windreduktion $f_"kin" = 1 - r dot u^2$
- Kondensation oberhalb LCL $->$ Cu-Bildung; Modell-Wolkenbase ableitbar
- Profilfortschreibung durch Massentransport & Subsidenz aus ICON-W

== 5.3 Outputstruktur
- v(z,t) pro Region — 100m-Auflösung, 30min-Bins (kompatibel zu IGC-Binning)
- Trockene CBL-Höhe (Modell-HTOP_DC-Äquivalent)
- Cu-Basis und -Top (falls feuchte Konvektion)
- Flugzeugsteigen via Polare (Standard-Klasse als Default)

== 5.4 Aufwand
Geschätzt 4–6 Wochen für eine saubere, vektorisierte numpy-Implementierung mit Tests
gegen Liechtis Beispielfall (Subsidenz-Sensitivität, Fig. 4 im Originalpaper).

== 5.5 Regionale Kopplung (Regtherm-Mechanismus)
Das Original-ALPTHERM (1993) behandelt jede Region isoliert. Liechti (2002) erweiterte
es zu Regtherm durch *horizontale Kopplung benachbarter Regionen*, um
Sekundärzirkulationen — Talwindsysteme und Seebrisen — zu erfassen. Relevant aus zwei
Gründen:
- *Feuchte-/Massentransport:* Talwinde führen feuchtere Vorlandluft ins Gebirge — beeinflusst Wolkenbasis und Onset.
- *Verbindung quer-segmentierter Täler (siehe 3.1.1):* Stellt den physikalischen Zusammenhang getrennter Segmente wieder her.

=== Modellierte Physik — thermisch getriebene Sekundärzirkulation:
Regtherm löst nicht die 3D-Strömung auf, sondern parametrisiert einen horizontalen
Austauschterm zwischen den vorhandenen 1D-Regionssäulen. Der abgebildete Kreislauf:
+ *Differentielle Erwärmung:* Benachbarte Regionen heizen sich unterschiedlich auf (Tal stärker als Ebene durch Volumeneffekt; Südseite stärker als Nordseite).
+ *Horizontaler Druckgradient:* Die wärmere Region hat geringere Dichte $->$ in der Höhe relativer Hochdruck, am Boden relativer Tiefdruck.
+ *Bodennahe Ausgleichsströmung:* Luft fließt bodennah von der kühleren zur wärmeren Region — das ist der Talwind / die Seebrise.
+ *Massenkontinuität:* Der einströmenden Bodenluft entspricht ein Gegenstrom in der Höhe, gespeist von der Konvektion der wärmeren Region.
+ *Rückkopplung auf die Thermik:* Die einströmende Luft bringt Feuchte und Masse: niedrigere Wolkenbasis (ggf. Überentwicklung), begrenzte CBL-Höhe, "Deckelung" der Nachmittagsthermik bei einsetzendem Talwind.

#note[
  *Zentral — der thermische Wind ist OUTPUT, nicht Input:* Die Talwind-/Seebrisen-
  Komponente wird vom Modell aus der differentiellen Erwärmung der gekoppelten CBLs
  _selbst erzeugt_. Der ICON-Bodenwind (10 m) darf *nicht* als Input der Kopplung dienen
  — sonst wäre die Zirkulation doppelt enthalten und das Modell redundant. Der
  ICON-Bodenwind ist stattdessen eine _Validierungsgröße_.
]

=== Tatsächliche Inputs der Kopplung:
#tbl(
  columns: (auto, auto, 1fr),
  header: ([Input], [Quelle], [Rolle]),
  ([CBL-Temperaturprofil je Region], [Modellkern (C) selbst], [Treibt Dichte-/Druckunterschied]),
  ([Geometr. Nachbarschaft + Distanz], [Regionsdefinition (A)], [Gradient = Δp / Δx]),
  ([Höhendifferenz der Regionsböden], [DEM (A)], [Geopotential-Bezug]),
  ([Synoptischer Wind (U/V auf Levels)], [ICON], [Modulator: Zusammenbruch bei Starkwind, Advektion]),
  ([Feuchteprofil je Region], [Modellkern + ICON-Init.], [Was der Talwind an Feuchte transportiert]),
)

Der synoptische Wind aus ICON ist also sehr wohl ein Input — aber als _Modulator_
(Steigwert-Reduktion via f_kin, Zusammenbruch der Zirkulation bei Starkwind,
Luftmassen-Advektion), nicht als Treiber der Sekundärzirkulation selbst.

=== Grenztypologie — eigene Weiterentwicklung über publiziertes Regtherm hinaus:
Das publizierte Regtherm behandelt Regionsnachbarschaften pauschal. Tatsächlich ist eine
Kopplung über einen 2500-m-Hauptkamm physikalisch etwas völlig anderes als der offene
Übergang Vorland$->$Voralpental. Entscheidend ist, wie leicht Luft zwischen den Säulen
ausgetauscht werden kann — eine Eigenschaft der _Grenztopographie_, nicht des bloßen
Polygonrands. Vier Grenztypen decken die Alpen-Realität ab:

#tbl(
  columns: (auto, auto, auto, 1fr),
  header: ([Typ], [Grenze], [Kopplung], [Charakter]),
  ([1], [Offener Übergang (Vorland ↔ Voralpental)], [stark, bidirektional], [Haupt-Feuchteeintrag, klassischer Talwind-Einlass]),
  ([2], [Talverzweigung (Tal ↔ Seitental)], [mittel, richtungsabh.], [kanalisiert durch Talgeometrie]),
  ([3], [Pass / Sattel], [schwach–mittel, schwellwertbehaftet], [Überströmen erst wenn CBL die Passhöhe erreicht]),
  ([4], [Hauptkamm (hohe Wasserscheide)], [≈ null / entkoppelt], [bodennaher Austausch praktisch null]),
)

#note[
  *Verbindender Parameter — effektive Schwellenhöhe:* Die vier Typen lassen sich über
  _eine_ kontinuierliche, DEM-ableitbare Größe parametrisieren statt über diskrete
  Kategorien: die Höhe des niedrigsten Übergangs entlang der gemeinsamen Grenze relativ
  zu den Talböden (Sattelhöhe). Offener Übergang: Schwelle ≈ Talboden $->$ max. Kopplung.
  Pass: moderat darüber $->$ schwellwertbehaftet. Hauptkamm: weit darüber $->$ keine
  Kopplung.
]

=== Durchlässigkeitsfaktor (Vorschlag, zu kalibrieren):
Der Austauschterm wird um einen Durchlässigkeitsfaktor erweitert, der den Druckgradienten
moduliert:

#formula[Austauschfluss ≈ (Druckgradient Δp/Δx) × D(z\_CBL − z\_Schwelle)]

Die Durchlässigkeit D ist eine Funktion der Differenz aus CBL-Höhe und Schwellenhöhe:
null unterhalb der Schwelle (Täler entkoppelt), ansteigend darüber. Plausible Form: ein
weicher Schwellwert, z.B. $D = max(0, (z_"CBL" - z_"Schwelle") \/ z_"skala")$, begrenzt
auf $[0,1]$, mit Übergangsskala $z_"skala"$ (Größenordnung einige hundert Meter). Damit
ergibt sich automatisch das richtige Verhalten: Hauptkämme koppeln nie, offene Übergänge
immer, Pässe ab dem Zeitpunkt, an dem die Thermik die Passhöhe durchbricht. *$z_"skala"$
und die genaue Form sind eigene Annahmen und gegen IGC-Daten zu kalibrieren* — sie gehen
über das publizierte Regtherm hinaus.

#note[
  *Gerichtete, tageszeitabhängige Kopplung am Vorland-Übergang (eigene Erweiterung):*
  Beim Typ-1-Übergang ist die Kopplung _asymmetrisch_: Tagsüber dominiert der Fluss
  Vorland$->$Gebirge (Talwind bergeinwärts, Feuchteeintrag — thermikrelevant); der
  nächtliche Rückfluss ist für die Thermik kaum relevant. Die Kopplung sollte daher kein
  symmetrischer Diffusionsterm sein, sondern ein _gerichteter, tageszeitabhängiger_ Term.
  Eigene Erweiterung, zu kalibrieren.
]

=== Phasenplan:
Die Kopplung ist _nicht_ Teil der ersten Modellversion. Phase 1 liefert isolierte
Regionen (Original-ALPTHERM-Verhalten) — für die meisten Strahlungstage bereits
brauchbar. Phase 2 fügt den Austauschterm hinzu, inklusive der Grenztypologie: pro
Regionsnachbarschaft wird einmalig die Schwellenhöhe aus dem DEM bestimmt (Komp. A
liefert das als Kanten-Attribut), dann Durchlässigkeitsfaktor und gerichtete Kopplung
kalibriert. Die genaue Parametrisierung ist bei Liechti *nicht publiziert* — das
REGTHERM-2001-Paper beschreibt das Konzept, nicht die Konstanten. Schwierigster zu
rekonstruierender Teil; wird gegen IGC-Daten und ICONs Bodenwindfeld kalibriert.

// =============================================================================
= 6. Komponente D — IGC-Validierungspipeline
// =============================================================================
Primärquelle WeGlide (offene REST-API), sekundär OLC bei Lücken.

== 6.1 Datenbeschaffung
- WeGlide-API: Flüge nach Datum und geographischem Filter (Alpen-Bbox).
- IGC-Files herunterladen, lokal cachen.
- Metadaten extrahieren: Pilot, Flugzeugklasse, Polare-Hinweis, Startplatz.

== 6.2 IGC-Aufbereitung (nach Richter 2011)
- Kreisflug-Detektion: Krümmungsparameter über gleitendes 2-min-Fenster.
- Verlagerungsvektor als Drift-Korrektur.
- Minimale Dauer 2 min, kontinuierliche Drehrichtung.
- Ausschluss: F-Schlepp-Phase, Motorsegler-Steigen, Hangwind/Welle.
- Pro Kreisflug-Phase: mittlere Vertikalgeschwindigkeit, mittlere Höhe, Zeitstempel.

== 6.3 Zuordnung zu Region und Zeit-Bin
- Räumlich: Kreisflug-Centroid in Region-Polygon (NICHT Startplatz!).
- Zeitlich: 30-min-Bins, lokale Sommerzeit beachten.
- Aggregation pro (Region, Tag, 30min-Bin): N Kreise, Median & Q90 $v_"climb"$, max. Höhe.

== 6.4 Aufwand
Geschätzt 3–4 Wochen. Kritischer Punkt: Robustheit der Kreisflug-Detektion. Richters
Implementierung als Referenz; in modernem Python mit _pyigc_ oder eigenem Parser.

== 6.5 WeGlide API-Zugang und ToS-Konformität
WeGlide stellt eine öffentliche Lese-API bereit (_api.weglide.org_, Swagger-Doku unter
_/docs_) und ermutigt ausdrücklich zur Nutzung. Lesezugriff ohne Authentifizierung; OAuth
nur für schreibenden Zugriff (hier nicht nötig). Keine veröffentlichten harten Rate
Limits; ToS untersagen qualitativ nur "exzessive oder missbräuchliche" Nutzung.

=== Kritische Punkte:
- *Firewall / nicht-residentielle IPs:* Server-IPs werden zur Spam-Abwehr oft blockiert — nicht wegen Rate Limits, sondern wegen IP-Herkunft. Lösung: API-Key anfragen.
- *API nicht final:* Endpunkte können sich ändern; Versionierung geplant.
- *Attribution:* Bei Veröffentlichung Quellenangabe Pflicht.
- *IGC-Qualität garantiert:* Logging-Lücken dürfen 120 s nicht überschreiten.

=== ToS-konforme Zugriffsstrategie:
- *API-Key proaktiv anfragen* mit Projektbeschreibung — löst Firewall-Problem.
- *Aggressives lokales Caching:* jedes IGC-File nur einmal ziehen.
- *Höfliches Crawling:* 1–2 s Verzögerung, sequenziell.
- *Inkrementelles Update* im laufenden Betrieb.
- *Vorhandener Python-Client* (PyPI: WeGlide-Python-Client).

// =============================================================================
= 7. Komponente E — Validierung und Parametertuning
// =============================================================================

== 7.1 Verbleibende Tuning-Parameter
#tbl(
  columns: (auto, 1fr, 1fr, auto),
  header: ([Parameter], [Bedeutung], [Tuning-Target], [Liechti]),
  ([$ΔT_0$], [Max. T-Diff. Paket/Umgebung pro Schicht], [IGC-Steigwerte (Median & Q90)], [0.5 K]),
  ([$P_0$], [Sättigungsschwelle Wärmestrom], [Tagesgang-Form (Onset, Ende)], [—]),
  ([$E_(n 0)$], [Entrainment-Koeffizient], [Höhenabhängigkeit v(z), Wolkenbasis], [0.02 s/m]),
  ([$D_(c 0)$], [Detrainment-Koeffizient], [Form der oberen CBL], [0.08 s/m]),
  ([r], [Windreduktion], [v vs. Windgeschwindigkeit], [geraten]),
  ([Bart-Skalierung], [mean lift $->$ best lift], [Verteilung gegen IGC-Q90], [implizit]),
)

== 7.2 Drei Validierungs-Layer
Die Tageklassifikation erfolgt automatisch aus ICON-Diagnostik:

#tbl(
  columns: (auto, auto, auto, 1fr),
  header: ([Tagestyp], [Erkannt durch], [Validierungs-Target], [Was wird kalibriert]),
  ([Cu-Tag], [HBAS_SC gültig], [HBAS_SC + IGC-Maxhöhen], [Feuchteprozess, LCL-Verhalten]),
  ([Blue Day], [HBAS_SC fehlt, HTOP_DC niedrig], [HTOP_DC + IGC-Maxhöhen], [CBL-Energiebilanz, Entrainment]),
  ([Gewittertag], [HBAS_CON gesetzt], [Tag ausschließen], [—]),
)

#note[
  *Vermeidung von Zirkularität:* Die Basishöhe wird primär gegen die ICON-Diagnostiken
  (HBAS_SC, HTOP_DC) getunt, da flächendeckend verfügbar. Die IGC-Maxhöhen dienen als
  _unabhängige_ Plausibilitätsprüfung. An Cu-Tagen sollten alle drei Größen konvergieren
  — IGC-Max liegt typisch 50–200 m unterhalb der Wolkenbasis (Wolkenflugverbot).
]

== 7.3 Regionsinterne Streuung als doppeltes Signal
Die in Komponente B berechnete regionsinterne Streuung (siehe 4.3) wird zweifach genutzt:
- *Als Unsicherheitsmaß der Prognose:* Hohe Streuung signalisiert eine unsichere bzw. inhomogene Vorhersage.
- *Als Qualitätskriterium der Regionsdefinition:* Dauerhaft hohe Varianz $->$ Region zu groß oder schneidet Luftmassengrenze $->$ Split-Kandidat.

Damit schließt sich der iterative Regionsschnitt aus Komponente A: Regionen nach
mittlerer saisonaler HBAS_SC-Standardabweichung ranken, oberste Kandidaten entlang
topographischer Strukturen aufteilen.

== 7.4 Tuning-Strategie
- Initialisierung mit Liechtis Originalwerten.
- Validierungs-Saison (mindestens April–September, idealerweise 2 Jahre).
- Bias-Analyse pro Tageszeit, Tagesstärke, Region — wie Richter (2011).
- Parameter-Optimierung mit Grid-Search oder Bayesian Optimization (scikit-optimize).
- Regionale Stratifizierung: $ΔT_0$ vermutlich verschieden für Voralpen, Hauptkamm, Südseite.

== 7.5 Erfolgsmetriken
- RMSE der vorhergesagten Steigwerte vs. IGC-Median < 0.3 m/s
- RMSE der Basishöhe vs. HBAS_SC < 200 m
- Klassifikationsgüte Tagestypen (Cu/Blue/Gewitter) > 85% gegen IGC-Realität
- Saisonale Bias-Stabilität: kein Trend April–September

// =============================================================================
= 8. Datenverfügbarkeits-Asymmetrie (Querschnittsthema A ↔ E)
// =============================================================================
Ein verwertbares Tuning-Sample erfordert *gleichzeitig* am selben Tag und in derselben
Region: (1) den passenden ICON-Lauf, (2) genug IGC-Flüge mit Thermiknutzung, (3) einen
überhaupt fliegbaren Tag. Diese drei Bedingungen sind stark ungleich verteilt — betrifft
sowohl Regionsdefinition (A) als auch Tuning (E).

== 8.1 Drei Asymmetrie-Achsen
- *Räumlich:* IGC-Dichte korreliert mit Fluggeländen, nicht mit meteorologischer Repräsentativität. Gerade fein verfeinerte Level-9-Hochgebirgsregionen haben oft die wenigsten IGC-Daten.
- *Zeitlich:* Flüge gibt es nur an guten Tagen. Schwachthermik- und Blue-Days sind systematisch unterrepräsentiert — aber genau dort macht das Modell die größten Fehler.
- *Selektion:* Auch an fliegbaren Tagen fliegen Piloten die besten Linien zur besten Zeit; Randzeiten und schwächere Regionen unterrepräsentiert.

== 8.2 Konsequenzen fürs Setup
- *IGC-Dichte als 4. Kriterium der Regionsdefinition:* Verfeinerung auf Level 9 nur wo genug IGC-Dichte existiert; sonst auf Level 8 belassen und per Parameter-Transfer versorgen.
- *Parameter-Transfer zwischen Regionen:* An datenreichen Regionen fitten, dann auf datenarme mit ähnlichem Charakter (Höhe, Exposition, Relieftyp) übertragen.
- *Mindest-Stichprobengröße als Gütefilter:* Pro (Region, Tag, 30-min-Bin) Mindestzahl Kreisflüge fordern; Tage/Bins darunter markieren und ausschließen.
- *Schnittmenge bestimmt die nutzbare Stichprobe:* Weder "alle ICON-Tage" noch "alle IGC-Flüge", sondern deren Schnittmenge unter den Gütefiltern — realistisch deutlich kleiner.

#note[
  *Früh abschätzen:* Vor dem Bau der vollen Pipeline lohnt eine grobe Abschätzung der
  nutzbaren Schnittmenge (IGC-Flüge je Region/Saison × fliegbare Tage mit ICON-Abdeckung).
  Das entscheidet, wie fein die Regionen für ein belastbares Tuning sein dürfen.
]

// =============================================================================
= 9. Datenhaltungs- und Archivierungsstrategie
// =============================================================================
Die beiden Validierungs-Datenquellen haben fundamental unterschiedliche Vergänglichkeit:
*sofort mit dem ICON-Mitschnitt beginnen, unabhängig vom Stand des Modellcodes.*
- *IGC-Daten sind quasi-permanent:* WeGlide/OLC halten Flüge über Jahre — rückwirkend jederzeit ziehbar.
- *ICON-Daten sind flüchtig:* Der DWD löscht nach ~2 Tagen. Jeder gute Thermiktag, der jetzt nicht als nativer GRIB2 mitgeschnitten wird, ist unwiederbringlich verloren.

== 9.1 Warum nicht rein selektiv nach IGC-Dichte archivieren
Nur die guten Flugtage mitzuschneiden würde den Schönwetter-Bias aus Abschnitt 8
einbauen: Schwachthermik- und Blue-Days — wo das Modell am meisten irrt — hätten keine
ICON-Daten. Vollständig alles auf Modelllevel-Ebene zu archivieren ist teuer. Lösung: ein
zweistufiges Archiv.

== 9.2 Zweistufiges Archiv
*Mehrere Läufe sammeln, nicht nur den operationellen Anker.* Für die Prognose (4.6) wird
_ein_ Lauf gewählt (03 UTC). Für Sammlung und Validierung gilt das Gegenteil: Der spätere
Tuning-Vergleich soll gegen den _bestmöglichen_ ICON-Input laufen. Entscheidend ist die
Vorlaufzeit zum Flugzeitpunkt — ein späterer Lauf hat mehr Morgenbeobachtungen
assimiliert und trennt ICONs NWP-Vorhersagefehler sauberer vom eigenen Modellfehler.

#tbl(
  columns: (auto, 1fr, 1fr),
  header: ([Lauf], [Für Validierung/Tuning], [Für operationelle Prognose]),
  ([00 UTC], [Frühe Basis, lange Vorlaufzeit], [Früheste Morgenprognose]),
  ([03 UTC], [Mittlere Vorlaufzeit], [Anker (operationell)]),
  ([06 UTC], [Sweet Spot — kurze Vorlaufzeit, ganzer Flugtag abgedeckt], [Vormittags-Update]),
  ([09 UTC], [Sehr aktuell, aber Onset teils vorbei], [Mittags-Update / Trigger]),
)

#tbl(
  columns: (auto, 1fr, auto, 1fr),
  header: ([Tier], [Was], [Läufe], [Zweck]),
  ([Tier 1 Forcing], [~15 Input-Variablen, Alpen-Bbox, schlank], [00/03/06/09 UTC, täglich], [Vermeidet Schönwetter-Bias; alle Tagestypen; alle Vorlaufzeiten]),
  ([Tier 2 Voll], [Volle 3D-Profile, alle Modelllevel], [06 UTC, an guten Tagen], [Reserve für Modell-Erweiterungen / neue Variablen]),
)

#note[
  *Tier-2-Entscheidung: diagnostisch statt prädiktiv.* Die DWD-Löschfrist (~2 Tage) läuft
  _pro Lauf ab dessen eigenem Init-Zeitpunkt_ — der 06-UTC-Lauf lebt bis ~06 UTC zwei
  Tage später. Die Entscheidung wird auf den 09-UTC-Lauf (verfügbar ~11:30 UTC)
  verschoben, der die Konvektion mit nur ~2 h Vorlauf bereits "sieht". Bei positiver
  Entscheidung wird retroaktiv das Tier-2-Vollprofil des _06-UTC-Laufs_ nachgeladen (noch
  im Löschfenster). Entscheidungsbasis (09 UTC) von archivierter Größe (06 UTC)
  entkoppelt.
]

== 9.3 Sofortmaßnahmen (auch ohne Modellcode)
*Maschine:* HomeServer als Primärsystem. Speicher ist der härtere Constraint (Archiv
wächst monoton über Jahre, Platten nachrüstbar). Der Bandbreiten-Constraint ist weicher:
Tier 1 ist schlank (läuft unbemerkt auch tagsüber), der große Tier-2-Download lässt sich
dank der 2-Tage-Frist in die Nacht verschieben.

#tbl(
  columns: (auto, auto, 1fr, auto),
  header: ([Job], [Zeit (UTC)], [Zweck], [Bandbreite]),
  ([Tier-1-Sammlung], [~06:30, 09:30, 12:30, 15:30], [Je Lauf (00/03/06/09) schlank ziehen], [gering, tagsüber ok]),
  ([Tier-2-Trigger], [~12:00], [09-UTC-Lauf diagnostisch auswerten], [minimal]),
  ([Tier-2-Download], [nachts, ~23:00], [06-UTC-Vollprofile guter Tage nachladen], [hoch, aber Freizeit]),
)

Der entscheidende Trick ist die *Entkopplung von Trigger (mittags, klein) und Download
(nachts, groß)*. Die Lauf-Verfügbarkeitslatenz (~2,5–3 h, siehe 4.6) ist in den
Job-Zeiten berücksichtigt.

=== Weitere Sofortmaßnahmen:
- *Ablagestruktur* nach Datum/Lauf/Variable, idealerweise schon im Zielformat (Zarr).
- *Metadaten-Log:* pro Tag Lauf, Variablen und Tier-Stufe festhalten.
- *Variablen großzügig wählen:* im Zweifel ein paar mehr — Speicher billiger als ein verlorener Thermiktag.
- *IGC parallel, ohne Eile:* kein WeGlide-Zugang nötig, um die ICON-Sammlung zu starten.

== 9.4 Format
*Zarr* ist dem reinen GRIB2-Stapel überlegen, sobald über viele Tage analysiert wird:
chunked, paralleler Zugriff auf Zeitreihen, ohne jedes GRIB-File einzeln zu öffnen.
Empfehlung: GRIB2 eingangsseitig als Rohformat behalten und täglich in ein wachsendes
Zarr-Archiv anhängen.

== 9.5 15-Min-Sub-Step-Auswertung (Phase 2 — post-M0)
DWD publiziert vier konvektions-relevante Variablen mit 15-Min-Auflösung
innerhalb der stündlichen GRIB-Dateien: CAPE_ML, TOT_PREC, HBAS_SC, HTOP_SC.
M0 verwirft aktuell drei der vier Sub-Steps via `filter_by_keys={"step":
lead_h}` und behält nur die Vollstunde — sonst entstünden Shape-Konflikte
mit den stündlichen Variablen (ASOB_S, ASHFL_S, ALHFL_S, T_2M, …).

Diese Asymmetrie ist die zentrale Designgrenze: das eigentliche
*Forcing* (Strahlung, Wärmeströme) bleibt stündlich, nur die
*Konvektions-State-Variablen* sind sub-hourly. Sinnvoll ist daher
nicht, das Komp.-C-Modell auf 15-Min internen Time-Step zu ziehen
(fake-Auflösung), sondern Sub-Step-Daten gezielt für Diagnostik,
Onset-Detection und Tier-2-Triggerlogik zu nutzen.

Vier konkrete Verbesserungen gegenüber Liechti 1993:

+ *Onset-Detection per Region (Komp. C / §7 Tuning):*
  Liechti markiert den Thermik-Beginn heuristisch (Bodenfluss überschreitet
  Schwelle). Mit 15-Min-HBAS_SC haben wir den beobachteten Onset direkt:
  erste Slice, in der die Variable von NaN/0 auf einen Wert springt — auf
  $plus.minus 7,5$ Min pinned statt ±30 Min beim 1h-Sampling. Tuning-Vorteil:
  $T_0$ lässt sich gegen modell-beobachteten Onset fitten, nicht nur gegen
  IGC-Frühaufsteher (vermeidet Selektion-Bias).

+ *Kurz- vs. Dauerregen-Klassifikator (Tier-2-Trigger, §9.2):*
  Heutiges Beispiel: `precip_window = 50 mm` spatial-max über 6 h sieht nach
  Gewittertag aus, ist aber typisch ein guter Flugtag mit lokalem
  Nachmittags-Schauer. Mit 15-Min-TOT_PREC differenzieren wir: $<= 2$ von 24
  Slices nass = *kurzer Schauer-Tag* (Flugtag); $> 6$ Slices nass = *Dauerregen*
  (kein Flugtag). Klassifikation wandert als `day_class`-Feld ins Manifest.

+ *Sustained-Peak-Bedingung (Tier-2-Trigger):*
  Aktuell feuert `cape_max > 100` schon bei einem einzigen Slice. Robuster:
  CAPE > Schwelle in mindestens 3 aufeinanderfolgenden 15-Min-Slices
  (entspricht 45 min Sustained-Konvektion). Reduziert False Positives durch
  Mesoskalen-Spikes, die im Stunden-Mittel verschwinden würden.

+ *Front-/Konvergenz-Detektion (neuer Trigger-Branch):*
  Liechtis 1D-Modell hat keine Frontphysik — kann den schärfsten Pre-Frontal-
  Steigwert-Tag ($Delta z > 5$ km in 30 min) nicht erklären. Detektor: CAPE
  springt $> 500$ J/kg innerhalb zweier benachbarter 15-Min-Slices.
  Eigene Risikokategorie in der Tier-2-Sammlung — diese Tage sind für
  Komp.-E-Validierung besonders wertvoll, da Liechti sie systematisch
  unterschätzt.

#note[
  *Architekturkonsequenz:* M0 archiviert aktuell nur Vollstunden. Für
  Phase 2 wird ein Mini-Zarr (`tier1_15min.zarr`) für die vier
  Sub-Step-Variablen geführt — separat vom Stunden-Zarr, um die
  Shape-Asymmetrie sauber zu kapseln. Aufwand pro Feature: 15–30
  Zeilen Code. Aktivierung sobald 2–4 Wochen Archiv vorliegen und
  die ersten Tunings auf Stundenbasis abgeschlossen sind.
]

// =============================================================================
= 10. Zeitplan und Meilensteine
// =============================================================================
#tbl(
  columns: (auto, 1fr, auto, 1fr),
  header: ([Phase], [Inhalt], [Dauer], [Meilenstein]),
  ([M0], [Archiv-Cronjobs (Kap. 9) — SOFORT], [1–2 Tage], [Tägl. ICON-Mitschnitt (00/03/06/09) + Tier-2-Trigger läuft]),
  ([M1], [Region & AHD (Komp. A)], [2 Wo.], [AHD-Profile für 20 Regionen]),
  ([M2], [ICON-Pipeline (Komp. B)], [3 Wo.], [Eine Region, eine Saison im Speicher]),
  ([M3], [Modellkern v0 (Komp. C)], [5 Wo.], [Reproduktion Liechti-Figs 3+4]),
  ([M4], [IGC-Pipeline (Komp. D)], [4 Wo.], [Validierungsdatensatz Saison 2025]),
  ([M5], [Erste End-to-End-Läufe], [2 Wo.], [Bias-Plots, erste Findings]),
  ([M6], [Parameter-Tuning (Komp. E)], [4 Wo.], [Kalibrierte Parameter pro Region]),
  ([M7], [Multi-Region-Skalierung], [3 Wo.], [Gesamter Alpenraum operationell]),
  ([M8], [Dokumentation & Publikation], [2 Wo.], [Reproduzierbare Pipeline]),
)

Gesamtdauer ca. 25 Wochen netto. Bei Teilzeit entsprechend länger. M1, M2 und M4 sind
parallelisierbar. *M0 hat höchste Priorität und läuft unabhängig vom übrigen Fortschritt*
— jeder ungesammelte gute Thermiktag ist als nativer ICON-GRIB2 unwiederbringlich
verloren (siehe Kap. 9).

// =============================================================================
= 11. Risiken und offene Fragen
// =============================================================================
#tbl(
  columns: (1fr, 1fr),
  header: ([Risiko], [Mitigation]),
  ([Auflösungsgrenzen ICON-D2 in tiefen Tälern], [Mehrere Stützpunkte pro Region, Mittelung; Korrekturfaktor pro Höhenklasse]),
  ([IGC-Selektion: Piloten fliegen beste Bärte, nicht Mittel], [Modell-Output als Q90 reporten; ggf. explizite Bart-Statistik]),
  ([WeGlide-API: Firewall blockt Server-IPs; ToS], [API-Key proaktiv anfragen; aggressives Caching, höfliches Crawling; OLC als Fallback]),
  ([HTOP_DC-Qualität für Blue Days nicht validiert], [Initial-Check an klaren Strahlungstagen im Inntal]),
  ([Talwind-/Konvergenzeffekte in Phase 1 nicht erfasst], [Regtherm-Kopplung als Phase-2-Erweiterung dokumentiert (5.5); ICON-W und Windvarianz als Datengrundlage]),
  ([Regionsschnitte ad hoc], [Iteration: grob starten, bei systematischen Biases verfeinern]),
)

// =============================================================================
= 12. Anhang
// =============================================================================

== 12.1 Software-Stack (Vorschlag)
Python 3.11+, Stack vollständig open source:
- *Numerik:* numpy, scipy, xarray
- *Geodaten:* geopandas, rasterio, shapely, pyproj
- *Regionalisierung:* PySAL/spopt (max_p_regions, skater), HydroBASINS/EU-Hydro, Alpenkonvention-Perimeter als Maske
- *NWP:* cfgrib (eccodes-Backend), herbie (DWD-Loader)
- *IGC:* aerofiles oder eigener Parser; WeGlide-Python-Client (PyPI)
- *Optimierung:* scikit-optimize, optuna
- *Visualisierung:* matplotlib, plotly, folium
- *Speicher:* Zarr für das wachsende ICON-Zeitreihenarchiv, NetCDF für einzelne Modelldaten, Parquet für Flugaggregate

== 12.2 Referenzliteratur
- Liechti, O. & Neininger, B. (1994): ALPTHERM — A PC-based model for atmospheric convection over complex topography. Technical Soaring 18(3), 73–78.
- Liechti, O. (2002): Regtherm — Regional coupling of valley wind systems and sea breezes. OSTIV Publication.
- Richter-Trummer, D. (2011): Verifikation des Grenzschichtmodells ALPTHERM anhand Flugdaten. Bachelorarbeit, Universität Innsbruck.
- Hindman, E. et al. (2007): Verification of TopTask Competition. Technical Soaring.
- Whiteman, C.D. (1982): Breakup of temperature inversions in deep mountain valleys. J. Appl. Meteor. 21, 270–289.
- Whiteman, C.D. (2000): Mountain Meteorology — Fundamentals and Applications. Oxford University Press.
- Zardi, D. & Whiteman, C.D. (2013): Diurnal Mountain Wind Systems. In: Mountain Weather Research and Forecasting, Springer, 35–119.
- Lehner, B. & Grill, G. (2013): Global river hydrography and network routing — HydroSHEDS / HydroBASINS. Hydrol. Process. 27, 2171–2186.
- Duque, J.C. et al. (2012): The max-p-regions problem. J. Regional Science 52, 397–419.
- DWD ICON Database Reference, Numerical Weather Prediction (opendata.dwd.de).

== 12.3 Verzeichnisstruktur (Vorschlag)
#formula[
alptherm-icon/ \
├── data/ \
│   ├── dem/                 \# Copernicus DEM (statisch) \
│   ├── regions/             \# GeoJSON-Polygone + AHD-NetCDFs \
│   ├── icon/                \# ICON GRIB2 / Zarr (Cache + Archiv) \
│   ├── igc/                 \# IGC-Files (Cache) \
│   └── aggregates/          \# Flugaggregate (Parquet) \
├── src/ \
│   ├── regions/             \# Komp. A \
│   ├── icon\_pipeline/       \# Komp. B \
│   ├── model/               \# Komp. C — Modellkern \
│   ├── igc\_pipeline/        \# Komp. D \
│   └── validation/          \# Komp. E \
├── notebooks/               \# Explorative Analyse \
├── configs/                 \# Region-Definitions, Parameter \
└── tests/
]

== 12.4 Historische ICON-Daten — Archiv-Optionen
Der DWD bietet kein Langzeitarchiv (nur 2-Tage-Fenster). Für Stufe-3-Klimatologie und
Rück-Validierung:

#tbl(
  columns: (auto, auto, auto, 1fr),
  header: ([Quelle], [Modell / Auflösung], [Abdeckung], [Eignung & Caveat]),
  ([Open-Meteo Historical Forecast API], [ICON-D2/-EU, Punktabfrage], [ab ~2021], [Pragmatischster Weg. Keine Auth, CC-BY-4.0. Nicht für lange Zeitreihen (Modellversionswechsel); für Varianzmuster unkritisch]),
  ([Open Climate Fix Zarr (HuggingFace)], [ICON-EU (7 km), volle Felder], [ab März 2023], [Näher an Rohdaten. Nur EU-Auflösung — für grobe Varianzmuster ausreichend]),
  ([ERA5 Reanalyse], [~25 km, lückenlos], [ab 1940], [Zu grob für Talwinde; nur als Ergänzung für großräumige Luftmassengrenzen]),
)

#note[
  *Konsistenz-Caveat:* Wird Stufe 3 auf Open-Meteo gerechnet, Komponente B aber auf
  nativem DWD-GRIB2, muss die HBAS_SC-Definition identisch sein. Open-Meteo
  interpoliert/prozessiert teils nach. Für relative Varianzmuster irrelevant, aber Quelle
  dokumentieren.
]

== 12.5 Nächster konkreter Schritt
Empfehlung: Mit Komponente A für eine einzelne Pilotregion starten — z.B. das
Unterinntal/Steinberge-Gebiet, das auch Richter analysiert hat. Vorteile: vorhandene
Referenzdaten, überschaubarer Scope, sofort testbare Geometrie. Parallel den
ICON-Variablenkatalog (Komp. B) implementieren, zunächst für genau diesen Punkt — und vor
allem den M0-Archiv-Cronjob (Kap. 9) sofort starten. Erst nach erfolgreichem
End-to-End-Test auf den ganzen Alpenraum skalieren.
