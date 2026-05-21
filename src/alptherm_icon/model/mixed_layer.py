"""Bulk mixed-layer evolution — Komp. C v0.1.

Encroachment model: given an initial morning θ(z) sounding and a cumulative
surface sensible-heat input H_cum [J/m²], find the CBL top z_i such that
the integrated θ deficit of the initial profile below z_i equals H_cum.
The mixed-layer θ_m is then θ_initial(z_i) (no entrainment closure).

Bin-wise parcel theory + AHD-weighted bin coupling (the real ALPTHERM) is
deferred to v0.2.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from alptherm_icon.model.thermo import C_P, w_star


@dataclass
class CblStep:
    """One time step of the mixed-layer state."""

    z_i_m: float  # CBL top height [m, same datum as the input profile]
    theta_m_K: float  # mixed-layer potential temperature [K]
    w_star_m_s: float  # convective velocity scale [m/s]
    h_cum_j_m2: float  # cumulative surface sensible heat input since t=0 [J/m²]


def cbl_top_from_cumulative_heat(
    theta_profile_K: np.ndarray,
    z_profile_m: np.ndarray,
    h_cum_j_m2: float,
    rho_kg_m3: float = 1.0,
) -> tuple[float, float]:
    """Find CBL top z_i by encroachment.

    Args:
        theta_profile_K: morning sounding potential temperature, ascending z.
        z_profile_m: heights of the profile, strictly ascending.
        h_cum_j_m2: cumulative surface sensible-heat input [J/m²].
        rho_kg_m3: bulk density approximation for the boundary layer [kg/m³].

    Returns:
        ``(z_i, theta_m)``: CBL top in the same datum as ``z_profile_m``,
        and the mixed-layer potential temperature taken as ``θ(z_i)`` of
        the initial profile (encroachment, no entrainment).
    """
    if theta_profile_K.shape != z_profile_m.shape:
        raise ValueError("theta_profile and z_profile must have the same shape")
    if theta_profile_K.size < 2:
        raise ValueError("need at least two profile levels")
    if np.any(np.diff(z_profile_m) <= 0):
        raise ValueError("z_profile must be strictly ascending")

    if h_cum_j_m2 <= 0:
        return float(z_profile_m[0]), float(theta_profile_K[0])

    # G(z_k) = c_p · ρ · ∫_{z[0]}^{z[k]} (θ(z[k]) - θ(z')) dz'
    # Computed via trapezoidal sum. G is non-decreasing for a stable
    # (monotone-θ) profile; we use it as a lookup curve for z_i(H_cum).
    g_curve = np.zeros_like(z_profile_m, dtype=np.float64)
    for k in range(1, z_profile_m.size):
        deficit = theta_profile_K[k] - theta_profile_K[: k + 1]
        widths = np.diff(z_profile_m[: k + 1])
        g_curve[k] = C_P * rho_kg_m3 * np.sum(0.5 * (deficit[:-1] + deficit[1:]) * widths)

    if h_cum_j_m2 >= g_curve[-1]:
        # Heat input exceeds what the profile can absorb — return top of profile.
        return float(z_profile_m[-1]), float(theta_profile_K[-1])

    k = int(np.searchsorted(g_curve, h_cum_j_m2))
    g_lo, g_hi = g_curve[k - 1], g_curve[k]
    z_lo, z_hi = z_profile_m[k - 1], z_profile_m[k]
    z_i = z_lo + (h_cum_j_m2 - g_lo) / (g_hi - g_lo) * (z_hi - z_lo)
    # θ at z_i by linear interpolation in z (the profile is θ(z), interpolating
    # in θ-vs-z space is consistent with the integral above).
    theta_m = theta_profile_K[k - 1] + (z_i - z_lo) / (z_hi - z_lo) * (
        theta_profile_K[k] - theta_profile_K[k - 1]
    )
    return float(z_i), float(theta_m)


def evolve_mixed_layer(
    theta_profile_K: np.ndarray,
    z_profile_m: np.ndarray,
    sensible_flux_w_m2: np.ndarray,
    dt_s: float,
    rho_kg_m3: float = 1.0,
) -> list[CblStep]:
    """Step the mixed-layer model forward through a series of surface fluxes.

    Args:
        theta_profile_K, z_profile_m: morning sounding (ascending z).
        sensible_flux_w_m2: surface sensible-heat flux at each step [W/m²].
            Length determines the number of output steps.
        dt_s: step duration [s].
        rho_kg_m3: bulk density approximation.

    Returns:
        One ``CblStep`` per input flux value. Step k uses the *forward* flux
        ``sensible_flux_w_m2[k]`` accumulated over ``dt_s`` — i.e. ``H_cum`` at
        step k is the sum of fluxes 0..k × dt_s.
    """
    h_cum = 0.0
    steps: list[CblStep] = []
    for flux in sensible_flux_w_m2:
        h_cum += max(float(flux), 0.0) * dt_s
        z_i, theta_m = cbl_top_from_cumulative_heat(
            theta_profile_K, z_profile_m, h_cum, rho_kg_m3=rho_kg_m3
        )
        z_i_above_sfc = max(z_i - float(z_profile_m[0]), 0.0)
        w = w_star(float(flux), z_i_above_sfc, theta_m, rho_kg_m3=rho_kg_m3)
        steps.append(
            CblStep(z_i_m=z_i, theta_m_K=theta_m, w_star_m_s=w, h_cum_j_m2=h_cum)
        )
    return steps
