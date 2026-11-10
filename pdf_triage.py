#!/usr/bin/env python3
"""pdf_triage.py - place PDFs no handler filed, and show what is waiting.

  pdf_triage.py status [--json]                      counts + oldest waiting file per folder
  pdf_triage.py place [--dry-run] [--held REASON] F  file one PDF (used by process_pdf.sh)
  pdf_triage.py sweep [--dry-run] [--resort] [--dir D]
                                                     run every waiting PDF through process_pdf.sh
Dry run is the default for `sweep`; pass --apply to move files.
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pdfflow import triage  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    p = sub.add_parser("place")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--held", metavar="REASON")
    p.add_argument("files", nargs="+")
    w = sub.add_parser("sweep")
    w.add_argument("--apply", action="store_true", help="move files (default is a dry run)")
    w.add_argument("--resort", action="store_true", help="also re-run unknown_type and needs_review")
    w.add_argument("--dir", action="append", default=[], help="extra folder (inside PDF/) to sweep")
    w.add_argument("--limit", type=int, default=0)
    a = ap.parse_args(argv)

    if a.cmd == "status":
        s_ = triage.summary()
        print(json.dumps(s_, indent=2)) if a.json else triage.print_status(s_)
        return 0
    if a.cmd == "place":
        for f in a.files:
            print(triage.format_record(triage.place(f, a.held, dry_run=a.dry_run)))
        return 0
    files = triage.candidates(a.resort, a.dir)
    if a.limit:
        files = files[: a.limit]
    print(f"[triage] sweep files={len(files)} mode={'APPLY' if a.apply else 'DRY-RUN'}", flush=True)
    rc = 0
    for i in range(0, len(files), 25):
        cmd = [os.path.join(ROOT, "process_pdf.sh"), "--no-wait"] + ([] if a.apply else ["--dry-run"]) + files[i:i + 25]
        r = subprocess.run(cmd)
        rc = max(rc, r.returncode)
    return rc


if __name__ == "__main__":
    sys.exit(main())
