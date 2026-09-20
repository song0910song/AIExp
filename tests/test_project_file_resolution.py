from __future__ import annotations

from pathlib import Path

import pytest

import lighting_agent.tools as agent_tools
from lighting_agent.project_files import ProjectFileResolutionError, resolve_project_file
from lighting_agent.project_store import ProjectStore
from lighting_agent.redesign_service import _resolve_report
from lighting_agent.schemas import DesignBrief


def _project(tmp_path: Path):
    store = ProjectStore(tmp_path / "projects")
    state = store.create(DesignBrief(project_name="Uploaded source resolution"))
    return store, state


def test_agent_tools_resolve_uploaded_basenames_in_project_directories(
    tmp_path, monkeypatch
) -> None:
    store, state = _project(tmp_path)
    documents = store.directory / f"{state.project_id}.documents"
    plans = store.directory / f"{state.project_id}.plans"
    documents.mkdir()
    plans.mkdir()
    report = documents / "512会议室_说明_报告-77dd228d.pdf"
    drawing = plans / "512会议室-11223344.dxf"
    report.write_bytes(b"pdf placeholder")
    drawing.write_text("dxf placeholder", encoding="utf-8")

    monkeypatch.setattr(agent_tools, "project_store", store)
    monkeypatch.setattr(
        agent_tools,
        "parse_dialux_report",
        lambda path: {"resolved_path": str(path)},
    )
    monkeypatch.setattr(
        agent_tools,
        "extract_design",
        lambda path: {"resolved_path": str(path)},
    )

    pdf_result = agent_tools.analyze_dialux_report.invoke(
        {"project_id": state.project_id, "source": report.name}
    )
    dxf_result = agent_tools.analyze_dxf_design.invoke(
        {"project_id": state.project_id, "source": drawing.name}
    )

    assert Path(pdf_result["report"]["resolved_path"]) == report.resolve()
    assert Path(dxf_result["snapshot"]["resolved_path"]) == drawing.resolve()


def test_dwg_is_reported_as_found_but_requires_dxf_for_deep_analysis(
    tmp_path, monkeypatch
) -> None:
    store, state = _project(tmp_path)
    plans = store.directory / f"{state.project_id}.plans"
    plans.mkdir()
    drawing = plans / "meeting-room.dwg"
    drawing.write_bytes(b"dwg placeholder")
    monkeypatch.setattr(agent_tools, "project_store", store)

    result = agent_tools.analyze_dxf_design.invoke(
        {"project_id": state.project_id, "source": drawing.name}
    )

    assert result["status"] == "needs_dxf_export"
    assert "已找到 DWG 文件" in result["message"]
    assert drawing.name in result["message"]


def test_redesign_service_resolves_an_uploaded_report_basename(tmp_path) -> None:
    store, state = _project(tmp_path)
    documents = store.directory / f"{state.project_id}.documents"
    documents.mkdir()
    report = documents / "design-report-abcd1234.pdf"
    report.write_bytes(b"pdf placeholder")

    assert _resolve_report(store.directory, state, report.name) == report.resolve()


def test_resolver_rejects_traversal_and_other_projects_managed_directories(tmp_path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"outside")
    other = root / "otherproject.documents"
    other.mkdir()
    (other / "private.pdf").write_bytes(b"private")

    with pytest.raises(ProjectFileResolutionError, match="当前项目目录"):
        resolve_project_file(root, "currentproject", "../outside.pdf", suffixes={".pdf"})
    with pytest.raises(ProjectFileResolutionError, match="当前项目目录"):
        resolve_project_file(
            root,
            "currentproject",
            "otherproject.documents/private.pdf",
            suffixes={".pdf"},
        )


def test_resolver_requires_relative_path_when_uploaded_name_is_ambiguous(tmp_path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    project_id = "currentproject"
    for kind in ("documents", "dialux-results"):
        directory = root / f"{project_id}.{kind}"
        directory.mkdir()
        (directory / "report.pdf").write_bytes(kind.encode())

    with pytest.raises(ProjectFileResolutionError, match="多个同名文件"):
        resolve_project_file(root, project_id, "report.pdf", suffixes={".pdf"})
