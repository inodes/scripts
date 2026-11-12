# AGENTS.md — AI Agent Operating Instructions & Architecture Reference

> **Notice to AI Agents & Developers**:
> This document is the authoritative operating instructions, architecture reference, and developer guide for `~/repos/scripts` — generic, shareable, and safe to commit (strictly zero PII).
> **Every agent working in this repository MUST read this file upon startup, verify its accuracy against the current codebase, and keep it up-to-date after completing work.**
>
> The detailed local project journal — Active Requests, Known Bugs, TODOs, and real statement/account audit specifics — lives in `AGENTS.local.md` (gitignored, never committed). **Every agent MUST also read `AGENTS.local.md` on startup** for the actual current state of the project, and update it (not this file) with local specifics as work progresses.

---

## 1. Core Operating Directives

0. **Implementation Plans First — Explicit Permission Required Before Action**:
   - **CRITICAL DIRECTIVE**: The agent MUST ALWAYS research, formulate, and present a clear plan before taking any modifying actions on user data or major architectural changes.
   - **NEVER MOVE, RENAME, OR DELETE USER PDFS WITHOUT APPROVAL**: Always default to `--dry-run` when demonstrating `--rename`, `--import`, or `--delete-duplicates` unless the user explicitly instructs you to execute the live operation.

1. **Git Workflow, Commits & Zero-PII Directives**:
   - **Check with User Prior to Committing**: Prior to executing any `git commit`, the agent MUST check with the user, describing both:
     1. The changes made verbally in clear detail.
     2. The exact proposed commit message(s).
   - **Zero-PII Commitment Rule**: NO Personally Identifiable Information (PII) is ever committed (including in documentation, unit tests, commit messages, or `AGENTS.md` itself):
     - Never include local usernames in committed documentation or scripts; always use `$USER`, `$HOME`, or `~`.
     - PII includes: Real account numbers, BSBs, card numbers, balances, merchant transactions, statement PDFs (`*.pdf`), CSV exports (`*.csv`), or SQLite databases (`*.db`).
     - Non-PII includes: Sanitized `.example.json` templates (`config.example.json`) and synthetic unit-test fixtures (`sample_text` / `sample_variants` on providers).
   - **PII Never Lives in the Repo Working Tree At All**:
     - The SQLite database (`pdf_audit.db`) and cached PDF text live outside the repo in `~/Library/Application Support/pdf-audit/` (configured via `data_dir` in `finance/statements/config.py`), never inside `~/repos/scripts/`.
     - Real personal configurations, private scan paths, and custom categorisation rules live in `~/Library/Mobile Documents/com~apple~CloudDocs/Finance/config.json` and `category_rules.json` (matching the `~/repos/tesla` architecture). Only sanitized `.example.json` templates exist in the repository.

2. **Per-Directory `direnv` + `.envrc` & Isolated `requirements.txt` (Mandatory)**:
   - **At all times**, `direnv` MUST be active with an appropriate `.envrc` (`layout python` + `.python-version`) in the repository directory, using a strictly confined local `requirements.txt` rather than global/system Python packages (matching the standard in `~/repos/tesla/.envrc`).
   - `.envrc` automatically watches `requirements.txt`, hashes it to `$VIRTUAL_ENV/.requirements.sha256`, and syncs dependencies into the isolated `.direnv/python-3.11` virtualenv whenever `requirements.txt` changes.
   - Any external script (such as `process_pdf.sh` invoked by Hazel) or subshell must resolve `$SCRIPT_REPO/.direnv/python-3.11/bin/python3` first so it always executes inside the repository's confined virtualenv rather than system `/usr/bin/python3`.
   - On a fresh clone or after editing `.envrc`, run `direnv allow .` to ensure the virtualenv is active and synced.

3. **Script Permissions (`u+x`)**:
   - Every `*.py` and `*.sh` file in the repository — CLI entry points, `finance/statements/` library modules, provider modules, and tests — must have the user-execute bit (`chmod u+x`).
   - Run `./fix_exec_bits.sh` (or `./fix_exec_bits.sh --check`) to verify and repair execute bits, and `./fix_exec_bits.sh --install-hook` on a fresh clone to install the `.git/hooks/pre-commit` hook.

