from lighting_agent.calculations import calculate_lumen_method, check_design_rules
from lighting_agent.schemas import CalculationInput, RuleRequirement


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
    assert result.installed_power_w == 240
    assert result.installed_power_density_w_m2 == 8.0
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


def test_rule_checker_supports_uniformity_requirement() -> None:
    checks = check_design_rules(
        [RuleRequirement(metric="uniformity_u0", operator="min", threshold=0.6)],
        {"uniformity_u0": 0.61},
    )

    assert checks[0].status == "pass"
