"""Desktop adapter for reviewed DXF/DWG-derived IFC jobs."""
from __future__ import annotations

import math
import re
from pathlib import Path

from .artifacts import RunError, atomic_json, sha256, verify_artifacts
from .desktop import controls, one
from .envelope_run import EnvelopeRun
from .fresh import require_values
from .results import parse_pdf
from .runner import CALCULATION_OPTIONS, NativeReplay


def _numeric(value):
    try:
        return float(str(value).replace(',', '.'))
    except (TypeError, ValueError):
        return None


def monitor_values_generic(snapshot: dict, expected: dict) -> dict:
    if one(snapshot, id='LightSceneName')['name'] != f'已激活的灯光场景：{expected["scene"]}':
        raise RunError('Generic results monitor has a different active scene')
    groups = [c['name'] for c in controls(snapshot, id='ResultGroupName') if c['name']]
    if expected['room'] not in groups or groups[-1] != expected['surface']:
        raise RunError(f'Generic results monitor room/surface mismatch: {groups}')
    if groups.count(expected['room']) != 1 or groups.count(expected['surface']) != 1:
        raise RunError('Generic results monitor room/surface is ambiguous')
    average = one(snapshot, id='ResultsMonitorSurfaceResultAverage')['name']
    uniformity = one(snapshot, id='ResultsMonitorSurfaceUniformityAverage')['name']
    if not re.fullmatch(r'\d+(?:[.,]\d+)? lx', average) or not re.fullmatch(r'\d+(?:[.,]\d+)?', uniformity):
        raise RunError('Generic results monitor is missing numeric results')
    values = {'average_illuminance_lx': float(average[:-3].replace(',', '.')),
              'uniformity_u0': float(uniformity.replace(',', '.'))}
    if not 0 <= values['uniformity_u0'] <= 1:
        raise RunError('Generic results monitor uniformity is out of range')
    return values


