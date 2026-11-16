"""Deterministic plan validator — the guardrail between the agent and `push`.

Loads knowledge/triathlon/rules.yaml and checks a TrainingPlan's *shape* without
any LLM: ramp-rate caps, recovery-week cadence, taper shape, race-day form (TSB)
target, intensity distribution, weekly hours vs tolerance, frequency minimums,
and hard sanity limits.

`errors` block a push; `warnings` annotate. This is what makes an LLM planner
safe to act on.
"""

from __future__ import annotations

import datetime as dt
import math
from enum import Enum

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from trinity.knowledge import load_rules
from trinity.models.common import Sport
from trinity.models.plan import DistanceBucket, EventCategory, IntensityClass, Phase, TrainingPlan
from trinity.planning.prescription import (
    content_class,
    content_tss,
    intensity_minutes,
    marathon_pace_minutes,
    quality_segments,
)

TAU_CTL = 42
TAU_ATL = 7


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


class Issue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    severity: Severity
    message: str
    week_index: int | None = None


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool = True
    issues: list[Issue] = Field(default_factory=list)
    projected_ctl_by_week: list[float] = Field(default_factory=list)
    projected_race_tsb: float | None = None

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.WARNING]


def _daily_plan_tss(plan: TrainingPlan) -> pd.Series:
    """Continuous daily planned-TSS series over the plan horizon (0-filled)."""
    sessions = plan.all_sessions()
    if not sessions:
        return pd.Series(dtype=float)
    start = plan.start_date
    end = max(s.date for s in sessions)
    idx = pd.date_range(start, end, freq="D").date
    daily = pd.Series(0.0, index=idx)
    for s in sessions:
        if s.date in daily.index:
            daily[s.date] += s.planned_tss
    return daily


def _project_load(plan: TrainingPlan, starting_ctl: float) -> tuple[pd.Series, pd.Series]:
    """Daily CTL/ATL over the plan horizon, seeded from starting fitness."""
    daily = _daily_plan_tss(plan)
    if daily.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float)
    idx = daily.index
    alpha_c, alpha_a = 1 - math.exp(-1 / TAU_CTL), 1 - math.exp(-1 / TAU_ATL)
    ctl, atl = [], []
    pc = pa = float(starting_ctl)
    for v in daily.to_numpy():
        pc += (v - pc) * alpha_c
        pa += (v - pa) * alpha_a
        ctl.append(pc)
        atl.append(pa)
    return pd.Series(ctl, index=idx), pd.Series(atl, index=idx)


def validate_plan(
    plan: TrainingPlan,
    *,
    starting_ctl: float | None = None,
    weekly_hours_tolerance: float | None = None,
    training_age: str = "intermediate",
) -> ValidationReport:
    rules = load_rules()
    report = ValidationReport()

    starting_ctl = starting_ctl if starting_ctl is not None else (plan.starting_ctl or 0.0)
    ctl_series, atl_series = _project_load(plan, starting_ctl)

    # Per-week projected CTL (end of each plan week).
    weekly_ctl: list[float] = []
    for wk in plan.weeks:
        week_end = wk.start_date + dt.timedelta(days=6)
        upto = ctl_series[[d for d in ctl_series.index if d <= week_end]]
        weekly_ctl.append(round(float(upto.iloc[-1]), 1) if len(upto) else starting_ctl)
    report.projected_ctl_by_week = weekly_ctl

    _check_ramp(plan, weekly_ctl, starting_ctl, rules, report, weekly_hours_tolerance)
    _check_weekly_progression(plan, rules, report)
    _check_recovery_cadence(plan, rules, report, training_age)
    _check_projected_acwr(plan, rules, report)
    _check_intensity_distribution(plan, rules, report)
    _check_volume_tolerance(plan, weekly_hours_tolerance, rules, report)
    _check_frequency(plan, rules, report)
    _check_hard_limits(plan, rules, report)
    _check_daily_stacking(plan, rules, report)
    _check_taper(plan, ctl_series, atl_series, rules, report)
    daily = _daily_plan_tss(plan)
    _check_label_vs_content(plan, report)
    _check_tss_provenance(plan, report)
    _check_taper_depth(plan, daily, rules, report)
    _check_post_race(plan, daily, report)
    _check_run_stacking(plan, report)
    _check_rest_weekday(plan, report)

    report.ok = len(report.errors) == 0
    return report


