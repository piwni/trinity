"""Spec-text consistency checks.

The plan passed the validator with 0 errors while its race cards disagreed with
each other: one card said "IF 0.85 on the real 245 W FTP" while the brief said
255 W; a card said "Fuel 100 g/h" against a 95 g/h protocol ceiling; a race-week
note claimed "the backup IM is already registered" when no such race existed; and
the World Championship section described a Kona/Nice alternation that ended.
Those are text facts, so they are linted from the spec text.
"""

from __future__ import annotations

from trinity.planning.spec_lint import lint_spec


def base_spec(**meta):
    m = {
        "declared": {"ftp_w": [250, 258, 265, 275], "cda": [0.232, 0.195],
                     "fuel_ceiling_g_per_h": 95},
        **meta,
    }
    return {
        "meta": m,
        "races": [{"name": "Malbork", "date": "2027-09-05", "priority": "A", "bucket": "full",
                   "splits": [{"leg": "Bike 180 km", "target": "4:55:00",
                               "detail": "@ 196 W = IF 0.76 on FTP 258. CdA 0.232"}]}],
        "weeks": [{"index": 52, "start": "2027-08-30", "phase": "taper", "type": "explicit",
                   "sessions": [{"day": 6, "sport": "other", "name": "RACE: Malbork",
                                 "category": "race", "minutes": 560}]}],
    }


def codes(spec, raw=""):
    return {i.code for i in lint_spec(spec, raw)}


def test_clean_spec_passes():
    assert not {c for c in codes(base_spec()) if c.startswith("lint_")} - {"lint_ok"}


def test_undeclared_ftp_in_a_card_is_an_error():
    s = base_spec()
    s["races"][0]["splits"][0]["detail"] = "208 W NP = IF 0.85 on the real 245 W FTP"
    assert "lint_undeclared_ftp" in codes(s)


def test_undeclared_cda_is_an_error():
    s = base_spec()
    s["races"][0]["splits"][0]["detail"] = "at CdA 0.175 a 4:28 split needs ~200 W"
    assert "lint_undeclared_cda" in codes(s)


def test_fuel_above_the_protocol_ceiling_is_an_error():
    s = base_spec()
    s["races"][0]["splits"][0]["detail"] = "Fuel 100 g/h on the bike"
    assert "lint_fuel_over_ceiling" in codes(s)


def test_race_session_must_match_a_race_in_the_calendar():
    s = base_spec()
    s["weeks"][0]["sessions"][0]["day"] = 5           # Saturday: no race that day
    assert "lint_race_not_in_calendar" in codes(s)


def test_reference_to_an_unregistered_backup_race_is_an_error():
    s = base_spec()
    s["weeks"][0]["sessions"][0]["desc"] = "race the clock, and the backup IM is already registered"
    assert "lint_phantom_race" in codes(s)


def test_stale_world_championship_facts_are_caught_in_the_raw_text():
    raw = "# IRONMAN alternates the men's championship between Kona and Nice.\n"
    assert "lint_stale_fact" in codes(base_spec(), raw)


def test_venue_unconfirmed_is_stale():
    raw = '  - name: "2028 World Championship (venue UNCONFIRMED)"\n'
    assert "lint_stale_fact" in codes(base_spec(), raw)


def test_race_card_splits_must_sum_to_its_stated_times():
    spec = {"meta": {}, "weeks": [], "races": [{
        "name": "IM", "date": "2027-09-05", "target_time": "8:50", "planning_time": "9:17",
        "splits": [
            {"leg": "Swim 3.8 km", "target": "1:09:00", "planning": "1:12:00"},
            {"leg": "T1", "target": "2:30", "planning": "3:00"},
            {"leg": "Bike 180 km", "target": "4:37:00", "planning": "4:55:34"},
            {"leg": "T2", "target": "1:30", "planning": "2:00"},
            {"leg": "Run 42.2 km", "target": "3:00:00", "planning": "3:04:00"},
        ]}]}
    assert not [i for i in lint_spec(spec) if i.code == "lint_split_total"]
    spec["races"][0]["target_time"] = "8:40"
    bad = [i for i in lint_spec(spec) if i.code == "lint_split_total"]
    assert bad and "8:50:00" in bad[0].message
