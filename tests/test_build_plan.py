"""Season periodization engine — goal-race selection and spec compile."""

from __future__ import annotations

import datetime as dt

from trinity.models.plan import IntensityClass
from trinity.planning.season import build_microcycle, build_plan


def _spec(*races):
    return {
        "meta": {
            "athlete_id": "athlete",
            "plan_id": "t",
            "start_date": "2026-09-07",
            "starting_ctl": 70.0,
        },
        "races": list(races),
        "weeks": [
            {
                "index": 1,
                "start": "2026-09-07",
                "phase": "base",
                "type": "explicit",
                "sessions": [
                    {
                        "day": 0,
                        "sport": "run",
                        "name": "easy",
                        "intensity": "easy",
                        "minutes": 30,
                    }
                ],
            }
        ],
    }


def test_build_plan_uses_latest_a_race_as_goal():
    spec = _spec(
        {"name": "Malbork", "date": "2027-09-05", "bucket": "full", "priority": "A"},
        {"name": "Hamburg", "date": "2028-06-04", "bucket": "full", "priority": "A"},
    )
    plan = build_plan(spec)
    assert plan.goal_race.name == "Hamburg"
    assert plan.goal_race.date.isoformat() == "2028-06-04"


def test_flat_strides_land_on_thursday_easy_run():
    """The strides note describes Thursday. It must not be pasted onto the key run."""
    sessions = build_microcycle(
        dt.date(2026, 9, 28),
        target_h=9.4,
        key=IntensityClass.MODERATE,
        long_ride_h=2.5,
        long_run_h=1.4,
        brick_run_h=0.5,
        anchors={},
        run_key="Threshold intro 5x5' @ 3:33-3:37/km off 90s jog.",
        run_easy="Z2 + 6x20s FLAT strides. Uphill work returns after the 10 km.",
        bike_tempo_easy=True,
    )
    wed = next(s for s in sessions if s.date.isoformat() == "2026-09-30" and s.sport.value == "run")
    thu = next(s for s in sessions if s.date.isoformat() == "2026-10-01" and s.sport.value == "run")
    assert "strides" not in (wed.description or "").lower()
    assert thu.description == "Z2 + 6x20s FLAT strides. Uphill work returns after the 10 km."
    assert thu.name == "Run easy + strides"
