"""Local GeoNames place index: nearest named place for a coordinate, offline unless online lookup is opted in."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

KM_PER_DEGREE = 6371.0 * math.pi / 180
RADIUS_KM = 25.0
NEAR_POPULATED_KM = 200.0
NEAR_ANY_KM = 500.0
TIER_ONE_CODES = frozenset(
    "PRK RESN RESW RESF RES RESV ISL BAY GLCR HBR COVE CAPE PEN SD STRT".split()
)
TIER_ONE_NAME = re.compile(
    r"\b(Reserve|Park|Nature|Game|Station|Island|Bay|Glacier|Harbour|Cove|Peninsula|Sound|Strait|Cape|Point)\b"
)
LANDMARK_CLASSES = frozenset("LTHP")
NON_FEATURE_FILES = frozenset({"countryInfo.txt", "readme.txt"})
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "Lumen/1.0 (local photo manager)"
ONLINE_INTERVAL_SECONDS = 1.0  # Nominatim usage policy: at most one request per second
ONLINE_NAME_FIELDS = ("nature_reserve", "national_park", "protected_area", "name", "town", "village", "city", "county")
_last_online_call: float | None = None
SCHEMA = """
CREATE TABLE IF NOT EXISTS places(id INTEGER PRIMARY KEY, name TEXT, country TEXT, feature_class TEXT,
    feature_code TEXT, population INTEGER, lat REAL, lon REAL, source TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS places_rtree USING rtree(id, min_lat, max_lat, min_lon, max_lon);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


@dataclass(frozen=True)
class Place:
    name: str
    country: str
    distance_km: float
    feature_code: str
    source: str
    near: bool = False

    @property
    def label(self) -> str:
        return f"{'near ' if self.near else ''}{self.name}, {self.country}"


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return math.hypot(x, y) * 6371.0


def country_names(source_dir: Path) -> dict[str, str]:
    """ISO code -> country name from countryInfo.txt; AQ is always Antarctica."""
    names: dict[str, str] = {}
    try:
        lines = (source_dir / "countryInfo.txt").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        columns = line.split("\t")
        if not line.startswith("#") and len(columns) > 4 and columns[4]:
            names[columns[0]] = columns[4]
    names["AQ"] = "Antarctica"
    return names


def _tier(name: str, feature_class: str, feature_code: str, source: str = "geonames") -> int:
    if source == "manual":  # the photographer's own fix outranks every looked-up name
        return 0
    # Name matches only count for landmarks, not spots/buildings (a "Cape Town" hotel is not a cape).
    if feature_code in TIER_ONE_CODES or (feature_class in LANDMARK_CLASSES and TIER_ONE_NAME.search(name)):
        return 1
    return 2 if feature_code.startswith("PPL") else 3


class PlaceIndex:
    def __init__(self, source_dir: Path, index_path: Path):
        self.source_dir = Path(source_dir)
        self.index_path = Path(index_path)
        self._connection: sqlite3.Connection | None = None
        self._ensured = False

    def __enter__(self) -> "PlaceIndex":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    @property
    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(self.index_path)
            self._connection.executescript(SCHEMA)
        return self._connection

    def _feature_files(self) -> list[Path]:
        if not self.source_dir.is_dir():
            return []
        return sorted(p for p in self.source_dir.glob("*.txt") if p.name not in NON_FEATURE_FILES)

    def ensure(self) -> dict:
        files = self._feature_files()
        manifest = json.dumps([[p.name, (s := p.stat()).st_size, s.st_mtime_ns] for p in files])
        db = self._db
        stored = db.execute("SELECT value FROM meta WHERE key = 'manifest'").fetchone()
        rebuilt = stored is None or stored[0] != manifest
        if rebuilt:
            names = country_names(self.source_dir)
            with db:
                kept = db.execute(
                    "SELECT name, country, feature_class, feature_code, population, lat, lon, source "
                    "FROM places WHERE source IN ('online', 'manual')"
                ).fetchall()
                db.execute("DELETE FROM places")
                db.execute("DELETE FROM places_rtree")
                for path in files:
                    db.executemany(
                        "INSERT INTO places(name, country, feature_class, feature_code, population, lat, lon, source) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 'geonames')",
                        _rows(path, names),
                    )
                db.executemany(
                    "INSERT INTO places(name, country, feature_class, feature_code, population, lat, lon, source) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    kept,
                )
                db.execute("INSERT INTO places_rtree SELECT id, lat, lat, lon, lon FROM places")
                db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('manifest', ?)", (manifest,))
        self._ensured = True
        places = db.execute("SELECT COUNT(*) FROM places").fetchone()[0]
        return {"files": len(files), "places": places, "rebuilt": rebuilt}

    def add(self, name: str, country: str, lat: float, lon: float, *,
            source: str = "online", feature_code: str = "ONLINE") -> int:
        db = self._db
        with db:
            cursor = db.execute(
                "INSERT INTO places(name, country, feature_class, feature_code, population, lat, lon, source) "
                "VALUES (?, ?, '', ?, 0, ?, ?, ?)",
                (name, country, feature_code, lat, lon, source),
            )
            db.execute("INSERT INTO places_rtree VALUES (?, ?, ?, ?, ?)", (cursor.lastrowid, lat, lat, lon, lon))
        return cursor.lastrowid

    def _within(self, lat: float, lon: float, radius: float, populated_only: bool = False) -> list[tuple]:
        dlat = radius / KM_PER_DEGREE
        cos_lat = math.cos(math.radians(lat))
        # ponytail: no antimeridian wrap; boxes clamp at ±180, split the query if Pacific frames matter
        dlon = 180.0 if cos_lat < 0.01 else min(180.0, radius / (KM_PER_DEGREE * cos_lat))
        rows = self._db.execute(
            "SELECT p.name, p.country, p.feature_class, p.feature_code, p.source, p.lat, p.lon FROM places_rtree r "
            "JOIN places p ON p.id = r.id "
            "WHERE r.max_lat >= ? AND r.min_lat <= ? AND r.max_lon >= ? AND r.min_lon <= ?"
            + (" AND p.feature_code LIKE 'PPL%'" if populated_only else ""),
            (lat - dlat, lat + dlat, lon - dlon, lon + dlon),
        ).fetchall()
        found = [(distance_km(lat, lon, plat, plon), name, country, code, source, feature_class)
                 for name, country, feature_class, code, source, plat, plon in rows]
        return [item for item in found if item[0] <= radius]

    def lookup(self, lat: float, lon: float) -> Place | None:
        if not self._ensured:
            self.ensure()
        close = self._within(lat, lon, RADIUS_KM)
        if close:
            distance, name, country, code, source, _ = min(close, key=lambda i: (_tier(i[1], i[5], i[3], i[4]), i[0]))
            return Place(name, country, distance, code, source)
        for radius, populated_only in ((NEAR_POPULATED_KM, True), (NEAR_ANY_KM, False)):
            found = self._within(lat, lon, radius, populated_only)
            if found:
                distance, name, country, code, source, _ = min(found)
                return Place(name, country, distance, code, source, near=True)
        return None


def online_lookup(lat: float, lon: float, *, timeout: float = 5.0, opener=None) -> Place | None:
    """Ask Nominatim for a place name; any failure or a nameless answer is None. Callers gate this on opt-in."""
    global _last_online_call
    if _last_online_call is not None:
        wait = ONLINE_INTERVAL_SECONDS - (time.monotonic() - _last_online_call)
        if wait > 0:
            time.sleep(wait)
    _last_online_call = time.monotonic()
    query = urllib.parse.urlencode({"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 10})
    request = urllib.request.Request(f"{NOMINATIM_URL}?{query}", headers={"User-Agent": USER_AGENT})
    try:
        with (opener or urllib.request.urlopen)(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except (OSError, ValueError):  # URLError/HTTPError/timeouts are OSError; bad JSON is ValueError
        return None
    if not isinstance(payload, dict):
        return None
    address = payload.get("address") if isinstance(payload.get("address"), dict) else {}
    fields = {**address, "name": payload.get("name")}
    name = next((value for key in ONLINE_NAME_FIELDS if isinstance(value := fields.get(key), str) and value.strip()), None)
    if name is None:
        return None
    country = address.get("country")
    return Place(name.strip(), country if isinstance(country, str) else "", 0.0, "ONLINE", "online")


INVERTED_NAME = re.compile(r"^(.+), (\S+)$")
ENGLISH_GENERIC = re.compile(
    r" (Harbour|Harbor|Bay|Island|Islands|Point|Cove|Glacier|Lake|Peninsula|Cape|Strait|Sound|Rock|Rocks|Beach"
    r"|Channel|Passage|Mount|Peak|Station|Base|Nunatak|Ridge|Hill|Hills|Shelf|Inlet|Bluff|Head)$"
)


def _english_name(name: str, alternates: str) -> str:
    """'Neko, puerto' -> 'Neko Harbour' from the alternates; other names unchanged."""
    if not INVERTED_NAME.match(name):
        return name
    candidates = [a for a in alternates.split(",")
                  if a.isascii() and "  " not in a and ENGLISH_GENERIC.search(a)]
    if not candidates:
        return name
    first_word = name.split()[0].rstrip(",")
    chosen = min(candidates, key=lambda a: a.split()[0] != first_word)  # min keeps the first on ties
    british = chosen[: -len("Harbor")] + "Harbour"
    return british if chosen.endswith(" Harbor") and british in candidates else chosen


def _rows(path: Path, names: dict[str, str]):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            columns = line.rstrip("\n").split("\t")
            try:
                lat, lon = float(columns[4]), float(columns[5])
                population = int(columns[14] or 0)
                code = columns[8]
                yield _english_name(columns[1], columns[3]), names.get(code, code), columns[6], columns[7], population, lat, lon
            except (IndexError, ValueError):
                continue
