"""Project-scoped tools for CAD interpretation and DIALux execution."""
from __future__ import annotations

import base64
import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from .cad_auto_analysis import apply_model_analysis
from .dialux_api import candidate_summary
from .dialux_protocol import DialuxProtocolError, open_in_dialux
from .drawing_preview import render_drawing_preview
from .rag import public_locator
from .rules import calculation_mapping
from .schemas import DialuxRunRequest, LuminaireSearchRequest

LOGGER = logging.getLogger(__name__)


def _public_outstanding(model: Any) -> list[str]:
    """Convert validator details into owner-facing, ID-free messages.

    ``SpatialModel.outstanding`` deliberately keeps precise audit diagnostics,
    including model-generated identifiers.  That precision belongs in the
    review workspace, not in a chat tool payload that a language model may
    quote verbatim, so this boundary emits a small stable vocabulary instead.
    """
    public: list[str] = []
    for issue in model.outstanding:
        text = str(issue)
        if "置信度或证据不足" in text:
            text = "部分空间的图面依据或置信度不足"
        elif "模型没有判断空间" in text or "模型引用了不存在的空间" in text:
            text = "部分空间尚未完成图面判断"
        elif "模型没有判断构件" in text or "模型引用了不存在的构件" in text:
            text = "部分门窗、家具或遮挡物尚未完成图面判断"
        elif "重叠" in text and "房间" in text:
            text = "房间边界存在重叠，需修正或排除外轮廓候选"
        elif "边界待确认" in text or "覆盖范围" in text:
            text = "空间边界和覆盖范围待确认"
        elif "完整性待确认" in text or "构件" in text:
            text = "门窗、柱、家具及遮挡物完整性待确认"
        elif text.startswith(("图纸尺度", "CAD 文件读取不完整", "没有待设计房间")):
            # These messages are already written for owners and do not contain
            # a model-generated identifier.
            pass
        else:
            text = "图纸识别仍有待确认事项"
        if text not in public:
            public.append(text)
    return public


def _public_calculation_mapping(ruleset: Any, model: Any) -> dict[str, Any]:
    """Keep rule evidence while removing model-only room/rule identifiers."""
    mapping = calculation_mapping(ruleset, model)
    for room in mapping.get("rooms", []):
        room.pop("room_id", None)
        for evaluation in room.get("evaluations", []):
            evaluation.pop("rule_ids", None)
        for requirement in room.get("requirements", []):
            requirement.pop("rule_id", None)
    return mapping


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content)


def _context(plan) -> dict[str, Any]:
    return {
        "drawing_units": plan.drawing_units,
        "meters_per_unit": plan.meters_per_drawing_unit,
        "candidates": [
            {"candidate_id": c.candidate_id, "name": c.inferred_name,
             "area_m2": c.area_m2, "boundary_points": len(c.boundary)}
            for c in plan.area_candidates
        ],
        "elements": [
            {"element_id": e.element_id, "kind": e.kind, "name": e.name, "room_id": e.room_id}
            for e in (plan.spatial_model.elements if plan.spatial_model else [])
        ],
        "labels": [label.text for label in plan.drawing_labels if label.text.strip()][:160],
    }


def _messages(plan, preview: bytes) -> list[Any]:
    schema = {"rooms": [{"candidate_id": "existing id", "action": "include|exclude",
                         "name": "optional", "usage": "optional", "floor": "optional",
                         "number": "optional", "confidence": 0.0, "evidence": [], "rationale": ""}],
              "elements": [{"element_id": "existing id", "action": "include|exclude",
                             "confidence": 0.0, "evidence": [], "rationale": ""}]}
    prompt = ("你是 CAD 照明空间审核模型。只能引用输入已有的 candidate_id 和 element_id，不能创建几何或事实。"
              "自动决定照明空间，所有决定给出证据和 0 到 1 置信度。只返回 JSON：" +
              json.dumps(schema, ensure_ascii=False))
    return [SystemMessage(content=prompt), HumanMessage(content=[
        {"type": "text", "text": json.dumps(_context(plan), ensure_ascii=False)},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(preview).decode("ascii")}},
    ])]


