"""Human review API for goals 1/2; no DIALux execution endpoints."""
from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from fastapi.responses import FileResponse
from pydantic import Field

from .project_store import ProjectStore, RevisionConflictError
from .rules import calculation_mapping, evidence_items, generate_candidates, rule_conflicts, validate_rule
from .schemas import (DesignRule, FieldProvenance, ProjectUpdate, RuleSet, SpatialElement,
                      SpatialRoom, StandardRecord, StrictModel)
from .spatial_model import assess_model


class ModelReviewRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    meters_per_unit: float = Field(gt=0, allow_inf_nan=False)
    rooms: list[SpatialRoom]
    elements: list[SpatialElement]
    coverage_confirmed: bool = False
    elements_reviewed: bool = False
    note: str = Field(min_length=1, max_length=2000)


class StandardRegistration(StrictModel):
    source_hash: str
    project_id: str | None = None
    number: str = Field(min_length=1, max_length=160)
    edition: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=300)
    effective_date: date
    scope: str = Field(min_length=1, max_length=2000)
    kind: Literal["official", "corporate", "owner"]
    source: str = Field(min_length=1, max_length=2000)
    source_verified: bool = False


class CandidateRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    standard_id: str


class ManualRuleRequest(CandidateRequest):
    evidence_key: str
    metric: Literal["illuminance", "uniformity", "ugr", "cri", "cct", "lpd"]


class RuleEditRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    rule: DesignRule


class RuleConfirmRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    reviewer: str = Field(min_length=1, max_length=160)
    note: str = Field(min_length=1, max_length=2000)


class RuleBindRequest(RuleConfirmRequest):
    coverage_confirmed: bool = False


