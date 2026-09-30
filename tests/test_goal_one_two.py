"""Goals 1/2 regression: real CAD/PDF parsing, mocked remote OCR, explicit review."""
from __future__ import annotations

from io import BytesIO, StringIO
import math
import base64
from pathlib import Path

import ezdxf
import fitz
import pytest
from fastapi.testclient import TestClient

from lighting_agent.document_loader import DocumentLoadError, load_document
from lighting_agent.document_loader import _extract_text, _markdown_tables, _ocr_layout
from lighting_agent.floor_plan import parse_floor_plan
from lighting_agent.drawing_preview import render_drawing_preview
from lighting_agent.project_store import ProjectStore
from lighting_agent.rag import LocalEvidenceStore
from lighting_agent.schemas import StandardRecord, DesignRule
from lighting_agent.rules import rule_conflicts
from lighting_agent.tools import make_tools
from lighting_agent.web_api import create_app


def cad(tmp_path, rectangles=((0, 0), (10, 0)), units=6):
    document = ezdxf.new("R2010")
    document.header["$INSUNITS"] = units
    for x, y in rectangles:
        document.modelspace().add_lwpolyline([(x, y), (x + 5, y), (x + 5, y + 4), (x, y + 4)], close=True, dxfattribs={"layer": "WALL"})
    path = tmp_path / "drawing.dxf"
    document.saveas(path)
    return document, path


def parse(path):
    return parse_floor_plan(path, storage_path=path.name)


def test_equal_area_rooms_stay_separate_and_exact_duplicates_merge(tmp_path):
    document, path = cad(tmp_path)
    document.modelspace().add_lwpolyline([(0, 0), (5, 0), (5, 4), (0, 4)], close=True, dxfattribs={"layer": "WALL"})
    document.saveas(path)
    plan = parse(path)
    assert len(plan.area_candidates) == 2
    assert [c.area_m2 for c in plan.area_candidates] == [20, 20]
    assert len({c.candidate_id for c in plan.area_candidates}) == 2
    assert plan.read_complete and not plan.spatial_model.design_ready
    assert plan.repairs and plan.drawing_paths


def test_more_than_fifty_rooms_not_silently_dropped(tmp_path):
    _, path = cad(tmp_path, [(10 * i, 0) for i in range(60)])
    assert len(parse(path).spatial_model.rooms) == 60


def test_rotated_scaled_nested_block_uses_world_coordinates(tmp_path):
    document, path = cad(tmp_path, [])
    block = document.blocks.new("office")
    block.add_lwpolyline([(0, 0), (5, 0), (5, 4), (0, 4)], close=True)
    nested = document.blocks.new("floor")
    nested.add_blockref("office", (10, 0))
    document.modelspace().add_blockref("floor", (100, 100), dxfattribs={"rotation": 90, "xscale": 2, "yscale": 2})
    document.saveas(path)
    plan = parse(path)
    assert len(plan.area_candidates) == 1
    candidate = plan.area_candidates[0]
    assert candidate.area_m2 == pytest.approx(80)
    assert min(p.x for p in candidate.boundary) == pytest.approx(92)
    assert min(p.y for p in candidate.boundary) == pytest.approx(120)
    assert any("WCS" in message for message in plan.repairs)


def test_full_boundary_separate_from_decimated_preview(tmp_path):
    document, path = cad(tmp_path, [])
    points = [(20 * math.cos(2 * math.pi * i / 1200), 20 * math.sin(2 * math.pi * i / 1200)) for i in range(1200)]
    document.modelspace().add_lwpolyline(points, close=True)
    document.saveas(path)
    candidate = parse(path).area_candidates[0]
    assert len(candidate.boundary) == 1200
    assert len(candidate.points) == 1000
    assert candidate.raw_area == pytest.approx(1256.6313, abs=.001)


