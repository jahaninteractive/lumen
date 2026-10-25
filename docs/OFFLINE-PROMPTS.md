# Offline prompts for a local model (Qwen3.8-27B via oMLX)

These prompts are written for a local coding agent backed by the 27B model.
They are deliberately narrower than prompts written for a large hosted model: one
feature at a time, named files, exact tests, and a hard "do not touch" list.

## How to run them

1. Start oMLX: `omlx start`. Confirm the model is loaded with
   `uv run lumen doctor --json`.
2. Use any coding agent that speaks the OpenAI chat API and can edit files
   and run shell commands (aider, OpenCode, Continue, or similar). Point it
   at `http://127.0.0.1:8000/v1` with the model name `Qwen3.8-27B-4bit`
   and the API key from `~/.omlx/settings.json`.
3. Before every task: `git add -A && git commit -m "checkpoint"` so you can
   `git reset --hard` if the model wanders.
4. After every task:
   `uv run python -W error -m unittest discover -s tests` and
   `node --check src/lumen/static/app.js`. Do not continue on red.
5. Keep the agent's context small. Give it only the files named in the
   task. If it starts rewriting whole files, stop it and ask for a diff.
6. If a task fails twice, `git reset --hard`, split the task in half, and
   try the first half alone.

## Rules to paste at the top of every session

```
You are working on Lumen, a local, non-destructive Sony RAW photo manager.
Hard rules:
- Never edit, move, rename, or delete any photo file. Only write under the
  Lumen library directory.
- Pillow is the only runtime dependency. Do not add packages.
- The HTTP server stays loopback-only. New POST routes must use the
  existing _trusted_mutation check and strict JSON validation. Never put
  source paths, fingerprints, or raw model output in a JSON response.
- Write failing unittest tests first, then make them pass. Tests must
  create their own small images with Pillow; never read a personal photo.
- Change only the files I name. Show me a diff before applying it.
- Keep the UI accessible: native buttons, visible focus, aria-live
  announcements through the existing announce() function, 44 px targets.
- No em dashes in docs or strings.
Run tests with:
  uv run python -W error -m unittest discover -s tests
  node --check src/lumen/static/app.js
```

---

## Task A: near-duplicate grouping with a perceptual hash

Goal: frames that look alike but were shot more than 2 seconds apart (a
subject re-shot, a bracketed retry) end up in the same burst group.

Files to change: `src/lumen/analysis.py`, `src/lumen/domain.py`,
`src/lumen/catalog.py`, `src/lumen/grouping.py`, `src/lumen/library.py`,
`src/lumen/config.py`. Tests: `tests/test_analysis.py`,
`tests/test_grouping.py`, `tests/test_library.py`.

Prompt:

```
Implement perceptual near-duplicate grouping in Lumen. Steps, in order:

1. In src/lumen/analysis.py add
   def perceptual_hash(path: Path) -> str
   that opens the image with Pillow, converts to grayscale, resizes to
   9x8 with Image.Resampling.LANCZOS, computes a difference hash (each
   bit is 1 when the left pixel is brighter than the right pixel), and
   returns 16 lowercase hex characters. Add
   def hash_distance(a: str, b: str) -> int
   returning the Hamming distance; raise ValueError if either value is
   not 16 hex characters.
   Tests in tests/test_analysis.py: identical images give distance 0, an
   image and its slightly blurred copy give distance below 8, two
   different random-noise images give distance above 20, and bad input
   raises ValueError.

2. In src/lumen/domain.py add field
   perceptual_hash: str | None = None
   to PhotoMetrics. In src/lumen/catalog.py add column
   perceptual_hash TEXT to the metrics table using
   ALTER TABLE ... ADD COLUMN inside a try/except sqlite3.OperationalError
   so existing databases upgrade. Save and load it in save_metrics and
   get_metrics. Test in tests/test_catalog.py that the value round-trips
   and that opening an old database without the column works.

3. In src/lumen/config.py add
   similarity_threshold: int = 10
   and similarity_window_seconds: float = 60.0
   to Settings, read from LUMEN_SIMILARITY_THRESHOLD and
   LUMEN_SIMILARITY_WINDOW in from_environment.

4. In src/lumen/grouping.py add
   class SimilarityMergePolicy with fields threshold and window_seconds and
   method merge(groups: list[list[Photo]], hashes: dict[int, str])
   -> list[list[Photo]]. It walks the time-ordered groups and merges a
   group into the previous group when both share camera_id, the gap
   between the previous group's last capture time and this group's first
   capture time is at most window_seconds, and the minimum hash distance
   between any photo in the two groups is at most threshold. Photos with
   no hash or no capture time never merge. The result must be
   deterministic and preserve the original order.
   Tests in tests/test_grouping.py: two groups with near hashes within
   the window merge; same hashes outside the window do not merge;
   different cameras do not merge; missing hashes do not merge.

5. In src/lumen/library.py: in _import_one compute
   metrics.perceptual_hash = perceptual_hash(preview_path) before
   save_metrics. In _regroup_and_write_sidecars, after
   TimeGapGroupingPolicy groups the photos, build a dict of photo id to
   hash from the catalog metrics and pass the groups through
   SimilarityMergePolicy(self.settings.similarity_threshold,
   self.settings.similarity_window_seconds).merge(...). Manual picks must
   still be honoured after a merge.
   Test in tests/test_library.py: import three generated images where
   two are near-identical 30 seconds apart and one is different; expect
   two groups.

Do not change scoring.py, composition.py, web.py, or the static files.
```

