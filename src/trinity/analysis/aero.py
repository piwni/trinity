"""Aerodynamic drag (CdA) from race files, and bike-split projection.

One set of physics constants (:class:`Physics`) serves both the fit and the
projection. That matters: a CdA fitted in a particular power meter's watts is an
*effective* number, only valid when the same rolling resistance, air density,
drivetrain loss and mass are used to project with it.

Two corrections that push windy-day fits in a known direction are applied:

1. **Wind height.** Weather-model wind is quoted at 10 m. At rider height (~1 m)
   over open terrain the log wind profile gives ~0.6-0.7x of that. Applying the
   raw 10 m figure attributes too much of the rider's power to wind, and so too
   little to drag -- it biases CdA LOW.
2. **Course orientation.** The head component a rider feels is
   ``v_wind * cos(wind_from - heading)``, resolved per segment from the GPS
   bearing. "Half the distance straight into the wind" is the worst case for a
   course aligned with the wind, and also biases CdA low on any other course.

Descents are capped at a realistic maximum speed (riders coast and brake), and
fits use only steady, pedalled, near-flat segments.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Physics:
    """The constants every fit and every projection share."""

    rho: float = 1.20            # kg/m^3, ~15-20 C at low altitude
    crr: float = 0.0045          # road tyres on mixed tarmac
    eta: float = 0.975           # drivetrain efficiency
    mass_kg: float = 83.0        # rider 73 + bike ~9 + bottles/kit
    g: float = 9.81
    # 10 m station wind -> rider height. Log profile u(z)=u*ln(z/z0): with
    # z=1 m, z_ref=10 m and open-terrain roughness z0~0.015 m this is ~0.65.
    wind_height_factor: float = 0.65
    max_speed_ms: float = 16.0   # ~58 km/h: coasting / braking on descents


DEFAULT_PHYSICS = Physics()

# Effective CdA rises over a long race: sitting up to drink ~1 L/h, aid stations,
# fatigue-driven position loss after ~3 h. Added to a 70.3-derived CdA when
# projecting a full-distance bike.
IM_POSITION_DECAY = (0.003, 0.008)


@dataclass(frozen=True)
class Chunk:
    """A course segment: length, grade (rise/run) and travel bearing (deg)."""

    length_m: float
    grade: float
    heading_deg: float


@dataclass(frozen=True)
class FitResult:
    cda: float
    n_chunks: int
    used_s: float


def head_wind(wind_ms_10m: float, wind_from_deg: float, heading_deg: float,
              phys: Physics = DEFAULT_PHYSICS) -> float:
    """Head-wind component at rider height (positive = into the rider)."""
    return (wind_ms_10m * phys.wind_height_factor
            * math.cos(math.radians(wind_from_deg - heading_deg)))


def power_needed(v: float, cda: float, grade: float, headwind: float,
                 phys: Physics = DEFAULT_PHYSICS) -> float:
    """Crank power to hold ground speed ``v`` (m/s)."""
    theta = math.atan(grade)
    air = v + headwind
    aero = 0.5 * phys.rho * cda * air * abs(air) * v
    roll = phys.crr * phys.mass_kg * phys.g * math.cos(theta) * v
    grav = phys.mass_kg * phys.g * math.sin(theta) * v
    return (aero + roll + grav) / phys.eta


def speed_at_power(power: float, cda: float, grade: float, headwind: float,
                   phys: Physics = DEFAULT_PHYSICS) -> float:
    """Steady ground speed at constant power; capped on descents."""
    hi = phys.max_speed_ms
    if power_needed(hi, cda, grade, headwind, phys) <= power:
        return hi
    lo = 0.3
    for _ in range(60):
        mid = (lo + hi) / 2
        if power_needed(mid, cda, grade, headwind, phys) < power:
            lo = mid
        else:
            hi = mid
    return lo


def simulate(chunks: list[Chunk], power: float, cda: float, wind_ms_10m: float,
             wind_from_deg: float, phys: Physics = DEFAULT_PHYSICS) -> float:
    """Seconds to ride ``chunks`` at constant power."""
    total = 0.0
    for c in chunks:
        hw = head_wind(wind_ms_10m, wind_from_deg, c.heading_deg, phys)
        total += c.length_m / speed_at_power(power, cda, c.grade, hw, phys)
    return total


def simulate_any_wind(chunks: list[Chunk], power: float, cda: float, wind_ms_10m: float,
                      phys: Physics = DEFAULT_PHYSICS, directions: int = 8) -> float:
    """Expected time when race-day wind direction is unknown: the mean over
    ``directions`` equally spaced bearings."""
    return float(np.mean([simulate(chunks, power, cda, wind_ms_10m, d, phys)
                          for d in np.linspace(0, 360, directions, endpoint=False)]))


def _bearing(lat1, lng1, lat2, lng2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lng2 - lng1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _chunk_table(df: pd.DataFrame, chunk_m: float) -> pd.DataFrame:
    """Per-chunk duration, distance, grade, heading and (if present) mean power."""
    d = df.dropna(subset=["t", "dist"]).sort_values("t").reset_index(drop=True).copy()
    d["dist"] = d["dist"] - d["dist"].min()
    d["dt"] = d["t"].diff().fillna(0.0)
    d["k"] = (d["dist"] // chunk_m).astype(int)
    rows = []
    for _, g in d.groupby("k"):
        if len(g) < 3:
            continue
        span = g["dist"].iloc[-1] - g["dist"].iloc[0]
        secs = g["t"].iloc[-1] - g["t"].iloc[0]
        if span < chunk_m * 0.6 or secs <= 0:
            continue
        rise = (g["alt"].iloc[-1] - g["alt"].iloc[0]) if "alt" in g else 0.0
        heading = (_bearing(g["lat"].iloc[0], g["lng"].iloc[0], g["lat"].iloc[-1], g["lng"].iloc[-1])
                   if {"lat", "lng"}.issubset(g.columns) and g["lat"].notna().all() else 0.0)
        row = {"secs": secs, "span": span, "v": span / secs,
               "grade": rise / span if span else 0.0, "heading": heading}
        if "watts" in g:
            w = g["watts"].fillna(0.0)
            row["power"] = float((w * g["dt"]).sum() / max(g["dt"].sum(), 1e-9))
        rows.append(row)
    return pd.DataFrame(rows)


def chunks_from_stream(df: pd.DataFrame, chunk_m: float = 1000.0) -> list[Chunk]:
    """A course (for projection) from a GPS/altitude stream.

    Chunk spans under-count the road (the gap to the next sample at every
    boundary, plus chunks dropped across GPS dropouts), so lengths are rescaled
    to the stream's true distance. Without this a time-based fit compares the
    full race time against a shorter course and inflates CdA.
    """
    tab = _chunk_table(df, chunk_m)
    chunks = [Chunk(float(r.span), float(np.clip(r.grade, -0.15, 0.15)), float(r.heading))
              for r in tab.itertuples()]
    dist = df["dist"].dropna()
    true_len = float(dist.max() - dist.min()) if len(dist) else 0.0
    got = sum(c.length_m for c in chunks)
    if got > 0 and true_len > 0:
        k = true_len / got
        chunks = [Chunk(c.length_m * k, c.grade, c.heading_deg) for c in chunks]
    return chunks


def fit_cda(df: pd.DataFrame, *, wind_ms: float, wind_from_deg: float,
            phys: Physics = DEFAULT_PHYSICS, chunk_m: float = 250.0) -> FitResult:
    """Effective CdA from a ride file with power, by energy balance over steady,
    pedalled, near-flat chunks: CdA = sum(residual power) / sum(aero term)."""
    tab = _chunk_table(df, chunk_m)
    if tab.empty or "power" not in tab:
        return FitResult(float("nan"), 0, 0.0)
    ok = tab[(tab["power"] > 80) & (tab["grade"].abs() < 0.04)
             & (tab["v"] > 6.0) & (tab["v"] < phys.max_speed_ms - 0.5)]
    num = den = 0.0
    for r in ok.itertuples():
        theta = math.atan(r.grade)
        hw = head_wind(wind_ms, wind_from_deg, r.heading, phys)
        air = r.v + hw
        x = 0.5 * phys.rho * air * abs(air) * r.v
        y = (r.power * phys.eta
             - phys.crr * phys.mass_kg * phys.g * math.cos(theta) * r.v
             - phys.mass_kg * phys.g * math.sin(theta) * r.v)
        num += y * r.secs
        den += x * r.secs
    cda = num / den if den > 0 else float("nan")
    return FitResult(round(cda, 4), len(ok), float(ok["secs"].sum()))


def fit_cda_from_time(chunks: list[Chunk], power: float, actual_s: float,
                      wind_ms: float, wind_from_deg: float,
                      phys: Physics = DEFAULT_PHYSICS) -> float:
    """CdA that makes a constant-``power`` ride over ``chunks`` take ``actual_s``.
    For files without power (e.g. a failed meter), at an assumed power."""
    lo, hi = 0.08, 0.60
    for _ in range(60):
        mid = (lo + hi) / 2
        if simulate(chunks, power, mid, wind_ms, wind_from_deg, phys) < actual_s:
            lo = mid        # too fast -> more drag
        else:
            hi = mid
    return round((lo + hi) / 2, 4)


def power_for_time(chunks: list[Chunk], target_s: float, cda: float, wind_ms: float,
                   phys: Physics = DEFAULT_PHYSICS) -> float:
    """Constant power needed for ``target_s`` with unknown wind direction."""
    lo, hi = 80.0, 400.0
    for _ in range(50):
        mid = (lo + hi) / 2
        if simulate_any_wind(chunks, mid, cda, wind_ms, phys) > target_s:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 1)