def _add(report, code, severity, message, week_index=None):
    report.issues.append(Issue(code=code, severity=severity, message=message, week_index=week_index))


def _is_effective_recovery(week, prev_week, red_lo: float) -> bool:
    """Recovery is DERIVED from the actual load drop, never trusted from the
    agent-supplied flag — otherwise a week mislabeled ``is_recovery=True`` could
    raise load and skip the ramp/cadence guardrails."""
    if prev_week is None or prev_week.planned_tss <= 0:
        return week.planned_tss == 0
    drop = (prev_week.planned_tss - week.planned_tss) / prev_week.planned_tss * 100
    return drop >= red_lo


def _check_ramp(plan, weekly_ctl, starting_ctl, rules, report, hours_tolerance=None):
    # No is_recovery exemption: a genuine recovery week is a *downward* move and
    # won't trip an upward cap, so always applying the cap is both safe and the
    # fix for the bypass.
    cap = rules["ramp"]["ctl_per_week_max"]
    base_cap = rules["ramp"]["ctl_per_week_max_base"]
    prev = starting_ctl
    for wk, ctl_end in zip(plan.weeks, weekly_ctl, strict=False):
        ramp = ctl_end - prev
        limit = base_cap if wk.phase is Phase.BASE else cap
        if ramp > limit:
            _add(report, "ramp_too_steep", Severity.ERROR,
                 f"Week {wk.week_index} projected CTL ramp {ramp:.1f}/wk exceeds cap {limit} ({wk.phase.value})",
                 wk.week_index)
        prev = ctl_end

    _check_build_progresses(plan, weekly_ctl, starting_ctl, rules, report, hours_tolerance)


def _check_build_progresses(plan, weekly_ctl, starting_ctl, rules, report,
                            hours_tolerance=None):
    """The other half of the ramp guardrail: a build block that does not actually
    raise CTL is too timid to drive adaptation, and is how a long plan arrives at
    its A race with the same fitness it started the season with. Measured per
    contiguous build/peak block (recovery valleys inside a block are expected and
    included), so a single flat week never trips it.

    Only build/peak is checked: ``ctl_per_week_min`` is a build-phase number and a
    long base block legitimately accumulates far more slowly.
    """
    floor = rules["ramp"].get("ctl_per_week_min")
    if not floor:
        return
    blocks: list[list[int]] = []
    for i, wk in enumerate(plan.weeks):
        if wk.phase in (Phase.BUILD, Phase.PEAK):
            if blocks and blocks[-1][-1] == i - 1:
                blocks[-1].append(i)
            else:
                blocks.append([i])
    for block in blocks:
        # Fewer than 5 weeks is too short to call a trend: a 3-week block sitting
        # at load equilibrium is a normal shape, not a timid one.
        if len(block) < 5:
            continue
        # Measure to the last NON-recovery week. A valley at the end of a block is
        # a deliberate unload, and including it would read every block as flat.
        loaded = [i for i in block if not plan.weeks[i].is_recovery]
        if len(loaded) < 3:
            continue
        start_ctl = weekly_ctl[block[0] - 1] if block[0] > 0 else starting_ctl
        gained = weekly_ctl[loaded[-1]] - start_ctl
        per_week = gained / (block.index(loaded[-1]) + 1)
        # A low ramp is only "timid" if there was room to load more. CTL gain per
        # week shrinks as CTL rises (the same absolute load buys less), so a block
        # already at the athlete's weekly-hours ceiling is at a physical limit, not
        # being timid — flagging it would just be noise the athlete cannot action.
        if hours_tolerance:
            peak_h = max(plan.weeks[i].planned_hours for i in loaded)
            if peak_h >= hours_tolerance * 0.9:
                continue
        if per_week < floor:
            first, last = plan.weeks[block[0]], plan.weeks[loaded[-1]]
            _add(report, "build_block_too_timid", Severity.WARNING,
                 f"Build block weeks {first.week_index}-{last.week_index} gains "
                 f"{gained:+.1f} CTL ({per_week:+.1f}/wk, min {floor}) — too timid "
                 "to drive adaptation",
                 first.week_index)


