# PDF handling

Every PDF follows one path. `process_pdf.sh` (generic; Hazel runs it) is the only thing that moves files.

```
PDF/unsorted/            all PDFs land here first
   |
   |- a section handler claims it (finance/pdf_handler.sh, ...)  -> its home, e.g. Finance/Payslips/<Employer>,
   |                                                                Finance/Invoices_Receipts/Apple
   |- handler recognised it but a check failed (exit 10)           -> PDF/needs_review/<reason>/
   |- unreadable (no text, needs OCR)                              -> PDF/needs_review/unreadable/
   |- byte-identical to a file already waiting                     -> PDF/needs_review/duplicate/
   |- a type rule matches (pdfflow/rules.py + config "triage")     -> PDF/<type>/
   `- nothing matches                                              -> PDF/unknown_type/
```

Reasons under `needs_review`: `no_date`, `unreconciled`, `format_changed` (a known supplier changed its
layout), `unreadable`, `duplicate`. Names are mapped in config `triage.reasons`.

Rules: files keep their names (a clash with different content gets `_NN`); nothing is deleted or
overwritten; files outside `PDF/` are never touched; every move is appended to
`<logs>/triage_manifest.jsonl`. Re-run anything by dropping it back into `unsorted`
or `process_pdf.sh --sweep --resort`.

## Commands
- `process_pdf.sh --status` (or `pdf_triage.py status [--json]`): files and oldest waiting file per folder.
  The same counts appear on the Audit tab of the finance report.
- `process_pdf.sh --sweep [--apply] [--resort] [--dir D]`: run everything waiting through the full flow.
  **Dry run unless `--apply`.**
- `process_pdf.sh [--dry-run] FILE.pdf`: what Hazel runs.

## Configuration (config.json, outside the repo)
`directories`: `pdf`, `unsorted`, `unknown_type`, `needs_review`. `triage`: `stale_days`, `types`
(ordered rules `{"name", "text", "filename"}` tried before the defaults; `"disabled": true` switches a
default off), `reasons`. Personal or supplier-specific rules live there, never in the repo.

## Adding a handler
Create `<section>/pdf_handler.sh` obeying the contract in `process_pdf.sh` (0 handled, 1 not mine,
10 mine but held with a `reason=` field, 2+ failure).

## Config vs data
`config.json` holds settings only (locations, providers, rules). Your own data (remittances, ESS
history, valuations, renovation costs, ...) lives as plain JSON in `directories.records`
(`Finance/Data/<name>.json`, one file per dataset, safe in iCloud because it is not a database).
An inline `"personal"` block in config.json is still read as a fallback; the data files win.

## Foreign-currency invoices and the optional bank cross-check
Apple Japan invoices are recognised (recorded as JPY, never mixed into AUD figures). To compare them
with bank lines, put rough rates in `Finance/Data/fx_rates.json` (data, not config: `{"JPY": {"2022-09-16":
{"aud": 0.0104, "source": "..."}}}`; a `YYYY-MM` key covers a month) and run
`python3 finance/apple_crosscheck.py` (read-only; `--all` also checks AUD invoices; window `--days`, amount
`--tolerance`, default 6% for card FX margin). It reports matched / ambiguous / unmatched / no_rate.

## AGL
AGL bills are filed by the `agl` provider into `{invoices}/AGL` as `AGL_Statement_<service>_<period start>_to_<period end>.pdf`.
The reading is done by `finance/statements/providers/agl_parse.py` (`AGLBillParser`), which `~/repos/tesla` imports
(its `config.json` `scripts_repo`) to pull usage and rates out of the same bills, so there is one copy of that code.
Tesla finds the bills through its own `directories.power_invoices`. AGL leaflets and offers are not bills and stay in triage.
The AGL folder is one shared folder: Tesla's `directories.power_invoices` points at `{invoices}/AGL` (`~/iCloud/Finance/Invoices_Receipts/AGL`)
and Tesla's manual usage CSVs (`AGL_YYYYMM.csv`, `MyUsageData_*.csv`) sit beside the bills. The CSVs are Tesla's inputs, so this
repo does not read, rename or file them; keep that in mind before tidying the folder.