def test_bulge_and_curves_not_replaced_with_chords(tmp_path):
    document, path = cad(tmp_path, [])
    document.modelspace().add_lwpolyline([(0, 0, 1), (10, 0, 1)], format="xyb", close=True)
    document.saveas(path)
    candidate = parse(path).area_candidates[0]
    assert candidate.area_m2 == pytest.approx(math.pi * 25, abs=.05)
    assert candidate.geometry_sources[0]["vertices_xyseb"][0][-1] == 1
    assert len(candidate.boundary) > 10


def test_small_gap_repair_is_logged_and_larger_gap_is_visible(tmp_path):
    document, path = cad(tmp_path, [])
    space = document.modelspace()
    for a, b in [((0, 0), (5, 0)), ((5.0005, 0), (5, 4)), ((5, 4), (0, 4)), ((0, 4), (0, 0))]:
        space.add_line(a, b, dxfattribs={"layer": "WALL"})
    document.saveas(path)
    plan = parse(path)
    assert len(plan.area_candidates) == 1
    assert any("1 mm" in s for s in plan.repairs)
    space[1].dxf.start = (5.01, 0)
    document.saveas(path)
    plan = parse(path)
    assert not plan.area_candidates
    assert any(i.code == "open_geometry" for i in plan.issues)


def test_semantic_furniture_is_not_a_room_and_missing_heights_not_invented(tmp_path):
    document, path = cad(tmp_path, [(0, 0)])
    furniture = document.blocks.new("office_desk")
    furniture.add_lwpolyline([(0, 0), (1, 0), (1, 1), (0, 1)], close=True)
    document.modelspace().add_blockref("office_desk", (2, 2))
    document.saveas(path)
    plan = parse(path)
    assert len(plan.spatial_model.elements) == 1
    assert plan.spatial_model.elements[0].height_m is None
    assert plan.spatial_model.elements[0].room_id is not None
    assert all(room.height_m is None for room in plan.spatial_model.rooms)


def test_dialux_aperture_codes_are_not_exposed_as_furniture_or_room_candidates(tmp_path):
    document, path = cad(tmp_path, [(0, 0)])
    aperture = document.blocks.new("2CC")
    aperture.add_lwpolyline([(0, 0), (.2, 0), (.2, .1), (0, .1)], close=True)
    document.modelspace().add_blockref("2CC", (2, 0), dxfattribs={"layer": "DLX_APERT"})
    document.saveas(path)
    plan = parse(path)
    assert len(plan.area_candidates) == 1
    assert plan.spatial_model.elements == []
    assert render_drawing_preview(plan).startswith(b"\x89PNG\r\n\x1a\n")


def test_dimension_entities_are_presented_as_human_readable_evidence(tmp_path):
    document, path = cad(tmp_path, [(0, 0)])
    document.modelspace().add_linear_dim(base=(0, 5), p1=(0, 0), p2=(5, 0)).render()
    document.saveas(path)
    plan = parse(path)
    assert any(label.text == "5" for label in plan.drawing_labels)


def test_floor_plan_analysis_sends_rendered_image_and_never_surface_cad_tags(environment, tmp_path):
    client, projects, evidence = environment
    state = imported_project(client, tmp_path)

    class Vision:
        def invoke(self, messages):
            image = messages[1].content[1]["image_url"]["url"]
            assert image.startswith("data:image/png;base64,")
            preview = base64.b64decode(image.split(",", 1)[1])
            assert preview.startswith(b"\x89PNG")
            assert "DLX_APERT" not in messages[1].content[0]["text"]
            return type("Response", (), {"content": '{"summary":"识别到会议室","spaces":[],"recognized_features":[],"scale_basis":"CAD 单位","design_implications":[],"clarifications":[]}'} )()

    tool = next(item for item in make_tools(projects=projects, evidence=evidence, dialux=object(),
                                            project_id=state["project_id"], vision_model=Vision())
                if item.name == "analyze_floor_plan")
    result = tool.invoke({})
    assert result["status"] == "vision_analyzed"
    assert result["analysis"]["summary"] == "识别到会议室"


