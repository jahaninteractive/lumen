from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .analysis import hamming
from .domain import Photo


@dataclass(frozen=True, slots=True)
class TimeGapGroupingPolicy:
    gap_seconds: float = 2.0

    def group(self, photos: list[Photo]) -> list[list[Photo]]:
        dated = [photo for photo in photos if photo.captured_at is not None]
        missing = sorted((photo for photo in photos if photo.captured_at is None), key=lambda item: item.path)
        dated.sort(key=lambda item: (item.camera_id, _utc(item.captured_at), item.path))

        groups: list[list[Photo]] = []
        current: list[Photo] = []
        for photo in dated:
            if not current:
                current = [photo]
                continue
            previous = current[-1]
            gap = (_utc(photo.captured_at) - _utc(previous.captured_at)).total_seconds()
            # Doubled exposure: in-camera long-exposure noise reduction blanks the shutter for a second exposure.
            allowed = self.gap_seconds + 2 * (previous.exposure_seconds or 0)
            if photo.camera_id == previous.camera_id and gap <= allowed:
                current.append(photo)
            else:
                groups.append(current)
                current = [photo]
        if current:
            groups.append(current)
        groups.extend([[photo] for photo in missing])
        groups.sort(key=lambda group: (_utc(group[0].captured_at) if group[0].captured_at else datetime.max.replace(tzinfo=timezone.utc), group[0].camera_id, group[0].path))
        return groups


def split_near_duplicates(group: list[Photo], hashes: dict[int, str | None], max_distance: int) -> list[list[Photo]]:
    """Split a burst where a frame's hash is more than max_distance bits from its cluster's first frame; a missing hash never splits."""
    if max_distance <= 0:
        return [group]
    clusters: list[list[Photo]] = []
    for photo in group:
        anchor = hashes.get(clusters[-1][0].id) if clusters else None
        current = hashes.get(photo.id)
        if not clusters or (anchor is not None and current is not None and hamming(anchor, current) > max_distance):
            clusters.append([photo])
        else:
            clusters[-1].append(photo)
    return clusters


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.max.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

