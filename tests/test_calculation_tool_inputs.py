from __future__ import annotations

import pytest

import lighting_agent.tools as agent_tools
from lighting_agent.project_store import ProjectStore
from lighting_agent.schemas import DesignBrief, LightingGroup, LuminaireCandidate


def _store_with_selected_luminaire(tmp_path):
    store = ProjectStore(tmp_path / "projects")
    project = store.create(
        DesignBrief(
            project_name="Tool input compatibility",
            space_type="Meeting room",
            lighting_groups=[
                LightingGroup(
                    group_id="general-01",
                    region_name="Room",
                    group_name="General",
                    area_m2=30,
                    mounting_height_m=2.7,
                    target_illuminance_lx=500,
                    confirmed=True,
                )
            ],
        )
    )
    candidate = LuminaireCandidate(
        luminaire_id="fixture-01",
        article_name="Fixture 01",
        power_w=24,
        luminous_flux_lm=3200,
        detail_url="https://example.test/fixture-01",
        matching_status="matches",
    )
    project, saved, _ = store.append_luminaires(project.project_id, project.revision, [candidate])
    assert saved == 1
    project = store.set_selected_luminaires(project.project_id, project.revision, [candidate.luminaire_id])
    return store, project


def test_agent_calculation_fills_group_and_selected_luminaire_values(tmp_path, monkeypatch) -> None:
    store, project = _store_with_selected_luminaire(tmp_path)
    monkeypatch.setattr(agent_tools, "project_store", store)

    result = agent_tools.calculate_preliminary_lighting.invoke(
        {
            "project_id": project.project_id,
            "expected_revision": project.revision,
            "target_uniformity_u0": 0.6,
            "max_lpd_w_m2": 6.5,
            "inputs": {
                "region_name": "Room",
                "group_name": "General",
                "area": "30 m2",
                "target_lx": "500 lx",
                "uf": "0.6",
                "mf": "0.8",
            },
        }
    )

    calculation = result["calculations"][0]
    assert calculation["group_id"] == "general-01"
    assert calculation["mounting_height_m"] == 2.7
    assert calculation["inputs"]["luminaire_luminous_flux_lm"] == 3200
    assert calculation["inputs"]["luminaire_power_w"] == 24
    assert calculation["luminaire_count"] == 10


def test_agent_calculation_extracts_nested_group_and_luminaire_objects(tmp_path, monkeypatch) -> None:
    store, project = _store_with_selected_luminaire(tmp_path)
    monkeypatch.setattr(agent_tools, "project_store", store)

    result = agent_tools.calculate_preliminary_lighting.invoke(
        {
            "project_id": project.project_id,
            "expected_revision": project.revision,
            "inputs": {
                "group": {
                    "id": "general-01",
                    "region": "Room",
                    "name": "General",
                    "area": "30 m2",
                    "mounting_height": "2.7 m",
                    "target_lx": "500 lx",
                    "confirmed": True,
                },
                "luminaire": {
                    "id": "fixture-01",
                    "lumens": "3200 lm",
                    "power": "24 W",
                    "article_name": "Fixture 01",
                },
                "uf": "60%",
                "mf": "80%",
            },
        }
    )

    calculation = result["calculations"][0]
    assert calculation["group_id"] == "general-01"
    assert calculation["inputs"]["utilization_factor"] == 0.6
    assert calculation["inputs"]["maintenance_factor"] == 0.8
    assert calculation["luminaire_count"] == 10


def test_agent_calculation_rejects_ambiguous_placeholder_group(tmp_path, monkeypatch) -> None:
    store = ProjectStore(tmp_path / "projects")
    project = store.create(
        DesignBrief(
            project_name="Ambiguous groups",
            space_type="Office",
            lighting_groups=[
                LightingGroup(
                    group_id="general-01",
                    region_name="Room",
                    group_name="General",
                    area_m2=20,
                    mounting_height_m=2.7,
                    target_illuminance_lx=500,
                    confirmed=True,
                ),
                LightingGroup(
                    group_id="accent-01",
                    region_name="Room",
                    group_name="Accent",
                    area_m2=10,
                    mounting_height_m=2.4,
                    target_illuminance_lx=300,
                    confirmed=True,
                ),
            ],
        )
    )
    monkeypatch.setattr(agent_tools, "project_store", store)

    with pytest.raises(ValueError, match="more than one|unknown lighting group"):
        agent_tools.calculate_preliminary_lighting.invoke(
            {
                "project_id": project.project_id,
                "expected_revision": project.revision,
                "inputs": {
                    "area_m2": 20,
                    "target_illuminance_lx": 500,
                    "luminaire_luminous_flux_lm": 3200,
                    "luminaire_power_w": 24,
                    "utilization_factor": 0.6,
                    "maintenance_factor": 0.8,
                },
            }
        )


def test_agent_calculation_uses_group_assignment_before_project_selection(tmp_path, monkeypatch) -> None:
    store, project = _store_with_selected_luminaire(tmp_path)
    second = LuminaireCandidate(
        luminaire_id="fixture-02",
        article_name="Fixture 02",
        power_w=40,
        luminous_flux_lm=5000,
        detail_url="https://example.test/fixture-02",
        matching_status="matches",
    )
    project, saved_count, _ = store.append_luminaires(project.project_id, project.revision, [second])
    assert saved_count == 1
    project = store.set_selected_luminaires(
        project.project_id,
        project.revision,
        ["fixture-01", "fixture-02"],
        {"general-01": ["fixture-01"]},
    )
    monkeypatch.setattr(agent_tools, "project_store", store)

    result = agent_tools.calculate_preliminary_lighting.invoke(
        {
            "project_id": project.project_id,
            "expected_revision": project.revision,
            "inputs": {
                "group_id": "general-01",
                "utilization_factor": 0.6,
                "maintenance_factor": 0.8,
            },
        }
    )

    calculation = result["calculations"][0]
    assert calculation["inputs"]["luminaire_luminous_flux_lm"] == 3200
    assert calculation["inputs"]["luminaire_power_w"] == 24


def test_agent_calculation_returns_recoverable_missing_luminaire_inputs(tmp_path, monkeypatch) -> None:
    store = ProjectStore(tmp_path / "projects")
    project = store.create(
        DesignBrief(
            project_name="Missing luminaire inputs",
            space_type="Office",
            lighting_groups=[
                LightingGroup(
                    group_id="general-01",
                    region_name="Room",
                    group_name="General",
                    area_m2=20,
                    mounting_height_m=2.7,
                    target_illuminance_lx=500,
                    confirmed=True,
                )
            ],
        )
    )
    monkeypatch.setattr(agent_tools, "project_store", store)

    result = agent_tools.calculate_preliminary_lighting.invoke(
        {
            "project_id": project.project_id,
            "expected_revision": project.revision,
            "inputs": {
                "group_id": "general-01",
                "utilization_factor": 0.6,
                "maintenance_factor": 0.8,
            },
        }
    )

    assert result["status"] == "needs_clarification"
    assert result["missing_fields"] == [
        "luminaire_luminous_flux_lm",
        "luminaire_power_w",
    ]
    assert store.get(project.project_id).calculations == []
