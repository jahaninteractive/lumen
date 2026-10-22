from __future__ import annotations

from dataclasses import dataclass

from .domain import Photo, PhotoMetrics


@dataclass(frozen=True, slots=True)
class RankedPhoto:
    photo: Photo
    metrics: PhotoMetrics
    total: float
    components: dict[str, float]


def rank_group(items: list[tuple[Photo, PhotoMetrics]]) -> list[RankedPhoto]:
    ranked: list[RankedPhoto] = []
    for photo, metrics in items:
        components = {"sharpness": metrics.sharpness, "exposure": metrics.exposure}
        weights = {"sharpness": 0.5, "exposure": 0.25}
        face_values = [value for value in (metrics.face_quality, metrics.eye_quality) if value is not None]
        if face_values:
            components["face_eye"] = sum(face_values) / len(face_values)
            weights["face_eye"] = 0.25
        total_weight = sum(weights.values())
        total = sum(components[name] * weight for name, weight in weights.items()) / total_weight
        ranked.append(RankedPhoto(photo=photo, metrics=metrics, total=round(total, 6), components=components))
    return sorted(ranked, key=lambda item: (-item.total, item.photo.path))

