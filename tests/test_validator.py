"""Plan-validator guardrail tests."""

from __future__ import annotations

import datetime as dt

from _helpers import build_week, good_plan, session, steep_plan
from trinity.models.common import Sport
from trinity.models.plan import (
    DistanceBucket,
    EventCategory,
    GoalRace,
    IntensityClass,
    Phase,
    PlannedSession,
    TrainingPlan,
)
from trinity.planning.validator import validate_plan


def test_good_plan_passes():
    rep = validate_plan(good_plan(), starting_ctl=80.0, weekly_hours_tolerance=13)
    assert rep.ok, [i.message for i in rep.errors]
    # An A race should land in a sensible peaked-form window.
    assert rep.projected_race_tsb is not None
    assert 5 <= rep.projected_race_tsb <= 30


def test_steep_ramp_is_blocked():
    rep = validate_plan(steep_plan(), starting_ctl=40.0)
    assert not rep.ok
    codes = {i.code for i in rep.errors}
    assert "ramp_too_steep" in codes or "weekly_tss_jump" in codes


def test_no_recovery_week_flagged():
    plan = steep_plan()
    rep = validate_plan(plan, starting_ctl=40.0)
    # Four straight load weeks with no recovery is allowed up to the cap; the
    # steep loads also trip ramp errors. Ensure errors exist and block push.
    assert len(rep.errors) >= 1


def test_projected_ctl_series_length_matches_weeks():
    plan = good_plan()
    rep = validate_plan(plan, starting_ctl=80.0)
    assert len(rep.projected_ctl_by_week) == len(plan.weeks)


def test_fake_recovery_flag_cannot_bypass_ramp():
    """Regression (C2): marking every week is_recovery=True must NOT let a load
    ramp slip past the validator."""
    start = dt.date(2026, 6, 22)
    tss = [700, 1400, 2100, 2800]
    weeks = [build_week(i + 1, start + dt.timedelta(weeks=i), Phase.BUILD, tss[i], recovery=True)
             for i in range(4)]
    plan = TrainingPlan(plan_id="fake-recovery", start_date=start, starting_ctl=40.0, weeks=weeks)
    rep = validate_plan(plan, starting_ctl=40.0)
    assert not rep.ok, "rising load disguised as recovery weeks must be blocked"
    codes = {i.code for i in rep.errors}
    assert codes & {"ramp_too_steep", "weekly_tss_jump", "fake_recovery_week", "projected_acwr_spike"}


def test_big_weekend_is_allowed():
    """Long ride one day + long run the next (the standard long-course big
    weekend) must NOT trip the same-day stacking guardrail."""
    rep = validate_plan(good_plan(), starting_ctl=80.0)
    assert "stacked_long_day" not in {i.code for i in rep.issues}


def test_long_run_stacked_on_brick_day_is_flagged():
    """Regression: a standalone long run on the same day as a brick (the exact
    Sunday bug) must surface as a stacked_long_day warning."""
    plan = good_plan()
    brick_day = plan.weeks[0].sessions[5].date  # the long-ride+brick day
    plan.weeks[0].sessions.append(
        session(brick_day, Sport.RUN, "Long run (stacked)", 70, 95, IntensityClass.EASY)
    )
    rep = validate_plan(plan, starting_ctl=80.0)
    hits = [i for i in rep.issues if i.code == "stacked_long_day"]
    assert hits, "a long run stacked on a brick day must be flagged"
    assert hits[0].week_index == 1


def test_taper_peak_without_goal_race_is_blocked():
    """Regression (I2): a plan that tapers/peaks but omits goal_race can't have
    its race-day form checked -> must be an error, not a silent pass."""
    start = dt.date(2026, 6, 22)
    weeks = [
        build_week(1, start, Phase.BUILD, 480),
        build_week(2, start + dt.timedelta(weeks=1), Phase.BUILD, 500),
        build_week(3, start + dt.timedelta(weeks=2), Phase.TAPER, 300, recovery=True),
    ]
    plan = TrainingPlan(plan_id="no-race", start_date=start, starting_ctl=80.0, weeks=weeks)
    rep = validate_plan(plan, starting_ctl=80.0)
    assert not rep.ok
    assert "missing_goal_race" in {i.code for i in rep.errors}


