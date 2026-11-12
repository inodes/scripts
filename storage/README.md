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
Overview (size and file-type mix per drive), Browse (unlimited folder drill-down; it
steps straight through single-folder chains), Duplicates, Lightroom (photos on disk
that no loaded catalogue references) and Run (buttons for scan, lrcat, dupes and the
HTML report, with a live job log).

## Command line

```bash
storage/storage_audit.py scan                  # catalogue every root (or: scan LaCie1)
storage/storage_audit.py lrcat ~/Lightroom/Lightroom.lrcat   # repeat per catalogue
storage/storage_audit.py dupes --full          # size -> partial hash -> SHA-256
storage/storage_audit.py report                # writes index.html to report_dir
```

`index.html` is a self-contained results page with the same Overview, Browse,
Duplicates and Lightroom tabs. Its folder drill-down is captured to four levels; use
the visual version for deeper levels.

Notes:
- Re-scanning a root replaces its rows, so run `dupes` again afterwards.
- `@Recycle`, `@Recently-Snapshot`, `.Trash` and similar folders are skipped
  (configurable via `exclude_names`).
- Reports and the catalogue contain real file paths, so keep them out of git.
