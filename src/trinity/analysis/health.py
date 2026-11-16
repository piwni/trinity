"""Health analysis — the deterministic half of the doctor agent.

Reads the athlete's hand-entered lab panels, compares each latest result with
the range on the report and with the screening thresholds in
``knowledge/health/rules.yaml``, and puts it next to the wellness data and the
flags the coach's and dietician's pipelines already produced.

It grades nothing beyond "inside / outside" and "above / below a cited
threshold". What a finding means, and what to do about it, is the agent's
judgment and a physician's decision.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import yaml
from pydantic import ValidationError

from trinity.analysis import metrics
from trinity.analysis.loaders import data_as_of
from trinity.knowledge import load_health_rules
from trinity.models.common import Provenance, Trend
from trinity.models.context_digest import DigestValue
from trinity.models.health_digest import (
    HealthDigest,
    HealthFlag,
    HealthRecords,
    LabMarker,
    LabPanel,
    LabResult,
    LabStatus,
    PanelSummary,
)
from trinity.models.nutrition_digest import NutritionDigest
from trinity.models.profile import AthleteProfile, Sex
from trinity.models.readiness import ReadinessFlag, ReadinessReport

MAX_PANELS = 6
MAX_EVENTS = 15
WEARABLE = "wearable wellness (intervals.icu)"


class HealthRecordsError(ValueError):
    """The records file is not in the expected shape. The message lists every problem."""


def load_records(path: Path) -> HealthRecords:
    """Parse ``health/records.yaml``. A missing file is an empty record, not an error."""
    if not path.exists():
        return HealthRecords()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise HealthRecordsError(f"{path} is not valid YAML: {exc}") from exc
    try:
        return HealthRecords.model_validate(raw)
    except ValidationError as exc:
        lines = [f"  {'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise HealthRecordsError(f"{path} has {len(lines)} problem(s):\n" + "\n".join(lines)) from exc


# ---------------------------------------------------------------- markers ---
def _norm_name(text: str) -> str:
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def _norm_unit(text: str) -> str:
    u = "".join(text.split()).casefold().replace("μ", "u").replace("µ", "u")
    return u.replace("mcg", "ug")


def _catalog_index(markers: dict) -> dict[str, str]:
    """Every spelling a marker may be entered under -> its catalog key."""
    index: dict[str, str] = {}
    for key, spec in markers.items():
        for spelling in (key, spec.get("name", ""), *(spec.get("aliases") or [])):
            if spelling:
                index.setdefault(_norm_name(str(spelling)), key)
    return index


def _to_threshold_unit(spec: dict, value: float, unit: str) -> float | None:
    """The value in the unit the marker's thresholds are written in, or None if
    the entered unit is not one the rulebook can convert."""
    factors = {_norm_unit(str(u)): float(f) for u, f in (spec.get("units") or {}).items()}
    factor = factors.get(_norm_unit(unit))
    return None if factor is None else value * factor


def _pick(threshold, sex: Sex | None) -> float | None:
    """A threshold is a number, or a {male, female} pair."""
    if isinstance(threshold, dict):
        return threshold.get(sex.value) if sex in (Sex.MALE, Sex.FEMALE) else None
    return threshold


def _threshold_flag(spec: dict, result: LabResult, sex: Sex | None,
                    notes: list[str]) -> tuple[HealthFlag | None, str | None]:
    low = spec.get("low") or {}
    if not low:
        return None, None
    sex_specific = any(isinstance(v, dict) for v in low.values())
    red, amber = _pick(low.get("red_below"), sex), _pick(low.get("amber_below"), sex)
    if red is None and amber is None:
        if sex_specific:
            notes.append("screening threshold is sex-specific and the athlete's sex is not "
                         "on file, so it was not applied")
        return None, None
    value = _to_threshold_unit(spec, result.value, result.unit)
    if value is None:
        notes.append(f"unit '{result.unit}' is not one the rulebook converts for this marker "
                     f"({', '.join(spec.get('units') or {})}), so the screening threshold "
                     "was not applied")
        return None, None
    unit = spec.get("unit", "")
    bounds = ", ".join(f"{label} <{bound:g}" for label, bound in (("amber", amber), ("red", red))
                       if bound is not None)
    threshold = f"screening threshold ({bounds} {unit})"
    if red is not None and value < red:
        return HealthFlag.RED, f"below the red {threshold}"
    if amber is not None and value < amber:
        return HealthFlag.AMBER, f"below the amber {threshold}"
    return HealthFlag.GREEN, f"meets the {threshold}"


def _lab_status(result: LabResult) -> LabStatus:
    if result.ref_low is None and result.ref_high is None:
        return LabStatus.UNKNOWN
    if result.ref_low is not None and result.value < result.ref_low:
        return LabStatus.LOW
    if result.ref_high is not None and result.value > result.ref_high:
        return LabStatus.HIGH
    return LabStatus.IN_RANGE


def _assess(key: str, spec: dict | None, history: list[tuple[LabPanel, LabResult]],
            as_of: dt.date, stale_after: int, sex: Sex | None) -> LabMarker:
    """`history` is every result for one marker, oldest first."""
    panel, result = history[-1]
    notes: list[str] = []
    status = _lab_status(result)
    lab_flag = {LabStatus.LOW: HealthFlag.AMBER, LabStatus.HIGH: HealthFlag.AMBER,
                LabStatus.IN_RANGE: HealthFlag.GREEN}.get(status)
    thr_flag, thr_basis = _threshold_flag(spec, result, sex, notes) if spec else (None, None)

    flags = [f for f in (lab_flag, thr_flag) if f is not None]
    flag = max(flags, key=lambda f: f.severity) if flags else HealthFlag.UNKNOWN
    if not flags:
        basis = None
        notes.append("no reference range on the report and no screening threshold: not assessable")
    elif thr_flag is not None and (lab_flag is None or thr_flag.severity > lab_flag.severity):
        basis = thr_basis
        if status is LabStatus.IN_RANGE and thr_flag is not HealthFlag.GREEN:
            notes.append("inside the lab's reference range, but below the screening threshold")
    else:
        where = {LabStatus.IN_RANGE: "inside", LabStatus.LOW: "below", LabStatus.HIGH: "above"}
        basis = f"{where[status]} the lab's reference range" + (f"; {thr_basis}" if thr_flag else "")

    marker = LabMarker(
        marker=key, name=(spec or {}).get("name", result.marker),
        category=(spec or {}).get("category"), in_catalog=spec is not None,
        value=result.value, unit=result.unit, date=panel.date,
        age_days=(as_of - panel.date).days, stale=(as_of - panel.date).days > stale_after,
        lab_ref_low=result.ref_low, lab_ref_high=result.ref_high, lab_status=status,
        flag=flag, flag_basis=basis,
        threshold_source=" ".join(str(spec["source"]).split()) if thr_basis and spec else None,
        n_results=len(history), notes=notes,
    )
    if result.note:
        marker.notes.append(f"athlete's note: {result.note}")
    if len(history) > 1:
        prev_panel, prev = history[-2]
        marker.previous_value, marker.previous_date = prev.value, prev_panel.date
        if _norm_unit(prev.unit) == _norm_unit(result.unit):
            marker.change = round(result.value - prev.value, 4)
        else:
            marker.notes.append(f"previous result was in '{prev.unit}', so no change is given")
    return marker


def assess_labs(records: HealthRecords, as_of: dt.date, sex: Sex | None = None) -> list[LabMarker]:
    """The latest result per marker, flagged. Catalog markers first, in rulebook order."""
    rules = load_health_rules()
    catalog: dict = rules.get("markers") or {}
    index = _catalog_index(catalog)
    stale_after = int((rules.get("labs") or {}).get("stale_after_days", 365))

    by_marker: dict[str, list[tuple[LabPanel, LabResult]]] = {}
    for panel in sorted(records.labs, key=lambda p: p.date):
        for result in panel.results:
            key = index.get(_norm_name(result.marker), result.marker.strip())
            by_marker.setdefault(key, []).append((panel, result))

    order = {key: i for i, key in enumerate(catalog)}
    return [
        _assess(key, catalog.get(key), history, as_of, stale_after, sex)
        for key, history in sorted(by_marker.items(),
                                   key=lambda kv: (order.get(kv[0], len(order)), kv[0]))
    ]


# ---------------------------------------------------------------- digest ----
def _panel_summaries(records: HealthRecords, activities: pd.DataFrame,
                     window_days: int) -> list[PanelSummary]:
    out = []
    for panel in sorted(records.labs, key=lambda p: p.date, reverse=True)[:MAX_PANELS]:
        summary = PanelSummary(date=panel.date, lab=panel.lab, fasting=panel.fasting,
                               context=panel.context, results=len(panel.results),
                               prior_window_days=window_days)
        if not activities.empty:
            # The draw is usually in the morning, so the day itself is not "before" it.
            start = panel.date - dt.timedelta(days=window_days)
            prior = activities[(activities["date"] >= start) & (activities["date"] < panel.date)]
            # Only when the fetched history spans the whole window: an empty
            # window outside it would read as "rested" when it is just unknown.
            covered = (min(activities["date"]) <= start
                       and max(activities["date"]) >= panel.date - dt.timedelta(days=1))
            if covered:
                summary.prior_tss = round(float(prior["tss"].sum()), 1)
                summary.prior_hours = round(float(prior["duration_s"].sum()) / 3600, 2)
        out.append(summary)
    return out


def _weight_change_90d(wellness: pd.DataFrame, as_of: dt.date) -> tuple[float | None, int]:
    """Mean of the last 14 days minus the 14 days ending 90 days ago."""
    if wellness.empty or "weight_kg" not in wellness:
        return None, 0
    w = wellness[["date", "weight_kg"]].dropna()

    def window(end: dt.date) -> pd.Series:
        return w[(w["date"] > end - dt.timedelta(days=14)) & (w["date"] <= end)]["weight_kg"]

    now, then = window(as_of), window(as_of - dt.timedelta(days=90))
    if now.empty or then.empty:
        return None, 0
    return round(float(now.mean() - then.mean()), 2), int(len(now) + len(then))


def _wearable(value, unit: str, as_of: dt.date, *, days: int | None = None,
              trend: str | None = None, samples: int = 0) -> DigestValue:
    if value is None or pd.isna(value):
        return DigestValue(unit=unit)
    return DigestValue(
        value=float(value), unit=unit, provenance=Provenance.ESTIMATED, source=WEARABLE,
        date_from=as_of - dt.timedelta(days=days) if days else None, date_to=as_of,
        sample_count=samples, trend=Trend(trend) if trend else Trend.UNKNOWN)


def build_digest(
    records: HealthRecords,
    activities: pd.DataFrame,
    wellness: pd.DataFrame,
    *,
    profile: AthleteProfile | None = None,
    readiness: ReadinessReport | None = None,
    nutrition: NutritionDigest | None = None,
    today: dt.date | None = None,
    athlete_id: str = "athlete",
) -> HealthDigest:
    """``today`` dates the lab results (how old, stale or not). The wellness
    summary is dated by the data itself, like the other digests."""
    today = today or dt.date.today()
    rules = load_health_rules()
    digest = HealthDigest(athlete_id=athlete_id, as_of=today)
    missing: list[str] = []
    assumptions: list[str] = []

    sex = profile.anthropometrics.sex if profile else None
    sex = sex if sex in (Sex.MALE, Sex.FEMALE) else None

    # --- labs ---
    digest.labs = assess_labs(records, today, sex)
    window = int((rules.get("labs") or {}).get("prior_training_window_days", 3))
    digest.panels = _panel_summaries(records, activities, window)
    if not digest.labs:
        missing.append("no lab results on file (add panels to health/records.yaml)")
    current = [m for m in digest.labs if not m.stale]
    assessable = [m for m in current if m.flag is not HealthFlag.UNKNOWN]
    if assessable:
        digest.overall_flag = max((m.flag for m in assessable), key=lambda f: f.severity)
    for m in current:
        if m.flag in (HealthFlag.AMBER, HealthFlag.RED):
            digest.attention.append(
                f"{m.flag.value.upper()} {m.name}: {m.value:g} {m.unit} on {m.date} "
                f"({m.flag_basis})")
    stale = [m.name for m in digest.labs if m.stale]
    if stale:
        missing.append(f"{len(stale)} marker(s) only have results older than "
                       f"{(rules.get('labs') or {}).get('stale_after_days', 365)} days: "
                       + ", ".join(stale))
    unassessable = [m.name for m in current if m.flag is HealthFlag.UNKNOWN]
    if unassessable:
        missing.append("not assessable (no range on the report, no threshold): "
                       + ", ".join(unassessable))
    if sex is None and digest.labs:
        missing.append("athlete's sex not on file: sex-specific thresholds not applied")
    if any(p.prior_tss is None for p in digest.panels):
        missing.append("training load before some panels is unknown (outside the fetched history)")

    # --- wellness ---
    if wellness.empty:
        missing.append("no wellness data (HRV, resting heart rate, sleep, weight)")
    else:
        as_of = data_as_of(activities, wellness)
        b = metrics.hrv_rhr_baselines(wellness, as_of)
        digest.hrv_rmssd_ms = _wearable(b.get("hrv_latest_ms"), "ms", as_of, trend=b.get("hrv_trend"))
        digest.hrv_baseline_60d_ms = _wearable(b.get("hrv_baseline_60d_ms"), "ms", as_of, days=60)
        digest.resting_hr_bpm = _wearable(b.get("resting_hr_latest_bpm"), "bpm", as_of,
                                          trend=b.get("resting_hr_trend"))
        digest.resting_hr_baseline_bpm = _wearable(b.get("resting_hr_baseline_bpm"), "bpm",
                                                   as_of, days=60)
        digest.sleep_avg_hours = _wearable(b.get("sleep_avg_hours"), "h", as_of, days=7)
        digest.weight_kg = _wearable(b.get("weight_kg"), "kg", as_of)
        change, samples = _weight_change_90d(wellness, as_of)
        digest.weight_change_90d_kg = _wearable(change, "kg", as_of, days=104, samples=samples)
        assumptions.append(f"wellness values are as of {as_of}, from a consumer wearable: "
                           "estimates, not clinical measurements")
        if digest.hrv_baseline_60d_ms.value is None:
            missing.append("HRV baseline unknown")

    # --- what the other pipelines already found ---
    if readiness is not None:
        digest.readiness_flag = readiness.overall_flag
        digest.readiness_notes = [f"{r.rule}: {r.message}" for r in readiness.results
                                  if r.flag is not ReadinessFlag.GREEN]
    else:
        missing.append("no readiness report (run `trinity readiness`)")
    if nutrition is not None and nutrition.days_logged:
        digest.energy_availability_flag = nutrition.energy_availability_flag
        digest.energy_availability_kcal_per_kg = nutrition.energy_availability_kcal_per_kg
    else:
        missing.append("no nutrition digest: energy availability unknown (run `trinity nutrition`)")
    if profile is not None and profile.load.combined.ctl.known:
        load = profile.load.combined
        digest.training_load_ctl = DigestValue(
            value=float(load.ctl.value), unit="TSS/day", provenance=load.ctl.provenance,
            source=load.ctl.source, date_to=load.as_of, trend=load.trend)
        if load.tsb.known:
            digest.training_form_tsb = DigestValue(
                value=float(load.tsb.value), unit="TSB", provenance=load.tsb.provenance,
                source=load.tsb.source, date_to=load.as_of)

    digest.events = sorted(records.events, key=lambda e: e.date, reverse=True)[:MAX_EVENTS]
    digest.assumptions = assumptions
    digest.missing = missing
    return digest
