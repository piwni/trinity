"""Workout-builder tests: text generation, round-trip parse, event payloads."""

from __future__ import annotations

import datetime as dt

from _helpers import good_plan
from trinity.models.common import Sport
from trinity.models.plan import (
    EventCategory,
    PlannedSession,
    StepKind,
    Target,
    TargetType,
    WorkoutStep,
)
from trinity.planning.workout_builder import (
    WorkoutBuildError,
    build_workout_text,
    parse_workout_text,
    plan_to_events,
    session_to_event,
)


def _paced(sport: Sport, target: Target, kind: StepKind = StepKind.INTERVAL) -> str:
    step = WorkoutStep(kind=kind, duration_s=180, target=target)
    session = PlannedSession(date=dt.date(2026, 11, 25), sport=sport, name="Paced",
                             structure=[step])
    return build_workout_text(session)


def test_absolute_run_pace_is_slower_first_with_pace_keyword():
    text = _paced(Sport.RUN, Target(type=TargetType.PACE, low_abs=205, high_abs=215))
    assert text == "- 3m 3:35-3:25/km Pace"


def test_narrow_run_pace_window_is_widened_to_ten_seconds():
    text = _paced(Sport.RUN, Target(type=TargetType.PACE, low_abs=215, high_abs=213))
    assert text == "- 3m 3:39-3:29/km Pace"


def test_single_run_pace_becomes_a_ten_second_window():
    text = _paced(Sport.RUN, Target(type=TargetType.PACE, low_abs=214))
    assert text == "- 3m 3:39-3:29/km Pace"


def test_swim_pace_uses_per_100m_and_a_four_second_minimum():
    assert _paced(Sport.SWIM, Target(type=TargetType.PACE, low_abs=110, high_abs=106)) == (
        "- 3m 1:50-1:46/100m Pace")
    assert _paced(Sport.SWIM, Target(type=TargetType.PACE, low_abs=106, high_abs=105)) == (
        "- 3m 1:48-1:44/100m Pace")


def test_narrow_percent_pace_window_is_widened_to_five_points():
    text = _paced(Sport.RUN, Target(type=TargetType.PACE, low_pct=0.98, high_pct=0.99))
    assert text == "- 3m 96-101% pace"


def test_build_and_parse_round_trip():
    plan = good_plan()
    bike_vo2 = plan.weeks[0].sessions[2]
    text = build_workout_text(bike_vo2)
    assert "5x" in text and "%" in text
    steps = parse_workout_text(text)
    repeat = [s for s in steps if s["type"] == "repeat"][0]
    assert repeat["reps"] == 5 and repeat["substeps"] == 2


def test_parse_rejects_malformed():
    try:
        parse_workout_text("this is not a workout line")
    except WorkoutBuildError:
        return
    raise AssertionError("expected WorkoutBuildError")


def test_event_has_stable_external_id_and_type():
    s = PlannedSession(date=dt.date(2026, 6, 22), sport=Sport.RUN, name="Easy run",
                       planned_tss=40, planned_duration_s=3000)
    ev = session_to_event(s, athlete_id="athlete")
    assert ev["external_id"] == "aithlete-athlete-2026-06-22-run"
    assert ev["type"] == "Run"
    assert ev["category"] == "WORKOUT"


def test_race_and_note_events():
    race = PlannedSession(date=dt.date(2026, 7, 1), sport=Sport.RUN, name="A race",
                          category=EventCategory.RACE)
    note = PlannedSession(date=dt.date(2026, 7, 1), sport=Sport.OTHER, name="Build starts",
                          category=EventCategory.NOTE, description="Begin build")
    assert session_to_event(race)["category"] == "RACE"
    note_ev = session_to_event(note)
    assert note_ev["category"] == "NOTE" and "type" not in note_ev


def test_plan_to_events_count_matches_sessions():
    plan = good_plan()
    assert len(plan_to_events(plan)) == len(plan.all_sessions())


def _distance(sport: Sport, metres: float) -> str:
    step = WorkoutStep(kind=StepKind.STEADY, distance_m=metres, target=Target(type=TargetType.OPEN))
    session = PlannedSession(date=dt.date(2026, 11, 25), sport=sport, name="Distance",
                             structure=[step])
    return build_workout_text(session)


def test_short_distances_use_mtr_because_intervals_reads_m_as_minutes():
    assert _distance(Sport.SWIM, 400) == "- 400mtr"
    assert _distance(Sport.RUN, 800) == "- 800mtr"


def test_long_distances_stay_in_km():
    assert _distance(Sport.SWIM, 1500) == "- 1.5km"
    assert _distance(Sport.RUN, 10000) == "- 10km"
