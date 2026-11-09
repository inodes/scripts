#!/usr/bin/env python3
"""audit_hazel_logs.py - Audit Hazel & process_pdf logs to discover unmatched PDFs and scaffold new providers.

Reads ~/Library/Logs/Hazel/process_pdf.log and ~/Library/Logs/Hazel/Hazel.log to:
  1. Summarize matched vs unmatched (failed/skipped) PDFs processed by Hazel.
  2. Group unmatched PDFs by download source domain (kMDItemWhereFroms), filename prefix,
     and detected document headers.
  3. Inspect candidate unmatched PDFs (--sample / --scaffold) to show their download URL,
     extracted text header, detected dates, and a ready-to-use BasePDFProvider class skeleton.

SYNOPSIS
    ./audit_hazel_logs.py [--unmatched] [--matched] [--errors] [--domains]
                          [--sample PATTERN_OR_FILE] [--scaffold PROVIDER_NAME] [--json]
"""
import argparse
import collections
import os
import re
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from statements import config, store  # noqa: E402
from statements.pdf import PDFTextExtractor, get_where_froms  # noqa: E402

DEFAULT_PROCESS_LOG = os.path.expanduser("~/Library/Logs/Hazel/process_pdf.log")
DEFAULT_HAZEL_LOG = os.path.expanduser("~/Library/Logs/Hazel/Hazel.log")
DEFAULT_PDF_DIR = os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/PDF")


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


def extract_domain(url: str) -> str:
    if not url:
        return "-"
    m = re.search(r"https?://([^/:\s]+)", url, re.I)
    return m.group(1).lower() if m else url[:40]


def filename_pattern(fname: str) -> str:
    stem = os.path.splitext(fname)[0]
    # Normalize digits and hashes into pattern tokens
    p = re.sub(r"\b\d{8}\b", "<YYYYMMDD>", stem)
    p = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "<YYYY-MM-DD>", p)
    p = re.sub(r"\b[0-9A-Fa-f]{16,}\b", "<HEX>", p)
    p = re.sub(r"\d+", "<N>", p)
    return p[:48]


