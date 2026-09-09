"""Aithlete CLI — the commands the agent skills (and humans) invoke.

    aithlete fetch       pull data from sources -> raw (offline by default)
    aithlete analyze     print a metrics summary from raw data
    aithlete readiness   compute today's readiness flag (+persist)
    aithlete profile     build a baseline provenance-tagged profile (+persist)
    aithlete context     build the bounded context digest the agent reads (+persist)
    aithlete fitatu-import  ingest Fitatu nutrition exports -> raw store
    aithlete nutrition   build the bounded nutrition digest (fueling+recovery) (+persist)
    aithlete validate    run the deterministic guardrails against a plan JSON
    aithlete push        validate then push a plan to intervals.icu (gated)
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from pathlib import Path

import pandas as pd
import typer
from rich.console import Console
from rich.table import Table

from aithlete import knowledge
from aithlete.analysis import context as context_mod
from aithlete.analysis import metrics, profile_builder
from aithlete.analysis import nutrition as nutrition_mod
from aithlete.analysis import readiness as readiness_mod
from aithlete.analysis.loaders import (
    data_as_of,
    load_activities,
    load_nutrition,
    load_settings,
    load_wellness,
)
from aithlete.config import get_config
from aithlete.connectors import fitatu, food_concepts, food_mappings
from aithlete.connectors.fetch import fetch_all
from aithlete.connectors.fooddata import FoodDataClient
from aithlete.connectors.intervals import IntervalsClient
from aithlete.models.plan import PlanInputs, TrainingPlan
from aithlete.models.profile import AthleteProfile
from aithlete.planning.validator import validate_plan
from aithlete.planning.workout_builder import garmin_sync_warnings, plan_to_events
from aithlete.storage.io import load_json, save_json
from aithlete.storage.paths import (
    active_plan_path,
    calendar_path,
    context_path,
    plan_path,
    profile_path,
)

app = typer.Typer(add_completion=False, help="Triathlete analysis + AI training planning.")
console = Console()


def _load_frames():
    acts = load_activities()
    well = load_wellness()
    return acts, well


@app.command()
def fetch(
    full_resync: bool = typer.Option(False, help="Ignore watermark and refetch the lookback window."),
    since: str = typer.Option(None, help="Fetch from this date (YYYY-MM-DD), e.g. your first training. Overrides --days."),
    days: int = typer.Option(180, help="Lookback window in days when --since is not given."),
    reset: bool = typer.Option(False, help="Wipe the regenerable raw cache + watermark before fetching (clean slate)."),
):
    """Pull data from sources, dedup, and persist to the partitioned raw store."""
    since_date = None
    if since:
        try:
            since_date = dt.date.fromisoformat(since)
        except ValueError as exc:
            console.print(f"[red]Invalid --since date '{since}': {exc}. Use YYYY-MM-DD.[/]")
            raise typer.Exit(2) from exc
    res = fetch_all(full_resync=full_resync, since=since_date, lookback_days=days, reset=reset)
    mode = "offline (synthetic athlete)" if res["offline"] else "live"
    console.print(f"[bold green]Fetched[/] {res['activities']} activities, "
                  f"{res['wellness_days']} wellness days [{mode}] window={res['window']}")


@app.command()
def analyze():
    """Print a human/agent-readable metrics summary from the raw data."""
    acts, well = _load_frames()
    if acts.empty:
        console.print("[yellow]No activities. Run `aithlete fetch` first.[/]")
        raise typer.Exit(1)
    as_of = data_as_of(acts, well)
    load = metrics.latest_load(acts)
    base = metrics.hrv_rhr_baselines(well, as_of)
    dist = metrics.intensity_distribution(acts)

    t = Table(title=f"Training load as of {as_of}")
    for col in ("series", "CTL", "ATL", "TSB", "trend"):
        t.add_column(col)
    for k, v in load.items():
        t.add_row(k, f"{v['ctl']:.1f}", f"{v['atl']:.1f}",
                  f"{v['tsb']:.1f}" if v["tsb"] is not None else "-", v["trend"])
    console.print(t)
    console.print(f"Ramp/wk: [bold]{metrics.ctl_ramp_per_week(acts)}[/] CTL/week")
    if dist:
        console.print(f"Intensity (time): easy {dist.get('easy',0)*100:.0f}% / "
                      f"moderate {dist.get('moderate',0)*100:.0f}% / hard {dist.get('hard',0)*100:.0f}%")
    console.print(f"HRV 60d baseline: {base.get('hrv_baseline_60d_ms')} ms (SWC {base.get('hrv_swc_ms')}) | "
                  f"RHR 60d baseline: {base.get('resting_hr_baseline_bpm')} bpm")
    for w in metrics.weekly_summary(acts, weeks=4):
        console.print(f"  week {w['week_start']}: {w['tss']:.0f} TSS / {w['hours']:.1f} h")


@app.command()
def readiness(
    subjective: float = typer.Option(None, help="Subjective wellness mean 1-5 (5 best)."),
    illness: str = typer.Option(None, help="'above_neck' or 'below_neck' if ill."),
):
    """Compute today's readiness flag and persist it for the context digest."""
    acts, well = _load_frames()
    rep = readiness_mod.evaluate(well, acts, subjective_wellness=subjective, illness=illness)
    color = {"green": "green", "amber": "yellow", "red": "red"}[rep.overall_flag.value]
    console.print(f"Readiness: [bold {color}]{rep.overall_flag.value.upper()}[/] -> {rep.recommended_action}")
    for r in rep.results:
        c = {"green": "green", "amber": "yellow", "red": "red"}[r.flag.value]
        console.print(f"  [{c}]{r.flag.value:5}[/] {r.rule}: {r.message}")
    out = get_config().context_dir / "readiness-athlete.json"
    save_json(out, rep.model_dump(mode="json"))
    console.print(f"[dim]saved {out}[/]")


