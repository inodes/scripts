"""ING Australia PDF Statement Provider.

Supports:
  1. ING Savings Maximiser (Solo)
  2. ING Orange Everyday (Solo & Joint)

Standard filename format:
  ING_Statement_{bsb}-{account_no}_{statement_no}_{period_end}00000000.pdf
"""
import datetime
import os
import re
from typing import Any, Dict, List, Optional

from ..money import D, cents, fmt
from .. import paths
from .base import BasePDFProvider, MONTHS, classify_transaction

DATE_RE = re.compile(r"^(\d{2}/\d{2}/\d{4})\b\s*(.*)$")
MONEY_RE = re.compile(r"-?\$?[\d,]+\.\d{2}")


class INGProvider(BasePDFProvider):
    name: str = "ing"
    display_name: str = "ING Australia"
    doc_type: str = "ing_statement"
    document_kind: str = "statement"
    enabled: bool = True
    cadence: str = "quarterly"
    expected_period_days: int = 91
    max_gap_days: int = 2
    target_dir: Optional[str] = paths.statements("ING")
    scan_dirs: List[str] = [
        paths.statements("ING"),
    ]
    source_domains: List[str] = ["ing.com.au", "campaigns.ing.com.au"]
    filename_template: Optional[str] = "ING_Statement_{bsb}-{account_no}_{statement_no}_{period_end}00000000.pdf"
    format_versions: List[str] = ["v1"]

    sample_text: Optional[str] = """
                                                     007      Client Number: 1000001
            Mr Sample Person                                  BSB Number: 923 100
            123 Test Street                                   Savings Maximiser Number: 12345678
            SYDNEY NSW 2000                                   Statement Number: 50
                                                              Statement From: 01/01/2024 to 31/03/2024

Balance
Opening Balance                Total Money In    Total Money Out      Closing Balance
            $1,000.00             $5,000.00             $-2,500.00          $3,500.00

Transactions
ACCOUNT NAME: Savings Maximiser
Date          Details                            Money Out $            Money In $  Balance $
15/01/2024    Deposit - Salary                                          5,000.00    6,000.00
20/01/2024    Withdrawal - Rent                  -2,500.00                          3,500.00

Standard Variable Rate as at end of statement period: 5.50% p.a.
"""

    def detect(self, text: str, pdf_path: str) -> bool:
        t = text or ""
        fname = os.path.basename(pdf_path or "").lower()
        if fname.startswith("ing_statement_"):
            return True
        has_ing_keyword = any(k in t for k in [
            "Savings Maximiser", "Orange Everyday", "ING Bank", "ING Australia",
            "923 100", "923-100", "Client Number:", "Client number 1:"
        ])
        has_statement_keyword = any(k in t for k in [
            "Statement From:", "Statement from:", "Statement Number:", "Statement number:"
        ])
        return has_ing_keyword and has_statement_keyword

    def generate_filename(
        self, data: Dict[str, Any], pdf_path: str, cfg: Optional[Dict[str, Any]] = None
    ) -> str:
        accts = data.get("accounts", [])
        bsb = (accts[0].get("bsb") if accts else "923100") or "923100"
        if len(bsb) == 6:
            bsb_fmt = f"{bsb[:3]}-{bsb[3:]}"
        else:
            bsb_fmt = bsb

        ano = accts[0].get("account_no", "00000000") if accts else "00000000"
        stmt_no = data.get("statement_no") or "1"
        p_end = data.get("period_end")
        p_str = p_end.strftime("%Y%m%d") if isinstance(p_end, datetime.date) else "00000000"
        return f"ING_Statement_{bsb_fmt}-{ano}_{stmt_no}_{p_str}00000000.pdf"

    def extract(self, text: str, pdf_path: str) -> Dict[str, Any]:
        t = text or ""
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

        # 1. Client / Customer Number
        c_m = re.search(r"Client [nN]umber(?:\s*1)?:\s*(\d+)", t)
        if c_m:
            data["customer_no"] = c_m.group(1)

        # Joint indicator check
        has_client_2 = bool(re.search(r"Client [nN]umber\s*2:\s*(\d+)", t))
        has_joint_names = bool(re.search(r"Mr\s+.*?and\s+Mrs\b|Mrs\s+.*?and\s+Mr\b", t, re.I))

        # 2. Statement Number
        s_m = re.search(r"Statement [nN]umber:\s*(\d+)", t)
        if s_m:
            data["statement_no"] = s_m.group(1)

        # 3. Statement Period
        p_m = re.search(
            r"Statement [fF]rom:\s*(\d{2})/(\d{2})/(\d{4})\s+to\s+(\d{2})/(\d{2})/(\d{4})", t
        )
        if p_m:
            p_start = datetime.date(int(p_m.group(3)), int(p_m.group(2)), int(p_m.group(1)))
            p_end = datetime.date(int(p_m.group(6)), int(p_m.group(5)), int(p_m.group(4)))
            data["period_start"] = p_start
            data["period_end"] = p_end
            data["issue_date"] = p_end
        else:
            # Fallback from filename: ING_Statement_..._YYYYMMDD00000000.pdf
            fn_m = re.search(r"_(\d{8})00000000", os.path.basename(pdf_path or ""))
            if fn_m:
                ds = fn_m.group(1)
                p_end = datetime.date(int(ds[:4]), int(ds[4:6]), int(ds[6:8]))
                data["period_end"] = p_end
                data["issue_date"] = p_end

        # 4. Account info
        acct_no = ""
        acct_name = "Orange Everyday"
        acct_type = "transaction"

        sm_m = re.search(r"Savings Maximiser\s+[nN]umber:\s*(\d+)", t, re.I)
        if sm_m:
            acct_no = sm_m.group(1)
            acct_name = "Savings Maximiser"
            acct_type = "savings"
        else:
            oe_m = re.search(r"Orange Everyday\s+[nN]umber:\s*(\d+)", t, re.I)
            if oe_m:
                acct_no = oe_m.group(1)
                acct_name = "Orange Everyday"
                acct_type = "transaction"
            else:
                gen_m = re.search(r"Account\s+[nN]umber:\s*(\d+)", t, re.I)
                if gen_m:
                    acct_no = gen_m.group(1)

        # Fallback account number from filename: ING_Statement_923-100-12345678_...
        if not acct_no:
            fn_acct_m = re.search(r"ING_Statement_\d+-\d+-(\d+)_", os.path.basename(pdf_path or ""))
            if fn_acct_m:
                acct_no = fn_acct_m.group(1)

        # Check explicit Account Name in transaction section
        an_m = re.search(r"ACCOUNT NAME:\s*([^\n\r]+)", t, re.I)
        if an_m:
            acct_name = an_m.group(1).strip()
            if "savings" in acct_name.lower():
                acct_type = "savings"

        # Determine ownership
        ownership = "solo"
        if has_client_2 or has_joint_names or "joint" in acct_name.lower():
            ownership = "joint"

        # 5. Interest Rate
        int_rate: Optional[Any] = None
        ir_m = re.search(r"Standard Variable Rate as at end of statement period:\s*([\d\.]+)%\s*p\.a\.", t, re.I)
        if ir_m:
            int_rate = D(ir_m.group(1))

        # 6. Balances & Totals
        opening_b = None
        money_in = None
        money_out = None
        closing_b = None

        bal_hdr_m = re.search(
            r"Opening [bB]alance\s+Total [mM]oney [iI]n\s+Total [mM]oney\s+[oO]ut\s+Closing [bB]alance", t, re.I
        )
        if bal_hdr_m:
            rest = t[bal_hdr_m.end():bal_hdr_m.end() + 400]
            for line in rest.splitlines():
                line_str = line.strip()
                if not line_str:
                    continue
                amts = MONEY_RE.findall(line_str)
                if len(amts) >= 4:
                    opening_b = D(amts[0])
                    money_in = D(amts[1])
                    money_out = D(amts[2])
                    closing_b = D(amts[3])
                    break

        # 7. Parse Transactions
        transactions: List[Dict[str, Any]] = []
        lines = t.splitlines()
        tx_mode = False
        cur_tx: Optional[Dict[str, Any]] = None

        for line in lines:
            line_str = line.strip()
            if not line_str:
                continue

            # Header detection
            if "Date" in line_str and "Details" in line_str and "Balance" in line_str:
                tx_mode = True
                continue

            # Stop conditions
            if tx_mode:
                if any(k in line_str for k in [
                    "Standard Variable Rate", "Total Interest", "Please check all transactions",
                    "Statement Continued Over", "Page 1 of", "Page 2 of", "Page 3 of"
                ]):
                    if cur_tx:
                        transactions.append(cur_tx)
                        cur_tx = None
                    if "Standard Variable Rate" in line_str or "Please check all" in line_str:
                        tx_mode = False
                    continue

                dm = DATE_RE.match(line_str)
                if dm:
                    if cur_tx:
                        transactions.append(cur_tx)
                        cur_tx = None

                    d_str = dm.group(1)
                    rest = dm.group(2).strip()
                    parts = d_str.split("/")
                    tx_date = datetime.date(int(parts[2]), int(parts[1]), int(parts[0]))

                    # Extract numbers at end of line: Money Out, Money In, Balance
                    amts = MONEY_RE.findall(rest)
                    desc = rest
                    tx_debit = None
                    tx_credit = None
                    tx_amt = None
                    tx_bal = None

                    if len(amts) >= 2:
                        tx_bal = D(amts[-1])
                        val = D(amts[-2])
                        if val < 0:
                            tx_debit = abs(val)
                            tx_amt = val
                        else:
                            tx_credit = val
                            tx_amt = val
                        # Strip amounts from description
                        for a in amts:
                            desc = desc.replace(a, "")
                        desc = desc.strip()
                    elif len(amts) == 1:
                        val = D(amts[0])
                        tx_amt = val
                        desc = desc.replace(amts[0], "").strip()

                    cur_tx = {
                        "date": tx_date,
                        "description": desc,
                        "debit": tx_debit,
                        "credit": tx_credit,
                        "amount": tx_amt,
                        "balance": tx_bal,
                        "category": classify_transaction(desc, tx_date or ""),
                    }
                elif cur_tx:
                    # Multi-line description continuation
                    if not any(k in line_str for k in ["Transactions (continued)", "ACCOUNT NAME:"]):
                        cur_tx["description"] = (cur_tx["description"] + " " + line_str).strip()
                        cur_tx["category"] = classify_transaction(cur_tx["description"], cur_tx.get("date") or "")

        if cur_tx:
            transactions.append(cur_tx)

        # Calculate sums
        calc_deb = sum((tx["debit"] for tx in transactions if tx.get("debit")), D(0))
        calc_cred = sum((tx["credit"] for tx in transactions if tx.get("credit")), D(0))

        tot_deb = abs(money_out) if money_out is not None else calc_deb
        tot_cred = money_in if money_in is not None else calc_cred

        # Reconcile cent-level: note that ING Savings Maximiser printed "Total Money In"
        # can omit monthly interest credits, while transactions include them.
        reconciled = False
        if opening_b is not None and closing_b is not None:
            if cents(opening_b + calc_cred - calc_deb) == cents(closing_b):
                reconciled = True
                tot_cred = calc_cred
                tot_deb = calc_deb
            elif cents(opening_b + tot_cred - tot_deb) == cents(closing_b):
                reconciled = True

        acct_dict: Dict[str, Any] = {
            "account_no": acct_no,
            "account_name": acct_name,
            "account_type": acct_type,
            "currency": "AUD",
            "bsb": "923100",
            "ownership": ownership,
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
            "interest_rate": int_rate,
            "reconciled": reconciled,
            "transactions": transactions,
        }

        data["accounts"] = [acct_dict]
        data["reconciled"] = reconciled
        return data

    def run_self_test(self, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = self.extract(self.sample_text or "", "sample_ing.pdf")
        accts = data.get("accounts", [])
        assert len(accts) == 1, f"Expected 1 account, got {len(accts)}"
        a = accts[0]
        assert a["account_no"] == "12345678", f"Wrong account_no: {a['account_no']}"
        assert a["opening_balance"] == D("1000.00"), f"Wrong opening: {a['opening_balance']}"
        assert a["closing_balance"] == D("3500.00"), f"Wrong closing: {a['closing_balance']}"
        assert a["reconciled"] is True, "Self test should reconcile"
        return {"status": "ok", "ok": True, "provider": self.name}
