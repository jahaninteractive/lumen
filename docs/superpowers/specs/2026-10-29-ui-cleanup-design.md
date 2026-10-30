# UI clean-up design (2026-10-29)

Reviewed at 1440 px against the running server. The Export tab opens by default and fills the first screen; the contact sheet is below the fold. The header prints the model name three times. Four status surfaces compete (header status, job strip, footer, per-panel status lines). Per-frame local AI calls hold the library lock for the whole model call, so culling waits.

## Decisions

1. **Assist tab.** Run-all, tag keepers, tag all, learned keywords and the day digest move out of Export and Review into one Assist tab. Export holds only scope, size, watermark, profile, destination. Review holds suggested rejects only.
2. **Default tab from state; panel folds.** Clicking the selected tab already folds the panel. A fresh visit (no remembered tab) opens Add photos when the catalog is empty and opens folded when it has frames.
3. **One place for the model, one for activity.** Header: identity, tool health, stale warning; no model names. Footer strip: Review and Fast selects, the single "model not loaded" sentence, the AI activity line, and the job progress bar with Cancel. The top job strip is gone.
4. **Advice runs without blocking and is remembered.** The library releases the mutation lock while a model answers and takes it only to write. The browser drops its busy flag for advice calls, shows a per-task status line in the panel that asked ("running", "ready", "failed: reason"), and reloads the stored answer when the frame or burst is reopened: burst critique (already stored), composition review (stored, now read back through the crops payload), develop suggestion (stored, now read back on the photo payload).
5. **One line of prose per panel, the rest behind "How this works".** Keyboard hints move to the Shortcuts dialog. The Export glossary goes.
6. **Rail shows what applies.** Merge controls appear once a batch is ticked; Jump to selected once a burst is selected. Digest lives in Assist.
7. **AI button grammar.** Verb plus object, no "with local AI" suffix, one shared marker (class `ai`, a `✦` prefix drawn by CSS). The not-loaded sentence appears once in the footer and once at the top of the frame dialog panel, never under each button.
8. **Frame view hierarchy.** "Choose as best" is the primary button; tagging is secondary. One "Export this frame" button with its options in the dialog footer on every tab; the Crop and Develop copies go.
9. **Contact sheet heading.** Refine scope and Refine previews sit behind a "Refine previews" disclosure under the filter bar. Compare marked shows only when a frame is marked. The filter bar stays two rows with no fold (user decision 2026-10-26).
10. **Add photos tab.** Ingest and Import become one tab with a two-choice radio: "Still on the memory card" (Ingest) and "Already on a drive" (Import). Panel ids `ingest-panel` and `import-panel` stay as the two sections.
11. **Astro calibration grid.** Darks, flats and biases become three rows of label, path, Choose, reset.

## Not changed

Segmented status filter, develop slider rows, dialogs, palette, 44 px controls, live announcements, tab keyboard model, loopback-only server and JSON validation.

## Copy

Panel first lines:

- Add photos: "Copy a memory card with verification, or add photos already on a drive. Originals are never moved, renamed or changed."
- Review: "The fast model looks for technical failures in keepers. Nothing is rejected until you choose Reject."
- Assist: "Tags, a problem scan and a caption for every keeper that lacks them. Already finished work is skipped."
- Astro: "Stacks the selected batch with one reviewed Siril script. Camera originals are never changed."
- Export: "sRGB JPEGs with camera EXIF, caption, keywords and rating. Only Full resolution keeps GPS."
- Clear: "Camera originals and finished exports remain. Working files move to recoverable library trash."

Header health: `Local AI online` or `Local AI offline`, followed by `<tool> missing` for each unavailable tool, joined by ` · `, after any stale reasons.

AI button labels: `Tag, scan and caption keepers (N)`, `Tag keepers (N)`, `Tag every untagged frame`, `Scan keepers for problems`, `Triage lights`, `Expand search`, `Pick the best frame`, `Suggest a crop`, `Draft a caption`, `Suggest settings`, `Tag this frame`, `Write today's digest`.

Per-task status lines: `<Label> running…`, `<Label> ready`, `<Label> failed: <reason>` where Label is `Burst pick`, `Crop suggestion`, `Develop suggestion`, `Caption`, `Tags`.
