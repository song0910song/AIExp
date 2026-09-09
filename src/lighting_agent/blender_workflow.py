"""Deterministic services for the conversational Blender lighting workflow.

The module talks directly to the locally installed Blender MCP add-on over its
JSON socket protocol.  It keeps generated models and renders inside the
project, reuses them only when their source/luminaire snapshot still matches,
and labels every illuminance result as a preliminary estimate.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .schemas import (
    BlenderEstimate,
    BlenderModelAsset,
    BlenderWorkflow,
    BlenderWorkflowNode,
    BlenderWorkflowParameters,
    ProjectState,
)

SOLVER_VERSION = "blender-workflow-estimate-1"


class BlenderWorkflowError(ValueError):
    """A workflow input cannot be processed safely."""


class BlenderConnectionError(BlenderWorkflowError):
    """Blender or its local MCP socket cannot be reached."""


def workflow_root(project_directory: Path, project_id: str) -> Path:
    """Return the project-local directory used by this workflow."""

    if not project_id.isalnum():
        raise BlenderWorkflowError("project_id must be alphanumeric")
    return project_directory / f"{project_id}.blender-workflow"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _mcp_address() -> tuple[str, int]:
    host = os.getenv("BLENDER_MCP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    raw_port = os.getenv("BLENDER_MCP_PORT", "9876").strip()
    try:
        port = int(raw_port)
    except ValueError as error:
        raise BlenderConnectionError("BLENDER_MCP_PORT 必须是有效端口") from error
    if not 1 <= port <= 65_535:
        raise BlenderConnectionError("BLENDER_MCP_PORT 必须在 1-65535 之间")
    return host, port


def mcp_request(command_type: str, params: dict[str, Any] | None = None, *, timeout: float = 5.0) -> dict[str, Any]:
    """Send one complete command to the Blender MCP add-on socket."""

    host, port = _mcp_address()
    payload = json.dumps(
        {"type": command_type, "params": params or {}},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    buffer = bytearray()
    try:
        with socket.create_connection((host, port), timeout=min(timeout, 5.0)) as client:
            client.settimeout(timeout)
            client.sendall(payload)
            while len(buffer) <= 32 * 1024 * 1024:
                chunk = client.recv(64 * 1024)
                if not chunk:
                    break
                buffer.extend(chunk)
                try:
                    response = json.loads(buffer.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(response, dict):
                    raise BlenderConnectionError("Blender MCP 返回了无效响应")
                if response.get("status") != "success":
                    raise BlenderConnectionError(str(response.get("message") or "Blender MCP 命令失败"))
                result = response.get("result")
                return result if isinstance(result, dict) else {"result": result}
    except BlenderConnectionError:
        raise
    except (OSError, TimeoutError) as error:
        raise BlenderConnectionError(f"无法连接 Blender MCP {host}:{port}：{error}") from error
    raise BlenderConnectionError("Blender MCP 响应不完整或超过 32 MB")


def probe_blender() -> tuple[str, str]:
    """Distinguish a working MCP socket from a merely running GUI process."""

    try:
        host, port = _mcp_address()
    except BlenderConnectionError as error:
        return "unavailable", str(error)
    try:
        result = mcp_request("ping", timeout=1.5)
        if result.get("pong") is True:
            return "connected", f"Blender MCP 已连接：{host}:{port}"
    except BlenderConnectionError:
        pass
    if _blender_process_running():
        return "unavailable", "Blender 已打开，但 MCP 服务不可用；请在 Blender 的 Blender MCP 面板中点击 Connect。"
    executable = discover_blender_executable()
    if executable:
        return "unavailable", f"Blender 尚未打开；已找到 {executable}，建模时会自动启动。"
    return "unavailable", "未找到 Blender。请先安装或自行打开 Blender，并在环境变量 BLENDER_EXECUTABLE 中指定 blender.exe。"


def _blender_process_running() -> bool:
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq blender.exe", "/NH"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            return "blender.exe" in result.stdout.casefold()
        result = subprocess.run(
            ["pgrep", "-f", "(^|/)blender($|\\s)"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def discover_blender_executable() -> Path | None:
    """Find Blender without assuming it was added to ``PATH``."""

    configured = os.getenv("BLENDER_EXECUTABLE", "").strip()
    candidates: list[Path] = [Path(configured)] if configured else []
    located = shutil.which("blender.exe" if sys.platform == "win32" else "blender")
    if located:
        candidates.append(Path(located))

    if sys.platform == "win32":
        try:
            import winreg

            for hive, key_name in (
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\blender.exe"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\blender.exe"),
                (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\App Paths\blender.exe"),
            ):
                try:
                    with winreg.OpenKey(hive, key_name) as key:
                        candidates.append(Path(winreg.QueryValue(key, None)))
                except OSError:
                    continue
        except ImportError:
            pass

        for drive_letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            root = Path(f"{drive_letter}:/")
            candidates.extend(
                [
                    root / "blender" / "blender.exe",
                    root / "Program Files" / "Blender Foundation" / "Blender" / "blender.exe",
                    root / "SteamLibrary" / "steamapps" / "common" / "Blender" / "blender.exe",
                ]
            )
        for program_root in filter(None, (os.getenv("ProgramFiles"), os.getenv("ProgramW6432"))):
            foundation = Path(program_root) / "Blender Foundation"
            if foundation.is_dir():
                candidates.extend(sorted(foundation.glob("**/blender.exe"), reverse=True))
    else:
        candidates.extend((Path("/Applications/Blender.app/Contents/MacOS/Blender"), Path("/usr/bin/blender")))

    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
            if resolved.is_file():
                return resolved
        except OSError:
            continue
    return None


def ensure_blender_running(*, open_file: Path | None = None, wait_seconds: float = 30.0) -> tuple[str, bool]:
    """Return a connected MCP message, starting Blender's GUI when necessary."""

    # Fail fast on a malformed endpoint instead of launching Blender and
    # waiting for a connection that can never succeed.
    _mcp_address()
    status, message = probe_blender()
    if status == "connected":
        return message, False
    if _blender_process_running():
        raise BlenderConnectionError(message)

    executable = discover_blender_executable()
    if executable is None:
        raise BlenderConnectionError(message)
    command = [str(executable)]
    if open_file and open_file.is_file():
        command.append(str(open_file.resolve()))
    try:
        subprocess.Popen(command, cwd=str(executable.parent), close_fds=True)
    except OSError as error:
        raise BlenderConnectionError(f"无法启动 Blender：{error}。请自行打开 Blender 后重试。") from error

    deadline = time.monotonic() + max(1.0, wait_seconds)
    last_message = ""
    while time.monotonic() < deadline:
        time.sleep(0.5)
        status, last_message = probe_blender()
        if status == "connected":
            return last_message, True
    raise BlenderConnectionError(
        f"Blender 已启动，但 {wait_seconds:g} 秒内未连接 MCP。{last_message}"
    )


def discover_render_paths(model_path: Path, root: Path) -> list[str]:
    """Find Blender render snapshots next to a saved model."""

    paths: list[str] = []
    for candidate in sorted(model_path.parent.glob("render*.png")):
        if not candidate.is_file():
            continue
        try:
            paths.append(candidate.resolve().relative_to(root.resolve()).as_posix())
        except ValueError:
            # A model produced by an external Blender session commonly keeps
            # its render snapshots beside the .blend outside the selected
            # project folder.  Keep the absolute path temporarily; the caller
            # copies it into the durable workflow directory below.
            paths.append(str(candidate.resolve()))
        if len(paths) >= 12:
            break
    return paths


def render_source_previews(
    source: Path,
    *,
    source_type: str,
    project_root: Path,
    project_id: str,
    floor_plan: Any | None = None,
) -> list[str]:
    """Render bounded visual previews for the conversation artifact sidebar."""

    preview_dir = workflow_root(project_root, project_id) / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    stem = hashlib.sha256(source.read_bytes()).hexdigest()[:12]
    rendered: list[Path] = []
    if source_type == "pdf":
        try:
            import fitz

            with fitz.open(source) as document:
                for index in range(min(3, document.page_count)):
                    target = preview_dir / f"{stem}-page-{index + 1}.png"
                    pixmap = document[index].get_pixmap(matrix=fitz.Matrix(1.45, 1.45), alpha=False)
                    pixmap.save(target)
                    rendered.append(target)
        except Exception:
            return []
    elif floor_plan is not None:
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            candidate = None
            index = getattr(floor_plan, "selected_area_candidate_index", None)
            candidates = getattr(floor_plan, "area_candidates", [])
            if index is not None and 0 <= index < len(candidates):
                candidate = candidates[index]
            elif candidates:
                candidate = candidates[0]
            if candidate is None or len(candidate.points) < 3:
                return []
            xs = [point.x for point in candidate.points]
            ys = [point.y for point in candidate.points]
            xs.append(xs[0])
            ys.append(ys[0])
            figure, axis = plt.subplots(figsize=(9, 6), dpi=130)
            axis.plot(xs, ys, color="#20252b", linewidth=1.8)
            axis.fill(xs, ys, color="#dbe5e8", alpha=0.7)
            axis.set_aspect("equal", adjustable="box")
            axis.set_title(f"CAD boundary preview - {source.name}")
            axis.set_xlabel(f"Drawing units: {floor_plan.drawing_units}")
            axis.grid(color="#c7ced4", linewidth=0.45)
            figure.tight_layout()
            target = preview_dir / f"{stem}-cad-boundary.png"
            figure.savefig(target)
            plt.close(figure)
            rendered.append(target)
        except Exception:
            return []
    return [path.relative_to(project_root).as_posix() for path in rendered if path.is_file()]


