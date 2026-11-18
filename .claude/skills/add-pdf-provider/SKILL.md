---
name: add-pdf-provider
description: Use when adding or changing a statement, invoice, receipt or payslip provider under finance/statements/providers, or when a provider reports format drift (new PDF layout).
---

# Add or change a PDF provider

**When to trigger:** a new institution or document type needs handling, or a known provider's PDF layout changed.

Rules live in `AGENTS.md` section 3 (provider architecture, multi-version formats). Do not restate them here; follow them.

## Steps
1. Plan first and get approval (global rule). Read `AGENTS.md` sections 3.1 and 3.2.
2. Same provider, new layout: add a tag to `format_versions` and a matching entry in `sample_variants`. Never create a second provider per year.
3. New provider: one file in `finance/statements/providers/<name>.py` holding all metadata (`name`, `document_kind`, `target_dir`, `scan_dirs`, `source_domains`, `filename_template`, cadence fields).
4. `sample_variants` text must be synthetic. No real account numbers, names, balances or addresses, in code, tests or commits.
5. Real values (paths, account identifiers) go in the external config under `~/iCloud/Finance/`, never in the repo.
6. Verify (run these and show output):
   - `cd finance && PDF_AUDIT_CONFIG=config.example.json python3 -c "from statements import config; from statements.providers import AVAILABLE_PROVIDERS; cfg = config.load(); assert all(p.run_self_test(cfg)['ok'] for p in AVAILABLE_PROVIDERS if p.name != 'generic')"`
   - `./fix_exec_bits.sh --check` from the repo root.
   - `python3 finance/pii_guard.py --all` and confirm "clean".
7. Never move or delete real PDFs without approval; demonstrate `--rename`, `--import` and `--delete-duplicates` with `--dry-run`.
8. Update `AGENTS.md` (generic) and `AGENTS.local.md` (specifics) if behaviour changed.
