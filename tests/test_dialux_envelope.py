from __future__ import annotations

from pathlib import Path
import json
import runpy

import ifcopenshell
import ifcopenshell.geom
from ifcopenshell.util.element import get_psets
from ifcopenshell.util.shape import get_volume
from ifcopenshell.validate import json_logger, validate
import pytest
from shapely.geometry import Polygon
from shapely.ops import unary_union

from lighting_agent.dialux_runner.envelope import OpeningTreatment, export_envelope
from lighting_agent.dialux_runner.envelope_jobs import prepare_envelope, validate_envelope_inputs, validate_envelope_job
from lighting_agent.dialux_runner.envelope_run import require_two_scene_lamps
from lighting_agent.dialux_runner.artifacts import RunError, RunStore, atomic_json
from lighting_agent.ifc_export import IfcExportError, IfcExportOptions
from lighting_agent.schemas import CadPoint, SpatialElement, SpatialModel


OPTIONS = IfcExportOptions(.12, .15, .10, 'IFC envelope fixture', 'synthetic-envelope')


def rectangle(x0, y0, x1, y1):
    return [CadPoint(x=x, y=y) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]


def fixture():
    model = SpatialModel.model_validate_json((Path(__file__).parent / 'fixtures/phase0/bridge-spatial-model.json').read_text(encoding='utf-8'))
    second = model.rooms[0].model_copy(deep=True)
    second.room_id, second.number = 'room-2', '102'
    second.boundary = rectangle(6.12, 0, 12.12, 4)
    model.rooms.append(second)
    model.elements.append(SpatialElement(element_id='door-1', kind='door', room_id='room-1',
        footprint=rectangle(6, 1.5, 6.12, 2.5), height_m=2.1, elevation_m=0,
        rotation_deg=90, material='Opaque door', reflectance=.5, status='confirmed'))
    return model


def shape(product):
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    return ifcopenshell.geom.create_shape(settings, product)


def test_shared_wall_is_single_solid_and_door_really_cuts_a_void():
    result = export_envelope(fixture(), OPTIONS)
    document = ifcopenshell.file.from_string(result.ifc.data.decode())
    logger = json_logger()
    validate(document, logger, express_rules=True)
    assert not [entry for entry in logger.statements if entry['level'] == 'error']
    assert (result.ifc.room_count, result.ifc.wall_count, result.ifc.slab_count) == (2, 7, 4)
    shared = [w for w in result.manifest['walls'] if len(w['room_ids']) == 2]
    assert len(shared) == 1
    footprints = [Polygon(w['footprint_m']) for w in result.manifest['walls']]
    assert sum(p.area for p in footprints) == pytest.approx(unary_union(footprints).area)
    wall = next(w for w in document.by_type('IfcWall') if w.Name == shared[0]['wall_id'])
    assert get_volume(shape(wall).geometry) == pytest.approx(.12 * 4 * 3 - .12 * 1 * 2.1)
    door = document.by_type('IfcDoor')[0]
    assert get_volume(shape(door).geometry) == pytest.approx(.04 * 1 * 2.1)
    assert door.FillsVoids[0].RelatingOpeningElement.VoidsElements[0].RelatingBuildingElement == wall
    assert len(wall.ProvidesBoundaries) == 2
    pset = get_psets(door)['Pset_LightingAgentOpening']
    assert pset['Filling'] == 'closed_door'
    assert pset['VisibleTransmittance'] == 0
    assert result.manifest['dialux_import_verified'] is False
    assert result.manifest['real_calculation_completed'] is False


def test_passage_is_an_explicit_leafless_control_and_never_an_open_door():
    treatment = OpeningTreatment(element_id='door-1', filling='open_passage', assumption_note='Explicit empty passage control')
    result = export_envelope(fixture(), OPTIONS, [treatment])
    document = ifcopenshell.file.from_string(result.ifc.data.decode())
    assert len(document.by_type('IfcOpeningElement')) == 1
    assert not document.by_type('IfcDoor')
    assert not document.by_type('IfcRelFillsElement')
    assert result.manifest['openings'][0]['treatment']['filling'] == 'open_passage'


