from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from xml.sax.saxutils import escape

from .domain import Artifact, Photo, PhotoEdit, PhotoMetrics, ProvenanceEvent


class SidecarWriter:
    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        photo: Photo,
        metrics: PhotoMetrics,
        events: list[ProvenanceEvent],
        *,
        selected: bool,
        description: str = "",
        keywords: list[str] | None = None,
        artifacts: list[Artifact] | None = None,
        active_crop: PhotoEdit | None = None,
        develop: dict | None = None,
        decision: str = "none",
        rating: int = 0,
        alt_text: str | None = None,
        place: str | None = None,
        country: str | None = None,
    ) -> tuple[Path, Path]:
        if photo.id is None:
            raise ValueError("photo must be stored before writing a sidecar")
        json_path, xmp_path = self.paths(photo)
        payload = {
            "schema_version": 1,
            "photo_id": photo.id,
            "source": {"path": photo.path, "fingerprint": photo.fingerprint, "mirror_path": photo.mirror_path},
            "capture": {
                "captured_at": photo.captured_at.isoformat() if photo.captured_at else None,
                "camera_id": photo.camera_id,
            },
            "preview_path": photo.preview_path,
            "preview_kind": photo.preview_kind,
            "measurements": asdict(metrics),
            "selection": {"best_in_group": selected, "manual": photo.manual_pick and not photo.model_pick,
                          "model": photo.model_pick},
            "decision": decision,
            "rating": rating,
            "description": description,
            "keywords": keywords or [],
            "place": place,
            "place_country": country,
            "active_crop": active_crop.to_dict() if active_crop else None,
            "artifacts": [
                {**asdict(artifact), "created_at": artifact.created_at.isoformat()}
                for artifact in artifacts or []
            ],
            "provenance": [
                {
                    "id": event.id,
                    "kind": event.kind,
                    "created_at": event.created_at.isoformat(),
                    "details": event.details,
                }
                for event in events
            ],
        }
        self._atomic_write(json_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        self._atomic_write(xmp_path, self._xmp(
            description, keywords or [], rating=-1 if decision == "reject" else rating,
            crop=active_crop.to_dict() if active_crop else None,
            crop_provenance=[
                {"id": event.id, "kind": event.kind, "created_at": event.created_at.isoformat(),
                 "details": event.details}
                for event in events if event.kind.startswith("crop_")
            ],
            ingest_provenance=[
                {"id": event.id, "created_at": event.created_at.isoformat(), "details": event.details}
                for event in events if event.kind == "ingested"
            ],
            develop=develop, alt_text=alt_text, place=place, country=country,
        ))
        return json_path, xmp_path

    def paths(self, photo: Photo) -> tuple[Path, Path]:
        stem = f"{photo.id}-{photo.fingerprint[:16]}"
        return self.directory / f"{stem}.lumen.json", self.directory / f"{stem}.xmp"

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _xmp(description: str, keywords: list[str], *, rating: int, provenance: dict | None = None, crop: dict | None = None,
             crop_provenance: list[dict] | None = None, ingest_provenance: list[dict] | None = None,
             develop: dict | None = None, alt_text: str | None = None, place: str | None = None,
             country: str | None = None) -> str:
        keyword_items = "".join(f"<rdf:li>{escape(keyword)}</rdf:li>" for keyword in keywords)
        export = f'<lumen:export>{escape(json.dumps(provenance, sort_keys=True))}</lumen:export>' if provenance else ""
        crop_payload = f'<lumen:crop>{escape(json.dumps(crop, sort_keys=True))}</lumen:crop>' if crop else ""
        # Only the controls the photographer set; its absence is what marks an untouched frame.
        develop_payload = f'<lumen:develop>{escape(json.dumps(develop, sort_keys=True))}</lumen:develop>' if develop else ""
        crop_history = (f'<lumen:cropProvenance>{escape(json.dumps(crop_provenance, sort_keys=True))}'
                        f'</lumen:cropProvenance>' if crop_provenance else "")
        ingest_history = (f'<lumen:ingested>{escape(json.dumps(ingest_provenance, sort_keys=True))}</lumen:ingested>'
                          if ingest_provenance else "")
        iptc_ns = ' xmlns:Iptc4xmpCore="http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/"' if alt_text or place else ""
        photoshop_ns = ' xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"' if place and country else ""
        location = f"<Iptc4xmpCore:Location>{escape(place)}</Iptc4xmpCore:Location>" if place else ""
        location += f"<photoshop:Country>{escape(country)}</photoshop:Country>" if place and country else ""
        alt = (f'<Iptc4xmpCore:AltTextAccessibility><rdf:Alt><rdf:li xml:lang="x-default">{escape(alt_text)}</rdf:li>'
               f'</rdf:Alt></Iptc4xmpCore:AltTextAccessibility>' if alt_text else "")
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmlns:lumen="https://lumen.local/ns/1.0/"{iptc_ns}{photoshop_ns} xmp:Rating="{rating}">
      <dc:description><rdf:Alt><rdf:li xml:lang="x-default">{escape(description)}</rdf:li></rdf:Alt></dc:description>
      <dc:subject><rdf:Bag>{keyword_items}</rdf:Bag></dc:subject>
      {alt}
      {location}
      {export}
      {crop_payload}
      {develop_payload}
      {crop_history}
      {ingest_history}
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
'''

