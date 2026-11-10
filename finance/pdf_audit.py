#!/usr/bin/env python3
"""pdf_audit.py - Pluggable PDF Statement & Invoice Analyser, Filing & Ledger Tool."""
import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from statements import config, paths, store  # noqa: E402
from statements.ingest import (  # noqa: E402
    hold_reason,
    matches_provider_filter,
    non_clobbering_path,
    same_pdf_content,
    set_mtime_from_metadata,
    sync,
    walk_pdfs,
)
from statements.money import D, cents, fmt  # noqa: E402
from statements.pdf import PDFTextExtractor  # noqa: E402
from statements.providers import (  # noqa: E402
    AVAILABLE_PROVIDERS,
    check_missing_periods_by_provider,
    detect_and_extract,
    detect_provider,
)

MAN = """
NAME
    pdf_audit.py - analyse, inspect, reconcile, rename, file, and query PDF statements by provider

SYNOPSIS
    pdf_audit.py COMMAND [OPTIONS]

DESCRIPTION
    A pluggable, multi-provider PDF statement & invoice engine combining:
      1. Content- and metadata-based provider & document-type detection across
         registered provider classes.
      2. Standardized non-clobbering filename generation, in-place renaming (--rename),
         and filing into canonical provider folders (--import).
      3. Exact cent-level statement auditing, missing-period gap detection, and
         transaction-level ledger extraction into a rebuildable SQLite database.

COMMANDS
    providers     list registered providers, source domains, templates, and target folders
    sync          read new/changed PDFs into the SQLite store (optionally --rename or --import)
    scan          scan a folder or PDF list (ad-hoc preview, rename, import, or JSON dump)
    inspect       deep-dive inspection card of one or more PDF files
    statements    list ingested statements with account counts, totals, and audit check
    statement     show one statement in full (all accounts, balances, and transactions)
    accounts      summary of all accounts held across ingested statements
    transactions  search, filter, and export transactions (table, CSV, or JSON)
    check         verify arithmetic reconciliation and missing periods across statements
    summary       monthly or category cashflow & spend summary across accounts
    report        generate an interactive, self-contained index.html dashboard
    fetch-email   search email (Gmail / IMAP) for invoices & statements and download PDFs

    Run 'pdf_audit.py COMMAND -h' for a command's options.
    Run 'pdf_audit.py providers' (or '--providers') to look up available providers.

SELECTING A STATEMENT
    latest | a filename fragment | a date inside the period (YYYY-MM-DD)
    | the month it ended (YYYY-MM) | a statement ID
"""


def table(rows, header, right=()):
    rows = [[("" if c is None else str(c)) for c in r] for r in rows]
    w = [
        max(len(str(h)), *(len(r[i]) for r in rows)) if rows else len(str(h))
        for i, h in enumerate(header)
    ]
    line = lambda r: "  ".join(
        (c.rjust(w[i]) if i in right else c.ljust(w[i])) for i, c in enumerate(r)
    ).rstrip()
    print(line(header))
    print("  ".join("─" * w[i] for i in range(len(header))))
    for r in rows:
        print(line(r))


def d(s):
    return date.fromisoformat(s) if isinstance(s, str) and len(s) >= 10 else None


class Ctx:
    def __init__(self, args):
        self.cfg = config.load()
        if getattr(args, "data_dir", None):
            self.cfg["data_dir"] = os.path.expanduser(args.data_dir)
            self.cfg["db"] = os.path.join(self.cfg["data_dir"], "pdf_audit.db")
        self.db = store.connect(self.cfg["db"])
        self.provider = getattr(args, "provider", None)


def select_statement(stmts, sel):
    if not stmts:
        return None
    if sel in (None, "latest"):
        return stmts[0]
    if str(sel).isdigit():
        sid = int(sel)
        by_id = next((s for s in stmts if s["id"] == sid), None)
        if by_id:
            return by_id
    needle = str(sel).lower()
    for s in stmts:
        if needle in os.path.basename(s["file"]).lower() or (
            s.get("target_filename") and needle in s["target_filename"].lower()
        ):
            return s
    try:
        if len(sel) == 7:
            y, m = map(int, sel.split("-"))
            return next(
                (
                    s
                    for s in stmts
                    if s.get("period_end")
                    and d(s["period_end"]).year == y
                    and d(s["period_end"]).month == m
                ),
                None,
            )
        day = d(sel)
        if day:
            return next(
                (
                    s
                    for s in stmts
                    if s.get("period_start")
                    and s.get("period_end")
                    and d(s["period_start"]) <= day <= d(s["period_end"])
                ),
                None,
            )
    except ValueError:
        pass
    return None


# ---------------------------------------------------------------- commands


