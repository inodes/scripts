"""HSBC Australia PDF Statement Providers.

Handles all three distinct HSBC Australia PDF statement formats:
  1. HSBC Credit Card Statement (e.g., HSBC Platinum Qantas Credit Card)
     - Provider name: hsbc_credit_card
     - Standard filename: HSBC_CreditCard_{last4}_{YYYYMMDD_start}_to_{YYYYMMDD_end}.pdf
  2. HSBC Premier Portfolio Financial Statement (multi-account: Offset, Premier CMA, Home Loans)
     - Provider name: hsbc_premier
     - Standard filename: HSBC_Statement_{YYYYMMDD_end}.pdf
  3. HSBC Simple Account Financial Statement (single-account interim/daily statement)
     - Provider name: hsbc_simple
     - Standard filename: HSBC_Statement_{YYYYMMDD_end}.pdf
  4. HSBCRouterProvider (provider name: hsbc)
     - Routes automatically to hsbc_credit_card, hsbc_premier, or hsbc_simple.
"""
import datetime
import os
import re
from itertools import combinations, product
from typing import Any, Dict, List, Optional

from ..money import D, cents
from .base import BasePDFProvider, MONTHS, classify_transaction

DATE_PREFIX_RE = re.compile(
    r"^(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec))\b\s*(.*)$"
)
MONEY_STR = r"\d{1,3}(?:,\d{3})*\.\d{2}(?:DR|CR)?"
TWO_AMT_RE = re.compile(rf"^(.*?)\s+({MONEY_STR})\s+({MONEY_STR})$")
ONE_AMT_RE = re.compile(rf"^(.*?)\s+({MONEY_STR})$")
ONLY_AMT_RE = re.compile(rf"^({MONEY_STR})$")
ONLY_TWO_AMT_RE = re.compile(rf"^({MONEY_STR})\s+({MONEY_STR})$")


def norm_account_no(s: str) -> str:
    s = re.sub(r"[\s\-]", "", s or "")
    if s.startswith("011") and len(s) >= 9:
        s = s[3:]
    return s


def resolve_tx_date(
    dd_mmm: Optional[str], p_start: Optional[datetime.date], p_end: Optional[datetime.date]
) -> Optional[datetime.date]:
    if not dd_mmm or not p_end:
        return p_end
    parts = dd_mmm.split()
    d_num = int(parts[0])
    m_num = MONTHS[parts[1].lower()]
    y = p_end.year
    if m_num > p_end.month:
        y -= 1
    try:
        return datetime.date(y, m_num, d_num)
    except ValueError:
        return p_end


