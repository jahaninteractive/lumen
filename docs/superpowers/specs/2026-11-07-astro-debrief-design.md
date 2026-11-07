# Astro debrief: what happened, what to try now, settings for next time

Date: 2026-11-07. Builds on the Astro tab (`2026-10-21-finish-compose-astro-housekeeping-design.md`) and 0.3.3,
which added `preview.jpg` and the reference frame's EXIF to every stack.

## Intent

The photographer stacks night-sky bursts at the desk after a trip and wants each stack to teach them how to do
better next time. Night time to shoot is short (the current trip is on a ship), so the advice has to be concrete:
exact settings to dial in, not a lesson. Two real jobs show what is wanted:

- Job 163 (4 lights, 20/13/15/15 s, 28 mm f/4, ISO 3200) stacked, but the TIFF looked broken (it is linear),
  the stars were soft (FWHM ~14 px) because the exposures were over the star-trail limit, the exposures were
  mixed, there were only 4 lights and no darks, and the foreground smeared because alignment follows the stars.
- Job 165 (4 lights) failed: the reference frame (frame 1, 6 s, taken 3 minutes before the 13-15 s others,
  26 stars against 59-69) shared only 6-7 star pairs with the rest, so Siril skipped all three and aborted.
  The reference stars were elongated (FWHMx 13.4 against FWHMy 9.7): the camera moved during the exposure.
  SteadyShot was on.

The debrief is what a person reading the EXIF and the Siril log would say about those, written by Lumen's
rules. The photographer decided: it must give the full result without any local AI; the model adds only a
last, optional note from looking at the stack.

## Goals

1. **Shooting place.** A select on the Astro tab, beside Recipe, labelled "Where you shot": Dark site, City or
   light pollution, Ship or boat, Handheld. Remembered per browser (`localStorage` `lumen-astro-place`, default
   Dark site). It changes only the advice, never the Siril script or the manifest, and changing it re-renders
   the debrief of the job on screen.
2. **Debrief for any job that ran** (succeeded or failed; not queued, cancelled before launch, or
   interrupted with no log), in three parts, shown in the Astro panel under the job summary:
   - **What happened.** Plain sentences from the evidence, each naming its source frame by position
     (`frame 1`) and file name.
   - **Try now.** Concrete actions with this set, each one something the photographer can do in the tab
     (exclude frame N and prepare again, switch to the Untracked recipe, add a darks folder). Advice only:
     nothing is excluded, switched or rerun by Lumen.
   - **Next time.** One settings line for the chosen place, for example
     `5 s · f/4 · ISO 6400 · 25 lights · 10 darks · SteadyShot off`, followed by at most three short reasons.
3. **Works with no AI at all.** Everything in goal 2 comes from rules over the job's own files. It needs no
   model, no network and no oMLX, and shows the same whether oMLX is running or not.
4. **Optional model note.** A "Look at the stack" button (✦ AI styling, `aria-describedby="ai-availability"`)
   on a succeeded job: the review-role model reads `preview.jpg` plus the debrief text and returns up to five
   short notes about what only eyes catch (cloud, haze, smeared or ghosted foreground, a tilting horizon,
   light glow from one side, composition of the Milky Way core). Disabled with the reason when the review model
   is not loaded or the job has no preview. Its notes sit under the debrief in their own block; it never sets or
   changes a number in "Next time".

## Non-goals

- No automatic restack, exclusion, recipe change or calibration choice.
- No change to the Siril script, the stacking options or the manifest.
- No per-frame triage changes (Triage lights stays as it is).
- No plate solving or declination lookup; the star-trail limit assumes the worst case (celestial equator).
- No weather, moon or light-pollution data.

## Anti-goals

- A model inventing settings numbers or a "what happened" fact. Numbers come only from the rules below.
- The debrief failing or going blank because oMLX is off, slow or wrong.
- Advice that reads as generic photography tips instead of naming this job's frames and values.

## Evidence the rules read

All from the job folder; nothing from the camera originals beyond what is already staged in `lights/`.