def test_floor_plan_analysis_reports_non_vision_model_without_fabricating(environment, tmp_path):
    client, projects, evidence = environment
    state = imported_project(client, tmp_path)
    tool = next(item for item in make_tools(projects=projects, evidence=evidence, dialux=object(),
                                            project_id=state["project_id"]) if item.name == "analyze_floor_plan")
    result = tool.invoke({})
    assert result["status"] == "vision_unavailable"
    assert "不能声称" in result["message"]


def test_xref_and_unknown_units_are_visible(tmp_path):
    document, path = cad(tmp_path, units=0)
    block = document.blocks.new("missing_xref")
    block.block.dxf.flags = 4
    document.modelspace().add_blockref("missing_xref", (0, 0))
    document.saveas(path)
    plan = parse(path)
    assert not plan.read_complete
    assert plan.external_references == ["missing_xref"]
    assert plan.spatial_model.meters_per_unit is None
    assert not plan.spatial_model.design_ready


def make_pdf(tmp_path, mixed_same_page=False):
    path = tmp_path / "mixed.pdf"
    with fitz.open() as image_pdf:
        page = image_pdf.new_page(width=200, height=200)
        page.insert_text((10, 30), "Scanned criterion")
        image = page.get_pixmap().tobytes("png")
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((30, 40), "Native illuminance >= 300 lx.")
        if mixed_same_page:
            page.insert_image(fitz.Rect(30, 90, 230, 290), stream=image)
        else:
            document.new_page().insert_image(fitz.Rect(0, 0, 500, 700), stream=image)
            document.new_page().insert_text((30, 40), "Third page retained.")
        document.save(path)
    return path


class FakeOCR:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def extract_pdf(self, path):
        with fitz.open(path) as pdf:
            self.calls.append(len(pdf))
            assert len(pdf) == 1
        if self.fail:
            raise DocumentLoadError("remote unavailable")
        return "| Space | Illuminance lx |\n|---|---|\n| Office | >= 500 |\nFootnote: maintained."


def test_mixed_pdf_extracts_each_scanned_page_with_physical_locators(tmp_path):
    path = make_pdf(tmp_path)
    ocr = FakeOCR()
    parsed = load_document(path, allowed_root=tmp_path, ocr_client=ocr)
    assert ocr.calls == [1]
    assert parsed.page_count == 3
    assert [p.page_number for p in parsed.pages] == [1, 2, 3]
    assert parsed.pages[1].status == "ocr_review"
    assert parsed.pages[1].tables[0].cells[1] == ["Office", ">= 500"]
    assert parsed.pages[0].blocks[0].bbox is not None
    assert parsed.pages[1].blocks[0].bbox is None
    store = LocalEvidenceStore(tmp_path / "evidence.db")
    store.add_document(parsed, source_type="standard")
    chunks = store.get_document_chunks(parsed.sha256)
    assert len(chunks) == 3
    assert chunks[1].locator.startswith("第 2 页")
    assert "Footnote" in chunks[1].content
    assert "Third page" not in chunks[1].content
    assert store.get_document_artifact(parsed.sha256)["review_required"]


def test_native_text_does_not_hide_scan_on_same_page(tmp_path):
    path = make_pdf(tmp_path, mixed_same_page=True)
    ocr = FakeOCR()
    parsed = load_document(path, allowed_root=tmp_path, ocr_client=ocr)
    assert len(ocr.calls) == 1
    assert "Native illuminance" in parsed.content
    assert "Footnote" in parsed.content


