"""Immutable inputs and crash-safe run records for the standalone executor."""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any
from uuid import uuid4


class RunError(RuntimeError):
    """An unmet precondition or unverified desktop operation."""


NATIVE_BASELINE_SHA256 = '32dd3d248f9d5e3fd77b641a2ad3e1b58c82c137928f00b0f8616254581cc99c'
BASELINE_RECORD_SHA256 = '258c22f5f6cf8df882da1b492921e334dca15b1243f13ea969414ac55322048e'
REPLAY_EXPECTED = {
    'building': 'IFC bridge fixture', 'floor': '1F', 'room': '101 - Bridge test room',
    'scene': '灯光场景 1', 'surface': '工作面 (101 - Bridge test room)', 'surface_index': 'WP1',
    'floor_area_m2': 24.0, 'workplane_height_m': .8, 'edge_margin_m': .5,
    'maintenance_factor': .8, 'mounting_height_m': 2.5,
    'ceiling_reflectance': .7, 'wall_reflectance': .5, 'floor_reflectance': .2,
}
MAX_LIMITS = {'startup_seconds': 120, 'step_seconds': 90, 'calculation_seconds': 300,
              'raytrace_seconds': 300, 'total_seconds': 1200}


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f'.{path.name}.{uuid4().hex}.tmp')
    try:
        with temporary.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def confined_file(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise RunError(f'Input is missing or outside its package: {relative}')
    return path


def verify_artifacts(root: Path, artifacts: dict[str, str]) -> None:
    if not artifacts:
        raise RunError('The artifact manifest is empty')
    for name, expected in artifacts.items():
        path = confined_file(root, name)
        if sha256(path) != expected:
            raise RunError(f'Artifact hash mismatch: {name}')


def prepare_replay(source: Path, output: Path, executable: Path) -> Path:
    """Snapshot the accepted synthetic native project, without opening DIALux.

    Replay intentionally cannot accept an arbitrary hand-written design record.
    Extending geometry/photometry belongs to a separately validated profile.
    """
    source, output, executable = source.resolve(), output.resolve(), executable.resolve()
    record_path = source / 'run-record.json'
    if sha256(record_path) != BASELINE_RECORD_SHA256:
        raise RunError('Acceptance record differs from the verified phase-0 archive')
    record = json.loads(record_path.read_text(encoding='utf-8'))
    if record.get('run_id') != 'phase0-20261005' or record.get('status') != 'single_room_bridge_verified':
        raise RunError('This profile requires the accepted phase0-20261005 synthetic bridge archive')
    if not all(record.get('checks', {}).get(key) is True for key in (
        'real_calculation_completed', 'native_project_saved', 'native_project_reopened',
        'results_exported', 'matching_room_surface_scene',
    )):
        raise RunError('Baseline acceptance evidence is incomplete')
    verify_artifacts(source, record['artifacts'])
    # Anchor the native template whose layout, geometry and photometry were
    # actually inspected. A modified template needs fresh acceptance.
    if record['artifacts'].get('bridge.evo') != NATIVE_BASELINE_SHA256:
        raise RunError('Native template differs from the accepted single-room bridge')
    if not executable.is_file():
        raise RunError(f'DIALux executable is missing: {executable}')
    output.mkdir(parents=True, exist_ok=False)
    inputs = output / 'inputs'
    inputs.mkdir()
    names = ('bridge.evo', 'bridge.dxf', 'bridge.ifc', 'spatial-model.json',
             'synthetic-bridge-luminaire.ies', 'run-record.json')
    hashes = {}
    for name in names:
        shutil.copyfile(source / name, inputs / name)
        hashes[f'inputs/{name}'] = sha256(inputs / name)
        expected = sha256(record_path) if name == 'run-record.json' else record['artifacts'][name]
        if hashes[f'inputs/{name}'] != expected:
            raise RunError(f'Input changed during snapshot: {name}')
    job = {
        'schema_version': 1, 'profile': 'evo-5.14.0.3-zh-synthetic-replay-v1',
        'run_id': uuid4().hex, 'prepared_at': timestamp(),
        'purpose': 'synthetic execution validation; not project design or compliance',
        'dialux_executable': str(executable), 'dialux_version': '5.14.0.3',
        'inputs': hashes, 'native_input': 'inputs/bridge.evo',
        'versions': record['versions'], 'layout': record['luminaire_layout'],
        'settings': record['calculation_settings'],
        'expected': REPLAY_EXPECTED,
        'limits': MAX_LIMITS,
    }
    atomic_json(output / 'job.json', job)
    return output / 'job.json'


def validate_replay_job(store: RunStore) -> None:
    """Reject edited metadata that would claim a layout/settings never applied."""
    job = store.job
    if job.get('expected') != REPLAY_EXPECTED or job.get('dialux_version') != '5.14.0.3':
        raise RunError('Job expectations differ from the accepted synthetic replay profile')
    if job.get('native_input') != 'inputs/bridge.evo' or job['inputs'].get('inputs/bridge.evo') != NATIVE_BASELINE_SHA256:
        raise RunError('Replay requires the exact accepted native template')
    if job['inputs'].get('inputs/run-record.json') != BASELINE_RECORD_SHA256:
        raise RunError('Replay acceptance record differs from the verified baseline')
    record = json.loads(confined_file(store.root, 'inputs/run-record.json').read_text(encoding='utf-8'))
    if job.get('layout') != record['luminaire_layout'] or job.get('settings') != record['calculation_settings'] or job.get('versions') != record['versions']:
        raise RunError('Layout, settings or version bindings changed; prepare a new validated profile')
    required = {'inputs/' + name for name in ('bridge.evo', 'bridge.dxf', 'bridge.ifc', 'spatial-model.json',
                                             'synthetic-bridge-luminaire.ies', 'run-record.json')}
    if set(job['inputs']) != required:
        raise RunError('Replay input package is incomplete')
    for name in required - {'inputs/run-record.json'}:
        if job['inputs'][name] != record['artifacts'].get(Path(name).name):
            raise RunError(f'Input binding differs from baseline acceptance: {name}')
    validate_limits(job)


def validate_limits(job: dict) -> None:
    if set(job.get('limits', {})) != set(MAX_LIMITS):
        raise RunError('All execution time limits must be specified')
    for key, maximum in MAX_LIMITS.items():
        value = job['limits'][key]
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not 0 < value <= maximum:
            raise RunError(f'Execution limit {key} must be positive and at most {maximum} seconds')


def validate_job(store: RunStore) -> None:
    if store.job['profile'] == 'evo-5.14.0.3-zh-synthetic-replay-v1':
        validate_replay_job(store)
    elif store.job['profile'] == 'evo-5.14.0.3-zh-synthetic-envelope-v1':
        from .envelope_jobs import validate_envelope_job
        validate_envelope_job(store)
        validate_limits(store.job)
    elif store.job['profile'] == 'evo-5.14.0.3-zh-generic-ifc-v1':
        from .generic_jobs import validate_generic_job
        validate_generic_job(store)
        validate_limits(store.job)
    else:
        from .imports import validate_import_job
        validate_import_job(store)
        validate_limits(store.job)


class RunStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.job_path = self.root / 'job.json'
        self.job = json.loads(self.job_path.read_text(encoding='utf-8'))
        if self.job.get('schema_version') != 1 or self.job.get('profile') not in (
            'evo-5.14.0.3-zh-synthetic-replay-v1', 'evo-5.14.0.3-zh-synthetic-ifc-v1',
            'evo-5.14.0.3-zh-synthetic-envelope-v1', 'evo-5.14.0.3-zh-generic-ifc-v1',
        ):
            raise RunError('Unsupported job profile')
        self.job_hash = sha256(self.job_path)
        self.state_path = self.root / 'state.json'
        self.state = json.loads(self.state_path.read_text(encoding='utf-8')) if self.state_path.exists() else {
            'schema_version': 1, 'run_id': self.job['run_id'], 'job_sha256': self.job_hash,
            'status': 'prepared', 'attempts': [],
        }
        if self.state.get('job_sha256') != self.job_hash or self.state.get('run_id') != self.job['run_id']:
            raise RunError('Job changed after execution began; prepare a new run')
        verify_artifacts(self.root, self.job['inputs'])

    def save(self) -> None:
        self.state['updated_at'] = timestamp()
        atomic_json(self.state_path, self.state)

    def completed(self) -> bool:
        if self.state['status'] != 'completed':
            return False
        attempt = self.state['attempts'][-1]
        verify_artifacts(self.root, attempt['artifacts'])
        return True

    def begin_attempt(self, *, resume: bool = False) -> Path:
        if self.state['status'] == 'completed':
            raise RunError('A completed run cannot be executed again')
        if self.state['attempts'] and not resume:
            raise RunError('Run has an earlier attempt; inspect state.json then use --resume')
        number = len(self.state['attempts']) + 1
        directory = self.root / f'attempt-{number:03d}'
        directory.mkdir(exist_ok=False)
        if self.state['attempts'] and self.state['attempts'][-1]['status'] == 'running':
            self.state['attempts'][-1]['status'] = 'interrupted'
        self.state['attempts'].append({'number': number, 'directory': directory.name,
                                      'status': 'running', 'started_at': timestamp(), 'steps': [],
                                      'executor_sources': self.executor_hashes()})
        self.state['status'] = 'running'
        self.save()
        return directory

    def event(self, kind: str, **details: Any) -> None:
        entry = {'at': timestamp(), 'event': kind, **details}
        with (self.root / 'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(entry, ensure_ascii=False, allow_nan=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())

    def step(self, name: str, status: str, **details: Any) -> None:
        self.state['attempts'][-1]['steps'].append({'name': name, 'status': status, 'at': timestamp(), **details})
        self.event('step', name=name, status=status, **details)
        self.save()

    def handoff(self, reason: str) -> None:
        self.state['status'] = 'needs_attention'
        self.state['attempts'][-1].update(status='needs_attention', reason=reason,
                                        ended_at=timestamp(), recovery='Inspect evidence and close the dedicated DIALux instance, then --resume; a new attempt reopens immutable inputs.')
        self.event('handoff', reason=reason)
        self.save()

    def finish(self, directory: Path) -> None:
        if self.state['attempts'][-1]['executor_sources'] != self.executor_hashes():
            raise RunError('Executor code changed during the attempt; preserve evidence and run a new attempt')
        artifacts = {str(p.relative_to(self.root)).replace('\\', '/'): sha256(p)
                     for p in sorted(directory.rglob('*')) if p.is_file()}
        self.state['attempts'][-1].update(status='completed', ended_at=timestamp(), artifacts=artifacts)
        self.state['status'] = 'completed'
        self.event('completed', attempt=directory.name)
        self.save()

    @staticmethod
    def executor_hashes() -> dict[str, str]:
        return {p.name: sha256(p) for p in sorted(Path(__file__).parent.iterdir())
                if p.is_file() and p.suffix in ('.py', '.ps1')}
