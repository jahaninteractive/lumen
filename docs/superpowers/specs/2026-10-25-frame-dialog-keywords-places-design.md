# Frame dialog, typed keywords, place editing

## Intent

The photographer culls a burst top to bottom with the keyboard and rarely
leaves the contact sheet. Cropping breaks that flow: the Compose panel sits
at the top of the page and the inspector in the right rail, both far from
the frame under the cursor. Everything a single frame needs already exists
on the page; it is just scattered. This design gathers it into one modal
frame view, adds the two per-frame edits the photographer asked for
(typing their own keyword, correcting the place), and gives the sheet the
width the inspector rail used to take.

Three deliverables, in landing order:

1. Typed keywords (small, server + inspector block).
2. Place editing with burst scope and "remember" (small, server + inspector block).
3. Frame dialog (moves the inspector and Compose into the loupe, adds 100% zoom, drops the rail).

The first two land inside today's inspector block so they work before the
dialog exists; the dialog then carries them along unchanged.

## Goals

- One key on the focused frame opens everything about that frame; Escape returns to the same frame.
- Crop choices load when the frame view opens or steps, never on every sheet click.
- A true 100% view for judging sharpness, not the 2048 px preview.
- A keyword the photographer types lands on that frame and teaches the vocabulary like an accepted suggestion.
- A corrected place can cover the burst and be remembered so nearby frames resolve to it offline.

## Non-goals and anti-goals

- No map tile. The frame view keeps the place name and the user-initiated Apple Maps link only. A tile would send coordinates to a map service on every open.
- No new per-frame features beyond zoom. Critique and triage stay burst-level.
- No bulk keyword apply across a burst; typed keywords are for the keeper.
- No edits to camera originals or sidecar locations; the usual non-destructive contract holds.
- Failure that would make this a failure even if it works: the dialog breaking keyboard culling, tab order, or live announcements that the sheet has today.

## Constraints

- Loopback-only server, same-origin and strict JSON on every mutation route, no source paths or raw model text in responses.
- Pillow is the only Python runtime dependency; exiftool and the vendored tools stay optional and discovered as today.
- Native `<dialog>`, native controls, the charcoal/amber/cyan workbench, and the existing announcer.
- Red-green tests, warning-strict unittest, `node --check` on `app.js`, Node scenarios in the existing harness for browser behaviour.
- Keep `docs/HANDOFF.md` current and park anything deferred in `docs/BACKLOG.md`.

## Part 1: typed keywords

Today a keyword reaches a frame only through the model's suggestion list
(`keywordLists` in `app.js`, `_move_keyword` in `library.py`). Add a way to
type one.

**UI.** In the keywords block, after the applied list: a labelled text
input "Add a keyword" with a `<datalist>` of every vocabulary word
(`GET /api/vocabulary` already returns the categories) and an "Add" button;
Enter in the input also adds. On success the input clears, the applied
list re-renders with the new word, focus returns to the input, and the
announcer says `Keyword <word> added to <name>`. A word already applied
(case-insensitive) is announced as already present and not sent.

**Server.** `POST /api/photos/<id>/keywords {action: "add", keyword}`
stays the route. When the word is not in `suggested`, it is a typed word:
trimmed, 1 to 40 characters, no `#`, deduped case-insensitively against
`subjects`. It is appended to `subjects`, saved through `save_tags` (a
frame with no tags row gets one with empty description and model
`photographer`), logged as event `keyword_added {keyword, typed: true}`,
the sidecar is rewritten, and the vocabulary learns it through the same
`learn_keywords` call an accepted suggestion uses. Response shape is
unchanged (`photo_id, subjects, suggested`).

**Survival.** Words the photographer put there outlive the model. The
tags data gains a `photographer` list: a typed word and an accepted
suggestion are both appended to it, and `remove_keyword` drops the word
from it. `tag_photo` builds `subjects` as the `photographer` words first,
then the model's applied words, deduped case-insensitively, so a retag or
an assist run adds to the photographer's keywords and never replaces
them. A model word the photographer removed earlier is not protected; a
retag may suggest or apply it again, which is today's behaviour. The strict JSON validation
rejects a missing or non-string keyword, and the regression lists for
cross-origin and malformed bodies cover the route already.

## Part 2: place editing

**UI.** The location line in the inspector gains an "Edit place" button
when the frame has a place or coordinates. It reveals an inline form:
text input "Place name" (prefilled), text input "Country" (prefilled, may
be blank), two checkboxes ticked by default, "Apply to whole burst" and
"Remember for nearby frames", and Save and Cancel. "Remember" is disabled
with the reason "no GPS fix" when the frame has no coordinates. On save
the line re-renders, the sheet's matched-reason text for affected frames
refreshes on the next load, and the announcer says
`Place set to <name> on N frames` (and `, remembered` when applicable).

**Server.** New route `POST /api/photos/<id>/place` with body
`{name, country, scope, remember}`: name 1 to 80 characters, country 0 to
60, `scope` one of `frame` or `burst`, `remember` boolean; anything else
is 400. `Library.set_place(photo_id, name, country, *, scope, remember)`
is `@_coordinated`: it resolves the target ids (the frame, or every photo
of its burst), calls `catalog.update_place` on each, appends
`place_edited {name, country, scope, remember}` to each, rewrites each
sidecar, and returns `{photo_ids, name, country, remembered}`.

