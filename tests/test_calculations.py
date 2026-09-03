import pytest
from pydantic import ValidationError

from lighting_agent.calculations import calculate_lumen_method, check_design_rules
from lighting_agent.schemas import CalculationInput, RuleRequirement


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


@pytest.mark.parametrize("metric", ["lpd_w_m2", "uniformity_u0"])
def test_removed_rule_metrics_are_rejected(metric: str) -> None:
    with pytest.raises(ValidationError):
        RuleRequirement(metric=metric, operator="min", threshold=0.6)
