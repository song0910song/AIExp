from __future__ import annotations

from pathlib import Path

import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.unit
from ifcopenshell.util.element import get_psets
from ifcopenshell.util.shape import get_volume
from ifcopenshell.validate import json_logger, validate
import pytest
from fastapi.testclient import TestClient

from lighting_agent.ifc_export import (
    IFC_SCHEMA,
    IfcExportError,
    IfcExportOptions,
    export_spatial_model,
)
from lighting_agent.project_store import ProjectStore
from lighting_agent.rag import LocalEvidenceStore
from lighting_agent.schemas import (
    CadPoint,
    DesignBrief,
    FloorPlan,
    FloorPlanAsset,
    ProjectUpdate,
    SpatialElement,
    SpatialModel,
)
from lighting_agent.web_api import create_app


def reviewed_model(*, two_rooms: bool = False, with_window: bool = False) -> SpatialModel:
    model = SpatialModel.model_validate_json(
        (Path(__file__).parent / "fixtures/phase0/bridge-spatial-model.json").read_text(encoding="utf-8")
    )
    rooms = model.rooms
    if two_rooms:
        rooms.append(rooms[0].model_copy(update={
            "room_id": "room-2",
            "number": "102",
            "name": "Second room",
            "boundary": [CadPoint(x=8, y=0), CadPoint(x=14, y=0), CadPoint(x=14, y=4), CadPoint(x=8, y=4)],
        }))
    elements = model.elements
    if with_window:
        elements.append(SpatialElement(
            element_id="window-1",
            kind="window",
            name="Test window",
            room_id="room-1",
            footprint=[CadPoint(x=2, y=0), CadPoint(x=4, y=0), CadPoint(x=4, y=0.1), CadPoint(x=2, y=0.1)],
            length_m=2,
            width_m=0.1,
            elevation_m=0.9,
            height_m=1.2,
            rotation_deg=0,
            material="Synthetic glazing",
            reflectance=0.1,
            status="confirmed",
        ))
    return model


def options() -> IfcExportOptions:
    return IfcExportOptions(
        project_name="IFC bridge fixture",
        project_id="bridgefixture",
        wall_thickness_m=0.12,
        floor_slab_thickness_m=0.15,
        ceiling_slab_thickness_m=0.1,
    )