def cmd_providers(ctx, args):
    rows = []
    out_json = []
    provs_cfg = ctx.cfg.get("providers", {})
    for p in AVAILABLE_PROVIDERS:
        if p.name == "generic":
            continue
        pc = provs_cfg.get(p.name, {})
        tc = pc.get("target_dir") or ctx.cfg.get("target_dirs", {}).get(p.name) or "-"
        tmpl = pc.get("filename_template") or "(provider default)"
        cadence = pc.get("cadence") or getattr(p, "cadence", "monthly")
        doc_kind = pc.get("document_kind") or getattr(p, "document_kind", "statement")
        domains = pc.get("source_domains") or getattr(p, "source_domains", [])
        enabled = pc.get("enabled", True)
        out_json.append(
            {
                "provider": p.name,
                "display_name": p.display_name,
                "doc_type": p.doc_type,
                "document_kind": doc_kind,
                "enabled": enabled,
                "cadence": cadence,
                "source_domains": domains,
                "filename_template": tmpl,
                "target_dir": tc,
            }
        )
        rows.append(
            [
                p.name,
                p.display_name,
                doc_kind,
                "yes" if enabled else "no",
                cadence,
                ", ".join(domains) or "-",
                tmpl,
                config.show_path(tc),
            ]
        )
    if getattr(args, "json", False):
        print(json.dumps(out_json, indent=2))
        return 0
    table(
        rows,
        [
            "Provider",
            "Display Name",
            "Kind",
            "Enabled",
            "Cadence",
            "Source Domains (WhereFroms)",
            "Filename Template",
            "Target Directory",
        ],
    )
    return 0


def cmd_sync(ctx, args):
    c = sync(
        ctx.db,
        ctx.cfg,
        paths=args.paths if args.paths else None,
        provider_filter=args.provider,
        force=args.force,
        recursive=args.recursive if args.recursive else None,
        do_rename=args.rename,
        do_import=args.do_import,
        dry_run=args.dry_run,
    )
    print(
        f"Statements read: {c['statements']}   account sections: {c['sections']}   "
        f"transactions: {c['transactions']:,}   unchanged: {c['unchanged']}   skipped: {c['skipped']}"
    )
    if c["actions"]:
        prefix = "[DRY-RUN] " if args.dry_run else ""
        for act in c["actions"]:
            if act["action"] == "duplicate":
                print(
                    f"  {prefix}DUPLICATE: {config.show_path(act['src'])} == {config.show_path(act['dest'])}"
                )
            else:
                verb = "Move" if act["action"] == "import" else "Rename"
                print(
                    f"  {prefix}{verb}: {config.show_path(act['src'])} -> {config.show_path(act['dest'])}"
                )

    rows = ctx.db.execute(
        "SELECT doc_type, COUNT(*), MIN(period_start), MAX(period_end), SUM(reconciled=1) "
        "FROM statements WHERE current=1 GROUP BY doc_type ORDER BY doc_type"
    ).fetchall()
    for r in rows:
        print(
            f"  • {r['doc_type']:<18} {r[1]:3d} active statements ({r[2] or '?'} to {r[3] or '?'}) — "
            f"{r[4]}/{r[1]} reconciled"
        )
    return 0


def render_missing_periods(statements, provider_filter, cfg):
    gaps = check_missing_periods_by_provider(statements, provider_filter=provider_filter, cfg=cfg)
    if gaps:
        print(f"\nMissing Statement Periods ({len(gaps)} gap(s) detected):")
        gap_rows = []
        for idx, g in enumerate(gaps):
            shifts = g.get("shift_rows") or []
            if not shifts:
                gap_rows.append(
                    [
                        g["provider"],
                        f"{g['missing_start']} to {g['missing_end']}",
                        g["days"],
                        g["estimated_missing"],
                        "-",
                        "-",
                        "-",
                        f"{g['before_file']} -> {g['after_file']}",
                    ]
                )
            else:
                for i, sr in enumerate(shifts):
                    acct_label = sr["account_no"]
                    if sr.get("account_name"):
                        acct_label = f"{sr['account_no']} ({sr['account_name']})"
                    gap_rows.append(
                        [
                            g["provider"] if i == 0 else "",
                            f"{g['missing_start']} to {g['missing_end']}" if i == 0 else "",
                            g["days"] if i == 0 else "",
                            g["estimated_missing"] if i == 0 else "",
                            acct_label,
                            sr["before_balance"],
                            sr["after_balance"],
                            f"{g['before_file']} -> {g['after_file']}" if i == 0 else "",
                        ]
                    )
            if idx < len(gaps) - 1 and len(shifts) > 1:
                gap_rows.append(["", "", "", "", "", "", "", ""])
        table(
            gap_rows,
            [
                "Provider",
                "Missing Period",
                "Days",
                "Est. Missing",
                "Account",
                "Prior Close",
                "Next Open",
                "Between Files",
            ],
            right={2, 5, 6},
        )
    else:
        print("\nTimeline Continuity: No missing statement periods detected for provider.")
    return gaps


