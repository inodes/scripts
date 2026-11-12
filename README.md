# Personal Automation & Tooling Suite (`~/repos/scripts`)

A modular repository for local automations and tools. All financial statements, invoices, and accounting tools are organized in the [`finance/`](finance/) subdirectory, with a single copy of each tool (no root-level duplicates). With direnv, `finance/` is on `PATH` inside the repo.

> **For AI Agents & Contributors**: Read [`AGENTS.md`](AGENTS.md) first for the complete architecture reference, multi-version provider design rules, zero-PII requirements, and verification checklist.

---

## Finance Suite (`finance/`)

1. **[`finance/pdf_audit.py`](finance/pdf_audit.py) (or `finance/pdf_audit.py`) — Full SQLite Statement & Transaction Ledger CLI**:
   - Ingests statements, account sections, and transactions into SQLite (`~/Library/Application Support/pdf-audit/pdf_audit.db`).
   - Supports `providers`, `sync`, `scan`, `inspect`, `statements`, `statement`, `accounts`, `transactions`, `check` (timeline gap detection), `summary`, `fetch-email`, and `report` (interactive HTML dashboard).

2. **[`finance/sync_statements.py`](finance/sync_statements.py) (or `finance/sync_statements.py`) — Discovery, Date-Retouching, Rename & Filing**:
   - Discovers statements, detects the matching provider and format version, verifies cent-level balance reconciliation, and moves/renames documents into provider target folders.
   - Retouches `mtime`, `atime`, and Finder creation date (`SetFile -d`) to `00:00:00` on the statement's ending date.
   - Content-aware duplicate deletion (`--delete-duplicates`) via `normalized_pdf_sha256()`.

3. **[`process_pdf.sh`](process_pdf.sh) — Generic PDF dispatcher (run by Hazel)**:
   - Not finance-specific. Offers each ready PDF to every `<section>/pdf_handler.sh` until one claims it (`0` handled, `1` not mine, `2+` failure). Finance plugs in via [`finance/pdf_handler.sh`](finance/pdf_handler.sh); add another section by adding its own handler.
   - Skips files that are not ready, serialises concurrent runs, logs to `~/Library/Logs/Hazel/process_pdf.log` (rotated at 5 MB). Exits `0` for handled or unclaimed files; only real failures (`>= 2`) are propagated.

4. **[`finance/fetch_email_invoices.py`](finance/fetch_email_invoices.py) — Email Invoice & Statement Extraction**:
   - Securely searches IMAP (Gmail / Google Workspace) using provider query presets (Apple, Qantas, Tesla, CommBank, HSBC, ING, etc.), extracts PDF attachments, and stages them for Hazel/auditing.

5. **[`finance/audit_hazel_logs.py`](finance/audit_hazel_logs.py) — Hazel Log Auditor & Format Drift Scaffolder**:
   - Inspects logs, groups unmatched PDFs, flags format drift (`DRIFT`), and scaffolds multi-version provider templates.

---

## Storage Audit (`storage/`)

**[`storage/storage_audit.py`](storage/storage_audit.py) — Read-only audit of files scattered across drives**:
   - Catalogues the Mac, external drives and QNAP shares into a local SQLite database, finds duplicates (size, partial hash, optional SHA-256), compares Lightroom catalogues with photos on disk, and writes an HTML report. Never moves or deletes anything. Writes a self-contained `index.html` results page with folder drill-down. [`storage/storage_audit_ui.py`](storage/storage_audit_ui.py) is the visual version: a local-only web app (127.0.0.1) to browse, drill down and run the scans from buttons. See [`storage/README.md`](storage/README.md).

---

## Quick Start

```bash
# 1. List all registered providers, document kinds, source domains, templates, and target folders
finance/pdf_audit.py providers
finance/sync_statements.py --providers

# 2. Preview standardized names and import actions for a folder (dry-run)
finance/sync_statements.py ~/iCloud/PDF --provider hsbc --where-from --import --dry-run

# 3. Import matched PDFs into their provider target_dir and delete true binary duplicates
finance/sync_statements.py ~/iCloud/PDF --provider hsbc --where-from --import --delete-duplicates

# 4. Audit Hazel logs for unmatched PDFs or format drift, and scaffold a new provider
finance/audit_hazel_logs.py
finance/audit_hazel_logs.py --sample "Receipt-Sample.pdf" --scaffold my_provider

# 5. Sync statements into SQLite and check arithmetic + timeline continuity (missing statement gaps)
finance/pdf_audit.py sync
finance/pdf_audit.py check

# 6. View accounts summary, category spend summary, or search/export transactions
finance/pdf_audit.py accounts
finance/pdf_audit.py summary --by category --from 2025-11-01 --exclude-transfers
finance/pdf_audit.py transactions --category Groceries --limit 25
```

---

## Multi-Version Providers (Handling Format Evolution Across Years)

Issuers frequently tweak or redesign their PDF templates over time (for example, a 2019/2021 statement layout vs. a 2026 layout). Rather than creating separate providers per year, a single provider class in [`statements/providers/`](statements/providers) handles all historical and current formats:

- **`format_versions`**: Lists all supported layout versions (e.g. `["v2019_classic", "v2025_dual_ccy"]`).
- **`detect_version(text, pdf_path)`**: Returns which version tag a PDF matches (or `None`), allowing `extract()` to apply version-specific regexes while normalising into the same filename template and target directory.
- **`sample_variants`**: Stores synthetic, PII-free text fixtures for **every** version in `format_versions`. Calling `provider.run_self_test()` automatically tests every historical version so adding a new 2026/2027 layout never breaks older 2019/2021 PDFs.
- **Format Drift Detection (`DRIFT`)**: When a PDF's download domain (`kMDItemWhereFroms`) matches a provider's `source_domains`, but `detect()` fails because the issuer redesigned their PDF, `process_pdf.sh` logs a `DRIFT` entry to `~/Library/Logs/Hazel/process_pdf.log` so `finance/audit_hazel_logs.py` immediately highlights which existing provider needs a new format version.

---

## Development, `direnv` Environment & Verification

This repository uses [`direnv`](.envrc) (`layout python` + [`.python-version`](.python-version)) to maintain a strictly confined, per-directory Python virtualenv (`.direnv/python-3.11`) synced automatically from [`requirements.txt`](requirements.txt) rather than global packages.

See [`AGENTS.md`](AGENTS.md) for complete developer documentation.

```bash
# 0. Activate & sync the isolated direnv Python virtualenv (first time or after .envrc edits)
direnv allow .

# 1. Fix & verify u+x execute permissions on all scripts and install git pre-commit hook
./fix_exec_bits.sh
./fix_exec_bits.sh --install-hook

# 2. Run multi-version self-tests across all registered providers
PDF_AUDIT_CONFIG=config.example.json python3 -c "from statements import config; from statements.providers import AVAILABLE_PROVIDERS; cfg = config.load(); assert all(p.run_self_test(cfg)['ok'] for p in AVAILABLE_PROVIDERS if p.name != 'generic')"

# 3. Check missing statement periods using the provider's cadence & balance continuity rules
finance/pdf_audit.py check --missing-only
```
