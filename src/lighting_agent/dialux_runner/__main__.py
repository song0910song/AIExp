"""Standalone CLI; intentionally not exposed as an agent tool or API route."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .artifacts import RunError, RunStore, atomic_json, prepare_replay, validate_job
from .runner import run_job


def main() -> int:
    parser = argparse.ArgumentParser(description='Standalone verified DIALux native replay and result collection')
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare', help='Snapshot the accepted phase-0 synthetic archive; no GUI actions')
    prepare.add_argument('--source', type=Path, required=True)
    prepare.add_argument('--output', type=Path, required=True)
    prepare.add_argument('--dialux', type=Path, required=True)
    fresh = commands.add_parser('prepare-import', help='Snapshot synthetic IFC/IES inputs; no native template or GUI actions')
    fresh.add_argument('--source', type=Path, required=True)
    fresh.add_argument('--output', type=Path, required=True)
    fresh.add_argument('--dialux', type=Path, required=True)
    envelope = commands.add_parser('prepare-envelope', help='Snapshot synthetic two-room door/window inputs for independent desktop validation')
    envelope.add_argument('--source', type=Path, required=True)
    envelope.add_argument('--output', type=Path, required=True)
    envelope.add_argument('--dialux', type=Path, required=True)
    generic = commands.add_parser('prepare-generic', help='Prepare a confirmed simple DXF/DWG-derived IFC job')
    generic.add_argument('--cad', type=Path, required=True)
    generic.add_argument('--model', type=Path, required=True)
    generic.add_argument('--photometry', type=Path, required=True)
    generic.add_argument('--output', type=Path, required=True)
    generic.add_argument('--dialux', type=Path, required=True)
    generic.add_argument('--project-name', required=True)
    generic.add_argument('--project-id', required=True)
    generic.add_argument('--default-usage')
    run = commands.add_parser('run', help='Execute on a dedicated DIALux instance')
    run.add_argument('directory', type=Path)
    run.add_argument('--resume', action='store_true', help='Replay immutable inputs in a new attempt after review')
    status = commands.add_parser('status', help='Validate inputs and completed artifacts, without GUI actions')
    status.add_argument('directory', type=Path)
    pause = commands.add_parser('pause', help='Request manual takeover at the next safe step boundary')
    pause.add_argument('directory', type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            print(prepare_replay(args.source, args.output, args.dialux))
        elif args.command == 'prepare-import':
            from .imports import prepare_import
            print(prepare_import(args.source, args.output, args.dialux))
        elif args.command == 'prepare-envelope':
            from .envelope_jobs import prepare_envelope
            print(prepare_envelope(args.source, args.output, args.dialux))
        elif args.command == 'prepare-generic':
            from lighting_agent.schemas import DialuxGeometryAssumptions, DialuxOptimizationRequest, SpatialModel
            from .generic_jobs import auto_layout, prepare_generic_job, prepare_reviewed_model
            model = SpatialModel.model_validate_json(args.model.read_text(encoding='utf-8'))
            if model.automation_status != 'auto_confirmed':
                raise RunError('CAD model has not passed automatic model analysis')
            assumptions = DialuxGeometryAssumptions(default_usage=args.default_usage)
            effective, readiness = prepare_reviewed_model(model, assumptions,
                                                          default_usage=args.default_usage, confirm=True)
            if not readiness.can_prepare:
                raise RunError('; '.join(readiness.issues))
            print(prepare_generic_job(
                cad_path=args.cad, photometry_path=args.photometry, model=effective,
                assumptions=assumptions, layout=auto_layout(effective),
                optimization=DialuxOptimizationRequest(),
                project_name=args.project_name, project_id=args.project_id,
                executable=args.dialux, output=args.output,
            ))
        elif args.command == 'run':
            state = run_job(args.directory, resume=args.resume)
            print(json.dumps({'run_id': state['run_id'], 'status': state['status']}, ensure_ascii=True))
        elif args.command == 'status':
            store = RunStore(args.directory)
            validate_job(store)
            store.completed()
            print(json.dumps(store.state, ensure_ascii=True, indent=2))
        elif args.command == 'pause':
            store = RunStore(args.directory)
            atomic_json(store.root / 'pause.request', {'request': 'pause before next step'})
            print('Pause requested; an active DIALux calculation is not forcibly interrupted.')
        return 0
    except (RunError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
