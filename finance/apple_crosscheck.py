#!/usr/bin/env python3
"""apple_crosscheck.py - optional: match Apple invoices to bank/card transactions.

Foreign-currency invoices (e.g. Apple Japan, JPY) are converted to AUD with the rough rate in
<records>/fx_rates.json, then looked up in the transactions database within a date window and
an amount tolerance (card FX margin and fees make the bank amount differ by a few percent).
Read-only: nothing is renamed, moved or written. Use --all to also check AUD invoices.

  apple_crosscheck.py [--folder DIR] [--days 7] [--tolerance 0.06] [--all] [--json]
"""
import argparse
import datetime
import json
import os
import sqlite3
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from statements import config, fx, paths  # noqa: E402
from statements.money import D  # noqa: E402
from statements.pdf import PDFTextExtractor  # noqa: E402
from statements.providers import AVAILABLE_PROVIDERS  # noqa: E402


def apple_documents(folder, include_aud):
    prov = next(p for p in AVAILABLE_PROVIDERS if p.name == "apple")
    for dp, dn, fn in os.walk(folder):
        dn[:] = [d for d in dn if d != "Duplicates"]
        for n in sorted(fn):
            if not n.lower().endswith(".pdf"):
                continue
            p = os.path.join(dp, n)
            text = PDFTextExtractor.extract_text(p) or ""
            if not prov.detect(text, p):
                continue
            d = prov.extract(text, p)
            if not d.get("reconciled") or not d.get("issue_date"):
                continue
            if d.get("currency", "AUD") == "AUD" and not include_aud:
                continue
            yield {"file": n, "date": d["issue_date"], "currency": d.get("currency", "AUD"),
                   "total": D(d.get("total")), "ref": d.get("reference")}


def candidates(db, day, days, aud, tol, any_desc):
    d0 = datetime.date.fromisoformat(day)
    lo, hi = (d0 - datetime.timedelta(days=days)).isoformat(), (d0 + datetime.timedelta(days=days)).isoformat()
    out = []
    for r in db.execute("SELECT tx_date, description, debit, amount, account_name FROM transactions "
                        "WHERE tx_date BETWEEN ? AND ?", (lo, hi)):
        amt = abs(D(r["debit"])) or abs(D(r["amount"]))
        if amt <= 0 or abs(amt - aud) > aud * Decimal(str(tol)) + Decimal("0.02"):
            continue
        apple = "APPLE" in (r["description"] or "").upper()
        if apple or any_desc:
            out.append({"date": r["tx_date"], "description": (r["description"] or "")[:48],
                        "amount": str(amt), "account": r["account_name"], "apple": apple})
    return sorted(out, key=lambda c: (not c["apple"], abs(Decimal(c["amount"]) - aud)))


def run(folder, db, days=7, tol=0.06, include_aud=False, any_desc=False, rates=None):
    rows = []
    for doc in apple_documents(folder, include_aud):
        est = fx.to_aud(doc["total"], doc["currency"], doc["date"], rates)
        row = {**doc, "total": str(doc["total"]), "aud_estimate": None, "rate": None, "status": "no_rate", "matches": []}
        if est:
            tol_here = tol if doc["currency"] != "AUD" else 0.0
            row["aud_estimate"], row["rate"] = str(est["aud"]), str(est["rate"])
            row["matches"] = candidates(db, doc["date"], days, est["aud"], tol_here, any_desc)
            row["status"] = {0: "unmatched", 1: "matched"}.get(len(row["matches"]), "ambiguous")
        rows.append(row)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--folder", default=paths.under("invoices", "Apple"))
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--tolerance", type=float, default=0.06)
    ap.add_argument("--all", action="store_true", help="also check AUD invoices (exact amount)")
    ap.add_argument("--any-description", action="store_true", help="do not require APPLE in the bank description")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    cfg = config.load()
    if not os.path.exists(cfg["db"]):
        print(f"No transactions database at {paths.show(cfg['db'])}; run sync first.", file=sys.stderr)
        return 2
    db = sqlite3.connect(f"file:{cfg['db']}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = run(a.folder, db, a.days, a.tolerance, a.all, a.any_description)
    if a.json:
        print(json.dumps(rows, indent=2))
        return 0
    for r in rows:
        est = f"~A${r['aud_estimate']} @ {r['rate']}" if r["aud_estimate"] else "no rate in fx_rates.json"
        print(f"{r['status']:10s} {r['date']}  {r['currency']} {r['total']:>9s}  ({est})  {r['file']}")
        for m in r["matches"][:3]:
            print(f"             -> {m['date']}  A${m['amount']}  {m['description']}  [{m['account']}]")
    print(f"\n{len(rows)} document(s): " + ", ".join(
        f"{k}={sum(1 for r in rows if r['status'] == k)}" for k in ("matched", "ambiguous", "unmatched", "no_rate")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