def _check_weekly_progression(plan, rules, report):
    tss_cap = rules["ramp"]["weekly_tss_increase_pct_max"]
    hours_cap = rules["ramp"]["weekly_hours_increase_pct_max"]
    red_lo = rules["recovery_week"]["volume_reduction_pct"][0]
    # Ramp is measured PEAK-TO-PEAK: compare each load week to the previous *load*
    # week, ignoring planned recovery valleys. Resuming normal load after a
    # deliberate down week is not a reckless ramp. A week that claims recovery but
    # doesn't actually drop load is treated as a load week (and flagged elsewhere).
    last_load_tss = last_load_hours = None
    weeks = plan.weeks
    affected = _race_affected_weeks(plan)
    for i, wk in enumerate(weeks):
        prev = weeks[i - 1] if i > 0 else None
        # Taper/transition (deload + race) weeks are not part of the progressive-
        # overload chain — same exclusion the recovery-cadence check applies.
        # Neither is a week holding a race or its recovery days: its load is set by
        # the race, not by the overload decision, so it is not a clean reference.
        if (wk.phase in (Phase.TAPER, Phase.TRANSITION) or _is_effective_recovery(wk, prev, red_lo)
                or wk.week_index in affected):
            continue
        tss, hours = wk.planned_tss, wk.planned_hours
        if last_load_tss and last_load_tss > 0:
            inc = (tss - last_load_tss) / last_load_tss * 100
            if inc > tss_cap:
                _add(report, "weekly_tss_jump", Severity.ERROR,
                     f"Week {wk.week_index} TSS jumps {inc:.0f}% vs prior load week (cap {tss_cap}%)",
                     wk.week_index)
        if last_load_hours and last_load_hours > 0:
            inc = (hours - last_load_hours) / last_load_hours * 100
            if inc > hours_cap:
                _add(report, "weekly_hours_jump", Severity.WARNING,
                     f"Week {wk.week_index} hours jump {inc:.0f}% vs prior load week (cap {hours_cap}%)",
                     wk.week_index)
        last_load_tss, last_load_hours = tss, hours


def _check_recovery_cadence(plan, rules, report, training_age="intermediate"):
    # Cadence cap is training-age specific (novice 2:1, others 3:1), bounded by
    # the absolute max_consecutive_load_weeks.
    by_age = rules["loading_pattern"].get("by_training_age", {})
    age_load_weeks = by_age.get(training_age, {}).get("load_weeks")
    abs_max = rules["loading_pattern"]["max_consecutive_load_weeks"]
    max_run = min(age_load_weeks, abs_max) if age_load_weeks else abs_max
    red_lo = rules["recovery_week"]["volume_reduction_pct"][0]
    weeks = plan.weeks
    run = 0
    for i, wk in enumerate(weeks):
        prev = weeks[i - 1] if i > 0 else None
        derived_recovery = _is_effective_recovery(wk, prev, red_lo)
        if derived_recovery or wk.phase in (Phase.TAPER, Phase.TRANSITION):
            run = 0
        else:
            run += 1
            if run > max_run:
                _add(report, "no_recovery_week", Severity.ERROR,
                     f"{run} consecutive load weeks by week {wk.week_index} (max {max_run} before a recovery week)",
                     wk.week_index)
        # A week claimed as recovery that doesn't actually reduce load is an ERROR
        # (this is the bypass vector — a fake recovery week hiding a load spike).
        if wk.is_recovery and not derived_recovery:
            _add(report, "fake_recovery_week", Severity.ERROR,
                 f"Week {wk.week_index} is marked is_recovery but does not reduce load >= {red_lo}%",
                 wk.week_index)


