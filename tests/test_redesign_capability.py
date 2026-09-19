from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from shapely.geometry import Polygon

from lighting_agent.calculations.field import load_fixture_kind
from lighting_agent.calculations.photometry import PhotometryParseError, parse_photometry_file
from lighting_agent.deliverables import build_redesign_package
from lighting_agent.dialux_report import parse_dialux_report
from lighting_agent.photometry_assets import PhotometryAssetStore
from lighting_agent.project_store import ProjectStore
from lighting_agent.rag import LocalEvidenceStore
from lighting_agent.redesign_service import RelayoutRequest, RetrofitRequest, run_relayout, run_retrofit
from lighting_agent.relayout import plan_relayout
from lighting_agent.retrofit import plan_retrofit
from lighting_agent.schemas import DesignBrief, DesignFixtureSpec
from lighting_agent.web_api import create_app


ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = ROOT / "experiments" / "dxf_redesign"


class NoDownload:
    def download_photometry_zip(self, _detail_url: str):
        raise AssertionError("local fixture paths must not access the network")


def _golden_design() -> dict:
    return json.loads((EXPERIMENT / "out" / "design.json").read_text(encoding="utf-8"))


def _golden_products(design: dict):
    spec = json.loads((EXPERIMENT / "finder" / "design_products.json").read_text(encoding="utf-8"))
    panels: list[dict] = []
    downlights: list[dict] = []
    for group, target in (("panels", panels), ("downlights", downlights)):
        for item in spec[group]:
            target.append(
                {
                    "key": item["key"],
                    "role": "existing" if "EXIST" in item["key"] else "candidate",
                    "watts": item["watts"],
                    "kind": load_fixture_kind(
                        EXPERIMENT / item["file"], item["label"], item["flux_lm"], item["mf"]
                    ),
                }
            )
    return panels, downlights


def test_standard_ldt_absolute_ies_and_type_b() -> None:
    panel = parse_photometry_file(EXPERIMENT / "light" / "AERMLED3HO60CW.ldt", "ldt")
    pak = parse_photometry_file(
        EXPERIMENT / "finder" / "photometry" / "NmXYVOTdSYeZYTKkU_N0dA" / "240459.ldt",
        "ldt",
    )
    absolute = parse_photometry_file(
        EXPERIMENT / "finder" / "photometry" / "Ty4CMIJRRISl4p-5riYvzw" / "5105.ies",
        "ies",
    )
    assert panel.declared_flux_lm == pytest.approx(4379.2)
    assert panel.power_w == pytest.approx(31.25)
    assert pak.declared_flux_lm == pytest.approx(8686.47)
    assert pak.power_w == pytest.approx(80)
    assert absolute.absolute_flux_declared is True
    assert absolute.declared_flux_lm is None
    assert absolute.total_flux_lm() == pytest.approx(7510.6, rel=0.001)

    content = (
        "IESNA:LM-63-2002\nTILT=NONE\n"
        "1 1000 1 2 1 2 2 1 1 0\n1 1 10\n0 90\n0\n100 0\n"
    )
    with pytest.raises(PhotometryParseError, match="Type B"):
        from lighting_agent.calculations.photometry import parse_photometry

        parse_photometry(content, "ies")


def test_real_dialux_report_gate_values() -> None:
    report = parse_dialux_report(EXPERIMENT / "report" / "512会议室_说明_报告.pdf")
    assert report["summary"]["maintenance_factor"] == 0.8
    assert report["summary"]["workplane_height_m"] == 0.75
    assert report["summary"]["mounting_height_range_m"] == [3.405, 4.173]
    assert report["results"]["mean_lx"] == 599
    assert report["results"]["target_lx"] == 750
    assert report["results"]["uo"] == pytest.approx(0.099)
    assert report["results"]["uo_target"] == pytest.approx(0.60)
    assert [item["count"] for item in report["luminaires"]] == [20, 16]
    assert report["totals"]["flux_lm"] == 98120
    assert report["totals"]["watts"] == pytest.approx(699.2)
    assert len(report["positions"]["panels"]) == 20
    assert len(report["positions"]["downlights"]) == 16


