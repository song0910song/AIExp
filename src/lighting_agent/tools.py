"""Project-scoped tools for the lighting knowledge assistant."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from .dialux_api import candidate_summary
from .rag import public_locator
from .schemas import LuminaireSearchRequest
from .rules import calculation_mapping


def make_tools(*, projects: Any, evidence: Any, dialux: Any, project_id: str | None):
    @tool
    def get_project() -> dict:
        """Read the active project's name, CAD summary and saved luminaire names."""

        if not project_id:
            return {"status": "no_active_project"}
        state = projects.get(project_id)
        plan = state.floor_plan
        return {
            "project_name": state.brief.project_name,
            "space_type": state.brief.space_type,
            "cad": {
                "file": plan.asset.source_name,
                "units": plan.drawing_units,
                "room_name": plan.room_name,
                "areas_m2": [item.area_m2 for item in plan.area_candidates[:12]],
                "selected_area_candidate_index": plan.selected_area_candidate_index,
                "warnings": plan.warnings,
                "read_complete": plan.read_complete,
                "model_version": plan.spatial_model.version if plan.spatial_model else None,
                "design_ready": plan.spatial_model.design_ready if plan.spatial_model else False,
                "rooms": [r.model_dump(mode="json", exclude={"boundary", "holes"}) for r in plan.spatial_model.rooms] if plan.spatial_model else [],
                "outstanding": plan.spatial_model.outstanding if plan.spatial_model else ["旧图纸需重新导入"],
            } if plan else None,
            "rules_and_settings": calculation_mapping(state.rule_set, plan.spatial_model if plan else None),
            "invalidated_dependencies": state.invalidated_dependencies,
            "saved_luminaires": [
                {"id": item.luminaire_id, "brand": item.brand_name, "name": item.article_name}
                for item in state.luminaires[-12:]
            ],
        }

    @tool
    def search_evidence(query: str) -> dict:
        """Search indexed standards and project documents; return exact excerpts and meaningful source locations."""

        results = evidence.search(query, top_k=5, project_id=project_id)
        evidence_items = []
        for item in results:
            entry = {
                "source": item.source_name,
                "type": item.source_type,
                "excerpt": item.excerpt,
            }
            location = public_locator(item.locator)
            if location:
                entry["locator"] = location
            evidence_items.append(entry)
        return {"evidence": evidence_items}

    @tool
    def search_luminaires(
        keyword: str,
        brand: str | None = None,
        target_cct_k: int | None = None,
        min_cri: int | None = None,
    ) -> dict:
        """Search DIALux Luminaire Finder; results are product facts, not design calculations."""

        request = LuminaireSearchRequest(
            keyword=keyword, brand=brand, target_cct_k=target_cct_k,
            min_cri=min_cri, max_results=5,
        )
        if not project_id:
            return {"candidates": [candidate_summary(item) for item in dialux.search(request)]}
        state = projects.get(project_id)
        result = dialux.search_with_run(request, project_id=project_id, project_revision=state.revision)
        updated, _, _ = projects.append_luminaires(
            project_id, state.revision, result.candidates, result.search_run
        )
        saved = {item.luminaire_id: item for item in updated.luminaires}
        return {
            "candidates": [
                candidate_summary(saved[item.luminaire_id])
                for item in result.candidates if item.luminaire_id in saved
            ],
            "warnings": result.search_run.warnings,
            "project_revision": updated.revision,
            "notice": "候选已保存在当前项目；用户可在会话中主动发送灯具到本机 DIALux。",
        }

    return [get_project, search_evidence, search_luminaires]