def _check_projected_acwr(plan, rules, report):
    """Acute:Chronic Workload Ratio on the PLANNED load. Flags load spikes the
    weekly-TSS cap can miss (e.g. a huge single session)."""
    daily = _daily_plan_tss(plan)
    if daily.empty:
        return
    hard_cap = rules["ramp"].get("acwr_hard_cap", 1.5)
    s = daily.reset_index(drop=True)
    cum = s.cumsum()
    for i in range(len(s)):
        if i < 27:
            continue  # need ~4 weeks of chronic load to be meaningful
        acute = cum.iloc[i] - (cum.iloc[i - 7] if i >= 7 else 0.0)
        chronic = (cum.iloc[i] - (cum.iloc[i - 28] if i >= 28 else 0.0)) / 4.0
        if chronic <= 0:
            continue
        acwr = acute / chronic
        if acwr > hard_cap:
            week_index = i // 7 + 1
            _add(report, "projected_acwr_spike", Severity.ERROR,
                 f"Projected ACWR {acwr:.2f} exceeds hard cap {hard_cap} around day {i + 1}",
                 week_index)
            return  # one report is enough


def _content_distribution(wk, anchors) -> dict:
    """Share of the week's time at each intensity, from prescription CONTENT: a
    'Z2' ride carrying 2 h at race power counts 2 h moderate, not 5 h easy."""
    tot = {c: 0.0 for c in IntensityClass}
    for s in wk.sessions:
        mins = s.planned_duration_s / 60.0
        if s.category is EventCategory.RACE:
            tot[IntensityClass.MODERATE] += mins
            continue
        for c, m in intensity_minutes(s.sport, s.intensity_class, s.description, mins,
                                      anchors).items():
            tot[c] += m
    total = sum(tot.values())
    return {c: (v / total if total else 0.0) for c, v in tot.items()}


def _check_intensity_distribution(plan, rules, report):
    min_easy = rules["intensity_distribution"]["min_easy_pct"] / 100.0
    for wk in plan.weeks:
        if wk.phase is Phase.PEAK or wk.planned_hours == 0:
            continue
        # A race is not a training-intensity choice: race weeks are exempt, or the
        # floor fires on every race week as an artifact and stops meaning anything.
        if any(s.category is EventCategory.RACE for s in wk.sessions):
            continue
        dist = _content_distribution(wk, plan.anchors)
        if dist[IntensityClass.EASY] < min_easy:
            _add(report, "too_much_intensity", Severity.WARNING,
                 f"Week {wk.week_index} easy share {dist[IntensityClass.EASY]*100:.0f}% below {min_easy*100:.0f}% min",
                 wk.week_index)


def _check_volume_tolerance(plan, tolerance, rules, report):
    hard_max = rules["hard_limits"]["max_weekly_hours"]
    for wk in plan.weeks:
        if wk.planned_hours > hard_max:
            _add(report, "weekly_hours_over_hard_limit", Severity.ERROR,
                 f"Week {wk.week_index} {wk.planned_hours:.1f}h exceeds hard limit {hard_max}h", wk.week_index)
        elif tolerance and wk.planned_hours > tolerance * 1.15:
            _add(report, "over_volume_tolerance", Severity.WARNING,
                 f"Week {wk.week_index} {wk.planned_hours:.1f}h exceeds athlete tolerance {tolerance:.1f}h +15%",
                 wk.week_index)


def _check_frequency(plan, rules, report):
    freq_min = rules["frequency_min_per_week"]
    for wk in plan.weeks:
        if wk.phase in (Phase.TAPER, Phase.TRANSITION) or wk.is_recovery:
            continue
        counts = {s.value: c for s, c in wk.sessions_by_sport().items()}
        for sport, mn in freq_min.items():
            if counts.get(sport, 0) < mn:
                _add(report, "low_frequency", Severity.WARNING,
                     f"Week {wk.week_index}: {counts.get(sport, 0)} {sport} sessions (< min {mn})", wk.week_index)


