"""Prepare synthetic phase-0 inputs; this does not run or validate DIALux."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import shutil

import ezdxf

from lighting_agent.ifc_export import IfcExportOptions, export_spatial_model, write_ifc
from lighting_agent.schemas import SpatialModel


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True, type=Path, help='New directory for this bridge run')
    args = parser.parse_args()
    fixtures = Path(__file__).resolve().parents[1] / 'tests' / 'fixtures' / 'phase0'
    model = SpatialModel.model_validate_json((fixtures / 'bridge-spatial-model.json').read_text(encoding='utf-8'))
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)

    cad = ezdxf.new('R2010')
    cad.units = ezdxf.units.M
    for layer in ('ROOM_BOUNDARY', 'FURNITURE'):
        cad.layers.new(layer)
    cad.modelspace().add_lwpolyline([(p.x, p.y) for p in model.rooms[0].boundary],
                                  close=True, dxfattribs={'layer': 'ROOM_BOUNDARY'})
    for element in model.elements:
        cad.modelspace().add_lwpolyline([(p.x, p.y) for p in element.footprint],
                                      close=True, dxfattribs={'layer': 'FURNITURE'})
    cad.saveas(output / 'bridge.dxf')
    model.source_sha256 = hashlib.sha256((output / 'bridge.dxf').read_bytes()).hexdigest()
    model.audit_log = ['Synthetic bridge only; heights, materials and furniture are test assumptions.']
    options = IfcExportOptions(project_name='IFC bridge fixture', project_id='synthetic-bridge',
        wall_thickness_m=0.12, floor_slab_thickness_m=0.15, ceiling_slab_thickness_m=0.10)
    result = export_spatial_model(model, options)
    write_ifc(result, output / 'bridge.ifc')
    (output / 'spatial-model.json').write_text(model.model_dump_json(indent=2), encoding='utf-8')
    shutil.copyfile(fixtures / 'synthetic-bridge-luminaire.ies', output / 'synthetic-bridge-luminaire.ies')
    artifacts = {}
    for path in sorted(output.iterdir()):
        artifacts[path.name] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'size_bytes': path.stat().st_size}
    manifest = {
        'schema_version': 1, 'prepared_at': datetime.now(UTC).isoformat(),
        'purpose': 'synthetic bridge validation only; not design or compliance evidence',
        'state': 'inputs_prepared', 'dialux_import_verified': False, 'real_calculation_completed': False,
        'native_project_saved': False, 'results_exported': False,
        'source_cad_sha256': model.source_sha256, 'spatial_model_version': model.version,
        'ifc_schema': result.schema, 'export_options': asdict(options), 'artifacts': artifacts,
    }
    (output / 'input-manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(output)
    print(f'IFC SHA256: {result.sha256}')


if __name__ == '__main__':
    main()
