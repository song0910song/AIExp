from __future__ import annotations

from io import BytesIO
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from fastapi.testclient import TestClient

from lighting_agent.calculations.photometry import (
    PhotometryParseError,
    parse_photometry,
)
from lighting_agent.calculations.preview import (
    IlluminancePreviewRequest,
    PreviewGeometryError,
    compute_illuminance_preview,
)
from lighting_agent.photometry_assets import PhotometryAssetStore
from lighting_agent.project_store import ProjectStore
from lighting_agent.rag import LocalEvidenceStore
from lighting_agent.schemas import LuminaireCandidate, ProjectState
from lighting_agent.web_api import create_app


class FakeDialux:
    def search(self, _request: Any) -> list[LuminaireCandidate]:
        return [
            LuminaireCandidate(
                luminaire_id="fixture-1",
                article_name="DL-01",
                brand_name="Example",
                power_w=24,
                luminous_flux_lm=3000,
                cct_k=4000,
                cri=90,
                detail_url="https://example.test/fixture-1",
                has_photometry_download=True,
                matching_status="matches",
            )
        ]


def make_client(tmp_path) -> TestClient:
    app = create_app(
        project_store=ProjectStore(tmp_path / "projects"),
        evidence_store=LocalEvidenceStore(tmp_path / "rag.json"),
        dialux_api=FakeDialux(),
    )
    return TestClient(app)


class FixedPayloadDownloader:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[str] = []

    def download_photometry_zip(self, detail_url: str) -> tuple[str, bytes]:
        self.calls.append(detail_url)
        return f"{detail_url}/download.zip", self.payload


def _vendor_zip(ies_text: str) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("fixture.ies", ies_text)
        archive.writestr("fixture.uld", "photometry")
    return buffer.getvalue()


def build_rotationally_symmetric_ies(
    *,
    lumens_per_lamp: str = "0",
    extra_header_tokens: str = "",
    peak_cd: float = 1000.0,
) -> str:
    """A single C plane with a cosine vertical spread (per-1000-lm basis)."""

    gammas = [0, 15, 30, 45, 60, 75, 90, 105, 120, 135, 150, 165, 180]
    import math as _math

    row = [
        round(max(0.0, peak_cd * _math.cos(_math.radians(g))) + (0.5 if g >= 90 else 0.0), 2)
        for g in gammas
    ]
    header = f"1 {lumens_per_lamp} 1 {len(gammas)} 1 1 2 0.6 0.6 0.08"
    if extra_header_tokens:
        header = f"{header} {extra_header_tokens}"
    lines = [
        "IESNA:LM-63-2002",
        "[TEST] PREVIEW",
        "[LAMP] LED 36W",
        "[TILT]=NONE",
        header,
        " ".join(str(value) for value in gammas),
        "0",
        " ".join(str(value) for value in row),
    ]
    return "\n".join(lines) + "\n"


def build_simple_ldt() -> str:
    lines = [
        "Example manufacturer",
        "LED module 36W",
        "Report fixture-1 v2",
        "1",
        "1",
        "3000",
        "0",
        "1",
        "0",
        "2",
        "180",
        "3",
        "0",
        "45",
        "90",
        "65",
        "100",
        "0",
        "0",
        "0",
        "0",
        "0",
        "1000",
        "3600",
        "2100",
        "400",
        "1000",
        "3600",
        "2100",
        "400",
    ]
    return "\n".join(lines) + "\n"


def test_parse_ies_rotational_symmetry_and_flux_basis() -> None:
    text = build_rotationally_symmetric_ies()
    distribution = parse_photometry(text, "ies")

    assert distribution.format == "ies"
    assert distribution.photometric_system == "C"
    assert distribution.lamp_count == 1
    assert distribution.declared_flux_lm is None
    assert distribution.absolute_flux_declared is False
    assert len(distribution.gamma_angles_deg) == 13
    assert distribution.intensity_cd[0][0] == pytest.approx(1000.0, abs=0.01)
    assert distribution.is_symmetric_about_vertical_axis()


