"""Deterministic, reviewable handoff artifacts."""

from __future__ import annotations

import json
import hashlib
import zipfile
from io import BytesIO
from pathlib import Path

from .photometry_assets import PhotometryAssetStore
from .schemas import ProjectState


def _canonical_json(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def handoff_snapshot(state: ProjectState) -> dict:
    """Return the immutable project inputs that a DIALux result must match."""

    return {
        "project_id": state.project_id,
        "project_revision": state.revision,
        "brief": state.brief.model_dump(mode="json"),
        "lighting_groups": [
            {
                **group.model_dump(mode="json"),
                "selected_luminaire_ids": state.luminaire_group_assignments.get(group.group_id, []),
            }
            for group in state.brief.lighting_groups
        ],
        "selected_luminaire_ids": state.selected_luminaire_ids,
    }


def handoff_id_for(state: ProjectState) -> str:
    digest = hashlib.sha256(_canonical_json(handoff_snapshot(state))).hexdigest()
    return f"handoff-{digest[:32]}"


def handoff_id_for_snapshot(snapshot: dict) -> str:
    digest = hashlib.sha256(_canonical_json(snapshot)).hexdigest()
    return f"handoff-{digest[:32]}"


def read_dialux_task_package(archive_bytes: bytes) -> dict:
    """Read the manifest embedded in a generated DIALux task archive."""

    with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
        try:
            payload = archive.read("dialux-task.json")
        except KeyError as error:
            raise ValueError("DIALux task archive does not contain dialux-task.json") from error
    package = json.loads(payload.decode("utf-8"))
    if not isinstance(package, dict) or not isinstance(package.get("handoff_id"), str):
        raise ValueError("DIALux task manifest is invalid")
    return package


def build_dialux_task_package(state: ProjectState) -> dict:
    """Return a simulation handoff for final selections only.

    Search results remain in the project as an audit trail, but are excluded
    from this handoff until a user explicitly confirms them as final lights.
    """

    snapshot = handoff_snapshot(state)
    handoff_id = handoff_id_for(state)
    return {
        "handoff_id": handoff_id,
        "project_id": state.project_id,
        "project_revision": state.revision,
        "input_snapshot": snapshot,
        "input_snapshot_sha256": hashlib.sha256(_canonical_json(snapshot)).hexdigest(),
        "brief": state.brief.model_dump(mode="json"),
        "lighting_groups": [
            {
                **group.model_dump(mode="json"),
                "selected_luminaire_ids": state.luminaire_group_assignments.get(group.group_id, []),
            }
            for group in state.brief.lighting_groups
        ],
        "selected_luminaire_ids": state.selected_luminaire_ids,
        "candidates": [
            {
                "luminaire_id": item.luminaire_id,
                "article_name": item.article_name,
                "detail_url": item.detail_url,
                "has_uld": item.has_uld,
                "has_photometry_download": item.has_photometry_download,
                "photometry": {
                    # `imageTriplet` from Luminaire Finder is the published
                    # luminaire intensity-distribution-curve preview. It is
                    # retained verbatim so the DIALux handoff can be audited
                    # and the curve can be opened from the exported package.
                    "curve_preview_url": item.photometry_image_url,
                    "curve_preview_available": item.photometry_image_url is not None,
                    "uld_available": item.has_uld,
                    "photometry_file_download_available": item.has_photometry_download,
                    "source_detail_url": item.detail_url,
                },
                "matching_status": item.matching_status,
                "missing_requested_fields": item.missing_requested_fields,
            }
            for item in state.selected_luminaires()
        ],
        "selection": {
            "status": "selected" if state.selected_luminaire_ids else "pending",
            "message": (
                "Only final selected luminaires are included and downloaded."
                if state.selected_luminaire_ids
                else "No final luminaires have been selected; no photometry files were downloaded."
            ),
        },
        "pending_simulation_metrics": ["maintained illuminance", "uniformity", "UGR", "installed power density"],
        "limitations": ["This package is a DIALux evo handoff, not an executed simulation result."],
    }


def build_dialux_task_archive(state: ProjectState, assets: PhotometryAssetStore) -> bytes:
    """Build a ZIP handoff with the task manifest and per-luminaire ZIP assets.

    Only explicitly selected luminaires are downloaded before archiving. Each
    vendor ZIP and extracted IES/LDT/ULD file is read from the durable project
    asset store. Failed public downloads remain visible in the manifest without
    preventing the rest of the handoff from being delivered.
    """

    package = build_dialux_task_package(state)
    downloaded: list[dict[str, str]] = []
    unavailable: list[dict[str, str]] = []
    by_luminaire_id = {asset.luminaire_id: asset for asset in assets.ensure_task_assets(state)}
    output = BytesIO()

    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for luminaire, candidate in zip(
            state.selected_luminaires(), package["candidates"], strict=True
        ):
            photometry = candidate["photometry"]
            asset = by_luminaire_id[luminaire.luminaire_id]
            if asset.status != "downloaded" or not asset.zip_file:
                photometry["bundle_file"] = None
                photometry["bundle_status"] = asset.status
                if asset.error:
                    photometry["download_error"] = asset.error
                unavailable.append({"luminaire_id": luminaire.luminaire_id, "reason": asset.error or asset.status})
                continue

            try:
                source = assets.read_file(state.project_id, asset.zip_file)
                archive_path = f"photometry/{Path(asset.zip_file).name}"
                archive.write(source, archive_path)
                extracted_paths: list[dict[str, object]] = []
                for extracted in asset.extracted_files:
                    extracted_source = assets.read_file(state.project_id, extracted.relative_path)
                    extracted_archive_path = f"photometry/{extracted.relative_path}"
                    archive.write(extracted_source, extracted_archive_path)
                    extracted_paths.append(
                        {
                            "bundle_file": extracted_archive_path,
                            "sha256": extracted.sha256,
                            "size_bytes": extracted.size_bytes,
                            "file_type": extracted.file_type,
                        }
                    )
                photometry["bundle_file"] = archive_path
                photometry["bundle_status"] = "downloaded"
                photometry["download_source_url"] = asset.source_url
                photometry["sha256"] = asset.sha256
                photometry["downloaded_at"] = asset.downloaded_at.isoformat() if asset.downloaded_at else None
                photometry["extracted_files"] = extracted_paths
                downloaded.append({"luminaire_id": luminaire.luminaire_id, "bundle_file": archive_path})
            except Exception as error:  # Individual public files must not abort the whole handoff.
                photometry["bundle_file"] = None
                photometry["bundle_status"] = "unavailable"
                photometry["download_error"] = str(error)
                unavailable.append({"luminaire_id": luminaire.luminaire_id, "reason": str(error)})

        package["photometry_downloads"] = {
            "downloaded": downloaded,
            "unavailable": unavailable,
        }
        snapshot = package["input_snapshot"]
        photometry_hashes = {
            item["luminaire_id"]: item["photometry"].get("sha256")
            for item in package["candidates"]
            if item["photometry"].get("sha256")
        }
        package["photometry_sha256_by_luminaire"] = photometry_hashes
        package["input_snapshot"] = {
            **snapshot,
            "photometry_sha256_by_luminaire": photometry_hashes,
        }
        package["input_snapshot_sha256"] = hashlib.sha256(
            _canonical_json(package["input_snapshot"])
        ).hexdigest()
        package["handoff_id"] = handoff_id_for_snapshot(package["input_snapshot"])
        archive.writestr("dialux-task.json", json.dumps(package, ensure_ascii=False, indent=2))
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "project_id": state.project_id,
                    "project_revision": state.revision,
                    "handoff_id": package["handoff_id"],
                    "input_snapshot_sha256": package["input_snapshot_sha256"],
                    "photometry_downloads": package["photometry_downloads"],
                    "selected_luminaire_assets": [
                        {
                            "luminaire_id": candidate["luminaire_id"],
                            "article_name": candidate["article_name"],
                            "bundle_file": candidate["photometry"].get("bundle_file"),
                            "extracted_files": candidate["photometry"].get("extracted_files", []),
                            "sha256": candidate["photometry"].get("sha256"),
                        }
                        for candidate in package["candidates"]
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        archive.writestr(
            "README.txt",
            "This archive contains dialux-task.json, manifest.json and final selected luminaire assets.\n"
            "Source ZIP files are in photometry/; extracted IES/LDT/ULD files are in photometry/extracted/.\n"
            "Every local asset is listed with its source and SHA-256 in the manifest.\n",
        )
    return output.getvalue()


def build_design_report(state: ProjectState) -> str:
    """Render facts and evidence already in ProjectState; no LLM claims are added."""

    brief = state.brief
    lines = [
        f"# 照明设计报告草稿：{brief.project_name}",
        "",
        f"- 项目 ID：`{state.project_id}`",
        f"- 项目版本：`{state.revision}`",
        f"- 工作流状态：`{state.workflow_status}`",
        f"- 生成时间：{state.updated_at.isoformat()}",
        "",
        "## 已确认设计条件",
        "",
        "| 项目 | 值 |",
        "| --- | --- |",
    ]
    labels = {
        "space_type": "空间类型",
        "area_m2": "面积 (m²)",
        "room_height_m": "房间高度 (m)",
        "target_illuminance_lx": "目标照度 (lx)",
        "target_cct_k": "目标色温 (K)",
        "min_cri": "最低显色指数 (Ra)",
        "target_ugr": "目标 UGR",
        "max_lpd_w_m2": "功率密度上限 (W/m²)",
        "max_power_w": "单灯/方案功率上限 (W)",
        "mounting": "安装方式",
        "min_ip_rating": "最低 IP 等级",
    }
    for field, label in labels.items():
        if field == "mounting_height_m":
            continue
        value = getattr(brief, field, None)
        lines.append(f"| {label} | {_markdown_value(value)} |")

    lines.extend(["", "## Lighting regions and groups", "", "| Region | Group | Area (m2) | Mounting point height (m) | Target illuminance (lx) | Status |", "| --- | --- | ---: | ---: | ---: | --- |"])
    if not brief.lighting_groups:
        lines.append("| - | - | - | - | - | pending |")
    else:
        for group in brief.lighting_groups:
            lines.append(
                f"| {group.region_name} | {group.group_name} | {group.area_m2:g} | "
                f"{group.mounting_height_m:g} | {group.target_illuminance_lx:g} | "
                f"{'confirmed' if group.confirmed else 'pending'} |"
            )

    lines.extend(["", "## 规范与项目证据", ""])
    if not state.evidence:
        lines.append("未保存可引用证据；不得将本报告视作规范符合性结论。")
    else:
        for item in state.evidence:
            locator = f"，{item.locator}" if item.locator else ""
            lines.extend([f"### {item.source_name}{locator}", "", f"> {item.excerpt}", ""])

    lines.extend(["## 初步计算", ""])
    if not state.calculations:
        lines.append("尚未执行初步计算。")
    else:
        for index, result in enumerate(state.calculations, start=1):
            lines.extend(
                [
                    f"### 计算 {index}：流明法",
                    "",
                    f"- 所需光通量：{result.required_luminous_flux_lm:g} lm",
                    f"- 估算灯具数量：{result.luminaire_count}",
                    f"- 装机功率：{result.installed_power_w:g} W",
                    f"- 装机功率密度：{result.installed_power_density_w_m2:g} W/m²",
                    "- 局限：" + "；".join(result.limitations),
                    "",
                ]
            )

    lines.extend(["", "## 平面图", ""])
    if state.floor_plan is None:
        lines.append("尚未导入 CAD 平面图；面积与空间几何仅来自任务书。")
    else:
        plan = state.floor_plan
        lines.extend(
            [
                f"- 源文件：{plan.asset.source_name}（{plan.asset.source_type.upper()}）",
                f"- 图纸单位：{plan.drawing_units}",
                f"- 候选闭合边界：{len(plan.area_candidates)} 个",
                "- 说明：图纸提取结果须经用户确认后才会写入任务书并参与后续计算。",
            ]
        )
        if plan.warnings:
            lines.extend(f"- 注意：{warning}" for warning in plan.warnings)

    lines.extend(["", "## 灯具位置与一致性审查", ""])
    if state.layout_analysis is None:
        lines.append("尚未执行 DXF/PDF 灯具位置一致性审查。")
    else:
        analysis = state.layout_analysis
        lines.extend(
            [
                f"- CAD 来源：{analysis.cad_source_name}（SHA-256 `{analysis.cad_sha256}`）",
                f"- 报告来源：{analysis.report_source_name}（第 {analysis.report_page_count} 页，SHA-256 `{analysis.report_sha256}`）",
                f"- 数量：DXF {analysis.dxf_count}，PDF 位置明细 {analysis.report_count}",
                "- 照明类别：当前仅保存未分类结果；没有照明用途、回路或目标对象证据时不作通用/重点照明定论。",
            ]
        )
        if analysis.model_parameters:
            lines.extend(
                [
                    "",
                    "| 型号 | 品牌 | 产品编号 | 数量 | 功率 (W) | 灯具光通量 (lm) |",
                    "| --- | --- | --- | ---: | ---: | ---: |",
                ]
            )
            for model, parameters in analysis.model_parameters.items():
                lines.append(
                    f"| {model} | {_markdown_value(parameters.get('manufacturer'))} | "
                    f"{_markdown_value(parameters.get('product_code'))} | {_markdown_value(parameters.get('quantity'))} | "
                    f"{_markdown_value(parameters.get('power_w'))} | {_markdown_value(parameters.get('luminous_flux_lm'))} |"
                )
        if analysis.coordinate_transform is not None:
            transform = analysis.coordinate_transform
            lines.append(
                f"- 坐标变换：比例 {transform.scale:g}，旋转 {transform.rotation_deg:g}°，"
                f"平移 ({transform.translation_x_m:g}, {transform.translation_y_m:g}) m；"
                f"锚点 {transform.anchor_count}，最大残差 {_markdown_value(transform.max_residual_m)} m。"
            )
        lines.extend(
            [
                "",
                "| 编号 | 型号 | 类别 | X (m) | Y (m) | Z (m) | 匹配 | 来源 |",
                "| --- | --- | --- | ---: | ---: | ---: | --- | --- |",
            ]
        )
        for placement in analysis.placements:
            source = f"{placement.report_page} 页 / {len(placement.dxf_entity_handles)} 个 DXF 实体" if placement.report_page else f"{len(placement.dxf_entity_handles)} 个 DXF 实体"
            lines.append(
                f"| {placement.placement_id} | {_markdown_value(placement.model)} | {placement.category} | "
                f"{placement.x_m:g} | {placement.y_m:g} | {_markdown_value(placement.z_m)} | "
                f"{placement.matching_status} | {source} |"
            )
        if analysis.checks:
            lines.extend(["", "### 一致性检查", "", "| 指标 | 状态 | 观测值 | 阈值 | 说明 |", "| --- | --- | ---: | ---: | --- |"])
            for check in analysis.checks:
                lines.append(
                    f"| {check.metric} | {check.status} | {_markdown_value(check.observed)} | "
                    f"{_markdown_value(check.threshold)} | {check.explanation} |"
                )
        if analysis.issues:
            lines.extend(["", "### 待处理问题", ""])
            lines.extend(
                f"- `{issue.severity}` `{issue.kind}`：{issue.message} 建议：{issue.required_action}"
                for issue in analysis.issues
            )
        if analysis.warnings:
            lines.extend(["", "### 解析注意事项", ""])
            lines.extend(f"- {warning}" for warning in analysis.warnings)

    lines.extend(["## 规则校核", "", "| 指标 | 状态 | 观测值 | 阈值 | 说明 |", "| --- | --- | --- | --- | --- |"])
    if not state.rule_checks:
        lines.append("| — | insufficient_data | — | — | 尚未执行规则校核 |")
    else:
        for check in state.rule_checks:
            lines.append(
                f"| {check.metric} | {check.status} | {_markdown_value(check.observed)} | "
                f"{_markdown_value(check.threshold)} | {check.explanation} |"
            )

    lines.extend(["", "## 候选灯具", "", "| 型号 | 品牌 | 功率 | CCT | CRI | IP | 资料 | 状态 |", "| --- | --- | --- | --- | --- | --- | --- |"])
    if not state.luminaires:
        lines.append("| — | — | — | — | — | — | — | 尚未查询 |")
    else:
        for item in state.luminaires:
            files = []
            if item.has_uld:
                files.append("ULD")
            if item.has_photometry_download:
                files.append("配光")
            missing = f"；缺少 {', '.join(item.missing_requested_fields)}" if item.missing_requested_fields else ""
            lines.append(
                f"| {item.article_name} | {_markdown_value(item.brand_name)} | {_markdown_value(item.power_w)} | "
                f"{_markdown_value(item.cct_k)} | {_markdown_value(item.cri)} | {_markdown_value(item.ip_rating)} | "
                f"[{', '.join(files) or '详情'}]({item.detail_url}) | {item.matching_status}{missing} |"
            )

    lines.extend(["", "## 仿真结果", "", "| 状态 | 指标 | 验证 | 来源 |", "| --- | --- | --- | --- |"])
    if not state.simulation_runs:
        lines.append("| — | — | — | 尚未导入 DIALux 或等效仿真结果 |")
    else:
        for run in state.simulation_runs:
            metrics = run.metrics
            summary = "—"
            if metrics is not None:
                parts = []
                if metrics.maintained_illuminance_lx is not None:
                    parts.append(f"Ē={metrics.maintained_illuminance_lx:g} lx")
                if metrics.uniformity_u0 is not None:
                    parts.append(f"U₀={metrics.uniformity_u0:g}")
                if metrics.ugr is not None:
                    parts.append(f"UGR={metrics.ugr:g}")
                if metrics.installed_power_density_w_m2 is not None:
                    parts.append(f"LPD={metrics.installed_power_density_w_m2:g} W/m²")
                summary = "，".join(parts) if parts else "已导入"
            status = run.status
            if run.stale_reason:
                status = f"{status}（{run.stale_reason}）"
            lines.append(
                f"| {status} | {summary} | {run.verification_status} | {_markdown_value(run.source_kind)} |"
            )

    lines.extend(["", "## 待确认事项", ""])
    if state.open_questions:
        lines.extend(f"- {question}" for question in state.open_questions)
    else:
        lines.append("当前任务书的必填字段已填写；仍需确认布灯、反射比及仿真条件。")
    lines.extend(
        [
            "",
            "## 人工复核声明",
            "",
            "本报告为可审查草稿。Luminaire Finder 结果仅用于候选灯具筛选；维持照度、均匀度、UGR、布灯方式及最终规范符合性，须由有资质人员在 DIALux evo 或等效软件中复核并签发。仿真结果仅在其输入与当前项目版本匹配（matched）时方可视为本项目结论。",
            "",
        ]
    )
    return "\n".join(lines)


def _markdown_value(value: object | None) -> str:
    return "—" if value is None or value == "" else str(value)