def cmd_scan(ctx, args):
    import shutil

    scan_dirs = args.paths if args.paths else ctx.cfg["scan_dirs"]
    pdfs = walk_pdfs(scan_dirs, recursive=args.recursive, skip_dirs=ctx.cfg.get("skip_dirs", ()))
    is_single = len(pdfs) == 1
    results = []
    rows = []

    seen_targets = {}
    for p in pdfs:
        txt = PDFTextExtractor.extract_text(p, db=ctx.db)
        if not txt:
            continue
        prov = detect_provider(txt, p, forced_provider=args.provider if is_single else None)
        if not matches_provider_filter(prov.name, prov.doc_type, args.provider):
            continue
        data = prov.extract(txt, p)
        data["target_filename"] = prov.generate_filename(data, p, ctx.cfg)
        data["target_dir"] = prov.resolve_target_dir(data, p, ctx.cfg)

        target_fname = data.get("target_filename") or os.path.basename(p)
        target_dir = data.get("target_dir") or os.path.dirname(p)
        action_status = "unchanged"

        held = hold_reason(data) if (args.rename or args.do_import) else None
        if held:
            action_status = f"HOLD ({held}): left in place"
        if (args.rename or args.do_import) and not held:
            dest_dir = target_dir if args.do_import else os.path.dirname(p)
            candidate = os.path.join(dest_dir, target_fname)
            if os.path.abspath(p) != os.path.abspath(candidate):
                free_dest = non_clobbering_path(p, candidate, db=ctx.db)
                if free_dest is None:
                    action_status = "duplicate (on disk)"
                elif args.dry_run and free_dest in seen_targets:
                    if same_pdf_content(p, seen_targets[free_dest], db=ctx.db):
                        action_status = f"duplicate of {os.path.basename(seen_targets[free_dest])}"
                    else:
                        base_f, ext_f = os.path.splitext(free_dest)
                        action_status = f"dry-run -> {os.path.basename(base_f)}_01{ext_f}"
                else:
                    seen_targets[free_dest] = p
                    action_status = (
                        ("dry-run " if args.dry_run else "")
                        + ("imported" if args.do_import else "renamed")
                        + f" -> {os.path.basename(free_dest)}"
                    )
                    if not args.dry_run:
                        paths.check_destination(free_dest)
                        os.makedirs(os.path.dirname(free_dest), exist_ok=True)
                        shutil.move(p, free_dest)
                        set_mtime_from_metadata(free_dest, data)
                        data["source_path"] = free_dest
                        data["source_file"] = os.path.basename(free_dest)

        accts = data.get("accounts") or []
        tx_cnt = sum(len(a.get("transactions", [])) for a in accts)
        results.append(data)
        rows.append(
            [
                data.get("doc_type"),
                f"{data.get('period_start') or '?'} to {data.get('period_end') or '?'}",
                len(accts),
                tx_cnt,
                "ok" if data.get("reconciled") else "DIFF",
                os.path.basename(p),
                target_fname,
                action_status if (args.rename or args.do_import) else "",
            ]
        )

    if args.json:
        print(json.dumps(results, indent=2, default=str))
        return 0

    if not rows:
        print("No matching PDF statements found.")
        return 0

    hdr = ["Type", "Period", "Accts", "Txs", "Check", "Current File", "Standard Name"]
    if args.rename or args.do_import:
        hdr.append("Action")
    else:
        rows = [r[:-1] for r in rows]
    table(rows, hdr, right={2, 3})
    print(f"\nTotal matched PDFs: {len(rows)}")
    if getattr(args, "check_missing", False):
        render_missing_periods(results, args.provider, ctx.cfg)
    return 0


