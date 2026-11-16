# Trinity

Open-source triathlete analysis and AI training planning.

> **Python does the math, the agent does the judgment, JSON is the contract.**

Trinity pulls your training, health and nutrition data, compresses it into small,
provenance-tagged digests, and lets an AI agent reason over them to build and
adjust a periodized triathlon plan. Every plan is checked by a **deterministic
validator** before anything is pushed to your calendar, and every training
adjustment is **gated by a readiness module** so the AI can't ramp you into the
ground.

> :warning: **Not medical advice.** See [`DISCLAIMER.md`](DISCLAIMER.md).

## Why this design

LLMs are good at judgment (interpreting trends, structuring a block, weighing
trade-offs) and bad at arithmetic and safety discipline. So Trinity splits the
work:

- **Python** fetches data, deduplicates it across sources, computes or fetches
  training-load and physiology metrics, runs readiness checks, and **validates**
  any plan against hard rules (ramp caps, recovery weeks, taper shape, race-day
  form target, intensity distribution, weekly-hours tolerance).
- **The agent** (run as agent skills) reads a bounded digest and authors or
  adjusts the plan as JSON.
- **JSON** is the only contract between them. The agent never sees raw streams
  and never computes a number that ends up in a plan.

```mermaid
flowchart LR
  Intervals["intervals.icu\n(activities, load, curves)"] --> Fetch
  OW["Open Wearables\n(recovery/sleep/HRV) - deferred"] --> Fetch
  Fetch["connectors\n(+ offline fixtures)"] --> Dedup["dedup / source-of-truth"]
  Dedup --> Raw["data/raw"]
  Fitatu["Fitatu exports\n(food log)"] --> Import["fitatu-import\n(+ USDA reference fill)"]
  Import --> Raw
  Raw --> Analysis["analysis (metrics)"]
  Raw --> Readiness["readiness (red flags)"]
  Raw --> Nutrition["nutrition (fueling math)"]
  Analysis --> Context["data/context\n(bounded digests)"]
  Readiness --> Context
  Nutrition --> Context
  Context --> Agent["agent (skills)"]
  Agent --> Plan["plan draft (JSON)"]
  Plan --> Validator["plan validator"]
  Validator -->|pass| Push["push to intervals.icu"]
  Validator -->|reject| Agent
  Readiness -.->|red flag triggers| Agent
```

## Data sources

| Concern | Source | Notes |
| --- | --- | --- |
| Activities, streams, training load (CTL/ATL/TSB), power/pace curves, eFTP | **intervals.icu** | Canonical. Fetched, not recomputed. |
| Recovery, sleep, HRV, RHR (Oura/Whoop) | **Open Wearables** | Deferred for v1; self-hosted unified API. See [`docs/open-wearables.md`](docs/open-wearables.md). |
| Food log | **Fitatu** meal-plan CSV exports | Imported with `trinity fitatu-import`. |
| Nutrients the food log leaves blank | **USDA FoodData Central** | Reference composition only. Every filled value traces back to a food record, and lookups are cached so later runs work offline. |
| Workout delivery to the watch | **intervals.icu → Garmin** | Via intervals' native Garmin Connect sync. Trinity never logs in to Garmin. |

The core pipeline runs **offline** against a synthetic athlete, so you can try it
with **no accounts or API keys** (`TRINITY_OFFLINE=true`, the default).

## Quickstart

```bash
# 1. Install (uv recommended)
uv sync --extra dev

# 2. Run the offline demo pipeline on the synthetic athlete
uv run trinity fetch
uv run trinity analyze
uv run trinity readiness
uv run trinity profile
uv run trinity context

# 3. (later) point at your real intervals.icu account
cp .env.example .env   # add INTERVALS_API_KEY, set TRINITY_OFFLINE=false
```

## Commands