def test_failed_ocr_preserves_page_gap_instead_of_claiming_complete(tmp_path):
    path = make_pdf(tmp_path)
    parsed = load_document(path, allowed_root=tmp_path, ocr_client=FakeOCR(fail=True))
    assert parsed.pages[1].page_number == 2
    assert parsed.pages[1].status == "needs_review"
    store = LocalEvidenceStore(tmp_path / "evidence.db")
    store.add_document(parsed)
    artifact = store.get_document_artifact(parsed.sha256)
    assert not artifact["extraction_complete"]
    assert len(artifact["pages"]) == 3


@pytest.fixture
def environment(tmp_path):
    projects = ProjectStore(tmp_path / "projects")
    evidence = LocalEvidenceStore(tmp_path / "evidence.db")
    client = TestClient(create_app(project_store=projects, evidence_store=evidence, user_documents_directory=tmp_path / "docs"))
    return client, projects, evidence


def imported_project(client, tmp_path):
    state = client.post("/api/projects", json={"project_name": "Review demo"}).json()
    _, path = cad(tmp_path, [(0, 0)])
    response = client.post(f"/api/projects/{state['project_id']}/floor-plan", data={"expected_revision": state["revision"]}, files={"file": ("rooms.dxf", path.read_bytes())})
    assert response.status_code == 201, response.text
    return response.json()["project"]


def model_payload(state, *, complete=True):
    model = state["floor_plan"]["spatial_model"]
    rooms = model["rooms"]
    if complete:
        for index, room in enumerate(rooms):
            room.update(floor="1F", number=str(index + 1), name=f"Office {index + 1}", usage="Office", elevation_m=0, height_m=3, ceiling_height_m=2.8, wall_reflectance=.5, ceiling_reflectance=.7, floor_reflectance=.2, status="confirmed")
    return {"expected_revision": state["revision"], "meters_per_unit": model["meters_per_unit"], "rooms": rooms, "elements": model["elements"], "coverage_confirmed": complete, "elements_reviewed": complete, "note": "Synthetic fixture assumptions explicitly confirmed for testing only"}


def registered_standard(client, *, project_id=None, verified=True):
    path = f"/api/projects/{project_id}/documents" if project_id else "/api/documents"
    response = client.post(path, files={"file": ("synthetic.md", b"| Space | Illuminance lx |\n|---|---|\n| Office | >= 500 |\nFootnote: test fixture, not a real standard.")})
    assert response.status_code == 201, response.text
    docs = client.get(path).json()
    response = client.post("/api/standards", json={"source_hash": docs[0]["source_hash"], "project_id": project_id, "number": "SYNTHETIC-001", "edition": "v1", "title": "Test only", "effective_date": "2020-01-01", "scope": "Office fixtures", "kind": "owner", "source": "Synthetic test fixture, no legal status", "source_verified": verified})
    assert response.status_code == 201, response.text
    return response.json()


def confirmed_rule(client, state, standard):
    root = f"/api/projects/{state['project_id']}"
    response = client.post(root + "/rules/candidates", json={"expected_revision": state["revision"], "standard_id": standard["standard_id"]})
    assert response.status_code == 200, response.text
    state = response.json()["project"]
    assert response.json()["generated"] == 1
    rule = state["rule_set"]["rules"][0]
    assert rule["status"] == "candidate"
    rule["evaluation_scope"] = "normal lighting"
    rule["conditions"].update(plane="working plane", workplane_height_m=.75, grid_x_m=.5, grid_y_m=.5, maintenance_factor=.8, additional="Fixture-only conditions explicitly reviewed")
    response = client.put(root + f"/rules/{rule['rule_id']}", json={"expected_revision": state["revision"], "rule": rule})
    assert response.status_code == 200, response.text
    state = response.json()
    response = client.post(root + f"/rules/{rule['rule_id']}/confirm", json={"expected_revision": state["revision"], "reviewer": "Fixture reviewer", "note": "Checked original and footnotes; synthetic only"})
    assert response.status_code == 200, response.text
    return response.json()