def test_parse_ies_reads_ballast_and_input_watts_extras() -> None:
    text = build_rotationally_symmetric_ies(
        lumens_per_lamp="3000", extra_header_tokens="1 0 36"
    )
    distribution = parse_photometry(text, "ies")

    assert distribution.declared_flux_lm == pytest.approx(3000)
    assert distribution.absolute_flux_declared is True
    assert distribution.power_w == pytest.approx(36)
    # The angle table must not shift when extras are consumed.
    assert distribution.gamma_angles_deg[0] == 0
    assert distribution.gamma_angles_deg[-1] == 180


def test_parse_ies_rejects_non_c_system() -> None:
    lines = [
        "IESNA:LM-63-2002",
        "[TILT]=NONE",
        "1 3000 1 3 2 3 2 0.6 0.6 0.08",
        "0 30 60",
        "0 45",
        "10 20 30",
        "40 50 60",
    ]
    with pytest.raises(PhotometryParseError):
        parse_photometry("\n".join(lines), "ies")


def test_parse_ldt_declares_flux_power_and_symmetry() -> None:
    distribution = parse_photometry(build_simple_ldt(), "ldt")

    assert distribution.format == "ldt"
    assert distribution.lamp_count == 1
    assert distribution.declared_flux_lm == pytest.approx(3000)
    assert distribution.power_w == pytest.approx(36)
    assert [angle for angle in distribution.c_angles_deg] == [0.0, 180.0]
    assert distribution.is_symmetric_about_vertical_axis()
    summary = distribution.summary()
    assert summary["full_circle_coverage"] is True
    assert summary["integrated_flux_lm"] > 0


def test_preview_point_below_centre_matches_inverse_square() -> None:
    distribution = parse_photometry(build_rotationally_symmetric_ies(), "ies")
    request = IlluminancePreviewRequest(
        luminaire_id="fixture-1",
        room_length_m=4.0,
        room_width_m=4.0,
        workplane_height_m=0.75,
        mounting_height_m=2.7,
        total_flux_lm=3000.0,
        fixture_rows=1,
        fixture_columns=1,
    )

    result = compute_illuminance_preview(distribution, request)

    height_above_plane = 2.7 - 0.75
    intensity_at_nadir = (
        distribution.intensity_cd[0][0] * 3000.0 / 1000.0
    )
    expected_centre = intensity_at_nadir / height_above_plane**2
    rows, columns = len(result.grid_y_coordinates_m), len(result.grid_x_coordinates_m)
    centre_row = rows // 2 - 1 if rows % 2 == 0 else rows // 2
    centre_column = columns // 2 - 1 if columns % 2 == 0 else columns // 2
    centre_value = result.illuminance_lx[centre_row][centre_column]
    # The centre grid point sits very near the fixture axis.
    assert result.maximum_illuminance_lx <= expected_centre + 1e-6
    assert centre_value == pytest.approx(expected_centre, rel=0.35)
    assert result.average_illuminance_lx > 0
    assert 0 < result.uniformity_u0 < 1
    assert result.calibration_scale == 1.0
    assert any("直射分量" in note for note in result.assumptions)


def test_preview_calibration_matches_lumen_method_average() -> None:
    distribution = parse_photometry(build_rotationally_symmetric_ies(), "ies")
    request = IlluminancePreviewRequest(
        luminaire_id="fixture-1",
        room_length_m=6.0,
        room_width_m=4.0,
        workplane_height_m=0.75,
        mounting_height_m=2.8,
        maintenance_factor=0.8,
        utilization_factor=0.55,
        total_flux_lm=2600.0,
        fixture_rows=2,
        fixture_columns=2,
    )

    result = compute_illuminance_preview(distribution, request)

    lumen_target = 2600.0 * 4 * 0.55 * 0.8 / (6.0 * 4.0)
    assert result.average_illuminance_lx == pytest.approx(lumen_target, rel=0.02)
    assert 0 < result.calibration_scale < 1
    assert result.installed_power_w is None