def test_relayout_and_retrofit_golden_results() -> None:
    design = _golden_design()
    room = Polygon(design["room"]["exterior"], design["room"]["interiors"])
    points = design["eval_grid"]["points"]
    design_kind = load_fixture_kind(
        EXPERIMENT / "finder" / "photometry" / "Ty4CMIJRRISl4p-5riYvzw" / "5105.ies",
        "J6012",
        7510.6,
        0.8,
    )
    relayout = plan_relayout(
        room,
        points,
        design_kind,
        target_lux=750,
        mounting_height_m=3.405,
        workplane_height_m=0.75,
        calibration_scale=1.2643,
        watts_per_fixture=70,
    )
    assert relayout["recommended"]["name"] == "P24_3x8"
    assert relayout["recommended"]["metrics"]["Em"] == pytest.approx(877.9, rel=0.03)
    assert relayout["recommended"]["metrics"]["Uo"] == pytest.approx(0.389, abs=0.05)

    panels, downlights = _golden_products(design)
    retrofit = plan_retrofit(
        points,
        design["luminaires"]["panel"],
        design["luminaires"]["downlight"],
        panels,
        downlights,
        target_lux=750,
        calibration_scale=1.2643,
        area_m2=room.area,
        panel_height_m=3.405,
        downlight_height_m=4.173,
        workplane_height_m=0.75,
    )
    assert retrofit["recommended"]["name"] == "AERMLED_EXIST + CDN40_55"
    assert retrofit["recommended"]["metrics"]["Em"] == pytest.approx(816.1, rel=0.03)
    assert retrofit["required_panel_flux"]["required_flux_lm"] == pytest.approx(5637, rel=0.02)
    first_meeting = next(item for item in retrofit["partial_sweep"] if item["metrics"]["Em"] >= 750)
    assert first_meeting["k"] == 12
    assert retrofit["minimal_partial"]["k"] == 13


def test_redesign_service_persists_run_and_builds_verified_package(tmp_path, monkeypatch) -> None:
    store = ProjectStore(tmp_path)
    state = store.create(DesignBrief(project_name="Golden redesign", target_illuminance_lx=750))
    design = _golden_design()
    dxf_path = tmp_path / "room.dxf"
    dxf_path.write_text("placeholder", encoding="utf-8")
    design["source"] = {
        "path": str(dxf_path),
        "sha256": hashlib.sha256(dxf_path.read_bytes()).hexdigest(),
        "size_bytes": dxf_path.stat().st_size,
        "dxfversion": "AC1032",
    }
    monkeypatch.setattr("lighting_agent.redesign_service.extract_design", lambda _path: design)
    shutil.copy2(EXPERIMENT / "report" / "512会议室_说明_报告.pdf", tmp_path / "report.pdf")
    shutil.copy2(
        EXPERIMENT / "finder" / "photometry" / "Ty4CMIJRRISl4p-5riYvzw" / "5105.ies",
        tmp_path / "design.ies",
    )
    assets = PhotometryAssetStore(tmp_path, NoDownload())
    request = RelayoutRequest(
        expected_revision=state.revision,
        dxf_source="room.dxf",
        report_source="report.pdf",
        calibration_scale=1.2643,
        design_fixtures=[
            DesignFixtureSpec(
                role="candidate", label="J6012", local_path="design.ies",
                file_type="ies", flux_lm=7700, watts=70,
            )
        ],
    )
    run, updated = run_relayout(store, tmp_path, assets, state.project_id, request)
    assert run.result["recommended"]["name"] == "P24_3x8"
    assert run.result["verdict"]["meets"] is True
    assert updated.revision == 1
    assert updated.design_runs[-1].run_id == run.run_id
    package = build_redesign_package(tmp_path, run)
    with zipfile.ZipFile(BytesIO(package)) as archive:
        names = set(archive.namelist())
        assert {
            "redesign-package.json", "redesign-report.md", "candidates.csv",
            "recommended_layout.csv", "layout_compare.png", "heatmap_compare.png",
        } <= names
        manifest = json.loads(archive.read("redesign-package.json"))
        assert manifest["run"]["run_id"] == run.run_id


