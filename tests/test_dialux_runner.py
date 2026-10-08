from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

import pytest

from lighting_agent.dialux_runner.artifacts import (
    MAX_LIMITS, NATIVE_BASELINE_SHA256, REPLAY_EXPECTED, RunError, RunStore,
    atomic_json, sha256, validate_replay_job, verify_artifacts,
)
from lighting_agent.dialux_runner.desktop import Desktop, SessionLock, one
from lighting_agent.dialux_runner.runner import monitor_values


def make_job(root: Path) -> Path:
    root.mkdir()
    (root / 'input.evo').write_bytes(b'isolated test input')
    atomic_json(root / 'job.json', {'schema_version': 1, 'profile': 'evo-5.14.0.3-zh-synthetic-replay-v1',
                                  'run_id': 'test-run', 'inputs': {'input.evo': sha256(root / 'input.evo')}})
    return root


def test_recovery_preserves_failed_attempt_and_never_reuses_its_files(tmp_path):
    root = make_job(tmp_path / 'job')
    store = RunStore(root)
    first = store.begin_attempt()
    (first / 'project.evo').write_bytes(b'partial native output')
    store.step('import', 'started')
    store.handoff('uncertain outcome after timeout')
    restored = RunStore(root)
    with pytest.raises(RunError, match='--resume'):
        restored.begin_attempt()
    second = restored.begin_attempt(resume=True)
    assert second != first
    assert list(second.iterdir()) == []
    assert (first / 'project.evo').read_bytes() == b'partial native output'
    assert restored.state['attempts'][0]['reason'] == 'uncertain outcome after timeout'
    assert restored.state['attempts'][0]['status'] == 'needs_attention'


def test_completed_run_revalidates_export_hashes(tmp_path):
    store = RunStore(make_job(tmp_path / 'job'))
    directory = store.begin_attempt()
    (directory / 'results.json').write_text('{"value": 12}', encoding='utf-8')
    store.finish(directory)
    assert RunStore(store.root).completed()
    (directory / 'results.json').write_text('{"value": 13}', encoding='utf-8')
    with pytest.raises(RunError, match='hash mismatch'):
        RunStore(store.root).completed()


def test_executor_edit_during_attempt_prevents_success(tmp_path, monkeypatch):
    store = RunStore(make_job(tmp_path / 'job'))
    directory = store.begin_attempt()
    monkeypatch.setattr(store, 'executor_hashes', lambda: {'runner.py': 'changed-during-run'})
    with pytest.raises(RunError, match='code changed'):
        store.finish(directory)
    assert store.state['status'] == 'running'


def test_timed_out_mutation_is_never_automatically_retried(tmp_path, monkeypatch):
    attempts = []

    def timeout(command, **kwargs):
        attempts.append(command)
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])

    monkeypatch.setattr(subprocess, 'run', timeout)
    desktop = Desktop(123, tmp_path / 'commands', time.monotonic() + 60)
    with pytest.raises(RunError, match='outcome is unknown'):
        desktop.click('CalculationButtonStart')
    assert len(attempts) == 1
    assert len(list((tmp_path / 'commands').glob('*.request.json'))) == 1


def test_save_wait_requires_new_bytes_even_when_old_file_is_stable(tmp_path, monkeypatch):
    from types import SimpleNamespace
    moments = [0.]
    reads = []

    def stat():
        reads.append(moments[0])
        # The native app acknowledges Save immediately but writes 2s later.
        return SimpleNamespace(st_size=10, st_mtime_ns=100 if moments[0] < 2 else 200)

    monkeypatch.setattr(time, 'monotonic', lambda: moments[0])
    monkeypatch.setattr(time, 'sleep', lambda seconds: moments.__setitem__(0, moments[0] + seconds))
    desktop = Desktop(123, tmp_path / 'commands', 30.)
    desktop.wait_file(SimpleNamespace(stat=stat), changed_since=(10, 100))
    assert reads[-1] >= 2.9


def test_transient_snapshot_can_retry_but_changed_process_cannot(tmp_path, monkeypatch):
    responses = [
        {'ok': False, 'code': 'snapshot_unavailable', 'error': 'dialog is rebuilding'},
        {'ok': True, 'result': {'started_ticks': '12345', 'main_window_handle': '6789', 'controls': []}},
        {'ok': False, 'code': 'desktop_failure', 'error': 'Target process has changed'},
    ]
    calls = []

    def response(command, **kwargs):
        calls.append(command)
        value = responses.pop(0)
        return subprocess.CompletedProcess(command, 0 if value['ok'] else 1, json.dumps(value), '')

    monkeypatch.setattr(subprocess, 'run', response)
    desktop = Desktop(123, tmp_path / 'commands', time.monotonic() + 60)
    assert desktop.snapshot()['started_ticks'] == '12345'
    assert len(calls) == 2
    with pytest.raises(RunError, match='process has changed'):
        desktop.snapshot()
    assert len(calls) == 3
    request = json.loads((tmp_path / 'commands/00003-snapshot.request.json').read_text(encoding='utf-8'))
    assert request['started_ticks'] == '12345'
    assert request['main_window_handle'] == '6789'


