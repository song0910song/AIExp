"""Fresh IFC/IES profile for the synthetic, artificial-light bridge fixture.

This profile deliberately does not accept arbitrary geometry or design rules.
Its inputs are exchange files, never a native project containing old results.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import os
from pathlib import Path
import re
import shutil
from uuid import uuid4

import ezdxf
import ifcopenshell

from lighting_agent.ifc_export import IfcExportOptions, export_spatial_model
from lighting_agent.schemas import SpatialModel
from .artifacts import (MAX_LIMITS, REPLAY_EXPECTED, RunError, atomic_json,
                        sha256, timestamp, verify_artifacts)

IMPORT_PROFILE = 'evo-5.14.0.3-zh-synthetic-ifc-v1'
IES_SHA256 = '94517eb1cdc558469ee82c37704b389155351e5be5199fe4a88364d81f1f1255'
OPTIONS = IfcExportOptions(.12, .15, .10, 'IFC bridge fixture', 'synthetic-bridge')
LAYOUT = [{'name': 'Bridge lamp 1', 'position_m': [3., 2., 2.5], 'rotation_deg': [0., 0., 0.]}]
SETTINGS = {'lighting_mode': 'artificial_only', 'daylight': False,
            'workplane_height_m': .8, 'edge_margin_m': .5, 'maintenance_factor': .8,
            'grid_mode': 'dialux_workplane_adaptive', 'direct_only': False, 'exclude_furniture': False,
            'virtual_surfaces_only': False, 'simplified_furniture': False}
INPUT_NAMES = {'bridge.dxf', 'spatial-model.json', 'bridge.ifc', 'synthetic-bridge-luminaire.ies'}


def validate_model(model: SpatialModel) -> None:
    # All dimensions/materials below are explicitly synthetic test assumptions.
    if (len(model.rooms) != 1 or len(model.elements) != 1 or model.meters_per_unit != 1
            or model.version != 7 or not model.coverage_confirmed or not model.elements_reviewed
            or not model.design_ready or model.outstanding):
        raise RunError('Fresh IFC profile requires the confirmed synthetic single-room model')
    room, desk = model.rooms[0], model.elements[0]
    expected_room = {'room_id': 'room-1', 'floor': '1F', 'number': '101', 'name': 'Bridge test room',
                     'usage': 'Office', 'area_m2': 24., 'elevation_m': 0., 'height_m': 3.,
                     'ceiling_height_m': 2.8, 'wall_reflectance': .5, 'ceiling_reflectance': .7,
                     'floor_reflectance': .2, 'status': 'confirmed'}
    expected_desk = {'element_id': 'desk-1', 'kind': 'furniture', 'name': 'Test desk',
                     'room_id': 'room-1', 'length_m': 1.2, 'width_m': .6, 'elevation_m': 0.,
                     'height_m': .75, 'rotation_deg': 0., 'material': 'Synthetic laminate',
                     'reflectance': .3, 'status': 'confirmed'}
    if any(getattr(room, k) != v for k, v in expected_room.items()) or room.holes:
        raise RunError('Room differs from the synthetic IFC profile')
    if any(getattr(desk, k) != v for k, v in expected_desk.items()):
        raise RunError('Furniture differs from the synthetic IFC profile')
    if ([(p.x, p.y) for p in room.boundary] != [(0, 0), (6, 0), (6, 4), (0, 4)]
            or [(p.x, p.y) for p in desk.footprint] != [(1, 1), (2.2, 1), (2.2, 1.6), (1, 1.6)]):
        raise RunError('Geometry differs from the synthetic IFC profile')


def canonical_ifc(data: str) -> str:
    """Ignore generated GUIDs/header time, preserve every exported entity field."""
    document = ifcopenshell.file.from_string(data)
    if document.schema != 'IFC4':
        raise RunError('Expected IFC4 exchange file')
    for entity in document.by_type('IfcRoot'):
        entity.GlobalId = f'{entity.id():022d}'
    # IFC SET attributes have no ordering; IfcOpenShell emits these from sets.
    for entity_type, attribute in (('IfcUnitAssignment', 'Units'),
                                   ('IfcRelContainedInSpatialStructure', 'RelatedElements'),
                                   ('IfcRelAggregates', 'RelatedObjects')):
        for entity in document.by_type(entity_type):
            setattr(entity, attribute, sorted(getattr(entity, attribute), key=lambda item: item.id()))
    return document.to_string().split('DATA;', 1)[1]


def validate_exchange_inputs(root: Path) -> SpatialModel:
    model = SpatialModel.model_validate_json((root / 'spatial-model.json').read_text(encoding='utf-8'))
    validate_model(model)
    if sha256(root / 'bridge.dxf') != model.source_sha256:
        raise RunError('CAD hash does not match the confirmed model')
    try:
        cad = ezdxf.readfile(root / 'bridge.dxf')
        expected = {'ROOM_BOUNDARY': [(p.x, p.y) for p in model.rooms[0].boundary],
                    'FURNITURE': [(p.x, p.y) for p in model.elements[0].footprint]}
        if cad.units != ezdxf.units.M or len(cad.modelspace()) != 2:
            raise RunError('Synthetic CAD must contain the metre-based room and furniture outlines')
        for layer, points in expected.items():
            entities = list(cad.modelspace().query(f'LWPOLYLINE[layer=="{layer}"]'))
            if len(entities) != 1 or not entities[0].closed or list(entities[0].get_points('xy')) != points:
                raise RunError('Synthetic CAD geometry differs from the confirmed model')
    except (OSError, ezdxf.DXFError) as error:
        raise RunError(f'Cannot validate synthetic CAD: {error}') from error
    if sha256(root / 'synthetic-bridge-luminaire.ies') != IES_SHA256:
        raise RunError('IES differs from the synthetic photometry profile')
    expected = export_spatial_model(model, OPTIONS)
    if canonical_ifc((root / 'bridge.ifc').read_text(encoding='utf-8')) != canonical_ifc(expected.data.decode('utf-8')):
        raise RunError('IFC geometry/material/source data do not match the confirmed model/export options')
    return model


def prepare_import(source: Path, output: Path, executable: Path) -> Path:
    source, output, executable = source.resolve(), output.resolve(), executable.resolve()
    model = validate_exchange_inputs(source)
    if not executable.is_file():
        raise RunError(f'DIALux executable is missing: {executable}')
    before = {name: sha256(source / name) for name in INPUT_NAMES}
    output.mkdir(parents=True, exist_ok=False)
    (output / 'inputs').mkdir()
    for name in sorted(INPUT_NAMES):
        shutil.copyfile(source / name, output / 'inputs' / name)
    hashes = {'inputs/' + name: value for name, value in before.items()}
    verify_artifacts(output, hashes)
    validate_exchange_inputs(output / 'inputs')
    job = {'schema_version': 1, 'profile': IMPORT_PROFILE, 'run_id': uuid4().hex,
           'prepared_at': timestamp(), 'purpose': 'synthetic IFC/IES execution validation; not project compliance',
           'dialux_executable': str(executable), 'dialux_version': '5.14.0.3', 'inputs': hashes,
           'ifc_input': 'inputs/bridge.ifc', 'ies_input': 'inputs/synthetic-bridge-luminaire.ies',
           'layout': LAYOUT, 'settings': SETTINGS, 'expected': REPLAY_EXPECTED,
           'export_options': asdict(OPTIONS), 'limits': dict(MAX_LIMITS),
           'versions': {'model': model.version, 'cad_sha256': model.source_sha256,
                        'layout': 'synthetic-one-lamp-v1', 'settings': 'artificial-only-v1',
                        'rules': None}}
    atomic_json(output / 'job.json', job)
    return output / 'job.json'


def validate_import_job(store) -> None:
    job = store.job
    if (job.get('dialux_version') != '5.14.0.3' or job.get('expected') != REPLAY_EXPECTED
            or job.get('layout') != LAYOUT or job.get('settings') != SETTINGS
            or job.get('export_options') != asdict(OPTIONS)
            or job.get('ifc_input') != 'inputs/bridge.ifc'
            or job.get('ies_input') != 'inputs/synthetic-bridge-luminaire.ies'
            or 'native_input' in job):
        raise RunError('Fresh IFC profile layout/settings/inputs were changed')
    if set(job['inputs']) != {'inputs/' + name for name in INPUT_NAMES}:
        raise RunError('Fresh IFC input package is incomplete')
    model = validate_exchange_inputs(store.root / 'inputs')
    if job.get('versions') != {'model': model.version, 'cad_sha256': model.source_sha256,
                               'layout': 'synthetic-one-lamp-v1', 'settings': 'artificial-only-v1', 'rules': None}:
        raise RunError('Fresh IFC version bindings changed')


def parse_import_report(text: str, *, filename: str, version: str) -> dict:
    text = text.lstrip('\ufeff').replace('\r', '')
    names = re.findall(r'^File Name: (.+)$', text, re.MULTILINE)
    schemas = re.findall(r'^Schema Version: (.+)$', text, re.MULTILINE)
    notes = re.findall(r'^(?:Info|Warning|Error):.*$', text, re.MULTILINE)
    expected_note = f'Info: 使用 DIALux 版本导入: {version}'
    if not text.startswith('DIALux evo IFC 报告') or names != [filename] or schemas != ['IFC4']:
        raise RunError('IFC report identity/schema is missing or mismatched')
    # This accepted synthetic model imports without warnings. Any unfamiliar
    # message requires review, including skipped rooms, furniture or containers.
    tail = text.split('笔记', 1)
    if (len(tail) != 2 or notes != [expected_note]
            or [line.strip() for line in tail[1].splitlines() if line.strip() and set(line.strip()) != {'-'}] != notes):
        raise RunError('IFC report contains warnings, errors or unrecognised notes')
    return {'file': filename, 'schema': 'IFC4', 'dialux_version': version,
            'messages': notes, 'warning_count': 0, 'error_count': 0}


def archive_import_report(pid: int, directory: Path, *, after_ns: int) -> dict:
    source = Path(os.environ['LOCALAPPDATA']) / 'DIAL GmbH/DIALux/DIALuxProjectTemp' / f'DIALux_IfcReport_{pid}.txt'
    stat = source.stat()
    if stat.st_mtime_ns < after_ns:
        raise RunError('IFC report predates this dedicated process/import')
    data = source.read_bytes()
    target = directory / 'ifc-import-report.txt'
    if target.exists():
        raise RunError('Refusing to overwrite a prior IFC report')
    # Copy original bytes, even when the report fails validation.
    shutil.copyfile(source, target)
    if target.read_bytes() != data:
        raise RunError('IFC report changed while being archived')
    parsed = parse_import_report(data.decode('utf-8-sig'), filename='bridge.ifc', version='5.14.0.3')
    parsed.update(source_process_id=pid, source_modified_ns=stat.st_mtime_ns,
                  sha256=hashlib.sha256(data).hexdigest(), file=target.name)
    atomic_json(directory / 'ifc-import-report.json', parsed)
    return parsed