4. **Provider-Agnostic CLI Help (`-h`)**:
   - `-h` / `--help` on `pdf_audit.py`, `sync_statements.py`, and `audit_hazel_logs.py` must remain completely **provider-agnostic** (no hardcoded institution names, account types, or provider-specific examples in help strings).
   - All provider-specific details (names, document kinds, source domains, filename templates, and target directories) are queried dynamically via `finance/pdf_audit.py providers` (or `--providers`) and `finance/sync_statements.py --providers`.

5. **Backups, Duplicate Handling & Non-Destructive Operations**:
   - **Never delete repo scripts.** To retire a script, move it to `archive/` and log why in `archive/archive_log.md`. Backups of untracked files (`config.json`) belong in `.bak_archive/`.
   - **Non-Clobbering File Moves & True Duplicate Detection**:
     - `is_true_binary_duplicate()` in `finance/statements/ingest.py` uses `normalized_pdf_sha256()` (`finance/statements/pdf.py`) to compare raw PDF bytes after stripping dynamic download-time wrapper metadata (`/CreationDate(D:...)`, `/ModDate(D:...)`, and trailer `/ID[<...><...>]`) plus extracted text equality (`same_pdf_content()`).
     - When a destination file already exists during `--rename` or `--import`:
       - If `--delete-duplicates` is passed AND `is_true_binary_duplicate()` is `True`, the redundant source PDF is deleted (`deleted_true_duplicate`).
       - Otherwise (or when the duplicate is not byte/stream-identical), the source PDF is moved into `<target_dir>/Duplicates/` (`provider.resolve_duplicates_dir()`) with the standardized provider filename (appending `_01`, `_02` via `non_clobbering_path()` if needed) so no file is ever overwritten.

6. **PDF Date Retouching & Companion `.txt` Generation (`mtime`, `atime`, and macOS Creation Date)**:
   - Every PDF renamed, imported, or audited by `sync_statements.py` automatically generates and synchronizes a companion `<filename>.txt` file containing the extracted text (`ensure_companion_txt()` in `finance/statements/ingest.py`).
   - When PDFs are moved or renamed, their companion `.txt` files are moved alongside them.
   - Both the `.pdf` and the companion `.txt` have their filesystem timestamps retouched via `set_mtime_from_metadata()` (`finance/statements/ingest.py`) to **`00:00:00` local midnight** of the document's `period_end` (or `issue_date` fallback):
     - Sets POSIX `atime` and `mtime` via `os.utime()`.
     - On macOS, also invokes `/usr/bin/SetFile -d "MM/DD/YYYY 00:00:00" -m "MM/DD/YYYY 00:00:00"` so Finder's **Date Created (`Birth`)** and **Date Modified** both match the exact statement date at `00:00:00`.

7. **"Verified" Means Tested Against Real Data & All Format Versions**:
   - Never claim a provider or parser is "verified" or "reconciled" unless you have run `run_self_test()` across all `sample_variants` AND tested `pdf_audit.py sync` / `pdf_audit.py check` against real PDFs with `0 differed`.

---

## 2. Repository Architecture & Layout

