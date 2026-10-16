"""Atomic project writes and explicit version/validation errors."""

import json
import os
import tempfile
from pathlib import Path

from pydantic import ValidationError

from stitch.models import Project

MAX_PROJECT_BYTES = 16 * 1024 * 1024


class ProjectError(ValueError):
    """A project cannot be opened or saved; safe to display in the UI."""


def load_project(path: Path) -> Project:
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_PROJECT_BYTES + 1)
        if len(data) > MAX_PROJECT_BYTES:
            raise ProjectError("Project exceeds the 16 MiB project-file limit.")
        raw = json.loads(data)
        if not isinstance(raw, dict) or type(raw.get("schema_version")) is not int:
            raise ProjectError("Project must declare an integer schema_version.")
        if raw["schema_version"] != 1:
            raise ProjectError(f"Unsupported project version {raw['schema_version']}; expected 1.")
        return Project.model_validate(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as exc:
        raise ProjectError(f"Cannot open project {path.name}: {exc}") from exc


def save_project(project: Project, path: Path) -> None:
    """Keep the previous project intact if serialization or writing fails."""
    temporary: Path | None = None
    try:
        # Revalidate even if a caller bypassed validation using model_copy/model_construct.
        validated = Project.model_validate(project.model_dump(mode="json"))
        payload = (validated.model_dump_json(indent=2) + "\n").encode("utf-8")
        if len(payload) > MAX_PROJECT_BYTES:
            raise ProjectError("Project exceeds the 16 MiB project-file limit.")
        # Put the temporary file beside the destination for same-filesystem replacement.
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".stitch-", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except (OSError, ValidationError) as exc:
        raise ProjectError(f"Cannot save project {path.name}: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
