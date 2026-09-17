"""Joint illuminance verification for lumen-method and DIALux results."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ..schemas import ProjectState, StrictModel


class IlluminanceMethodCheck(StrictModel):
    method: Literal["lumen_method", "dialux"]
    status: Literal["pass", "fail", "missing", "unverified", "stale"]
    observed_illuminance_lx: float | None = Field(default=None, ge=0)
    target_illuminance_lx: float | None = Field(default=None, gt=0)
    source_id: str | None = None
    explanation: str


class IlluminanceVerification(StrictModel):
    """One auditable decision using illuminance as the only acceptance metric."""

    overall_status: Literal["pass", "fail", "pending"]
    target_illuminance_lx: float | None = Field(default=None, gt=0)
    lumen_method: IlluminanceMethodCheck
    dialux: IlluminanceMethodCheck
    difference_percent: float | None = Field(default=None, ge=0)
    iteration: int = Field(ge=0)
    action: Literal[
        "target_reached",
        "revise_design",
        "confirm_target",
        "run_lumen_method",
        "await_dialux_result",
        "rerun_dialux",
    ]
    message: str


def _lumen_check(state: ProjectState, target: float | None) -> IlluminanceMethodCheck:
    if target is None:
        return IlluminanceMethodCheck(
            method="lumen_method",
            status="missing",
            explanation="尚未确认目标照度。",
        )
    if not state.calculations:
        return IlluminanceMethodCheck(
            method="lumen_method",
            status="missing",
            target_illuminance_lx=target,
            explanation="尚未执行流明法初算。",
        )
    result = state.calculations[-1]
    observed = result.estimated_illuminance_lx
    passed = observed >= target
    return IlluminanceMethodCheck(
        method="lumen_method",
        status="pass" if passed else "fail",
        observed_illuminance_lx=observed,
        target_illuminance_lx=target,
        source_id=result.calculated_at.isoformat(),
        explanation=(
            f"流明法估算照度 {observed:g} lx，{'达到' if passed else '未达到'}目标 {target:g} lx。"
        ),
    )


def _dialux_check(state: ProjectState, target: float | None) -> IlluminanceMethodCheck:
    if target is None:
        return IlluminanceMethodCheck(
            method="dialux",
            status="missing",
            explanation="尚未确认目标照度。",
        )
    if not state.simulation_runs:
        return IlluminanceMethodCheck(
            method="dialux",
            status="missing",
            target_illuminance_lx=target,
            explanation="尚未导入 DIALux 仿真图片或设计报告。",
        )
    run = state.simulation_runs[-1]
    if run.status == "stale" or run.verification_status == "stale":
        return IlluminanceMethodCheck(
            method="dialux",
            status="stale",
            target_illuminance_lx=target,
            source_id=run.run_id,
            explanation="DIALux 结果对应的项目输入已经变化，需要重新仿真。",
        )
    if run.status != "succeeded" or run.verification_status != "matched":
        return IlluminanceMethodCheck(
            method="dialux",
            status="unverified",
            target_illuminance_lx=target,
            source_id=run.run_id,
            explanation="DIALux 结果与当前任务包或项目版本不匹配。",
        )
    observed = run.metrics.maintained_illuminance_lx if run.metrics is not None else None
    if observed is None:
        return IlluminanceMethodCheck(
            method="dialux",
            status="missing",
            target_illuminance_lx=target,
            source_id=run.run_id,
            explanation="DIALux 证据中尚未记录维持照度。",
        )
    passed = observed >= target
    return IlluminanceMethodCheck(
        method="dialux",
        status="pass" if passed else "fail",
        observed_illuminance_lx=observed,
        target_illuminance_lx=target,
        source_id=run.run_id,
        explanation=(
            f"DIALux 维持照度 {observed:g} lx，{'达到' if passed else '未达到'}目标 {target:g} lx。"
        ),
    )


def evaluate_illuminance(state: ProjectState) -> IlluminanceVerification:
    """Require both methods to reach the target before accepting the design."""

    target = state.brief.target_illuminance_lx
    lumen = _lumen_check(state, target)
    dialux = _dialux_check(state, target)
    difference = None
    if (
        lumen.observed_illuminance_lx is not None
        and dialux.observed_illuminance_lx is not None
        and dialux.observed_illuminance_lx > 0
    ):
        difference = round(
            abs(lumen.observed_illuminance_lx - dialux.observed_illuminance_lx)
            / dialux.observed_illuminance_lx
            * 100,
            2,
        )

    if target is None:
        overall, action = "pending", "confirm_target"
        message = "确认目标照度后才能开始联合检验。"
    elif lumen.status == "fail" or dialux.status == "fail":
        overall, action = "fail", "revise_design"
        message = "至少一种检验结果未达到目标照度，需要调整灯具数量、光通量或布灯后继续迭代。"
    elif lumen.status == "pass" and dialux.status == "pass":
        overall, action = "pass", "target_reached"
        message = "流明法与 DIALux 仿真均达到目标照度，本轮迭代停止。"
    elif lumen.status == "missing":
        overall, action = "pending", "run_lumen_method"
        message = "先完成流明法初算，再与 DIALux 结果共同检验。"
    elif dialux.status == "stale":
        overall, action = "pending", "rerun_dialux"
        message = "项目输入已变化，请基于最新任务包重新运行 DIALux。"
    elif dialux.status == "unverified":
        overall, action = "pending", "rerun_dialux"
        message = "当前 DIALux 证据无法对应到最新任务包，请重新导出并上传。"
    else:
        overall, action = "pending", "await_dialux_result"
        message = "流明法已完成，等待上传含维持照度的 DIALux 仿真图片或设计报告。"

    return IlluminanceVerification(
        overall_status=overall,
        target_illuminance_lx=target,
        lumen_method=lumen,
        dialux=dialux,
        difference_percent=difference,
        iteration=len(state.simulation_runs),
        action=action,
        message=message,
    )
