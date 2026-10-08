"""Prepare a two-room door/window test; no DIALux actions or acceptance claims."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
import shutil

import ezdxf

from lighting_agent.dialux_runner.artifacts import atomic_json, sha256, timestamp
from lighting_agent.dialux_runner.envelope import OpeningTreatment, export_envelope
from lighting_agent.ifc_export import IfcExportOptions, write_ifc
from lighting_agent.schemas import CadPoint, SpatialElement, SpatialModel


def fixture_model(fixtures: Path) -> SpatialModel:
    model = SpatialModel.model_validate_json((fixtures / 'bridge-spatial-model.json').read_text(encoding='utf-8'))
    model.version = 8
    model.rooms[0].name = 'Envelope room A'
    room_b = model.rooms[0].model_copy(deep=True)
    room_b.room_id, room_b.number, room_b.name = 'room-2', '102', 'Envelope room B'
    room_b.boundary = [CadPoint(x=x, y=y) for x, y in ((6.12, 0), (12.12, 0), (12.12, 4), (6.12, 4))]
    model.rooms.append(room_b)
    model.elements.extend([
        SpatialElement(element_id='door-1', kind='door', name='Closed test door', room_id='room-1',
            footprint=[CadPoint(x=x, y=y) for x, y in ((6, 1.5), (6.12, 1.5), (6.12, 2.5), (6, 2.5))],
            length_m=1, width_m=.12, height_m=2.1, elevation_m=0, rotation_deg=90,
            material='Synthetic opaque door', reflectance=.5, status='confirmed'),
        SpatialElement(element_id='window-1', kind='window', name='Test glazing', room_id='room-1',
            footprint=[CadPoint(x=x, y=y) for x, y in ((2, -.12), (4, -.12), (4, 0), (2, 0))],
            length_m=2, width_m=.12, height_m=1.2, elevation_m=.9, rotation_deg=0,
            material='Synthetic test glazing', reflectance=.1, status='confirmed'),
    ])
    model.audit_log = ['Synthetic two-room envelope: dimensions, materials, glazing and closed-door panel are test assumptions; artificial lighting only.']
    return model


def prepare(output: Path, *, passage: bool = False) -> None:
    fixtures = Path(__file__).resolve().parents[1] / 'tests/fixtures/phase0'
    model = fixture_model(fixtures)
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    cad = ezdxf.new('R2010')
    cad.units = ezdxf.units.M
    for room in model.rooms:
        layer = 'ROOM_' + room.room_id
        cad.layers.new(layer)
        cad.modelspace().add_lwpolyline([(p.x, p.y) for p in room.boundary], close=True, dxfattribs={'layer': layer})
    for element in model.elements:
        layer = element.kind.upper() + '_' + element.element_id
        cad.layers.new(layer)
        cad.modelspace().add_lwpolyline([(p.x, p.y) for p in element.footprint], close=True, dxfattribs={'layer': layer})
    cad.saveas(output / 'bridge.dxf')
    model.source_sha256 = sha256(output / 'bridge.dxf')
    treatments = [OpeningTreatment(element_id='window-1', filling='glazing', panel_thickness_m=.008,
        visible_transmittance=.7, refractive_index=1.52, assumption_note='Synthetic clear test pane: R=0.10, T=0.70, n=1.52, thickness=8 mm; verify optical values in DIALux.')]
    if passage:
        treatments.append(OpeningTreatment(element_id='door-1', filling='open_passage',
            assumption_note='Synthetic control case: empty passage without any door leaf; not a hinged-open door.'))
    options = IfcExportOptions(.12, .15, .10, 'IFC envelope fixture', 'synthetic-envelope')
    result = export_envelope(model, options, treatments)
    write_ifc(result.ifc, output / 'bridge.ifc')
    atomic_json(output / 'spatial-model.json', model.model_dump())
    # Save resolved defaults so the actual assumptions are reviewable and hashed.
    atomic_json(output / 'opening-treatments.json', [o['treatment'] for o in result.manifest['openings']])
    atomic_json(output / 'envelope-manifest.json', result.manifest)
    atomic_json(output / 'export-options.json', asdict(options))
    shutil.copyfile(fixtures / 'synthetic-bridge-luminaire.ies', output / 'synthetic-bridge-luminaire.ies')
    atomic_json(output / 'input-manifest.json', {'prepared_at': timestamp(), 'state': 'inputs_prepared',
        'purpose': 'synthetic envelope validation only; not design/compliance evidence',
        'artifacts': {p.name: sha256(p) for p in sorted(output.iterdir()) if p.is_file()}})
    print(output)
    print(f'IFC SHA256: {result.ifc.sha256}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--passage', action='store_true', help='Explicit empty-passage control instead of the default closed door')
    args = parser.parse_args()
    prepare(args.output_dir, passage=args.passage)


if __name__ == '__main__':
    main()