def _two_a_race_plan():
    """20 weeks: an EARLY A race reached with no taper at all (deep negative
    form), and a LATE A race reached properly tapered. ``goal_race`` is the late
    one, as build_plan sets it."""
    start = dt.date(2026, 6, 22)
    weeks = []
    for i in range(20):
        phase, tss, rec = Phase.BUILD, 520, False
        if i % 4 == 3:
            tss, rec = 300, True
        if i in (18, 19):  # genuine taper into the late race
            phase, tss, rec = Phase.TAPER, 260, False
        weeks.append(build_week(i + 1, start + dt.timedelta(weeks=i), phase, tss, recovery=rec))
    # Early race lands on the Sunday of a full load week -> deep negative form.
    early = GoalRace(name="Early A", date=start + dt.timedelta(weeks=6, days=6),
                     bucket=DistanceBucket.FULL, priority="A")
    # Late race lands on the Sunday of the second taper week -> properly peaked.
    late = GoalRace(name="Late A", date=start + dt.timedelta(weeks=19, days=6),
                    bucket=DistanceBucket.FULL, priority="A")
    return TrainingPlan(plan_id="two-a", start_date=start, starting_ctl=80.0, weeks=weeks,
                        goal_race=late, races=[early, late])


def test_every_a_race_gets_race_day_form_validated():
    """Regression: _check_taper only looked at plan.goal_race, so an earlier A
    race — a full Ironman with a time goal — had its taper and race-day form
    checked by nothing at all. Every A race must be validated."""
    rep = validate_plan(_two_a_race_plan(), starting_ctl=80.0, training_age="advanced")
    tsb_issues = [i for i in rep.issues if i.code == "race_tsb_out_of_range"]
    assert any("Early A" in i.message for i in tsb_issues), (
        f"the early A race must have its race-day TSB validated; got {[i.message for i in rep.issues]}"
    )
    assert not rep.ok, "an un-tapered A race is an error, not a warning"


def test_taper_duration_measures_the_run_before_each_race():
    """Regression: taper duration summed EVERY taper week in the plan, so a
    21-month plan with a taper before each of 6 races reported a '63 day taper'
    and warned spuriously. Only the consecutive run before the race counts."""
    rep = validate_plan(_two_a_race_plan(), starting_ctl=80.0, training_age="advanced")
    msgs = [i.message for i in rep.issues if i.code == "taper_duration_out_of_range"]
    assert not any("63d" in m or "9wk" in m for m in msgs), (
        f"taper duration must not sum unrelated taper weeks; got {msgs}"
    )


def test_timid_build_block_is_flagged():
    """rules.yaml declares ramp.ctl_per_week_min ('below this, build phase is too
    timid to drive adaptation') but nothing read it. A build block that holds load
    flat for weeks is the failure mode that produces a 21-month plan arriving at
    its A race with last year's fitness — it must surface."""
    start = dt.date(2026, 6, 22)
    # Six build weeks at a load that exactly sustains the starting CTL: no ramp.
    weeks = [build_week(i + 1, start + dt.timedelta(weeks=i), Phase.BUILD, 560)
             for i in range(6)]
    plan = TrainingPlan(plan_id="timid", start_date=start, starting_ctl=80.0, weeks=weeks,
                        goal_race=GoalRace(name="A", date=start + dt.timedelta(weeks=6, days=6),
                                           bucket=DistanceBucket.FULL, priority="A"))
    rep = validate_plan(plan, starting_ctl=80.0, training_age="advanced")
    assert "build_block_too_timid" in {i.code for i in rep.issues}, (
        f"a flat build block must be flagged; got {[i.code for i in rep.issues]}"
    )


def test_genuine_build_block_is_not_flagged_as_timid():
    """The timid check must not fire on a block that is actually progressing."""
    rep = validate_plan(good_plan(), starting_ctl=80.0, weekly_hours_tolerance=13)
    assert "build_block_too_timid" not in {i.code for i in rep.issues}


