# Backlog

Deferred items with a known reason. Each entry says what, why it was deferred, and the trigger for picking it up. Keep `docs/HANDOFF.md` as the status ledger; this file only holds work nobody is doing yet.

## From the 2026-11-23 clean-Mac test of Lumen.app

- **oMLX on a 16 GB Mac needs written help (website and guide).** On an M4 Mac mini with 16 GB, oMLX refused the fast model: "Model … (5.63GB) does not fit under the dynamic memory ceiling (5.16GB). Close other apps to free RAM (… 13.00GB static cap …), raise memory_guard_tier (safe → balanced → aggressive), or use a smaller model." The photographer got it loading by turning oMLX's memory limit off. The help should say: on 16 GB take only the fast model, close other apps before loading, and where oMLX's memory guard setting is and which tier to pick; whether `balanced` is enough was not tried. It belongs beside the Settings hint that names which model to download. Trigger: the website's install page is written, or the Settings hint is built.

## From the 2026-11-22 Canon raws

- **Canon files not yet measured.** An RF lens with built-in corrections (does the helper offer lens correction?), a Dual Pixel raw, an HDR PQ raw, a body older than the 5D Mark III, and a Siril stack of CR2 or CR3 lights. Trigger: one of those files to hand; import it, check the preview, the Develop tab's disabled controls and a full export, then add a row to the guide's "Cameras measured".

## From the 2026-11-22 Leica and Nikon raws