def _safe_relative_path(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    resolved_root = root.resolve()
    if target != resolved_root and resolved_root not in target.parents:
        raise BlenderWorkflowError("工作流资产路径越过项目目录")
    return target


def register_model(
    *,
    source: Path,
    project_root: Path,
    project_id: str,
    source_sha256: str | None,
    previous: BlenderModelAsset | None = None,
) -> BlenderModelAsset:
    """Copy/register one saved ``.blend`` and preserve reusable renders."""

    source = source.expanduser().resolve()
    if source.suffix.casefold() != ".blend":
        raise BlenderWorkflowError("模型文件必须是 .blend")
    if not source.is_file():
        raise BlenderWorkflowError(f"模型文件不存在：{source}")

    root = workflow_root(project_root, project_id)
    model_dir = root / "model"
    model_dir.mkdir(parents=True, exist_ok=True)
    target = model_dir / f"{project_id}.blend"
    if source != target:
        shutil.copy2(source, target)
    model_hash = sha256_file(target)
    status, message = probe_blender()
    render_paths = discover_render_paths(source, project_root)

    # Keep renders outside the selected project folder usable by copying them
    # into the durable workflow directory.  Existing in-project renders are
    # referenced directly and therefore are not duplicated.
    durable_renders: list[str] = []
    render_dir = model_dir / "renders"
    for relative in render_paths:
        render_source = Path(relative) if Path(relative).is_absolute() else _safe_relative_path(project_root, relative)
        if render_source.parent == model_dir or render_source.is_absolute() and render_source.parent != render_dir:
            render_target = render_dir / render_source.name
            render_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(render_source, render_target)
            durable_renders.append(render_target.relative_to(project_root).as_posix())
        else:
            durable_renders.append(relative)

    summary: dict[str, Any] = {
        "file_size_bytes": target.stat().st_size,
        "registered_at": datetime.now(UTC).isoformat(),
    }
    manifest = source.parent / "model_manifest.json"
    if manifest.is_file():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                summary.update({key: payload[key] for key in ("room", "reported_floor_area_m2", "modeled_height_m", "fixture_counts") if key in payload})
        except (OSError, ValueError):
            summary["manifest_warning"] = "model_manifest.json 无法读取"

    return BlenderModelAsset(
        model_path=target.relative_to(project_root).as_posix(),
        source_sha256=source_sha256,
        model_sha256=model_hash,
        status="ready",
        reused=previous is not None and previous.model_sha256 == model_hash,
        blender_version=None,
        mcp_status=status,
        render_paths=durable_renders,
        scene_summary=summary,
        message=message,
    )


def _room_dimensions(state: ProjectState) -> tuple[float | None, float | None]:
    brief = state.brief
    if brief.length_m and brief.width_m:
        return brief.length_m, brief.width_m
    plan = state.floor_plan
    if plan and plan.selected_area_candidate_index is not None:
        index = plan.selected_area_candidate_index
        if 0 <= index < len(plan.area_candidates):
            candidate = plan.area_candidates[index]
            if candidate.length_m and candidate.width_m:
                return candidate.length_m, candidate.width_m
    return None, None


def workflow_source_fingerprint(workflow: BlenderWorkflow) -> str | None:
    """Hash the ordered immutable inputs used to build a scene."""

    if not workflow.source_assets:
        return None
    digest = hashlib.sha256()
    for asset in workflow.source_assets:
        digest.update(asset.sha256.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def modeling_missing_fields(state: ProjectState) -> list[str]:
    """Return evidence fields that must be confirmed before geometry creation."""

    missing: list[str] = []
    if not state.blender_workflow.source_assets:
        missing.append("source_file")
    length, width = _room_dimensions(state)
    selected_plan = bool(
        state.floor_plan
        and state.floor_plan.selected_area_candidate_index is not None
    )
    confirmed = state.brief.confirmed_fields
    if not length or not width:
        missing.extend(["length_m", "width_m"])
    elif not selected_plan and not {"length_m", "width_m"}.issubset(confirmed):
        missing.append("confirmed_room_dimensions")
    if not state.brief.room_height_m:
        missing.append("room_height_m")
    elif "room_height_m" not in confirmed:
        missing.append("confirmed_room_height_m")
    return list(dict.fromkeys(missing))


def _selected_outline(state: ProjectState) -> list[list[float]]:
    plan = state.floor_plan
    if not plan or plan.selected_area_candidate_index is None or not plan.meters_per_drawing_unit:
        return []
    index = plan.selected_area_candidate_index
    if not 0 <= index < len(plan.area_candidates):
        return []
    points = plan.area_candidates[index].points
    if len(points) < 3:
        return []
    scale = plan.meters_per_drawing_unit
    converted = [[float(point.x) * scale, float(point.y) * scale] for point in points]
    if len(converted) > 1 and converted[0] == converted[-1]:
        converted.pop()
    min_x = min(point[0] for point in converted)
    min_y = min(point[1] for point in converted)
    return [[round(x - min_x, 5), round(y - min_y, 5)] for x, y in converted[:500]]


def _source_cad_path(state: ProjectState, project_root: Path) -> Path | None:
    """Return the uploaded CAD source, if this project has one."""

    for asset in reversed(state.blender_workflow.source_assets):
        if asset.source_type not in {"dxf", "dwg"}:
            continue
        candidate = _safe_relative_path(project_root, asset.storage_path)
        if candidate.is_file():
            return candidate
    return None


def _cad_geometry_spec(state: ProjectState, project_root: Path) -> dict[str, Any]:
    """Extract bounded furniture, aperture and luminaire hints from DIALux DXF.

    These are visual/context objects, never used as photometric inputs.  The
    parser deliberately keeps only the simple rectangular primitives that are
    stable in DIALux exports so malformed CAD cannot create unbounded Blender
    work.
    """

    source = _source_cad_path(state, project_root)
    if source is None or source.suffix.casefold() != ".dxf":
        return {}
    try:
        import ezdxf
        from shapely import LineString, ops

        document = ezdxf.readfile(source)
        entities = [entity for entity in document.modelspace() if entity.dxftype() == "POLYLINE"]
    except Exception:
        return {}

    def entity_points(entity: Any) -> list[tuple[float, float]]:
        try:
            return [
                (float(vertex.dxf.location.x), float(vertex.dxf.location.y))
                for vertex in entity.vertices
            ]
        except Exception:
            return []

    def line_polygons(layer: str) -> list[Any]:
        lines: list[Any] = []
        for entity in entities:
            if str(entity.dxf.layer).upper() != layer:
                continue
            points = entity_points(entity)
            for start, end in zip(points, points[1:]):
                if start != end:
                    lines.append(LineString([start, end]))
        if not lines:
            return []
        try:
            merged = ops.unary_union(lines)
            return list(ops.polygonize(list(getattr(merged, "geoms", [merged]))))
        except Exception:
            return []

    # Normalize all CAD hints to the same origin as the selected room outline.
    contour_points = [point for entity in entities if str(entity.dxf.layer).upper() == "DLX_CONT" for point in entity_points(entity)]
    origin_x = min((point[0] for point in contour_points), default=0.0)
    origin_y = min((point[1] for point in contour_points), default=0.0)

    def normalized_box(polygon: Any) -> tuple[float, float, float, float] | None:
        x0, y0, x1, y1 = polygon.bounds
        if x1 - x0 < 0.03 or y1 - y0 < 0.03:
            return None
        return x0 - origin_x, x1 - origin_x, y0 - origin_y, y1 - origin_y

    # DIALux emits 72 POLYLINE records per luminaire in its DXF export.  Keep
    # the grouping when present; for other exports, fall back to spatially
    # clustered record centers.  This recovers exact fixture coordinates even
    # when the project has not yet selected a final luminaire candidate.
    lum_entities = [entity for entity in entities if str(entity.dxf.layer).upper() == "DLX_LUM"]
    groups: list[list[Any]] = []
    if lum_entities and len(lum_entities) % 72 == 0:
        groups = [lum_entities[index : index + 72] for index in range(0, len(lum_entities), 72)]
    else:
        centers: list[tuple[float, float, Any]] = []
        for entity in lum_entities:
            points = entity_points(entity)
            if points:
                centers.append((sum(x for x, _ in points) / len(points), sum(y for _, y in points) / len(points), entity))
        clusters: list[list[Any]] = []
        for cx, cy, entity in centers:
            match = next((cluster for cluster in clusters if math.dist((cx, cy), cluster[0]) < 0.34), None)
            if match is None:
                clusters.append([(cx, cy), entity])
            else:
                match.append(entity)
        groups = [[item for item in cluster[1:]] for cluster in clusters]

    fixtures: list[dict[str, Any]] = []
    for index, group in enumerate(groups[:128], start=1):
        points = [point for entity in group for point in entity_points(entity)]
        if not points:
            continue
        x0, x1 = min(x for x, _ in points), max(x for x, _ in points)
        y0, y1 = min(y for _, y in points), max(y for _, y in points)
        dx, dy = x1 - x0, y1 - y0
        if max(dx, dy) < 0.26:
            kind = "spot"
            fixture_width, fixture_length = 0.22, 0.22
        elif max(dx, dy) < 0.95:
            kind = "panel"
            fixture_width, fixture_length = max(0.30, min(dx, dy)), max(0.60, max(dx, dy))
        else:
            continue
        fixtures.append(
            {
                "index": index,
                "x": (x0 + x1) / 2 - origin_x,
                "y": (y0 + y1) / 2 - origin_y,
                "kind": kind,
                "width": round(fixture_width, 4),
                "length": round(fixture_length, 4),
            }
        )

    # Convert the DLX_OBJ linework into one conservative conference-table
    # footprint.  The source contains many small nested strokes, so using the
    # aggregate bounds is more stable than treating every stroke as furniture.
    object_points = [point for entity in entities if str(entity.dxf.layer).upper() == "DLX_OBJ" for point in entity_points(entity)]
    furniture: list[dict[str, Any]] = []
    if object_points:
        x0, x1 = min(x for x, _ in object_points), max(x for x, _ in object_points)
        y0, y1 = min(y for _, y in object_points), max(y for _, y in object_points)
        if x1 - x0 >= 2.0 and y1 - y0 >= 3.0:
            furniture.append(
                {
                    "kind": "conference_table",
                    "x": (x0 + x1) / 2 - origin_x,
                    "y": (y0 + y1) / 2 - origin_y,
                    "length": min(6.4, x1 - x0),
                    "width": min(3.8, y1 - y0),
                }
            )

    apertures: list[dict[str, Any]] = []
    seen_apertures: set[tuple[int, int, int, int]] = set()
    for polygon in sorted(line_polygons("DLX_APERT"), key=lambda item: item.area, reverse=True):
        box = normalized_box(polygon)
        if box is None or polygon.area < 0.008:
            continue
        x0, x1, y0, y1 = box
        key = tuple(round(value * 100) for value in box)
        if key in seen_apertures:
            continue
        seen_apertures.add(key)
        apertures.append({"x": (x0 + x1) / 2, "y": (y0 + y1) / 2, "length": x1 - x0, "width": y1 - y0})
        if len(apertures) >= 48:
            break
    return {"fixtures": fixtures, "furniture": furniture, "apertures": apertures}


def _fixture_count(state: ProjectState, length: float, width: float) -> int:
    # Preserve source layout count when a DIALux CAD plan exposes discrete
    # luminaire symbols.  A final lighting proposal still replaces these by
    # its own calculated array after the user confirms a catalogue product.
    # The caller may have no selected final luminaire during the initial model.
    latest_by_group: dict[str, int] = {}
    for calculation in state.calculations:
        latest_by_group[calculation.group_id] = calculation.luminaire_count
    if latest_by_group:
        return max(1, min(64, sum(latest_by_group.values())))
    selected_flux = [
        item.luminous_flux_lm
        for item in state.selected_luminaires()
        if item.luminous_flux_lm and item.luminous_flux_lm > 0
    ]
    target = state.brief.target_illuminance_lx
    if selected_flux and target:
        estimate = math.ceil(target * length * width / (sum(selected_flux) / len(selected_flux) * 0.6 * 0.8))
        return max(1, min(64, estimate))
    return 4


def _model_outline(state: ProjectState) -> list[list[float]]:
    """Use the selected CAD contour only when it agrees with confirmed room size."""

    outline = _selected_outline(state)
    if len(outline) < 3:
        return []
    length, width = _room_dimensions(state)
    if not length or not width:
        return []
    span_x = max(point[0] for point in outline) - min(point[0] for point in outline)
    span_y = max(point[1] for point in outline) - min(point[1] for point in outline)
    # FloorPlan candidates historically label the x span as ``length_m``.
    # Accept either orientation but reject the old 0.0254-scaled outline.
    target_pairs = ((length, width), (width, length))
    if any(abs(span_x - expected_x) <= max(0.25, expected_x * 0.08) and abs(span_y - expected_y) <= max(0.25, expected_y * 0.08) for expected_x, expected_y in target_pairs):
        return outline
    return []


def _modeling_parameters(parameters: BlenderWorkflowParameters) -> dict[str, float]:
    return {
        "workplane_height_m": parameters.workplane_height_m,
        "grid_spacing_m": parameters.grid_spacing_m,
        "floor_reflectance": parameters.floor_reflectance if parameters.floor_reflectance is not None else 0.2,
        "wall_reflectance": parameters.wall_reflectance if parameters.wall_reflectance is not None else 0.5,
        "ceiling_reflectance": parameters.ceiling_reflectance if parameters.ceiling_reflectance is not None else 0.7,
    }


def _scene_script(spec: dict[str, Any]) -> str:
    """Build the trusted Blender Python program for one project snapshot."""

    payload = json.dumps(spec, ensure_ascii=False, separators=(",", ":"))
    return f'''import bpy, json, math, os
from mathutils import Vector

spec = json.loads({payload!r})
if bpy.context.object and bpy.context.object.mode != 'OBJECT':
    try:
        bpy.ops.object.mode_set(mode='OBJECT')
    except Exception:
        pass
# Build in a project-specific scene so opening the workflow never deletes a
# user's current Blender scene or unsaved modelling work.  A previous scene
# generated for the same project is discarded; unrelated scenes and objects
# remain available in the running Blender session.
project_scene_name = f"Lighting Agent - {{spec['project_id']}}"
for old_scene in list(bpy.data.scenes):
    if old_scene.get('lighting_agent_project_id') != spec['project_id']:
        continue
    if old_scene == bpy.context.scene:
        fallback_scene = next((candidate for candidate in bpy.data.scenes if candidate != old_scene), None)
        if fallback_scene is None:
            continue
        if bpy.context.window:
            bpy.context.window.scene = fallback_scene
    bpy.data.scenes.remove(old_scene)
scene = bpy.data.scenes.new(project_scene_name)
if bpy.context.window:
    bpy.context.window.scene = scene
if scene.world is None:
    scene.world = bpy.data.worlds.new(f"{{project_scene_name}} World")
bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
for datablocks in (bpy.data.meshes, bpy.data.curves, bpy.data.materials, bpy.data.cameras, bpy.data.lights):
    for block in list(datablocks):
        if block.users == 0:
            datablocks.remove(block)

scene.render.engine = 'BLENDER_WORKBENCH'
scene.display.shading.light = 'STUDIO'
scene.display.shading.studio_light = 'paint.sl'
scene.display.shading.show_shadows = False
scene.display.shading.show_cavity = True
scene.display.shading.cavity_type = 'WORLD'
scene.display.shading.color_type = 'MATERIAL'
scene.render.resolution_x = 800
scene.render.resolution_y = 520
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = 'PNG'
scene.render.film_transparent = False
scene.render.image_settings.color_mode = 'RGBA'
scene.world.color = (0.035, 0.04, 0.05)
scene['lighting_agent_project_id'] = spec['project_id']
scene['lighting_agent_scene'] = True
scene['lighting_agent_source_sha256'] = spec['source_sha256']
scene['lighting_agent_selected_luminaires'] = json.dumps(spec['selected_luminaire_ids'])
scene['lighting_agent_preliminary_only'] = True

def material(name, color, emission=None):
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = (*color, 1.0)
    mat.use_nodes = True
    shader = next(node for node in mat.node_tree.nodes if node.type == 'BSDF_PRINCIPLED')
    shader.inputs['Base Color'].default_value = (*color, 1.0)
    shader.inputs['Roughness'].default_value = 0.65
    if emission:
        shader.inputs['Emission Color'].default_value = (*emission, 1.0)
        shader.inputs['Emission Strength'].default_value = 3.0
    return mat

def attach_photometry(light_data, photometry_path):
    """Attach a real IES profile to the Blender light when Blender supports it.

    Workbench renders below intentionally remain fast and deterministic, but
    keeping the IES node in the saved light data means the project model can
    be switched to Cycles/Eevee later without losing the selected profile.
    LDT/ULD files are still retained as auditable metadata and are evaluated
    by the web-side preliminary solver when their format is supported.
    """
    if not photometry_path or not os.path.isfile(photometry_path):
        return False
    if not str(photometry_path).lower().endswith('.ies'):
        light_data['photometry_path'] = photometry_path
        light_data['photometry_embedded'] = False
        light_data['photometry_note'] = '保留配光路径；当前 Blender 节点仅尝试嵌入 IES。'
        return False
    try:
        light_data.use_nodes = True
        nodes = light_data.node_tree.nodes
        links = light_data.node_tree.links
        nodes.clear()
        output = nodes.new(type='ShaderNodeOutputLight')
        emission = nodes.new(type='ShaderNodeEmission')
        ies = nodes.new(type='ShaderNodeTexIES')
        ies.filepath = photometry_path
        links.new(ies.outputs.get('Factor'), emission.inputs.get('Strength'))
        links.new(emission.outputs.get('Emission'), output.inputs.get('Surface'))
        light_data['photometry_path'] = photometry_path
        light_data['photometry_embedded'] = True
        return True
    except Exception as error:
        light_data['photometry_path'] = photometry_path
        light_data['photometry_embedded'] = False
        light_data['photometry_note'] = str(error)[:500]
        return False

floor_mat = material('Floor reflectance', (0.18, 0.2, 0.22))
wall_mat = material('Wall reflectance', (0.72, 0.75, 0.77))
ceiling_mat = material('Ceiling reflectance', (0.88, 0.89, 0.9))
fixture_mat = material('Luminaire diffuser', (0.72, 0.76, 0.78), (1.0, 0.93, 0.78))
workplane_mat = material('Workplane', (0.12, 0.42, 0.46))

def cube(name, location, dimensions, mat=None):
    bpy.ops.mesh.primitive_cube_add(location=location)
    obj = bpy.context.object
    obj.name = name
    obj.dimensions = dimensions
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    if mat:
        obj.data.materials.append(mat)
    return obj

length = float(spec['length_m'])
width = float(spec['width_m'])
height = float(spec['height_m'])
outline = spec.get('outline') or [[0, 0], [width, 0], [width, length], [0, length]]
vertices = [(float(x), float(y), 0.0) for x, y in outline]
floor_mesh = bpy.data.meshes.new('Room floor mesh')
floor_mesh.from_pydata(vertices, [], [list(range(len(vertices)))])
floor = bpy.data.objects.new('Room floor', floor_mesh)
bpy.context.collection.objects.link(floor)
floor.data.materials.append(floor_mat)

wall_thickness = max(0.06, min(width, length) * 0.012)
min_y = min(point[1] for point in outline)
max_y = max(point[1] for point in outline)
for index, start in enumerate(outline):
    end = outline[(index + 1) % len(outline)]
    dx, dy = end[0] - start[0], end[1] - start[1]
    segment = math.hypot(dx, dy)
    if segment < 0.05:
        continue
    wall = cube(
        f'Wall {{index + 1:02d}}',
        ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2, height / 2),
        (segment + wall_thickness, wall_thickness, height),
        wall_mat,
    )
    wall.rotation_euler[2] = math.atan2(dy, dx)
    wall['reflectance'] = spec['reflectances']['wall']
    if (start[1] + end[1]) / 2 <= min_y + (max_y - min_y) * 0.04:
        wall.hide_render = True

ceiling = bpy.data.objects.new('Ceiling', floor_mesh.copy())
bpy.context.collection.objects.link(ceiling)
ceiling.location.z = height
ceiling.data.materials.append(ceiling_mat)
ceiling.hide_render = True
ceiling['reflectance'] = spec['reflectances']['ceiling']
floor['reflectance'] = spec['reflectances']['floor']

workplane_height = float(spec['workplane_height_m'])
workplane = cube(
    'Workplane',
    (width / 2, length / 2, workplane_height),
    (max(0.1, width - 0.18), max(0.1, length - 0.18), 0.012),
    workplane_mat,
)
workplane['grid_spacing_m'] = spec['grid_spacing_m']
workplane.hide_render = True

grid_objects = []
spacing = max(0.1, float(spec['grid_spacing_m']))
for index in range(1, min(40, int(width / spacing))):
    line = cube(f'Grid X {{index:02d}}', (index * spacing, length / 2, workplane_height + 0.01), (0.012, length, 0.008), workplane_mat)
    line.hide_render = True
    grid_objects.append(line)
for index in range(1, min(40, int(length / spacing))):
    line = cube(f'Grid Y {{index:02d}}', (width / 2, index * spacing, workplane_height + 0.01), (width, 0.012, 0.008), workplane_mat)
    line.hide_render = True
    grid_objects.append(line)

fixture_count = int(spec['fixture_count'])
columns = max(1, int(math.ceil(math.sqrt(fixture_count * width / max(length, 0.01)))))
rows = max(1, int(math.ceil(fixture_count / columns)))
luminaires = spec.get('luminaires') or [{{'id': 'generic', 'name': 'Generic placeholder', 'flux_lm': None, 'power_w': None, 'cct_k': 4000}}]
for index in range(fixture_count):
    row, column = divmod(index, columns)
    x = (column + 0.5) * width / columns
    y = (row + 0.5) * length / rows
    luminaire = luminaires[index % len(luminaires)]
    body = cube(f"Luminaire {{index + 1:02d}} - {{luminaire['name']}}", (x, y, height - 0.07), (0.58, 0.58, 0.08), fixture_mat)
    body['luminaire_id'] = luminaire['id']
    body['article_name'] = luminaire['name']
    body['luminous_flux_lm'] = luminaire.get('flux_lm') or 0
    body['power_w'] = luminaire.get('power_w') or 0
    body['photometry_path'] = luminaire.get('photometry_path') or ''
    lamp_data = bpy.data.lights.new(f"Light {{index + 1:02d}}", type='AREA')
    lamp_data.shape = 'DISK'
    lamp_data.size = 0.48
    flux = luminaire.get('flux_lm') or 3200
    lamp_data.energy = max(40.0, min(900.0, float(flux) * 0.10))
    cct = int(luminaire.get('cct_k') or 4000)
    lamp_data.color = (1.0, 0.88, 0.72) if cct < 3500 else ((0.88, 0.94, 1.0) if cct > 5000 else (1.0, 0.96, 0.88))
    attach_photometry(lamp_data, luminaire.get('photometry_path'))
    lamp = bpy.data.objects.new(lamp_data.name, lamp_data)
    lamp.location = (x, y, height - 0.12)
    bpy.context.collection.objects.link(lamp)

fill_data = bpy.data.lights.new('Render fill', type='AREA')
fill_data.energy = 450
fill_data.shape = 'RECTANGLE'
fill_data.size = max(width, length) * 0.7
fill = bpy.data.objects.new('Render fill', fill_data)
fill.location = (width / 2, -max(width, length) * 0.18, height * 0.72)
fill.rotation_euler = (math.radians(64), 0, 0)
bpy.context.collection.objects.link(fill)

camera_data = bpy.data.cameras.new('Lighting Agent Camera')
camera = bpy.data.objects.new('Lighting Agent Camera', camera_data)
bpy.context.collection.objects.link(camera)
scene.camera = camera

def point_camera(location, target, orthographic=False):
    camera.location = location
    direction = Vector(target) - camera.location
    camera.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()
    camera.data.type = 'ORTHO' if orthographic else 'PERSP'

diagonal = math.hypot(width, length)
camera.data.lens = 38
point_camera((width * 0.95, -diagonal * 0.68, height * 0.82), (width / 2, length / 2, height * 0.38))
bpy.ops.wm.save_as_mainfile(filepath=spec['model_path'])
scene.render.filepath = spec['render_room_path']
bpy.ops.render.render(write_still=True)

workplane.hide_render = False
for line in grid_objects:
    line.hide_render = False
camera.data.ortho_scale = max(width, length) * 1.16
point_camera((width / 2, length / 2, height + diagonal), (width / 2, length / 2, 0), orthographic=True)
scene.render.filepath = spec['render_plan_path']
bpy.ops.render.render(write_still=True)

workplane.hide_render = True
for line in grid_objects:
    line.hide_render = True
bpy.ops.wm.save_as_mainfile(filepath=spec['model_path'])
print(json.dumps({{'blender_version': bpy.app.version_string, 'objects': len(scene.objects), 'fixture_count': fixture_count}}))
'''


def _photometry_path_by_luminaire(project_root: Path, state: ProjectState) -> dict[str, str]:
    manifest = project_root / f"{state.project_id}.photometry" / "manifest.json"
    if not manifest.is_file():
        return {}
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    records = payload.get("assets", {}) if isinstance(payload, dict) else {}
    paths: dict[str, str] = {}
    for luminaire_id in state.selected_luminaire_ids:
        record = records.get(luminaire_id, {}) if isinstance(records, dict) else {}
        for extracted in record.get("extracted_files", []) if isinstance(record, dict) else []:
            if extracted.get("file_type") not in {"ies", "ldt"}:
                continue
            candidate = (manifest.parent / str(extracted.get("relative_path", ""))).resolve()
            if manifest.parent.resolve() in candidate.parents and candidate.is_file():
                paths[luminaire_id] = str(candidate)
                break
    return paths


def _photometry_snapshot(paths: dict[str, str]) -> dict[str, str]:
    """Return stable hashes for the photometry files used by a model.

    A saved model must be rebuilt when a previously unavailable IES/LDT file
    becomes available, or when a supplier replaces the file behind the same
    luminaire id.  Hashing the small local files makes that decision explicit
    while preserving the fast model-reuse path for unchanged projects.
    """

    snapshot: dict[str, str] = {}
    for luminaire_id, raw_path in sorted(paths.items()):
        path = Path(raw_path)
        try:
            snapshot[luminaire_id] = sha256_file(path)
        except (OSError, ValueError):
            # Keep an unreadable path distinguishable from a model that was
            # built without any photometry at all.  The subsequent build will
            # still retain the path in scene metadata and report the warning.
            snapshot[luminaire_id] = "unreadable"
    return snapshot


def create_or_reuse_blender_model(
    state: ProjectState,
    *,
    project_root: Path,
    project_id: str,
    force: bool = False,
) -> BlenderModelAsset:
    """Create a real Blender scene, or reuse the matching saved snapshot."""

    missing = modeling_missing_fields(state)
    if missing:
        raise BlenderWorkflowError("建模证据不足，请确认：" + "、".join(missing))
    source_sha = workflow_source_fingerprint(state.blender_workflow)
    assert source_sha is not None
    selected_ids = list(state.selected_luminaire_ids)
    existing = state.blender_workflow.model
    photometry = _photometry_path_by_luminaire(project_root, state)
    photometry_snapshot = _photometry_snapshot(photometry)
    model_parameters = _modeling_parameters(state.blender_workflow.parameters)
    if existing and existing.status == "ready" and not force:
        model_file = _safe_relative_path(project_root, existing.model_path)
        renders_exist = existing.render_paths and all(
            _safe_relative_path(project_root, relative).is_file()
            for relative in existing.render_paths
        )
        matching_luminaires = existing.scene_summary.get("selected_luminaire_ids", []) == selected_ids
        matching_parameters = existing.scene_summary.get("modeling_parameters") == model_parameters
        matching_photometry = existing.scene_summary.get("photometry_snapshot", {}) == photometry_snapshot
        if model_file.is_file() and renders_exist and existing.source_sha256 == source_sha and matching_luminaires and matching_parameters and matching_photometry:
            status, message = probe_blender()
            return existing.model_copy(update={"reused": True, "mcp_status": status, "message": "已复用与当前资料及灯具匹配的模型。" if status == "connected" else message})

    length, width = _room_dimensions(state)
    assert length is not None and width is not None and state.brief.room_height_m is not None
    root = workflow_root(project_root, project_id)
    model_dir = root / "model"
    render_dir = root / "renders"
    model_dir.mkdir(parents=True, exist_ok=True)
    render_dir.mkdir(parents=True, exist_ok=True)
    model_path = model_dir / f"{project_id}.blend"
    ensure_blender_running(open_file=model_path if model_path.is_file() else None)
    cad_geometry = _cad_geometry_spec(state, project_root)
    source_fixtures = cad_geometry.get("fixtures", []) if isinstance(cad_geometry, dict) else []
    outline = _model_outline(state)
    model_width = max((point[0] for point in outline), default=width) - min((point[0] for point in outline), default=0.0) if outline else width
    model_length = max((point[1] for point in outline), default=length) - min((point[1] for point in outline), default=0.0) if outline else length

    luminaires = [
        {
            "id": item.luminaire_id,
            "name": item.article_name,
            "flux_lm": item.luminous_flux_lm,
            "power_w": item.power_w,
            "cct_k": item.cct_k or state.brief.target_cct_k,
            "photometry_path": photometry.get(item.luminaire_id),
        }
        for item in state.selected_luminaires()
    ]
    parameters = state.blender_workflow.parameters
    render_room = render_dir / "render-room.png"
    render_plan = render_dir / "render-workplane.png"
    spec = {
        "project_id": project_id,
        "source_sha256": source_sha,
        "selected_luminaire_ids": selected_ids,
        "length_m": model_length,
        "width_m": model_width,
        "height_m": state.brief.room_height_m,
        "outline": outline,
        "workplane_height_m": parameters.workplane_height_m,
        "grid_spacing_m": parameters.grid_spacing_m,
        "reflectances": {
            "floor": parameters.floor_reflectance if parameters.floor_reflectance is not None else 0.2,
            "wall": parameters.wall_reflectance if parameters.wall_reflectance is not None else 0.5,
            "ceiling": parameters.ceiling_reflectance if parameters.ceiling_reflectance is not None else 0.7,
        },
        "fixture_count": len(source_fixtures) or _fixture_count(state, length, width),
        "cad_geometry": cad_geometry,
        "luminaires": luminaires,
        "model_path": str(model_path.resolve()),
        "render_room_path": str(render_room.resolve()),
        "render_plan_path": str(render_plan.resolve()),
    }
    result = mcp_request("execute_code", {"code": _scene_script(spec)}, timeout=240.0)
    if not model_path.is_file() or not render_room.is_file() or not render_plan.is_file():
        raise BlenderWorkflowError("Blender MCP 已返回，但模型或渲染图未完整写入项目目录")
    try:
        addon_info = mcp_request("get_addon_info", timeout=5.0)
    except BlenderConnectionError:
        addon_info = {}
    scene_summary = {
        "generated_by": "blender_mcp.execute_code",
        "source_sha256": source_sha,
        "selected_luminaire_ids": selected_ids,
        "fixture_count": spec["fixture_count"],
        "cad_geometry_counts": {
            "fixtures": len(source_fixtures),
            "furniture": len(cad_geometry.get("furniture", [])) if isinstance(cad_geometry, dict) else 0,
            "apertures": len(cad_geometry.get("apertures", [])) if isinstance(cad_geometry, dict) else 0,
        },
        "room": {"length_m": length, "width_m": width, "height_m": state.brief.room_height_m},
        "workplane_height_m": parameters.workplane_height_m,
        "photometry_luminaire_ids": sorted(photometry),
        "photometry_snapshot": photometry_snapshot,
        "modeling_parameters": model_parameters,
        "mcp_result": result.get("result", "")[-1_000:] if isinstance(result.get("result"), str) else result,
    }
    return BlenderModelAsset(
        model_path=model_path.relative_to(project_root).as_posix(),
        source_sha256=source_sha,
        model_sha256=sha256_file(model_path),
        status="ready",
        reused=False,
        blender_version=str(addon_info.get("blender_version") or "unknown"),
        mcp_status="connected",
        render_paths=[
            render_room.relative_to(project_root).as_posix(),
            render_plan.relative_to(project_root).as_posix(),
        ],
        scene_summary=scene_summary,
        message="Blender MCP 已完成建模、渲染并保存可复用模型。",
    )


def _effective_flux(
    state: ProjectState,
    parameters: BlenderWorkflowParameters,
    *,
    length: float,
    width: float,
) -> float | None:
    if parameters.total_flux_lm:
        return parameters.total_flux_lm
    selected = state.selected_luminaires()
    values = [item.luminous_flux_lm for item in selected if item.luminous_flux_lm and item.luminous_flux_lm > 0]
    if values:
        return float(sum(values) / len(values) * _fixture_count(state, length, width))
    if state.calculations:
        # The latest lumen-method result records the required flux, which is a
        # useful provisional total when no catalogue flux is available.
        latest = state.calculations[-1]
        if latest.required_luminous_flux_lm > 0:
            return latest.required_luminous_flux_lm
    return None


def _photometric_estimate(
    state: ProjectState,
    parameters: BlenderWorkflowParameters,
    *,
    project_root: Path,
    project_id: str,
    length: float,
    width: float,
) -> BlenderEstimate | None:
    paths = _photometry_path_by_luminaire(project_root, state)
    selected = state.selected_luminaires()
    source_luminaire = next((item for item in selected if item.luminaire_id in paths), None)
    if source_luminaire is None:
        return None
    try:
        from .calculations.photometry import parse_photometry_file
        from .calculations.preview import IlluminancePreviewRequest, compute_illuminance_preview

        source_path = Path(paths[source_luminaire.luminaire_id])
        distribution = parse_photometry_file(source_path)
        fixture_count = _fixture_count(state, length, width)
        fixture_columns = max(1, int(math.ceil(math.sqrt(fixture_count * width / max(length, 0.01)))))
        fixture_rows = max(1, int(math.ceil(fixture_count / fixture_columns)))
        while fixture_rows * fixture_columns < fixture_count:
            fixture_rows += 1
        per_fixture_flux = source_luminaire.luminous_flux_lm
        if per_fixture_flux is None and parameters.total_flux_lm is not None:
            per_fixture_flux = parameters.total_flux_lm / fixture_count
        result = compute_illuminance_preview(
            distribution,
            IlluminancePreviewRequest(
                luminaire_id=source_luminaire.luminaire_id,
                fixture_rows=fixture_rows,
                fixture_columns=fixture_columns,
                fixture_count=fixture_count,
                room_length_m=length,
                room_width_m=width,
                workplane_height_m=parameters.workplane_height_m,
                mounting_height_m=state.brief.room_height_m,
                maintenance_factor=parameters.maintenance_factor,
                utilization_factor=parameters.utilization_factor,
                total_flux_lm=per_fixture_flux,
            ),
        )
    except Exception:
        # A malformed supplier file falls back to the explicitly labelled
        # point-source estimate below; it is never reported as successful IES use.
        return None

    heatmap = _heatmap_path(
        project_root,
        project_id,
        result.grid_x_coordinates_m,
        result.grid_y_coordinates_m,
        result.illuminance_lx,
        title=f"Preliminary illuminance - {source_luminaire.article_name}",
    )
    assumptions = [
        f"照度分布采用 {source_path.name} 的真实 {source_path.suffix[1:].upper()} 坎德拉表。",
        *result.assumptions,
        f"工作面高度取 {parameters.workplane_height_m:g} m；灯具安装高度取 {state.brief.room_height_m:g} m。",
    ]
    if len(selected) > 1:
        assumptions.append("当前方案包含多款灯具；趋势网格以首款具有可解析配光的基础照明灯具代表。")
    limitations = [
        *result.limitations,
        "反射率已记录用于方案上下文，但本次网格未执行完整光线追踪或可靠 UGR 计算。",
    ]
    average = result.average_illuminance_lx
    uniformity = result.minimum_illuminance_lx / average if average > 0 else None
    return BlenderEstimate(
        solver_version=f"{SOLVER_VERSION}+ies-ldt",
        status="succeeded",
        input_project_revision=state.revision,
        area_m2=round(length * width, 3),
        grid_rows=result.grid_rows,
        grid_columns=result.grid_columns,
        grid_x_coordinates_m=result.grid_x_coordinates_m,
        grid_y_coordinates_m=result.grid_y_coordinates_m,
        illuminance_lx=result.illuminance_lx,
        average_illuminance_lx=average,
        minimum_illuminance_lx=result.minimum_illuminance_lx,
        maximum_illuminance_lx=result.maximum_illuminance_lx,
        uniformity_u0=round(uniformity, 4) if uniformity is not None else None,
        heatmap_path=heatmap or None,
        assumptions=assumptions,
        limitations=limitations,
        message="已基于下载的 IES/LDT 配光生成方案级照度趋势。",
    )


def _grid_axis(size: float, margin: float, spacing: float) -> list[float]:
    if size <= 0:
        return []
    margin = min(max(0.0, margin), max(0.0, size / 2 - 0.01))
    usable = max(0.02, size - 2 * margin)
    count = max(2, min(80, int(math.ceil(usable / spacing)) + 1))
    if count == 1:
        return [size / 2]
    return [margin + index * usable / (count - 1) for index in range(count)]


def _heatmap_path(
    project_root: Path,
    project_id: str,
    xs: list[float],
    ys: list[float],
    values: list[list[float]],
    *,
    title: str,
) -> str:
    root = workflow_root(project_root, project_id)
    target = root / "renders" / "illuminance-heatmap.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        figure, axis = plt.subplots(figsize=(10, 6), dpi=140)
        image = axis.imshow(
            values,
            origin="lower",
            extent=(min(xs), max(xs), min(ys), max(ys)),
            aspect="auto",
            cmap="viridis",
        )
        figure.colorbar(image, ax=axis, label="Illuminance (lx)")
        axis.set_xlabel("X (m)")
        axis.set_ylabel("Y (m)")
        axis.set_title(title)
        figure.tight_layout()
        figure.savefig(target)
        plt.close(figure)
    except Exception:
        return ""
    return target.relative_to(project_root).as_posix()


def estimate_workplane(
    state: ProjectState,
    parameters: BlenderWorkflowParameters,
    *,
    project_root: Path,
    project_id: str,
    allow_provisional: bool = True,
) -> BlenderEstimate:
    """Produce a deterministic, labelled preliminary work-plane estimate."""

    length, width = _room_dimensions(state)
    current_revision = state.revision
    if not length or not width:
        return BlenderEstimate(
            solver_version=SOLVER_VERSION,
            status="blocked",
            input_project_revision=current_revision,
            assumptions=[],
            limitations=["缺少已确认的房间长宽或可换算的图纸边界。"],
            message="请先确认图纸房间边界，或在项目任务书中填写 length_m / width_m。",
        )

    assumptions: list[str] = []
    uf = parameters.utilization_factor
    mf = parameters.maintenance_factor
    if uf is None:
        if not allow_provisional:
            return BlenderEstimate(
                solver_version=SOLVER_VERSION,
                status="blocked",
                input_project_revision=current_revision,
                area_m2=length * width,
                assumptions=[],
                limitations=["利用系数尚未确认。"],
                message="请确认利用系数 UF。",
            )
        uf = 0.60
        assumptions.append("UF 未确认，暂采用 0.60 进行方案估算。")
    if mf is None:
        if not allow_provisional:
            return BlenderEstimate(
                solver_version=SOLVER_VERSION,
                status="blocked",
                input_project_revision=current_revision,
                area_m2=length * width,
                assumptions=[],
                limitations=["维护系数尚未确认。"],
                message="请确认维护系数 MF。",
            )
        mf = 0.80
        assumptions.append("MF 未确认，暂采用 0.80 进行方案估算。")
    if parameters.floor_reflectance is None or parameters.wall_reflectance is None or parameters.ceiling_reflectance is None:
        assumptions.append("地面/墙面/顶棚反射率尚未全部确认；本次结果未进行可靠的反射计算。")

    photometric = _photometric_estimate(
        state,
        parameters,
        project_root=project_root,
        project_id=project_id,
        length=length,
        width=width,
    )
    if photometric is not None:
        return photometric

    flux = _effective_flux(state, parameters, length=length, width=width)
    if not flux or flux <= 0:
        return BlenderEstimate(
            solver_version=SOLVER_VERSION,
            status="blocked",
            input_project_revision=current_revision,
            area_m2=length * width,
            assumptions=[],
            limitations=["缺少已确认灯具光通量。"],
            message="请通过 DIALux 灯具搜索下载/确认灯具光通量，或填写方案总光通量。",
        )

    xs = _grid_axis(width, parameters.grid_margin_m, parameters.grid_spacing_m)
    ys = _grid_axis(length, parameters.grid_margin_m, parameters.grid_spacing_m)
    if not xs or not ys:
        raise BlenderWorkflowError("无法建立工作面网格")

    # The selected-id list describes luminaire *types*, not the number of
    # installed fixtures.  Use the same auditable array count as the model
    # builder and lumen-method fallback so a single selected panel does not
    # collapse a 20-fixture scheme into one point source.
    fixture_count = _fixture_count(state, length, width)
    # Use a regular array as a transparent fallback when Blender scene
    # metadata does not expose fixture coordinates to the web service.
    fixture_columns = max(1, int(math.ceil(math.sqrt(fixture_count * width / max(length, 0.01)))))
    fixture_rows = max(1, int(math.ceil(fixture_count / fixture_columns)))
    fixture_positions = [
        ((column + 0.5) * width / fixture_columns, (row + 0.5) * length / fixture_rows)
        for row in range(fixture_rows)
        for column in range(fixture_columns)
    ][:fixture_count]
    mounting = state.brief.room_height_m or max(parameters.workplane_height_m + 2.4, 2.7)
    emission_height = max(0.2, mounting - parameters.workplane_height_m)
    flux_per_fixture = flux / fixture_count
    direct: list[list[float]] = []
    for y in ys:
        row: list[float] = []
        for x in xs:
            value = 0.0
            for fx, fy in fixture_positions:
                dx, dy = fx - x, fy - y
                distance_sq = dx * dx + dy * dy + emission_height * emission_height
                cos_theta = emission_height / math.sqrt(distance_sq)
                # Isotropic point-source approximation, then calibrated to UF.
                value += flux_per_fixture / (4 * math.pi) * cos_theta / distance_sq
            row.append(value)
        direct.append(row)

    direct_flat = [value for row in direct for value in row]
    direct_average = sum(direct_flat) / len(direct_flat)
    target_average = flux * uf * mf / (length * width)
    scale = target_average / direct_average if direct_average > 1e-9 else 1.0
    values = [[round(value * scale, 2) for value in row] for row in direct]
    flat = [value for row in values for value in row]
    average = sum(flat) / len(flat)
    minimum, maximum = min(flat), max(flat)
    uniformity = minimum / average if average > 0 else None
    heatmap = _heatmap_path(
        project_root,
        project_id,
        xs,
        ys,
        values,
        title="Preliminary work-plane illuminance (approximate)",
    )
    assumptions.extend(
        [
            f"工作面高度取 {parameters.workplane_height_m:g} m；灯具安装高度取 {mounting:g} m。",
            f"总光通量取 {flux:g} lm，灯具按 {fixture_count} 个规则阵列估算。",
            "直接点照度分量按规则阵列计算，并以 UF/MF 对平均值做整体校准。",
        ]
    )
    limitations = [
        "这是 Blender/智能体方案估算，不是 DIALux 或其他专业照明软件的合规仿真。",
        "未包含真实 IES/LDT 配光、家具遮挡、精确反射、日光动态和 UGR。",
        "正式设计仍需在 DIALux evo 或等效软件中复核。",
    ]
    return BlenderEstimate(
        solver_version=SOLVER_VERSION,
        status="succeeded",
        input_project_revision=current_revision,
        area_m2=round(length * width, 3),
        grid_rows=len(ys),
        grid_columns=len(xs),
        grid_x_coordinates_m=[round(value, 3) for value in xs],
        grid_y_coordinates_m=[round(value, 3) for value in ys],
        illuminance_lx=values,
        average_illuminance_lx=round(average, 2),
        minimum_illuminance_lx=round(minimum, 2),
        maximum_illuminance_lx=round(maximum, 2),
        uniformity_u0=round(uniformity, 4) if uniformity is not None else None,
        heatmap_path=heatmap or None,
        assumptions=assumptions,
        limitations=limitations,
    )


def _display_path(project_root: Path, relative: str | None) -> Path | None:
    if not relative:
        return None
    try:
        path = _safe_relative_path(project_root, relative)
    except BlenderWorkflowError:
        return None
    return path if path.is_file() else None


def build_workflow_report_markdown(state: ProjectState, project_root: Path) -> str:
    workflow = state.blender_workflow
    brief = state.brief
    estimate = workflow.estimate
    lines = [
        f"# Blender 照明优化方案：{brief.project_name}",
        "",
        f"- 项目 ID：`{state.project_id}`",
        f"- 项目版本：`{state.revision}`",
        f"- 报告时间：{datetime.now(UTC).isoformat()}",
        "- 结论性质：方案估算 / 近似照度预览，不是 DIALux 合规结果",
        "",
        "## 工作流节点",
        "",
        "| 节点 | 状态 | 说明 |",
        "| --- | --- | --- |",
    ]
    for node in workflow.nodes:
        lines.append(f"| {node.title} | {node.status} | {node.message or node.description} |")

    lines.extend(["", "## 输入资料", ""])
    if workflow.source_assets:
        for source in workflow.source_assets:
            lines.append(f"- {source.source_name}（{source.source_type.upper()}，SHA-256 `{source.sha256}`）")
            if source.extracted_text_preview:
                preview = " ".join(source.extracted_text_preview.split())[:500]
                lines.append(f"  - 文本摘录：{preview}")
    else:
        lines.append("尚未上传图纸或设计报告。")

    lines.extend(["", "## Blender 模型", ""])
    if workflow.model:
        lines.extend(
            [
                f"- 模型文件：`{workflow.model.model_path}`",
                f"- 模型状态：{workflow.model.status}；是否复用：{'是' if workflow.model.reused else '否'}",
                f"- MCP 状态：{workflow.model.mcp_status}",
                f"- 模型 SHA-256：`{workflow.model.model_sha256 or '—'}`",
            ]
        )
        if workflow.model.render_paths:
            lines.extend(f"- 三维渲染图：`{path}`" for path in workflow.model.render_paths)
        else:
            lines.append("- 三维渲染图：尚未生成")
    else:
        lines.append("尚未登记 Blender 模型。")

    lines.extend(["", "## 优化后的灯具方案", ""])
    selected = state.selected_luminaires()
    if selected:
        lines.extend(["| 灯具 | 品牌 | 功率 | 光通量 | 配光 |", "| --- | --- | --- | --- | --- |"])
        photometry_ids = set(workflow.model.scene_summary.get("photometry_luminaire_ids", [])) if workflow.model else set()
        for luminaire in selected:
            lines.append(
                "| "
                + " | ".join(
                    (
                        luminaire.article_name,
                        luminaire.brand_name or "未提供",
                        f"{luminaire.power_w:g} W" if luminaire.power_w is not None else "未提供",
                        f"{luminaire.luminous_flux_lm:g} lm" if luminaire.luminous_flux_lm is not None else "未提供",
                        "IES/LDT 已写入模型上下文" if luminaire.luminaire_id in photometry_ids else "待核验",
                    )
                )
                + " |"
            )
    else:
        lines.append("尚未确认最终灯具，不能形成优化后方案。")

    lines.extend(["", "## 照度估算参数", "", "| 参数 | 值 |", "| --- | --- |"])
    params = workflow.parameters
    for label, value in (
        ("工作面高度 (m)", params.workplane_height_m),
        ("网格间距 (m)", params.grid_spacing_m),
        ("网格边距 (m)", params.grid_margin_m),
        ("利用系数 UF", params.utilization_factor),
        ("维护系数 MF", params.maintenance_factor),
        ("地面反射率", params.floor_reflectance),
        ("墙面反射率", params.wall_reflectance),
        ("顶棚反射率", params.ceiling_reflectance),
        ("总光通量 (lm)", params.total_flux_lm),
    ):
        lines.append(f"| {label} | {'—' if value is None else value} |")

    lines.extend(["", "## 照度分析", ""])
    if estimate and estimate.status == "succeeded":
        lines.extend(
            [
                f"- 平均照度：{estimate.average_illuminance_lx:g} lx",
                f"- 最小照度：{estimate.minimum_illuminance_lx:g} lx",
                f"- 最大照度：{estimate.maximum_illuminance_lx:g} lx",
                f"- 均匀度 U0（近似）：{estimate.uniformity_u0 if estimate.uniformity_u0 is not None else '—'}",
                f"- 工作面网格：{estimate.grid_rows} 行 × {estimate.grid_columns} 列",
                f"- 照度截图：`{estimate.heatmap_path or '尚未生成'}`",
            ]
        )
    elif estimate:
        lines.append(f"估算被阻塞：{estimate.message or '请补充输入。'}")
    else:
        lines.append("尚未执行照度估算。")

    if estimate:
        lines.extend(["", "### 假设", ""])
        lines.extend(f"- {item}" for item in estimate.assumptions)
        lines.extend(["", "### 局限", ""])
        lines.extend(f"- {item}" for item in estimate.limitations)

    lines.extend(
        [
            "",
            "## 人工复核声明",
            "",
            "本报告用于早期方案估算和沟通。模型、光通量、维护系数、反射率、工作面网格和近似照度均应由设计人员确认；正式照度、均匀度、UGR、眩光、日光及规范符合性必须在 DIALux evo 或等效专业软件中复核。",
            "",
        ]
    )
    return "\n".join(lines)


def render_workflow_report_pdf(state: ProjectState, project_root: Path, target: Path) -> list[str]:
    """Render a compact PDF report with model and illuminance screenshots."""

    workflow = state.blender_workflow
    target.parent.mkdir(parents=True, exist_ok=True)
    model_image_paths: list[Path] = []
    if workflow.model:
        model_image_paths.extend(
            path for relative in workflow.model.render_paths if (path := _display_path(project_root, relative)) is not None
        )
    heatmap = (
        _display_path(project_root, workflow.estimate.heatmap_path)
        if workflow.estimate and workflow.estimate.heatmap_path
        else None
    )
    if not state.selected_luminaire_ids:
        raise BlenderWorkflowError("生成优化方案前必须先确认最终灯具")
    if workflow.model is None or workflow.model.status != "ready":
        raise BlenderWorkflowError("生成优化方案前必须先完成当前 Blender 模型")
    if workflow.model.scene_summary.get("selected_luminaire_ids") != state.selected_luminaire_ids:
        raise BlenderWorkflowError("最终灯具尚未同步到当前 Blender 模型")
    if not model_image_paths:
        raise BlenderWorkflowError("生成报告前必须先通过 Blender MCP 生成至少一张真实三维渲染图")
    if heatmap is None:
        raise BlenderWorkflowError("生成报告前必须先完成照度初算并生成热力图")
    image_paths = [*model_image_paths, heatmap]

    try:
        from xml.sax.saxutils import escape

        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import (
            Image as ReportImage,
            KeepTogether,
            PageBreak,
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )

        font_path = next(
            (
                path
                for path in (
                    Path("C:/Windows/Fonts/simhei.ttf"),
                    Path("C:/Windows/Fonts/msyh.ttc"),
                    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
                )
                if path.is_file()
            ),
            None,
        )
        if font_path is None:
            raise BlenderWorkflowError("未找到可嵌入 PDF 的中文字体")
        font_name = "LightingReportCJK"
        if font_name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(font_name, str(font_path), subfontIndex=0))

        page_size = landscape(A4)
        document = SimpleDocTemplate(
            str(target),
            pagesize=page_size,
            leftMargin=18 * mm,
            rightMargin=18 * mm,
            topMargin=19 * mm,
            bottomMargin=17 * mm,
            title=f"{state.brief.project_name} - 三维照明优化方案",
            author="Lighting Design Agent",
        )
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "CJKTitle",
            parent=styles["Title"],
            fontName=font_name,
            fontSize=24,
            leading=32,
            textColor=colors.HexColor("#17191c"),
            spaceAfter=8 * mm,
        )
        heading_style = ParagraphStyle(
            "CJKHeading",
            parent=styles["Heading1"],
            fontName=font_name,
            fontSize=17,
            leading=23,
            textColor=colors.HexColor("#20252b"),
            spaceAfter=6 * mm,
        )
        body_style = ParagraphStyle(
            "CJKBody",
            parent=styles["BodyText"],
            fontName=font_name,
            fontSize=10,
            leading=16,
            textColor=colors.HexColor("#3f4852"),
        )
        centred_style = ParagraphStyle("CJKCenter", parent=body_style, alignment=TA_CENTER, fontSize=9)
        small_style = ParagraphStyle("CJKSmall", parent=body_style, fontSize=8, leading=12, textColor=colors.HexColor("#66717d"))

        estimate = workflow.estimate
        assert estimate is not None
        metric_rows = [
            ["平均照度", "最小照度", "最大照度", "均匀度 U0", "工作面网格"],
            [
                f"{estimate.average_illuminance_lx:g} lx",
                f"{estimate.minimum_illuminance_lx:g} lx",
                f"{estimate.maximum_illuminance_lx:g} lx",
                str(estimate.uniformity_u0 if estimate.uniformity_u0 is not None else "-"),
                f"{estimate.grid_rows} x {estimate.grid_columns}",
            ],
        ]
        metrics = Table(metric_rows, colWidths=[(page_size[0] - 36 * mm) / 5] * 5, rowHeights=[11 * mm, 14 * mm])
        metrics.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (-1, -1), font_name),
                    ("FONTSIZE", (0, 0), (-1, 0), 8),
                    ("FONTSIZE", (0, 1), (-1, 1), 13),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#67727d")),
                    ("TEXTCOLOR", (0, 1), (-1, 1), colors.HexColor("#17191c")),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f4f5")),
                    ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd2d7")),
                    ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dce1e4")),
                ]
            )
        )

        story: list[Any] = [
            Spacer(1, 12 * mm),
            Paragraph("三维照明优化方案", title_style),
            Paragraph(escape(state.brief.project_name), ParagraphStyle("ProjectName", parent=heading_style, fontSize=15, textColor=colors.HexColor("#27606a"))),
            Paragraph(
                f"项目版本 r{state.revision} / 方案估算 / {datetime.now(UTC).astimezone().strftime('%Y-%m-%d %H:%M')}",
                body_style,
            ),
            Spacer(1, 13 * mm),
            metrics,
            Spacer(1, 15 * mm),
            Paragraph(
                "本报告汇总智能体优化后的灯具方案、Blender MCP 三维模型和工作面照度初算。"
                "结果用于前期方案比较，不构成 DIALux、UGR 或规范符合性结论。",
                ParagraphStyle("Lead", parent=body_style, fontSize=12, leading=20),
            ),
        ]

        selected_luminaires = state.selected_luminaires()
        luminaire_rows: list[list[Any]] = [["优化后灯具", "品牌", "功率", "光通量", "数量"]]
        fixture_count = int(workflow.model.scene_summary.get("fixture_count", 0) or 0)
        count_by_luminaire = {
            item.luminaire_id: fixture_count // len(selected_luminaires)
            + (1 if index < fixture_count % len(selected_luminaires) else 0)
            for index, item in enumerate(selected_luminaires)
        }
        for luminaire in selected_luminaires:
            luminaire_rows.append(
                [
                    Paragraph(escape(luminaire.article_name), small_style),
                    Paragraph(escape(luminaire.brand_name or "未提供"), small_style),
                    f"{luminaire.power_w:g} W" if luminaire.power_w is not None else "-",
                    f"{luminaire.luminous_flux_lm:g} lm" if luminaire.luminous_flux_lm is not None else "-",
                    str(count_by_luminaire[luminaire.luminaire_id]),
                ]
            )
        luminaire_table = Table(
            luminaire_rows,
            repeatRows=1,
            colWidths=[92 * mm, 38 * mm, 28 * mm, 32 * mm, 20 * mm],
        )
        luminaire_table.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (-1, -1), font_name),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#edf3f4")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#334149")),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d4dade")),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        story.extend([Spacer(1, 8 * mm), luminaire_table])

        for index, image_path in enumerate(image_paths):
            label = "照度分析热力图" if image_path == heatmap else ("Blender 三维渲染" if index == 0 else "Blender 工作面视图")
            image = ReportImage(str(image_path))
            max_width = page_size[0] - 40 * mm
            max_height = page_size[1] - 74 * mm
            scale = min(max_width / image.drawWidth, max_height / image.drawHeight)
            image.drawWidth *= scale
            image.drawHeight *= scale
            story.extend(
                [
                    PageBreak(),
                    KeepTogether(
                        [
                            Paragraph(label, heading_style),
                            image,
                            Spacer(1, 4 * mm),
                            Paragraph(escape(image_path.name), centred_style),
                        ]
                    ),
                ]
            )

        assumptions = "<br/>".join(f"- {escape(item)}" for item in estimate.assumptions) or "- 无"
        limitations = "<br/>".join(f"- {escape(item)}" for item in estimate.limitations)
        parameters = workflow.parameters
        parameter_rows = [
            ["参数", "值", "参数", "值"],
            ["工作面高度", f"{parameters.workplane_height_m:g} m", "网格间距", f"{parameters.grid_spacing_m:g} m"],
            ["维护系数 MF", str(parameters.maintenance_factor or "-"), "利用系数 UF", str(parameters.utilization_factor or "-")],
            ["地面反射率", str(parameters.floor_reflectance if parameters.floor_reflectance is not None else "-"), "墙面反射率", str(parameters.wall_reflectance if parameters.wall_reflectance is not None else "-")],
            ["顶棚反射率", str(parameters.ceiling_reflectance if parameters.ceiling_reflectance is not None else "-"), "方案总光通量", f"{parameters.total_flux_lm:g} lm" if parameters.total_flux_lm is not None else "由灯具读取"],
        ]
        parameter_table = Table(parameter_rows, colWidths=[42 * mm, 52 * mm, 42 * mm, 74 * mm])
        parameter_table.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (-1, -1), font_name),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#edf3f4")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#334149")),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d4dade")),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        story.extend(
            [
                PageBreak(),
                Paragraph("计算前提与复核边界", heading_style),
                parameter_table,
                Spacer(1, 8 * mm),
                Paragraph("已采用的前提", ParagraphStyle("Subhead", parent=body_style, fontSize=12, textColor=colors.HexColor("#20252b"), spaceAfter=3 * mm)),
                Paragraph(assumptions, body_style),
                Spacer(1, 8 * mm),
                Paragraph("局限与后续复核", ParagraphStyle("Subhead2", parent=body_style, fontSize=12, textColor=colors.HexColor("#20252b"), spaceAfter=3 * mm)),
                Paragraph(limitations, body_style),
                Spacer(1, 8 * mm),
                Paragraph(
                    "正式设计应在 DIALux evo 或等效专业软件中复核真实材料反射、遮挡、日光、UGR、功率密度和规范符合性。",
                    ParagraphStyle("Warning", parent=body_style, backColor=colors.HexColor("#fff7df"), borderColor=colors.HexColor("#e7c66a"), borderWidth=0.5, borderPadding=8),
                ),
            ]
        )

        def draw_page(canvas: Any, doc: Any) -> None:
            canvas.saveState()
            canvas.setFont(font_name, 8)
            canvas.setFillColor(colors.HexColor("#67727d"))
            canvas.drawString(18 * mm, 10 * mm, state.brief.project_name[:48])
            canvas.drawRightString(page_size[0] - 18 * mm, 10 * mm, f"{doc.page}")
            canvas.setStrokeColor(colors.HexColor("#d8dde1"))
            canvas.line(18 * mm, 14 * mm, page_size[0] - 18 * mm, 14 * mm)
            canvas.restoreState()

        document.build(story, onFirstPage=draw_page, onLaterPages=draw_page)
    except Exception as error:
        raise BlenderWorkflowError(f"无法生成 PDF 报告：{error}") from error
    return [path.relative_to(project_root).as_posix() for path in image_paths]


def update_node(
    workflow: BlenderWorkflow,
    node_id: str,
    *,
    status: str,
    message: str | None = None,
    output_refs: list[str] | None = None,
) -> BlenderWorkflow:
    nodes = []
    for node in workflow.nodes:
        if node.node_id == node_id:
            nodes.append(
                node.model_copy(
                    update={
                        "status": status,
                        "message": message,
                        "output_refs": output_refs if output_refs is not None else node.output_refs,
                        "updated_at": datetime.now(UTC),
                    }
                )
            )
        else:
            nodes.append(node)
    return workflow.model_copy(update={"nodes": nodes, "updated_at": datetime.now(UTC)})


__all__ = [
    "BlenderConnectionError",
    "BlenderWorkflowError",
    "SOLVER_VERSION",
    "build_workflow_report_markdown",
    "create_or_reuse_blender_model",
    "discover_blender_executable",
    "ensure_blender_running",
    "estimate_workplane",
    "mcp_request",
    "modeling_missing_fields",
    "probe_blender",
    "register_model",
    "render_source_previews",
    "render_workflow_report_pdf",
    "sha256_file",
    "update_node",
    "workflow_root",
    "workflow_source_fingerprint",
]