def test_hard_runs_closer_than_48h_are_flagged():
    """rules.yaml declares hard_session_spacing_hours (run 48 h) but nothing read
    it. Two hard runs on consecutive days is the soleus-injury pattern."""
    plan = good_plan()
    wk = plan.weeks[0]
    hard_run = next(s for s in wk.sessions if s.sport is Sport.RUN
                    and s.intensity_class is IntensityClass.HARD)
    wk.sessions.append(session(hard_run.date + dt.timedelta(days=1), Sport.RUN,
                               "Second hard run", 60, 50, IntensityClass.HARD))
    rep = validate_plan(plan, starting_ctl=80.0)
    assert "hard_sessions_too_close" in {i.code for i in rep.issues}


def test_a_race_below_season_peak_fitness_is_flagged():
    """An A race must be the fittest point of the season. A plan that peaks months
    earlier and arrives at its A race with less chronic load has mis-sequenced the
    whole year — and nothing caught it, because the taper check only looks at form
    (CTL-ATL), which a deep enough taper always flatters."""
    start = dt.date(2026, 6, 22)
    weeks = []
    for i in range(16):
        # Big spring block, then a long slump, then a modest run-in to the A race.
        tss = 900 if i < 5 else (380 if i < 11 else 600)
        phase = Phase.BUILD if i < 14 else Phase.TAPER
        weeks.append(build_week(i + 1, start + dt.timedelta(weeks=i), phase, tss,
                                recovery=(i in (5, 9))))
    plan = TrainingPlan(
        plan_id="peaked-early", start_date=start, starting_ctl=70.0, weeks=weeks,
        goal_race=GoalRace(name="Late A", date=start + dt.timedelta(weeks=15, days=6),
                           bucket=DistanceBucket.FULL, priority="A"))
    rep = validate_plan(plan, starting_ctl=70.0, training_age="advanced")
    assert "a_race_below_season_peak" in {i.code for i in rep.issues}, (
        f"got {sorted({i.code for i in rep.issues})}"
    )


def test_well_sequenced_a_race_is_not_flagged():
    rep = validate_plan(good_plan(), starting_ctl=80.0, weekly_hours_tolerance=13)
    assert "a_race_below_season_peak" not in {i.code for i in rep.issues}


def test_zero_rest_days_is_flagged():
    """rules.yaml declares min_rest_days_per_week but nothing read it, so a plan
    scheduling sessions on all 7 days every week for 21 months passed clean."""
    start = dt.date(2026, 6, 22)
    weeks = [build_week(i + 1, start + dt.timedelta(weeks=i), Phase.BUILD, 480,
                        recovery=(i == 3)) for i in range(4)]
    for wk in weeks:  # fill the one empty day so every day carries a session
        wk.sessions.append(session(wk.start_date + dt.timedelta(days=4), Sport.SWIM,
                                   "Extra swim", 30, 40, IntensityClass.EASY))
    plan = TrainingPlan(plan_id="no-rest", start_date=start, starting_ctl=80.0, weeks=weeks,
                        goal_race=GoalRace(name="A", date=start + dt.timedelta(weeks=4),
                                           bucket=DistanceBucket.HALF, priority="A"))
    rep = validate_plan(plan, starting_ctl=80.0, training_age="advanced")
    assert "no_rest_day" in {i.code for i in rep.issues}


# --- content-based checks (independent review 2026-09-24) ---------------------

def _codes(issues):
    return {i.code for i in issues}


def test_easy_label_with_race_power_content_is_an_error():
    """A 'Long ride (Z2)' carrying 3x40' at race power must not pass as easy."""
    plan = good_plan()
    plan.anchors = {"ftp_w": 250.0}
    ride = plan.weeks[0].sessions[5]
    ride.description = "3x40' @ 200 W (rest Z2)"
    rep = validate_plan(plan, starting_ctl=80.0)
    assert "label_contradicts_content" in _codes(rep.errors)


def test_hand_typed_tss_that_disagrees_with_content_is_flagged():
    plan = good_plan()
    plan.anchors = {"ftp_w": 250.0}
    ride = plan.weeks[0].sessions[5]
    ride.description = "3x40' @ 200 W (rest Z2)"
    ride.intensity_class = IntensityClass.MODERATE
    ride.planned_tss = 120.0                      # content says ~220 for 180 min
    rep = validate_plan(plan, starting_ctl=80.0)
    assert "tss_not_from_content" in _codes(rep.errors)


