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
File-type colours, mix bars and the sunburst need the folder index (`index`, or Build index in the UI; new scans
build it automatically). Charts default to a distinct colour per folder and can be switched to colour by file type.
Free space: each drive card and the top of Browse show how full the volume is (this scan / other files / free),
with a warning below 20% free and a banner on Overview below 15%. Charts at the top of a drive add "Used by other
files" and "Free space" slices (toggle: Show free space). Capacity is read live when the drive is mounted and
remembered from the last scan otherwise.
Overview has a "Drives and free space" table covering every mounted drive (configured or not); shares on the same
NAS volume are grouped. Type a size in GB, or click "Plan a move of this folder" in Browse, to see which drives it
fits on and how long the copy would take over Gigabit Ethernet, 2.5/10 GbE, Wi-Fi or USB.
Anything the UI starts shows a progress bar under the header with what it is doing.
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
- To scan the whole Mac disk add `"Macintosh-HD": "/"` as a root. `/Volumes` (your other drives), `/System/Volumes`
  (the same data again) and a few system paths are skipped automatically; add more with `exclude_paths`
  (absolute folders). Grant your terminal Full Disk Access so protected folders can be read. Don't also keep a
  root inside it (e.g. `~/Pictures`): the scan warns because those files would be counted twice.
- To look at a big folder of the main disk separately (a quick rescan, its own totals), add it as its own root and list the
  root's name in `"carve_out": ["Pictures"]`. Any root that contains it then skips it automatically, so nothing is counted
  twice and the overlap warning goes away. Leave the name out of `carve_out` to keep it in both. Re-scan the parent root
  after changing this, since its earlier rows still include the folder.
- The catalogue is a local SQLite file in WAL mode (never put it in iCloud).
- Catalogues are only found inside scanned roots, so add your local Lightroom folder
  (`Lightroom-local` in the example config) as a root.
- Re-scanning a root replaces its rows, so run `dupes` again afterwards.
- `@Recycle`, `@Recently-Snapshot`, `.Trash` and similar folders are skipped
  (configurable via `exclude_names`).
- Reports and the catalogue contain real file paths, so keep them out of git.

## Hardware report (`hw_report.py`)

Read-only report of this Mac's Thunderbolt and USB topology (with link speeds), storage, network
interfaces and mounted shares, so you can see which cable or port is limiting a drive or the NAS.

```bash
storage/hw_report.py          # writes <report_dir>/hardware/hardware_report.txt and raw.json
```

Serial numbers, MAC addresses, UUIDs and share user names are redacted before anything is saved.
Like the reports above, the output is for you only: keep it out of git.

- **Splitting a big folder out of a drive**: in the UI (Browse, any folder below the top of a drive) press *Split this folder out into its own root*. It adds the root and a `carve_out` entry to config.json (timestamped `.bak-` backup beside it), then offers to scan the new root and rescan the parent. A split-out root shows a banner ("Split out: X is the folder Parent / path") and is labelled "in Parent" in the drive list; *Merge back* reverses it.

## Verifying a copy (`verify_copy.py`)
`python3 verify_copy.py /Volumes/LaCie1/FCP /Volumes/FCP [--sub Drone.fcpbundle ...] [--hash] [--out report.txt]` is a read-only check that the
destination holds every file in the source and nothing on the source is newer. It reports missing files, source-newer files, same-time size
mismatches and (with `--hash`) SHA-256 mismatches; files edited later on the destination are listed as fine. Final Cut cache (Thumbnail Media,
`.fcpcache`) is ignored. Exit status 1 means something needs copying before the source is retired. `--hash` reads both copies in full, so run it
per bundle, ideally overnight.