```text
scripts/
├── AGENTS.md                  # AI Agent operating instructions & architecture reference (committed, PII-free)
├── AGENTS.local.md            # Project journal: Active Requests, Bugs, TODOs, gap audits (gitignored)
├── CLAUDE.md                  # Concise session quick-start pointer for Claude/AI agents (committed, PII-free)
├── README.md                  # User-facing overview, quick start, Hazel workflow, and CLI reference
├── LICENSE                    # MIT License
├── CONTRIBUTING.md            # Contribution guidelines
├── SECURITY.md                # Security policy
├── .envrc                     # direnv Python virtualenv & PATH setup
├── .python-version            # Python version pin (3.11.13)
├── .gitignore                 # Git ignore rules (excludes PII, PDFs, DBs, config.json, AGENTS.local.md)
├── requirements.txt           # Python dependencies (pypdf>=4.0)
├── fix_exec_bits.sh           # Ensures all *.py and *.sh files have u+x and installs pre-commit hook
├── process_pdf.sh             # Generic PDF dispatcher run by Hazel: offers each PDF to every <section>/pdf_handler.sh
└── finance/                   # 📂 Finance & Statement Audit Toolkit (one section; others can be added alongside)
    ├── config.example.json        # Sanitized provider & directory configuration template (committed)
    ├── category_rules.example.json# Sanitized category rules template (committed)
    ├── pdf_handler.sh             # Finance handler for ../process_pdf.sh (--provider auto --import --delete-duplicates --hazel)
    ├── sync_statements.py         # Provider discovery, inspection, --rename, --import, --delete-duplicates, --where-from, --hazel
    ├── pdf_audit.py               # Full SQLite ledger CLI: providers, sync, scan, inspect, statements, statement, accounts, transactions, check, summary, report
    ├── fetch_email_invoices.py    # Automated IMAP invoice & statement email attachment downloader
    ├── audit_hazel_logs.py        # Audits ~/Library/Logs/Hazel/process_pdf.log + Hazel.log; inspects & scaffolds multi-version providers
    ├── tests/                     # Provider unit tests
    └── statements/                # Core library & pluggable provider package
        ├── __init__.py
        ├── config.py              # Dynamically builds defaults from AVAILABLE_PROVIDERS + merges iCloud/local config.json
        ├── money.py               # Exact Decimal arithmetic, DR/CR sign parsing, and currency formatting
        ├── pdf.py                 # PDFTextExtractor, fast ctypes macOS kMDItemWhereFroms reader, and normalized_pdf_sha256()
        ├── store.py               # SQLite schema (files, pdftext, statements, account_sections, transactions) & deduplication
        ├── ingest.py              # Directory walking, is_true_binary_duplicate, set_mtime_from_metadata (SetFile), and SQLite ingestion
        ├── report.py              # Self-contained interactive dashboard (index.html), loan timeline & capital equity models
        └── providers/             # Pluggable PDF document providers (statements, invoices, receipts, payslips)
            ├── __init__.py        # AVAILABLE_PROVIDERS registry, detect_provider(), detect_and_extract()
            ├── base.py            # BasePDFProvider interface, multi-version detect_version/sample_variants, category classifier
            ├── hsbc.py            # HSBC Australia providers (credit card, premier, simple)
            ├── commbank.py        # Commonwealth Bank Australia provider
            ├── ing.py             # ING Australia provider
            ├── workday.py         # Workday / payroll payslip provider (v2019_payroll, v2025_workday)
            ├── aapt.py            # AAPT payslip provider (v2012_aapt)
            ├── vendors.py         # VendorProvider base + Telstra, Vodafone, Aussie Broadband, Bunnings, Google Play (ISO-dated names)
            ├── apple.py           # Apple receipts, credit notes, JP receipts, App Store emails
            ├── agl.py             # AGL bills filed to {invoices}/AGL as AGL_Statement_<svc>_<issue date>.pdf (single ISO date, never a range)
            └── agl_parse.py       # AGL parsing/rates/naming, shared: ~/repos/tesla imports it via its config scripts_repo
```

---

## 3. Provider Architecture & Multi-Version Format Evolution

### 3.1 The Provider Holds All Information
Every provider subclass in `finance/statements/providers/<provider>.py` is the single source of truth for its own metadata, rules, and defaults:
- `name`: Unique snake_case identifier (e.g. `hsbc_premier`).
- `display_name`: Human-readable title.
- `doc_type`: Document type identifier stored in SQLite.
- `document_kind`: One of `"statement"`, `"invoice"`, `"receipt"`, `"payslip"`, or `"document"`.
- `enabled`: Boolean (`True` by default).
- `target_dir`: Canonical folder where `--import` moves standardized PDFs.
- `duplicates_dir`: Folder where non-identical duplicate PDFs are moved (defaults to `<target_dir>/Duplicates`).
- `scan_dirs`: Default inbox folders scanned when `--provider <name>` is passed without explicit paths.
- `source_domains`: List of domain substrings matched against macOS `com.apple.metadata:kMDItemWhereFroms` (read via fast `ctypes` `libc.getxattr` in `finance/statements/pdf.py`). Used for:
  1. Fast pre-filtering when `--where-from` is passed.
  2. Automatic **format-drift detection** (`[process_pdf] DRIFT`) when a downloaded file's domain matches a known provider, but its PDF text fails `detect()`.
