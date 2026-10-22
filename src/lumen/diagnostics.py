from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import Settings
from .omlx import OmlxError, get_server_status
from .tools import tool_status


def doctor_report(settings: Settings) -> dict[str, Any]:
    tool_specs = {
        "omlx": ("--version",),
        "exiftool": ("-ver",),
        "darktable-cli": ("--version",),
        "siril-cli": ("--version",),
        "ffmpeg": ("-version",),
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
    return {
        "library": str(settings.library_dir),
        "tools": tools,
        "vision_analyzer": {
            "available": settings.vision_analyzer_path.is_file(),
            "path": str(settings.vision_analyzer_path),
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

