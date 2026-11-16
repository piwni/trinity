"""Health records (input) and the health digest (what the doctor agent reads).

Same contract as the other digests: Python compares every lab value with its
range and thresholds and summarizes the wellness data; the agent never does that
arithmetic itself. Anything that could not be assessed is stated, not assumed.

- Input: ``<data_dir>/health/records.yaml``, hand-entered by the athlete (or
  transcribed from a lab report and confirmed by them).
- Output: ``<data_dir>/context/health-<athlete>.json``.
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


# ---------------------------------------------------------------- input -----
class LabResult(BaseModel):
    """One line of a lab report, copied as printed."""

    model_config = ConfigDict(extra="forbid")
    marker: str
    value: float
    unit: str = ""
    # The reference range printed on the report, in the same unit as the value.
    ref_low: float | None = None
    ref_high: float | None = None
    note: str | None = None


class LabPanel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: dt.date
    lab: str | None = None
    fasting: bool | None = None
    # Anything about the day that changes how the numbers read: "morning after a
    # race", "third day of a cold", "rest day".
    context: str | None = None
    source: str | None = None
    results: list[LabResult] = Field(default_factory=list)


class HealthEvent(BaseModel):
    """Other health data the athlete puts in: an illness, an injury, a
    medication or supplement, a measurement that is not a lab value."""

    model_config = ConfigDict(extra="forbid")
    date: dt.date
    kind: str
    note: str


class HealthRecords(BaseModel):
    model_config = ConfigDict(extra="forbid")
    labs: list[LabPanel] = Field(default_factory=list)
    events: list[HealthEvent] = Field(default_factory=list)


# --------------------------------------------------------------- output -----
class HealthFlag(str, Enum):
    """Traffic light plus an explicit UNKNOWN: with no range and no threshold to
    compare against, a result is not assessable, which is not the same as fine."""

    GREEN = "green"
    AMBER = "amber"
    RED = "red"
    UNKNOWN = "unknown"

    @property
    def severity(self) -> int:
        return {"unknown": 0, "green": 0, "amber": 1, "red": 2}[self.value]


class LabStatus(str, Enum):
    """Position against the reference range printed on the lab report."""

    LOW = "low"
    IN_RANGE = "in_range"
    HIGH = "high"
    UNKNOWN = "unknown"   # the report gave no range


class LabMarker(BaseModel):
    """The latest result for one marker."""

    model_config = ConfigDict(extra="forbid")
    marker: str                    # catalog key, or the name as entered if not in the catalog
    name: str
    category: str | None = None
    in_catalog: bool = True
    value: float
    unit: str
    date: dt.date
    age_days: int
    stale: bool = False            # older than the rulebook's stale_after_days
    lab_ref_low: float | None = None
    lab_ref_high: float | None = None
    lab_status: LabStatus = LabStatus.UNKNOWN
    flag: HealthFlag = HealthFlag.UNKNOWN
    flag_basis: str | None = None  # what the flag rests on, in words
    threshold_source: str | None = None
    # Earlier result for the same marker, for trend. `change` is only given when
    # both results are in the same unit.
    previous_value: float | None = None
    previous_date: dt.date | None = None
    change: float | None = None
    n_results: int = 1
    notes: list[str] = Field(default_factory=list)


class PanelSummary(BaseModel):
    """One blood draw, with the training load that preceded it."""

    model_config = ConfigDict(extra="forbid")
    date: dt.date
    lab: str | None = None
    fasting: bool | None = None
    context: str | None = None
    results: int = 0
    prior_window_days: int = 3
    prior_tss: float | None = None     # combined TSS in the window before the draw
    prior_hours: float | None = None


class HealthDigest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = SCHEMA_VERSION
    athlete_id: str = "athlete"
    generated_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC))
    as_of: dt.date | None = None

    # Labs.
    overall_flag: HealthFlag = HealthFlag.UNKNOWN   # worst current (non-stale) lab flag
    attention: list[str] = Field(default_factory=list)
    labs: list[LabMarker] = Field(default_factory=list)
    panels: list[PanelSummary] = Field(default_factory=list)

    # Wellness, from the wearable feed.
    hrv_rmssd_ms: DigestValue = Field(default_factory=DigestValue)
    hrv_baseline_60d_ms: DigestValue = Field(default_factory=DigestValue)
    resting_hr_bpm: DigestValue = Field(default_factory=DigestValue)
    resting_hr_baseline_bpm: DigestValue = Field(default_factory=DigestValue)
    sleep_avg_hours: DigestValue = Field(default_factory=DigestValue)
    weight_kg: DigestValue = Field(default_factory=DigestValue)
    weight_change_90d_kg: DigestValue = Field(default_factory=DigestValue)

    # What the other two agents already computed. Pointers, not a recomputation.
    readiness_flag: ReadinessFlag | None = None
    readiness_notes: list[str] = Field(default_factory=list)
    energy_availability_flag: ReadinessFlag | None = None
    energy_availability_kcal_per_kg: DigestValue = Field(default_factory=DigestValue)
    training_load_ctl: DigestValue = Field(default_factory=DigestValue)
    training_form_tsb: DigestValue = Field(default_factory=DigestValue)

    # The athlete's own entries, most recent first.
    events: list[HealthEvent] = Field(default_factory=list)

    # Honesty.
    assumptions: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    approx_token_budget: int = 2000

    def sha256(self) -> str:
        payload = self.model_dump(mode="json", exclude={"generated_at"})
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()
