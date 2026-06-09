# Komponente C — ALPTHERM kernel spec & validation reference

Source: **Liechti & Neininger (1994), "ALPTHERM — A PC-based Model for
Atmospheric Convection over Complex Topography"**, Technical Soaring 18(3),
55–62 (`docs/references/Liechti_Neininger_1994_ALPTHERM.pdf`).

This is the M3 reference. The kernel (`model/`) must reproduce the paper's
worked example (Table 2 / Figs 3–4). Equation numbers below are the paper's.

> ⚠️ The numeric fixtures under `tests/fixtures/liechti1994/` are transcribed
> from a scanned 1994 print. The scalar columns (ground T, Td) are legible and
> high-confidence; the dense lift-rate digit matrix and some cloud base/top
> cells are **lower-confidence and should be spot-checked against the PDF**
> before being trusted as exact golden values.

## Radiation → surface fluxes
| Eq | Quantity | Formula | Constants |
|----|----------|---------|-----------|
| (1) | solar elevation ε | `sin ε = sinφ sinη cosβ − cosφ cosη cosβ sinα + cosφ sinβ cosα` | φ=47° (Swiss plateau) |
| (2a)(2b) | atmos. transmission | `T(z)=exp[−Γ·sin(ε_max)/sin ε]`, `Γ=Γ_max·exp(−z/z_Γ)` | Γ_max=0.323=ln0.74, z_Γ=2333 m |
| (3) | incoming radiation | `S = S₀·sin ε·T(z)` | S₀=1200 W/m² |
| (4) | absorbed | `Q_k = S·(1−A)` | A (albedo)=0.15 |
| (5) | outgoing | `Q_f = σ(T_S⁴ − μ·T_A⁴)` | σ=5.67e−8 W/m²K⁴ |
| (6) | humidity factor | `μ = 0.594 + 0.0416·√e` | e = vapour pressure [hPa] |
| (7) | radiation budget | `P = Q_k − Q_f` | |
| (8) | soil–air ΔT | `T_S − T_A = δ·P` | δ=0.005 K·m²/W |
| (9) | latent flux | `P_lat = Evap·(1−G)·P` | Evap=0.60 |
| (10) | sensible flux | `P_sens = (1−Evap)·(1−G)·P` | G=0.15 |

## Dynamics (per layer, Δz=100 m, Δt=120 s)
| Eq | Quantity | Formula | Constants |
|----|----------|---------|-----------|
| (11) | sensible heat into layer | `H_sens = P_sens·Δt·S_G` | S_G = layer surface area |
| (12a) | ΔT (linear regime) | `ΔT = ΔT₀·P_sens/P₀`  (if P_sens<P₀) | P₀=75 W/m² |
| (12b) | ΔT (saturated regime) | `ΔT = ΔT₀`  (if P_sens≥P₀) | ΔT₀=0.5 K (avg over Δz) |
| (13) | parcel mass | `m_p = H_sens/(c_p·ΔT)` | c_p=1005 J/kgK |
| (14) | evaporated water | `m_water = H_lat/L`, `H_lat=P_lat·Δt·S_G` | L=2.5e6 J/kg |
| (15) | energy gain n→n+1 | `dE/m_p = g·Δz·((ρf/ρp)_{n+1}+(ρf/ρp)_n)/2 − 1)` | ρp at new layer **incl. condensation** |
| (16) | cumulative energy | `E/m = Σ (dE/m_p)`  — rise while >0 | |
| (17) | updraft velocity | `v = (2·E/m)^0.5` | |
| (18a)(18b) | entrain/detrain | `En = En₀·|v|`, `De = De₀·|v|` | En₀=De₀=0.02 (accel) / 0.08 (slowing) [m/s]⁻¹ |
| (19) | wind reduction | `f_kin = 1 − r·u²` (multiplies energy) | r=1.65e−4 (km/h)⁻² |

**Cycle per Δt:** radiation→fluxes→per-layer parcels rise via (15)–(17) with
(18) mixing → each parcel deposits mass/water/heat in its equilibrium layer →
ground mass deficit compensated by **subsidence of the free atmosphere** →
apply synoptic advection/large-scale subsidence → repeat for the full day.

**Outputs (paper):** lift rate = mean v of parcels crossing an altitude **minus
1 m/s sailplane sink**, binned; cumulus base/top from condensation level; cloud
cover [octas] per layer.

## ICON supersession — which 1994 equations we still compute

The 1994 paper had to *parameterise* the surface energy balance from a handful
of ground observations (a single albedo, a fixed evaporation fraction, a soil–air
ΔT coefficient). ICON-D2 ships those quantities as **prognostic/diagnostic surface
fields**, so the corresponding empirical equations are replaced by direct field
reads. The convective dynamics (parcel theory) have no ICON equivalent and remain
the kernel. All replacement fields below are already archived
(`archive/variables.py`).

