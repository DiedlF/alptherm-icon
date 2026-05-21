# References

Bibliography for the alptherm-icon project. PDFs of all open-access works are
mirrored under [docs/references/](references/). The Whiteman (2000) textbook is
copyrighted and is referenced by ISBN only.

Mapping back to the implementation plan §10.2 and to the components defined in
the top-level [README](../README.md).

## Core model — Komp. C (Modellkern)

### Liechti & Neininger (1994)
**ALPTHERM — A PC-based Model for Atmospheric Convection over Complex Topography**
O. Liechti, B. Neininger. *Technical Soaring* 18(3), 55–62 (OSTIV).

The original ALPTHERM paper. Introduces the 1-D slab-convection formulation
driven by the area–height distribution (AHD) of the region. This is the
mathematical core that Komp. C reimplements.

- PDF: [references/Liechti_Neininger_1994_ALPTHERM.pdf](references/Liechti_Neininger_1994_ALPTHERM.pdf)
- Journal: <https://ts.ostiv.org/index.php/ts/article/view/218>

### Liechti (2002)
**REGTHERM 2001, Convection Model with Local Winds**
O. Liechti. *Technical Soaring* 26(1) (OSTIV).

Successor to ALPTHERM. Adds horizontal coupling between neighbouring AHD
regions so that secondary circulations (sea breezes, valley winds) modulate
the convective columns. Relevant once we move beyond a single-region pilot in
Komp. C.

- PDF: [references/Liechti_2002_REGTHERM_2001.pdf](references/Liechti_2002_REGTHERM_2001.pdf)
- Journal: <https://ts.ostiv.org/index.php/ts/article/view/279>

## Validation — Komp. D + E

### Richter-Trummer (2011)
**Verifikation des Grenzschichtmodells ALPTHERM anhand Flugdaten —
Vergleich von ALPTHERM-Vorhersagen mit Segelflugdaten**
D. Richter-Trummer. Bachelorarbeit, Institut für Atmosphären- und
Kryosphärenwissenschaften, Universität Innsbruck, Februar 2011.

Reference dataset and methodology for our Inntal / Steinberge pilot region
(plan §10.5). Defines how IGC-derived climb statistics are matched against
ALPTHERM column outputs — directly informs the Komp. D pipeline contract and
the Komp. E success metrics in plan §7.5.

- PDF: [references/Richter-Trummer_2011_BA.pdf](references/Richter-Trummer_2011_BA.pdf)
- Institutional page: <http://acinn.uibk.ac.at/node/806> (currently 404 —
  PDF mirrored from <https://streckenflug.at/download/Bac_Richter_final.pdf>)

### Liechti — Verification of Thermal Forecasts with Glider Flight Data
O. Liechti. *Technical Soaring* (OSTIV).

Author's own verification methodology using scored glider flights
(Viking Glide 2005). Complementary validation framing to Richter-Trummer;
useful when designing the metrics for Komp. E.

- PDF: [references/Liechti_Verification_of_Thermal_Forecasts.pdf](references/Liechti_Verification_of_Thermal_Forecasts.pdf)
- Journal: <https://journals.sfu.ca/ts/index.php/ts/article/view/164>

## Physical guardrails — Komp. A (region cuts)

### Whiteman (2000)
**Mountain Meteorology: Fundamentals and Applications**
C. D. Whiteman. Oxford University Press, 355 pp.
ISBN 0-19-513271-8.

Standard reference for diurnal mountain–valley wind systems, slope flows,
inversion behaviour, and the physical reasoning behind catchment-scale region
definitions in Komp. A. Copyrighted — not redistributed here.

- Publisher: <https://global.oup.com/academic/product/mountain-meteorology-9780195132717>
- Internet Archive lending copy: <https://archive.org/details/mountainmeteorol0000whit>

## Other useful entry points

- **WeGlide API docs (data source for Komp. D)**:
  <https://docs.weglide.org/creators/developers.html>
- **DWD ICON model documentation** (data source for Komp. B):
  <https://www.dwd.de/EN/research/weatherforecasting/num_modelling/01_num_weather_prediction_modells/icon_description.html>
