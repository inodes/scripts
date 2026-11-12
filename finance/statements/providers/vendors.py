"""Everyday vendor invoices and receipts: telco, broadband, hardware store, app stores.

One small provider per vendor on a shared base. Each vendor lists its known layouts
(`detect_version` -> `parse`), every one of which must yield a date, a reference and a total to be
treated as complete; anything else is held (see ingest.hold_reason) and never renamed or moved.

Standard filenames:  {Stem}_{YYYYMMDD}_{reference}.pdf  under {invoices}/<Vendor>[/<subfolder>]
Rendered email bodies (not original PDFs) go to a sub-folder so originals stay separate.

  AussieBBProvider   Invoice (Tax Invoice, "Invoice #"), Payment Receipt, rendered "TAX INVOICE"
  TelstraProvider    Bill ("Tax Invoice ... Bill number", ABN 33 051 775 556)
  VodafoneProvider   The "email bill" notice
  BunningsProvider   Digital receipt (store, date/time, total)
  GooglePlayProvider Order receipt ("Order number", "Order date")
"""
import datetime
import os
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .. import paths
from .apple import MONTHS, _clean
from .base import BasePDFProvider


def _num(s: str) -> Decimal:
    try:
        return Decimal(re.sub(r"[^\d.\-]", "", s))
    except Exception:
        return Decimal("0.00")


def _date(y: int, m: int, d: int) -> Optional[datetime.date]:
    try:
        return datetime.date(y, m, d)
    except ValueError:
        return None


def _dmy_slash(m) -> Optional[datetime.date]:  # dd/mm/yyyy (or yy)
    y = int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)
    return _date(y, int(m.group(2)), int(m.group(1)))


def _d_mon_y(day: str, mon: str, year: str) -> Optional[datetime.date]:
    mo = MONTHS.get(mon[:3].lower())
    return _date(int(year), mo, int(day)) if mo else None


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9\-]+", "-", s).strip("-")