def test_changed_input_and_changed_job_are_rejected_before_resume(tmp_path):
    store = RunStore(make_job(tmp_path / 'job'))
    store.begin_attempt()
    (store.root / 'input.evo').write_bytes(b'changed')
    with pytest.raises(RunError, match='hash mismatch'):
        RunStore(store.root)
    job = json.loads(store.job_path.read_text(encoding='utf-8'))
    job['inputs']['input.evo'] = sha256(store.root / 'input.evo')
    atomic_json(store.job_path, job)
    with pytest.raises(RunError, match='Job changed'):
        RunStore(store.root)


def test_interrupted_attempt_is_preserved_on_recovery(tmp_path):
    root = make_job(tmp_path / 'job')
    store = RunStore(root)
    store.begin_attempt()
    restored = RunStore(root)
    restored.begin_attempt(resume=True)
    assert restored.state['attempts'][0]['status'] == 'interrupted'
    assert restored.state['attempts'][1]['status'] == 'running'


def test_manifest_cannot_escape_package(tmp_path):
    root = tmp_path / 'job'
    root.mkdir()
    outside = tmp_path / 'outside.txt'
    outside.write_text('external input', encoding='utf-8')
    with pytest.raises(RunError, match='outside'):
        verify_artifacts(root, {'../outside.txt': sha256(outside)})


def monitor_snapshot():
    def control(id, name):
        return {'id': id, 'name': name, 'offscreen': False, 'enabled': True}
    return {'controls': [control('LightSceneName', '已激活的灯光场景：灯光场景 1'),
                         control('ResultGroupName', '101'), control('ResultGroupName', '工作面 (101)'),
                         control('ResultsMonitorSurfaceResultAverage', '23.2 lx'),
                         control('ResultsMonitorSurfaceUniformityAverage', '0.46')]}


def test_monitor_requires_a_single_result_and_matching_scene_room_surface():
    expected = {'room': '101', 'surface': '工作面 (101)', 'scene': '灯光场景 1'}
    snapshot = monitor_snapshot()
    assert monitor_values(snapshot, expected) == {'average_illuminance_lx': 23.2, 'uniformity_u0': .46}
    snapshot['controls'].append(snapshot['controls'][-2].copy())
    with pytest.raises(RunError, match='exactly one'):
        monitor_values(snapshot, expected)
    with pytest.raises(RunError, match='scene'):
        monitor_values(monitor_snapshot(), {**expected, 'scene': '灯光场景 2'})
    with pytest.raises(RunError, match='room/surface'):
        monitor_values(monitor_snapshot(), {**expected, 'room': '102'})


def test_duplicate_controls_cannot_choose_an_arbitrary_target():
    snapshot = monitor_snapshot()
    snapshot['controls'].append(snapshot['controls'][0].copy())
    with pytest.raises(RunError, match='exactly one'):
        one(snapshot, id='LightSceneName')


def test_profile_cannot_claim_unapplied_layout_settings_or_unbounded_runtime(tmp_path, monkeypatch):
    root = tmp_path / 'job'
    (root / 'inputs').mkdir(parents=True)
    names = ('bridge.evo', 'bridge.dxf', 'bridge.ifc', 'spatial-model.json', 'synthetic-bridge-luminaire.ies')
    record = {'luminaire_layout': [{'position_m': [3, 2, 2.5]}], 'calculation_settings': {'grid_mode': 'automatic'},
              'versions': {'layout': 'v1'}, 'artifacts': {name: 'a' * 64 for name in names}}
    record['artifacts']['bridge.evo'] = NATIVE_BASELINE_SHA256
    atomic_json(root / 'inputs/run-record.json', record)
    job = {'expected': REPLAY_EXPECTED, 'dialux_version': '5.14.0.3', 'native_input': 'inputs/bridge.evo',
           'inputs': {'inputs/' + name: record['artifacts'][name] for name in names},
           'layout': record['luminaire_layout'], 'settings': record['calculation_settings'],
           'versions': record['versions'], 'limits': dict(MAX_LIMITS)}
    job['inputs']['inputs/run-record.json'] = sha256(root / 'inputs/run-record.json')
    monkeypatch.setattr('lighting_agent.dialux_runner.artifacts.BASELINE_RECORD_SHA256', job['inputs']['inputs/run-record.json'])
    from types import SimpleNamespace
    store = SimpleNamespace(root=root, job=job)
    validate_replay_job(store)
    job['layout'] = [{'position_m': [0, 0, 0]}]
    with pytest.raises(RunError, match='Layout'):
        validate_replay_job(store)
    job['layout'] = record['luminaire_layout']
    for bad_limit in (0, -1, float('nan'), float('inf'), 1201, True, '300'):
        job['limits']['total_seconds'] = bad_limit
        with pytest.raises(RunError, match='limit'):
            validate_replay_job(store)


@pytest.mark.skipif(os.name != 'nt', reason='Windows session mutex')
def test_desktop_mutex_excludes_other_executors_and_releases_after_exception():
    name = 'Local\\DIALuxExecutorTest-' + uuid4().hex

    def competing():
        with SessionLock(name):
            return 'acquired'

    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(ValueError, match='test failure'):
            with SessionLock(name):
                with pytest.raises(RunError, match='owns this desktop'):
                    pool.submit(competing).result(timeout=10)
                raise ValueError('test failure')
        assert pool.submit(competing).result(timeout=10) == 'acquired'
