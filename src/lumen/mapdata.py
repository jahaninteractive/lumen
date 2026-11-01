"""Map layers from the photographer's files: GPX tracks and waypoints, GeoJSON features, and the geometry helpers
that decide what is worth drawing at a zoom level."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime

GPX_NS = "{http://www.topografix.com/GPX/1/1}"
# Below these zooms a kind is clutter (every road in a country at once); kinds not listed always draw.
KIND_MIN_ZOOM = {"road": 6, "place": 5, "river": 3, "lake": 3}
# Natural Earth names its files by layer, so the file name is the only kind a bare NE download carries.
NE_FILE_KINDS = (
    ("coastline", "coastline"),
    ("admin_0_boundary", "border"),
    ("rivers", "river"),
    ("lakes", "lake"),
    ("roads", "road"),
    ("populated_places", "place"),
)
PARK_BOUNDARIES = frozenset({"protected_area", "national_park"})


@dataclass(frozen=True)
class Feature:
    kind: str
    name: str
    rank: int
    geometry: dict

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        """(min_lat, max_lat, min_lon, max_lon) over every vertex of the geometry."""
        positions = list(_positions(self.geometry["coordinates"]))
        lons = [p[0] for p in positions]
        lats = [p[1] for p in positions]
        return min(lats), max(lats), min(lons), max(lons)


@dataclass(frozen=True)
class TrackPoint:
    time: datetime  # timezone-aware UTC, so geotagging can offset camera-local times against it
    lat: float
    lon: float
    ele: float | None


def _positions(coordinates):
    if coordinates and isinstance(coordinates[0], (int, float)):
        yield coordinates
    else:
        for part in coordinates:
            yield from _positions(part)


def _rounded(coordinates):
    if coordinates and isinstance(coordinates[0], (int, float)):
        return [round(float(value), 5) for value in coordinates]
    return [_rounded(part) for part in coordinates]


def _gpx_time(text: str) -> datetime:
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def parse_gpx(text: str) -> tuple[list[Feature], list[TrackPoint]]:
    """Tracks and routes as one LineString each (segments joined), waypoints as Points, plus every timed track
    point in time order across all tracks and segments. Untimed points still draw but cannot date a photo, so they
    are left out of the points. Raises ValueError for anything that is not GPX 1.1."""
    root = ET.fromstring(text)
    if root.tag != GPX_NS + "gpx":
        raise ValueError("not a GPX 1.1 document")
    features: list[Feature] = []
    points: list[TrackPoint] = []
    lines = [(trk, trk.iter(GPX_NS + "trkpt")) for trk in root.iter(GPX_NS + "trk")]
    lines += [(rte, rte.iter(GPX_NS + "rtept")) for rte in root.iter(GPX_NS + "rte")]
    for number, (line, vertices) in enumerate(lines, start=1):
        coordinates = []
        for vertex in vertices:
            lat, lon = float(vertex.get("lat")), float(vertex.get("lon"))
            coordinates.append([lon, lat])
            when = vertex.findtext(GPX_NS + "time")
            if line.tag == GPX_NS + "trk" and when:
                ele = vertex.findtext(GPX_NS + "ele")
                points.append(TrackPoint(_gpx_time(when.strip()), lat, lon, float(ele) if ele else None))
        if len(coordinates) >= 2:
            name = line.findtext(GPX_NS + "name") or f"Track {number}"
            features.append(Feature("track", name, 0, {"type": "LineString", "coordinates": _rounded(coordinates)}))
    for wpt in root.iter(GPX_NS + "wpt"):
        position = [float(wpt.get("lon")), float(wpt.get("lat"))]
        name = wpt.findtext(GPX_NS + "name") or ""
        features.append(Feature("waypoint", name, 0, {"type": "Point", "coordinates": _rounded(position)}))
    points.sort(key=lambda point: point.time)
    return features, points


def _kind(properties: dict, source_name: str) -> str:
    if properties.get("kind"):
        return str(properties["kind"])
    for marker, kind in NE_FILE_KINDS:
        if marker in source_name:
            return kind
    if properties.get("boundary") in PARK_BOUNDARIES or properties.get("leisure") == "nature_reserve":
        return "park"
    if properties.get("waterway"):
        return "river"
    if properties.get("natural") == "water":
        return "lake"
    if properties.get("highway"):
        return "road"
    return "other"


def parse_geojson(text: str, *, source_name: str = "") -> list[Feature]:
    """Features of a FeatureCollection, kinded by an explicit `kind` property, then the Natural Earth file name,
    then OSM-style tags, else "other". Features without coordinates are skipped. Raises ValueError for anything
    that is not a FeatureCollection."""
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        raise ValueError("not a GeoJSON FeatureCollection")
    features = []
    for item in data.get("features") or []:
        geometry = item.get("geometry") or {}
        if not geometry.get("coordinates"):
            continue
        properties = item.get("properties") or {}
        rank = int(properties["scalerank"]) if properties.get("scalerank") is not None else 0
        features.append(Feature(
            _kind(properties, source_name),
            str(properties.get("name") or ""),
            rank,
            {"type": geometry["type"], "coordinates": _rounded(geometry["coordinates"])},
        ))
    return features


def decimate(coords: list[list[float]], step: float) -> list[list[float]]:
    """Drops vertices closer than `step` degrees (in both lon and lat) to the last kept one; the first and last
    vertices always survive so a line keeps its ends."""
    if len(coords) < 2:
        return list(coords)
    kept = [coords[0]]
    for vertex in coords[1:-1]:
        last = kept[-1]
        if abs(vertex[0] - last[0]) >= step or abs(vertex[1] - last[1]) >= step:
            kept.append(vertex)
    kept.append(coords[-1])
    return kept


def pixel_degrees(zoom: float) -> float:
    """Degrees of longitude one 256-pixel-tile pixel spans at `zoom`."""
    return 360 / (256 * 2 ** zoom)