- `filename_template`: Template string rendered by `BasePDFProvider.render_filename_template()` using extracted tokens:
  - `{provider}`, `{doc_type}`, `{customer_no}`, `{account}`, `{account_no}`, `{last4}`
  - `{date}`, `{date_compact}`, `{period_start}`, `{period_end}`, `{issue_date}`
  - `{period_start_compact}`, `{period_end_compact}`, `{issue_date_compact}`
- `cadence`, `expected_period_days`, `max_gap_days`: Used by `provider.check_missing_periods()` (`--check-missing` / `--missing-only`) to detect missing statement periods and balance shifts across gaps.

### 3.2 Multi-Version Format Evolution (Handling 2019/2021 vs. 2026 PDFs)
Banks, utilities, and merchants periodically tweak or redesign their PDF templates over time. **Never create separate providers for different years of the same document** (e.g., do not create `hsbc_premier_2019` and `hsbc_premier_2026`). Instead, version the format inside the single provider class so all historical and modern PDFs share the same `target_dir`, `filename_template`, and SQLite continuity checks:

1. **Declare `format_versions`**:
   ```python
   format_versions = ["v2019_classic", "v2025_dual_ccy"]
   ```
2. **Implement `detect_version(self, text: str, pdf_path: str) -> Optional[str]`**:
   - Inspect `text` (and/or `pdf_path`) and return the matching version tag (e.g. `"v2019_classic"` or `"v2025_dual_ccy"`), or `None` if the PDF does not belong to this provider.
   - `detect(self, text, pdf_path)` should simply return `self.detect_version(text, pdf_path) is not None`.
   - Inside `extract(self, text, pdf_path)`, call `ver = self.detect_version(text, pdf_path)` whenever version-specific regexes or section parsers are needed, and include `"format_version": ver` in the returned dictionary.
3. **Maintain `sample_variants` for Automated Regression Testing**:
   - Populate `sample_variants: Dict[str, str]` with a synthetic, PII-free sample text for **every** version in `format_versions`:
     ```python
     sample_variants = {
         "v2019_classic": """...synthetic 2019 layout...""",
         "v2025_dual_ccy": """...synthetic 2025/2026 layout...""",
     }
     ```
   - `BasePDFProvider.run_self_test()` automatically loops through **every key/text in `sample_variants`** and verifies that:
     1. `detect_version()` returns the expected version key,
     2. `extract()` succeeds and extracts accounts/dates, and
     3. `reconciled` is `True` (`opening + credits - debits == closing`).
   - This guarantees that adding a `v2026` or `v2027` layout never breaks parsing for older `v2019` / `v2021` PDFs.

---

## 3a. File Locations (mandatory)

- **Never hard-code an iCloud (or any user) path.** Use `statements/paths.py`: `paths.get("pdf")`, `paths.statements("HSBC")`, `paths.payslips(...)`, `paths.under("reports", "index.html")`. All of them derive from `config.json` -> `directories`.
- **Call `paths.check_destination(dest)` before any move or write.** It refuses loose files in container folders (iCloud root, Finance, Statements, Payslips, Shares), anything inside the repo, and databases in iCloud.
- The SQLite index is local (`directories.data`); generated HTML goes to `directories.reports`; neither goes in a statements folder.
- Each tool exists once. `process_pdf.sh` (repo root) is generic and section-agnostic: it must never contain finance- or provider-specific logic. Each section plugs in via `<section>/pdf_handler.sh` (exit `0` handled, `1` not mine, `2+` failure).
- A provider match is not proof. Only rename/move a document that also yields a date and reconciles (`hold_reason()`); otherwise log `HOLD` and leave it in place.

## 4. Hazel Integration, Logging & Scaffolding New Providers

### 4.1 How Hazel Calls `process_pdf.sh`
- Hazel rules (e.g. **"Provider PDF Processor"** on `~/iCloud/PDF`) run `process_pdf.sh` as a **"Run shell script" action** (not a "Passes shell script" condition), via the forwarder at `~/iCloud/hazel/scripts/process_pdf.sh`, which delegates to `~/repos/scripts/process_pdf.sh` (the only copy).
- `process_pdf.sh` skips files that are not ready (missing, not a PDF, hidden/iCloud placeholder, empty, still being written, inside `Duplicates/`/`Archive/`), takes a lock so concurrent Hazel runs are serialised, then offers each file to every `<section>/pdf_handler.sh` until one claims it. The finance handler runs:
  ```bash
  sync_statements.py --provider auto --import --delete-duplicates --hazel "$1"
  ```
