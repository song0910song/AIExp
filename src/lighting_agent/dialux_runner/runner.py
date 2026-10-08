"""Run the accepted native bridge profile on a dedicated DIALux instance."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import math
from pathlib import Path
import re
import shutil
import subprocess
import time

from PIL import Image

from .artifacts import RunError, RunStore, atomic_json, sha256, timestamp, validate_job, verify_artifacts
from .desktop import Desktop, SessionLock, controls, one
from .results import parse_pdf

CALCULATION_OPTIONS = (
    'PopupStandardCalcOptionsSceneCalculationDirectOnly',
    'PopupStandardCalcOptionsSceneCalculationExcludeFurniture',
    'PopupStandardCalcOptionsSceneCalculationVirtualSurfacesOnly',
    'PopupStandardCalcOptionsSceneCalculationSimplifiedFurniture',
)


def monitor_values(snapshot: dict, expected: dict) -> dict:
    if one(snapshot, id='LightSceneName')['name'] != f'已激活的灯光场景：{expected["scene"]}':
        raise RunError('Results monitor has a different active scene')
    # Each call validates one selected room and one selected surface. A result
    # row from another room must not be associated with the current context.
    groups = [c['name'] for c in controls(snapshot, id='ResultGroupName') if c['name']]
    if groups != [expected['room'], expected['surface']]:
        raise RunError(f'Results monitor room/surface mismatch: {groups}')
    average = one(snapshot, id='ResultsMonitorSurfaceResultAverage')['name']
    uniformity = one(snapshot, id='ResultsMonitorSurfaceUniformityAverage')['name']
    if not re.fullmatch(r'\d+(?:[.,]\d+)? lx', average) or not re.fullmatch(r'\d+(?:[.,]\d+)?', uniformity):
        raise RunError('Results monitor is missing numeric results')
    values = {'average_illuminance_lx': float(average[:-3].replace(',', '.')),
              'uniformity_u0': float(uniformity.replace(',', '.'))}
    if not 0 <= values['uniformity_u0'] <= 1:
        raise RunError('Results monitor uniformity is out of range')
    return values


def process_exists(pid: int) -> bool:
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    api.OpenProcess.restype = ctypes.c_void_p
    api.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = api.OpenProcess(0x1000, False, pid)
    if handle:
        api.CloseHandle(handle)
        return True
    return ctypes.get_last_error() == 5  # Access denied also means not safe to reuse.


class NativeReplay:
    def __init__(self, store: RunStore, directory: Path):
        self.store, self.directory, self.job = store, directory, store.job
        self.limits = self.job['limits']
        self.deadline = time.monotonic() + self.limits['total_seconds']
        self.desktop: Desktop | None = None
        self.current_step = 'preflight'
        self.expected = self.job['expected']

    @contextmanager
    def step(self, name: str):
        self.current_step = name
        if (self.store.root / 'pause.request').exists():
            raise RunError('Manual pause requested before the next desktop step')
        if time.monotonic() >= self.deadline:
            raise RunError('Total run time limit reached')
        self.store.step(name, 'started')
        print(f'{timestamp()} {name}', flush=True)
        try:
            yield
        except BaseException:
            self.store.step(name, 'unverified')
            raise
        self.store.step(name, 'verified')

    def evidence(self, name: str, *, capture: bool = True) -> dict:
        snapshot = self.desktop.snapshot()
        atomic_json(self.directory / f'{name}.json', snapshot)
        if capture:
            self.desktop.call('capture', path=str(self.directory / f'{name}.png'))
        return snapshot

    def start(self) -> None:
        executable = Path(self.job['dialux_executable'])
        if not executable.is_file() or executable.name.lower() != 'dialux_x64.exe':
            raise RunError('The configured DIALux executable is unavailable')
        if not shutil.which('pdftotext'):
            raise RunError('Poppler pdftotext must be on PATH for this profile')
        # DIALux only ever opens the per-attempt copy, never an archived input.
        working = self.directory / 'working.evo'
        shutil.copyfile(self.store.root / self.job['native_input'], working)
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        process = subprocess.Popen([str(executable), str(working)], cwd=executable.parent,
                                   startupinfo=startup, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.store.state['attempts'][-1]['process_id'] = process.pid
        self.store.save()
        self.desktop = Desktop(process.pid, self.directory / 'commands', self.deadline)
        end = min(self.deadline, time.monotonic() + self.limits['startup_seconds'])
        snapshot = None
        while time.monotonic() < end:
            if process.poll() is not None:
                raise RunError('Dedicated DIALux process exited before opening the project')
            try:
                snapshot = self.desktop.snapshot()
            except RunError as error:
                if 'no main window yet' not in str(error):
                    raise
            if snapshot and snapshot['title'].startswith(str(working) + ' - DIALux'):
                break
            time.sleep(.4)
        else:
            raise RunError('DIALux startup timed out or a different project is open')
        if snapshot['file_version'] != self.job['dialux_version'] or Path(snapshot['executable']).resolve() != executable.resolve():
            raise RunError('DIALux executable/version differs from the verified profile')
        self.store.state['attempts'][-1]['process_started_ticks'] = snapshot['started_ticks']
        self.store.state['attempts'][-1]['desktop_session'] = snapshot['session_id']
        self.store.save()
        self.desktop.expected_project = working
        self.evidence('opened-input')

    def configure_and_clear(self) -> None:
        desktop = self.desktop
        desktop.goto_tool('MenuGotoShowLuminaireArrangementTool', 'LuminaireArrangementTool')
        desktop.toggle('ShowResultsMonitor', True)
        monitor_values(desktop.snapshot(), self.expected)
        settings = desktop.call('configure_calculation', mode_label='全部一起', option_ids=CALCULATION_OPTIONS)
        atomic_json(self.directory / 'calculation-options.json', settings)
        if one(settings, id='PopupStandardCalcOptionsSwitchButton')['name'] != '全部一起':
            raise RunError('Only the verified full-scene calculation mode is supported')
        if any(one(settings, id=control_id)['value'] != 'Off' for control_id in CALCULATION_OPTIONS):
            raise RunError('Full calculation options were not confirmed together')
        desktop.wait(lambda s: bool(controls(s, id='CalculationButtonShowCalcOptionsDropDown', value='Off')),
                     seconds=15, description='calculation options applied and closed')
        before = desktop.snapshot()
        if not one(before, id='DiscardResultsButton')['enabled']:
            raise RunError('Expected saved baseline results before clearing the working copy')
        desktop.click('DiscardResultsButton')
        desktop.wait(lambda s: bool(controls(s, id='CalculationButtonStart', enabled=True))
                     and bool(controls(s, id='DiscardResultsButton', enabled=False))
                     and not controls(s, id='ResultsMonitorSurfaceResultAverage'),
                     seconds=self.limits['step_seconds'], description='old results to be cleared')
        self.evidence('old-results-cleared')

    def calculate(self) -> dict:
        desktop = self.desktop
        started = desktop.call('start_and_observe', selector={'id': 'CalculationButtonStart'},
                               activity_id='CalculationButtonAbort')
        atomic_json(self.directory / 'calculation-start-observation.json', started)
        self.evidence('calculation-started')
        desktop.wait(lambda s: not controls(s, id='CalculationButtonAbort')
                     and bool(controls(s, id='DiscardResultsButton', enabled=True)),
                     seconds=self.limits['calculation_seconds'], description='calculation to finish')
        desktop.toggle('ShowResultsMonitor', True)
        snapshot = self.evidence('calculation-completed')
        values = monitor_values(snapshot, self.expected)
        atomic_json(self.directory / 'monitor-results.json', values)
        return values

    def save_project(self) -> Path:
        project = self.directory / 'project.evo'
        self.desktop.call('menu', path='File/SaveAs')
        self.desktop.save_dialog(project)
        self.desktop.wait(lambda s: s['title'].startswith(str(project) + ' - DIALux'),
                          seconds=self.limits['step_seconds'], description='saved native project title')
        self.desktop.expected_project = project
        return project

    def export_pdf(self, filename: str = 'room-summary.pdf') -> Path:
        desktop = self.desktop
        if controls(desktop.snapshot(), id='ShowResultsMonitor', enabled=True):
            desktop.toggle('ShowResultsMonitor', False)
        desktop.goto_tool('MenuGotoShowOutputTreeTool', 'OutputTreeTool')
        snapshot = desktop.snapshot()
        selected = controls(snapshot, type='ListItem', value=True)
        wanted = f'_{self.expected["room"]}_摘要 / {self.expected["scene"]}'
        if len([c for c in selected if c['id'].endswith(wanted)]) != 1:
            raise RunError('The selected report is not the requested room/scene summary')
        desktop.call('click', selector={'parent_id': 'MenuSaveAs', 'name': '另存为...', 'type': 'Text'})
        desktop.call('invoke', selector={'id': 'ExportFormatMenuItem_Pdf', 'type': 'MenuItem'})
        desktop.wait(lambda s: bool(controls(s, id='buttonOk')), seconds=20, description='PDF export settings')
        for control_id, value in (('checkBoxOpenAfterExport', False), ('checkBoxEmbeddedFonts', True),
                                  ('checkBoxExportRtfTextAsImage', False)):
            desktop.toggle(control_id, value)
        desktop.click('buttonOk')
        pdf = self.directory / filename
        desktop.save_dialog(pdf)
        return pdf

    def render(self) -> Path:
        desktop = self.desktop
        desktop.select_mode('ExportMode', ready_id='RaytraceTool')
        desktop.call('select', selector={'id': 'ViewTool', 'type': 'TabItem'})
        desktop.call('select', selector={'id': 'RaytraceTool', 'type': 'TabItem'})
        desktop.click('buttonRenderer')
        desktop.wait(lambda s: bool(controls(s, id='aid_Start', enabled=True)), seconds=60,
                     description='raytracer ready')
        started = desktop.call('start_and_observe', selector={'id': 'aid_Start'}, activity_id='aid_Abort')
        atomic_json(self.directory / 'raytrace-start-observation.json', started)
        self.evidence('raytrace-started')
        desktop.wait(lambda s: bool(controls(s, id='aid_Start', enabled=True))
                     and bool(controls(s, id='aid_Abort', enabled=False)),
                     seconds=self.limits['raytrace_seconds'], description='raytrace to finish')
        self.evidence('raytrace-completed')
        desktop.click('aid_Export')
        path = self.directory / 'dialux-raytrace.jpg'
        desktop.save_dialog(path)
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            if image.size != (400, 300) or image.format != 'JPEG':
                raise RunError('Raytrace differs from the accepted 400 x 300 JPEG profile')
        snapshot = desktop.snapshot()
        if controls(snapshot, id='RaytraceWindowCloseButton', enabled=True):
            desktop.click('RaytraceWindowCloseButton')
        return path

    def reopen(self, project: Path, calculated: dict) -> None:
        desktop = self.desktop
        snapshot = desktop.snapshot()
        if snapshot['title'].startswith(str(project) + '* - DIALux'):
            stat = project.stat()
            previous = (stat.st_size, stat.st_mtime_ns)
            desktop.click('Save')
            desktop.wait(lambda s: s['title'].startswith(str(project) + ' - DIALux'), seconds=60,
                         description='final project save')
            # DIALux can clear the title's dirty marker before its background
            # save writes new bytes. Unchanged old bytes are not a saved result.
            desktop.wait_file(project, seconds=60, changed_since=previous)
        else:
            desktop.wait_file(project)
        before = sha256(project)
        # Actually unload the result project before opening it again; selecting
        # the already-open path could simply activate an existing document.
        desktop.open_project(self.directory / 'working.evo')
        desktop.open_project(project)
        desktop.goto_tool('MenuGotoShowLuminaireArrangementTool', 'LuminaireArrangementTool')
        desktop.toggle('ShowResultsMonitor', True)
        values = monitor_values(self.evidence('reopened-project'), self.expected)
        if values != calculated or sha256(project) != before:
            raise RunError('Reopened project results or saved bytes changed')

    def execute(self) -> None:
        with self.step('open_verified_input_copy'):
            self.start()
        with self.step('configure_full_calculation_and_clear_old_results'):
            self.configure_and_clear()
        with self.step('real_calculation'):
            values = self.calculate()
        with self.step('save_native_project'):
            project = self.save_project()
        with self.step('export_and_validate_pdf'):
            pdf = self.export_pdf()
            result = parse_pdf(pdf, self.directory / 'room-summary.txt', self.expected)
            for metric, value in values.items():
                if not math.isclose(value, result['metrics'][metric]['value'], rel_tol=0, abs_tol=1e-9):
                    raise RunError(f'PDF and current calculation disagree: {metric}')
            result.update(run_id=self.job['run_id'], job_sha256=self.store.job_hash,
                          input_artifacts=self.job['inputs'], versions=self.job['versions'],
                          native_project='project.evo', execution_profile=self.job['profile'])
        with self.step('fresh_dialux_raytrace_and_export'):
            self.render()
        with self.step('reopen_and_verify_native_results'):
            self.reopen(project, values)
            result['native_project_sha256'] = sha256(project)
            result['checks'] = {'real_calculation_completed': True, 'native_project_saved': True,
                                'native_project_reopened': True, 'results_exported': True,
                                'matching_room_surface_scene': True, 'fresh_raytrace_exported': True}
            atomic_json(self.directory / 'results.json', result)
        verify_artifacts(self.store.root, self.job['inputs'])
        self.store.finish(self.directory)


def run_job(root: Path, *, resume: bool = False) -> dict:
    with SessionLock():
        store = RunStore(root)
        validate_job(store)
        if store.completed():
            return store.state  # Idempotent: revalidate files, perform no desktop action.
        if (store.root / 'pause.request').exists():
            raise RunError('Remove pause.request after reviewing the run before starting/resuming')
        if store.state['attempts']:
            pid = store.state['attempts'][-1].get('process_id')
            if pid and process_exists(pid):
                raise RunError(f'Previous DIALux instance {pid} is still running; inspect and close that instance before --resume')
        directory = store.begin_attempt(resume=resume)
        if store.job['profile'] == 'evo-5.14.0.3-zh-synthetic-envelope-v1':
            from .envelope_run import EnvelopeRun
            runner = EnvelopeRun(store, directory)
        elif store.job['profile'] == 'evo-5.14.0.3-zh-generic-ifc-v1':
            from .generic_run import GenericImport
            runner = GenericImport(store, directory)
        elif store.job['profile'] == 'evo-5.14.0.3-zh-synthetic-ifc-v1':
            from .fresh import FreshImport
            runner = FreshImport(store, directory)
        else:
            runner = NativeReplay(store, directory)
        try:
            runner.execute()
        except (Exception, KeyboardInterrupt) as error:
            # A timeout never means "safe to click again". Preserve the working
            # process for inspection, and replay immutable inputs on recovery.
            if runner.desktop:
                try:
                    runner.desktop.deadline = time.monotonic() + 20
                    runner.evidence('failure')
                except Exception as capture_error:
                    store.event('failure_evidence_unavailable', reason=str(capture_error))
            store.handoff(f'{runner.current_step}: {error or type(error).__name__}')
            raise RunError(f'Execution needs attention; see {store.state_path}: {error}') from error
        return store.state
