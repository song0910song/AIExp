import pytest
from pydantic import ValidationError

from lighting_agent.calculations import calculate_lumen_method, check_design_rules
from lighting_agent.schemas import (
    CalculationInput,
    CalculationResult,
    DesignBrief,
    LightingGroup,
    RuleRequirement,
    SimulationMetrics,
)


def test_lumen_method_is_reproducible() -> None:
    result = calculate_lumen_method(
        CalculationInput(
            group_id="group-a",
            region_name="Open office",
            group_name="General lighting",
            mounting_height_m=2.7,
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
    assert result.installed_power_w == 240
    assert "not a point-by-point" in result.limitations[0]


def test_rule_checker_preserves_insufficient_data() -> None:
    checks = check_design_rules(
        [
            RuleRequirement(metric="cri", operator="min", threshold=80, evidence_id="evidence-1"),
            RuleRequirement(metric="ugr", operator="max", threshold=19),
        ],
        {"cri": 80},
    )

    assert checks[0].status == "pass"
    assert checks[0].evidence_id == "evidence-1"
    assert checks[1].status == "insufficient_data"


def test_removed_brief_metrics_are_dropped_from_legacy_payloads() -> None:
    group = LightingGroup.model_validate(
        {
            "group_id": "group-a-1",
            "region_name": "Open office",
            "group_name": "General lighting",
            "area_m2": 30,
            "mounting_height_m": 2.7,
            "target_illuminance_lx": 500,
            "target_uniformity_u0": 0.6,
            "max_lpd_w_m2": None,
        }
    )
    brief = DesignBrief.model_validate(
        {
            "project_name": "Legacy brief",
            "lighting_groups": [group.model_dump(mode="json")],
            "target_uniformity_u0": 0.6,
            "max_lpd_w_m2": 6.5,
            "confirmed_fields": ["target_uniformity_u0", "target_illuminance_lx"],
            "lighting_parameter_sources": {
                "target_uniformity_u0": {"evidence_ids": ["evidence-1"]}
            },
        }
    )

    assert "target_uniformity_u0" not in group.model_dump()
    assert "max_lpd_w_m2" not in group.model_dump()
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
        "ugr": None,
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


@pytest.mark.parametrize("metric", ["lpd_w_m2", "uniformity_u0"])
def test_removed_rule_metrics_are_rejected(metric: str) -> None:
    with pytest.raises(ValidationError):
        RuleRequirement(metric=metric, operator="min", threshold=0.6)