def install_review_routes(app, projects, evidence, project_root, project_view):
    def state_for(project_id, revision=None):
        state = projects.get(project_id)
        if revision is not None and state.revision != revision:
            raise RevisionConflictError("项目已更新，请刷新后重试")
        return state

    def records_for(project_id=None):
        records = evidence.list_standards()
        if project_id:
            state_for(project_id)
            records += evidence.list_standards(project_id=project_id)
        return list({r.standard_id: r for r in records}.values())

    def standard_for(standard_id, project_id=None):
        record = next((r for r in records_for(project_id) if r.standard_id == standard_id), None)
        if record is None:
            raise HTTPException(404, "标准版本不在当前可访问范围")
        artifact = evidence.get_document_artifact(record.source_hash, project_id=record.project_id)
        if not artifact:
            raise HTTPException(422, "原始证据未归档，请重新导入文件")
        path = Path(artifact["source_path"])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != record.file_sha256:
            raise HTTPException(409, "标准原文件缺失或哈希改变，需恢复原件或登记新版本")
        return record, artifact

    def store_rules(state, rules, *, changed=True):
        if changed:
            rules.version += 1
            rules.status = "draft"
            rules.invalidation_reason = "规则已修改；需重新确认并绑定版本"
            rules.coverage_confirmed = False
        updated = projects.update(state.project_id, ProjectUpdate(expected_revision=state.revision, rule_set=rules,
            invalidated_dependencies=["规则版本/绑定已变化；历史计算结果不代表当前规则"]))
        return project_view(updated)

    @app.get("/api/projects/{project_id}/floor-plan/source")
    def cad_source(project_id: str):
        state = state_for(project_id)
        if not state.floor_plan:
            raise HTTPException(404, "尚未上传 CAD")
        root = project_root(project_id).resolve()
        path = (root / state.floor_plan.asset.storage_path).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(404, "原始 CAD 文件不可用")
        return FileResponse(path, filename=state.floor_plan.asset.source_name)

    @app.put("/api/projects/{project_id}/spatial-model")
    def review_model(project_id: str, request: ModelReviewRequest):
        state = state_for(project_id, request.expected_revision)
        if not state.floor_plan or not state.floor_plan.spatial_model:
            raise HTTPException(422, "请重新导入 CAD 以建立多房间空间模型")
        plan = state.floor_plan.model_copy(deep=True)
        previous = plan.spatial_model
        model = previous.model_copy(deep=True)
        # A candidate must remain visible until explicitly excluded.
        expected = {r.room_id for r in previous.rooms}
        if not expected.issubset({r.room_id for r in request.rooms}):
            raise HTTPException(422, "不能静默删除房间；请保留并填写排除原因")
        if not {e.element_id for e in previous.elements}.issubset({e.element_id for e in request.elements}):
            raise HTTPException(422, "不能静默删除构件；请显式标记排除")
        model.rooms = [r.model_copy(deep=True) for r in request.rooms]
        model.elements = [e.model_copy(deep=True) for e in request.elements]
        model.meters_per_unit = request.meters_per_unit
        model.geometry_tolerance_m = (previous.geometry_tolerance_m / previous.meters_per_unit
            if previous.geometry_tolerance_m and previous.meters_per_unit else 0.001) * model.meters_per_unit
        model.coverage_confirmed = request.coverage_confirmed
        model.elements_reviewed = request.elements_reviewed
        for collection, old, id_field in ((model.rooms, previous.rooms, "room_id"), (model.elements, previous.elements, "element_id")):
            old_by_id = {getattr(item, id_field): item for item in old}
            for item in collection:
                original = old_by_id.get(getattr(item, id_field))
                item.provenance = original.provenance.copy() if original else {}
                for field, value in item.model_dump(exclude={"provenance", "area_m2", "position", "length_m", "width_m"}).items():
                    if original is None or getattr(original, field) != getattr(item, field):
                        item.provenance[field] = FieldProvenance(
                            source="user_correction" if field in {"boundary", "holes", "footprint"} else "user_assumption",
                            locator=f"人工核对 / 模型 v{previous.version + 1}", note=request.note,
                            confirmed=item.status == "confirmed")
                    elif item.status == "confirmed" and field in item.provenance:
                        item.provenance[field] = item.provenance[field].model_copy(update={"confirmed": True})
        model.version += 1
        model.audit_log.append(f"{datetime.now(UTC).isoformat()} / v{model.version}: {request.note}; 尺度 {previous.meters_per_unit} → {model.meters_per_unit}")
        try:
            assess_model(model, plan)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        plan.spatial_model = model
        plan.meters_per_drawing_unit = model.meters_per_unit
        for candidate in plan.area_candidates:
            candidate.area_m2 = candidate.raw_area * model.meters_per_unit**2
            xs, ys = [p.x for p in candidate.boundary], [p.y for p in candidate.boundary]
            candidate.length_m = (max(xs) - min(xs)) * model.meters_per_unit
            candidate.width_m = (max(ys) - min(ys)) * model.meters_per_unit
        # Legacy single-room measurements are no longer a source of truth after editing.
        brief = state.brief.model_copy(deep=True)
        for field in ("area_m2", "length_m", "width_m"):
            setattr(brief, field, None)
            brief.confirmed_fields.discard(field)
        plan.selected_area_candidate_index = None
        return project_view(projects.update(project_id, ProjectUpdate(expected_revision=request.expected_revision,
            floor_plan=plan, brief=brief, **ProjectStore._invalidate(state, "空间模型/尺度已变更；适用规则及关联结果需复核"))))

    @app.get("/api/projects/{project_id}/spatial-model/export")
    def export_model(project_id: str):
        plan = state_for(project_id).floor_plan
        if not plan or not plan.spatial_model:
            raise HTTPException(404, "尚未建立空间模型")
        return {"spatial_model": plan.spatial_model.model_dump(mode="json"),
                "source_asset": plan.asset.model_dump(mode="json"),
                "source_geometry": [c.model_dump(mode="json", exclude={"points"}) for c in plan.area_candidates],
                "ifc_generated": False}

    @app.get("/api/standards")
    def list_standards(project_id: str | None = None):
        return [r.model_dump(mode="json") for r in records_for(project_id)]

    @app.post("/api/standards", status_code=201)
    def register_standard(request: StandardRegistration):
        if request.project_id:
            state_for(request.project_id)
        artifact = evidence.get_document_artifact(request.source_hash, project_id=request.project_id)
        if not artifact:
            raise HTTPException(422, "请先上传标准全文；搜索摘要不可登记为条文证据")
        record = StandardRecord(**request.model_dump(), file_sha256=artifact["source_sha256"])
        try:
            return evidence.register_standard(record).model_dump(mode="json")
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/api/standards/{standard_id}/evidence")
    def standard_evidence(standard_id: str, project_id: str | None = None):
        record, artifact = standard_for(standard_id, project_id)
        return {"standard": record.model_dump(mode="json"), "items": list(evidence_items(artifact)),
                "extraction_complete": artifact["extraction_complete"], "review_required": artifact["review_required"]}

    @app.get("/api/standards/{standard_id}/source")
    def standard_source(standard_id: str, project_id: str | None = None):
        record, artifact = standard_for(standard_id, project_id)
        path = Path(artifact["source_path"])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != record.file_sha256:
            raise HTTPException(409, "原始文件不存在或哈希已变化；不能当作登记版本使用")
        return FileResponse(path, content_disposition_type="inline")

    @app.post("/api/projects/{project_id}/rules/candidates")
    def add_candidates(project_id: str, request: CandidateRequest):
        state = state_for(project_id, request.expected_revision)
        record, artifact = standard_for(request.standard_id, project_id)
        rules = state.rule_set.model_copy(deep=True) if state.rule_set else RuleSet()
        if record.standard_id not in {s.standard_id for s in rules.standards}:
            rules.standards.append(record)
        known = {r.rule_id for r in rules.rules}
        generated = generate_candidates(record, artifact)
        rules.rules += [r for r in generated if r.rule_id not in known]
        return {"project": store_rules(state, rules), "generated": len([r for r in generated if r.rule_id not in known]),
                "notice": "候选仅供人工核对；未识别条款可从原文手动添加规则"}

    @app.post("/api/projects/{project_id}/rules/manual")
    def manual_rule(project_id: str, request: ManualRuleRequest):
        state = state_for(project_id, request.expected_revision)
        record, artifact = standard_for(request.standard_id, project_id)
        item = next((i for i in evidence_items(artifact) if i["key"] == request.evidence_key), None)
        if not item:
            raise HTTPException(422, "证据位置不存在")
        rules = state.rule_set.model_copy(deep=True) if state.rule_set else RuleSet()
        if record.standard_id not in {s.standard_id for s in rules.standards}:
            rules.standards.append(record)
        rules.rules.append(DesignRule(standard_id=record.standard_id, metric=request.metric,
            evidence_key=item["key"], evidence_text=item["text"], locator=item["locator"], page_number=item["page"]))
        return store_rules(state, rules)

    @app.put("/api/projects/{project_id}/rules/{rule_id}")
    def edit_rule(project_id: str, rule_id: str, request: RuleEditRequest):
        state = state_for(project_id, request.expected_revision)
        rules = state.rule_set.model_copy(deep=True) if state.rule_set else RuleSet()
        index = next((i for i, r in enumerate(rules.rules) if r.rule_id == rule_id), None)
        if index is None or request.rule.rule_id != rule_id:
            raise HTTPException(404, "规则不存在")
        old = rules.rules[index]
        rule = request.rule.model_copy(deep=True)
        rule.condition_provenance = old.condition_provenance.copy()
        for key, value in rule.conditions.model_dump().items():
            if value != getattr(old.conditions, key) and value not in (None, ""):
                rule.condition_provenance[key] = FieldProvenance(source="user_assumption", locator="设计人员输入的计算条件", note=rule.review_note)
        if any(getattr(rule, field) != getattr(old, field) for field in ("standard_id", "evidence_key", "evidence_text", "locator", "page_number")):
            raise HTTPException(422, "不能修改证据身份或原文；请从新证据添加候选")
        if rule.status == "rejected" and not rule.review_note:
            raise HTTPException(422, "请填写不适用/排除原因")
        rule.status = "rejected" if rule.status == "rejected" else "candidate"
        rule.reviewer = None
        rule.reviewed_at = None
        rules.rules[index] = rule
        return store_rules(state, rules)

    @app.post("/api/projects/{project_id}/rules/{rule_id}/confirm")
    def confirm_rule(project_id: str, rule_id: str, request: RuleConfirmRequest):
        state = state_for(project_id, request.expected_revision)
        rules = state.rule_set.model_copy(deep=True) if state.rule_set else RuleSet()
        rule = next((r for r in rules.rules if r.rule_id == rule_id), None)
        if not rule or rule.status == "rejected":
            raise HTTPException(422, "规则不存在或已排除")
        record, artifact = standard_for(rule.standard_id, project_id)
        problems = validate_rule(rule, record, artifact)
        if problems:
            raise HTTPException(422, "；".join(problems))
        rule.status = "confirmed"
        rule.reviewer = request.reviewer
        rule.review_note = request.note
        rule.reviewed_at = datetime.now(UTC)
        for key, provenance in rule.condition_provenance.items():
            rule.condition_provenance[key] = provenance.model_copy(update={"confirmed": True, "note": request.note})
        conflicts = rule_conflicts(rules.rules)
        if conflicts:
            raise HTTPException(422, "；".join(conflicts))
        return store_rules(state, rules)

    @app.post("/api/projects/{project_id}/rules/bind")
    def bind_rules(project_id: str, request: RuleBindRequest):
        state = state_for(project_id, request.expected_revision)
        rules = state.rule_set.model_copy(deep=True) if state.rule_set else RuleSet()
        model = state.floor_plan.spatial_model if state.floor_plan else None
        if not model:
            raise HTTPException(422, "请先导入并核对空间模型")
        if not request.coverage_confirmed:
            raise HTTPException(422, "请确认已核对全部适用条款、表格和脚注，没有遗漏要求")
        if any(r.status == "candidate" for r in rules.rules) or not any(r.status == "confirmed" for r in rules.rules):
            raise HTTPException(422, "所有候选须确认或显式排除，且至少保留一条已确认规则")
        for rule in rules.rules:
            if rule.status == "confirmed":
                record, artifact = standard_for(rule.standard_id, project_id)
                if not artifact["extraction_complete"]:
                    raise HTTPException(422, "标准含未完成抽取页面；请重新识别或补充有定位的完整正文，不能宣称规则齐备")
                problems = validate_rule(rule, record, artifact)
                if problems:
                    raise HTTPException(422, "；".join(problems))
        rules.status = "bound"
        rules.bound_version = rules.version
        rules.bound_model_version = model.version
        rules.bound_source_sha256 = model.source_sha256
        rules.bound_by = request.reviewer
        rules.bound_at = datetime.now(UTC)
        rules.invalidation_reason = None
        rules.coverage_confirmed = True
        rules.binding_note = request.note
        mapping = calculation_mapping(rules, model)
        if mapping["issues"]:
            raise HTTPException(422, "；".join(mapping["issues"]))
        return store_rules(state, rules, changed=False)

    @app.get("/api/projects/{project_id}/design-settings")
    def design_settings(project_id: str):
        state = state_for(project_id)
        return calculation_mapping(state.rule_set, state.floor_plan.spatial_model if state.floor_plan else None)