def cmd_inspect(ctx, args):
    results = []
    rc = 0
    for raw_path in args.pdfs:
        pdf_path = os.path.expanduser(raw_path)
        if not os.path.isfile(pdf_path):
            print(f"Error: File not found: {pdf_path}", file=sys.stderr)
            rc = 1
            continue

        txt = PDFTextExtractor.extract_text(pdf_path, db=ctx.db)
        prov, data = detect_and_extract(txt, pdf_path, forced_provider=args.provider, cfg=ctx.cfg)
        if args.json:
            results.append(data)
            continue

        print("═" * 84)
        print(f"PDF STATEMENT INSPECTION: {os.path.basename(pdf_path)}")
        print("═" * 84)
        print(f"  Source Path:      {config.show_path(os.path.abspath(pdf_path))}")
        print(f"  File Size:        {os.path.getsize(pdf_path):,} bytes")
        print(f"  Detected Type:    {prov.display_name} (`{data.get('doc_type')}`)")
        print(f"  Customer / Card:  {data.get('customer_no') or '-'}")
        print(f"  Statement Period: {data.get('period_start') or '?'} to {data.get('period_end') or '?'}")
        print(f"  Standard Name:    {data.get('target_filename')}")
        print(f"  Target Folder:    {config.show_path(data.get('target_dir'))}")
        if data.get("source_url"):
            src_u = data["source_url"]
            print(f"  Download Source:  {src_u[:80]}{'...' if len(src_u) > 80 else ''}")
        print(f"  Reconciled:       {'✓ YES (all accounts & transactions balance)' if data.get('reconciled') else '⚠ NO'}")
        if data.get("portfolio_deposits") is not None:
            print(
                f"  Portfolio Summary: Deposits {fmt(data['portfolio_deposits'])} | "
                f"Loans {fmt(data['portfolio_loans'])} | Net {fmt(data['portfolio_net_position'])}"
            )
        print("─" * 84)

        for acct in data.get("accounts", []):
            txs = acct.get("transactions", [])
            rate_str = f" · Rate {acct['interest_rate']}% p.a." if acct.get("interest_rate") else ""
            lim_str = f" · Limit {fmt(acct['credit_limit'])}" if acct.get("credit_limit") else ""
            note_str = f"[{acct.get('note', '')}]" if acct.get("note") else ""
            print(
                f"\nAccount: {acct.get('account_name', 'Unknown')} ({acct.get('account_no', '-')}) "
                f"{note_str}{rate_str}{lim_str}".strip()
            )
            print(
                f"  Opening: {fmt(acct.get('opening_balance'))}   "
                f"Debits ({acct.get('calc_count_debits', 0)}): {fmt(acct.get('calc_debits'))}   "
                f"Credits ({acct.get('calc_count_credits', 0)}): {fmt(acct.get('calc_credits'))}   "
                f"Closing: {fmt(acct.get('closing_balance'))}"
            )
            if acct.get("minimum_payment") is not None:
                pts = f"   Qantas Points: {acct['qantas_points_earned']:,}" if acct.get("qantas_points_earned") else ""
                print(
                    f"  Min Payment: {fmt(acct['minimum_payment'])}   "
                    f"Due Date: {acct.get('due_date') or '-'}{pts}"
                )
            if txs:
                print()
                tx_rows = [
                    [
                        t.get("date") or "-",
                        (t.get("description") or "")[:48],
                        fmt(t["debit"]) if t.get("debit") else "",
                        fmt(t["credit"]) if t.get("credit") else "",
                        fmt(t.get("balance")),
                        t.get("category") or "",
                    ]
                    for t in txs[:25]
                ]
                table(
                    tx_rows,
                    ["Date", "Description", "Debit", "Credit", "Balance", "Category"],
                    right={2, 3, 4},
                )
                if len(txs) > 25:
                    print(f"  ... and {len(txs) - 25} more transactions (use 'statement' or '--json' to view all)")
        print()

    if args.json:
        out = results[0] if len(results) == 1 else results
        print(json.dumps(out, indent=2, default=str))
    return rc


def cmd_statements(ctx, args):
    stmts = store.statement_rows(
        ctx.db, provider=args.provider, doc_type=args.type, current_only=not args.all
    )
    if not stmts:
        print("No statements in database. Run: pdf_audit.py sync")
        return 1

    rows = []
    for s in stmts:
        d_obj = s["data"]
        accts = d_obj.get("accounts", [])
        tx_cnt = sum(len(a.get("transactions", [])) for a in accts)
        tot_deb = sum((D(a.get("calc_debits")) for a in accts), D(0))
        tot_cred = sum((D(a.get("calc_credits")) for a in accts), D(0))
        rows.append(
            [
                s["id"],
                s["doc_type"],
                f"{s.get('period_start') or '?'} to {s.get('period_end') or '?'}",
                len(accts),
                tx_cnt,
                fmt(tot_deb),
                fmt(tot_cred),
                "matches" if s["reconciled"] else "DIFF",
                os.path.basename(s["file"]),
            ]
        )
    table(
        rows,
        ["ID", "Type", "Period", "Accts", "Txs", "Total Debits", "Total Credits", "Audit", "File"],
        right={0, 3, 4, 5, 6},
    )
    return 0


def cmd_statement(ctx, args):
    stmts = store.statement_rows(
        ctx.db, provider=args.provider, doc_type=args.type, current_only=False
    )
    s = select_statement(stmts, args.statement)
    if not s:
        print(f"No statement matches '{args.statement}'.")
        return 1

    d_obj = s["data"]
    print(
        f"Statement #{s['id']} · {d_obj.get('provider_display')} ({s['doc_type']}) · "
        f"{s.get('period_start') or '?'} to {s.get('period_end') or '?'}"
    )
    print(
        f"File: {config.show_path(s['file'])}   "
        f"Standard Name: {s.get('target_filename')}   "
        f"Audit: {'matches' if s['reconciled'] else 'DIFF'}"
    )
    if d_obj.get("portfolio_deposits") is not None:
        print(
            f"Portfolio: Deposits {fmt(d_obj['portfolio_deposits'])}   "
            f"Loans {fmt(d_obj['portfolio_loans'])}   "
            f"Net Position {fmt(d_obj['portfolio_net_position'])}"
        )

    for acct in d_obj.get("accounts", []):
        if args.account and args.account not in acct.get("account_no", "") and args.account.lower() not in acct.get("account_name", "").lower():
            continue
        txs = acct.get("transactions", [])
        rate_str = f" · Rate {acct['interest_rate']}% p.a." if acct.get("interest_rate") else ""
        lim_str = f" · Limit {fmt(acct['credit_limit'])}" if acct.get("credit_limit") else ""
        print(
            f"\n─── {acct['account_name']} ({acct['account_no']}) [{acct['note']}]{rate_str}{lim_str} ───"
        )
        print(
            f"Opening: {fmt(acct['opening_balance'])}   "
            f"Debits ({acct['calc_count_debits']}): {fmt(acct['calc_debits'])}   "
            f"Credits ({acct['calc_count_credits']}): {fmt(acct['calc_credits'])}   "
            f"Closing: {fmt(acct['closing_balance'])}"
        )
        if txs:
            tx_rows = [
                [
                    t.get("date") or "-",
                    (t.get("description") or "")[:52],
                    fmt(t["debit"]) if t.get("debit") else "",
                    fmt(t["credit"]) if t.get("credit") else "",
                    fmt(t.get("balance")),
                    t.get("category") or "",
                ]
                for t in txs
            ]
            table(
                tx_rows,
                ["Date", "Description", "Debit", "Credit", "Balance", "Category"],
                right={2, 3, 4},
            )
    return 0