- **Bodies and files not yet measured.** A Leica Q file straight from the camera (the Q3 sample had been re-saved by Lightroom, so its embedded JPEGs are Lightroom's), an SL file with a built-in lens correction, and a lossless-compressed Nikon Z file. Each should work through the same paths; import one, check the preview size, the Develop tab's disabled controls and a full export, then add a row to the guide's "Cameras measured". Trigger: the photographer has such a file.
- **An astro stack from Leica or Nikon lights.** DNG and NEF lights are accepted, but no two lights from one body were to hand, so Siril has never stacked either. Trigger: a night of Leica or Nikon frames.
- **Leica M develop speed if it bites.** A 60 MP M11 render measured 0.8 s at the window size and 1.4 s at full size; fine today. Trigger: the photographer finds dragging sliders on M11 files slow.
- **Rendered previews are labelled `embedded`.** A Leica M9 or M (Typ 240) preview rendered by `raw_developer` at import still has `preview_kind` `embedded`, as the `sips` and darktable fallbacks always did. Harmless so far; revisit if anything starts branching on that label to mean "came from the camera".

## From the 2026-11-21 settings and AI engine design

- **Try Online against OpenAI and Google.** Only one service has been run for real (the guide's "Online services tried"). For each: press Test, tag one frame, pick one burst, then add a row to that table. Known before starting: the OpenAI address in `preferences.SERVICE_PRESETS` was written from memory and its exact base string was not seen in the documentation; that service documents `max_tokens` as deprecated in favour of `max_completion_tokens` and refused by its reasoning models, so the first call to one may be a 400 and `OmlxTagger._complete` would need to send the other field on Online; either service's model list may be unreadable (now an empty list, names typed) or may answer 400 for a bad key, which Test would then pass. Trigger: the photographer has a key for either.
- **Prove the no-thinking request against LM Studio, and try Ollama.** `chat_template_kwargs: {enable_thinking: false}` goes to Base, but on the test PC reasoning was switched off in LM Studio by hand, so nobody knows whether LM Studio honours it. Test: turn reasoning back on in LM Studio, pick one burst with the review model, read `reasoning_tokens` in its log. If ignored, find what it does accept. Ollama has not been run at all. Trigger: next session at the home PC.
- **Watch pick quality with reasoning off.** The one rationale seen with reasoning off was generic where the reasoning one named the cropped head and the busy wave. One sample on a different burst. Trigger: a pick that disagrees with the photographer's eye; then compare the same bursts both ways.
- **A job left "running" by a crash may block the engine switch.** `web._check_saved_change` refuses a change of engine while a `web.MODEL_BATCHES` job is queued or running; nobody checked whether a job row left running by a killed server is cleared at startup. Trigger: check before the next release; a refused switch with no batch visible is the symptom.
- **Test cannot prove a key where the model list is unreadable.** `omlx.list_models` answers empty for 400, 404 and 405, so on such a service Test passes with a wrong key and the first chat call is what refuses it. A one-token chat call from Test would prove it, at a small cost per press. Trigger: a photographer saves a wrong key and is confused by a passing Test.
- **Tokens per model, and an estimated cost.** The tally is one total per engine for the running server; fast and review models are priced differently and the count is lost on restart. Cost needs a per-model tally and prices typed in Settings, since Lumen has no price list. Trigger: the photographer asks after a real batch on Online.
- **Safari and VoiceOver pass on the Settings view.** It was driven in headless Chromium only, and the fold-away Base and Online setup was never seen in a browser by its author. One-at-a-time folding uses `<details name>`, which needs Safari 17.2; older versions let both open.
- **Left as they are, with the reason to revisit.** A model name missing from a service's non-empty list is refused until Test refreshes the list. Two settings saves at the same instant can drop a model switch made in the rail (`nonlocal settings` rebound without a lock). `lumen serve` started on a remote engine skips This Mac's review-model pick. `preferences.save` commits per key. Trigger: any of them is hit in use.
- **Restart server action.** A button in the Settings view's Server section that restarts `lumen serve`, so a stale server (the `stale` reasons shown there) or a setting that applies after a restart does not need a terminal. A non-goal of `docs/superpowers/specs/2026-11-21-settings-and-ai-engine-design.md`: it needs its own rules, at least refusing while an export or astro job runs, and a way for the page to find the server again. Trigger: the photographer asks, or a setting that applies only after a restart is added.
- **A different engine per model role.** Fast-role work (tagging, reject scan, triage) on This Mac and the review role on Base or Online, or the other way round. A non-goal of the same design; one engine switch was chosen so there is one thing to understand and one marker to show. It would sit on top of `Library.tagger_for(role)`. Trigger: a big cull where tagging should stay free and local while review goes elsewhere. Evidence from 2026-11-21: on a home PC with a 16 GB graphics card the fast model tagged in a second or two against 3 s on this Mac, while the review model did not fit and ran at half this Mac's speed, so tagging on Base with review on This Mac would beat either engine alone.
- **A service's native API, and judgment classifiers.** See the design's "Room for other kinds of engine": a second wire format is a second branch in `OmlxTagger._complete` chosen by a saved setting per engine; a classifier for the fast role is a second tagger class returned by `Library.tagger_for("fast")` and needs the engine-per-role item first. Trigger: a service's chat-compatible endpoint proves unreliable or is withdrawn, or a classifier proves better at the fast role's yes/no and one-of-a-set questions.

- **JPEG exports outside the library.** The photographer wants exports on an external or shared drive because the Mac's disk is short. Taken out of the settings round because the export path needs investigating first. Known so far: three places pin exports to the library (`Library._prepare_export`, `web.reveal_outputs`, and `DarktablePipeline.export_jpeg`, which hard-codes `output_root / "jpeg"`); exports publish with `os.link` from a temp directory inside the destination, and exFAT and most network shares have no hard links; the contract line in `AGENTS.md` keeping derived JPEGs inside the library would need rewording; the destination must never be a folder holding catalogued originals, and a drive root above them needs a per-export check on the final dated directory; nobody has looked at what the Export tab and the contact sheet's exported state do when the drive is not connected. Trigger: the photographer asks again, or the Settings page has shipped and has a place for the folder.

## From the 2026-11-01 Map tab

- **Time-scrubbing along a track.** A slider that walks a GPX track and highlights the frames taken near each moment. A non-goal of `docs/superpowers/specs/2026-11-01-map-tab-design.md`; the tab places frames and draws tracks but has no time axis. Trigger: the photographer asks where they were at a given hour more than once.
- **Shown frames goes stale when the Review filters change.** With the Map tab open, changing the Review filters does not refetch `/api/map/photos`; switching tabs or the Frames radio does. Trigger: the first time it misleads.

## From the model-speed research (closed 2026-10-31)

- **Species confidence gate.** Both models list species on frames with no animal: the 8B put six vocabulary species into `subjects` at 0.1 to 0.6 on an empty hill, the 27B a kite at 0.1 to 0.6 on three frames without one. A cheap rule to test first: drop species when `subjects` names no animal, and drop any species under a floor (0.5 looked right on the safari and seabird runs). Needs a few frames with ground truth (ledger step 8) so the floor is judged on a score. Trigger: the next tagging change, or the first time a wrong species reaches an export's IPTC keywords.
- **Other-family review models.** `gemma-4-26b-a4b-it-4bit` (15.3 GB) is the one candidate with plausible upside over the 27B; Gemma 3 12B QAT, Gemma 4 e4b and GLM-4.6V-Flash are sized in `docs/model-speed/HANDOFF.md`. Deferred because each is hours on the satellite link and the expected gain is small. Trigger: an idle night on a faster connection, or an oMLX release that caches hybrid-model prefixes.

## From the 2026-10-31 assessment (`docs/ASSESSMENT.md`)

- **16 GB test.** The README says the fast model needs about 6 GB and 16 GB is the minimum, but it has only run on a 48 GB M4. Needs another machine; confirm or change the claim to "untested".

## From the 2026-10-23 UI volume review

- **Whole-catalog payload.** `/api/groups` returns every photo of every burst on each `load()` (600 KB, 282 ms for 1001 frames; client render 23 ms). Paging is client-side only. Fine to ~5 k frames; add server paging keyed by burst when a real library crosses that or the fetch exceeds ~1 s.
- **Safari and VoiceOver pass.** No manual pass was done for the sticky-layout, thumbnail, and tab changes; the headless checks ran in Chromium only.

## From the 2026-10-25 frame view

- **L-size ARW embedded JPEG size unverified.** The Full size toggle serves `JpgFromRaw`; on M-size raws that is 3504x2336. Run `exiftool -b -JpgFromRaw file.ARW | sips -g pixelWidth -g pixelHeight` on an L-size file; if it is not ~7008 px, decide whether to fall back to a darktable render.
- Empty "Suggested keywords" heading shows on untagged frames.
- Correcting a remembered place now replaces manual rows within 1 km; rows further apart than that but inside the 25 km radius still coexist and the nearer one wins.
- The viewport is dark while the full-size JPEG loads.
- The "thumbnails pruned" log count includes full-size files.

## From the 2026-10-25 own crop

- The 8 MP crop error passes through the route by string match on the message; a typed error would be sturdier.
- Reopening the same frame keeps the previous crop outline until its crops arrive.

## From the 2026-11-23 Lumen.app and its disk image

The Homebrew tap planned here on 2026-10-26 was dropped for `Lumen.app` (design `docs/superpowers/specs/2026-11-22-app-dmg-design.md`), which needs no Homebrew, uv or Xcode tools on the photographer's Mac.

- **Automatic updates.** A new version is a new disk image dragged over the old app. Trigger: photographers ask for it, or releases come often enough that old copies linger.
- **A checkout server on another port goes undetected.** The app only probes `127.0.0.1:8765`; a `lumen serve --port <other>` on the same library is not seen, so the app starts a second server on it. Trigger: someone runs both and hits it.
- **A release build in CI.** The build runs on the photographer's Mac, where the signing certificate lives. Trigger: more than one person cuts releases, or the certificate can live in CI secrets.
- **Local AI in the app's setup.** Not offered (photographer, 2026-11-22): oMLX must be opened once before it has a model folder, its server is started by hand, and the models are 6 to 22 GB. The shell route still installs them. Trigger: oMLX can be installed and started without a person, or a smaller vision model is good enough.
- **Uninstall's Keychain removal runs on the main thread.** Two `security` calls with a 15 s timeout each; the window can stall in the worst case. Trigger: a stall is seen.
- **Quit and closing the setup window can block for up to 10 s** while the server or the setup script stops, with the menu bar frozen. Trigger: it is noticed in use.
- **A server too busy to answer `/api/jobs` within a second counts as having no jobs.** Quit and Uninstall then stop it without asking about a running job. Trigger: a job is lost that way, or the server is seen to stall that long.
- **A slow Lumen on port 8765 is read as absent.** The launch probe waits a second; a Lumen that answers later is not seen, so the app tries 8765 itself, fails to bind and says "Lumen could not start." Trigger: it is reported, or the probe moves to a connect check.
- **exiftool.org serves only the current version.** The tarball comes from the SourceForge link exiftool.org gives and is checked against exiftool.org/checksums.txt; a version bump means a new URL and hash in `scripts/build_app.sh` (SourceForge keeps the old tarballs). Trigger: the next exiftool bump.

## Keep the export 16-bit through crop and resize

`raw_developer` writes a genuine 16-bit TIFF, but Pillow caps RGB TIFFs at
8 bits per channel when `export_jpeg` reads it back, so the crop, resize and
watermark all run on 8-bit pixels. The develop maths itself happens in Core
Image's float pipeline, which is where banding would otherwise have come from,
so this is a refinement rather than a defect.

Moving the crop and the long-edge resize into the helper — both are Core Image
one-liners — would push the truncation to after the resample. Deliberately not
done in `2026-10-26-develop-controls-design.md`: the Pillow assembly is
working, tested code with EXIF-orientation subtleties, and the risk of
double-applying orientation outweighs a difference nobody has seen yet.

Trigger: visible banding in a smooth gradient (a sunset sky) in an exported
JPEG that looked clean in the develop preview.

## Carry the camera MakerNote into exported JPEGs

Neither `sips` nor Core Image carries tag 37500 through the render, so the
MakerNote rebase in `export_jpeg` only ever fires for darktable renders — and
darktable cannot decode this camera's reduced-size raws. Exports from this
library therefore ship without the Sony MakerNote. Sourcing it with
`exiftool -TagsFromFile <original>` instead of from the render would fix it
for every decoder.

Trigger: wanting Sony-specific capture data (lens corrections, creative look,
focus point) to survive into the JPEG.

## Map: cleared thumbs and trash snapshot reads

Thumbs of cleared frames (`/thumb/cleared/<key>`) live in `<library>/cache/thumbs/`, so the next cleanup prunes them and the map rebuilds each from the trashed preview on demand. Trigger: the map is visibly slow to show cleared thumbs after a cleanup.

The trash scan opens each `trash/*/catalog.sqlite3` with `mode=ro`; `immutable=1` would also skip SQLite's locking and journal checks. Trigger: a `-wal` or `-shm` file ever appears in the trash.

## Map tab (added 2026-11-02)

- **Safari: clicking Map from the bottom of a long contact sheet does not scroll to the top.** Reported 2026-11-01 in Safari 26.5; Firefox behaves. Three fixes landed and did not clear it for the photographer: the Map tab now scrolls instantly and re-asserts the top after the map draws (a16d105), the main pane opts out of scroll anchoring (aac9559), and `.main-top` sits above `.contact-top` because at the sheet's end Safari slid that sticky header over the tab row and a WebDriver hit test showed the header taking the click (83a1281, verified by driving Safari through `safaridriver`, where the fixed page scrolls to 0). The remaining difference is something in the photographer's real session the probe does not reproduce: a candidate is the panel being folded (clicking the selected tab toggles the panel instead of scrolling), another is trackpad momentum eating the click. Trigger: next Safari session; reproduce with the Web Inspector console open (`document.querySelector('#main-content').scrollTop` before and after the click) before changing more code. Probe scripts: rebuild from the description above; they were in a session scratchpad.
- **A malformed polygon ring in a dropped overlay can fail `GET /api/map/features`.** `parse_geojson` checks that coordinates are nested lists of finite positions but not the nesting depth, so a `Polygon` whose coordinates are a bare ring (one level shallow) reaches `clip_ring`, which raises `TypeError` for every viewport over its box; the terrain lookup already swallows this (`Library.terrain_for`), the features route does not. Predates the terrain work. Fix: check geometry depth per type in `parse_geojson` and skip the feature as malformed. Trigger: a photographer-drawn `.geojson` overlay fails the map.
- **Terrain labels can be crossed by lines drawn later.** Mountain, plain and desert names draw in their kind's slot, so lakes, coastline, borders and roads drawn after can run through them. Trigger: a readability complaint at zoom 6; move area labels into a late slot or add simple collision avoidance.