def _parse_deposit_or_loan_transactions(
    acct: Dict[str, Any], p_start: Optional[datetime.date], p_end: Optional[datetime.date]
):
    raw_lines: List[str] = acct.pop("raw_lines", [])
    cur_date_str: Optional[str] = None
    cur_desc: List[str] = []
    entries: List[Dict[str, Any]] = []

    i = 0
    while i < len(raw_lines):
        line = raw_lines[i]
        dm = DATE_PREFIX_RE.match(line)
        if dm:
            cur_date_str = dm.group(1)
            rest = dm.group(2).strip()
        else:
            rest = line

        ob_m = re.match(
            rf"^(?:OPENING BALANCE|BALANCE BROUGHT FORWARD)(?:\s+({MONEY_STR}))?$", rest, re.I
        )
        if ob_m:
            if ob_m.group(1):
                acct["opening_balance"] = D(ob_m.group(1))
            else:
                if i + 2 < len(raw_lines) and "starting debit interest rate" in raw_lines[i + 1].lower():
                    m_b = (
                        TWO_AMT_RE.match(raw_lines[i + 2])
                        or ONE_AMT_RE.match(raw_lines[i + 2])
                        or ONLY_AMT_RE.match(raw_lines[i + 2])
                    )
                    if m_b:
                        acct["opening_balance"] = D(m_b.group(m_b.lastindex))
                        i += 3
                        continue
            i += 1
            continue

        if rest.lower().startswith("starting debit interest rate") or rest.lower().startswith("debit balances only"):
            i += 1
            if i < len(raw_lines) and "pa" in raw_lines[i].lower():
                m_b = ONE_AMT_RE.match(raw_lines[i])
                if m_b and acct["opening_balance"] is None:
                    acct["opening_balance"] = D(m_b.group(2))
                i += 1
                if i < len(raw_lines) and ONLY_AMT_RE.match(raw_lines[i]):
                    if acct["opening_balance"] is None:
                        acct["opening_balance"] = D(raw_lines[i])
                    i += 1
            continue

        cb_m = re.match(rf"^(?:CLOSING BALANCE|BALANCE AUD)\s+({MONEY_STR})$", rest, re.I)
        if cb_m:
            acct["closing_balance"] = D(cb_m.group(1))
            i += 1
            continue

        rc_m = re.match(
            rf"^(New debit interest rate\s+[\d\.]+%\s*pa)(?:\s+({MONEY_STR}))?$", rest, re.I
        )
        if rc_m:
            bal_str = rc_m.group(2)
            bal_val = D(bal_str) if (bal_str and bal_str.endswith(("DR", "CR"))) else None
            entries.append(
                {
                    "kind": "info",
                    "date": resolve_tx_date(cur_date_str, p_start, p_end),
                    "desc": rc_m.group(1),
                    "amt": D(0),
                    "bal": bal_val,
                }
            )
            cur_desc = []
            i += 1
            continue

        m_cad = re.match(r"^CLOSED ACCOUNT DIN\s+([\d,\.]+)\s+OFFSET$", rest, re.I)
        if m_cad:
            debit_amt = m_cad.group(1)
            i += 1
            benefit_amt = None
            if i < len(raw_lines):
                m_ben = re.match(r"^BENEFIT\s+([\d,\.]+)(?:\s+[\d,\.]+)?$", raw_lines[i], re.I)
                if m_ben:
                    benefit_amt = m_ben.group(1)
            desc = f"CLOSED ACCOUNT DIN OFFSET BENEFIT {benefit_amt}" if benefit_amt else "CLOSED ACCOUNT DIN"
            entries.append(
                {
                    "kind": "tx",
                    "date": resolve_tx_date(cur_date_str, p_start, p_end),
                    "desc": desc,
                    "amt": D(debit_amt),
                    "bal": None,
                }
            )
            cur_desc = []
            i += 1
            continue

        m2 = TWO_AMT_RE.match(rest)
        if m2:
            desc_part, a_str, b_str = m2.group(1).strip(), m2.group(2), m2.group(3)
            if (
                "OFFSET BENEFIT" in desc_part.upper()
                and "HOME LOAN" in acct["account_name"].upper()
                and not b_str.endswith("DR")
                and b_str != "0.00"
            ):
                full_desc = " ".join(cur_desc + [f"{desc_part} {a_str}"]).strip()
                entries.append(
                    {
                        "kind": "tx",
                        "date": resolve_tx_date(cur_date_str, p_start, p_end),
                        "desc": full_desc,
                        "amt": D(b_str),
                        "bal": None,
                    }
                )
                cur_desc = []
                i += 1
                continue
            full_desc = " ".join(cur_desc + ([desc_part] if desc_part else [])).strip()
            entries.append(
                {
                    "kind": "tx",
                    "date": resolve_tx_date(cur_date_str, p_start, p_end),
                    "desc": full_desc,
                    "amt": D(a_str),
                    "bal": D(b_str),
                }
            )
            cur_desc = []
            i += 1
            continue

        m2_only = ONLY_TWO_AMT_RE.match(rest)
        if m2_only:
            a_str, b_str = m2_only.group(1), m2_only.group(2)
            full_desc = " ".join(cur_desc).strip()
            entries.append(
                {
                    "kind": "tx",
                    "date": resolve_tx_date(cur_date_str, p_start, p_end),
                    "desc": full_desc,
                    "amt": D(a_str),
                    "bal": D(b_str),
                }
            )
            cur_desc = []
            i += 1
            continue

        m1 = ONE_AMT_RE.match(rest)
        if m1:
            desc_part, a_str = m1.group(1).strip(), m1.group(2)
            full_desc = " ".join(cur_desc + ([desc_part] if desc_part else [])).strip()
            entries.append(
                {
                    "kind": "tx",
                    "date": resolve_tx_date(cur_date_str, p_start, p_end),
                    "desc": full_desc,
                    "amt": D(a_str),
                    "bal": None,
                }
            )
            cur_desc = []
            i += 1
            continue

        m1_only = ONLY_AMT_RE.match(rest)
        if m1_only:
            a_str = m1_only.group(1)
            full_desc = " ".join(cur_desc).strip()
            entries.append(
                {
                    "kind": "tx",
                    "date": resolve_tx_date(cur_date_str, p_start, p_end),
                    "desc": full_desc,
                    "amt": D(a_str),
                    "bal": None,
                }
            )
            cur_desc = []
            i += 1
            continue

        cur_desc.append(rest)
        i += 1

    if acct["opening_balance"] is None:
        acct["opening_balance"] = D(0)
    if acct["closing_balance"] is None:
        acct["closing_balance"] = acct["header_balance"]

    curr_bal = acct["opening_balance"]
    tx_list = []
    chunk = []

    def hint_sign(item):
        d_up = item["desc"].upper()
        if any(
            w in d_up
            for w in [
                "TRANSFER FROM", "LOAN PAYMENT FROM", "REFUND", "SALARY", "NDIS",
                "CLOSE A/C", "PAYOFF"
            ]
        ):
            return 1
        return -1

    for e in entries:
        if e["kind"] == "info":
            if e["bal"] is not None:
                curr_bal = e["bal"]
            continue
        chunk.append(e)
        if e["bal"] is not None:
            target_bal = e["bal"]
            diff = target_bal - curr_bal
            k = len(chunk)
            solved = None
            base = [hint_sign(item) for item in chunk]
            if sum(s * item["amt"] for s, item in zip(base, chunk)) == diff:
                solved = tuple(base)
            elif k <= 26:
                for r_flips in range(1, min(k + 1, 9)):
                    found = None
                    for idxs in combinations(range(k), r_flips):
                        cand = list(base)
                        for idx in idxs:
                            cand[idx] = -cand[idx]
                        if sum(s * item["amt"] for s, item in zip(cand, chunk)) == diff:
                            found = tuple(cand)
                            break
                    if found is not None:
                        solved = found
                        break
                if solved is None and k <= 16:
                    for signs in product((-1, 1), repeat=k):
                        if sum(s * item["amt"] for s, item in zip(signs, chunk)) == diff:
                            solved = signs
                            break
            if solved is None:
                solved = tuple(base)

            running = curr_bal
            for s, item in zip(solved, chunk):
                running += s * item["amt"]
                tx_list.append(
                    {
                        "date": str(item["date"]) if item["date"] else None,
                        "description": item["desc"],
                        "card_used": None,
                        "debit": str(cents(item["amt"])) if s == -1 else None,
                        "credit": str(cents(item["amt"])) if s == 1 else None,
                        "amount": str(cents(s * item["amt"])),
                        "balance": str(cents(running)),
                        "printed_balance": str(cents(item["bal"])) if item["bal"] is not None else None,
                        "category": classify_transaction(item["desc"], item.get("date", "")),
                    }
                )
            curr_bal = target_bal
            chunk = []

    calc_deb = sum((D(t["debit"]) for t in tx_list if t["debit"] is not None), D(0))
    calc_cred = sum((D(t["credit"]) for t in tx_list if t["credit"] is not None), D(0))
    cnt_deb = sum(1 for t in tx_list if t["debit"] is not None)
    cnt_cred = sum(1 for t in tx_list if t["credit"] is not None)
    calc_close = acct["opening_balance"] - calc_deb + calc_cred

    cnt_ok = True
    if acct["count_debits"] is not None:
        cnt_ok = cnt_ok and (cnt_deb == acct["count_debits"])
    if acct["count_credits"] is not None:
        cnt_ok = cnt_ok and (cnt_cred == acct["count_credits"])

    reconciled = (
        (acct["total_debits"] is None or calc_deb == acct["total_debits"])
        and (acct["total_credits"] is None or calc_cred == acct["total_credits"])
        and acct["closing_balance"] == acct["header_balance"]
        and calc_close == acct["closing_balance"]
        and cnt_ok
    )

    acct["opening_balance"] = str(cents(acct["opening_balance"]))
    acct["closing_balance"] = str(cents(acct["closing_balance"]))
    acct["header_balance"] = str(cents(acct["header_balance"]))
    acct["calc_closing_balance"] = str(cents(calc_close))
    acct["total_debits"] = str(cents(acct["total_debits"])) if acct["total_debits"] is not None else None
    acct["total_credits"] = str(cents(acct["total_credits"])) if acct["total_credits"] is not None else None
    acct["calc_debits"] = str(cents(calc_deb))
    acct["calc_credits"] = str(cents(calc_cred))
    acct["calc_count_debits"] = cnt_deb
    acct["calc_count_credits"] = cnt_cred
    acct["interest_rate"] = str(acct["interest_rate"]) if acct["interest_rate"] is not None else None
    acct["credit_limit"] = str(cents(acct["credit_limit"])) if acct["credit_limit"] is not None else None
    acct["reconciled"] = reconciled
    acct["note"] = "matches" if reconciled else f"differs (calc close {calc_close} vs printed {acct['closing_balance']})"
    acct["transactions"] = tx_list


