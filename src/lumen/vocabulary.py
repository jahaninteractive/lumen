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


def load_blocked(settings: Settings) -> list[str]:
    """Casefolded words the photographer forgot; they are never learned again."""
    load_vocabulary(settings)  # copies the starter in and validates the file
    blocked = json.loads(settings.vocabulary_path.read_text(encoding="utf-8")).get("blocked", [])
    if not isinstance(blocked, list) or not all(isinstance(word, str) for word in blocked):
        raise ValueError("vocabulary.json is invalid: blocked must be a list of keywords")
    return [word.strip().casefold() for word in blocked]


def _save(settings: Settings, **fields) -> None:
    payload = json.loads(settings.vocabulary_path.read_text(encoding="utf-8")) | fields
    SidecarWriter._atomic_write(settings.vocabulary_path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def forget_keyword(settings: Settings, word: str) -> bool:
    """Drop a word (any case) from every category and block it; True when anything changed."""
    key = word.strip().casefold()
    vocab = load_vocabulary(settings)
    blocked = load_blocked(settings)
    kept = {name: [w for w in words if w.casefold() != key] for name, words in vocab.items()}
    if kept == vocab and key in blocked:
        return False
    _save(settings, categories=kept, blocked=blocked + ([] if key in blocked else [key]))
    return True


def learn_keywords(settings: Settings, words_by_category: dict[str, list[str]]) -> list[str]:
    """Append words the model (or the photographer) used to <library>/vocabulary.json; returns the words actually added."""
    vocab = load_vocabulary(settings)
    known = {word.casefold() for word in all_keywords(vocab)} | set(load_blocked(settings))
    added = []
    for category, words in words_by_category.items():
        for word in words:
            clean = word.strip()
            if not clean or clean.casefold() in known:
                continue
            vocab.setdefault(category, []).append(clean)
            known.add(clean.casefold())
            added.append(clean)
    if added:
        _save(settings, categories=vocab)
    return added
