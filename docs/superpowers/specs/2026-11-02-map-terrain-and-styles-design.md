# Map terrain, natural colours and map styles

Date: 2026-11-02. Extends `2026-11-01-map-tab-design.md`; everything there still holds.

## Intent

The Map tab draws coastline, borders, rivers, lakes, roads and town names as lines over the charcoal
background, so land and sea are one colour and the map says nothing about the ground. The photographer wants
two things from it: to pick out which range or plain a burst was shot in while culling, and, years later, to
read a frame against its terrain ("the cheetah was near the volcanic hills"). The first is a readable map with
mountains, plains and deserts named on it, with sea blue and land coloured. The second is that terrain name
reaching the frame itself, in the frame dialog, as a fact about the frame.

## Goals

1. **Natural Earth physical layers.** `scripts/bootstrap.sh` fetches five more layers from the same pinned
   commit into `vendor/data/map/`, each against a pinned SHA-256, and the prompt's size estimate rises to match
   (about 100 MB). The layers, with their sizes from HEAD requests on 2026-11-02:

   | File | Gives | Size |
   |---|---|---|
   | `ne_10m_land.geojson` | land polygons, so sea and land are told apart | 9.7 MB |
   | `ne_10m_geography_regions_polys.geojson` | mountain ranges, plateaus, plains, deserts, basins, peninsulas as named areas (`featurecla`, `name`, `scalerank`) | 5.3 MB |
   | `ne_10m_geography_regions_points.geojson` | label points for the small physical regions | 0.3 MB |
   | `ne_10m_geography_regions_elevation_points.geojson` | named peaks and passes with `elevation` | 0.8 MB |
   | `ne_10m_geography_marine_polys.geojson` | sea, gulf and bay names | 1.6 MB |

   The photographer approved this download (about 18 MB) in conversation on 2026-11-02. The files are already in
   `vendor/downloads/natural-earth/` (ignored by Git) with these SHA-256s, so the implementer fetches nothing:

   | File | SHA-256 |
   |---|---|
   | `ne_10m_land.geojson` | `1ac90796408bc6ad6911d69448485d3c4dbf2190370080368a09976e1c9f7416` |
   | `ne_10m_geography_regions_polys.geojson` | `b7b26e50ea917d3696aec87f932def2bf5f890f5770e441d59c162c6f4c92a77` |
   | `ne_10m_geography_regions_points.geojson` | `25821b530966e62756faebf53699f12bf73550f8228aa4bbdf3e4132dd7cf806` |
   | `ne_10m_geography_regions_elevation_points.geojson` | `f98a16867867146ec4146d6d4b18c823eeedb2825947de666116cf9a4e3f43cb` |
   | `ne_10m_geography_marine_polys.geojson` | `53f865e8ffa966cdd402145c82c5cd14ee7ce974cd0eb9a3f59f03a4cfd2d66c` |

   What the files hold, read on 2026-11-02: the regions polygon file names its properties in upper case
   (`FEATURECLA`, `NAME`, `SCALERANK`) while every other file uses lower case, so `parse_geojson` reads
   properties case-insensitively. Its classes are Island 295, Range/mtn 222, Island group 160, Plateau 72,
   Desert 58, Pen/cape 57, Geoarea 43, Coast 37, Plain 30, Delta 12, Peninsula 11, Basin 9, Continent 7,
   Valley 6, Lowland 5, Tundra 4, Isthmus 4, Wetlands 3, Gorge 3, Foothills 3, Lake 3, Depression 2,
   Dragons-be-here 1. The points file has island, cape, waterfall, pole, plain, island group (lower case). The
   elevation file has mountain 633, spot elevation 61, depression 9, plateau 5, pass 2, cape 1, with
   `elevation` in metres. The marine file has sea, gulf, bay, strait, sound, channel, generic, lagoon, fjord,
   ocean, river, reef, inlet. The land file is 11 features: 9 `Land` MultiPolygons that each span the world
   (one per scalerank band), one with no class, and one `Null island`.