def test_model_review_binding_and_cad_replacement_invalidate_dependencies(environment, tmp_path):
    client, projects, _ = environment
    state = imported_project(client, tmp_path)
    root = f"/api/projects/{state['project_id']}"
    response = client.put(root + "/spatial-model", json=model_payload(state))
    assert response.status_code == 200, response.text
    state = response.json()
    assert state["floor_plan"]["spatial_model"]["design_ready"]
    assert state["floor_plan"]["spatial_model"]["rooms"][0]["provenance"]["height_m"]["source"] == "user_assumption"
    standard = registered_standard(client)
    state = confirmed_rule(client, state, standard)
    response = client.post(root + "/rules/bind", json={"expected_revision": state["revision"], "reviewer": "Reviewer", "note": "Fixture applicability", "coverage_confirmed": True})
    assert response.status_code == 200, response.text
    state = response.json()
    mapping = client.get(root + "/design-settings").json()
    assert mapping["status"] == "ready_for_dialux_setup"
    assert not mapping["simulation_performed"]
    assert mapping["compliance_status"] == "not_evaluated"
    assert mapping["rooms"][0]["conditions"]["grid_x_m"] == .5
    _, path = cad(tmp_path, [(100, 0)])
    response = client.post(root + "/floor-plan", data={"expected_revision": state["revision"]}, files={"file": ("replacement.dxf", path.read_bytes())})
    assert response.status_code == 201, response.text
    replaced = response.json()["project"]
    assert replaced["rule_set"]["status"] == "stale"
    assert not replaced["floor_plan"]["spatial_model"]["design_ready"]
    assert replaced["floor_plan"]["spatial_model"]["rooms"][0]["status"] == "pending"
    assert client.get(root + "/design-settings").json()["status"] == "needs_review"
    assert projects.revisions(state["project_id"])[-2].rule_set.status == "bound"


def test_missing_data_cannot_be_auto_confirmed_and_private_standards_do_not_leak(environment, tmp_path):
    client, _, _ = environment
    state = imported_project(client, tmp_path)
    root = f"/api/projects/{state['project_id']}"
    standard = registered_standard(client, project_id=state["project_id"])
    response = client.post(root + "/rules/candidates", json={"expected_revision": state["revision"], "standard_id": standard["standard_id"]})
    state = response.json()["project"]
    rule = state["rule_set"]["rules"][0]
    response = client.post(root + f"/rules/{rule['rule_id']}/confirm", json={"expected_revision": state["revision"], "reviewer": "Reviewer", "note": "missing conditions"})
    assert response.status_code == 422
    assert client.get("/api/standards").json() == []
    other = client.post("/api/projects", json={"project_name": "Other"}).json()
    assert client.get(f"/api/standards/{standard['standard_id']}/evidence", params={"project_id": other["project_id"]}).status_code == 404
    assert client.get(f"/api/standards/{standard['standard_id']}/source", params={"project_id": other["project_id"]}).status_code == 404


def test_no_silent_room_deletion_invalid_polygons_or_stale_overwrites(environment, tmp_path):
    client, _, _ = environment
    state = imported_project(client, tmp_path)
    root = f"/api/projects/{state['project_id']}"
    payload = model_payload(state)
    payload["rooms"] = []
    assert client.put(root + "/spatial-model", json=payload).status_code == 422
    payload = model_payload(state)
    payload["rooms"][0]["boundary"] = [{"x": x, "y": y} for x, y in [(0, 0), (2, 2), (2, 0), (0, 2)]]
    assert client.put(root + "/spatial-model", json=payload).status_code == 422
    payload["expected_revision"] = 0
    assert client.put(root + "/spatial-model", json=payload).status_code == 409


