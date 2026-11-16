"""The three agents and their skills must point at things that exist.

Agent and skill files are prompts: nothing imports them, so a renamed command or
a moved rulebook breaks them silently. These tests are the import check they
otherwise never get.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import typer
import yaml

from trinity import knowledge
from trinity.cli import app

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / ".claude" / "agents"
SKILLS = ROOT / ".claude" / "skills"
TEAM = {
    "coach": {"triathlete-profile", "training-plan", "adjust-plan"},
    "doctor": {"health-review"},
    "dietician": {"nutrition-review"},
}
DOMAINS = {"coach": "triathlon", "doctor": "health", "dietician": "nutrition"}


def _frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} has no frontmatter"
    head, body = text[4:].split("\n---\n", 1)
    return yaml.safe_load(head), body


def _prompt_files() -> list[Path]:
    return sorted(AGENTS.glob("*.md")) + sorted(SKILLS.glob("*/SKILL.md"))


def test_the_team_is_exactly_coach_doctor_dietician():
    assert {p.stem for p in AGENTS.glob("*.md")} == set(TEAM)


@pytest.mark.parametrize("name", sorted(TEAM))
def test_agent_definition_is_loadable_and_names_its_skills(name):
    meta, body = _frontmatter(AGENTS / f"{name}.md")
    assert meta["name"] == name
    assert len(meta["description"]) > 40
    assert set(meta["skills"]) == TEAM[name]
    assert len(body.strip()) > 400
    for skill in meta["skills"]:
        skill_meta, _ = _frontmatter(SKILLS / skill / "SKILL.md")
        assert skill_meta["name"] == skill
    # Each agent knows the other two by name, so hand-offs can be asked for.
    for other in set(TEAM) - {name}:
        assert f"**{other}**" in body


def test_only_the_coach_may_delegate():
    for name in ("doctor", "dietician"):
        meta, _ = _frontmatter(AGENTS / f"{name}.md")
        assert "Agent" not in meta["tools"]
    coach, _ = _frontmatter(AGENTS / "coach.md")
    assert "tools" not in coach   # inherits everything, including the Agent tool


def test_every_skill_belongs_to_an_agent():
    assert {p.parent.name for p in SKILLS.glob("*/SKILL.md")} == set().union(*TEAM.values())


@pytest.mark.parametrize("name", sorted(TEAM))
def test_each_agent_has_a_rulebook_and_notes(name):
    domain = DOMAINS[name]
    assert knowledge.path("rules.yaml", domain).is_file()
    assert knowledge.path("notes.md", domain).is_file()
    _, body = _frontmatter(AGENTS / f"{name}.md")
    assert f"src/trinity/knowledge/{domain}/" in body


@pytest.mark.parametrize("path", _prompt_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_prompts_only_mention_commands_and_files_that_exist(path):
    text = path.read_text(encoding="utf-8")
    commands = set(typer.main.get_command(app).commands)
    for cmd in re.findall(r"uv run trinity ([a-z][a-z-]*)", text):
        assert cmd in commands, f"{path.name} mentions `trinity {cmd}`, which is not a command"
    for ref in re.findall(r"`((?:src|examples|data/fixtures)/[\w./-]+|DISCLAIMER\.md)`", text):
        assert (ROOT / ref).exists(), f"{path.name} points at {ref}, which does not exist"


def test_cursor_reads_the_same_skills():
    assert (ROOT / ".cursor" / "skills").resolve() == SKILLS.resolve()
