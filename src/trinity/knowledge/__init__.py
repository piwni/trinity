"""The rulebooks: parameterized YAML the Python guardrails enforce, plus the
cited notes the agents read.

One folder per agent's domain, shipped inside the package so an installed
``trinity`` carries its own rules:

- ``triathlon/`` — the coach: plan shape, readiness, zones, dated facts.
- ``nutrition/`` — the dietician: fueling thresholds.
- ``health/``    — the doctor: lab markers and health thresholds.

Set ``TRINITY_KNOWLEDGE_DIR`` to load a tuned copy from somewhere else.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from trinity.config import get_config


def path(name: str, subdir: str = "triathlon") -> Path:
    """Where a rulebook file lives, e.g. ``path("rules.yaml", "health")``."""
    return get_config().knowledge_dir / subdir / name


@lru_cache(maxsize=16)
def _load_file(file: Path) -> dict[str, Any]:
    if not file.exists():
        raise FileNotFoundError(f"knowledge file missing: {file}")
    with file.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _load(name: str, subdir: str = "triathlon") -> dict[str, Any]:
    # Cached by resolved path, not by name, so a changed TRINITY_KNOWLEDGE_DIR
    # can never be answered from another directory's copy.
    return _load_file(path(name, subdir))


def load_rules() -> dict[str, Any]:
    return _load("rules.yaml")


def load_readiness_rules() -> dict[str, Any]:
    return _load("readiness.yaml")


def load_zones() -> dict[str, Any]:
    return _load("zones.yaml")


def load_facts() -> list[dict]:
    """Dated facts the spec linter checks prose against. Empty if the file is absent."""
    file = path("facts.yaml")
    if not file.exists():
        return []
    return list((_load_file(file) or {}).get("facts") or [])


def load_nutrition_rules() -> dict[str, Any]:
    return _load("rules.yaml", subdir="nutrition")


def load_health_rules() -> dict[str, Any]:
    return _load("rules.yaml", subdir="health")


def file_sha256(name: str, subdir: str = "triathlon") -> str:
    return hashlib.sha256(path(name, subdir).read_bytes()).hexdigest()


def knowledge_version() -> int:
    return int(load_rules().get("schema_version", 1))
