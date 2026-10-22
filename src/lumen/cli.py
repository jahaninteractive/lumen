from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .config import Settings
from .diagnostics import doctor_report
from .library import CleanupError, ExportRequest, Library, public_message
from .omlx import OmlxTagger
from .pipelines import PipelineRegistry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lumen", description="Local, non-destructive RAW photography manager")
    parser.add_argument("--library", type=Path, help="catalog/cache directory (default: ~/Pictures/Lumen Library)")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="check local tools and model readiness")
    doctor.add_argument("--json", action="store_true")

    importer = commands.add_parser("import", help="import a file or folder without changing originals")
    importer.add_argument("path", type=Path)
    importer.add_argument("--json", action="store_true")

    groups = commands.add_parser("groups", help="list burst groups and scores")
    groups.add_argument("--json", action="store_true")

    tag = commands.add_parser("tag", help="tag previews with the configured local oMLX model")
    selection = tag.add_mutually_exclusive_group(required=True)
    selection.add_argument("--photo", type=int)
    selection.add_argument("--all", action="store_true")
    tag.add_argument("--retag", action="store_true", help="with --all, also re-tag photos that already have tags")
    tag.add_argument("--json", action="store_true")

    jobs = commands.add_parser("jobs", help="list recent background jobs")
    jobs.add_argument("--json", action="store_true")

    cancel = commands.add_parser("cancel", help="cancel a queued job, or a running job owned by this process")
    cancel.add_argument("job_id", type=int)
    cancel.add_argument("--json", action="store_true")

    pick = commands.add_parser("pick", help="make a frame the manual best shot for its group")
    pick.add_argument("photo_id", type=int)

    process = commands.add_parser("process", help="run a non-destructive local processing hook")
    process.add_argument("photo_id", type=int)
    process.add_argument("--pipeline", choices=("darktable",), default="darktable")
    process.add_argument("--profile", type=Path, help="optional darktable XMP sidecar/style")
    process.add_argument("--output", type=Path)
    process.add_argument("--json", action="store_true")

    export = commands.add_parser("export", help="export a photo or all group keepers as JPEG")
    selection = export.add_mutually_exclusive_group(required=True)
    selection.add_argument("--photo", type=int)
    selection.add_argument("--keepers", action="store_true")
    export.add_argument("--profile", type=Path, help="optional darktable XMP profile")
    export.add_argument("--json", action="store_true")

    remove = commands.add_parser("remove", help="remove a group from Lumen with recoverable working files")
    remove.add_argument("group_id", type=int)
    remove.add_argument("--json", action="store_true")

    clear = commands.add_parser("clear", help="clear the workspace with exact CLEAR confirmation")
    clear.add_argument("confirmation")
    clear.add_argument("--json", action="store_true")

    siril = commands.add_parser("siril", help="run a reviewed Siril script from the managed outputs folder")
    siril.add_argument("script", type=Path)
    siril.add_argument("--json", action="store_true")

    astro = commands.add_parser("astro", help="prepare, run, inspect, or cancel a managed Siril stacking job")
    actions = astro.add_subparsers(dest="action", required=True)
    prepare = actions.add_parser("prepare", help="prepare a confirmed astro group as a queued job")
    prepare.add_argument("group_id", type=int)
    prepare.add_argument("--confirm", action="store_true", help="explicitly confirm this group is an astro stack")
    prepare.add_argument("--exclude", type=int, action="append", default=[], metavar="PHOTO_ID")
    for kind in ("darks", "flats", "biases"):
        prepare.add_argument(f"--{kind}", type=Path, help=f"folder of {kind[:-1]} calibration frames")
    prepare.add_argument("--json", action="store_true")
    for action, help_text in (("run", "run a queued job and wait for it"), ("status", "show a job's progress"),
                              ("cancel", "cancel a queued or running job owned by this process")):
        sub = actions.add_parser(action, help=help_text)
        sub.add_argument("job_id", type=int)
        sub.add_argument("--json", action="store_true")

    serve = commands.add_parser("serve", help="start the local contact-sheet UI")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--open", action="store_true", dest="open_browser")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_environment()
    if args.library:
        settings = Settings(
            library_dir=args.library.expanduser(),
            omlx_url=settings.omlx_url,
            omlx_model=settings.omlx_model,
            burst_gap_seconds=settings.burst_gap_seconds,
        )

    if args.command == "doctor":
        report = doctor_report(settings)
        _print(report, args.json)
        return 0 if report["tools"]["exiftool"]["available"] and report["omlx_model"]["complete"] else 2

    if args.command in {"import", "jobs", "cancel"} or (args.command == "tag" and args.all):
        try:
            with Library(settings) as library:
                if args.command == "jobs":
                    payload = {"jobs": library.list_job_status()}
                elif args.command == "cancel":
                    library.cancel_job(args.job_id)
                    payload = library.job_status(args.job_id)
                else:
                    job = (library.start_import_job(args.path) if args.command == "import" else
                           library.start_tag_job(OmlxTagger(settings.omlx_url, settings.omlx_model), retag=args.retag))
                    library.wait_for_job(job.id)
                    payload = library.job_status(job.id)
        except (ValueError, OverflowError, OSError, RuntimeError) as error:
            payload = {"error": public_message(str(error))}
        _print(payload, args.json)
        if args.command == "jobs":
            return 0
        if args.command == "cancel":
            return 0 if payload.get("status") == "cancelled" else 2
        return 0 if payload.get("status") == "succeeded" and not (payload.get("result") or {}).get("failed") else 2

    if args.command == "groups":
        with Library(settings) as library:
            payload = {"groups": _serializable_groups(library.list_groups())}
        _print(payload, args.json)
        return 0

    if args.command == "tag":
        with Library(settings) as library:
            result = library.tag_photo(args.photo, OmlxTagger(settings.omlx_url, settings.omlx_model))
        _print({"tagged": [{"photo_id": args.photo, **asdict(result)}]}, args.json)
        return 0

    if args.command == "pick":
        with Library(settings) as library:
            library.set_pick(args.photo_id)
        print(f"Photo {args.photo_id} is now the manual best shot.")
        return 0

    if args.command == "process":
        with Library(settings) as library:
            item = library.get_photo(args.photo_id)
            if not item:
                raise ValueError(f"photo {args.photo_id} not found")
            source = Path(item["photo"].path)
            output = (args.output or settings.output_dir / f"{args.photo_id}-darktable.tif").expanduser().resolve()
            if library.catalog.photo_by_path(str(output)):
                raise ValueError("pipeline output cannot replace a catalog original")
            pipeline = PipelineRegistry().darktable(settings.output_dir)
            result = pipeline.run(source, output, args.profile)
            library.catalog.append_event(args.photo_id, "pipeline", asdict(result) | {"output": str(result.output)})
        _print(asdict(result) | {"output": str(result.output)}, args.json)
        return 0 if result.success else 2

    if args.command == "siril":
        pipeline = PipelineRegistry().siril(settings.output_dir)
        result = pipeline.run(args.script)
        _print(asdict(result) | {"output": str(result.output)}, args.json)
        return 0 if result.success else 2

    if args.command in {"export", "remove", "clear"}:
        try:
            with Library(settings) as library:
                if args.command == "export":
                    photo_ids = (args.photo,) if args.photo is not None else tuple(library.keeper_ids())
                    results = library.export_jpegs(ExportRequest(photo_ids, args.profile))
                    payload = {"results": [asdict(result) for result in results]}
                    code = 0 if all(result.success for result in results) else 2
                else:
                    result = library.remove_group(args.group_id) if args.command == "remove" else library.clear_workspace(args.confirmation)
                    payload = asdict(result)
                    code = 0
        except CleanupError as error:
            payload = _cleanup_error_payload(error)
            code = 2
        except (ValueError, OverflowError, OSError, RuntimeError) as error:
            payload = {"error": str(error)}
            code = 2
        _print(payload, args.json)
        return code

    if args.command == "astro":
        try:
            with Library(settings) as library:
                if args.action == "prepare":
                    if not args.confirm:
                        raise ValueError("astro stacking requires explicit photographer confirmation (--confirm)")
                    job_id = library.prepare_astro_job(
                        args.group_id, confirmed=True, excluded_photo_ids=args.exclude,
                        darks=args.darks, flats=args.flats, biases=args.biases,
                    ).id
                elif args.action == "run":
                    job_id = library.run_astro_job(args.job_id).id
                    library.wait_for_job(job_id)
                elif args.action == "cancel":
                    library.cancel_job(args.job_id)
                    job_id = args.job_id
                else:
                    job_id = args.job_id
                payload = library.job_status(job_id)
        except (ValueError, OverflowError, OSError, RuntimeError) as error:
            payload = {"error": public_message(str(error))}
        _print(payload, args.json)
        return 0 if payload.get("status") in {"queued", "running", "succeeded", "cancelled"} else 2

    if args.command == "serve":
        from .web import make_server

        server = make_server(settings, port=args.port)
        url = f"http://127.0.0.1:{server.server_port}"
        print(f"Lumen is running at {url}")
        if args.open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    return 2


def _serializable_groups(groups: list[dict]) -> list[dict]:
    result = []
    for group in groups:
        serialized = {key: value for key, value in group.items() if key != "photos"}
        serialized["photos"] = []
        for photo in group["photos"]:
            item = dict(photo)
            item["name"] = Path(item["path"]).name
            item["preview_url"] = f"/preview/{item['id']}?v={item['fingerprint'][:16]}"
            item.pop("path", None)
            item.pop("fingerprint", None)
            item.pop("preview_path", None)
            item.pop("astro_json", None)
            serialized["photos"].append(item)
        result.append(serialized)
    return result


def _cleanup_error_payload(error: CleanupError) -> dict:
    return {
        "error": str(error),
        "recovery_path": error.recovery_path,
        "snapshot_path": error.snapshot_path,
        "recovery_complete": error.recovery_complete,
        "restoration_errors": [str(failure) for failure in error.restoration_errors],
    }


def _json_default(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _print(payload, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, default=_json_default, sort_keys=True))
        return
    print(json.dumps(payload, default=_json_default, indent=2, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())

