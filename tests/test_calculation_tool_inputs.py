from __future__ import annotations

import lighting_agent.tools as agent_tools
from lighting_agent.project_store import ProjectStore
from lighting_agent.schemas import CalculationInput, DesignBrief, LuminaireCandidate


def _store_with_selected_luminaire(tmp_path):
    store = ProjectStore(tmp_path / "projects", import_legacy=False)
    project = store.create(DesignBrief(project_name="Tool input compatibility", space_type="Meeting room", area_m2=30, target_illuminance_lx=500))
    candidate = LuminaireCandidate(luminaire_id="fixture-01", article_name="Fixture 01", power_w=24, luminous_flux_lm=3200, detail_url="https://example.test/fixture-01", matching_status="matches")
    project, saved, _ = store.append_luminaires(project.project_id, project.revision, [candidate])
    assert saved == 1
    project = store.set_selected_luminaires(project.project_id, project.revision, [candidate.luminaire_id])
    return store, project


def test_agent_calculation_fills_project_and_selected_luminaire_values(tmp_path, monkeypatch) -> None:
    store, project = _store_with_selected_luminaire(tmp_path)
    monkeypatch.setattr(agent_tools, "project_store", store)
    result = agent_tools.calculate_preliminary_lighting.invoke({"project_id": project.project_id, "expected_revision": project.revision, "inputs": {"uf": "0.6", "mf": "0.8"}})
    calculation = result["calculations"][0]
    assert calculation["inputs"]["luminaire_luminous_flux_lm"] == 3200
    assert calculation["inputs"]["luminaire_power_w"] == 24
    assert calculation["luminaire_count"] == 10


def test_agent_calculation_accepts_nested_luminaire_object(tmp_path, monkeypatch) -> None:
    store, project = _store_with_selected_luminaire(tmp_path)
    monkeypatch.setattr(agent_tools, "project_store", store)
    result = agent_tools.calculate_preliminary_lighting.invoke({"project_id": project.project_id, "expected_revision": project.revision, "inputs": {"luminaire": {"id": "fixture-01", "lumens": "3200 lm", "power": "24 W"}, "uf": "60%", "mf": "80%"}})
    calculation = result["calculations"][0]
    assert calculation["inputs"]["utilization_factor"] == 0.6
    assert calculation["inputs"]["maintenance_factor"] == 0.8
    assert calculation["luminaire_count"] == 10


def test_agent_calculation_returns_recoverable_missing_luminaire_inputs(tmp_path, monkeypatch) -> None:
    store = ProjectStore(tmp_path / "projects", import_legacy=False)
    project = store.create(DesignBrief(project_name="Missing luminaire inputs", space_type="Office", area_m2=20, target_illuminance_lx=500))
    monkeypatch.setattr(agent_tools, "project_store", store)
    result = agent_tools.calculate_preliminary_lighting.invoke({"project_id": project.project_id, "expected_revision": project.revision, "inputs": {"utilization_factor": 0.6, "maintenance_factor": 0.8}})
    assert result["status"] == "needs_clarification"
    assert result["missing_fields"] == ["luminaire_luminous_flux_lm", "luminaire_power_w"]
    assert store.get(project.project_id).calculations == []


def test_calculation_input_ignores_removed_group_fields() -> None:
    item = CalculationInput.model_validate({"group_id": "legacy", "region_name": "Room", "group_name": "General", "mounting_height_m": 2.7, "area_m2": 30, "target_illuminance_lx": 500, "luminaire_luminous_flux_lm": 3200, "luminaire_power_w": 24, "utilization_factor": 0.6, "maintenance_factor": 0.8})
    assert "group_id" not in item.model_dump()
