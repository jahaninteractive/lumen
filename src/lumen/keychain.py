"""API keys for the Base and Online engines, kept in the macOS Keychain under service `Lumen`.

A key is a secret: it never goes on a command line (where `ps` would show it), in an
error message, or in a log line. `set_key` hands it to `security -i` on stdin.
"""
from __future__ import annotations

import re
import subprocess

SECURITY = "/usr/bin/security"
SERVICE = "Lumen"
KEY_ENGINES = ("base", "online")
KEY_PATTERN = re.compile(r"[A-Za-z0-9._\-+/=:~]{8,512}")  # no quotes or spaces, so it is safe inside the quoted -w "..."
TIMEOUT = 15


def _engine(engine: str) -> str:
    if engine not in KEY_ENGINES:
        raise ValueError("only Base and Online have keys")
    return engine


def _run(run, argv: list[str], **kwargs) -> subprocess.CompletedProcess | None:
    """The finished process, or None when `security` is missing or hangs (a Keychain dialog left open)."""
    try:
        result = run(argv, capture_output=True, text=True, check=False, timeout=TIMEOUT, **kwargs)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return result


def get_key(engine: str, run=subprocess.run) -> str | None:
    result = _run(run, [SECURITY, "find-generic-password", "-s", SERVICE, "-a", _engine(engine), "-w"])
    return result.stdout.strip() if result is not None and result.returncode == 0 else None


def set_key(engine: str, key: str, run=subprocess.run) -> None:
    _engine(engine)
    if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
        raise ValueError("that does not look like an API key")
    result = _run(run, [SECURITY, "-i"], input=f'add-generic-password -U -s {SERVICE} -a {engine} -w "{key}"\n')
    # `security -i` can exit 0 when the command it read failed, so the read-back is the real check.
    if result is None or result.returncode != 0 or get_key(engine, run) != key:
        raise RuntimeError("could not save the key to the Keychain")


def delete_key(engine: str, run=subprocess.run) -> None:
    """Removes the key; a key that was never saved is not an error."""
    if _run(run, [SECURITY, "delete-generic-password", "-s", SERVICE, "-a", _engine(engine)]) is None:
        raise RuntimeError("could not remove the key from the Keychain")
