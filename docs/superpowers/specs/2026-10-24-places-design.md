# Places: turn GPS into names the catalog, sidecars, and models can use

Date: 2026-10-24. The user noticed the GeoNames files under `vendor/data/geonames/` (downloaded 22 Oct) are not used anywhere. Decided in chat: build a local place index; an optional, off-by-default online lookup whose answers are cached into that index; `ZA.txt` added beside `AQ.txt` and `cities500.txt`.

## Intent

Every frame with a GPS fix gets a human place name and country, so the inspector, filters, sidecars, JPEG exports, keywords, descriptions, captions, and digests say "Madikwe Game Reserve, South Africa" or "Neko Harbour, Antarctica" instead of nothing.

## Goals

1. **Local place index.** `geonames.py` builds `<library>/cache/geonames.sqlite` (places + R-tree) from every `*.txt` feature file in the GeoNames folder (`LUMEN_GEONAMES_DIR`, default `<project>/vendor/data/geonames`), rebuilt when those files change. `lookup(lat, lon)` ranks by tier, then distance: tier 1 protected/natural landmarks (feature codes PRK, RESN, RESW, RESF, RES, RESV, ISL, BAY, GLCR, HBR, COVE, CAPE, PEN, SD, STRT, or a name matching `Reserve|Park|Nature|Game|Station|Island|Bay|Glacier|Harbour|Cove|Peninsula|Sound|Strait|Cape|Point`), tier 2 populated places (`PPL*`), tier 3 anything else; search radius 25 km. Beyond that, the nearest populated place within 200 km is reported as "near <name>"; beyond that the nearest feature within 500 km (Antarctic seas); else no place. Country name from `countryInfo.txt` (`AQ` → "Antarctica").
2. **Place on the photo.** `photos.place` (text) and `photos.place_country` resolved on import for frames with coordinates, backfilled for existing frames on reimport, shown in the inspector beside the coordinates, searched by the text filter, written to JSON and XMP sidecars (`Iptc4xmpCore:Location`, `photoshop:Country`) and to exported JPEG XMP.
3. **Place in the AI work.** Tag and caption prompts state the place as context ("Taken at Madikwe Game Reserve, South Africa"); the place name and country are applied as keywords under a `places` vocabulary category (learned into the photographer's file like other words); day digests list places with frame counts.
4. **Online lookup, opt-in.** `LUMEN_GEOCODE_ONLINE=1` enables a reverse lookup against OpenStreetMap Nominatim for frames the local index cannot name within 25 km; each answer is inserted into the index as source `online` so later frames and sessions resolve locally; one request per second, 5 s timeout, failures silently fall back to the local answer. Off by default because the product contract is fully local and coordinates can expose sensitive sites.

## Non-goals

Polygons or boundary accuracy (nearest named feature is the rule). Multiple languages. Editing places by hand in the UI (a later feature; the CLI can `lumen place <id> --set` only if cheap).

## Anti-goals

Sending coordinates anywhere unless the user set the opt-in. A place that claims the wrong country because a town across a border is closer: the tiering plus country files makes reserves win within 25 km; the tie remains possible and is documented.

## Constraints

Pillow only runtime dependency (sqlite3 and urllib are stdlib). Loopback server. Strict JSON. Warning-strict suite green; Node scenarios for browser changes; tests build tiny GeoNames fixtures, never read `vendor/data`.

## Calls made here

- Index schema: `places(id INTEGER PRIMARY KEY, name TEXT, country TEXT, feature_class TEXT, feature_code TEXT, population INTEGER, lat REAL, lon REAL, source TEXT)` + `places_rtree(id, min_lat, max_lat, min_lon, max_lon)`; `meta(key, value)` holds the source-file manifest JSON.
- Distance: equirectangular approximation in km (good to <1 % at these radii).
- `Place` dataclass: `name, country, distance_km, feature_code, source, near: bool`; `label` property: `"name, country"` or `"near name, country"`.
- Inspector shows `Place: <label> · 7 km from Madikwe Game Reserve` only when `near`; otherwise just the label.
