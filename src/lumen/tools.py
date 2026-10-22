from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

VENDOR_APPS = Path(__file__).resolve().parents[2] / "vendor" / "apps"
_BUNDLES = {"darktable-cli": "darktable.app", "siril-cli": "Siril.app"}


@dataclass(frozen=True, slots=True)
class ToolStatus:
    name: str
    available: bool
    path: str | None
    version: str | None = None


def find_tool(name: str) -> str | None:
    """PATH first, then the project-local app bundle; never a path that resolves outside it."""
    if path := shutil.which(name):
        return path
    if name not in _BUNDLES:
        return None
    bundle = VENDOR_APPS.resolve() / _BUNDLES[name]
    executable = (bundle / "Contents" / "MacOS" / name).resolve()
    if executable.is_relative_to(bundle) and executable.is_file() and os.access(executable, os.X_OK):
        return str(executable)
    return None


def tool_status(name: str, version_args: tuple[str, ...] = ("--version",)) -> ToolStatus:
    path = find_tool(name)
    if not path:
        return ToolStatus(name=name, available=False, path=None)
    version = None
    try:
        result = subprocess.run([path, *version_args], capture_output=True, text=True, timeout=5, check=False)
        version = (result.stdout or result.stderr).splitlines()[0][:160]
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    return ToolStatus(name=name, available=True, path=path, version=version)

