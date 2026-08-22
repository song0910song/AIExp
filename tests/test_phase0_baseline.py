import hashlib
import json
from pathlib import Path

from lighting_agent.deliverables import build_design_report, build_dialux_task_package
from lighting_agent import agent
from lighting_agent.project_store import ProjectStore
from lighting_agent.schemas import DesignBrief


FIXTURES = Path(__file__).parent / "fixtures" / "phase0"


def test_phase0_agent_tool_inventory_matches_documented_baseline() -> None:
    names = [
        tool.name
        for tool in (
            agent.get_project,
            agent.create_project,
            agent.ask_user,
            agent.update_project_brief,
            agent.apply_rag_lighting_parameters,
            agent.search_evidence,
            agent.adopt_evidence,
            agent.add_document,
            agent.calculate_preliminary_lighting,
            agent.check_design_rules,
            agent.prepare_luminaire_search,
            agent.search_luminaires,
            agent.get_luminaire_detail,
            agent.select_luminaires,
            agent.create_dialux_task_package,
            agent.generate_design_report,
        )
    ]
    assert len(names) == 16
    assert "import_selected_luminaires_to_dialux" not in names


def test_phase0_fixture_set_is_complete_and_redacted() -> None:
    projects = sorted((FIXTURES / "projects").glob("*.json"))
    assert len(projects) == 10
    assert (FIXTURES / "sample-room.dxf").stat().st_size > 0
    assert (FIXTURES / "standard-gb50034-2024.md").stat().st_size > 0
    candidates = json.loads((FIXTURES / "luminaire-candidates.json").read_text(encoding="utf-8"))
    assert candidates and all(item["detail_url"].startswith("https://example.invalid/") for item in candidates)
    result = json.loads((FIXTURES / "dialux-result.json").read_text(encoding="utf-8"))
    assert result["source_kind"] == "manual_form"
    assert all("脱敏" in json.loads(path.read_text(encoding="utf-8"))["project_name"] for path in projects)


def test_phase0_same_input_has_stable_revision_package_and_report(tmp_path) -> None:
    payload = json.loads((FIXTURES / "projects" / "project-05.json").read_text(encoding="utf-8"))
    payload.pop("template_id")
    brief = DesignBrief.model_validate(payload)
    state = ProjectStore(tmp_path / "project").create(brief)

    assert state.revision == 0
    assert build_dialux_task_package(state) == build_dialux_task_package(state)
    assert build_design_report(state) == build_design_report(state)


def test_phase0_fixture_hashes_are_repeatable() -> None:
    hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(FIXTURES.rglob("*")) if path.is_file()]
    assert len(hashes) >= 13
    assert hashes == [hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(FIXTURES.rglob("*")) if path.is_file()]