def _check_hard_limits(plan, rules, report):
    max_session_h = rules["hard_limits"]["max_single_session_hours"]
    brick_required = rules["long_session"]["brick_required_in_build"]
    for wk in plan.weeks:
        for s in wk.sessions:
            # The cap is for TRAINING sessions; you can't shorten an Ironman.
            if s.category is EventCategory.RACE:
                continue
            if s.planned_duration_s / 3600 > max_session_h:
                _add(report, "session_too_long", Severity.ERROR,
                     f"Week {wk.week_index} '{s.name}' {s.planned_duration_s/3600:.1f}h exceeds {max_session_h}h",
                     wk.week_index)
        if (brick_required and wk.phase is Phase.BUILD and not wk.is_recovery
                and not any(s.is_brick for s in wk.sessions)):
            _add(report, "missing_brick", Severity.WARNING,
                 f"Build week {wk.week_index} has no brick session", wk.week_index)

    _check_rest_days(plan, rules, report)
    _check_hard_session_spacing(plan, rules, report)


def _check_hard_session_spacing(plan, rules, report):
    """Minimum recovery between hard sessions in the same discipline. Run is the
    most injurious and so carries the longest spacing — two hard runs inside 48 h
    on a modest weekly volume is the classic soleus/Achilles pattern."""
    spacing = rules.get("hard_session_spacing_hours", {})
    if not spacing:
        return
    week_of = {}
    by_sport: dict[str, list[dt.date]] = {}
    for wk in plan.weeks:
        for s in wk.sessions:
            if s.intensity_class is not IntensityClass.HARD:
                continue
            if s.category is EventCategory.RACE:
                continue  # you don't get to reschedule a race
            by_sport.setdefault(s.sport.value, []).append(s.date)
            week_of[(s.sport.value, s.date)] = wk.week_index
    for sport, min_hours in spacing.items():
        days = sorted(by_sport.get(sport, []))
        for earlier, later in zip(days, days[1:], strict=False):
            gap_h = (later - earlier).days * 24
            if 0 < gap_h < min_hours:
                _add(report, "hard_sessions_too_close", Severity.WARNING,
                     f"Two hard {sport} sessions {gap_h}h apart "
                     f"({earlier} -> {later}), min {min_hours}h",
                     week_of[(sport, later)])


def _check_rest_days(plan, rules, report):
    """Scheduled full days off. A plan that puts a session on all seven days every
    week is a compliance failure before it is a physiological one, and for an
    athlete with a job it silently guarantees non-adherence. Race weeks are exempt
    (the race itself reorganises the week)."""
    min_rest = rules["hard_limits"].get("min_rest_days_per_week", 0)
    if min_rest <= 0:
        return
    for wk in plan.weeks:
        if any(s.category is EventCategory.RACE for s in wk.sessions):
            continue
        busy = {s.date for s in wk.sessions}
        rest = 7 - len({d for d in busy if 0 <= (d - wk.start_date).days < 7})
        if rest < min_rest:
            _add(report, "no_rest_day", Severity.WARNING,
                 f"Week {wk.week_index} schedules sessions on all 7 days "
                 f"({rest} rest days < min {min_rest})", wk.week_index)


def _check_daily_stacking(plan, rules, report):
    """Flag days that stack more than one long/key endurance effort. A bike->run
    brick is one intended unit; a standalone long run or long ride on top of it
    (the exact bug where a Sunday carried a long run AND a brick) is an overload +
    scheduling smell. The big-weekend pattern (long ride one day, long run the
    next) is fine because this is a *per-day* check."""
    cfg = rules.get("daily_load", {})
    lr_min = cfg.get("long_run_minutes", 75)
    lride_h = cfg.get("long_ride_hours", 2.5)
    max_anchors = cfg.get("max_endurance_anchors_per_day", 1)

    by_day: dict[dt.date, list] = {}
    week_of: dict[dt.date, int] = {}
    for wk in plan.weeks:
        for s in wk.sessions:
            by_day.setdefault(s.date, []).append(s)
            week_of[s.date] = wk.week_index

    for day in sorted(by_day):
        ss = by_day[day]
        if any(s.category is EventCategory.RACE for s in ss):
            continue
        has_brick = any(s.is_brick for s in ss)
        long_runs = [s for s in ss if s.sport is Sport.RUN and not s.is_brick
                     and s.planned_duration_s >= lr_min * 60]
        long_rides = [s for s in ss if s.sport is Sport.BIKE
                      and s.planned_duration_s >= lride_h * 3600]
        # The brick's own ride is part of the brick unit; only count standalone
        # long rides when there is no brick that day.
        anchors = (1 if has_brick else len(long_rides)) + len(long_runs)
        if anchors > max_anchors:
            pieces = ["a brick"] if has_brick else [
                f"long ride {s.planned_duration_s / 3600:.1f}h" for s in long_rides]
            pieces += [f"long run {round(s.planned_duration_s / 60)}min" for s in long_runs]
            _add(report, "stacked_long_day", Severity.WARNING,
                 f"Week {week_of[day]} {day:%a %Y-%m-%d}: {anchors} long/key efforts "
                 f"on one day ({', '.join(pieces)}) — spread across days to protect "
                 f"key-session quality and recovery", week_of[day])


