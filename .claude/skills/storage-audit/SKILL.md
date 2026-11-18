---
name: storage-audit
description: Use when running, extending or debugging the read-only storage audit (storage/storage_audit.py and its UI) that catalogues drives, duplicates and Lightroom catalogues.
---

# Storage audit

**When to trigger:** scanning drives or shares, finding duplicates or loose photos, reading Lightroom or Aperture catalogues, or changing `storage/`.

## Facts
- The audit is read-only: it never modifies, moves or deletes the user's files. Keep it that way. Anything that acts on files is a separate script with `--dry-run`.
- Real config lives in `~/iCloud/scripts/storage/config.json`, outside the repo. Use `config.example.json` for tests.
- The SQLite catalogue stays on a local disk, never in iCloud and never in the repo.
- Standard library only.

## Steps
1. Plan first and get approval. Check `git status` for in-flight changes in `storage/` and do not overwrite someone's uncommitted work.
2. Read `python3 storage/storage_audit.py -h` for current subcommands rather than relying on memory.
3. Test with the example config. Run `python3 -m py_compile` on changed files.
4. Any script that acts on files gets `-h`, `--dry-run` and `u+x` (see `~/iCloud/claude/AGENTS.md`). Run `./fix_exec_bits.sh --check`.
5. Report counts and real output only. Say what was not run against real drives.