- **Exit codes**: handlers return `0` handled, `1` not mine, `2+` failure. The dispatcher exits `0` when every file was handled or unclaimed (Hazel treats non-zero from a "Run shell script" action as an error), and `2+` on a real failure. `PROCESS_PDF_STRICT=1` makes it exit `1` when a file is unclaimed, for use as a "Passes shell script" condition.
- **Hold gate**: a recognised document that has no extracted date or fails reconciliation is logged as `HOLD` and left in place.
- **Structured Log Output (`~/Library/Logs/Hazel/process_pdf.log` & `stdout`)**:
  Every invocation writes a single-line structured log record to both `stdout` and `~/Library/Logs/Hazel/process_pdf.log`:
  - `MATCH`: `[process_pdf] MATCH status=imported provider=hsbc_premier file="statement-3.pdf" -> "HSBC_Premier_000000_20260617.pdf" dest="~/iCloud/Finance/Statements/HSBC/..." date=2026-06-17 reconciled=True`
  - `SKIP`: `[process_pdf] SKIP status=unmatched file="JBHiFi-Receipt.pdf" path="..." source_domain="none" reason=no_matching_provider`
  - `DRIFT`: `[process_pdf] DRIFT status=unmatched_format_drift file="statement.pdf" path="..." source_domain="hsbc.com.au" expected_provider="hsbc_premier" reason=domain_matched_but_content_format_changed`

### 4.2 Auditing Unmatched PDFs & Scaffolding Providers (`audit_hazel_logs.py`)
Use `finance/audit_hazel_logs.py` to review Hazel processing activity, spot format drift on existing providers, and scaffold new providers or new format versions:

```bash
# 1. View summary of matched PDFs, unmatched PDFs grouped by domain/pattern, and Hazel errors
finance/audit_hazel_logs.py

# 2. Also scan an inbox folder directly for any PDFs not yet logged by Hazel
finance/audit_hazel_logs.py --scan-unmatched ~/iCloud/PDF

# 3. Inspect a specific unmatched PDF (extracts WhereFroms domain, dates, amounts, and first 25 lines)
finance/audit_hazel_logs.py --sample "JBHiFi-Receipt"

# 4. Generate a ready-to-paste multi-version BasePDFProvider class (auto-infers version e.g. v2021)
finance/audit_hazel_logs.py --sample "JBHiFi-Receipt" --scaffold jbhifi

# 5. Or specify an explicit format version tag when adding a new era to an existing/new provider
finance/audit_hazel_logs.py --sample "statement.pdf" --scaffold hsbc_premier --version v2027_redesign
```

---

## 5. Verification & Update Checklist for Agents

Before completing any task in `~/repos/scripts`:
1. **Syntax Check**:
   ```bash
   find . -name '*.py' -not -path './.direnv/*' -print0 | xargs -0 python3 -m py_compile
   ```
2. **Execute Bits Check**:
   ```bash
   ./fix_exec_bits.sh --check
   ```
3. **Multi-Version Provider Self-Tests**:
   ```bash
   PDF_AUDIT_CONFIG=config.example.json python3 -c "from statements import config; from statements.providers import AVAILABLE_PROVIDERS; cfg = config.load(); assert all(p.run_self_test(cfg)['ok'] for p in AVAILABLE_PROVIDERS if p.name != 'generic')"
   ```
4. **Zero-PII Check**:
   - Confirm `git status` shows no real PDFs, DBs, CSVs, `config.json`, or `AGENTS.local.md` staged.
   - Grep modified files to ensure no personal usernames, account numbers, BSBs, or balances are hardcoded.
5. **Documentation & Journal Update**:
   - Keep `AGENTS.md`, `README.md`, and `CLAUDE.md` synchronized with any new CLI flags or architectural changes.
   - Record completed requests, new bugs, or statement gap findings in `AGENTS.local.md`.
