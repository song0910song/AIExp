from pathlib import Path
import shutil
import os
import time

import ezdxf
import ifcopenshell
import pytest

from lighting_agent.dialux_runner.artifacts import RunError, RunStore, sha256, validate_job
from lighting_agent.dialux_runner.imports import (
    OPTIONS, archive_import_report, canonical_ifc, parse_import_report, prepare_import,
)
from lighting_agent.ifc_export import export_spatial_model
from lighting_agent.schemas import SpatialModel
from lighting_agent.dialux_runner.fresh import require_values

FIXTURES = Path(__file__).parent / 'fixtures/phase0'


@pytest.fixture
def exchange(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    model = SpatialModel.model_validate_json((FIXTURES / 'bridge-spatial-model.json').read_text(encoding='utf-8'))
    cad = ezdxf.new('R2010')
    cad.units = ezdxf.units.M
    cad.modelspace().add_lwpolyline([(p.x, p.y) for p in model.rooms[0].boundary], close=True,
                                  dxfattribs={'layer': 'ROOM_BOUNDARY'})
    cad.modelspace().add_lwpolyline([(p.x, p.y) for p in model.elements[0].footprint], close=True,
                                  dxfattribs={'layer': 'FURNITURE'})
    cad.saveas(root / 'bridge.dxf')
    model.source_sha256 = sha256(root / 'bridge.dxf')
    (root / 'spatial-model.json').write_text(model.model_dump_json(indent=2), encoding='utf-8')
    (root / 'bridge.ifc').write_bytes(export_spatial_model(model, OPTIONS).data)
    shutil.copyfile(FIXTURES / 'synthetic-bridge-luminaire.ies', root / 'synthetic-bridge-luminaire.ies')
    return root


def test_fresh_package_needs_no_native_project_and_is_artificial_only(exchange, tmp_path):
    executable = tmp_path / 'DIALux_x64.exe'
    executable.touch()
    job = prepare_import(exchange, tmp_path / 'job', executable)
    store = RunStore(job.parent)
    validate_job(store)
    assert store.job['settings']['daylight'] is False
    assert store.job['settings']['lighting_mode'] == 'artificial_only'
    assert 'native_input' not in store.job
    assert not list((job.parent / 'inputs').glob('*.evo'))
    assert store.state['status'] == 'prepared'


@pytest.mark.parametrize('field', ['daylight', 'layout', 'ies', 'cad', 'geometry', 'reflectance'])
def test_changed_exchange_or_claimed_settings_are_rejected(exchange, tmp_path, field):
    executable = tmp_path / 'DIALux_x64.exe'
    executable.touch()
    job = prepare_import(exchange, tmp_path / 'job', executable)
    store = RunStore(job.parent)
    if field == 'daylight':
        store.job['settings']['daylight'] = True
    elif field == 'layout':
        store.job['layout'][0]['position_m'][0] = 1.5
    else:
        root = job.parent / 'inputs'
        if field == 'ies':
            with (root / 'synthetic-bridge-luminaire.ies').open('a') as stream:
                stream.write('\nchanged')
        elif field == 'cad':
            (root / 'bridge.dxf').write_bytes(b'changed')
        else:
            document = ifcopenshell.open(root / 'bridge.ifc')
            if field == 'geometry':
                document.by_type('IfcExtrudedAreaSolid')[0].Depth = 1.0
            else:
                document.by_type('IfcColourRgb')[0].Red = .9
            document.write(root / 'bridge.ifc')
    with pytest.raises(RunError):
        validate_job(store)


def test_ifc_guid_and_set_order_changes_are_allowed_but_entity_deletion_is_not(exchange):
    source = (exchange / 'bridge.ifc').read_text(encoding='utf-8')
    document = ifcopenshell.file.from_string(source)
    for entity in document.by_type('IfcRoot'):
        entity.GlobalId = ifcopenshell.guid.new()
    units = document.by_type('IfcUnitAssignment')[0]
    units.Units = tuple(reversed(units.Units))
    assert canonical_ifc(document.to_string()) == canonical_ifc(source)
    document.remove(document.by_type('IfcFurniture')[0])
    assert canonical_ifc(document.to_string()) != canonical_ifc(source)


REPORT = ('DIALux evo IFC 报告\n\nIFC 文件\n---\nFile Name: bridge.ifc\n'
          'File Size: 14 KB\nSchema Version: IFC4\n\n笔记\n---\n'
          'Info: 使用 DIALux 版本导入: 5.14.0.3\n')


def test_ifc_report_accepts_only_bound_clean_import():
    result = parse_import_report(REPORT, filename='bridge.ifc', version='5.14.0.3')
    assert result['warning_count'] == 0
    assert result['schema'] == 'IFC4'


@pytest.mark.parametrize('report', [
    REPORT + 'Warning: 物件被跳过 "Bridge test room" (IfcSpace)\n',
    REPORT + 'Warning: 由于缺少几何体而跳过对象 "Test desk"\n',
    REPORT + 'Info: 项目无座标系统 "Site"\n',
    REPORT + '未知消息\n',
    REPORT.replace('bridge.ifc', 'other.ifc'),
    REPORT.replace('5.14.0.3', '5.14.0.5'),
    REPORT.replace('IFC4', 'IFC2X3'),
    REPORT + 'Info: 使用 DIALux 版本导入: 5.14.0.3\n',
])
def test_ifc_report_warnings_missing_identity_and_unknown_notes_require_review(report):
    with pytest.raises(RunError):
        parse_import_report(report, filename='bridge.ifc', version='5.14.0.3')


def test_stale_report_is_rejected_and_failed_report_bytes_are_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    source = tmp_path / 'local/DIAL GmbH/DIALux/DIALuxProjectTemp/DIALux_IfcReport_123.txt'
    source.parent.mkdir(parents=True)
    source.write_text(REPORT, encoding='utf-8')
    output = tmp_path / 'attempt'
    output.mkdir()
    after = time.time_ns()
    os.utime(source, ns=(after - 2_000_000_000, after - 2_000_000_000))
    with pytest.raises(RunError, match='predates'):
        archive_import_report(123, output, after_ns=after)
    assert not (output / 'ifc-import-report.txt').exists()
    source.write_text(REPORT + 'Warning: skipped room\n', encoding='utf-8')
    os.utime(source, ns=(after + 1_000_000_000, after + 1_000_000_000))
    with pytest.raises(RunError, match='warnings'):
        archive_import_report(123, output, after_ns=after)
    assert (output / 'ifc-import-report.txt').read_bytes() == source.read_bytes()
    assert not (output / 'ifc-import-report.json').exists()


def test_ui_value_checks_reject_stale_or_missing_coordinates_and_photometry():
    def snapshot(value):
        return {'controls': [{'id': 'PositionVectorControl_Z', 'value': value, 'offscreen': False}]}
    require_values(snapshot('2.500'), {'PositionVectorControl_Z': 2.5})
    for value in ('4.000', '', None, 'NaN', 'Infinity'):
        with pytest.raises(RunError):
            require_values(snapshot(value), {'PositionVectorControl_Z': 2.5})
    with pytest.raises(RunError):
        require_values(snapshot('2.500'), {'TotalLuminaireFlux_1': 1000.})
