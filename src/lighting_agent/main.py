"""Command-line interface for local, testable lighting-design workflows."""

from __future__ import annotations

import argparse
import json
from typing import Any

from .agent import interactive_chat, invoke_agent
from .calculations import calculate_lumen_method
from .dialux_api import DialuxAPI
from .deliverables import build_design_report, build_dialux_task_archive, read_dialux_task_package
from .photometry_assets import PhotometryAssetStore
from .document_loader import load_document
from .project_store import ProjectStore
from .rag import create_evidence_store, format_evidence
from .schemas import (
    CalculationInput,
    DesignBrief,
    LuminaireSearchRequest,
    ProjectUpdate,
    SimulationMetrics,
    SimulationRun,
)


def _print(value: Any) -> None:
    def default(item: Any) -> Any:
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json")
        return str(item)

    print(json.dumps(value, ensure_ascii=False, indent=2, default=default))


def _add_brief_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--space-type")
    parser.add_argument("--area-m2", type=float)
    parser.add_argument("--length-m", type=float)
    parser.add_argument("--width-m", type=float)
    parser.add_argument("--room-height-m", type=float)
    parser.add_argument("--mounting-height-m", type=float)
    parser.add_argument("--target-lx", type=float)
    parser.add_argument("--target-cct-k", type=int)
    parser.add_argument("--min-cri", type=int)
    parser.add_argument("--target-ugr", type=float)
    parser.add_argument("--max-lpd-w-m2", type=float)
    parser.add_argument("--max-power-w", type=float)
    parser.add_argument("--mounting")
    parser.add_argument("--min-ip-rating")
    parser.add_argument("--brand", action="append", default=[])
    parser.add_argument("--notes")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Auditable indoor-lighting design assistant")
    subcommands = parser.add_subparsers(dest="command", required=True)

    create = subcommands.add_parser("init-project", help="create a versioned design project")
    create.add_argument("project_name")
    _add_brief_arguments(create)

    show = subcommands.add_parser("show-project", help="show a saved project")
    show.add_argument("project_id")

    add_document = subcommands.add_parser("add-document", help="index a workspace document for evidence retrieval")
    add_document.add_argument("file_path")
    add_document.add_argument("--source-type", choices=["standard", "project_document", "user_note"], default="project_document")

    evidence = subcommands.add_parser("search-evidence", help="search indexed evidence")
    evidence.add_argument("query")

    calculate = subcommands.add_parser("calculate", help="run and save a lumen-method estimate")
    calculate.add_argument("project_id")
    calculate.add_argument("--revision", type=int, required=True)
    calculate.add_argument("--area-m2", type=float, required=True)
    calculate.add_argument("--target-lx", type=float, required=True)
    calculate.add_argument("--lumens", type=float, required=True)
    calculate.add_argument("--power-w", type=float, required=True)
    calculate.add_argument("--utilization-factor", type=float, required=True)
    calculate.add_argument("--maintenance-factor", type=float, required=True)

    luminaire = subcommands.add_parser("search-luminaires", help="search DIALux Luminaire Finder candidates")
    luminaire.add_argument("keyword")
    luminaire.add_argument("--language", default="zh")
    luminaire.add_argument("--brand")
    luminaire.add_argument("--brand-id")
    luminaire.add_argument("--target-cct-k", type=int)
    luminaire.add_argument("--min-cri", type=int)
    luminaire.add_argument("--max-power-w", type=float)
    luminaire.add_argument("--min-ip-rating")
    luminaire.add_argument("--max-results", type=int, default=5)

    task = subcommands.add_parser("create-dialux-task", help="create a DIALux evo handoff package")
    task.add_argument("project_id")
    task.add_argument("--revision", type=int, required=True)

    report = subcommands.add_parser("generate-report", help="create a reviewable Markdown design report")
    report.add_argument("project_id")
    report.add_argument("--revision", type=int, required=True)

    result = subcommands.add_parser("import-dialux-result", help="import a structured DIALux result and verify it against the current handoff")
    result.add_argument("project_id")
    result.add_argument("--revision", type=int, required=True)
    result.add_argument("--handoff-id", required=True)
    result.add_argument("--source-kind", choices=["dialux_pdf", "dialux_csv", "dialux_json", "manual_form"], default="manual_form")
    result.add_argument("--maintained-lx", type=float)
    result.add_argument("--minimum-lx", type=float)
    result.add_argument("--uniformity-u0", type=float)
    result.add_argument("--ugr", type=float)
    result.add_argument("--lpd-w-m2", type=float)
    result.add_argument("--solver-version")

    chat = subcommands.add_parser("chat", help="run the LLM agent; requires LIGHTING_LLM_API_KEY")
    chat.add_argument("message", nargs="?", help="single-turn message; omit it or use --interactive for a continuous session")
    chat.add_argument("-i", "--interactive", action="store_true", help="start a continuous terminal chat")
    return parser