def _extract_bank_statement_accounts(
    text: str, p_start: Optional[datetime.date], p_end: Optional[datetime.date]
) -> List[Dict[str, Any]]:
    lines = text.splitlines()
    accts: List[Dict[str, Any]] = []
    cur_acct: Optional[Dict[str, Any]] = None
    in_table = False

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        m_p = re.match(
            r"^([A-Za-z0-9 /&\(\)\-]+?)\s+BSB No\.\s*([\d\-]+)\s+Account\s+([\d\-]+)\s+Currency\s+([A-Z]+)\s+Balance\s+([\d,\.]+(?:DR|CR)?)$",
            line,
        )
        if m_p:
            cur_acct = {
                "account_name": m_p.group(1).strip(),
                "bsb": m_p.group(2).strip(),
                "account_no": norm_account_no(m_p.group(3)),
                "account_raw": m_p.group(3).strip(),
                "currency": m_p.group(4).strip(),
                "header_balance": D(m_p.group(5)),
                "raw_lines": [],
                "opening_balance": None,
                "closing_balance": None,
                "total_debits": None,
                "total_credits": None,
                "count_debits": None,
                "count_credits": None,
                "interest_rate": None,
                "credit_limit": None,
            }
            accts.append(cur_acct)
            in_table = False
            continue

        m_s = re.match(
            r"^([A-Z]{3})\s+([A-Za-z0-9 /&\(\)\-]+?)\s+Branch\s+([A-Za-z0-9 ]+?)\s+Account No\s+([\d\-]+)\s+Balance\s+([\d,\.]+(?:DR|CR)?)$",
            line,
        )
        if m_s:
            bsb_m = re.search(r"(\d{6})$", m_s.group(3).strip())
            acct_num = norm_account_no(m_s.group(4))
            if cur_acct is not None and cur_acct["account_no"] == acct_num and cur_acct["closing_balance"] is None:
                in_table = False
                continue
            cur_acct = {
                "account_name": m_s.group(2).strip(),
                "bsb": bsb_m.group(1) if bsb_m else m_s.group(3).strip(),
                "account_no": acct_num,
                "account_raw": m_s.group(4).strip(),
                "currency": m_s.group(1).strip(),
                "header_balance": D(m_s.group(5)),
                "raw_lines": [],
                "opening_balance": None,
                "closing_balance": None,
                "total_debits": None,
                "total_credits": None,
                "count_debits": None,
                "count_credits": None,
                "interest_rate": None,
                "credit_limit": None,
            }
            accts.append(cur_acct)
            in_table = False
            continue

        if cur_acct is None:
            continue

        m_lim = re.match(
            r"^(?:Current|Credit) Limit\s+([\d,\.]+)\s+Current Debit Balance Interest Rate\s+([\d\.]+)%\s*p\.a\.",
            line,
            re.I,
        )
        if m_lim:
            cur_acct["credit_limit"] = D(m_lim.group(1))
            cur_acct["interest_rate"] = D(m_lim.group(2))
            continue

        if re.match(
            r"^Date\s+Transaction Details\s+Debits?/Withdrawals?\s+Credits?/Deposits?\s+Balance$",
            line,
            re.I,
        ) or re.match(r"^Date\s+Transaction Details\s+Debit\s+Credit\s+Balance$", line, re.I):
            in_table = True
            continue

        if (
            re.match(r"^Statement period\b", line, re.I)
            or line == "=====PAGE====="
            or line.startswith("• ")
            or line.startswith("HSBC Bank Australia")
            or line == "Important Information"
            or line.startswith("END OF STATEMENT")
        ):
            in_table = False
            continue

        m_tot = re.match(r"^Transaction Total\s+([\d,\.]+\d{2})\s+([\d,\.]+\d{2})$", line)
        if m_tot:
            cur_acct["total_debits"] = D(m_tot.group(1))
            cur_acct["total_credits"] = D(m_tot.group(2))
            in_table = False
            continue

        m_num = re.match(r"^Transaction Number(?:\s+(\d+)\s+(\d+))?$", line)
        if m_num:
            cur_acct["count_debits"] = int(m_num.group(1)) if m_num.group(1) else 0
            cur_acct["count_credits"] = int(m_num.group(2)) if m_num.group(2) else 0
            in_table = False
            continue

        if in_table:
            cur_acct["raw_lines"].append(line)

    for a in accts:
        a["period_start"] = str(p_start) if p_start else None
        a["period_end"] = str(p_end) if p_end else None
        _parse_deposit_or_loan_transactions(a, p_start, p_end)
    return accts