def test_retrofit_service_persists_golden_result(tmp_path, monkeypatch) -> None:
    store = ProjectStore(tmp_path)
    state = store.create(DesignBrief(project_name="Golden retrofit", target_illuminance_lx=750))
    design = _golden_design()
    dxf_path = tmp_path / "room.dxf"
    dxf_path.write_text("placeholder", encoding="utf-8")
    design["source"] = {
        "path": str(dxf_path), "sha256": hashlib.sha256(dxf_path.read_bytes()).hexdigest(),
        "size_bytes": dxf_path.stat().st_size, "dxfversion": "AC1032",
    }
    monkeypatch.setattr("lighting_agent.redesign_service.extract_design", lambda _path: design)
    shutil.copy2(EXPERIMENT / "report" / "512会议室_说明_报告.pdf", tmp_path / "report.pdf")
    files = {
        "existing-panel.ldt": EXPERIMENT / "light" / "AERMLED3HO60CW.ldt",
        "existing-downlight.ies": EXPERIMENT / "light" / "ERD2804W_RAD848F.ies",
        "cdn40.ies": EXPERIMENT / "finder" / "photometry" / "0FIIi7cdStm2Mz1L6FZafQ" / "82542.ies",
    }
    for name, source in files.items():
        shutil.copy2(source, tmp_path / name)
    fixture = lambda role, label, path, flux, watts: DesignFixtureSpec(
        role=role, label=label, local_path=path, flux_lm=flux, watts=watts,
        maintenance_factor=0.8,
    )
    request = RetrofitRequest(
        expected_revision=0, dxf_source="room.dxf", report_source="report.pdf",
        calibration_scale=1.2643,
        existing_panel=fixture("existing", "AERMLED", "existing-panel.ldt", 4379.2, 30),
        existing_downlight=fixture("existing", "ERD", "existing-downlight.ies", 750, 6.2),
        candidate_downlights=[fixture("candidate", "CDN40", "cdn40.ies", 2958, 38.917)],
    )
    run, updated = run_retrofit(
        store, tmp_path, PhotometryAssetStore(tmp_path, NoDownload()), state.project_id, request
    )
    assert run.result["recommended"]["metrics"]["Em"] == pytest.approx(816.1, rel=0.03)
    assert run.result["minimal_partial"]["k"] == 13
    assert updated.design_runs[-1].mode == "retrofit"


def test_redesign_web_endpoint_returns_history_and_package(tmp_path, monkeypatch) -> None:
    store = ProjectStore(tmp_path / "projects")
    state = store.create(DesignBrief(project_name="Web redesign", target_illuminance_lx=750))
    root = store.directory
    design = _golden_design()
    dxf_path = root / "room.dxf"
    dxf_path.write_text("placeholder", encoding="utf-8")
    design["source"] = {
        "path": str(dxf_path), "sha256": hashlib.sha256(dxf_path.read_bytes()).hexdigest(),
        "size_bytes": dxf_path.stat().st_size, "dxfversion": "AC1032",
    }
    monkeypatch.setattr("lighting_agent.redesign_service.extract_design", lambda _path: design)
    shutil.copy2(EXPERIMENT / "report" / "512会议室_说明_报告.pdf", root / "report.pdf")
    shutil.copy2(
        EXPERIMENT / "finder" / "photometry" / "Ty4CMIJRRISl4p-5riYvzw" / "5105.ies",
        root / "design.ies",
    )
    app = create_app(
        project_store=store,
        evidence_store=LocalEvidenceStore(tmp_path / "rag.json"),
        dialux_api=NoDownload(),
    )
    client = TestClient(app)
    response = client.post(
        f"/api/projects/{state.project_id}/redesign/relayout",
        json={
            "expected_revision": 0,
            "dxf_source": "room.dxf",
            "report_source": "report.pdf",
            "calibration_scale": 1.2643,
            "design_fixtures": [
                {
                    "role": "candidate", "label": "J6012", "local_path": "design.ies",
                    "file_type": "ies", "flux_lm": 7700, "watts": 70,
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    run_id = response.json()["run"]["run_id"]
    history = client.get(f"/api/projects/{state.project_id}/redesign/runs")
    assert history.status_code == 200
    assert history.json()["runs"][0]["run_id"] == run_id
    package = client.get(f"/api/projects/{state.project_id}/redesign/runs/{run_id}/package")
    assert package.status_code == 200
    assert package.headers["content-type"].startswith("application/zip")
