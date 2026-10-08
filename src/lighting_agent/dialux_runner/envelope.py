"""Standalone rectangular-room IFC envelope with explicit wall openings.

This does not widen the accepted phase-0/API exporter. Room outlines are clear
internal faces, in CAD units; opening footprints describe the complete void
through the host wall. Only one level with a common storey height is supported.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Literal

import ifcopenshell
import ifcopenshell.api.feature
from pydantic import BaseModel, ConfigDict, Field, model_validator
import numpy as np
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from lighting_agent.ifc_export import (
    IFC_SCHEMA, IfcExportError, IfcExportOptions, IfcExportResult,
    _assign_material, _assign_profile_shape, _assign_pset, _assign_source_pset,
    _create_ceiling_slab, _create_element, _create_floor_slab, _room_polygon,
)
from lighting_agent.schemas import SpatialElement, SpatialModel, SpatialRoom
from lighting_agent.spatial_model import REQUIRED_ROOM_FIELDS

EPS = 1e-7


class OpeningTreatment(BaseModel):
    """Optical/physical assumptions kept separate from reviewed CAD facts.

    An open passage has no door leaf; it must not be described as a hinged door
    swung open. Glazing values are explicit and require a DIALux read-back.
    """

    model_config = ConfigDict(extra='forbid', allow_inf_nan=False, frozen=True)
    element_id: str = Field(min_length=1)
    filling: Literal['closed_door', 'glazing', 'open_passage'] = 'closed_door'
    panel_thickness_m: float = Field(default=.04, gt=0, le=.2)
    visible_transmittance: float = Field(default=0., ge=0, le=1)
    refractive_index: float | None = Field(default=None, ge=1, le=3)
    assumption_note: str = Field(default='Design assumption: closed opaque ordinary door, simplified solid panel.', min_length=1)

    @model_validator(mode='after')
    def optical_scope(self):
        if self.filling != 'glazing' and self.visible_transmittance != 0:
            raise ValueError('Only glazing may have nonzero visible transmittance')
        if self.filling == 'glazing' and (self.visible_transmittance <= 0 or 'visible_transmittance' not in self.model_fields_set):
            raise ValueError('Glazing requires explicit positive visible transmittance')
        if self.filling == 'glazing' and 'panel_thickness_m' not in self.model_fields_set:
            raise ValueError('Glazing requires explicit panel thickness')
        if self.filling == 'glazing' and self.refractive_index is None:
            raise ValueError('Glazing requires explicit refractive index')
        if self.filling != 'glazing' and self.refractive_index is not None:
            raise ValueError('Refractive index only applies to glazing')
        if self.filling != 'closed_door' and 'assumption_note' not in self.model_fields_set:
            raise ValueError('Glazing/open passage requires its own assumption note')
        return self


@dataclass(frozen=True)
class Wall:
    key: str
    polygon: Polygon
    # ``general`` is used for exact curved/diagonal/concave wall strips.  Such
    # walls are exported as-is; openings are intentionally blocked until a
    # unique host and local frame can be proven.
    axis: Literal['x', 'y', 'general']
    room_ids: tuple[str, ...]
    reflectance: float


@dataclass(frozen=True)
class Opening:
    element: SpatialElement
    polygon: Polygon
    wall: Wall
    treatment: OpeningTreatment
    room_ids: tuple[str, ...]


@dataclass(frozen=True)
class EnvelopeExport:
    ifc: IfcExportResult
    manifest: dict


def _rectangle(polygon: Polygon, label: str) -> Polygon:
    if (not polygon.is_valid or polygon.area <= EPS or polygon.interiors
            or polygon.symmetric_difference(box(*polygon.bounds)).area > EPS):
        raise IfcExportError(f'{label}: standalone envelope requires an axis-aligned rectangle without holes')
    return polygon


def _valid_polygon(polygon: Polygon, label: str) -> Polygon:
    if polygon.geom_type != 'Polygon' or not polygon.is_valid or polygon.area <= EPS:
        raise IfcExportError(f'{label}: envelope requires a valid planar polygon')
    if sum(len(ring.coords) - 1 for ring in (polygon.exterior, *polygon.interiors)) > 50_000:
        raise IfcExportError(f'{label}: IFC profile exceeds the 50000-vertex limit')
    return polygon


def _parts(geometry) -> list[Polygon]:
    if geometry.is_empty:
        return []
    if geometry.geom_type == 'Polygon':
        return [geometry] if geometry.area > EPS else []
    if geometry.geom_type in ('MultiPolygon', 'GeometryCollection'):
        return [part for item in geometry.geoms for part in _parts(item)]
    return []


def _validate_model(model: SpatialModel) -> tuple[list[SpatialRoom], dict[str, Polygon]]:
    if not model.design_ready or not model.coverage_confirmed or not model.elements_reviewed or model.outstanding:
        raise IfcExportError('The spatial model must be fully reviewed before envelope export')
    if not model.meters_per_unit or not math.isfinite(model.meters_per_unit):
        raise IfcExportError('A finite positive CAD unit scale is required')
    for items, attribute in ((model.rooms, 'room_id'), (model.elements, 'element_id')):
        ids = [getattr(item, attribute) for item in items]
        if len(set(ids)) != len(ids):
            raise IfcExportError(f'Duplicate {attribute}')
    rooms = sorted((r for r in model.rooms if r.status != 'excluded'), key=lambda r: r.room_id)
    if not rooms:
        raise IfcExportError('No active rooms')
    polygons = {}
    for room in rooms:
        if room.status != 'confirmed' or any(getattr(room, field) in (None, '') for field in REQUIRED_ROOM_FIELDS):
            raise IfcExportError(f'Room {room.room_id} has unconfirmed or missing parameters')
        if room.ceiling_height_m > room.height_m:
            raise IfcExportError(f'Room {room.room_id}: clear height exceeds storey height')
        polygon = _valid_polygon(_room_polygon(room, model.meters_per_unit), room.room_id)
        if room.area_m2 is not None and not math.isclose(room.area_m2, polygon.area, abs_tol=EPS, rel_tol=0):
            raise IfcExportError(f'Room {room.room_id}: area differs from its exact boundary')
        polygons[room.room_id] = polygon
    complex_rooms = any(polygon.symmetric_difference(box(*polygon.bounds)).area > EPS
                        for polygon in polygons.values())
    if complex_rooms and any(element.status != 'excluded' and element.kind in ('door', 'window')
                             for element in model.elements):
        raise IfcExportError('Complex room envelopes with doors/windows require a validated wall host')
    if len({(r.floor, r.elevation_m, r.height_m) for r in rooms}) != 1:
        raise IfcExportError('Standalone envelope currently requires one floor, elevation and storey height')
    for element in model.elements:
        if element.status == 'excluded':
            continue
        if element.status != 'confirmed' or element.room_id not in polygons:
            raise IfcExportError(f'Element {element.element_id}: confirmed active room is required')
        if any(getattr(element, field) in (None, '') for field in ('height_m', 'elevation_m', 'material', 'reflectance', 'rotation_deg')):
            raise IfcExportError(f'Element {element.element_id}: physical/material parameters are incomplete')
        if len(element.footprint) < 3:
            raise IfcExportError(f'Element {element.element_id}: exact footprint is required')
        polygon = Polygon([(p.x * model.meters_per_unit, p.y * model.meters_per_unit) for p in element.footprint])
        if not polygon.is_valid or polygon.area <= EPS:
            raise IfcExportError(f'Element {element.element_id}: invalid footprint')
        room = next(r for r in rooms if r.room_id == element.room_id)
        if (element.elevation_m < room.elevation_m - EPS
                or element.elevation_m + element.height_m > room.elevation_m + room.ceiling_height_m + EPS):
            raise IfcExportError(f'Element {element.element_id}: vertical bounds exceed the occupied room')
        if element.kind not in ('door', 'window') and not polygons[element.room_id].buffer(EPS).covers(polygon):
            raise IfcExportError(f'Element {element.element_id}: footprint is outside its room')
    return rooms, polygons


def wall_topology(rooms: list[SpatialRoom], polygons: dict[str, Polygon], thickness: float) -> list[Wall]:
    """Partition the union of wall strips once, retaining exact clear dimensions."""
    if any(polygon.symmetric_difference(box(*polygon.bounds)).area > EPS for polygon in polygons.values()):
        interiors = unary_union(list(polygons.values()))
        strips = [polygon.buffer(thickness, join_style='mitre').difference(polygon)
                  for polygon in polygons.values()]
        wall_area = unary_union(strips).difference(interiors)
        walls = []
        for part in sorted(_parts(wall_area), key=lambda polygon: polygon.bounds):
            ids = tuple(room.room_id for room in rooms
                        if part.distance(polygons[room.room_id].boundary) <= thickness + EPS)
            reflectances = {room.wall_reflectance for room in rooms if room.room_id in ids}
            if not ids or len(reflectances) != 1:
                raise IfcExportError('General walls need one consistent reflectance and an unambiguous room relation')
            walls.append(Wall(f'wall-{len(walls) + 1:03d}', part, 'general', ids, reflectances.pop()))
        return walls
    for i, room in enumerate(rooms):
        a = polygons[room.room_id]
        for other in rooms[i + 1:]:
            b = polygons[other.room_id]
            if a.intersection(b).area > EPS or a.distance(b) < thickness - EPS:
                raise IfcExportError('Rooms overlap or leave less than the explicit wall thickness; clear boundaries must include the wall gap')
            ax0, ay0, ax1, ay1 = a.bounds
            bx0, by0, bx1, by1 = b.bounds
            gaps = []
            if min(ay1, by1) - max(ay0, by0) > EPS:
                gaps.append(max(ax0 - bx1, bx0 - ax1))
            if min(ax1, bx1) - max(ax0, bx0) > EPS:
                gaps.append(max(ay0 - by1, by0 - ay1))
            if any(thickness + EPS < gap < 2 * thickness - EPS for gap in gaps):
                raise IfcExportError('Facing wall strips overlap with an ambiguous wall thickness')
    assigned = Polygon()
    interiors = unary_union(list(polygons.values()))
    walls = []
    room_by_id = {r.room_id: r for r in rooms}
    for room in rooms:
        x0, y0, x1, y1 = polygons[room.room_id].bounds
        candidates = [('y', box(x0 - thickness, y0, x0, y1)),
                      ('y', box(x1, y0, x1 + thickness, y1)),
                      ('x', box(x0 - thickness, y0 - thickness, x1 + thickness, y0)),
                      ('x', box(x0 - thickness, y1, x1 + thickness, y1 + thickness))]
        for axis, candidate in candidates:
            for polygon in sorted(_parts(candidate.difference(assigned).difference(interiors)), key=lambda p: p.bounds):
                ids = tuple(r.room_id for r in rooms if polygons[r.room_id].boundary.intersection(polygon.boundary).length > EPS)
                if not ids:
                    ids = (room.room_id,)  # corner return, same finish as its source wall
                reflectances = {room_by_id[key].wall_reflectance for key in ids}
                if len(reflectances) != 1:
                    raise IfcExportError('Shared wall faces with different reflectances require separate finish layers')
                walls.append(Wall(f'wall-{len(walls) + 1:03d}', polygon, axis, ids, reflectances.pop()))
            assigned = assigned.union(candidate)
    return walls


def _openings(model: SpatialModel, rooms: list[SpatialRoom], polygons: dict[str, Polygon], walls: list[Wall],
              treatments: list[OpeningTreatment], thickness: float) -> list[Opening]:
    ids = [t.element_id for t in treatments]
    if len(set(ids)) != len(ids):
        raise IfcExportError('Duplicate opening treatment')
    treatments_by_id = {t.element_id: t for t in treatments}
    elements = sorted((e for e in model.elements if e.status != 'excluded' and e.kind in ('door', 'window')), key=lambda e: e.element_id)
    if set(ids) - {e.element_id for e in elements}:
        raise IfcExportError('Opening treatment references an absent or excluded opening')
    openings = []
    for element in elements:
        treatment = treatments_by_id.get(element.element_id)
        if treatment is None:
            if element.kind == 'window':
                raise IfcExportError(f'Window {element.element_id}: explicit glazing transmission/thickness assumptions are required')
            treatment = OpeningTreatment(element_id=element.element_id)
        if (element.kind == 'window') != (treatment.filling == 'glazing'):
            raise IfcExportError(f'Element {element.element_id}: filling type conflicts with door/window kind')
        if treatment.panel_thickness_m > thickness + EPS:
            raise IfcExportError('Panel thickness exceeds the wall thickness')
        if element.reflectance + treatment.visible_transmittance > 1 + EPS:
            raise IfcExportError('Reflection plus transmission must not exceed one')
        polygon = _rectangle(Polygon([(p.x * model.meters_per_unit, p.y * model.meters_per_unit) for p in element.footprint]), element.element_id)
        hosts = [w for w in walls if w.polygon.buffer(EPS).covers(polygon)]
        if len(hosts) != 1:
            raise IfcExportError(f'Opening {element.element_id}: requires exactly one wall host, away from corners/junctions')
        wall = hosts[0]
        x0, y0, x1, y1 = polygon.bounds
        depth, width = (y1 - y0, x1 - x0) if wall.axis == 'x' else (x1 - x0, y1 - y0)
        if not math.isclose(depth, thickness, abs_tol=EPS, rel_tol=0):
            raise IfcExportError(f'Opening {element.element_id}: footprint must cut through the full wall thickness')
        adjacent = tuple(r.room_id for r in rooms if math.isclose(polygons[r.room_id].boundary.intersection(polygon.boundary).length,
                                                                 width, abs_tol=EPS, rel_tol=0))
        if element.room_id not in adjacent or len(adjacent) > 2:
            raise IfcExportError(f'Opening {element.element_id}: not entirely on the named room face')
        for room in rooms:
            if room.room_id in adjacent and element.elevation_m + element.height_m > room.elevation_m + room.ceiling_height_m + EPS:
                raise IfcExportError('Opening exceeds an adjacent room ceiling')
        for previous in openings:
            z_overlap = min(element.elevation_m + element.height_m, previous.element.elevation_m + previous.element.height_m) - max(element.elevation_m, previous.element.elevation_m)
            if z_overlap > EPS and polygon.intersection(previous.polygon).area > EPS:
                raise IfcExportError('Openings overlap; duplicate doors from adjacent rooms must be reconciled')
        openings.append(Opening(element, polygon, wall, treatment, adjacent))
    return openings


def _assign_opening_shape(document: Any, body: Any, product: Any, opening: Opening, depth: float) -> float:
    """IFC door/window local X is width; local Y is the wall normal.

    World-coordinate profiles alone produce valid solids, but DIALux infers
    an incorrect face/width for a door in a wall running along world Y.
    """
    x0, y0, x1, y1 = opening.polygon.bounds
    width = x1 - x0 if opening.wall.axis == 'x' else y1 - y0
    _assign_profile_shape(document, body, product, box(0, -depth / 2, width, depth / 2), opening.element.height_m, 0)
    matrix = np.eye(4)
    if opening.wall.axis == 'x':
        matrix[:3, 3] = [x0, (y0 + y1) / 2, opening.element.elevation_m]
    else:
        matrix[:3, 0] = [0, 1, 0]
        matrix[:3, 1] = [-1, 0, 0]
        matrix[:3, 3] = [(x0 + x1) / 2, y0, opening.element.elevation_m]
    ifcopenshell.api.geometry.edit_object_placement(document, product=product, matrix=matrix, is_si=True)
    return width


def export_envelope(model: SpatialModel, options: IfcExportOptions,
                    treatments: list[OpeningTreatment] | None = None) -> EnvelopeExport:
    # Revalidate mutated Pydantic objects as well as incoming JSON.
    model = SpatialModel.model_validate(model.model_dump())
    treatments = [OpeningTreatment.model_validate(t.model_dump()) for t in (treatments or [])]
    rooms, polygons = _validate_model(model)
    walls = wall_topology(rooms, polygons, options.wall_thickness_m)
    openings = _openings(model, rooms, polygons, walls, treatments, options.wall_thickness_m)
    document = ifcopenshell.api.project.create_file(version=IFC_SCHEMA)
    project = ifcopenshell.api.root.create_entity(document, ifc_class='IfcProject', name=options.project_name)
    _assign_pset(document, project, 'Pset_LightingAgentSource', {'ProjectId': options.project_id,
        'SourceModelVersion': model.version, 'SourceCADSHA256': model.source_sha256,
        'ExportProfile': 'standalone-rectangular-envelope-v1', 'LightingMode': 'artificial_only'})
    units = [ifcopenshell.api.unit.add_si_unit(document, unit_type=kind) for kind in ('LENGTHUNIT', 'AREAUNIT', 'VOLUMEUNIT')]
    ifcopenshell.api.unit.assign_unit(document, units=units)
    context = ifcopenshell.api.context.add_context(document, context_type='Model')
    body = ifcopenshell.api.context.add_context(document, context_type='Model', context_identifier='Body', target_view='MODEL_VIEW', parent=context)
    project.RepresentationContexts = (context,)
    site = ifcopenshell.api.root.create_entity(document, ifc_class='IfcSite', name='Site')
    building = ifcopenshell.api.root.create_entity(document, ifc_class='IfcBuilding', name=options.project_name)
    storey = ifcopenshell.api.root.create_entity(document, ifc_class='IfcBuildingStorey', name=rooms[0].floor)
    storey.Elevation = rooms[0].elevation_m
    for parent, child in ((project, site), (site, building), (building, storey)):
        ifcopenshell.api.aggregate.assign_object(document, products=[child], relating_object=parent)
        child.CompositionType = 'ELEMENT'
        matrix = np.eye(4)
        if child == storey:
            matrix[2, 3] = storey.Elevation
        ifcopenshell.api.geometry.edit_object_placement(document, product=child, matrix=matrix, is_si=True)
    spaces = {}
    for room in rooms:
        space = ifcopenshell.api.root.create_entity(document, ifc_class='IfcSpace', name=room.number)
        space.CompositionType, space.LongName, space.Description = 'ELEMENT', room.name, room.usage
        _assign_profile_shape(document, body, space, polygons[room.room_id], room.ceiling_height_m, room.elevation_m)
        ifcopenshell.api.aggregate.assign_object(document, products=[space], relating_object=storey)
        _assign_source_pset(document, space, model, room.room_id)
        _assign_pset(document, space, 'Pset_LightingAgentRoom', {'AreaM2': polygons[room.room_id].area,
            'ClearHeightM': room.ceiling_height_m, 'StoreyHeightM': room.height_m,
            'WallReflectance': room.wall_reflectance, 'CeilingReflectance': room.ceiling_reflectance,
            'FloorReflectance': room.floor_reflectance})
        _create_floor_slab(document, body, storey, polygons[room.room_id], room.elevation_m,
                           room.floor_reflectance, options.floor_slab_thickness_m, model, room.room_id)
        _create_ceiling_slab(document, body, storey, polygons[room.room_id], room.elevation_m, room, options, model)
        spaces[room.room_id] = space
    products = {}
    for wall in walls:
        product = ifcopenshell.api.root.create_entity(document, ifc_class='IfcWall', predefined_type='SOLIDWALL', name=wall.key)
        _assign_profile_shape(document, body, product, wall.polygon, rooms[0].height_m, rooms[0].elevation_m)
        ifcopenshell.api.spatial.assign_container(document, products=[product], relating_structure=storey)
        _assign_material(document, product, f'Wall finish R{wall.reflectance:.2f}', wall.reflectance)
        _assign_pset(document, product, 'Pset_LightingAgentSource', {'RoomIds': json.dumps(wall.room_ids),
            'SourceModelVersion': model.version, 'SourceCADSHA256': model.source_sha256})
        _assign_pset(document, product, 'Pset_LightingAgentGeometryAssumptions', {'WallThicknessM': options.wall_thickness_m,
            'ThicknessSource': 'explicit export assumption; clear room faces preserved'})
        for room_id in wall.room_ids:
            document.create_entity('IfcRelSpaceBoundary', GlobalId=ifcopenshell.guid.new(), RelatingSpace=spaces[room_id],
                RelatedBuildingElement=product, PhysicalOrVirtualBoundary='PHYSICAL',
                InternalOrExternalBoundary='INTERNAL' if len(wall.room_ids) == 2 else 'EXTERNAL')
        products[wall.key] = product
    opening_records = []
    for opening in openings:
        element, treatment = opening.element, opening.treatment
        void = ifcopenshell.api.root.create_entity(document, ifc_class='IfcOpeningElement', predefined_type='OPENING', name=f'{element.element_id} void')
        _assign_opening_shape(document, body, void, opening, options.wall_thickness_m)
        ifcopenshell.api.feature.add_feature(document, feature=void, element=products[opening.wall.key])
        _assign_source_pset(document, void, model, element.room_id)
        record = {'element_id': element.element_id, 'host_wall': opening.wall.key, 'room_ids': opening.room_ids,
                  'void_global_id': void.GlobalId, 'filling_global_id': None, 'treatment': treatment.model_dump(),
                  'reflectance': element.reflectance, 'bounds_m': opening.polygon.bounds,
                  'elevation_m': element.elevation_m, 'height_m': element.height_m}
        if treatment.filling != 'open_passage':
            is_door = treatment.filling == 'closed_door'
            panel = ifcopenshell.api.root.create_entity(document, ifc_class='IfcDoor' if is_door else 'IfcWindow',
                predefined_type='DOOR' if is_door else 'WINDOW', name=element.name or element.element_id)
            panel.OverallWidth = _assign_opening_shape(document, body, panel, opening, treatment.panel_thickness_m)
            panel.OverallHeight = element.height_m
            if is_door:
                panel.OperationType = 'NOTDEFINED'
            else:
                panel.PartitioningType = 'SINGLE_PANEL'
            ifcopenshell.api.spatial.assign_container(document, products=[panel], relating_structure=storey)
            ifcopenshell.api.feature.add_filling(document, opening=void, element=panel)
            _assign_material(document, panel, element.material, element.reflectance)
            if not is_door:
                for item in panel.Representation.Representations[0].Items:
                    item.StyledByItem[0].Styles[0].Styles[0].Transparency = treatment.visible_transmittance
            _assign_source_pset(document, panel, model, element.room_id)
            _assign_pset(document, panel, 'Pset_LightingAgentOpening', {'ElementId': element.element_id,
                'Filling': treatment.filling, 'VisibleTransmittance': treatment.visible_transmittance,
                'RefractiveIndex': treatment.refractive_index,
                'PanelThicknessM': treatment.panel_thickness_m, 'AssumptionNote': treatment.assumption_note,
                'OpticalStatus': 'IFC style value; DIALux optical read-back required'})
            record['filling_global_id'] = panel.GlobalId
        opening_records.append(record)
    for element in model.elements:
        if element.status != 'excluded' and element.kind not in ('door', 'window'):
            room = next(r for r in rooms if r.room_id == element.room_id)
            _create_element(document, body, storey, model, room, element, room.elevation_m)
    data = document.to_string().encode('utf-8')
    reopened = ifcopenshell.file.from_string(data.decode('utf-8'))
    if len(reopened.by_type('IfcSpace')) != len(rooms) or len(reopened.by_type('IfcRelVoidsElement')) != len(openings):
        raise IfcExportError('Envelope serialization lost a room/opening')
    result = IfcExportResult(data=data, sha256=hashlib.sha256(data).hexdigest(), room_count=len(rooms),
        wall_count=len(walls), slab_count=2 * len(rooms), element_count=sum(e.status != 'excluded' for e in model.elements))
    return EnvelopeExport(result, {'schema_version': 1, 'profile': 'standalone-rectangular-envelope-v1',
        'lighting_mode': 'artificial_only', 'source_cad_sha256': model.source_sha256,
        'model_version': model.version, 'ifc_sha256': result.sha256, 'dialux_import_verified': False,
        'dialux_optics_verified': False, 'real_calculation_completed': False,
        'rooms': [{'room_id': r.room_id, 'name': f'{r.number} - {r.name}', 'bounds_m': polygons[r.room_id].bounds,
                   'area_m2': polygons[r.room_id].area, 'elevation_m': r.elevation_m, 'clear_height_m': r.ceiling_height_m} for r in rooms],
        'walls': [{'wall_id': w.key, 'room_ids': w.room_ids, 'area_m2': w.polygon.area,
                   'footprint_m': list(w.polygon.exterior.coords), 'reflectance': w.reflectance} for w in walls],
        'openings': opening_records,
        'limitations': ['one storey; axis-aligned rectangular clear room outlines',
                        'uniform explicit wall thickness; common storey height; same finish on both sides of a shared wall',
                        'simplified panels without frames; open_passage contains no swung door leaf',
                        'IFC transparency is not evidence of DIALux optical transmission; desktop verification required']})
