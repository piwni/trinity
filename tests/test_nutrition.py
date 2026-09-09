"""Nutrition pipeline tests — Fitatu parsing, energy availability, carb tiers,
and the observational recovery correlations."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from aithlete.analysis import nutrition as nut
from aithlete.connectors import fitatu
from aithlete.knowledge import load_nutrition_rules
from aithlete.models.common import Tracked
from aithlete.models.nutrition_digest import MicronutrientStatus
from aithlete.models.profile import AthleteProfile, Sex
from aithlete.models.readiness import ReadinessFlag

EMPTY_ACTS = pd.DataFrame(columns=["date", "sport", "tss", "duration_s", "avg_w", "intensity_class"])
EMPTY_WELL = pd.DataFrame(columns=["date", "hrv_rmssd_ms", "resting_hr_bpm", "sleep_hours", "weight_kg"])


def _profile(weight: float = 70.0) -> AthleteProfile:
    p = AthleteProfile()
    p.anthropometrics.weight_kg = Tracked.measured(weight, "test")
    p.anthropometrics.sex = Sex.MALE
    return p


def _daily(dates, *, items: int = 8, **overrides) -> pd.DataFrame:
    """Build a daily nutrition frame with all required columns (defaults to 0).

    Every nutrient defaults to FULL per-item coverage (``<field>_n == item_count``)
    and to nothing imputed, so tests that aren't about coverage aren't silently
    marked unknown. Pass e.g. ``iron_mg_n=1`` to simulate a sparsely-logged
    nutrient, or ``calcium_mg_i=4`` to simulate reference-filled items.
    """
    rows = []
    for i, d in enumerate(dates):
        row = {c: 0.0 for c in fitatu.NUTRIENT_FIELDS}
        row.update({fitatu.coverage_field(c): items for c in fitatu.NUTRIENT_FIELDS})
        row.update({fitatu.imputed_field(c): 0 for c in fitatu.NUTRIENT_FIELDS})
        row.update({"date": d, "meal_count": 4, "item_count": items, "alcoholic_drinks": 0})
        for k, v in overrides.items():
            row[k] = v[i] if isinstance(v, (list, tuple)) else v
        rows.append(row)
    return pd.DataFrame(rows)[fitatu.OUTPUT_COLUMNS]


# --- parser ----------------------------------------------------------------
def test_parse_and_daily_totals(tmp_path):
    csv = tmp_path / "meal_plan.csv"
    csv.write_text(
        'Date,Meal,"Products and dishes","calories (kcal)","Protein (g)","Carbohydrates (g)","Caffeine (mg)"\n'
        '2026-01-01,Breakfast,Oats,300,10,50,0\n'
        '2026-01-01,Lunch,Chicken,400,40,0,0\n'
        '2026-01-01,Lunch,Coffee,5,0,0,80\n'
        '2026-01-02,Breakfast,Toast,200,6,30,\n'
    )
    daily = fitatu.daily_totals(fitatu.parse_export(csv))
    assert list(daily["date"]) == [dt.date(2026, 1, 1), dt.date(2026, 1, 2)]
    d1 = daily.iloc[0]
    assert d1["calories_kcal"] == 705.0
    assert d1["protein_g"] == 50.0
    assert d1["caffeine_mg"] == 80.0
    assert d1["meal_count"] == 2          # Breakfast + Lunch
    assert d1["item_count"] == 3
    # blank caffeine on day 2 coerced to 0, not NaN
    assert daily.iloc[1]["caffeine_mg"] == 0.0


def test_per_item_coverage_counts_only_items_with_values(tmp_path):
    csv = tmp_path / "meal_plan.csv"
    csv.write_text(
        'Date,Meal,"Products and dishes","calories (kcal)","Iron (mg)","Caffeine (mg)"\n'
        '2026-01-01,Breakfast,Oats,300,2.5,\n'
        '2026-01-01,Lunch,Chicken,400,,\n'
        '2026-01-01,Dinner,Rice,350,,0\n'      # logged zero caffeine = a known value
    )
    daily = fitatu.daily_totals(fitatu.parse_export(csv)).iloc[0]
    assert daily["item_count"] == 3
    assert daily["calories_kcal_n"] == 3       # every item carries calories
    assert daily["iron_mg_n"] == 1             # only the oats
    assert daily["caffeine_mg_n"] == 1         # the explicit 0 counts as present
    assert daily["iron_mg"] == 2.5             # blanks still sum as 0 for the total


def test_legacy_partition_without_coverage_columns_survives_reimport(tmp_path, monkeypatch):
    """Partitions written before coverage tracking have no `<field>_n` columns.
    Re-import must extend them rather than raising, leaving coverage NaN."""
    monkeypatch.setenv("AITHLETE_DATA_DIR", str(tmp_path))
    from aithlete.analysis.loaders import load_nutrition
    from aithlete.config import get_config
    get_config.cache_clear()
    try:
        out = tmp_path / "raw" / "fitatu" / "nutrition" / "2026"
        out.mkdir(parents=True)
        (out / "data.csv").write_text(
            "date,calories_kcal,meal_count,item_count,alcoholic_drinks\n"
            "2026-01-01,2000,4,10,0\n"
        )
        new = tmp_path / "new.csv"
        new.write_text(
            'Date,Meal,"Products and dishes","calories (kcal)"\n2026-01-02,Breakfast,Oats,300\n'
        )
        fitatu.import_exports([new])
        get_config.cache_clear()
        stored = load_nutrition().set_index("date")
        assert len(stored) == 2                                   # legacy day retained
        assert pd.isna(stored.loc[dt.date(2026, 1, 1), "calories_kcal_n"])   # unknown, not 0
        assert stored.loc[dt.date(2026, 1, 2), "calories_kcal_n"] == 1
    finally:
        get_config.cache_clear()


def test_alcohol_proxy_excludes_non_alcoholic():
    assert fitatu._is_alcoholic("Red Wine") is True
    assert fitatu._is_alcoholic("Non-alcoholic beer") is False
    assert fitatu._is_alcoholic("Piwo bezalkoholowe IPA") is False
    assert fitatu._is_alcoholic("Banana") is False


def test_import_writes_partition(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHLETE_DATA_DIR", str(tmp_path))
    from aithlete.config import get_config
    get_config.cache_clear()
    try:
        csv = tmp_path / "m.csv"
        csv.write_text(
            'Date,Meal,"Products and dishes","calories (kcal)"\n'
            '2026-03-01,Breakfast,Oats,300\n'
        )
        res = fitatu.import_exports([csv])
        assert res["days"] == 1 and res["years"] == [2026]
        out = tmp_path / "raw" / "fitatu" / "nutrition" / "2026" / "data.csv"
        assert out.exists()
    finally:
        get_config.cache_clear()


def test_reimport_merges_and_supersedes_without_data_loss(tmp_path, monkeypatch):
    """A second import extends the store (B1) and updates overlapping dates by
    keep-last instead of summing or dropping prior months."""
    monkeypatch.setenv("AITHLETE_DATA_DIR", str(tmp_path))
    from aithlete.analysis.loaders import load_nutrition
    from aithlete.config import get_config
    get_config.cache_clear()
    try:
        a = tmp_path / "a.csv"
        a.write_text(
            'Date,Meal,"Products and dishes","calories (kcal)"\n'
            '2026-01-01,Breakfast,Oats,300\n'
        )
        fitatu.import_exports([a])
        b = tmp_path / "b.csv"
        b.write_text(
            'Date,Meal,"Products and dishes","calories (kcal)"\n'
            '2026-01-02,Breakfast,Toast,200\n'
            '2026-01-01,Breakfast,"Bigger breakfast",500\n'  # supersedes the first import
        )
        fitatu.import_exports([b])
        get_config.cache_clear()
        stored = load_nutrition().set_index("date")
        assert len(stored) == 2                                   # Jan-01 not lost (B1)
        assert stored.loc[dt.date(2026, 1, 1), "calories_kcal"] == 500.0  # superseded, not summed (B2)
        assert stored.loc[dt.date(2026, 1, 2), "calories_kcal"] == 200.0
    finally:
        get_config.cache_clear()


def test_overlapping_dates_across_files_are_not_summed(tmp_path, monkeypatch):
    """Two files in one import containing the same day -> keep-last, not summed (B2)."""
    monkeypatch.setenv("AITHLETE_DATA_DIR", str(tmp_path))
    from aithlete.analysis.loaders import load_nutrition
    from aithlete.config import get_config
    get_config.cache_clear()
    try:
        f1 = tmp_path / "f1.csv"
        f1.write_text('Date,Meal,"Products and dishes","calories (kcal)"\n2026-01-01,B,x,100\n')
        f2 = tmp_path / "f2.csv"
        f2.write_text('Date,Meal,"Products and dishes","calories (kcal)"\n2026-01-01,B,y,400\n')
        fitatu.import_exports([f1, f2])
        get_config.cache_clear()
        stored = load_nutrition()
        assert len(stored) == 1
        assert stored.iloc[0]["calories_kcal"] == 400.0  # later file wins, not 500.0
    finally:
        get_config.cache_clear()


# --- energy availability ---------------------------------------------------
def test_low_energy_availability_flags_red():
    dates = [dt.date(2026, 4, 1) + dt.timedelta(days=i) for i in range(20)]
    nutrition = _daily(dates, calories_kcal=1900.0, carbs_g=200.0, protein_g=120.0, fat_g=60.0)
    digest = nut.build_digest(nutrition, EMPTY_ACTS, EMPTY_WELL, profile=_profile(70))
    # EA = 1900 / 70 ≈ 27.1 -> below 30 -> RED
    assert digest.energy_availability_flag is ReadinessFlag.RED
    assert digest.energy_availability_kcal_per_kg.value < 30


def test_adequate_energy_availability_flags_green():
    dates = [dt.date(2026, 4, 1) + dt.timedelta(days=i) for i in range(20)]
    nutrition = _daily(dates, calories_kcal=3400.0, carbs_g=450.0, protein_g=130.0, fat_g=90.0)
    digest = nut.build_digest(nutrition, EMPTY_ACTS, EMPTY_WELL, profile=_profile(70))
    # EA = 3400 / 70 ≈ 48.6 -> GREEN
    assert digest.energy_availability_flag is ReadinessFlag.GREEN
    assert digest.protein_flag is ReadinessFlag.GREEN  # 130/70 ≈ 1.86 g/kg


def test_exercise_energy_lowers_ea():
    weight = 70.0
    acts = pd.DataFrame([
        {"date": dt.date(2026, 4, 5), "sport": "bike", "tss": 100, "duration_s": 7200,
         "avg_w": 200.0, "intensity_class": "moderate"},
    ])
    eee = nut.exercise_energy_by_day(acts, weight)
    # 200 W * 7200 s / 1000 = 1440 kJ ≈ 1440 kcal
    assert abs(eee.iloc[0]["exercise_kcal"] - 1440.0) < 1.0


def test_normalised_power_used_when_average_power_missing():
    """intervals.icu populates np_w far more often than avg_w. Falling through to
    the MET estimate when real power data is on file under-counts the ride."""
    acts = pd.DataFrame([
        {"date": dt.date(2026, 4, 5), "sport": "bike", "tss": 100, "duration_s": 7200,
         "avg_w": None, "np_w": 200.0, "intensity_class": "moderate"},
    ])
    kcal = nut.exercise_energy_by_day(acts, 70.0).iloc[0]["exercise_kcal"]
    # 200 W * 0.95 * 7200 s / 1000 = 1368 kJ, not the 1120 kcal MET would give.
    assert abs(kcal - 1368.0) < 1.0


def test_average_power_wins_over_normalised_power():
    acts = pd.DataFrame([
        {"date": dt.date(2026, 4, 5), "sport": "bike", "tss": 100, "duration_s": 7200,
         "avg_w": 200.0, "np_w": 240.0, "intensity_class": "moderate"},
    ])
    kcal = nut.exercise_energy_by_day(acts, 70.0).iloc[0]["exercise_kcal"]
    assert abs(kcal - 1440.0) < 1.0


def test_running_energy_scales_with_actual_pace():
    """A fixed MET underprices a fast runner. 13.5 km/h must cost distinctly more
    per hour than 10 km/h, and more than the 10.5 table value."""
    def kcal(dist_m: float) -> float:
        acts = pd.DataFrame([{
            "date": dt.date(2026, 4, 5), "sport": "run", "tss": 80, "duration_s": 3600,
            "distance_m": dist_m, "avg_w": None, "intensity_class": "moderate",
        }])
        return nut.exercise_energy_by_day(acts, 70.0).iloc[0]["exercise_kcal"]

    slow, fast = kcal(10_000), kcal(13_500)
    assert fast > slow * 1.25
    assert slow == pytest.approx(10.5 * 70.0, rel=0.03)   # matches the table at 10 km/h
    assert fast == pytest.approx(13.9 * 70.0, rel=0.03)


def test_run_without_distance_falls_back_to_the_table():
    acts = pd.DataFrame([{
        "date": dt.date(2026, 4, 5), "sport": "run", "tss": 80, "duration_s": 3600,
        "distance_m": None, "avg_w": None, "intensity_class": "moderate",
    }])
    kcal = nut.exercise_energy_by_day(acts, 70.0).iloc[0]["exercise_kcal"]
    assert kcal == pytest.approx(10.5 * 70.0, rel=0.01)


def test_implausible_run_speed_is_ignored():
    """A GPS glitch giving 60 km/h must not price a run as an elite effort."""
    acts = pd.DataFrame([{
        "date": dt.date(2026, 4, 5), "sport": "run", "tss": 80, "duration_s": 3600,
        "distance_m": 60_000, "avg_w": None, "intensity_class": "moderate",
    }])
    kcal = nut.exercise_energy_by_day(acts, 70.0).iloc[0]["exercise_kcal"]
    assert kcal == pytest.approx(10.5 * 70.0, rel=0.01)


def test_running_power_does_not_use_the_cycling_identity():
    """The ~24% efficiency identity is cycling-only; a running power field must
    still fall through to MET."""
    acts = pd.DataFrame([
        {"date": dt.date(2026, 4, 5), "sport": "run", "tss": 100, "duration_s": 3600,
         "avg_w": None, "np_w": 300.0, "intensity_class": "moderate"},
    ])
    kcal = nut.exercise_energy_by_day(acts, 70.0).iloc[0]["exercise_kcal"]
    assert abs(kcal - 10.5 * 70.0) < 1.0   # MET moderate run, not 285 kcal


# --- carb periodization ----------------------------------------------------
def test_carb_tier_selection():
    tiers = load_nutrition_rules()["carbohydrate"]["tiers"]
    assert nut._carb_tier(10, tiers)[0] == "rest"
    assert nut._carb_tier(60, tiers)[0] == "moderate"
    assert nut._carb_tier(120, tiers)[0] == "high"
    assert nut._carb_tier(400, tiers)[0] == "very_high"


def test_key_session_underfueling_detected():
    # A hard day (high TSS) with very low carbs should count as under-fuelled.
    d = dt.date(2026, 4, 10)
    nutrition = _daily([d], calories_kcal=2500.0, carbs_g=150.0, protein_g=120.0)  # 150/70 ≈ 2.1 g/kg
    acts = pd.DataFrame([
        {"date": d, "sport": "run", "tss": 120, "duration_s": 5400,
         "avg_w": None, "intensity_class": "hard"},
    ])
    digest = nut.build_digest(nutrition, acts, EMPTY_WELL, profile=_profile(70))
    assert digest.key_session_fuel_pct.value == 0.0  # the one key day missed its carb target


# --- micronutrient coverage ------------------------------------------------
def test_sparsely_logged_micronutrient_is_unknown_not_deficient():
    """Iron supplied by 1 item in 20 cannot support a deficiency claim, however
    far below target the sum lands."""
    dates = [dt.date(2026, 4, 1) + dt.timedelta(days=i) for i in range(20)]
    nutrition = _daily(dates, items=20, calories_kcal=3400.0, carbs_g=450.0,
                       protein_g=130.0, iron_mg=1.0, iron_mg_n=1)
    digest = nut.build_digest(nutrition, EMPTY_ACTS, EMPTY_WELL, profile=_profile(70))
    iron = next(f for f in digest.micronutrient_flags if f.nutrient == "iron_mg")
    assert iron.flag is MicronutrientStatus.UNKNOWN
    assert iron.reliable is False
    assert iron.coverage_pct == 5.0            # per ITEM, not per day
    assert any("unknown" in m for m in digest.missing)


def test_well_logged_micronutrient_below_target_still_flags():
    """Coverage gating must not mute a nutrient that IS logged properly."""
    dates = [dt.date(2026, 4, 1) + dt.timedelta(days=i) for i in range(20)]
    nutrition = _daily(dates, items=20, calories_kcal=3400.0, carbs_g=450.0,
                       protein_g=130.0, iron_mg=1.0)   # coverage defaults to 20/20
    digest = nut.build_digest(nutrition, EMPTY_ACTS, EMPTY_WELL, profile=_profile(70))
    iron = next(f for f in digest.micronutrient_flags if f.nutrient == "iron_mg")
    assert iron.reliable is True
    assert iron.coverage_pct == 100.0
    assert iron.flag is MicronutrientStatus.RED   # 1 mg vs 11 mg target


def test_partially_upgraded_store_estimates_from_days_that_have_coverage():
    """Half the window predates per-item tracking. Those days must be excluded
    from the coverage estimate, not counted as zero coverage."""
    dates = [dt.date(2026, 4, 1) + dt.timedelta(days=i) for i in range(20)]
    nutrition = _daily(dates, items=10, calories_kcal=3400.0, carbs_g=450.0,
                       protein_g=130.0, iron_mg=12.0)
    nutrition.loc[:9, "iron_mg_n"] = pd.NA        # first 10 days are legacy rows
    digest = nut.build_digest(nutrition, EMPTY_ACTS, EMPTY_WELL, profile=_profile(70))
    iron = next(f for f in digest.micronutrient_flags if f.nutrient == "iron_mg")
    assert iron.coverage_days == 10               # measured over the upgraded half
    assert iron.coverage_pct == 100.0             # not 50.0 — legacy days aren't zeros
    assert iron.reliable is True
    assert any("predate per-item nutrient tracking" in m for m in digest.missing)


def test_missing_coverage_columns_report_unknown_not_zero():
    """A frame with no `<field>_n` columns (legacy partition) must read as
    'coverage unknown', never as 'nothing was logged'."""
    dates = [dt.date(2026, 4, 1) + dt.timedelta(days=i) for i in range(20)]
    nutrition = _daily(dates, calories_kcal=3400.0, carbs_g=450.0, protein_g=130.0, iron_mg=1.0)
    nutrition = nutrition.drop(columns=fitatu.COVERAGE_FIELDS)
    digest = nut.build_digest(nutrition, EMPTY_ACTS, EMPTY_WELL, profile=_profile(70))
    iron = next(f for f in digest.micronutrient_flags if f.nutrient == "iron_mg")
    assert iron.flag is MicronutrientStatus.UNKNOWN
    assert iron.coverage_pct is None
    assert iron.coverage_basis == "unavailable"


# --- recovery correlations -------------------------------------------------
def test_recovery_correlation_detected():
    dates = [dt.date(2026, 4, 1) + dt.timedelta(days=i) for i in range(20)]
    carbs = [100.0 + 10 * i for i in range(20)]
    nutrition = _daily(dates, calories_kcal=[2500.0] * 20, carbs_g=carbs, protein_g=120.0)
    # next-morning HRV rises with prior-day carbs (deterministic, strong).
    well = pd.DataFrame({
        "date": [d + dt.timedelta(days=1) for d in dates],
        "hrv_rmssd_ms": [50.0 + 0.3 * c for c in carbs],
        "resting_hr_bpm": [50.0] * 20,
        "sleep_hours": [7.5] * 20,
        "weight_kg": [70.0] * 20,
    })
    digest = nut.build_digest(nutrition, EMPTY_ACTS, well, profile=_profile(70))
    finding = next((c for c in digest.recovery_correlations
                    if c.driver == "carbs_g_per_kg" and c.outcome == "next_day_hrv_rmssd_ms"), None)
    assert finding is not None
    assert finding.n >= 14
    assert finding.r > 0.9 and finding.notable


def test_load_confounded_association_is_not_notable():
    """Carbs track training load, and load depresses next-morning HRV on its own.
    The raw coefficient is strong; once TSS is regressed out nothing remains, so
    the finding must not be surfaced as notable."""
    dates = [dt.date(2026, 4, 1) + dt.timedelta(days=i) for i in range(30)]
    load = [40.0 + 4 * i for i in range(30)]
    # Carbs are a pure function of load; HRV is driven by load alone.
    nutrition = _daily(dates, calories_kcal=3000.0, protein_g=120.0,
                       carbs_g=[2.0 * t for t in load])
    acts = pd.DataFrame([
        {"date": d, "sport": "bike", "tss": t, "duration_s": 3600,
         "avg_w": None, "intensity_class": "moderate"}
        for d, t in zip(dates, load, strict=True)
    ])
    well = pd.DataFrame({
        "date": [d + dt.timedelta(days=1) for d in dates],
        "hrv_rmssd_ms": [110.0 - 0.25 * t for t in load],
        "resting_hr_bpm": [50.0] * 30,
        "sleep_hours": [7.5] * 30,
        "weight_kg": [70.0] * 30,
    })
    digest = nut.build_digest(nutrition, acts, well, profile=_profile(70))
    finding = next(c for c in digest.recovery_correlations
                   if c.driver == "carbs_g_per_kg" and c.outcome == "next_day_hrv_rmssd_ms")
    assert abs(finding.r) > 0.9              # raw association looks decisive
    assert finding.controlled_for == ["tss"]
    assert abs(finding.r_partial) < 0.25     # and entirely vanishes once adjusted
    assert finding.notable is False


def test_real_but_trivial_association_is_not_notable():
    """A tight correlation that moves HRV less than the athlete's own smallest
    worthwhile change is inside the daily scatter — strength alone must not
    promote it."""
    dates = [dt.date(2026, 4, 1) + dt.timedelta(days=i) for i in range(30)]
    carbs = [300.0 + 5 * (i % 6) for i in range(30)]
    # HRV tracks carbs perfectly but over a span of well under 1 ms.
    hrv = [90.0 + 0.002 * c for c in carbs]
    nutrition = _daily(dates, calories_kcal=3000.0, protein_g=120.0, carbs_g=carbs)
    well = pd.DataFrame({
        "date": [d + dt.timedelta(days=1) for d in dates],
        "hrv_rmssd_ms": hrv,
        "resting_hr_bpm": [50.0] * 30,
        "sleep_hours": [7.5] * 30,
        "weight_kg": [70.0] * 30,
    })
    digest = nut.build_digest(nutrition, EMPTY_ACTS, well, profile=_profile(70))
    finding = next(c for c in digest.recovery_correlations
                   if c.driver == "carbs_g_per_kg" and c.outcome == "next_day_hrv_rmssd_ms")
    assert abs(finding.r) > 0.9
    assert finding.effect_unit == "ms"
    assert abs(finding.effect_p10_p90) < finding.materiality_threshold
    assert finding.notable is False


def test_wellness_missing_column_does_not_crash():
    """A non-empty wellness frame missing a column (e.g. sleep_hours) must not
    raise (S1) — correlations needing it are simply skipped."""
    dates = [dt.date(2026, 4, 1) + dt.timedelta(days=i) for i in range(20)]
    nutrition = _daily(dates, calories_kcal=3000.0, carbs_g=400.0, protein_g=120.0)
    well = pd.DataFrame({
        "date": [d + dt.timedelta(days=1) for d in dates],
        "hrv_rmssd_ms": [60.0] * 20,
        # no resting_hr_bpm, no sleep_hours, no weight_kg
    })
    digest = nut.build_digest(nutrition, EMPTY_ACTS, well, profile=_profile(70))
    assert digest.days_logged == 20
    # sleep-based correlations are absent, but the build still succeeds
    assert all("sleep_hours" not in c.outcome for c in digest.recovery_correlations)


def test_empty_nutrition_is_safe():
    digest = nut.build_digest(pd.DataFrame(), EMPTY_ACTS, EMPTY_WELL, profile=_profile())
    assert digest.days_logged == 0
    assert digest.missing