def make_tools(*, projects: Any, evidence: Any, dialux: Any, project_id: str | None,
               vision_model: Any | None = None, dialux_executor: Any | None = None,
               dialux_handoff: Any | None = None):
    @tool
    def get_project() -> dict:
        """Read project state and automatic CAD decisions."""
        if not project_id:
            return {"status": "no_active_project"}
        state = projects.get(project_id)
        plan = state.floor_plan
        model = plan.spatial_model if plan else None
        return {"project_name": state.brief.project_name, "space_type": state.brief.space_type,
                "cad": None if not plan else {"file": plan.asset.source_name, "units": plan.drawing_units,
                    "read_complete": plan.read_complete, "model_version": model.version if model else None,
                    "automation_status": model.automation_status if model else "pending",
                    "rooms": [{"floor": r.floor, "number": r.number, "name": r.name, "usage": r.usage,
                               "area_m2": r.area_m2, "status": r.status}
                              for r in model.rooms] if model else [],
                    "outstanding": _public_outstanding(model) if model else ["CAD 尚未自动分析"]},
                "rules_and_settings": _public_calculation_mapping(state.rule_set, model),
                "invalidated_dependencies": state.invalidated_dependencies,
                "saved_luminaires": [{"id": x.luminaire_id, "brand": x.brand_name, "name": x.article_name}
                                     for x in state.luminaires[-12:]]}

    @tool
    def analyze_floor_plan() -> dict:
        """Run, validate, and persist multimodal CAD decisions."""
        if not project_id:
            return {"status": "no_active_project"}
        state = projects.get(project_id)
        plan = state.floor_plan
        if plan is None:
            return {"status": "no_drawing"}
        if vision_model is None:
            return {"status": "vision_unavailable", "message": "未配置 CAD 视觉模型；无法自动判断空间"}
        try:
            response = vision_model.invoke(_messages(plan, render_drawing_preview(plan)))
            report = json.loads(_text(response.content)[:20000].removeprefix("```json").removesuffix("```").strip())
            analyzed = apply_model_analysis(plan, report, model_name=getattr(vision_model, "model", None))
            current = projects.get(project_id)
            updated = projects.set_floor_plan(project_id, current.revision,
                                              plan.model_copy(update={"spatial_model": analyzed}), None)
            model = updated.floor_plan.spatial_model
            dialux_result = None
            if model.automation_status == "auto_confirmed" and dialux_executor is not None:
                run = dialux_executor.auto_start(project_id)
                dialux_result = {"status": run.status}
                if run.error:
                    dialux_result["error"] = run.error
            included_rooms = [r for r in model.rooms if r.status == "confirmed"]
            excluded_rooms = [r for r in model.rooms if r.status == "excluded"]
            return {"status": "vision_analyzed" if model.automation_status == "auto_confirmed" else model.automation_status,
                    "automation_status": model.automation_status,
                    "analysis": {
                        "automation_status": model.automation_status,
                        "room_count": len(model.rooms),
                        "included_room_count": len(included_rooms),
                        "excluded_room_count": len(excluded_rooms),
                    },
                    "outstanding": _public_outstanding(model), "dialux": dialux_result}
        except Exception:
            LOGGER.warning("Automatic CAD analysis failed", exc_info=True)
            return {"status": "needs_attention", "message": "图纸识别未完整完成，请查看图纸工作区中的检查提示。"}

    @tool
    def search_evidence(query: str) -> dict:
        """Search standards and project documents."""
        rows = []
        for item in evidence.search(query, top_k=5, project_id=project_id):
            row = {"source": item.source_name, "type": item.source_type, "excerpt": item.excerpt}
            locator = public_locator(item.locator)
            if locator:
                row["locator"] = locator
            rows.append(row)
        return {"evidence": rows}

    @tool
    def search_luminaires(keyword: str, brand: str | None = None,
                          target_cct_k: int | None = None, min_cri: int | None = None) -> dict:
        """Search and persist DIALux catalogue luminaires."""
        request = LuminaireSearchRequest(keyword=keyword, brand=brand, target_cct_k=target_cct_k,
                                         min_cri=min_cri, max_results=5)
        if not project_id:
            return {"candidates": [candidate_summary(x) for x in dialux.search(request)]}
        state = projects.get(project_id)
        result = dialux.search_with_run(request, project_id=project_id, project_revision=state.revision)
        updated, _, _ = projects.append_luminaires(project_id, state.revision, result.candidates, result.search_run)
        saved = {x.luminaire_id: x for x in updated.luminaires}
        return {"candidates": [candidate_summary(saved[x.luminaire_id]) for x in result.candidates],
                "warnings": result.search_run.warnings, "project_revision": updated.revision}

    tools = [get_project, analyze_floor_plan, search_evidence, search_luminaires]
    if project_id:
        @tool
        def send_luminaire_to_dialux(luminaire_id: str) -> dict:
            """Open a saved luminaire in local DIALux."""
            state = projects.get(project_id)
            candidate = next((x for x in state.luminaires if x.luminaire_id == luminaire_id), None)
            if candidate is None:
                return {"status": "not_found", "luminaire_id": luminaire_id}
            try:
                (dialux_handoff or open_in_dialux)(dialux.resolve_send_to_dialux_url(candidate.detail_url))
            except DialuxProtocolError as error:
                return {"status": "failed", "luminaire_id": luminaire_id, "error": error.as_dict()}
            except Exception as error:
                return {"status": "failed", "luminaire_id": luminaire_id, "error": {"message": str(error)}}
            return {"status": "launched", "luminaire_id": luminaire_id, "article_name": candidate.article_name}
        tools.append(send_luminaire_to_dialux)
    if project_id and dialux_executor is not None:
        @tool
        def prepare_dialux_run(photometry_path: str | None = None, luminaire_id: str | None = None,
                               confirm_assumptions: bool = False, assumptions: dict[str, Any] | None = None,
                               layout: list[dict[str, Any]] | None = None,
                               optimization: dict[str, Any] | None = None) -> dict:
            """Prepare and automatically start a real DIALux run."""
            state = projects.get(project_id)
            request = DialuxRunRequest.model_validate({"expected_revision": state.revision,
                "photometry_path": photometry_path, "luminaire_id": luminaire_id,
                "confirm_assumptions": confirm_assumptions, "assumptions": assumptions or {},
                "layout": layout or [], "optimization": optimization or {}})
            return dialux_executor.prepare(project_id, request).model_dump(mode="json")

        @tool
        def get_dialux_run(run_id: str) -> dict:
            """Read a DIALux run and verified result."""
            return dialux_executor.get(project_id, run_id).model_dump(mode="json")

        @tool
        def start_dialux_run(run_id: str) -> dict:
            """Resume a prepared DIALux run."""
            return dialux_executor.start(project_id, run_id).model_dump(mode="json")
        tools.extend([prepare_dialux_run, get_dialux_run, start_dialux_run])
    return tools
