"""Consistency checks on a season spec's TEXT.

The plan validator reasons about load; it cannot see that one race card says
"IF 0.85 on the real 245 W FTP" while the brief says 255 W, that a card says
"Fuel 100 g/h" against a 95 g/h protocol ceiling, or that a race-week note refers
to a backup race nobody registered for. Those are statements in prose, so they are
linted from the spec:

- **single source of truth** -- every FTP and CdA a card states must be one the
  spec declares in ``meta.declared``; fuel rates may not exceed the declared
  ceiling;
- **calendar references** -- every race session must fall on a race in
  ``races:``, and prose may not claim a race that isn't there;
- **stale facts** -- prose is checked against the dated facts table in
  ``knowledge/triathlon/facts.yaml``;
- **split totals** -- a race card's legs must add up to the time it states, for
  both the stretch (``target``) and ``planning`` columns.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterator
from pathlib import Path

import yaml

from trinity.planning.validator import Issue, Severity

FACTS_PATH = Path(__file__).resolve().parents[3] / "knowledge" / "triathlon" / "facts.yaml"

# "on FTP 258", "FTP 265", "on the real 245 W FTP", "on 250 W FTP"
_FTP_USE = re.compile(
    r"\bFTP\s+(?P<a>\d{3})\b|\bon\s+(?:the\s+)?(?:real\s+|planning\s+|stretch\s+|tested\s+)?"
    r"(?P<b>\d{3})\s*W?\s+FTP\b", re.IGNORECASE)
_CDA_USE = re.compile(r"\bCdA\s+(?:of\s+)?(?:~\s*)?(?P<v>0\.\d{3})\b")
_FUEL = re.compile(r"(?P<lo>\d{2,3})(?:\s*-\s*(?P<hi>\d{2,3}))?\s*g/h\b")
_PHANTOM = re.compile(r"\bbackup\s+(?:IM|IRONMAN)\b[^.]{0,60}\b(?:registered|booked|entered)\b",
                      re.IGNORECASE)


def _texts(obj, path="") -> Iterator[tuple[str, str]]:
    """Every string in the parsed spec, with a readable location."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "declared":
                continue
            yield from _texts(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _texts(v, f"{path}[{i}]")
    elif isinstance(obj, str):
        yield path, obj


def _add(out: list[Issue], code: str, msg: str, sev: Severity = Severity.ERROR) -> None:
    out.append(Issue(code=code, severity=sev, message=msg, week_index=None))


def _load_facts(path: Path = FACTS_PATH) -> list[dict]:
    if not path.exists():
        return []
    return list((yaml.safe_load(path.read_text()) or {}).get("facts") or [])


def _clock_s(text, *, near: float | None = None) -> float | None:
    """Clock text to seconds. Three parts are h:mm:ss; two parts are m:ss for a
    leg, but a race total like '8:50' is h:mm while '35:00' (a 10 km) is m:ss --
    so for a total (``near`` = the legs' sum) the reading closer to it is used."""
    if text is None:
        return None
    try:
        nums = [float(p) for p in str(text).strip().split(":")]
    except ValueError:
        return None
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) != 2:
        return None
    ms, hm = nums[0] * 60 + nums[1], nums[0] * 3600 + nums[1] * 60
    if near is None:
        return ms
    return hm if abs(hm - near) < abs(ms - near) else ms


def _fmt(s: float) -> str:
    s = int(round(s))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def lint_spec(spec: dict, raw_text: str = "", facts_path: Path = FACTS_PATH,
              tolerance_s: float = 90.0) -> list[Issue]:
    out: list[Issue] = []
    meta = spec.get("meta") or {}
    declared = meta.get("declared") or {}
    ftps = {int(x) for x in declared.get("ftp_w") or []}
    cdas = {round(float(x), 3) for x in declared.get("cda") or []}
    ceiling = declared.get("fuel_ceiling_g_per_h")

    for where, text in _texts(spec):
        if ftps:
            for m in _FTP_USE.finditer(text):
                v = int(m.group("a") or m.group("b"))
                if v not in ftps:
                    _add(out, "lint_undeclared_ftp",
                         f"{where}: states FTP {v} W, not a declared value {sorted(ftps)}")
        if cdas:
            for m in _CDA_USE.finditer(text):
                v = round(float(m.group("v")), 3)
                if v not in cdas:
                    _add(out, "lint_undeclared_cda",
                         f"{where}: states CdA {v}, not a declared value {sorted(cdas)}")
        if ceiling:
            for m in _FUEL.finditer(text):
                hi = float(m.group("hi") or m.group("lo"))
                if hi > float(ceiling):
                    _add(out, "lint_fuel_over_ceiling",
                         f"{where}: {m.group(0)} exceeds the {ceiling} g/h protocol ceiling")
        if _PHANTOM.search(text) and not any(
                "backup" in str(r.get("name", "")).lower() for r in spec.get("races") or []):
            _add(out, "lint_phantom_race",
                 f"{where}: refers to a registered backup race that is not in the calendar")

    race_days = {dt.date.fromisoformat(str(r["date"])) for r in spec.get("races") or []}
    for w in spec.get("weeks") or []:
        monday = dt.date.fromisoformat(str(w["start"]))
        for s in (w.get("sessions") or []) + (w.get("extra_sessions") or []):
            if s.get("category") == "race":
                day = monday + dt.timedelta(days=int(s["day"]))
                if day not in race_days:
                    _add(out, "lint_race_not_in_calendar",
                         f"week {w['index']}: race session '{s.get('name')}' on {day} matches "
                         "no race in the calendar")

    for r in spec.get("races") or []:
        legs = r.get("splits") or []
        for col, key in (("target", "target_time"), ("planning", "planning_time")):
            parts = [_clock_s(leg.get(col)) for leg in legs]
            if not parts or any(x is None for x in parts):
                continue
            total = sum(parts)
            stated = _clock_s(r.get(key), near=total)
            if stated is None:
                continue
            if abs(total - stated) > tolerance_s:
                _add(out, "lint_split_total",
                     f"{r.get('name')}: {col} legs sum to {_fmt(total)} but {key} says "
                     f"{_fmt(stated)}")

    haystack = raw_text or "\n".join(t for _, t in _texts(spec))
    for fact in _load_facts(facts_path):
        for pat in fact.get("forbidden") or []:
            for m in re.finditer(pat, haystack):
                line = haystack[:m.start()].count("\n") + 1
                _add(out, "lint_stale_fact",
                     f"line {line}: '{m.group(0)}' contradicts fact '{fact['id']}' "
                     f"(verified {fact.get('verified')}): {fact['statement'].strip()[:90]}...")
    return out
