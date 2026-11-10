"""Apple invoices and receipts (Apple Pty Ltd, Australia; plus overseas retail receipts).

Formats:
  1. v_retail_receipt   Retail store receipt emailed as a "Duplicate Tax Invoice"
                        (header "Apple Store, <City>" or "Apple <Location>", "*R##########*").
  2. v_online_classic   Online store tax invoice ("Reference No.", "Reference Date", "Weborder Number").
  3. v_online_receipt   Online store "Invoice Receipt" ("Invoice Number / Invoice Date", order W#########).

Standard filenames (all under {invoices}/Apple):
  Apple_Store_Receipt_{YYYYMMDD}_{R-number}.pdf   (Apple_Store_Refund_... for a refund receipt)
  Apple_Invoice_{YYYYMMDD}_{reference or invoice number}.pdf
  Apple_Credit_Note_{YYYYMMDD}_{reference}.pdf
"""
import datetime
import os
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .. import paths
from .base import BasePDFProvider

MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def _clean(text: str) -> str:
    # Apple's retail receipts use non-breaking / private-use characters between words.
    return re.sub("[\ufffd\uffff\uf8ff\u00a0\u2007\u202f]", " ", text or "")


def _classic_ref(t: str) -> Optional[str]:
    """Reference number of a classic online invoice: 10 digits, or two letters + 8 digits (2024+)."""
    m = (re.search(r"Reference No\.\s*(\d{10}|[A-Z]{2}\d{8})\b", t)
         or re.search(r"(?m)^\s*(\d{10}|[A-Z]{2}\d{8})\s*$", t))
    return m.group(1) if m else None


def _money(s: str) -> Decimal:
    try:
        return Decimal(re.sub(r"[^\d.\-]", "", s))
    except Exception:
        return Decimal("0.00")


def _month_date(month: str, day: str, year: str) -> Optional[datetime.date]:
    mo = MONTHS.get(month[:3].lower())
    try:
        return datetime.date(int(year), mo, int(day)) if mo else None
    except ValueError:
        return None


