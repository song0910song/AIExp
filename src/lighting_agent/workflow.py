"""Route project work from uploaded facts, independently of chat history."""

from pathlib import Path

from .dialux_report import DialuxReportParseError, parse_dialux_report
from .project_files import ProjectFileResolutionError, resolve_project_file
from .schemas import ProjectState


def dialux_report_sources(root: Path, state: ProjectState) -> list[str]:
    """Find only this project's readable DIALux reports, not arbitrary PDFs."""

    root = Path(root).resolve()
    sources = [
        artifact.storage_path
        for run in reversed(state.simulation_runs)
        if run.source_kind == "dialux_pdf"
        for artifact in run.artifacts
        if artifact.file_name.casefold().endswith(".pdf")
    ]
    directory = root / f"{state.project_id}.documents"
    if directory.is_dir():
        sources.extend(
            str(path.relative_to(root).as_posix())
            for path in sorted(directory.iterdir())
            if path.is_file() and path.suffix.casefold() == ".pdf"
        )
    reports: list[str] = []
    seen: set[str] = set()
    for source in sources:
        try:
            path = resolve_project_file(root, state.project_id, source, suffixes={".pdf"})
            report = parse_dialux_report(path)
        except (ProjectFileResolutionError, DialuxReportParseError, OSError):
            continue
        # Identical uploads through the document and evidence endpoints count once.
        digest = report["source"]["sha256"]
        if digest not in seen:
            reports.append(str(path.relative_to(root.resolve()).as_posix()))
            seen.add(digest)
    return reports


def project_workflow(root: Path, state: ProjectState) -> dict:
    """Expose the next branch and a plain-text request for missing files."""

    root = Path(root).resolve()
    dxf_source = None
    needs_dxf_export = False
    if state.floor_plan:
        needs_dxf_export = state.floor_plan.asset.source_type == "dwg"
        if not needs_dxf_export:
            try:
                path = resolve_project_file(
                    root, state.project_id, state.floor_plan.asset.storage_path,
                    suffixes={".dxf"},
                )
                dxf_source = str(path.relative_to(root.resolve()).as_posix())
            except ProjectFileResolutionError:
                pass
    reports = dialux_report_sources(root, state)
    missing = []
    if not dxf_source:
        missing.append("DIALux 导出的 DXF 平面图")
    if not reports:
        missing.append("DIALux PDF 设计报告")
    if not missing:
        stage = "analyze_existing_design" if len(reports) == 1 else "choose_report"
        message = (
            "平面图和 DIALux PDF 报告已齐全，请先解析并交叉校验，再进入存量照明重设计；"
            "无需用户另外要求重设计，也无需重复进行新建项目的初算和选型。"
            if len(reports) == 1 else
            "发现多份不同的 DIALux PDF 报告，请用文字询问本次采用哪一份，不要自行猜测。"
        )
    else:
        stage = "awaiting_redesign_files" if state.luminaires else "standard_design"
        message = (
            "请上传" + "和".join(missing) + "，文件齐全后我会先分析资料，再继续存量照明重设计。"
        )
        if needs_dxf_export:
            message = "已收到 DWG，但灯位与评价网格提取需要 DXF，请将平面图另存为 DXF。" + message
        if stage == "standard_design":
            message = "先根据现有说明补全必要参数、进行初算与灯具选型；选型完成后再用纯文字提醒：" + message
    return {
        "stage": stage,
        "dxf_source": dxf_source,
        "report_source": reports[0] if len(reports) == 1 else None,
        "report_sources": reports,
        "missing_files": missing,
        "message": message,
        "upload_request_format": "plain_text",
    }
