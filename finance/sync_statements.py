#!/usr/bin/env python3
"""sync_statements.py - Provider-Configurable PDF Statement Rename, Move & Filing Tool.

Discovers, classifies by provider, audits, standardizes filenames, and moves PDF
statements into their configured provider folders.

SYNOPSIS
    ./sync_statements.py [--scan [DIR_OR_PDF ...]] [--provider NAME] [--where-from]
                         [--rename] [--import] [--dry-run] [--recursive] [--json]
    ./sync_statements.py --inspect PDF [PDF ...] [--provider NAME] [--json]
    ./sync_statements.py --providers [--json]

EXAMPLES
    # Look up all registered providers, source domains, templates, and target folders
    ./sync_statements.py --providers

    # Preview all matching statements in a folder (dry-run default)
    ./sync_statements.py ~/iCloud/PDF --provider <provider>

    # Fast-scan only files whose download metadata (kMDItemWhereFroms) matches the provider
    ./sync_statements.py ~/iCloud/PDF --provider <provider> --where-from --import

    # Rename statements in-place to their standardized provider filenames
    ./sync_statements.py ~/iCloud/PDF --provider <provider> --rename

    # Move & rename statements into their configured provider folder
    ./sync_statements.py ~/iCloud/PDF --provider <provider> --import

    # Inspect a single PDF statement in detail
    ./sync_statements.py --inspect /path/to/statement.pdf
"""
import argparse
import filecmp
import json
import os
import re
import shutil
import sys
from typing import Any, Dict, List, Optional

# Set by --allow-unreconciled: permit rename/import of documents that fail the date/reconciliation gate.
ALLOW_UNVERIFIED = False

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from statements import config, paths, store  # noqa: E402
from statements.ingest import (  # noqa: E402
    is_true_binary_duplicate,
    matches_provider_filter,
    non_clobbering_path,
    same_pdf_content,
    set_mtime_from_metadata,
    ensure_companion_txt,
    store_statement,
    unique_path_in_dir,
    walk_pdfs,
)
from statements.money import D, fmt  # noqa: E402
from statements.pdf import PDFTextExtractor, file_sha256, get_where_froms  # noqa: E402
from statements.providers import (  # noqa: E402
    AVAILABLE_PROVIDERS,
    check_missing_periods_by_provider,
    detect_and_extract,
    detect_provider,
    detect_provider_by_metadata,
)


def table(rows: List[List[Any]], header: List[str], right=()):
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


def show_providers(cfg: Dict[str, Any], as_json: bool = False) -> int:
    rows = []
    out_json = []
    provs_cfg = cfg.get("providers", {})
    for p in AVAILABLE_PROVIDERS:
        if p.name == "generic":
            continue
        pc = provs_cfg.get(p.name, {})
        tc = pc.get("target_dir") or cfg.get("target_dirs", {}).get(p.name) or "-"
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
    if as_json:
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


def print_missing_periods(
    statements: List[Dict[str, Any]],
    provider_filter: Optional[str],
    cfg: Dict[str, Any],
) -> List[Dict[str, Any]]:
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


def handle_duplicate_file(
    src_path: str,
    existing_path: str,
    target_fname: str,
    duplicates_dir: str,
    data: Dict[str, Any],
    do_import: bool,
    do_rename: bool,
    dry_run: bool,
    delete_duplicates: bool,
) -> tuple[str, str]:
    """Handle a duplicate PDF:
    - If delete_duplicates is set and it is a verified true binary duplicate (byte-identical
      or identical after stripping PDF download timestamps /CreationDate and /ID), delete it.
    - Otherwise, when do_import or do_rename is set, move it into the provider's Duplicates
      folder (e.g. ~/iCloud/Finance/Statements/HSBC/Duplicates) with its standardized filename.
    Returns (final_path, action_status).
    """
    true_bin_dup = is_true_binary_duplicate(src_path, existing_path)
    existing_name = os.path.basename(existing_path)
    prefix = "dry-run " if (dry_run and (do_import or do_rename or delete_duplicates)) else ""
    kind = "true duplicate" if true_bin_dup else "duplicate"

    if true_bin_dup and delete_duplicates:
        if not dry_run:
            try:
                src_txt = os.path.splitext(src_path)[0] + ".txt"
                if os.path.isfile(src_txt):
                    try:
                        os.remove(src_txt)
                    except OSError:
                        pass
                os.remove(src_path)
                return existing_path, f"{kind} of {existing_name} (deleted)"
            except OSError:
                pass
        return src_path, f"{prefix}{kind} of {existing_name} (delete)"

    if do_import or do_rename:
        base_target, ext_target = os.path.splitext(target_fname)
        dup_fname = f"{base_target}_01{ext_target}" if ("_01" in os.path.basename(src_path)) else target_fname
        dup_dest = unique_path_in_dir(duplicates_dir, dup_fname)
        dup_display = f"{config.show_path(duplicates_dir)}/{os.path.basename(dup_dest)}"
        if not dry_run:
            paths.check_destination(dup_dest)
            os.makedirs(duplicates_dir, exist_ok=True)
            src_txt = os.path.splitext(src_path)[0] + ".txt"
            dup_txt = os.path.splitext(dup_dest)[0] + ".txt"
            if os.path.isfile(src_txt) and src_txt != dup_txt:
                try:
                    shutil.move(src_txt, dup_txt)
                except OSError:
                    pass
            shutil.move(src_path, dup_dest)
            set_mtime_from_metadata(dup_dest, data)
            return dup_dest, f"{kind} -> {dup_display}"
        return src_path, f"{prefix}{kind} -> {dup_display}"

    return src_path, f"{kind} of {existing_name}"

