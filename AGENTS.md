# AGENTS.md - rules for AI agents in this repo

Cross-project rules (plan first, commit approval, dry-run, backups, PII, direnv, "verified") live in the user's own global instructions (their global `CLAUDE.md` and cross-project `AGENTS.md`), outside this repo. Follow them too; if you cannot see them, tell the user. This file holds only what is specific to this repo, and it is generic: no personal specifics, safe to commit.

The project journal (open requests, known bugs, statement gap audits) is `AGENTS.local.md`, which is git-ignored. Read it at the start of a task and update it, not this file.

## 1. Layout

- `process_pdf.sh` (repo root): the generic PDF dispatcher run by Hazel. It offers each PDF to every `<section>/pdf_handler.sh`. It must never contain finance- or provider-specific logic.
- `finance/`: statement and invoice toolkit. Commands: `sync_statements.py`, `pdf_audit.py`, `audit_hazel_logs.py`, `fetch_email_invoices.py`, `pdf_handler.sh`. The library is `finance/statements/` (`config.py`, `money.py`, `pdf.py`, `store.py`, `ingest.py`, `report.py`, `paths.py`) with pluggable providers in `finance/statements/providers/`.
- `storage/`: read-only drive and photo inventory (`storage_audit.py`, `storage_audit_ui.py`).
- Each tool exists once. `finance/statements/providers/agl_parse.py` is shared with the tesla repo through its `scripts_repo` config key, so do not duplicate it there.
- Section details: `finance/README.md`, `storage/README.md`, `PDF_HANDLING.md`.

## 2. Working rules

1. **Approval first.** Never move, rename or delete user PDFs without approval. Demonstrate `--rename`, `--import` and `--delete-duplicates` with `--dry-run` unless the user explicitly asks for the live run.
2. **No PII in the repo**: no real account numbers, BSBs, card numbers, balances, transactions, statement PDFs, CSV exports or SQLite databases, in files, tests, docs or commit messages. Use `$USER`, `$HOME` or `~`, never a real username. Only sanitised `*.example.json` templates and synthetic `sample_variants` fixtures are allowed.
3. **Real data never lives in the working tree.** The SQLite database and cached PDF text live in the data directory (`directories.data`), real configuration and category rules in the user's config folder outside the repo. Show the diff and the exact commit message before committing.
4. **Never delete repo scripts from disk.** Move retired scripts to `archive/` and log why in `archive/archive_log.md`. `archive/` and `.bak_archive/` are local and gitignored: git history keeps old versions, so never commit an archive. Back up untracked files (`config.json`) into `.bak_archive/`.
5. **direnv**: keep `.envrc` (`layout python` plus `.python-version`) active with the repo's own `requirements.txt`. Anything that calls the repo from outside (such as `process_pdf.sh` under Hazel) must resolve `$SCRIPT_REPO/.direnv/python-3.11/bin/python3` first. Run `direnv allow .` on a fresh clone or after editing `.envrc`.
6. **Executable bit**: every `*.py` and `*.sh` is `u+x`. Run `./fix_exec_bits.sh --check` to verify; run `./fix_exec_bits.sh --install-hook` on a fresh clone.
7. **Provider-agnostic help**: `-h` on `pdf_audit.py`, `sync_statements.py` and `audit_hazel_logs.py` names no institution, account type or provider-specific example. Provider details come from `finance/pdf_audit.py providers` and `finance/sync_statements.py --providers`.
8. **"Verified" means you ran it**: `run_self_test()` across every `sample_variants` entry, plus `pdf_audit.py sync` and `pdf_audit.py check` on real PDFs with `0 differed`. Otherwise say "implemented, not tested on real data".

## 3. File handling

- **Locations**: never hard-code a user or cloud path. Use `finance/statements/paths.py` (`paths.get("pdf")`, `paths.statements(<name>)`, `paths.payslips(...)`, `paths.under("reports", "index.html")`), which derives every path from `config.json` -> `directories`. An optional machine-local bootstrap file, `~/.config/repos/config.json` (override with `$REPOS_CONFIG`), holds locations only: never settings or data, and never in a synced folder.
- **Check before writing**: call `paths.check_destination(dest)` before any move or write. It refuses loose files in container folders, anything inside the repo, and databases in a synced folder.
- The SQLite index stays local (`directories.data`); generated HTML goes to `directories.reports`; neither goes in a statements folder.
- **No clobbering**: if the destination exists during `--rename` or `--import`, the source is deleted only when `--delete-duplicates` is given and `is_true_binary_duplicate()` is true (it compares the PDF bytes after stripping download-time metadata, plus the extracted text). Otherwise it moves to `<target_dir>/Duplicates/` under the standard name, with `_01`, `_02` appended by `non_clobbering_path()`.
- **Companion text and dates**: every PDF renamed, imported or audited gets a companion `<name>.txt` of extracted text (moved with it). Both files have their timestamps set to local midnight of the document's `period_end` (fallback `issue_date`): `atime` and `mtime` through `os.utime()`, and on macOS the creation date through `SetFile`.
- **A match is not proof**: only rename or move a document that also yields a date and reconciles (`hold_reason()`); otherwise log `HOLD` and leave it in place.

