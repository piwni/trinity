"""The season-spec commands, end to end on the synthetic athlete.

These used to be standalone scripts. The tests run them the way a user does,
through the installed CLI, so a missing import or a path that only resolves
from a source checkout fails here.
"""

from __future__ import annotations

import datetime as dt

import yaml
from typer.testing import CliRunner

from trinity.cli import app
from trinity.reports.pdf import markdown_to_html
from trinity.storage.io import load_json
from trinity.storage.paths import plan_path

runner = CliRunner()


def _spec() -> dict:
    return {
        "meta": {
            "athlete_id": "athlete",
            "plan_id": "t-season",
            "start_date": "2026-09-07",
            "starting_ctl": 60.0,
            "done_through": "2026-09-08",
            "title": "Test season",
        },
        "races": [{
            "name": "Test Half", "date": "2026-09-20", "bucket": "1/2", "priority": "A",
            "target_time": "5:00:00",
            "splits": [
                {"leg": "Swim 1.9 km", "target": "0:35:00"},
                {"leg": "Bike 90 km", "target": "2:40:00", "detail": "200 W"},
                {"leg": "Run 21.1 km", "target": "1:45:00"},
            ],
        }],
        "weeks": [{
            "index": 1, "start": "2026-09-07", "phase": "base", "type": "explicit",
            "focus": "Easy aerobic week",
            "sessions": [
                {"day": 1, "sport": "run", "name": "Run easy", "intensity": "easy", "minutes": 40},
                {"day": 2, "sport": "bike", "name": "Bike Z2", "intensity": "easy", "minutes": 60},
                {"day": 3, "sport": "swim", "name": "Swim aerobic", "intensity": "easy", "minutes": 45},
            ],
        }],
    }


def _write_spec(tmp_path, spec: dict):
    path = tmp_path / "season.yaml"
    path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    return path


def test_build_plan_writes_the_plan(tmp_data_dir):
    spec_path = _write_spec(tmp_data_dir, _spec())
    res = runner.invoke(app, ["build-plan", str(spec_path)])
    assert res.exit_code == 0, res.output
    plan = load_json(plan_path("t-season"))
    assert plan["goal_race"]["name"] == "Test Half"
    assert [s["name"] for s in plan["weeks"][0]["sessions"]] == [
        "Run easy", "Bike Z2", "Swim aerobic"]
    # The week table is printed for the user to eyeball.
    assert "swim/bike/run" in res.output and "1/1/1" in res.output


def test_build_plan_refuses_a_spec_that_fails_the_lint(tmp_data_dir):
    spec = _spec()
    # Prose states an FTP the spec never declared.
    spec["meta"]["declared"] = {"ftp_w": [250]}
    spec["meta"]["rationale"] = "Race power is IF 0.85 on FTP 300."
    spec_path = _write_spec(tmp_data_dir, spec)
    res = runner.invoke(app, ["build-plan", str(spec_path)])
    assert res.exit_code == 1, res.output
    assert "lint_undeclared_ftp" in res.output
    assert "spec lint failed" in res.output
    assert not plan_path("t-season").exists()


def test_build_plan_missing_spec_is_a_usage_error(tmp_data_dir):
    res = runner.invoke(app, ["build-plan", str(tmp_data_dir / "nope.yaml")])
    assert res.exit_code == 2


def test_report_and_horizon_build_from_persisted_data(tmp_data_dir):
    spec_path = _write_spec(tmp_data_dir, _spec())
    for cmd in (["fetch"], ["profile"], ["build-plan", str(spec_path)]):
        res = runner.invoke(app, cmd)
        assert res.exit_code == 0, res.output

    res = runner.invoke(app, ["build-report", str(spec_path)])
    assert res.exit_code == 0, res.output
    report = (tmp_data_dir / "reports" / "t-season.html").read_text(encoding="utf-8")
    assert "<h1>Test season</h1>" in report
    assert "Guardrail report" in report and "Run easy" in report

    res = runner.invoke(app, ["build-horizon", str(spec_path), "--start", "2026-09-07",
                              "--end", "2026-09-13", "--label", "Test view"])
    assert res.exit_code == 0, res.output
    assert "(1 weeks," in res.output
    horizon = (tmp_data_dir / "reports" / "t-season-to-year-end.html").read_text(encoding="utf-8")
    assert "<h1>Test view · through 13 Sep 2026</h1>" in horizon
    # 8 Sep is on or before done_through; 9 Sep is not.
    assert horizon.count("t-done'>done") == 1


def test_horizon_with_no_weeks_in_range_fails_cleanly(tmp_data_dir):
    spec_path = _write_spec(tmp_data_dir, _spec())
    assert runner.invoke(app, ["build-plan", str(spec_path)]).exit_code == 0
    res = runner.invoke(app, ["build-horizon", str(spec_path), "--start", "2027-01-04"])
    assert res.exit_code == 1
    assert "no weeks between" in res.output


def test_horizon_default_window_runs_to_year_end(tmp_data_dir, monkeypatch):
    from trinity.reports import horizon

    class _Date(dt.date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 9)  # a Wednesday

    monkeypatch.setattr(horizon.dt, "date", _Date)
    spec_path = _write_spec(tmp_data_dir, _spec())
    assert runner.invoke(app, ["build-plan", str(spec_path)]).exit_code == 0
    out, weeks, _ = horizon.build_horizon(spec_path)
    assert weeks == 1
    html = out.read_text(encoding="utf-8")
    # Starts from that week's Monday and is labelled with the plan id.
    assert "<title>t-season · 7 Sep – end of 2026</title>" in html


def test_markdown_note_becomes_a_printable_document(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("# Notatka\n\n| a | b |\n| --- | --- |\n| 1 | zażółć |\n", encoding="utf-8")
    doc = markdown_to_html(note)
    assert "<title>note</title>" in doc
    assert "<table>" in doc and "zażółć" in doc
    assert "@page { size: A4" in doc
