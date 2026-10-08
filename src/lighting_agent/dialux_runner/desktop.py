"""Bounded UI Automation commands and a Windows session mutex."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any, Callable

from .artifacts import RunError, atomic_json


class SnapshotUnavailable(RunError):
    """UIA tree changed during a read; no mutation was attempted."""


class SessionLock:
    """The Windows Local namespace is shared by all processes in this session."""

    def __init__(self, name: str = r'Local\LightingAgent.DIALuxExecutor'):
        self.name = name
        self.handle = None

    def __enter__(self):
        if os.name != 'nt':
            raise RunError('DIALux execution requires a Windows interactive desktop')
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        api.CreateMutexW.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.ReleaseMutex.argtypes = [wintypes.HANDLE]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api = api
        self.handle = api.CreateMutexW(None, False, self.name)
        if not self.handle:
            raise RunError(f'Cannot create desktop mutex: {ctypes.get_last_error()}')
        result = api.WaitForSingleObject(self.handle, 0)
        if result not in (0, 0x80):  # Acquired or abandoned by a crashed process.
            api.CloseHandle(self.handle)
            self.handle = None
            raise RunError('Another executor owns this desktop session')
        return self

    def __exit__(self, *_):
        if self.handle:
            self.api.ReleaseMutex(self.handle)
            self.api.CloseHandle(self.handle)
            self.handle = None


def controls(snapshot: dict, **selector: Any) -> list[dict]:
    return [c for c in snapshot['controls'] if not c['offscreen']
            and all(c.get(key) == value for key, value in selector.items())]


def one(snapshot: dict, **selector: Any) -> dict:
    matches = controls(snapshot, **selector)
    if len(matches) != 1:
        raise RunError(f'Expected exactly one control {selector}, found {len(matches)}')
    return matches[0]


class Desktop:
    def __init__(self, pid: int, log_dir: Path, deadline: float):
        self.pid = pid
        self.log_dir = log_dir
        log_dir.mkdir(exist_ok=True)
        self.started_ticks: str | None = None
        self.main_window_handle: str | None = None
        self.expected_project: Path | None = None
        self.deadline = deadline
        self.sequence = 0

    def call(self, action: str, **kwargs) -> Any:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RunError('Total run time limit reached')
        self.sequence += 1
        prefix = self.log_dir / f'{self.sequence:05d}-{action}'
        request = {'pid': self.pid, 'started_ticks': self.started_ticks, 'action': action,
                   'main_window_handle': self.main_window_handle,
                   'expected_project': str(self.expected_project) if self.expected_project else None, **kwargs}
        atomic_json(prefix.with_suffix('.request.json'), request)
        try:
            response = subprocess.run([
                'powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                '-File', str(Path(__file__).with_name('uia.ps1')),
                '-RequestPath', str(prefix.with_suffix('.request.json')),
            ], capture_output=True, encoding='utf-8', errors='replace', timeout=min(30, remaining),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        except subprocess.TimeoutExpired as error:
            raise RunError(f'Desktop command timed out; operation outcome is unknown: {action}') from error
        try:
            payload = json.loads(response.stdout)
        except ValueError as error:
            raise RunError(f'Desktop returned invalid JSON: {response.stderr[:600]}') from error
        atomic_json(prefix.with_suffix('.response.json'), payload)
        if response.returncode or not payload.get('ok'):
            if payload.get('code') == 'snapshot_unavailable':
                raise SnapshotUnavailable(payload['error'])
            raise RunError(payload.get('error', response.stderr[:600]))
        return payload['result']

    def snapshot(self) -> dict:
        for attempt in range(3):
            try:
                value = self.call('snapshot')
                break
            except SnapshotUnavailable:
                if attempt == 2:
                    raise
                time.sleep(.25)
        if self.started_ticks is None:
            self.started_ticks = value['started_ticks']
            self.main_window_handle = value['main_window_handle']
        return value

    def wait(self, predicate: Callable[[dict], Any], *, seconds: float, description: str) -> dict:
        deadline = min(self.deadline, time.monotonic() + seconds)
        last = None
        while time.monotonic() < deadline:
            # Retry only reads; never replay a mutation whose outcome is unknown.
            snapshot = self.snapshot()
            last = snapshot
            if predicate(snapshot):
                return snapshot
            time.sleep(min(.3, max(0, deadline - time.monotonic())))
        raise RunError(f'Timed out waiting for {description}; last title: {(last or {}).get("title")}')

    def click(self, control_id: str, **selector) -> None:
        self.call('click', selector={'id': control_id, **selector})

    def select_mode(self, name: str, *, ready_id: str) -> None:
        if controls(self.snapshot(), id=ready_id, type='TabItem', enabled=True):
            return
        selector = {'name': f'Dial.Dialux.InteractionModes.{name}', 'type': 'TabItem'}
        self.call('select', selector=selector)
        # DIALux virtualises the selected mode tab itself out of parts of the
        # UIA tree. Check the destination's actual tool palette instead.
        self.wait(lambda s: len(controls(s, id=ready_id, type='TabItem', enabled=True)) == 1,
                  seconds=30, description=name)

    def goto_tool(self, menu_id: str, tool_id: str) -> None:
        self.call('menu', path=f'MenuGotoConstructionMode/{menu_id}')
        self.wait(lambda s: bool(controls(s, id=tool_id, type='TabItem', value=True)),
                  seconds=60, description=tool_id)

    def toggle(self, control_id: str, enabled: bool) -> dict:
        value = 'On' if enabled else 'Off'
        self.call('toggle', selector={'id': control_id}, value=value)
        return self.wait(lambda s: len(controls(s, id=control_id, value=value)) == 1, seconds=15,
                         description=f'{control_id}={value}')

    def open_project(self, path: Path) -> None:
        self.call('menu', path='File/OpenProject')
        self.file_dialog(path, save=False)
        self.wait(lambda s: s['title'].startswith(str(path.resolve()) + ' - DIALux'), seconds=90,
                  description='requested native project')
        self.expected_project = path.resolve()

    def file_dialog(self, path: Path, *, save: bool, title: str | None = None) -> None:
        path = path.resolve()
        if save and path.exists():
            raise RunError(f'Refusing to overwrite an existing export: {path}')
        if not save and not path.is_file():
            raise RunError(f'File to open is missing: {path}')
        field = 'save_edits' if save else 'open_edits'
        snapshot = self.wait(lambda s: len(s.get('native_dialogs', [])) == 1
                             and s['native_dialogs'][0][field] == 1,
                             seconds=20, description='native file dialog')
        dialog = snapshot['native_dialogs'][0]
        if title and dialog['title'] != title:
            raise RunError(f'Unexpected native dialog: {dialog["title"]}')
        self.call('file_dialog', title=dialog['title'], edit_id=1001 if save else 1148, path=str(path))
        self.wait(lambda s: not s.get('native_dialogs'), seconds=30, description='file dialog to close')
        if save:
            self.wait_file(path)

    def save_dialog(self, path: Path) -> None:
        self.file_dialog(path, save=True)

    def wait_file(self, path: Path, seconds: float = 30, *, changed_since: tuple[int, int] | None = None) -> None:
        end = min(self.deadline, time.monotonic() + seconds)
        previous = None
        stable = 0
        while time.monotonic() < end:
            try:
                stat = path.stat()
                current = (stat.st_size, stat.st_mtime_ns)
            except FileNotFoundError:
                current = None
            stable = stable + 1 if current and current != changed_since and current == previous and current[0] > 0 else 0
            if stable >= 3:
                return
            previous = current
            time.sleep(.3)
        raise RunError(f'Export is missing, empty or still changing: {path}')