class HSBCCreditCardProvider(BasePDFProvider):
    """HSBC Credit Card Statement Provider (e.g., HSBC Platinum Qantas Credit Card)."""

    name = "hsbc_credit_card"
    display_name = "HSBC Credit Card"
    doc_type = "hsbc_credit_card"
    target_dir = "~/Library/Mobile Documents/com~apple~CloudDocs/Finance/Statements/HSBC"
    scan_dirs = [
        "~/Library/Mobile Documents/com~apple~CloudDocs/Finance/Statements/HSBC",
        "~/Library/Mobile Documents/com~apple~CloudDocs/PDF",
        "~/Downloads",
    ]
    source_domains = [
        "hsbc.com.au",
        "online-banking.hsbc.com.au",
        "hsbc.com",
    ]
    filename_template = "HSBC_CreditCard_{last4}_{period_end_compact}.pdf"
    cadence = "monthly"
    expected_period_days = 30
    max_gap_days = 1
    format_versions = ["v2025_qantas_card"]
    sample_text = """HSBC Platinum Qantas Credit Card Statement
Page 1 of 2
Statement Summary
Opening Balance $1,000.00
Closing Balance $650.50
Minimum Payment $25.00
Payment Due Date 29/12/2025
Account Number Statement Period Interest Rate Cash Rate Credit Limit Available Credit
4000 1111 2222 1234 5 Nov 2025 to 4 Dec 2025 19.99% p.a. 21.99% p.a. $20,000.00 $19,349.50
Points earned Points to be credited
150 150
Transactions
Date Card Used Transaction Details Amount ($)
6/11/25 1234 WOOLWORTHS METRO SYDNEY $50.50
10/11/25 1234 BPAY PAYMENT RECEIVED -$500.00
15/11/25 9999 OVERSEAS HOTEL TOKYO $97.00
ORIGINAL TRANSACTION AMOUNT 10000.00 JPY
15/11/25 OVERSEAS TRANSACTION FEE $3.00
"""

    def detect(self, text: str, pdf_path: str) -> bool:
        t_low = text.lower()
        return (
            "hsbc" in t_low
            and ("credit card" in t_low or "hsbc credit cards" in t_low)
            and "minimum payment" in t_low
            and "available credit" in t_low
        )

    def extract(self, text: str, pdf_path: str) -> Dict[str, Any]:
        fname = os.path.basename(pdf_path)
        m_hdr = re.search(
            r"Account Number\s+Statement Period\s+Interest Rate\s+Cash Rate\s+Credit Limit\s+Available Credit\s*\n\s*"
            r"(\d{4}\s+\d{4}\s+\d{4}\s+\d{4})\s+"
            r"(\d{1,2}\s+[A-Za-z]{3}\s+\d{2,4})\s+to\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{2,4})\s+"
            r"([\d\.]+)%\s*p\.a\.\s+([\d\.]+)%\s*p\.a\.\s+\$([\d,\.]+)\s+\$([\d,\.]+)",
            text,
            re.I,
        )

        card_num = re.sub(r"\s+", "", m_hdr.group(1)) if m_hdr else "UNKNOWN"
        p_start = self.parse_flexible_date(m_hdr.group(2)) if m_hdr else None
        p_end = self.parse_flexible_date(m_hdr.group(3)) if m_hdr else None
        interest_rate = D(m_hdr.group(4)) if m_hdr else None
        cash_rate = D(m_hdr.group(5)) if m_hdr else None
        limit = D(m_hdr.group(6)) if m_hdr else None
        avail = D(m_hdr.group(7)) if m_hdr else None

        m_open = re.search(r"Opening Balance\s+(-?\$?[\d,\.]+\d{2})", text, re.I)
        m_close = re.search(r"Closing Balance\s+(-?\$?[\d,\.]+\d{2})", text, re.I)
        m_min = re.search(r"Minimum Payment\s+\$([\d,\.]+\d{2})", text, re.I)
        m_due = re.search(r"Payment Due Date\s+(\d{2}/\d{2}/\d{4})", text, re.I)
        m_pts = re.search(
            r"Points earned\s+Points to be credited\s*\n\s*([\d,]+)\s+([\d,]+)", text, re.I
        )

        open_bal = D(m_open.group(1)) if m_open else D(0)
        close_bal = D(m_close.group(1)) if m_close else D(0)
        min_pay = D(m_min.group(1)) if m_min else D(0)
        due_date = self.parse_flexible_date(m_due.group(1)) if m_due else None
        points_earned = int(m_pts.group(1).replace(",", "")) if m_pts else None

        txs: List[Dict[str, Any]] = []
        cur_date = p_start
        running = open_bal
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if re.match(r"^\d{1,2}/\d{2}/\d{2}\s+(?:OPENING|CLOSING)\s+BALANCE\s+", line, re.I):
                continue
            m_tx = re.match(
                r"^(\d{1,2}/\d{2}/\d{2})\s+(?:(\d{4})\s+)?(.*?)\s+(-?\$[\d,]+\.\d{2})$", line
            )
            if m_tx:
                cur_date = self.parse_flexible_date(m_tx.group(1))
                card_used = m_tx.group(2)
                desc = m_tx.group(3).strip()
                amt_raw = m_tx.group(4)
                neg = amt_raw.startswith("-")
                val = abs(D(amt_raw))
                # For a credit card liability balance, a charge increases balance owed (+val),
                # while a payment/refund reduces balance owed (-val).
                delta = -val if neg else val
                running += delta
                txs.append(
                    {
                        "date": str(cur_date) if cur_date else None,
                        "card_used": card_used,
                        "description": desc,
                        "debit": str(cents(val)) if not neg else None,
                        "credit": str(cents(val)) if neg else None,
                        "amount": str(cents(-val if not neg else val)),
                        "balance": str(cents(-running)),
                        "printed_balance": None,
                        "category": classify_transaction(desc, str(cur_date) if cur_date else ""),
                    }
                )
                continue

            m_fee = re.match(r"^(OVERSEAS TRANSACTION FEE)\s+(-?\$[\d,]+\.\d{2})$", line, re.I)
            if m_fee:
                val = abs(D(m_fee.group(2)))
                running += val
                txs.append(
                    {
                        "date": str(cur_date) if cur_date else None,
                        "card_used": txs[-1]["card_used"] if txs else None,
                        "description": m_fee.group(1),
                        "debit": str(cents(val)),
                        "credit": None,
                        "amount": str(cents(-val)),
                        "balance": str(cents(-running)),
                        "printed_balance": None,
                        "category": "Bank Fees",
                    }
                )
                continue

            m_fx = re.match(r"^ORIGINAL TRANSACTION AMOUNT\s+(.+)$", line, re.I)
            if m_fx and txs:
                txs[-1]["description"] += f" (FX: {m_fx.group(1).strip()})"
                continue

        calc_deb = sum((D(t["debit"]) for t in txs if t["debit"] is not None), D(0))
        calc_cred = sum((D(t["credit"]) for t in txs if t["credit"] is not None), D(0))
        cnt_deb = sum(1 for t in txs if t["debit"] is not None)
        cnt_cred = sum(1 for t in txs if t["credit"] is not None)
        calc_close = open_bal + calc_deb - calc_cred
        reconciled = calc_close == close_bal

        acct_section = {
            "account_name": "HSBC Platinum Qantas Credit Card",
            "account_no": card_num,
            "bsb": None,
            "currency": "AUD",
            "period_start": str(p_start) if p_start else None,
            "period_end": str(p_end) if p_end else None,
            "opening_balance": str(cents(-open_bal)),
            "closing_balance": str(cents(-close_bal)),
            "calc_closing_balance": str(cents(-calc_close)),
            "total_debits": str(cents(calc_deb)),
            "total_credits": str(cents(calc_cred)),
            "calc_debits": str(cents(calc_deb)),
            "calc_credits": str(cents(calc_cred)),
            "count_debits": cnt_deb,
            "count_credits": cnt_cred,
            "calc_count_debits": cnt_deb,
            "calc_count_credits": cnt_cred,
            "interest_rate": str(interest_rate) if interest_rate is not None else None,
            "cash_rate": str(cash_rate) if cash_rate is not None else None,
            "credit_limit": str(cents(limit)) if limit is not None else None,
            "available_credit": str(cents(avail)) if avail is not None else None,
            "minimum_payment": str(cents(min_pay)),
            "due_date": str(due_date) if due_date else None,
            "qantas_points_earned": points_earned,
            "reconciled": reconciled,
            "note": "matches" if reconciled else f"differs (calc {calc_close} vs printed {close_bal})",
            "transactions": txs,
        }

        return {
            "provider": self.name,
            "provider_display": self.display_name,
            "doc_type": self.doc_type,
            "format_version": "v2025_qantas_card",
            "source_file": fname,
            "source_path": pdf_path,
            "customer_no": card_num,
            "period_start": str(p_start) if p_start else None,
            "period_end": str(p_end) if p_end else None,
            "issue_date": str(p_end) if p_end else None,
            "reconciled": reconciled,
            "accounts": [acct_section],
            "warnings": [] if reconciled else [acct_section["note"]],
        }

    def default_filename(self, data: Dict[str, Any], original_path: str) -> str:
        accts = data.get("accounts") or []
        last4 = (accts[0].get("account_no") or "")[-4:] if accts else "Card"
        p_end = (data.get("period_end") or "").replace("-", "")
        if p_end:
            return f"HSBC_CreditCard_{last4}_{p_end}.pdf"
        return os.path.basename(original_path)