class VendorProvider(BasePDFProvider):
    document_kind: str = "invoice"
    enabled: bool = True
    cadence: str = "irregular"
    vendor_dir: str = ""
    filename_template: Optional[str] = None
    sample_expect: Dict[str, str] = {}

    @property
    def target_dir(self) -> Optional[str]:  # type: ignore[override]
        return paths.under("invoices", self.vendor_dir)

    @property
    def scan_dirs(self) -> List[str]:  # type: ignore[override]
        return [paths.under("invoices", self.vendor_dir), paths.get("pdf")]

    def versions(self, t: str) -> Optional[str]:
        raise NotImplementedError

    def parse(self, ver: str, t: str) -> Dict[str, Any]:
        raise NotImplementedError

    def detect_version(self, text: str, pdf_path: str = "") -> Optional[str]:
        return self.versions(_clean(text))

    def detect(self, text: str, pdf_path: str = "") -> bool:
        return self.detect_version(text, pdf_path) is not None

    def extract(self, text: str, pdf_path: str = "") -> Dict[str, Any]:
        t = _clean(text)
        ver = self.versions(t)
        data: Dict[str, Any] = {
            "provider": self.name, "doc_type": self.doc_type, "document_kind": self.document_kind,
            "format_version": ver, "company": self.display_name, "accounts": [], "transactions": [],
            "warnings": [], "reconciled": False, "currency": "AUD",
        }
        if not ver:
            return data
        r = self.parse(ver, t)
        issue: Optional[datetime.date] = r.get("issue")
        if issue:
            data["issue_date"] = issue.isoformat()
            data["period_start"] = data["period_end"] = issue.isoformat()
            data["date_compact"] = data["period_end_compact"] = issue.strftime("%Y%m%d")
        data["reference"] = r.get("ref")
        data["total"] = r.get("total")
        data["filename_stem"] = r.get("stem")
        data["subdir"] = r.get("subdir")
        data["email_receipt"] = bool(r.get("subdir"))
        data["reconciled"] = bool(issue and r.get("ref") and r.get("total") is not None and r.get("ok", True))
        return data

    def resolve_target_dir(self, data, original_path, cfg=None):
        base = super().resolve_target_dir(data, original_path, cfg)
        return os.path.join(base, data["subdir"]) if base and data.get("subdir") else base

    def default_filename(self, data: Dict[str, Any], original_path: str) -> str:
        current = os.path.basename(original_path or "")
        if not (data.get("date_compact") and data.get("reference") and data.get("filename_stem")):
            return current
        c = data["date_compact"]
        stem = f"{data['filename_stem']}_{c[:4]}-{c[4:6]}-{c[6:8]}_{data['reference']}"  # ISO date in names
        if re.fullmatch(re.escape(stem) + r"(_\d{2})?\.pdf", current):
            return current
        return stem + ".pdf"

    def run_self_test(self, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        results: Dict[str, Any] = {"provider": self.name, "ok": True, "variants": {}}
        for ver, sample in self.sample_variants.items():
            d = self.extract(sample)
            name = self.default_filename(d, "sample.pdf")
            ok = self.detect_version(sample) == ver and d.get("reconciled") and name == self.sample_expect[ver]
            results["variants"][ver] = {"ok": bool(ok), "filename": name, "total": str(d.get("total"))}
            if not ok:
                results["ok"] = False
        return results


class AussieBBProvider(VendorProvider):
    name = "aussiebb"
    display_name = "Aussie Broadband"
    doc_type = "aussiebb_invoice"
    vendor_dir = "Aussie Broadband"
    source_domains = ["aussiebroadband.com.au"]
    format_versions = ["v_invoice", "v_invoice_2024", "v_payment_receipt", "v_tax_invoice_text", "v_email_notice"]
    sample_variants = {
        "v_invoice": """
 Tax Invoice
 Invoice #: 0000001
 Issue Date: 07/10/2019
 Debit Date: 21/10/2019
 Opening Balance Payments/Credits This Invoice Total Due
 $0.00 - $0.00 + $99.00 = $99.00
 This invoice includes $9.00 GST
 Aussie Broadband Pty Ltd
 ABN 29 132 090 192
""",
        "v_invoice_2024": """
 Customer: Sample Person Invoice Number: 00000004
 Your Aussie Broadband Tax Invoice
 Billing Period: 2 May 2024 - 1 Jun 2024
 Amount Due
 $129.00
 This invoice includes $11.73 GST
 Issue Date Invoice Number Customer Number
 2 May 2024 00000004 0000000
 Aussie Broadband Limited ABN 29 132 090 192
""",
        "v_email_notice": """
Aussie Broadband Tax Invoice (02/03/2021)
Dear Sample, Your Aussie Broadband invoice (#00000005) is attached for your records.
Total Due: $99.00
This will be paid by Direct Debit on 17-03-2021.
""",
        "v_payment_receipt": """
PAYMENT RECEIPT
17-11-2020
Payment Total: $99.00
Payment Method: Direct Debit
 INVOICE #0000002 $99.00
 Total: $99.00
 Aussie Broadband Limited
 ABN 29 132 090 192
""",
        "v_tax_invoice_text": """
TAX INVOICE
Aussie Broadband Pty Ltd
ABN 29 132 090 192
2019-12-02
Customer Number: 0000000
This Invoice: $99.00
 New Invoice #0000003 Due Date 2019-12-16
This invoice includes $9.00 GST.
""",
    }
    sample_expect = {
        "v_invoice": "ABB_Invoice_2019-10-07_0000001.pdf",
        "v_invoice_2024": "ABB_Invoice_2024-05-02_00000004.pdf",
        "v_email_notice": "ABB_Invoice_Notice_2021-03-02_00000005.pdf",
        "v_payment_receipt": "ABB_Payment_Receipt_2020-11-17_0000002.pdf",
        "v_tax_invoice_text": "ABB_Invoice_2019-12-02_0000003.pdf",
    }

    def versions(self, t):
        if "Aussie Broadband" not in t and "AUSSIE BROADBAND" not in t.upper():
            return None
        if re.search(r"PAYMENT RECEIPT", t) and re.search(r"INVOICE #\d+", t) and "Payment Total:" in t:
            return "v_payment_receipt"
        if re.search(r"Your Aussie Broadband Tax Invoice", t) and re.search(r"Invoice Number:\s*\d+", t) \
                and re.search(r"Issue Date\s+Invoice Number\s+Customer Number", t):
            return "v_invoice_2024"
        if re.search(r"Aussie Broadband Tax Invoice \(\d{2}/\d{2}/\d{4}\)", t) and re.search(r"invoice \(#\d+\)", t):
            return "v_email_notice"
        if re.search(r"Tax Invoice", t) and re.search(r"Invoice #:\s*\d+", t) and re.search(r"Issue Date:", t):
            return "v_invoice"
        if re.search(r"TAX INVOICE", t) and re.search(r"New Invoice #\d+", t) and re.search(r"This Invoice:", t):
            return "v_tax_invoice_text"
        return None

    def parse(self, ver, t):
        if ver == "v_invoice":
            m = re.search(r"Issue Date:\s*(\d{1,2})/(\d{1,2})/(\d{4})", t)
            ref = re.search(r"Invoice #:\s*(\d+)", t)
            tot = (re.search(r"Total Due:\s*\$(-?[\d,.]+)", t)
                   or re.search(r"\$[\d,.]+\s*-\s*\$[\d,.]+\s*\+\s*\$([\d,.]+)\s*=", t)
                   or re.search(r"Total Due\s*\$[\d,.]+(?: cr)?\s*\$[\d,.]+\s*\$[\d,.]+\s*\$([\d,.]+)", t))
            return {"issue": _dmy_slash(m) if m else None, "ref": ref and ref.group(1),
                    "total": _num(tot.group(1)) if tot else None, "stem": "ABB_Invoice"}
        if ver == "v_invoice_2024":
            m = re.search(r"Issue Date\s+Invoice Number\s+Customer Number\s+(\d{1,2}) ([A-Za-z]{3}) (20\d\d)\s+(\d+)", t)
            tot = re.search(r"Amount Due\s*\$([\d,.]+)", t)
            return {"issue": _d_mon_y(m.group(1), m.group(2), m.group(3)) if m else None, "ref": m and m.group(4),
                    "total": _num(tot.group(1)) if tot else None, "stem": "ABB_Invoice"}
        if ver == "v_email_notice":
            m = re.search(r"Tax Invoice \((\d{2})/(\d{2})/(\d{4})\)", t)
            ref = re.search(r"invoice \(#(\d+)\)", t)
            tot = re.search(r"Total Due:\s*\$([\d,.]+)", t)
            return {"issue": _dmy_slash(m) if m else None, "ref": ref and ref.group(1),
                    "total": _num(tot.group(1)) if tot else None, "stem": "ABB_Invoice_Notice", "subdir": "Email_Receipts"}
        if ver == "v_payment_receipt":
            m = re.search(r"(?m)^\s*(\d{2})-(\d{2})-(\d{4})\s*$", t)
            ref = re.search(r"INVOICE #(\d+)", t)
            tot = re.search(r"Payment Total:\s*\$([\d,.]+)", t)
            return {"issue": _dmy_slash(m) if m else None, "ref": ref and ref.group(1),
                    "total": _num(tot.group(1)) if tot else None, "stem": "ABB_Payment_Receipt"}
        m = re.search(r"(?m)^\s*(\d{4})-(\d{2})-(\d{2})\s*$", t)
        ref = re.search(r"New Invoice #(\d+)", t)
        tot = re.search(r"This Invoice:\s*\$([\d,.]+)", t)
        return {"issue": _date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None,
                "ref": ref and ref.group(1), "total": _num(tot.group(1)) if tot else None,
                "stem": "ABB_Invoice", "subdir": "Email_Receipts"}


class TelstraProvider(VendorProvider):
    name = "telstra"
    display_name = "Telstra"
    doc_type = "telstra_bill"
    vendor_dir = "Telstra"
    source_domains = ["telstra.com", "telstra.com.au"]
    format_versions = ["v_bill"]
    sample_variants = {
        "v_bill": """
 Telstra Corporation Limited ABN 33 051 775 556
 Tax Invoice
 Bill Issued 08 October 2012
 Due by 24 October 2012
 Bill number: 000 0000 001
 Total due: $116.53
 Account number 0000 00000 0000
""",
    }
    sample_expect = {"v_bill": "Telstra_Bill_2012-10-08_0000000001.pdf"}

    def versions(self, t):
        if "33 051 775 556" in t and re.search(r"Bill number:?\s*\d", t) and re.search(r"Total due:?\s*\$", t):
            return "v_bill"
        return None

    def parse(self, ver, t):
        ref = re.search(r"Bill number:?\s*([\d ]{9,14})", t)
        tot = re.search(r"Total due:?\s*\$([\d,]+\.\d{2})", t)
        m = re.search(r"Issued\s*:?\s*(\d{1,2}) ([A-Za-z]+) (20\d\d)", t)
        issue = _d_mon_y(m.group(1), m.group(2), m.group(3)) if m else None
        if not issue:  # later layouts print the issue date alone on a line
            m = re.search(r"^\s*(\d{1,2}) ([A-Z][a-z]{2,8}) (20\d\d)\s*$", t, re.M)
            issue = _d_mon_y(m.group(1), m.group(2), m.group(3)) if m else None
        return {"issue": issue, "ref": ref and re.sub(r"\D", "", ref.group(1)),
                "total": _num(tot.group(1)) if tot else None, "stem": "Telstra_Bill"}


class VodafoneProvider(VendorProvider):
    name = "vodafone"
    display_name = "Vodafone"
    doc_type = "vodafone_bill"
    vendor_dir = "Vodafone"
    source_domains = ["vodafone.com.au"]
    format_versions = ["v_email_notice"]
    sample_variants = {
        "v_email_notice": """
Your Vodafone email bill
 Total due
 $0.00 $167.97 2012 $167.97
 Account no: 000000000
 Invoice no: 0000000002
 Date of issue: 18 Aug 2012
""",
    }
    sample_expect = {"v_email_notice": "Vodafone_Bill_Notice_2012-08-18_0000000002.pdf"}

    def versions(self, t):
        if re.search(r"Your Vodafone email bill", t, re.I) and re.search(r"Invoice no:\s*\d+", t):
            return "v_email_notice"
        return None

    def parse(self, ver, t):
        m = re.search(r"Date of issue:\s*(\d{1,2}) ([A-Za-z]+) (20\d\d)", t)
        ref = re.search(r"Invoice no:\s*(\d+)", t)
        tot = (re.search(r"Total due\s*\$[\d,.]+\s*\$([\d,.]+)", t)
               or re.search(r"Total due\s*\$([\d,.]+)", t))
        return {"issue": _d_mon_y(m.group(1), m.group(2), m.group(3)) if m else None, "ref": ref and ref.group(1),
                "total": _num(tot.group(1)) if tot else None, "stem": "Vodafone_Bill_Notice", "subdir": "Email_Receipts"}


class BunningsProvider(VendorProvider):
    name = "bunnings"
    display_name = "Bunnings"
    doc_type = "bunnings_receipt"
    vendor_dir = "Bunnings"
    source_domains = ["bunnings.com.au"]
    format_versions = ["v_digital_receipt"]
    sample_variants = {
        "v_digital_receipt": """
 EXAMPLEVILLE
 BUNNINGS GROUP LIMITED
 ABN 26 008 672 179
 Thu 26/12/2019 04:06:40 PM
 DIGITAL RECEIPT
 ** TAX INVOICE **
 0000001 SAMPLE ITEM $100.00
Total $100.00
GST INCLUDED IN THE TOTAL $9.09
 EFT $100.00
""",
    }
    sample_expect = {"v_digital_receipt": "Bunnings_Receipt_2019-12-26_EXAMPLEVILLE-160640.pdf"}

    def versions(self, t):
        if "BUNNINGS GROUP LIMITED" in t.upper() and "DIGITAL RECEIPT" in t.upper():
            return "v_digital_receipt"
        return None

    def parse(self, ver, t):
        m = re.search(r"\b(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2}):(\d{2})\s*([AP]M)?", t)
        store = re.search(r"(?m)^\s*([A-Z][A-Z .'\-]{2,30})\s*\n\s*BUNNINGS GROUP LIMITED", t, re.I)
        tot = re.search(r"(?m)^\s*Total\s+\$([\d,]+\.\d{2})\s*$", t)
        gst = re.search(r"GST INCLUDED IN THE TOTAL\s+\$([\d,]+\.\d{2})", t)
        issue = hhmmss = None
        if m:
            issue = _dmy_slash(m)
            hh = int(m.group(4)) % 12 + (12 if (m.group(7) or "").upper() == "PM" else 0) if m.group(7) else int(m.group(4))
            hhmmss = f"{hh:02d}{m.group(5)}{m.group(6)}"
        ref = f"{_slug(store.group(1).strip().upper())}-{hhmmss}" if store and hhmmss else None
        total = _num(tot.group(1)) if tot else None
        ok = total is not None and (gst is None or _num(gst.group(1)) <= total / 11 + Decimal("0.05"))
        return {"issue": issue, "ref": ref, "total": total, "stem": "Bunnings_Receipt", "ok": ok}


class GooglePlayProvider(VendorProvider):
    name = "google_play"
    display_name = "Google Play"
    doc_type = "google_play_receipt"
    vendor_dir = "Google"
    source_domains = ["google.com"]
    format_versions = ["v_order_receipt"]
    sample_variants = {
        "v_order_receipt": """
Thank you.
You've made a subscription purchase from Google LLC on Google Play.
Order number: SOP.0000-0000-0000-00000
Order date: 09-Jul-2018 10:04:59 AEST
Yearly Subscription - Auto Renews on 09-Jul-2019
 Total: $124.99/year
 (Includes GST of $11.36)
Google Play
""",
    }
    sample_expect = {"v_order_receipt": "Google_Play_Receipt_2018-07-09_SOP.0000-0000-0000-00000.pdf"}

    def versions(self, t):
        if "Google Play" in t and re.search(r"Order number:\s*\S+", t) and re.search(r"Order date:\s*\d", t):
            return "v_order_receipt"
        return None

    def parse(self, ver, t):
        m = re.search(r"Order date:\s*(\d{1,2})[- ]([A-Za-z]{3,4})[- ](\d{4})", t)
        ref = re.search(r"Order number:\s*([A-Za-z0-9.\-]+)", t)
        tot = re.search(r"Total:\s*\$([\d,]+\.\d{2})", t)
        return {"issue": _d_mon_y(m.group(1), m.group(2), m.group(3)) if m else None,
                "ref": ref and ref.group(1), "total": _num(tot.group(1)) if tot else None,
                "stem": "Google_Play_Receipt", "subdir": "Email_Receipts"}
