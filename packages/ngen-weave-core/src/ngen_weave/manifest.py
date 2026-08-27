"""Project manifest (`ngen-weave.json`): parsing plus manifest-driven discovery."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ngen_weave.constants import MANIFEST_NAME
from ngen_weave.discovery import discover
from ngen_weave.errors import ConfigError
from ngen_weave.workflow import Workflow


@dataclass(frozen=True)
class ProjectManifest:
    """Parsed project manifest: the workflow modules it lists.

    Attributes:
        modules: Module paths declared under the top-level "modules" key.
    """

    modules: tuple[str, ...]


def load_project_manifest(root: Path = Path(".")) -> ProjectManifest:
    """Load the project manifest from root's `ngen-weave.json`.

    A missing file yields an empty manifest so entry-point discovery still
    applies to projects without a manifest.

    Args:
        root: Project root directory holding `ngen-weave.json`.

    Returns:
        The parsed manifest.

    Raises:
        ConfigError: The file exists but its JSON is malformed, or the parsed
            document is not an object with a list of string entries under
            "modules". Errors always name the file.
    """
    path = root / MANIFEST_NAME
    if not path.is_file():
        return ProjectManifest(modules=())
    try:
        parsed = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"{path}: cannot read project manifest: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ConfigError(f"{path}: expected an object with a 'modules' list")
    modules = parsed.get("modules", [])
    if not isinstance(modules, list) or not all(isinstance(m, str) for m in modules):
        raise ConfigError(f"{path}: 'modules' must be a list of module paths")
    return ProjectManifest(modules=tuple(modules))


def discover_from_manifest(
    manifest: ProjectManifest, *, strict: bool = True
) -> dict[str, type[Workflow]]:
    """Discover every workflow class defined by the manifest's modules.

    Args:
        manifest: Parsed project manifest naming local workflow modules.
        strict: True raises ConfigError on import failures; False skips them.

    Returns:
        This call's registrations as class-path-to-class.
    """
    return discover(manifest.modules, strict=strict, source=f"project manifest {MANIFEST_NAME}")