@app.command()
def profile(athlete: str = typer.Option("athlete", help="Athlete id.")):
    """Build a baseline provenance-tagged profile from fetched data (+persist).

    The agent refines interpretive fields (strengths/limiters, notes) afterwards.
    """
    acts, well = _load_frames()
    settings = load_settings()
    as_of = data_as_of(acts, well)
    prof = profile_builder.build_profile(settings, acts, well, as_of, athlete_id=athlete)
    # Preserve agent-authored interpretive fields if a profile already exists.
    # Race results are auto-detected from history on every run (not preserved).
    existing = load_json(profile_path(athlete))
    if existing:
        prev = AthleteProfile.model_validate(existing)
        prof.strengths = prev.strengths
        prof.limiters = prev.limiters
        prof.injury_history = prev.injury_history
        prof.training_age = prev.training_age
        prof.weekly_volume_tolerance_hours = prev.weekly_volume_tolerance_hours
        prof.notes = prev.notes
    save_json(profile_path(athlete), prof.model_dump(mode="json"))
    console.print(f"[green]Wrote baseline profile[/] {profile_path(athlete)}")
    if prof.results:
        bests = prof.best_times()
        console.print(f"  Detected [bold]{len(prof.results)}[/] races; "
                      f"PRs in {len(bests)} distance(s):")
        for bucket, bt in sorted(bests.items(), key=lambda kv: kv[0].value):
            h, rem = divmod(int(bt.total_time_s), 3600)
            m, s = divmod(rem, 60)
            console.print(f"    {bucket.value:>4} {h}:{m:02d}:{s:02d}  "
                          f"{bt.event_name or '—'} ({bt.date})")
    console.print(f"  FTP {prof.bike.ftp_w.value} W ({prof.bike.ftp_w.provenance.value}), "
                  f"CTL {prof.load.combined.ctl.value}, VO2max {prof.bike.vo2max.value} (estimated)")