def _check_taper(plan, ctl_series, atl_series, rules, report):
    peaks = any(wk.phase in (Phase.TAPER, Phase.PEAK) for wk in plan.weeks)
    if plan.goal_race is None and not plan.races:
        # A plan that tapers/peaks but declares no race can't have its race-day
        # form checked — that's a guardrail bypass. Require the race.
        if peaks:
            _add(report, "missing_goal_race", Severity.ERROR,
                 "Plan has a taper/peak phase but no goal_race; declare the race so race-day form can be validated")
        return
    if len(ctl_series) == 0:
        return

    # Validate EVERY declared race, not just the headline one: a multi-season plan
    # can carry more than one A race, and an un-validated A race is exactly where
    # a missing taper hides. plan.races wins when present; fall back to goal_race.
    races = plan.races or [plan.goal_race]
    races = sorted((r for r in races if r is not None), key=lambda r: r.date)
    headline = plan.goal_race or races[-1]

    # Races far beyond the plan's horizon (e.g. a championship months later that
    # the plan doesn't yet cover) can't have their form projected — grading them
    # on the last week the plan happens to contain would be meaningless. A race
    # landing on or shortly after the final week IS the plan's target, so allow a
    # fortnight of slack.
    horizon = max(wk.start_date for wk in plan.weeks) + dt.timedelta(days=20)

    for race in races:
        if race.date > horizon:
            continue
        _check_taper_duration(plan, race, rules, report)
        priority = race.priority.upper()
        target = rules["taper"]["target_tsb_by_priority"].get(priority)
        # Race-day TSB = yesterday's CTL - ATL (intervals 'Form').
        days = [d for d in ctl_series.index if d < race.date]
        if not days:
            continue
        last = days[-1]
        tsb = float(ctl_series[last] - atl_series[last])
        if race is headline or race.date == headline.date:
            report.projected_race_tsb = round(tsb, 1)
        if target and not (target[0] <= tsb <= target[1]):
            sev = Severity.ERROR if priority == "A" else Severity.WARNING
            _add(report, "race_tsb_out_of_range", sev,
                 f"{race.name} ({race.date}): projected race-day TSB {tsb:.1f} outside "
                 f"target {target} for priority {priority} race")

        # Form (CTL-ATL) is not fitness. A deep enough taper flatters TSB no matter
        # how little chronic load is underneath it, so an A race also has to arrive
        # at (or very near) the season's peak CTL. A plan that peaks in spring and
        # races in September has mis-sequenced the year, and the TSB check alone
        # cannot see it.
        if priority == "A":
            ctl_at_race = float(ctl_series[last])
            # Only the build leading to THIS race counts (a later A race legitimately
            # goes higher), and a taper-sized dip is expected, so the peak only
            # matters if it happened in a different part of the season entirely.
            upto = {d: float(v) for d, v in ctl_series.items() if d <= race.date}
            peak_date = max(upto, key=lambda d: upto[d])
            peak = upto[peak_date]
            stale_days = (race.date - peak_date).days
            if stale_days > 42 and ctl_at_race < peak - 2.0:
                _add(report, "a_race_below_season_peak", Severity.WARNING,
                     f"{race.name} ({race.date}): pre-race CTL {ctl_at_race:.1f} is "
                     f"{peak - ctl_at_race:.1f} below the {peak:.1f} peak reached "
                     f"{stale_days} days earlier ({peak_date}) — the A race is not the "
                     "fittest point of its own build")


