# Regroup by key and "Belongs with…"

Status: approved in conversation 2026-11-09; builder check done 2026-11-09, awaiting the photographer's read.

## Why

The photographer regroups every trip by hand. After import they go through the burst list and merge frames into
the moment they belong to: a person shot from a metre to the left, or the same frame taken again a minute later.
Two parts of this are a chore:

1. **The clicking.** Marking a burst for merge needs its "Select for merge" checkbox, which sits in a different place
   depending on the burst's layout (a single frame's header is compact and the label is hidden), so the mouse has to
   hunt for it. `M` already merges what's marked; nothing marks by key.
2. **The deciding.** A frame that missed its burst, often taken a minute later, has to be found by eye among
   dozens of nearby bursts.

## What the spike measured (2026-11-08, read-only, against the live library and its trash snapshots)

- Today's rule (time gap, then a dHash split) agrees with the photographer's final groups on about 79% of
  neighbouring frame pairs. The best tuned time-plus-dHash rule reaches 80%, and a rule using Apple Vision's image
  feature print is no better (72% on all history, level with the existing rule on the current trip). The photographer groups by moment and intent,
  which neither measure sees. **So Lumen does not auto-merge.**
- Ranking is different. For the 103 frames the photographer joined to a burst across a gap of more than 20 s, the
  feature-print distance put the right burst first 59% of the time, in the top 3 83%, and in the top 5 90%, out
  of a median of 13 bursts within ten minutes. **So Lumen suggests.**
- `VNGenerateImageFeaturePrintRequest` on a preview JPEG: 768 floats, about 7 ms a frame (4,429 previews in 30 s).

## Goals

- Mark and unmark a burst for merging from the keyboard, from any frame in it.
- Mark a run of bursts in one gesture.
- For the focused frame, show the bursts nearby that most resemble it and move the frame into one with a single
  action.

## Non-goals

- Automatic merging or regrouping by similarity. The spike showed it would be wrong about one pair in five.
- Learning a personal grouping threshold.
- A suggestions panel in the frame view (option B in the mockup). It can follow later if wanted.
- Changing the burst-gap or near-duplicate split.

## Anti-goals

- A frame changes burst without the photographer choosing it.
- A suggestion leaks a source path, fingerprint or raw feature print to the browser.
- The shortcut collides with an existing key or fires inside a text field or open dialog.

## Design

### Keys (burst list)

| Key | Action |
|---|---|
| `G` | Mark or unmark the focused frame's burst for merging. It's the same state as the checkbox (`state.mergeSelection`), so the box ticks and the rail's count updates. |
| `Shift`+`G` | Mark every burst from the most recently marked one to the focused frame's burst, inclusive, in list order. If none is marked, it behaves like `G`. |
| `M` | Unchanged: merges the marked bursts. |
| `B` | Opens or closes "Belongs with…" under the focused frame. |

`G` and `B` are free today (the shortcuts dialog lists P R S X F U 0–5 C M Z K). Both follow the existing culling
key rules: ignored in text fields and open dialogs. Add both to the shortcuts dialog in `index.html`.

A marked burst gets a cyan outline (`--cyan`) in addition to the ticked checkbox, so the state is visible without
finding the box. The checkbox stays for mouse users. The live region announces "Burst N marked for merge (k marked)"
and the unmark equivalent.

### "Belongs with…" (option A: inline under the focused frame)

- Opened with `B` on a focused frame. It renders as a panel directly under that frame's burst grid, inside the burst
  section, and closes on `B`, `Esc`, or when focus moves to another burst.
- It lists up to **four** other bursts, nearest first by feature-print distance. A burst's distance is that of its
  closest frame to the focused frame. Candidates are bursts from the same camera with at least one frame
  captured within **10 minutes** of the focused frame, excluding the frame's own burst. *(Builder's call, see
  Decisions.)* The route takes the same filter query string as `GET /api/groups`, so every candidate is a burst the
  browser already lists; the browser names it "Burst N" from its own list, as it does everywhere else.
- Each row shows up to three thumbnails, "Burst N · HH:MM · k frames", and a **Move here** button. Rows are in tab
  order; `Enter` on a row's button moves the frame. `G` and `B` take no key from rating or culling; while focus is on
  a panel button, culling keys are ignored as they are on every other non-frame button today.