def test_confirmed_rule_edits_reset_review_and_evidence_is_immutable(environment, tmp_path):
    client, _, _ = environment
    state = imported_project(client, tmp_path)
    state = confirmed_rule(client, state, registered_standard(client))
    rule = state["rule_set"]["rules"][0]
    root = f"/api/projects/{state['project_id']}/rules/{rule['rule_id']}"
    rule["evidence_text"] += "fake clause"
    assert client.put(root, json={"expected_revision": state["revision"], "rule": rule}).status_code == 422
    rule["evidence_text"] = rule["evidence_text"].removesuffix("fake clause")
    rule["threshold"] = 300
    updated = client.put(root, json={"expected_revision": state["revision"], "rule": rule}).json()
    assert updated["rule_set"]["rules"][0]["status"] == "candidate"
    assert updated["rule_set"]["rules"][0]["reviewer"] is None


def test_registered_version_survives_reindex_and_restricts_source_deletion(environment):
    client, _, evidence = environment
    standard = registered_standard(client)
    artifact = evidence.get_document_artifact(standard["source_hash"])
    document = load_document(artifact["source_path"], allowed_root=Path(artifact["source_path"]).parent)
    evidence.add_document(document, source_type="standard")
    assert evidence.list_standards()[0].standard_id == standard["standard_id"]
    assert client.get(f"/api/standards/{standard['standard_id']}/source").status_code == 200


def test_identical_xy_on_different_storeys_does_not_merge(tmp_path):
    document, path = cad(tmp_path, [(0, 0)])
    document.modelspace().add_lwpolyline([(0, 0), (5, 0), (5, 4), (0, 4)], close=True, dxfattribs={"layer": "WALL", "elevation": 3})
    document.saveas(path)
    plan = parse(path)
    assert len(plan.spatial_model.rooms) == 2
    assert {r.elevation_m for r in plan.spatial_model.rooms} == {0, 3}
    assert len({r.room_id for r in plan.spatial_model.rooms}) == 2


def test_conflicting_rules_are_not_silently_combined_or_strictest_selected():
    a = DesignRule(standard_id="a", locator="p1", evidence_text="test", evidence_key="a", metric="illuminance", operator=">=", threshold=300, unit="lx", applies_to=["Office"], status="confirmed")
    b = a.model_copy(deep=True, update={"rule_id": "different", "standard_id": "b", "threshold": 500})
    assert rule_conflicts([a, b])
    b.evaluation_scope = "emergency"
    assert not rule_conflicts([a, b])
    b.evaluation_scope = None
    b.applies_to = ["Corridor"]
    assert not rule_conflicts([a, b])
    b.applies_to = ["Office"]
    b.threshold = 300
    a.conditions.plane = "desk"
    b.conditions.plane = "floor"
    assert rule_conflicts([a, b])


def test_unsupported_threshold_or_reversed_comparison_cannot_confirm(environment, tmp_path):
    client, _, _ = environment
    state = confirmed_rule(client, imported_project(client, tmp_path), registered_standard(client))
    rule = state["rule_set"]["rules"][0]
    root = f"/api/projects/{state['project_id']}/rules/{rule['rule_id']}"
    for changes in ({"threshold": 300}, {"threshold": 500, "operator": "<="}):
        rule.update(changes)
        response = client.put(root, json={"expected_revision": state["revision"], "rule": rule})
        assert response.status_code == 200
        state = response.json()
        response = client.post(root + "/confirm", json={"expected_revision": state["revision"], "reviewer": "Reviewer", "note": "test altered rule"})
        assert response.status_code == 422


def test_long_markdown_tables_keep_headers_across_chunk_boundaries(tmp_path):
    source = tmp_path / "long.md"
    source.write_text("| Space | Illuminance lx |\n|---|---|\n" + "\n".join(f"| Office {i} | >= 500 |" for i in range(85)), encoding="utf-8")
    parsed = load_document(source, allowed_root=tmp_path)
    assert len(parsed.pages[0].tables[0].cells) == 86
    assert parsed.pages[0].tables[0].cells[0] == ["Space", "Illuminance lx"]