def test_window_requires_explicit_optics_and_preserves_the_supplied_values():
    model = fixture()
    model.elements.append(SpatialElement(element_id='window-1', kind='window', room_id='room-1',
        footprint=rectangle(2, -.12, 4, 0), height_m=1.2, elevation_m=.9,
        rotation_deg=0, material='Test glass', reflectance=.1, status='confirmed'))
    with pytest.raises(IfcExportError, match='explicit glazing'):
        export_envelope(model, OPTIONS)
    treatment = OpeningTreatment(element_id='window-1', filling='glazing', panel_thickness_m=.008,
        visible_transmittance=.7, refractive_index=1.52, assumption_note='Synthetic glass only')
    result = export_envelope(model, OPTIONS, [treatment])
    document = ifcopenshell.file.from_string(result.ifc.data.decode())
    window = document.by_type('IfcWindow')[0]
    assert get_volume(shape(window).geometry) == pytest.approx(.008 * 2 * 1.2)
    assert get_psets(window)['Pset_LightingAgentOpening']['VisibleTransmittance'] == .7
    assert window.Representation.Representations[0].Items[0].StyledByItem[0].Styles[0].Styles[0].Transparency == .7
    assert not result.manifest['dialux_optics_verified']


@pytest.mark.parametrize('fault', ['no_wall_gap', 'overlap', 'ambiguous_gap', 'different_finish', 'different_floor',
    'pending_room', 'pending_door', 'wrong_room', 'outside_room', 'partial_void', 'duplicate_opening',
    'above_ceiling', 'bad_area', 'duplicate_room_id', 'nonrectangular'])
def test_incomplete_or_ambiguous_geometry_is_rejected(fault):
    model = fixture()
    room, door = model.rooms[1], model.elements[-1]
    if fault in ('no_wall_gap', 'overlap', 'ambiguous_gap'):
        x = {'no_wall_gap': 6, 'overlap': 5, 'ambiguous_gap': 6.18}[fault]
        room.boundary = rectangle(x, 0, x + 6, 4)
    elif fault == 'different_finish':
        room.wall_reflectance = .6
    elif fault == 'different_floor':
        room.floor = '2F'
    elif fault == 'pending_room':
        room.status = 'pending'
    elif fault == 'pending_door':
        door.status = 'pending'
    elif fault == 'wrong_room':
        door.room_id = 'missing'
    elif fault == 'outside_room':
        model.elements[0].footprint = rectangle(20, 1, 21, 2)
    elif fault == 'partial_void':
        door.footprint = rectangle(6, 1.5, 6.06, 2.5)
    elif fault == 'duplicate_opening':
        model.elements.append(door.model_copy(update={'element_id': 'door-2', 'room_id': 'room-2'}))
    elif fault == 'above_ceiling':
        door.elevation_m = 1
    elif fault == 'bad_area':
        room.area_m2 = 30
    elif fault == 'duplicate_room_id':
        room.room_id = 'room-1'
    else:
        room.boundary[1].x += 1
    with pytest.raises(IfcExportError):
        export_envelope(model, OPTIONS)


def test_drawing_scale_and_nonzero_level_elevation_are_applied_once_to_void_and_leaf():
    model = fixture()
    model.meters_per_unit = .001
    for room in model.rooms:
        room.boundary = [CadPoint(x=p.x * 1000, y=p.y * 1000) for p in room.boundary]
        room.elevation_m = 3.2
    for element in model.elements:
        element.footprint = [CadPoint(x=p.x * 1000, y=p.y * 1000) for p in element.footprint]
        element.elevation_m += 3.2
    result = export_envelope(model, OPTIONS)
    document = ifcopenshell.file.from_string(result.ifc.data.decode())
    for kind, height in [('IfcOpeningElement', 2.1), ('IfcDoor', 2.1), ('IfcSpace', 2.8)]:
        geometry = shape(document.by_type(kind)[0]).geometry
        assert min(geometry.verts[2::3]) == pytest.approx(3.2)
        assert max(geometry.verts[2::3]) == pytest.approx(3.2 + height)
    assert max(shape(document.by_type('IfcDoor')[0]).geometry.verts[0::3]) == pytest.approx(6.08)


@pytest.mark.parametrize('treatment', [
    {'element_id': 'door-1', 'visible_transmittance': .1},
    {'element_id': 'door-1', 'filling': 'open_passage'},
    {'element_id': 'window-1', 'filling': 'glazing', 'assumption_note': 'Explicit'},
    {'element_id': 'door-1', 'panel_thickness_m': float('nan')},
])
def test_no_implicit_open_or_transmitting_door(treatment):
    with pytest.raises(ValueError):
        OpeningTreatment(**treatment)


