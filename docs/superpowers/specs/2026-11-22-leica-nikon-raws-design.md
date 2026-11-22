# Leica and Nikon raws: design

Date: 2026-11-22. Status: approved 2026-11-22.

## Intent

Lumen catalogues, culls, develops and exports Sony `.ARW`. The photographer wants the same for Leica `.DNG` and
Nikon `.NEF`, with nothing new to install.

## What was measured

Eight sample files, probed with the installed tools (macOS Core Image through `raw_developer`, darktable 5.6.2,
exiftool). None is in the repository.

| File | Body | Core Image | darktable | Largest embedded JPEG | Lens profile |
|---|---|---|---|---|---|
| `RAW_LEICA_M240.DNG` | M (Typ 240), 2014 | renders | renders | 160 x 120 | no |
| `L1004235.DNG` | M9, 2009 | renders | renders | none | no |
| `9268498212.dng` | M11 Monochrom, 2023 | renders | renders | 2112 x 1408 | no |
| `1716728168.dng` | M11, 2022 | renders, every control acts | renders | 2112 x 1408 | no |
| `Mlenny.com-Leica-Q3-RAW-sample.dng` | Q3, 2024 (re-saved by Lightroom) | renders, every control acts | renders | 9520 x 6336 (Lightroom's) | yes, on by default |
| `RAW_NIKON_D800_…NEF` | D800, uncompressed | renders | renders | 7360 x 4912 | no |
| `_AUS9380.NEF` | Z 8, high-efficiency | renders | **fails** | 8256 x 5504 | yes |
| `Nikon-Z9-raw-00002.nef` | Z 9, high-efficiency | renders | **fails** | 8256 x 5504 | yes |

Other differences from Sony files:

- `PreviewImage`, the tag tried first today, is 570 and 640 px on the Nikons and 160 px or absent on the Leicas.
- The M240 and the M11 write a GPS block of latitude 0, longitude 0 with an empty hemisphere reference.
- Leica's serial is in `CameraSerialNumber`; the M9 sample's is masked as `*******`.
- The M11 Monochrom is a one-channel raw (photometric 34892). Core Image renders it, but sharpness, contrast and
  both noise reductions change nothing on it (measured: zero difference), and tint colours the frame.
- Its embedded JPEG is 2112 px against a 9536 px raw, so it serves as a preview but not as a full-size view.
- The M11 records `FNumber` 1 behind an f/2 lens (the body cannot read an M lens's aperture) and the M240
  records none; the EXIF tab and the astro stem show what the file says.
- A 60 MP M11 render takes 4.8 s warm (10 s cold) against about 1 s for the Z 9 and 0.4 s for the Sony. The
  Develop tab will feel slow on M11 files; the build measures it and the guide says so. Speeding it up is not
  in this round. (The build's real run, 2026-11-22, measured 0.8 s warm at the tab's window size and 1.4 s
  at full size; the guide gives those.)
- The Q3 records a true aperture, a time zone and a serial, and its lens correction works (measured: turning
  it off changes the render). The sample was re-saved by Lightroom, so its embedded JPEGs are Lightroom's, not
  the camera's: what a Q3 embeds straight from the card is unmeasured, and the preview rule covers either case.
- Also: neither Leica nor the D800 records `OffsetTimeOriginal`.

## Goals

- Import, preview, cull, group, develop and export `.dng` and `.nef` exactly as `.arw`.
- A Leica frame gets a real preview and a real full-size view although the file carries none.
- A high-efficiency Nikon raw says plainly what darktable cannot do with it, as a reduced-size Sony raw does.

## Non-goals

- Canon, Fuji or other makers (the shared list makes each a one-line addition once a sample is measured).
- Per-maker develop defaults: the helper already reports each file's own.
- DNGs from phones or converters. They will import, but only camera DNGs were measured.
- Committing the samples: they carry other photographers' work and total 185 MB.

## Anti-goal

A Leica or Nikon frame that silently behaves as a non-raw: no develop panel, skipped by copy-to-burst, a
postage-stamp preview, or a pin at 0°N 0°E.

## Approaches

1. **Extend the raw list and fix what the samples broke (chosen).** Every file decodes through the route ARW
   already takes; the work is the four places that assumed Sony.
2. **Convert to DNG or TIFF on import.** Rejected: writes large derived files for no gain, since both formats
   decode directly, and puts a conversion between the photographer and the original.
3. **Route the new formats through darktable.** Rejected: it fails on the Z 8 file and would bypass the develop
   settings, which only the raw developer applies.

## Design

- **One list.** `pipelines.RAW_EXTENSIONS = {".arw", ".dng", ".nef"}`. `domain.SUPPORTED_EXTENSIONS` and
  `astro.ASTRO_SUFFIXES` are built from it. `library.py`'s copy-to-burst check and the Develop tab stop comparing
  the format to `ARW`: `GET /api/photos/<id>` carries `is_raw`.
- **Previews.** As built (a review ruling changed the order to keep a Sony import at one exiftool run):
  `ExifToolPreviewProvider` takes the first embedded JPEG at least `MIN_EMBEDDED_PREVIEW` (1000) long in the order
  `PreviewImage`, `OtherImage`, `JpgFromRaw`, `ThumbnailImage`, so a Nikon preview comes from its 1620 px
  `OtherImage`; with none, a raw is rendered by `raw_developer` at the preview size, with `sips`, darktable and the
  largest thumbnail behind it. `full_size_for` reads `JpgFromRaw`, `OtherImage`, `PreviewImage` and renders at
  `long_edge: 0` when the largest embedded JPEG is under half the raw's own `ImageSize` (the M11's is a fifth of
  it) or absent. The provider takes the helper's path where it is constructed.
- **Metadata.** Serial is `SerialNumber`, else `CameraSerialNumber`; a value with no letter or digit is
  `unknown`. A fix of exactly 0, 0 is no fix.
- **Unsupported controls.** The helper reports `supported`, the decoder's own answer per control for that file
  (`CIRAWFilter.isSharpnessSupported` and its siblings), replacing the lone `lens_correction_supported`. The
  panel disables an unsupported slider with the reason, as it does the lens box today; `DevelopSettings` saved
  for such a control is refused by the helper rather than silently ignored. Tint on a monochrome raw is left
  alone: it tones the frame, which is harmless and arguably wanted.
- **Warning.** `raw_warning` is set for a `.nef` whose `NEFCompression` is high-efficiency (13 or 14), worded as
  the Sony one: darktable cannot decode it, the refine pass and `--profile` exports are unavailable, JPEG export
  is unaffected.
- **Export.** No route change. Verified by a real full export of each sample: develop settings applied,
  MakerNote copied from the original, IPTC written.
- **Astro.** DNG and NEF lights are accepted. The debrief stem already has to cope with a missing value; the
  aperture part is omitted when the lens recorded none.
- **Words.** README, guide, the About text and the import promise say Sony, Leica and Nikon raws. `AGENTS.md`,
  `CHANGELOG.md`, version 0.7.0, `docs/HANDOFF.md`.

## Tests

Red-green, warning-strict, per item above. Fixtures are built in the test (injected exiftool and helper
runners), as the suite does now; no test reads `Examples/`.

## Left to the builder

The exact warning text, where the helper path is threaded into the preview provider, and whether `is_raw` is
also useful on the group payload.

## Not yet measured

A straight-from-camera Leica Q file, an SL file with a built-in lens correction, and a
lossless-compressed Nikon Z file. Any may differ; each is supported once one imports.