@app.command()
def context(athlete: str = typer.Option("athlete", help="Athlete id.")):
    """Build the bounded context digest (profile + data + readiness + calendar)."""
    acts, well = _load_frames()
    prof_data = load_json(profile_path(athlete))
    if not prof_data:
        console.print("[yellow]No profile yet. Run `aithlete profile` first.[/]")
        raise typer.Exit(1)
    prof = AthleteProfile.model_validate(prof_data)
    rep_data = load_json(get_config().context_dir / "readiness-athlete.json")
    rep = None
    if rep_data:
        from aithlete.models.readiness import ReadinessReport
        rep = ReadinessReport.model_validate(rep_data)
    cal = load_json(calendar_path(athlete), default=[])
    digest = context_mod.build_digest(prof, acts, well, readiness=rep, calendar=cal, athlete_id=athlete)
    save_json(context_path(athlete), digest.model_dump(mode="json"))
    console.print(f"[green]Wrote context digest[/] {context_path(athlete)}")
    console.print(f"  sha256={digest.sha256()[:16]} | readiness={digest.readiness_flag.value} | "
                  f"races={len(digest.upcoming_races)} | missing={digest.missing}")


@app.command(name="fitatu-import")
def fitatu_import(
    from_: str = typer.Option(
        None, "--from", help="CSV file, directory, or glob of Fitatu meal_plan exports. "
        "Defaults to data/raw/fitatu/_inbox/*.csv.",
    ),
):
    """Ingest Fitatu meal-plan exports into the partitioned nutrition store."""
    inbox = get_config().raw_dir / "fitatu" / "_inbox"
    paths = fitatu.resolve_inputs(from_, inbox)
    if not paths:
        hint = from_ or str(inbox)
        console.print(f"[yellow]No Fitatu CSVs found at[/] {hint}")
        raise typer.Exit(1)
    # `like_product` mappings resolve against the log itself, so the composition
    # table is derived from the very exports being imported.
    rows = pd.concat([fitatu.parse_export(p) for p in paths], ignore_index=True)
    reference, unresolved = food_mappings.build_reference(
        log_composition=fitatu.log_composition(rows))
    floors = knowledge.load_nutrition_rules()["micronutrients"].get("zero_override_floor") or {}
    res = fitatu.import_exports(paths, reference, zero_override_floor=floors)
    win = f" window={res['window'][0]}..{res['window'][1]}" if res["window"] else ""
    console.print(f"[bold green]Imported[/] {res['days']} day(s) of nutrition from "
                  f"{res['files']} file(s){win} -> years {res['years']}")
    if reference:
        console.print(f"  enriched from reference tables: {len(reference)} mapped product(s)")
    if unresolved:
        console.print(f"  [yellow]{len(unresolved)} mapped product(s) unresolved[/] "
                      "(low confidence, or food not cached and offline)")
    if not reference and not unresolved:
        console.print("  [dim]no product mappings yet — run `aithlete food-gaps` to find "
                      "which blanks are worth closing[/]")


@app.command(name="food-gaps")
def food_gaps(
    limit: int = typer.Option(25, help="How many unmapped products to report."),
    nutrient: str = typer.Option(
        "calcium_mg", help="Rank gaps by the nutrient this product fails to supply."),
    candidates: bool = typer.Option(
        False, "--candidates", help="Query FoodData Central for match candidates "
        "(needs FDC_API_KEY and AITHLETE_OFFLINE=false)."),
):
    """Rank food-log products whose nutrient data is blank, worst offenders first.

    Ranking is by mass, not by how often a product appears: 56 entries of kefir
    matter far more than 56 entries of a spice. Use this to decide which products
    are worth mapping in data/food_mappings.json — the top ~200 products carry
    most of the intake, and the long tail can stay unmapped.
    """
    inbox = get_config().raw_dir / "fitatu" / "_inbox"
    paths = fitatu.resolve_inputs(None, inbox)
    if not paths:
        console.print(f"[yellow]No Fitatu CSVs found at[/] {inbox}")
        raise typer.Exit(1)

    rows = pd.concat([fitatu.parse_export(p) for p in paths], ignore_index=True)
    gaps = fitatu.blank_products(rows, nutrient)
    mapped = set(food_mappings.load_mappings())
    gaps = gaps[~gaps["product"].isin(mapped)]
    if gaps.empty:
        console.print(f"[green]Every product supplying {nutrient} is logged or mapped.[/]")
        return

    total_g = float(gaps["grams"].sum())
    console.print(f"[bold]{len(gaps)} unmapped product(s)[/] with no {nutrient}, "
                  f"{total_g / 1000:.1f} kg logged in total")
    table = Table("product", "items", "grams", "share of gap")
    for _, r in gaps.head(limit).iterrows():
        table.add_row(str(r["product"])[:58], str(int(r["items"])),
                      f"{r['grams']:.0f}", f"{r['grams'] / total_g * 100:4.1f}%")
    console.print(table)

    if not candidates:
        return
    client = FoodDataClient()
    if not client.online:
        console.print("[yellow]Candidate lookup needs FDC_API_KEY and AITHLETE_OFFLINE=false.[/]")
        raise typer.Exit(1)
    console.print("\n[bold]FoodData Central candidates[/] (pick one per product, "
                  "then record it in data/food_mappings.json):")
    for _, r in gaps.head(limit).iterrows():
        console.print(f"\n[cyan]{r['product']}[/]")
        for c in client.search(str(r["product"])):
            console.print(f"  {c.fdc_id:>9}  {c.data_type:<14} {c.description[:60]}")


