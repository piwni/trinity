"""Nutrition digest — the bounded, provenance-tagged fueling summary the
nutritionist skill reads.

Same contract as the training context digest: Python computes every number here
(from Fitatu daily totals + training load + wellness); the agent never recomputes
them. Missing/assumed data is stated explicitly rather than silently filled.
Persisted to ``data/context/nutrition-athlete.json``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from trinity import SCHEMA_VERSION
from trinity.models.context_digest import DigestValue
from trinity.models.readiness import ReadinessFlag


class MicronutrientStatus(str, Enum):
    """Traffic light plus an explicit UNKNOWN.

    Mirrors :class:`ReadinessFlag`'s values so existing readers keep working, but
    adds the state the traffic light cannot express: the logged data is too sparse
    to say anything. Previously that case was reported as GREEN ("don't alarm"),
    which reads as "intake is adequate" — an equally unfounded claim.
    """

    GREEN = "green"
    AMBER = "amber"
    RED = "red"
    UNKNOWN = "unknown"


class MicronutrientFlag(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nutrient: str
    mean_intake: float
    target: float
    unit: str
    pct_of_target: float
    flag: MicronutrientStatus
    # % of logged FOOD ITEMS that supplied a value for this nutrient. None when the
    # partition predates coverage tracking. A day-level measure is useless here:
    # one item out of ~17 makes the daily total non-zero.
    coverage_pct: float | None = None
    coverage_basis: str = "items"   # "items" | "items+reference" | "unavailable"
    coverage_days: int = 0          # logged days the coverage figure is measured over
    reliable: bool = True           # False when coverage is too low to trust the flag
    # Share of the COVERED items whose value came from a reference food table
    # rather than the athlete's log. Coverage says how much of the intake we can
    # account for; this says how much of that we had to supply ourselves.
    imputed_pct: float = 0.0


class CorrelationFinding(BaseModel):
    """An observational association (never causal)."""

    model_config = ConfigDict(extra="forbid")
    driver: str          # e.g. "energy_availability"
    outcome: str         # e.g. "next_day_hrv_rmssd_ms"
    method: str          # "pearson" | "spearman"
    r: float
    n: int
    lag_days: int        # 0 = same day, 1 = next-day outcome
    notable: bool
    note: str | None = None

    # --- confounder adjustment -------------------------------------------------
    # Raw `r` conflates the driver with training load: the athlete eats most on the
    # hardest days, and hard days depress next-morning HRV on their own. `r_partial`
    # is the association with `controlled_for` regressed out.
    r_partial: float | None = None
    controlled_for: list[str] = Field(default_factory=list)

    # --- effect size -----------------------------------------------------------
    # A coefficient says how tightly two things move together, not how far the
    # outcome actually moves. `effect_p10_p90` is the modelled change in the outcome
    # (in `effect_unit`) between a low-normal and high-normal day for this driver,
    # after adjustment. Compare against `materiality_threshold`, the athlete's own
    # smallest worthwhile change (0.5 x SD of the outcome).
    effect_p10_p90: float | None = None
    effect_unit: str | None = None
    materiality_threshold: float | None = None


class MonthSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    month: str           # "2026-01"
    days_logged: int
    mean_intake_kcal: float | None = None
    mean_energy_availability: float | None = None
    mean_carbs_g_per_kg: float | None = None
    mean_protein_g_per_kg: float | None = None


class NutritionDigest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = SCHEMA_VERSION
    athlete_id: str = "athlete"
    generated_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC))
    window_from: dt.date | None = None
    window_to: dt.date | None = None
    days_logged: int = 0

    # Energy.
    mean_intake_kcal: DigestValue = Field(default_factory=DigestValue)
    mean_exercise_kcal: DigestValue = Field(default_factory=DigestValue)
    energy_availability_kcal_per_kg: DigestValue = Field(default_factory=DigestValue)
    energy_availability_flag: ReadinessFlag = ReadinessFlag.GREEN
    mean_energy_balance_kcal: DigestValue = Field(default_factory=DigestValue)
    low_ea_days_pct: DigestValue = Field(default_factory=DigestValue)

    # Macros (per kg body mass/day means).
    carbs_g_per_kg: DigestValue = Field(default_factory=DigestValue)
    protein_g_per_kg: DigestValue = Field(default_factory=DigestValue)
    fat_g_per_kg: DigestValue = Field(default_factory=DigestValue)
    protein_flag: ReadinessFlag = ReadinessFlag.GREEN
    key_session_fuel_pct: DigestValue = Field(default_factory=DigestValue)  # % of key days meeting carb min
    carb_underfuel_days_pct: DigestValue = Field(default_factory=DigestValue)

    # Micronutrients + diet quality.
    micronutrient_flags: list[MicronutrientFlag] = Field(default_factory=list)
    sugars_pct_energy: DigestValue = Field(default_factory=DigestValue)
    saturated_pct_energy: DigestValue = Field(default_factory=DigestValue)
    fibre_g: DigestValue = Field(default_factory=DigestValue)
    caffeine_mg: DigestValue = Field(default_factory=DigestValue)
    alcohol_drinks_per_week: DigestValue = Field(default_factory=DigestValue)
    weight_kg: DigestValue = Field(default_factory=DigestValue)

    # Trends + associations.
    monthly: list[MonthSummary] = Field(default_factory=list)
    recovery_correlations: list[CorrelationFinding] = Field(default_factory=list)

    # Honesty.
    assumptions: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    approx_token_budget: int = 1500

    def sha256(self) -> str:
        payload = self.model_dump(mode="json", exclude={"generated_at"})
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()