def test_tampered_source_file_blocks_confirmation_and_download(environment, tmp_path):
    client, _, evidence = environment
    state = imported_project(client, tmp_path)
    standard = registered_standard(client)
    artifact = evidence.get_document_artifact(standard["source_hash"])
    Path(artifact["source_path"]).write_text("changed after registration", encoding="utf-8")
    assert client.get(f"/api/standards/{standard['standard_id']}/source").status_code == 409
    response = client.post(f"/api/projects/{state['project_id']}/rules/candidates", json={"expected_revision": state["revision"], "standard_id": standard["standard_id"]})
    assert response.status_code == 409


def test_ocr_preserves_all_blocks_html_tables_and_declared_coordinate_space():
    payload = {"coordinate_space": "image_pixels", "results": [
        {"block_content": "First paragraph", "block_bbox": [1, 2, 100, 30]},
        {"block_content": "Important footnote", "block_bbox": [1, 50, 100, 70]},
    ]}
    assert _extract_text(payload) == "First paragraph\n\nImportant footnote"
    blocks = _ocr_layout(payload, "p2")
    assert len(blocks) == 2
    assert blocks[0].bbox is None  # pixel coordinates must not be mislabeled PDF points
    assert blocks[0].source_bbox == (1, 2, 100, 30)
    payload["coordinate_space"] = "pdf_points"
    assert _ocr_layout(payload, "p2")[0].bbox == (1, 2, 100, 30)
    table = _markdown_tables("<table><tr><th>Space</th><th>Illuminance lx</th></tr><tr><td rowspan='2'>Office</td><td>500</td></tr><tr><td>300</td></tr></table>", "p1")[0]
    assert table.cells == [["Space", "Illuminance lx"], ["Office", "500"], ["Office", "300"]]


def test_native_pdf_table_has_table_and_cell_positions(tmp_path):
    path = tmp_path / "table.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page(width=400, height=300)
        for x in (30, 170, 350):
            page.draw_line((x, 30), (x, 150))
        for y in (30, 70, 110, 150):
            page.draw_line((30, y), (350, y))
        for text, point in [("Space", (40, 55)), ("Illuminance lx", (180, 55)), ("Office", (40, 95)), (">= 500", (180, 95)), ("Corridor", (40, 135)), (">= 100", (180, 135))]:
            page.insert_text(point, text)
        pdf.save(path)
    ocr = FakeOCR()
    parsed = load_document(path, allowed_root=tmp_path, ocr_client=ocr)
    assert ocr.calls == []
    assert parsed.pages[0].tables[0].cells[1] == ["Office", ">= 500"]
    assert parsed.pages[0].tables[0].bbox is not None
    assert parsed.pages[0].tables[0].cell_bboxes


def test_glare_and_workplane_use_separate_targets_not_false_conflicts():
    a = DesignRule(standard_id="a", locator="p1", evidence_text="fixture", evidence_key="a", metric="illuminance", operator=">=", threshold=300, unit="lx", applies_to=["Office"], evaluation_scope="normal", status="confirmed")
    a.conditions.plane = "desk"
    b = a.model_copy(deep=True, update={"rule_id": "ugr", "metric": "ugr", "operator": "<=", "threshold": 19})
    b.conditions.plane = "observer"
    assert not rule_conflicts([a, b])


def test_cad_replacement_preserves_explicit_project_usage(environment, tmp_path):
    client, projects, _ = environment
    state = client.post("/api/projects", json={"project_name": "Explicit", "space_type": "Office"}).json()
    document, path = cad(tmp_path, [(0, 0)])
    document.modelspace().add_text("Office", dxfattribs={"insert": (1, 1)})
    document.saveas(path)
    for _ in range(2):
        response = client.post(f"/api/projects/{state['project_id']}/floor-plan", data={"expected_revision": state["revision"]}, files={"file": ("office.dxf", path.read_bytes())})
        assert response.status_code == 201
        state = response.json()["project"]
    assert projects.get(state["project_id"]).brief.space_type == "Office"
