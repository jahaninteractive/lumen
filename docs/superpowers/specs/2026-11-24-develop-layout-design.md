# Lumen Develop Tab Layout Design

Status: tried and set aside on 2026-11-24. The photographer chose option A, saw
it in their own window and rejected it ("hate it - it doesnt make any sense"),
then rejected a rough stacked layout too ("no its bad - best to return it for
now"). The Develop tab is as 0.10.0 left it. See "What was tried" before
proposing any of this again.

## What was tried (2026-11-24)

- The photographer's window is about 1376 by 1385, not the 1456 by 900 this
  document assumed. There the column nearly fits and the gap under the photo is
  about 700 px, so a 100 px Straighten strip sits alone above a large blank
  area. Any future layout is judged at that size first.
- Option A was built on branch `feature/develop-layout` (commit `67f12af`, kept,
  not merged). It needed a DOM move: `#loupe .frame-panel` is the scrolling box,
  so CSS cannot place a child of `#develop-panel` under the photo.
- Stacked (photo on top, the whole Develop tab in three columns beneath, in
  windows 1150 px or taller) was a throwaway and is gone. It did not make the
  photo bigger: Full size, the tabs, the controls and the Export row take about
  650 px of a 1330 px dialog, leaving a photo of 887 by 592 against today's
  900 by 600. Four columns squeeze the sliders to nothing; three is the most.
- Option B does not fit at 900 px tall (about 115 px under a landscape frame).

## Intent

The Develop tab's right column has grown too long. With "Where to start" open it
is about 1450 px of controls beside a landscape photo that is about 600 px tall,
so the column scrolls while the space under the photo sits empty. Use that space
without changing what any control does.

Raised by the photographer on 2026-11-24, after Straighten was added (0.10.0) and
moved below "Reset all" and "Apply to every frame in this burst".

## What is in the column today, top to bottom

1. Histogram with the tone curve and its channel boxes, "Reset the curve".
2. Where to start: the suggestion's status line, the review model's rationale
   (about ten lines of prose), "What to change", Suggest settings, Discard the
   suggestion, Park the handles here. About 450 px when a suggestion is shown.
3. Brightness, White Balance, Contrast, Sharpness, Lens Correction, Noise
   Reduction (collapsible).
4. How this works (collapsible).
5. Reset all, Apply to every frame in this burst.
6. Straighten (collapsible; opens on a frame with an angle): "Set per frame; not
   copied to the burst.", slider, number, Auto, Draw horizon, Reset, megapixels
   left, hint line. About 190 px open.
7. The status line.

Under the photo there is only the Full size button.

## Goals

- The column fits, or nearly fits, a 900 px tall window on a landscape frame
  with one section open.
- Controls sit beside what they act on: picture geometry with the picture, tone
  with the histogram.
- No control changes its id, behaviour, saved value or wording.
- Keyboard order stays sensible: it follows the new reading order.

## Non-goals

- New controls, new develop settings, or any server change.
- Restyling: the charcoal, amber and cyan workbench and the native controls stay.
- The Frame, Crop and EXIF tabs. The band under the photo exists only while the
  Develop tab is selected.
- Moving the histogram. It has to stay beside the sliders while they move.

## Options

### A. Picture tools under the picture

Straighten leaves the column and becomes a strip under the photo, beside Full
size: slider, number, Auto, Draw horizon, Reset, megapixels left, hint line.

    +------------- photo -------------+   Frame  Crop  [Develop]  EXIF
    |                                 |   histogram + curve
    +---------------------------------+   Where to start ...
    [Full size]  Straighten --o-- 5.7     Brightness, White Balance ...
    [Auto] [Draw horizon] [Reset]         How this works
    25.0 MP remaining                     Reset all / Apply to burst

For: the photographer watches the horizon while turning it, so the control
belongs under it. It is plainly not part of what "Apply to every frame" copies,
which the note has to say today. Against: it removes only about 190 px.

The strip is always visible on the Develop tab (no collapsing): it is one or two
rows. The note "Set per frame; not copied to the burst." stays, as the strip's
last line or its title text.

### B. The suggestion reads under the picture

"Where to start" leaves the column and sits under the photo at the photo's
width: status line, rationale, "What to change", Suggest settings, Discard, Park
the handles here.

    +------------- photo -------------+   histogram + curve
    +---------------------------------+   Brightness
    [Full size]                           White Balance ...
    Where to start                        Straighten
    The histogram median at 28 ...        Reset all / Apply
    What to change [______________]
    [Suggest settings] [Discard]

For: it is the tallest block and it is prose, which reads better at 60 to 75
characters than in a 380 px column. The column becomes histogram and handles
only. Against: the rationale talks about the histogram, which would sit
diagonally across from it rather than directly above.

### C. Both

A and B together: the band under the photo holds the picture tools first, then
the suggestion. The column keeps the histogram, the six tone sections, How this
works and the two burst buttons, roughly half its present height.

    +------------- photo -------------+   histogram + curve
    +---------------------------------+   Brightness ... Noise Reduction
    [Full size] Straighten --o-- 5.7      How this works
    [Auto] [Draw] [Reset]  25.0 MP        Reset all / Apply
    Where to start: The histogram ...
    What to change [______] [Suggest]

For: the column probably stops scrolling. Against: the largest change, and the
keyboard order changes most.

## Recommendation

A first, then B, which arrives at C in two reviewable steps. A is small and
answers the question that started this (Straighten is per frame). B deserves the
photographer's eye on reading width and on the rationale sitting away from the
histogram before it is kept.

## Constraints on every option

- Portrait frames fill the viewport height (`--loupe-h`), so the band starts
  lower and the dialog scrolls to reach it. Landscape frames get the benefit.
- Below 980 px the frame view is already one column; the band stacks between the
  photo and the controls there.
- Full size keeps the viewport's fixed box to pan in; the band stays below it.
- The straighten grid and Draw horizon overlay are children of
  `#loupe-viewport` and are re-attached after every preview load. They do not
  move.
- The band is hidden on the Frame, Crop and EXIF tabs, so the crop tab's own
  layout under the photo is unchanged.
- Tab order follows DOM order. Whatever moves under the photo is reached after
  the viewport and Full size and before the tablist, or after the panel; decide
  once and test it.
- `tests/test_web.py` pins that every develop range's slider and number field
  live inside `#develop-panel`, and pins the order of the section headings. A
  control that leaves the panel needs that test changed on purpose, with the
  reason in the test.

## Open points for the plan

1. Whether the band is part of `#develop-panel` in the accessibility tree (one
   `role="tabpanel"` with two visual regions, using CSS grid placement) or a
   second region labelled for the same tab. Grid placement keeps the DOM, the
   pinned tests and the tab order almost as they are, and is preferred if the
   dialog's layout allows it.
2. Where "Park the handles here" and the per-panel status line (`develop-task`)
   go in option B.
3. Whether the Straighten strip keeps a heading.

## Tests

- The existing Develop HTML and JS harness tests pass, changed only where a
  moved control makes the old assertion impossible.
- A new HTML test for the reading order under the photo.
- A screenshot of the Develop tab at 1456 by 900 and at 900 by 1000, landscape
  and portrait, read by the lead before the photographer is asked to look.
- `node --check src/lumen/static/app.js`.
