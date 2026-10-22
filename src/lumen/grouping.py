from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

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
            if photo.camera_id == previous.camera_id and gap <= self.gap_seconds:
                current.append(photo)
            else:
                groups.append(current)
                current = [photo]
        if current:
            groups.append(current)
        groups.extend([[photo] for photo in missing])
        groups.sort(key=lambda group: (_utc(group[0].captured_at) if group[0].captured_at else datetime.max.replace(tzinfo=timezone.utc), group[0].camera_id, group[0].path))
        return groups


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.max.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

