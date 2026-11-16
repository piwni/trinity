# Examples: real season specs, plans and reports

Complete, runnable examples of the spec → plan → report pipeline, so you can see
it end-to-end without connecting your own accounts. They use one athlete's
**real** intervals.icu history (shared with consent) as the data feed.

| Spec | Season | Plan | Report |
| --- | --- | --- | --- |
| `season-2026-malbork.yaml` | 2026: Syców 1/2 IM → Malbork full IM | `goal-2026-malbork.json` | `trinity-plan-2026.pdf` |
| `season-2028-hamburg.yaml` | 2027 race season → Malbork full (A) → IRONMAN Hamburg 2028 (A) | `goal-2028-hamburg.json` | `trinity-plan-2028.pdf` |
| `season-2028-b.yaml` | The same goals, with the volume remodelled from 5 Oct 2026 | `goal-2028-b.json` | `trinity-plan-2028-b.pdf` |

```
examples/
  season-*.yaml                 # the season specs: the only athlete/season-specific input
  data/                         # the data feed (point TRINITY_DATA_DIR at it)
    profiles/athlete.json       # profile built by `trinity profile`
    raw/intervals/...           # normalized activities + wellness (CSV partitions)
    manual_*.json               # hand-entered metrics and race results
    plans/goal-*.json           # generated TrainingPlans
    reports/goal-*.html         # generated season reports
    reports/trinity-plan-*.pdf  # the same reports rendered to PDF (showcase)
```

`reports/` also holds two derived documents for the 2028-b plan: a short
"this week to year end" view (`trinity-plan-2028-b-to-year-end.pdf`, from
`trinity build-horizon`) and a two-page brief for a coach
(`coach-brief-2028-b.pdf`, a markdown note through `trinity render-pdf`).

## What a spec drives

A spec holds everything specific to the athlete and season: goal races (with
target times and split tables), the weekly macro-structure, volume ceilings,
equipment notes, and the rationale prose. The engines are generic. To plan a
different season, copy a spec and edit it; `season-2026-malbork.yaml` is the
smallest one to start from.

The 2028 specs also declare the FTP and CdA values their prose is allowed to
state. `trinity build-plan` lints the spec first and refuses to build if the
text disagrees with those numbers or with the dated facts in
`src/trinity/knowledge/triathlon/facts.yaml`.

## Regenerate one yourself

The commands read the spec and the data dir:

```bash
export TRINITY_DATA_DIR="$PWD/examples/data"

# 1. Build the plan from the spec, then run it through the validator.
uv run trinity build-plan examples/season-2026-malbork.yaml
uv run trinity validate examples/data/plans/goal-2026-malbork.json

# 2. Build the HTML report (metrics computed from examples/data, copy from the spec).
uv run trinity build-report examples/season-2026-malbork.yaml

# 3. Render the PDF (needs Chrome or Chromium).
uv run trinity render-pdf examples/data/reports/goal-2026-malbork.html \
  examples/data/reports/trinity-plan-2026.pdf
```

The short view of the 2028-b plan was built with:

```bash
uv run trinity build-horizon examples/season-2028-b.yaml --start 2026-10-05 --label "Plan B"
```

`trinity validate` prints every error and warning plus the projected race-day
TSB, and exits non-zero while errors remain. The validator's rules keep getting
stricter, so a plan generated under an older rulebook may need rebuilding
before it passes.

## Using your own data

Don't point `TRINITY_DATA_DIR` at `examples/`. Leave it unset (it defaults to
`data/`, which is git-ignored). Then:

```bash
uv run trinity fetch --reset --since 2025-01-01   # pull your intervals.icu history
uv run trinity profile                            # build data/profiles/athlete.json
cp examples/season-2026-malbork.yaml data/my-season.yaml   # edit your races/targets
uv run trinity build-plan data/my-season.yaml
uv run trinity build-report data/my-season.yaml
```
