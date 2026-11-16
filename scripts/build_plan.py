"""Build a TrainingPlan from a declarative season spec (YAML).

Generic periodization engine: all athlete/season-specific input lives in the
spec file (see examples/season-2026-malbork.yaml), nothing is hardcoded here.
Each week is either generated from the swim-heavy build microcycle or listed
explicitly (fixed / taper / recovery / race weeks).

Run:
  uv run python scripts/build_plan.py [SPEC.yaml]
  TRINITY_DATA_DIR=examples/data uv run python scripts/build_plan.py examples/season-2026-malbork.yaml

Then: uv run trinity validate <plans_dir>/<plan_id>.json
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import yaml

from trinity.models.common import Sport
from trinity.models.plan import (
    EventCategory,
    GoalRace,
    IntensityClass,
    Phase,
    PlannedSession,
    PlanWeek,
    TrainingPlan,
)
from trinity.models.profile import DistanceBucket
from trinity.planning.prescription import RATE, content_tss, derive_intensity
from trinity.planning.spec_lint import lint_spec
from trinity.planning.workout_builder import EXTERNAL_ID_PREFIX
from trinity.storage.io import save_json
from trinity.storage.paths import plan_path

DEFAULT_SPEC = Path(__file__).resolve().parents[1] / "examples" / "season-2026-malbork.yaml"

INTENSITY = {"easy": IntensityClass.EASY, "moderate": IntensityClass.MODERATE,
             "hard": IntensityClass.HARD}
CATEGORY = {"workout": EventCategory.WORKOUT, "race": EventCategory.RACE,
            "note": EventCategory.NOTE}

def mk_session(day: dt.date, *, sport: Sport, name: str, intensity: IntensityClass,
               minutes: float, anchors: dict, brick: bool = False, desc: str = "",
               category: EventCategory = EventCategory.WORKOUT,
               tss: float | None = None, flags: list[str] | None = None) -> PlannedSession:
    """A session whose label and load come from its CONTENT.

    The label is upgraded (never downgraded) when the prescription holds more
    work than it admits to, and TSS is each work block at IF^2 plus the rest at
    the easy rate -- so a 5 h "Z2" ride carrying 2 h at race power is billed as
    what it is. An explicit ``tss`` (races) is kept as given.
    """
    dur = int(round(minutes * 60))
    if category is EventCategory.WORKOUT:
        intensity = derive_intensity(intensity, desc, sport, anchors=anchors,
                                     session_minutes=minutes)
    if tss is None:
        tss = content_tss(sport, intensity, desc, minutes, anchors)
    return PlannedSession(
        date=day, sport=sport, name=name, intensity_class=intensity,
        planned_tss=tss, planned_duration_s=dur, is_brick=brick,
        category=category, description=desc or None, flags=list(flags or []),
        external_id=(f"{EXTERNAL_ID_PREFIX}-{day.isoformat()}-{sport.value}-"
                     f"{name[:12].replace(' ', '_')}"),
    )


def race_tss_from_card(card: dict, anchors: dict, session_minutes: float) -> float | None:
    """Race-day TSS from the race card's own splits, so a race's load follows its
    targets instead of a hand-typed number. The PLANNING column is used when the
    card has one (the load actually expected), else the target. Swim at ~65 TSS/h (race-effort sTSS),
    bike at IF = card watts / FTP, run at IF = threshold / split pace; any session
    time beyond the legs (warm-up) at the easy run rate."""
    import re

    ftp = float(card.get("ftp_w") or anchors.get("ftp_w") or 0)
    thr = float(anchors.get("run_threshold_s_per_km") or 0)
    legs = card.get("splits") or []
    tss = 0.0
    used = 0.0
    for leg in legs:
        name = str(leg.get("leg", ""))
        secs = _hms_to_s(str(leg.get("planning") or leg.get("target") or "0"))
        if secs <= 0:
            continue
        if name.lower().startswith("swim"):
            tss += secs / 3600 * 65.0
        elif name.lower().startswith("bike"):
            m = re.search(r"(\d{2,3})(?:\s*-\s*(\d{2,3}))?\s*W\b", str(leg.get("detail", "")))
            if not (m and ftp):
                return None
            mid = (float(m.group(1)) + float(m.group(2) or m.group(1))) / 2
            tss += secs / 3600 * (mid / ftp) ** 2 * 100
        elif name.lower().startswith("run"):
            km = re.search(r"([\d.]+)\s*km", name)
            if not (km and thr):
                return None
            pace_s = secs / float(km.group(1))
            tss += secs / 3600 * (thr / pace_s) ** 2 * 100
        else:
            continue          # transitions
        used += secs
    if used <= 0:
        return None
    extra_min = max(session_minutes - used / 60.0, 0.0)
    return round(tss + extra_min / 60.0 * RATE[(Sport.RUN, IntensityClass.EASY)], 1)


def _hms_to_s(text: str) -> float:
    parts = [p for p in text.replace("'", "").split(":") if p.strip()]
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return 0.0
    while len(nums) < 3:
        nums.insert(0, 0.0)
    return nums[0] * 3600 + nums[1] * 60 + nums[2]


def session_from_spec(monday: dt.date, spec: dict, anchors: dict,
                      race_cards: dict | None = None) -> PlannedSession:
    day = monday + dt.timedelta(days=int(spec["day"]))
    category = CATEGORY[spec.get("category", "workout")]
    minutes = float(spec["minutes"])
    tss = spec.get("tss")
    if category is EventCategory.RACE and tss is None and race_cards:
        card = race_cards.get(day)
        if card is not None:
            tss = race_tss_from_card(card, anchors, minutes)
    return mk_session(
        day, sport=Sport(spec["sport"]), name=spec["name"],
        intensity=INTENSITY[spec.get("intensity", "easy")], minutes=minutes,
        anchors=anchors, brick=bool(spec.get("brick", False)), desc=spec.get("desc", ""),
        category=category, tss=tss, flags=spec.get("flags"),
    )


def watts(ftp_w: float | None, lo_pct: float, hi_pct: float) -> str:
    """Render a %FTP band as absolute watts so a prescription can never be read
    against the wrong FTP. Percentages alone are ambiguous whenever the training
    platform's FTP setting disagrees with the athlete's real threshold."""
    if not ftp_w:
        return f"{lo_pct:.0f}-{hi_pct:.0f}% FTP"
    return f"{round(ftp_w * lo_pct / 100)}-{round(ftp_w * hi_pct / 100)} W"


def pace(thr_s_per_km: float | None, lo_pct: float, hi_pct: float) -> str:
    """Render a %threshold-pace band as min:sec/km. lo_pct is the SLOWER end."""
    if not thr_s_per_km:
        return f"{lo_pct:.0f}-{hi_pct:.0f}% threshold pace"
    def fmt(p: float) -> str:
        s = thr_s_per_km / (p / 100.0)
        return f"{int(s // 60)}:{int(round(s % 60)):02d}"
    return f"{fmt(lo_pct)}-{fmt(hi_pct)}/km"


def build_microcycle(monday: dt.date, *, target_h: float, key: IntensityClass,
                     long_ride_h: float, long_run_h: float, brick_run_h: float = 0.5,
                     anchors: dict, long_ride_blocks: str | None = None,
                     swim_css: str | None = None, half_focus: bool = False,
                     ftp_w: float | None = None, run_threshold_s: float | None = None,
                     run_key: str | None = None, bike_key: str | None = None,
                     no_vo2: bool = False, ow_swim: bool = False,
                     fuel_g_per_h: str = "70-90", long_run_finish: str | None = None,
                     brick_pace: str | None = None, bike_tempo_easy: bool = False,
                     easy_days: int = 0, run_easy: str | None = None) -> list[PlannedSession]:
    """Build week (Mon-Sun). Fixed long ride/brick/long run/key sets anchor the
    week; support sessions scale so weekly hours land near target_h.

    Monday is left EMPTY as a genuine rest day. Keeps swim>=3/wk and run>=4/wk.
    Labels and TSS are derived from each session's CONTENT (see mk_session), so a
    session carrying race-pace work cannot be counted as easy.

    ``brick_pace`` turns the brick into a timed IM-pace run ("60' @ 4:22/km");
    ``no_vo2`` drops VO2 from the bike key set (peak IM weeks); ``bike_key``
    overrides it outright (test weeks); ``easy_days`` makes the first N days
    post-race easy at half duration, AFTER scaling, so the week simply comes out
    lighter rather than the rest of it being inflated to compensate.
    Nothing athlete-specific is hardcoded: every pace and watt comes from the spec.
    """
    def S(off, sport, name, inten, mins, **kw):
        return mk_session(monday + dt.timedelta(days=off), sport=sport, name=name,
                          intensity=inten, minutes=mins, anchors=anchors, **kw)

    bike_key_h = 0.95 if target_h >= 15 else 0.8
    bike_tempo_h = 1.1 if target_h >= 15 else 0.9
    fuel = (f"Fuel {fuel_g_per_h} g/h: one concentrated 1:0.8 glucose:fructose bottle (or "
            "hydrogel) plus separate water bottles, so carbohydrate and fluid are set "
            "independently; gels on the bike top-up only (gels on the run are fine). "
            "Log g/h, ml/h and a 1-5 GI score.")
    if brick_pace and brick_run_h >= 0.75:
        brick_name = "Brick run (IM pace)"
        brick_desc = (f"Brick: {round(brick_run_h * 60)}' @ {brick_pace} (IM pace) off the "
                      "fluid-only final 30' of the ride -- the bike->run handover that failed "
                      "at Wolsztyn. Fuel 60-75 g/h (gels fine).")
    else:
        brick_name = "Brick run off bike"
        brick_desc = "First 10' at race pace off the bike, then Z2."
    if long_ride_blocks:
        long_ride_name = "Long ride + IM blocks"
        long_ride_desc = (f"TT bike in aero. IM-pace work: {long_ride_blocks} (rest of ride steady "
                          f"Z2). {fuel}")
    else:
        long_ride_name = "Long ride (Z2)"
        long_ride_desc = f"TT bike in aero, steady Z2, race position. {fuel}"
    easy_week = key is IntensityClass.EASY
    if easy_week:
        bike_key_desc = ("Z2 throughout, a few 1' pickups late to keep snap. This is a valley "
                         "-- no key set.")
    elif bike_key:
        bike_key_desc = bike_key
    elif half_focus:
        bike_key_desc = (f"Half-IM race-power over/unders 3x10': 2' @ {watts(ftp_w, 100, 105)} "
                         f"/ 3' @ {watts(ftp_w, 88, 92)}. NOT a VO2 session.")
    elif no_vo2:
        bike_key_desc = (f"Threshold 3x12' @ {watts(ftp_w, 95, 100)}. No VO2 in the peak IM "
                         "weeks -- the long ride carries the load.")
    else:
        bike_key_desc = (f"VO2 5x4' @ {watts(ftp_w, 108, 115)} (equal rest) OR threshold "
                         f"3x12' @ {watts(ftp_w, 95, 100)}. Alternate week to week.")
    swim_third = (
        "OPEN WATER in the wetsuit: 2500-3000m continuous including 6x(100m sighting every "
        "3rd stroke + 50m drafting on feet). Beach start + 90-degree buoy turns."
        if ow_swim else
        "Pool: continuous + pull/paddles, 2500-3000m steady.")
    run_key_desc = run_key or (
        f"Threshold 5x6' @ {pace(run_threshold_s, 98, 102)} off 90s jog.")
    if long_run_finish:
        long_run_name = "Long run (steady finish)"
        long_run_desc = f"Aerobic long run; {long_run_finish}"
    else:
        long_run_name = "Long run (Z2)"
        long_run_desc = "Aerobic long run, conversational throughout."
    E, M, H = IntensityClass.EASY, IntensityClass.MODERATE, IntensityClass.HARD
    # (session, is_anchor): anchors keep their prescribed duration; the rest scale.
    out: list[tuple[PlannedSession, bool]] = [
        (S(1, Sport.SWIM, "Swim technique + aerobic", E, 50,
           desc="Drills + 8-12x100 aerobic @ CSS+5s. Form under low fatigue."), False),
        (S(1, Sport.BIKE, "Bike key set", key, bike_key_h * 60, desc=bike_key_desc),
         not easy_week),
        ((S(2, Sport.RUN, "Run easy + strides", E, 40,
            desc="Z2 + 6x20s strides. Valley week -- no run quality."), False)
         if easy_week else
         (S(2, Sport.RUN, "Run key set", H if key is H else M, 55, desc=run_key_desc), True)),
        (S(2, Sport.SWIM, "Swim threshold", M, 55,
           desc=(f"Main set ~2000m @ CSS (target {swim_css})." if swim_css
                 else "Main set ~2000m @ CSS.")), False),
        ((S(3, Sport.BIKE, "Bike Z2 endurance", E, bike_tempo_h * 60,
            desc="Aerobic Z2 throughout. This ride is volume, not quality."), False)
         if bike_tempo_easy else
         (S(3, Sport.BIKE, "Bike tempo/sweet-spot", M, bike_tempo_h * 60,
            desc=f"2-3x20' @ {watts(ftp_w, 85, 90)} (true sweet spot)."), False)),
        (S(3, Sport.RUN,
           "Run easy + strides" if run_easy else "Run easy (frequency)", E, 35,
           desc=run_easy or "Z2 only. Fourth run: tendon loading + volume, zero intensity."),
         False),
        (S(4, Sport.SWIM, "Swim aerobic endurance", E, 50, desc=swim_third), False),
        (S(4, Sport.OTHER, "Strength (lower + core)", E, 45,
           desc="Posterior chain + calf/Achilles loading + core. Injury prevention, "
                "not fatigue."), False),
        (S(5, Sport.BIKE, long_ride_name, E, long_ride_h * 60, desc=long_ride_desc), True),
        (S(5, Sport.RUN, brick_name, E, brick_run_h * 60, brick=True, desc=brick_desc), True),
    ]
    if long_run_h > 0:
        out.append((S(6, Sport.RUN, long_run_name, E, long_run_h * 60, desc=long_run_desc), True))
    anchor_h = sum(s.planned_duration_s for s, a in out if a) / 3600
    flex_h = sum(s.planned_duration_s for s, a in out if not a) / 3600
    want_flex = max(target_h - anchor_h, flex_h * 0.5)
    factor = want_flex / flex_h if flex_h > 0 else 1.0
    scaled: list[PlannedSession] = []
    for s, is_anchor in out:
        mins = s.planned_duration_s / 60 * (1.0 if is_anchor else factor)
        scaled.append(mk_session(s.date, sport=s.sport, name=s.name, intensity=s.intensity_class,
                                 minutes=mins, anchors=anchors, brick=s.is_brick,
                                 desc=s.description or ""))
    if easy_days:
        # Post-race: the first N days are easy at half duration. Key sets become
        # easy sessions -- the race was the quality.
        eased = []
        cutoff = monday + dt.timedelta(days=easy_days)
        for s in scaled:
            if s.date < cutoff:
                easy_name = {Sport.SWIM: "Swim easy", Sport.BIKE: "Easy spin",
                             Sport.RUN: "Run easy", Sport.OTHER: "Mobility"}[s.sport]
                eased.append(mk_session(
                    s.date, sport=s.sport, name=f"{easy_name} (post-race)", intensity=E,
                    minutes=s.planned_duration_s / 60 * 0.5, anchors=anchors,
                    desc="Post-race: Z1-2 only, half duration. The race was this week's quality.",
                    flags=["post-race-easy"]))
            else:
                eased.append(s)
        scaled = eased
    return scaled


def build_plan(spec: dict) -> TrainingPlan:
    meta = spec["meta"]
    anchors = {k: float(meta[k]) for k in ("ftp_w", "run_threshold_s_per_km")
               if meta.get(k) is not None}
    a_races = [r for r in spec["races"] if str(r.get("priority", "")).upper() == "A"]
    a_race = max(a_races, key=lambda r: str(r["date"]))
    # Carry every race the spec targets so the validator can check each one's taper
    # and race-day form, not just the headline race. Races the plan doesn't cover
    # (e.g. a championship beyond the plan's last week) are still listed; the
    # validator skips any race with no projected days before it.
    races = [
        GoalRace(name=r["name"], date=dt.date.fromisoformat(str(r["date"])),
                 bucket=DistanceBucket(r["bucket"]), priority=str(r.get("priority", "A")).upper())
        for r in sorted(spec["races"], key=lambda r: str(r["date"]))
        if r["bucket"] in {b.value for b in DistanceBucket}
    ]
    race_cards = {dt.date.fromisoformat(str(r["date"])): r for r in spec["races"]}
    plan = TrainingPlan(
        athlete_id=meta["athlete_id"],
        plan_id=meta["plan_id"],
        start_date=dt.date.fromisoformat(str(meta["start_date"])),
        starting_ctl=float(meta["starting_ctl"]),
        goal_race=GoalRace(name=a_race["name"], date=dt.date.fromisoformat(str(a_race["date"])),
                           bucket=DistanceBucket(a_race["bucket"]), priority="A"),
        races=races,
        anchors=anchors,
        rest_weekday=meta.get("rest_weekday"),
        rationale=meta.get("rationale", "").strip() or None,
    )
    weeks: list[PlanWeek] = []
    for w in spec["weeks"]:
        monday = dt.date.fromisoformat(str(w["start"]))
        if w["type"] == "microcycle":
            mc = dict(w["microcycle"])
            mc["key"] = INTENSITY[mc["key"]]
            mc.setdefault("swim_css", meta.get("swim_css"))
            # Anchors for absolute watt/pace rendering come from the spec meta, so
            # a prescription can never silently resolve against the wrong FTP.
            mc.setdefault("ftp_w", meta.get("ftp_w"))
            mc.setdefault("run_threshold_s", meta.get("run_threshold_s_per_km"))
            sessions = build_microcycle(monday, anchors=anchors, **mc)
            sessions += [session_from_spec(monday, s, anchors, race_cards)
                         for s in w.get("extra_sessions", [])]
        else:
            sessions = [session_from_spec(monday, s, anchors, race_cards) for s in w["sessions"]]
        weeks.append(PlanWeek(week_index=int(w["index"]), start_date=monday,
                              phase=Phase(w["phase"]), is_recovery=bool(w.get("is_recovery", False)),
                              sessions=sessions))
    plan.weeks = weeks
    return plan


def main() -> None:
    spec_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SPEC
    raw = spec_path.read_text()
    spec = yaml.safe_load(raw)
    issues = lint_spec(spec, raw)
    for i in issues:
        print(f"  {i.severity.value:<7} {i.code}: {i.message}")
    if any(i.severity.value == "error" for i in issues):
        sys.exit(f"spec lint failed: {sum(i.severity.value == 'error' for i in issues)} error(s)")
    plan = build_plan(spec)
    out = plan_path(plan.plan_id)
    save_json(out, plan.model_dump(mode="json"))

    print(f"Wrote {out}  (from {spec_path})")
    print(f"{'wk':>2} {'start':>10} {'phase':>10} {'rec':>3} {'h':>5} {'TSS':>6}  swim/bike/run")
    for w in plan.weeks:
        cnt = {sp.value: c for sp, c in w.sessions_by_sport().items()}
        print(f"{w.week_index:>2} {w.start_date.isoformat():>10} {w.phase.value:>10} "
              f"{'Y' if w.is_recovery else '-':>3} {w.planned_hours:>5.1f} {w.planned_tss:>6.0f}  "
              f"{cnt.get('swim', 0)}/{cnt.get('bike', 0)}/{cnt.get('run', 0)}")


if __name__ == "__main__":
    main()
