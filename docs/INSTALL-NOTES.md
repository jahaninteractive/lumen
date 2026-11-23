# Installing Lumen from the disk image: notes for the install guide

Source material for whoever writes the website's install page. It records what
the app does at each step, the exact words on screen, and what confused the
photographer during three test installs on a clean Mac (2026-11-23, M4 Mac
mini, 16 GB, no Homebrew or developer tools). The reference for behaviour is
`AGENTS.md` ("Lumen.app"); for the reader-facing steps, `README.md` and
`docs/GUIDE.md`. If this file and the app disagree, the app is right.

## Who the page is for

Photographers who take a lot of pictures and are not at home in Terminal. They
should never be told to run a script or restart a server. The checkout route
(`scripts/bootstrap.sh`) stays for people who want it and belongs on a separate,
clearly technical page.

## What they need

- An Apple Silicon Mac, macOS 13.5 or later.
- About 80 MB for Lumen itself. The optional pieces are listed with sizes in
  the app, and it allows twice the listed size while installing.
- Nothing else: Lumen carries its own Python, exiftool and helpers.

## The steps, as the app presents them

1. **Open the disk image** (`Lumen-<version>.dmg`). It holds Lumen and a
   shortcut to Applications. It is signed and notarized, so macOS shows only
   its usual "downloaded from the internet" question.
2. **Open Lumen.** Launched from the disk image it asks "Move Lumen to your
   Applications folder?" Choose **Move**. It copies itself, opens the copy and
   the disk image can be ejected. (Dragging it to Applications by hand also
   works.)
3. **Welcome to Lumen.** The first launch shows one window. Its message:
   "Lumen is ready to use. These extras are optional; choose any to download
   now, or add them later from the Lumen menu." Nothing here is required.
   - **Places** (about 50 MB): turns each photo's GPS position into a place
     name, on this Mac. Most people want this. Under it, **Add a country…**
     picks the countries they photograph in by name, which adds parks,
     reserves and bays for those countries.
   - **Map** (about 100 MB): the world map behind the Map tab, offline. Most
     people want this.
   - **darktable** (310 MB): a free raw editor Lumen uses for refined
     previews, darktable looks, and exporting HEIC, PNG or TIFF photos. Raws
     and camera JPEGs export without it.
   - **Siril** (230 MB): a free astronomy program, only for stacking night-sky
     frames.
   - The bottom line shows the space needed and the space free. If there is
     not enough, Install warns first.
4. **Install**, or **Open Lumen** to skip. A progress bar runs and a log shows
   each download. It ends with "Finished. Press Open Lumen to start."
5. **Lumen opens in the default browser.** That is normal: Lumen's window is a
   browser tab, and the app in the Dock is what keeps it running.
6. **Coming back.** Click Lumen in the Dock to open the tab again.
   Right-click the Dock icon for **Open Lumen**, **Set Up Lumen…** and
   **Uninstall Lumen…**. The same items, and **About Lumen**, are in the Lumen
   menu, but the menu bar belongs to the browser while the tab is in front, so
   lead with the Dock icon.
7. **Adding extras later.** Set Up Lumen… shows what is installed. Countries
   already fetched read "Added: …"; new picks read "To add: …".
8. **Quitting.** Quit Lumen from the Dock or the menu. The server stops, and
   the browser tab says "Lumen has stopped" with a Reload button.

## Privacy prompts

macOS asks, the first time, for access to Pictures, removable volumes (memory
cards) and any Desktop or Documents folder photos are imported from, naming
Lumen. (Expected from the app's design; the tester did not record which
prompts appeared or their wording, so capture them before quoting.) Say they are expected and why: Lumen reads photos from those places and
never changes the originals.

## AI advice

The app does not install local AI. The setup window says: "AI advice: connect
a service in Settings, or see the guide to run it on this Mac."

- **Online or Base** (easiest): Settings (the gear, far right), AI engine,
  choose the engine, paste the key, press Test. This worked first time.
- **This Mac (oMLX)**: installed by hand from oMLX's own site. Settings names
  the models to download in oMLX. What the tester hit on 16 GB:
  - Only the fast model fits. Take that one only; the review model wants 32 GB
    or more.
  - oMLX refused to load it with its memory guard on `safe` ("does not fit
    under the dynamic memory ceiling"). On `balanced` it loaded. The page
    should show where that setting is in oMLX's admin page.
  - oMLX then suggested `sudo sysctl iogpu.wired_limit_mb=13312`. That is a
    Terminal command needing an administrator password, and it resets on
    restart. Present it as optional, for people comfortable with Terminal.
    Whether advice runs acceptably without it is not yet measured.
  - Close other apps before loading the model.

## Uninstalling

Right-click the Dock icon, **Uninstall Lumen…**. One window:

- Lumen and the tools and data it downloaded always go to the Trash.
- Two boxes, both off: "Also remove my working data" (ratings, picks, tags,
  crops, develop settings) and "Also remove my exported files".
- Camera originals are never touched. Saved service keys are removed from the
  Keychain. oMLX and any models are left alone; remove oMLX separately.
- It ends with "Lumen was uninstalled." Everything is in the Trash, so nothing
  is lost until it is emptied.

Worth saying on the page, because the tester was pleased by it: reinstalling
later finds the library that was kept, with all ratings and edits.

## Updating

There are no automatic updates. A new version is a new disk image; open it and
choose Move again, which replaces the old copy. The library and the downloaded
extras stay.

## What confused the tester, and what it became

Use these as the questions the page should answer before they are asked.

| Confusion | Now |
|---|---|
| Two-letter country codes meant nothing | Countries are picked by name |
| Could not tell the install had finished | "Finished." line and a Done or Open Lumen button |
| A warning about `hdiutil` in the log | Hidden |
| Could not find Uninstall or Set Up | Dock icon's right-click menu |
| Not clear Lumen was already installed at the setup window | "Welcome to Lumen… ready to use… extras are optional" |
| Did not know what Places, Map, darktable, Siril are for | One line each saying what and why |
| Reopened setup said "None chosen" for countries | "Added: …" shows what is installed |
| Browser tab kept half-working after uninstall | "Lumen has stopped" notice |
| Header said "omlx missing" while oMLX was running | No longer shown |
| Which oMLX model to download | Named in Settings |
| oMLX would not load the model on 16 GB | See "AI advice" above; needs a section on the page |

## Pictures

`LUMEN_APP_SNAPSHOT=<dir>` renders the setup, uninstall and About windows from
a built app without a screen (light and `-dark`, twice size); the last set is
in `dist/screenshots/`, which Git ignores. Their buttons are grey and they have
no shadow, and the uninstall pair shows a scratch folder, so real screenshots
from a Mac (Cmd-Shift-4, Space, click the window) are better for the page's
main pictures. Still to capture on a real screen: the Dock menu, the country
pull-down open, the progress bar mid-install, and the browser's "Lumen has
stopped" notice.

## Not yet tested

The astro (Siril) route in the app, the "Not enough free space" warning, an
update over an existing copy, and a first launch with no internet connection.
