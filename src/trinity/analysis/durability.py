"""Aerobic decoupling and output fade, computed from activity streams.

Decoupling (Pa:Hr for runs, Pw:Hr for rides) is the drift in output-per-heartbeat
between the first and second half of a steady effort. It is the long-course
durability metric that matters more than peak threshold: it answers "how much do
you still have at hour four", which is what decides an Ironman.

    decoupling % = (output/HR first half - output/HR second half)
                   / (output/HR first half) x 100

TWO THINGS THIS MODULE EXISTS TO GET RIGHT, because both are easy to get wrong in
ways the output does not advertise:

1. **Stops are not slow running.** A mid-session stop must be excluded, or a
   6-minute pause in the second half turns a flat 27 km run into a fictional 12%
   decoupling. Stops are detected from a ROLLING window, not per sample.
2. **GPS jitter is not a stop.** Roughly 7% of 1 Hz samples report zero distance
   progress while the athlete is running, with the distance landing on the next
   sample. Filtering per-sample speed removes their time but keeps their distance,
   which reports a 4:03/km run as 3:47/km. The rolling window is what avoids this.

Because both failures are silent, every result carries ``reference_error_pct``
against a pace the caller already knows (the device's own moving pace) and refuses
to be ``trustworthy`` when the two disagree. Prefer checking that flag over
trusting the number.

Decoupling also has a blind spot worth knowing: if output AND heart rate both fall
-- the signature of running out of fuel rather than running out of fitness -- the
ratio barely moves and decoupling looks benign. ``fade_pct`` is reported alongside
precisely so that case is visible.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

# A stationary or walking athlete, in m/s. Above this we call it running/riding.
MIN_MOVING_SPEED_MS = 1.0
# Samples further apart than this are a paused clock, not a slow patch.
MAX_SAMPLE_GAP_S = 5.0
# Rolling window for the moving/stopped decision. Long enough to ride out
# per-sample GPS jitter, short enough to catch a genuine stop.
MOVING_WINDOW_S = 30
# Minimum moving duration before a first/second-half split means anything.
MIN_MOVING_S = 600
# How far the computed pace may sit from the caller's reference before we stop
# trusting the result.
DEFAULT_TOLERANCE_PCT = 2.0


@dataclass(frozen=True)
class Durability:
    """Result of a first-half / second-half durability split."""

    decoupling_pct: float
    # None when output is held by construction (ERG trainer), where a fade
    # figure would be false reassurance rather than a measurement.
    fade_pct: float | None
    first_half_output: float
    second_half_output: float
    first_half_hr: float
    second_half_hr: float
    mean_speed_ms: float
    moving_s: float
    stopped_s: float
    reference_error_pct: float | None
    trustworthy: bool

    @property
    def looks_like_fuel_failure(self) -> bool:
        """Output fell materially while heart rate ALSO fell. That is substrate
        running out, not the aerobic system reaching its ceiling -- and decoupling
        alone will under-report it."""
        if self.fade_pct is None:
            return False
        return self.fade_pct > 6.0 and self.second_half_hr < self.first_half_hr


def moving_mask(
    samples: pd.DataFrame,
    *,
    min_speed_ms: float = MIN_MOVING_SPEED_MS,
    window_s: int = MOVING_WINDOW_S,
    max_gap_s: float = MAX_SAMPLE_GAP_S,
) -> pd.Series:
    """Boolean mask of samples where the athlete is genuinely moving.

    Speed is assessed over a rolling ``window_s`` window rather than per sample,
    which is what keeps GPS jitter (zero-distance samples mid-run) from being
    mistaken for stops.
    """
    t, dist = samples["t"], samples["dist"]
    span_d = dist - dist.shift(window_s)
    span_t = t - t.shift(window_s)
    rolling = (span_d / span_t.where(span_t > 0)).bfill()
    gap = t.diff().fillna(1.0)
    return (gap <= max_gap_s) & (rolling > min_speed_ms)


def aerobic_decoupling(
    samples: pd.DataFrame,
    *,
    output: str = "speed",
    reference_pace_s_per_km: float | None = None,
    tolerance_pct: float = DEFAULT_TOLERANCE_PCT,
    skip_warmup_s: float = 0.0,
    measure_fade: bool = True,
) -> Durability | None:
    """Split a steady effort in half by MOVING TIME and measure the drift.

    ``samples`` needs ``t`` (seconds), ``dist`` (metres) and ``hr``; ``watts`` too
    when ``output="power"``. Returns None when there is not enough moving data to
    say anything.

    Halves are by moving time (the TrainingPeaks convention) so the same code
    serves rides and runs.

    Pass ``measure_fade=False`` when output is controlled by a device (ERG mode
    holds power by construction): decoupling stays valid, fade does not.
    """
    need = {"t", "dist", "hr"} | ({"watts"} if output == "power" else set())
    if samples is None or samples.empty or not need.issubset(samples.columns):
        return None

    df = samples.dropna(subset=["t", "dist"]).sort_values("t").reset_index(drop=True)
    if len(df) < 2:
        return None
    df["dt"] = df["t"].diff().fillna(1.0)
    df["dd"] = df["dist"].diff().fillna(0.0).clip(lower=0.0)

    moving = moving_mask(df)
    stopped_s = float(df.loc[~moving, "dt"].sum())
    mv = df[moving & df["hr"].notna()].copy()
    if output == "power":
        mv = mv[mv["watts"].notna()]
    if mv.empty or mv["dt"].sum() < MIN_MOVING_S:
        return None

    mv["elapsed"] = mv["dt"].cumsum()
    if skip_warmup_s:
        mv = mv[mv["elapsed"] > skip_warmup_s]
        if mv.empty or mv["dt"].sum() < MIN_MOVING_S:
            return None
        mv["elapsed"] = mv["dt"].cumsum()

    moving_s = float(mv["dt"].sum())
    distance_m = float(mv["dd"].sum())
    mean_speed = distance_m / moving_s if moving_s else 0.0

    midpoint = mv["elapsed"].iloc[-1] / 2.0
    halves = (mv[mv["elapsed"] <= midpoint], mv[mv["elapsed"] > midpoint])
    if any(h.empty or h["dt"].sum() <= 0 for h in halves):
        return None

    out, hrs = [], []
    for half in halves:
        secs = float(half["dt"].sum())
        if output == "power":
            # Duration-weighted mean power.
            out.append(float((half["watts"] * half["dt"]).sum() / secs))
        else:
            out.append(float(half["dd"].sum()) / secs)
        hrs.append(float((half["hr"] * half["dt"]).sum() / secs))

    (o1, o2), (h1, h2) = out, hrs
    if o1 <= 0 or h1 <= 0 or h2 <= 0:
        return None
    r1, r2 = o1 / h1, o2 / h2
    decoupling = (r1 - r2) / r1 * 100.0
    fade = (o1 - o2) / o1 * 100.0

    ref_err: float | None = None
    trustworthy = True
    if reference_pace_s_per_km is not None and mean_speed > 0:
        computed = 1000.0 / mean_speed
        ref_err = (computed - reference_pace_s_per_km) / reference_pace_s_per_km * 100.0
        trustworthy = abs(ref_err) <= tolerance_pct

    return Durability(
        decoupling_pct=round(decoupling, 2),
        fade_pct=round(fade, 2) if measure_fade else None,
        first_half_output=round(o1, 3),
        second_half_output=round(o2, 3),
        first_half_hr=round(h1, 1),
        second_half_hr=round(h2, 1),
        mean_speed_ms=round(mean_speed, 4),
        moving_s=round(moving_s, 1),
        stopped_s=round(stopped_s, 1),
        reference_error_pct=None if ref_err is None else round(ref_err, 2),
        trustworthy=trustworthy,
    )


def summarise(results: list[Durability]) -> dict[str, float | int | None]:
    """Mean decoupling and fade across TRUSTWORTHY results only."""
    good = [r for r in results if r is not None and r.trustworthy]
    if not good:
        return {"decoupling_pct": None, "fade_pct": None, "sample_count": 0}
    fades = [r.fade_pct for r in good if r.fade_pct is not None]
    return {
        "decoupling_pct": round(float(np.mean([r.decoupling_pct for r in good])), 2),
        "fade_pct": round(float(np.mean(fades)), 2) if fades else None,
        "sample_count": len(good),
    }
