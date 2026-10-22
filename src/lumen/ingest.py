"""Verified card ingest: plan, copy with SHA-256 verification, classify reruns, and record a resumable manifest.

The card is read-only: nothing under it is opened for writing and no file is created there. Each copy streams
into a partial file beside its destination, is fsynced, re-read, compared, and only then renamed into place.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .domain import SUPPORTED_EXTENSIONS
from .previews import fingerprint_file, read_metadata
from .sidecars import SidecarWriter

VERIFIED = frozenset({"copied", "already_present"})


class IngestVerifyError(OSError):
    """A copy whose re-read hash never matched the card."""


@dataclass(frozen=True, slots=True)
class IngestItem:
    relative: str
    source: Path
    size: int
    captured_date: str
    destinations: tuple[Path, ...]


def validate_ingest(card: Path, destinations: Sequence[Path], *, library_dir: Path) -> tuple[Path, tuple[Path, ...]]:
    """Resolve and check the card and destinations before anything is copied; raises ValueError."""
    card = card.expanduser().resolve()
    if not card.is_dir():
        raise ValueError("card folder does not exist")
    library = library_dir.expanduser().resolve()
    if card.is_relative_to(library):
        raise ValueError("the card cannot be inside the Lumen library")
    if not destinations:
        raise ValueError("ingest needs at least one destination")
    roots = tuple(path.expanduser().resolve() for path in destinations)
    if len(set(roots)) != len(roots):
        raise ValueError("each destination must be a different folder")
    for root in roots:
        if not root.is_dir():  # An unmounted drive must not silently become a folder on the startup disk.
            raise ValueError(f"destination folder does not exist: {root.name}")
        if root.is_relative_to(library):
            raise ValueError("destinations must be outside the Lumen library")
        if root.is_relative_to(card) or card.is_relative_to(root):
            raise ValueError("a destination cannot contain the card or be inside it")
    return card, roots


def _card_files(card: Path):
    """(relative path, supported) for every regular, visible file; symlinks and hidden entries are never followed."""
    for directory, folders, files in os.walk(card, followlinks=False):
        folders[:] = sorted(name for name in folders if not name.startswith("."))
        for name in sorted(files):
            path = Path(directory) / name
            if name.startswith(".") or path.is_symlink() or not path.is_file():
                continue
            yield path.relative_to(card).as_posix(), path.suffix.lower() in SUPPORTED_EXTENSIONS


def unplanned_files(card: Path) -> list[str]:
    """Visible card files Lumen does not copy (videos, notes); their presence means the card is not fully verified."""
    return [relative for relative, supported in _card_files(card.expanduser().resolve()) if not supported]


def _captured_date(path: Path, today: date) -> str:
    captured = read_metadata(path).get("captured_at")
    if captured:
        try:
            return datetime.fromisoformat(captured).date().isoformat()
        except ValueError:
            pass
    try:
        mtime = path.stat().st_mtime
        if mtime > 0:  # Epoch-zero mtimes are a camera without a clock, not a date.
            return datetime.fromtimestamp(mtime).date().isoformat()
    except (OSError, OverflowError, ValueError):
        pass
    return today.isoformat()


def plan_ingest(card: Path, destinations: Sequence[Path], *, library_dir: Path, today: date) -> list[IngestItem]:
    card, roots = validate_ingest(card, destinations, library_dir=library_dir)
    items = []
    for relative, supported in _card_files(card):
        if not supported:
            continue
        source = card / relative
        captured = _captured_date(source, today)
        items.append(IngestItem(relative, source, source.stat().st_size, captured,
                                tuple(root / captured / relative for root in roots)))
    return items


def _partial(destination: Path) -> Path:
    return destination.with_name(f".{destination.name}.lumen-partial")


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def copy_verified(source: Path, destination: Path, *, chunk_size: int = 1 << 20, hasher=hashlib.sha256) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = _partial(destination)
    for _attempt in range(2):
        try:
            source_digest = hasher()
            with source.open("rb") as reader, partial.open("wb") as writer:
                for chunk in iter(lambda: reader.read(chunk_size), b""):
                    source_digest.update(chunk)
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            _fsync_directory(partial.parent)
            copy_digest = hasher()
            with partial.open("rb") as reader:
                for chunk in iter(lambda: reader.read(chunk_size), b""):
                    copy_digest.update(chunk)
            expected = source_digest.hexdigest()
            if copy_digest.hexdigest() == expected:
                if os.path.lexists(destination):  # Never replace a file that appeared meanwhile.
                    raise FileExistsError(f"a different file already exists at {destination.name}")
                partial.replace(destination)
                _fsync_directory(destination.parent)
                return expected
        finally:
            partial.unlink(missing_ok=True)
    raise IngestVerifyError(f"copy of {source.name} did not verify after one retry")


def classify_existing(destination: Path, source_hash: str) -> str | None:
    _partial(destination).unlink(missing_ok=True)  # Leftover of an interrupted copy: never evidence of a copy.
    if destination.is_file() and not destination.is_symlink() and fingerprint_file(destination) == source_hash:
        return "already_present"
    return None


def ingest_item(item: IngestItem, roots: Sequence[Path]) -> dict:
    """Copy one card file to every destination in order and return its manifest entry."""
    entry = {"relative": item.relative, "size": item.size, "sha256": None,
             "captured_date": item.captured_date, "outcomes": {}}
    errors = []
    for root, final in zip(roots, item.destinations):
        try:
            if os.path.lexists(final) and entry["sha256"] is None:
                entry["sha256"] = fingerprint_file(item.source)
            outcome = classify_existing(final, entry["sha256"] or "")
            if outcome is None:
                if os.path.lexists(final):
                    raise FileExistsError(f"a different file already exists at {final.name}")
                digest = copy_verified(item.source, final)
                if entry["sha256"] not in (None, digest):
                    raise IngestVerifyError(f"{item.source.name} changed on the card during the copy")
                entry["sha256"] = digest
                outcome = "copied"
        except OSError as error:
            outcome = "failed"
            errors.append(f"{root.name}: {error.strerror or error}")  # strerror keeps paths out of the message
        entry["outcomes"][str(root)] = outcome
    if errors:
        entry["error"] = "; ".join(errors)
    return entry


def summarize(files: Sequence[dict], roots: Sequence[Path], *, planned: int, skipped: int = 0) -> dict:
    """Per-file counts: a file is verified only when every destination holds a verified copy."""
    counts = {"copied": 0, "verified": 0, "already_present": 0, "failed": 0, "files": planned, "skipped": skipped}
    for entry in files:
        outcomes = [entry["outcomes"].get(str(root)) for root in roots]
        if all(outcome in VERIFIED for outcome in outcomes):
            counts["verified"] += 1
            counts["copied" if "copied" in outcomes else "already_present"] += 1
        else:
            counts["failed"] += 1
    counts["card_verified_everywhere"] = counts["verified"] == planned and len(files) == planned and skipped == 0
    return counts


def write_manifest(path: Path, manifest: dict) -> None:
    SidecarWriter._atomic_write(path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
