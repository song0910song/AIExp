from lighting_agent.calculations import calculate_lumen_method
from lighting_agent.project_store import ProjectStore
from lighting_agent.schemas import CalculationInput, DesignBrief, LightingGroup


def test_groups_keep_independent_mounting_heights_and_calculations() -> None:
    base = dict(
        area_m2=20,
        target_illuminance_lx=500,
        luminaire_luminous_flux_lm=3200,
        luminaire_power_w=24,
        utilization_factor=0.6,
        maintenance_factor=0.8,
    )
    general = calculate_lumen_method(
        CalculationInput(
            group_id="general-01",
            region_name="Meeting room",
            group_name="General lighting",
            mounting_height_m=2.7,
            **base,
        )
    )
    accent = calculate_lumen_method(
        CalculationInput(
            group_id="accent-01",
            region_name="Meeting room",
            group_name="Accent lighting",
            mounting_height_m=2.2,
            target_illuminance_lx=750,
            **{key: value for key, value in base.items() if key != "target_illuminance_lx"},
        )
    )
    assert general.group_id == "general-01"
    assert general.mounting_height_m == 2.7
    assert accent.group_id == "accent-01"
    assert accent.mounting_height_m == 2.2
    assert accent.luminaire_count > general.luminaire_count


def test_project_persists_confirmed_lighting_groups(tmp_path) -> None:
    brief = DesignBrief(
        project_name="Grouped project",
        space_type="Meeting room",
        lighting_groups=[
            LightingGroup(
                group_id="general-01",
                region_name="Room A",
                group_name="General",
                area_m2=30,
                mounting_height_m=2.7,
                target_illuminance_lx=500,
                confirmed=True,
            ),
            LightingGroup(
                group_id="accent-01",
                region_name="Room A",
                group_name="Accent",
                area_m2=10,
                mounting_height_m=2.2,
                target_illuminance_lx=750,
                confirmed=True,
            ),
        ],
    )
    state = ProjectStore(tmp_path / "projects").create(brief)
    assert [item.mounting_height_m for item in state.brief.lighting_groups] == [2.7, 2.2]
    assert state.brief.missing_design_inputs() == []