class AppleProvider(BasePDFProvider):
    name: str = "apple"
    display_name: str = "Apple (invoices & receipts)"
    doc_type: str = "apple_invoice"
    document_kind: str = "invoice"
    enabled: bool = True
    cadence: str = "irregular"
    target_dir: Optional[str] = paths.under("invoices", "Apple")
    scan_dirs: List[str] = [paths.under("invoices", "Apple"), paths.get("pdf")]
    source_domains: List[str] = ["apple.com", "store.apple.com"]
    filename_template: Optional[str] = None  # built in default_filename (depends on format)
    format_versions: List[str] = ["v_retail_receipt", "v_online_classic", "v_online_receipt"]

    sample_variants: Dict[str, str] = {
        "v_retail_receipt": """
 DUPLICATE TAX INVOICE
A
Apple Store, Exampleville
Apple Pty Ltd
1 Example St
Exampleville NSW 2000
ABN : 46 002 510 054
March 05, 2012: 13:08 PM
Customer  SAMPLE PERSON
Sample Dock
Part Number: XX000X/A
Unit Price  GST%  Qty
$ 90.00  10.0%  1
GST Ex. Price  GST  Total
$ 90.00  $ 9.00  $ 99.00
Total Tender   $ 99.00
*R0000000001*
        """,
        "v_online_classic": """
Apple Pty Ltd
Incorporated in New South Wales A.C.N.002 510 054 A.B.N.46 002 510 054  Page 1 of Your TAX INVOICE
Reference No. 0000000002
Sold To Customer  Ship To Customer  Reference Date 07/07/13
Sample Person  100001
Product No.  Product Description & Serial No.s  Quantity
X0000X/A  SAMPLE PROGRAM  1  0  1  90.00  90.00  10.00  9.00  99.00
Weborder Number: W000000001
 Special  90.00  9.00  99.00
 Instructions  Total Value (Excl.GST)  Total GST  Total Price (Incl. GST)
        """,
        "v_online_receipt": """
Apple Store
Invoice Receipt
Invoice Number  Invoice Date
MA00000001  25 September 2023
Order Number:  Order Date:
W0000000001  25 September 2023
Sold To:  Location:
Sample Person  Apple Exampleville
Order Details
Sample Phone  1  1  A$1,999.00
For a total of A$1,999.00
        """,
    }

    def detect_version(self, text: str, pdf_path: str = "") -> Optional[str]:
        # Text extractors differ in how they order columns, so match on labels rather than layout.
        t = _clean(text)
        if "Invoice Receipt" in t and "Invoice Number" in t and "Invoice Date" in t and "Apple" in t:
            return "v_online_receipt"
        if ("Reference No." in t and "Reference Date" in t and _classic_ref(t)
                and re.search(r"Weborder Number|Total Price \(Incl\. GST\)", t)):
            return "v_online_classic"
        if re.search(r"\*R\d{10}\*", t) and re.search(r"^\s*Apple (Store, )?[A-Z]", t, re.M) and "Total" in t:
            return "v_retail_receipt"
        return None

    def detect(self, text: str, pdf_path: str = "") -> bool:
        return self.detect_version(text, pdf_path) is not None

    def extract(self, text: str, pdf_path: str = "") -> Dict[str, Any]:
        t = _clean(text)
        ver = self.detect_version(t, pdf_path)
        data: Dict[str, Any] = {
            "provider": self.name, "doc_type": self.doc_type, "document_kind": self.document_kind,
            "format_version": ver, "company": "Apple", "accounts": [], "transactions": [],
            "warnings": [], "reconciled": False,
        }
        issue: Optional[datetime.date] = None
        ref: Optional[str] = None
        total = Decimal("0.00")

        if ver == "v_retail_receipt":
            m = re.search(r"\b([A-Z][a-z]+) (\d{1,2}), (20\d\d)", t)
            issue = _month_date(*m.groups()) if m else None
            m = re.search(r"\*(R\d{10})\*", t)
            ref = m.group(1) if m else None
            # Some extractors split digits ("$ 1 21.00"), so allow spaces inside the amount.
            m = (re.search(r"Total Tender\s+\$\s*([\d, ]*\d ?\.\d{2})", t)
                 or re.search(r"(?m)^\s*Total\s+\$\s*([\d, ]*\d ?\.\d{2})\s*$", t))  # overseas stores
            total = _money(m.group(1)) if m else total
            data["filename_stem"] = "Apple_Store_Receipt"
            # A tender line proves the receipt is complete, even at $0.00 (exchange, trade-in, pickup).
            data["reconciled"] = m is not None
            if "Refunded to original tender" in t:
                data["filename_stem"] = "Apple_Store_Refund"
                data["reconciled"] = True
        elif ver == "v_online_classic":
            ref = _classic_ref(t)
            m = (re.search(r"Reference Date\s+(\d{2})/(\d{2})/(\d{2,4})", t)
                 or re.search(r"(?m)^\s*(\d{2})/(\d{2})/(\d{2,4})\s*$", t))
            if m:
                y = int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)
                try:
                    issue = datetime.date(y, int(m.group(2)), int(m.group(1)))
                except ValueError:
                    issue = None
            m = re.search(
                r"(?:Special|Instructions)\s+([\d,]+\.\d{2})(-?)\s+([\d,]+\.\d{2})-?\s+([\d,]+\.\d{2})-?", t)
            data["filename_stem"] = "Apple_Invoice"
            if m:
                gst, total = _money(m.group(3)), _money(m.group(4))
                if m.group(2):  # trailing minus signs: a credit note / refund
                    data["filename_stem"] = "Apple_Credit_Note"
                    total = -total
                # GST is at most one eleventh of a GST-inclusive amount.
                data["reconciled"] = abs(total) > 0 and Decimal("0") <= gst <= abs(total) / 11 + Decimal("0.05")
        elif ver == "v_online_receipt":
            m = re.search(r"Invoice Number\s+(?:Invoice Date\s+)?([A-Z]{2}\d{8})\b", t)
            ref = m.group(1) if m else None
            m = re.search(r"Invoice Date\s+(?:[A-Z]{2}\d{8}\s+)?(\d{1,2}) ([A-Z][a-z]+) (20\d\d)", t)
            issue = _month_date(m.group(2), m.group(1), m.group(3)) if m else None
            m = re.search(r"For a total of A?\$\s*([\d,]+\.\d{2})", t)
            total = _money(m.group(1)) if m else total
            data["reconciled"] = total > 0
            data["filename_stem"] = "Apple_Invoice"

        if issue:
            data["issue_date"] = issue.isoformat()
            data["period_start"] = data["period_end"] = issue.isoformat()
            data["date_compact"] = data["period_end_compact"] = issue.strftime("%Y%m%d")
        data["reference"] = ref
        data["total"] = total
        if not ref:
            data["reconciled"] = False  # the reference is part of the filename; without it we cannot file safely
        return data

    def default_filename(self, data: Dict[str, Any], original_path: str) -> str:
        current = os.path.basename(original_path or "")
        if not (data.get("date_compact") and data.get("reference") and data.get("filename_stem")):
            return current
        stem = f"{data['filename_stem']}_{data['date_compact']}_{data['reference']}"
        if re.fullmatch(re.escape(stem) + r"(_\d{2})?\.pdf", current):
            return current  # keep a duplicate suffix
        return stem + ".pdf"

    def run_self_test(self, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        results: Dict[str, Any] = {"provider": self.name, "ok": True, "variants": {}}
        expect = {
            "v_retail_receipt": "Apple_Store_Receipt_20120305_R0000000001.pdf",
            "v_online_classic": "Apple_Invoice_20130707_0000000002.pdf",
            "v_online_receipt": "Apple_Invoice_20230925_MA00000001.pdf",
        }
        for ver, sample in self.sample_variants.items():
            d = self.extract(sample)
            name = self.default_filename(d, "sample.pdf")
            ok = self.detect_version(sample) == ver and d.get("reconciled") and name == expect[ver]
            results["variants"][ver] = {"ok": bool(ok), "filename": name, "total": str(d.get("total"))}
            if not ok:
                results["ok"] = False
        return results