def cmd_accounts(ctx, args):
    q = """
    SELECT
      s_sec.account_no,
      MAX(s_sec.account_name) AS account_name,
      MAX(s_sec.doc_type) AS doc_type,
      COUNT(*) AS stmt_count,
      MIN(s_sec.period_start) AS min_start,
      MAX(s_sec.period_end) AS max_end,
      SUM(CAST(s_sec.calc_debits AS REAL)) AS sum_deb,
      SUM(CAST(s_sec.calc_credits AS REAL)) AS sum_cred,
      SUM(s_sec.calc_count_debits + s_sec.calc_count_credits) AS tx_count,
      (SELECT s2.closing_balance FROM account_sections s2
       JOIN statements st2 ON st2.id = s2.statement_id
       WHERE s2.account_no = s_sec.account_no AND st2.current = 1
       ORDER BY s2.period_end DESC, s2.id DESC LIMIT 1) AS latest_balance,
      (SELECT s2.interest_rate FROM account_sections s2
       JOIN statements st2 ON st2.id = s2.statement_id
       WHERE s2.account_no = s_sec.account_no AND st2.current = 1 AND s2.interest_rate IS NOT NULL
       ORDER BY s2.period_end DESC, s2.id DESC LIMIT 1) AS latest_rate
    FROM account_sections s_sec
    JOIN statements st ON st.id = s_sec.statement_id
    WHERE st.current = 1
    GROUP BY s_sec.account_no
    ORDER BY max_end DESC, s_sec.account_no
    """
    rows_db = ctx.db.execute(q).fetchall()
    if not rows_db:
        print("No accounts found. Run: pdf_audit.py sync")
        return 1

    rows = []
    for r in rows_db:
        rows.append(
            [
                r["account_no"],
                r["account_name"],
                r["doc_type"],
                r["stmt_count"],
                f"{r['min_start'] or '?'} to {r['max_end'] or '?'}",
                r["tx_count"] or 0,
                fmt(r["sum_deb"] or 0),
                fmt(r["sum_cred"] or 0),
                fmt(r["latest_balance"]),
                f"{r['latest_rate']}%" if r["latest_rate"] else "-",
            ]
        )
    table(
        rows,
        [
            "Account No",
            "Account Name",
            "Type",
            "Stmts",
            "Date Range",
            "Txs",
            "Total Debits",
            "Total Credits",
            "Latest Balance",
            "Rate",
        ],
        right={3, 5, 6, 7, 8, 9},
    )
    return 0


def cmd_transactions(ctx, args):
    q = """
    SELECT t.*
    FROM transactions t
    JOIN statements s ON s.id = t.statement_id
    WHERE s.current = 1
    """
    params = []
    if args.provider:
        if args.provider.lower() == "hsbc":
            q += " AND t.provider LIKE 'hsbc%'"
        else:
            q += " AND (t.provider = ? OR t.doc_type = ?)"
            params.extend([args.provider, args.provider])
    if args.account:
        q += " AND (t.account_no LIKE ? OR LOWER(t.account_name) LIKE ?)"
        params.extend([f"%{args.account}%", f"%{args.account.lower()}%"])
    if args.category:
        q += " AND LOWER(t.category) LIKE ?"
        params.append(f"%{args.category.lower()}%")
    if args.search:
        q += " AND LOWER(t.description) LIKE ?"
        params.append(f"%{args.search.lower()}%")
    if args.date_from:
        q += " AND t.tx_date >= ?"
        params.append(args.date_from)
    if args.date_to:
        q += " AND t.tx_date <= ?"
        params.append(args.date_to)
    if args.min_amount is not None:
        q += " AND ABS(CAST(t.amount AS REAL)) >= ?"
        params.append(float(args.min_amount))

    q += " ORDER BY t.tx_date DESC, t.statement_id DESC, t.seq DESC"
    if args.limit and not args.csv and not args.json:
        q += f" LIMIT {int(args.limit)}"

    rows_db = [dict(r) for r in ctx.db.execute(q, params).fetchall()]

    if args.json:
        print(json.dumps(rows_db, indent=2))
        return 0

    if args.csv:
        out_f = open(args.csv, "w", newline="", encoding="utf-8") if args.csv != "-" else sys.stdout
        writer = csv.writer(out_f)
        writer.writerow(
            [
                "date",
                "account_no",
                "account_name",
                "description",
                "card_used",
                "debit",
                "credit",
                "amount",
                "balance",
                "category",
                "doc_type",
            ]
        )
        for r in rows_db:
            writer.writerow(
                [
                    r["tx_date"],
                    r["account_no"],
                    r["account_name"],
                    r["description"],
                    r["card_used"] or "",
                    r["debit"] or "",
                    r["credit"] or "",
                    r["amount"],
                    r["balance"],
                    r["category"],
                    r["doc_type"],
                ]
            )
        if out_f is not sys.stdout:
            out_f.close()
            print(f"Wrote {len(rows_db)} transactions to {args.csv}")
        return 0

    if not rows_db:
        print("No matching transactions.")
        return 0

    tot_deb = sum((D(r["debit"]) for r in rows_db if r["debit"]), D(0))
    tot_cred = sum((D(r["credit"]) for r in rows_db if r["credit"]), D(0))

    rows = [
        [
            r["tx_date"] or "-",
            r["account_no"],
            (r["description"] or "")[:48],
            fmt(r["debit"]) if r["debit"] else "",
            fmt(r["credit"]) if r["credit"] else "",
            fmt(r["balance"]),
            r["category"] or "",
        ]
        for r in rows_db
    ]
    table(
        rows,
        ["Date", "Account", "Description", "Debit", "Credit", "Balance", "Category"],
        right={3, 4, 5},
    )
    print(
        f"\nShowing {len(rows_db)} transaction(s) — "
        f"Debits: {fmt(tot_deb)}   Credits: {fmt(tot_cred)}   Net: {fmt(tot_cred - tot_deb)}"
    )
    return 0