class HSBCPremierProvider(BasePDFProvider):
    """HSBC Premier Portfolio Financial Statement Provider (multi-account: Offset, CMA, Home Loans)."""

    name = "hsbc_premier"
    display_name = "HSBC Premier Portfolio"
    doc_type = "hsbc_premier"
    target_dir = "~/Library/Mobile Documents/com~apple~CloudDocs/Finance/Statements/HSBC"
    scan_dirs = [
        "~/Library/Mobile Documents/com~apple~CloudDocs/Finance/Statements/HSBC",
        "~/Library/Mobile Documents/com~apple~CloudDocs/PDF",
        "~/Downloads",
    ]
    source_domains = [
        "hsbc.com.au",
        "online-banking.hsbc.com.au",
        "hsbc.com",
    ]
    filename_template = "HSBC_Premier_{customer_no}_{period_end_compact}.pdf"
    cadence = "monthly"
    expected_period_days = 30
    max_gap_days = 1
    format_versions = ["v2019_classic", "v2025_dual_ccy"]
    sample_text = """HSBC Bank Australia Limited
Financial Statement
CUSTOMER NUMBER 011-100001
STATEMENT PERIOD 18 May 2026 TO 17 Jun 2026
Your Portfolio at a Glance
Your Deposits and Dual CCY Investment 350,000.00
Your Loans 750,000.00DR
Net position 400,000.00DR
Details of your Accounts
HSBC OFFSET SAV A/C BSB No. 012-001 Account 100001-135 Currency AUD Balance 350,000.00
Date Transaction Details Debits/Withdrawals Credits/Deposits Balance
18 May OPENING BALANCE 300,000.00
25 May EMPLOYER PTY LTD SALARY 60,000.00 360,000.00
02 Jun SYDWAT0000000000 Sydney Water 500.00
02 Jun Loan Payment To: 011-100001-258 9,500.00 350,000.00
17 Jun CLOSING BALANCE 350,000.00
Transaction Total 10,000.00 60,000.00
Transaction Number 2 1
=====PAGE=====
HOME LOAN BSB No. 012-001 Account 100001-258 Currency AUD Balance 750,000.00DR
Current Limit 800,000.00 Current Debit Balance Interest Rate 6.14% p.a.
Date Transaction Details Debits/Withdrawals Credits/Deposits Balance
18 May OPENING BALANCE
Starting debit interest rate is
6.14% pa 757,500.00DR
17 Jun DEBIT INTEREST OFFSET BENEFIT 1,900.00 2,000.00
17 Jun Loan Payment From: 011-100001-135 9,500.00 750,000.00DR
17 Jun CLOSING BALANCE 750,000.00DR
Transaction Total 2,000.00 9,500.00
Transaction Number 1 1
"""
    sample_variants = {
        "v2019_classic": """HSBC Bank Australia Limited
Financial Statement
CUSTOMER NUMBER 011-100001
STATEMENT PERIOD TO 17 Oct 2019
Your Portfolio at a Glance
Your Deposits 50,000.00
Your Loans 400,000.00DR
Net position 350,000.00DR
Details of your Accounts
AUD PREMIER CMA Branch SYDNEY 342011 Account No 011-100001-118 Balance 50,000.00
Date Transaction Details Debit Credit Balance
10 Sep OPENING BALANCE 40,000.00
15 Sep SALARY DEPOSIT 12,000.00 52,000.00
01 Oct UTILITY BILL 2,000.00 50,000.00
17 Oct BALANCE AUD 50,000.00
Transaction Total 2,000.00 12,000.00
Transaction Number 1 1
""",
        "v2025_dual_ccy": sample_text,
    }

    def detect_version(self, text: str, pdf_path: str) -> Optional[str]:
        if not self.detect(text, pdf_path):
            return None
        if "dual ccy investment" in text.lower() or "bsb no." in text.lower():
            return "v2025_dual_ccy"
        return "v2019_classic"

    def detect(self, text: str, pdf_path: str) -> bool:
        t_low = text.lower()
        return "hsbc" in t_low and "your portfolio at a glance" in t_low

    def extract(self, text: str, pdf_path: str) -> Dict[str, Any]:
        fname = os.path.basename(pdf_path)
        pm = re.search(
            r"STATEMENT PERIOD\s+(?:(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\s+)?TO\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
            text,
            re.I,
        )
        p_start = self.parse_flexible_date(pm.group(1)) if (pm and pm.group(1)) else None
        p_end = self.parse_flexible_date(pm.group(2)) if pm else None

        cm = re.search(r"CUSTOMER NUMBER\s+([\d\-]+)", text, re.I)
        customer_no = norm_account_no(cm.group(1).strip()) if cm else "000000"

        dep_m = re.search(r"Your Deposits(?: and Dual CCY Investment)?\s+([\d,\.]+\d{2})", text, re.I)
        loan_m = re.search(r"Your Loans\s+([\d,\.]+\d{2}(?:DR|CR)?)", text, re.I)
        net_m = re.search(r"Net position\s+([\d,\.]+\d{2}(?:DR|CR)?)", text, re.I)

        accts = _extract_bank_statement_accounts(text, p_start, p_end)
        if not p_start and accts:
            # Infer period_start from earliest transaction date if header only had "TO <date>"
            all_dates = [
                t["date"] for a in accts for t in a.get("transactions", []) if t.get("date")
            ]
            if all_dates:
                p_start = datetime.date.fromisoformat(min(all_dates))
                for a in accts:
                    a["period_start"] = str(p_start)

        reconciled = bool(accts) and all(a["reconciled"] for a in accts)
        warnings = [f"{a['account_name']} ({a['account_no']}): {a['note']}" for a in accts if not a["reconciled"]]

        return {
            "provider": self.name,
            "provider_display": self.display_name,
            "doc_type": self.doc_type,
            "format_version": self.detect_version(text, pdf_path) or "v2025_dual_ccy",
            "source_file": fname,
            "source_path": pdf_path,
            "customer_no": customer_no,
            "period_start": str(p_start) if p_start else None,
            "period_end": str(p_end) if p_end else None,
            "issue_date": str(p_end) if p_end else None,
            "portfolio_deposits": str(cents(D(dep_m.group(1)))) if dep_m else None,
            "portfolio_loans": str(cents(D(loan_m.group(1)))) if loan_m else None,
            "portfolio_net_position": str(cents(D(net_m.group(1)))) if net_m else None,
            "reconciled": reconciled,
            "accounts": accts,
            "warnings": warnings,
        }

    def default_filename(self, data: Dict[str, Any], original_path: str) -> str:
        cust = norm_account_no(data.get("customer_no") or "") or "000000"
        p_end = (data.get("period_end") or "").replace("-", "")
        if cust and p_end:
            return f"HSBC_Premier_{cust}_{p_end}.pdf"
        if p_end:
            return f"HSBC_Premier_{p_end}.pdf"
        return os.path.basename(original_path)