2. **New feature kinds in `mapdata`.** `NE_FILE_KINDS` learns the five files. The land file is kind `land`. The
   marine file is kind `sea`. The two geography-regions files (polygons and points) take their kind from
   `featurecla` through one mapping table in `mapdata`: `mountain` (Range/mtn, Foothills, Plateau), `plain`
   (Plain, Lowland, Basin, Delta, Valley, Depression, Tundra, Wetlands), `desert` (Desert), everything else
   `region` (peninsulas, islands, island groups, capes, coasts, gorges, geoareas). The mapping is
   case-insensitive. Features whose class is `Continent`, `Dragons-be-here` or `Lake` (the lakes layer already
   has them) are dropped at parse time, as is the land file's `Null island`. The
   elevation file is kind `peak`, and its name is composed at parse time as `<name>, <elevation> m` when the
   file carries an elevation, so the browser and the lookup never see a second field. Every other kind keeps
   `rank` as `scalerank`.

   **Polygons are indexed one per row and clipped per request.** `parse_geojson` splits a MultiPolygon into one
   `Polygon` feature per member (same kind, name and rank), so the R-tree sees each landmass and each island on
   its own instead of nine world-sized boxes. `_drawn` clips every polygon ring to the padded viewport with a
   Sutherland-Hodgman pass against the box (the clip window is a rectangle, so the stdlib arithmetic is about
   thirty lines) before decimating; a ring clipped to nothing is dropped and a polygon with no outer ring left
   returns None. The earlier note that rings are not clipped because it distorts the fill applied to lines'
   run-splitting, which would open a ring; Sutherland-Hodgman keeps it closed. Lakes gain the same clipping.

3. **Zoom gating.** `KIND_MIN_ZOOM` gains `sea: 3`, `mountain`/`plain`/`desert`/`region`: 3, `peak: 6`. `land`
   always draws. In the browser a terrain or sea label is drawn only when its `rank` (scalerank) is at most
   `zoom - 1`, so the Drakensberg appears before every foothill does, and a polygon's label is skipped when its
   bounding box on screen is under about 80 pixels on its long side.