def cmd_check(ctx, args):
    prov_filter = args.provider or ctx.provider
    q_sec = (
        "SELECT s.id, s.file, s.provider, s.doc_type, s.period_start, s.period_end, "
        "sec.account_no, sec.account_name, sec.opening_balance, sec.closing_balance, "
        "sec.calc_closing_balance, sec.total_debits, sec.calc_debits, "
        "sec.total_credits, sec.calc_credits, sec.reconciled, sec.note "
        "FROM account_sections sec "
        "JOIN statements s ON s.id = sec.statement_id "
        "WHERE s.current = 1 "
    )
    q_params = []
    if prov_filter and prov_filter.lower() != "auto":
        if prov_filter.lower() == "hsbc":
            q_sec += "AND s.provider LIKE 'hsbc%' "
        else:
            q_sec += "AND (s.provider = ? OR s.doc_type = ?) "
            q_params.extend([prov_filter, prov_filter])
    q_sec += "ORDER BY s.period_end DESC, sec.account_no"

    rows_db = ctx.db.execute(q_sec, q_params).fetchall()
    if not rows_db:
        print("No statements to check. Run: pdf_audit.py sync")
        return 1

    bad = [r for r in rows_db if not r["reconciled"]]
    if not getattr(args, "missing_only", False):
        print(
            f"Checked {len(rows_db)} account sections across active statements: "
            f"{len(rows_db) - len(bad)} matched, {len(bad)} differed."
        )
        if bad or args.all:
            show = rows_db if args.all else bad
            rows = [
                [
                    r["id"],
                    f"{r['period_start'] or '?'} to {r['period_end'] or '?'}",
                    r["account_no"],
                    r["account_name"],
                    fmt(r["opening_balance"]),
                    fmt(r["calc_debits"]),
                    fmt(r["calc_credits"]),
                    fmt(r["closing_balance"]),
                    fmt(r["calc_closing_balance"]),
                    "ok" if r["reconciled"] else "DIFF",
                    os.path.basename(r["file"]),
                ]
                for r in show
            ]
            table(
                rows,
                [
                    "ID",
                    "Period",
                    "Account",
                    "Name",
                    "Opening",
                    "Debits",
                    "Credits",
                    "Printed Close",
                    "Calc Close",
                    "Check",
                    "File",
                ],
                right={0, 4, 5, 6, 7, 8},
            )

    stmts = store.statement_rows(ctx.db, provider=prov_filter, current_only=True)
    render_missing_periods(stmts, prov_filter, ctx.cfg)
    return 3 if bad else 0