---

## Task B: freeform crop

Goal: the photographer can drag a rectangle on the preview and save it as
the active crop, in addition to the suggested candidates.

Files to change: `src/lumen/domain.py`, `src/lumen/composition.py`,
`src/lumen/library.py`, `src/lumen/web.py`, `src/lumen/static/index.html`,
`src/lumen/static/app.js`, `src/lumen/static/styles.css`. Tests:
`tests/test_composition.py`, `tests/test_library.py`, `tests/test_web.py`.

Prompt, part 1 (backend only):

```
Add freeform crops to Lumen's backend. Do not touch the static files yet.

1. In src/lumen/domain.py, PhotoEdit currently allows aspect values
   "native", "3:2", "4:5", "1:1". Add "custom". Allow author value
   "manual" for custom crops (already allowed).

2. In src/lumen/composition.py add
   def custom_candidate(width: int, height: int, rect: CropRect)
   -> CropCandidate
   It validates dimensions with _validate_dimensions, converts rect to a
   pixel box with rect.pixel_box(width, height), rejects boxes whose
   pixel count is below MIN_CROP_PIXELS with ValueError("custom crop is
   below 8 MP"), and returns a CropCandidate with kind "custom", aspect
   "custom", rationale "photographer crop", primary False, revealable
   False, and an id built the same way as _candidate (sha256 of
   "custom|custom|WxH|l,t,r,b", first 16 hex chars, prefixed "crop-").
   Tests in tests/test_composition.py: a valid rect returns a candidate
   with a crop-<16 hex> id; a tiny rect raises ValueError; the same rect
   always gives the same id.

3. In src/lumen/library.py add
   @_coordinated
   def choose_custom_crop(self, photo_id: int, rect: CropRect) -> PhotoEdit
   It loads the photo (ValueError if unknown), reads dimensions with
   read_image_dimensions, builds the candidate with custom_candidate,
   constructs a PhotoEdit with author "manual", aspect "custom", and the
   candidate's rect, rationale and id, then calls self._change_crop.
   Test in tests/test_library.py: choosing a custom crop persists it,
   writes it to the sidecar, and a below-8MP rect raises ValueError and
   leaves the previous crop in place.

4. In src/lumen/web.py add route POST /api/photos/<id>/crop/custom.
   The body must be exactly {"rect": {"left","top","width","height"}}
   with four finite numbers; anything else raises ValueError("custom
   crop requires a rect with left, top, width and height"). Build a
   CropRect (its own validation raises ValueError), call
   choose_custom_crop, and on any ValueError, OSError, RuntimeError
   respond with the public message "crop could not be selected for this
   photo" exactly as the existing /crop route does. Return edit.to_dict().
   Tests in tests/test_web.py: valid body returns the edit; extra keys
   are rejected; cross-origin Origin header is rejected with 403; the
   error response contains no file path.

Run the full test suite before reporting.
```

Prompt, part 2 (UI only):

