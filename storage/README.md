# Storage audit (`storage/`)

Read-only audit of files scattered across the Mac, external drives (LaCie1) and
the QNAP. It never moves, renames or deletes anything.

Real settings live outside the repo in an iCloud folder that mirrors this one
(`repos/scripts/storage` -> `<iCloud>/scripts/storage/config.json`). Nothing in the
code spells that path out; it is found in this order:

1. `--config PATH`
2. `$STORAGE_AUDIT_CONFIG`
3. a git-ignored `config.json` beside the script: either full settings or just a
   pointer, `{"config_file": "<path to the real config>"}`
4. `<iCloud>/<repo name>/<section>/config.json`, with the iCloud root taken from
   `finance/statements/paths.py` (the one place that defines it, optionally set for all repos
   by `~/.config/repos/config.json`, e.g. `{"icloud": "~/iCloud"}`)

Copy `config.example.json` to the real location and edit the roots; the local pointer file is optional. The catalogue
database stays local (`~/Library/Application Support/storage_audit/`), never in
iCloud. Reports go to `report_dir` if set, otherwise a `reports/` folder beside
`config.json`.

## Visual version

```bash
storage/storage_audit_ui.py            # opens http://127.0.0.1:8765 in your browser
```

A local-only web app (standard library, listens on 127.0.0.1 only) with tabs for
Overview (size and file-type mix per drive), Browse (unlimited folder drill-down, a
file-type colour key you can click to filter, colour mix bars per folder), Duplicates,
Lightroom (every `.lrcat` found, with Read buttons and photos on disk that a catalogue
does not reference), Aperture (every `.aplibrary`: size breakdown, projects, photos and
how many are already in Lightroom) and Run (scan, index, lrcat, aperture, dupes and the
HTML report, with a live job log).
Browse can show the folder you are in as a Table, Bars, Pie or Treemap (colour is the file type; click a
slice or tile to drill in). The Sunburst button in the top bar toggles a ring chart of the whole drive with
your current folder outlined and its share of the drive listed; click any arc to jump there.
An "Open in Finder" button at the top opens whatever folder you are viewing (macOS only, and only
inside your configured roots); catalogues, libraries and duplicate paths have their own
"Show" buttons. The static report offers "Copy path" instead. The UI opens the catalogue read-only, so it can stay
open while you scan, but stop it if a scan fails with "database is locked".

## Command line

```bash
storage/storage_audit.py scan                  # catalogue every root (or: scan LaCie1)
storage/storage_audit.py index                 # build the fast folder index (scan does this too)
storage/storage_audit.py lrcat --all           # read every .lrcat found (or name specific ones)
storage/storage_audit.py aperture --all        # read every Aperture library found
storage/storage_audit.py dupes --full          # size -> partial hash -> SHA-256
storage/storage_audit.py report                # writes index.html to report_dir
```

`index.html` is a self-contained results page with the same Overview, Browse,
Duplicates, Lightroom and Aperture tabs. Its folder drill-down is captured to four
levels (`report_depth` in config.json); use the visual version for deeper levels.

Notes:
- The catalogue is a local SQLite file in WAL mode (never put it in iCloud).
- Catalogues are only found inside scanned roots, so add your local Lightroom folder
  (`Lightroom-local` in the example config) as a root.
- Re-scanning a root replaces its rows, so run `dupes` again afterwards.
- `@Recycle`, `@Recently-Snapshot`, `.Trash` and similar folders are skipped
  (configurable via `exclude_names`).
- Reports and the catalogue contain real file paths, so keep them out of git.
