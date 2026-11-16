"""Read intensity and load out of a session's prescription text.

A session's label and its content can drift apart. The plan this was written for
had a 5 h "Long ride (Z2)" carrying 3x40' at race power, counted 100% towards the
easy share and billed at the easy-riding TSS rate. So labels, intensity
distribution and TSS all come from what a session actually prescribes.

Only prescriptions with an explicit target are counted as work: ``3x40' @ 200 W``,
``4x8' @ 3:30/km``, ``last 40' @ 4:16/km``, ``40' at MP``. Strides, drills and
unpaced warm-ups are not. Swim and strength text is not parsed (swim load uses
the per-hour rate; pool sets are written in metres, not minutes).

Everything is resolved against the plan's anchors (``ftp_w``,
``run_threshold_s_per_km``), so one number drives every card.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from trinity.models.common import Sport
from trinity.models.plan import IntensityClass

# TSS per hour by (sport, intensity) for text with no parseable targets.
# Calibrated to typical AG triathlete data (easy = Z1-2, moderate =
# tempo/threshold, hard = VO2/anaerobic).
RATE: dict[tuple[Sport, IntensityClass], float] = {
    (Sport.SWIM, IntensityClass.EASY): 32, (Sport.SWIM, IntensityClass.MODERATE): 48,
    (Sport.SWIM, IntensityClass.HARD): 62,
    (Sport.BIKE, IntensityClass.EASY): 46, (Sport.BIKE, IntensityClass.MODERATE): 70,
    (Sport.BIKE, IntensityClass.HARD): 88,
    (Sport.RUN, IntensityClass.EASY): 55, (Sport.RUN, IntensityClass.MODERATE): 75,
    (Sport.RUN, IntensityClass.HARD): 95,
    (Sport.OTHER, IntensityClass.EASY): 22, (Sport.OTHER, IntensityClass.MODERATE): 30,
    (Sport.OTHER, IntensityClass.HARD): 35,
}

# A session whose prescribed work exceeds this share of its duration is not easy.
QUALITY_SHARE_FOR_UPGRADE = 0.10

# IF implied by named targets, per sport.
_KEYWORD_IF: list[tuple[str, dict[Sport, float]]] = [
    (r"IM[- ]pace|marathon pace|\bMP\b", {Sport.RUN: 0.84, Sport.BIKE: 0.76}),
    (r"IM power", {Sport.BIKE: 0.76}),
    (r"half[- ]pace|70\.3 pace", {Sport.RUN: 0.90, Sport.BIKE: 0.83}),
    (r"race[- ]pace|race[- ]power", {Sport.RUN: 0.90, Sport.BIKE: 0.83}),
    (r"5 ?km effort", {Sport.RUN: 1.05}),
    (r"sweet ?spot", {Sport.BIKE: 0.88}),
    (r"threshold", {Sport.RUN: 0.98, Sport.BIKE: 0.98}),
]
_KW = "|".join(f"(?:{p})" for p, _ in _KEYWORD_IF)

_SEP = r"\s*(?:@|\bat\b)\s*"
_TGT = (
    r"(?:(?P<w1>\d{2,3})(?:\s*-\s*(?P<w2>\d{2,3}))?\s*W\b"
    r"|(?P<p1>\d:\d\d)(?:\s*-\s*(?P<p2>\d:\d\d))?\s*/\s*km"
    r"|(?P<f1>\d{2,3})(?:\s*-\s*(?P<f2>\d{2,3}))?\s*%\s*(?:of\s+)?FTP"
    rf"|(?P<kw>{_KW}))"
)
_REP = re.compile(
    r"(?P<reps>\d+)(?:\s*-\s*(?P<reps2>\d+))?\s*x\s*(?P<n>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>'|′|s\b|km\b)(?P<gap>[^@.;]{0,40}?)" + _SEP + _TGT,
    re.IGNORECASE,
)
_CONT = re.compile(
    r"(?<![x\d.])(?P<n>\d+)\s*(?:'|′)\s*(?:continuous\s+)?" + _SEP + _TGT,
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Segment:
    """One block of prescribed work."""

    minutes: float
    intensity_factor: float


def _pace_s(txt: str) -> float:
    m, s = txt.split(":")
    return int(m) * 60 + int(s)


def _target_if(m: re.Match, sport: Sport, anchors: dict) -> tuple[float | None, float | None]:
    """(IF, pace_s_per_km) implied by a matched target."""
    ftp = anchors.get("ftp_w")
    thr = anchors.get("run_threshold_s_per_km")
    if m.group("w1"):
        if not ftp:
            return None, None
        lo = float(m.group("w1"))
        hi = float(m.group("w2") or lo)
        return (lo + hi) / 2 / ftp, None
    if m.group("p1"):
        lo = _pace_s(m.group("p1"))
        hi = _pace_s(m.group("p2") or m.group("p1"))
        pace = (lo + hi) / 2
        if sport is Sport.RUN and thr:
            return thr / pace, pace
        return None, pace
    if m.group("f1"):
        lo = float(m.group("f1"))
        hi = float(m.group("f2") or lo)
        return (lo + hi) / 200, None
    kw = m.group("kw")
    if kw:
        for pat, table in _KEYWORD_IF:
            if re.fullmatch(pat, kw, flags=re.IGNORECASE) and sport in table:
                iff = table[sport]
                return iff, (thr / iff if (thr and sport is Sport.RUN) else None)
    return None, None


def _parse_alternative(text: str, sport: Sport, anchors: dict) -> list[Segment]:
    segs: list[Segment] = []
    taken: list[tuple[int, int]] = []
    thr = anchors.get("run_threshold_s_per_km")
    for m in _REP.finditer(text):
        iff, pace = _target_if(m, sport, anchors)
        if iff is None:
            continue
        reps = float(m.group("reps2") or m.group("reps"))
        n = float(m.group("n"))
        unit = m.group("unit").lower()
        if unit in ("'", "′"):
            minutes = reps * n
        elif unit == "s":
            minutes = reps * n / 60.0
        else:  # km
            pace = pace or (thr / iff if thr else 300.0)
            minutes = reps * n * pace / 60.0
        segs.append(Segment(minutes, iff))
        taken.append(m.span())
    for m in _CONT.finditer(text):
        if any(a <= m.start() < b for a, b in taken):
            continue
        iff, _ = _target_if(m, sport, anchors)
        if iff is None:
            continue
        segs.append(Segment(float(m.group("n")), iff))
    return segs


def quality_segments(
    description: str | None, *, sport: Sport, anchors: dict, session_minutes: float
) -> list[Segment]:
    """Blocks of prescribed work in a session, with the IF of each.

    ``A OR B`` counts one alternative (the longer), never both. The total never
    exceeds the session's own duration.
    """
    if not description or sport not in (Sport.BIKE, Sport.RUN):
        return []
    alts = re.split(r"\s+OR\s+", description)
    best: list[Segment] = []
    for alt in alts:
        segs = _parse_alternative(alt, sport, anchors)
        if sum(s.minutes for s in segs) > sum(s.minutes for s in best):
            best = segs
    total = sum(s.minutes for s in best)
    if session_minutes > 0 and total > session_minutes:
        k = session_minutes / total
        best = [Segment(s.minutes * k, s.intensity_factor) for s in best]
    return best


_ORDER = {IntensityClass.EASY: 0, IntensityClass.MODERATE: 1, IntensityClass.HARD: 2}


def content_class(segments: list[Segment], session_minutes: float) -> IntensityClass | None:
    """Intensity implied by content, or None when the work is too small to matter."""
    work = sum(s.minutes for s in segments)
    if session_minutes <= 0 or work / session_minutes <= QUALITY_SHARE_FOR_UPGRADE:
        return None
    w_if = sum(s.minutes * s.intensity_factor for s in segments) / work
    return IntensityClass.HARD if w_if >= 1.0 else IntensityClass.MODERATE


def derive_intensity(
    label: IntensityClass, description: str | None, sport: Sport, *,
    anchors: dict, session_minutes: float,
) -> IntensityClass:
    """The label, upgraded if the content is harder. Never downgraded: an author
    marking something hard has a reason the text may not spell out."""
    segs = quality_segments(description, sport=sport, anchors=anchors,
                            session_minutes=session_minutes)
    found = content_class(segs, session_minutes)
    if found is None:
        return label
    return found if _ORDER[found] > _ORDER[label] else label


def content_tss(
    sport: Sport, label: IntensityClass, description: str | None,
    session_minutes: float, anchors: dict,
) -> float:
    """TSS from prescription content: each work block at IF^2 x 100 per hour, the
    remainder at the easy rate. Falls back to the label's hourly rate when the
    text has no parseable targets (and always for swim / strength)."""
    if sport not in (Sport.BIKE, Sport.RUN):
        return round(RATE[(sport, label)] * session_minutes / 60.0, 1)
    segs = quality_segments(description, sport=sport, anchors=anchors,
                            session_minutes=session_minutes)
    if not segs:
        return round(RATE[(sport, label)] * session_minutes / 60.0, 1)
    work = sum(s.minutes for s in segs)
    tss = sum(s.minutes / 60.0 * s.intensity_factor ** 2 * 100.0 for s in segs)
    tss += max(session_minutes - work, 0.0) / 60.0 * RATE[(sport, IntensityClass.EASY)]
    return round(tss, 1)


def intensity_minutes(
    sport: Sport, label: IntensityClass, description: str | None,
    session_minutes: float, anchors: dict,
) -> dict[IntensityClass, float]:
    """Split a session's minutes into easy / moderate / hard by content.

    Work blocks are classed by IF (>=0.95 hard, >=0.75 moderate, else easy); the
    remainder is easy. Sessions with no parseable targets fall back to their label.
    """
    out = {c: 0.0 for c in IntensityClass}
    segs = quality_segments(description, sport=sport, anchors=anchors,
                            session_minutes=session_minutes)
    if not segs:
        out[label] += session_minutes
        return out
    for s in segs:
        if s.intensity_factor >= 0.95:
            out[IntensityClass.HARD] += s.minutes
        elif s.intensity_factor >= 0.75:
            out[IntensityClass.MODERATE] += s.minutes
        else:
            out[IntensityClass.EASY] += s.minutes
    out[IntensityClass.EASY] += max(session_minutes - sum(s.minutes for s in segs), 0.0)
    return out


def marathon_pace_minutes(
    sport: Sport, description: str | None, session_minutes: float, anchors: dict
) -> float:
    """Minutes of running at marathon / Ironman pace (IF 0.78-0.93)."""
    if sport is not Sport.RUN:
        return 0.0
    segs = quality_segments(description, sport=sport, anchors=anchors,
                            session_minutes=session_minutes)
    return sum(s.minutes for s in segs if 0.78 <= s.intensity_factor < 0.93)
