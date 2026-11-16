"""End-to-end persistence test: fetch -> raw -> load, all offline in a tmp dir."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from trinity.analysis.loaders import (
    _apply_power_corrections,
    load_activities,
    load_settings,
    load_wellness,
)
from trinity.config import get_config
from trinity.connectors.fetch import fetch_all
from trinity.connectors.intervals import IntervalsClient
from trinity.storage.io import load_json
from trinity.storage.paths import watermark_path


def test_unconfigured_open_wearables_returns_no_recovery(tmp_data_dir, monkeypatch):
    """Regression: missing OW credentials used to overlay the synthetic athlete
    onto real Garmin wellness (HRV ~65 / RHR ~50 for Feb–Jun 2026)."""
    monkeypatch.setenv("TRINITY_OFFLINE", "false")
    monkeypatch.delenv("OPEN_WEARABLES_API_KEY", raising=False)
    monkeypatch.delenv("OPEN_WEARABLES_USER_ID", raising=False)
    get_config.cache_clear()
    from trinity.connectors.openwearables import OpenWearablesClient
    rows = OpenWearablesClient().recovery_summary(dt.date(2026, 3, 1), dt.date(2026, 3, 31))
    assert rows == []


def test_fetch_persists_and_reloads(tmp_data_dir):
    res = fetch_all(full_resync=True)
    assert res["offline"] is True
    assert res["activities"] > 0

    acts = load_activities()
    well = load_wellness()
    settings = load_settings()
    assert not acts.empty and set(acts["sport"]) <= {"swim", "bike", "run"}
    assert not well.empty
    assert settings.get("bike", {}).get("ftp_w")
    # Unconfigured OW must not retag synthetic recovery as open_wearables.
    assert set(well["source"].dropna()) <= {"intervals"}

    wm = load_json(watermark_path("intervals"))
    assert wm.get("last_synced")


def test_incremental_fetch_preserves_history(tmp_data_dir):
    """Regression (C1): a re-run must merge partitions, not overwrite them, so
    the 7-day incremental window can never delete older activities."""
    fetch_all(full_resync=True)
    full = load_activities()
    assert not full.empty
    n_full = len(full)

    # Ordinary incremental re-run (watermark present -> only the recent tail).
    res = fetch_all()
    assert res["activities"] < n_full  # the incremental slice itself is small

    after = load_activities()
    assert len(after) == n_full, "incremental fetch dropped history"
    assert after["id"].is_unique, "merge introduced duplicate activity ids"


def _ride(day: int, sport: str = "bike", np_w: float = 200.0, tss: float = 100.0) -> dict:
    return {"date": dt.date(2026, 8, day), "sport": sport, "np_w": np_w, "avg_w": np_w - 20,
            "intensity_factor": 0.7, "tss": tss}


def _corrected(rows: list[dict], factor: float = 1.25) -> pd.DataFrame:
    corr = [{"from": "2026-08-14", "to": "2026-08-25", "sports": ["bike"], "factor": factor}]
    return _apply_power_corrections(pd.DataFrame(rows), corr)


def test_power_correction_scales_load_quadratically():
    """A meter 25% low understates watts by 25% but TSS by 56%, because TSS goes
    as NP x IF and IF is NP/FTP. Scaling load linearly would silently under-record
    every affected session's training stress."""
    out = _corrected([_ride(20)]).iloc[0]
    assert out["np_w"] == pytest.approx(250.0)
    assert out["avg_w"] == pytest.approx(225.0)
    assert out["intensity_factor"] == pytest.approx(0.875)
    assert out["tss"] == pytest.approx(156.25)


def test_power_correction_respects_window_and_sport():
    out = _corrected([_ride(13), _ride(14), _ride(25), _ride(26), _ride(20, sport="run")])
    untouched = out[out.date.isin([dt.date(2026, 8, d) for d in (13, 26)]) | (out.sport == "run")]
    assert (untouched["np_w"] == 200.0).all(), "correction leaked outside its window"
    assert (untouched["tss"] == 100.0).all()
    edges = out[out.date.isin([dt.date(2026, 8, 14), dt.date(2026, 8, 25)])]
    assert (edges["np_w"] == 250.0).all(), "window bounds must be inclusive"


def test_power_correction_tolerates_missing_data():
    """Rides with no power recorded, and frames lacking power columns entirely,
    must pass through rather than raise."""
    out = _corrected([_ride(20, np_w=float("nan"))])
    assert pd.isna(out.iloc[0]["np_w"])
    assert out.iloc[0]["tss"] == pytest.approx(156.25)

    bare = _apply_power_corrections(
        pd.DataFrame([{"date": dt.date(2026, 8, 20), "sport": "bike"}]),
        [{"from": "2026-08-14", "to": "2026-08-25", "factor": 1.25}],
    )
    assert len(bare) == 1


def test_no_corrections_configured_is_a_no_op():
    rows = pd.DataFrame([_ride(20)])
    assert _apply_power_corrections(rows.copy(), []).equals(rows)


def test_offline_push_is_idempotent(tmp_data_dir):
    client = IntervalsClient()
    events = [
        {"category": "WORKOUT", "external_id": "x1", "name": "A", "start_date_local": "2026-06-01T09:00:00"},
        {"category": "WORKOUT", "external_id": "x2", "name": "B", "start_date_local": "2026-06-02T09:00:00"},
    ]
    client.push_events(events)
    client.push_events(events)  # same ids -> upsert, no duplicates
    stored = load_json(client.config.raw_dir / "intervals" / "_pushed_events.json")
    assert len(stored["events"]) == 2


def test_manually_supplied_thresholds_keep_their_provenance(synth_frames):
    """A value from manual_metrics.json must not be labelled as a connector value."""
    from trinity.analysis.loaders import _merge_manual_metrics, data_as_of
    from trinity.analysis.profile_builder import build_profile

    acts, well, base = synth_frames
    fetched = {**base, "bike": {**base.get("bike", {}), "ftp_w": 280, "lthr_bpm": 165},
               "run": {**base.get("run", {}), "threshold_pace_s_per_km": 210}}
    settings = _merge_manual_metrics(
        fetched, {"bike": {"ftp_w": 250}, "run": {"threshold_pace_s_per_km": 215}})
    prof = build_profile(settings, acts, well, data_as_of(acts, well))
    assert prof.bike.ftp_w.value == 250
    assert "manual" in prof.bike.ftp_w.source
    assert "manual" in prof.run.threshold_pace_s_per_km.source
    assert "intervals" in prof.bike.lthr_bpm.source
