# Frame view round two: own crop, tabs, condensed filters

## Intent

The frame view (`docs/superpowers/specs/2026-10-25-frame-dialog-keywords-places-design.md`) works. Three follow-ups from first use: the photographer wants to draw their own crop on the big image instead of cropping the raw later; the right column scrolls too far, so its content should split into two tabs with deliberate switching; and the contact sheet's filter form spreads its controls loosely and wastes vertical space. Also drop the duplicated frame name from the right column.

## Goals

- Drag a rectangle on the frame-view image, at preview or full size, and get a "Your crop" card that saves through the existing crop path and exports cropped.
- Two remembered tabs in the right column: Frame and Crop.
- Filter form folded to one row plus a native collapsed section, with a count of active filters on the summary.

## Non-goals and anti-goals

- No pixel editing, rotation, or straightening. A crop is a rectangle in normalised coordinates, as today.
- No change to how crops export; `PhotoEdit.rect` already drives the JPEG pipeline.
- No new keyboard conflicts: culling keys, `[`/`]`, `z`, Escape keep their meaning. Escape during a drag cancels the drag only.
- Nothing leaves the frame view to another page or dialog.

## Constraints

Same as the previous spec: loopback-only, strict JSON on mutations, no paths in responses, Pillow only, native controls, announcements, red-green warning-strict tests, Node scenarios, `node --check`.

## Part 1: your own crop

**Drawing.** With the frame view open and the image shown at either size, pointer-down on the image starts a rectangle; move stretches it; pointer-up ends it. The rectangle is shown as an amber outline with a dimmed outside. Its edges are converted to a normalised rect (`left, top, width, height` in 0..1 of the displayed image's natural size, so preview and full size give the same rect). A rectangle under 24 display pixels on either side is ignored. Drawing again replaces the previous one. Escape while dragging cancels the drag and does not close the dialog.

**Aspect lock.** A small `<select id="crop-lock">` beside the card: Free (default), 3:2, 2:3, 1:1, 4:5. When locked, the height follows the width as the pointer moves; the lock applies while drawing and is remembered in `localStorage` as `lumen-crop-lock`.

**Card.** The drawn rect becomes the first card in `#primary-crops`, heading "Your crop", kind `manual`, aspect text from the lock or `free`, with the same megapixel line and resolution warning the suggestions get, computed in the browser from the source size the crops payload already carries (`source_width`/`source_height`; add them to `GET /api/photos/<id>/crops` if absent). "Use this crop" posts it. The card stays while the frame is open and is cleared on step or close; a saved manual crop comes back from `active_crop` on reopen (it is the active crop with `candidate_id: "crop-manual"`) and is drawn on the image as the outline.

**Server.** `POST /api/photos/<id>/crop` accepts either `{candidate_id}` as today or `{candidate_id: "crop-manual", rect: {left, top, width, height}}` with four finite numbers. `Library.choose_crop(photo_id, candidate_id, rect=None)`: for `crop-manual`, build a `CropRect` (its own validation rejects out-of-bounds), compute the pixel box against the source dimensions, refuse a crop below `MIN_CROP_PIXELS` with a public message (`crop keeps too few pixels; at least 8 MP`), and store a `PhotoEdit` with `candidate_id="crop-manual"`, `aspect` as the client sent it (one of `free, 3:2, 2:3, 1:1, 4:5`; anything else is 400), `author="photographer"`, `rationale="drawn by the photographer"`. Sidecar and export use the rect as they do for any crop. `review_composition` ignores a manual active crop when listing candidate ids (it is not a generated candidate).

## Part 2: tabs in the right column

The right column gets a `role="tablist"` with two tabs: **Frame** (`#frame-tab` → verdict, matches, ranking, location and place editor, decision controls, warnings, metrics, description, keywords, caption controls, the actions row) and **Crop** (`#crop-tab` → crop lock, crop cards, Ask local AI, advice, More crop shapes, the crop status line, Export this frame). Close stays below both. Arrow Left/Right, Home, End move between tabs when a tab has focus, reusing the main tab row's key handling (`tabs` and `selectTab` are generalised to take a tablist, or the handler is duplicated in four lines; builder's choice, no third copy). The chosen tab is remembered as `lumen-frame-tab` and restored on open; the default is Frame. Culling keys keep working from either panel. The `h3` frame name at the top of the inspector is removed; the dialog heading carries it.

## Part 3: condensed filter form

`#filter-form` becomes: row one holds Tag text with its Expand button, Apply, Reset, and a `<details id="filter-more">` whose `<summary>` reads `More filters` plus ` (N active)` when any of its fields differ from the defaults. Inside: From, To, Camera, Decision, Minimum rating, Keepers only, Tagged only, Show rejected. All ids and behaviours stay; only the grouping changes. The details open state is remembered as `lumen-filter-more`. On a narrow width the row wraps as it does today.

## Decided

- Free aspect by default, lock choices 3:2, 2:3, 1:1, 4:5. (My call.)
- Minimum drag 24 display px; below that nothing happens, no announcement. (My call.)
- Manual crop lives in the same `PhotoEdit` row as any crop, so "Keep original" clears it like any other. (My call.)
- Dialog heading keeps the name; the inspector `h3` goes. (Requested.)
- Filter defaults: empty dates, any camera, any decision, rating 0, all three boxes unticked; the count counts fields that differ. (My call.)

## Left to the builder

- Overlay implementation (a positioned `div` over the `img` inside the scrolling viewport, or an SVG); it must track scroll and both image sizes.
- Whether the aspect lock select sits above the cards or beside "Your crop".
- Scenario names and the harness extensions for pointer events.