def _taper_weeks_before(plan, race) -> int:
    """Consecutive taper weeks immediately preceding a race. Counting EVERY taper
    week in the plan is wrong once a plan spans several races — a 21-month plan
    with a taper before each of six races would report a 9-week taper."""
    weeks = sorted(plan.weeks, key=lambda w: w.start_date)
    before = [w for w in weeks if w.start_date <= race.date]
    run = 0
    for wk in reversed(before):
        if wk.phase is Phase.TAPER:
            run += 1
        else:
            break
    return run


def _check_taper_duration(plan, race, rules, report):
    """Taper length (consecutive taper weeks before the race) within the
    distance-specific window from rules.yaml. Advisory (warning)."""
    window = rules["taper"]["duration_days_by_distance"].get(race.bucket.value)
    if not window:
        return
    taper_weeks = _taper_weeks_before(plan, race)
    if taper_weeks == 0:
        # Only flag missing taper for A/B races (C races may be raced through).
        if race.priority.upper() in ("A", "B"):
            _add(report, "no_taper", Severity.WARNING,
                 f"{race.name}: no taper phase before priority {race.priority} "
                 f"{race.bucket.value} race")
        return
    taper_days = taper_weeks * 7
    lo, hi = window
    # Allow a week of slack on each side (weeks are coarse-grained).
    if not (lo - 7 <= taper_days <= hi + 7):
        _add(report, "taper_duration_out_of_range", Severity.WARNING,
             f"{race.name}: taper ~{taper_days}d ({taper_weeks}wk) outside {window}d "
             f"window for {race.bucket.value}")


# --- content, race-recovery and schedule checks -------------------------------

def _race_days(plan) -> list[dt.date]:
    return sorted({s.date for s in plan.all_sessions() if s.category is EventCategory.RACE})


def _race_affected_weeks(plan, post_days: int = 4) -> set[int]:
    """Weeks holding a race or any of the ``post_days`` days after one."""
    affected = set()
    races = _race_days(plan)
    for wk in plan.weeks:
        end = wk.start_date + dt.timedelta(days=6)
        for r in races:
            if wk.start_date <= r + dt.timedelta(days=post_days) and r <= end:
                affected.add(wk.week_index)
    return affected


def _check_label_vs_content(plan, report):
    """An EASY-labelled session whose prescription holds >10% work is mislabelled."""
    for wk in plan.weeks:
        for s in wk.sessions:
            if s.category is not EventCategory.WORKOUT or s.intensity_class is not IntensityClass.EASY:
                continue
            mins = s.planned_duration_s / 60.0
            segs = quality_segments(s.description, sport=s.sport, anchors=plan.anchors,
                                    session_minutes=mins)
            found = content_class(segs, mins)
            if found is not None:
                work = sum(x.minutes for x in segs)
                _add(report, "label_contradicts_content", Severity.ERROR,
                     f"Week {wk.week_index} '{s.name}' is labelled easy but prescribes "
                     f"{work:.0f}' of work ({work / mins * 100:.0f}% of {mins:.0f}') -> {found.value}",
                     wk.week_index)


def _check_tss_provenance(plan, report, tolerance: float = 0.25):
    """Planned TSS must follow the prescription (IF x duration), not the hours.
    Only checkable when the plan carries the anchors its paces were rendered from."""
    if not plan.anchors:
        return
    for wk in plan.weeks:
        for s in wk.sessions:
            if s.category is not EventCategory.WORKOUT or s.planned_duration_s <= 0:
                continue
            want = content_tss(s.sport, s.intensity_class, s.description,
                               s.planned_duration_s / 60.0, plan.anchors)
            if want > 0 and abs(s.planned_tss - want) / want > tolerance:
                _add(report, "tss_not_from_content", Severity.ERROR,
                     f"Week {wk.week_index} '{s.name}': planned {s.planned_tss:.0f} TSS vs "
                     f"{want:.0f} from its prescription", wk.week_index)