def resolve_scan_inputs(args, cfg: Dict[str, Any]) -> List[str]:
    targets = []
    if args.scan_arg:
        targets.append(args.scan_arg)
    if args.paths:
        targets.extend(args.paths)
    if not targets:
        if args.provider and args.provider.lower() != "auto":
            pc = config.provider_config(cfg, args.provider.lower())
            if pc.get("scan_dirs"):
                targets.extend(pc["scan_dirs"])
        if not targets:
            targets.extend(cfg.get("scan_dirs", []))
    return walk_pdfs(targets, recursive=args.recursive, skip_dirs=cfg.get("skip_dirs", ()))


def run_batch(
    pdfs: List[str],
    cfg: Dict[str, Any],
    db,
    provider_filter: Optional[str] = None,
    do_rename: bool = False,
    do_import: bool = False,
    dry_run: bool = True,
    as_json: bool = False,
    check_missing: bool = False,
    missing_only: bool = False,
    where_from_only: bool = False,
    delete_duplicates: bool = False,
    hazel_log: bool = False,
) -> int:
    if not pdfs:
        if as_json:
            print(json.dumps([]))
        elif hazel_log:
            print("[process_pdf] SKIP status=no_files_found")
            return 1
        else:
            print("No PDF files found to process.")
        return 0

    is_single = len(pdfs) == 1
    results = []
    rows = []
    hazel_lines = []
    seen_targets: Dict[str, str] = {}
    skipped_unrecognised = 0
    moved_count = 0
    renamed_count = 0
    already_count = 0
    duplicate_count = 0

    for p in pdfs:
        orig_name = os.path.basename(p)
        try:
            st = os.stat(p)
        except OSError:
            if hazel_log:
                hazel_lines.append(f"[process_pdf] ERROR file={orig_name} status=not_found")
            continue

        meta_prov, meta_url = detect_provider_by_metadata(p, provider_filter=provider_filter, cfg=cfg)
        if where_from_only and not meta_url:
            skipped_unrecognised += 1
            if hazel_log and is_single:
                hazel_lines.append(f"[process_pdf] SKIP file={orig_name} reason=no_matching_where_froms")
            continue

        try:
            sha = file_sha256(p)
        except OSError:
            continue

        urls = get_where_froms(p)
        src_domain = "-"
        if urls:
            m_dom = re.search(r"https?://([^/:\s]+)", urls[0], re.I)
            src_domain = m_dom.group(1).lower() if m_dom else urls[0][:40]

        # Fast 2-page probe first so non-matching multi-page PDFs (books/manuals) return in <0.05s
        probe_txt = PDFTextExtractor.extract_text(p, db=db, max_pages=2)
        if not probe_txt:
            probe_txt = PDFTextExtractor.extract_text(p, db=db)
        if not probe_txt:
            skipped_unrecognised += 1
            if hazel_log and is_single:
                hazel_lines.append(
                    f"[process_pdf] SKIP file={orig_name} path=\"{p}\" source_domain={src_domain} reason=empty_or_unreadable_pdf"
                )
            continue

        prov = detect_provider(probe_txt, p, forced_provider=provider_filter if is_single else None)
        if not matches_provider_filter(prov.name, prov.doc_type, provider_filter):
            skipped_unrecognised += 1
            if hazel_log and is_single:
                if meta_prov:
                    hazel_lines.append(
                        f"[process_pdf] DRIFT file={orig_name} path=\"{p}\" source_domain={src_domain} "
                        f"expected_provider={meta_prov.name} reason=domain_matched_but_content_format_changed"
                    )
                else:
                    hazel_lines.append(
                        f"[process_pdf] SKIP file={orig_name} path=\"{p}\" source_domain={src_domain} "
                        f"provider=unmatched reason=not_supported_provider"
                    )
            continue

        txt = PDFTextExtractor.extract_text(p, db=db, sha=sha) or probe_txt

        data = prov.extract(txt, p)
        target_fname = prov.generate_filename(data, p, cfg)
        target_dir = prov.resolve_target_dir(data, p, cfg) or os.path.dirname(p)
        duplicates_dir = prov.resolve_duplicates_dir(data, p, cfg)
        data["target_filename"] = target_fname
        data["target_dir"] = target_dir
        data["duplicates_dir"] = duplicates_dir
        urls = get_where_froms(p)
        if urls:
            data["where_froms"] = urls
            data["source_url"] = urls[0]
        if meta_url:
            data["matched_source_url"] = meta_url

        # Safety gate: never rename or move a document we could not date or reconcile.
        # A provider "match" on its own is not proof (e.g. an invoice that merely mentions a provider's name).
        hold = None
        if (do_import or do_rename) and not ALLOW_UNVERIFIED:
            if not (data.get("period_end") or data.get("issue_date")):
                hold = "no_document_date"
            elif not data.get("reconciled"):
                hold = "not_reconciled"
        if hold:
            skipped_unrecognised += 1
            msg = (
                f"[process_pdf] HOLD file={os.path.basename(p)} provider={data.get('doc_type')} reason={hold} "
                f"action=\"left in place; inspect, then re-run with --allow-unreconciled if it is genuine\""
            )
            if hazel_log:
                hazel_lines.append(msg)
            else:
                print(msg, file=sys.stderr)
            continue

        orig_name = os.path.basename(p)
        dest_dir = target_dir if do_import else os.path.dirname(p)
        candidate = os.path.join(dest_dir, target_fname)
        final_path = p

        if os.path.abspath(p) == os.path.abspath(candidate):
            if not dry_run and (do_rename or do_import):
                ensure_companion_txt(p, data=data, db=db)
                if set_mtime_from_metadata(p, data):
                    action_status = "already standard (retouched date)"
                else:
                    action_status = "already standard"
            else:
                action_status = "already standard"
            already_count += 1
        else:
            free_dest = non_clobbering_path(p, candidate, db=db)
            if free_dest is None or (re.search(r"(_01|\(1\))\b", orig_name) and duplicates_dir):
                duplicate_count += 1
                final_path, action_status = handle_duplicate_file(
                    src_path=p,
                    existing_path=candidate,
                    target_fname=target_fname,
                    duplicates_dir=duplicates_dir,
                    data=data,
                    do_import=do_import,
                    do_rename=do_rename,
                    dry_run=dry_run,
                    delete_duplicates=delete_duplicates,
                )
            elif free_dest in seen_targets:
                prev_src = seen_targets[free_dest]
                if same_pdf_content(p, prev_src, db=db) or re.search(r"(_01|\(1\))\b", orig_name):
                    duplicate_count += 1
                    final_path, action_status = handle_duplicate_file(
                        src_path=p,
                        existing_path=prev_src,
                        target_fname=target_fname,
                        duplicates_dir=duplicates_dir,
                        data=data,
                        do_import=do_import,
                        do_rename=do_rename,
                        dry_run=dry_run,
                        delete_duplicates=delete_duplicates,
                    )
                else:
                    base_f, ext_f = os.path.splitext(free_dest)
                    alt_dest = f"{base_f}_01{ext_f}"
                    seen_targets[alt_dest] = p
                    action_status = f"{'dry-run ' if dry_run else ''}{'move' if do_import else 'rename'} -> {os.path.basename(alt_dest)}"
                    if not dry_run and (do_rename or do_import):
                        paths.check_destination(alt_dest)
                        os.makedirs(os.path.dirname(alt_dest), exist_ok=True)
                        src_txt = os.path.splitext(p)[0] + ".txt"
                        alt_txt = os.path.splitext(alt_dest)[0] + ".txt"
                        if os.path.isfile(src_txt) and src_txt != alt_txt:
                            try:
                                shutil.move(src_txt, alt_txt)
                            except OSError:
                                pass
                        shutil.move(p, alt_dest)
                        set_mtime_from_metadata(alt_dest, data)
                        ensure_companion_txt(alt_dest, data=data, db=db)
                        final_path = alt_dest
                        if do_import:
                            moved_count += 1
                        else:
                            renamed_count += 1
            else:
                seen_targets[free_dest] = p
                verb = "move" if do_import else ("rename" if do_rename else "preview")
                prefix = "dry-run " if (dry_run and (do_rename or do_import)) else ""
                dest_display = (
                    f"{config.show_path(dest_dir)}/{os.path.basename(free_dest)}"
                    if do_import
                    else os.path.basename(free_dest)
                )
                action_status = f"{prefix}{verb} -> {dest_display}"
                if not dry_run and (do_rename or do_import):
                    paths.check_destination(free_dest)
                    os.makedirs(os.path.dirname(free_dest), exist_ok=True)
                    src_txt = os.path.splitext(p)[0] + ".txt"
                    dest_txt = os.path.splitext(free_dest)[0] + ".txt"
                    if os.path.isfile(src_txt) and src_txt != dest_txt:
                        try:
                            shutil.move(src_txt, dest_txt)
                        except OSError:
                            pass
                    shutil.move(p, free_dest)
                    set_mtime_from_metadata(free_dest, data)
                    ensure_companion_txt(free_dest, data=data, db=db)
                    final_path = free_dest
                    if do_import:
                        moved_count += 1
                    else:
                        renamed_count += 1

        data["source_path"] = final_path
        data["source_file"] = os.path.basename(final_path)
        if not dry_run:
            store_statement(db, final_path, sha, data)

        accts = data.get("accounts") or []
        tx_cnt = sum(len(a.get("transactions", [])) for a in accts)
        results.append(data)
        audit_tag = "ok" if data.get("reconciled") else "DIFF"
        period_str = f"{data.get('period_start') or '?'}..{data.get('period_end') or '?'}"
        date_touch = data.get("period_end") or data.get("issue_date") or "-"
        hazel_lines.append(
            f"[process_pdf] MATCH file={orig_name} provider={data.get('doc_type')} "
            f"period={period_str} date_retouched={date_touch} accts={len(accts)} txs={tx_cnt} "
            f"audit={audit_tag} standard_name={target_fname} action=\"{action_status}\""
        )
        rows.append(
            [
                orig_name,
                data.get("doc_type"),
                f"{data.get('period_start') or '?'} to {data.get('period_end') or '?'}",
                len(accts),
                tx_cnt,
                audit_tag,
                target_fname,
                config.show_path(target_dir),
                action_status,
            ]
        )

    store.mark_current(db)
    db.commit()

    gaps = (
        check_missing_periods_by_provider(results, provider_filter=provider_filter, cfg=cfg)
        if (check_missing or missing_only)
        else []
    )

    if as_json:
        if missing_only:
            print(json.dumps(gaps, indent=2, default=str))
        elif check_missing:
            print(json.dumps({"statements": results, "missing_periods": gaps}, indent=2, default=str))
        else:
            print(json.dumps(results, indent=2, default=str))
        return 0

    if hazel_log:
        if not hazel_lines:
            print(f"[process_pdf] SUMMARY matched=0 skipped={skipped_unrecognised}")
        else:
            for hl in hazel_lines:
                print(hl)
            if len(rows) > 1:
                print(
                    f"[process_pdf] SUMMARY matched={len(rows)} moved={moved_count} "
                    f"renamed={renamed_count} standard={already_count} "
                    f"duplicates={duplicate_count} skipped={skipped_unrecognised}"
                )
        return 0 if len(rows) > 0 else 1

    if not rows:
        print("No matching provider statements found.")
        return 0

    if not missing_only:
        table(
            rows,
            [
                "Source File",
                "Provider Type",
                "Statement Period",
                "Accts",
                "Txs",
                "Audit",
                "Standard Filename",
                "Target Folder",
                "Action",
            ],
            right={3, 4},
        )
        print(
            f"\nSummary: {len(rows)} matched statement(s) | "
            f"{already_count} already standard | "
            f"{duplicate_count} duplicate(s)"
            + (f" | {skipped_unrecognised} non-matching skipped" if skipped_unrecognised else "")
        )
        if dry_run:
            if do_rename or do_import:
                print("Note: Dry-run mode active — no files were modified on disk.")
            else:
                print("Tip: Pass --rename to rename in-place, or --import to move into the provider's Target Folder.")
        else:
            if do_import:
                print(f"Successfully moved & standardized {moved_count} statement(s) into their provider folder.")
            elif do_rename:
                print(f"Successfully renamed {renamed_count} statement(s) in-place.")

    if check_missing or missing_only:
        print_missing_periods(results, provider_filter=provider_filter, cfg=cfg)

    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="sync_statements.py",
        description="Provider-Configurable PDF Statement Rename, Move & Filing Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("paths", nargs="*", help="PDF files or folders to scan")
    p.add_argument("--scan", dest="scan_arg", nargs="?", const="", metavar="DIR_OR_PDF", help="scan folder or PDF")
    p.add_argument("--inspect", nargs="+", metavar="PDF", help="deep-dive inspect one or more PDF files")
    p.add_argument("--providers", action="store_true", help="list registered providers, source domains, templates, and target folders")
    p.add_argument(
        "--provider",
        default="auto",
        help="filter or force provider (run '--providers' to list available providers)",
    )
    p.add_argument("--target-dir", help="override target directory for --import")
    p.add_argument("--rename", action="store_true", help="rename PDFs in-place to standard provider filename")
    p.add_argument("--import", dest="do_import", action="store_true", help="move & rename PDFs into provider's target folder")
    p.add_argument(
        "--check-missing",
        "--missing-periods",
        dest="check_missing",
        action="store_true",
        help="check for missing statement periods using the provider's cadence & balance rules",
    )
    p.add_argument(
        "--missing-only",
        action="store_true",
        help="show only missing statement periods for the selected provider",
    )
    p.add_argument(
        "--where-from",
        dest="where_from_only",
        action="store_true",
        help="fast-scan only PDFs whose macOS download metadata (kMDItemWhereFroms) matches the provider's source_domains",
    )
    p.add_argument(
        "--delete-duplicates",
        action="store_true",
        help="delete source PDF when a verified true binary duplicate already exists in the target folder",
    )
    p.add_argument(
        "--hazel",
        dest="hazel_log",
        action="store_true",
        help="emit compact single-line structured log output suitable for Hazel.log",
    )
    p.add_argument("--dry-run", action="store_true", default=None, help="preview rename/import without changing disk")
    p.add_argument(
        "--allow-unreconciled",
        action="store_true",
        help="rename/import even when a document has no extracted date or fails reconciliation (manual use only)",
    )
    p.add_argument("--recursive", "-r", action="store_true", help="scan subdirectories recursively")
    p.add_argument("--json", action="store_true", help="output structured JSON")
    p.add_argument("--data-dir", help="override SQLite database directory")

    args = p.parse_args(argv)
    global ALLOW_UNVERIFIED
    ALLOW_UNVERIFIED = bool(args.allow_unreconciled)
    cfg = config.load()
    if args.data_dir:
        cfg["data_dir"] = os.path.expanduser(args.data_dir)
        cfg["db"] = os.path.join(cfg["data_dir"], "pdf_audit.db")
    if args.target_dir:
        exp_t = os.path.abspath(os.path.expanduser(args.target_dir))
        for k in list(cfg.get("target_dirs", {}).keys()):
            cfg["target_dirs"][k] = exp_t
        for k in list(cfg.get("providers", {}).keys()):
            cfg["providers"][k]["target_dir"] = exp_t

    if args.providers:
        return show_providers(cfg, as_json=args.json)

    if args.inspect:
        from pdf_audit import Ctx, cmd_inspect

        inspect_ns = argparse.Namespace(
            pdfs=args.inspect,
            provider=None if args.provider == "auto" else args.provider,
            json=args.json,
            data_dir=args.data_dir,
        )
        return cmd_inspect(Ctx(inspect_ns), inspect_ns)

    is_dry_run = True
    if args.rename or args.do_import:
        is_dry_run = False
    if args.dry_run is not None:
        is_dry_run = args.dry_run

    db = store.connect(cfg["db"])
    pdfs = resolve_scan_inputs(args, cfg)
    return run_batch(
        pdfs=pdfs,
        cfg=cfg,
        db=db,
        provider_filter=args.provider,
        do_rename=args.rename,
        do_import=args.do_import,
        dry_run=is_dry_run,
        as_json=args.json,
        check_missing=args.check_missing,
        missing_only=args.missing_only,
        where_from_only=args.where_from_only,
        delete_duplicates=args.delete_duplicates,
        hazel_log=args.hazel_log,
    )


if __name__ == "__main__":
    sys.exit(main())
