"""Project-scoped orchestration around the standalone DIALux executor."""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import threading
from uuid import uuid4

from .config import Settings
from .dialux_runner.artifacts import RunError, atomic_json
from .dialux_runner.generic_jobs import (
    GENERIC_PROFILE, auto_layout, generate_layout_candidates, prepare_generic_job,
    prepare_reviewed_model,
)
from .dialux_runner.runner import run_job
from .schemas import (
    DialuxLayoutItem, DialuxReadiness, DialuxRunRecord,
    DialuxRunRequest, ProjectState,
)


class DialuxRunNotFoundError(FileNotFoundError):
    pass


class DialuxExecutionService:
    """Create immutable jobs and run at most one desktop job per process."""

    def __init__(
        self,
        projects,
        *,
        settings: Settings | None = None,
        executable: Path | None = None,
        dialux_api=None,
    ):
        self.projects = projects
        self.settings = settings or Settings()
        self.dialux_api = dialux_api
        configured_value = executable or (Path(getattr(self.settings, "dialux_executable", ""))
                                         if getattr(self.settings, "dialux_executable", None) else None)
        configured = configured_value
        self.executable = configured.resolve() if configured else None
        self._lock = threading.RLock()
        self._active: str | None = None

    def _root(self, project_id: str) -> Path:
        self.projects.get(project_id)
        root = self.projects.directory / f"{project_id}.dialux-runs"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _record_path(self, project_id: str, run_id: str) -> Path:
        return self._root(project_id) / run_id / "record.json"

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    def _save(self, record: DialuxRunRecord) -> DialuxRunRecord:
        path = self._record_path(record.project_id, record.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        record.updated_at = self._now()
        atomic_json(path, record.model_dump(mode="json"))
        return record

    def get(self, project_id: str, run_id: str) -> DialuxRunRecord:
        path = self._record_path(project_id, run_id)
        if not path.is_file():
            raise DialuxRunNotFoundError(f"DIALux run {run_id!r} does not exist")
        return DialuxRunRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self, project_id: str) -> list[DialuxRunRecord]:
        root = self._root(project_id)
        records = []
        for path in root.glob("*/record.json"):
            try:
                records.append(DialuxRunRecord.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return sorted(records, key=lambda item: item.created_at, reverse=True)

    def _state_model(self, state: ProjectState):
        if not state.floor_plan or not state.floor_plan.spatial_model:
            return None
        return state.floor_plan.spatial_model

    def readiness(self, project_id: str, request: DialuxRunRequest) -> tuple[DialuxReadiness, object, list[DialuxLayoutItem]]:
        state = self.projects.get(project_id)
        model = self._state_model(state)
        if model is None:
            return DialuxReadiness(status="blocked", complexity="unknown", issues=["项目尚未确认空间模型"],
                                   assumptions=request.assumptions), None, []
        if model.automation_status != "auto_confirmed":
            return DialuxReadiness(
                status="blocked", complexity="unknown",
                issues=["CAD 模型尚未通过自动分析；禁止跳过模型判断启动 DIALux"],
                assumptions=request.assumptions,
            ), None, []
        effective, readiness = prepare_reviewed_model(
            model, request.assumptions, default_usage=state.brief.space_type,
            confirm=True,
        )
        optimization = request.optimization
        unsupported_optimization = []
        if optimization.max_power_w is not None:
            unsupported_optimization.append(
                "当前通用 DIALux 结果没有经过验证的总输入功率字段，不能执行 max_power_w 硬约束"
            )
        if optimization.enabled and optimization.objective != "balanced":
            unsupported_optimization.append(
                "当前布局候选只调整位置，尚不能优化灯具数量或功率；请使用 balanced 目标"
            )
        if unsupported_optimization:
            readiness.issues.extend(unsupported_optimization)
        layout = request.layout or (auto_layout(effective) if readiness.can_prepare else [])
        candidate = self._luminaire(state, request.luminaire_id)
        readiness.luminaire_id = request.luminaire_id
        if candidate is not None:
            readiness.luminaire_name = candidate.article_name
        if request.photometry_path:
            readiness.photometry_path = request.photometry_path
            readiness.photometry_source = "user_upload"
        elif request.luminaire_id and candidate is not None and self._candidate_has_uld(candidate):
            readiness.photometry_source = "dialux_catalogue"
            readiness.photometry_path = self._cached_luminaire_relative_path(project_id, candidate.luminaire_id)
        elif request.luminaire_id and candidate is None:
            readiness.issues.append("所选智能体灯具不在当前项目候选列表中")
            readiness.status = "blocked"
            readiness.can_prepare = readiness.can_start = False
        elif request.luminaire_id and not self._candidate_has_uld(candidate):
            readiness.issues.append("所选灯具没有可供 DIALux 使用的 ULD 文件，请改选带有 DIALux 文件的候选")
            readiness.status = "blocked"
            readiness.can_prepare = readiness.can_start = False
        elif request.luminaire_id:
            readiness.issues.append("当前服务未配置 DIALux 灯具目录下载器")
            readiness.status = "blocked"
            readiness.can_prepare = readiness.can_start = False
        else:
            readiness.issues.append("需要本地 IES、LDT 或 ULD 光度文件；产品目录链接不能替代计算文件")
            if readiness.can_prepare:
                readiness.status = "needs_confirmation"
            readiness.can_prepare = False
            readiness.can_start = False
        if request.expected_revision != state.revision:
            readiness.issues.append(f"项目已更新，请重新读取当前版本（当前修订 {state.revision}）")
            readiness.status = "blocked"
            readiness.can_prepare = readiness.can_start = False
        if not layout and readiness.can_prepare:
            readiness.issues.append("无法生成每个房间至少一盏灯的初始布局")
            readiness.can_prepare = readiness.can_start = False
        if unsupported_optimization:
            readiness.status = "blocked"
            readiness.can_prepare = readiness.can_start = False
        return readiness, effective, layout

    def prepare(self, project_id: str, request: DialuxRunRequest, *, run_id: str | None = None) -> DialuxRunRecord:
        state_at_creation = self.projects.get(project_id)
        if request.expected_revision != state_at_creation.revision:
            # Still create a diagnostic draft so the client can display why it
            # was rejected without losing the user's assumptions.
            pass
        readiness, effective, layout = self.readiness(project_id, request)
        record = DialuxRunRecord(
            run_id=run_id or uuid4().hex, project_id=project_id,
            status="draft", profile=GENERIC_PROFILE, readiness=readiness, request=request,
            source_sha256=state_at_creation.floor_plan.asset.sha256 if state_at_creation.floor_plan else None,
            model_analysis_sha256=(state_at_creation.floor_plan.spatial_model.analysis_sha256
                                   if state_at_creation.floor_plan and state_at_creation.floor_plan.spatial_model else None),
        )
        self._save(record)
        if not readiness.can_prepare or effective is None:
            return record
        if self.executable is None or not self.executable.is_file():
            record.status = "needs_attention"
            record.error = "未配置可用的 DIALux_x64.exe；请设置 DIALUX_EXECUTABLE"
            return self._save(record)
        state = self.projects.get(project_id)
        plan = state.floor_plan
        assert plan is not None
        cad_path = (self.projects.directory / plan.asset.storage_path).resolve()
        if not cad_path.is_file() or not cad_path.is_relative_to(self.projects.directory.resolve()):
            record.status = "needs_attention"
            record.error = "项目原始 CAD 文件不存在或超出项目目录"
            return self._save(record)
        try:
            photometry_path = self._resolve_photometry(
                project_id, request.photometry_path, luminaire_id=request.luminaire_id,
            )
        except RunError as error:
            record.status = "needs_attention"
            record.error = str(error)
            return self._save(record)
        if photometry_path is None:
            record.status = "needs_attention"
            record.error = "光度文件不存在或不在项目目录"
            return self._save(record)
        output = self._record_path(project_id, record.run_id).parent
        try:
            prepare_generic_job(
                cad_path=cad_path, photometry_path=photometry_path, model=effective,
                assumptions=request.assumptions, layout=layout,
                optimization=request.optimization, project_name=state.brief.project_name,
                project_id=project_id, executable=self.executable, output=output,
                luminaire_id=request.luminaire_id,
                photometry_source="dialux_catalogue" if not request.photometry_path else "user_upload",
            )
            optimization_jobs = []
            candidates = generate_layout_candidates(effective, layout, request.optimization)
            for index, candidate in enumerate(candidates[1:], 1):
                candidate_root = output / "candidates" / f"candidate-{index:03d}"
                prepare_generic_job(
                    cad_path=cad_path, photometry_path=photometry_path, model=effective,
                    assumptions=request.assumptions, layout=candidate,
                    optimization=request.optimization, project_name=state.brief.project_name,
                    project_id=project_id, executable=self.executable, output=candidate_root,
                    luminaire_id=request.luminaire_id,
                    photometry_source="dialux_catalogue" if not request.photometry_path else "user_upload",
                )
                optimization_jobs.append(str(candidate_root.relative_to(self.projects.directory)).replace("\\", "/"))
        except (RunError, OSError, ValueError) as error:
            record.status = "needs_attention"
            record.error = str(error)
            return self._save(record)
        record.status = "prepared"
        record.job_directory = str(output.relative_to(self.projects.directory)).replace("\\", "/")
        resolved_relative = str(photometry_path.relative_to(self.projects.directory.resolve())).replace("\\", "/")
        record.request = request.model_copy(update={"layout": layout, "photometry_path": resolved_relative})
        if optimization_jobs:
            record = record.model_copy(update={"optimization_jobs": optimization_jobs})
        record = self._save(record)
        # The automatic pipeline starts the real desktop calculation as soon
        # as the immutable job has been prepared.  The start method is
        # idempotent for already queued/running/completed records.
        return self.start(project_id, record.run_id)

    def auto_start(self, project_id: str) -> DialuxRunRecord:
        """Start DIALux from an automatically approved model when input is unambiguous."""
        state = self.projects.get(project_id)
        model = self._state_model(state)
        if model is None or model.automation_status != "auto_confirmed":
            raise RunError("CAD model has not passed automatic validation")

        for previous in self.list(project_id):
            if (previous.source_sha256 == state.floor_plan.asset.sha256
                    and previous.model_analysis_sha256 == model.analysis_sha256
                    and previous.status in {"queued", "running", "completed"}):
                return previous

        light_root = self.projects.directory / f"{project_id}.photometry"
        files = sorted(path for path in light_root.glob("*")
                       if path.is_file() and path.suffix.casefold() in {".ies", ".ldt", ".uld"}) \
            if light_root.is_dir() else []
        candidates = [item for item in state.luminaires if self._candidate_has_uld(item)]
        photometry_path = None
        luminaire_id = None
        issue = None
        if len(files) == 1:
            photometry_path = str(files[0].relative_to(self.projects.directory)).replace("\\", "/")
        elif len(files) > 1:
            if len(candidates) == 1:
                # A unique catalogue luminaire gives us an unambiguous official
                # ULD source even if unrelated local files are also present.
                luminaire_id = candidates[0].luminaire_id
            else:
                issue = "Multiple photometry files are available; automatic selection is ambiguous"
        elif len(candidates) == 1:
            luminaire_id = candidates[0].luminaire_id
        elif len(candidates) > 1:
            issue = "Multiple DIALux luminaires are available; automatic selection is ambiguous"
        else:
            issue = "No IES/LDT/ULD photometry file or DIALux luminaire candidate is available"

        request = DialuxRunRequest(expected_revision=state.revision,
                                   photometry_path=photometry_path, luminaire_id=luminaire_id)
        record = self.prepare(project_id, request)
        if record.status == "draft":
            record.status = "needs_attention"
            record.error = issue or "; ".join(record.readiness.issues if record.readiness else []) or \
                "DIALux job is not ready"
            return self._save(record)
        return record

    @staticmethod
    def _luminaire(state: ProjectState, luminaire_id: str | None):
        if not luminaire_id:
            return None
        return next((item for item in state.luminaires if item.luminaire_id == luminaire_id), None)

    @staticmethod
    def _candidate_has_uld(candidate) -> bool:
        return bool(getattr(candidate, "has_uld", False) or getattr(candidate, "dialux_protocol_url", None))

    @staticmethod
    def _cached_luminaire_relative_path(project_id: str, luminaire_id: str) -> str:
        digest = hashlib.sha256(luminaire_id.encode("utf-8")).hexdigest()[:24]
        return f"{project_id}.photometry/dialux-{digest}.uld"

    def _resolve_photometry(
        self,
        project_id: str,
        relative: str | None,
        *,
        luminaire_id: str | None = None,
    ) -> Path | None:
        auto = relative is None
        if not relative:
            if not luminaire_id:
                return None
            relative = self._cached_luminaire_relative_path(project_id, luminaire_id)
        root = self.projects.directory.resolve()
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            return None
        if path.is_file() and path.stat().st_size > 0:
            return path
        if not auto or not luminaire_id:
            return None

        state = self.projects.get(project_id)
        candidate = self._luminaire(state, luminaire_id)
        if candidate is None:
            raise RunError("所选智能体灯具不在当前项目候选列表中")
        if not self._candidate_has_uld(candidate):
            raise RunError("所选灯具没有可供 DIALux 使用的 ULD 文件")
        if self.dialux_api is None or not hasattr(self.dialux_api, "download_luminaire_uld"):
            raise RunError("当前服务未配置 DIALux 灯具目录下载器")
        if path.suffix.casefold() != ".uld":
            raise RunError("自动获取的 DIALux 灯具文件路径无效")
        try:
            downloaded_value = self.dialux_api.download_luminaire_uld(candidate.detail_url, path)
            downloaded = Path(downloaded_value or path).resolve()
        except Exception as error:
            raise RunError(f"无法自动获取所选 DIALux 灯具文件：{error}") from error
        if not downloaded.is_relative_to(root) or not downloaded.is_file() or downloaded.stat().st_size <= 0:
            raise RunError("DIALux 灯具文件下载结果无效")
        return downloaded

    def confirm(self, project_id: str, run_id: str) -> DialuxRunRecord:
        record = self.get(project_id, run_id)
        if record.status != "draft" or record.request is None:
            return record
        request = record.request.model_copy(update={"confirm_assumptions": True})
        return self.prepare(project_id, request, run_id=run_id)

    def start(self, project_id: str, run_id: str, *, resume: bool = False) -> DialuxRunRecord:
        record = self.get(project_id, run_id)
        if record.status not in {"prepared", "needs_attention"}:
            return record
        if not record.job_directory:
            record.error = "运行尚未准备完成"
            record.status = "needs_attention"
            return self._save(record)
        with self._lock:
            if self._active and self._active != run_id:
                record.status = "needs_attention"
                record.error = "已有另一个 DIALux 桌面任务正在运行"
                return self._save(record)
            self._active = run_id
        record.status = "queued"
        self._save(record)

        def worker():
            try:
                current = self.get(project_id, run_id)
                current.status = "running"
                self._save(current)
                job_directories = [current.job_directory]
                job_directories.extend(current.model_extra.get("optimization_jobs", []))
                candidate_results = []
                for index, relative_directory in enumerate(job_directories):
                    state = run_job(self.projects.directory / relative_directory, resume=resume if index == 0 else False)
                    if state.get("status") != "completed":
                        raise RunError(f"DIALux candidate {index + 1} did not complete")
                    job_root = self.projects.directory / relative_directory
                    result_paths = sorted(job_root.glob("attempt-*/results.json"))
                    if not result_paths:
                        raise RunError(f"DIALux candidate {index + 1} has no results.json")
                    candidate_results.append(json.loads(result_paths[-1].read_text(encoding="utf-8")))
                current = self.get(project_id, run_id)
                current.status = "completed"
                current.result = self._select_result(current, candidate_results)
                current.error = None
                self._save(current)
            except Exception as error:  # desktop failures are persisted for inspection/resume
                try:
                    current = self.get(project_id, run_id)
                    current.status = "needs_attention"
                    current.error = str(error)
                    self._save(current)
                finally:
                    with self._lock:
                        self._active = None
            else:
                with self._lock:
                    self._active = None

        threading.Thread(target=worker, name=f"dialux-{run_id[:8]}", daemon=True).start()
        return self.get(project_id, run_id)

    @staticmethod
    def _select_result(record: DialuxRunRecord, candidates: list[dict]) -> dict:
        optimization = record.request.optimization if record.request else None
        if not optimization or not optimization.enabled or len(candidates) == 1:
            return candidates[0]

        def values(result):
            rows = result.get("rooms", [])
            averages, uniformities = [], []
            for row in rows:
                metrics = row.get("metrics", {})
                average = metrics.get("average_illuminance_lx", {}).get("value")
                uniformity = metrics.get("uniformity_u0", {}).get("value")
                if isinstance(average, (int, float)):
                    averages.append(float(average))
                if isinstance(uniformity, (int, float)):
                    uniformities.append(float(uniformity))
            return averages, uniformities

        def score(result):
            averages, uniformities = values(result)
            expected_rooms = len({item.room_id for item in record.request.layout})
            if not expected_rooms:
                expected_rooms = len(result.get("rooms", []))
            average_required = optimization.target_illuminance_lx is not None
            uniformity_required = optimization.min_uniformity_u0 is not None
            average_missing = max(0, expected_rooms - len(averages)) if average_required else 0
            uniformity_missing = max(0, expected_rooms - len(uniformities)) if uniformity_required else 0
            average_gap = sum(max(0., optimization.target_illuminance_lx - value)
                               / optimization.target_illuminance_lx for value in averages) \
                if average_required else 0.
            uniformity_gap = sum(
                max(0., optimization.min_uniformity_u0 - value)
                / max(optimization.min_uniformity_u0, 1e-9) for value in uniformities
            ) \
                if uniformity_required else 0.
            feasible = (average_missing == 0 and uniformity_missing == 0
                        and not average_gap and not uniformity_gap)
            return (0 if feasible else 1, average_missing + uniformity_missing,
                    average_gap + uniformity_gap)

        selected_index, selected = min(enumerate(candidates), key=lambda item: score(item[1]))
        return {
            **selected,
            "optimization": {
                "enabled": True, "candidate_count": len(candidates),
                "selected_candidate": selected_index + 1,
                "candidate_scores": [score(result) for result in candidates],
                "hard_constraints": {
                    "target_illuminance_lx": optimization.target_illuminance_lx,
                    "min_uniformity_u0": optimization.min_uniformity_u0,
                },
                "secondary_objective_applied": False,
                "status": "feasible_candidate_selected" if score(selected)[0] == 0 else "no_feasible_candidate",
            },
            "optimization_candidates": candidates,
        }

    def pause(self, project_id: str, run_id: str) -> DialuxRunRecord:
        record = self.get(project_id, run_id)
        if not record.job_directory:
            return record
        atomic_json(self.projects.directory / record.job_directory / "pause.request", {"request": "pause before next step"})
        return record
