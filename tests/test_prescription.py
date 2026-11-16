"""Reading intensity out of a session's prescription text.

A session's label ("easy", "Z2") and its content ("3x40' @ IM power") drifted
apart in the plan: a 5 h "Long ride (Z2)" carried 2 h at race power and counted
100% towards the easy share. Load and intensity must come from what the session
actually prescribes.
"""

from __future__ import annotations

import pytest

from trinity.models.common import Sport
from trinity.models.plan import IntensityClass
from trinity.planning.prescription import (
    content_tss,
    derive_intensity,
    quality_segments,
)

ANCHORS = {"ftp_w": 250.0, "run_threshold_s_per_km": 215.0}


def mins(desc, sport=Sport.BIKE, total=120):
    return sum(s.minutes for s in quality_segments(desc, sport=sport, anchors=ANCHORS,
                                                   session_minutes=total))


def test_power_intervals_read_minutes_and_if():
    segs = quality_segments("3x40' @ 198-206 W (rest Z2)", sport=Sport.BIKE,
                            anchors=ANCHORS, session_minutes=300)
    assert sum(s.minutes for s in segs) == pytest.approx(120)
    assert segs[0].intensity_factor == pytest.approx(202 / 250, abs=0.005)


def test_range_of_reps_takes_the_upper_bound():
    assert mins("2-3x20' @ 217-230 W (true sweet spot)") == pytest.approx(60)


def test_run_pace_intervals_convert_to_if_against_threshold():
    segs = quality_segments("Threshold 4x8' @ 3:30/km off 2' jog", sport=Sport.RUN,
                            anchors=ANCHORS, session_minutes=55)
    assert sum(s.minutes for s in segs) == pytest.approx(32)
    assert segs[0].intensity_factor == pytest.approx(215 / 210, abs=0.005)


def test_km_reps_convert_to_minutes_via_their_pace():
    # 3 x 3 km at 4:16/km = 38.4 min
    assert mins("IM-pace 3x3km @ 4:14-4:18/km off 3' jog", sport=Sport.RUN, total=60) \
        == pytest.approx(38.4, abs=0.2)


def test_continuous_block_and_finish_segments():
    assert mins("Threshold 30' CONTINUOUS @ 3:30/km.", Sport.RUN, 55) == pytest.approx(30)
    assert mins("Aerobic long run; last 40' @ 4:16/km.", Sport.RUN, 120) == pytest.approx(40)


def test_keyword_targets_are_understood():
    """The review's own example: a 'Long run (Z2)' with 40' at MP."""
    segs = quality_segments("Long run (Z2) with 40' at MP", sport=Sport.RUN,
                            anchors=ANCHORS, session_minutes=120)
    assert sum(s.minutes for s in segs) == pytest.approx(40)
    assert 0.78 <= segs[0].intensity_factor <= 0.92


def test_warmup_minutes_are_not_attached_to_a_later_target():
    """'20' Z2 + 5x2' @ 3:30' is 10 min of work, not 20 min at threshold."""
    assert mins("20' Z2 + 5x2' @ 3:30-3:35/km off 90s jog", Sport.RUN, 45) == pytest.approx(10)


def test_alternatives_count_once():
    """'A OR B' prescribes one of them. Count the bigger, never both."""
    total = mins("VO2 5x4' @ 275-293 W (equal rest) OR threshold 3x12' @ 242-255 W.")
    assert total == pytest.approx(36)


def test_unprescribed_text_has_no_quality():
    assert mins("Aerobic Z2 throughout. Scenic, conversational.") == 0
    assert mins("Z2 + 6x20s strides.", Sport.RUN, 40) == 0, "strides without a target are not work"


def test_swim_text_is_not_parsed_as_run_or_bike_work():
    assert mins("8x100 @ CSS, long rest.", Sport.SWIM, 40) == 0


def test_segments_never_exceed_the_session():
    assert mins("3x40' @ 200 W", Sport.BIKE, total=90) <= 90


def test_easy_label_is_upgraded_when_content_is_not_easy():
    """The exact bug: a Z2-labelled long ride with 2 h at race power."""
    got = derive_intensity(IntensityClass.EASY, "3x40' @ 198-206 W", Sport.BIKE,
                           anchors=ANCHORS, session_minutes=300)
    assert got is IntensityClass.MODERATE


def test_labels_are_never_downgraded():
    got = derive_intensity(IntensityClass.HARD, "Aerobic spin", Sport.BIKE,
                           anchors=ANCHORS, session_minutes=60)
    assert got is IntensityClass.HARD


def test_short_quality_below_ten_percent_keeps_an_easy_label():
    got = derive_intensity(IntensityClass.EASY, "Z2, finish with 5' @ 3:40/km", Sport.RUN,
                           anchors=ANCHORS, session_minutes=90)
    assert got is IntensityClass.EASY


def test_content_tss_follows_the_work_not_the_hours():
    """Same 5 h, very different load: pure Z2 versus 2 h at race power."""
    z2 = content_tss(Sport.BIKE, IntensityClass.EASY, "Z2 throughout", 300, ANCHORS)
    im = content_tss(Sport.BIKE, IntensityClass.MODERATE, "3x40' @ 198-206 W", 300, ANCHORS)
    assert im > z2 * 1.15
    # 120' at IF 0.808 -> ~131 TSS, plus 180' of easy riding
    assert 250 < im < 300


def test_content_tss_falls_back_to_the_label_rate_when_text_has_no_targets():
    t = content_tss(Sport.BIKE, IntensityClass.MODERATE, "Bike tempo (light)", 60, ANCHORS)
    assert t == pytest.approx(70, abs=1)
