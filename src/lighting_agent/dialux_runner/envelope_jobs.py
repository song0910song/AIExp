"""Immutable, narrowly bounded two-room envelope execution profile."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import shutil
from uuid import uuid4

import ezdxf

from lighting_agent.ifc_export import IfcExportOptions
from lighting_agent.schemas import SpatialModel
from .artifacts import MAX_LIMITS, REPLAY_EXPECTED, RunError, atomic_json, sha256, timestamp, verify_artifacts
from .envelope import OpeningTreatment, export_envelope
from .imports import IES_SHA256, SETTINGS, canonical_ifc, validate_model

ENVELOPE_PROFILE = 'evo-5.14.0.3-zh-synthetic-envelope-v1'
OPTIONS = IfcExportOptions(.12, .15, .10, 'IFC envelope fixture', 'synthetic-envelope')
ROOM_NAMES = ['101 - Envelope room A', '102 - Envelope room B']
EXPECTED = [{**REPLAY_EXPECTED, 'building': OPTIONS.project_name, 'room': name,
             'surface': f'工作面 ({name})', 'surface_index': None} for name in ROOM_NAMES]
LAYOUT = [{'name': f'Envelope lamp {i + 1}', 'room': name, 'position_m': [x, 2., 2.5],
           'rotation_deg': [0., 0., 0.]} for i, (name, x) in enumerate(zip(ROOM_NAMES, [3., 9.12]))]
INPUT_NAMES = {'bridge.dxf', 'bridge.ifc', 'spatial-model.json', 'opening-treatments.json',
               'export-options.json', 'synthetic-bridge-luminaire.ies'}


def validate_envelope_inputs(root: Path) -> tuple[SpatialModel, list[OpeningTreatment]]:
    model = SpatialModel.model_validate_json((root / 'spatial-model.json').read_text(encoding='utf-8'))
    if len(model.rooms) != 2 or len(model.elements) != 3 or model.version != 8:
        raise RunError('Envelope profile requires the synthetic two-room, three-element fixture')
    if [r.name for r in model.rooms] != ['Envelope room A', 'Envelope room B']:
        raise RunError('Envelope room identity differs')
    # Reuse the fixed phase-0 fixture checks for unchanged reviewed properties.
    for i, room in enumerate(model.rooms):
        expected_points = ([(0., 0.), (6., 0.), (6., 4.), (0., 4.)] if i == 0
                           else [(6.12, 0.), (12.12, 0.), (12.12, 4.), (6.12, 4.)])
        if room.room_id != f'room-{i + 1}' or room.number != str(101 + i) or [(p.x, p.y) for p in room.boundary] != expected_points:
            raise RunError('Envelope room coordinates/identity differ')
        candidate = model.model_copy(deep=True)
        candidate.version, candidate.rooms, candidate.elements = 7, [room.model_copy(deep=True)], [model.elements[0].model_copy(deep=True)]
        candidate.rooms[0].room_id, candidate.rooms[0].number, candidate.rooms[0].name = 'room-1', '101', 'Bridge test room'
        candidate.rooms[0].boundary = model.rooms[0].boundary
        validate_model(candidate)
    specs = [dict(element_id='door-1', kind='door', name='Closed test door', room_id='room-1', height_m=2.1,
                  elevation_m=0., reflectance=.5, material='Synthetic opaque door', rotation_deg=90., length_m=1., width_m=.12),
             dict(element_id='window-1', kind='window', name='Test glazing', room_id='room-1', height_m=1.2,
                  elevation_m=.9, reflectance=.1, material='Synthetic test glazing', rotation_deg=0., length_m=2., width_m=.12)]
    outlines = [[(6., 1.5), (6.12, 1.5), (6.12, 2.5), (6., 2.5)], [(2., -.12), (4., -.12), (4., 0.), (2., 0.)]]
    for element, spec, outline in zip(model.elements[1:], specs, outlines):
        if element.status != 'confirmed' or any(getattr(element, k) != v for k, v in spec.items()) or [(p.x, p.y) for p in element.footprint] != outline:
            raise RunError('Envelope opening differs from the synthetic geometry/material profile')
    treatments = [OpeningTreatment.model_validate(t) for t in json.loads((root / 'opening-treatments.json').read_text(encoding='utf-8'))]
    if len(treatments) != 2 or [t.element_id for t in treatments] != ['door-1', 'window-1']:
        raise RunError('Both resolved opening treatments must be recorded in order')
    door, glass = treatments
    if (door.filling not in ('closed_door', 'open_passage') or door.panel_thickness_m != .04
            or glass.filling != 'glazing' or glass.panel_thickness_m != .008
            or glass.visible_transmittance != .7 or glass.refractive_index != 1.52):
        raise RunError('Envelope profile opening optics differ from the synthetic test')
    if json.loads((root / 'export-options.json').read_text(encoding='utf-8')) != asdict(OPTIONS):
        raise RunError('Envelope export assumptions differ')
    if sha256(root / 'bridge.dxf') != model.source_sha256:
        raise RunError('CAD is not bound to the reviewed envelope model')
    cad = ezdxf.readfile(root / 'bridge.dxf')
    expected_cad = {'ROOM_' + r.room_id: [(p.x, p.y) for p in r.boundary] for r in model.rooms}
    expected_cad.update({e.kind.upper() + '_' + e.element_id: [(p.x, p.y) for p in e.footprint] for e in model.elements})
    if cad.units != ezdxf.units.M or len(cad.modelspace()) != len(expected_cad):
        raise RunError('Envelope CAD units/entity count differ')
    for layer, points in expected_cad.items():
        entities = list(cad.modelspace().query(f'LWPOLYLINE[layer=="{layer}"]'))
        if (len(entities) != 1 or not entities[0].closed or list(entities[0].get_points('xy')) != points
                or any(p[2] != 0 for p in entities[0].get_points('xyb')) or entities[0].dxf.elevation != 0):
            raise RunError('Envelope CAD exact geometry differs from the reviewed model')
    if sha256(root / 'synthetic-bridge-luminaire.ies') != IES_SHA256:
        raise RunError('Envelope profile requires the synthetic photometry fixture')
    generated = export_envelope(model, OPTIONS, treatments)
    if canonical_ifc((root / 'bridge.ifc').read_text(encoding='utf-8')) != canonical_ifc(generated.ifc.data.decode('utf-8')):
        raise RunError('IFC envelope geometry/materials/source/treatments differ from its reviewed inputs')
    return model, treatments


def prepare_envelope(source: Path, output: Path, executable: Path) -> Path:
    source, output, executable = source.resolve(), output.resolve(), executable.resolve()
    validate_envelope_inputs(source)
    if not executable.is_file():
        raise RunError('DIALux executable is missing')
    hashes = {'inputs/' + name: sha256(source / name) for name in sorted(INPUT_NAMES)}
    output.mkdir(parents=True, exist_ok=False)
    (output / 'inputs').mkdir()
    for name in sorted(INPUT_NAMES):
        shutil.copyfile(source / name, output / 'inputs' / name)
    verify_artifacts(output, hashes)
    model, treatments = validate_envelope_inputs(output / 'inputs')
    job = {'schema_version': 1, 'profile': ENVELOPE_PROFILE, 'run_id': uuid4().hex,
        'prepared_at': timestamp(), 'purpose': 'synthetic two-room envelope execution; not project compliance',
        'dialux_executable': str(executable), 'dialux_version': '5.14.0.3', 'inputs': hashes,
        'ifc_input': 'inputs/bridge.ifc', 'ies_input': 'inputs/synthetic-bridge-luminaire.ies',
        'expected': EXPECTED[0], 'rooms': EXPECTED, 'layout': LAYOUT, 'settings': SETTINGS,
        'opening_treatments': [t.model_dump() for t in treatments], 'export_options': asdict(OPTIONS),
        'limits': dict(MAX_LIMITS), 'versions': {'model': model.version, 'cad_sha256': model.source_sha256,
            'layout': 'synthetic-two-room-two-lamps-v1', 'settings': 'artificial-only-v1', 'rules': None}}
    atomic_json(output / 'job.json', job)
    return output / 'job.json'


def validate_envelope_job(store) -> None:
    job = store.job
    if (job.get('dialux_version') != '5.14.0.3' or job.get('rooms') != EXPECTED or job.get('expected') != EXPECTED[0]
            or job.get('layout') != LAYOUT or job.get('settings') != SETTINGS or 'native_input' in job
            or job.get('export_options') != asdict(OPTIONS) or job.get('ifc_input') != 'inputs/bridge.ifc'
            or job.get('ies_input') != 'inputs/synthetic-bridge-luminaire.ies'
            or set(job['inputs']) != {'inputs/' + name for name in INPUT_NAMES}):
        raise RunError('Envelope execution profile inputs/layout/settings were changed')
    model, treatments = validate_envelope_inputs(store.root / 'inputs')
    if job.get('opening_treatments') != [t.model_dump() for t in treatments] or job.get('versions') != {
            'model': model.version, 'cad_sha256': model.source_sha256, 'layout': 'synthetic-two-room-two-lamps-v1',
            'settings': 'artificial-only-v1', 'rules': None}:
        raise RunError('Envelope optical/source version bindings differ')