def test_single_room_exports_valid_ifc4_with_geometry_and_traceability():
    result = export_spatial_model(reviewed_model(), options())
    document = ifcopenshell.file.from_string(result.data.decode("utf-8"))
    logger = json_logger()
    validate(document, logger, express_rules=True)

    assert result.schema == IFC_SCHEMA == "IFC4"
    assert ifcopenshell.util.unit.calculate_unit_scale(document) == pytest.approx(1.0)
    assert result.sha256
    assert (result.room_count, result.wall_count, result.slab_count, result.element_count) == (1, 1, 2, 1)
    assert len(document.by_type("IfcProject")) == 1
    assert len(document.by_type("IfcBuilding")) == 1
    building = document.by_type("IfcBuilding")[0]
    assert building.Representation is None
    assert all(container.ObjectPlacement is not None for container in (
        document.by_type("IfcSite")[0], building, document.by_type("IfcBuildingStorey")[0]
    ))
    assert len(document.by_type("IfcBuildingStorey")) == 1
    assert len(document.by_type("IfcSpace")) == 1
    assert all(item.CompositionType == "ELEMENT" for item in document.by_type("IfcSpatialStructureElement"))
    assert len(document.by_type("IfcWall")) == 1
    assert len(document.by_type("IfcSlab")) == 2
    furniture_geometry = document.by_type("IfcFurniture")
    assert len(furniture_geometry) == 1
    assert furniture_geometry[0].PredefinedType == "USERDEFINED"
    assert not [item for item in logger.statements if item["level"] == "error"]

    geometry_settings = ifcopenshell.geom.settings()
    geometry_settings.set(geometry_settings.USE_WORLD_COORDS, True)
    space_shape = ifcopenshell.geom.create_shape(geometry_settings, document.by_type("IfcSpace")[0])
    xs = space_shape.geometry.verts[0::3]
    ys = space_shape.geometry.verts[1::3]
    zs = space_shape.geometry.verts[2::3]
    assert max(xs) - min(xs) == pytest.approx(6.0)
    assert max(ys) - min(ys) == pytest.approx(4.0)
    assert max(zs) - min(zs) == pytest.approx(2.8)
    ceiling = next(s for s in document.by_type("IfcSlab") if s.PredefinedType == "ROOF")
    ceiling_shape = ifcopenshell.geom.create_shape(geometry_settings, ceiling)
    assert min(ceiling_shape.geometry.verts[2::3]) == pytest.approx(2.8)
    assert max(ceiling_shape.geometry.verts[2::3]) == pytest.approx(2.9)
    furniture_shape = ifcopenshell.geom.create_shape(geometry_settings, furniture_geometry[0])
    assert max(furniture_shape.geometry.verts[2::3]) == pytest.approx(0.75)
    for product, reflectance in [(document.by_type("IfcWall")[0], 0.5), (ceiling, 0.7),
                                 (furniture_geometry[0], 0.3)]:
        item = product.Representation.Representations[0].Items[0]
        colour = item.StyledByItem[0].Styles[0].Styles[0].SurfaceColour
        assert (colour.Red, colour.Green, colour.Blue) == pytest.approx((reflectance,) * 3)

    space = document.by_type("IfcSpace")[0]
    psets = get_psets(space)
    assert psets["Pset_LightingAgentRoom"]["AreaM2"] == pytest.approx(24)
    assert psets["Pset_LightingAgentRoom"]["CeilingReflectance"] == pytest.approx(0.7)
    assert psets["Pset_LightingAgentSource"]["SourceModelVersion"] == 7
    assert psets["Pset_LightingAgentSource"]["SourceCADSHA256"] == "a" * 64

    wall_psets = get_psets(document.by_type("IfcWall")[0])
    assert wall_psets["Pset_LightingAgentGeometryAssumptions"]["WallThicknessM"] == pytest.approx(0.12)
    assert wall_psets["Pset_LightingAgentReflectance"]["DiffuseReflectance"] == pytest.approx(0.5)
    assert get_psets(document.by_type("IfcProject")[0])["Pset_LightingAgentSource"]["ProjectId"] == "bridgefixture"


@pytest.mark.parametrize("fault", ["room_parameter", "furniture_pending", "furniture_material", "coverage"])
def test_design_ready_flag_does_not_bypass_required_confirmations(fault):
    model = reviewed_model()
    if fault == "room_parameter":
        model.rooms[0].ceiling_height_m = None
    elif fault == "furniture_pending":
        model.elements[0].status = "pending"
    elif fault == "furniture_material":
        model.elements[0].reflectance = None
    else:
        model.coverage_confirmed = False
    with pytest.raises(IfcExportError):
        export_spatial_model(model, options())


def test_millimetre_coordinates_and_storey_elevation_are_applied_once():
    model = reviewed_model()
    model.meters_per_unit = 0.001
    room = model.rooms[0]
    room.boundary = [CadPoint(x=p.x * 1000, y=p.y * 1000) for p in room.boundary]
    room.elevation_m = 3.2
    model.elements[0].footprint = [CadPoint(x=p.x * 1000, y=p.y * 1000) for p in model.elements[0].footprint]
    model.elements[0].elevation_m = 3.2
    document = ifcopenshell.file.from_string(export_spatial_model(model, options()).data.decode("utf-8"))
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    shape = ifcopenshell.geom.create_shape(settings, document.by_type("IfcSpace")[0])
    assert max(shape.geometry.verts[0::3]) == pytest.approx(6.0)
    assert max(shape.geometry.verts[1::3]) == pytest.approx(4.0)
    assert min(shape.geometry.verts[2::3]) == pytest.approx(3.2)
    assert max(shape.geometry.verts[2::3]) == pytest.approx(6.0)


