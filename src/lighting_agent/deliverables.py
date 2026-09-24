"""Redesign packages and unverified DIALux evidence records."""

from __future__ import annotations

import json
import hashlib
import zipfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Literal

from .schemas import (
    DesignRun,
    DialuxVisionAnalysis,
    ProjectState,
    SimulationArtifact,
    SimulationMetrics,
    SimulationRun,
)


def _canonical_json(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def build_redesign_package(project_root: Path, run: DesignRun) -> bytes:
    """Build a verified redesign ZIP from one persisted design run."""

    root = Path(project_root).resolve()
    files: list[tuple[Path, object]] = []
    manifest_files: list[dict[str, object]] = []
    for artifact in run.artifacts:
        target = (root / artifact.relative_path).resolve()
        if root not in target.parents or not target.is_file():
            raise FileNotFoundError(artifact.relative_path)
        content = target.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest != artifact.sha256 or len(content) != artifact.size_bytes:
            raise ValueError(f"Redesign artifact integrity check failed: {artifact.name}")
        files.append((target, artifact))
        manifest_files.append(
            {
                "name": artifact.name,
                "sha256": digest,
                "size_bytes": len(content),
                "media_type": artifact.media_type,
            }
        )
    manifest = {
        "schema_version": "redesign-package-1",
        "run": run.model_dump(mode="json", exclude={"artifacts"}),
        "files": manifest_files,
    }
    output = BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("redesign-package.json", _canonical_json(manifest))
        for target, artifact in files:
            archive.write(target, artifact.name)
    return output.getvalue()


def build_unverified_simulation_run(
    state: ProjectState,
    *,
    metrics: SimulationMetrics,
    source_kind: str,
    solver_version: str | None = None,
    parser_version: str | None = None,
    artifacts: list[SimulationArtifact] | None = None,
    metric_source: Literal["manual", "pdf_text", "vision"] | None = None,
    vision_analysis: DialuxVisionAnalysis | None = None,
) -> SimulationRun:
    """Keep uploaded evidence without claiming it verifies a new design."""

    evidence = list(artifacts or [])
    return SimulationRun(
        kind="精算", status="unverified", verification_status="unverified",
        input_project_revision=state.revision,
        selected_luminaire_ids=list(state.selected_luminaire_ids),
        solver_version=solver_version, parser_version=parser_version,
        artifact_path=evidence[0].storage_path if evidence else None,
        source_file=evidence[0].file_name if evidence else None,
        source_sha256=evidence[0].sha256 if evidence else None,
        source_kind=source_kind, artifacts=evidence, metrics=metrics,
        metric_source=metric_source, vision_analysis=vision_analysis,
        verification_messages=["已保存为现状分析证据；尚未验证其与当前新方案一致，不能据此声明新方案达标。"],
        completed_at=datetime.now(UTC),
    )

