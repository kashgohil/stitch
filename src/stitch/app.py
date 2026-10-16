"""Application entry point and a terminal-friendly installation check."""

import argparse
import json
import logging
import sys
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
    args = parser.parse_args(argv)
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
        if args.check:
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

    from PySide6.QtWidgets import QApplication

    from stitch.window import MainWindow

    app = QApplication([sys.argv[0]])
    app.setApplicationName("Stitch")
    app.setOrganizationName("Stitch")
    window = MainWindow(paths)
    window.show()
    return app.exec()