@app.command(name="food-map")
def food_map(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what each concept resolves to, write nothing."),
    only: str = typer.Option(
        "", help="Resolve just the concepts whose key contains this string."),
    repin: bool = typer.Option(
        False, "--repin", help="Ignore existing pins and search again. Can CHANGE "
        "settled matches — scope it with --only."),
):
    """Resolve data/food_concepts.json into product -> USDA mappings.

    Concepts group products that pose the same food question, so a match is
    decided once and applied to the whole group. Rerun after adding concepts for
    newly logged products; mappings for products not covered by any concept are
    left untouched, so hand-written entries survive.

    Whatever a concept resolves to is pinned back into the concepts file, which
    makes reruns both instant and stable. That is not just tidiness: FDC's search
    ordering shifts between runs, and an unpinned rerun once silently swapped
    rice for rice FLOUR. Reopening a settled match takes --repin.

    Needs FDC_API_KEY and AITHLETE_OFFLINE=false the first time a food is seen;
    afterwards the local cache answers and this works offline.
    """
    everything = food_concepts.load_concepts()
    if not everything:
        console.print(f"[yellow]No concepts at[/] {food_concepts.concepts_path()}")
        console.print("Use `aithlete food-gaps` to find what needs grouping.")
        raise typer.Exit(1)
    concepts = ({k: v for k, v in everything.items() if only.lower() in k.lower()}
                if only else everything)
    if not concepts:
        console.print(f"[yellow]No concept key contains[/] {only!r}")
        raise typer.Exit(1)

    products = sum(len(c.products) for c in concepts.values())
    console.print(f"Resolving [bold]{len(concepts)} concept(s)[/] "
                  f"covering {products} product(s)...")
    res = food_concepts.resolve(concepts, repin=repin)

    table = Table("concept", "products", "USDA record", "confidence")
    for key, c in concepts.items():
        m = next((res.mappings[p] for p in c.products if p in res.mappings), None)
        table.add_row(key[:24], str(len(c.products)),
                      (m.fdc_description[:46] if m else "[red]-- none --[/]"),
                      c.confidence if m else "")
    console.print(table)

    applied = sum(1 for m in res.mappings.values() if m.trusted)
    console.print(f"\n[bold]{len(res.mappings)}[/] product mapping(s), "
                  f"[green]{applied}[/] applicable (high/medium), "
                  f"{len(res.unmatched)} left unmatched")
    if res.skipped:
        console.print(f"\n[yellow]Not applied ({len(res.skipped)} concept(s)):[/]")
        for key, why in res.skipped:
            console.print(f"  {key:24} {why}")

    changed = {k: v for k, v in res.chosen.items() if everything[k].pin != v}
    if changed:
        console.print(f"\n[bold]{len(changed)} concept(s) pinned[/] to a record:")
        for key, fdc_id in list(changed.items())[:12]:
            was = everything[key].pin
            console.print(f"  {key:24} {f'{was} -> ' if was else ''}{fdc_id}")

    if dry_run:
        console.print("\n[dim]--dry-run: nothing written.[/]")
        return
    # Merge rather than replace: mappings recorded by hand for one-off products
    # are not represented by any concept and must not be dropped by a rerun.
    merged = {**food_mappings.load_mappings(), **res.mappings}
    path = food_mappings.save_mappings(merged, unmatched=res.unmatched)
    if changed:
        food_concepts.save_concepts(
            {**everything,
             **{k: replace(everything[k], pin=v) for k, v in changed.items()}})
    console.print(f"\nWrote {len(merged)} mapping(s) -> {path}")
    console.print("Run `aithlete fitatu-import` to apply them.")