def cmd_summary(ctx, args):
    q = """
    SELECT t.*
    FROM transactions t
    JOIN statements s ON s.id = t.statement_id
    WHERE s.current = 1
    """
    params = []
    if args.account:
        q += " AND (t.account_no LIKE ? OR LOWER(t.account_name) LIKE ?)"
        params.extend([f"%{args.account}%", f"%{args.account.lower()}%"])
    if args.date_from:
        q += " AND t.tx_date >= ?"
        params.append(args.date_from)
    if args.date_to:
        q += " AND t.tx_date <= ?"
        params.append(args.date_to)
    if args.exclude_transfers:
        q += " AND t.category NOT IN ('Transfer', 'Credit Card Payment')"

    txs = ctx.db.execute(q, params).fetchall()
    if not txs:
        print("No transactions found for summary.")
        return 1

    by_group = defaultdict(lambda: {"debits": D(0), "credits": D(0), "count": 0})
    for t in txs:
        if args.by == "month":
            key = (t["tx_date"] or "unknown")[:7]
        elif args.by == "account":
            key = f"{t['account_no']} ({t['account_name']})"
        else:
            key = t["category"] or "General"
        if t["debit"]:
            by_group[key]["debits"] += D(t["debit"])
        if t["credit"]:
            by_group[key]["credits"] += D(t["credit"])
        by_group[key]["count"] += 1

    items = sorted(
        by_group.items(),
        key=lambda kv: kv[0] if args.by == "month" else kv[1]["debits"],
        reverse=True,
    )
    rows = []
    tot_d, tot_c, tot_n = D(0), D(0), 0
    for k, v in items:
        net = v["credits"] - v["debits"]
        tot_d += v["debits"]
        tot_c += v["credits"]
        tot_n += v["count"]
        rows.append([k, v["count"], fmt(v["debits"]), fmt(v["credits"]), fmt(net)])

    rows.append(["Total", tot_n, fmt(tot_d), fmt(tot_c), fmt(tot_c - tot_d)])
    label = {"month": "Month", "category": "Category", "account": "Account"}[args.by]
    table(rows, [label, "Txs", "Total Debits", "Total Credits", "Net Cashflow"], right={1, 2, 3, 4})
    return 0


def cmd_report(ctx, args):
    from statements.report import build_report
    out = getattr(args, "output", None)
    out_path = build_report(
        ctx.db,
        output_path=out,
        cfg=ctx.cfg,
        provider_filter=getattr(args, "provider", None),
    )
    print(f"Interactive financial dashboard generated successfully:\n  {config.show_path(out_path)}")
    if getattr(args, "open_browser", False):
        import subprocess
        subprocess.run(["open", out_path])
    return 0


def cmd_fetch_email(ctx, args):
    import subprocess
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fetch_email_invoices.py")
    cmd = [sys.executable, script_path]
    if getattr(args, "preset", None):
        cmd.append(args.preset)
    if getattr(args, "query", None):
        cmd.extend(["--query", args.query])
    if getattr(args, "since", None):
        cmd.extend(["--since", args.since])
    if getattr(args, "limit", None):
        cmd.extend(["--limit", str(args.limit)])
    if getattr(args, "target_dir", None):
        cmd.extend(["--target-dir", args.target_dir])
    if getattr(args, "dry_run", False):
        cmd.append("--dry-run")
    if getattr(args, "setup", False):
        cmd.append("--setup")
    if getattr(args, "sync", False):
        cmd.append("--sync")
    return subprocess.run(cmd).returncode