def _brief_from_args(args: argparse.Namespace) -> DesignBrief:
    values = {
        "project_name": args.project_name,
        "space_type": args.space_type,
        "area_m2": args.area_m2,
        "length_m": args.length_m,
        "width_m": args.width_m,
        "room_height_m": args.room_height_m,
        "mounting_height_m": args.mounting_height_m,
        "target_illuminance_lx": args.target_lx,
        "target_cct_k": args.target_cct_k,
        "min_cri": args.min_cri,
        "target_ugr": args.target_ugr,
        "max_lpd_w_m2": args.max_lpd_w_m2,
        "max_power_w": args.max_power_w,
        "mounting": args.mounting,
        "min_ip_rating": args.min_ip_rating,
        "preferred_brands": args.brand,
        "notes": args.notes,
    }
    confirmed = {key for key, value in values.items() if value not in (None, [], "") and key != "project_name"}
    return DesignBrief(**values, confirmed_fields=confirmed)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    store = ProjectStore()
    evidence_store = create_evidence_store()
    if args.command == "init-project":
        _print(store.create(_brief_from_args(args)))
        return
    if args.command == "show-project":
        _print(store.get(args.project_id))
        return
    if args.command == "add-document":
        document = load_document(args.file_path)
        chunks = evidence_store.add_document(document, source_type=args.source_type)
        _print({"source_name": document.source_name, "sha256": document.sha256, "indexed_chunks": chunks})
        return
    if args.command == "search-evidence":
        results = evidence_store.search(args.query)
        _print({"evidence": [item.model_dump(mode="json") for item in results], "formatted": format_evidence(results)})
        return
    if args.command == "calculate":
        result = calculate_lumen_method(
            CalculationInput(
                area_m2=args.area_m2,
                target_illuminance_lx=args.target_lx,
                luminaire_luminous_flux_lm=args.lumens,
                luminaire_power_w=args.power_w,
                utilization_factor=args.utilization_factor,
                maintenance_factor=args.maintenance_factor,
            )
        )
        state = store.get(args.project_id)
        updated = store.update(
            args.project_id,
            ProjectUpdate(expected_revision=args.revision, calculations=[*state.calculations, result]),
        )
        _print({"calculation": result, "project_revision": updated.revision})
        return
    if args.command == "search-luminaires":
        request = LuminaireSearchRequest(
            keyword=args.keyword,
            language=args.language,
            brand=args.brand,
            brand_id=args.brand_id,
            target_cct_k=args.target_cct_k,
            min_cri=args.min_cri,
            max_power_w=args.max_power_w,
            min_ip_rating=args.min_ip_rating,
            max_results=args.max_results,
        )
        _print({"candidates": DialuxAPI().search(request), "notice": "候选灯具仍需 DIALux evo 仿真核验。"})
        return
    if args.command in {"create-dialux-task", "generate-report"}:
        state = store.get(args.project_id)
        if state.revision != args.revision:
            raise ValueError(f"Project revision is {state.revision}, but command expected {args.revision}")
        if args.command == "create-dialux-task":
            target = store.directory / f"{state.project_id}.dialux-task.zip"
            target.write_bytes(build_dialux_task_archive(state, PhotometryAssetStore(store.directory, DialuxAPI())))
            _print({"task_package": str(target), "project_revision": state.revision})
        else:
            target = store.directory / f"{state.project_id}.design-report.md"
            target.write_text(build_design_report(state), encoding="utf-8")
            _print({"report": str(target), "project_revision": state.revision})
        return
    if args.command == "import-dialux-result":
        state = store.get(args.project_id)
        if state.revision != args.revision:
            raise ValueError(f"Project revision is {state.revision}, but command expected {args.revision}")
        handoff_path = store.directory / f"{state.project_id}.dialux-task.zip"
        if not handoff_path.exists():
            raise ValueError("No DIALux task package exists for this project; create one first")
        package = read_dialux_task_package(handoff_path.read_bytes())
        messages: list[str] = []
        if args.handoff_id != package.get("handoff_id"):
            messages.append("handoff_id 与当前任务包不匹配")
        if package.get("input_snapshot", {}).get("project_id") != state.project_id:
            messages.append("任务包不属于当前项目")
        if package.get("input_snapshot", {}).get("selected_luminaire_ids", []) != state.selected_luminaire_ids:
            messages.append("任务包中的最终灯具与当前项目不一致")
        metrics = SimulationMetrics(
            maintained_illuminance_lx=args.maintained_lx,
            minimum_illuminance_lx=args.minimum_lx,
            uniformity_u0=args.uniformity_u0,
            ugr=args.ugr,
            installed_power_density_w_m2=args.lpd_w_m2,
        )
        status = "matched" if not messages else "mismatch"
        run = SimulationRun(
            kind="精算",
            status="succeeded" if status == "matched" else "unverified",
            input_project_revision=args.revision,
            solver_version=args.solver_version,
            handoff_id=args.handoff_id,
            input_snapshot_sha256=package.get("input_snapshot_sha256"),
            selected_luminaire_ids=list(package.get("selected_luminaire_ids", [])),
            photometry_sha256_by_luminaire=dict(package.get("photometry_sha256_by_luminaire", {})),
            source_kind=args.source_kind,
            metrics=metrics,
            verification_status=status,
            verification_messages=messages,
        )
        updated = store.append_simulation_run(state.project_id, args.revision, run)
        _print({"simulation_run": run, "project_revision": updated.revision, "verification_messages": messages})
        return
    if args.command == "chat":
        if args.interactive or args.message is None:
            interactive_chat()
            return
        print(invoke_agent(args.message))
        return
    raise AssertionError(f"Unhandled command: {args.command}")
