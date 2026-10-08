"""Conservative IFC4 export for the phase-0 DIALux bridge validation."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any

import ifcopenshell
import ifcopenshell.api.aggregate
import ifcopenshell.api.context
import ifcopenshell.api.geometry
import ifcopenshell.api.material
import ifcopenshell.api.pset
import ifcopenshell.api.project
import ifcopenshell.api.root
import ifcopenshell.api.spatial
import ifcopenshell.api.unit
import ifcopenshell.util.unit
import numpy as np
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient

from .schemas import SpatialElement, SpatialModel, SpatialRoom
from .spatial_model import REQUIRED_ROOM_FIELDS


IFC_SCHEMA = "IFC4"
MAX_IFC_PROFILE_VERTICES = 50_000


class IfcExportError(ValueError):
    """The reviewed model cannot yet be represented without losing facts."""


@dataclass(frozen=True, slots=True)
class IfcExportOptions:
    wall_thickness_m: float
    floor_slab_thickness_m: float
    ceiling_slab_thickness_m: float
    project_name: str
    project_id: str

    def __post_init__(self) -> None:
        if not 0.03 <= self.wall_thickness_m <= 1.0:
            raise ValueError("墙厚假设必须在 0.03–1.0 m 范围内")
        if not 0.03 <= self.floor_slab_thickness_m <= 1.0:
            raise ValueError("楼板厚度假设必须在 0.03–1.0 m 范围内")
        if not 0.03 <= self.ceiling_slab_thickness_m <= 1.0:
            raise ValueError("顶板厚度假设必须在 0.03–1.0 m 范围内")
        if not self.project_name.strip() or not self.project_id.strip():
            raise ValueError("IFC 导出需要项目名称和项目 ID")


@dataclass(frozen=True, slots=True)
class IfcExportResult:
    data: bytes
    sha256: str
    room_count: int
    wall_count: int
    slab_count: int
    element_count: int
    schema: str = IFC_SCHEMA


def export_spatial_model(model: SpatialModel, options: IfcExportOptions) -> IfcExportResult:
    """Create an IFC4 bridge model from one fully confirmed room."""
    room, elements = _validate_export_scope(model)
    document = ifcopenshell.api.project.create_file(version=IFC_SCHEMA)
    project = ifcopenshell.api.root.create_entity(
        document, ifc_class="IfcProject", name=options.project_name
    )
    _assign_pset(document, project, "Pset_LightingAgentSource", {
        "ProjectId": options.project_id,
        "SourceModelVersion": model.version,
        "SourceCADSHA256": model.source_sha256,
    })
    length_unit = ifcopenshell.api.unit.add_si_unit(document, unit_type="LENGTHUNIT")
    area_unit = ifcopenshell.api.unit.add_si_unit(document, unit_type="AREAUNIT")
    volume_unit = ifcopenshell.api.unit.add_si_unit(document, unit_type="VOLUMEUNIT")
    ifcopenshell.api.unit.assign_unit(document, units=[length_unit, area_unit, volume_unit])
    model_context = ifcopenshell.api.context.add_context(document, context_type="Model")
    body_context = ifcopenshell.api.context.add_context(
        document,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=model_context,
    )
    project.RepresentationContexts = (model_context,)

    polygon = _room_polygon(room, model.meters_per_unit)
    elevation = float(room.elevation_m or 0)
    site = ifcopenshell.api.root.create_entity(document, ifc_class="IfcSite", name="Site")
    building = ifcopenshell.api.root.create_entity(
        document, ifc_class="IfcBuilding", name=options.project_name
    )
    storey = ifcopenshell.api.root.create_entity(
        document, ifc_class="IfcBuildingStorey", name=room.floor or "Level 1"
    )
    storey.Elevation = room.elevation_m
    ifcopenshell.api.aggregate.assign_object(document, products=[site], relating_object=project)
    ifcopenshell.api.aggregate.assign_object(document, products=[building], relating_object=site)
    ifcopenshell.api.aggregate.assign_object(document, products=[storey], relating_object=building)
    # Spatial containers need explicit coordinate systems, not physical solids.
    for container in (site, building, storey):
        container.CompositionType = "ELEMENT"
        matrix = np.eye(4)
        if container == storey:
            matrix[2, 3] = elevation
        ifcopenshell.api.geometry.edit_object_placement(
            document, product=container, matrix=matrix, is_si=True
        )

    space = ifcopenshell.api.root.create_entity(
        document, ifc_class="IfcSpace", name=room.number or room.room_id
    )
    space.LongName = room.name or room.room_id
    space.Description = room.usage or ""
    # DIALux 5.14.0.3 skips spaces whose optional CompositionType is unset.
    space.CompositionType = "ELEMENT"
    _assign_profile_shape(document, body_context, space, polygon, room.ceiling_height_m, elevation)
    ifcopenshell.api.aggregate.assign_object(
        document, products=[space], relating_object=storey
    )
    _assign_pset(
        document,
        space,
        "Pset_LightingAgentRoom",
        {
            "RoomId": room.room_id,
            "RoomNumber": room.number or "",
            "Usage": room.usage or "",
            "AreaM2": polygon.area,
            "ClearHeightM": room.ceiling_height_m,
            "StoreyHeightM": room.height_m,
            "WallReflectance": room.wall_reflectance,
            "CeilingReflectance": room.ceiling_reflectance,
            "FloorReflectance": room.floor_reflectance,
        },
    )
    _assign_source_pset(document, space, model, room.room_id)

    walls = _create_wall_ring(
        document, body_context, storey, polygon, elevation, room.height_m,
        room.wall_reflectance, options.wall_thickness_m, model, room.room_id,
    )
    _create_floor_slab(
        document, body_context, storey, polygon, elevation,
        room.floor_reflectance, options.floor_slab_thickness_m, model, room.room_id,
    )
    _create_ceiling_slab(document, body_context, storey, polygon, elevation, room, options, model)
    for element in elements:
        _create_element(
            document, body_context, storey, model, room, element, elevation
        )

    data = document.to_string().encode("utf-8")
    # Parse the serialized exchange file as a final guard against malformed STEP.
    reopened = ifcopenshell.file.from_string(data.decode("utf-8"))
    if reopened.schema != IFC_SCHEMA or len(reopened.by_type("IfcSpace")) != 1:
        raise IfcExportError("IFC 序列化校验失败")
    if abs(ifcopenshell.util.unit.calculate_unit_scale(reopened) - 1.0) > 1e-9:
        raise IfcExportError("IFC 长度单位不是米，拒绝导出错误尺度模型")
    return IfcExportResult(
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        room_count=1,
        wall_count=len(walls),
        slab_count=2,
        element_count=len(elements),
    )


def write_ifc(result: IfcExportResult, output_path: Path) -> Path:
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(result.data)
    return output_path


def _validate_export_scope(model: SpatialModel) -> tuple[SpatialRoom, list[SpatialElement]]:
    if not model.design_ready or not model.coverage_confirmed or not model.elements_reviewed or model.outstanding:
        raise IfcExportError("空间模型尚未完成核对，不能生成 IFC")
    if not model.meters_per_unit:
        raise IfcExportError("空间模型缺少有效的图纸单位换算")
    rooms = [room for room in model.rooms if room.status != "excluded"]
    if len(rooms) != 1:
        raise IfcExportError("阶段 0 IFC 桥接验证仅支持一个待设计房间")
    room = rooms[0]
    if room.status != "confirmed":
        raise IfcExportError(f"房间 {room.room_id} 尚未确认")
    missing = [field for field in REQUIRED_ROOM_FIELDS if getattr(room, field) in (None, "")]
    if missing:
        raise IfcExportError(f"房间 {room.room_id} 缺少已确认参数：{', '.join(missing)}")
    if room.ceiling_height_m > room.height_m:
        raise IfcExportError(f"房间 {room.room_id} 吊顶高度不能高于层高")
    elements = [element for element in model.elements if element.status != "excluded"]
    room_polygon = _room_polygon(room, model.meters_per_unit)
    for element in elements:
        if element.status != "confirmed":
            raise IfcExportError(f"构件 {element.element_id} 尚未确认")
        missing = [field for field in ("height_m", "elevation_m", "material", "reflectance", "rotation_deg")
                   if getattr(element, field) in (None, "")]
        if missing:
            raise IfcExportError(f"构件 {element.element_id} 缺少已确认参数：{', '.join(missing)}")
    unsupported = [element for element in elements if element.kind in {"door", "window"}]
    if unsupported:
        names = ", ".join(element.name or element.element_id for element in unsupported)
        raise IfcExportError(f"阶段 0 尚未实现门窗开口与墙体宿主关系：{names}")
    wrong_room = [element for element in elements if element.room_id != room.room_id]
    if wrong_room:
        names = ", ".join(element.name or element.element_id for element in wrong_room)
        raise IfcExportError(f"构件不属于唯一导出房间：{names}")
    for element in elements:
        if element.elevation_m < room.elevation_m - 1e-7 or \
                element.elevation_m + element.height_m > room.elevation_m + room.ceiling_height_m + 1e-7:
            raise IfcExportError(f"构件 {element.name or element.element_id} 超出房间垂直范围")
        footprint = _element_polygon(element, model.meters_per_unit)
        if not room_polygon.buffer(1e-7).covers(footprint):
            raise IfcExportError(f"构件 {element.name or element.element_id} 超出房间边界")
    return room, elements


def _room_polygon(room: SpatialRoom, meters_per_unit: float) -> Polygon:
    polygon = Polygon(
        [(point.x * meters_per_unit, point.y * meters_per_unit) for point in room.boundary],
        [[(point.x * meters_per_unit, point.y * meters_per_unit) for point in hole] for hole in room.holes],
    )
    if not polygon.is_valid or polygon.area <= 0:
        raise IfcExportError(f"房间 {room.room_id} 几何无效")
    return orient(polygon, sign=1.0)


def _profile(document: Any, polygon: Polygon, name: str) -> Any:
    polygon = orient(polygon, sign=1.0)
    vertex_count = sum(len(ring.coords) - 1 for ring in (polygon.exterior, *polygon.interiors))
    if vertex_count > MAX_IFC_PROFILE_VERTICES:
        raise IfcExportError(
            f"IFC 几何 {name} 有 {vertex_count} 个轮廓顶点，超过安全上限 {MAX_IFC_PROFILE_VERTICES}"
        )
    outer = _polyline(document, list(polygon.exterior.coords))
    if polygon.interiors:
        inner = tuple(_polyline(document, list(ring.coords)) for ring in polygon.interiors)
        return document.createIfcArbitraryProfileDefWithVoids(
            "AREA", name, outer, inner
        )
    return document.createIfcArbitraryClosedProfileDef("AREA", name, outer)


def _polyline(document: Any, coordinates: list[tuple[float, float]]) -> Any:
    points = tuple(
        document.createIfcCartesianPoint((float(x), float(y))) for x, y in coordinates
    )
    return document.createIfcPolyline(points)


def _assign_profile_shape(
    document: Any,
    body_context: Any,
    product: Any,
    polygon: Polygon,
    depth_m: float,
    elevation_m: float,
) -> None:
    representation = ifcopenshell.api.geometry.add_profile_representation(
        document, context=body_context, profile=_profile(document, polygon, product.Name), depth=depth_m
    )
    ifcopenshell.api.geometry.assign_representation(
        document, product=product, representation=representation
    )
    matrix = np.eye(4)
    matrix[2, 3] = elevation_m
    ifcopenshell.api.geometry.edit_object_placement(
        document, product=product, matrix=matrix, is_si=True
    )


def _create_wall_ring(
    document: Any,
    body_context: Any,
    storey: Any,
    room_polygon: Polygon,
    elevation_m: float,
    height_m: float,
    reflectance: float,
    thickness_m: float,
    model: SpatialModel,
    room_id: str,
) -> list[Any]:
    wall_area = room_polygon.buffer(thickness_m, join_style="mitre").difference(room_polygon)
    polygons = [wall_area] if wall_area.geom_type == "Polygon" else list(wall_area.geoms)
    walls = []
    for index, polygon in enumerate(polygons, start=1):
        wall = ifcopenshell.api.root.create_entity(
            document, ifc_class="IfcWall", predefined_type="SOLIDWALL",
            name=f"{room_id} perimeter wall {index}",
        )
        _assign_profile_shape(document, body_context, wall, polygon, height_m, elevation_m)
        ifcopenshell.api.spatial.assign_container(
            document, products=[wall], relating_structure=storey
        )
        _assign_material(document, wall, f"Wall finish R{reflectance:.2f}", reflectance)
        _assign_pset(document, wall, "Pset_LightingAgentGeometryAssumptions", {
            "WallThicknessM": thickness_m,
            "ThicknessSource": "explicit export assumption; not CAD fact",
        })
        _assign_source_pset(document, wall, model, room_id)
        walls.append(wall)
    return walls


def _create_floor_slab(
    document: Any,
    body_context: Any,
    storey: Any,
    polygon: Polygon,
    elevation_m: float,
    reflectance: float,
    thickness_m: float,
    model: SpatialModel,
    room_id: str,
) -> Any:
    slab = ifcopenshell.api.root.create_entity(
        document, ifc_class="IfcSlab", predefined_type="FLOOR", name=f"{room_id} floor slab"
    )
    _assign_profile_shape(
        document, body_context, slab, polygon, thickness_m, elevation_m - thickness_m
    )
    ifcopenshell.api.spatial.assign_container(
        document, products=[slab], relating_structure=storey
    )
    _assign_material(document, slab, f"Floor finish R{reflectance:.2f}", reflectance)
    _assign_pset(document, slab, "Pset_LightingAgentGeometryAssumptions", {
        "FloorSlabThicknessM": thickness_m,
        "ThicknessSource": "explicit export assumption; not CAD fact",
    })
    _assign_source_pset(document, slab, model, room_id)
    return slab


def _create_ceiling_slab(
    document: Any,
    body_context: Any,
    storey: Any,
    polygon: Polygon,
    elevation_m: float,
    room: SpatialRoom,
    options: IfcExportOptions,
    model: SpatialModel,
) -> Any:
    # IfcSpace defines the occupied volume; it does not supply a physical ceiling.
    slab = ifcopenshell.api.root.create_entity(
        document, ifc_class="IfcSlab", predefined_type="ROOF", name=f"{room.room_id} ceiling slab"
    )
    _assign_profile_shape(
        document, body_context, slab, polygon, options.ceiling_slab_thickness_m,
        elevation_m + room.ceiling_height_m,
    )
    ifcopenshell.api.spatial.assign_container(document, products=[slab], relating_structure=storey)
    _assign_material(document, slab, f"Ceiling finish R{room.ceiling_reflectance:.2f}", room.ceiling_reflectance)
    _assign_pset(document, slab, "Pset_LightingAgentGeometryAssumptions", {
        "CeilingSlabThicknessM": options.ceiling_slab_thickness_m,
        "ThicknessSource": "explicit export assumption; not CAD fact",
        "RepresentationPurpose": "simplified opaque ceiling at reviewed clear height",
    })
    _assign_source_pset(document, slab, model, room.room_id)
    return slab


def _create_element(
    document: Any,
    body_context: Any,
    storey: Any,
    model: SpatialModel,
    room: SpatialRoom,
    element: SpatialElement,
    room_elevation_m: float,
) -> None:
    if len(element.footprint) < 3 or element.height_m is None:
        raise IfcExportError(f"构件 {element.element_id} 缺少可生成实体的边界或高度")
    polygon = _element_polygon(element, model.meters_per_unit)
    classes = {
        "column": "IfcColumn",
        "furniture": "IfcFurniture",
        "obstruction": "IfcBuildingElementProxy",
    }
    product = ifcopenshell.api.root.create_entity(
        document,
        ifc_class=classes[element.kind],
        name=element.name or element.element_id,
    )
    if element.kind == "furniture":
        product.PredefinedType = "USERDEFINED"
        product.ObjectType = "Furniture"
    elif element.kind == "obstruction":
        product.ObjectType = element.kind.capitalize()
    elevation_m = float(element.elevation_m if element.elevation_m is not None else room_elevation_m)
    _assign_profile_shape(document, body_context, product, orient(polygon, sign=1.0), element.height_m, elevation_m)
    ifcopenshell.api.spatial.assign_container(
        document, products=[product], relating_structure=storey
    )
    if element.material and element.reflectance is not None:
        _assign_material(document, product, element.material, element.reflectance)
    _assign_pset(document, product, "Pset_LightingAgentSource", {
        "ElementId": element.element_id,
        "Kind": element.kind,
        "RoomId": room.room_id,
        "SourceModelVersion": model.version,
        "SourceCADSHA256": model.source_sha256,
    })


def _assign_material(document: Any, product: Any, name: str, reflectance: float) -> None:
    material = ifcopenshell.api.material.add_material(
        document, name=name, category="surface finish"
    )
    ifcopenshell.api.material.assign_material(
        document, products=[product], material=material
    )
    # Custom property sets preserve provenance, but importers read the surface
    # style to initialise actual material reflectance. Use neutral diffuse grey.
    colour = document.createIfcColourRgb(None, reflectance, reflectance, reflectance)
    shading = document.createIfcSurfaceStyleShading(colour, 0.0)
    style = document.createIfcSurfaceStyle(name, "BOTH", (shading,))
    for representation in product.Representation.Representations:
        if representation.RepresentationIdentifier == "Body":
            for item in representation.Items:
                document.createIfcStyledItem(item, (style,), name)
    _assign_pset(document, product, "Pset_LightingAgentReflectance", {
        "MaterialName": name,
        "DiffuseReflectance": reflectance,
        "ReflectanceSource": "reviewed spatial model",
    })


def _assign_source_pset(
    document: Any, product: Any, model: SpatialModel, room_id: str
) -> None:
    _assign_pset(document, product, "Pset_LightingAgentSource", {
        "RoomId": room_id,
        "SourceModelVersion": model.version,
        "SourceCADSHA256": model.source_sha256,
    })


def _assign_pset(document: Any, product: Any, name: str, properties: dict[str, Any]) -> None:
    pset = ifcopenshell.api.pset.add_pset(document, product=product, name=name)
    ifcopenshell.api.pset.edit_pset(document, pset=pset, properties=properties)


def _element_polygon(element: SpatialElement, meters_per_unit: float) -> Polygon:
    polygon = Polygon(
        [(point.x * meters_per_unit, point.y * meters_per_unit) for point in element.footprint],
        [[(point.x * meters_per_unit, point.y * meters_per_unit) for point in ring]
         for ring in element.holes],
    )
    if not polygon.is_valid or polygon.area <= 0:
        raise IfcExportError(f"构件 {element.name or element.element_id} 几何无效")
    return orient(polygon, sign=1.0)