def _race_plan(taper_tss, n_build=6):
    start = dt.date(2026, 6, 22)
    weeks = [build_week(i + 1, start + dt.timedelta(weeks=i), Phase.BUILD, 600,
                        recovery=False) for i in range(n_build)]
    for j in range(2):
        weeks.append(build_week(n_build + j + 1, start + dt.timedelta(weeks=n_build + j),
                                Phase.TAPER, taper_tss))
    race_day = start + dt.timedelta(weeks=n_build + 2)
    return TrainingPlan(plan_id="t", start_date=start, starting_ctl=80.0, weeks=weeks,
                        goal_race=GoalRace(name="A", date=race_day,
                                           bucket=DistanceBucket.FULL, priority="A"))


def test_shallow_taper_before_an_a_race_is_an_error():
    """Final 14 days must be >=40% below the peak 2 weeks (Mujika)."""
    rep = validate_plan(_race_plan(taper_tss=520), starting_ctl=80.0, training_age="advanced")
    assert "taper_too_shallow" in _codes(rep.errors)


def test_proper_taper_passes_the_depth_check():
    rep = validate_plan(_race_plan(taper_tss=300), starting_ctl=80.0, training_age="advanced")
    assert "taper_too_shallow" not in _codes(rep.issues)


def test_heavy_training_right_after_a_race_is_an_error():
    plan = good_plan()
    wk = plan.weeks[1]
    race_day = wk.start_date + dt.timedelta(days=2)
    wk.sessions.append(PlannedSession(date=race_day, sport=Sport.OTHER, name="RACE: B",
                                      category=EventCategory.RACE, planned_tss=250,
                                      planned_duration_s=4 * 3600))
    rep = validate_plan(plan, starting_ctl=80.0)
    assert "post_race_too_heavy" in _codes(rep.errors)


def test_marathon_pace_on_consecutive_days_is_an_error():
    """75' of IM-pace brick on Saturday plus 40' at MP on Sunday = 115' in 36 h."""
    plan = good_plan()
    plan.anchors = {"run_threshold_s_per_km": 215.0}
    wk = plan.weeks[0]
    sat, sun = wk.start_date + dt.timedelta(days=5), wk.start_date + dt.timedelta(days=6)
    wk.sessions.append(session(sat, Sport.RUN, "Brick run (IM pace)", 80, 75,
                               IntensityClass.MODERATE, brick=True))
    wk.sessions[-1].description = "Brick: 75' @ 4:20/km (IM pace)"
    long_run = next(s for s in wk.sessions if s.date == sun and s.sport is Sport.RUN)
    long_run.description = "Aerobic long run; last 40' @ 4:20/km"
    rep = validate_plan(plan, starting_ctl=80.0)
    assert "run_stacking" in _codes(rep.errors)


def test_monday_session_is_an_error_when_the_plan_rests_on_mondays():
    plan = good_plan()
    plan.rest_weekday = 0
    rep = validate_plan(plan, starting_ctl=80.0)
    assert "monday_session" in _codes(rep.errors)


def test_documented_monday_exception_is_allowed():
    plan = good_plan()
    plan.rest_weekday = 0
    for wk in plan.weeks:
        for s in wk.sessions:
            if s.date.weekday() == 0:
                s.flags.append("monday-exception: already executed")
    rep = validate_plan(plan, starting_ctl=80.0)
    assert "monday_session" not in _codes(rep.issues)


def test_post_race_week_is_not_the_ramp_baseline():
    """A week lightened by a race (or its recovery days) is not a clean load
    reference: resuming normal load afterwards is not a reckless ramp."""
    start = dt.date(2026, 6, 22)
    tss = [500, 520, 420, 380, 540]
    weeks = [build_week(i + 1, start + dt.timedelta(weeks=i), Phase.BUILD, t)
             for i, t in enumerate(tss)]
    race_day = start + dt.timedelta(weeks=2, days=6)
    weeks[2].sessions.append(PlannedSession(date=race_day, sport=Sport.OTHER, name="RACE: B",
                                            category=EventCategory.RACE, planned_tss=40,
                                            planned_duration_s=3600))
    plan = TrainingPlan(plan_id="t", start_date=start, starting_ctl=75.0, weeks=weeks)
    rep = validate_plan(plan, starting_ctl=75.0, training_age="advanced")
    jumps = [i for i in rep.issues if i.code == "weekly_tss_jump" and i.week_index == 5]
    assert not jumps, [i.message for i in jumps]