class HSBCSimpleProvider(BasePDFProvider):
    """HSBC Simple Account Financial Statement Provider (single-account statement)."""

    name = "hsbc_simple"
    display_name = "HSBC Simple Statement"
    doc_type = "hsbc_simple"
    target_dir = "~/Library/Mobile Documents/com~apple~CloudDocs/Finance/Statements/HSBC"
    scan_dirs = [
        "~/Library/Mobile Documents/com~apple~CloudDocs/Finance/Statements/HSBC",
        "~/Library/Mobile Documents/com~apple~CloudDocs/PDF",
        "~/Downloads",
    ]
    source_domains = [
        "hsbc.com.au",
        "online-banking.hsbc.com.au",
        "hsbc.com",
    ]
    filename_template = "HSBC_{account_type_tag}_{account_no}_{period_end_compact}.pdf"
    cadence = "daily"
    expected_period_days = 1
    max_gap_days = 1
    format_versions = ["v2019_interim"]
    sample_text = """HSBC Bank Australia Limited
Financial Statement
Your Statement at a Glance
CUSTOMER NUMBER 011-100001
STATEMENT PERIOD 18 Oct 2019 TO 21 Oct 2019
Account Summary
PREMIER CMA 100001-118 2,100.00
Details of your Accounts
AUD PREMIER CMA Branch SYDNEY 342011 Account No 011-100001-118 Balance 2,100.00
Date Transaction Details Debits/Withdrawals Credits/Deposits Balance
18 Oct BALANCE BROUGHT FORWARD 2,600.00
21 Oct EFTPOS VISA AUD BUNNINGS RYDALMERE 100.00
21 Oct EFTPOS VISA AUD COLES NEUTRAL BAY 400.00
21 Oct TRANSFER FROM 011-100001-135 1,000.00
21 Oct EFTPOS VISA AUD QANTAS AIRWAYS 1,000.00 2,100.00
21 Oct CLOSING BALANCE 2,100.00
Transaction Total 1,500.00 1,000.00
Transaction Number 3 1
"""

    def detect(self, text: str, pdf_path: str) -> bool:
        t_low = text.lower()
        return "hsbc" in t_low and "your statement at a glance" in t_low

    def extract(self, text: str, pdf_path: str) -> Dict[str, Any]:
        fname = os.path.basename(pdf_path)
        pm = re.search(
            r"STATEMENT PERIOD\s+(?:(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\s+)?TO\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
            text,
            re.I,
        )
        p_start = self.parse_flexible_date(pm.group(1)) if (pm and pm.group(1)) else None
        p_end = self.parse_flexible_date(pm.group(2)) if pm else None

        cm = re.search(r"CUSTOMER NUMBER\s+([\d\-]+)", text, re.I)
        customer_no = norm_account_no(cm.group(1).strip()) if cm else "000000"

        accts = _extract_bank_statement_accounts(text, p_start, p_end)
        if not p_start:
            # Check "17 Oct Balance Brought Forward" for start date
            m_bbf = re.search(
                r"^(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec))\s+Balance Brought Forward",
                text,
                re.I | re.M,
            )
            if m_bbf and p_end:
                p_start = resolve_tx_date(m_bbf.group(1), None, p_end)
            elif p_end:
                p_start = p_end
            for a in accts:
                a["period_start"] = str(p_start) if p_start else None

        reconciled = bool(accts) and all(a["reconciled"] for a in accts)
        warnings = [f"{a['account_name']} ({a['account_no']}): {a['note']}" for a in accts if not a["reconciled"]]

        return {
            "provider": self.name,
            "provider_display": self.display_name,
            "doc_type": self.doc_type,
            "format_version": "v2019_interim",
            "source_file": fname,
            "source_path": pdf_path,
            "customer_no": customer_no,
            "period_start": str(p_start) if p_start else None,
            "period_end": str(p_end) if p_end else None,
            "issue_date": str(p_end) if p_end else None,
            "reconciled": reconciled,
            "accounts": accts,
            "warnings": warnings,
        }

    def default_filename(self, data: Dict[str, Any], original_path: str) -> str:
        accts = data.get("accounts") or []
        acct_name = (accts[0].get("account_name") or "") if accts else ""
        raw_acct_no = (accts[0].get("account_no") or "") if accts else (data.get("customer_no") or "")
        acct_no = norm_account_no(raw_acct_no)
        tag = "Statement"
        if "CMA" in acct_name.upper():
            tag = "CMA"
        elif "HOME LOAN" in acct_name.upper() or "LOAN" in acct_name.upper():
            tag = "HomeLoan"
        p_end = (data.get("period_end") or "").replace("-", "")
        if acct_no and p_end:
            return f"HSBC_{tag}_{acct_no}_{p_end}.pdf"
        if p_end:
            return f"HSBC_{tag}_{p_end}.pdf"
        return os.path.basename(original_path)


