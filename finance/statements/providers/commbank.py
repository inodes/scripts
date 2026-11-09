"""Commonwealth Bank of Australia (CommBank) PDF Statement Provider.

Supports:
  1. CommBank Smart Access / Streamline Transaction Accounts (Solo)
  2. CommBank Gold & Platinum Awards Credit Cards (Solo)

Standard filename format:
  Statement_{account_no}_{period_end}.pdf
"""
import datetime
import os
import re
from typing import Any, Dict, List, Optional

from ..money import D, cents, fmt
from .base import BasePDFProvider, MONTHS, classify_transaction

DATE_PREFIX_RE = re.compile(r"^(\d{1,2}\s+[A-Za-z]{3})\b\s*(.*)$")
MONEY_RE = re.compile(r"-?\$?[\d,]+\.\d{2}(?:\s*(?:CR|DR))?")


def parse_cba_date_str(s: str) -> Optional[datetime.date]:
    """Parse 'DD Month YYYY' or 'DD Mon YYYY' into datetime.date."""
    if not s:
        return None
    parts = s.strip().split()
    if len(parts) >= 3:
        try:
            d = int(parts[0])
            m_str = parts[1][:3].lower()
            m = MONTHS.get(m_str)
            y = int(parts[2])
            if m:
                return datetime.date(y, m, d)
        except (ValueError, TypeError):
            pass
    return None


