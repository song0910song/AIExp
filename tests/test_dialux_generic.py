from __future__ import annotations

from pathlib import Path

import ezdxf
import ifcopenshell
import ifcopenshell.geom
import pytest
from ifcopenshell.util.shape import get_volume
from shapely.geometry import Point

from lighting_agent.dialux_runner.artifacts import RunError, RunStore, sha256, validate_job
from lighting_agent.dialux_runner.generic_jobs import (
    GENERIC_PROFILE, generate_layout_candidates, geometry_mode, prepare_generic_job,
    prepare_reviewed_model,
)
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


def test_complex_single_room_preserves_curves_and_holes_in_ifc(generic_inputs, tmp_path):
    _, model, cad_path, photometry, executable = generic_inputs
    outer = Point(3, 2).buffer(2, quad_segs=12)
    model.rooms[0].boundary = [
        type(model.rooms[0].boundary[0])(x=x, y=y)
        for x, y in list(outer.exterior.coords)[:-1]
    ]
    model.rooms[0].holes = [[
        type(model.rooms[0].boundary[0])(x=x, y=y)
        for x, y in [(3.2, 1.9), (3.6, 1.9), (3.6, 2.3), (3.2, 2.3)]
    ]]
    model.elements[0].footprint = [
        type(model.rooms[0].boundary[0])(x=x, y=y)
        for x, y in [(2., 1.8), (2.5, 1.8), (2.5, 2.3), (2., 2.3)]
    ]
    assert geometry_mode(model) == "complex_single_room"
    effective, readiness = prepare_reviewed_model(
        model, DialuxGeometryAssumptions(default_usage="Office"), default_usage="Office", confirm=True
    )
    assert readiness.status == "ready"
    assert readiness.complexity == "complex"
    assert readiness.can_prepare

    job_path = prepare_generic_job(
        cad_path=cad_path, photometry_path=photometry, model=effective,
        assumptions=DialuxGeometryAssumptions(default_usage="Office"),
        layout=[DialuxLayoutItem(room_id="room-1", position_m=[3, 2, 2.5])],
        optimization=DialuxOptimizationRequest(), project_name="Curved room", project_id="curved-room",
        executable=executable, output=tmp_path / "complex-job",
    )
    store = RunStore(job_path.parent)
    validate_job(store)
    assert store.job["geometry_mode"] == "complex_single_room"
    document = ifcopenshell.open(store.root / "inputs/bridge.ifc")
    space = document.by_type("IfcSpace")[0]
    profile = space.Representation.Representations[0].Items[0].SweptArea
    assert profile.is_a("IfcArbitraryProfileDefWithVoids")
    assert len(profile.InnerCurves) == 1
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    geometry = ifcopenshell.geom.create_shape(settings, space).geometry
    assert get_volume(geometry) == pytest.approx((outer.area - .16) * 2.8, abs=1e-5)


def test_complex_multiroom_wall_topology_is_exported_without_rectangular_approximation(generic_inputs, tmp_path):
    _, model, cad_path, photometry, executable = generic_inputs
    second = model.rooms[0].model_copy(deep=True)
    second.room_id = "room-2"
    second.number = "2"
    second.boundary = [
        type(second.boundary[0])(x=x, y=y)
        for x, y in [(6.12, 0), (12.12, 0), (11.12, 4), (6.12, 4)]
    ]
    second.area_m2 = None
    model.rooms.append(second)

    effective, readiness = prepare_reviewed_model(
        model, DialuxGeometryAssumptions(default_usage="Office"), default_usage="Office", confirm=True
    )

    assert readiness.status == "ready"
    assert readiness.complexity == "complex"
    assert readiness.can_prepare
    job_path = prepare_generic_job(
        cad_path=cad_path, photometry_path=photometry, model=effective,
        assumptions=DialuxGeometryAssumptions(default_usage="Office"),
        layout=[DialuxLayoutItem(room_id="room-1", position_m=[3, 2, 2.5]),
                DialuxLayoutItem(room_id="room-2", position_m=[9, 2, 2.5])],
        optimization=DialuxOptimizationRequest(), project_name="Complex envelope", project_id="complex-envelope",
        executable=executable, output=tmp_path / "complex-envelope-job",
    )
    store = RunStore(job_path.parent)
    validate_job(store)
    assert store.job["geometry_mode"] == "complex_multiroom"
    document = ifcopenshell.open(store.root / "inputs/bridge.ifc")
    assert len(document.by_type("IfcSpace")) == 2
    assert len(document.by_type("IfcWall")) == 1
    wall = document.by_type("IfcWall")[0]
    assert len(wall.ProvidesBoundaries) == 2
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    space_volumes = sorted(get_volume(ifcopenshell.geom.create_shape(settings, space).geometry)
                           for space in document.by_type("IfcSpace"))
    assert space_volumes == pytest.approx([22 * 2.8, 24 * 2.8])


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
