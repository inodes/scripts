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
├── process_pdf.sh                 # Generic Hazel entry script (with fallback rule support)
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

**Report location:** the generated `index.html` is written to `directories.reports` in `config.json`
(or an explicit `report_path`), falling back to `directories.statements`. Keep it inside iCloud.

**Database location:** the SQLite index is kept in a *local* folder (`data_dir`, default
`~/Library/Application Support/pdf-audit`), never in iCloud, because sync can corrupt a live SQLite/WAL file.
`store.connect` refuses an iCloud path unless `PDF_AUDIT_ALLOW_ICLOUD_DB=1`. Reports (HTML), PDFs and
`config.json` live in iCloud; the database can be rebuilt at any time from the PDFs.
