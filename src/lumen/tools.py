from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolStatus:
    name: str
    available: bool
    path: str | None
    version: str | None = None


def tool_status(name: str, version_args: tuple[str, ...] = ("--version",)) -> ToolStatus:
    path = shutil.which(name)
    if not path:
        return ToolStatus(name=name, available=False, path=None)
    version = None
    try:
        result = subprocess.run([path, *version_args], capture_output=True, text=True, timeout=5, check=False)
        version = (result.stdout or result.stderr).splitlines()[0][:160]
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    return ToolStatus(name=name, available=True, path=path, version=version)

