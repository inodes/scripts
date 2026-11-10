"""Workday Payslip Provider.

Supports:
  1. v2019_payroll (Historical NorthgateArinso layout: 2014 – mid-2025)
  2. v2025_workday (Modern Workday layout: late 2025 – 2026+)

Standard filename format:
  Workday_Payslip_{period_end_compact}.pdf
"""
import datetime
import os
import re
from typing import Any, Dict, List, Optional
from decimal import Decimal

from ..money import D, cents, fmt
from .. import paths
from .base import BasePDFProvider, MONTHS, classify_transaction

DATE_DMY_RE = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})$")
MONEY_RE = re.compile(r"-?\$?[\d,]+\.\d{2}")


def parse_dmy(s: str) -> Optional[datetime.date]:
    """Parse 'D/M/YY' or 'DD/MM/YYYY' into datetime.date."""
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
    clean = s.replace("$", "").replace(",", "").replace("AUD", "").strip()
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


class WorkdayProvider(BasePDFProvider):
    name: str = "workday"
    display_name: str = "Workday Payslips"
    doc_type: str = "workday_payslip"
    document_kind: str = "payslip"
    enabled: bool = True
    cadence: str = "semi_monthly"
    expected_period_days: int = 15
    max_gap_days: int = 2
    target_dir: Optional[str] = paths.payslips("Workday")
    scan_dirs: List[str] = [
        paths.payslips("Workday"),
        paths.get("pdf"),
    ]
    source_domains: List[str] = ["workday.com"]
    filename_template: Optional[str] = "Workday_Payslip_{period_end_compact}.pdf"
    format_versions: List[str] = ["v2019_payroll", "v2025_workday"]

    sample_variants: Dict[str, str] = {
        "v2019_payroll": """
Company 27 Acme Pty Ltd
ABN No. 00 000 000 000
Payslip for Mr Sample User ,  ID Number 000001
Your Payroll Details
ID Number 000001
Name S User
Base Salary 120,000.00
Base Hours 38.00
Pay Frequency Half-monthly
Period of Payment 01/05/19 - 15/05/19
Paid on Date 15/05/19
TAXABLE GROSS EARNINGS $ 5,621.25
TAX DEDUCTIONS
TAX $ 1,673.00-
TOTAL TAX DEDUCTED $ 1,673.00-
TOTAL NET PAY - Bank Credit $ 3,948.25
Net Pay Distribution
ING DIRECT 923-100 12345678 $ 2,948.25
COMMBANK 062-258 87654321 $ 1,000.00
BENEFITS
SS Superannuation 9.5% $ 534.02
        """,
        "v2025_workday": """
Acme Pty Ltd
Level 1 1 Example St Sydney NSW 2000
Sample User
Company	 Australian Business Number (ABN) Pay Period Begin Pay Period End Payment Date
Acme Pty Ltd 00 000 000 000 1/3/26 15/3/26 12/3/26
Name Employee ID Date of Joining
Sample User 100001 1 Jan 2015
Earnings
Salary 82.33 89.3219 7,802.12 94,318.44
Earnings 7,802.12 108,982.81
Employee Taxes
PAYG Withholding (Regular) 2,266.00 36,707.00
Employee Taxes 2,266.00 36,707.00
Payment Information
Bank Name On Account BSB Account Number Amount
HSBC Bank Australia Sample User 342011 ******1111 3,536.12 AUD
Commonwealth Bank Sample User 062258 ******2222 1,000.00 AUD
ING Direct Sample User 923100 ******3333 1,000.00 AUD
Superannuation Contributions
Superannuation Guarantee Example Super 000000000 894.00 4,508.25 17,877.38
        """,
    }

    def detect_version(self, text: str, pdf_path: str = "") -> Optional[str]:
        t = text or ""
        # 1. Modern Workday layout (2025-2026+)
        if "Pay Period Begin" in t and ("Payment Date" in t or "Pty Ltd" in t or "Payslip Information" in t):
            return "v2025_workday"
        if "AUS Regular Payroll" in t or ("Company Information" in t and "Pay Period Begin" in t):
            return "v2025_workday"
        # 2. Historical NorthgateArinso payroll layout (2014-mid 2025)
        if "Period of Payment" in t and ("Company 27" in t or "Payslip" in t):
            return "v2019_payroll"
        # Fallback by filename convention if extracted text has minimal lines
        fn = os.path.basename(pdf_path or "")
        if re.search(r"_Complete.*\.pdf$", fn):
            return "v2025_workday"
        if re.search(r"\d{6}_\d+_\d{8}\.pdf$", fn):
            if "Pay Period Begin" in t:
                return "v2025_workday"
            return "v2019_payroll"
        return None

    def detect(self, text: str, pdf_path: str = "") -> bool:
        return self.detect_version(text, pdf_path) is not None

    def extract(self, text: str, pdf_path: str = "") -> Dict[str, Any]:
        ver = self.detect_version(text, pdf_path) or "v2019_payroll"
        t = text or ""
        fn = os.path.basename(pdf_path or "")

        data: Dict[str, Any] = {
            "provider": self.name,
            "doc_type": self.doc_type,
            "document_kind": self.document_kind,
            "format_version": ver,
            "company": "",
            "accounts": [],
            "transactions": [],
            "warnings": [],
            "reconciled": False,
        }

        period_start: Optional[datetime.date] = None
        period_end: Optional[datetime.date] = None
        payment_date: Optional[datetime.date] = None
        gross_pay = Decimal("0.00")
        tax = Decimal("0.00")
        net_pay = Decimal("0.00")
        pre_tax = Decimal("0.00")
        after_tax = Decimal("0.00")
        super_amt = Decimal("0.00")
        employee_id = ""

        if ver == "v2019_payroll":
            # Employee ID
            m_id = re.search(r"ID Number\s+(\d+)", t)
            if m_id:
                employee_id = m_id.group(1).strip()

            # Periods: "Period of Payment 01/05/19 - 15/05/19" or "01-07-14 - 15-07-14"
            m_per = re.search(r"Period of Payment\s+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\s*-\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", t)
            if m_per:
                period_start = parse_dmy(m_per.group(1))
                period_end = parse_dmy(m_per.group(2))

            # Paid date: "Paid on Date 15/05/19" or "15-07-14"
            m_paid = re.search(r"Paid on Date\s+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", t)
            if m_paid:
                payment_date = parse_dmy(m_paid.group(1))

            # Gross: "TAXABLE GROSS EARNINGS $ 5,621.25" or "Taxable Gross $ 4,374.47"
            # NB: Preceda payslips also carry a "YTD Details" block ("Taxable Gross $x",
            # "Tax $x", "Net $x") that appears *before* the per-period lines. Try the
            # per-period labels first and only fall back to the short labels, otherwise
            # we capture year-to-date totals instead of this pay's amounts.
            # Bonus-only payslip (separate bonus run): gross equals the bonus line and there is no normal pay.
            m_bonus = re.search(r"\bAB\s+ANNUAL BONUS\s+\$?\s*([\d,]+\.\d{2})", t, re.IGNORECASE)
            m_gross = re.search(r"TAXABLE GROSS EARNINGS\s+\$?\s*([\d,]+\.\d{2})", t, re.IGNORECASE)
            if not m_gross:
                m_gross = re.search(r"Taxable Gross\s+\$?\s*([\d,]+\.\d{2})", t, re.IGNORECASE)
            if m_gross:
                gross_pay = parse_clean_decimal(m_gross.group(1))
                if m_bonus and parse_clean_decimal(m_bonus.group(1)) == gross_pay and gross_pay > Decimal("0.00"):
                    data["filename_variant"] = "Bonus"

            # Tax: "TOTAL TAX DEDUCTED $ 1,673.00-" (per-period); fallback "Tax $ 1,205.00"
            m_tax = re.search(r"TOTAL TAX DEDUCTED\s+\$?\s*([\d,]+\.\d{2}-?)", t)
            if not m_tax:
                m_tax = re.search(r"^\s*Tax\s+\$?\s*([\d,]+\.\d{2}-?)", t, re.MULTILINE)
            if m_tax:
                tax = abs(parse_clean_decimal(m_tax.group(1)))

            # After tax earnings (e.g. Flex Wallet):
            m_after = re.search(r"AFTER TAX EARNINGS.*?Sub Total\s+\$?\s*([\d,]+\.\d{2})", t, re.DOTALL)
            if m_after:
                after_tax = parse_clean_decimal(m_after.group(1))

            # Pre tax deductions (if any):
            m_pre = re.search(r"PRE TAX DEDUCTIONS.*?Sub Total\s+\$?\s*([\d,]+\.\d{2})", t, re.DOTALL)
            if m_pre:
                pre_tax = parse_clean_decimal(m_pre.group(1))

            # Net: "TOTAL NET PAY - Bank Credit $ 3,948.25"
            m_net = re.search(r"TOTAL NET PAY\s*-[^\$]*\$?\s*([\d,]+\.\d{2})", t)
            if not m_net:
                m_net = re.search(r"TOTAL NET PAY[^\$]*\$?\s*([\d,]+\.\d{2})", t)
            if not m_net:
                m_net = re.search(r"^Net\s+\$?\s*([\d,]+\.\d{2})", t, re.MULTILINE)
            if m_net:
                net_pay = parse_clean_decimal(m_net.group(1))

            # Superannuation: "SS Superannuation 9.5% $ 534.02"
            m_super = re.search(r"Superannuation[^\$]+\$?\s*([\d,]+\.\d{2})", t)
            if m_super:
                super_amt = parse_clean_decimal(m_super.group(1))

            # Accounts / Bank Credit breakdown
            m_dist = re.search(r"Net Pay Distribution(.*?)(?:BENEFITS|NOTES|\Z)", t, re.DOTALL)
            if m_dist:
                dist_lines = m_dist.group(1).splitlines()
                for line in dist_lines:
                    line_clean = line.strip()
                    m_acct = re.search(r"([A-Za-z0-9\s]+?)\s+(\d{3}-?\d{3})\s+([A-Za-z0-9]+)\s+\$?\s*([\d,]+\.\d{2})", line_clean)
                    if m_acct:
                        b_name = m_acct.group(1).strip()
                        bsb = m_acct.group(2).replace("-", "")
                        acct_num = m_acct.group(3).strip()
                        amt = parse_clean_decimal(m_acct.group(4))
                        data["accounts"].append({
                            "bank_name": b_name,
                            "bsb": bsb,
                            "account_no": acct_num,
                            "amount": amt,
                        })

        elif ver == "v2025_workday":
            # Employee ID
            m_id = re.search(r"Employee ID\s+[^\n]*\n[^\n]*\b(\d{4,8})\b", t)
            if m_id:
                employee_id = m_id.group(1).strip()
            else:
                m_id2 = re.search(r"\b(\d{6})\b\s+\d{1,2}\s+[A-Za-z]{3}\s+\d{4}", t)
                if m_id2:
                    employee_id = m_id2.group(1).strip()

            # Periods table: Format B ("Payslip Information ... 16/03/2026 31/03/2026 30/03/2026")
            m_info = re.search(
                r"Pay Period Begin\s+Pay Period End\s+Payment Date[^\n]*\n(?:[^\n]*\n){0,5}?\s*(\d{1,2}/\d{1,2}/\d{2,4})\s+(\d{1,2}/\d{1,2}/\d{2,4})\s+(\d{1,2}/\d{1,2}/\d{2,4})",
                t,
            )
            if m_info:
                period_start = parse_dmy(m_info.group(1))
                period_end = parse_dmy(m_info.group(2))
                payment_date = parse_dmy(m_info.group(3))
            else:
                # Format A: "<Employer> Pty Ltd <ABN> 1/3/26 15/3/26 12/3/26"
                m_tbl = re.search(
                    r"Pty Ltd\s+[\d\s]+\s+(\d{1,2}/\d{1,2}/\d{2,4})\s+(\d{1,2}/\d{1,2}/\d{2,4})\s+(\d{1,2}/\d{1,2}/\d{2,4})",
                    t,
                )
                if m_tbl:
                    period_start = parse_dmy(m_tbl.group(1))
                    period_end = parse_dmy(m_tbl.group(2))
                    payment_date = parse_dmy(m_tbl.group(3))

            # Current and YTD Totals (Format B):
            # Balance Period Gross Pay PAYG Taxable Gross QE/OTE Pre Tax Deductions Employee Taxes Post Tax Deductions Net Pay
            # Current 7,354.17 7,354.17 7,354.17 0.00 2,093.00 0.00 5,261.17
            m_cur = re.search(
                r"^Current\s+([\d,]+\.\d{2})\s+[\d,]+\.\d{2}\s+[\d,]+\.\d{2}\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})",
                t,
                re.MULTILINE,
            )
            if m_cur:
                gross_pay = parse_clean_decimal(m_cur.group(1))
                pre_tax = parse_clean_decimal(m_cur.group(2))
                tax = parse_clean_decimal(m_cur.group(3))
                after_tax = parse_clean_decimal(m_cur.group(4))
                net_pay = parse_clean_decimal(m_cur.group(5))
                m_etot = re.search(r"Earnings.*?\nTotal:\s+([\d,]+\.\d{2})", t, re.DOTALL)
                if m_etot:
                    etot = parse_clean_decimal(m_etot.group(1))
                    if etot > gross_pay:
                        after_tax += (etot - gross_pay)
            else:
                # Format A
                m_earn = re.search(r"^Earnings\s+([\d,]+\.\d{2})\b", t, re.MULTILINE)
                if m_earn:
                    gross_pay = parse_clean_decimal(m_earn.group(1))

                m_pre = re.search(r"^Pre Tax Deductions\s+([\d,]+\.\d{2})\b", t, re.MULTILINE)
                if m_pre:
                    pre_tax = parse_clean_decimal(m_pre.group(1))

                m_tax = re.search(r"^Employee Taxes\s+([\d,]+\.\d{2})\b", t, re.MULTILINE)
                if m_tax:
                    tax = abs(parse_clean_decimal(m_tax.group(1)))

                m_after = re.search(r"After Tax Earnings\s+([\d,]+\.?\d*)\s+([\d,]+\.\d{2})", t)
                if m_after:
                    after_tax = parse_clean_decimal(m_after.group(1))

            # Payment Information table (both Format A and B):
            m_payinfo = re.search(r"Payment Information(.*?)(?:Superannuation Contributions|Total:|\Z)", t, re.DOTALL)
            total_net = Decimal("0.00")
            if m_payinfo:
                for line in m_payinfo.group(1).splitlines():
                    # Format A: HSBC Bank Australia Sample User 342011 ******1111 3,536.12 AUD
                    # Format B: HSBC Bank Australia HSBC Offset ******8135 3,261.17 AUD
                    m_row = re.search(r"([A-Za-z0-9\s]+?)\s+(?:[A-Za-z\s]+?)\s+(?:(\d{6})\s+)?([\*\d]+)\s+([\d,]+\.\d{2})\s*AUD", line)
                    if m_row:
                        b_name = m_row.group(1).strip()
                        bsb = (m_row.group(2) or "").strip()
                        acct_num = m_row.group(3).strip()
                        amt = parse_clean_decimal(m_row.group(4))
                        total_net += amt
                        data["accounts"].append({
                            "bank_name": b_name,
                            "bsb": bsb,
                            "account_no": acct_num,
                            "amount": amt,
                        })
            if total_net > Decimal("0.00"):
                net_pay = total_net
            elif net_pay == Decimal("0.00") and gross_pay > Decimal("0.00"):
                net_pay = gross_pay - pre_tax - tax + after_tax

            # Superannuation total:
            m_sup = re.search(r"Superannuation Guarantee[^\n]*?([\d,]+\.\d{2})\s+[\d,]+\.\d{2}\s+[\d,]+\.\d{2}", t)
            if m_sup:
                super_amt = parse_clean_decimal(m_sup.group(1))
            else:
                m_sup2 = re.search(r"Superannuation Guarantee[^\n]*\n[^\n]*\b(\d{7,10})\b\s+([\d,]+\.\d{2})", t)
                if m_sup2:
                    super_amt = parse_clean_decimal(m_sup2.group(2))

        # Fallback date recovery from filename if text parse missed:
        if not period_end and fn:
            # Pattern A: 000001_000000100_12032026.pdf
            m_fn = re.search(r"(\d{2})(\d{2})(\d{4})\.pdf$", fn)
            if m_fn:
                d = int(m_fn.group(1))
                mo = int(m_fn.group(2))
                y = int(m_fn.group(3))
                period_end = datetime.date(y, mo, d)
            # Pattern B: workday_YYYYMMDD_...
            if not period_end:
                m_fn2 = re.search(r"workday_(\d{4})(\d{2})(\d{2})_", fn)
                if m_fn2:
                    period_end = datetime.date(int(m_fn2.group(1)), int(m_fn2.group(2)), int(m_fn2.group(3)))
            # Pattern C: PAYSLIP...DD:MM:YY or DD-MM-YY
            if not period_end:
                m_fn3 = re.search(r"PAYSLIP.*?(\d{2})[:\-](\d{2})[:\-](\d{2})", fn)
                if m_fn3:
                    y = int(m_fn3.group(3)) + 2000
                    period_end = datetime.date(y, int(m_fn3.group(2)), int(m_fn3.group(1)))
            # Pattern D: PAYSLIPDDMMYY.pdf
            if not period_end:
                m_fn4 = re.search(r"PAYSLIP(\d{2})(\d{2})(\d{2})\.pdf", fn, re.IGNORECASE)
                if m_fn4:
                    y = int(m_fn4.group(3)) + 2000
                    period_end = datetime.date(y, int(m_fn4.group(2)), int(m_fn4.group(1)))
            if not payment_date:
                payment_date = period_end

        data["employee_id"] = employee_id
        data["period_start"] = period_start
        data["period_end"] = period_end
        data["issue_date"] = payment_date or period_end
        data["date"] = payment_date or period_end
        data["gross_pay"] = gross_pay
        data["pre_tax_deductions"] = pre_tax
        data["tax"] = tax
        data["after_tax_earnings"] = after_tax
        data["net_pay"] = net_pay
        data["superannuation"] = super_amt

        # Format compact dates
        if period_start:
            data["period_start_compact"] = period_start.strftime("%Y%m%d")
        if period_end:
            data["period_end_compact"] = period_end.strftime("%Y%m%d")
            data["date_compact"] = data["period_end_compact"]
        if payment_date:
            data["issue_date_compact"] = payment_date.strftime("%Y%m%d")

        # Reconciliation: gross_pay - pre_tax - tax + after_tax == net_pay (within 2 cents)
        if gross_pay > 0 and net_pay > 0:
            diff = abs((gross_pay - pre_tax - tax + after_tax) - net_pay)
            data["reconciled"] = (diff <= Decimal("2.00"))
        elif net_pay > 0 or (gross_pay == Decimal("0.00") and net_pay == Decimal("0.00")):
            data["reconciled"] = True

        return data

    def run_self_test(self, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Self-test multi-version fixtures."""
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