- No distance number is shown; the order is the signal.
- Empty state: "No similar bursts within 10 minutes." If the frame has no feature print and cannot get one
  (helper missing), the panel says the helper is missing and names `scripts/bootstrap.sh`, like other helper refusals.

### Moving a frame

A move puts one frame into another burst and must survive automatic regrouping and re-import, exactly as merges and
splits do today. It is a combination of the two existing mechanisms: the frame leaves its burst like `split_photo`
(its old burst's merge pointers are re-anchored by `_reanchor_merges`), then joins the target as a merge does (its
`merged_into` names the target burst's stable key). It appends a `frame_moved` provenance event
`{group_id, from_group_id}`, re-ranks both bursts, and rewrites sidecars as the other two do. The response tells the
browser enough to re-render both bursts and keep focus on the moved frame in its new burst.

Moving the last frame out of a burst leaves that burst empty, and it disappears as a merge would make it.

The move must not regroup frames the photographer didn't touch. If the moved frame bridged a time gap, its old
burst's remaining frames are pinned together (merge pointers to the old burst's anchor) in the same transaction, so
the old burst stays one burst. A manual pick on the moved frame is dropped, as a merge keeps only one.

After a move the browser re-renders both bursts and focus stays on the moved frame in its new burst.

### Feature print

- `tools/vision_analyzer.swift` adds `VNGenerateImageFeaturePrintRequest` to the requests it already performs and
  returns the vector in its JSON. The schema version goes to 3, and `analysis.py` accepts 1–3 (including
  `parse_vision_regions`, which today accepts only 2), treating the print as optional.
- The print is stored per photo in the catalog alongside the other metrics (768 float32 values, about 3 KB a frame,
  about 0.8 MB for a 270-frame trip). It is never sent to the browser.
- New imports and preview refines get it through the existing `analyze_faces` path.
- The helper gains a batch form: given several image paths it performs every request on each and prints one JSON
  object per line, in order. The single-path form stays unchanged so `analyze_faces` and existing test stubs keep
  working. One process start costs about 0.15–0.25 s; the requests themselves take about 7 ms a frame.
- Frames imported before this change have none. A "Belongs with…" request computes the missing prints for the
  focused frame and its candidates in **one** batch call, from their existing previews, and stores them; no job.
  For a typical 40 candidates that is well under a second the first time, then free. The model never runs, so the
  mutation lock is held only to write the prints.
- `lumen doctor` and `/api/status` already report whether `vision_analyzer` is available; nothing new is needed.

### Route

`GET /api/photos/<id>/belongs-with` returns the candidate list: burst IDs, their display data and thumbnail URLs
only. The move is a mutation route (`POST /api/photos/<id>/move`, `{group_id}`) and keeps the loopback, host/origin
and strict JSON validation every other mutation route has.

## Constraints

- No new runtime Python dependency; distances are computed in plain Python (768-dimension Euclidean over at most a
  few hundred vectors).
- Originals are never touched. The feature print is derived data inside the library.
- Accessible native controls, keyboard tab behaviour and live announcements as elsewhere in the burst list.
- Version bump in `src/lumen/__init__.py` and `pyproject.toml`, a `CHANGELOG.md` line, `AGENTS.md` and
  `docs/HANDOFF.md` updated.
- Red-green tests; no test runs the Swift helper (stub it as existing tests do).

## Decisions made after the builder check (not discussed with the photographer)

- Candidates follow the browser's current filters, so a suggestion is always a burst on screen.
- One batch call to the helper for missing prints instead of a backfill job or one process per frame.
- A move pins the old burst's remaining frames together rather than letting time grouping split them; this
  follows the anti-goal that nothing changes burst unless chosen. (`split_photo` today does not pin; that is
  left as it is.)
- Culling keys stay ignored on panel buttons, as on every other non-frame button.

## Left to the builder

- The exact catalog column or table for the feature print, and its encoding.
- How the move is expressed in `Catalog` (one transaction, reusing `set_merged_into` and the split key handling).
- Panel styling within the workbench palette, following the mockup in `.superpowers/brainstorm/` (local only).
- Whether the on-demand backfill writes through `save_metrics` or a narrower update.