def test_irregular_furniture_with_an_internal_void_keeps_its_profile():
    model = reviewed_model()
    furniture = model.elements[0]
    furniture.footprint = [
        CadPoint(x=1, y=1), CadPoint(x=3, y=1), CadPoint(x=3, y=3),
        CadPoint(x=2, y=3), CadPoint(x=2, y=2), CadPoint(x=1, y=2),
    ]
    furniture.holes = [[
        CadPoint(x=1.2, y=1.2), CadPoint(x=1.5, y=1.2),
        CadPoint(x=1.5, y=1.5), CadPoint(x=1.2, y=1.5),
    ]]

    document = ifcopenshell.file.from_string(export_spatial_model(model, options()).data.decode("utf-8"))
    product = document.by_type("IfcFurniture")[0]
    profile = product.Representation.Representations[0].Items[0].SweptArea
    assert profile.is_a("IfcArbitraryProfileDefWithVoids")
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    volume = get_volume(ifcopenshell.geom.create_shape(settings, product).geometry)
    assert volume == pytest.approx((3 - .09) * .75, abs=1e-5)


@pytest.mark.parametrize("model", [
    SpatialModel(source_sha256="a" * 64, meters_per_unit=1, design_ready=False),
    reviewed_model(two_rooms=True),
    reviewed_model(with_window=True),
])
def test_export_rejects_unreviewed_or_not_yet_supported_geometry(model):
    with pytest.raises(IfcExportError):
        export_spatial_model(model, options())


@pytest.mark.parametrize("wall_thickness,floor_thickness", [(0, 0.1), (0.1, -0.1), (1.1, 0.1)])
def test_export_rejects_unreasonable_geometry_assumptions(wall_thickness, floor_thickness):
    with pytest.raises(ValueError):
        IfcExportOptions(
            project_name="Fixture",
            project_id="bridgefixture",
            wall_thickness_m=wall_thickness,
            floor_slab_thickness_m=floor_thickness,
            ceiling_slab_thickness_m=0.1,
        )


def test_project_api_exports_ifc_pinned_to_current_model_revision(tmp_path):
    projects = ProjectStore(tmp_path / "projects")
    evidence = LocalEvidenceStore(tmp_path / "evidence.db")
    client = TestClient(create_app(
        project_store=projects,
        evidence_store=evidence,
        user_documents_directory=tmp_path / "documents",
        vision_model=False,
    ))
    state = projects.create(DesignBrief(project_name="Bridge project"))
    model = reviewed_model()
    plan = FloorPlan(
        asset=FloorPlanAsset(
            source_name="synthetic.dxf",
            source_type="dxf",
            storage_path="synthetic.dxf",
            sha256=model.source_sha256,
            size_bytes=0,
        ),
        drawing_units="m",
        meters_per_drawing_unit=1,
        read_complete=True,
        spatial_model=model,
    )
    state = projects.update(
        state.project_id,
        ProjectUpdate(expected_revision=state.revision, floor_plan=plan),
    )

    response = client.post(
        f"/api/projects/{state.project_id}/spatial-model/ifc",
        json={
            "expected_revision": state.revision,
            "wall_thickness_m": 0.12,
            "floor_slab_thickness_m": 0.15,
            "ceiling_slab_thickness_m": 0.1,
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers["x-ifc-schema"] == "IFC4"
    assert response.headers["x-spatial-model-version"] == "7"
    assert response.headers["x-source-cad-sha256"] == model.source_sha256
    assert response.headers["content-disposition"].endswith('model-v7.ifc"')
    assert ifcopenshell.file.from_string(response.content.decode("utf-8")).schema == "IFC4"

    stale = client.post(
        f"/api/projects/{state.project_id}/spatial-model/ifc",
        json={
            "expected_revision": state.revision - 1,
            "wall_thickness_m": 0.12,
            "floor_slab_thickness_m": 0.15,
            "ceiling_slab_thickness_m": 0.1,
        },
    )
    assert stale.status_code == 409

    plan.asset.sha256 = "b" * 64
    state = projects.update(state.project_id, ProjectUpdate(expected_revision=state.revision, floor_plan=plan))
    mismatched = client.post(
        f"/api/projects/{state.project_id}/spatial-model/ifc",
        json={"expected_revision": state.revision, "wall_thickness_m": 0.12,
              "floor_slab_thickness_m": 0.15, "ceiling_slab_thickness_m": 0.1},
    )
    assert mismatched.status_code == 422