| Command | What it does |
| --- | --- |
| `trinity fetch` | Pull data from the sources, dedup, and persist to the partitioned raw store. |
| `trinity analyze` | Print a metrics summary (load, ramp, intensity split, HRV/RHR baselines). |
| `trinity readiness` | Compute today's readiness flag and save it for the context digest. |
| `trinity profile` | Build the provenance-tagged athlete profile; keeps agent-written interpretive fields. |
| `trinity context` | Build the bounded context digest the agent reads. |
| `trinity durability` | Aerobic decoupling and output fade from activity streams (needs live intervals.icu). |
| `trinity aero` | Fit effective CdA from outdoor race rides (needs live intervals.icu). |
| `trinity fitatu-import` | Ingest Fitatu exports into the nutrition store. |
| `trinity food-gaps` | Rank food-log products whose nutrient data is blank, by logged mass. |
| `trinity food-map` | Resolve food concepts into product → USDA mappings, pinned once settled. |
| `trinity nutrition` | Build the bounded nutrition digest (fueling and recovery). |
| `trinity validate PLAN.json` | Run the deterministic guardrails. Exits non-zero on errors. |
| `trinity push PLAN.json` | Validate, then upsert the plan's events to intervals.icu. `--dry-run` writes them to a file instead. |

Run `uv run trinity <command> --help` for options.

## Configuration

Everything is set through environment variables or `.env`
(template: [`.env.example`](.env.example)).

| Variable | Purpose |
| --- | --- |
| `TRINITY_OFFLINE` | `true` (default) serves the synthetic athlete and never touches the network. |
| `TRINITY_DATA_DIR` | Where data is read and written. Defaults to `data/` in the repo, which is git-ignored. |
| `INTERVALS_API_KEY`, `INTERVALS_ATHLETE_ID` | intervals.icu access. |
| `OPEN_WEARABLES_BASE_URL`, `OPEN_WEARABLES_API_KEY`, `OPEN_WEARABLES_USER_ID` | Open Wearables access (optional). |
| `FDC_API_KEY` | USDA FoodData Central, only needed the first time a food is looked up. |
| `OPENAI_API_KEY` | Only for `scripts/speak_openai.py` (text-to-speech). |

## Agent skills

Four skills in [`.cursor/skills/`](.cursor/skills/) drive the CLI and read the
digests. The agent supplies judgment while Python supplies and checks the numbers.

- `triathlete-profile` — analyse history and refine `data/profiles/athlete.json`
- `training-plan` — author a periodized plan, validated before push
- `adjust-plan` — revise the active plan, gated by readiness
- `nutritionist` — judge fueling against training load and recovery

## Season plans and reports

A whole season can be described in one YAML spec and built without the agent.
[`examples/`](examples/README.md) has real specs with their plans and reports.

| Script | What it does |
| --- | --- |
| `scripts/build_plan.py SPEC.yaml` | Lint the spec, then generate the `TrainingPlan` JSON from it. |
| `scripts/build_report.py SPEC.yaml` | Build the HTML season report from the spec, plan, profile and history. |
| `scripts/build_horizon.py` | Short "this week to year end" view of one plan (paths are hardcoded). |
| `scripts/md_to_pdf.py NOTE.md` | Render a markdown note to a print-ready A4 PDF via headless Chrome. |
| `scripts/model_brick_options.py` | Exploratory: compare brick-structure variants against the validator. |
| `scripts/speak_openai.py` | Render text to MP3. |

The spec linter fails the build when the prose disagrees with the numbers
(declared FTP/CdA values, fuel ceiling, split totals, calendar references) or
with the dated facts in [`facts.yaml`](knowledge/triathlon/facts.yaml).

## How the math works

Training formulas are documented and cited in
[`knowledge/triathlon/notes.md`](knowledge/triathlon/notes.md), nutrition in
[`knowledge/nutrition/notes.md`](knowledge/nutrition/notes.md). In short:

- **Per-sport stress scores** normalize to `100 = 1 h at threshold`: power TSS
  (Coggan), run rTSS / hrTSS, swim sTSS (CSS-based), so combined load is additive.