- **FITS headers of the converted lights** (`process/light_0000N.fit`, written by Siril's `convert` before
  registration, kept in `frames.json` once `process/` is deleted; see clean-up below): `DATE-OBS`, `EXPTIME`, `ISOSPEED`, `APERTURE`, `FOCALLEN`, `XPIXSZ` (pixel pitch in µm, measured
  by Siril). Read with a small header reader (FITS headers are 80-character ASCII cards in 2880-byte blocks;
  stop at `END`), so the main evidence needs neither exiftool nor the source drive. *Builder's call, made
  after the check: `lights/` holds symlinks to the originals, which go missing when the drive is unplugged.*
- **Raw EXIF, only for what FITS lacks**: `ImageStabilization` (SteadyShot) and `MaxApertureValue`, one
  exiftool call (`-json -n`) over the staged lights, made when `frames.json` is written and stored in it, so
  later debriefs do not need the source drive either. Sources offline or exiftool missing: those two rules are
  skipped and "What happened" says so in one line ("SteadyShot and the lens's widest aperture need the
  originals; the drive is offline"); aperture then falls back to the f-number shot.
- A job whose `frames.json` lists no frames (Siril failed before `convert` finished) gets the log rules only.
- **`siril.log`**, parsed line by line (ANSI stripped as `execute_job` already does). Both recipes write the
  same per-frame block, so one parser covers both:
  - Reference frame. Tracked: the `Reference Image:` block, always image 1, with `Found N stars in reference`,
    `FWHMx: a` and `FWHMy: b`. Untracked: `Trial #1: After sequence analysis, we are choosing image N as new
    reference for registration`, with no FWHMx/FWHMy lines.
  - Per matched frame: `Matching stars in image k: done`, then `Initial pair matches`, `Pair matches after
    fitting`, `rotation: r deg`, `dx`, `dy`, `FWHM: x px`, `roundness: q`. Roundness (minor/major axis, 1 is
    round) is the elongation measure for both recipes; Tracked's reference FWHMx/FWHMy is used only for the
    reference itself.
  - `Not enough star pairs (n): Image k skipped` (frame k failed to match, n pairs).
  - `Total: a failed, b registered`, `No image was registered to the reference`, `Registration aborted`,
    Untracked's `Total: a failed, b exported` after `seqapplyreg`.
  - The stack's `Using star weighted FWHM images filter (below x)`, `number of filtered-in images: N` and
    `N images have been stacked`.
  - `Script execution failed` / `finished successfully`.
  Siril registers on several threads, so `Found N ... stars in image` lines are not in frame order and name no
  image; only the reference count, the per-frame blocks and the `Image k skipped` lines tie to a frame. Median
  and range rules use the unordered counts.
  Siril's image numbers are 1-based positions in the sequence, which is the `lights/light_0000N` order in the
  manifest (excluded lights are not staged); the debrief maps them to file names through the manifest.
  Real logs to build test fixtures from: jobs 163 and 166 (Tracked, succeeded), 165 (Tracked, all frames
  skipped) in `<library>/outputs/astro/`, and `2026-11-07-astro-debrief-untracked-siril.log` beside this
  document (Untracked, 165's frames 2-4, succeeded; paths redacted). Copy the lines needed into the tests with
  paths replaced; tests never read the library.
- **Manifest**: light count, excluded count, darks/flats/biases counts, recipe.

## Rules

Thresholds are named constants in one place so they can be tuned.

### What happened

- Registration result: "All N frames matched the reference" or "Frames 2, 3 and 4 did not match the reference
  (6-7 shared stars; Siril needs more) and were skipped". When registration aborted: "Nothing was stacked".
- Reference odd one out: the reference's star count is under half the median of the others, or its exposure
  differs from the median by more than 25%, or it was taken more than 2 minutes from its nearest neighbour.
  Name which.
- Mixed exposures: max/min exposure over 1.25 → list the exposures.
- Gap in the burst: any neighbouring pair more than 2 minutes apart → name the pair and the gap (sky rotates,
  a ship turns).
- Soft stars: reference FWHM in pixels against a sharp-star bound of 6 px ("stars are about N px wide").
- Elongated stars: median roundness under 0.8 (or, for a Tracked reference with no matched frames, FWHMx/FWHMy
  over 1.25) → "stars are stretched one way: the camera moved during each exposure". Jobs 163/166 read 0.73-0.74
  (FWHMx/FWHMy 14.06/10.38), so this fires on them.
- Drift between frames: largest per-frame shift (`dx`, `dy`) and rotation, reported as "the frame moved up to
  N px and r° between shots" when over 20 px or 0.5°; with Ship it is the deck turning.
- Over the star-trail limit: any exposure above the NPF limit (below) → "frames 1-4 ran 13-20 s; stars trail
  past about 5 s at 28 mm f/4".
- SteadyShot on (for Dark site, City and Ship, where a tripod is assumed): say it can add blur on a tripod.
- Few lights: under 10 → "4 lights only halve the noise; 25 cut it to a fifth".
- No darks: one line, unless the place is Ship or Handheld.
- Filtered out by the stack: N lights dropped by the FWHM or roundness filter → name them when the log says.

### Try now

- A reference odd one out with registration failures → "Exclude frame 1 (file) and prepare the stack again".
- Registration failures without an odd reference, or a burst gap → "Prepare again with the Untracked recipe,
  which picks the best frame as the reference".
- No darks and the place is Dark site or City → "Add darks if you shot any".
- Nothing to suggest → "Nothing to change in this set".

### Next time (per place)

NPF limit, full form, at declination 0:
`t = (16.856 · N + 0.0997 · f + 13.713 · p) / f` seconds, with N the f-number recommended below, f the focal
length in mm, p the pixel pitch in µm from `XPIXSZ`. For jobs 163/165 (28 mm, f/4, p = 5.07 µm) that is about 5.0 s. Round to the nearest whole second (4.99 s shows as 5 s), minimum 1 s; below that, one
decimal. Pitch unknown → assume 4 µm and say so.

Aperture: the lens's widest at that focal length (`MaxApertureValue`), else the f-number shot.

| Place | Exposure | ISO | Lights | Darks | Also |
|---|---|---|---|---|---|
| Dark site | NPF limit | 6400 | 25 | 10, same exposure, ISO and temperature | SteadyShot off on a tripod |
| City or light pollution | NPF limit | 1600 | 25 | 10 | Flats matter more: the glow is uneven. SteadyShot off |
| Ship or boat | min(NPF, 3 s) | 12800 | 40, one unbroken burst | not needed | SteadyShot off on a tripod; brace against the roll; Untracked recipe |
| Handheld | 0.5 s (min with NPF) | 12800 | 40 | not needed | SteadyShot on; Untracked recipe |

One ISO per place, not a range, because the photographer asked for exact settings to dial in. *Builder's
call, made after the check.* The values sit at the top of the usable range for each place because the
exposure is capped short; stacking many frames pays back the noise.

Reasons under the line (at most three, the most relevant first): the ones the "What happened" rules fired, in
words tied to the setting they change (for example "Shorter: stars were stretched by deck motion").

## The model note

- Route: `POST /api/astro/jobs/<id>/review` with `{place}`; review role through `Library.tagger_for("review")`;
  model call with the mutation lock released, lock taken only to write the event, as every other advice call.
- Input: `preview.jpg` at `omlx.MODEL_IMAGE_LONG_EDGE` and the debrief rendered as plain text. Prompt asks for
  JSON `{"notes": ["..."]}`, each note one sentence about something visible, and tells it the numbers are
  already decided and not to give camera settings.
- Parser: at most 5 notes, each a non-empty string trimmed to 200 characters; anything else fails the parse.
- Stored as a provenance event `astro_reviewed` (photo_id null, details `{job_id, place, notes, model}`),
  failures as `astro_review_failed`; the newest for that job is read back as `review` on the debrief
  response, so a reopened job shows it again. Only `notes` reaches the browser.
- Status uses the existing per-panel line (`Stack review running…`, `ready`, `failed: <reason>`).

## API and UI

- `GET /api/astro/jobs/<id>/debrief?place=<dark_site|city|ship|handheld>` → `{place, happened: [..],
  try_now: [..], next_time: {settings: "...", reasons: [..]}, review: {notes} | null}`. Computed on request,
  not stored. An unknown place is a 400. A job that has not run (status `queued`, or `cancelled`/`interrupted` before Siril launched) answers 400
  "no debrief until the stack has run"; decide from the job status, not from the log file, which exists from
  the moment a job is prepared.
  No paths in the payload: frames by position and file name only.
- The panel fetches it when a job reaches a terminal state, on resume of a remembered job, and when the place
  select changes. Three headed lists in a `<section>` under `#astro-summary`, plus the model block. Plain native
  controls; the place select is a labelled `<select>`; the debrief is not a live region (the job status line
  already announces the end).
- "How this works" gains one line: the debrief reads the EXIF and Siril's log; the model note is optional.

## Job folder clean-up and output names

Agreed with the photographer on 2026-11-07. Measured: Siril's `process/` folder is 2.6 GB for a four-light job
(jobs 163, 166), against 189 MB for the TIFF. The TIFF stays; it is the only file that can be developed further.

1. **`frames.json` before clean-up.** Once Siril has exited (succeeded or failed; not when cancelled before
   launch), Lumen reads the FITS header of each `process/light_0000N.fit` (the converted, unregistered frames)
   and writes `<job>/frames.json`: per frame its position, staged file name, `DATE-OBS`, `EXPTIME`, `ISOSPEED`,
   `APERTURE`, `FOCALLEN`, `XPIXSZ` and `INSTRUME`. The debrief reads `frames.json`, never `process/`, so the
   "FITS headers" evidence above means this file. A frame whose `.fit` is missing is absent from the file.
2. **Delete `process/`.** After `frames.json` is written, `process/` is removed: `shutil.rmtree` on
   exactly `<job>/process`, after checking it resolves inside the job directory and is not a symlink. This runs
   for succeeded and failed jobs and for a job cancelled mid-run once Siril has stopped. A failed removal is a
   log line, not a job failure. `masters/` (calibration masters, small) stays. `execute_job`'s refusal to launch
   into a folder that already has `process/` is unchanged.
3. **Names from the EXIF.** After `verify_tiff` and the metadata copy, `result.tif` and `preview.jpg` are
   renamed to `<stem>.tif` and `<stem>.jpg`, where the stem is
   `YYYY-MM-DD_HHMM_<camera>_<focal>mm_f<aperture>_<count>x<exposure>s_ISO<iso>`, for example
   `2026-10-18_2129_ILCE-7CM2_28mm_f4_4x15s_ISO3200`:
   - date and time: the first light's capture time as the catalog stores it (the camera clock, as shown in the
     contact sheet), not FITS `DATE-OBS`, which is UT;
   - camera: `INSTRUME` without the leading make (`Sony ILCE-7CM2` → `ILCE-7CM2`);
   - focal length and aperture without trailing zeros (`28mm`, `f4`, `f2.8`);
   - count: the lights stacked; exposure: the median, or `min-max` when exposures are mixed (`4x13-20s`);
   - each field reduced to `[A-Za-z0-9.-]`; a value that is missing drops its field.
   The rename happens before the artifact is recorded, so the `astro_tiff` artifact path, the job payload's
   `result.name` and `result.preview`, the Finder reveal (`open -R` on the artifact path) and the panel's path
   list all carry the new names. `manifest.json` and `load_manifest` keep `result.tif` as the name Siril
   writes; the final names live in the artifact. A stem that already exists in the job folder (it cannot,
   one stack per job) is not overwritten: the rename is refused and the outputs keep their Siril names.
4. Existing jobs 163-166 are left as they are (no migration).

## Constraints

- Pillow only; exiftool is already required for exports. No new dependency, no network.
- Tests never contact oMLX (patch `lumen.library.OmlxTagger` and `get_server_status`), and never run exiftool
  through the globally patched `Popen` in the astro fixtures (patch `lumen.astro.find_tool` or inject a runner).
- Version bump, CHANGELOG line, `docs/HANDOFF.md` entry, warning-strict suite, `node --check`.

## Builder's calls

- Module layout (the rules can live in `astro.py` or a sibling module), the exact sentence wording, the layout
  inside the debrief section, and how the place select and debrief are tested in the JS harness.
- Whether the debrief is cached per job within the process.