```
Add a freeform crop editor to the Compose panel. Backend route
POST /api/photos/<id>/crop/custom already exists and takes
{"rect": {"left","top","width","height"}} in normalized 0..1 coordinates.

Change only src/lumen/static/index.html, app.js, styles.css and
tests/test_web.py.

1. In index.html, inside #compose-workspace after #more-crop-shapes, add:
   <details id="custom-crop">
     <summary>Freeform crop</summary>
     <div id="custom-crop-stage" class="crop-stage"></div>
     <p id="custom-crop-readout" class="quiet"></p>
     <div class="button-row">
       <button type="button" id="use-custom-crop">Use freeform crop</button>
       <button type="button" id="reset-custom-crop" class="secondary">Reset</button>
     </div>
   </details>

2. In app.js add renderCustomCrop() called from renderComposition(). It
   puts the preview image into #custom-crop-stage with an absolutely
   positioned <div class="crop-box" tabindex="0" role="application"
   aria-label="Crop rectangle"> over it. The box starts as the active
   crop if one exists, otherwise the centre 80 percent of the image.
   Pointer drag inside the box moves it; drag on the four corner
   handles resizes it; the box never leaves the image. Arrow keys move
   the box by 1 percent, Shift+Arrow resizes by 1 percent. After every
   change update #custom-crop-readout with the remaining megapixels
   (computed from the candidate source dimensions you can derive from
   candidates[0].pixel_box) and announce() the readout on keyboard
   changes only.

3. #use-custom-crop posts the normalized rect to
   /api/photos/<id>/crop/custom, then calls loadComposition(). Errors go
   to #compose-status and announce(), matching applyCrop. Disable the
   button when state.busy or when the readout is below 8 MP.

4. In styles.css add .crop-stage (position relative, max-width 100%),
   .crop-box (position absolute, 2px solid var(--amber), box-shadow
   0 0 0 9999px rgba(0,0,0,.55), cursor move, visible focus outline),
   and .crop-handle (16px squares at the corners, touch-friendly).

5. In tests/test_web.py extend the existing HTML structure test to
   assert the new details element, the two buttons by id, and that the
   buttons are native <button> elements.

Then run node --check src/lumen/static/app.js and the full suite.
```

---

## Task C: card detection

Goal: when a camera card is mounted, Lumen lists it so the photographer
can start an ingest or import with one click. Nothing happens
automatically; the user still presses the button.

Files to change: `src/lumen/web.py`, `src/lumen/cli.py`,
`src/lumen/static/index.html`, `src/lumen/static/app.js`. Tests:
`tests/test_web.py`, `tests/test_cli.py`.

Prompt:

```
Add mounted-card detection to Lumen.

1. In src/lumen/web.py add a module-level function
   def list_cards(volumes: Path = Path("/Volumes")) -> list[dict]
   It returns one entry for every directory directly under volumes that
   contains a DCIM subdirectory: {"name": <volume name>, "path":
   <volume path as string>, "dcim": <path to DCIM>, "image_count":
   <count of files under DCIM whose suffix lower-cased is in
   lumen.library.SUPPORTED_EXTENSIONS>}. Ignore volumes that cannot be
   read (PermissionError, OSError). Sort by name. The volumes path is a
   parameter so tests can use a temporary directory.
   Add route GET /api/cards returning {"cards": list_cards()}. This is a
   read-only GET and needs only the existing _trusted_host check. It is
   acceptable to return the volume path here because the user chose to
   mount it and must paste it into the import form anyway.
   Tests in tests/test_web.py: a temp directory with two volumes, one
   with DCIM holding three .ARW and one .txt file and one without DCIM,
   returns one card with image_count 3. Patch lumen.web.list_cards in
   the route test.

2. In src/lumen/cli.py add command `cards` with --json that prints the
   same list. Test in tests/test_cli.py with the function patched.

3. In src/lumen/static/index.html, above #import-form add:
   <section id="cards" aria-labelledby="cards-heading">
     <h2 id="cards-heading">Cards</h2>
     <div id="card-list"><p class="quiet">No camera cards mounted.</p></div>
     <button type="button" id="refresh-cards" class="secondary">Refresh cards</button>
   </section>

4. In app.js add loadCards(): GET /api/cards, render one row per card
   with its name, image count, and a native button "Use this card" that
   fills #import-path with the card's dcim path and focuses it, then
   announce()s "Card <name> selected". Call loadCards() from load() and
   from #refresh-cards. Poll every 10 seconds only while the page is
   visible (check document.visibilityState) and stop polling on
   visibilitychange to hidden. Do not start an import or ingest
   automatically under any circumstance.

Run node --check and the full suite before reporting.
```

---

## If the model gets stuck

- Paste the exact failing test output back to it and say "fix only this".
- Ask for the smallest change that makes the test pass, then refactor as a
  separate step.
- If it proposes adding a dependency, say no and ask for a Pillow-only or
  standard-library version.
- If it wants to rewrite `library.py` or `catalog.py`, stop, reset, and
  give it only the single function to change with its surrounding 20
  lines.
