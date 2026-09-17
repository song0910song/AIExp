import pytest
from pydantic import ValidationError

from lighting_agent.calculations import calculate_lumen_method, check_design_rules
from lighting_agent.schemas import (
    CalculationInput,
    CalculationResult,
    DesignBrief,
    RuleRequirement,
    SimulationMetrics,
)
from lighting_agent.calculations.verification import evaluate_illuminance
from lighting_agent.schemas import ProjectState, SimulationRun


def test_lumen_method_is_reproducible() -> None:
    result = calculate_lumen_method(
        CalculationInput(
            area_m2=30,
            target_illuminance_lx=500,
            luminaire_luminous_flux_lm=3200,
            luminaire_power_w=24,
            utilization_factor=0.6,
            maintenance_factor=0.8,
        )
    )

    assert result.required_luminous_flux_lm == 15000
    assert result.luminaire_count == 10
    assert result.estimated_illuminance_lx == 512
    assert result.installed_power_w == 240
    assert "not a point-by-point" in result.limitations[0]


def test_rule_checker_preserves_insufficient_illuminance_data() -> None:
    checks = check_design_rules(
        [
            RuleRequirement(metric="illuminance_lx", operator="min", threshold=500, evidence_id="evidence-1"),
        ],
        {},
    )

    assert checks[0].status == "insufficient_data"
    assert checks[0].evidence_id == "evidence-1"


def test_removed_brief_metrics_are_dropped_from_legacy_payloads() -> None:
    brief = DesignBrief.model_validate(
        {
            "project_name": "Legacy brief",
            "lighting_groups": [{"group_id": "group-a-1"}],
            "target_uniformity_u0": 0.6,
            "max_lpd_w_m2": 6.5,
            "confirmed_fields": ["target_uniformity_u0", "target_illuminance_lx"],
            "lighting_parameter_sources": {
                "target_uniformity_u0": {"evidence_ids": ["evidence-1"]}
            },
        }
    )

    assert "target_uniformity_u0" not in brief.model_dump()
    assert "max_lpd_w_m2" not in brief.model_dump()
    assert "target_uniformity_u0" not in brief.confirmed_fields
    assert "target_uniformity_u0" not in brief.lighting_parameter_sources


def test_removed_simulation_metric_is_dropped_from_legacy_payload() -> None:
    metrics = SimulationMetrics.model_validate(
        {
            "maintained_illuminance_lx": 500,
            "uniformity_u0": 0.6,
            "installed_power_density_w_m2": 6.5,
        }
    )

    assert metrics.model_dump() == {
        "maintained_illuminance_lx": 500.0,
        "minimum_illuminance_lx": None,
    }


def test_removed_calculation_metric_is_dropped_from_legacy_payload() -> None:
    result = calculate_lumen_method(
        CalculationInput(
            area_m2=30,
            target_illuminance_lx=500,
            luminaire_luminous_flux_lm=3200,
            luminaire_power_w=24,
            utilization_factor=0.6,
            maintenance_factor=0.8,
        )
    )
    payload = result.model_dump(mode="json")
    payload["installed_power_density_w_m2"] = 6.5

    restored = CalculationResult.model_validate(payload)

    assert "installed_power_density_w_m2" not in restored.model_dump()


@pytest.mark.parametrize("metric", ["lpd_w_m2", "uniformity_u0", "cri", "ugr"])
def test_removed_rule_metrics_are_rejected(metric: str) -> None:
    with pytest.raises(ValidationError):
        RuleRequirement(metric=metric, operator="min", threshold=0.6)


def test_joint_illuminance_verification_requires_both_methods() -> None:
    calculation = calculate_lumen_method(
        CalculationInput(
            area_m2=30,
            target_illuminance_lx=500,
            luminaire_luminous_flux_lm=3200,
            luminaire_power_w=24,
            utilization_factor=0.6,
            maintenance_factor=0.8,
        )
    )
    state = ProjectState(
        brief=DesignBrief(project_name="Joint check", target_illuminance_lx=500),
        calculations=[calculation],
        simulation_runs=[
            SimulationRun(
                kind="精算",
                status="succeeded",
                input_project_revision=0,
                metrics=SimulationMetrics(maintained_illuminance_lx=510),
                verification_status="matched",
            )
        ],
    )

    verification = evaluate_illuminance(state)

    assert verification.overall_status == "pass"
    assert verification.action == "target_reached"
    assert verification.lumen_method.observed_illuminance_lx == 512
    assert verification.dialux.observed_illuminance_lx == 510


def test_joint_illuminance_verification_fails_when_dialux_is_below_target() -> None:
    calculation = calculate_lumen_method(
        CalculationInput(
            area_m2=30,
            target_illuminance_lx=500,
            luminaire_luminous_flux_lm=3200,
            luminaire_power_w=24,
            utilization_factor=0.6,
            maintenance_factor=0.8,
        )
    )
    state = ProjectState(
        brief=DesignBrief(project_name="Joint check", target_illuminance_lx=500),
        calculations=[calculation],
        simulation_runs=[
            SimulationRun(
                kind="精算",
                status="succeeded",
                input_project_revision=0,
                metrics=SimulationMetrics(maintained_illuminance_lx=480),
                verification_status="matched",
            )
        ],
    )

    verification = evaluate_illuminance(state)

    assert verification.overall_status == "fail"
    assert verification.action == "revise_design"