@app.command()
def nutrition(athlete: str = typer.Option("athlete", help="Athlete id.")):
    """Build the bounded nutrition digest (fueling + recovery) and persist it."""
    nut = load_nutrition()
    if nut.empty:
        console.print("[yellow]No nutrition data. Run `aithlete fitatu-import` first.[/]")
        raise typer.Exit(1)
    acts, well = _load_frames()
    prof = None
    prof_data = load_json(profile_path(athlete))
    if prof_data:
        prof = AthleteProfile.model_validate(prof_data)
    digest = nutrition_mod.build_digest(nut, acts, well, profile=prof, athlete_id=athlete)
    out = get_config().context_dir / f"nutrition-{athlete}.json"
    save_json(out, digest.model_dump(mode="json"))

    ea = digest.energy_availability_kcal_per_kg
    color = {"green": "green", "amber": "yellow", "red": "red"}[digest.energy_availability_flag.value]
    console.print(f"Energy availability: [bold {color}]{digest.energy_availability_flag.value.upper()}[/] "
                  f"({ea.value} {ea.unit}) over {digest.days_logged} logged days "
                  f"[{digest.window_from}..{digest.window_to}]")
    console.print(f"  intake {digest.mean_intake_kcal.value} kcal/day | "
                  f"carbs {digest.carbs_g_per_kg.value} g/kg | "
                  f"protein {digest.protein_g_per_kg.value} g/kg ({digest.protein_flag.value}) | "
                  f"fat {digest.fat_g_per_kg.value} g/kg")
    if digest.key_session_fuel_pct.value is not None:
        console.print(f"  key-session fueling: {digest.key_session_fuel_pct.value}% of hard/long days hit carb target; "
                      f"under-fuelled {digest.carb_underfuel_days_pct.value}% of all days")
    cmap = {"amber": "yellow", "red": "red"}
    low = [m for m in digest.micronutrient_flags if m.reliable and m.flag.value in cmap]
    if low:
        console.print("  micronutrient flags: " + ", ".join(
            f"[{cmap[m.flag.value]}]{m.nutrient} {m.pct_of_target:.0f}%[/]" for m in low))
    under = [m for m in digest.micronutrient_flags if not m.reliable]
    if under:
        detail = ", ".join(
            f"{m.nutrient} ({m.coverage_pct:.0f}% of items)" if m.coverage_pct is not None
            else f"{m.nutrient} (coverage unrecorded)" for m in under)
        console.print(f"  [dim]not assessable from the food log: {detail}[/]")
    notable = [c for c in digest.recovery_correlations if c.notable]
    for c in notable:
        shown = c.r_partial if c.r_partial is not None else c.r
        adj = f" adj. for {'+'.join(c.controlled_for)}" if c.controlled_for else ""
        effect = f", {c.effect_p10_p90:+g} {c.effect_unit}" if c.effect_p10_p90 is not None else ""
        console.print(f"  [dim]assoc[/] {c.driver} -> {c.outcome}: r={shown}{adj}{effect} (n={c.n})")
    if digest.recovery_correlations and not notable:
        console.print("  [dim]no nutrition factor moves recovery beyond day-to-day scatter[/]")
    console.print(f"[dim]saved {out} | sha256={digest.sha256()[:16]}[/]")


