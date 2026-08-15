"""Reproducible preliminary calculations; no LLM decisions are made here."""

from __future__ import annotations

from math import ceil
from typing import Mapping

from ..schemas import CalculationInput, CalculationResult, RuleCheck, RuleRequirement


def calculate_lumen_method(inputs: CalculationInput) -> CalculationResult:
    """Estimate quantity via N = E × A / (Φ × UF × MF)."""

    required_flux = inputs.target_illuminance_lx * inputs.area_m2
    effective_flux_per_luminaire = (
        inputs.luminaire_luminous_flux_lm * inputs.utilization_factor * inputs.maintenance_factor
    )
    luminaire_count = ceil(required_flux / effective_flux_per_luminaire)
    installed_power = luminaire_count * inputs.luminaire_power_w
    return CalculationResult(
        inputs=inputs,
        required_luminous_flux_lm=round(required_flux, 2),
        luminaire_count=luminaire_count,
        installed_power_w=round(installed_power, 2),
        installed_power_density_w_m2=round(installed_power / inputs.area_m2, 3),
        assumptions=[
            "Utilization factor and maintenance factor are confirmed design assumptions.",
            "Luminaires are assumed to be distributed uniformly in the calculation area.",
        ],
        limitations=[
            "This is a lumen-method estimate, not a point-by-point lighting simulation.",
            "Illuminance uniformity, glare (UGR), reflectance and luminaire layout require DIALux evo or an equivalent calculation.",
        ],
    )


def check_design_rules(
    requirements: list[RuleRequirement], observations: Mapping[str, float | int | None]
) -> list[RuleCheck]:
    """Compare provided evidence-derived rules with explicit observed values."""

    checks: list[RuleCheck] = []
    for rule in requirements:
        observed_raw = observations.get(rule.metric)
        if observed_raw is None:
            checks.append(
                RuleCheck(
                    metric=rule.metric,
                    status="insufficient_data",
                    threshold=rule.threshold,
                    evidence_id=rule.evidence_id,
                    explanation=f"No observed {rule.metric} was supplied for deterministic comparison.",
                )
            )
            continue
        observed = float(observed_raw)
        passed = observed >= rule.threshold if rule.operator == "min" else observed <= rule.threshold
        comparison = "at least" if rule.operator == "min" else "at most"
        checks.append(
            RuleCheck(
                metric=rule.metric,
                status="pass" if passed else "fail",
                observed=observed,
                threshold=rule.threshold,
                evidence_id=rule.evidence_id,
                explanation=f"Observed {observed:g}; requirement is {comparison} {rule.threshold:g}.",
            )
        )
    return checks
