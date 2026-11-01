from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import __version__
from .config import Settings
from .geonames import PlaceIndex
from .omlx import ROLES, OmlxError, get_server_status
from .astro import probe_siril
from .tools import tool_status


_PACKAGE_DIR = Path(__file__).resolve().parent
_HELPER_SOURCE = _PACKAGE_DIR.parents[1] / "tools" / "raw_developer.swift"


def source_digest() -> str:
    """A fingerprint of the Python this package is made of."""
    digest = hashlib.sha256()
    for path in sorted(_PACKAGE_DIR.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


# Taken as the process loads. The server serves static files from disk on every request but
# keeps the Python it imported at startup, so the page always looks current even when the
# routes behind it are not. Comparing this later is how it notices.
LOADED_DIGEST = source_digest()


def stale_sources(settings: Settings, loaded_digest: str | None = None) -> list[str]:
    """Why a restart or a rebuild is needed, in words the photographer can act on.

    Reaches the browser, so it names no paths: the contract forbids them in responses.
    """
    reasons: list[str] = []
    try:
        if source_digest() != (LOADED_DIGEST if loaded_digest is None else loaded_digest):
            reasons.append("This server is running code older than the files on disk. "
                           "Restart it with lumen serve, then reload this page.")
    except OSError:  # an unreadable source tree is not something to report as staleness
        pass
    helper = settings.raw_developer_path
    try:
        if _HELPER_SOURCE.is_file() and helper.is_file() \
                and _HELPER_SOURCE.stat().st_mtime > helper.stat().st_mtime:
            reasons.append("The raw developer was changed but not rebuilt. "
                           "Run scripts/bootstrap.sh, then reload this page.")
    except OSError:
        pass
    return reasons


def doctor_report(settings: Settings) -> dict[str, Any]:
    tool_specs = {
        "omlx": ("--version",),
        "exiftool": ("-ver",),
        "darktable-cli": ("--version",),
        "siril-cli": ("--version",),
        "swiftc": ("--version",),
    }
    tools = {}
    for name, arguments in tool_specs.items():
        status = tool_status(name, arguments)
        tools[name] = {
            "available": status.available,
            "path": status.path,
            "version": status.version,
            "required": name in {"exiftool"},
        }
        if name == "siril-cli" and status.path:
            try:
                siril = probe_siril(status.path)
                tools[name] |= {"version": siril.version_text, "supported": siril.supported}
            except RuntimeError:
                tools[name]["supported"] = False
    model_path = _find_model(settings.omlx_model)
    vision = False
    complete = False
    if model_path:
        try:
            config = json.loads((model_path / "config.json").read_text())
            vision = isinstance(config.get("vision_config"), dict)
            complete = vision and any(model_path.glob("*.safetensors"))
        except (OSError, json.JSONDecodeError):
            pass
    try:
        live = get_server_status(settings.omlx_url)
        loaded_models = live["loaded_models"]
        target_loaded = settings.omlx_model in loaded_models
        if target_loaded:
            summary = f"{settings.omlx_model} · loaded and selected"
        elif loaded_models:
            summary = f"Loaded: {', '.join(loaded_models)} · Lumen target: {settings.omlx_model}"
        else:
            summary = f"oMLX online · no model loaded · target: {settings.omlx_model}"
        live_report = live | {
            "online": True,
            "target_loaded": target_loaded,
            "summary": summary,
        }
    except (OmlxError, ValueError) as error:
        live_report = {
            "online": False,
            "loaded_models": [],
            "default_model": None,
            "version": None,
            "target_loaded": False,
            "summary": f"{settings.omlx_model} target · oMLX offline",
            "error": str(error),
        }
    roles = {role: {"model": (model := getattr(settings, attribute)), "loaded": model in live_report["loaded_models"]}
             for role, attribute in ROLES.items()}
    geonames_dir = settings.geonames_dir
    geonames = {"dir": str(geonames_dir) if geonames_dir else None, "files": 0, "places": 0,
                "online": settings.geocode_online}
    if geonames_dir and geonames_dir.is_dir():
        with PlaceIndex(geonames_dir, settings.geonames_index_path) as index:
            counts = index.ensure()
        geonames |= {"files": counts["files"], "places": counts["places"]}
    map_dir = settings.map_dir
    return {
        "version": __version__,
        "library": str(settings.library_dir),
        "tools": tools,
        "vision_analyzer": {
            "available": settings.vision_analyzer_path.is_file(),
            "path": str(settings.vision_analyzer_path),
        },
        "raw_developer": {
            "available": settings.raw_developer_path.is_file(),
            "path": str(settings.raw_developer_path),
        },
        "omlx_model": {
            "name": settings.omlx_model,
            "path": str(model_path) if model_path else None,
            "vision_config": vision,
            "complete": complete,
            "recommendation": (
                "Use the installed model; no download needed."
                if complete
                else "Finish or install a vision-capable MLX model, then run `lumen doctor --json` again."
            ),
        },
        "omlx_live": live_report,
        "omlx_roles": roles,
        "geonames": geonames,
        "map": {"dir": str(map_dir) if map_dir else None,
                "layers": len(list(map_dir.glob("*.geojson"))) if map_dir and map_dir.is_dir() else 0},
    }


def _find_model(model: str) -> Path | None:
    candidates: list[Path] = [Path.home() / "Models" / "mlx-community" / model]
    for settings_path in (
        Path.home() / ".omlx" / "settings.json",
        Path.home() / "Library" / "Application Support" / "oMLX" / "config.json",
    ):
        try:
            data = json.loads(settings_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        model_settings = data.get("model", {}) if isinstance(data.get("model"), dict) else {}
        configured = [data.get("model_dir"), model_settings.get("model_dir")]
        configured.extend(model_settings.get("model_dirs", []) if isinstance(model_settings.get("model_dirs"), list) else [])
        for value in configured:
            if not isinstance(value, str) or not value:
                continue
            root = Path(value).expanduser()
            candidates.extend((root / model, root / "mlx-community" / model))
    for candidate in candidates:
        if (candidate / "config.json").is_file():
            return candidate.resolve()
    return None

