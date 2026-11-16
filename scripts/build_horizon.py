"""Short plan PDF: the current week through the end of the year.

One page-ish block per week: the session table, then hours by sport and
estimated run kilometres. Run km is not stored on the plan (sessions are
written in minutes), so easy running is counted at 4:55/km, long runs stop
at the written cap, and the 8 Nov race is 10 km.

    uv run python scripts/build_horizon.py
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_report as br  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "data" / "plans" / "goal-2028-b.json"
SPEC = ROOT / "examples" / "season-2028-b.yaml"
OUT_HTML = ROOT / "data" / "reports" / "goal-2028-b-to-year-end.html"

# Current week starts Monday 5 Oct 2026. Include every week that still has
# a day in 2026, so the week of 28 Dec (which ends 3 Jan) is in.
START = dt.date(2026, 10, 5)
YEAR_END = dt.date(2026, 12, 31)
DONE_BEFORE = dt.date(2026, 11, 16)  # through Sunday 15 Nov, the day before this week

EASY_MIN_PER_KM = 4 + 55 / 60  # recent easy pace
BRICK_MIN_PER_KM = 4 + 40 / 60
JOG_MIN_PER_KM = 5 + 20 / 60
VO2_MIN_PER_KM = 3 + 25 / 60
DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def esc(x) -> str:
    return html.escape("" if x is None else str(x))


def hours(seconds: float) -> float:
    return seconds / 3600


def run_km(session: dict) -> float:
    mins = session["planned_duration_s"] / 60
    # Wednesday's 4x8 was measured at 15.1 km, longer than the 55 min on the plan.
    if session.get("date") == "2026-10-07" and session.get("sport") == "run":
        return 15.1
    name = (session.get("name") or "").lower()
    desc = session.get("description") or ""
    if session.get("category") == "RACE" or name.startswith("race"):
        return 10.0
    cap = None
    m = re.search(r"Cap (\d+) km", desc)
    if m:
        cap = int(m.group(1))
    if "vo2" in name:
        # 20% easy bookends, 40% at 3:25, 40% jog.
        km = 0.2 * mins / EASY_MIN_PER_KM + 0.4 * mins / VO2_MIN_PER_KM + 0.4 * mins / JOG_MIN_PER_KM
    elif "key" in name or "opener" in name:
        km = 15.06 * (mins / 62)  # Wednesday's 4x8 was 15.1 km in 62 min
    elif "brick" in name:
        km = mins / BRICK_MIN_PER_KM
    else:
        km = mins / EASY_MIN_PER_KM
    if cap is not None:
        km = min(km, cap)
    return km


def summarize(sessions: list[dict]) -> dict:
    out = {"total_s": 0, "swim_s": 0, "bike_s": 0, "run_s": 0, "strength_s": 0,
           "other_s": 0, "run_km": 0.0,
           "n_swim": 0, "n_bike": 0, "n_run": 0, "n_strength": 0}
    for s in sessions:
        if s.get("category") == "NOTE":
            continue
        dur = s["planned_duration_s"]
        out["total_s"] += dur
        sport = s.get("sport")
        name = (s.get("name") or "").lower()
        if sport == "swim":
            out["swim_s"] += dur
            out["n_swim"] += 1
        elif sport == "bike":
            out["bike_s"] += dur
            out["n_bike"] += 1
        elif sport == "run":
            out["run_s"] += dur
            out["run_km"] += run_km(s)
            out["n_run"] += 1
        elif sport == "other" and "strength" in name:
            out["strength_s"] += dur
            out["n_strength"] += 1
        else:
            out["other_s"] += dur
    return out


def fmt_h(seconds: float) -> str:
    return f"{seconds / 3600:.1f} h"


def main() -> None:
    plan = json.loads(PLAN.read_text())
    spec = yaml.safe_load(SPEC.read_text())
    focus = {int(w["index"]): w.get("focus", "") for w in spec["weeks"]}

    weeks = []
    for w in plan["weeks"]:
        start = dt.date.fromisoformat(w["start_date"])
        end = start + dt.timedelta(days=6)
        if end < START or start > YEAR_END:
            continue
        weeks.append(w)

    parts: list[str] = []
    A = parts.append
    A("<!doctype html><html><head><meta charset='utf-8'>")
    A(f"<title>Plan B · {START.strftime('%-d %b')} – end of 2026</title>")
    A(f"<style>{CSS}</style></head><body>")
    A("<h1>Plan B · through the end of 2026</h1>")
    A("<p class='sub'>5 October – 3 January. The last block is the week of 28 December, "
      "which finishes on 3 January. Sessions through Sunday 15 November are marked done.</p>")
    A("<p class='note'>Run kilometres are estimated. Easy running is counted at 4:55/km, "
      "which is the recent easy pace. A long run stops at its written cap. Hours are a ceiling. "
      "The 8 November 10 km is off (Web Summit), so the 10 km test moves to spring 2027.</p>")

    grand = summarize([s for w in weeks for s in w["sessions"]])
    A("<div class='kpis'>")
    for label, val in (
        ("Total", fmt_h(grand["total_s"])),
        ("Swim", fmt_h(grand["swim_s"])),
        ("Bike", fmt_h(grand["bike_s"])),
        ("Run", fmt_h(grand["run_s"])),
        ("Run km", f"{grand['run_km']:.0f} km"),
        ("Strength", fmt_h(grand["strength_s"])),
    ):
        A(f"<div class='kpi'><div class='v'>{val}</div><div class='l'>{label}</div></div>")
    A("</div>")

    A("<h2>Week by week</h2>")
    A("<table class='index'><tr><th>Week</th><th>Start</th><th class='r'>Total</th>"
      "<th class='r'>Swim</th><th class='r'>Bike</th><th class='r'>Run</th>"
      "<th class='r'>Run km</th><th class='r'>Strength</th></tr>")
    for w in weeks:
        sm = summarize(w["sessions"])
        A(f"<tr><td>{w['week_index']}</td><td class='mono'>{esc(w['start_date'])}</td>"
          f"<td class='r mono'>{fmt_h(sm['total_s'])}</td>"
          f"<td class='r mono'>{fmt_h(sm['swim_s'])}</td>"
          f"<td class='r mono'>{fmt_h(sm['bike_s'])}</td>"
          f"<td class='r mono'>{fmt_h(sm['run_s'])}</td>"
          f"<td class='r mono'>{sm['run_km']:.0f}</td>"
          f"<td class='r mono'>{fmt_h(sm['strength_s'])}</td></tr>")
    A("</table>")

    for w in weeks:
        start = dt.date.fromisoformat(w["start_date"])
        end = start + dt.timedelta(days=6)
        sm = summarize(w["sessions"])
        rec = " · recovery" if w.get("is_recovery") else ""
        A("<section class='week'>")
        A(f"<h2>Week {w['week_index']} · {start.strftime('%-d %b')} – {end.strftime('%-d %b')}"
          f"<span class='phase'>{esc(w['phase'])}{rec}</span></h2>")
        foc = focus.get(w["week_index"], "")
        if foc:
            A(f"<p class='focus'>{esc(foc)}</p>")
        A("<table><tr><th class='day'>Day</th><th>Session</th><th>Int.</th>"
          "<th class='r'>Dur</th><th>Prescription</th></tr>")
        by_date: dict[str, list] = {}
        for s in w["sessions"]:
            by_date.setdefault(s["date"], []).append(s)
        for i in range(7):
            day = start + dt.timedelta(days=i)
            rows = by_date.get(day.isoformat(), [])
            if not rows:
                dn = f"{DOW[day.weekday()]} {day.strftime('%-d.%m')}"
                A(f"<tr><td class='day mono'>{esc(dn)}</td><td>Rest</td>"
                  "<td></td><td></td><td class='small'></td></tr>")
                continue
            for s in rows:
                dn = f"{DOW[day.weekday()]} {day.strftime('%-d.%m')}"
                tag = ("<span class='tag t-race'>race</span>" if s.get("category") == "RACE"
                       else f"<span class='tag t-{esc(s['intensity_class'])}'>{esc(s['intensity_class'])}</span>")
                brk = " <span class='tag t-moderate'>brick</span>" if s.get("is_brick") else ""
                done = " <span class='tag t-done'>done</span>" if day < DONE_BEFORE else ""
                A(f"<tr><td class='day mono'>{esc(dn)}</td>"
                  f"<td>{br.sport_icon(s)}{esc(s['name'])}{brk}{done}</td>"
                  f"<td>{tag}</td><td class='r mono'>{br.hms(s['planned_duration_s'])}</td>"
                  f"<td class='small'>{esc(s.get('description') or '')}</td></tr>")
        A("</table>")
        bits = [
            f"<b>{fmt_h(sm['total_s'])}</b> total",
            f"swim <b>{fmt_h(sm['swim_s'])}</b> ({sm['n_swim']})",
            f"bike <b>{fmt_h(sm['bike_s'])}</b> ({sm['n_bike']})",
            f"run <b>{fmt_h(sm['run_s'])}</b> · <b>{sm['run_km']:.0f} km</b> ({sm['n_run']})",
            f"strength <b>{fmt_h(sm['strength_s'])}</b>",
        ]
        if sm["other_s"] >= 600:
            bits.append(f"other <b>{fmt_h(sm['other_s'])}</b>")
        A(f"<p class='sum'>{' · '.join(bits)}</p>")
        A("</section>")

    A("</body></html>")
    OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUT_HTML.write_text("\n".join(parts), encoding="utf-8")
    print(f"Wrote {OUT_HTML} ({len(weeks)} weeks, {grand['run_km']:.0f} km run)")


CSS = """
:root { --ink:#14181f; --mut:#5b6675; --line:#d7dde6; --accent:#0b66c3; }
* { box-sizing: border-box; }
body { font: 11px/1.45 -apple-system, "Helvetica Neue", Arial, sans-serif; color: var(--ink); margin: 0; }
h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: -0.3px; }
h2 { font-size: 15px; margin: 0 0 4px; }
h2 .phase { font-weight: 500; color: var(--mut); font-size: 12px; margin-left: 8px; }
p { margin: 4px 0; }
.sub { color: var(--mut); margin-bottom: 8px; }
.note { background: #f3f5f8; border-left: 3px solid var(--accent); padding: 6px 10px; margin: 8px 0 12px; }
.kpis { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0 14px; }
.kpi { border: 1px solid var(--line); border-radius: 7px; padding: 6px 10px; min-width: 88px; }
.kpi .v { font-size: 16px; font-weight: 700; }
.kpi .l { color: var(--mut); font-size: 9px; text-transform: uppercase; letter-spacing: .4px; }
table { border-collapse: collapse; width: 100%; margin: 6px 0; font-size: 10.2px; }
th, td { border: 1px solid var(--line); padding: 3px 6px; text-align: left; vertical-align: top; }
th { background: #eef2f7; font-size: 9px; text-transform: uppercase; letter-spacing: .3px; }
td.r, th.r { text-align: right; }
.mono { font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 10px; }
.small { font-size: 9.4px; color: var(--mut); }
.day { color: var(--mut); width: 72px; }
.focus { color: var(--mut); font-size: 10px; margin: 0 0 4px; }
.sum { background: #f6f8fb; border: 1px solid var(--line); border-radius: 6px; padding: 6px 10px; margin: 6px 0 0; }
.tag { display: inline-block; padding: 1px 6px; border-radius: 4px; font-size: 9px; font-weight: 700; text-transform: uppercase; }
.t-easy { background: #e3f0ff; color: #0b66c3; }
.t-moderate { background: #fff1d6; color: #9a6b00; }
.t-hard { background: #ffe0e3; color: #b00020; }
.t-race { background: #122; color: #fff; }
.t-done { background: #e7f6ec; color: #127a3f; }
.ico { display: inline-block; width: 12px; height: 12px; margin-right: 3px; vertical-align: -2px; }
.ico svg { width: 12px; height: 12px; display: block; }
.i-swim { color: #0b66c3; } .i-bike { color: #c2410c; }
.i-run { color: #127a3f; } .i-strength { color: #6d28d9; }
.week { break-before: page; }
.week table, .sum { break-inside: avoid; }
@page { size: A4; margin: 12mm 12mm 14mm; }
"""


if __name__ == "__main__":
    main()
