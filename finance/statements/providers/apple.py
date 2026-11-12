"""Apple invoices and receipts (Apple Pty Ltd, Australia; plus overseas retail receipts).

Formats:
  1. v_retail_receipt   Retail store receipt emailed as a "Duplicate Tax Invoice"
                        (header "Apple Store, <City>" or "Apple <Location>", "*R##########*").
  2. v_online_classic   Online store tax invoice ("Reference No.", "Reference Date", "Weborder Number").
  3. v_online_receipt   Online store "Invoice Receipt" ("Invoice Number / Invoice Date", order W#########).
  4. v_tax_invoice      Apple Pty Limited "Tax Invoice" PDF (2021+): "Tax Invoice Number", MM/DD/YYYY date.
  Email receipts (rendered from the email, not original Apple PDFs; filed in {invoices}/Apple/Email_Receipts):
  5. v_email_itunes     "Billed To" + Order ID/Number + Issue Date + Order Total (iTunes / App Store).
  6. v_email_order      Store order confirmation ("Ordered on", "Order Number", "Order Total").
  7. v_email_applecare  AppleCare+ "Tax Invoice / Receipt & Renewal Notice".
  8. v_email_adjust     "Adjustment Note" (cancellation / credit).
  9. v_email_appstore   "Tax Invoice" with APPLE ID / DATE / ORDER ID / DOCUMENT NO. / TOTAL (subscriptions, 2021-2024).
 10. v_email_appstore_new  "Tax Invoice", then date, "Order ID:", "Document:", "Account Balance" (2025+).
 11. v_japan_invoice   Apple Japan, Inc. invoice (Japanese labels, amounts in JPY; data["currency"] = "JPY").

Standard filenames (all under {invoices}/Apple):
  Apple_Store_Receipt_{YYYYMMDD}_{R-number}.pdf   (Apple_Store_Refund_... for a refund receipt)
  Apple_Invoice_{YYYYMMDD}_{reference or invoice number}.pdf
  Apple_Credit_Note_{YYYYMMDD}_{reference}.pdf
Email receipts: Apple_Receipt_ / Apple_Order_Confirmation_ / Apple_Adjustment_Note_{YYYYMMDD}_{id}.pdf
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


EMAIL_SUBDIR = "Email_Receipts"
_DMY = r"(\d{1,2})/(\d{1,2})/(\d{2,4})"


def _dmy(m) -> Optional[datetime.date]:
    y = int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)
    try:
        return datetime.date(y, int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def _amount(t: str, pattern: str) -> Optional[Decimal]:
    """Dollar amount after a label; tolerates extractors that split digits ("$13.9 9")."""
    m = re.search(pattern + r"\s*-?\s*A?\$\s*(\d[\d,\s]*?)\s*\.\s*(\d)\s*(\d)\b", t)
    return Decimal(re.sub(r"[,\s]", "", m.group(1)) + "." + m.group(2) + m.group(3)) if m else None


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
        "v_email_appstore": """
Tax Invoice

APPLE ID
sample@example.com BILLED TO
Card .... 0000
Sample Person
DATE
02 Jun. 2024
ORDER ID
ML0000AAAA
DOCUMENT NO.
000000000001
Sample Service
Monthly
$14.9 9
TOTAL $14.9 9
Apple Pty Ltd. Total Includes GST.
        """,
        "v_email_appstore_new": """
Tax Invoice
10 November 2025
Order ID:ML0000BBBB
Document:000000000002
Apple Account:sample@example.com
Sample Service (Monthly)
$44.99
Billing and Payment
Sample Person
Account Balance $44.99
        """,
        "v_japan_invoice": """