@pytest.fixture
def envelope_inputs(tmp_path):
    prepare = runpy.run_path(str(Path(__file__).parents[1] / 'scripts/prepare_dialux_envelope.py'))['prepare']
    root = tmp_path / 'source'
    prepare(root)
    return root


def test_envelope_job_has_immutable_optical_bindings_and_no_native_template(envelope_inputs, tmp_path):
    executable = tmp_path / 'DIALux_x64.exe'
    executable.touch()
    output = tmp_path / 'job'
    prepare_envelope(envelope_inputs, output, executable)
    store = RunStore(output)
    validate_envelope_job(store)
    assert 'native_input' not in store.job
    assert store.job['settings']['daylight'] is False
    assert len(store.job['rooms']) == len(store.job['layout']) == 2
    assert store.job['opening_treatments'][0]['filling'] == 'closed_door'
    assert store.job['opening_treatments'][1]['refractive_index'] == 1.52
    store.job['opening_treatments'][0]['filling'] = 'open_passage'
    with pytest.raises(RunError, match='bindings'):
        validate_envelope_job(store)


@pytest.mark.parametrize('fault', ['glazing', 'room', 'ifc', 'cad_bulge', 'options'])
def test_envelope_input_tampering_cannot_be_hidden_by_prepared_flags(envelope_inputs, fault):
    root = envelope_inputs
    if fault == 'ifc':
        path = root / 'bridge.ifc'
        path.write_text(path.read_text(encoding='utf-8').replace('Synthetic opaque door', 'Different door'), encoding='utf-8')
    elif fault == 'cad_bulge':
        import ezdxf
        from lighting_agent.dialux_runner.artifacts import sha256
        cad = ezdxf.readfile(root / 'bridge.dxf')
        entity = list(cad.modelspace())[0]
        points = list(entity.get_points('xyb'))
        points[0] = (*points[0][:2], .2)
        entity.set_points(points, format='xyb')
        cad.saveas(root / 'bridge.dxf')
        model = json.loads((root / 'spatial-model.json').read_text(encoding='utf-8'))
        model['source_sha256'] = sha256(root / 'bridge.dxf')
        atomic_json(root / 'spatial-model.json', model)
    else:
        filename = {'glazing': 'opening-treatments.json', 'room': 'spatial-model.json', 'options': 'export-options.json'}[fault]
        data = json.loads((root / filename).read_text(encoding='utf-8'))
        if fault == 'glazing':
            data[1]['visible_transmittance'] = .6
        elif fault == 'room':
            data['rooms'][1]['ceiling_height_m'] = 2.9
        else:
            data['wall_thickness_m'] = .15
        atomic_json(root / filename, data)
    with pytest.raises(RunError):
        validate_envelope_inputs(root)


@pytest.mark.parametrize('label,selected,valid', [
    ('2个灯具（选择了1个）', 1, True),
    ('2个灯具（选择了2个）', 2, True),
    ('2个灯具', None, True),
    ('1个灯具', None, False),
    ('2个灯具（选择了1个）', 2, False),
    ('2个灯具（选择了2个）', None, False),
])
def test_scene_total_is_separate_from_selected_lamp_count(label, selected, valid):
    snapshot = {'controls': [{'name': label, 'id': '', 'type': 'Text', 'offscreen': False},
                             {'name': str(selected), 'id': 'SelectedLuminaireCount', 'type': 'Text',
                              'offscreen': selected is None}]}
    if valid:
        require_two_scene_lamps(snapshot, selected=selected)
    else:
        with pytest.raises(RunError):
            require_two_scene_lamps(snapshot, selected=selected)


def test_applied_dimming_allows_selected_or_unselected_two_lamp_group():
    for label, selected in [('2个灯具', None), ('2个灯具（选择了2个）', 2)]:
        snapshot = {'controls': [{'name': label, 'id': '', 'type': 'Text', 'offscreen': False},
                                 {'name': str(selected), 'id': 'SelectedLuminaireCount', 'type': 'Text',
                                  'offscreen': selected is None}]}
        require_two_scene_lamps(snapshot, selected=2, allow_unselected=True)
