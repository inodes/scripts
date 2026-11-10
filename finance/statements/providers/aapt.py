"""AAPT Limited Payslip Provider.

Supports:
  Historical monthly payslips from AAPT Limited (2012 – 2014)

Standard filename format:
  AAPT_Payslip_{period_end_compact}.pdf
"""
import datetime
import os
import re
from typing import Any, Dict, List, Optional
from decimal import Decimal

from ..money import D, cents, fmt
from .. import paths
from .base import BasePDFProvider, classify_transaction

DATE_DMY_RE = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})$")


def parse_dmy(s: str) -> Optional[datetime.date]:
    """Parse 'DD/MM/YYYY' into datetime.date."""
    if not s:
        return None
    m = DATE_DMY_RE.match(s.strip())
    if m:
        try:
            d = int(m.group(1))
            mo = int(m.group(2))
            y = int(m.group(3))
            if y < 100:
                y += 2000
            return datetime.date(y, mo, d)
        except (ValueError, TypeError):
            pass
    return None


def parse_clean_decimal(s: str) -> Decimal:
    """Parse a dollar string into Decimal."""
    if not s:
        return Decimal("0.00")
    clean = s.replace("$", "").replace(",", "").strip()
    is_neg = False
    if clean.endswith("-"):
        is_neg = True
        clean = clean[:-1].strip()
    elif clean.startswith("-"):
        is_neg = True
        clean = clean[1:].strip()
    try:
        val = Decimal(clean)
        return -val if is_neg else val
    except Exception:
        return Decimal("0.00")


