# Finance & Statement Audit Suite (`~/repos/scripts/finance`)

A pluggable, multi-provider financial statement & invoice auditing, parsing, duplicate elimination, date-retouching, and SQLite ledger engine.

---

## Architecture & Component Overview

```
finance/
├── statements/                    # Core Python package
│   ├── __init__.py                # Package exports
│   ├── config.py                  # Dynamic provider defaults + iCloud config loader
│   ├── money.py                   # High-precision Decimal arithmetic & sign parsing
│   ├── pdf.py                     # Fast pypdf extraction + macOS kMDItemWhereFroms reader
│   ├── store.py                   # SQLite database schema, statements & transaction store
│   ├── ingest.py                  # Directory walking, true binary duplicate checks & date retouching
│   ├── report.py                  # Interactive self-contained HTML report (index.html) generator
│   └── providers/                 # Pluggable PDF document provider classes
│       ├── __init__.py            # AVAILABLE_PROVIDERS registry & dispatcher
│       ├── base.py                # BasePDFProvider interface & transaction classifier
│       ├── hsbc.py                # HSBC Australia providers (Credit Card, Premier, Simple)
│       ├── commbank.py            # Commonwealth Bank Australia (Smart Access & Credit Cards)
│       ├── ing.py                 # ING Australia (Savings Maximiser & Orange Everyday)
│       ├── workday.py             # Workday / payroll payslips (v2019_payroll, v2025_workday)
│       └── aapt.py                # AAPT payslips (v2012_aapt)
├── pdf_audit.py                   # Full SQLite ledger & interactive report CLI
├── sync_statements.py             # Statement rename, import & duplicate elimination CLI
├── fetch_email_invoices.py        # Automated IMAP invoice & statement email downloader
├── audit_hazel_logs.py            # Hazel processing log auditor & provider scaffolder
├── pdf_handler.sh                 # Finance handler called by the generic ../process_pdf.sh
├── config.example.json            # Sanitized provider & directory template
├── category_rules.example.json    # Sanitized category rules template
└── tests/                         # Synthetic, zero-PII unit tests
```

---

## Zero-PII iCloud Configuration (Mirroring `~/repos/tesla`)

To keep `~/repos/scripts` strictly clean and shareable without leaking Personally Identifiable Information (PII) or local paths:

- **Active Configuration**: Loaded dynamically from `~/Library/Mobile Documents/com~apple~CloudDocs/Finance/config.json`.
- **Custom Category Overrides**: Loaded from `~/Library/Mobile Documents/com~apple~CloudDocs/Finance/category_rules.json` (or the `"category_rules"` block inside `config.json`).
- **Database**: SQLite ledger is persisted at `~/Library/Application Support/pdf-audit/pdf_audit.db`.
- **Git Safety**: Real PDFs, CSVs, databases, `config.json`, and `category_rules.json` are git-ignored.

---

## Command Reference

### 1. `pdf_audit.py` — Ledger & Interactive Dashboard
```bash
# List available providers and configurations
./pdf_audit.py providers

# Sync statements from scan_dirs into SQLite
./pdf_audit.py sync

# Reconcile accounts and detect timeline gaps
./pdf_audit.py check
./pdf_audit.py check --missing-only

# Generate self-contained interactive dashboard (index.html)
./pdf_audit.py report

# Query accounts, spend summaries, or transactions
./pdf_audit.py accounts
./pdf_audit.py summary --by category --from 2025-11-01 --exclude-transfers
./pdf_audit.py transactions --category Groceries --limit 25

# Fetch invoices and statements from email
./pdf_audit.py fetch-email --target apple --since-days 60 --sync
```

### 2. `sync_statements.py` — Statement Renaming & Filing
```bash
# Preview standardized names for matching PDFs (dry run)
./sync_statements.py ~/iCloud/PDF --provider hsbc --where-from --import --dry-run

# Execute import into provider folder and delete true binary duplicates
./sync_statements.py ~/iCloud/PDF --provider hsbc --where-from --import --delete-duplicates
```

### 3. `fetch_email_invoices.py` — Automated Attachment Fetching
```bash
# Download Apple receipts from the last 90 days to ~/iCloud/PDF
./fetch_email_invoices.py --target apple --since-days 90 --sync

# Download Qantas travel itineraries & e-tickets
./fetch_email_invoices.py --target qantas --since-days 180 --sync
```

### 4. `audit_hazel_logs.py` — Hazel Log Inspection & Scaffolding
```bash
# Inspect Hazel logs for unmatched PDFs and format drift
./audit_hazel_logs.py

# Scaffold a new provider from a sample PDF
./audit_hazel_logs.py --sample "Sample.pdf" --scaffold my_provider
```

### Private data lives in `config.json` (outside the repo)

Account aliases (`accounts`), and the `personal` section (property profile and valuation anchors,
renovation ledger, ESS history, remittances, holdings defaults, vehicle purchase refs, renovation window),
plus user-specific `category_rules` and provider scan directories, are read from
`~/Library/Mobile Documents/com~apple~CloudDocs/Finance/config.json`. Nothing in the repository contains
real identifiers; fixtures use fabricated values. See `config.example.json` for the shape.

## File locations (one rule: nothing hard-codes an iCloud path)

Every location is defined once, in `statements/paths.py`, and comes from the `directories` section of
`config.json` (values can reference each other, e.g. `"reports": "{finance}/Reports"`):

| Key | Default | Holds |
|---|---|---|
| `icloud` | iCloud Drive root | |
| `finance` | `{icloud}/Finance` | `config.json`, `category_rules.json` only |
| `statements` | `{finance}/Statements` | one sub-folder per provider, each with its own `Duplicates/` |
| `payslips` | `{finance}/Payslips` | one sub-folder per employer |
| `shares` | `{finance}/Shares` | equity exports |
| `reports` | `{finance}/Reports` | generated `index.html` (or set `report_path`) |
| `pdf` | `{icloud}/PDF` | Hazel staging folder |
| `landing` | `~/Downloads` | |
| `data` | `~/Library/Application Support/pdf-audit` | SQLite index: **local only** |
| `logs` | `~/Library/Logs/Hazel` | `process_pdf.log` |

`paths.check_destination()` is called before every move or write and refuses to:
put a document or report loose in a container folder (iCloud root, Finance, Statements, Payslips, Shares);
write anything inside the git repository; or keep the database in iCloud (sync can corrupt SQLite).
The report is written atomically (temp file, then rename). The database can always be rebuilt from the PDFs.

## `pdf_handler.sh` (Finance's hook into the generic dispatcher)

`../process_pdf.sh` at the repo root is the generic, section-agnostic script Hazel runs. It offers each
PDF to every `<section>/pdf_handler.sh`; `finance/pdf_handler.sh` is this section's handler and simply runs
`sync_statements.py --provider auto --import --delete-duplicates --hazel` (exit `0` handled, `1` not a
finance document, `2+` failure).

A recognised document is only renamed or moved when it also yields a date **and** reconciles; otherwise it is
logged as `HOLD` and left in place (`--allow-unreconciled` / `PDF_AUDIT_ALLOW_UNRECONCILED=1` for manual runs).