class HSBCRouterProvider(BasePDFProvider):
    """Composite router for any HSBC Australia PDF statement."""

    name = "hsbc"
    display_name = "HSBC Australia (All Statement Types)"
    doc_type = "hsbc"
    target_dir = "~/Library/Mobile Documents/com~apple~CloudDocs/Finance/Statements/HSBC"
    scan_dirs = [
        "~/Library/Mobile Documents/com~apple~CloudDocs/Finance/Statements/HSBC",
        "~/Library/Mobile Documents/com~apple~CloudDocs/PDF",
        "~/Downloads",
    ]
    source_domains = [
        "hsbc.com.au",
        "online-banking.hsbc.com.au",
        "hsbc.com",
    ]

    def __init__(self):
        self._sub_providers = [
            HSBCCreditCardProvider(),
            HSBCPremierProvider(),
            HSBCSimpleProvider(),
        ]

    def route(self, text: str, pdf_path: str) -> BasePDFProvider:
        for p in self._sub_providers:
            if p.detect(text, pdf_path):
                return p
        return self._sub_providers[1]

    def detect(self, text: str, pdf_path: str) -> bool:
        return any(p.detect(text, pdf_path) for p in self._sub_providers)

    def extract(self, text: str, pdf_path: str) -> Dict[str, Any]:
        sub = self.route(text, pdf_path)
        return sub.extract(text, pdf_path)

    def default_filename(self, data: Dict[str, Any], original_path: str) -> str:
        dt = data.get("doc_type")
        for p in self._sub_providers:
            if p.doc_type == dt:
                return p.default_filename(data, original_path)
        return os.path.basename(original_path)

    def check_missing_periods(
        self,
        statements: List[Dict[str, Any]],
        cfg: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        raw_gaps: List[Dict[str, Any]] = []
        for sub in self._sub_providers:
            sub_stmts = [
                s
                for s in statements
                if (s.get("doc_type") or s.get("provider")) in (sub.name, sub.doc_type)
            ]
            if sub_stmts:
                raw_gaps.extend(sub.check_missing_periods(sub_stmts, cfg=cfg))

        real_gaps: List[Dict[str, Any]] = []
        for g in raw_gaps:
            m_start = g.get("missing_start")
            m_end = g.get("missing_end")
            if not m_start or not m_end:
                real_gaps.append(g)
                continue
            covered = False
            for s in statements:
                ps = s.get("period_start")
                pe = s.get("period_end")
                if not ps or not pe:
                    continue
                # If another statement spans across or bridges this period
                if ps <= m_start and pe >= m_end:
                    covered = True
                    break
                if ps <= m_start and pe >= m_start:
                    d_m_end = datetime.date.fromisoformat(m_end)
                    d_pe = datetime.date.fromisoformat(pe)
                    if (d_m_end - d_pe).days <= 1:
                        covered = True
                        break
            if not covered:
                real_gaps.append(g)
        return real_gaps

    def run_self_test(self, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        results = [sub.run_self_test(cfg=cfg) for sub in self._sub_providers]
        ok = all(r.get("ok") for r in results)
        return {"provider": self.name, "ok": ok, "sub_providers": results}