def parse_process_log(log_path: str, pdf_dir: str) -> Dict[str, Any]:
    matched: Dict[str, Dict[str, Any]] = {}
    unmatched: Dict[str, Dict[str, Any]] = {}
    if not os.path.isfile(log_path):
        return {"matched": matched, "unmatched": unmatched}

    with open(log_path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if "[process_pdf]" not in line:
                continue
            ts = line[:19] if re.match(r"^\d{4}-\d{2}-\d{2}", line) else ""
            if "[process_pdf] MATCH " in line:
                m_file = re.search(r"file=(.+?)\s+provider=(\S+)\s+period=(\S+)", line)
                m_std = re.search(r"standard_name=(\S+)\s+action=\"([^\"]+)\"", line)
                if m_file:
                    fname = m_file.group(1)
                    matched[fname] = {
                        "timestamp": ts,
                        "file": fname,
                        "provider": m_file.group(2),
                        "period": m_file.group(3),
                        "standard_name": m_std.group(1) if m_std else "-",
                        "action": m_std.group(2) if m_std else "-",
                    }
            elif "[process_pdf] SKIP " in line:
                m_path = re.search(r'path="([^"]+)"', line)
                m_dom = re.search(r"source_domain=(\S+)", line)
                m_reason = re.search(r"reason=(\S+)", line)
                m_file = re.search(r"SKIP\s+file=(.+?)(?:\s+path=|\s+provider=|\s+reason=)", line)
                if not m_file:
                    continue
                fname = m_file.group(1).strip()
                full_path = m_path.group(1) if m_path else os.path.join(pdf_dir, fname)
                dom = m_dom.group(1) if m_dom else "-"
                if dom == "-" and os.path.isfile(full_path):
                    urls = get_where_froms(full_path)
                    if urls:
                        dom = extract_domain(urls[0])
                unmatched[fname] = {
                    "timestamp": ts,
                    "file": fname,
                    "path": full_path,
                    "exists": os.path.isfile(full_path),
                    "source_domain": dom,
                    "pattern": filename_pattern(fname),
                    "reason": m_reason.group(1) if m_reason else "unmatched",
                }
    return {"matched": matched, "unmatched": unmatched}


def parse_hazel_log_errors(hazel_log_path: str) -> List[Dict[str, str]]:
    errors: List[Dict[str, str]] = []
    if not os.path.isfile(hazel_log_path):
        return errors
    seen = set()
    with open(hazel_log_path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            m_err = re.match(
                r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\.\d+\s+hazelworker\[\d+\]\s+\[Error\]\s+(.+)$",
                line,
            )
            if m_err:
                msg = m_err.group(2)
                if msg not in seen:
                    seen.add(msg)
                    errors.append({"timestamp": m_err.group(1), "error": msg})
    return errors


def inspect_for_provider(
    pdf_path: str,
    db,
    provider_name: Optional[str] = None,
    version_tag: Optional[str] = None,
) -> int:
    if not os.path.isfile(pdf_path):
        print(f"Error: PDF file not found: {pdf_path}", file=sys.stderr)
        return 1
    fname = os.path.basename(pdf_path)
    urls = get_where_froms(pdf_path)
    dom = extract_domain(urls[0]) if urls else None
    txt = PDFTextExtractor.extract_text(pdf_path, db=db)
    lines = [ln.strip() for ln in (txt or "").splitlines() if ln.strip() and ln.strip() != "=====PAGE====="]
    dates_found = re.findall(
        r"\b(?:\d{1,2}[ \t]+[A-Za-z]{3,9}[ \t]+\d{2,4}|\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})\b",
        txt or "",
    )
    amounts_found = re.findall(r"\$?\d{1,5}(?:,\d{3})*\.\d{2}(?:DR|CR)?", txt or "")

    # Infer a default version tag from earliest detected year or file mtime
    if not version_tag:
        yr = None
        for d in dates_found[:5]:
            m4 = re.search(r"\b(20\d{2})\b", d)
            if m4:
                yr = m4.group(1)
                break
            m2 = re.search(r"/(\d{2})$", d)
            if m2 and 0 <= int(m2.group(1)) <= 35:
                yr = f"20{m2.group(1)}"
                break
        if not yr:
            m_fn = re.search(r"\b(20\d{2})\b", fname)
            yr = m_fn.group(1) if m_fn else "2026"
        version_tag = f"v{yr}"

    print("═" * 88)
    print(f"PROVIDER CANDIDATE ANALYSIS: {fname}")
    print("═" * 88)
    print(f"  File Path:          {config.show_path(pdf_path)}")
    print(f"  File Size:          {os.path.getsize(pdf_path):,} bytes")
    print(f"  Filename Pattern:   {filename_pattern(fname)}")
    print(f"  WhereFroms Domain:  {dom or '(none)'}")
    if urls:
        print(f"  WhereFroms URL:     {urls[0][:100]}{'...' if len(urls[0]) > 100 else ''}")
    print(f"  Extracted Text:     {len(txt or ''):,} chars ({len(lines)} non-empty lines)")
    print(f"  Candidate Dates:    {', '.join(dict.fromkeys(dates_found[:8])) or '(none)'}")
    print(f"  Candidate Amounts:  {', '.join(dict.fromkeys(amounts_found[:8])) or '(none)'}")
    print(f"  Inferred Version:   {version_tag}")
    print("─" * 88)
    print("First 25 Lines of Extracted PDF Text:")
    for i, ln in enumerate(lines[:25], 1):
        print(f"  {i:2d}: {ln[:100]}")

    if provider_name:
        cls_name = "".join(w.capitalize() for w in re.split(r"[_\-\s]+", provider_name)) + "Provider"
        sample_preview = "\n".join(lines[:15])
        dom_list = [dom] if dom and dom != "-" else []
        print("\n" + "─" * 88)
        print(f"Scaffolded Multi-Version Provider Class (`{cls_name}` — `{version_tag}`):")
        print("─" * 88)
        print(f'''class {cls_name}(BasePDFProvider):
    name = "{provider_name.lower()}"
    display_name = "{provider_name.replace('_', ' ').title()}"
    doc_type = "{provider_name.lower()}"
    document_kind = "invoice"  # statement | invoice | receipt | payslip
    target_dir = "~/Library/Mobile Documents/com~apple~CloudDocs/Finance/{provider_name.title()}"
    source_domains = {dom_list!r}
    filename_template = "{provider_name.upper()}_{{date_compact}}.pdf"
    cadence = "monthly"
    format_versions = ["{version_tag}"]
    sample_text = """{sample_preview}"""
    sample_variants = {{
        "{version_tag}": sample_text,
        # Add newer/older format evolutions here (e.g. "v2021": "...", "v2026": "...")
        # run_self_test() automatically verifies every version in sample_variants!
    }}

    def detect_version(self, text: str, pdf_path: str) -> Optional[str]:
        t_low = text.lower()
        if "{provider_name.lower()}" in t_low:
            return "{version_tag}"
        return None

    def detect(self, text: str, pdf_path: str) -> bool:
        return self.detect_version(text, pdf_path) is not None
''')
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="audit_hazel_logs.py",
        description="Audit Hazel & process_pdf logs to inspect unmatched PDFs and build new providers",
    )
    p.add_argument("--process-log", default=DEFAULT_PROCESS_LOG, help="path to process_pdf.log")
    p.add_argument("--hazel-log", default=DEFAULT_HAZEL_LOG, help="path to Hazel.log")
    p.add_argument("--pdf-dir", default=DEFAULT_PDF_DIR, help="default watched PDF directory")
    p.add_argument("--unmatched", action="store_true", help="list all unmatched/skipped PDFs")
    p.add_argument("--matched", action="store_true", help="list all matched PDFs")
    p.add_argument("--domains", action="store_true", help="group unmatched PDFs by download source domain & pattern")
    p.add_argument("--errors", action="store_true", help="show recent Hazel.log shell/rule errors")
    p.add_argument("--sample", metavar="FILE_OR_SUBSTR", help="deep-dive inspect an unmatched PDF to design a provider")
    p.add_argument("--scaffold", metavar="PROVIDER_NAME", help="generate a BasePDFProvider skeleton (use with --sample)")
    p.add_argument("--version", dest="version_tag", metavar="VERSION_TAG", help="explicit format version tag for --scaffold (e.g. v2021, v2026)")
    p.add_argument("--limit", type=int, default=50, help="max rows to display in unmatched list (default 50, 0 for all)")
    p.add_argument("--json", action="store_true", help="output structured JSON")
    args = p.parse_args(argv)

    cfg = config.load()
    db = store.connect(cfg["db"])
    parsed = parse_process_log(args.process_log, args.pdf_dir)
    matched = parsed["matched"]
    unmatched = parsed["unmatched"]
    hazel_errors = parse_hazel_log_errors(args.hazel_log)

    if args.sample:
        target = os.path.expanduser(args.sample)
        if not os.path.isfile(target):
            cand = os.path.join(args.pdf_dir, args.sample)
            if os.path.isfile(cand):
                target = cand
            else:
                needle = args.sample.lower()
                found = next(
                    (u["path"] for u in unmatched.values() if needle in u["file"].lower() and u["exists"]),
                    None,
                )
                if found:
                    target = found
        return inspect_for_provider(
            target, db=db, provider_name=args.scaffold, version_tag=args.version_tag
        )

    if args.json:
        import json

        print(
            json.dumps(
                {
                    "matched_count": len(matched),
                    "unmatched_count": len(unmatched),
                    "matched": list(matched.values()),
                    "unmatched": list(unmatched.values()),
                    "hazel_errors": hazel_errors[-25:],
                },
                indent=2,
            )
        )
        return 0

    print(
        f"Hazel PDF Processor Audit: {len(matched)} matched | "
        f"{len(unmatched)} unmatched (candidates for new providers) | "
        f"{len(hazel_errors)} unique Hazel.log error(s)"
    )

    # Group unmatched by (source_domain, pattern)
    by_dom = collections.Counter()
    by_pat = collections.defaultdict(list)
    for u in unmatched.values():
        dom = u["source_domain"]
        by_dom[dom] += 1
        by_pat[(dom, u["pattern"])].append(u["file"])

    if args.domains or (not args.unmatched and not args.matched and not args.errors):
        if by_pat:
            print("\nTop Unmatched PDF Groups (by Download Domain & Filename Pattern):")
            grp_rows = []
            for (dom, pat), files in sorted(by_pat.items(), key=lambda kv: (-len(kv[1]), kv[0])):
                grp_rows.append([len(files), dom, pat, files[0]])
            table(
                grp_rows[:30],
                ["Count", "Download Domain (WhereFroms)", "Filename Pattern", "Example File"],
                right={0},
            )

    if args.matched:
        print(f"\nMatched PDFs ({len(matched)}):")
        m_rows = [
            [m["timestamp"], m["file"], m["provider"], m["period"], m["standard_name"], m["action"]]
            for m in matched.values()
        ]
        table(m_rows, ["Timestamp", "Source File", "Provider", "Period", "Standard Name", "Action"])

    if args.unmatched or (not args.domains and not args.matched and not args.errors):
        if unmatched:
            u_list = list(unmatched.values())
            shown = u_list if args.limit == 0 else u_list[: args.limit]
            print(f"\nUnmatched PDFs ({len(shown)} of {len(u_list)} shown):")
            u_rows = [
                [u["timestamp"], u["file"][:52], u["source_domain"], u["reason"]]
                for u in shown
            ]
            table(u_rows, ["Timestamp", "Unmatched File", "Source Domain (WhereFroms)", "Reason"])
            print(
                "\nTip: Inspect any unmatched PDF to create a provider:\n"
                "  ./audit_hazel_logs.py --sample \"<filename>\" --scaffold <provider_name>"
            )

    if args.errors and hazel_errors:
        print(f"\nRecent Unique Errors in Hazel.log ({len(hazel_errors[-20:])} shown):")
        e_rows = [[e["timestamp"], e["error"][:100]] for e in hazel_errors[-20:]]
        table(e_rows, ["Timestamp", "Hazel Error"])

    return 0


if __name__ == "__main__":
    sys.exit(main())