| 1994 eq(s) | What it parameterised | ICON field replacing it | Status |
|---|---|---|---|
| (1)–(4) | incoming/absorbed shortwave `Q_k` | `ASOB_S` (net SW at surface) | archived; **not yet wired into kernel** |
| (5)–(6) | outgoing longwave `Q_f` + humidity factor μ | `ATHB_S` (net LW at surface) | archived; not yet wired |
| (7) | radiation budget `P = Q_k − Q_f` | `ASOB_S + ATHB_S` | archived; not yet wired |
| (8) | soil–air ΔT (`T_S − T_A = δ·P`, δ=0.005) | `T_G` (skin/ground temperature) directly | archived; not yet wired |
| (9) | latent flux `P_lat` (Evap=0.60, G=0.15) | `ALHFL_S` (surface latent-heat flux) directly | archived; not yet wired |
| (10) | sensible flux `P_sens` (1−Evap, 1−G) | `ASHFL_S` (surface sensible-heat flux) directly | archived; **kernel currently uses `ASOB_S × sensible_fraction` proxy (v0.1)** |
| (9)/(10) | constant evaporation fraction `Evap=0.60` | `W_SO` (soil moisture, 4 layers) | archived; secondary — `ALHFL_S` already gives latent flux directly, `W_SO` is a cross-check / fallback partition input |
| cycle text | "apply synoptic advection / large-scale subsidence" | ICON `W` (vertical velocity) | planned |

> ⚠️ **Sign convention & de-averaging (v0.2 wiring).** ICON surface fluxes need
> two transforms before they can feed Eq (11)/(14):
> 1. **De-averaging.** `ASOB_S`, `ATHB_S`, `ASHFL_S`, `ALHFL_S` are published as
>    *means since model init* (W/m², GRIB `stepType=avg`) — **not** instantaneous and
>    **not** accumulated J/m². The interval-mean flux is recovered via
>    `(mean_i·t_i − mean_{i-1}·t_{i-1}) / (t_i − t_{i-1})`, where `t` is lead time since
>    init. The v0.1 proxy fed the running mean directly, which lags solar noon and
>    understates midday flux (verified on the inntal_steinberge run: ~570 W/m² peak net
>    SW de-averaged vs. ~250 W/m² running mean). See `model/forcing.py`.
> 2. **Sign.** DWD surface turbulent fluxes are downward-positive: an upward
>    sensible-heat flux into the CBL is **negative** `ASHFL_S`. The kernel wants
>    `P_sens > 0` for a heating surface, so `P_sens = −ASHFL_S` (likewise
>    `P_lat = −ALHFL_S`). `ASOB_S + ATHB_S` is already net-downward and needs no flip.

**Still computed by the kernel (no ICON equivalent):**

- (11), (14) — flux → per-layer energy (`H_sens`, `H_lat`); the *fluxes* now come
  from ICON, but the per-layer integration over `Δt·S_G` stays.
- (12)–(13) — parcel ΔT regime and parcel mass `m_p`.
- (15)–(17) — buoyant energy gain, cumulative energy, updraft velocity `v`.
- (18) — entrainment/detrainment closure.
- (19) — wind-shear energy reduction `f_kin`.

Constants that become **obsolete** once the ICON fields are wired in: `A` (0.15),
`δ` (0.005), `Evap` (0.60), `G` (0.15), `S₀` (1200), `Γ_max`/`z_Γ`, and the μ
coefficients (0.594, 0.0416) — all subsumed by `ASOB_S`/`ATHB_S`/`T_G`/
`ASHFL_S`/`ALHFL_S`. They remain documented above only as the validation reference
for reproducing the paper's worked example.

## Worked example (the validation case)
- **Region:** Swiss plateau ("Voralpen"), spring day (Swiss Nationals, May 1993).
- **Initial sounding:** weighted superposition of Payerne 02h radiosonde +
  Voralpen ground-station network 08h (Table 1 → `initial_sounding.csv`).
  Above ~1500 mASL the air is **dry** (Td collapses to −18 °C) — this is the
  "high pressure building up, dry air above 1500 m" setup driving Example 1.
- **Golden output:** `table2_forecast.csv` — 30-min ground T/Td plus cloud
  base/top onset. Clouds first form **13:00** (base ~1700 m), deepening to
  ~2400–2600 m by late afternoon; convection ends ~18:30.

### Figure 4 — subsidence sensitivity (the headline M3 test)
Identical initial profile, run at 4 subsidence rates → `figure4_subsidence.csv`:
| v_sub [m/h] | Result |
|---|---|
| 0 | "perfect gliding", 1–2/8 shallow cumulus |
| 5 | cumuli vertical extent clearly reduced, cover → 0–1/8 |
| 10 | clouds only briefly ~13:00 and again after 16:00 |
| 20 (+) | **no convective clouds at all** |

A correct kernel must show cloud cover and cumulus depth decreasing
monotonically with subsidence, vanishing by 20 m/h.