4. **Natural colours and a map style select.** A `<select id="map-style">` on the Map panel's side column,
   labelled "Map style", offers **Workbench** (today's look), **Natural dark** and **Natural light**, remembered
   in `localStorage` under `lumen-map-style`, default Workbench. `mapColour` becomes a per-style palette:
   - **Workbench**: charcoal sea, graphite land, cyan rivers and lakes as today, terrain polygons unfilled with
     muted labels, peaks as small muted triangles. Nothing the photographer has learnt to read moves.
   - **Natural dark**: deep blue sea, dark warm-grey land, lakes and rivers a lighter blue, mountains tinted
     brown, plains tinted green, deserts tinted sand, all fills at low alpha over the land, labels in the
     muted text colour, sea names in a paler blue italic.
   - **Natural light**: pale blue sea, cream land, the same tints at a slightly higher alpha, labels dark.
   Frames, clusters, the open fan, tracks, waypoints and the amber selection keep their colours in all three
   styles; only the base map changes. The canvas alone changes colour; the surrounding workbench stays charcoal.

5. **Drawing order.** `MAP_ORDER` becomes land, terrain fills (mountain, plain, desert), lake, coastline, border,
   river, road, park, sea labels, region and terrain labels, peak, track, waypoint, place. The sea is the canvas
   background; `land` polygons fill over it with `evenodd`, then lakes fill with the water colour as they do
   now. `sea` polygons are never filled, only their names drawn. A polygon's label sits at the centre of the
   bounding box of the geometry the server sent, which is the ring already clipped to the padded viewport: so
   the Kalahari is named while the photographer is zoomed inside it, and a region that only enters a corner of
   the view is named in that corner, over itself. The 80-pixel skip in goal 3 measures that same clipped box. A
   terrain Point draws only its label.

6. **Terrain on the frame.** `MapIndex.terrain_at(lat, lon)` returns the names of `mountain`, `plain`, `desert`
   and `region` polygons that contain the point, smallest bounding box first, using the R-tree for candidates
   and an even-odd ray cast over the rings (stdlib only). `Library.get_photo` adds `"terrain": [names]` for a
   frame with a fix when the index already exists on disk and is not being built; otherwise the key is absent,
   so a first-run lookup never makes the frame dialog wait on a 100 MB parse. The frame dialog's location line
   reads `Place: Madikwe, South Africa · Waterberg, Bushveld Basin · 24.7° S, 26.3° E` (place, then terrain
   names joined by ", ", then the fix), and `Waterberg, Bushveld Basin · 24.7° S ...` when no place is set. The
   terrain is read, never stored, so no re-import, catalog column or provenance event is involved.

7. **Doctor and status unchanged in shape.** `lumen doctor`'s `map.layers` count rises on its own; nothing new
   is added to `/api/status`.

8. **Tests.** Unit tests for the `featurecla` mapping, the dropped classes, the peak name composition, the new
   zoom gates, `terrain_at` (a point inside, outside, and inside a hole), the land polygon surviving `query`
   split per polygon and clipped to the viewport (a ring that leaves the box comes back closed, one wholly outside
   is gone), and `get_photo` with and without an index; a web test that `terrain` reaches `GET /api/photos/<id>`
   and that nothing else new does; `node --check`. Tests build fixtures and never read `vendor/data`.

## Non-goals

- Shaded relief or contours: a raster at hundreds of megabytes, out of reach on this connection and out of
  character for a vector map.
- Filtering or searching frames by terrain, or writing terrain into tags, captions or exports. Trigger: the
  photographer asks "show me everything from the Waterberg".
- Terrain in the Frames in view list, the cleared-frame card or the contact sheet.
- Light mode for the rest of the workbench.

## Anti-goals

- A fill that hides the frames: every terrain tint is low alpha over the land, and the sea stays darker than
  the cyan dots in the dark styles and lighter than them in the light one.
- Any network call from the server for the map; the five files come through `scripts/bootstrap.sh` only.
- A path, fingerprint or source name reaching the browser; features carry `kind`, `name`, `rank`, `geometry`
  as today.

## Constraints

- Pillow is the only runtime Python dependency; the point-in-polygon test is stdlib arithmetic.
- `/api/map/features` keeps its shape. Acceptance check: a zoom-12 viewport on the African coast answers in
  under 300 KB and 300 ms on the development machine with the real files in `vendor/data/map`; the measurement
  goes in the handoff.
- Keyboard: the style select is a native `<select>` and sits in the existing tab order of the side column; a
  change redraws and announces nothing (it is the photographer's own action).
- `__version__` bump in both places and a `CHANGELOG.md` line; `docs/HANDOFF.md` updated at the end.

## Calls made here (the photographer did not decide these)

- The `featurecla` to kind mapping and the dropped classes.
- Peaks carry their elevation in the name.
- Three styles in one select rather than separate light/dark and natural/workbench controls.
- Default style stays Workbench so the tab looks the same until the photographer chooses otherwise.
- Terrain appears in the frame dialog's location line only.
- Labels are centred on the visible (clipped) part of a polygon, not on the whole polygon's box.
- `NE_FILE_KINDS` is matched in order with `admin_0_boundary` before `land`, so the border file is not caught by
  the `land` marker; the builder may use a marker such as `ne_10m_land` instead.

## Left to the builder

- The exact hex values of the two natural palettes, within the descriptions above.
- Label collision: none is attempted; the scalerank gate is the only thinning.
- Whether `region` polygons are filled (they are not, by default) or only labelled.