# ---------------------------------------------------------------- CLI parser


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="pdf_audit.py",
        description=MAN,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        usage="pdf_audit.py COMMAND [OPTIONS]",
    )
    p.add_argument("--data-dir", help="database folder (overrides config.json)")
    p.add_argument(
        "--provider",
        help="filter by provider (run 'pdf_audit.py providers' to list available providers)",
    )
    p.add_argument(
        "--providers",
        action="store_true",
        help="list registered providers, source domains, templates, and target folders",
    )
    sp = p.add_subparsers(dest="cmd", metavar="COMMAND")
    RD = argparse.RawDescriptionHelpFormatter

    s = sp.add_parser(
        "providers",
        help="list registered providers, source domains, templates, and target folders",
        formatter_class=RD,
    )
    s.add_argument("--json", action="store_true", help="output providers as JSON")
    s.set_defaults(fn=cmd_providers)

    s = sp.add_parser(
        "sync",
        help="read new or changed PDF statements into SQLite (and optionally rename/import)",
        formatter_class=RD,
    )
    s.add_argument("paths", nargs="*", help="optional folders or PDF files to sync (default: config.json scan_dirs)")
    s.add_argument("--provider", help="only sync matching provider (see 'pdf_audit.py providers')")
    s.add_argument("--force", action="store_true", help="re-read every PDF even if unchanged")
    s.add_argument("--recursive", action="store_true", help="walk subdirectories recursively")
    s.add_argument("--rename", action="store_true", help="standardize filenames in place")
    s.add_argument("--import", dest="do_import", action="store_true", help="move into target provider folder with standard name")
    s.add_argument("--dry-run", action="store_true", help="preview rename/import actions without moving files")
    s.set_defaults(fn=cmd_sync)

    s = sp.add_parser(
        "scan",
        help="scan folders or PDFs (preview standard names, rename/import, or output JSON)",
        formatter_class=RD,
    )
    s.add_argument("paths", nargs="*", help="folders or PDF files to scan")
    s.add_argument("--provider", help="filter or force provider (see 'pdf_audit.py providers')")
    s.add_argument("--recursive", action="store_true", help="scan subdirectories recursively")
    s.add_argument("--rename", action="store_true", help="rename PDFs in-place to standard filename")
    s.add_argument("--import", dest="do_import", action="store_true", help="move PDFs to provider's target folder")
    s.add_argument("--check-missing", "--missing-periods", dest="check_missing", action="store_true", help="check for missing statement periods using the provider's cadence & balance rules")
    s.add_argument("--dry-run", action="store_true", help="preview rename/import without changing disk")
    s.add_argument("--json", action="store_true", help="dump structured JSON")
    s.set_defaults(fn=cmd_scan)

    s = sp.add_parser("inspect", help="deep-dive inspect one or more PDF files", formatter_class=RD)
    s.add_argument("pdfs", nargs="+", metavar="PDF", help="path(s) to PDF file(s)")
    s.add_argument("--provider", help="force specific provider (see 'pdf_audit.py providers')")
    s.add_argument("--json", action="store_true", help="output extracted structure as JSON")
    s.set_defaults(fn=cmd_inspect)

    s = sp.add_parser("statements", help="list all ingested statements with audit status", formatter_class=RD)
    s.add_argument("--provider", help="filter by provider (see 'pdf_audit.py providers')")
    s.add_argument("--type", help="filter by document type")
    s.add_argument("--all", action="store_true", help="include duplicate copies")
    s.set_defaults(fn=cmd_statements)

    s = sp.add_parser("statement", help="show one statement in full detail", formatter_class=RD)
    s.add_argument("statement", nargs="?", default="latest", help="latest | ID | filename | YYYY-MM | YYYY-MM-DD")
    s.add_argument("--provider", help="filter by provider")
    s.add_argument("--type", help="filter by document type")
    s.add_argument("--account", help="show only matching account number or name")
    s.set_defaults(fn=cmd_statement)

    s = sp.add_parser("accounts", help="summary of all accounts held across statements", formatter_class=RD)
    s.set_defaults(fn=cmd_accounts)

    s = sp.add_parser("transactions", help="search, filter, and export transactions", formatter_class=RD)
    s.add_argument("--provider", help="filter by provider")
    s.add_argument("--account", help="filter by account number or name")
    s.add_argument("--category", help="filter by category")
    s.add_argument("--search", help="case-insensitive substring match on description")
    s.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD", help="start date inclusive")
    s.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD", help="end date inclusive")
    s.add_argument("--min-amount", type=float, help="minimum absolute dollar amount")
    s.add_argument("--limit", type=int, default=100, help="max rows for table display (default 100, 0 for all)")
    s.add_argument("--csv", metavar="FILE", help="export matching transactions to CSV file (or '-' for stdout)")
    s.add_argument("--json", action="store_true", help="export matching transactions as JSON")
    s.set_defaults(fn=cmd_transactions)

    s = sp.add_parser("check", help="verify arithmetic reconciliation and missing statement periods", formatter_class=RD)
    s.add_argument("--provider", help="filter by provider (see 'pdf_audit.py providers')")
    s.add_argument("--missing-only", "--check-missing", dest="missing_only", action="store_true", help="show only missing statement periods for the selected provider")
    s.add_argument("--all", action="store_true", help="print every account section, not just mismatches")
    s.set_defaults(fn=cmd_check)

    s = sp.add_parser("summary", help="aggregate spend & cashflow by category, month, or account", formatter_class=RD)
    s.add_argument("--by", choices=("category", "month", "account"), default="category")
    s.add_argument("--account", help="filter by account number or name")
    s.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD")
    s.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD")
    s.add_argument("--exclude-transfers", action="store_true", help="exclude internal transfers and credit card payments")
    s.set_defaults(fn=cmd_summary)

    s = sp.add_parser("report", help="generate an interactive, self-contained index.html dashboard", formatter_class=RD)
    s.add_argument("-o", "--output", help="output HTML path (default: config.json report_path, or directories.reports/index.html)")
    s.add_argument("--provider", help="filter by provider (default: all providers)")
    s.add_argument("--open", dest="open_browser", action="store_true", help="open report in default web browser")
    s.set_defaults(fn=cmd_report)

    s = sp.add_parser("fetch-email", help="search email (Gmail / IMAP) for invoices & statements and download PDFs", formatter_class=RD)
    s.add_argument("preset", nargs="?", default="all", help="target provider preset (apple, qantas, tesla, commbank, hsbc, ing, utilities, bunnings, all)")
    s.add_argument("--query", "-q", help="custom Gmail/IMAP search query")
    s.add_argument("--since", help="search emails since date (YYYY-MM-DD)")
    s.add_argument("--limit", "-n", type=int, default=30, help="max emails to inspect per provider (default 30)")
    s.add_argument("--target-dir", "-o", help="target staging directory for attachments (default: ~/iCloud/PDF)")
    s.add_argument("--dry-run", action="store_true", help="preview matching emails without downloading")
    s.add_argument("--setup", action="store_true", help="interactive setup to configure email credentials")
    s.add_argument("--sync", action="store_true", help="automatically run 'pdf_audit.py sync' after download")
    s.set_defaults(fn=cmd_fetch_email)

    args = p.parse_args(argv)
    if getattr(args, "providers", False):
        return cmd_providers(Ctx(args), args)
    if not getattr(args, "fn", None):
        p.print_help()
        return 2
    ctx = Ctx(args)
    try:
        return args.fn(ctx, args) or 0
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
