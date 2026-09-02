from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from lighting_agent.calculations.layout import analyze_luminaire_layout
from lighting_agent.floor_plan import parse_floor_plan
from lighting_agent.project_store import ProjectStore
from lighting_agent.rag import LocalEvidenceStore
from lighting_agent.report_parser import parse_luminaire_report
from lighting_agent.web_api import create_app


DESIGN_FILES = Path(__file__).parents[1] / "docs" / "设计文件"


def test_phase1_real_512_files_extract_and_match_all_luminaires() -> None:
    plan = parse_floor_plan(DESIGN_FILES / "output.dxf", storage_path="output.dxf")
    report = parse_luminaire_report(DESIGN_FILES / "设计案_报告.pdf")
    analysis = analyze_luminaire_layout(plan, report)

    assert plan.drawing_units == "m"
    assert len(plan.luminaire_placements) == 36
    assert {len(item.dxf_entity_handles) for item in plan.luminaire_placements} == {72}
    assert len(report.placements) == 36
    assert {item.model: item.quantity for item in report.luminaires} == {
        "CEAH-M6311": 16,
        "PAK41101Y": 20,
    }
    assert analysis.dxf_count == analysis.report_count == 36
    assert analysis.model_parameters["CEAH-M6311"]["power_w"] == 38.9
    assert analysis.model_parameters["PAK41101Y"]["luminous_flux_lm"] == 2880
    assert len(analysis.placements) == 36
    assert all(item.matching_status == "matched" for item in analysis.placements)
    assert all(item.category == "unclassified" for item in analysis.placements)
    assert analysis.coordinate_transform is not None
    assert analysis.coordinate_transform.anchor_count == 36
    assert analysis.coordinate_transform.max_residual_m is not None
    assert analysis.coordinate_transform.max_residual_m < 0.05
    assert all(item.report_page in {17, 18, 19, 20, 21, 22, 23, 24} for item in analysis.placements)
    assert all(any("DLX_LUM:entity-" in ref for ref in item.source_refs) for item in analysis.placements)


def test_phase1_layout_analysis_is_versioned_and_available_over_http(tmp_path) -> None:
    projects = ProjectStore(tmp_path / "projects")
    app = create_app(
        project_store=projects,
        evidence_store=LocalEvidenceStore(tmp_path / "rag.json"),
    )
    client = TestClient(app)
    project = client.post("/api/projects", json={"project_name": "512 会议室"}).json()
    project_id = project["project_id"]

    plan_response = client.post(
        f"/api/projects/{project_id}/floor-plan",
        data={"expected_revision": "0"},
        files={"file": ("output.dxf", (DESIGN_FILES / "output.dxf").read_bytes(), "application/dxf")},
    )
    assert plan_response.status_code == 201, plan_response.text
    assert len(plan_response.json()["floor_plan"]["luminaire_placements"]) == 36

    response = client.post(
        f"/api/projects/{project_id}/layout-analysis",
        data={"expected_revision": "1"},
        files={"report_file": ("设计案_报告.pdf", (DESIGN_FILES / "设计案_报告.pdf").read_bytes(), "application/pdf")},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["matched_count"] == 36
    assert payload["project"]["revision"] == 2
    assert payload["project"]["layout_analysis"]["report_count"] == 36
    assert client.get(f"/api/projects/{project_id}/layout-analysis").json()["dxf_count"] == 36
    assert len(projects.revisions(project_id)) == 3


def test_phase1_coordinate_mismatch_is_an_issue_after_transform() -> None:
    plan = parse_floor_plan(DESIGN_FILES / "output.dxf", storage_path="output.dxf")
    report = parse_luminaire_report(DESIGN_FILES / "设计案_报告.pdf")
    changed = report.placements[0].model_copy(update={"x_m": report.placements[0].x_m + 1.0})
    report = report.model_copy(update={"placements": [changed, *report.placements[1:]]})

    analysis = analyze_luminaire_layout(plan, report, coordinate_tolerance_m=0.05)

    assert analysis.dxf_count == analysis.report_count == 36
    assert any(issue.kind == "coordinate_mismatch" for issue in analysis.issues)
    assert any(item.matching_status == "coordinate_mismatch" for item in analysis.placements)
    assert next(check for check in analysis.checks if check.metric == "coordinate_residual").status == "warning"