class AAPTProvider(BasePDFProvider):
    name: str = "aapt"
    display_name: str = "AAPT Limited"
    doc_type: str = "aapt_payslip"
    document_kind: str = "payslip"
    enabled: bool = True
    cadence: str = "monthly"
    expected_period_days: int = 30
    max_gap_days: int = 4
    target_dir: Optional[str] = paths.payslips("AAPT")
    scan_dirs: List[str] = [
        paths.payslips("AAPT"),
        paths.get("pdf"),
    ]
    source_domains: List[str] = ["aapt.com.au"]
    filename_template: Optional[str] = "AAPT_Payslip_{period_end_compact}.pdf"
    format_versions: List[str] = ["v2012_aapt"]

    sample_variants: Dict[str, str] = {
        "v2012_aapt": """
Pay Location: New South Wales
Staff Number: 100001
Sample A B
Pay Date: 13/03/2012
Pay Period: 01/03/2012 to 31/03/2012
Pay Run Number: 001001
Annual Salary 95,367.00
BSB: 000-000
Account: 12345678
Account Name: Mr Sample Person
Gross  7,947.25 77,753.55
Taxable  7,947.25 77,753.55
Tax  2,089.00 21,090.00
Net Pay  5,858.25 56,663.55
Superannuation
BT LIFETIME SUPER 715.25 6,997.80
Official Copy of AAPT Limited Payslip
ABN 00000000000
        """
    }

    def detect_version(self, text: str, pdf_path: str = "") -> Optional[str]:
        t = text or ""
        fn = os.path.basename(pdf_path or "")
        if fn == "leave.pdf":
            return None
        # The employer name alone is not enough (invoices addressed to the company also contain it).
        if "Staff Number" in t and "Pay Period" in t and ("AAPT" in t or "Pay Location" in t):
            return "v2012_aapt"
        return None

    def detect(self, text: str, pdf_path: str = "") -> bool:
        return self.detect_version(text, pdf_path) is not None

    def extract(self, text: str, pdf_path: str = "") -> Dict[str, Any]:
        t = text or ""
        data: Dict[str, Any] = {
            "provider": self.name,
            "doc_type": self.doc_type,
            "document_kind": self.document_kind,
            "format_version": "v2012_aapt",
            "company": "AAPT Limited",
            "accounts": [],
            "transactions": [],
            "warnings": [],
            "reconciled": False,
        }

        # Employee & Run Number
        m_staff = re.search(r"Staff Number:\s*(\d+)", t)
        if m_staff:
            data["employee_id"] = m_staff.group(1).strip()
        m_run = re.search(r"Pay Run Number:\s*(\d+)", t)
        if m_run:
            data["pay_run_number"] = m_run.group(1).strip()

        # Dates
        period_start: Optional[datetime.date] = None
        period_end: Optional[datetime.date] = None
        payment_date: Optional[datetime.date] = None

        m_per = re.search(r"Pay Period:\s*(\d{2}/\d{2}/\d{4})\s*to\s*(\d{2}/\d{2}/\d{4})", t)
        if m_per:
            period_start = parse_dmy(m_per.group(1))
            period_end = parse_dmy(m_per.group(2))

        m_paid = re.search(r"Pay Date:\s*(\d{2}/\d{2}/\d{4})", t)
        if m_paid:
            payment_date = parse_dmy(m_paid.group(1))

        # Financials
        m_gross = re.search(r"^Gross\s+([\d,]+\.\d{2})", t, re.MULTILINE)
        gross_pay = parse_clean_decimal(m_gross.group(1)) if m_gross else Decimal("0.00")

        m_tax = re.search(r"^Tax\s+([\d,]+\.\d{2})", t, re.MULTILINE)
        tax = parse_clean_decimal(m_tax.group(1)) if m_tax else Decimal("0.00")

        m_net = re.search(r"^Net Pay\s+([\d,]+\.\d{2})", t, re.MULTILINE)
        net_pay = parse_clean_decimal(m_net.group(1)) if m_net else Decimal("0.00")

        # Deductions: lines between the "Deductions" header and the "Gross" line, e.g.
        #   "MISCELLANEOUS DEDUCTION  28.29 28.29"  (This Pay, Year to Date)
        # Most payslips have an empty block, so the "Gross" line must not be read as a deduction.
        deductions = Decimal("0.00")
        m_ded = re.search(r"^\s*Deductions\s*$(.*?)^\s*Gross\s", t, re.DOTALL | re.MULTILINE)
        if m_ded:
            for line in m_ded.group(1).splitlines():
                amts = re.findall(r"-?[\d,]+\.\d{2}", line)
                if amts:
                    deductions += parse_clean_decimal(amts[0])

        # Superannuation
        super_amt = Decimal("0.00")
        m_sup = re.search(r"Superannuation\s*\n\s*[A-Za-z0-9\s]+?\s+([\d,]+\.\d{2})", t)
        if m_sup:
            super_amt = parse_clean_decimal(m_sup.group(1))

        # Bank Account
        m_bsb = re.search(r"BSB:\s*([\d-]+)", t)
        m_acct = re.search(r"Account:\s*(\d+)", t)
        if m_bsb and m_acct:
            data["accounts"].append({
                "bank_name": "Commonwealth Bank",
                "bsb": m_bsb.group(1).strip(),
                "account_no": m_acct.group(1).strip(),
                "amount": net_pay,
            })

        data["period_start"] = period_start
        data["period_end"] = period_end
        data["issue_date"] = payment_date or period_end
        data["date"] = payment_date or period_end
        data["gross_pay"] = gross_pay
        data["tax"] = tax
        data["deductions"] = deductions
        data["net_pay"] = net_pay
        data["superannuation"] = super_amt

        if period_start:
            data["period_start_compact"] = period_start.strftime("%Y%m%d")
        if period_end:
            data["period_end_compact"] = period_end.strftime("%Y%m%d")
            data["date_compact"] = data["period_end_compact"]
        if payment_date:
            data["issue_date_compact"] = payment_date.strftime("%Y%m%d")

        # Cent reconciliation: gross - tax - deductions == net
        if gross_pay > 0 and net_pay > 0:
            diff = abs((gross_pay - tax - deductions) - net_pay)
            data["reconciled"] = (diff <= Decimal("0.05"))
        elif net_pay > 0:
            data["reconciled"] = True

        return data

    def run_self_test(self, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Self-test."""
        results: Dict[str, Any] = {"provider": self.name, "ok": True, "variants": {}}
        for ver, sample in self.sample_variants.items():
            detected_ver = self.detect_version(sample)
            extracted = self.extract(sample)
            is_ok = (
                detected_ver == ver
                and extracted.get("period_end") is not None
                and extracted.get("net_pay", Decimal("0.00")) > Decimal("0.00")
                and extracted.get("reconciled", False)
            )
            results["variants"][ver] = {
                "ok": is_ok,
                "detected": detected_ver,
                "period_end": str(extracted.get("period_end")),
                "net_pay": str(extracted.get("net_pay")),
                "reconciled": extracted.get("reconciled"),
            }
            if not is_ok:
                results["ok"] = False
        return results
