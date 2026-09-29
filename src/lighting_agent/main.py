"""Command-line access to CAD, evidence and DIALux catalogue search."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .dialux_api import DialuxAPI
from .document_loader import load_document
from .floor_plan import parse_floor_plan
from .project_store import ProjectStore
from .rag import create_evidence_store
from .schemas import DesignBrief, LuminaireSearchRequest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Lighting CAD and knowledge workbench")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("init-project", help="create a project")
    create.add_argument("project_name")
    create.add_argument("--space-type")
    show = commands.add_parser("show-project", help="show a project")
    show.add_argument("project_id")
    cad = commands.add_parser("analyze-cad", help="inspect a DXF or DWG without changing a project")
    cad.add_argument("file_path", type=Path)
    document = commands.add_parser("add-document", help="index a local document")
    document.add_argument("file_path")
    document.add_argument("--source-type", choices=["standard", "project_document", "user_note"], default="standard")
    search = commands.add_parser("search-evidence", help="retrieve indexed excerpts")
    search.add_argument("query")
    luminaires = commands.add_parser("search-luminaires", help="query DIALux Luminaire Finder")
    luminaires.add_argument("keyword")
    luminaires.add_argument("--brand")
    luminaires.add_argument("--target-cct-k", type=int)
    luminaires.add_argument("--min-cri", type=int)
    return parser


def _output(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=lambda item: item.model_dump(mode="json")))


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "init-project":
        _output(ProjectStore().create(DesignBrief(project_name=args.project_name, space_type=args.space_type)))
    elif args.command == "show-project":
        _output(ProjectStore().get(args.project_id))
    elif args.command == "analyze-cad":
        _output(parse_floor_plan(args.file_path, storage_path=args.file_path.name))
    elif args.command == "add-document":
        document = load_document(args.file_path)
        count = create_evidence_store().add_document(document, source_type=args.source_type)
        _output({"source_name": document.source_name, "indexed_chunks": count})
    elif args.command == "search-evidence":
        _output({"evidence": create_evidence_store().search(args.query)})
    elif args.command == "search-luminaires":
        request = LuminaireSearchRequest(
            keyword=args.keyword, brand=args.brand, target_cct_k=args.target_cct_k,
            min_cri=args.min_cri,
        )
        _output({"candidates": DialuxAPI().search(request)})


if __name__ == "__main__":
    main()
