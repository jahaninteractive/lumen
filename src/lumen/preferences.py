"""Settings the photographer saves from the Settings view, kept in the catalog's preferences table.

A shell variable always wins: a key whose variable is set cannot be saved and keeps the environment's value.
"""
from __future__ import annotations

import json
import os
import unicodedata
from dataclasses import replace

from . import omlx
from .catalog import Catalog
from .config import Settings

ENGINES = ("mac", "base", "online")
ENGINE_LABELS = {"mac": "This Mac", "base": "Base", "online": "Online"}

# setting key (the Settings field name) -> shell variable that fixes it
SAVED: dict[str, str | None] = {
    "ai_engine": None,
    "base_url": None,
    "base_fast_model": None,
    "base_review_model": None,
    "online_url": None,
    "online_fast_model": None,
    "online_review_model": None,
    "near_duplicate_distance": "LUMEN_NEAR_DUP_DISTANCE",
    "geocode_online": "LUMEN_GEOCODE_ONLINE",
}

# The one agreed place service names appear in code: the Online engine's presets.
# listing_headers / listing_key_header: what that host's model listing needs beyond the standard request (omlx.list_models).
SERVICE_PRESETS: tuple[dict, ...] = (
    {"name": "Anthropic", "url": "https://api.anthropic.com/v1",
     "listing_headers": {"anthropic-version": "2023-06-01"}, "listing_key_header": "x-api-key"},
    {"name": "Google", "url": "https://generativelanguage.googleapis.com/v1beta/openai"},
    {"name": "OpenAI", "url": "https://api.openai.com/v1"},
)

MODEL_NAME_MAX = 200
_MODEL_KEYS = {"base_fast_model", "base_review_model", "online_fast_model", "online_review_model"}
_URL_KEYS = {"base_url", "online_url"}


def validate(key: str, value) -> object:
    """The stored form of a setting, or ValueError with a sentence for the page."""
    if key not in SAVED:
        raise ValueError(f"unknown setting: {key}")
    if key == "ai_engine":
        if value not in ENGINES:
            raise ValueError("The engine must be This Mac, Base or Online.")
    elif key == "near_duplicate_distance":
        if type(value) is not int or not 0 <= value <= 64:
            raise ValueError("The near-duplicate distance must be a whole number from 0 to 64.")
    elif key == "geocode_online":
        if type(value) is not bool:
            raise ValueError("Online place lookup must be on or off.")
    elif key in _MODEL_KEYS:
        if type(value) is not str or len(value) > MODEL_NAME_MAX or any(unicodedata.category(c) == "Cc" for c in value):
            raise ValueError(f"A model name must be text of at most {MODEL_NAME_MAX} characters on one line.")
    elif key in _URL_KEYS:
        if type(value) is not str:
            raise ValueError("An address must be text.")
        if value:  # "" clears it
            value = omlx.normalise_engine_url(key.removesuffix("_url"), value)
    return value


def _shell_fixed(key: str) -> bool:
    return SAVED[key] is not None and SAVED[key] in os.environ


def _saved(catalog: Catalog) -> dict:
    """Every saved, readable, valid value whose key the shell does not fix."""
    values = {}
    for key in SAVED:
        raw = catalog.get_preference(f"setting.{key}")
        if raw is None or _shell_fixed(key):
            continue
        try:
            values[key] = validate(key, json.loads(raw))
        except ValueError:  # unreadable or no longer valid: the default stands
            continue
    return values


def overlay(settings: Settings, catalog: Catalog) -> Settings:
    return replace(settings, **_saved(catalog))


def load(settings: Settings) -> Settings:
    if not settings.database_path.exists():
        return settings
    with Catalog(settings.database_path) as catalog:
        return overlay(settings, catalog)


def save(catalog: Catalog, changes: dict) -> None:
    stored = {}
    for key, value in changes.items():
        stored[key] = validate(key, value)
        if _shell_fixed(key):
            raise ValueError(f"{key} is set by your shell ({SAVED[key]})")
    for key, value in stored.items():  # every value is checked before the first write
        catalog.set_preference(f"setting.{key}", json.dumps(value))


def describe(settings: Settings, catalog: Catalog) -> list[dict]:
    saved = _saved(catalog)
    rows = []
    for key in SAVED:
        source = "shell" if _shell_fixed(key) else "saved" if key in saved else "default"
        rows.append({"key": key, "value": saved.get(key, getattr(settings, key)), "source": source,
                     "editable": source != "shell"})
    return rows