@app.command()
def validate(plan_file: Path = typer.Argument(..., help="Path to a plan JSON.")):
    """Run the deterministic guardrails. Exits non-zero on errors."""
    plan = TrainingPlan.model_validate(load_json(plan_file))
    rep = _validate_with_profile(plan)
    _print_validation(rep)
    raise typer.Exit(0 if rep.ok else 1)


@app.command()
def push(
    plan_file: Path = typer.Argument(..., help="Path to a plan JSON."),
    target: str = typer.Option("intervals", help="Push target (currently: intervals)."),
    dry_run: bool = typer.Option(False, help="Build + validate events but do not push."),
    set_active: bool = typer.Option(True, help="Record this as the active plan."),
):
    """Validate (gate) then push a plan to intervals.icu."""
    if target != "intervals":
        console.print(f"[red]Unsupported target '{target}'. Only 'intervals' is supported.[/]")
        raise typer.Exit(2)
    plan = TrainingPlan.model_validate(load_json(plan_file))

    # Stamp provenance inputs.
    plan.inputs = _stamp_inputs(plan)

    rep = _validate_with_profile(plan)
    _print_validation(rep)
    if not rep.ok:
        console.print("[red]Push blocked: fix validation errors first.[/]")
        raise typer.Exit(1)

    events = plan_to_events(plan)
    for w in garmin_sync_warnings(plan):
        console.print(f"[yellow]garmin-sync:[/] {w}")

    # Persist the (now stamped) plan and active pointer.
    save_json(plan_path(plan.plan_id), plan.model_dump(mode="json"))
    if set_active:
        save_json(active_plan_path(plan.athlete_id), plan.model_dump(mode="json"))

    if dry_run:
        out = get_config().plans_dir / f"{plan.plan_id}-events.json"
        save_json(out, {"events": events})
        console.print(f"[green]Dry run:[/] {len(events)} events written to {out} (not pushed)")
        return

    client = IntervalsClient()
    result = client.push_events(events, upsert=True)
    console.print(f"[green]Pushed[/] {len(events)} events to intervals.icu: {result}")


# --- helpers ---------------------------------------------------------------
def _validate_with_profile(plan: TrainingPlan):
    prof_data = load_json(profile_path(plan.athlete_id))
    tolerance = starting_ctl = None
    training_age = "intermediate"
    if prof_data:
        prof = AthleteProfile.model_validate(prof_data)
        tolerance = prof.weekly_volume_tolerance_hours
        training_age = prof.training_age.value
        if prof.load.combined.ctl.known:
            starting_ctl = prof.load.combined.ctl.value
    if plan.starting_ctl is not None:
        starting_ctl = plan.starting_ctl
    return validate_plan(plan, starting_ctl=starting_ctl,
                         weekly_hours_tolerance=tolerance, training_age=training_age)


def _stamp_inputs(plan: TrainingPlan) -> PlanInputs:
    digest_data = load_json(context_path(plan.athlete_id))
    sha = None
    if digest_data:
        from aithlete.models.context_digest import ContextDigest
        sha = ContextDigest.model_validate(digest_data).sha256()
    prof_data = load_json(profile_path(plan.athlete_id))
    prof_at = None
    if prof_data:
        prof_at = AthleteProfile.model_validate(prof_data).generated_at
    return PlanInputs(
        context_digest_sha256=sha,
        knowledge_version=knowledge.knowledge_version(),
        profile_generated_at=prof_at,
        rules_sha256=knowledge.file_sha256("rules.yaml"),
    )


def _print_validation(rep) -> None:
    status = "[bold green]PASS[/]" if rep.ok else "[bold red]FAIL[/]"
    console.print(f"Validation: {status}  (errors={len(rep.errors)}, warnings={len(rep.warnings)})")
    if rep.projected_race_tsb is not None:
        console.print(f"  projected race-day TSB: {rep.projected_race_tsb}")
    for i in rep.issues:
        c = "red" if i.severity.value == "error" else "yellow"
        console.print(f"  [{c}]{i.severity.value:7}[/] {i.code}: {i.message}")


if __name__ == "__main__":
    app()