def test_preview_rejects_impossible_geometry() -> None:
    distribution = parse_photometry(build_rotationally_symmetric_ies(), "ies")
    request = IlluminancePreviewRequest(
        luminaire_id="fixture-1",
        room_length_m=5.0,
        room_width_m=4.0,
        workplane_height_m=0.75,
        mounting_height_m=0.7,
        total_flux_lm=2000.0,
    )
    with pytest.raises(PreviewGeometryError):
        compute_illuminance_preview(distribution, request)


def test_api_generates_and_invalidates_photometry_preview(tmp_path) -> None:
    client = make_client(tmp_path)
    created = client.post(
        "/api/projects",
        json={
            "project_name": "预览会议室",
            "space_type": "会议室",
            "area_m2": 24,
            "length_m": 6,
            "width_m": 4,
            "lighting_groups": [{
                "group_id": "group-0001",
                "region_name": "Meeting room",
                "group_name": "General lighting",
                "area_m2": 24,
                "mounting_height_m": 2.8,
                "target_illuminance_lx": 300,
                "utilization_factor": 0.55,
                "maintenance_factor": 0.8,
                "confirmed": True,
            }],
            "target_illuminance_lx": 300,
            "workplane_height_m": 0.75,
        },
    )
    assert created.status_code == 201
    project = created.json()

    searched = client.post(
        f"/api/projects/{project['project_id']}/luminaires",
        json={"keyword": "downlight", "expected_revision": project["revision"], "max_results": 1},
    )
    assert searched.status_code == 200
    updated_project = searched.json()["project"]

    selected = client.put(
        f"/api/projects/{project['project_id']}/selected-luminaires",
        json={
            "expected_revision": updated_project["revision"],
            "luminaire_ids": ["fixture-1"],
            "group_assignments": {"group-0001": ["fixture-1"]},
        },
    )
    assert selected.status_code == 200

    # Install a downloaded photometry asset through the real asset store.
    projects_dir = tmp_path / "projects"
    asset_store = PhotometryAssetStore(
        projects_dir,
        FixedPayloadDownloader(_vendor_zip(build_rotationally_symmetric_ies())),
    )
    detail = client.get(f"/api/projects/{project['project_id']}")
    state_model = ProjectState.model_validate(detail.json())
    asset = asset_store.download(state_model, "fixture-1")
    assert asset.status == "downloaded"

    parsed = client.get(
        f"/api/projects/{project['project_id']}/luminaires/fixture-1/photometry/parse"
    )
    assert parsed.status_code == 200
    summary = parsed.json()["summary"]
    assert summary["format"] == "ies"
    assert summary["symmetric_about_vertical_axis"] is True

    generated = client.post(
        f"/api/projects/{project['project_id']}/photometry-preview",
        json={
            "expected_revision": detail.json()["revision"],
            "luminaire_id": "fixture-1",
            "lighting_group_id": "group-0001",
            "fixture_rows": 2,
            "fixture_columns": 3,
        },
    )
    assert generated.status_code == 200
    preview_payload = generated.json()["preview"]
    assert preview_payload["kind"] == "preview"
    assert preview_payload["result"]["average_illuminance_lx"] > 0
    assert preview_payload["result"]["limitations"]

    stored = client.get(f"/api/projects/{project['project_id']}/photometry-preview")
    assert stored.status_code == 200
    body = stored.json()
    assert body["is_current"] is True
    assert body["preview"]["input_snapshot_sha256"] == preview_payload["input_snapshot_sha256"]

    # A brief update marks the stored preview stale (revision advances too).
    bumped = client.put(
        f"/api/projects/{project['project_id']}/brief",
        json={
            "expected_revision": detail.json()["revision"],
            "brief": {**detail.json()["brief"], "notes": "增加备注以推进版本"},
        },
    )
    assert bumped.status_code == 200
    after_bump = client.get(f"/api/projects/{project['project_id']}/photometry-preview")
    assert after_bump.json()["is_current"] is False
    assert after_bump.json()["stale_reasons"]
