from __future__ import annotations

import json
from pathlib import Path

import ezdxf
import pytest

from lighting_agent.dialux_runner.artifacts import RunError, RunStore, sha256, validate_job
from lighting_agent.dialux_runner.generic_jobs import (
    GENERIC_PROFILE, auto_layout, generate_layout_candidates, geometry_mode, prepare_generic_job, prepare_reviewed_model,
)
from lighting_agent.dialux_runner.real_cad import prepare_real_cad_model
from lighting_agent.schemas import DialuxGeometryAssumptions, DialuxLayoutItem, DialuxOptimizationRequest, SpatialModel


FIXTURE = Path(__file__).parent / "fixtures" / "phase0"


@pytest.fixture
def generic_inputs(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    model = SpatialModel.model_validate_json((FIXTURE / "bridge-spatial-model.json").read_text(encoding="utf-8"))
    cad = ezdxf.new("R2010")
    cad.units = ezdxf.units.M
    cad.modelspace().add_lwpolyline([(p.x, p.y) for p in model.rooms[0].boundary], close=True,
                                    dxfattribs={"layer": "ROOM_BOUNDARY"})
    cad.modelspace().add_lwpolyline([(p.x, p.y) for p in model.elements[0].footprint], close=True,
                                    dxfattribs={"layer": "FURNITURE"})
    cad_path = source / "actual-plan.dxf"
    cad.saveas(cad_path)
    model.source_sha256 = sha256(cad_path)
    effective, readiness = prepare_reviewed_model(
        model, DialuxGeometryAssumptions(default_usage="Office"), default_usage="Office", confirm=True
    )
    assert readiness.can_prepare
    photometry = source / "fixture.ies"
    photometry.write_bytes((FIXTURE / "synthetic-bridge-luminaire.ies").read_bytes())
    executable = tmp_path / "DIALux_x64.exe"
    executable.touch()
    return source, effective, cad_path, photometry, executable


def test_generic_profile_accepts_reviewed_dxf_and_arbitrary_photometry(generic_inputs, tmp_path):
    _, model, cad_path, photometry, executable = generic_inputs
    job_path = prepare_generic_job(
        cad_path=cad_path, photometry_path=photometry, model=model,
        assumptions=DialuxGeometryAssumptions(default_usage="Office"),
        layout=[DialuxLayoutItem(room_id="room-1", position_m=[3, 2, 2.5])],
        optimization=DialuxOptimizationRequest(), project_name="Generic", project_id="generic",
        executable=executable, output=tmp_path / "job",
    )
    store = RunStore(job_path.parent)
    validate_job(store)
    assert store.job["profile"] == GENERIC_PROFILE
    assert store.job["settings"]["daylight"] is False
    assert store.job["photometry_input"].endswith(".ies")
    assert len(store.job["rooms"]) == 1


def test_complex_room_is_blocked_instead_of_approximated(generic_inputs):
    _, model, _, _, _ = generic_inputs
    model.rooms[0].boundary[2].x = 5.5
    _, readiness = prepare_reviewed_model(
        model, DialuxGeometryAssumptions(default_usage="Office"), default_usage="Office", confirm=True
    )
    assert readiness.status == "blocked"
    assert readiness.complexity == "complex"
    assert not readiness.can_prepare
    assert any("复杂墙体" in issue for issue in readiness.issues)


def test_generic_job_rejects_layout_tampering(generic_inputs, tmp_path):
    _, model, cad_path, photometry, executable = generic_inputs
    job_path = prepare_generic_job(
        cad_path=cad_path, photometry_path=photometry, model=model,
        assumptions=DialuxGeometryAssumptions(default_usage="Office"),
        layout=[DialuxLayoutItem(room_id="room-1", position_m=[3, 2, 2.5])],
        optimization=DialuxOptimizationRequest(), project_name="Generic", project_id="generic",
        executable=executable, output=tmp_path / "job",
    )
    store = RunStore(job_path.parent)
    store.job["layout"][0]["position_m"][0] = 1
    with pytest.raises(RunError):
        validate_job(store)


def test_optimization_candidates_are_bounded_and_deterministic(generic_inputs):
    _, model, _, _, _ = generic_inputs
    base = [DialuxLayoutItem(room_id="room-1", position_m=[3, 2, 2.5])]
    optimization = DialuxOptimizationRequest(enabled=True, max_iterations=4, target_illuminance_lx=300)
    candidates = generate_layout_candidates(model, base, optimization)
    assert 1 < len(candidates) <= 4
    assert candidates[0][0].position_m == base[0].position_m
    assert generate_layout_candidates(model, base, optimization) == candidates


def test_real_cad_review_preserves_source_and_explicitly_excludes_other_candidates(tmp_path):
    cad_path = FIXTURE / "sample-room.dxf"
    model, plan, review = prepare_real_cad_model(
        cad_path, candidate_index=0, room_number="512", room_name="512 meeting room",
        usage="Meeting room",
    )
    assert model.design_ready
    assert model.source_sha256 == plan.asset.sha256
    assert len(model.rooms) == 6
    assert [room.status for room in model.rooms].count("confirmed") == 1
    assert [room.status for room in model.rooms].count("excluded") == 5
    assert review["read_complete_before_review"] is False
    assert review["accepted_annotation_entities"] == {"ACAD_TABLE": 1}
    assert geometry_mode(model) == "orthogonal_single_room"

    photometry = tmp_path / "fixture.ies"
    photometry.write_bytes((FIXTURE / "synthetic-bridge-luminaire.ies").read_bytes())
    executable = tmp_path / "DIALux_x64.exe"
    executable.touch()
    job_path = prepare_generic_job(
        cad_path=cad_path, photometry_path=photometry, model=model,
        assumptions=DialuxGeometryAssumptions(default_usage="Meeting room"),
        layout=auto_layout(model), optimization=DialuxOptimizationRequest(),
        project_name="Real CAD", project_id="real-cad", executable=executable,
        output=tmp_path / "job", cad_review=review,
    )
    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["geometry_mode"] == "orthogonal_single_room"
    assert job["cad_review"]["candidate_id"] == model.rooms[0].room_id
    assert job["source_cad_sha256"] == plan.asset.sha256
    validate_job(RunStore(job_path.parent))