When `remember` is true and the edited frame has coordinates, it adds a
row to the place index with `source='manual'`, `feature_code='MANUAL'`,
at that frame's coordinates. A manual row must win: `_tier` returns 0 for
`source == 'manual'` (it needs the source passed through), and `ensure()`
preserves manual rows across a GeoNames rebuild the same way it preserves
online rows. Future imports within `RADIUS_KM` of that point then resolve
to the remembered name without the online flag. `remember` with no
coordinates is silently false in the response.

A frame already carrying coordinates is not re-resolved on an unchanged
reimport, so an unremembered edit also survives reimport; a changed file
is re-imported and re-resolved, which is the existing behaviour.

## Part 3: frame dialog

The existing `#loupe` dialog becomes the frame view. Enter already opens
it, `[` and `]` already step through the sheet, culling keys and the
rating digits already act inside it, Escape already closes and refocuses
the origin frame. This part widens it and fills it.

**Layout.** Two columns on desktop, stacked on narrow widths: the viewport
on the left, a scrolling column on the right holding, in order, the
inspector content (name, verdict, ranking, location with the place editor,
decision controls, warnings, metrics, description, keywords with the typed
input, caption controls, actions), then the Compose workspace (Ask local AI,
advice, primary crops, more crop shapes, compose status), then an
"Export this frame" button, then Close. The heading stays the frame name.
`renderInspector` renders into whichever container is live; the
`#compose-workspace` markup moves into the dialog rather than being
duplicated, and the Compose tab is removed from the tab row. The right
rail `aside.inspector` is removed; the contact sheet takes its width.
"Back to top" moves next to the existing jump controls.

**Opening and stepping.** `openLoupe` and `stepLoupe` select the frame
(as a sheet click does), run `inspect` and `loadComposition` for it, and
announce `<name>, N of M`. The existing request-id guards drop stale
responses when stepping fast. Arrow keys keep panning the viewport when it
has focus; `[` and `]` step frames from anywhere in the dialog.

**Zoom.** A "Full size" toggle button (key `z`, `aria-pressed`) swaps the
viewport image between the stored preview and the largest JPEG the camera
embedded in the raw. New route `GET /full/<id>?v=` serves
`<library>/cache/full/<id>.jpg`, produced on first request with the
exiftool extraction `create_preview` already uses but taking `JpgFromRaw`
first (on the photographer's a7C II files `PreviewImage` is the same
1616x1080 image the preview already shows; `JpgFromRaw` is 3504x2336 for
M-size raws), saved unresized with `exif_transpose` applied, and pruned
alongside thumbnails. The button's accessible text carries the real pixel
size once loaded (`Full size, 3504 by 2336`), so the view never claims a
magnification it does not have; whether L-size raws embed a full 7008 px
JPEG is unknown until one is checked, and the design holds either way. If
no embedded JPEG exists or exiftool is missing, the route answers 404 and
the button is disabled with "no full-size image in this file"; if the
original is unreachable (offline travel volume) the reason is "original
is offline". Scroll position is centred on first zoom-in; the loupe's
pan keys apply.

**Export this frame.** Calls the existing synchronous current-scope export
with the selected id and reports through the existing export status text,
mirrored into the dialog.

**Keyboard and accessibility.** Culling keys keep working in the dialog
except while focus is in an input, textarea, or select (today's rule).
Tab order is viewport, zoom toggle, the right column top to bottom, Close.
Every announcement the inspector makes today still fires. The Shortcuts
legend gains `z` and drops nothing.

## Decided

- Open key stays Enter; step keys stay `[` and `]`; arrow keys pan. (My call: arrows already pan the loupe, and `[`/`]` are already learned.)
- The Compose tab goes away; crops live only in the frame view. (My call: one place for one job.)
- The inspector rail goes away rather than being repurposed. (My call: nothing burst-level lives there today; burst critique already shows inside the inspector and comes along.)
- Full-size zoom uses the embedded camera JPEG, not a darktable render. (My call: zero new dependencies, no decode wait, good enough for sharpness.)
- Remember radius is the existing `RADIUS_KM` (25 km); no new setting.
- Inside the frame view the culling-key guard that skips non-frame buttons is lifted, so keys work from the dialog's own buttons; inputs, textareas, and selects still swallow keys. (My call.)
- `remove_keyword` on a typed word drops it rather than moving it to `suggested`. (My call: it was never a suggestion.)
- `cache/full` files are named by fingerprint like previews, so a changed original never serves a stale zoom. (My call.)
- "Export this frame" reuses the Export tab's current size and watermark settings. (My call.)
- The removed Compose tab: a remembered `lumen-tab` of `compose` falls back to Export. (My call.)

## Left to the builder

- Exact dialog CSS and breakpoint; keep the viewport at least two thirds of the width on desktop.
- Whether the typed-keyword datalist is built once per `load()` or on demand.
- The Node scenario names and the unittest split, following the existing `tests/test_web.py` pattern.
- How `prune_thumbnails` is generalised to also prune `cache/full`.
