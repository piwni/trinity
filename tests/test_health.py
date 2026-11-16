"""Health digest — lab results against their ranges and thresholds, plus wellness.

The doctor agent reads what this produces and adds judgment. The tests pin the
part that must not be left to judgment: which flag a number gets, and when the
honest answer is "cannot tell".
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from trinity.analysis import health
from trinity.cli import app
from trinity.models.health_digest import HealthFlag, HealthRecords, LabStatus
from trinity.models.profile import AthleteProfile, Sex
from trinity.storage.io import load_json
from trinity.storage.paths import health_digest_path, health_records_path

TODAY = dt.date(2026, 6, 20)
SAMPLE = Path(__file__).resolve().parents[1] / "data" / "fixtures" / "health.sample.yaml"


def _records(*results, date="2026-06-10", **panel) -> HealthRecords:
    return HealthRecords.model_validate(
        {"labs": [{"date": date, "results": list(results), **panel}]})


def _one(result: dict, sex: Sex | None = None, **kw):
    [marker] = health.assess_labs(_records(result, **kw), TODAY, sex)
    return marker


@pytest.mark.parametrize(("value", "flag"), [
    (60, HealthFlag.GREEN),
    (34.9, HealthFlag.AMBER),   # below 35: stage 1
    (20, HealthFlag.AMBER),     # thresholds are "below", so 20 itself is still amber
    (19.9, HealthFlag.RED),
])
def test_ferritin_screening_threshold_applies_inside_the_lab_range(value, flag):
    m = _one({"marker": "Ferritin", "value": value, "unit": "ng/mL",
              "ref_low": 15, "ref_high": 400})
    assert m.marker == "ferritin"
    assert m.lab_status is LabStatus.IN_RANGE
    assert m.flag is flag
    assert m.threshold_source and "Sim et al. 2019" in m.threshold_source
    if flag is not HealthFlag.GREEN:
        assert "inside the lab's reference range, but below the screening threshold" in m.notes


def test_vitamin_d_is_converted_from_ng_per_ml_before_the_threshold():
    # 19 ng/mL = 47.4 nmol/L -> amber; 21 ng/mL = 52.4 nmol/L -> green; 11 -> 27.5 -> red.
    assert _one({"marker": "25(OH)D", "value": 19, "unit": "ng/ml"}).flag is HealthFlag.AMBER
    assert _one({"marker": "Witamina D3 25(OH)", "value": 21, "unit": "ng/mL"}).flag is HealthFlag.GREEN
    assert _one({"marker": "vitamin d", "value": 11, "unit": "ng/mL"}).flag is HealthFlag.RED
    assert _one({"marker": "vitamin d", "value": 47, "unit": "nmol/L"}).flag is HealthFlag.AMBER


def test_unconvertible_unit_skips_the_threshold_and_says_so():
    m = _one({"marker": "Ferritin", "value": 10, "unit": "pmol/L"})
    assert m.flag is HealthFlag.UNKNOWN
    assert any("not one the rulebook converts" in n for n in m.notes)
    # With a lab range it still gets the lab-range flag.
    m = _one({"marker": "Ferritin", "value": 10, "unit": "pmol/L", "ref_low": 40, "ref_high": 600})
    assert m.flag is HealthFlag.AMBER and m.lab_status is LabStatus.LOW


def test_haemoglobin_threshold_is_sex_specific_and_withheld_when_sex_unknown():
    hb = {"marker": "Hb", "value": 12.5, "unit": "g/dL"}   # 125 g/L
    assert _one(hb, Sex.MALE).flag is HealthFlag.RED
    assert _one(hb, Sex.FEMALE).flag is HealthFlag.GREEN
    unknown = _one(hb, None)
    assert unknown.flag is HealthFlag.UNKNOWN
    assert any("sex-specific" in n for n in unknown.notes)


def test_out_of_lab_range_is_amber_never_graded_further():
    high = _one({"marker": "CK", "value": 2400, "unit": "U/L", "ref_high": 190})
    assert high.flag is HealthFlag.AMBER and high.lab_status is LabStatus.HIGH
    assert high.flag_basis == "above the lab's reference range"


def test_no_range_and_no_threshold_is_unknown_not_green():
    m = _one({"marker": "TSH", "value": 1.9, "unit": "mIU/L"})
    assert m.flag is HealthFlag.UNKNOWN
    assert m.flag_basis is None


def test_marker_outside_the_catalog_is_kept_as_entered():
    m = _one({"marker": "Lipoprotein(a)", "value": 12, "unit": "mg/dL", "ref_high": 30})
    assert m.marker == "Lipoprotein(a)" and m.in_catalog is False
    assert m.flag is HealthFlag.GREEN


def test_latest_result_wins_and_carries_the_previous_one():
    records = HealthRecords.model_validate({"labs": [
        {"date": "2026-06-10", "results": [{"marker": "ferritin", "value": 28, "unit": "ng/mL"}]},
        {"date": "2025-11-18", "results": [{"marker": "Ferrytyna", "value": 61, "unit": "ug/L"}]},
    ]})
    [m] = health.assess_labs(records, TODAY)
    assert (m.value, m.date, m.n_results) == (28, dt.date(2026, 6, 10), 2)
    assert (m.previous_value, m.previous_date) == (61, dt.date(2025, 11, 18))
    # ng/mL and ug/L are the same quantity but different spellings: no change is
    # computed across units, rather than guessing.
    assert m.change is None
    assert any("previous result was in" in n for n in m.notes)


def test_old_results_are_stale_and_do_not_drive_the_overall_flag():
    records = _records({"marker": "ferritin", "value": 10, "unit": "ng/mL"}, date="2024-01-05")
    digest = health.build_digest(records, pd.DataFrame(), pd.DataFrame(), today=TODAY)
    [m] = digest.labs
    assert m.stale and m.flag is HealthFlag.RED
    assert digest.overall_flag is HealthFlag.UNKNOWN
    assert digest.attention == []
    assert any("older than 365 days" in line for line in digest.missing)


def test_digest_without_any_records_states_what_is_missing():
    digest = health.build_digest(HealthRecords(), pd.DataFrame(), pd.DataFrame(), today=TODAY)
    assert digest.labs == [] and digest.overall_flag is HealthFlag.UNKNOWN
    joined = " | ".join(digest.missing)
    assert "no lab results" in joined and "no wellness data" in joined
    assert "no readiness report" in joined and "energy availability unknown" in joined


def test_digest_on_synthetic_athlete_reports_training_before_the_draw(synth_frames):
    acts, well, _ = synth_frames
    records = health.load_records(SAMPLE)
    profile = AthleteProfile()
    profile.anthropometrics.sex = Sex.MALE
    digest = health.build_digest(records, acts, well, profile=profile, today=TODAY)

    by_key = {m.marker: m for m in digest.labs}
    assert by_key["ferritin"].flag is HealthFlag.AMBER          # 28: inside range, below 35
    assert by_key["ferritin"].change == -33                     # 61 -> 28, same unit
    assert by_key["haemoglobin"].flag is HealthFlag.GREEN       # 146 g/L, male
    assert by_key["ck"].flag is HealthFlag.AMBER                # above the lab's range
    assert by_key["vitamin_d_25oh"].flag is HealthFlag.GREEN    # 31 ng/mL = 77 nmol/L
    assert digest.overall_flag is HealthFlag.AMBER
    assert any(line.startswith("AMBER Ferritin") for line in digest.attention)

    latest, earlier = digest.panels
    assert latest.date == dt.date(2026, 6, 10)
    assert latest.prior_tss is not None and latest.prior_hours is not None
    # The November panel predates the synthetic history: unknown, not zero.
    assert earlier.prior_tss is None
    assert digest.hrv_baseline_60d_ms.value is not None
    assert [e.kind for e in digest.events] == ["illness", "supplement", "injury"]


def test_bad_records_file_reports_every_problem(tmp_path):
    bad = tmp_path / "records.yaml"
    bad.write_text("labs:\n  - date: 2026-06-10\n    results:\n"
                   "      - {marker: ferritin, value: low, unit: ng/mL}\n"
                   "      - {marker: crp, value: 1, range: '0-5'}\n", encoding="utf-8")
    with pytest.raises(health.HealthRecordsError) as exc:
        health.load_records(bad)
    msg = str(exc.value)
    assert "2 problem(s)" in msg
    assert "labs.0.results.0.value" in msg and "labs.0.results.1.range" in msg


def test_health_command_end_to_end(tmp_data_dir):
    runner = CliRunner()
    assert runner.invoke(app, ["fetch"]).exit_code == 0

    # Before any records exist the digest is still built, from wellness alone.
    res = runner.invoke(app, ["health"])
    assert res.exit_code == 0, res.output
    assert "No lab results yet" in res.output
    assert load_json(health_digest_path())["labs"] == []

    health_records_path().parent.mkdir(parents=True)
    shutil.copy(SAMPLE, health_records_path())
    res = runner.invoke(app, ["health"])
    assert res.exit_code == 0, res.output
    saved = load_json(health_digest_path())
    assert {m["marker"] for m in saved["labs"]} >= {"ferritin", "haemoglobin", "vitamin_d_25oh"}

    health_records_path().write_text("labs: [{date: nope}]", encoding="utf-8")
    res = runner.invoke(app, ["health"])
    assert res.exit_code == 2
    assert "problem(s)" in res.output
