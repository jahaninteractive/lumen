from __future__ import annotations

import json
from importlib import resources

from .config import Settings
from .sidecars import SidecarWriter

MAX_SUGGESTED = 3


def load_vocabulary(settings: Settings) -> dict[str, list[str]]:
    """The photographer's keyword list; the packaged starter is copied in once and never overwritten."""
    path = settings.vocabulary_path
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        SidecarWriter._atomic_write(path, resources.files("lumen").joinpath("data/vocabulary.json").read_text(encoding="utf-8"))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"vocabulary.json is invalid: {error}") from error
    categories = payload.get("categories") if isinstance(payload, dict) else None
    if not isinstance(categories, dict) or not all(
            isinstance(words, list) and all(isinstance(word, str) and word.strip() for word in words)
            for words in categories.values()):
        raise ValueError("vocabulary.json is invalid: categories must map names to lists of keywords")
    return {name: [word.strip() for word in words] for name, words in categories.items()}


def all_keywords(vocab: dict[str, list[str]]) -> list[str]:
    return [word for words in vocab.values() for word in words]


def match_keywords(candidates: list[str], vocab: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    """(applied in the vocabulary's spelling, up to three free-text suggestions); case and whitespace insensitive."""
    canonical = {word.casefold(): word for word in reversed(all_keywords(vocab))}  # first spelling wins
    applied, suggested, seen = [], [], set()
    for candidate in candidates:
        key = candidate.strip().casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        if key in canonical:
            applied.append(canonical[key])
        elif len(suggested) < MAX_SUGGESTED:
            suggested.append(candidate.strip())
    return applied, suggested
