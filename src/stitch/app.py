"""Application entry point and a terminal-friendly installation check."""

import argparse
import json
import logging
import signal
import sys
import threading
from dataclasses import asdict
from logging.handlers import RotatingFileHandler
from pathlib import Path

from stitch.dependencies import installation_help, probe_tools
from stitch.paths import AppPaths


def configure_logging(paths: AppPaths) -> None:
    logger = logging.getLogger("stitch")
    handler = RotatingFileHandler(
        paths.logs / "stitch.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stitch desktop video assembler")
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument(
        "--check", action="store_true", help="Check storage and media tools, then exit"
    )
    commands.add_argument("--probe", type=Path, metavar="FILE", help="Print media metadata as JSON")
    commands.add_argument(
        "--inspect-project",
        type=Path,
        metavar="FILE",
        help="Validate a project and check its sources",
    )
    commands.add_argument(
        "--project-schema", action="store_true", help="Print the project JSON Schema"
    )
    commands.add_argument(
        "--render-clip",
        type=Path,
        metavar="PROJECT",
        help="Render one original-media clip into an isolated diagnostic job (not a final export)",
    )
    parser.add_argument(
        "--clip-index",
        type=int,
        metavar="N",
        help="One-based clip number for --render-clip (default: 1)",
    )
    commands.add_argument("--export", type=Path, metavar="PROJECT", help="Export a saved sequence")
    parser.add_argument("--output", type=Path, metavar="FILE", help="Final path for --export")
    parser.add_argument(
        "--overwrite", action="store_true", help="Allow --export to replace its output"
    )
    args = parser.parse_args(argv)
    if args.clip_index is not None and (not args.render_clip or args.clip_index < 1):
        parser.error("--clip-index requires --render-clip and a positive one-based number")
    if bool(args.export) != bool(args.output) or (args.overwrite and not args.export):
        parser.error("--export requires --output; --output and --overwrite require --export")
    if args.probe or args.inspect_project or args.project_schema:
        from stitch.media import MediaError, probe_media
        from stitch.models import Project
        from stitch.project_io import ProjectError, load_project
        from stitch.sources import source_status

        try:
            if args.probe:
                print(probe_media(args.probe).model_dump_json(indent=2))
            elif args.project_schema:
                print(json.dumps(Project.model_json_schema(), indent=2))
            else:
                project = load_project(args.inspect_project)
                statuses = [source_status(source) for source in project.sources]
                print(
                    json.dumps(
                        {
                            "name": project.name,
                            "schema_version": project.schema_version,
                            "clip_count": len(project.clips),
                            "sources": [asdict(s) for s in statuses],
                        },
                        indent=2,
                    )
                )
                return 0 if all(s.state == "ready" for s in statuses) else 1
        except (MediaError, ProjectError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    paths = AppPaths.default()
    try:
        paths.ensure()
        configure_logging(paths)
    except OSError as exc:
        message = (
            f"Cannot initialize Stitch storage: {exc}. Check folder permissions and free space."
        )
        if args.check or args.render_clip or args.export:
            print(message, file=sys.stderr)
        else:
            from PySide6.QtWidgets import QApplication, QMessageBox

            app = QApplication([sys.argv[0]])
            QMessageBox.critical(None, "Stitch could not start", message)
            app.quit()
        return 1

    if args.check:
        tools = probe_tools()
        print(
            json.dumps(
                {
                    "paths": {key: str(value) for key, value in asdict(paths).items()},
                    "tools": [asdict(tool) for tool in tools],
                },
                indent=2,
            )
        )
        if not all(tool.available for tool in tools):
            print(installation_help(), file=sys.stderr)
            return 1
        return 0

    if args.export:
        from stitch.export import export_project
        from stitch.process import RenderCancelled
        from stitch.project_io import ProjectError, load_project
        from stitch.render_plan import RenderError

        cancel = threading.Event()
        previous_handler = signal.signal(signal.SIGINT, lambda *_: cancel.set())
        last_update = [None]

        def show_progress(update):
            key = (update.stage, int(update.fraction * 100))
            if key != last_update[0]:
                print(
                    f"{update.message} · {update.fraction:.0%} · {update.elapsed:.0f}s",
                    file=sys.stderr,
                )
                last_update[0] = key

        try:
            result = export_project(
                load_project(args.export),
                args.output,
                paths,
                overwrite=args.overwrite,
                cancel=cancel,
                progress=show_progress,
            )
            print(
                json.dumps(
                    {"path": str(result.path), "job": str(result.job), "warnings": result.warnings},
                    indent=2,
                )
            )
        except RenderCancelled as exc:
            print(str(exc), file=sys.stderr)
            return 130
        except (RenderError, ProjectError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        finally:
            signal.signal(signal.SIGINT, previous_handler)
        return 0

    if args.render_clip:
        from stitch.project_io import ProjectError, load_project
        from stitch.render import render_segment
        from stitch.render_plan import RenderError

        try:
            project = load_project(args.render_clip)
            index = (args.clip_index or 1) - 1
            if index >= len(project.clips):
                raise RenderError(
                    f"Project has {len(project.clips)} clips; choose a valid clip number."
                )
            result = render_segment(project, project.clips[index].id, paths)
            print(
                json.dumps(
                    {
                        "path": str(result.path),
                        "job": str(result.job),
                        "warnings": result.warnings,
                        "metadata": result.metadata.model_dump(mode="json"),
                    },
                    indent=2,
                )
            )
        except (RenderError, ProjectError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0

    from PySide6.QtWidgets import QApplication

    from stitch.window import MainWindow

    app = QApplication([sys.argv[0]])
    app.setApplicationName("Stitch")
    app.setOrganizationName("Stitch")
    window = MainWindow(paths)
    window.show()
    return app.exec()