納品書
Apple Japan, Inc.
ご請求番号: AJ00000001
ご請求⽇: 09/16/2022
ご請求⾦額: 1,100 円
製品名 数量 単価 ⼩計
Sample Accessory 1 1,000 1,000
⼩計 JPY 1,000 消費税 JPY 100 合計 JPY 1,100
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
        if "Apple Japan" in t and re.search(r"[A-Z]{2}\d{8}", t) and "JPY" in t and "請求番号" in t:
            return "v_japan_invoice"
        if re.search(r"Tax Invoice Number:\s*\S+", t) and "Tax Invoice Date:" in t and "Apple Pty" in t:
            return "v_tax_invoice"
        if re.search(r"(Tax Invoice|Receipt) & Renewal Notice", t) and re.search(r"Document:\s*\d", t):
            return "v_email_applecare"
        if re.search(r"Adjustment Note", t[:300]) and re.search(r"DOCUMENT NO\.\s*\d", t):
            return "v_email_adjust"
        if (re.match(r"\s*Tax Invoice\s", t) and re.search(r"ORDER ID", t) and re.search(r"DOCUMENT NO\.\s*\d", t)
                and re.search(r"(?m)^DATE\b", t)):
            return "v_email_appstore"
        if re.match(r"\s*Tax Invoice\s", t) and re.search(r"Order ID:\s*[A-Z0-9]+", t) and re.search(r"Document:\s*\d", t):
            return "v_email_appstore_new"
        if re.search(r"Ordered on:\s*" + _DMY, t) and re.search(r"Order Number:\s*[A-Z]?\d+", t):
            return "v_email_order"
        if ("Billed To" in t[:200] and re.search(r"Order (Number|ID):\s*[A-Z0-9]+", t)
                and re.search(r"Issue Date:\s*" + _DMY, t)):
            return "v_email_itunes"
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
            "warnings": [], "reconciled": False, "currency": "AUD",
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

        elif ver == "v_tax_invoice":
            ref = re.search(r"Tax Invoice Number:\s*([A-Z]{2}\d{8}|\d{10})\b", t)
            ref = ref.group(1) if ref else None
            m = re.search(r"Tax Invoice Date:\s*(\d{2})/(\d{2})/(\d{4})", t)  # US order MM/DD/YYYY
            if m:
                try:
                    issue = datetime.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
                except ValueError:
                    issue = None
            # amounts here have no "$" sign, so read them directly
            def _aud(label):
                mm = re.search(label + r"\s*AUD\s*(-?[\d,]+\.\d{2})", t)
                return Decimal(mm.group(1).replace(",", "")) if mm else None
            ex, gst, inc = _aud(r"Total Value \(Excl\.GST\)"), _aud(r"(?<!\()GST"), _aud(r"Total Value \(Incl\.GST\)")
            total = inc if inc is not None else total
            data["reconciled"] = None not in (ex, gst, inc) and abs(ex + gst - inc) <= Decimal("0.02")
            data["filename_stem"] = "Apple_Invoice"
            if inc is not None and inc < 0:
                data["filename_stem"] = "Apple_Credit_Note"
        elif ver == "v_japan_invoice":
            # Labels are Japanese; the extractor may return Kangxi-radical look-alikes (⽇, ⼩), so match loosely.
            m = re.search(r"請求番号:\s*([A-Z]{2}\d{8})", t)
            ref = m.group(1) if m else None
            m = re.search(r"請求.:\s*(\d{2})/(\d{2})/(\d{4})", t)  # MM/DD/YYYY
            if m:
                try:
                    issue = datetime.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
                except ValueError:
                    issue = None
            m = re.search(r".計\s*JPY\s*([\d,]+)\s*消費税\s*JPY\s*([\d,]+)\s*合計\s*JPY\s*([\d,]+)", t)
            data["currency"] = "JPY"
            if m:
                sub, tax, tot = (Decimal(x.replace(",", "")) for x in m.groups())
                total = tot
                data["reconciled"] = sub + tax == tot and tot > 0
            data["filename_stem"] = "Apple_Invoice"
        elif ver == "v_email_itunes":
            m = re.search(r"Order (?:Number|ID):\s*([A-Z0-9]+)", t)
            ref = m.group(1) if m else None
            m = re.search(r"Issue Date:\s*" + _DMY, t)
            issue = _dmy(m) if m else None
            tot = _amount(t, r"Order Total:")
            total = tot if tot is not None else total
            data["reconciled"] = tot is not None
            data["filename_stem"] = "Apple_Receipt"
            data["email_receipt"] = True
        elif ver == "v_email_order":
            m = re.search(r"Order Number:\s*([A-Z]?\d+)", t)
            ref = m.group(1) if m else None
            m = re.search(r"Ordered on:\s*" + _DMY, t)
            issue = _dmy(m) if m else None
            tot = _amount(t, r"Order Total")
            total = tot if tot is not None else total
            data["reconciled"] = tot is not None
            data["filename_stem"] = "Apple_Order_Confirmation"
            data["email_receipt"] = True
        elif ver in ("v_email_appstore", "v_email_appstore_new"):
            if ver == "v_email_appstore":
                m = re.search(r"DOCUMENT NO\.\s*(\d+)", t)
                dm = re.search(r"(?m)^DATE\s+(\d{1,2}) ([A-Z][a-z]{2,8})\.? (20\d\d)", t)
                tot = _amount(t, r"TOTAL")
            else:
                m = re.search(r"Document:\s*(\d+)", t)
                dm = re.search(r"(\d{1,2}) ([A-Z][a-z]{2,8})\.? (20\d\d)", t)
                tot = _amount(t, r"Account Balance") or _amount(t, r"")
            ref = m.group(1) if m else None
            issue = _month_date(dm.group(2), dm.group(1), dm.group(3)) if dm else None
            total = tot if tot is not None else total
            data["reconciled"] = tot is not None
            data["filename_stem"] = "Apple_Receipt"
            data["email_receipt"] = True
        elif ver in ("v_email_applecare", "v_email_adjust"):
            m = re.search(r"Document(?: NO\.|:)?\s*:?\s*(\d{8,})", t) or re.search(r"DOCUMENT NO\.\s*(\d+)", t)
            ref = m.group(1) if m else None
            m = re.search(r"(\d{1,2}) ([A-Z][a-z]{2,8})\.? (20\d\d)", t)
            issue = _month_date(m.group(2), m.group(1), m.group(3)) if m else None
            if ver == "v_email_applecare":
                tot = _amount(t, r"")
                data["filename_stem"] = "Apple_Receipt"
            else:
                tot = _amount(t, r"TOTAL")
                if tot is not None:
                    tot = -tot
                data["filename_stem"] = "Apple_Adjustment_Note"
            total = tot if tot is not None else total
            data["reconciled"] = tot is not None
            data["email_receipt"] = True

        if issue:
            data["issue_date"] = issue.isoformat()
            data["period_start"] = data["period_end"] = issue.isoformat()
            data["date_compact"] = data["period_end_compact"] = issue.strftime("%Y%m%d")
        data["reference"] = ref
        data["total"] = total
        if not ref:
            data["reconciled"] = False  # the reference is part of the filename; without it we cannot file safely
        return data

    def resolve_target_dir(self, data, original_path, cfg=None):
        base = super().resolve_target_dir(data, original_path, cfg)
        if base and data.get("email_receipt"):
            return os.path.join(base, EMAIL_SUBDIR)
        return base

    def default_filename(self, data: Dict[str, Any], original_path: str) -> str:
        current = os.path.basename(original_path or "")
        if not (data.get("date_compact") and data.get("reference") and data.get("filename_stem")):
            return current
        c = data["date_compact"]
        stem = f"{data['filename_stem']}_{c[:4]}-{c[4:6]}-{c[6:8]}_{data['reference']}"  # ISO date in names
        if re.fullmatch(re.escape(stem) + r"(_\d{2})?\.pdf", current):
            return current  # keep a duplicate suffix
        return stem + ".pdf"

    def run_self_test(self, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        results: Dict[str, Any] = {"provider": self.name, "ok": True, "variants": {}}
        expect = {
            "v_retail_receipt": "Apple_Store_Receipt_2012-03-05_R0000000001.pdf",
            "v_online_classic": "Apple_Invoice_2013-07-07_0000000002.pdf",
            "v_online_receipt": "Apple_Invoice_2023-09-25_MA00000001.pdf",
            "v_japan_invoice": "Apple_Invoice_2022-09-16_AJ00000001.pdf",
            "v_email_appstore": "Apple_Receipt_2024-06-02_000000000001.pdf",
            "v_email_appstore_new": "Apple_Receipt_2025-11-10_000000000002.pdf",
        }
        for ver, sample in self.sample_variants.items():
            d = self.extract(sample)
            name = self.default_filename(d, "sample.pdf")
            ok = self.detect_version(sample) == ver and d.get("reconciled") and name == expect[ver]
            results["variants"][ver] = {"ok": bool(ok), "filename": name, "total": str(d.get("total"))}
            if not ok:
                results["ok"] = False
        return results
