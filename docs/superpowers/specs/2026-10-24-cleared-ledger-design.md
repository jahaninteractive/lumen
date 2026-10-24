# Cleared-frame ledger: never re-cull what you already finished

Date: 2026-10-24. Agreed with the user in chat: a new card copy brought back ~230 of 388 frames that had been culled and cleared in an earlier session; removing them batch by batch was the chore. The user chose fingerprints over a time filter.

## Intent

A frame the photographer has cleared from Lumen is finished. Lumen should remember that by content (SHA-256 fingerprint, already computed on import) and keep such frames out of the next import unless explicitly asked for them, and offer one action that removes those already sitting in the catalog today.

## Goals

1. **Ledger.** Every `remove_group` / `clear_workspace` records the removed frames' fingerprints with a timestamp in a catalog table that cleanup never deletes.
2. **Seed from trash.** The ledger can be filled from the `catalog.sqlite3` snapshots Lumen already wrote into `<library>/trash/*/` on earlier clears, so past sessions count. Seeding is idempotent and runs before any ledger-driven action.
3. **Import skips cleared frames** by default and reports the count (`previously_cleared`) in the job result and job strip. An Import-dialog checkbox and CLI flag `--include-cleared` bring them in anyway.
4. **Remove previously cleared frames now.** A Clear-tab control shows how many live frames are in the ledger and removes them in one recoverable cleanup (same trash mechanics as today). Bursts that lose only some frames keep the rest with a valid best pick.

## Non-goals

A time-of-day filter and select-all on batches (user agreed to drop them). Fuzzy/visual duplicate detection across different files. Editing the ledger by hand.

## Anti-goals

Losing a frame the photographer wants: skipping is reported, never silent, and the include switch is one click. Deleting originals or anything outside the library. A ledger write that fails cleanup.

## Constraints

Pillow only; loopback server; strict JSON; warning-strict suite green; Node scenarios for browser changes; commit per task.

## Calls made here

- Table `cleared_fingerprints(fingerprint TEXT PRIMARY KEY, cleared_at TEXT NOT NULL)` created in `Catalog.__init__` like the others; `_cleanup` inserts before deleting rows (`INSERT OR IGNORE`).
- Seed reads `photos.fingerprint` from every `<trash>/*/catalog.sqlite3` via a read-only separate `sqlite3.connect` (`?mode=ro` URI); unreadable snapshots are skipped and counted, never fatal. `cleared_at` for seeded rows is the trash folder's timestamp prefix.
- A frame that was cleared and later deliberately re-imported with "include" is still in the ledger; "Remove previously cleared frames" would remove it again. Documented in the UI copy.
- Import summary gains `previously_cleared`; CLI import prints it; the import job result carries it.
