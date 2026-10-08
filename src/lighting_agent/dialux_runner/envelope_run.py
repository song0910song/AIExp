"""DIALux desktop adapter for the strictly validated synthetic envelope."""
from __future__ import annotations

import math
from pathlib import Path
import re

from .artifacts import RunError, atomic_json, sha256, verify_artifacts
from .desktop import controls, one
from .fresh import FreshImport, require_values
from .imports import archive_import_report
from .results import parse_pdf
from .runner import CALCULATION_OPTIONS, NativeReplay, monitor_values


def require_two_scene_lamps(snapshot: dict, *, selected: int | None,
                            allow_unselected: bool = False) -> None:
    labels = [c['name'] for c in controls(snapshot, type='Text')
              if re.fullmatch(r'\d+个灯具(?:（选择了\d+个）)?', c['name'])]
    wanted = '2个灯具' if selected is None else f'2个灯具（选择了{selected}个）'
    if allow_unselected and selected == 2 and labels == ['2个灯具']:
        return
    if labels != [wanted]:
        raise RunError(f'Active scene luminaire group differs: {labels}, expected {wanted}')
    if selected is not None and one(snapshot, id='SelectedLuminaireCount')['name'] != str(selected):
        raise RunError('Scene group selection does not match its luminaire total')


class EnvelopeRun(FreshImport):
    def search_tree(self, text: str, *, suffix: str) -> dict:
        desktop = self.desktop
        snapshot = desktop.snapshot()
        if not controls(snapshot, id='ProjectTreeToolSearch'):
            if not controls(snapshot, id='ProjectTreeTool', type='TabItem', enabled=True):
                desktop.goto_tool('MenuGotoShowProjectTreeTool', 'ProjectTreeTool')
            else:
                desktop.call('select', selector={'id': 'ProjectTreeTool', 'type': 'TabItem'})
        if not controls(desktop.snapshot(), id='ProjectTreeToolSearch'):
            desktop.call('select', selector={'id': 'ViewTool', 'type': 'TabItem'})
            desktop.call('select', selector={'id': 'ProjectTreeTool', 'type': 'TabItem'})
        desktop.wait(lambda s: bool(controls(s, id='ProjectTreeToolSearch')), seconds=20,
                     description='project tree search field')
        desktop.call('set', selector={'id': 'ProjectTreeToolSearch'}, value=text)
        snapshot = desktop.wait(lambda s: any(c['id'].endswith(suffix) for c in controls(s, type='ListItem')),
                                seconds=20, description='envelope project tree object')
        matches = [c for c in controls(snapshot, type='ListItem') if c['id'].endswith(suffix)]
        if len(matches) != 1:
            raise RunError('Envelope tree object is ambiguous')
        return matches[0]

    def import_ifc(self) -> None:
        desktop = self.desktop
        prefix = 'bridge.ifc_IFC envelope fixture_Site_IFC envelope fixture_1F'
        snapshot = desktop.snapshot()
        for kind, count in [('IfcSpace', 2), ('IfcFurniture', 1), ('IfcWindow', 1)]:
            one(snapshot, id=f'{prefix}_{kind} ({count})', type='ListItem')
        if self.job['opening_treatments'][0]['filling'] == 'closed_door':
            one(snapshot, id=prefix + '_IfcDoor (1)', type='ListItem')
        if any(c['value'] != 'On' for c in controls(snapshot, type='CheckBox', id='')):
            raise RunError('Envelope IFC import contains deselected objects')
        desktop.click('NextPage')
        desktop.wait(lambda s: bool(controls(s, id='CheckBox_IfcReflectance')), seconds=20, description='IFC options')
        desktop.call('select', selector={'id': 'RadioButton_GeometryImportOption_True'})
        desktop.toggle('CheckBox_IfcReflectance', False)
        if one(self.evidence('ifc-import-options'), id='RadioButton_GeometryImportOption_True')['value'] is not True:
            raise RunError('IFC new method was not confirmed')
        desktop.click('FinishPage')
        desktop.wait(lambda s: not controls(s, id='FinishPage') and bool(controls(s, id='aid_AddBuilding', enabled=True)),
                     seconds=self.limits['step_seconds'], description='envelope import')
        self.import_report = archive_import_report(desktop.pid, self.directory, after_ns=self.import_started_ns)
        desktop.call('menu', path='File/SaveAs')
        working = self.directory / 'working.evo'
        desktop.save_dialog(working)
        desktop.wait(lambda s: s['title'].startswith(str(working) + ' - DIALux'), seconds=60, description='envelope saved')
        desktop.expected_project = working

    def room_context(self, expected: dict, *, verify_properties: bool = True) -> None:
        snapshot = self.desktop.snapshot()
        if not controls(snapshot, id='SpaceTool', type='TabItem', enabled=True):
            self.desktop.goto_tool('MenuGotoShowLuminaireArrangementTool', 'LuminaireArrangementTool')
        self.desktop.call('select', selector={'id': 'SpaceTool', 'type': 'TabItem'})
        if verify_properties and not controls(self.desktop.snapshot(), id='ObjectName', value=expected['room']):
            self.desktop.call('room_context', room=expected['room'])
        if verify_properties:
            snapshot = self.desktop.wait(lambda s: bool(controls(s, id='ObjectName', value=expected['room'])),
                                         seconds=20, description='explicit room context')
            require_values(snapshot, {'ObjectName': expected['room'], 'Height': 2.8,
                                      'WorkplaneNameTextBox': expected['surface']})
        else:
            # After calculation DIALux keeps the light-scene object selected,
            # while the results monitor exposes the selected room/workplane.
            # Use that result group as the authoritative room context.
            if not any(expected['room'] in c['name'] for c in controls(snapshot, id='ResultGroupName')):
                self.desktop.call('room_context', room=expected['room'])
            snapshot = self.desktop.wait(
                lambda s: any(expected['room'] in c['name'] for c in controls(s, id='ResultGroupName')),
                seconds=20, description='results room context')

    def configure_rooms(self) -> None:
        self.search_tree('Test desk', suffix='_Test desk')
        self.evidence('furniture-present')
        for index, expected in enumerate(self.job['rooms'], 1):
            self.search_tree(expected['room'], suffix='_' + expected['room'])
            self.room_context(expected)
            for control_id, value in [('WorkplaneHeightDoubleBox', '.800'), ('WorkplaneMarginDoubleBox', '.500'),
                                      ('MaintenanceFactor', '.80')]:
                self.desktop.call('set', selector={'id': control_id}, value=value)
            require_values(self.evidence(f'room-{index}-settings'), {'ObjectName': expected['room'], 'Height': 2.8,
                'WorkplaneHeightDoubleBox': .8, 'WorkplaneMarginDoubleBox': .5, 'MaintenanceFactor': .8})

    def select_opening(self, name: str, *, width: float, height: float, xyz: list[float]) -> None:
        item = self.search_tree(name, suffix='_' + name)
        self.desktop.call('select', selector={'id': item['id'], 'type': 'ListItem'})
        self.desktop.goto_tool('MenuGotoShowBuildingOpeningTool', 'BuildingOpeningTool')
        if not controls(self.desktop.snapshot(), id='BuildingOpeningCatalogItemDisplayName'):
            self.desktop.call('select', selector={'id': 'SpaceTool', 'type': 'TabItem'})
            self.desktop.call('select', selector={'id': 'BuildingOpeningTool', 'type': 'TabItem'})
        self.desktop.wait(lambda s: bool(controls(s, id='BuildingOpeningCatalogItemDisplayName')),
                          seconds=20, description='selected door or window dimensions')
        require_values(self.evidence(name.replace(' ', '-').lower() + '-dimensions'), {
            'BuildingOpeningCatalogItemDisplayName': name, 'ArbitraryBuildingOpeningWidth': width,
            'ArbitraryBuildingOpeningHeight': height, **{'Position_' + a: v for a, v in zip('XYZ', xyz)}})

    def pick_material(self, name: str) -> dict:
        desktop = self.desktop
        desktop.goto_tool('MenuGotoShowMaterialTool', 'MaterialTool')
        if not controls(desktop.snapshot(), id='aid_PickMaterial', enabled=True):
            desktop.call('select', selector={'id': 'SpaceTool', 'type': 'TabItem'})
            desktop.call('select', selector={'id': 'MaterialTool', 'type': 'TabItem'})
        desktop.wait(lambda s: bool(controls(s, id='aid_PickMaterial', enabled=True)),
                     seconds=20, description='material tool ready')
        desktop.click('buttonRenderer')
        desktop.click('CadCommandButtonViewSelected')
        desktop.click('aid_PickMaterial')
        desktop.click('_renderHost')
        snapshot = desktop.snapshot()
        require_values(snapshot, {'MaterialName': name})
        return snapshot

    def opening_materials(self, *, apply: bool) -> None:
        desktop = self.desktop
        self.room_context(self.job['rooms'][0])
        if self.job['opening_treatments'][0]['filling'] == 'closed_door':
            self.select_opening('Closed test door', width=1., height=2.1, xyz=[6.08, 2., 1.05])
            snapshot = self.pick_material('Synthetic opaque door')
            require_values(snapshot, {'TotalReflexion': 50., 'Mirroring': 0.})
            selected = desktop.call('combo_value', id='MaterialTypeComboBox')
            if selected['selected_id'] != 'MaterialTypePlastic' or controls(snapshot, id='Transmission'):
                raise RunError('Closed door material is not opaque')
            atomic_json(self.directory / ('door-material.json' if apply else 'reopened-door-material.json'), selected)
            self.evidence('door-opaque' if apply else 'reopened-door-opaque')
        desktop.click('ToggleButton_SiteContextSelectionButton')
        self.select_opening('Test glazing', width=2., height=1.2, xyz=[3., -.06, 1.5])
        self.pick_material('Synthetic test glazing')
        if apply:
            self.evidence('glazing-imported-material')
            desktop.call('combo_select', id='MaterialTypeComboBox', item_id='MaterialTypeTransparent')
            for key, value in [('TotalReflexion', '10'), ('Transmission', '70'), ('IndexOfRefraction', '1.520')]:
                desktop.call('set', selector={'id': key}, value=value)
            desktop.click('aid_PaintMaterial')
            desktop.click('_renderHost')
        # Read the actual face again after application/reopen, not the paint palette.
        desktop.click('aid_PickMaterial')
        desktop.click('_renderHost')
        snapshot = self.evidence('glazing-applied-material' if apply else 'reopened-glazing-material')
        require_values(snapshot, {'MaterialName': 'Synthetic test glazing', 'TotalReflexion': 10.,
                                  'Transmission': 70., 'IndexOfRefraction': 1.52})
        selected = desktop.call('combo_value', id='MaterialTypeComboBox')
        if selected['selected_id'] != 'MaterialTypeTransparent':
            raise RunError('Glazing material is not transparent')
        atomic_json(self.directory / ('glazing-material.json' if apply else 'reopened-glazing-material-type.json'), selected)

    def place_lamps(self) -> None:
        desktop = self.desktop
        for index, (lamp, expected) in enumerate(zip(self.job['layout'], self.job['rooms']), 1):
            self.room_context(expected)
            desktop.call('menu', path='File/Import/ImportLuminaire')
            desktop.file_dialog(self.store.root / self.job['ies_input'], save=False, title='载入灯具文件')
            desktop.call('select', selector={'id': 'SpaceTool', 'type': 'TabItem'})
            desktop.call('select', selector={'id': 'LuminaireArrangementTool', 'type': 'TabItem'})
            desktop.wait(lambda s: bool(controls(s, id='aid_DrawPoint', enabled=True)), seconds=30, description='photometry imported')
            require_values(desktop.snapshot(), {'TotalLuminaireFlux_1': 1000., 'Load_1': 10.})
            desktop.click('buttonFloorPlan')
            desktop.click('CadCommandButtonViewAll')
            desktop.click('aid_DrawPoint')
            desktop.click('_renderHost')
            desktop.wait(lambda s: bool(controls(s, id='PositionVectorControl_X')), seconds=20, description='single lamp created')
            for prefix, values in [('PositionVectorControl', lamp['position_m']), ('RotationVectorControl', lamp['rotation_deg'])]:
                for axis, value in zip('XYZ', values):
                    desktop.call('set', selector={'id': prefix + '_' + axis}, value=f'{value:.3f}')
            desktop.call('set', selector={'id': 'ArrangementObjectName'}, value=lamp['name'])
            snapshot = self.evidence(f'lamp-{index}-layout')
            require_values(snapshot, {'ArrangementObjectName': lamp['name'], 'TotalLuminaireFlux_1': 1000., 'Load_1': 10.,
                **{'PositionVectorControl_' + a: v for a, v in zip('XYZ', lamp['position_m'])},
                **{'RotationVectorControl_' + a: v for a, v in zip('XYZ', lamp['rotation_deg'])}})
            if one(snapshot, id='SelectedLuminaireCount')['name'] != '1':
                raise RunError('Exactly one lamp must be selected after placement')

    def scene_settings(self) -> None:
        desktop = self.desktop
        desktop.call('select', selector={'id': 'LightSceneTool', 'type': 'TabItem'})
        require_values(desktop.snapshot(), {'ObjectName': self.expected['scene']})
        selected = desktop.call('combo_select', id='ReferenceSkyTypeComboBox', item_id='ReferenceSkyTypeComboBox_无日光')
        if selected['selected_name'] != 'Undefined':
            raise RunError('Artificial-only scene was not selected')
        atomic_json(self.directory / 'artificial-scene.json', selected)
        snapshot = self.evidence('scene-settings-before-dimming')
        require_values(snapshot, {'DimValueDoubleBox': 100.})
        require_two_scene_lamps(snapshot, selected=1)
        desktop.click('SelectAllLuminairesInGroupButton_1')
        require_two_scene_lamps(self.evidence('scene-both-lamps-selected'), selected=2)
        desktop.click('SetMaxDimValuesButton')
        snapshot = self.evidence('scene-settings')
        require_two_scene_lamps(snapshot, selected=2, allow_unselected=True)
        require_values(snapshot, {'DimValueDoubleBox': 100.})
        settings = desktop.call('configure_calculation', mode_label='全部一起', option_ids=CALCULATION_OPTIONS)
        atomic_json(self.directory / 'calculation-options.json', settings)
        if any(one(settings, id=key)['value'] != 'Off' for key in CALCULATION_OPTIONS):
            raise RunError('Full calculation options were not confirmed')
        if one(desktop.snapshot(), id='DiscardResultsButton')['enabled']:
            raise RunError('Fresh envelope unexpectedly has old results')
        working = self.directory / 'working.evo'
        stat = working.stat()
        desktop.click('Save')
        desktop.wait_file(working, changed_since=(stat.st_size, stat.st_mtime_ns))

    def collect_monitor(self, prefix: str) -> list[dict]:
        values = []
        if not controls(self.desktop.snapshot(), id='ShowResultsMonitor', value='On'):
            self.desktop.toggle('ShowResultsMonitor', True)
        for index, expected in enumerate(self.job['rooms'], 1):
            self.room_context(expected, verify_properties=False)
            values.append(monitor_values(self.evidence(f'{prefix}-room-{index}'), expected))
        atomic_json(self.directory / f'{prefix}-results.json', values)
        return values

    def calculate_envelope(self) -> list[dict]:
        desktop = self.desktop
        started = desktop.call('start_and_observe', selector={'id': 'CalculationButtonStart'}, activity_id='CalculationButtonAbort')
        atomic_json(self.directory / 'calculation-start-observation.json', started)
        desktop.wait(lambda s: not controls(s, id='CalculationButtonAbort') and bool(controls(s, id='DiscardResultsButton', enabled=True)),
                     seconds=self.limits['calculation_seconds'], description='two-room calculation finished')
        return self.collect_monitor('calculated')

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
                                              seconds=20, description='searched room adaptive workplane')
            grid = [c for c in controls(grid_snapshot, type='ListItem') if c['id'].endswith(grid_suffix)]
            if len(grid) != 1:
                raise RunError('Envelope workplane grid is absent or ambiguous')
            atomic_json(self.directory / f'room-{index}-workplane-grid.json',
                        {'mode': 'dialux_workplane_adaptive', 'report_item': grid[0]['id']})
            suffix = f'_{expected["room"]}_摘要 / {expected["scene"]}'
            self.desktop.call('set', selector={'id': 'SearchBox'}, value=f'摘要 / {expected["scene"]}')
            summary_snapshot = self.desktop.wait(lambda s: any(c['id'].endswith(suffix) for c in controls(s, type='ListItem')),
                                                 seconds=20, description='searched room summary')
            matches = [c for c in controls(summary_snapshot, type='ListItem') if c['id'].endswith(suffix)]
            if len(matches) != 1:
                raise RunError('Envelope room report is absent or ambiguous')
            self.desktop.call('select', selector={'id': matches[0]['id'], 'type': 'ListItem'})
            pdf = NativeReplay.export_pdf(self, f'room-{index}-summary.pdf')
            result = parse_pdf(pdf, self.directory / f'room-{index}-summary.txt', expected)
            for metric, value in observed.items():
                if not math.isclose(result['metrics'][metric]['value'], value, abs_tol=1e-9, rel_tol=0):
                    raise RunError('Room PDF differs from the current result monitor')
            results.append(result)
        if len({result['surface_index'] for result in results}) != len(results):
            raise RunError('Two different rooms have the same workplane index')
        self.expected = self.job['expected']
        return results

    def reopen_envelope(self, project: Path, values: list[dict]) -> None:
        desktop = self.desktop
        if desktop.snapshot()['title'].startswith(str(project) + '* - DIALux'):
            stat = project.stat()
            desktop.click('Save')
            desktop.wait_file(project, seconds=60, changed_since=(stat.st_size, stat.st_mtime_ns))
        else:
            desktop.wait_file(project)
        before = sha256(project)
        desktop.open_project(self.directory / 'working.evo')
        desktop.open_project(project)
        if self.collect_monitor('reopened') != values:
            raise RunError('Reopened room results differ')
        desktop.toggle('ShowResultsMonitor', False)
        self.opening_materials(apply=False)
        if sha256(project) != before:
            raise RunError('Archived project changed during read-back')

    def execute(self) -> None:
        with self.step('launch_ifc_import'):
            self.start()
        with self.step('import_envelope_and_archive_report'):
            self.import_ifc()
        with self.step('verify_two_rooms_and_workplanes'):
            self.configure_rooms()
        with self.step('verify_door_and_apply_glazing_optics'):
            self.opening_materials(apply=True)
        with self.step('place_two_lamps'):
            self.place_lamps()
        with self.step('artificial_only_scene'):
            self.scene_settings()
        with self.step('real_two_room_calculation'):
            values = self.calculate_envelope()
        with self.step('save_native_project'):
            project = self.save_project()
        with self.step('export_two_room_reports'):
            results = self.export_rooms(values)
        with self.step('fresh_raytrace'):
            self.room_context(self.job['rooms'][0])
            self.render()
        with self.step('reopen_results_and_materials'):
            self.reopen_envelope(project, values)
        atomic_json(self.directory / 'results.json', {'schema_version': 1, 'profile': self.job['profile'],
            'run_id': self.job['run_id'], 'job_sha256': self.store.job_hash, 'input_artifacts': self.job['inputs'],
            'versions': self.job['versions'], 'lighting_mode': 'artificial_only', 'opening_treatments': self.job['opening_treatments'],
            'ifc_import_report': self.import_report, 'rooms': results, 'native_project_sha256': sha256(project),
            'compliance': {'status': 'not_evaluated'}, 'checks': {'real_calculation_completed': True,
                'native_project_reopened': True, 'two_room_results_exported': True, 'fresh_raytrace_exported': True,
                'opening_dimensions_read_back': True, 'glazing_optics_applied_and_read_back': True,
                'reopened_optics_verified': True, 'daylight_disabled': True}})
        verify_artifacts(self.store.root, self.job['inputs'])
        self.store.finish(self.directory)