## 4. Providers

A provider subclass of `BasePDFProvider` in `finance/statements/providers/<name>.py` holds all its own metadata and rules:

- Identity: `name` (snake_case), `display_name`, `doc_type`, `document_kind` (`statement`, `invoice`, `receipt`, `payslip` or `document`), `enabled`.
- Folders: `target_dir`, `duplicates_dir` (default `<target_dir>/Duplicates`), `scan_dirs`.
- `source_domains`: matched against the macOS download-source attribute. They pre-filter with `--where-from` and drive format-drift detection (the domain matches but `detect()` fails).
- `filename_template`: rendered from `{provider}`, `{doc_type}`, `{customer_no}`, `{account}`, `{account_no}`, `{last4}`, `{date}`, `{period_start}`, `{period_end}`, `{issue_date}` and their `_compact` forms.
- `cadence`, `expected_period_days`, `max_gap_days`: used by `check_missing_periods()`.

**One provider per document, many format versions.** When a template is redesigned, do not add a provider per year. In the same class:

1. Declare `format_versions = ["v2019_classic", "v2025_dual_ccy"]`.
2. Implement `detect_version(text, pdf_path)` returning the tag or `None`; `detect()` is `detect_version(...) is not None`; `extract()` calls it and returns `"format_version"`.
3. Keep `sample_variants`: a synthetic, PII-free sample for every version. `run_self_test()` checks that `detect_version()` returns the key, `extract()` finds accounts and dates, and `reconciled` is true (opening + credits - debits = closing). Adding a new layout can then never break older ones.

## 5. Hazel

- Hazel rules on the PDF inbox run `process_pdf.sh` as a "Run shell script" action (not a "Passes shell script" condition) through a forwarder that delegates to this repo's copy, the only one.
- It skips files that are not ready (missing, not a PDF, hidden or placeholder, empty, still being written, under `Duplicates/` or `Archive/`), takes a lock to serialise concurrent runs, and offers each file to every `<section>/pdf_handler.sh` until one claims it. The finance handler runs `sync_statements.py --provider auto --import --delete-duplicates --hazel "$1"`.
- Handler exit codes: `0` handled, `1` not mine, `2+` failure. The dispatcher exits `0` when every file was handled or unclaimed, and `2+` on a real failure; `PROCESS_PDF_STRICT=1` makes it exit `1` for an unclaimed file.
- Each run logs one line to stdout and `~/Library/Logs/Hazel/process_pdf.log`: `MATCH` (imported, with provider, file, destination, date, reconciled), `SKIP` (no matching provider) or `DRIFT` (the domain matched a known provider but the content format changed).
- To review activity, find drift or scaffold a provider, use `finance/audit_hazel_logs.py` (see its `-h`): `--scan-unmatched <inbox>` scans a folder, `--sample <name>` inspects one PDF, `--scaffold <provider>` generates a provider class, and `--version <tag>` sets the format version tag.

## 6. Before you finish

1. Syntax: `find . -name '*.py' -not -path './.direnv/*' -print0 | xargs -0 python3 -m py_compile`
2. Execute bits: `./fix_exec_bits.sh --check`
3. Provider self-tests: `cd finance && PDF_AUDIT_CONFIG=config.example.json python3 -c "from statements import config; from statements.providers import AVAILABLE_PROVIDERS; cfg = config.load(); assert all(p.run_self_test(cfg)['ok'] for p in AVAILABLE_PROVIDERS if p.name != 'generic')"`
4. Zero PII: `git status` shows no real PDFs, databases, CSVs, `config.json` or `AGENTS.local.md` staged, and the changed files hold no usernames, account numbers, BSBs or balances.
5. Keep `AGENTS.md`, `README.md` and the section READMEs in step with any new flag or structure change, and record finished items in `AGENTS.local.md`.
