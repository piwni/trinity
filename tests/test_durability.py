"""Aerobic decoupling from activity streams.

The two bugs this module exists to prevent, both found by hand-rolling the
calculation and both silently wrong in the output:

1. Counting a mid-session STOP as slow running. A 6-minute stop in the second
   half turned a genuinely flat 27 km run into a fictional 12% decoupling.
2. Dropping GPS jitter samples per-sample. ~7% of samples read zero distance
   while running, so filtering on per-sample speed removes their TIME but keeps
   the distance elsewhere -- which reported a 4:03/km run as 3:47/km.

Hence every result carries a check against a known reference pace.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from trinity.analysis.durability import aerobic_decoupling


def stream(segments, *, jitter_every=0, stop_after=None, stop_s=0):
    """Build a 1 Hz sample stream from (duration_s, speed_ms, hr) segments.

    jitter_every: every Nth sample reports no distance progress (GPS jitter),
      with the distance showing up on the following sample -- the real-world
      pattern that broke the per-sample filter.
    stop_after / stop_s: insert a stationary gap after N seconds of movement.
    """
    t, dist, hr = [], [], []
    clock, covered, moved = 0.0, 0.0, 0.0
    for dur, speed, bpm in segments:
        for _ in range(int(dur)):
            if stop_after is not None and stop_s and moved >= stop_after:
                for _ in range(int(stop_s)):      # stationary: clock runs, distance does not
                    clock += 1
                    t.append(clock)
                    dist.append(covered)
                    hr.append(bpm * 0.8)
                stop_s = 0
            clock += 1
            moved += 1
            covered += speed
            t.append(clock)
            hr.append(bpm)
            dist.append(covered)
    df = pd.DataFrame({"t": t, "dist": dist, "hr": hr})
    if jitter_every:
        held = df["dist"].copy()
        mask = (df.index % jitter_every) == 0
        held[mask] = held.shift(1)[mask]          # freeze distance on jitter samples
        df["dist"] = held.bfill()
    return df


def test_steady_effort_has_no_decoupling():
    df = stream([(1800, 4.0, 150), (1800, 4.0, 150)])
    r = aerobic_decoupling(df)
    assert abs(r.decoupling_pct) < 0.5
    assert abs(r.fade_pct) < 0.5
    assert r.trustworthy


def test_cardiac_drift_shows_positive_decoupling():
    """Same speed, heart rate 10% higher in the second half -> ~9% decoupling.
    This is the HEALTHY pattern: output held, cost rose."""
    df = stream([(1800, 4.0, 150), (1800, 4.0, 165)])
    r = aerobic_decoupling(df)
    assert 8.0 < r.decoupling_pct < 10.0
    assert abs(r.fade_pct) < 0.5, "pace did not fade, only HR rose"


def test_pace_fade_at_constant_hr_shows_as_fade():
    df = stream([(1800, 4.0, 150), (1800, 3.6, 150)])
    r = aerobic_decoupling(df)
    assert r.fade_pct > 9.0
    assert r.decoupling_pct > 9.0


def test_falling_hr_with_collapsing_pace_is_caught_by_fade_not_decoupling():
    """The Wolsztyn 2026 signature, with that race's real numbers: pace 4:20 -> 4:48
    (3.847 -> 3.474 m/s) while heart rate ALSO fell, 147 -> 140. Because both the
    numerator and the denominator drop, decoupling roughly HALVES the apparent
    problem -- so a decoupling target alone would pass the exact failure it is
    supposed to prevent. Fade is what exposes it."""
    r = aerobic_decoupling(stream([(1800, 3.847, 147), (1800, 3.474, 140)]))
    assert r.fade_pct > 9.0, "pace fade must be visible"
    assert r.decoupling_pct < r.fade_pct * 0.7, (
        f"decoupling {r.decoupling_pct}% must materially under-report fade {r.fade_pct}%"
    )
    assert r.looks_like_fuel_failure, "output and HR both falling = substrate, not ceiling"


def test_cardiac_drift_is_not_reported_as_fuel_failure():
    """The healthy pattern (Syców): pace held, HR rose. Must NOT be flagged."""
    r = aerobic_decoupling(stream([(1800, 4.0, 158), (1800, 3.97, 163)]))
    assert not r.looks_like_fuel_failure
    assert r.decoupling_pct > r.fade_pct, "the cost rose faster than the output fell"


def test_a_mid_session_stop_is_not_counted_as_slow_running():
    """Regression for bug 1. Identical steady run, once clean and once with a
    6-minute stop dropped into the second half: the answers must agree."""
    clean = aerobic_decoupling(stream([(1800, 4.0, 150), (1800, 4.0, 150)]))
    stopped = aerobic_decoupling(
        stream([(1800, 4.0, 150), (1800, 4.0, 150)], stop_after=2400, stop_s=360)
    )
    assert abs(stopped.decoupling_pct - clean.decoupling_pct) < 1.0
    assert stopped.stopped_s >= 300
    assert abs(stopped.fade_pct) < 1.5


def test_gps_jitter_does_not_inflate_speed():
    """Regression for bug 2. A stream where every 15th sample reports no distance
    progress must still yield the true pace, not a ~7% faster one."""
    true_speed = 4.0
    r = aerobic_decoupling(stream([(1800, true_speed, 150), (1800, true_speed, 150)],
                                  jitter_every=15))
    got = 1000.0 / r.mean_speed_ms
    want = 1000.0 / true_speed
    assert abs(got - want) / want < 0.02, f"pace {got:.0f}s/km vs true {want:.0f}s/km"


def test_reference_pace_disagreement_marks_result_untrustworthy():
    """The guard that would have caught both bugs: compare against a pace we know
    independently (the device's own moving pace) and refuse to be trusted if it
    disagrees."""
    df = stream([(1800, 4.0, 150), (1800, 4.0, 150)])
    good = aerobic_decoupling(df, reference_pace_s_per_km=250.0)   # 1000/4.0
    assert good.trustworthy and abs(good.reference_error_pct) < 1.0
    bad = aerobic_decoupling(df, reference_pace_s_per_km=275.0)    # 10% off
    assert not bad.trustworthy
    assert bad.reference_error_pct < -5.0


def test_short_or_empty_stream_returns_nothing():
    assert aerobic_decoupling(pd.DataFrame({"t": [], "dist": [], "hr": []})) is None
    assert aerobic_decoupling(stream([(60, 4.0, 150)])) is None


def test_fade_is_not_reported_when_output_is_held_by_construction():
    """On an ERG trainer the power is held by the device, so 'power fade' is
    meaningless and a 0% reading would be false reassurance. Decoupling is still
    valid (heart rate is free to drift). The caller says when fade can't be
    measured, and the result must say 'unknown' rather than 'no fade'."""
    df = stream([(1800, 4.0, 150), (1800, 4.0, 160)])
    df["watts"] = 200.0
    r = aerobic_decoupling(df, output="power", measure_fade=False)
    assert r.fade_pct is None
    assert r.decoupling_pct > 5.0, "HR drift is still real on ERG"
    assert not r.looks_like_fuel_failure, "no fade measurement -> no fuel verdict"


def test_power_streams_use_watts_as_the_output():
    df = stream([(1800, 4.0, 150), (1800, 4.0, 150)])
    df["watts"] = np.r_[np.full(len(df) // 2, 200.0), np.full(len(df) - len(df) // 2, 180.0)]
    r = aerobic_decoupling(df, output="power")
    assert r.fade_pct > 9.0, "200 W -> 180 W is a 10% power fade"
