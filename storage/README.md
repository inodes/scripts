# Storage audit (`storage/`)

Read-only audit of files scattered across the Mac, external drives (LaCie1) and
the QNAP. It never moves, renames or deletes anything.

Real settings live in `~/Library/Mobile Documents/com~apple~CloudDocs/Storage/config.json`
(copy `config.example.json` there and edit the roots). The catalogue database stays
local (`~/Library/Application Support/storage_audit/`), never in iCloud. HTML reports
go to the configured `report_dir`.

```bash
storage/storage_audit.py scan                  # catalogue every root (or: scan LaCie1)
storage/storage_audit.py lrcat ~/Lightroom/Lightroom.lrcat   # repeat per catalogue
storage/storage_audit.py dupes --full          # size -> partial hash -> SHA-256
storage/storage_audit.py report                # HTML: sizes, kinds, stale folders, duplicates, uncatalogued photos
```

`@Recycle`, `@Recently-Snapshot`, `.Trash` and similar folders are skipped (configurable
via `exclude_names`). The report contains real file paths, so keep it out of git.