- **Fitness/Fatigue/Form (CTL/ATL/TSB)** via the Banister impulse-response model
  (τ = 42 / 7). Fetched from intervals.icu when available; recomputed to validate.
- **Physiology anchors**: FTP/eFTP (Coggan), CP/W′ (Monod–Scherrer, Skiba),
  run critical speed / Riegel extrapolation, swim CSS, LTHR; VO2max and lactate
  threshold are always flagged `estimated` unless from a lab test.
- **Readiness**: HRV vs 60-day baseline ± SWC (Plews/Hopkins), RHR elevation,
  sleep debt, ACWR (Gabbett), monotony/strain (Foster), neck-check illness rule.
- **Durability**: aerobic decoupling (Pa:Hr for runs, Pw:Hr for rides) and output
  fade from raw streams, dropped when they disagree with the device's moving pace.
- **Aero**: effective CdA from race files, corrected for wind height and course
  bearing, fitted both by energy balance and against the actual race time.
- **Nutrition**: energy availability, carbohydrate periodization, protein, and
  micronutrient flags that say `unknown` when the food log cannot support a verdict.

The rulebooks are plain, tunable YAML that the Python guardrails enforce:
[`rules.yaml`](knowledge/triathlon/rules.yaml) (plan shape),
[`readiness.yaml`](knowledge/triathlon/readiness.yaml) (daily go/no-go),
[`zones.yaml`](knowledge/triathlon/zones.yaml) (intensity zones) and
[`nutrition/rules.yaml`](knowledge/nutrition/rules.yaml) (fueling thresholds).

## Testing

```bash
uv run pytest
uv run ruff check src tests scripts
```

Tests are hermetic and offline. The context-digest golden test
(`tests/golden/context_digest.json`) locks the exact summary the agent reads.

## Project layout

```
src/trinity/
  cli.py             # the `trinity` command
  config.py          # env + paths
  knowledge.py       # rulebook loaders + content hashes
  connectors/        # intervals, openwearables, fitatu, fooddata (USDA), food
                     # concepts/mappings, dedup, fetch orchestration, synthetic athlete
  analysis/          # metrics, readiness, races, durability, aero, nutrition,
                     # profile builder, context digest
  planning/          # validator (guardrails), workout builder, prescription
                     # parsing, season-spec lint
  models/            # pydantic schemas (schema_version + provenance)
  storage/           # atomic JSON / CSV io, path conventions
scripts/             # season-spec engines and report rendering
knowledge/           # triathlon/ and nutrition/ rulebooks + cited notes
examples/            # real season specs with data, plans and reports
data/                # your data; git-ignored apart from fixtures/
docs/                # integration guides
tests/               # offline tests, incl. a golden-file context digest
.cursor/skills/      # the four agent skills
```

## Status & roadmap

Early v1: single athlete, runs locally, offline-capable, pushes to intervals.icu.

- [x] Offline pipeline on a synthetic athlete (no accounts needed)
- [x] Provenance-tagged profile, bounded context digest, readiness gate
- [x] Deterministic plan validator + idempotent intervals.icu workout push
- [x] Nutrition analysis from Fitatu exports, with reference-data fill for blanks
- [x] Durability and aero analysis from activity streams
- [x] Spec-driven season plans and reports
- [ ] Live intervals.icu recording fixtures (vcr cassettes)
- [ ] Open Wearables recovery wired into readiness by default
- [ ] Pushing computed wellness (readiness / DFA-a1 / decoupling) back to intervals
- [ ] Multi-athlete support (coaching)
- [ ] Hosted deployment (scheduled runs, data outside the laptop)

## Formerly Aithlete

Trinity was called Aithlete until November 2026. Calendar events keep the
`aithlete-` prefix in their `external_id` so that events already pushed to
intervals.icu are updated in place rather than duplicated.

## Contributing

Issues and PRs welcome. Please keep the core contract intact: math and safety
live in Python (with tests), judgment lives in the agent, and JSON is the
boundary. New physiology formulas must cite a source in `knowledge/`.

## License

MIT — see [`LICENSE`](LICENSE).
