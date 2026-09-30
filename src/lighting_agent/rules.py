"""Evidence-backed rule candidates, explicit review, conflict checks and settings.

This module prepares DIALux inputs; it never runs or claims a lighting simulation.
No standard thresholds are bundled in code or taken from search snippets.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date

from .schemas import DesignRule, RuleSet, StandardRecord

METRICS = {
    "illuminance": (r"照度|illuminance", "lx"),
    "uniformity": (r"均匀度|uniformity|U[₀0_]", "1"),
    "ugr": (r"UGR|统一眩光|眩光值", "1"),
    "cri": (r"显色|\bRa\b|CRI", "1"),
    "cct": (r"色温|CCT", "K"),
    "lpd": (r"功率密度|LPD", "W/m²"),
}


def evidence_items(artifact: dict):
    """An immutable locator/key for a block or an exact table row."""
    for page in artifact.get("pages", []):
        for block in page.get("blocks", []):
            text = block["text"]
            yield {"key": block["block_id"], "text": text, "locator": f"{page['locator']} / {block['block_id']}",
                   "page": page.get("page_number"), "status": page["status"], "headers": None, "cells": None}
        for table in page.get("tables", []):
            for index, row in enumerate(table["cells"][1:], 2):
                header = table["cells"][0]
                text = " | ".join(str(v or "") for v in header) + "\n" + " | ".join(str(v or "") for v in row)
                yield {"key": f"{table['table_id']}:row-{index}", "text": text,
                       "locator": f"{page['locator']} / 表 {table['table_id']} / 行 {index}",
                       "page": page.get("page_number"), "status": page["status"], "headers": header, "cells": row}


def generate_candidates(standard: StandardRecord, artifact: dict) -> list[DesignRule]:
    candidates = []
    for item in evidence_items(artifact):
        if item["status"] in {"empty", "needs_review"}:
            continue
        for metric, (pattern, unit) in METRICS.items():
            context = item["text"]
            value_text = context
            applies_to = []
            if item["headers"]:
                columns = [i for i, h in enumerate(item["headers"]) if re.search(pattern, str(h), re.I)]
                if len(columns) != 1 or columns[0] >= len(item["cells"]):
                    continue
                value_text = str(item["cells"][columns[0]] or "")
                applies_to = [str(item["cells"][0]).strip()] if columns[0] and item["cells"][0] else []
            else:
                match = re.search(pattern, context, re.I)
                if not match or "|" in context:
                    continue
                value_text = context[match.end():].split("\n")[0][:120]
            comparison = re.search(r"(>=|≥|不低于|至少|不小于|<=|≤|不大于|不超过|至多|=|为)\s*(\d+(?:\.\d+)?)", value_text)
            threshold = None
            operator = None
            if comparison:
                op, value = comparison.groups()
                operator = ">=" if op in {">=", "≥", "不低于", "至少", "不小于"} else "<=" if op in {"<=", "≤", "不大于", "不超过", "至多"} else "="
                threshold = float(value)
            elif item["headers"] and re.fullmatch(r"\s*\d+(?:\.\d+)?\s*(?:lx|K|W/m[²2])?\s*", value_text):
                threshold = float(re.search(r"\d+(?:\.\d+)?", value_text).group())
                # A bare table number is NOT enough to infer a comparison operator.
            candidates.append(DesignRule(
                rule_id=hashlib.sha256(f"{standard.standard_id}:{item['key']}:{metric}".encode()).hexdigest()[:24],
                standard_id=standard.standard_id, evidence_key=item["key"], evidence_text=context,
                locator=item["locator"], page_number=item["page"], metric=metric, operator=operator, threshold=threshold,
                unit=unit if unit == "1" or re.search(re.escape(unit).replace("²", "[²2]"), context, re.I) else None,
                applies_to=applies_to, review_note="自动候选：适用性、表头/脚注、比较方式与计算条件均需人工确认",
            ))
    return candidates


def validate_rule(rule: DesignRule, standard: StandardRecord, artifact: dict) -> list[str]:
    problems = []
    source = next((item for item in evidence_items(artifact) if item["key"] == rule.evidence_key), None)
    if not source or (source["text"], source["locator"], source["page"]) != (rule.evidence_text, rule.locator, rule.page_number):
        problems.append("证据文本/位置与原文件不一致；请重新生成或选择原文证据")
    elif source["status"] in {"needs_review", "empty"}:
        problems.append("来源页抽取未完成，不能确认规则")
    if artifact.get("source_sha256") != standard.file_sha256:
        problems.append("文件哈希与登记版本不一致")
    if not standard.source_verified:
        problems.append("发布机构/授权来源尚未核实")
    if standard.effective_date > date.today():
        problems.append("标准尚未实施；不能直接绑定为当前适用规则")
    if not rule.applies_to or any(not x.strip() for x in rule.applies_to):
        problems.append("适用空间用途待填写")
    if not rule.evaluation_scope:
        problems.append("评价场景/口径待确认（例如一般照明、应急照明；不同场景不得混用）")
    if rule.operator is None or rule.threshold is None:
        problems.append("比较方式和阈值待填写")
    # Require literal numeric support in the pinned source. A designer can add
    # a separately sourced owner requirement, but cannot silently change a code.
    numbers = {float(v) for v in re.findall(r"(?<![\d.])\d+(?:\.\d+)?(?![\d.])", rule.evidence_text)}
    for value in (rule.threshold, rule.upper_threshold if rule.operator == "range" else None):
        if value is not None and value not in numbers:
            problems.append("阈值在所选原文中没有数值依据；请选择正确条款或另行登记有依据的业主要求")
    literal = [r for r in generate_candidates(standard, artifact)
               if r.evidence_key == rule.evidence_key and r.metric == rule.metric and r.operator is not None and r.threshold is not None]
    if len(literal) == 1 and (literal[0].operator, literal[0].threshold) != (rule.operator, rule.threshold):
        problems.append("比较方式或阈值与原文的明确要求不一致；设计假设须单独登记，不可改写标准条文")
    if rule.unit != METRICS[rule.metric][1]:
        problems.append(f"指标单位应为 {METRICS[rule.metric][1]}")
    if rule.operator == "range" and (rule.upper_threshold is None or rule.threshold is None or rule.upper_threshold < rule.threshold):
        problems.append("区间上限缺失或小于下限")
    if rule.metric == "uniformity" and any(v is not None and v > 1 for v in (rule.threshold, rule.upper_threshold)):
        problems.append("均匀度应在 0–1 范围内")
    if rule.metric == "cri" and any(v is not None and v > 100 for v in (rule.threshold, rule.upper_threshold)):
        problems.append("显色指数不得大于 100")
    c = rule.conditions
    if rule.metric in {"illuminance", "uniformity"}:
        for field in ("plane", "workplane_height_m", "grid_x_m", "grid_y_m", "maintenance_factor"):
            if getattr(c, field) in (None, ""):
                problems.append(f"计算条件 {field} 待确认")
    if rule.metric == "ugr" and (not c.glare_method or not c.glare_observers):
        problems.append("UGR 评价方法与观察对象/方向待确认")
    return problems


def rule_conflicts(rules: list[DesignRule]) -> list[str]:
    conflicts = []
    active = [r for r in rules if r.status == "confirmed"]
    for i, a in enumerate(active):
        for b in active[i + 1:]:
            if not set(a.applies_to).intersection(b.applies_to) or a.evaluation_scope != b.evaluation_scope:
                continue
            ac, bc = a.conditions, b.conditions
            common = {key for key, value in ac.model_dump().items() if value not in (None, "")} & {key for key, value in bc.model_dump().items() if value not in (None, "")}
            if evaluation_target(a) != evaluation_target(b):
                # UGR observer conditions are not an illuminance working plane.
                common &= {"maintenance_factor"}
            if any(getattr(ac, field) != getattr(bc, field) for field in common):
                conflicts.append(f"{a.rule_id} 与 {b.rule_id}：相同用途的评价条件不一致，需分清适用范围，不能合并")
            elif a.metric == b.metric and (a.operator, a.threshold, a.upper_threshold, a.unit) != (b.operator, b.threshold, b.upper_threshold, b.unit):
                conflicts.append(f"{a.rule_id} 与 {b.rule_id}：同用途指标阈值不同，需人工选择适用规则，不能机械取最严值")
    return conflicts


def evaluation_target(rule: DesignRule) -> str:
    return "workplane" if rule.metric in {"illuminance", "uniformity"} else "glare" if rule.metric == "ugr" else "product" if rule.metric in {"cri", "cct"} else "power_density"


def calculation_mapping(ruleset: RuleSet | None, model) -> dict:
    issues = []
    rooms = []
    if ruleset is None or ruleset.status != "bound" or ruleset.bound_version != ruleset.version:
        issues.append("尚未绑定明确的已确认规则版本")
    if ruleset and not ruleset.coverage_confirmed:
        issues.append("适用条款及附加条件的完整性尚未经设计人员核对")
    if model is None or not model.design_ready:
        issues.append("空间模型尚不足以用于照明设计")
    if ruleset and model and (ruleset.bound_model_version != model.version or ruleset.bound_source_sha256 != model.source_sha256):
        issues.append("规则与当前 CAD/模型版本不一致，请重新确认适用性并绑定")
    for room in model.rooms if model else []:
        if room.status == "excluded":
            continue
        applicable = [r for r in ruleset.rules if r.status == "confirmed" and room.usage in r.applies_to] if ruleset else []
        if not applicable:
            issues.append(f"房间 {room.name or room.room_id} 没有已确认的适用规则")
        evaluations = {}
        for rule in applicable:
            key = (rule.evaluation_scope, evaluation_target(rule))
            evaluation = evaluations.setdefault(key, {"scope": key[0], "target": key[1], "conditions": {}, "rule_ids": []})
            evaluation["rule_ids"].append(rule.rule_id)
            settings = evaluation["conditions"]
            for key, value in rule.conditions.model_dump().items():
                if value not in (None, ""):
                    if key in settings and settings[key] != value:
                        issues.append(f"房间 {room.room_id} 的 {key} 存在冲突")
                    settings[key] = value
        # Compatibility summary includes only values shared by every evaluation.
        values = [v["conditions"] for v in evaluations.values()]
        common = {key: value for key, value in values[0].items() if all(other.get(key) == value for other in values[1:])} if values else {}
        rooms.append({"room_id": room.room_id, "usage": room.usage, "conditions": common, "evaluations": list(evaluations.values()),
                      "requirements": [r.model_dump(mode="json") for r in applicable]})
    if ruleset:
        issues.extend(rule_conflicts(ruleset.rules))
    return {"status": "ready_for_dialux_setup" if not issues else "needs_review",
            "rule_version": ruleset.bound_version if ruleset else None,
            "model_version": model.version if model else None, "issues": list(dict.fromkeys(issues)),
            "rooms": rooms, "simulation_performed": False, "compliance_status": "not_evaluated"}