def _check_taper_depth(plan, daily, rules, report):
    """Before an A race the taper window must be >=40% lighter than the peak
    block of the same length before it (Mujika & Padilla; Bosquet). The window
    is 14 days for a full distance, 7 days (the final week) for shorter races."""
    if daily.empty:
        return
    need = rules["taper"]["volume_reduction_pct"][0] / 100.0
    races = plan.races or ([plan.goal_race] if plan.goal_race else [])
    for race in races:
        if race is None or race.priority.upper() != "A":
            continue
        window_days = 14 if race.bucket is DistanceBucket.FULL else 7
        win = [race.date - dt.timedelta(days=k) for k in range(1, window_days + 1)]
        if not all(d in daily.index for d in win):
            continue
        taper_mean = float(daily[win].mean())
        start = min(win)
        hist = daily[[d for d in daily.index if start - dt.timedelta(days=56) <= d < start]]
        if len(hist) < window_days:
            continue
        peak = float(hist.rolling(window_days).mean().max())
        if peak <= 0:
            continue
        cut = 1 - taper_mean / peak
        if cut < need:
            _add(report, "taper_too_shallow", Severity.ERROR,
                 f"{race.name} ({race.date}): final {window_days} days are only {cut * 100:.0f}% "
                 f"below the peak {window_days}-day block (need >= {need * 100:.0f}%)")


def _check_post_race(plan, daily, report, post_days: int = 4, cap: float = 0.5):
    """The 4 days after a race must stay under 50% of normal daily load."""
    if daily.empty:
        return
    races = set(_race_days(plan))
    for r in sorted(races):
        post = [r + dt.timedelta(days=k) for k in range(1, post_days + 1)]
        if not all(d in daily.index for d in post) or any(d in races for d in post):
            continue
        pre = [d for d in daily.index if r - dt.timedelta(days=28) <= d < r]
        if len(pre) < 7:
            continue
        normal = float(daily[pre].mean())
        after = float(daily[post].mean())
        if normal > 0 and after > cap * normal:
            wk = next((w.week_index for w in plan.weeks
                       if w.start_date <= post[0] <= w.start_date + dt.timedelta(days=6)), None)
            _add(report, "post_race_too_heavy", Severity.ERROR,
                 f"Race {r}: the {post_days} days after average {after:.0f} TSS/day, "
                 f"{after / normal * 100:.0f}% of the {normal:.0f}/day norm (cap {cap * 100:.0f}%)",
                 wk)


def _check_run_stacking(plan, report, cap_min: float = 90.0):
    """No more than 90' of marathon/IM-pace running in any 36 h (two consecutive
    days, since sessions carry dates not times). Never MP Saturday AND Sunday."""
    per_day: dict[dt.date, float] = {}
    for s in plan.all_sessions():
        if s.category is EventCategory.RACE:
            continue
        m = marathon_pace_minutes(s.sport, s.description, s.planned_duration_s / 60.0, plan.anchors)
        if m:
            per_day[s.date] = per_day.get(s.date, 0.0) + m
    for d, m in sorted(per_day.items()):
        pair = m + per_day.get(d + dt.timedelta(days=1), 0.0)
        if pair > cap_min:
            wk = next((w.week_index for w in plan.weeks
                       if w.start_date <= d <= w.start_date + dt.timedelta(days=6)), None)
            _add(report, "run_stacking", Severity.ERROR,
                 f"{d} + next day: {pair:.0f}' of marathon/IM-pace running within 36 h "
                 f"(cap {cap_min:.0f}')", wk)


_DAY = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _check_rest_weekday(plan, report):
    """The plan's rest day stays empty unless a session documents an exception."""
    if plan.rest_weekday is None:
        return
    for wk in plan.weeks:
        for s in wk.sessions:
            if s.date.weekday() != plan.rest_weekday:
                continue
            if any(f.startswith(("monday-exception", "rest-day-exception")) for f in s.flags):
                continue
            _add(report, "monday_session", Severity.ERROR,
                 f"Week {wk.week_index} '{s.name}' on {s.date} ({_DAY[plan.rest_weekday]}, the "
                 "plan's rest day) with no documented exception", wk.week_index)
