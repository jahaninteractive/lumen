"""Verified card ingest: plan, copy with SHA-256 verification, classify reruns, and record a resumable manifest.

The card is read-only: nothing under it is opened for writing and no file is created there. Every visible regular
file is copied regardless of extension (only photos are imported later). Each copy streams into a partial file beside
its destination, is flushed to the media, re-read from the device, compared, and only then renamed into place.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
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


def _device(path: Path) -> int:
    return os.stat(path).st_dev


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
        if _device(root) == _device(card):  # Writing beside the card on its own volume would write to the card.
            raise ValueError("destination is on the same volume as the card")
    return card, roots


@dataclass(frozen=True, slots=True)
class CardScan:
    files: tuple[tuple[str, os.stat_result], ...]  # every visible regular file, any extension
    hidden: tuple[str, ...]  # dot-prefixed entries: reported, never copied, never blocking
    unreadable: tuple[str, ...]  # entries Lumen could not list or stat: each one blocks the verdict


def scan_card(card: Path) -> CardScan:
    """Walk the card without following symlinks; any listing or stat error is recorded, never dropped."""
    card = card.expanduser().resolve()
    files, hidden, unreadable = [], [], []

    def relative(path: str) -> str:
        return Path(path).relative_to(card).as_posix() if Path(path) != card else "."

    for directory, folders, names in os.walk(card, onerror=lambda error: unreadable.append(relative(error.filename or card)),
                                             followlinks=False):
        hidden.extend(relative(os.path.join(directory, name)) for name in folders if name.startswith("."))
        folders[:] = sorted(name for name in folders if not name.startswith("."))
        for name in sorted(names):
            path = os.path.join(directory, name)
            if name.startswith("."):
                hidden.append(relative(path))
                continue
            try:
                status = os.lstat(path)
            except OSError:
                unreadable.append(relative(path))
                continue
            if stat.S_ISREG(status.st_mode):
                files.append((relative(path), status))
            elif not stat.S_ISLNK(status.st_mode):
                unreadable.append(relative(path))  # sockets, devices: not copyable, so not verified
    return CardScan(tuple(files), tuple(sorted(hidden)), tuple(sorted(unreadable)))


def _captured_date(path: Path, mtime: float, today: date) -> str:
    if path.suffix.lower() in SUPPORTED_EXTENSIONS:
        captured = read_metadata(path).get("captured_at")
        if captured:
            try:
                return datetime.fromisoformat(captured).date().isoformat()
            except ValueError:
                pass
    try:
        if mtime > 0:  # Epoch-zero mtimes are a camera without a clock, not a date.
            return datetime.fromtimestamp(mtime).date().isoformat()
    except (OSError, OverflowError, ValueError):
        pass
    return today.isoformat()


def plan_ingest(card: Path, destinations: Sequence[Path], *, library_dir: Path, today: date,
                scan: CardScan | None = None) -> list[IngestItem]:
    card, roots = validate_ingest(card, destinations, library_dir=library_dir)
    items = []
    for relative, status in (scan or scan_card(card)).files:
        source = card / relative
        captured = _captured_date(source, status.st_mtime, today)
        items.append(IngestItem(relative, source, status.st_size, captured,
                                tuple(root / captured / relative for root in roots)))
    return items


def _partial(destination: Path) -> Path:
    return destination.with_name(f".{destination.name}.lumen-partial")


def full_fsync(descriptor: int) -> None:
    """Flush to the media: on macOS plain fsync leaves bytes in the drive cache, F_FULLFSYNC does not."""
    if hasattr(fcntl, "F_FULLFSYNC"):
        try:
            fcntl.fcntl(descriptor, fcntl.F_FULLFSYNC)
            return
        except OSError:  # Filesystems without F_FULLFSYNC support fall back to fsync.
            pass
    os.fsync(descriptor)


def fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        full_fsync(descriptor)
    finally:
        os.close(descriptor)


def _hash_uncached(path: Path, hasher, chunk_size: int) -> str:
    """Re-read bypassing the unified buffer cache so the verify pass sees the device, not RAM."""
    descriptor = os.open(path, os.O_RDONLY)
    if hasattr(fcntl, "F_NOCACHE"):
        fcntl.fcntl(descriptor, fcntl.F_NOCACHE, 1)
    digest = hasher()
    with os.fdopen(descriptor, "rb") as reader:
        for chunk in iter(lambda: reader.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
                full_fsync(writer.fileno())
            fsync_directory(partial.parent)
            expected = source_digest.hexdigest()
            if _hash_uncached(partial, hasher, chunk_size) == expected:
                if os.path.lexists(destination):  # Never replace a file that appeared meanwhile.
                    raise FileExistsError(f"a different file already exists at {destination.name}")
                partial.replace(destination)
                fsync_directory(destination.parent)
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
            label = "primary" if root == roots[0] else "mirror"  # labels and strerror keep paths out of the message
            errors.append(f"{label}: {error.strerror or error}")
        entry["outcomes"][str(root)] = outcome
    if errors:
        entry["error"] = "; ".join(errors)
    return entry


def summarize(files: Sequence[dict], roots: Sequence[Path], *, planned: int, skipped: int = 0, unreadable: int = 0) -> dict:
    """Per-file counts: a file is verified only when every destination holds a verified copy.

    Hidden entries (``skipped``) are reported only; anything Lumen could not read blocks the verdict, as does an
    empty plan, so an unreadable card never reads as verified.
    """
    counts = {"copied": 0, "verified": 0, "already_present": 0, "failed": 0, "files": planned,
              "skipped": skipped, "unreadable": unreadable}
    for entry in files:
        outcomes = [entry["outcomes"].get(str(root)) for root in roots]
        if all(outcome in VERIFIED for outcome in outcomes):
            counts["verified"] += 1
            counts["copied" if "copied" in outcomes else "already_present"] += 1
        else:
            counts["failed"] += 1
    counts["card_verified_everywhere"] = (planned > 0 and counts["verified"] == planned and len(files) == planned
                                          and unreadable == 0)
    return counts


def write_manifest(path: Path, manifest: dict) -> None:
    SidecarWriter._atomic_write(path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