class GenericImport(EnvelopeRun):
    """Use the validated envelope UI sequence with dynamic project contents."""

    def __init__(self, store, directory):
        super().__init__(store, directory)
        self.expected = self.job['rooms'][0]

    def import_ifc(self) -> None:
        desktop = self.desktop
        snapshot = desktop.snapshot()
        items = controls(snapshot, type='ListItem')
        for kind, count in self.job.get('entity_counts', {}).items():
            if not count:
                continue
            matches = [item for item in items if re.search(rf'_{re.escape(kind)} \({count}\)$', item['id'])]
            if len(matches) != 1:
                raise RunError(f'IFC import tree is missing an unambiguous {kind} count of {count}')
        if any(c.get('value') not in ('On', True) for c in controls(snapshot, type='CheckBox', id='')):
            raise RunError('Generic IFC import contains deselected objects')
        desktop.click('NextPage')
        desktop.wait(lambda s: bool(controls(s, id='CheckBox_IfcReflectance')), seconds=20,
                     description='IFC import options')
        desktop.call('select', selector={'id': 'RadioButton_GeometryImportOption_True'})
        desktop.toggle('CheckBox_IfcReflectance', False)
        options = self.evidence('ifc-import-options')
        if one(options, id='RadioButton_GeometryImportOption_True')['value'] is not True:
            raise RunError('IFC new import method was not confirmed')
        desktop.click('FinishPage')
        desktop.wait(lambda s: not controls(s, id='FinishPage') and bool(controls(s, id='aid_AddBuilding', enabled=True)),
                     seconds=self.limits['step_seconds'], description='generic IFC import')
        self.import_report = self._archive_generic_report()
        desktop.call('menu', path='File/SaveAs')
        working = self.directory / 'working.evo'
        desktop.save_dialog(working)
        desktop.wait(lambda s: s['title'].startswith(str(working) + ' - DIALux'), seconds=60,
                     description='generic project saved')
        desktop.expected_project = working

    def _archive_generic_report(self) -> dict:
        # The existing importer validates the report identity and preserves the
        # original bytes. Generic jobs intentionally use the same bridge.ifc
        # filename so the DIALux report remains auditable and comparable.
        from .imports import archive_import_report
        return archive_import_report(self.desktop.pid, self.directory, after_ns=self.import_started_ns)

    def room_context(self, expected: dict, *, verify_properties: bool = True) -> None:
        snapshot = self.desktop.snapshot()
        if not controls(snapshot, id='SpaceTool', type='TabItem', enabled=True):
            self.desktop.goto_tool('MenuGotoShowLuminaireArrangementTool', 'LuminaireArrangementTool')
        self.desktop.call('select', selector={'id': 'SpaceTool', 'type': 'TabItem'})
        if verify_properties and not controls(self.desktop.snapshot(), id='ObjectName', value=expected['room']):
            self.desktop.call('room_context', room=expected['room'])
        if verify_properties:
            snapshot = self.desktop.wait(lambda s: bool(controls(s, id='ObjectName', value=expected['room'])),
                                         seconds=20, description='explicit generic room context')
            require_values(snapshot, {'ObjectName': expected['room'],
                                      'Height': expected['clear_height_m'],
                                      'WorkplaneNameTextBox': expected['surface']})
        else:
            if not any(expected['room'] in c['name'] for c in controls(snapshot, id='ResultGroupName')):
                self.desktop.call('room_context', room=expected['room'])
            self.desktop.wait(lambda s: any(expected['room'] in c['name'] for c in controls(s, id='ResultGroupName')),
                              seconds=20, description='generic results room context')

    def configure_rooms(self) -> None:
        for index, expected in enumerate(self.job['rooms'], 1):
            item = self.search_tree(expected['room'], suffix='_' + expected['room'])
            # Selecting the unique tree item is more stable than opening the
            # context menu when the imported project has just one room.
            self.desktop.call('select', selector={'id': item['id'], 'type': 'ListItem'})
            self.room_context(expected)
            for control_id, value in [('WorkplaneHeightDoubleBox', self.job['settings']['workplane_height_m']),
                                      ('WorkplaneMarginDoubleBox', self.job['settings']['edge_margin_m']),
                                      ('MaintenanceFactor', self.job['settings']['maintenance_factor'])]:
                self.desktop.call('set', selector={'id': control_id}, value=f'{value:.3f}')
            require_values(self.evidence(f'room-{index}-settings'), {
                'ObjectName': expected['room'], 'Height': expected['clear_height_m'],
                'WorkplaneHeightDoubleBox': self.job['settings']['workplane_height_m'],
                'WorkplaneMarginDoubleBox': self.job['settings']['edge_margin_m'],
                'MaintenanceFactor': self.job['settings']['maintenance_factor'],
            })

    def collect_monitor(self, prefix: str) -> list[dict]:
        values = []
        if not controls(self.desktop.snapshot(), id='ShowResultsMonitor', value='On'):
            self.desktop.toggle('ShowResultsMonitor', True)
        for index, expected in enumerate(self.job['rooms'], 1):
            self.room_context(expected, verify_properties=False)
            values.append(monitor_values_generic(self.evidence(f'{prefix}-room-{index}'), expected))
        atomic_json(self.directory / f'{prefix}-results.json', values)
        return values

    def place_lamps(self) -> None:
        desktop = self.desktop
        for index, lamp in enumerate(self.job['layout'], 1):
            expected = next(item for item in self.job['rooms'] if item['room_id'] == lamp['room_id'])
            self.room_context(expected)
            desktop.call('menu', path='File/Import/ImportLuminaire')
            desktop.file_dialog(self.store.root / self.job['photometry_input'], save=False, title='载入灯具文件')
            desktop.call('select', selector={'id': 'SpaceTool', 'type': 'TabItem'})
            desktop.call('select', selector={'id': 'LuminaireArrangementTool', 'type': 'TabItem'})
            desktop.wait(lambda s: bool(controls(s, id='aid_DrawPoint', enabled=True)), seconds=30,
                         description='generic photometry imported')
            snapshot = self.desktop.snapshot()
            flux = _numeric(one(snapshot, id='TotalLuminaireFlux_1')['value'])
            load = _numeric(one(snapshot, id='Load_1')['value'])
            if flux is None or flux <= 0 or load is None or load <= 0:
                raise RunError('Imported photometry has no positive flux and power values')
            desktop.click('buttonFloorPlan')
            desktop.click('CadCommandButtonViewAll')
            desktop.click('aid_DrawPoint')
            desktop.click('_renderHost')
            desktop.wait(lambda s: bool(controls(s, id='PositionVectorControl_X')), seconds=20,
                         description='generic luminaire placement')
            for prefix, values in [('PositionVectorControl', lamp['position_m']),
                                   ('RotationVectorControl', lamp['rotation_deg'])]:
                for axis, value in zip('XYZ', values):
                    desktop.call('set', selector={'id': prefix + '_' + axis}, value=f'{value:.3f}')
            desktop.call('set', selector={'id': 'ArrangementObjectName'}, value=lamp.get('name') or f'Lamp {index}')
            check = self.evidence(f'lamp-{index}-layout')
            require_values(check, {
                'ArrangementObjectName': lamp.get('name') or f'Lamp {index}',
                # DIALux displays placement fields at millimetre precision;
                # retain the exact bound coordinates in job.json while
                # comparing the UI at its displayed precision.
                **{'PositionVectorControl_' + axis: round(value, 3) for axis, value in zip('XYZ', lamp['position_m'])},
                **{'RotationVectorControl_' + axis: round(value, 3) for axis, value in zip('XYZ', lamp['rotation_deg'])},
            })
            if one(check, id='SelectedLuminaireCount')['name'] != '1':
                raise RunError('Exactly one generic luminaire must be selected after placement')

    def scene_settings(self) -> None:
        desktop = self.desktop
        desktop.call('select', selector={'id': 'LightSceneTool', 'type': 'TabItem'})
        require_values(desktop.snapshot(), {'ObjectName': self.job['rooms'][0]['scene']})
        selected = desktop.call('combo_select', id='ReferenceSkyTypeComboBox', item_id='ReferenceSkyTypeComboBox_无日光')
        if selected['selected_name'] != 'Undefined':
            raise RunError('Artificial-only scene was not confirmed')
        atomic_json(self.directory / 'artificial-scene.json', selected)
        snapshot = self.evidence('scene-settings-before-dimming')
        require_values(snapshot, {'DimValueDoubleBox': 100.})
        expected_count = len(self.job['layout'])
        labels = [c['name'] for c in controls(snapshot, type='Text')
                  if re.fullmatch(r'\d+个灯具(?:（选择了\d+个）)?', c['name'])]
        accepted_labels = {
            f'{expected_count}个灯具',
            f'{expected_count}个灯具（选择了{expected_count}个）',
        }
        if len(labels) != 1 or labels[0] not in accepted_labels:
            raise RunError(f'Generic scene luminaire count differs: {labels}, expected {expected_count}')
        if controls(snapshot, id='SelectAllLuminairesInGroupButton_1', enabled=True):
            desktop.click('SelectAllLuminairesInGroupButton_1')
        selected_snapshot = self.evidence('scene-all-luminaires-selected')
        if one(selected_snapshot, id='SelectedLuminaireCount')['name'] != str(expected_count):
            raise RunError('Generic scene selection count differs from layout')
        desktop.click('SetMaxDimValuesButton')
        settings_snapshot = self.evidence('scene-settings')
        require_values(settings_snapshot, {'DimValueDoubleBox': 100.})
        settings = desktop.call('configure_calculation', mode_label='全部一起', option_ids=CALCULATION_OPTIONS)
        atomic_json(self.directory / 'calculation-options.json', settings)
        if any(one(settings, id=key)['value'] != 'Off' for key in CALCULATION_OPTIONS):
            raise RunError('Full calculation options were not confirmed')
        if one(desktop.snapshot(), id='DiscardResultsButton')['enabled']:
            raise RunError('Fresh generic project unexpectedly has old results')
        stat = (self.directory / 'working.evo').stat()
        desktop.click('Save')
        desktop.wait_file(self.directory / 'working.evo', changed_since=(stat.st_size, stat.st_mtime_ns))

    def export_rooms(self, values: list[dict]) -> list[dict]:
        results = []
        for index, (expected, observed) in enumerate(zip(self.job['rooms'], values), 1):
            self.expected = expected
            if controls(self.desktop.snapshot(), id='ShowResultsMonitor', enabled=True):
                self.desktop.toggle('ShowResultsMonitor', False)
            self.desktop.goto_tool('MenuGotoShowOutputTreeTool', 'OutputTreeTool')
            grid_suffix = f'_{expected["surface"]} / {expected["scene"]} / 直角照度 (自适应)'
            self.desktop.call('set', selector={'id': 'SearchBox'}, value=expected['surface'])
            grid_snapshot = self.desktop.wait(lambda s: any(c['id'].endswith(grid_suffix) for c in controls(s, type='ListItem')),
                                              seconds=20, description='generic adaptive workplane')
            grids = [c for c in controls(grid_snapshot, type='ListItem') if c['id'].endswith(grid_suffix)]
            if len(grids) != 1:
                raise RunError('Generic workplane grid is absent or ambiguous')
            atomic_json(self.directory / f'room-{index}-workplane-grid.json',
                        {'mode': 'dialux_workplane_adaptive', 'report_item': grids[0]['id']})
            suffix = f'_{expected["room"]}_摘要 / {expected["scene"]}'
            self.desktop.call('set', selector={'id': 'SearchBox'}, value=f'摘要 / {expected["scene"]}')
            summary = self.desktop.wait(lambda s: any(c['id'].endswith(suffix) for c in controls(s, type='ListItem')),
                                        seconds=20, description='generic room summary')
            matches = [c for c in controls(summary, type='ListItem') if c['id'].endswith(suffix)]
            if len(matches) != 1:
                raise RunError('Generic room report is absent or ambiguous')
            self.desktop.call('select', selector={'id': matches[0]['id'], 'type': 'ListItem'})
            pdf = NativeReplay.export_pdf(self, f'room-{index}-summary.pdf')
            result = parse_pdf(pdf, self.directory / f'room-{index}-summary.txt', expected)
            for metric, value in observed.items():
                if not math.isclose(result['metrics'][metric]['value'], value, abs_tol=1e-9, rel_tol=0):
                    raise RunError('Generic room PDF differs from the current result monitor')
            results.append(result)
        self.expected = self.job['rooms'][0]
        return results

    def reopen_generic(self, project: Path, values: list[dict]) -> None:
        if self.desktop.snapshot()['title'].startswith(str(project) + '* - DIALux'):
            stat = project.stat()
            self.desktop.click('Save')
            self.desktop.wait_file(project, seconds=60, changed_since=(stat.st_size, stat.st_mtime_ns))
        else:
            self.desktop.wait_file(project)
        before = sha256(project)
        self.desktop.open_project(self.directory / 'working.evo')
        self.desktop.open_project(project)
        if self.collect_monitor('reopened') != values or sha256(project) != before:
            raise RunError('Reopened generic project results or saved bytes changed')

    def execute(self) -> None:
        with self.step('launch_ifc_import'):
            self.start()
        with self.step('import_ifc_and_archive_report'):
            self.import_ifc()
        with self.step('verify_geometry_and_set_workplanes'):
            self.configure_rooms()
        with self.step('import_photometry_and_place_luminaires'):
            self.place_lamps()
        with self.step('artificial_only_scene'):
            self.scene_settings()
        with self.step('real_calculation'):
            values = self.calculate_envelope()
        with self.step('save_native_project'):
            project = self.save_project()
        with self.step('export_and_validate_room_reports'):
            results = self.export_rooms(values)
        with self.step('fresh_raytrace'):
            expected = self.job['rooms'][0]
            item = self.search_tree(expected['room'], suffix='_' + expected['room'])
            self.desktop.call('select', selector={'id': item['id'], 'type': 'ListItem'})
            self.room_context(expected)
            self.render()
        with self.step('reopen_results'):
            self.reopen_generic(project, values)
        atomic_json(self.directory / 'results.json', {
            'schema_version': 1, 'profile': self.job['profile'], 'run_id': self.job['run_id'],
            'job_sha256': self.store.job_hash, 'input_artifacts': self.job['inputs'],
            'versions': self.job['versions'], 'lighting_mode': 'artificial_only',
            'rooms': results, 'optimization': self.job.get('optimization', {}),
            'ifc_import_report': self.import_report, 'native_project_sha256': sha256(project),
            'compliance': {'status': 'not_evaluated'},
            'checks': {'real_calculation_completed': True, 'native_project_reopened': True,
                       'room_results_exported': True, 'fresh_raytrace_exported': True,
                       'daylight_disabled': True, 'photometry_read_back': True},
        })
        verify_artifacts(self.store.root, self.job['inputs'])
        # Optimization candidates run one after another. Close the dedicated
        # clean project so the next candidate can own the desktop session.
        self.desktop.call('close')
        self.store.finish(self.directory)
