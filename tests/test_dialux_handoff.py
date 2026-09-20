"""Tests for the shared DIALux handoff verification helpers."""

from __future__ import annotations

from lighting_agent.deliverables import (
    build_simulation_run_from_handoff,
    verify_dialux_handoff,
)
from lighting_agent.schemas import SimulationMetrics

_HANDOFF_ID = "handoff-0123456789abcdef"
_SNAPSHOT_SHA256 = "a" * 64


def _package(**overrides: object) -> dict:
    package: dict = {
        "handoff_id": _HANDOFF_ID,
        "input_snapshot_sha256": _SNAPSHOT_SHA256,
        "input_snapshot": {
            "project_id": "0123456789abcdef",
            "project_revision": 3,
            "selected_luminaire_ids": ["lum-1"],
        },
        "selected_luminaire_ids": ["lum-1"],
        "photometry_sha256_by_luminaire": {"lum-1": "b" * 64},
    }
    package.update(overrides)
    return package


def _verify(package: dict, **overrides: object) -> list[str]:
    parameters: dict = {
        "project_id": "0123456789abcdef",
        "expected_revision": 3,
        "handoff_id": _HANDOFF_ID,
        "selected_luminaire_ids": ["lum-1"],
        "input_snapshot_sha256": _SNAPSHOT_SHA256,
    }
    parameters.update(overrides)
    return verify_dialux_handoff(package, **parameters)


def test_verify_dialux_handoff_accepts_matching_package() -> None:
    assert _verify(_package()) == []


def test_verify_dialux_handoff_reports_all_mismatches() -> None:
    messages = _verify(
        _package(),
        project_id="ffffffffffffffff",
        expected_revision=9,
        handoff_id="handoff-ffffffffffffffff",
        selected_luminaire_ids=["lum-2"],
        input_snapshot_sha256="c" * 64,
    )
    assert len(messages) == 5


def test_verify_dialux_handoff_skips_snapshot_check_when_absent() -> None:
    assert _verify(_package(), input_snapshot_sha256=None) == []


def test_build_simulation_run_from_handoff_marks_matched() -> None:
    run = build_simulation_run_from_handoff(
        _package(),
        project_id="0123456789abcdef",
        expected_revision=3,
        handoff_id=_HANDOFF_ID,
        selected_luminaire_ids=["lum-1"],
        metrics=SimulationMetrics(maintained_illuminance_lx=750),
        source_kind="manual_form",
        parser_version="manual-form-1",
    )

    assert run.verification_status == "matched"
    assert run.status == "succeeded"
    assert run.handoff_id == _HANDOFF_ID
    assert run.input_snapshot_sha256 == _SNAPSHOT_SHA256
    assert run.selected_luminaire_ids == ["lum-1"]
    assert run.photometry_sha256_by_luminaire == {"lum-1": "b" * 64}
    assert run.verification_messages == []


def test_build_simulation_run_from_handoff_marks_mismatch() -> None:
    run = build_simulation_run_from_handoff(
        _package(),
        project_id="0123456789abcdef",
        expected_revision=99,
        handoff_id=_HANDOFF_ID,
        selected_luminaire_ids=["lum-1"],
        metrics=SimulationMetrics(maintained_illuminance_lx=750),
        source_kind="manual_form",
    )

    assert run.verification_status == "mismatch"
    assert run.status == "unverified"
    assert run.verification_messages == ["任务包中的项目 revision 与导入 revision 不一致"]
