"""Validate a plan against the athlete's persisted profile.

``validate_plan`` is pure: it takes the tolerances it should enforce. This is
the one place that looks those up (volume tolerance, training age, current CTL)
so that `validate`, `push` and the season report all gate a plan the same way.
"""

from __future__ import annotations

from trinity.models.plan import TrainingPlan
from trinity.models.profile import AthleteProfile
from trinity.planning.validator import ValidationReport, validate_plan
from trinity.storage.io import load_json
from trinity.storage.paths import profile_path


def validate_with_profile(plan: TrainingPlan) -> ValidationReport:
    prof_data = load_json(profile_path(plan.athlete_id))
    tolerance = starting_ctl = None
    training_age = "intermediate"
    if prof_data:
        prof = AthleteProfile.model_validate(prof_data)
        tolerance = prof.weekly_volume_tolerance_hours
        training_age = prof.training_age.value
        if prof.load.combined.ctl.known:
            starting_ctl = prof.load.combined.ctl.value
    if plan.starting_ctl is not None:
        starting_ctl = plan.starting_ctl
    return validate_plan(plan, starting_ctl=starting_ctl,
                         weekly_hours_tolerance=tolerance, training_age=training_age)
