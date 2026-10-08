"""Desktop sequence creating a new, synthetic IFC/IES project."""
from __future__ import annotations

import math
from pathlib import Path
import shutil
import subprocess
import time

from .artifacts import RunError, atomic_json, sha256, verify_artifacts
from .desktop import Desktop, controls, one
from .imports import archive_import_report
from .results import parse_pdf
from .runner import CALCULATION_OPTIONS, NativeReplay


def require_values(snapshot: dict, expected: dict[str, str | float]) -> None:
    for control_id, value in expected.items():
        observed = one(snapshot, id=control_id)['value']
        if isinstance(value, str):
            valid = observed == value
        else:
            try:
                valid = math.isclose(float(str(observed).replace(',', '.')), value, rel_tol=0, abs_tol=1e-6)
            except (ValueError, TypeError):
                valid = False
        if not valid:
            raise RunError(f'DIALux property differs: {control_id}={observed}, expected {value}')


class FreshImport(NativeReplay):
    def start(self) -> None:
        executable = Path(self.job['dialux_executable'])
        if not executable.is_file() or executable.name.lower() != 'dialux_x64.exe' or not shutil.which('pdftotext'):
            raise RunError('DIALux executable and Poppler pdftotext are required')
        self.import_started_ns = time.time_ns()
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        process = subprocess.Popen([str(executable), str(self.store.root / self.job['ifc_input'])],
                                   cwd=executable.parent, startupinfo=startup,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.store.state['attempts'][-1]['process_id'] = process.pid
        self.store.save()
        self.desktop = Desktop(process.pid, self.directory / 'commands', self.deadline)
        end = min(self.deadline, time.monotonic() + self.limits['startup_seconds'])
        while time.monotonic() < end:
            if process.poll() is not None:
                raise RunError('Dedicated DIALux process exited before IFC import')
            try:
                snapshot = self.desktop.snapshot()
            except RunError as error:
                if 'no main window yet' not in str(error):
                    raise
                time.sleep(.3)
                continue
            if controls(snapshot, id='FilterTreeListBox') and controls(snapshot, id='NextPage', enabled=True):
                break
            time.sleep(.3)
        else:
            raise RunError('IFC import wizard did not open before the startup limit')
        if snapshot['file_version'] != self.job['dialux_version'] or Path(snapshot['executable']).resolve() != executable.resolve():
            raise RunError('DIALux executable/version differs from the import profile')
        self.store.state['attempts'][-1].update(process_started_ticks=snapshot['started_ticks'],
                                               desktop_session=snapshot['session_id'])
        self.store.save()
        self.evidence('ifc-import-tree')

    def import_ifc(self) -> None:
        desktop = self.desktop
        snapshot = desktop.snapshot()
        prefix = 'bridge.ifc_IFC bridge fixture_Site_IFC bridge fixture_1F'
        one(snapshot, id=prefix + '_IfcSpace (1)', type='ListItem')
        one(snapshot, id=prefix + '_IfcFurniture (1)', type='ListItem')
        if any(c['value'] != 'On' for c in controls(snapshot, type='CheckBox', id='')):
            raise RunError('IFC import contains deselected objects')
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
                     seconds=self.limits['step_seconds'], description='IFC import completed')
        self.import_report = archive_import_report(desktop.pid, self.directory, after_ns=self.import_started_ns)
        # Save the new, uncalculated project early; this is also the independent
        # document used to unload/reopen the final calculated project later.
        working = self.directory / 'working.evo'
        desktop.call('menu', path='File/SaveAs')
        desktop.save_dialog(working)
        desktop.wait(lambda s: s['title'].startswith(str(working) + ' - DIALux'), seconds=60,
                     description='new IFC project saved')
        desktop.expected_project = working

    def search_tree(self, text: str, *, suffix: str) -> dict:
        desktop = self.desktop
        desktop.goto_tool('MenuGotoShowProjectTreeTool', 'ProjectTreeTool')
        desktop.call('set', selector={'id': 'ProjectTreeToolSearch'}, value=text)
        snapshot = desktop.wait(lambda s: any(c['id'].endswith(suffix) for c in controls(s, type='ListItem')),
                                seconds=20, description=f'project tree object {text}')
        matches = [c for c in controls(snapshot, type='ListItem') if c['id'].endswith(suffix)]
        if len(matches) != 1:
            raise RunError(f'Project tree has ambiguous objects: {text}')
        return matches[0]

    def configure_room(self) -> None:
        desktop = self.desktop
        self.search_tree('Test desk', suffix='_Test desk')
        self.evidence('ifc-furniture-present')
        self.search_tree('101 - Bridge test room', suffix='_101 - Bridge test room')
        self.evidence('ifc-room-present')
        desktop.call('set', selector={'id': 'ProjectTreeToolSearch'}, value='')
        desktop.goto_tool('MenuGotoShowLuminaireArrangementTool', 'LuminaireArrangementTool')
        desktop.call('select', selector={'id': 'SpaceTool', 'type': 'TabItem'})
        desktop.click('ToggleButton_RoomContextSelectionButton')
        desktop.wait(lambda s: bool(controls(s, id='ObjectName', value=self.expected['room'])), seconds=20,
                     description='imported room properties')
        for control_id, value in (('WorkplaneHeightDoubleBox', '.800'), ('WorkplaneMarginDoubleBox', '.500'),
                                  ('MaintenanceFactor', '.80')):
            desktop.call('set', selector={'id': control_id}, value=value)
        snapshot = self.evidence('room-settings')
        require_values(snapshot, {'ObjectName': self.expected['room'], 'Height': 2.8,
                                  'WorkplaneNameTextBox': self.expected['surface'],
                                  'WorkplaneHeightDoubleBox': .8, 'WorkplaneMarginDoubleBox': .5,
                                  'MaintenanceFactor': .8})

    def import_and_place(self) -> None:
        desktop = self.desktop
        desktop.call('menu', path='File/Import/ImportLuminaire')
        desktop.file_dialog(self.store.root / self.job['ies_input'], save=False, title='载入灯具文件')
        # Refresh the WPF tool pane after native file import; otherwise its
        # visible content can be absent from the UI Automation tree.
        desktop.call('select', selector={'id': 'SpaceTool', 'type': 'TabItem'})
        desktop.call('select', selector={'id': 'LuminaireArrangementTool', 'type': 'TabItem'})
        desktop.wait(lambda s: bool(controls(s, id='aid_DrawPoint', enabled=True)), seconds=30,
                     description='IES import and placement tool')
        snapshot = self.evidence('ies-imported')
        require_values(snapshot, {'TotalLuminaireFlux_1': 1000., 'Load_1': 10.})
        # The fixed single-room profile centres the view on its only room.
        # This click only creates a provisional point; actual coordinates are
        # then written numerically and read back before any calculation.
        desktop.click('buttonFloorPlan')
        desktop.click('CadCommandButtonViewAll')
        desktop.click('aid_DrawPoint')
        desktop.click('_renderHost')
        desktop.wait(lambda s: bool(controls(s, id='PositionVectorControl_X')), seconds=20,
                     description='one provisional luminaire')
        lamp = self.job['layout'][0]
        for prefix, values in (('PositionVectorControl', lamp['position_m']), ('RotationVectorControl', lamp['rotation_deg'])):
            for axis, value in zip('XYZ', values):
                desktop.call('set', selector={'id': prefix + '_' + axis}, value=f'{value:.3f}')
        desktop.call('set', selector={'id': 'ArrangementObjectName'}, value=lamp['name'])
        snapshot = self.evidence('luminaire-layout')
        if one(snapshot, id='SelectedLuminaireCount')['name'] != '1':
            raise RunError('Expected one selected luminaire after placement')
        require_values(snapshot, {'ArrangementObjectName': lamp['name'], 'PositionVectorControl_X': 3.,
                                  'PositionVectorControl_Y': 2., 'PositionVectorControl_Z': 2.5,
                                  'RotationVectorControl_X': 0., 'RotationVectorControl_Y': 0.,
                                  'RotationVectorControl_Z': 0., 'TotalLuminaireFlux_1': 1000., 'Load_1': 10.})
        desktop.click('Save')
        desktop.wait_file(self.directory / 'working.evo')

    def artificial_scene_and_calculation(self) -> None:
        desktop = self.desktop
        desktop.call('select', selector={'id': 'LightSceneTool', 'type': 'TabItem'})
        desktop.wait(lambda s: bool(controls(s, id='ReferenceSkyTypeComboBox')), seconds=20,
                     description='active light scene properties')
        require_values(desktop.snapshot(), {'ObjectName': self.expected['scene']})
        daylight = desktop.call('combo_select', id='ReferenceSkyTypeComboBox', item_id='ReferenceSkyTypeComboBox_无日光')
        if daylight['selected_name'] != 'Undefined':
            raise RunError('Artificial-only scene was not confirmed')
        atomic_json(self.directory / 'artificial-scene.json', daylight)
        snapshot = self.evidence('light-scene-settings')
        require_values(snapshot, {'DimValueDoubleBox': 100.})
        counts = [c['name'] for c in controls(snapshot, type='Text') if '个灯具' in c['name']]
        if counts != ['1个灯具']:
            raise RunError(f'Active light scene luminaire count differs: {counts}')
        settings = desktop.call('configure_calculation', mode_label='全部一起', option_ids=CALCULATION_OPTIONS)
        atomic_json(self.directory / 'calculation-options.json', settings)
        if any(one(settings, id=key)['value'] != 'Off' for key in CALCULATION_OPTIONS):
            raise RunError('Full calculation options were not applied')
        snapshot = desktop.snapshot()
        if one(snapshot, id='DiscardResultsButton')['enabled'] or controls(snapshot, id='ResultsMonitorSurfaceResultAverage'):
            raise RunError('A fresh project unexpectedly already has results')
        desktop.click('Save')
        self.evidence('fresh-project-before-calculation')

    def export_pdf(self) -> Path:
        if controls(self.desktop.snapshot(), id='ShowResultsMonitor', enabled=True):
            self.desktop.toggle('ShowResultsMonitor', False)
        self.desktop.goto_tool('MenuGotoShowOutputTreeTool', 'OutputTreeTool')
        snapshot = self.desktop.snapshot()
        wanted = f'_{self.expected["room"]}_摘要 / {self.expected["scene"]}'
        matches = [c for c in controls(snapshot, type='ListItem') if c['id'].endswith(wanted)]
        grid_suffix = f'_{self.expected["surface"]} / {self.expected["scene"]} / 直角照度 (自适应)'
        grids = [c for c in controls(snapshot, type='ListItem') if c['id'].endswith(grid_suffix)]
        if len(grids) != 1:
            raise RunError('The room workplane is not the expected adaptive illuminance surface')
        atomic_json(self.directory / 'workplane-grid.json', {'mode': 'dialux_workplane_adaptive', 'report_item': grids[0]['id']})
        if len(matches) != 1:
            self.evidence('report-selection-unavailable')
            raise RunError('The new project room summary is not uniquely visible')
        self.desktop.call('select', selector={'id': matches[0]['id'], 'type': 'ListItem'})
        return super().export_pdf()

    def execute(self) -> None:
        with self.step('launch_ifc_import'):
            self.start()
        with self.step('import_ifc_and_archive_report'):
            self.import_ifc()
        with self.step('verify_geometry_and_set_workplane'):
            self.configure_room()
        with self.step('import_ies_and_place_luminaire'):
            self.import_and_place()
        with self.step('artificial_only_full_calculation_settings'):
            self.artificial_scene_and_calculation()
        with self.step('real_calculation'):
            values = self.calculate()
        with self.step('save_native_project'):
            project = self.save_project()
        with self.step('export_and_validate_pdf'):
            result = parse_pdf(self.export_pdf(), self.directory / 'room-summary.txt', self.expected)
            for metric, value in values.items():
                if not math.isclose(value, result['metrics'][metric]['value'], rel_tol=0, abs_tol=1e-9):
                    raise RunError(f'PDF and current calculation disagree: {metric}')
            result.update(run_id=self.job['run_id'], job_sha256=self.store.job_hash,
                          input_artifacts=self.job['inputs'], versions=self.job['versions'],
                          native_project='project.evo', execution_profile=self.job['profile'],
                          lighting_mode='artificial_only', ifc_import_report=self.import_report)
        with self.step('fresh_dialux_raytrace_and_export'):
            self.render()
        with self.step('reopen_and_verify_native_results'):
            self.reopen(project, values)
            result['native_project_sha256'] = sha256(project)
            result['checks'] = {'real_calculation_completed': True, 'native_project_saved': True,
                                'native_project_reopened': True, 'results_exported': True,
                                'matching_room_surface_scene': True, 'fresh_raytrace_exported': True,
                                'ifc_import_report_archived': True, 'fresh_ifc_ies_project': True,
                                'daylight_disabled': True}
            atomic_json(self.directory / 'results.json', result)
        verify_artifacts(self.store.root, self.job['inputs'])
        self.store.finish(self.directory)