class CommBankProvider(BasePDFProvider):
    name: str = "commbank"
    display_name: str = "Commonwealth Bank"
    doc_type: str = "cba_statement"
    document_kind: str = "statement"
    enabled: bool = True
    cadence: str = "monthly"
    expected_period_days: int = 30
    max_gap_days: int = 3
    target_dir: Optional[str] = os.path.expanduser(
        "~/Library/Mobile Documents/com~apple~CloudDocs/Finance/Statements/commbank"
    )
    duplicates_dir: Optional[str] = os.path.expanduser(
        "~/Library/Mobile Documents/com~apple~CloudDocs/Finance/Statements/commbank/Duplicates"
    )
    scan_dirs: List[str] = [
        os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/Finance/Statements/commbank"),
    ]
    source_domains: List[str] = ["commbank.com.au", "netbank.com.au"]
    filename_template: Optional[str] = "Statement_{account_no}_{period_end_compact}.pdf"
    format_versions: List[str] = ["v_classic", "v_modern"]

    sample_text: Optional[str] = """
MR SAMPLE PERSON
Account number 06 2258 12345678
Smart Access
Australian credit licence 234945
Name: SAMPLE PERSON
Bank, State & Branch number (BSB) 06 2258.

Statement 146 (Page 1 of 2)
Statement begins 1 August 2024
Statement ends 31 October 2024
Closing balance $1,500.00 CR 

Date Transaction Debit Credit Balance
01 Aug 2024 OPENING BALANCE $500.00 CR
05 Aug Salary NetBank 2,000.00 $2,500.00 CR
10 Aug Direct Debit Rent 1,000.00 $1,500.00 CR
"""

    def detect(self, text: str, pdf_path: str) -> bool:
        t = text or ""
        fname = os.path.basename(pdf_path or "")
        if fname.startswith("Statement_") and any(b in fname for b in ["062258", "494053", "552033"]):
            return True
        has_cba_marker = any(k in t for k in [
            "Commonwealth Bank", "commbank.com.au", "NetBank", "06 2258", "BSB 06 2258",
            "Smart Access", "Gold Awards Card", "Platinum Awards", "Australian credit licence 234945"
        ])
        has_stmt_marker = any(k in t for k in [
            "Statement begins", "Statement ends", "Statement Period", "Statement period", "Your Statement"
        ]) or bool(re.search(r"Statement\s+(?:Period|\d+)", t, re.I)) or bool(re.search(r"Y\s*o\s*u\s*r\s+S\s*t\s*a\s*t\s*e\s*m\s*e\s*n\s*t", t, re.I))
        return has_cba_marker and has_stmt_marker

    def detect_version(self, text: str, pdf_path: str) -> str:
        if "Statement begins" in (text or ""):
            return "v_classic"
        return "v_modern"

    def generate_filename(
        self, data: Dict[str, Any], pdf_path: str, cfg: Optional[Dict[str, Any]] = None
    ) -> str:
        accts = data.get("accounts", [])
        ano = accts[0].get("account_no", "00000000") if accts else "00000000"
        p_end = data.get("period_end")
        p_str = p_end.strftime("%Y%m%d") if isinstance(p_end, datetime.date) else "00000000"
        return f"Statement_{ano}_{p_str}.pdf"

    def extract(self, text: str, pdf_path: str) -> Dict[str, Any]:
        t = text or ""
        ver = self.detect_version(t, pdf_path)
        data: Dict[str, Any] = {
            "provider": self.name,
            "doc_type": self.doc_type,
            "document_kind": self.document_kind,
            "file": pdf_path,
            "customer_no": None,
            "statement_no": None,
            "period_start": None,
            "period_end": None,
            "issue_date": None,
            "accounts": [],
            "reconciled": False,
        }

        # 1. Statement Number
        sn_m = re.search(r"Statement\s+(\d+)\b", t)
        if sn_m:
            data["statement_no"] = sn_m.group(1)

        # 2. Statement Period
        # Classic: "Statement begins DD Month YYYY" / "Statement ends DD Month YYYY"
        p_b_m = re.search(r"Statement begins\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})", t, re.I)
        p_e_m = re.search(r"Statement ends\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})", t, re.I)
        if p_b_m and p_e_m:
            data["period_start"] = parse_cba_date_str(p_b_m.group(1))
            data["period_end"] = parse_cba_date_str(p_e_m.group(1))
            data["issue_date"] = data["period_end"]
        else:
            # Modern: "Statement [pP]eriod\s+DD Month YYYY - DD Month YYYY"
            p_mod = re.search(
                r"Statement\s+[pP]eriod[\s\n\r]+(\d{1,2}\s+[A-Za-z]+\s+\d{4})\s*-\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
                t,
            )
            if p_mod:
                data["period_start"] = parse_cba_date_str(p_mod.group(1))
                data["period_end"] = parse_cba_date_str(p_mod.group(2))
                data["issue_date"] = data["period_end"]

        # Fallback period_end from filename: Statement_{account}_{YYYYMMDD}.pdf or Statements{YYYYMMDD}.pdf
        if not data.get("period_end"):
            fn_m = re.search(r"(?:_|Statements)(\d{8})(?:_\d+)?\.pdf$", os.path.basename(pdf_path or ""), re.I)
            if fn_m:
                ds = fn_m.group(1)
                try:
                    p_end = datetime.date(int(ds[:4]), int(ds[4:6]), int(ds[6:8]))
                    data["period_end"] = p_end
                    data["issue_date"] = p_end
                except ValueError:
                    pass

        # 3. Account Number & Type
        acct_no = ""
        acct_name = "Smart Access"
        acct_type = "transaction"
        bsb = "062258"

        an_m = re.search(r"Account\s+[nN]umber[\s\n\r]+([0-9 ]+)", t, re.I)
        if not an_m:
            an_m = re.search(r"ACCOUNT NUMBER[\s\n\r]+([0-9 ]+)", t, re.I)
        if an_m:
            raw_no = re.sub(r"\s+", "", an_m.group(1).strip())
            if len(raw_no) >= 6:
                acct_no = raw_no

        if not acct_no:
            fn_ano = re.search(r"Statement_([0-9]+)_", os.path.basename(pdf_path or ""))
            if fn_ano:
                acct_no = fn_ano.group(1)

        # Detect specific card or account type
        if "smart access" in t.lower() or "streamline" in t.lower() or acct_no.startswith("062258"):
            acct_name = "Smart Access"
            acct_type = "transaction"
            bsb = "062258"
        elif "platinum awards" in t.lower():
            acct_name = "Platinum Awards Credit Card"
            acct_type = "credit_card"
            bsb = ""
        elif "gold awards" in t.lower() or "awards card" in t.lower():
            acct_name = "Gold Awards Credit Card"
            acct_type = "credit_card"
            bsb = ""
        elif acct_no.startswith("4940") or acct_no.startswith("5520"):
            acct_name = "CommBank Awards Credit Card"
            acct_type = "credit_card"
            bsb = ""

        # 4. Balances
        opening_b = None
        closing_b = None
        tot_deb = None
        tot_cred = None

        # Opening balance
        ob_m = re.search(
            r"(?:OPENING BALANCE|Opening balance(?: at [0-9A-Za-z ]+)?)\s+\$?([0-9,.]+)(?:\s*(CR|DR))?",
            t,
            re.I,
        )
        if ob_m:
            val = D(ob_m.group(1))
            if ob_m.group(2) == "DR":
                val = -abs(val)
            opening_b = val

        # Closing balance
        cb_m = re.search(
            r"(?:Closing [bB]alance(?: at [0-9A-Za-z ]+)?|Closing Balance)\s+\$?([0-9,.]+)(?:\s*(CR|DR))?",
            t,
            re.I,
        )
        if cb_m:
            val = D(cb_m.group(1))
            if cb_m.group(2) == "DR":
                val = -abs(val)
            closing_b = val

        # Check stated summary table (Smart Access / Streamline):
        sum_m = re.search(
            r"Opening balance\s*-\s*Total debits\s*(?:\+)?\s*Total credits\s*=\s*Closing balance[\s\n\r]+(?:\$?([0-9,.]+|Nil)(?:\s*(?:CR|DR))?)\s*(?:\$?([0-9,.]+|Nil))\s*(?:\$?([0-9,.]+|Nil))\s*(?:\$?([0-9,.]+|Nil))",
            t,
            re.I,
        )
        if sum_m:
            g = [D("0.00") if v.lower() == "nil" else D(v.replace(",", "")) for v in sum_m.groups()]
            opening_b = g[0]
            tot_deb = g[1]
            tot_cred = g[2]
            closing_b = g[3]

        # Credit card totals: "New charges $X", "Payments/refunds -$Y"
        nc_m = re.search(r"(?:New charges|New transactions and charges)\s+\$?([0-9,.]+)", t, re.I)
        pr_m = re.search(r"Payments/refunds\s+([$-]?[0-9,.]+)", t, re.I)
        if nc_m:
            tot_deb = D(nc_m.group(1))
        if pr_m:
            tot_cred = abs(D(pr_m.group(1)))

        # 5. Transactions
        transactions: List[Dict[str, Any]] = []
        p_end = data.get("period_end")
        lines = t.splitlines()

        if acct_type != "credit_card":
            transactions = self._extract_smart_access_transactions(lines, p_end, opening_b or D(0), closing_b or D(0))
        else:
            tx_mode = False
            cur_tx: Optional[Dict[str, Any]] = None

            for line in lines:
                line_str = line.strip()
                if not line_str:
                    continue

                if "Date Transaction" in line_str or "Date Transaction details" in line_str:
                    tx_mode = True
                    continue

                if tx_mode:
                    if any(k in line_str for k in [
                        "BALANCE CARRIED FORWARD", "Closing balance", "Closing Balance",
                        "Statement begins", "Statement ends", "Page number Page",
                        "Credit limit", "Purchase Rate"
                    ]):
                        if cur_tx:
                            transactions.append(cur_tx)
                            cur_tx = None
                        if "Closing balance" in line_str or "Purchase Rate" in line_str:
                            tx_mode = False
                        continue

                    dm = DATE_PREFIX_RE.match(line_str)
                    if dm:
                        if cur_tx:
                            transactions.append(cur_tx)
                            cur_tx = None

                        d_str = dm.group(1)
                        rest = dm.group(2).strip()

                        # Ignore OPENING BALANCE row
                        if "OPENING BALANCE" in rest:
                            continue

                        # Date parsing
                        parts = d_str.split()
                        d_day = int(parts[0])
                        m_num = MONTHS.get(parts[1].lower(), 1)
                        y = p_end.year if p_end else 2024
                        if p_end and m_num > p_end.month:
                            y -= 1
                        try:
                            tx_date = datetime.date(y, m_num, d_day)
                        except ValueError:
                            tx_date = p_end

                        amts = MONEY_RE.findall(rest)
                        desc = rest
                        tx_deb = None
                        tx_cred = None
                        tx_amt = None
                        tx_bal = None

                        if len(amts) >= 2:
                            tx_bal = D(amts[-1].replace("CR", "").replace("DR", "").strip())
                            val = D(amts[-2].replace("CR", "").replace("DR", "").strip())
                            if "PAYMENT" in rest.upper() or val < 0:
                                tx_cred = abs(val)
                                tx_amt = abs(val)
                            else:
                                tx_deb = abs(val)
                                tx_amt = -abs(val)

                            for a in amts:
                                desc = desc.replace(a, "")
                            desc = desc.strip()
                        elif len(amts) == 1:
                            val = D(amts[0].replace("CR", "").replace("DR", "").strip())
                            tx_amt = val
                            desc = desc.replace(amts[0], "").strip()

                        cur_tx = {
                            "date": tx_date,
                            "description": desc,
                            "debit": tx_deb,
                            "credit": tx_cred,
                            "amount": tx_amt,
                            "balance": tx_bal,
                            "category": classify_transaction(desc, tx_date or ""),
                        }
                    elif cur_tx:
                        if not any(k in line_str for k in ["Statement", "Account number", "Enquiries"]):
                            cur_tx["description"] = (cur_tx["description"] + " " + line_str).strip()
                            cur_tx["category"] = classify_transaction(cur_tx["description"], cur_tx.get("date") or "")

            if cur_tx:
                transactions.append(cur_tx)

        calc_deb = sum((tx["debit"] for tx in transactions if tx.get("debit")), D(0))
        calc_cred = sum((tx["credit"] for tx in transactions if tx.get("credit")), D(0))

        if tot_deb is None:
            tot_deb = calc_deb
        if tot_cred is None:
            tot_cred = calc_cred

        # Reconcile cent-level
        reconciled = False
        if opening_b is not None and closing_b is not None:
            if acct_type == "credit_card":
                # For credit card: closing = opening + new_charges - payments
                if cents(opening_b + tot_deb - tot_cred) == cents(closing_b):
                    reconciled = True
                elif cents(opening_b + calc_deb - calc_cred) == cents(closing_b):
                    reconciled = True
            else:
                # For transaction account: closing = opening + credits - debits
                if cents(opening_b + tot_cred - tot_deb) == cents(closing_b):
                    reconciled = True
                elif cents(opening_b + calc_cred - calc_deb) == cents(closing_b):
                    reconciled = True

        acct_dict: Dict[str, Any] = {
            "account_no": acct_no,
            "account_name": acct_name,
            "account_type": acct_type,
            "currency": "AUD",
            "bsb": bsb,
            "ownership": "solo",
            "period_start": data.get("period_start"),
            "period_end": data.get("period_end"),
            "opening_balance": opening_b,
            "closing_balance": closing_b,
            "calc_closing_balance": (opening_b + tot_cred - tot_deb) if opening_b is not None else None,
            "total_debits": tot_deb,
            "total_credits": tot_cred,
            "calc_debits": calc_deb,
            "calc_credits": calc_cred,
            "count_debits": len([tx for tx in transactions if tx.get("debit")]),
            "count_credits": len([tx for tx in transactions if tx.get("credit")]),
            "interest_rate": None,
            "reconciled": reconciled,
            "transactions": transactions,
        }

        data["accounts"] = [acct_dict]
        data["reconciled"] = reconciled
        return data

    def _extract_smart_access_transactions(
        self, lines: List[str], p_end: Optional[datetime.date], opening_b: Any, closing_b: Any
    ) -> List[Dict[str, Any]]:
        clean_lines = [l.strip() for l in lines if l.strip()]
        start_idx = -1
        for i, l in enumerate(clean_lines):
            if "OPENING BALANCE" in l:
                start_idx = i
                break

        if start_idx == -1:
            return []

        page_header_patterns = [
            re.compile(r"^\d{4}\.\d{5}\."),
            re.compile(r"^SL\.[A-Z0-9.]+"),
            re.compile(r"^V\d{2}\.\d{2}\.\d{2}"),
            re.compile(r"^Statement\s+\d+", re.I),
            re.compile(r"^\(Page\s+\d+\s+of\s+\d+\)", re.I),
            re.compile(r"^(?:Account\s+Number|Account\s+number)\b", re.I),
            re.compile(r"^06\s*2258\s*\d{8}\b"),
        ]

        def is_header_or_noise(line: str) -> bool:
            if line in ["CR", "DR", "$", "(", ")", "-", "+", "="]:
                return True
            return any(p.search(line) for p in page_header_patterns)

        date_only_re = re.compile(r"^(\d{1,2})\s+([A-Za-z]{3})$")
        date_prefix_re = re.compile(r"^(\d{1,2})\s+([A-Za-z]{3})\b\s*(.*)$")
        money_re = re.compile(r"\b\d{1,3}(?:,\d{3})*\.\d{2}\b")

        raw_blocks = []
        cur_date = None
        cur_lines = []

        for i in range(start_idx + 1, len(clean_lines)):
            l = clean_lines[i]
            if "CLOSING BALANCE" in l or "Debit Interest Rate Summary" in l:
                break
            if "Opening balance" in l and (i + 1 < len(clean_lines) and clean_lines[i + 1] == "-"):
                break
            if "=====PAGE=====" in l:
                continue
            if any(p.search(l) for p in page_header_patterns):
                continue
            if (
                any(h in l for h in ["Date", "Transaction", "Debit", "Credit", "Balance"])
                and len(l.split()) <= 4
                and not date_prefix_re.match(l)
            ):
                continue

            m_date_only = date_only_re.match(l)
            m_date_prefix = date_prefix_re.match(l)

            if m_date_only:
                if cur_date is not None:
                    raw_blocks.append((cur_date, cur_lines))
                cur_date = (int(m_date_only.group(1)), MONTHS.get(m_date_only.group(2).lower(), 1))
                cur_lines = []
            elif m_date_prefix and not any(k in l for k in ["Statement", "Enquiries", "Smart Access", "Enjoy"]):
                if cur_date is not None:
                    raw_blocks.append((cur_date, cur_lines))
                cur_date = (int(m_date_prefix.group(1)), MONTHS.get(m_date_prefix.group(2).lower(), 1))
                rest = m_date_prefix.group(3).strip()
                cur_lines = [rest] if rest else []
            else:
                if cur_date is not None:
                    cur_lines.append(l)

        if cur_date is not None and cur_lines:
            raw_blocks.append((cur_date, cur_lines))

        txs = []
        prev_balance = opening_b
        for (d_day, m_num), blk_lines in raw_blocks:
            y = p_end.year if p_end else 2024
            if p_end and m_num > p_end.month:
                y -= 1
            tx_date = datetime.date(y, m_num, d_day)

            amts = []
            has_paren = any("(" in l for l in blk_lines)
            for l in blk_lines:
                if is_header_or_noise(l):
                    continue
                for fv in money_re.findall(l):
                    amts.append(D(fv.replace(",", "")))

            desc_tokens = []
            for l in blk_lines:
                if is_header_or_noise(l):
                    continue
                cleaned = (
                    money_re.sub("", l)
                    .replace("CR", "")
                    .replace("DR", "")
                    .replace("$", "")
                    .replace("(", "")
                    .replace(")", "")
                    .strip()
                )
                if cleaned and not cleaned.isdigit() and len(cleaned) > 1:
                    desc_tokens.append(cleaned)
            desc = " ".join(desc_tokens)

            tx_deb, tx_cred, tx_amt, tx_bal = None, None, None, None
            if len(amts) >= 2:
                tx_bal = amts[-1]
                val = amts[-2]
                diff = tx_bal - prev_balance
                if diff == val:
                    tx_cred, tx_amt = val, val
                elif diff == -val:
                    tx_deb, tx_amt = val, -val
                elif has_paren:
                    tx_deb, tx_amt = val, -val
                elif any(k in desc.lower() for k in ["salary", "deposit", "transfer from", "credit"]):
                    tx_cred, tx_amt = val, val
                else:
                    tx_deb, tx_amt = val, -val
                prev_balance = tx_bal
            elif len(amts) == 1:
                val = amts[0]
                if any(k in desc.lower() for k in ["salary", "deposit", "transfer from", "credit"]):
                    tx_cred, tx_amt = val, val
                else:
                    tx_deb, tx_amt = val, -val
                if prev_balance is not None:
                    prev_balance += tx_cred or -tx_deb
                tx_bal = prev_balance

            txs.append({
                "date": tx_date,
                "description": desc,
                "debit": tx_deb,
                "credit": tx_cred,
                "amount": tx_amt,
                "balance": tx_bal,
                "category": classify_transaction(desc, tx_date),
            })
        return txs

    def run_self_test(self, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = self.extract(self.sample_text or "", "sample_cba.pdf")
        accts = data.get("accounts", [])
        assert len(accts) == 1, f"Expected 1 account, got {len(accts)}"
        a = accts[0]
        assert a["opening_balance"] == D("500.00"), f"Wrong opening: {a['opening_balance']}"
        assert a["closing_balance"] == D("1500.00"), f"Wrong closing: {a['closing_balance']}"
        assert a["reconciled"] is True, "Self test should reconcile"
        return {"status": "ok", "ok": True, "provider": self.name}
