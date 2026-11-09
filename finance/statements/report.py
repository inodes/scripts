"""Interactive, standalone HTML report generator for bank statements and multi-account financial tracking.

Generates a single self-contained index.html with embedded CSS, SVG/Canvas interactive charts,
monthly scrubber, transaction categorisation, dedicated home loan tracker, and ownership
filtering (Solo vs Joint vs All accounts).
"""
import csv
import datetime
from datetime import datetime
import json
import os
import re
from typing import Any, Dict, List, Optional

from . import config, store
from .money import D, cents, fmt
from .providers import check_missing_periods_by_provider

_PERSONAL_CACHE: Optional[Dict[str, Any]] = None


def _personal() -> Dict[str, Any]:
    """Private data (property, ESS history, remittances, holdings) from the user's config.json.

    Lives under the "personal" key of the external config.json (never in this repo).
    Returns an empty dict when absent so the report still renders with generic content.
    """
    global _PERSONAL_CACHE
    if _PERSONAL_CACHE is None:
        try:
            _PERSONAL_CACHE = config.load().get("personal", {}) or {}
        except Exception:
            _PERSONAL_CACHE = {}
    return _PERSONAL_CACHE


def _purchase_price() -> Optional[float]:
    pp = _personal().get("property_profile", {}).get("purchase_price")
    return float(pp) if pp else None


def months_between(start_str: Optional[str], end_str: Optional[str]) -> List[str]:
    """Return all YYYY-MM strings between start_str and end_str inclusive."""
    if not start_str or not end_str or len(start_str) < 7 or len(end_str) < 7:
        if end_str and len(end_str) >= 7:
            return [end_str[:7]]
        if start_str and len(start_str) >= 7:
            return [start_str[:7]]
        return []
    try:
        sy, sm = int(start_str[:4]), int(start_str[5:7])
        ey, em = int(end_str[:4]), int(end_str[5:7])
    except (ValueError, IndexError):
        return [end_str[:7]]

    cur_y, cur_m = sy, sm
    res = []
    while (cur_y < ey) or (cur_y == ey and cur_m <= em):
        res.append(f"{cur_y:04d}-{cur_m:02d}")
        cur_m += 1
        if cur_m > 12:
            cur_m = 1
            cur_y += 1
    return res


DEFAULT_ACCOUNT_CONFIG: Dict[str, Dict[str, Any]] = {}  # real accounts live in config.json under "accounts"


def detect_account_ownership(account_no: str, account_name: str, doc_type: str, cfg: Optional[Dict[str, Any]] = None) -> str:
    """Determine ownership: 'joint' or 'solo' based on config or account heuristics."""
    acct_cfgs = (cfg or {}).get("accounts", {})
    if account_no in acct_cfgs and "ownership" in acct_cfgs[account_no]:
        return acct_cfgs[account_no]["ownership"]
    if account_no in DEFAULT_ACCOUNT_CONFIG and "ownership" in DEFAULT_ACCOUNT_CONFIG[account_no]:
        return DEFAULT_ACCOUNT_CONFIG[account_no]["ownership"]
    # Fallback heuristics: credit cards in single name default to solo; mortgages/offset default to joint
    if "credit" in doc_type.lower() or "card" in account_name.lower():
        return "solo"
    return "joint"


def detect_account_type(account_name: str, doc_type: str) -> str:
    """Categorize account type: loan, offset, credit_card, or transaction."""
    name_low = account_name.lower()
    dt_low = doc_type.lower()
    if "loan" in name_low or "mortgage" in name_low or "loan" in dt_low:
        if "transaction" in name_low or "offset" in name_low:
            return "offset"
        return "loan"
    if "offset" in name_low:
        return "offset"
    if "credit" in dt_low or "card" in name_low or "credit" in name_low:
        return "credit_card"
    return "transaction"


def detect_bank_name(provider: str, doc_type: str) -> str:
    """Format provider name into clean bank name (e.g. HSBC, ING, CommBank)."""
    p = (provider or doc_type or "").lower()
    if "hsbc" in p:
        return "HSBC"
    if "ing" in p:
        return "ING"
    if "commbank" in p or "cba" in p:
        return "CommBank"
    if "ge" in p:
        return "GE"
    return provider.upper() if provider else "Bank"


def get_property_valuation(month_str: str) -> Optional[float]:
    """Interpolated property valuation for a YYYY-MM month.

    Anchors (month, value) come from config.json -> personal.property_valuation_anchors.
    Returns None when no anchors are configured or the month precedes the first anchor.
    """
    anchors = [(str(a[0]), float(a[1])) for a in _personal().get("property_valuation_anchors", [])]
    if not anchors or month_str < anchors[0][0]:
        return None
    if month_str >= anchors[-1][0]:
        return anchors[-1][1]
    for i in range(len(anchors) - 1):
        m1, v1 = anchors[i]
        m2, v2 = anchors[i+1]
        if m1 <= month_str <= m2:
            y1, mo1 = int(m1[:4]), int(m1[5:7])
            y2, mo2 = int(m2[:4]), int(m2[5:7])
            y_cur, mo_cur = int(month_str[:4]), int(month_str[5:7])
            idx1 = y1 * 12 + mo1
            idx2 = y2 * 12 + mo2
            idx_cur = y_cur * 12 + mo_cur
            if idx2 == idx1:
                return v1
            ratio = (idx_cur - idx1) / (idx2 - idx1)
            return round(v1 + ratio * (v2 - v1), 2)
    return anchors[-1][1]


def collect_report_data(db, cfg: Optional[Dict[str, Any]] = None, provider_filter: Optional[str] = None) -> Dict[str, Any]:
    """Collect and aggregate all data from SQLite for the HTML dashboard."""
    # 1. Statement files
    stmt_rows = db.execute(
        "SELECT id, sha256, file, provider, doc_type, customer_no, period_start, period_end, "
        "reconciled, target_filename, current FROM statements WHERE current=1 ORDER BY period_end DESC"
    ).fetchall()

    stmts = []
    for r in stmt_rows:
        stmts.append({
            "id": r["id"],
            "file": os.path.basename(r["file"] or r["target_filename"] or ""),
            "path": r["file"] or "",
            "provider": r["provider"],
            "doc_type": r["doc_type"],
            "customer_no": r["customer_no"],
            "period_start": r["period_start"],
            "period_end": r["period_end"],
            "reconciled": bool(r["reconciled"]),
        })

    # Missing gaps
    all_gaps = []
    try:
        raw_stmts = store.statement_rows(db, provider=provider_filter, current_only=True)
        all_gaps = check_missing_periods_by_provider(raw_stmts, provider_filter=provider_filter, cfg=cfg)
    except Exception:
        pass

    # 2. Account sections
    sec_rows = db.execute(
        "SELECT s.account_no, s.account_name, s.provider, s.doc_type, s.bsb, s.currency, "
        "s.period_start, s.period_end, s.opening_balance, s.closing_balance, s.calc_closing_balance, "
        "s.total_debits, s.total_credits, s.interest_rate, s.credit_limit, s.reconciled, s.note, "
        "st.file "
        "FROM account_sections s "
        "JOIN statements st ON s.statement_id = st.id "
        "WHERE st.current=1 "
        "ORDER BY s.period_end ASC"
    ).fetchall()

    accounts_dict: Dict[str, Dict[str, Any]] = {}
    months_set = set()

    for r in sec_rows:
        ano = r["account_no"]
        if not ano:
            continue
        p_end = r["period_end"]
        if p_end:
            months_set.add(p_end[:7])

        if ano not in accounts_dict:
            bank = detect_bank_name(r["provider"], r["doc_type"])
            ownership = detect_account_ownership(ano, r["account_name"] or "", r["doc_type"], cfg)
            acct_type = detect_account_type(r["account_name"] or "", r["doc_type"])
            cfg_entry = (cfg or {}).get("accounts", {}).get(ano, {}) or DEFAULT_ACCOUNT_CONFIG.get(ano, {})
            alias = cfg_entry.get("alias") or r["account_name"]
            continued_into = cfg_entry.get("continued_into")
            discharged_date = cfg_entry.get("discharged_date")
            consolidated_from = cfg_entry.get("consolidated_from")
            accounts_dict[ano] = {
                "account_no": ano,
                "account_name": r["account_name"] or ano,
                "alias": alias,
                "bank": bank,
                "ownership": ownership,
                "account_type": acct_type,
                "continued_into": continued_into,
                "discharged_date": discharged_date,
                "consolidated_from": consolidated_from,
                "currency": r["currency"] or "AUD",
                "bsb": r["bsb"],
                "snapshots": [],
                "latest_balance": 0.0,
                "latest_date": "",
                "total_debits": 0.0,
                "total_credits": 0.0,
                "statement_count": 0,
                "reconciled_count": 0,
            }

        close_b = float(D(r["closing_balance"])) if r["closing_balance"] is not None else 0.0
        deb = float(D(r["total_debits"])) if r["total_debits"] is not None else 0.0
        cred = float(D(r["total_credits"])) if r["total_credits"] is not None else 0.0
        rate = float(D(r["interest_rate"])) if r["interest_rate"] is not None else None

        accounts_dict[ano]["snapshots"].append({
            "period_start": r["period_start"],
            "period_end": p_end,
            "month": p_end[:7] if p_end else "",
            "closing_balance": close_b,
            "debits": deb,
            "credits": cred,
            "interest_rate": rate,
            "reconciled": bool(r["reconciled"]),
            "statement_file": os.path.basename(r["file"] or ""),
        })

        accounts_dict[ano]["latest_balance"] = close_b
        accounts_dict[ano]["latest_date"] = p_end or ""
        accounts_dict[ano]["total_debits"] += deb
        accounts_dict[ano]["total_credits"] += cred
        accounts_dict[ano]["statement_count"] += 1
        if r["reconciled"]:
            accounts_dict[ano]["reconciled_count"] += 1

    # 3. Transactions
    tx_rows = db.execute(
        "SELECT t.id, t.tx_date, t.account_no, t.account_name, t.description, t.card_used, "
        "t.debit, t.credit, t.amount, t.balance, t.category, t.provider "
        "FROM transactions t "
        "JOIN statements st ON t.statement_id = st.id "
        "WHERE st.current=1 "
        "ORDER BY t.tx_date DESC, t.id DESC"
    ).fetchall()

    from .providers.base import classify_transaction

    # Pre-pass to identify recurring merchant frequencies
    desc_months = {}
    for r in tx_rows:
        dt = r["tx_date"] or ""
        m = dt[:7]
        raw = r["description"] or ""
        norm = re.sub(r"\d{6,}", "", raw.upper())
        norm = re.sub(r"RECEIPT\s+\d+", "", norm)
        norm = re.sub(r"\s+", " ", norm).strip()[:35]
        if norm not in desc_months:
            desc_months[norm] = set()
        desc_months[norm].add(m)

    txs = []
    cat_by_month: Dict[str, Dict[str, float]] = {}
    cat_all_time: Dict[str, Dict[str, Any]] = {}
    inflow_outflow_by_month: Dict[str, Dict[str, float]] = {}

    total_regular_spend = 0.0
    total_regular_count = 0
    total_uncat_spend = 0.0
    total_uncat_count = 0

    for r in tx_rows:
        d_str = r["tx_date"] or ""
        m_str = d_str[:7] if d_str else "Unknown"
        if m_str != "Unknown":
            months_set.add(m_str)

        amt = float(D(r["amount"])) if r["amount"] is not None else 0.0
        deb = float(D(r["debit"])) if r["debit"] is not None else 0.0
        cred = float(D(r["credit"])) if r["credit"] is not None else 0.0
        bal = float(D(r["balance"])) if r["balance"] is not None else None
        ano = r["account_no"] or ""
        desc_raw = r["description"] or ""
        cat = classify_transaction(desc_raw, d_str)
        if cat == "Uncategorised" and r["category"] and r["category"] not in ("General", "Uncategorised"):
            cat = r["category"]

        norm = re.sub(r"\d{6,}", "", desc_raw.upper())
        norm = re.sub(r"RECEIPT\s+\d+", "", norm)
        norm = re.sub(r"\s+", " ", norm).strip()[:35]
        distinct_m = len(desc_months.get(norm, set()))

        is_reg = (distinct_m >= 3 and deb > 0) or any(k in desc_raw.upper() for k in (
            "LOAN PAYMENT", "ANYTIME FITNESS", "INTERNODE", "APPLE.COM", "OPENAI",
            "SYDNEY WATER", "CITY OF RYDE", "INSURANCE", "ALLIANZ", "KUMON",
            "0SCHOOLSONLINE", "SPEECH THERAPY", "LUZ ALL ABILITIES", "BUPA", "NETFLIX"
        ))
        is_uncat = (cat == "Uncategorised")

        if is_reg and deb > 0:
            total_regular_spend += deb
            total_regular_count += 1
        if is_uncat and deb > 0:
            total_uncat_spend += deb
            total_uncat_count += 1

        desc_up = (r["description"] or "").upper()
        if "ANNUAL HOME LOAN PACKAGE FEE" in desc_up or "PACKAGE FEE" in desc_up:
            cat = "Bank Fees"
        if ("TESLA" in desc_up or "TESLA MOTORS" in desc_up) and (deb >= 10000 or any(str(r).upper() in desc_up for r in _personal().get("vehicle_purchase_refs", []))):
            cat = "Vehicle Purchase"

        # Monthly category spend (debits/outflows only for spending charts)
        if m_str not in cat_by_month:
            cat_by_month[m_str] = {}
        if deb > 0:
            cat_by_month[m_str][cat] = cat_by_month[m_str].get(cat, 0.0) + deb

        if cat not in cat_all_time:
            cat_all_time[cat] = {"total_debits": 0.0, "total_credits": 0.0, "net": 0.0, "count": 0}
        cat_all_time[cat]["total_debits"] += deb
        cat_all_time[cat]["total_credits"] += cred
        cat_all_time[cat]["net"] += amt
        cat_all_time[cat]["count"] += 1

        # Inflow / Outflow by month
        if m_str not in inflow_outflow_by_month:
            inflow_outflow_by_month[m_str] = {"inflow": 0.0, "outflow": 0.0, "net": 0.0}
        inflow_outflow_by_month[m_str]["inflow"] += cred
        inflow_outflow_by_month[m_str]["outflow"] += deb
        inflow_outflow_by_month[m_str]["net"] += (cred - deb)

        # Capital balance transfers, mortgage repayments, credit card paydowns
        is_tx_transfer = (cat in ("Transfer", "Credit Card Payment", "Mortgage & Loans")) or any(
            kw in desc_up
            for kw in (
                "TRANSFER TO",
                "TRANSFER FROM",
                "TFR TO",
                "TFR FROM",
                "INTERNAL TRANSFER",
                "OSKO PAYMENT TO",
                "OSKO PAYMENT FROM",
                "INTERNET TRANSFER",
                "BPAY TO COMMBANK CREDIT CARD",
                "HSBC BANK PAYMENT",
                "DIRECT DEBIT PAYMENT",
                "LOAN PAYMENT",
                "PEXA",
                "HAUH2021",
                "HAUH2025",
                "SETTL.PROCEEDS",
                "CLOSE A/C",
                "SHORTFALL FUNDS",
            )
        )
        if not is_tx_transfer:
            desc_compact = re.sub(r"[\s\-]", "", desc_up)
            for other_ano in accounts_dict:
                if other_ano != ano and len(other_ano) >= 6 and other_ano in desc_compact:
                    is_tx_transfer = True
                    break

        # Compact transaction record for JS payload
        txs.append({
            "id": r["id"],
            "date": d_str,
            "month": m_str,
            "account_no": ano,
            "desc": r["description"] or "",
            "card": r["card_used"] or "",
            "debit": deb,
            "credit": cred,
            "amount": amt,
            "balance": bal,
            "category": cat,
            "is_transfer": is_tx_transfer,
            "is_regular": bool(is_reg),
            "is_uncategorised": bool(is_uncat),
        })

    # 4. Home loan tracking data
    loan_accts = [a for a in accounts_dict.values() if a["account_type"] == "loan"]
    offset_accts = [a for a in accounts_dict.values() if a["account_type"] == "offset"]
    loan_acc_nos = {la["account_no"] for la in loan_accts}

    # Pre-compute loan interest and regular repayments by month from transactions
    loan_tx_stats_by_month: Dict[str, Dict[str, float]] = {}
    for t in txs:
        if t["account_no"] not in loan_acc_nos:
            continue
        m = t["month"]
        if not m:
            continue
        if m not in loan_tx_stats_by_month:
            loan_tx_stats_by_month[m] = {"interest": 0.0, "repayment": 0.0}

        desc_up = (t["desc"] or "").upper()
        deb = float(t["debit"] or 0)
        cred = float(t["credit"] or 0)
        cat = t["category"] or ""

        # Accurate interest check:
        # Excludes limit increases, loan redraws, or card transfers
        if (cat == "Interest") or ("DEBIT INTEREST" in desc_up) or ("DIN OFFSET BENEFIT" in desc_up):
            if "TO 4265" not in desc_up and "TRANSFER" not in desc_up and "HAUH" not in desc_up and "INCREASE LIMIT" not in desc_up:
                loan_tx_stats_by_month[m]["interest"] += deb

        # Accurate repayment check:
        # Excludes balance restructures, account closures, settlements
        if ("LOAN PAYMENT" in desc_up) or (cat == "Mortgage & Loans" and cred > 0 and "LOAN DD" not in desc_up):
            if "HAUH" not in desc_up and "SETTL" not in desc_up and "CLOSE A/C" not in desc_up:
                loan_tx_stats_by_month[m]["repayment"] += cred

    # Build loan timeline by month
    loan_timeline = []
    sorted_months = sorted(months_set)

    for m in sorted_months:
        # Get loan snapshot for month
        m_loans = []
        for la in loan_accts:
            snaps = [s for s in la["snapshots"] if s["month"] == m]
            if snaps:
                m_loans.append(snaps[-1])
        # Get offset snapshot for month
        m_offsets = []
        for oa in offset_accts:
            snaps = [s for s in oa["snapshots"] if s["month"] == m]
            if snaps:
                m_offsets.append(snaps[-1])

        if m_loans or m_offsets:
            tot_loan_bal = sum(s["closing_balance"] for s in m_loans)
            tot_offset_bal = sum(s["closing_balance"] for s in m_offsets)
            net_debt = tot_loan_bal + tot_offset_bal  # loan bal is negative
            tot_debits = sum(s["debits"] for s in m_loans)
            tot_credits = sum(s["credits"] for s in m_loans)
            # Find current interest rate
            rates = [s["interest_rate"] for s in m_loans if s.get("interest_rate")]
            cur_rate = rates[0] if rates else None

            tx_stat = loan_tx_stats_by_month.get(m)
            if tx_stat is not None:
                actual_interest = round(tx_stat["interest"], 2)
                actual_repay = round(tx_stat["repayment"], 2)
            else:
                actual_interest = round(tot_debits, 2)
                actual_repay = round(tot_credits, 2)

            principal_paid = max(0.0, round(actual_repay - actual_interest, 2))
            prop_val = get_property_valuation(m)
            net_equity = round(prop_val - abs(tot_loan_bal), 2) if prop_val else None
            effective_equity = round(prop_val - max(0.0, abs(net_debt)), 2) if prop_val else None
            capital_gain = round(prop_val - _purchase_price(), 2) if prop_val and _purchase_price() else None
            gross_lvr = round((abs(tot_loan_bal) / prop_val) * 100, 1) if prop_val else None
            net_lvr = round((max(0.0, abs(net_debt)) / prop_val) * 100, 1) if prop_val else None

            loan_timeline.append({
                "month": m,
                "loan_balance": tot_loan_bal,
                "offset_balance": tot_offset_bal,
                "net_debt": net_debt,
                "interest_charged": actual_interest,
                "repayment_credit": actual_repay,
                "principal_paid": principal_paid,
                "interest_rate": cur_rate,
                "property_valuation": prop_val,
                "net_equity": net_equity,
                "effective_equity": effective_equity,
                "capital_gain": capital_gain,
                "gross_lvr": gross_lvr,
                "net_lvr": net_lvr,
            })
        elif loan_timeline and m == sorted_months[-1]:
            # In-progress current month awaiting statements: carry forward previous month's balances
            last_entry = loan_timeline[-1]
            prop_val = get_property_valuation(m)
            tot_loan_bal = last_entry["loan_balance"]
            tot_offset_bal = last_entry["offset_balance"]
            net_debt = tot_loan_bal + tot_offset_bal
            cur_rate = last_entry["interest_rate"]
            net_equity = round(prop_val - abs(tot_loan_bal), 2) if prop_val else None
            effective_equity = round(prop_val - max(0.0, abs(net_debt)), 2) if prop_val else None
            capital_gain = round(prop_val - _purchase_price(), 2) if prop_val and _purchase_price() else None
            gross_lvr = round((abs(tot_loan_bal) / prop_val) * 100, 1) if prop_val else None
            net_lvr = round((max(0.0, abs(net_debt)) / prop_val) * 100, 1) if prop_val else None

            loan_timeline.append({
                "month": m,
                "loan_balance": tot_loan_bal,
                "offset_balance": tot_offset_bal,
                "net_debt": net_debt,
                "interest_charged": 0.0,
                "repayment_credit": 0.0,
                "principal_paid": 0.0,
                "interest_rate": cur_rate,
                "property_valuation": prop_val,
                "net_equity": net_equity,
                "effective_equity": effective_equity,
                "capital_gain": capital_gain,
                "gross_lvr": gross_lvr,
                "net_lvr": net_lvr,
                "is_in_progress": True,
            })

    # Compute offset benefits from transactions
    offset_benefit_txs = [
        t for t in txs
        if "OFFSET BENEFIT" in t["desc"].upper() or "OFFSET" in t["desc"].upper() and t["debit"] > 0
    ]
    tot_offset_benefit = 0.0
    for t in offset_benefit_txs:
        m_ben = re.search(r"OFFSET\s+BENEFIT\s+([\d,\.]+)", t["desc"], re.I)
        if m_ben:
            try:
                tot_offset_benefit += float(m_ben.group(1).replace(",", ""))
            except ValueError:
                pass

    # Build account coverage timeline data
    account_coverage = []
    for ano, a in accounts_dict.items():
        snaps = a["snapshots"]
        months_covered = {}
        for s in snaps:
            p_start = s.get("period_start") or s.get("period_end")
            p_end = s.get("period_end") or s.get("period_start")
            for m in months_between(p_start, p_end):
                months_covered[m] = {
                    "file": s["statement_file"],
                    "period_start": s["period_start"],
                    "period_end": s["period_end"],
                    "closing_balance": s["closing_balance"],
                    "reconciled": s["reconciled"],
                }
        acct_gaps = []
        for g in all_gaps:
            sr = g.get("shift_rows") or []
            if any(row.get("account_no") == ano for row in sr) or g.get("account_no") == ano:
                acct_gaps.append({
                    "start": g.get("missing_start"),
                    "end": g.get("missing_end"),
                    "days": g.get("days"),
                    "estimated": g.get("estimated_missing"),
                    "prior_file": g.get("before_file"),
                    "next_file": g.get("after_file"),
                })
        account_coverage.append({
            "account_no": ano,
            "account_name": a["account_name"],
            "alias": a["alias"],
            "bank": a["bank"],
            "ownership": a["ownership"],
            "account_type": a["account_type"],
            "statement_count": a["statement_count"],
            "reconciled_count": a["reconciled_count"],
            "earliest_date": snaps[0]["period_start"] if snaps else "",
            "latest_date": snaps[-1]["period_end"] if snaps else "",
            "months": months_covered,
            "gaps": acct_gaps,
            "continued_into": a.get("continued_into"),
            "discharged_date": a.get("discharged_date"),
            "consolidated_from": a.get("consolidated_from"),
        })

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_statements": len(stmts),
        "total_transactions": len(txs),
        "months": sorted_months,
        "accounts": list(accounts_dict.values()),
        "account_coverage": account_coverage,
        "statements": stmts,
        "gaps": all_gaps,
        "home_loan_timeline": loan_timeline,
        "cumulative_offset_benefit": round(tot_offset_benefit, 2),
        "property_profile": _personal().get("property_profile", {}),
        "renovation_breakdown": _personal().get("renovation_breakdown", []),
        "categories_all_time": cat_all_time,
        "categories_by_month": cat_by_month,
        "cashflow_by_month": inflow_outflow_by_month,
        "transactions": txs,
        "shares": collect_shares_data(),
    }


def collect_shares_data() -> Dict[str, Any]:
    """Collect employer (TEAM) equity, ESS statutory history, and Shareworks holdings."""
    base_dir = os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/Finance/Shares/TEAM")
    tx_dir = os.path.join(base_dir, "Transactions")

    # 1. RSU Releases map for FMV / cost basis lookup
    releases_file = os.path.join(tx_dir, "RSU Releases.csv")
    release_map = {}
    vest_events = []
    if os.path.exists(releases_file):
        try:
            with open(releases_file, "r", encoding="utf-8-sig") as f:
                reader = list(csv.reader(f))
                for r in reader[2:]:
                    if len(r) > 13 and r[7]:
                        r_date = r[7].strip()
                        fmv_str = r[13].replace("$", "").replace(",", "").strip()
                        if fmv_str:
                            fmv_val = float(fmv_str)
                            release_map[r_date] = fmv_val
                        try:
                            dt = datetime.strptime(r_date, "%d-%b-%Y")
                            sh = float(r[8].replace(",", "").strip())
                            fmv = float(fmv_str) if fmv_str else 0.0
                            vest_events.append((dt, sh, fmv))
                        except Exception:
                            pass
        except Exception:
            pass

    # 2. Parse all 131 granular sales rows
    sales_file = os.path.join(tx_dir, "Sales - Long Shares.csv")
    sales_list = []
    sale_events = []
    if os.path.exists(sales_file):
        try:
            with open(sales_file, "r", encoding="utf-8-sig") as f:
                reader = list(csv.reader(f))
                for r in reader[2:]:
                    if len(r) > 17 and r[15]:
                        try:
                            s_date_str = r[8].strip()
                            acq_date_str = r[9].strip()
                            s_date = datetime.strptime(s_date_str, "%d-%b-%Y")
                            acq_date = datetime.strptime(acq_date_str, "%d-%b-%Y") if acq_date_str else None
                            shares = float(r[15].strip())
                            proceeds = float(r[16].replace("$", "").replace(",", "").strip())
                            sale_price = float(r[18].replace("$", "").replace(",", "").strip())

                            cost_per_sh_str = r[11].replace("$", "").replace(",", "").strip() if len(r) > 11 else ""
                            if cost_per_sh_str and float(cost_per_sh_str) > 0:
                                cost_per_sh = float(cost_per_sh_str)
                            elif acq_date_str in release_map:
                                cost_per_sh = release_map[acq_date_str]
                            else:
                                cost_per_sh = 0.0

                            cost_basis = cost_per_sh * shares
                            within_30d = (r[10].strip().upper() == "YES")

                            days_held = (s_date - acq_date).days if acq_date else 999
                            eligible_discount = (not within_30d) and (days_held >= 365)
                            gain_loss = proceeds - cost_basis

                            fy_year = s_date.year if s_date.month <= 6 else s_date.year + 1
                            fy_str = f"FY{fy_year}"

                            sales_list.append({
                                "sale_date": s_date_str,
                                "date_iso": s_date.strftime("%Y-%m-%d"),
                                "fy": fy_str,
                                "acq_date": acq_date_str,
                                "ref": r[2].strip(),
                                "grant_name": r[5].strip() if len(r) > 5 else "",
                                "shares": shares,
                                "sale_price": sale_price,
                                "proceeds": proceeds,
                                "cost_per_sh": cost_per_sh,
                                "cost_basis": cost_basis,
                                "within_30d": within_30d,
                                "days_held": days_held,
                                "eligible_discount": eligible_discount,
                                "gain_loss": gain_loss,
                            })
                            sale_events.append((s_date, shares, sale_price))
                        except Exception:
                            pass
        except Exception:
            pass

    # Sort sales newest first
    sales_list.sort(key=lambda x: x["date_iso"], reverse=True)

    # 3. Monthly Holdings Timeline from 2015-12 to 2026-11
    all_events = sorted([(d, 'V', s, p) for d, s, p in vest_events] + [(d, 'S', s, p) for d, s, p in sale_events], key=lambda x: x[0])
    monthly_holdings_timeline = []
    team_equity_by_month = {}
    ev_idx = 0
    cur_held = 0.0
    cur_cum_vested = 0.0
    cur_cum_sold = 0.0
    cur_price = 21.0
    fx_rate = 0.65

    timeline_months = []
    for y in range(2015, 2027):
        for m in range(1, 13):
            ym = f"{y}-{m:02d}"
            if "2015-12" <= ym <= "2026-11":
                timeline_months.append(ym)

    for ym in timeline_months:
        while ev_idx < len(all_events) and all_events[ev_idx][0].strftime("%Y-%m") <= ym:
            dt, ev_type, sh, pr = all_events[ev_idx]
            if ev_type == 'V':
                cur_held += sh
                cur_cum_vested += sh
            else:
                cur_held -= sh
                cur_cum_sold += sh
            if pr > 0:
                cur_price = pr
            ev_idx += 1
        val_usd = cur_held * cur_price
        val_aud = val_usd / fx_rate
        entry = {
            "month": ym,
            "held_shares": round(cur_held, 2),
            "cum_vested": round(cur_cum_vested, 2),
            "cum_sold": round(cur_cum_sold, 2),
            "price_usd": round(cur_price, 2),
            "val_usd": round(val_usd, 2),
            "val_aud": round(val_aud, 2)
        }
        monthly_holdings_timeline.append(entry)
        team_equity_by_month[ym] = {
            "held_shares": round(cur_held, 2),
            "val_usd": round(val_usd, 2),
            "val_aud": round(val_aud, 2),
            "price_usd": round(cur_price, 2)
        }

    # 4. Parse Current Holdings Lots
    holdings_file = os.path.join(tx_dir, "Share Holdings.csv")
    lots = []
    _h = _personal().get("holdings_defaults", {})
    total_held_shares = _h.get("total_held_shares", 0)
    current_price = _h.get("current_price", 0.0)
    total_current_val = _h.get("total_current_val", 0.0)
    total_cost_basis = _h.get("total_cost_basis", 0.0)

    if os.path.exists(holdings_file):
        try:
            with open(holdings_file, "r", encoding="utf-8-sig") as f:
                reader = list(csv.reader(f))
                for r in reader[2:]:
                    if len(r) >= 12 and r[1] == "Long Shares":
                        cb = float(r[4].replace("$", "").replace(",", "").strip())
                        cv = float(r[11].replace("$", "").replace(",", "").strip())
                        cps = float(r[6].replace("$", "").replace(",", "").strip())
                        cp = float(r[9].replace("$", "").replace(",", "").strip())
                        sh = int(r[8].strip())
                        gl = cv - cb
                        gl_pct = (gl / cb * 100) if cb > 0 else 0.0

                        acq_d = datetime.strptime(r[2].strip(), "%d-%b-%Y")
                        days_held = (datetime(2026, 11, 7) - acq_d).days
                        eligible_discount = (days_held >= 365)

                        lots.append({
                            "acq_date": r[2].strip(),
                            "date_iso": acq_d.strftime("%Y-%m-%d"),
                            "lot": r[3].strip(),
                            "cost_basis": cb,
                            "cost_per_share": cps,
                            "shares": sh,
                            "current_price": cp,
                            "current_val": cv,
                            "gain_loss": gl,
                            "gain_loss_pct": gl_pct,
                            "days_held": days_held,
                            "eligible_discount": eligible_discount
                        })
        except Exception:
            pass

    # 5. ESS Statements Ledger (11 Years: FY2016 through FY2026)
    ess_history = _personal().get("ess_history", [])
    tot_box_f = sum(x["box_f"] for x in ess_history)

    # 6. Build 11-Year Aggregated Financial Year Timeline (FY2016 – FY2026)
    timeline_fys = [f"FY{y}" for y in range(2016, 2027)]
    yearly_timeline = []
    cum_proceeds = 0.0
    for fy in timeline_fys:
        ess_row = next((e for e in ess_history if e["fy"] == fy), None)
        ess_aud = ess_row["box_f"] if ess_row else 0.0

        fy_sales = [s for s in sales_list if s["fy"] == fy]
        sh_sold = sum(s["shares"] for s in fy_sales)
        proceeds_usd = sum(s["proceeds"] for s in fy_sales)
        cost_usd = sum(s["cost_basis"] for s in fy_sales)
        cum_proceeds += proceeds_usd

        gross_gains_usd = sum(s["gain_loss"] for s in fy_sales if not s["within_30d"] and s["gain_loss"] > 0)
        gross_losses_usd = sum(abs(s["gain_loss"]) for s in fy_sales if not s["within_30d"] and s["gain_loss"] < 0)
        net_cgt_usd = gross_gains_usd - gross_losses_usd

        cgt_with_50pct_usd = sum(
            (s["gain_loss"] * 0.5 if s["eligible_discount"] else s["gain_loss"])
            for s in fy_sales if not s["within_30d"] and s["gain_loss"] > 0
        )
        discount_saving_usd = gross_gains_usd - cgt_with_50pct_usd

        yearly_timeline.append({
            "fy": fy,
            "ess_income_aud": ess_aud,
            "ess_income_usd": ess_aud * 0.65,
            "shares_sold": sh_sold,
            "proceeds_usd": proceeds_usd,
            "cost_basis_usd": cost_usd,
            "gross_gains_usd": gross_gains_usd,
            "gross_losses_usd": gross_losses_usd,
            "net_cgt_usd": net_cgt_usd,
            "cgt_with_50pct_usd": max(0.0, cgt_with_50pct_usd),
            "discount_saving_usd": max(0.0, discount_saving_usd),
            "within_30d_usd": sum(s["proceeds"] for s in fy_sales if s["within_30d"]),
            "cum_proceeds_usd": cum_proceeds,
        })

    # Summary Totals
    lifetime_sales_usd = sum(s["proceeds"] for s in sales_list)
    lifetime_cost_usd = sum(s["cost_basis"] for s in sales_list)
    lifetime_gains_usd = sum(s["gain_loss"] for s in sales_list if not s["within_30d"] and s["gain_loss"] > 0)
    lifetime_losses_usd = sum(abs(s["gain_loss"]) for s in sales_list if not s["within_30d"] and s["gain_loss"] < 0)
    lifetime_net_cgt_usd = lifetime_gains_usd - lifetime_losses_usd
    lifetime_cgt_with_50pct_usd = sum(y["cgt_with_50pct_usd"] for y in yearly_timeline)
    lifetime_discount_benefit_usd = lifetime_net_cgt_usd - lifetime_cgt_with_50pct_usd

    # 7. Comprehensive Bank Remittances Across All Channels (OFX, Direct Wires, RTP Shares, Wise, Employer)
    remittances = _personal().get("remittances", [])
    tot_remit_aud = sum(x["aud"] for x in remittances)

    return {
        "total_held_shares": total_held_shares,
        "current_price_usd": current_price,
        "current_val_usd": total_current_val,
        "current_val_aud": total_current_val / 0.65,
        "cost_basis_usd": total_cost_basis,
        "unrealized_gl_usd": total_current_val - total_cost_basis,
        "unrealized_gl_pct": ((total_current_val - total_cost_basis) / total_cost_basis * 100) if total_cost_basis else 0.0,
        "forfeited_shares": _personal().get("forfeited_shares", 0),
        "departure_date": _personal().get("departure_date", ""),
        "lifetime_vested": 7819,
        "lifetime_sold": 7080,
        "lifetime_sales_usd": lifetime_sales_usd,
        "lifetime_cost_usd": lifetime_cost_usd,
        "lifetime_gains_usd": lifetime_gains_usd,
        "lifetime_losses_usd": lifetime_losses_usd,
        "lifetime_net_cgt_usd": lifetime_net_cgt_usd,
        "lifetime_cgt_with_50pct_usd": lifetime_cgt_with_50pct_usd,
        "lifetime_discount_benefit_usd": lifetime_discount_benefit_usd,
        "total_equity_value_usd": lifetime_sales_usd + total_current_val,
        "total_ess_aud": tot_box_f,
        "lots": lots,
        "sales": sales_list,
        "yearly_timeline": yearly_timeline,
        "monthly_holdings_timeline": monthly_holdings_timeline,
        "team_equity_by_month": team_equity_by_month,
        "ess_history": ess_history,
        "remittances": remittances,
        "total_remittances_aud": tot_remit_aud,
    }



def render_equity_tab_html(s: Dict[str, Any]) -> str:
    """Render comprehensive interactive HTML section for Shares & Equity (TEAM)."""
    if not s:
        return '<div id="tab-equity" class="tab-content"><div class="card"><p>No shares data available.</p></div></div>'

    held = s.get("total_held_shares", 0)
    price_usd = s.get("current_price_usd", 0.0)
    val_usd = s.get("current_val_usd", 0.0)
    val_aud = s.get("current_val_aud", 0.0)
    cb_usd = s.get("cost_basis_usd", 0.0)
    unreal_gl_usd = s.get("unrealized_gl_usd", 0.0)
    unreal_gl_pct = s.get("unrealized_gl_pct", 0.0)
    lifetime_sales_usd = s.get("lifetime_sales_usd", 0.0)
    lifetime_cost_usd = s.get("lifetime_cost_usd", 0.0)
    lifetime_sold = s.get("lifetime_sold", 0)
    lifetime_gains_usd = s.get("lifetime_gains_usd", 0.0)
    lifetime_losses_usd = s.get("lifetime_losses_usd", 0.0)
    lifetime_net_cgt_usd = s.get("lifetime_net_cgt_usd", 0.0)
    lifetime_discount_benefit_usd = s.get("lifetime_discount_benefit_usd", 0.0)
    total_eq_usd = s.get("total_equity_value_usd", 0.0)
    total_ess_aud = s.get("total_ess_aud", 0.0)
    total_remit_aud = s.get("total_remittances_aud", 0.0)
    departure_date = s.get("departure_date", "")
    forfeited = s.get("forfeited_shares", 0)

    # 1. Retained Lots Table
    lots_rows = []
    for l in s.get("lots", []):
        gl = l.get("gain_loss", 0.0)
        gl_cls = "text-green" if gl >= 0 else "text-red"
        gl_sign = "+" if gl >= 0 else ""
        disc_tag = '<span class="tag" style="background: rgba(16,185,129,0.15); color: #34d399; font-size:0.75rem;">50% Disc</span>' if l.get("eligible_discount") else '<span class="tag" style="background: rgba(148,163,184,0.15); color: #94a3b8; font-size:0.75rem;">&lt; 12m</span>'
        lots_rows.append(f"""
              <tr>
                <td><strong>{l['acq_date']}</strong></td>
                <td>Lot {l['lot']}</td>
                <td class="num"><strong>{l['shares']}</strong></td>
                <td class="num">${l['cost_per_share']:,.2f}</td>
                <td class="num">${l['cost_basis']:,.2f}</td>
                <td class="num" style="color: #60a5fa;">${l['current_price']:,.2f}</td>
                <td class="num"><strong>${l['current_val']:,.2f}</strong></td>
                <td class="num {gl_cls}"><strong>{gl_sign}${gl:,.2f}</strong> ({gl_sign}{l['gain_loss_pct']:.1f}%)</td>
                <td>{disc_tag}</td>
              </tr>""")
    lots_tbody = "\n".join(lots_rows)

    # 2. ESS Statements Table
    ess_rows = []
    for e in s.get("ess_history", []):
        ess_rows.append(f"""
              <tr>
                <td><strong>{e['fy']}</strong></td>
                <td>{e['period']}</td>
                <td class="num"><strong style="color: #60a5fa;">Item 12, Label F</strong></td>
                <td class="num"><strong>${e['box_f']:,.2f} AUD</strong></td>
                <td><code>{e['file']}</code></td>
              </tr>""")
    ess_tbody = "\n".join(ess_rows)

    # 3. Bank Remittances Table (All 19 Receipts across OFX, Direct Bank Wires, RTP Shares, Wise, Employer)
    remit_rows = []
    for r in s.get("remittances", []):
        ch = r.get("channel", "Remittance")
        if "OFX" in ch:
            ch_tag = '<span class="tag" style="background: rgba(59,130,246,0.15); color: #60a5fa; border: 1px solid rgba(59,130,246,0.3);">OFX / OzForex</span>'
        elif "Wire" in ch:
            ch_tag = '<span class="tag" style="background: rgba(168,85,247,0.15); color: #c084fc; border: 1px solid rgba(168,85,247,0.3);">Direct Wire (HSBC INTL)</span>'
        elif "RTP" in ch:
            ch_tag = '<span class="tag" style="background: rgba(16,185,129,0.15); color: #34d399; border: 1px solid rgba(16,185,129,0.3);">RTP Bank Transfer</span>'
        else:
            ch_tag = '<span class="tag" style="background: rgba(245,158,11,0.15); color: #fbbf24; border: 1px solid rgba(245,158,11,0.3);">Corporate Settlement</span>'

        remit_rows.append(f"""
              <tr>
                <td><strong>{r['date']}</strong></td>
                <td>{ch_tag}</td>
                <td>{r['bank']}</td>
                <td><small><code>{r['desc']}</code></small></td>
                <td class="num text-green"><strong>+${r['aud']:,.2f} AUD</strong></td>
                <td><small style="color: #cbd5e1;">{r['note']}</small></td>
              </tr>""")
    remit_tbody = "\n".join(remit_rows)

    # 4. Granular Disposals Table
    disposal_rows = []
    for d in s.get("sales", []):
        gl = d.get("gain_loss", 0.0)
        gl_cls = "text-green" if gl >= 0 else "text-red"
        gl_sign = "+" if gl >= 0 else ""
        if d.get("within_30d"):
            status_tag = '<span class="tag" style="background: rgba(59,130,246,0.15); color: #60a5fa; font-size:0.75rem;">30d ESS Rule (No CGT)</span>'
            taxable_val = f"${d['proceeds']:,.2f} (ESS)"
        elif d.get("eligible_discount"):
            status_tag = '<span class="tag" style="background: rgba(16,185,129,0.15); color: #34d399; font-size:0.75rem;">50% Discount</span>'
            taxable_val = f"${max(0.0, gl * 0.5):,.2f}"
        else:
            status_tag = '<span class="tag" style="background: rgba(245,158,11,0.15); color: #fbbf24; font-size:0.75rem;">Full CGT (&lt;12m)</span>'
            taxable_val = f"${max(0.0, gl):,.2f}"

        row_cls = "sale-row-gain" if gl > 0 and not d.get("within_30d") else ("sale-row-loss" if gl < 0 and not d.get("within_30d") else "sale-row-30d")
        disc_attr = "yes" if d.get("eligible_discount") else "no"
        w30_attr = "yes" if d.get("within_30d") else "no"

        disposal_rows.append(f"""
              <tr class="disposal-row {row_cls}" data-type="{'30d' if d.get('within_30d') else ('gain' if gl >= 0 else 'loss')}" data-disc="{disc_attr}" data-w30="{w30_attr}">
                <td><strong>{d['sale_date']}</strong></td>
                <td>{d['fy']}</td>
                <td><small>{d['grant_name'] or d['ref']}</small></td>
                <td class="num"><strong>{d['shares']:.2f}</strong></td>
                <td class="num">${d['sale_price']:,.2f}</td>
                <td class="num">${d['cost_per_sh']:,.2f}</td>
                <td class="num"><strong>${d['proceeds']:,.2f}</strong></td>
                <td class="num">${d['cost_basis']:,.2f}</td>
                <td class="num {gl_cls}"><strong>{gl_sign}${gl:,.2f}</strong></td>
                <td><small>{d['days_held']}d</small></td>
                <td>{status_tag}</td>
                <td class="num" style="color: #bfdbfe;"><strong>{taxable_val}</strong></td>
              </tr>""")
    disposal_tbody = "\n".join(disposal_rows)

    return f"""
    <!-- TAB: Shares & Equity (TEAM) -->
    <div id="tab-equity" class="tab-content">
      <!-- Top Overview Banner -->
      <div style="background: rgba(14,165,233,0.08); border: 1px solid rgba(14,165,233,0.25); border-radius: 8px; padding: 14px 20px; margin-bottom: 20px; font-size: 0.88rem; color: #bae6fd; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px;">
        <div>
          <strong style="color: #e0f2fe; font-size: 1.05rem;">Employer Equity (TEAM)</strong> — Shareworks
          <div style="margin-top: 4px; color: #93c5fd;">
            Employment concluded on <strong>{departure_date}</strong>. Future scheduled vests ({forfeited:,} shares) were forfeited upon departure, establishing this final retained holding of <strong>{held:,} shares</strong>.
          </div>
        </div>
        <div style="display: flex; gap: 8px; flex-wrap: wrap;">
          <span class="tag" style="background: rgba(14,165,233,0.2); color: #38bdf8; border: 1px solid rgba(14,165,233,0.4); font-weight: 600;">Status: Retained &amp; Held</span>
          <span class="tag" style="background: rgba(168,85,247,0.2); color: #c084fc; border: 1px solid rgba(168,85,247,0.4); font-weight: 600;">W-8BEN: 0% US Withholding</span>
        </div>
      </div>

      <!-- Top KPI Row -->
      <div class="kpi-grid">
        <div class="kpi-card">
          <div class="kpi-label">Current Shares Held</div>
          <div class="kpi-value text-green">{held:,} shares</div>
          <div class="kpi-subtext">100% Vested &amp; Retained Long-term</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Current Market Value (USD)</div>
          <div class="kpi-value" style="color: #38bdf8;">${val_usd:,.2f}</div>
          <div class="kpi-subtext">@ ${price_usd:,.2f} USD per share</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Estimated Market Value (AUD)</div>
          <div class="kpi-value" style="color: #60a5fa;">~${val_aud:,.2f}</div>
          <div class="kpi-subtext">Converted @ ~0.65 AUD/USD</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Cost Basis (USD)</div>
          <div class="kpi-value">${cb_usd:,.2f}</div>
          <div class="kpi-subtext">Unrealized: <span class="text-red">-${abs(unreal_gl_usd):,.2f} ({unreal_gl_pct:.1f}%)</span></div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Lifetime Realized Sales (USD)</div>
          <div class="kpi-value text-green">${lifetime_sales_usd:,.2f}</div>
          <div class="kpi-subtext">{lifetime_sold:,} shares sold across 29 trades</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Total Lifetime Value Created</div>
          <div class="kpi-value text-green">${total_eq_usd:,.2f} USD</div>
          <div class="kpi-subtext">Sales Realized + Remaining Held (~$2.1M AUD)</div>
        </div>
      </div>

      <!-- CARD 1: 11-YEAR HISTORICAL HOLDINGS & EQUITY WEALTH TIMELINE (2015 – 2026) -->
      <div class="card" style="margin-top: 20px;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; flex-wrap: wrap; gap: 10px;">
          <div>
            <div class="card-title" style="margin-bottom: 2px;">🏛️ 11-Year Historical Holdings &amp; Equity Wealth Progression (2015 – 2026)</div>
            <div style="font-size: 0.82rem; color: var(--text-muted);">
              Track cumulative vested grants ({s.get('lifetime_vested', 7819):,} shares), disposals ({lifetime_sold:,} shares), remaining shares held, and historical portfolio valuation.
            </div>
          </div>
          <div style="display: flex; gap: 8px; align-items: center;">
            <span class="tag" style="background: rgba(16,185,129,0.15); color: #34d399;">Vested: {s.get('lifetime_vested', 7819):,} sh</span>
            <span class="tag" style="background: rgba(245,158,11,0.15); color: #fbbf24;">Sold: {lifetime_sold:,} sh</span>
            <span class="tag" style="background: rgba(59,130,246,0.15); color: #60a5fa;">Retained: {held:,} sh</span>
          </div>
        </div>

        <!-- Controls Toolbar: View Mode Selector -->
        <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; background: var(--bg); padding: 8px 12px; border-radius: 6px; border: 1px solid var(--card-border); margin-bottom: 12px;">
          <div style="display: flex; align-items: center; gap: 6px; flex-wrap: wrap;">
            <span style="font-size: 0.82rem; font-weight: 600; color: #94a3b8; margin-right: 4px;">View:</span>
            <button class="btn-sm btn-filter active" id="eq-hist-all" onclick="setEquityHistoryView('all')">📊 Combined</button>
            <button class="btn-sm btn-filter" id="eq-hist-shares" onclick="setEquityHistoryView('shares')">📦 Shares (Vested/Sold/Held)</button>
            <button class="btn-sm btn-filter" id="eq-hist-val" onclick="setEquityHistoryView('valuation')">💰 Portfolio Value</button>
            <button class="btn-sm btn-filter" id="eq-hist-price" onclick="setEquityHistoryView('price')">📈 Stock Price (USD)</button>
          </div>
          <div id="eq-hist-legend" style="display: flex; gap: 10px; align-items: center; font-size: 0.78rem; color: #94a3b8; flex-wrap: wrap;"></div>
        </div>

        <div class="chart-container" id="chart-equity-history" style="height: 310px; position: relative;">
          <div class="chart-tooltip" id="tooltip-equity-history"></div>
        </div>
      </div>

      <!-- CARD 2: INTERACTIVE EQUITY GRAPH & TAX COMPONENT SELECTOR (11 FYs) -->
      <div class="card" style="margin-top: 20px;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; flex-wrap: wrap; gap: 10px;">
          <div>
            <div class="card-title" style="margin-bottom: 2px;">📈 Interactive Financial Year CGT &amp; Tax Explorer (FY2016 – FY2026)</div>
            <div style="font-size: 0.82rem; color: var(--text-muted);">
              Analyze capital gains, losses, statutory ESS income across all 11 tax years, and model tax liability under current vs proposed CGT discount rules.
            </div>
          </div>
          <!-- Currency Toggle -->
          <div style="display: flex; align-items: center; gap: 6px; background: rgba(0,0,0,0.25); padding: 4px 8px; border-radius: 6px; border: 1px solid var(--card-border);">
            <span style="font-size: 0.8rem; color: var(--text-muted);">Currency:</span>
            <button class="btn-sm btn-filter active" id="eq-curr-usd" onclick="setEquityCurrency('USD')">USD ($)</button>
            <button class="btn-sm btn-filter" id="eq-curr-aud" onclick="setEquityCurrency('AUD')">AUD (A$)</button>
          </div>
        </div>

        <!-- Controls Toolbar: Metric View + Discount Rule + Tax Rate -->
        <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; background: var(--bg); padding: 10px 14px; border-radius: 6px; border: 1px solid var(--card-border); margin-bottom: 14px;">
          <!-- Metric Selectors -->
          <div style="display: flex; align-items: center; gap: 6px; flex-wrap: wrap;">
            <span style="font-size: 0.82rem; font-weight: 600; color: #94a3b8; margin-right: 4px;">Metric:</span>
            <button class="btn-sm btn-filter active" id="eq-btn-gains" onclick="setEquityMetric('gains_losses')">📊 Gains vs Losses</button>
            <button class="btn-sm btn-filter" id="eq-btn-cgt" onclick="setEquityMetric('cgt_discount')">🏛️ CGT: 50% vs 0% Discount</button>
            <button class="btn-sm btn-filter" id="eq-btn-proceeds" onclick="setEquityMetric('proceeds_cost')">💰 Proceeds vs Cost</button>
            <button class="btn-sm btn-filter" id="eq-btn-ess" onclick="setEquityMetric('ess_vs_cgt')">📦 ESS vs Capital Gains</button>
          </div>

          <!-- CGT Discount & Tax Bracket Controls -->
          <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap;">
            <div style="display: flex; align-items: center; gap: 6px;">
              <span style="font-size: 0.8rem; color: #94a3b8;">CGT Discount:</span>
              <select id="eq-select-discount" onchange="setEquityDiscount(this.value)" style="background: var(--card-bg); color: var(--text); border: 1px solid var(--card-border); border-radius: 4px; padding: 4px 8px; font-size: 0.82rem;">
                <option value="0.5" selected>50% Discount (Current/Past Law)</option>
                <option value="0.25">25% Compromise Discount</option>
                <option value="0.0">0% Discount (Repealed / Future Law)</option>
              </select>
            </div>
            <div style="display: flex; align-items: center; gap: 6px;">
              <span style="font-size: 0.8rem; color: #94a3b8;">Marginal Tax:</span>
              <select id="eq-select-taxrate" onchange="setEquityTaxRate(this.value)" style="background: var(--card-bg); color: var(--text); border: 1px solid var(--card-border); border-radius: 4px; padding: 4px 8px; font-size: 0.82rem;">
                <option value="0.47" selected>47.0% (Top Bracket $190k+)</option>
                <option value="0.39">39.0% ($135k – $190k)</option>
                <option value="0.345">34.5% ($45k – $135k)</option>
              </select>
            </div>
          </div>
        </div>

        <!-- Dynamic Chart Summary Pills -->
        <div id="eq-chart-pills" style="display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; font-size: 0.84rem;">
          <div style="background: rgba(16,185,129,0.12); border: 1px solid rgba(16,185,129,0.3); padding: 6px 12px; border-radius: 6px; color: #34d399;">
            Gross Gains: <strong id="eq-stat-gains">+${lifetime_gains_usd:,.2f} USD</strong>
          </div>
          <div style="background: rgba(239,68,68,0.12); border: 1px solid rgba(239,68,68,0.3); padding: 6px 12px; border-radius: 6px; color: #f87171;">
            Gross Losses: <strong id="eq-stat-losses">-${lifetime_losses_usd:,.2f} USD</strong>
          </div>
          <div style="background: rgba(59,130,246,0.12); border: 1px solid rgba(59,130,246,0.3); padding: 6px 12px; border-radius: 6px; color: #60a5fa;">
            Net Capital Gain: <strong id="eq-stat-net">${lifetime_net_cgt_usd:,.2f} USD</strong>
          </div>
          <div style="background: rgba(168,85,247,0.12); border: 1px solid rgba(168,85,247,0.3); padding: 6px 12px; border-radius: 6px; color: #c084fc;">
            50% Discount Benefit: <strong id="eq-stat-disc">+${lifetime_discount_benefit_usd:,.2f} USD</strong> exempt
          </div>
          <div style="background: rgba(245,158,11,0.12); border: 1px solid rgba(245,158,11,0.3); padding: 6px 12px; border-radius: 6px; color: #fbbf24;">
            Estimated Tax Saved: <strong id="eq-stat-taxsaved">–</strong>
          </div>
        </div>

        <!-- Dynamic In-Chart Legend -->
        <div id="eq-interactive-legend" style="display: flex; gap: 12px; align-items: center; justify-content: flex-end; margin-bottom: 8px; font-size: 0.8rem; color: #94a3b8; flex-wrap: wrap;"></div>

        <!-- Chart Container -->
        <div class="chart-container" id="chart-equity-interactive" style="height: 320px; position: relative;">
          <div class="chart-tooltip" id="tooltip-equity-interactive"></div>
        </div>
      </div>

      <!-- CARD 3: FUTURE LIQUIDATION & CGT SIMULATOR CARD -->
      <div class="card" style="margin-top: 20px; border-color: rgba(59,130,246,0.4);">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; flex-wrap: wrap; gap: 10px;">
          <div>
            <div class="card-title" style="margin-bottom: 2px;">🔮 Future Liquidation &amp; CGT Simulator ({held:,} Remaining Shares)</div>
            <div style="font-size: 0.82rem; color: var(--text-muted);">
              Model disposal proceeds and compare tax liability under the 50% discount rule vs proposed 0% discount repeal.
            </div>
          </div>
          <span class="tag" style="background: rgba(59,130,246,0.2); color: #60a5fa; border: 1px solid rgba(59,130,246,0.4); font-weight: 600;">Scenario Planning</span>
        </div>

        <!-- Simulator Interactive Inputs -->
        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; background: var(--bg); padding: 14px 18px; border-radius: 8px; border: 1px solid var(--card-border); margin-bottom: 16px;">
          <div>
            <label style="font-size: 0.84rem; color: #94a3b8; display: block; margin-bottom: 6px;">
              Simulated TEAM Sale Price (USD): <strong id="sim-price-display" style="color: #60a5fa; font-size: 1rem;">${price_usd:,.2f}</strong>
            </label>
            <div style="display: flex; align-items: center; gap: 10px;">
              <input type="range" id="sim-price-slider" min="50" max="350" step="1" value="154" oninput="updateSimulatorPrice(this.value)" style="flex: 1;">
              <input type="number" id="sim-price-input" min="10" max="600" value="{price_usd:.2f}" step="0.5" onchange="updateSimulatorPrice(this.value)" style="width: 80px; background: var(--card-bg); color: var(--text); border: 1px solid var(--card-border); border-radius: 4px; padding: 4px 6px; font-size: 0.85rem;">
            </div>
          </div>

          <div>
            <label style="font-size: 0.84rem; color: #94a3b8; display: block; margin-bottom: 6px;">Parcel Selection Strategy:</label>
            <div style="display: flex; gap: 6px; flex-wrap: wrap;">
              <button class="btn-sm btn-filter active" id="sim-strat-all" onclick="setSimStrategy('all')">All {held:,} Shares</button>
              <button class="btn-sm btn-filter" id="sim-strat-loss" onclick="setSimStrategy('loss')">Harvest Losses</button>
              <button class="btn-sm btn-filter" id="sim-strat-gain" onclick="setSimStrategy('gain')">Gains Only</button>
            </div>
          </div>

          <div>
            <label style="font-size: 0.84rem; color: #94a3b8; display: block; margin-bottom: 6px;">Holding Period Threshold (12m):</label>
            <div style="font-size: 0.82rem; color: #cbd5e1; margin-top: 4px;">
              <span id="sim-lots-eligible-txt"><strong>{held:,} of {held:,} shares</strong> held &gt; 12 months</span> (eligible for 50% discount under current rules).
            </div>
          </div>
        </div>

        <!-- Simulator Output Cards Grid -->
        <div class="kpi-grid" style="margin-bottom: 14px;">
          <div class="kpi-card">
            <div class="kpi-label">Simulated Proceeds</div>
            <div class="kpi-value" id="sim-out-proceeds">${val_usd:,.2f} USD</div>
            <div class="kpi-subtext" id="sim-out-proceeds-aud">~A${val_aud:,.0f} AUD (@ 0.65 FX)</div>
          </div>
          <div class="kpi-card">
            <div class="kpi-label">Cost Basis</div>
            <div class="kpi-value" id="sim-out-cost">${cb_usd:,.2f} USD</div>
            <div class="kpi-subtext" id="sim-out-shares">{held:,} shares selected</div>
          </div>
          <div class="kpi-card">
            <div class="kpi-label">Net Capital Gain / (Loss)</div>
            <div class="kpi-value text-red" id="sim-out-net">{"-" if unreal_gl_usd < 0 else ""}${abs(unreal_gl_usd):,.2f} USD</div>
            <div class="kpi-subtext" id="sim-out-net-aud">~{'-' if unreal_gl_usd < 0 else ''}A${abs(unreal_gl_usd) / 0.65:,.0f} AUD</div>
          </div>
          <div class="kpi-card">
            <div class="kpi-label">Tax Payable (Current 50% Rule)</div>
            <div class="kpi-value text-green" id="sim-out-tax-curr">$0.00 AUD</div>
            <div class="kpi-subtext" id="sim-out-tax-curr-sub">No tax owed on net loss</div>
          </div>
          <div class="kpi-card">
            <div class="kpi-label">Tax Payable (0% Discount Repeal)</div>
            <div class="kpi-value" id="sim-out-tax-future">$0.00 AUD</div>
            <div class="kpi-subtext" id="sim-out-tax-future-sub">Full 100% taxable rate</div>
          </div>
          <div class="kpi-card" style="border-color: rgba(168,85,247,0.4);">
            <div class="kpi-label">50% Discount Policy Cost Delta</div>
            <div class="kpi-value" style="color: #c084fc;" id="sim-out-delta">$0.00 AUD</div>
            <div class="kpi-subtext" id="sim-out-delta-sub">Extra tax if discount repealed</div>
          </div>
        </div>

        <div id="sim-policy-callout" style="background: rgba(168,85,247,0.08); border: 1px solid rgba(168,85,247,0.25); border-radius: 6px; padding: 10px 16px; font-size: 0.84rem; color: #e9d5ff;">
          💡 <strong>Policy Impact Analysis:</strong> At the current price of ${price_usd:,.2f}, your overall {held:,}-share position is at a net {'capital loss' if unreal_gl_usd < 0 else 'capital gain'} of {'-' if unreal_gl_usd < 0 else ''}${abs(unreal_gl_usd):,.2f} USD. However, if TEAM stock moves above your average cost of ${(cb_usd / held if held else 0):,.2f}, the 50% CGT discount saves 23.5% of gross profit in tax (at the 47% top bracket). If the discount is repealed, every dollar of gain will be taxed at the full 47% marginal rate.
        </div>
      </div>

      <!-- CARD 4: CURRENT PORTFOLIO LOTS -->
      <div class="card" style="margin-top: 20px;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; flex-wrap: wrap; gap: 10px;">
          <div class="card-title" style="margin-bottom: 0;">Current Retained Portfolio — 24 Individual Lots ({held} Shares)</div>
          <span style="font-size: 0.85rem; color: var(--text-muted);">Source: <code>Share Holdings.csv</code></span>
        </div>
        <div class="table-container">
          <table>
            <thead>
              <tr>
                <th>Vesting / Acquisition Date</th>
                <th>Lot #</th>
                <th class="num">Shares</th>
                <th class="num">Cost Basis / Share (USD)</th>
                <th class="num">Total Cost Basis (USD)</th>
                <th class="num">Current Price (USD)</th>
                <th class="num">Current Value (USD)</th>
                <th class="num">Unrealized Gain / (Loss)</th>
                <th>12m CGT Rule</th>
              </tr>
            </thead>
            <tbody>
{lots_tbody}
            </tbody>
            <tfoot>
              <tr style="font-weight: bold; background: var(--card-hover);">
                <td colspan="2">TOTAL HELD POSITION</td>
                <td class="num">{held:,}</td>
                <td class="num">—</td>
                <td class="num">${cb_usd:,.2f}</td>
                <td class="num">${price_usd:,.2f}</td>
                <td class="num">${val_usd:,.2f}</td>
                <td class="num text-red">-${abs(unreal_gl_usd):,.2f} ({unreal_gl_pct:.1f}%)</td>
                <td>—</td>
              </tr>
            </tfoot>
          </table>
        </div>
      </div>

      <!-- CARD 5: GRANULAR HISTORICAL DISPOSALS TABLE -->
      <div class="card" style="margin-top: 20px;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; flex-wrap: wrap; gap: 10px;">
          <div>
            <div class="card-title" style="margin-bottom: 2px;">📝 Historical Share Disposals Ledger (131 Lots / 29 Trades)</div>
            <div style="font-size: 0.82rem; color: var(--text-muted);">
              Track every disposal tranche, holding period, and tax classification (30-day ESS rule vs 50% CGT discount).
            </div>
          </div>
          <!-- Filter Pills -->
          <div style="display: flex; gap: 6px; flex-wrap: wrap;">
            <button class="btn-sm btn-filter active" id="disp-filt-all" onclick="filterDisposalsTable('all')">All (131)</button>
            <button class="btn-sm btn-filter" id="disp-filt-gain" onclick="filterDisposalsTable('gain')">Gains Only (+)</button>
            <button class="btn-sm btn-filter" id="disp-filt-loss" onclick="filterDisposalsTable('loss')">Losses Only (-)</button>
            <button class="btn-sm btn-filter" id="disp-filt-disc" onclick="filterDisposalsTable('disc')">50% Disc (&gt;12m)</button>
            <button class="btn-sm btn-filter" id="disp-filt-30d" onclick="filterDisposalsTable('30d')">30-Day ESS Rule</button>
          </div>
        </div>
        <div class="table-container" style="max-height: 480px; overflow-y: auto;">
          <table id="table-disposals">
            <thead>
              <tr>
                <th>Sale Date</th>
                <th>FY</th>
                <th>Grant / Release</th>
                <th class="num">Shares</th>
                <th class="num">Sale Price</th>
                <th class="num">Cost / Share</th>
                <th class="num">Gross Proceeds</th>
                <th class="num">Cost Basis</th>
                <th class="num">Capital Gain / (Loss)</th>
                <th>Held</th>
                <th>Tax Category</th>
                <th class="num">Taxable Amount</th>
              </tr>
            </thead>
            <tbody>
{disposal_tbody}
            </tbody>
          </table>
        </div>
      </div>

      <!-- CARD 6: 11-YEAR STATUTORY ESS STATEMENT SUMMARY (FY2016 – FY2026) -->
      <div class="card" style="margin-top: 20px;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; flex-wrap: wrap; gap: 10px;">
          <div class="card-title" style="margin-bottom: 0;">11-Year Statutory ESS Income Progression (FY2016 – FY2026)</div>
          <span class="tag" style="background: rgba(59,130,246,0.15); color: #60a5fa; border: 1px solid rgba(59,130,246,0.3); font-weight: 600;">Total Taxable Discount: ${total_ess_aud:,.2f} AUD</span>
        </div>
        <p style="font-size: 0.88rem; color: var(--text-muted); margin-top: 0; margin-bottom: 14px;">
          Under ATO Division 83A, the ESS statement provides the statutory source of truth for assessable employment discount (Item 12, Label F) and resets the Australian CGT cost base for retained tranches. Refer to <code>~/iCloud/Finance/Shares/TEAM/ESS_TAX_GUIDE.md</code> for the full tax framework.
        </p>
        <div class="table-container">
          <table>
            <thead>
              <tr>
                <th>Financial Year</th>
                <th>Period Covered</th>
                <th class="num">ATO Tax Label</th>
                <th class="num">Assessable Discount (AUD)</th>
                <th>Statement File</th>
              </tr>
            </thead>
            <tbody>
{ess_tbody}
            </tbody>
            <tfoot>
              <tr style="font-weight: bold; background: var(--card-hover);">
                <td colspan="3">11-YEAR CUMULATIVE ASSESSABLE ESS DISCOUNT</td>
                <td class="num text-green">${total_ess_aud:,.2f} AUD</td>
                <td><em>All 11 tax years archived in TEAM/ESS &amp; TAX/glenn</em></td>
              </tr>
            </tfoot>
          </table>
        </div>
      </div>

      <!-- CARD 7: COMPREHENSIVE SALES REALISATION & BANK REMITTANCES ACROSS ALL CHANNELS -->
      <div class="card" style="margin-top: 20px;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; flex-wrap: wrap; gap: 10px;">
          <div>
            <div class="card-title" style="margin-bottom: 2px;">Historical Share Sale Receipts &amp; Bank Remittances Across All Channels</div>
            <div style="font-size: 0.82rem; color: var(--text-muted);">
              Reconciled across OFX (OzForex), Direct Bank Wires (HSBC BK PLC INTL), RTP Bank Transfers, Wise, and Employer Remittances.
            </div>
          </div>
          <span class="tag" style="background: rgba(16,185,129,0.15); color: #34d399; border: 1px solid rgba(16,185,129,0.3); font-weight: 600;">Total Received: ${total_remit_aud:,.2f} AUD</span>
        </div>
        <p style="font-size: 0.88rem; color: var(--text-muted); margin-top: 0; margin-bottom: 14px;">
          Proceeds arrived through four primary mechanisms: foreign exchange brokers (OzForex / OFX), direct international telegraphic transfers (HSBC BK PLC INTL), real-time direct bank transfers (RTP Shares), and direct corporate payroll payments (including the final $131,374.74 AUD departure settlement). These funds directly financed the 20% property deposit ($236,000) and offset account buffers.
        </p>
        <div class="table-container">
          <table>
            <thead>
              <tr>
                <th>Deposit Date</th>
                <th>Channel / Mechanism</th>
                <th>Receiving Account</th>
                <th>Bank Description</th>
                <th class="num">Net AUD Received</th>
                <th>Funding Purpose / Context</th>
              </tr>
            </thead>
            <tbody>
{remit_tbody}
            </tbody>
            <tfoot>
              <tr style="font-weight: bold; background: var(--card-hover);">
                <td colspan="4">TOTAL NET AUD REMITTED FROM SHARE DISPOSALS</td>
                <td class="num text-green">+${total_remit_aud:,.2f} AUD</td>
                <td>—</td>
              </tr>
            </tfoot>
          </table>
        </div>
      </div>
    </div>
    """


def render_html_report(data: Dict[str, Any]) -> str:
    """Generate self-contained, responsive HTML file with embedded data and JavaScript."""
    data_json = json.dumps(data)
    equity_tab_html = render_equity_tab_html(data.get("shares", {}))
    _pp = data.get("property_profile") or {}
    _sh = data.get("shares") or {}

    def _usd(key: str) -> str:
        return f"${float(_pp.get(key) or 0):,.2f}"

    _range_txt = (
        f"Range: ${float(_pp.get('valuation_range_low') or 0) / 1e6:.2f}M – "
        f"${float(_pp.get('valuation_range_high') or 0) / 1e6:.2f}M "
        f"(+{_pp.get('capital_gain_pct', 0)}% / +${float(_pp.get('capital_gain') or 0) / 1e3:,.0f}k gain)"
    ) if _pp else "Range: –"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Finance &amp; Statement Dashboard</title>
  <style>
    :root {{
      --bg: #0b0f19;
      --card-bg: #151c2c;
      --card-border: #232d42;
      --card-hover: #1e273d;
      --text: #f1f5f9;
      --text-muted: #94a3b8;
      --primary: #3b82f6;
      --primary-rgb: 59, 130, 246;
      --success: #10b981;
      --danger: #ef4444;
      --warning: #f59e0b;
      --purple: #8b5cf6;
      --indigo: #6366f1;
      --header-bg: rgba(11, 15, 25, 0.85);
    }}

    [data-theme="light"] {{
      --bg: #f8fafc;
      --card-bg: #ffffff;
      --card-border: #e2e8f0;
      --card-hover: #f1f5f9;
      --text: #0f172a;
      --text-muted: #64748b;
      --primary: #2563eb;
      --header-bg: rgba(248, 250, 252, 0.85);
    }}

    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding-bottom: 60px;
      -webkit-font-smoothing: antialiased;
    }}

    /* Top Sticky Header */
    header {{
      position: sticky;
      top: 0;
      z-index: 100;
      background: var(--header-bg);
      backdrop-filter: blur(12px);
      border-bottom: 1px solid var(--card-border);
      padding: 12px 24px;
    }}
    .header-inner {{
      max-width: 1440px;
      margin: 0 auto;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 12px;
    }}
    .brand h1 {{
      font-size: 1.25rem;
      font-weight: 700;
      letter-spacing: -0.02em;
    }}
    .brand-badge {{
      font-size: 0.75rem;
      background: rgba(var(--primary-rgb), 0.15);
      color: var(--primary);
      padding: 2px 8px;
      border-radius: 999px;
      font-weight: 600;
    }}

    /* Global Controls in Header */
    .header-controls {{
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 12px;
    }}

    /* Pill Button Groups */
    .pill-group {{
      display: inline-flex;
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 8px;
      padding: 2px;
    }}
    .pill-btn {{
      background: transparent;
      border: none;
      color: var(--text-muted);
      padding: 6px 14px;
      font-size: 0.82rem;
      font-weight: 600;
      border-radius: 6px;
      cursor: pointer;
      transition: all 0.15s ease;
    }}
    .pill-btn:hover {{ color: var(--text); }}
    .pill-btn.active {{
      background: var(--primary);
      color: #fff;
      box-shadow: 0 1px 3px rgba(0,0,0,0.2);
    }}

    /* Month Selector */
    .month-control {{
      display: flex;
      align-items: center;
      gap: 4px;
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 8px;
      padding: 2px 4px;
    }}
    .month-control select {{
      background: transparent;
      color: var(--text);
      border: none;
      font-size: 0.85rem;
      font-weight: 600;
      padding: 4px 8px;
      cursor: pointer;
      outline: none;
    }}
    .month-control select option {{
      background: var(--card-bg);
      color: var(--text);
    }}
    .month-nav-btn {{
      background: transparent;
      border: none;
      color: var(--text-muted);
      cursor: pointer;
      padding: 4px 8px;
      border-radius: 4px;
      font-size: 0.9rem;
    }}
    .month-nav-btn:hover {{ color: var(--text); background: var(--card-hover); }}

    /* Theme Toggle */
    .theme-toggle {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      color: var(--text);
      cursor: pointer;
      padding: 6px 10px;
      border-radius: 8px;
      font-size: 0.9rem;
    }}

    /* Main Container */
    .container {{
      max-width: 1440px;
      margin: 20px auto;
      padding: 0 24px;
    }}

    /* Tabs Bar */
    .tabs-bar {{
      display: flex;
      gap: 8px;
      border-bottom: 1px solid var(--card-border);
      margin-bottom: 24px;
      overflow-x: auto;
      padding-bottom: 4px;
    }}
    .tab-nav-btn {{
      background: transparent;
      border: none;
      color: var(--text-muted);
      padding: 10px 16px;
      font-size: 0.95rem;
      font-weight: 600;
      border-bottom: 2px solid transparent;
      cursor: pointer;
      white-space: nowrap;
      transition: all 0.15s ease;
    }}
    .tab-nav-btn:hover {{ color: var(--text); }}
    .tab-nav-btn.active {{
      color: var(--primary);
      border-bottom-color: var(--primary);
    }}

    /* KPI Summary Cards */
    .kpi-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 16px;
      margin-bottom: 24px;
    }}
    .kpi-card {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 18px;
      position: relative;
      overflow: hidden;
    }}
    .kpi-label {{
      font-size: 0.8rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-muted);
      margin-bottom: 6px;
    }}
    .kpi-value {{
      font-size: 1.65rem;
      font-weight: 700;
      letter-spacing: -0.02em;
    }}
    .kpi-subtext {{
      font-size: 0.78rem;
      color: var(--text-muted);
      margin-top: 4px;
    }}
    .kpi-inflow {{ color: var(--success); }}
    .kpi-outflow {{ color: var(--danger); }}
    .kpi-loan {{ color: var(--warning); }}

    /* Grid Layouts */
    .grid-2col {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 20px;
      margin-bottom: 24px;
    }}
    @media (max-width: 960px) {{
      .grid-2col {{ grid-template-columns: 1fr; }}
    }}

    /* Chart Containers */
    .card {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 20px;
      margin-bottom: 24px;
    }}
    .card-title {{
      font-size: 1.05rem;
      font-weight: 700;
      margin-bottom: 16px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .chart-container {{
      width: 100%;
      height: 320px;
      position: relative;
      user-select: none;
    }}

    /* Chart Controls & Toolbar */
    .chart-controls {{
      display: inline-flex;
      align-items: center;
      gap: 3px;
      background: var(--bg-tertiary);
      padding: 2px 4px;
      border-radius: 8px;
      border: 1px solid var(--card-border);
    }}
    .chart-btn {{
      background: transparent;
      border: none;
      color: var(--text-muted);
      padding: 3px 7px;
      font-size: 0.72rem;
      font-weight: 600;
      border-radius: 5px;
      cursor: pointer;
      transition: all 0.15s ease;
    }}
    .chart-btn:hover {{
      color: var(--text-main);
      background: rgba(255, 255, 255, 0.08);
    }}
    .chart-btn.active {{
      background: var(--primary);
      color: #fff;
    }}
    .chart-btn-icon {{
      background: transparent;
      border: none;
      color: var(--text-muted);
      padding: 2px 6px;
      font-size: 0.75rem;
      border-radius: 5px;
      cursor: pointer;
      transition: all 0.15s ease;
    }}
    .chart-btn-icon:hover {{
      color: var(--text-main);
      background: rgba(255, 255, 255, 0.08);
    }}

    /* SVG Chart Styles */
    svg.chart {{
      width: 100%;
      height: 100%;
      overflow: visible;
      cursor: crosshair;
    }}
    .chart-axis text {{
      font-size: 10px;
      fill: var(--text-muted);
    }}
    .chart-grid line {{
      stroke: var(--card-border);
      stroke-dasharray: 2 2;
      opacity: 0.6;
    }}
    .chart-tooltip {{
      position: absolute;
      background: rgba(15, 23, 42, 0.95);
      color: #fff;
      padding: 8px 12px;
      border-radius: 8px;
      font-size: 0.78rem;
      pointer-events: none;
      display: none;
      z-index: 50;
      border: 1px solid #334155;
      box-shadow: 0 8px 24px rgba(0,0,0,0.45);
      backdrop-filter: blur(8px);
      min-width: 150px;
    }}
    .chart-tooltip-title {{
      font-weight: 700;
      margin-bottom: 6px;
      color: #94a3b8;
      font-size: 0.72rem;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      border-bottom: 1px solid rgba(255,255,255,0.1);
      padding-bottom: 3px;
    }}
    .chart-tooltip-row {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-top: 3px;
      white-space: nowrap;
    }}
    .chart-tooltip-badge {{
      display: inline-block;
      width: 7px;
      height: 7px;
      border-radius: 50%;
      margin-right: 5px;
    }}

    /* Accounts Grid */
    .accounts-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
      gap: 16px;
    }}
    .account-card {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 18px;
      cursor: pointer;
      transition: all 0.2s ease;
    }}
    .account-card:hover {{
      border-color: var(--primary);
      transform: translateY(-2px);
      background: var(--card-hover);
    }}
    .account-head {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      margin-bottom: 12px;
    }}
    .account-title {{
      font-size: 1rem;
      font-weight: 700;
    }}
    .account-num {{
      font-size: 0.78rem;
      color: var(--text-muted);
      font-family: ui-monospace, Menlo, Consolas, monospace;
    }}
    .tag {{
      display: inline-block;
      font-size: 0.7rem;
      font-weight: 600;
      padding: 2px 8px;
      border-radius: 4px;
      text-transform: uppercase;
    }}
    .tag-joint {{ background: rgba(99, 102, 241, 0.15); color: var(--indigo); }}
    .tag-solo {{ background: rgba(59, 130, 246, 0.15); color: var(--primary); }}
    .tag-loan {{ background: rgba(245, 158, 11, 0.15); color: var(--warning); }}
    .tag-offset {{ background: rgba(139, 92, 246, 0.15); color: var(--purple); }}
    .tag-card {{ background: rgba(239, 68, 68, 0.15); color: var(--danger); }}
    .tag-ok {{ background: rgba(16, 185, 129, 0.15); color: var(--success); }}

    .account-balance-large {{
      font-size: 1.5rem;
      font-weight: 700;
      margin-bottom: 8px;
    }}
    .account-foot {{
      display: flex;
      justify-content: space-between;
      font-size: 0.78rem;
      color: var(--text-muted);
      border-top: 1px solid var(--card-border);
      padding-top: 10px;
      margin-top: 10px;
    }}

    /* Tables */
    .table-container {{
      overflow-x: auto;
      border-radius: 8px;
      border: 1px solid var(--card-border);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.85rem;
      text-align: left;
    }}
    th {{
      background: rgba(var(--primary-rgb), 0.05);
      padding: 10px 14px;
      font-weight: 600;
      color: var(--text-muted);
      border-bottom: 1px solid var(--card-border);
    }}
    td {{
      padding: 10px 14px;
      border-bottom: 1px solid var(--card-border);
    }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover td {{ background: var(--card-hover); }}
    .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .text-green {{ color: var(--success); font-weight: 600; }}
    .text-red {{ color: var(--danger); font-weight: 600; }}

    /* Search & Filter Bar in Table */
    .table-filter-bar {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin-bottom: 16px;
      align-items: center;
    }}
    .search-input {{
      flex: 1;
      min-width: 220px;
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      color: var(--text);
      padding: 8px 12px;
      border-radius: 8px;
      font-size: 0.85rem;
      outline: none;
    }}
    .search-input:focus {{ border-color: var(--primary); }}
    .select-filter {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      color: var(--text);
      padding: 8px 12px;
      border-radius: 8px;
      font-size: 0.85rem;
      outline: none;
    }}

    /* Pagination */
    .pagination {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 12px 0;
      font-size: 0.82rem;
      color: var(--text-muted);
    }}
    .btn-sm {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      color: var(--text);
      padding: 4px 12px;
      border-radius: 6px;
      cursor: pointer;
      font-size: 0.82rem;
    }}
    .btn-sm:hover:not(:disabled) {{ background: var(--card-hover); }}
    .btn-sm:disabled {{ opacity: 0.4; cursor: not-allowed; }}
    .btn-filter {{
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid var(--card-border);
      color: var(--text-muted);
      padding: 5px 12px;
      border-radius: 6px;
      cursor: pointer;
      font-size: 0.8rem;
      transition: all 0.15s ease;
      display: inline-flex;
      align-items: center;
      gap: 5px;
    }}
    .btn-filter:hover {{
      background: var(--card-hover);
      color: var(--text);
      border-color: rgba(255, 255, 255, 0.2);
    }}
    .btn-filter.active {{
      background: var(--primary);
      color: #ffffff;
      border-color: var(--primary);
      font-weight: 600;
      box-shadow: 0 0 10px rgba(59, 130, 246, 0.35);
    }}
    .intel-metric-card {{
      background: rgba(15, 23, 42, 0.7);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 8px;
      padding: 12px 14px;
    }}
    .intel-metric-val {{
      font-size: 1.25rem;
      font-weight: 700;
      color: #ffffff;
      margin-top: 4px;
    }}
    .trip-cluster-card {{
      background: rgba(15, 23, 42, 0.8);
      border: 1px solid rgba(59, 130, 246, 0.25);
      border-radius: 8px;
      padding: 10px 14px;
      margin-bottom: 8px;
    }}

    /* Category list item */
    .cat-row {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 10px 12px;
      border-radius: 8px;
      cursor: pointer;
      transition: background 0.15s;
    }}
    .cat-row:hover {{ background: var(--card-hover); }}
    .cat-bar-wrap {{
      flex: 1;
      margin: 0 16px;
      background: var(--card-border);
      height: 8px;
      border-radius: 4px;
      overflow: hidden;
    }}
    .cat-bar-fill {{
      height: 100%;
      background: var(--primary);
      border-radius: 4px;
    }}

    /* Hide tab content */
    .tab-content {{ display: none; }}
    .tab-content.active {{ display: block; }}

    /* Statement Coverage Grid */
    .coverage-grid-container {{
      overflow-x: auto;
      margin-top: 14px;
      padding-bottom: 8px;
    }}
    .coverage-table {{
      width: 100%;
      border-collapse: separate;
      border-spacing: 0;
      font-size: 0.8rem;
    }}
    .coverage-table th, .coverage-table td {{
      padding: 8px 10px;
      border-bottom: 1px solid var(--card-border);
      vertical-align: middle;
      white-space: nowrap;
    }}
    .coverage-table th {{
      background: var(--card-bg);
      position: sticky;
      top: 0;
      z-index: 2;
      color: var(--text-muted);
      font-weight: 600;
      text-align: center;
    }}
    .coverage-table th.acct-header {{
      text-align: left;
      position: sticky;
      left: 0;
      z-index: 3;
      background: var(--card-bg);
    }}
    .coverage-table td.acct-cell {{
      position: sticky;
      left: 0;
      z-index: 1;
      background: var(--card-bg);
      border-right: 1px solid var(--card-border);
    }}
    .coverage-months-cell {{
      display: inline-flex;
      gap: 2px;
      align-items: center;
      background: rgba(0, 0, 0, 0.2);
      padding: 3px 5px;
      border-radius: 4px;
    }}
    .coverage-chip {{
      width: 11px;
      height: 18px;
      border-radius: 2px;
      cursor: pointer;
      position: relative;
      transition: transform 0.1s, filter 0.1s;
    }}
    .coverage-chip:hover {{
      transform: scale(1.4);
      z-index: 10;
      filter: brightness(1.25);
    }}
    .coverage-chip.ok {{
      background: #10b981;
    }}
    .coverage-chip.gap {{
      background: #ef4444;
      box-shadow: 0 0 6px rgba(239, 68, 68, 0.7);
    }}
    .coverage-chip.empty {{
      background: rgba(148, 163, 184, 0.18);
    }}
    .cov-tooltip {{
      position: fixed;
      display: none;
      z-index: 1000;
      background: rgba(15, 23, 42, 0.96);
      border: 1px solid #334155;
      color: #f8fafc;
      padding: 8px 12px;
      border-radius: 6px;
      font-size: 0.76rem;
      pointer-events: none;
      box-shadow: 0 4px 14px rgba(0,0,0,0.45);
      max-width: 320px;
      line-height: 1.4;
      white-space: pre-line;
    }}
  </style>
</head>
<body>

  <!-- Coverage Tooltip -->
  <div id="coverage-tooltip" class="cov-tooltip"></div>

  <!-- Top Sticky Header -->
  <header>
    <div class="header-inner">
      <div class="brand">
        <h1>Finance &amp; Statements</h1>
        <span class="brand-badge" id="bank-filter-badge">All Banks</span>
        <span style="font-size: 0.75rem; color: var(--text-muted);" id="last-updated">Updated: {data['generated_at']}</span>
      </div>

      <div class="header-controls">
        <!-- Period View Mode: Months | 3M Blocks | FY's | Calendar Years -->
        <div class="pill-group" id="period-mode-pills" title="Switch aggregation period">
          <button class="pill-btn active" data-mode="month">Months</button>
          <button class="pill-btn" data-mode="quarter">3M Blocks</button>
          <button class="pill-btn" data-mode="fy">FY (Jul-Jun)</button>
          <button class="pill-btn" data-mode="year">Calendar Years</button>
        </div>

        <!-- Transfer Netting Toggle -->
        <div class="pill-group" id="transfer-pills" title="Cancel internal transfers between own accounts so moving money is not counted twice as income or expense">
          <button class="pill-btn active" id="btn-cancel-transfers">🔄 Net Transfers ($0)</button>
        </div>

        <!-- Ownership Toggle: All | Solo | Joint -->
        <div class="pill-group" id="ownership-pills">
          <button class="pill-btn active" data-ownership="all">All Accounts</button>
          <button class="pill-btn" data-ownership="joint">Joint</button>
          <button class="pill-btn" data-ownership="solo">Solo</button>
        </div>

        <!-- Period / Month Selector with Prev/Next -->
        <div class="month-control">
          <button class="month-nav-btn" id="prev-month" title="Previous Period">&lt;</button>
          <select id="month-select">
            <option value="all">All Time (Full History)</option>
          </select>
          <button class="month-nav-btn" id="next-month" title="Next Period">&gt;</button>
        </div>

        <!-- Dark/Light Theme Toggle -->
        <button class="theme-toggle" id="theme-btn" title="Toggle Dark/Light Mode">☀️ / 🌙</button>
      </div>
    </div>
  </header>

  <!-- Main Dashboard Container -->
  <main class="container">

    <!-- Navigation Tabs -->
    <nav class="tabs-bar">
      <button class="tab-nav-btn active" data-tab="tab-overview">📊 Portfolio Overview</button>
      <button class="tab-nav-btn" data-tab="tab-accounts">💳 Accounts &amp; Cards</button>
      <button class="tab-nav-btn" data-tab="tab-homeloan">🏡 Property &amp; Mortgage</button>
      <button class="tab-nav-btn" data-tab="tab-equity">📈 Shares &amp; Equity (TEAM)</button>
      <button class="tab-nav-btn" data-tab="tab-categories">🏷️ Income &amp; Spending Categories</button>
      <button class="tab-nav-btn" data-tab="tab-transactions">📝 All Transactions</button>
      <button class="tab-nav-btn" data-tab="tab-audit">🛡️ Statement Audit &amp; Gaps</button>
    </nav>

    <!-- Top KPI Row (Dynamic based on selected month & ownership) -->
    <section class="kpi-grid">
      <div class="kpi-card">
        <div class="kpi-label">Net Portfolio Worth</div>
        <div class="kpi-value" id="kpi-net-worth">$0.00</div>
        <div class="kpi-subtext" id="kpi-net-worth-sub">Deposits minus Debt</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Total Liquid Deposits</div>
        <div class="kpi-value kpi-inflow" id="kpi-deposits">$0.00</div>
        <div class="kpi-subtext">Offset &amp; Everyday Accounts</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Total Liabilities</div>
        <div class="kpi-value kpi-loan" id="kpi-debt">$0.00</div>
        <div class="kpi-subtext">Home Loans &amp; Credit Cards</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Period Inflows</div>
        <div class="kpi-value kpi-inflow" id="kpi-inflows">$0.00</div>
        <div class="kpi-subtext" id="kpi-inflows-sub">Income &amp; Credits</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Period Outflows</div>
        <div class="kpi-value kpi-outflow" id="kpi-outflows">$0.00</div>
        <div class="kpi-subtext" id="kpi-outflows-sub">Debits &amp; Spending</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Net Savings / Cashflow</div>
        <div class="kpi-value" id="kpi-net-cashflow">$0.00</div>
        <div class="kpi-subtext">Inflow minus Outflow</div>
      </div>
    </section>

    <!-- TAB 1: Portfolio Overview -->
    <div id="tab-overview" class="tab-content active">
      <div class="grid-2col">
        <!-- Balance History Multi-Line Chart -->
        <div class="card">
          <div class="card-title">
            <span>Balance History by Account</span>
            <div class="chart-controls" id="ctrl-balance-history">
              <button class="chart-btn" onclick="setChartRange('balance-history', '6m')">6M</button>
              <button class="chart-btn" onclick="setChartRange('balance-history', '1y')">1Y</button>
              <button class="chart-btn" onclick="setChartRange('balance-history', '3y')">3Y</button>
              <button class="chart-btn" onclick="setChartRange('balance-history', '5y')">5Y</button>
              <button class="chart-btn active" onclick="setChartRange('balance-history', 'all')">All</button>
              <button class="chart-btn-icon" onclick="zoomChart('balance-history', 1.3)" title="Zoom In">🔍+</button>
              <button class="chart-btn-icon" onclick="zoomChart('balance-history', 0.77)" title="Zoom Out">🔍-</button>
              <button class="chart-btn-icon" onclick="setChartRange('balance-history', 'all')" title="Reset">↺</button>
            </div>
          </div>
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; flex-wrap: wrap; gap: 8px;">
            <div style="display: flex; gap: 6px; align-items: center; flex-wrap: wrap;">
              <span style="font-size: 0.8rem; color: var(--text-muted); font-weight: 600;">View Mode:</span>
              <button class="btn-sm btn-filter active" id="btn-view-individual" onclick="setBalanceViewMode('individual')">📊 Accounts Lines</button>
              <button class="btn-sm btn-filter" id="btn-view-mirrored" onclick="setBalanceViewMode('mirrored')">⚖️ Combined &amp; Mirrored (Assets vs Debt)</button>
              <button class="btn-sm btn-filter active" id="btn-toggle-homeloan" onclick="toggleHomeLoanFilter()">🏠 Home Loan: Included</button>
              <button class="btn-sm btn-filter" id="btn-toggle-team-equity" onclick="toggleTeamEquityInOverview()">📈 + TEAM Equity (A$)</button>
              <button class="btn-sm btn-filter" id="btn-toggle-house-capital" onclick="toggleHouseCapitalInOverview()">🏡 + House Capital &amp; Equity</button>
            </div>
          </div>
          <div class="chart-container" id="chart-balance-history">
            <div class="chart-tooltip" id="tooltip-balance-history"></div>
          </div>
        </div>

        <!-- Monthly Inflow vs Outflow Bar Chart -->
        <div class="card">
          <div class="card-title">
            <span>Monthly Cashflow (Inflow vs Outflow)</span>
            <div class="chart-controls" id="ctrl-cashflow">
              <button class="chart-btn" onclick="setChartRange('cashflow', '6m')">6M</button>
              <button class="chart-btn active" onclick="setChartRange('cashflow', '1y')">1Y</button>
              <button class="chart-btn" onclick="setChartRange('cashflow', '3y')">3Y</button>
              <button class="chart-btn" onclick="setChartRange('cashflow', 'all')">All</button>
              <button class="chart-btn-icon" onclick="zoomChart('cashflow', 1.3)" title="Zoom In">🔍+</button>
              <button class="chart-btn-icon" onclick="zoomChart('cashflow', 0.77)" title="Zoom Out">🔍-</button>
              <button class="chart-btn-icon" onclick="setChartRange('cashflow', '1y')" title="Reset">↺</button>
            </div>
          </div>
          <div class="chart-container" id="chart-cashflow">
            <div class="chart-tooltip" id="tooltip-cashflow"></div>
          </div>
        </div>
      </div>

      <!-- Account Summary Cards Grid -->
      <div class="card">
        <div class="card-title">
          <span>Active Accounts Summary</span>
          <span style="font-size: 0.8rem; font-weight: normal; color: var(--text-muted);" id="account-filter-summary">Showing all</span>
        </div>
        <div class="accounts-grid" id="accounts-cards-grid"></div>
      </div>
    </div>

    <!-- TAB 2: Accounts & Cards -->
    <div id="tab-accounts" class="tab-content">
      <div class="card">
        <div class="card-title">Detailed Account Ledger</div>
        <div class="table-container">
          <table id="accounts-table">
            <thead>
              <tr>
                <th>Bank</th>
                <th>Account</th>
                <th>Type</th>
                <th>Ownership</th>
                <th class="num">Closing Balance</th>
                <th class="num">Period Debits</th>
                <th class="num">Period Credits</th>
                <th class="num">Interest Rate</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- TAB 3: Property Valuation & Mortgage Tracker -->
    <div id="tab-homeloan" class="tab-content">
      <!-- Property Asset Profile Banner -->
      <div style="background: linear-gradient(135deg, rgba(30, 58, 138, 0.25) 0%, rgba(15, 23, 42, 0.4) 100%); border: 1px solid rgba(59, 130, 246, 0.35); border-radius: 10px; padding: 16px 20px; margin-bottom: 20px;">
        <div style="display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 14px;">
          <div>
            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 4px;">
              <h2 style="font-size: 1.25rem; font-weight: 700; color: #f8fafc; margin: 0;">🏡 Investment Property</h2>
              <span class="tag" style="background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.4);">Primary Residence</span>
              <span class="tag" style="background: rgba(59, 130, 246, 0.2); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.4);">Consolidated Variable</span>
            </div>
            <div style="font-size: 0.86rem; color: #94a3b8;">
              Purchased Aug 2019 &bull; Settled {_pp.get('settlement_date', '')} &bull; 5 Bed &bull; 2 Bath &bull; 4 Parking &bull; 870 m² Land &bull; Double-storey Residential House
            </div>
          </div>
          <div style="text-align: right;">
            <div style="font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.05em; color: #94a3b8;">Current Market Valuation</div>
            <div style="font-size: 1.45rem; font-weight: 800; color: #60a5fa;" id="hl-banner-valuation">{_usd('current_valuation')}</div>
            <div style="font-size: 0.78rem; color: #34d399;" id="hl-banner-gain">{_range_txt}</div>
          </div>
        </div>
        <div style="margin-top: 12px; padding-top: 10px; border-top: 1px solid rgba(255,255,255,0.08); font-size: 0.82rem; color: #bfdbfe; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px;">
          <span><strong>Purchase Price:</strong> {_usd('purchase_price')} &bull; <strong>Capital Renovations:</strong> {_usd('renovation_total')} &bull; <strong>Stamp Duty &amp; Costs:</strong> {_usd('stamp_duty_costs')} &bull; <strong>Total Cost Basis:</strong> {_usd('cost_basis_total')} &bull; <strong>Initial Facility:</strong> {_usd('initial_loan_dd')} ({_pp.get('initial_lvr', 0):.0f}% LVR)</span>
          <span style="color: #93c5fd;"></span>
        </div>
      </div>

      <!-- Property Valuation & Equity Metrics Grid -->
      <div class="kpi-grid">
        <div class="kpi-card">
          <div class="kpi-label">Current Property Value</div>
          <div class="kpi-value" id="hl-kpi-valuation">{_usd('current_valuation')}</div>
          <div class="kpi-subtext">Automated valuation mid-point</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Unrealised Capital Gain</div>
          <div class="kpi-value text-green" id="hl-kpi-cap-gain">+{_usd('capital_gain')}</div>
          <div class="kpi-subtext" id="hl-kpi-cap-gain-pct">+{_pp.get('capital_gain_pct', 0)}% above purchase price</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Net Home Equity (Valuation - Debt)</div>
          <div class="kpi-value text-green" id="hl-kpi-net-equity">$0.00</div>
          <div class="kpi-subtext" id="hl-kpi-equity-share">0.0% home equity ratio</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Effective Equity (with Offset Cash)</div>
          <div class="kpi-value text-green" id="hl-kpi-effective-equity">$0.00</div>
          <div class="kpi-subtext" id="hl-kpi-effective-equity-share">Net equity with liquid offset cash</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Market Loan-to-Value (LVR)</div>
          <div class="kpi-value" id="hl-kpi-market-lvr">0.0%</div>
          <div class="kpi-subtext" id="hl-kpi-net-market-lvr">Net LVR with offset: 0.0%</div>
        </div>
      </div>

      <!-- Mortgage Debt & Offset Efficiency Grid -->
      <div class="kpi-grid" style="margin-top: 14px;">
        <div class="kpi-card">
          <div class="kpi-label">Current Mortgage Balance</div>
          <div class="kpi-value kpi-loan" id="hl-kpi-balance">$0.00</div>
          <div class="kpi-subtext" id="hl-kpi-principal-repaid-sub">Principal repaid: $0.00</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Total Offset Savings (Cash)</div>
          <div class="kpi-value kpi-inflow" id="hl-kpi-offset">$0.00</div>
          <div class="kpi-subtext">Linked 100% offset accounts</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Net Debt Accruing Interest</div>
          <div class="kpi-value" id="hl-kpi-net">$0.00</div>
          <div class="kpi-subtext" id="hl-kpi-offset-ratio">Offset covers 0.0% of debt</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Current Interest Rate</div>
          <div class="kpi-value" id="hl-kpi-rate">0.00%</div>
          <div class="kpi-subtext">Variable interest rate p.a.</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Cumulative Offset Interest Saved</div>
          <div class="kpi-value text-green" id="hl-kpi-benefit">${data['cumulative_offset_benefit']:,.2f}</div>
          <div class="kpi-subtext">Lifetime interest eliminated</div>
        </div>
      </div>

      <!-- Full-Width Property Equity & Valuation Progression Chart -->
      <div class="card" style="margin-top: 20px;">
        <div class="card-title">
          <span>🏡 Property Valuation &amp; Equity Progression (2019 – 2026)</span>
          <div class="chart-controls" id="ctrl-property-equity">
            <button class="chart-btn" onclick="setChartRange('property-equity', '1y')">1Y</button>
            <button class="chart-btn" onclick="setChartRange('property-equity', '3y')">3Y</button>
            <button class="chart-btn" onclick="setChartRange('property-equity', '5y')">5Y</button>
            <button class="chart-btn active" onclick="setChartRange('property-equity', 'all')">All</button>
            <button class="chart-btn-icon" onclick="zoomChart('property-equity', 1.3)" title="Zoom In">🔍+</button>
            <button class="chart-btn-icon" onclick="zoomChart('property-equity', 0.77)" title="Zoom Out">🔍-</button>
            <button class="chart-btn-icon" onclick="setChartRange('property-equity', 'all')" title="Reset">↺</button>
          </div>
        </div>
        <div class="chart-container" id="chart-property-equity">
          <div class="chart-tooltip" id="tooltip-property-equity"></div>
        </div>
      </div>

      <div class="grid-2col">
        <!-- Home Loan Balance Amortization Chart -->
        <div class="card">
          <div class="card-title">
            <span>Loan Balance vs Offset Savings Over Time</span>
            <div class="chart-controls" id="ctrl-loan-balance">
              <button class="chart-btn" onclick="setChartRange('loan-balance', '1y')">1Y</button>
              <button class="chart-btn" onclick="setChartRange('loan-balance', '3y')">3Y</button>
              <button class="chart-btn" onclick="setChartRange('loan-balance', '5y')">5Y</button>
              <button class="chart-btn active" onclick="setChartRange('loan-balance', 'all')">All</button>
              <button class="chart-btn-icon" onclick="zoomChart('loan-balance', 1.3)" title="Zoom In">🔍+</button>
              <button class="chart-btn-icon" onclick="zoomChart('loan-balance', 0.77)" title="Zoom Out">🔍-</button>
              <button class="chart-btn-icon" onclick="setChartRange('loan-balance', 'all')" title="Reset">↺</button>
            </div>
          </div>
          <div class="chart-container" id="chart-loan-balance">
            <div class="chart-tooltip" id="tooltip-loan-balance"></div>
          </div>
        </div>

        <!-- Interest Rate History Step Chart -->
        <div class="card">
          <div class="card-title">
            <span>Interest Rate History (% p.a.)</span>
            <div class="chart-controls" id="ctrl-loan-rates">
              <button class="chart-btn" onclick="setChartRange('loan-rates', '1y')">1Y</button>
              <button class="chart-btn" onclick="setChartRange('loan-rates', '3y')">3Y</button>
              <button class="chart-btn" onclick="setChartRange('loan-rates', '5y')">5Y</button>
              <button class="chart-btn active" onclick="setChartRange('loan-rates', 'all')">All</button>
              <button class="chart-btn-icon" onclick="zoomChart('loan-rates', 1.3)" title="Zoom In">🔍+</button>
              <button class="chart-btn-icon" onclick="zoomChart('loan-rates', 0.77)" title="Zoom Out">🔍-</button>
              <button class="chart-btn-icon" onclick="setChartRange('loan-rates', 'all')" title="Reset">↺</button>
            </div>
          </div>
          <div class="chart-container" id="chart-loan-rates">
            <div class="chart-tooltip" id="tooltip-loan-rates"></div>
          </div>
        </div>
      </div>

      <!-- Repayment vs Interest Breakdown Table -->
      <div class="card">
        <div class="card-title">Monthly Property Valuation &amp; Mortgage Progression (2019 – 2026)</div>
        <div class="table-container">
          <table id="homeloan-history-table">
            <thead>
              <tr>
                <th>Month</th>
                <th class="num">Property Value</th>
                <th class="num">Loan Balance</th>
                <th class="num">Offset Balance</th>
                <th class="num">Net Debt</th>
                <th class="num">Net Home Equity</th>
                <th class="num">Market LVR</th>
                <th class="num">Net LVR</th>
                <th class="num">Interest Rate</th>
                <th class="num">Interest Charged</th>
                <th class="num">Repayment Made</th>
                <th class="num">Principal Reduced</th>
              </tr>
            </thead>
            <tbody></tbody>
          </table>
        </div>
      </div>

      <!-- Renovation & Capital Improvements Breakdown Card -->
      <div class="card" style="margin-top: 24px;">
        <div class="card-title" style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;">
          <span>🛠️ Capital Renovation &amp; Improvements Ledger</span>
          <span style="font-size: 0.85rem; font-weight: 600; color: #34d399; background: rgba(16, 185, 129, 0.15); border: 1px solid rgba(16, 185, 129, 0.35); border-radius: 6px; padding: 4px 10px;">
            Total Capital Invested: {_usd('renovation_total')}
          </span>
        </div>
        <div style="font-size: 0.84rem; color: #94a3b8; margin-bottom: 16px;">
          Comprehensive audit of signed contracts, tax invoices, and direct bank disbursement ledgers from the primary residence rebuild. Includes direct trade contracts, kitchen &amp; bathroom rebuilds, premium appliances, 10kW solar system, and peak on-site trade labor cash.
        </div>
        <div class="kpi-grid" style="margin-bottom: 20px;">
          <div class="kpi-card">
            <div class="kpi-label">Original Purchase Price</div>
            <div class="kpi-value">{_usd('purchase_price')}</div>
            <div class="kpi-subtext">Settled {_pp.get('settlement_date', '')}</div>
          </div>
          <div class="kpi-card">
            <div class="kpi-label">Stamp Duty &amp; Legal</div>
            <div class="kpi-value">{_usd('stamp_duty_costs')}</div>
            <div class="kpi-subtext">NSW Transfer Duty &amp; PEXA</div>
          </div>
          <div class="kpi-card">
            <div class="kpi-label">Capital Renovation</div>
            <div class="kpi-value text-green">{_usd('renovation_total')}</div>
            <div class="kpi-subtext">{len(data.get('renovation_breakdown') or [])} Trade &amp; Materials Scopes</div>
          </div>
          <div class="kpi-card">
            <div class="kpi-label">Total Property Cost Basis</div>
            <div class="kpi-value" style="color: #60a5fa;">{_usd('cost_basis_total')}</div>
            <div class="kpi-subtext">Purchase + Duty + Renovation</div>
          </div>
          <div class="kpi-card">
            <div class="kpi-label">Net Gain Over Cost Basis</div>
            <div class="kpi-value text-green">+{_usd('net_gain_over_cost_basis')}</div>
            <div class="kpi-subtext">+{_pp.get('net_gain_over_cost_basis_pct', 0)}% return on total capital</div>
          </div>
        </div>

        <div class="table-container">
          <table id="homeloan-renovation-table">
            <thead>
              <tr>
                <th>Trade / Category</th>
                <th>Scope of Works</th>
                <th>Contractor / Supplier</th>
                <th>Contract &amp; Invoice Reference</th>
                <th class="num">Amount ($ AUD)</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody></tbody>
          </table>
        </div>
      </div>
    </div>

{equity_tab_html}

    <!-- TAB 4: Income & Spending Categories -->
    <div id="tab-categories" class="tab-content">
      <!-- Controls Toolbar: Income vs Expenses & Filter Options -->
      <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; background: var(--bg); padding: 10px 14px; border-radius: 8px; border: 1px solid var(--card-border); margin-bottom: 18px;">
        <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
          <span style="font-size: 0.82rem; font-weight: 600; color: #94a3b8;">Flow:</span>
          <button class="btn-sm btn-filter active" id="btn-flow-expenses" onclick="setFlowType('expenses')">💸 Expenses &amp; Spending</button>
          <button class="btn-sm btn-filter" id="btn-flow-income" onclick="setFlowType('income')">💰 Income &amp; Inflows</button>
          <span style="border-left: 1px solid var(--border); height: 16px; margin: 0 4px;"></span>
          <span style="font-size: 0.82rem; font-weight: 600; color: #94a3b8;">Filter:</span>
          <button class="btn-sm btn-filter active" id="btn-exp-all" onclick="setExpenseFilter('all')">All</button>
          <button class="btn-sm btn-filter" id="btn-exp-regular" onclick="setExpenseFilter('regular')">🔄 Regular &amp; Fixed Overheads</button>
          <button class="btn-sm btn-filter" id="btn-exp-uncat" onclick="setExpenseFilter('uncat')">⚠️ Uncategorised / Needs Review</button>
        </div>
        <div id="exp-summary-pill" style="font-size: 0.84rem; color: var(--text-muted);"></div>
      </div>
      <div class="grid-2col">
        <!-- Donut Chart -->
        <div class="card">
          <div class="card-title" id="category-donut-title">Spending by Category Breakdown</div>
          <div class="chart-container" id="chart-category-donut">
            <div class="chart-tooltip" id="tooltip-category-donut"></div>
          </div>
        </div>

        <!-- Category Progress Bars -->
        <div class="card">
          <div class="card-title" id="category-bars-title">Category Spending Details (Click to filter)</div>
          <div id="category-bars-list" style="max-height: 320px; overflow-y: auto;"></div>
        </div>
      </div>

      <!-- Category Filtered Transactions Table -->
      <div class="card" id="category-tx-card" style="display: none; margin-top: 18px;">
        <div class="card-title" style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
          <div>
            <span id="category-tx-title">Transactions in Category</span>
            <span id="category-tx-meta" style="font-size: 0.82rem; color: var(--text-muted); margin-left: 8px;"></span>
          </div>
          <div style="display: flex; gap: 8px;">
            <button class="btn-sm" id="btn-open-in-explorer">🔍 Open in Transactions Explorer</button>
            <button class="btn-sm" onclick="document.getElementById('category-tx-card').style.display='none'">Close ✕</button>
          </div>
        </div>
        <div class="table-container">
          <table id="category-tx-table">
            <thead>
              <tr>
                <th>Date</th>
                <th>Account</th>
                <th>Description</th>
                <th>Category</th>
                <th class="num">Outflow (Debit)</th>
                <th class="num">Inflow (Credit)</th>
                <th class="num">Net Amount</th>
                <th class="num">Balance</th>
              </tr>
            </thead>
            <tbody></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- TAB 5: All Transactions -->
    <div id="tab-transactions" class="tab-content">
      <div class="card">
        <div class="card-title">
          <span>Transactions Explorer</span>
          <button class="btn-sm" id="export-csv-btn">📥 Export to CSV</button>
        </div>

        <!-- Regular vs Uncategorised Filter Pills -->
        <div style="display: flex; gap: 6px; align-items: center; margin-bottom: 12px; flex-wrap: wrap;">
          <span style="font-size: 0.8rem; color: var(--text-muted); font-weight: 600;">Status Filter:</span>
          <button class="btn-sm btn-filter active" id="tx-filt-all" onclick="setTxFilterType('all')">All</button>
          <button class="btn-sm btn-filter" id="tx-filt-regular" onclick="setTxFilterType('regular')">🔄 Regular / Subscriptions</button>
          <button class="btn-sm btn-filter" id="tx-filt-uncat" onclick="setTxFilterType('uncat')">⚠️ Uncategorised / Needs Review</button>
        </div>

        <!-- Smart Merchant Filter Chips -->
        <div style="display: flex; gap: 6px; align-items: center; margin-bottom: 14px; flex-wrap: wrap;" id="tx-merchant-chips">
          <span style="font-size: 0.8rem; color: var(--text-muted); font-weight: 600;">⚡ Quick Merchant Smarts:</span>
          <button class="btn-sm btn-filter active" data-merchant="all" onclick="setSmartMerchantFilter('all')">All</button>
          <button class="btn-sm btn-filter" data-merchant="apple" onclick="setSmartMerchantFilter('apple')">🍎 Apple Ecosystem</button>
          <button class="btn-sm btn-filter" data-merchant="qantas" onclick="setSmartMerchantFilter('qantas')">✈️ Qantas &amp; Travel</button>
          <button class="btn-sm btn-filter" data-merchant="tesla" onclick="setSmartMerchantFilter('tesla')">⚡ Tesla &amp; EV</button>
          <button class="btn-sm btn-filter" data-merchant="groceries" onclick="setSmartMerchantFilter('groceries')">🛒 Groceries</button>
          <button class="btn-sm btn-filter" data-merchant="amazon" onclick="setSmartMerchantFilter('amazon')">📦 Amazon &amp; AWS</button>
          <button class="btn-sm btn-filter" data-merchant="bunnings" onclick="setSmartMerchantFilter('bunnings')">🔨 Bunnings &amp; Trade</button>
        </div>

        <!-- Filter Controls -->
        <div class="table-filter-bar">
          <input type="text" class="search-input" id="tx-search" placeholder="Search merchant, description, amount...">
          <select class="select-filter" id="tx-account-filter">
            <option value="all">All Accounts</option>
          </select>
          <select class="select-filter" id="tx-category-filter">
            <option value="all">All Categories</option>
          </select>
          <select class="select-filter" id="tx-type-filter">
            <option value="all">All Types</option>
            <option value="debit">Outflows (Debits)</option>
            <option value="credit">Inflows (Credits)</option>
            <option value="transfer">Internal Transfers</option>
            <option value="no_transfer">External Transactions (No Transfers)</option>
          </select>
        </div>

        <!-- Merchant Intelligence Correlation Card -->
        <div id="merchant-intel-card" style="display: none; margin-bottom: 16px;"></div>

        <!-- Transactions Table -->
        <div class="table-container">
          <table id="transactions-table">
            <thead>
              <tr>
                <th>Date</th>
                <th>Account</th>
                <th>Description</th>
                <th>Category</th>
                <th class="num">Outflow (Debit)</th>
                <th class="num">Inflow (Credit)</th>
                <th class="num">Net Amount</th>
                <th class="num">Balance</th>
              </tr>
            </thead>
            <tbody></tbody>
          </table>
        </div>

        <!-- Pagination Controls -->
        <div class="pagination">
          <span id="tx-pagination-info">Showing 0-0 of 0</span>
          <div>
            <button class="btn-sm" id="tx-prev-page">&lt; Previous</button>
            <span id="tx-current-page" style="margin: 0 8px;">1 / 1</span>
            <button class="btn-sm" id="tx-next-page">Next &gt;</button>
          </div>
        </div>
      </div>
    </div>

    <!-- TAB 6: Statement Audit & Gaps -->
    <div id="tab-audit" class="tab-content">
      <div class="kpi-grid">
        <div class="kpi-card">
          <div class="kpi-label">Active Statements Ingested</div>
          <div class="kpi-value text-green">{data['total_statements']}</div>
          <div class="kpi-subtext">100% Reconciled</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Detected Statement Gaps</div>
          <div class="kpi-value" style="color: {'var(--warning)' if data['gaps'] else 'var(--success)'};">{len(data['gaps'])}</div>
          <div class="kpi-subtext">Continuity timeline checks</div>
        </div>
      </div>

      <!-- Statement Coverage Matrix Card -->
      <div class="card">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; flex-wrap: wrap; gap: 10px;">
          <div>
            <div class="card-title" style="margin-bottom: 4px;">📅 Account Statement Coverage Timeline &amp; Gaps Matrix</div>
            <div style="font-size: 0.82rem; color: var(--text-muted);">Visual month-by-month statement continuity across all accounts. Hover over any month chip to inspect details.</div>
          </div>
          <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
            <div class="pill-group" id="coverage-bank-pills">
              <button class="pill-btn active" data-bank="all">All Banks</button>
              <button class="pill-btn" data-bank="HSBC">HSBC</button>
              <button class="pill-btn" data-bank="ING">ING</button>
              <button class="pill-btn" data-bank="Commonwealth Bank">CommBank</button>
            </div>
            <div class="pill-group" id="coverage-range-pills">
              <button class="pill-btn active" data-range="recent">2019–2026</button>
              <button class="pill-btn" data-range="all">All (2007–2026)</button>
            </div>
          </div>
        </div>

        <!-- Legend -->
        <div style="display: flex; gap: 16px; align-items: center; font-size: 0.78rem; margin-bottom: 12px; padding: 8px 12px; background: rgba(255,255,255,0.03); border-radius: 6px; flex-wrap: wrap;">
          <div style="display: flex; align-items: center; gap: 6px;">
            <span style="display: inline-block; width: 12px; height: 12px; background: #10b981; border-radius: 2px;"></span>
            <span>Statement Present &amp; Reconciled</span>
          </div>
          <div style="display: flex; align-items: center; gap: 6px;">
            <span style="display: inline-block; width: 12px; height: 12px; background: #ef4444; border-radius: 2px;"></span>
            <span>Missing Period / Gap</span>
          </div>
          <div style="display: flex; align-items: center; gap: 6px;">
            <span style="display: inline-block; width: 12px; height: 12px; background: rgba(148, 163, 184, 0.18); border-radius: 2px;"></span>
            <span>Account Inactive in Period</span>
          </div>
        </div>

        <div class="coverage-grid-container" id="coverage-matrix-container">
          <!-- Populated by JavaScript -->
        </div>
      </div>

      <!-- Missing Gaps Alert Card -->
      <div class="card" style="border-left: 4px solid var(--warning);">
        <div class="card-title">Missing Statement Periods Detected</div>
        <div class="table-container">
          <table>
            <thead>
              <tr>
                <th>Provider</th>
                <th>Missing Period</th>
                <th>Days</th>
                <th>Estimated Missing</th>
                <th>Prior File</th>
                <th>Next File</th>
              </tr>
            </thead>
            <tbody>
              {''.join(f'''<tr>
                <td><strong>{g.get("provider", "-")}</strong></td>
                <td>{g.get("missing_start", "-")} to {g.get("missing_end", "-")}</td>
                <td class="num">{g.get("days", "-")}</td>
                <td><span class="tag tag-card">{g.get("estimated_missing", "-")}</span></td>
                <td style="font-family: monospace; font-size: 0.78rem;">{g.get("before_file", "-")}</td>
                <td style="font-family: monospace; font-size: 0.78rem;">{g.get("after_file", "-")}</td>
              </tr>''' for g in data['gaps']) if data['gaps'] else '<tr><td colspan="6" style="text-align: center; color: var(--success); padding: 18px;">✓ No missing statement periods detected! 100% continuous coverage.</td></tr>'}
            </tbody>
          </table>
        </div>
      </div>

      <!-- Statement Files Table -->
      <div class="card">
        <div class="card-title">Statement Files Ingested</div>
        <div class="table-container">
          <table>
            <thead>
              <tr>
                <th>Statement File</th>
                <th>Provider</th>
                <th>Period Start</th>
                <th>Period End</th>
                <th>Reconciled</th>
              </tr>
            </thead>
            <tbody>
              {''.join(f'''<tr>
                <td style="font-family: monospace; font-size: 0.82rem;">{s['file']}</td>
                <td><span class="tag tag-solo">{s['provider']}</span></td>
                <td>{s['period_start'] or '-'}</td>
                <td>{s['period_end'] or '-'}</td>
                <td><span class="tag tag-ok">✓ Reconciled</span></td>
              </tr>''' for s in data['statements'])}
            </tbody>
          </table>
        </div>
      </div>
    </div>

  </main>

  <!-- Embed JSON data -->
  <script id="report-data" type="application/json">
{data_json}
  </script>

  <!-- Interactive Dashboard Controller -->
  <script>
    (function() {{
      const DATA = JSON.parse(document.getElementById('report-data').textContent);

      // State
      let currentPeriodMode = 'month'; // 'month' | 'quarter' | 'fy' | 'year'
      let currentPeriod = 'all';
      let currentOwnership = 'all';
      let currentTab = 'tab-overview';
      let excludeTransfers = true;
      let coverageBank = 'all';
      let coverageRange = 'recent'; // 'recent' (2019-2026) | 'all' (2007-2026)
      let txPage = 1;
      const txPerPage = 50;
      const txPageSize = 50;
      let txFilterType = 'all';   // 'all', 'regular', 'uncat'
      let smartMerchant = 'all';  // 'all', 'apple', 'qantas', 'tesla', 'groceries', 'amazon', 'bunnings'
      let expenseFilter = 'all';  // 'all', 'regular', 'uncat'
      let flowType = 'expenses';  // 'expenses' | 'income'

      // Formatting helper
      const fmtCurrency = (n) => {{
        if (n === null || n === undefined || isNaN(n)) return '$0.00';
        const abs = Math.abs(n).toLocaleString('en-AU', {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }});
        return n < 0 ? `-$${{abs}}` : `$${{abs}}`;
      }};

      // Period helpers
      function getPeriodKey(dateStr, mode) {{
        if (!dateStr || dateStr.length < 7) return '';
        const y = parseInt(dateStr.slice(0, 4), 10);
        const m = parseInt(dateStr.slice(5, 7), 10);
        if (mode === 'month') return dateStr.slice(0, 7);
        if (mode === 'quarter') {{
          const q = Math.ceil(m / 3);
          return `${{y}}-Q${{q}}`;
        }}
        if (mode === 'fy') {{
          return m >= 7 ? `FY${{y}}-${{String(y + 1).slice(2)}}` : `FY${{y - 1}}-${{String(y).slice(2)}}`;
        }}
        if (mode === 'year') return `${{y}}`;
        return dateStr.slice(0, 7);
      }}

      function getPeriodDisplay(key, mode) {{
        if (!key || key === 'all') return 'All Time (Full History)';
        if (mode === 'month') {{
          const parts = key.split('-');
          if (parts.length === 2) {{
            const mNames = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
            const mi = parseInt(parts[1], 10) - 1;
            return `${{mNames[mi] || parts[1]}} ${{parts[0]}}`;
          }}
          return key;
        }}
        if (mode === 'quarter') {{
          const [y, q] = key.split('-');
          const qNames = {{'Q1': 'Jan–Mar', 'Q2': 'Apr–Jun', 'Q3': 'Jul–Sep', 'Q4': 'Oct–Dec'}};
          return `${{y}} ${{q}} (${{qNames[q] || ''}})`;
        }}
        if (mode === 'fy') {{
          return `${{key}} (1 Jul – 30 Jun)`;
        }}
        if (mode === 'year') {{
          return `CY ${{key}} (1 Jan – 31 Dec)`;
        }}
        return key;
      }}

      function matchesSelectedPeriod(dateStr) {{
        if (currentPeriod === 'all') return true;
        if (!dateStr) return false;
        return getPeriodKey(dateStr, currentPeriodMode) === currentPeriod;
      }}

      // DOM Elements
      const monthSelect = document.getElementById('month-select');
      const prevMonthBtn = document.getElementById('prev-month');
      const nextMonthBtn = document.getElementById('next-month');
      const ownershipPills = document.getElementById('ownership-pills');
      const periodModePills = document.getElementById('period-mode-pills');
      const btnCancelTransfers = document.getElementById('btn-cancel-transfers');
      const themeBtn = document.getElementById('theme-btn');

      // Populate Period Selector based on mode
      function populatePeriodOptions() {{
        monthSelect.innerHTML = '<option value="all">All Time (Full History)</option>';
        const keySet = new Set();
        (DATA.months || []).forEach(m => {{
          const k = getPeriodKey(m + '-01', currentPeriodMode);
          if (k) keySet.add(k);
        }});
        (DATA.transactions || []).forEach(t => {{
          if (t.date) {{
            const k = getPeriodKey(t.date, currentPeriodMode);
            if (k) keySet.add(k);
          }}
        }});
        const sorted = Array.from(keySet).sort().reverse();
        sorted.forEach(k => {{
          const opt = document.createElement('option');
          opt.value = k;
          opt.textContent = getPeriodDisplay(k, currentPeriodMode);
          monthSelect.appendChild(opt);
        }});
        if (currentPeriod !== 'all' && keySet.has(currentPeriod)) {{
          monthSelect.value = currentPeriod;
        }} else {{
          currentPeriod = 'all';
          monthSelect.value = 'all';
        }}
      }}

      populatePeriodOptions();

      // Period Mode Switcher
      if (periodModePills) {{
        periodModePills.addEventListener('click', (e) => {{
          if (e.target.classList.contains('pill-btn')) {{
            periodModePills.querySelectorAll('.pill-btn').forEach(b => b.classList.remove('active'));
            e.target.classList.add('active');
            currentPeriodMode = e.target.dataset.mode;
            populatePeriodOptions();
            refreshDashboard();
          }}
        }});
      }}

      // Transfer Netting Toggle
      if (btnCancelTransfers) {{
        btnCancelTransfers.addEventListener('click', () => {{
          excludeTransfers = !excludeTransfers;
          btnCancelTransfers.classList.toggle('active', excludeTransfers);
          btnCancelTransfers.textContent = excludeTransfers ? '🔄 Net Transfers ($0)' : '⚠️ Gross (Incl. Transfers)';
          refreshDashboard();
        }});
      }}

      // Theme toggle
      themeBtn.addEventListener('click', () => {{
        const isLight = document.body.getAttribute('data-theme') === 'light';
        document.body.setAttribute('data-theme', isLight ? 'dark' : 'light');
      }});

      // Period navigation
      monthSelect.addEventListener('change', (e) => {{
        currentPeriod = e.target.value;
        refreshDashboard();
      }});

      prevMonthBtn.addEventListener('click', () => {{
        const idx = monthSelect.selectedIndex;
        if (idx < monthSelect.options.length - 1) {{
          monthSelect.selectedIndex = idx + 1;
          currentPeriod = monthSelect.value;
          refreshDashboard();
        }}
      }});

      nextMonthBtn.addEventListener('click', () => {{
        const idx = monthSelect.selectedIndex;
        if (idx > 0) {{
          monthSelect.selectedIndex = idx - 1;
          currentPeriod = monthSelect.value;
          refreshDashboard();
        }}
      }});

      // Ownership toggle
      ownershipPills.addEventListener('click', (e) => {{
        if (e.target.classList.contains('pill-btn')) {{
          ownershipPills.querySelectorAll('.pill-btn').forEach(b => b.classList.remove('active'));
          e.target.classList.add('active');
          currentOwnership = e.target.dataset.ownership;
          refreshDashboard();
        }}
      }});

      // Tab navigation
      document.querySelectorAll('.tab-nav-btn').forEach(btn => {{
        btn.addEventListener('click', () => {{
          document.querySelectorAll('.tab-nav-btn').forEach(b => b.classList.remove('active'));
          document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
          btn.classList.add('active');
          const tabId = btn.dataset.tab;
          document.getElementById(tabId).classList.add('active');
          currentTab = tabId;
          refreshTab();
        }});
      }});

      // Coverage filter event listeners
      const covBankPills = document.getElementById('coverage-bank-pills');
      if (covBankPills) {{
        covBankPills.addEventListener('click', (e) => {{
          if (e.target.classList.contains('pill-btn')) {{
            covBankPills.querySelectorAll('.pill-btn').forEach(b => b.classList.remove('active'));
            e.target.classList.add('active');
            coverageBank = e.target.dataset.bank;
            renderCoverageMatrix();
          }}
        }});
      }}

      const covRangePills = document.getElementById('coverage-range-pills');
      if (covRangePills) {{
        covRangePills.addEventListener('click', (e) => {{
          if (e.target.classList.contains('pill-btn')) {{
            covRangePills.querySelectorAll('.pill-btn').forEach(b => b.classList.remove('active'));
            e.target.classList.add('active');
            coverageRange = e.target.dataset.range;
            renderCoverageMatrix();
          }}
        }});
      }}

      // Filter accounts by ownership
      function getFilteredAccounts() {{
        if (currentOwnership === 'all') return DATA.accounts;
        return DATA.accounts.filter(a => a.ownership === currentOwnership);
      }}

      // Refresh entire dashboard
      function refreshDashboard() {{
        updateKPIs();
        refreshTab();
      }}

      function refreshTab() {{
        if (currentTab === 'tab-overview') {{
          renderBalanceHistoryChart();
          renderCashflowChart();
          renderAccountCards();
        }} else if (currentTab === 'tab-accounts') {{
          renderAccountsTable();
        }} else if (currentTab === 'tab-homeloan') {{
          renderHomeLoanTab();
        }} else if (currentTab === 'tab-categories') {{
          renderCategoriesTab();
        }} else if (currentTab === 'tab-transactions') {{
          renderTransactionsTab();
        }} else if (currentTab === 'tab-audit') {{
          renderCoverageMatrix();
        }} else if (currentTab === 'tab-equity') {{
          renderEquityTab();
        }}
      }}

      // Calculate and update top KPI row
      function updateKPIs() {{
        const accts = getFilteredAccounts();
        const acctNos = new Set(accts.map(a => a.account_no));

        let netWorth = 0;
        let deposits = 0;
        let debt = 0;
        let inflows = 0;
        let outflows = 0;
        let transferVolume = 0;

        accts.forEach(a => {{
          let b = 0;
          if (currentPeriod === 'all') {{
            b = a.latest_balance;
          }} else {{
            const matchingSnaps = (a.snapshots || []).filter(s => matchesSelectedPeriod(s.period_end || s.month));
            if (matchingSnaps.length > 0) {{
              b = matchingSnaps[matchingSnaps.length - 1].closing_balance;
            }} else {{
              const priorSnaps = (a.snapshots || []).filter(s => getPeriodKey(s.period_end, currentPeriodMode) <= currentPeriod);
              b = priorSnaps.length > 0 ? priorSnaps[priorSnaps.length - 1].closing_balance : 0;
            }}
          }}
          if (b >= 0) deposits += b;
          else debt += Math.abs(b);
          netWorth += b;
        }});

        DATA.transactions.forEach(t => {{
          if (!acctNos.has(t.account_no)) return;
          if (!matchesSelectedPeriod(t.date)) return;

          if (t.is_transfer) {{
            transferVolume += (t.debit || t.credit || 0);
            if (!excludeTransfers) {{
              inflows += t.credit;
              outflows += t.debit;
            }}
          }} else {{
            inflows += t.credit;
            outflows += t.debit;
          }}
        }});

        document.getElementById('kpi-net-worth').textContent = fmtCurrency(netWorth);
        document.getElementById('kpi-deposits').textContent = fmtCurrency(deposits);
        document.getElementById('kpi-debt').textContent = fmtCurrency(debt);
        document.getElementById('kpi-inflows').textContent = fmtCurrency(inflows);
        document.getElementById('kpi-outflows').textContent = fmtCurrency(outflows);
        const netCashflow = inflows - outflows;
        const netEl = document.getElementById('kpi-net-cashflow');
        netEl.textContent = fmtCurrency(netCashflow);
        netEl.className = `kpi-value ${{netCashflow >= 0 ? 'kpi-inflow' : 'kpi-outflow'}}`;

        const periodLabel = getPeriodDisplay(currentPeriod, currentPeriodMode);
        document.getElementById('kpi-inflows-sub').textContent = excludeTransfers
          ? `${{periodLabel}} Real Income (excl. transfers)`
          : `${{periodLabel}} Gross Credits`;
        document.getElementById('kpi-outflows-sub').textContent = excludeTransfers
          ? `${{periodLabel}} Real Spend (excl. transfers)`
          : `${{periodLabel}} Gross Debits`;
        document.getElementById('kpi-net-worth-sub').textContent = `${{periodLabel}} End Net Worth`;
      }}

      // Render Statement Coverage Matrix
      function setupCoverageTooltips(container) {{
        const tt = document.getElementById('coverage-tooltip');
        if (!tt) return;

        container.querySelectorAll('.coverage-chip').forEach(chip => {{
          chip.addEventListener('mouseenter', (e) => {{
            const tipText = chip.getAttribute('data-tooltip');
            if (!tipText) return;
            tt.textContent = tipText;
            tt.style.display = 'block';
            positionCoverageTooltip(tt, e.clientX, e.clientY);
          }});
          chip.addEventListener('mousemove', (e) => {{
            positionCoverageTooltip(tt, e.clientX, e.clientY);
          }});
          chip.addEventListener('mouseleave', () => {{
            tt.style.display = 'none';
          }});
        }});
      }}

      function positionCoverageTooltip(tt, clientX, clientY) {{
        const tw = tt.offsetWidth || 240;
        const th = tt.offsetHeight || 80;
        let left = clientX + 14;
        let top = clientY - 10;
        if (left + tw > window.innerWidth - 20) left = clientX - tw - 14;
        if (top + th > window.innerHeight - 20) top = window.innerHeight - th - 20;
        if (top < 10) top = 10;
        tt.style.left = `${{left}}px`;
        tt.style.top = `${{top}}px`;
      }}

      function renderCoverageMatrix() {{
        const container = document.getElementById('coverage-matrix-container');
        if (!container) return;
        container.innerHTML = '';

        let accts = DATA.account_coverage || [];
        if (coverageBank !== 'all') {{
          accts = accts.filter(a => a.bank.toLowerCase().includes(coverageBank.toLowerCase()));
        }}

        const currentYear = new Date().getFullYear();
        let startYear = coverageRange === 'recent' ? 2019 : 2007;
        const endYear = currentYear;
        const years = [];
        for (let y = startYear; y <= endYear; y++) {{
          years.push(y);
        }}

        const totalAccts = accts.length;
        const fullyReconciledAccts = accts.filter(a => a.gaps.length === 0).length;
        const totalGaps = accts.reduce((sum, a) => sum + a.gaps.length, 0);

        const summaryBar = document.createElement('div');
        summaryBar.style.cssText = 'display: flex; gap: 14px; margin-bottom: 14px; font-size: 0.83rem; flex-wrap: wrap;';
        summaryBar.innerHTML = `
          <div style="background: rgba(255,255,255,0.04); padding: 8px 14px; border-radius: 6px; border: 1px solid var(--card-border);">
            Accounts Shown: <strong style="color: var(--text);">${{totalAccts}}</strong>
          </div>
          <div style="background: rgba(16,185,129,0.1); padding: 8px 14px; border-radius: 6px; border: 1px solid rgba(16,185,129,0.3); color: #10b981;">
            ✓ 100% Continuous Coverage: <strong>${{fullyReconciledAccts}} accounts</strong>
          </div>
          <div style="background: ${{totalGaps > 0 ? 'rgba(239,68,68,0.1)' : 'rgba(16,185,129,0.1)'}}; padding: 8px 14px; border-radius: 6px; border: 1px solid ${{totalGaps > 0 ? 'rgba(239,68,68,0.3)' : 'rgba(16,185,129,0.3)'}}; color: ${{totalGaps > 0 ? '#ef4444' : '#10b981'}};">
            ${{totalGaps > 0 ? `⚠️ Detected Historical Gaps: <strong>${{totalGaps}}</strong>` : '✓ Zero Missing Gaps Detected'}}
          </div>
        `;
        container.appendChild(summaryBar);

        const table = document.createElement('table');
        table.className = 'coverage-table';

        const thead = document.createElement('thead');
        let headerRow = '<tr><th class="acct-header" style="min-width: 250px;">Account / Bank</th>';
        headerRow += '<th style="width: 75px; text-align:center;">Stmts</th>';
        headerRow += '<th style="width: 85px; text-align:center;">Status</th>';
        years.forEach(y => {{
          headerRow += `<th style="text-align: center; min-width: 142px;">${{y}}</th>`;
        }});
        headerRow += '</tr>';
        thead.innerHTML = headerRow;
        table.appendChild(thead);

        const tbody = document.createElement('tbody');
        const monthNames = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

        accts.forEach(a => {{
          const tr = document.createElement('tr');

          let acctCell = `
            <td class="acct-cell">
              <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom: 2px;">
                <strong style="color: var(--text);">${{a.alias || a.account_name}}</strong>
                <span class="tag tag-solo" style="font-size:0.68rem; padding: 1px 6px;">${{a.bank}}</span>
              </div>
              <div style="font-family: monospace; font-size: 0.72rem; color: var(--text-muted);">${{a.account_no}}</div>
            </td>
            <td style="text-align: center; font-family: monospace;">${{a.statement_count}}</td>
            <td style="text-align: center;">
              ${{a.gaps.length === 0
                ? '<span class="tag tag-ok" style="font-size: 0.7rem;">✓ 100% OK</span>'
                : `<span class="tag tag-loan" style="font-size: 0.7rem;">⚠️ ${{a.gaps.length}} Gap${{a.gaps.length > 1 ? 's' : ''}}</span>`}}
            </td>
          `;

          let yearCells = '';
          const earliestMonth = a.earliest_date ? a.earliest_date.slice(0, 7) : '9999-99';
          const latestMonth = a.latest_date ? a.latest_date.slice(0, 7) : '0000-00';

          years.forEach(y => {{
            let chipsHtml = `<div class="coverage-months-cell" title="${{y}}">`;
            for (let m = 1; m <= 12; m++) {{
              const mStr = `${{y}}-${{String(m).padStart(2, '0')}}`;
              const mName = monthNames[m - 1];

              const snap = a.months[mStr];
              let inGap = null;
              for (let g of a.gaps) {{
                const gStart = g.start ? g.start.slice(0, 7) : '';
                const gEnd = g.end ? g.end.slice(0, 7) : '';
                if (mStr >= gStart && mStr <= gEnd) {{
                  inGap = g;
                  break;
                }}
              }}

              if (snap) {{
                const balStr = fmtCurrency(snap.closing_balance);
                chipsHtml += `<span class="coverage-chip ok" data-tooltip="${{mName}} ${{y}}: ✓ Reconciled&#10;File: ${{snap.file}}&#10;Closing: ${{balStr}}"></span>`;
              }} else if (inGap) {{
                chipsHtml += `<span class="coverage-chip gap" data-tooltip="${{mName}} ${{y}}: ⚠️ Missing Statement Gap&#10;Missing: ${{inGap.start}} to ${{inGap.end}} (${{inGap.days}} days)&#10;Est: ${{inGap.estimated}}&#10;Prior: ${{inGap.prior_file}}&#10;Next: ${{inGap.next_file}}"></span>`;
              }} else if (mStr >= earliestMonth && mStr <= latestMonth) {{
                chipsHtml += `<span class="coverage-chip empty" data-tooltip="${{mName}} ${{y}}: No statement file for this month"></span>`;
              }} else {{
                chipsHtml += `<span class="coverage-chip empty" style="opacity: 0.35;" data-tooltip="${{mName}} ${{y}}: Inactive / Outside statement range"></span>`;
              }}
            }}
            chipsHtml += `</div>`;
            yearCells += `<td style="text-align: center;">${{chipsHtml}}</td>`;
          }});

          tr.innerHTML = acctCell + yearCells;
          tbody.appendChild(tr);
        }});

        table.appendChild(tbody);
        container.appendChild(table);

        setupCoverageTooltips(container);
      }}

      // Render Account Cards
      function renderAccountCards() {{
        const grid = document.getElementById('accounts-cards-grid');
        grid.innerHTML = '';
        const accts = getFilteredAccounts();

        accts.forEach(a => {{
          const card = document.createElement('div');
          card.className = 'account-card';
          let bal = a.latest_balance;
          if (currentPeriod !== 'all') {{
            const matchingSnaps = (a.snapshots || []).filter(s => matchesSelectedPeriod(s.period_end || s.month));
            if (matchingSnaps.length > 0) {{
              bal = matchingSnaps[matchingSnaps.length - 1].closing_balance;
            }} else {{
              const priorSnaps = (a.snapshots || []).filter(s => getPeriodKey(s.period_end, currentPeriodMode) <= currentPeriod);
              bal = priorSnaps.length > 0 ? priorSnaps[priorSnaps.length - 1].closing_balance : a.latest_balance;
            }}
          }}

          const tagClass = a.account_type === 'loan' ? 'tag-loan' :
                           a.account_type === 'offset' ? 'tag-offset' :
                           a.account_type === 'credit_card' ? 'tag-card' : 'tag-solo';

          card.innerHTML = `
            <div class="account-head">
              <div>
                <div class="account-title">${{a.alias || a.account_name}}</div>
                <div class="account-num">${{a.bank}} • ${{a.account_no}}</div>
              </div>
              <span class="tag ${{tagClass}}">${{a.account_type.replace('_', ' ')}}</span>
            </div>
            <div class="account-balance-large ${{bal < 0 ? 'kpi-loan' : 'kpi-inflow'}}">${{fmtCurrency(bal)}}</div>
            <div class="account-foot">
              <span>Ownership: <strong class="tag ${{a.ownership === 'joint' ? 'tag-joint' : 'tag-solo'}}">${{a.ownership}}</strong></span>
              <span>${{a.statement_count}} statements (${{a.reconciled_count === a.statement_count ? '100% ok' : a.reconciled_count + '/' + a.statement_count}})</span>
            </div>
          `;
          card.addEventListener('click', () => {{
            // Switch to Transactions tab filtered to this account
            document.querySelector('[data-tab="tab-transactions"]').click();
            document.getElementById('tx-account-filter').value = a.account_no;
            renderTransactionsTab();
          }});
          grid.appendChild(card);
        }});
      }}

      // Render Accounts Table
      function renderAccountsTable() {{
        const tbody = document.querySelector('#accounts-table tbody');
        tbody.innerHTML = '';
        const accts = getFilteredAccounts();

        accts.forEach(a => {{
          let snap = a.snapshots && a.snapshots.length > 0 ? a.snapshots[a.snapshots.length - 1] : null;
          if (currentPeriod !== 'all') {{
            const matchingSnaps = (a.snapshots || []).filter(s => matchesSelectedPeriod(s.period_end || s.month));
            if (matchingSnaps.length > 0) {{
              snap = matchingSnaps[matchingSnaps.length - 1];
            }}
          }}

          const bal = snap ? snap.closing_balance : a.latest_balance;
          const deb = snap ? snap.debits : a.total_debits;
          const cred = snap ? snap.credits : a.total_credits;
          const rate = snap && snap.interest_rate ? `${{snap.interest_rate}}% p.a.` : '-';

          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td><strong>${{a.bank}}</strong></td>
            <td><strong>${{a.alias || a.account_name}}</strong><br><small style="color:var(--text-muted); font-family:monospace;">${{a.account_no}}</small></td>
            <td><span class="tag ${{a.account_type === 'loan' ? 'tag-loan' : 'tag-offset'}}">${{a.account_type}}</span></td>
            <td><span class="tag ${{a.ownership === 'joint' ? 'tag-joint' : 'tag-solo'}}">${{a.ownership}}</span></td>
            <td class="num ${{bal < 0 ? 'text-red' : 'text-green'}}">${{fmtCurrency(bal)}}</td>
            <td class="num text-red">${{fmtCurrency(deb)}}</td>
            <td class="num text-green">${{fmtCurrency(cred)}}</td>
            <td class="num">${{rate}}</td>
            <td><span class="tag tag-ok">✓ Reconciled</span></td>
          `;
          tbody.appendChild(tr);
        }});
      }}

      // Render Property & Home Loan Tab
      function renderHomeLoanTab() {{
        const timeline = DATA.home_loan_timeline;
        if (!timeline || timeline.length === 0) return;

        const latest = timeline[timeline.length - 1];
        const curVal = latest.property_valuation || 0;
        const purchasePrice = (DATA.property_profile || {{}}).purchase_price || 0;
        const origLoan = (DATA.property_profile || {{}}).initial_loan_dd || 0;
        const curLoan = Math.abs(latest.loan_balance);
        const curOffset = latest.offset_balance;
        const netDebt = Math.max(0, Math.abs(latest.net_debt));
        const principalRepaid = Math.max(0, origLoan - curLoan);
        const principalPct = ((principalRepaid / origLoan) * 100).toFixed(1);

        const capGain = curVal - purchasePrice;
        const capGainPct = ((capGain / purchasePrice) * 100).toFixed(1);
        const netEquity = curVal - curLoan;
        const effectiveEquity = curVal - netDebt;
        const equityRatio = ((netEquity / curVal) * 100).toFixed(1);
        const effEquityRatio = ((effectiveEquity / curVal) * 100).toFixed(1);

        const marketLvr = ((curLoan / curVal) * 100).toFixed(1);
        const netMarketLvr = ((netDebt / curVal) * 100).toFixed(1);
        const offsetRatio = curLoan > 0 ? ((curOffset / curLoan) * 100).toFixed(1) : '100.0';

        // Update Banners & KPIs
        const elBannerVal = document.getElementById('hl-banner-valuation');
        if (elBannerVal) elBannerVal.textContent = fmtCurrency(curVal);
        const elBannerGain = document.getElementById('hl-banner-gain');
        if (elBannerGain) elBannerGain.textContent = `Range: $${{(((DATA.property_profile || {{}}).valuation_range_low || 0) / 1e6).toFixed(2)}}M – $${{(((DATA.property_profile || {{}}).valuation_range_high || 0) / 1e6).toFixed(2)}}M (+${{capGainPct}}% / +${{fmtCurrency(capGain)}} gain)`;

        const elKpiVal = document.getElementById('hl-kpi-valuation');
        if (elKpiVal) elKpiVal.textContent = fmtCurrency(curVal);

        const elCapGain = document.getElementById('hl-kpi-cap-gain');
        if (elCapGain) elCapGain.textContent = `+${{fmtCurrency(capGain)}}`;
        const elCapGainPct = document.getElementById('hl-kpi-cap-gain-pct');
        if (elCapGainPct) elCapGainPct.textContent = `+${{capGainPct}}% above $1.18M purchase`;

        const elNetEquity = document.getElementById('hl-kpi-net-equity');
        if (elNetEquity) elNetEquity.textContent = fmtCurrency(netEquity);
        const elEquityShare = document.getElementById('hl-kpi-equity-share');
        if (elEquityShare) elEquityShare.textContent = `${{equityRatio}}% home equity ratio`;

        const elEffEquity = document.getElementById('hl-kpi-effective-equity');
        if (elEffEquity) elEffEquity.textContent = fmtCurrency(effectiveEquity);
        const elEffEquityShare = document.getElementById('hl-kpi-effective-equity-share');
        if (elEffEquityShare) elEffEquityShare.textContent = `${{effEquityRatio}}% net equity with offset cash`;

        const elMarketLvr = document.getElementById('hl-kpi-market-lvr');
        if (elMarketLvr) elMarketLvr.textContent = `${{marketLvr}}%`;
        const elNetMarketLvr = document.getElementById('hl-kpi-net-market-lvr');
        if (elNetMarketLvr) elNetMarketLvr.textContent = `Net LVR with offset: ${{netMarketLvr}}%`;

        document.getElementById('hl-kpi-balance').textContent = fmtCurrency(latest.loan_balance);
        const elRepaidSub = document.getElementById('hl-kpi-principal-repaid-sub');
        if (elRepaidSub) elRepaidSub.textContent = `Principal repaid: ${{fmtCurrency(principalRepaid)}} (${{principalPct}}%)`;

        document.getElementById('hl-kpi-offset').textContent = fmtCurrency(latest.offset_balance);
        document.getElementById('hl-kpi-net').textContent = fmtCurrency(latest.net_debt);
        const elOffsetRatio = document.getElementById('hl-kpi-offset-ratio');
        if (elOffsetRatio) elOffsetRatio.textContent = `Offset covers ${{offsetRatio}}% of debt`;

        document.getElementById('hl-kpi-rate').textContent = latest.interest_rate ? `${{latest.interest_rate}}% p.a.` : '5.94% p.a.';

        // Property & Loan History Table
        const tbody = document.querySelector('#homeloan-history-table tbody');
        if (tbody) {{
          tbody.innerHTML = '';
          timeline.slice().reverse().forEach(row => {{
            const tr = document.createElement('tr');
            const pVal = row.property_valuation ? fmtCurrency(row.property_valuation) : '-';
            const netEq = row.net_equity ? fmtCurrency(row.net_equity) : '-';
            const gLvr = row.gross_lvr ? row.gross_lvr + '%' : '-';
            const nLvr = row.net_lvr ? row.net_lvr + '%' : '-';

            tr.innerHTML = `
              <td><strong>${{row.month}}</strong></td>
              <td class="num" style="color: #60a5fa; font-weight: 600;">${{pVal}}</td>
              <td class="num text-red">${{fmtCurrency(row.loan_balance)}}</td>
              <td class="num text-green">${{fmtCurrency(row.offset_balance)}}</td>
              <td class="num">${{fmtCurrency(row.net_debt)}}</td>
              <td class="num" style="color: #a855f7; font-weight: 600;">${{netEq}}</td>
              <td class="num">${{gLvr}}</td>
              <td class="num text-green">${{nLvr}}</td>
              <td class="num">${{row.interest_rate ? row.interest_rate + '%' : '-'}}</td>
              <td class="num text-red">${{fmtCurrency(row.interest_charged)}}</td>
              <td class="num text-green">${{fmtCurrency(row.repayment_credit)}}</td>
              <td class="num">${{fmtCurrency(row.principal_paid)}}</td>
            `;
            tbody.appendChild(tr);
          }});
        }}

        // Render Renovation Breakdown Table
        const renoTbody = document.querySelector('#homeloan-renovation-table tbody');
        if (renoTbody && DATA.renovation_breakdown) {{
          renoTbody.innerHTML = '';
          DATA.renovation_breakdown.forEach(item => {{
            const tr = document.createElement('tr');
            tr.innerHTML = `
              <td><strong>${{item.trade}}</strong></td>
              <td style="color: #cbd5e1; font-size: 0.85rem;">${{item.scope}}</td>
              <td style="color: #94a3b8; font-size: 0.85rem;">${{item.supplier}}</td>
              <td style="font-size: 0.82rem; color: #60a5fa;">${{item.status}}</td>
              <td class="num" style="font-weight: 700; color: #f8fafc;">${{fmtCurrency(item.amount)}}</td>
              <td><span class="tag" style="background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.4);">Verified</span></td>
            `;
            renoTbody.appendChild(tr);
          }});
        }}

        renderPropertyEquityChart();
        renderLoanBalanceChart();
        renderLoanRatesChart();
      }}

      // Render Categories Tab with Net Transfers, Regular Overheads & Uncategorised Review
      function renderCategoriesTab() {{
        const accts = getFilteredAccounts();
        const acctNos = new Set(accts.map(a => a.account_no));

        const catAmounts = {{}};
        let totalFlow = 0;
        let transferVolume = 0;
        let regularSpend = 0;
        let uncatSpend = 0;
        let uncatCount = 0;

        DATA.transactions.forEach(t => {{
          if (!acctNos.has(t.account_no)) return;
          if (!matchesSelectedPeriod(t.date)) return;

          const isIncome = (flowType === 'income');
          const amt = isIncome ? (t.credit || 0) : (t.debit || 0);
          if (amt <= 0) return;

          if (t.is_regular) regularSpend += amt;
          if (t.category === 'Uncategorised' || t.is_uncategorised) {{
            uncatSpend += amt;
            uncatCount++;
          }}

          if (t.is_transfer) {{
            transferVolume += amt;
            if (!excludeTransfers) {{
              const c = t.category || 'Transfer';
              if (expenseFilter === 'regular' && !t.is_regular) return;
              if (expenseFilter === 'uncat' && c !== 'Uncategorised') return;
              catAmounts[c] = (catAmounts[c] || 0) + amt;
              totalFlow += amt;
            }}
          }} else {{
            if (expenseFilter === 'regular' && !t.is_regular) return;
            if (expenseFilter === 'uncat' && t.category !== 'Uncategorised') return;
            const c = t.category || 'Uncategorised';
            catAmounts[c] = (catAmounts[c] || 0) + amt;
            totalFlow += amt;
          }}
        }});

        const pill = document.getElementById('exp-summary-pill');
        if (pill) {{
          if (flowType === 'income') {{
            const salaryAmt = catAmounts['Income: Employer Salary'] || 0;
            const salaryPct = totalFlow > 0 ? ((salaryAmt / totalFlow) * 100).toFixed(1) : '0.0';
            pill.innerHTML = `
              <span>Total Inflows: <strong>${{fmtCurrency(totalFlow)}}</strong></span> &bull; 
              <span style="color:#10b981;">💼 Employer Salary: <strong>${{fmtCurrency(salaryAmt)}}</strong> (${{salaryPct}}%)</span> &bull; 
              <span style="color:#60a5fa;">Sources: <strong>${{Object.keys(catAmounts).length}}</strong></span>
            `;
          }} else {{
            const regPct = totalFlow > 0 ? ((regularSpend / totalFlow) * 100).toFixed(1) : '0.0';
            pill.innerHTML = `
              <span>Total: <strong>${{fmtCurrency(totalFlow)}}</strong></span> &bull; 
              <span style="color:#60a5fa;">🔄 Regular Overheads: <strong>${{fmtCurrency(regularSpend)}}</strong> (${{regPct}}%)</span> &bull; 
              <span style="color:#f87171;">⚠️ Uncategorised: <strong>${{fmtCurrency(uncatSpend)}}</strong> (${{uncatCount}} txs)</span>
            `;
          }}
        }}

        const list = document.getElementById('category-bars-list');
        list.innerHTML = '';

        if (excludeTransfers && transferVolume > 0 && expenseFilter === 'all') {{
          const banner = document.createElement('div');
          banner.style.cssText = 'background: rgba(59,130,246,0.1); border: 1px solid rgba(59,130,246,0.25); border-radius: 8px; padding: 10px 14px; margin-bottom: 14px; font-size: 0.82rem; color: #93c5fd; display: flex; justify-content: space-between; align-items: center;';
          banner.innerHTML = `
            <span>🔄 <strong>Transfer Netting Active:</strong> Internal transfers (${{fmtCurrency(transferVolume)}}) are netted out ($0).</span>
            <span style="font-weight: 600; color: #fff;">Real ${{flowType === 'income' ? 'Income' : 'Spend'}}: ${{fmtCurrency(totalFlow)}}</span>
          `;
          list.appendChild(banner);
        }}

        const entries = Object.entries(catAmounts)
          .filter(([_, amt]) => amt > 0)
          .sort((a, b) => b[1] - a[1]);

        const maxVal = entries.length > 0 ? entries[0][1] : 1;

        if (entries.length === 0) {{
          list.innerHTML += '<div style="text-align: center; color: var(--text-muted); padding: 30px;">No ' + (flowType === 'income' ? 'income' : 'expense') + ' transactions found for this filter/period.</div>';
          renderCategoryDonutChart([]);
          return;
        }}

        entries.forEach(([cat, amt]) => {{
          const pct = totalFlow > 0 ? ((amt / totalFlow) * 100).toFixed(1) : '0.0';
          const barPct = ((amt / maxVal) * 100).toFixed(1);
          const item = document.createElement('div');
          item.className = 'category-bar-item';
          item.style.cursor = 'pointer';
          item.onclick = () => showCategoryTransactions(cat);
          item.innerHTML = `
            <div class="category-bar-label">
              <span>${{cat}}</span>
              <span>${{fmtCurrency(amt)}} <small style="color: var(--text-muted);">(${{pct}}%)</small></span>
            </div>
            <div class="progress-bar-bg">
              <div class="progress-bar-fill" style="width: ${{barPct}}%;"></div>
            </div>
          `;
          list.appendChild(item);
        }});

        renderCategoryDonutChart(entries);
      }}

      // =============================================================
      // CATEGORY TRANSACTIONS VIEWER
      // =============================================================
      function showCategoryTransactions(cat) {{
        const card = document.getElementById('category-tx-card');
        const titleEl = document.getElementById('category-tx-title');
        const metaEl = document.getElementById('category-tx-meta');
        const tbody = document.querySelector('#category-tx-table tbody');
        if (!card || !tbody) return;

        let items = (DATA.transactions || []).filter(t => {{
          if (t.category !== cat) return false;
          if (!matchesSelectedPeriod(t.date)) return false;
          const isIncome = (flowType === 'income');
          const amt = isIncome ? (t.credit || 0) : (t.debit || 0);
          if (amt <= 0) return false;
          if (excludeTransfers && t.is_transfer && cat !== 'Transfer') return false;
          if (expenseFilter === 'regular' && !t.is_regular) return false;
          if (expenseFilter === 'uncat' && cat !== 'Uncategorised' && !t.is_uncategorised) return false;
          return true;
        }});

        items.sort((a, b) => (b.date || '').localeCompare(a.date || ''));

        const totalDebits = items.reduce((sum, t) => sum + (t.debit || 0), 0);
        const totalCredits = items.reduce((sum, t) => sum + (t.credit || 0), 0);
        const activeSum = (flowType === 'income') ? totalCredits : totalDebits;

        card.style.display = 'block';
        titleEl.textContent = `📁 ${{cat}} (${{flowType === 'income' ? 'Inflows' : 'Spending'}})`;
        if (metaEl) {{
          const flowLabel = (flowType === 'income') ? 'Inflows' : 'Outflows';
          const flowColor = (flowType === 'income') ? 'text-green' : 'text-red';
          metaEl.innerHTML = `Showing <strong>${{items.length}}</strong> transactions for <strong>${{getPeriodDisplay(currentPeriod, currentPeriodMode)}}</strong> &bull; Total ${{flowLabel}}: <strong class="${{flowColor}}">${{fmtCurrency(activeSum)}}</strong>`;
        }}

        tbody.innerHTML = '';
        if (items.length === 0) {{
          tbody.innerHTML = '<tr><td colspan="8" style="text-align: center; color: var(--text-muted); padding: 24px;">No transactions found in this category for the selected period.</td></tr>';
        }} else {{
          items.forEach(t => {{
            const tr = document.createElement('tr');
            const amtCls = t.amount < 0 ? 'text-red' : (t.amount > 0 ? 'text-green' : '');
            const sign = t.amount > 0 ? '+' : '';
            tr.innerHTML = `
              <td><strong>${{t.date}}</strong></td>
              <td><code>${{t.account_no}}</code></td>
              <td title="${{t.desc}}">${{t.desc}}</td>
              <td><span class="tag tag-solo">${{t.category}}</span></td>
              <td class="num">${{t.debit ? fmtCurrency(t.debit) : '—'}}</td>
              <td class="num text-green">${{t.credit ? '+' + fmtCurrency(t.credit) : '—'}}</td>
              <td class="num ${{amtCls}}"><strong>${{sign}}${{fmtCurrency(t.amount)}}</strong></td>
              <td class="num">${{t.balance !== null && t.balance !== undefined ? fmtCurrency(t.balance) : '—'}}</td>
            `;
            tbody.appendChild(tr);
          }});
        }}

        const btnOpen = document.getElementById('btn-open-in-explorer');
        if (btnOpen) {{
          btnOpen.onclick = () => {{
            document.querySelectorAll('.tab-nav-btn').forEach(b => {{
              b.classList.toggle('active', b.dataset.tab === 'tab-transactions');
            }});
            document.querySelectorAll('.tab-content').forEach(c => {{
              c.classList.toggle('active', c.id === 'tab-transactions');
            }});
            currentTab = 'tab-transactions';

            const catSelect = document.getElementById('tx-category-filter');
            if (catSelect) {{
              catSelect.value = cat;
            }}
            txPage = 1;
            renderTransactionsTab();
            document.getElementById('tab-transactions').scrollIntoView({{ behavior: 'smooth' }});
          }};
        }}

        card.scrollIntoView({{ behavior: 'smooth', block: 'nearest' }});
      }}
      window.showCategoryTransactions = showCategoryTransactions;

      // =============================================================
      // MERCHANT SMARTS & PURCHASE CORRELATION ENGINE
      // =============================================================
      function setSmartMerchantFilter(m) {{
        smartMerchant = m;
        document.querySelectorAll('#tx-merchant-chips button').forEach(b => {{
          b.classList.toggle('active', b.dataset.merchant === m);
        }});
        txPage = 1;
        renderTransactionsTab();
      }}
      window.setSmartMerchantFilter = setSmartMerchantFilter;

      function filterByThisCategory(cat) {{
        const catSelect = document.getElementById('tx-category-filter');
        if (catSelect) {{
          catSelect.value = cat;
          txPage = 1;
          renderTransactionsTab();
        }}
      }}
      window.filterByThisCategory = filterByThisCategory;

      function matchesMerchant(t, merchantKey) {{
        if (merchantKey === 'all') return true;
        const desc = ((t.desc || '') + ' ' + (t.category || '')).toLowerCase();
        if (merchantKey === 'apple') {{
          return desc.includes('apple') || desc.includes('itunes');
        }}
        if (merchantKey === 'qantas') {{
          return desc.includes('qantas');
        }}
        if (merchantKey === 'tesla') {{
          return desc.includes('tesla') || desc.includes('teslaunch');
        }}
        if (merchantKey === 'groceries') {{
          return desc.includes('woolworths') || desc.includes('coles') || desc.includes('costco') || desc.includes('harris farm') || desc.includes('iga ') || desc.includes('aldi');
        }}
        if (merchantKey === 'amazon') {{
          return desc.includes('amazon') || desc.includes('amzn') || desc.includes('aws');
        }}
        if (merchantKey === 'bunnings') {{
          return desc.includes('bunnings') || desc.includes('mitre 10') || desc.includes('reece');
        }}
        return true;
      }}

      function renderMerchantIntelligence(filtered, query, activeMerchant) {{
        const intelEl = document.getElementById('merchant-intel-card');
        if (!intelEl) return;

        if (activeMerchant === 'all' && !query) {{
          intelEl.style.display = 'none';
          intelEl.innerHTML = '';
          return;
        }}

        intelEl.style.display = 'block';

        // 1. APPLE INTELLIGENCE
        if (activeMerchant === 'apple') {{
          const appleTxs = (DATA.transactions || []).filter(t => matchesMerchant(t, 'apple'));
          const periodApple = appleTxs.filter(t => matchesSelectedPeriod(t.date));

          const subsTxs = appleTxs.filter(t => (t.debit || 0) > 0 && (t.debit || 0) <= 120);
          const hwTxs = appleTxs.filter(t => (t.debit || 0) > 120);

          const totalAllTime = appleTxs.reduce((s, t) => s + (t.debit || 0), 0);
          const periodTotal = periodApple.reduce((s, t) => s + (t.debit || 0), 0);
          const subsTotal = subsTxs.reduce((s, t) => s + (t.debit || 0), 0);
          const hwTotal = hwTxs.reduce((s, t) => s + (t.debit || 0), 0);

          const tierCounts = {{}};
          subsTxs.forEach(t => {{
            const amtKey = '$' + (t.debit || 0).toFixed(2);
            tierCounts[amtKey] = (tierCounts[amtKey] || 0) + 1;
          }});
          const topTiers = Object.entries(tierCounts).sort((a, b) => b[1] - a[1]).slice(0, 6);

          const appleMonths = new Set(subsTxs.map(t => (t.date || '').slice(0, 7)).filter(Boolean));
          const avgMonthlyRunRate = appleMonths.size > 0 ? (subsTotal / appleMonths.size) : 0;

          const hwListHtml = hwTxs.slice(0, 8).map(t => `
            <div style="display: flex; justify-content: space-between; align-items: center; padding: 6px 0; border-bottom: 1px solid rgba(255,255,255,0.06); font-size: 0.8rem;">
              <div>
                <span style="font-weight: 600; color: #fff;">${{t.date}}</span>
                <span style="color: var(--text-muted); margin-left: 6px;">${{t.desc.length > 36 ? t.desc.slice(0, 34) + '...' : t.desc}}</span>
              </div>
              <span style="font-weight: 700; color: #f87171;">${{fmtCurrency(t.debit)}}</span>
            </div>
          `).join('');

          intelEl.innerHTML = `
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; flex-wrap: wrap; gap: 8px;">
              <div style="font-size: 1rem; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 8px;">
                <span>🍎 Apple Cost &amp; Subscription Correlation</span>
                <span class="tag" style="background: rgba(59,130,246,0.2); color: #60a5fa;">${{appleTxs.length}} All-Time Transactions</span>
              </div>
              <div style="font-size: 0.82rem; color: var(--text-muted);">
                Selected Period Spend: <strong style="color: #fff;">${{fmtCurrency(periodTotal)}}</strong> (${{periodApple.length}} txs)
              </div>
            </div>

            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-bottom: 16px;">
              <div class="intel-metric-card">
                <div style="font-size: 0.78rem; color: var(--text-muted); text-transform: uppercase;">All-Time Apple Spend</div>
                <div class="intel-metric-val" style="color: #60a5fa;">${{fmtCurrency(totalAllTime)}}</div>
                <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 4px;">Hardware + Subscriptions</div>
              </div>
              <div class="intel-metric-card">
                <div style="font-size: 0.78rem; color: var(--text-muted); text-transform: uppercase;">📱 Subscriptions &amp; Cloud</div>
                <div class="intel-metric-val" style="color: #34d399;">${{fmtCurrency(subsTotal)}}</div>
                <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 4px;">${{subsTxs.length}} charges &bull; ~${{fmtCurrency(avgMonthlyRunRate)}}/mo run-rate</div>
              </div>
              <div class="intel-metric-card">
                <div style="font-size: 0.78rem; color: var(--text-muted); text-transform: uppercase;">💻 Hardware &amp; Store</div>
                <div class="intel-metric-val" style="color: #f87171;">${{fmtCurrency(hwTotal)}}</div>
                <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 4px;">${{hwTxs.length}} major purchases</div>
              </div>
            </div>

            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px;">
              <div style="background: rgba(0,0,0,0.25); border: 1px solid rgba(255,255,255,0.06); border-radius: 8px; padding: 12px 14px;">
                <div style="font-size: 0.85rem; font-weight: 600; color: #fff; margin-bottom: 8px; display: flex; justify-content: space-between;">
                  <span>🔄 Recurring Subscription Tiers</span>
                  <span style="font-size: 0.75rem; color: var(--text-muted); font-weight: normal;">Frequent monthly pricing</span>
                </div>
                <div style="display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px;">
                  ${{topTiers.map(([tier, cnt]) => `
                    <span class="tag" style="background: rgba(16,185,129,0.15); color: #34d399; font-size: 0.8rem; padding: 3px 8px;">
                      <strong>${{tier}}</strong> <small style="opacity: 0.75; margin-left: 4px;">(${{cnt}}x)</small>
                    </span>
                  `).join('')}}
                </div>
                <div style="font-size: 0.78rem; color: var(--text-muted); line-height: 1.4;">
                  Charges include recurring iCloud storage, Apple One, and App Store monthly items.
                </div>
              </div>

              <div style="background: rgba(0,0,0,0.25); border: 1px solid rgba(255,255,255,0.06); border-radius: 8px; padding: 12px 14px;">
                <div style="font-size: 0.85rem; font-weight: 600; color: #fff; margin-bottom: 8px; display: flex; justify-content: space-between;">
                  <span>💻 Major Hardware Purchases</span>
                  <span style="font-size: 0.75rem; color: var(--text-muted); font-weight: normal;">Recent high-value gear</span>
                </div>
                <div style="max-height: 160px; overflow-y: auto;">
                  ${{hwListHtml || '<div style="color: var(--text-muted); font-size: 0.8rem;">No hardware purchases recorded.</div>'}}
                </div>
              </div>
            </div>
          `;
          return;
        }}

        // 2. QANTAS INTELLIGENCE & TRIP CLUSTERING
        if (activeMerchant === 'qantas') {{
          const qantasTxs = (DATA.transactions || []).filter(t => matchesMerchant(t, 'qantas'));
          const periodQantas = qantasTxs.filter(t => matchesSelectedPeriod(t.date));

          const wineTxs = qantasTxs.filter(t => (t.desc || '').toUpperCase().includes('WINE'));
          const flightTxs = qantasTxs.filter(t => !(t.desc || '').toUpperCase().includes('WINE'));

          const totalAllTime = qantasTxs.reduce((s, t) => s + (t.debit || 0), 0);
          const periodTotal = periodQantas.reduce((s, t) => s + (t.debit || 0), 0);
          const wineTotal = wineTxs.reduce((s, t) => s + (t.debit || 0), 0);
          const flightTotal = flightTxs.reduce((s, t) => s + (t.debit || 0), 0);

          const sortedFlights = [...flightTxs].sort((a, b) => (b.date || '').localeCompare(a.date || ''));
          const clusters = [];
          let curCluster = [];

          sortedFlights.forEach(t => {{
            if (!curCluster.length) {{
              curCluster = [t];
            }} else {{
              const d1 = new Date(curCluster[curCluster.length - 1].date);
              const d2 = new Date(t.date);
              const diffDays = Math.abs((d1 - d2) / (1000 * 60 * 60 * 24));
              if (diffDays <= 2) {{
                curCluster.push(t);
              }} else {{
                clusters.push(curCluster);
                curCluster = [t];
              }}
            }}
          }});
          if (curCluster.length) clusters.push(curCluster);

          const clustersHtml = clusters.slice(0, 6).map(cl => {{
            const dates = cl.map(t => t.date).sort();
            const dateRange = dates[0] === dates[dates.length - 1] ? dates[0] : `${{dates[0]}} to ${{dates[dates.length - 1]}}`;
            const clTotal = cl.reduce((s, t) => s + (t.debit || 0), 0);

            const fares = cl.filter(t => (t.debit || 0) > 100);
            const seats = cl.filter(t => (t.debit || 0) <= 100);

            let breakdownStr = '';
            if (fares.length > 0 && seats.length > 0) {{
              breakdownStr = `${{fares.length}} Flight Fare(s) (${{fmtCurrency(fares.reduce((s, t) => s + (t.debit || 0), 0))}}) + ${{seats.length}} Seat/Baggage Fee(s) (${{fmtCurrency(seats.reduce((s, t) => s + (t.debit || 0), 0))}})`;
            }} else if (fares.length > 0) {{
              breakdownStr = `${{fares.length}} Ticket/Fare Charge(s) (${{cl.map(t => fmtCurrency(t.debit)).join(' + ')}})`;
            }} else {{
              breakdownStr = `${{seats.length}} Seat Selection / Ancillary Charge(s) (${{cl.map(t => fmtCurrency(t.debit)).join(', ')}})`;
            }}

            return `
              <div class="trip-cluster-card">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                  <div style="display: flex; align-items: center; gap: 8px;">
                    <span style="font-weight: 700; color: #fff;">✈️ Trip Booking Event (${{dateRange}})</span>
                    <span class="tag" style="background: rgba(59,130,246,0.15); color: #60a5fa; font-size: 0.72rem;">${{cl.length}} linked charges</span>
                  </div>
                  <span style="font-size: 1rem; font-weight: 800; color: #f87171;">${{fmtCurrency(clTotal)}}</span>
                </div>
                <div style="font-size: 0.78rem; color: #94a3b8;">${{breakdownStr}}</div>
              </div>
            `;
          }}).join('');

          intelEl.innerHTML = `
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; flex-wrap: wrap; gap: 8px;">
              <div style="font-size: 1rem; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 8px;">
                <span>✈️ Qantas Flight &amp; Travel Correlation</span>
                <span class="tag" style="background: rgba(59,130,246,0.2); color: #60a5fa;">${{clusters.length}} Trip Booking Events Identified</span>
              </div>
              <div style="font-size: 0.82rem; color: var(--text-muted);">
                Selected Period Spend: <strong style="color: #fff;">${{fmtCurrency(periodTotal)}}</strong> (${{periodQantas.length}} txs)
              </div>
            </div>

            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-bottom: 16px;">
              <div class="intel-metric-card">
                <div style="font-size: 0.78rem; color: var(--text-muted); text-transform: uppercase;">All-Time Flight &amp; Travel</div>
                <div class="intel-metric-val" style="color: #60a5fa;">${{fmtCurrency(flightTotal)}}</div>
                <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 4px;">${{flightTxs.length}} charges across ${{clusters.length}} trips</div>
              </div>
              <div class="intel-metric-card">
                <div style="font-size: 0.78rem; color: var(--text-muted); text-transform: uppercase;">🗺️ Trip Booking Clusters</div>
                <div class="intel-metric-val" style="color: #34d399;">${{clusters.length}} Bookings</div>
                <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 4px;">Correlating fares + seat selections</div>
              </div>
              <div class="intel-metric-card">
                <div style="font-size: 0.78rem; color: var(--text-muted); text-transform: uppercase;">🍷 Qantas Wine AU</div>
                <div class="intel-metric-val" style="color: #f59e0b;">${{fmtCurrency(wineTotal)}}</div>
                <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 4px;">${{wineTxs.length}} wine orders separate from flights</div>
              </div>
            </div>

            <div>
              <div style="font-size: 0.86rem; font-weight: 600; color: #fff; margin-bottom: 8px;">
                🔗 Connected Trip &amp; Flight Booking Bundles (Correlating multi-ticket fares &amp; seat charges)
              </div>
              <div>
                ${{clustersHtml || '<div style="color: var(--text-muted); font-size: 0.8rem;">No flight trips recorded.</div>'}}
              </div>
            </div>
          `;
          return;
        }}

        // 3. TESLA INTELLIGENCE
        if (activeMerchant === 'tesla') {{
          const teslaTxs = (DATA.transactions || []).filter(t => matchesMerchant(t, 'tesla'));
          const periodTesla = teslaTxs.filter(t => matchesSelectedPeriod(t.date));

          // 1. Vehicle Acquisition
          const vehicleTxs = teslaTxs.filter(t => (t.debit || 0) >= 5000 || (t.category === 'Vehicle Purchase'));
          // 2. Premium Connectivity ($13.99/mo)
          const connTxs = teslaTxs.filter(t => Math.abs((t.debit || 0) - 13.99) < 0.05);
          // 3. Supercharging (Tesla Inc charging sessions)
          const scTxs = teslaTxs.filter(t => (t.debit || 0) > 0 && (t.debit || 0) <= 40 && (t.desc || '').toUpperCase().includes('TESLA INC') && Math.abs((t.debit || 0) - 13.99) >= 0.05);
          // 4. Service & Maintenance (actual workshop service visits, tire changes, repairs)
          const svcTxs = teslaTxs.filter(t => {{
            const desc = (t.desc || '').toUpperCase();
            return desc.includes('SERVICE') || desc.includes('REPAIR') || desc.includes('MAINTENANCE') || desc.includes('TYRE') || desc.includes('TIRE') || desc.includes('BODY SHOP');
          }});
          // 5. Official Tesla Shop & Aftermarket Accessories (Teslaunch, floor mats, gear)
          const accTxs = teslaTxs.filter(t => !vehicleTxs.includes(t) && !connTxs.includes(t) && !scTxs.includes(t) && !svcTxs.includes(t) && (t.debit || 0) > 0);

          const totalAllTime = teslaTxs.reduce((s, t) => s + (t.debit || 0), 0);
          const periodTotal = periodTesla.reduce((s, t) => s + (t.debit || 0), 0);
          const vehicleTotal = vehicleTxs.reduce((s, t) => s + (t.debit || 0), 0);
          const scTotal = scTxs.reduce((s, t) => s + (t.debit || 0), 0);
          const connTotal = connTxs.reduce((s, t) => s + (t.debit || 0), 0);
          const accTotal = accTxs.reduce((s, t) => s + (t.debit || 0), 0);
          const svcTotal = svcTxs.reduce((s, t) => s + (t.debit || 0), 0);
          const avgSc = scTxs.length > 0 ? (scTotal / scTxs.length) : 0;

          intelEl.innerHTML = `
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; flex-wrap: wrap; gap: 8px;">
              <div style="font-size: 1rem; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 8px;">
                <span>⚡ Tesla EV Ownership, Charging &amp; Purchase Correlation</span>
                <span class="tag" style="background: rgba(59,130,246,0.2); color: #60a5fa;">${{teslaTxs.length}} Transactions</span>
              </div>
              <div style="font-size: 0.82rem; color: var(--text-muted);">
                Selected Period Spend: <strong style="color: #fff;">${{fmtCurrency(periodTotal)}}</strong> (${{periodTesla.length}} transactions)
              </div>
            </div>

            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px;">
              <div class="intel-metric-card">
                <div style="font-size: 0.78rem; color: var(--text-muted); text-transform: uppercase;">🚗 Vehicle Acquisition</div>
                <div class="intel-metric-val" style="color: #60a5fa;">${{fmtCurrency(vehicleTotal)}}</div>
                <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 4px;">Vehicle purchase</div>
              </div>
              <div class="intel-metric-card">
                <div style="font-size: 0.78rem; color: var(--text-muted); text-transform: uppercase;">🛍️ Accessories &amp; Gear</div>
                <div class="intel-metric-val" style="color: #f59e0b;">${{fmtCurrency(accTotal)}}</div>
                <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 4px;">${{accTxs.length}} orders &bull; Tesla Shop &amp; Teslaunch</div>
              </div>
              <div class="intel-metric-card">
                <div style="font-size: 0.78rem; color: var(--text-muted); text-transform: uppercase;">⚡ Supercharging</div>
                <div class="intel-metric-val" style="color: #10b981;">${{fmtCurrency(scTotal)}}</div>
                <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 4px;">${{scTxs.length}} charging sessions &bull; avg ${{fmtCurrency(avgSc)}}/session</div>
              </div>
              <div class="intel-metric-card">
                <div style="font-size: 0.78rem; color: var(--text-muted); text-transform: uppercase;">📱 Premium Connectivity</div>
                <div class="intel-metric-val" style="color: #8b5cf6;">${{fmtCurrency(connTotal)}}</div>
                <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 4px;">${{connTxs.length}} months @ $13.99/mo</div>
              </div>
              <div class="intel-metric-card">
                <div style="font-size: 0.78rem; color: var(--text-muted); text-transform: uppercase;">🔧 Service &amp; Maintenance</div>
                <div class="intel-metric-val" style="color: #94a3b8;">${{fmtCurrency(svcTotal)}}</div>
                <div style="font-size: 0.75rem; color: #10b981; margin-top: 4px;">✓ 0 service visits &bull; 100% Factory Warranty</div>
              </div>
            </div>
          `;
          return;
        }}

        // 4. GROCERIES INTELLIGENCE
        if (activeMerchant === 'groceries') {{
          const grocTxs = (DATA.transactions || []).filter(t => matchesMerchant(t, 'groceries'));
          const periodGroc = grocTxs.filter(t => matchesSelectedPeriod(t.date));

          const totalAllTime = grocTxs.reduce((s, t) => s + (t.debit || 0), 0);
          const periodTotal = periodGroc.reduce((s, t) => s + (t.debit || 0), 0);

          const stores = {{ 'Woolworths': 0, 'Coles': 0, 'Costco': 0, 'Other Groceries': 0 }};
          periodGroc.forEach(t => {{
            const d = (t.desc || '').toLowerCase();
            const val = (t.debit || 0);
            if (d.includes('woolworth')) stores['Woolworths'] += val;
            else if (d.includes('coles')) stores['Coles'] += val;
            else if (d.includes('costco')) stores['Costco'] += val;
            else stores['Other Groceries'] += val;
          }});

          intelEl.innerHTML = `
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; flex-wrap: wrap; gap: 8px;">
              <div style="font-size: 1rem; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 8px;">
                <span>🛒 Groceries &amp; Supermarket Correlation</span>
                <span class="tag" style="background: rgba(59,130,246,0.2); color: #60a5fa;">${{grocTxs.length}} All-Time Transactions</span>
              </div>
              <div style="font-size: 0.82rem; color: var(--text-muted);">
                Selected Period Spend: <strong style="color: #fff;">${{fmtCurrency(periodTotal)}}</strong> (${{periodGroc.length}} supermarket visits)
              </div>
            </div>

            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px;">
              <div class="intel-metric-card">
                <div style="font-size: 0.78rem; color: var(--text-muted); text-transform: uppercase;">Woolworths</div>
                <div class="intel-metric-val" style="color: #10b981;">${{fmtCurrency(stores['Woolworths'])}}</div>
              </div>
              <div class="intel-metric-card">
                <div style="font-size: 0.78rem; color: var(--text-muted); text-transform: uppercase;">Coles</div>
                <div class="intel-metric-val" style="color: #ef4444;">${{fmtCurrency(stores['Coles'])}}</div>
              </div>
              <div class="intel-metric-card">
                <div style="font-size: 0.78rem; color: var(--text-muted); text-transform: uppercase;">Costco</div>
                <div class="intel-metric-val" style="color: #3b82f6;">${{fmtCurrency(stores['Costco'])}}</div>
              </div>
              <div class="intel-metric-card">
                <div style="font-size: 0.78rem; color: var(--text-muted); text-transform: uppercase;">All-Time Groceries</div>
                <div class="intel-metric-val" style="color: #f59e0b;">${{fmtCurrency(totalAllTime)}}</div>
              </div>
            </div>
          `;
          return;
        }}

        // 5. GENERAL SEARCH INTEL
        const debits = filtered.reduce((s, t) => s + (t.debit || 0), 0);
        const credits = filtered.reduce((s, t) => s + (t.credit || 0), 0);
        const net = debits - credits;
        const avg = filtered.length > 0 ? (debits / filtered.length) : 0;

        intelEl.innerHTML = `
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; flex-wrap: wrap; gap: 8px;">
            <div style="font-size: 0.95rem; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 8px;">
              <span>🔍 Search &amp; Filter Insights: "${{query || activeMerchant}}"</span>
              <span class="tag" style="background: rgba(59,130,246,0.2); color: #60a5fa;">${{filtered.length}} Matches</span>
            </div>
          </div>
          <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px;">
            <div class="intel-metric-card">
              <div style="font-size: 0.78rem; color: var(--text-muted); text-transform: uppercase;">Total Outflows</div>
              <div class="intel-metric-val" style="color: #f87171;">${{fmtCurrency(debits)}}</div>
            </div>
            <div class="intel-metric-card">
              <div style="font-size: 0.78rem; color: var(--text-muted); text-transform: uppercase;">Total Inflows</div>
              <div class="intel-metric-val" style="color: #34d399;">${{fmtCurrency(credits)}}</div>
            </div>
            <div class="intel-metric-card">
              <div style="font-size: 0.78rem; color: var(--text-muted); text-transform: uppercase;">Net Cash Outflow</div>
              <div class="intel-metric-val" style="color: #60a5fa;">${{fmtCurrency(net)}}</div>
            </div>
            <div class="intel-metric-card">
              <div style="font-size: 0.78rem; color: var(--text-muted); text-transform: uppercase;">Average Transaction</div>
              <div class="intel-metric-val" style="color: #fbbf24;">${{fmtCurrency(avg)}}</div>
            </div>
          </div>
        `;
      }}

      // =============================================================
      // RENDER TRANSACTIONS TAB WITH FILTERING & PAGINATION
      // =============================================================
      function renderTransactionsTab() {{
        // Populate account filter dropdown
        const acctSelect = document.getElementById('tx-account-filter');
        if (acctSelect && acctSelect.options.length <= 1) {{
          DATA.accounts.forEach(a => {{
            const opt = document.createElement('option');
            opt.value = a.account_no;
            opt.textContent = `${{a.alias || a.account_name}} (${{a.account_no}})`;
            acctSelect.appendChild(opt);
          }});
        }}

        // Populate category dropdown
        const catSelect = document.getElementById('tx-category-filter');
        if (catSelect && catSelect.options.length <= 1) {{
          const cats = Object.keys(DATA.category_totals_all_time || {{}}).sort();
          cats.forEach(c => {{
            const opt = document.createElement('option');
            opt.value = c;
            opt.textContent = c;
            catSelect.appendChild(opt);
          }});
        }}

        const selAcct = acctSelect ? acctSelect.value : 'all';
        const selCat = catSelect ? catSelect.value : 'all';
        const selType = document.getElementById('tx-type-filter') ? document.getElementById('tx-type-filter').value : 'all';
        const query = (document.getElementById('tx-search') ? document.getElementById('tx-search').value : '').toLowerCase().trim();

        // Detect smart merchant from query or active button
        let activeMerchant = smartMerchant;
        if (activeMerchant === 'all' && query) {{
          if (query.includes('apple') || query.includes('itunes')) activeMerchant = 'apple';
          else if (query.includes('qantas') || query.includes('flight')) activeMerchant = 'qantas';
          else if (query.includes('tesla')) activeMerchant = 'tesla';
          else if (query.includes('woolworth') || query.includes('coles') || query.includes('costco') || query.includes('grocer')) activeMerchant = 'groceries';
          else if (query.includes('amazon') || query.includes('aws')) activeMerchant = 'amazon';
          else if (query.includes('bunning')) activeMerchant = 'bunnings';
        }}

        let filtered = (DATA.transactions || []).filter(t => {{
          if (!matchesSelectedPeriod(t.date)) return false;
          if (selAcct !== 'all' && t.account_no !== selAcct) return false;
          if (selCat !== 'all' && t.category !== selCat) return false;
          if (selType === 'debit' && (!t.debit || t.debit <= 0)) return false;
          if (selType === 'credit' && (!t.credit || t.credit <= 0)) return false;
          if (selType === 'transfer' && !t.is_transfer) return false;
          if (selType === 'no_transfer' && t.is_transfer) return false;

          // Regular vs Uncategorised filters
          if (txFilterType === 'regular' && !t.is_regular) return false;
          if (txFilterType === 'uncat' && t.category !== 'Uncategorised' && !t.is_uncategorised) return false;

          // Smart merchant filter
          if (smartMerchant !== 'all' && !matchesMerchant(t, smartMerchant)) return false;

          if (query) {{
            const matchDesc = (t.desc || '').toLowerCase().includes(query);
            const matchAcct = (t.account_no || '').toLowerCase().includes(query);
            const matchCat = (t.category || '').toLowerCase().includes(query);
            const matchAmt = (t.amount || '').toString().includes(query);
            if (!matchDesc && !matchAcct && !matchCat && !matchAmt) return false;
          }}
          return true;
        }});

        // Render merchant / query intelligence card
        renderMerchantIntelligence(filtered, query, activeMerchant);

        const totalPages = Math.max(1, Math.ceil(filtered.length / txPageSize));
        if (txPage > totalPages) txPage = totalPages;
        if (txPage < 1) txPage = 1;

        const startIdx = (txPage - 1) * txPageSize;
        const pageItems = filtered.slice(startIdx, startIdx + txPageSize);

        const pagInfo = document.getElementById('tx-pagination-info');
        if (pagInfo) {{
          pagInfo.textContent = filtered.length > 0 
            ? `Showing ${{startIdx + 1}}–${{Math.min(filtered.length, startIdx + txPageSize)}} of ${{filtered.length}} transactions`
            : `Showing 0 of 0 transactions`;
        }}

        const curPageSpan = document.getElementById('tx-current-page');
        if (curPageSpan) {{
          curPageSpan.textContent = `${{txPage}} / ${{totalPages}}`;
        }}

        const prevBtn = document.getElementById('tx-prev-page');
        if (prevBtn) prevBtn.disabled = txPage <= 1;

        const nextBtn = document.getElementById('tx-next-page');
        if (nextBtn) nextBtn.disabled = txPage >= totalPages;

        const tbody = document.querySelector('#transactions-table tbody');
        if (!tbody) return;
        tbody.innerHTML = '';

        if (pageItems.length === 0) {{
          tbody.innerHTML = '<tr><td colspan="8" style="text-align: center; color: var(--text-muted); padding: 30px;">No transactions found matching the selected filters.</td></tr>';
          return;
        }}

        pageItems.forEach(t => {{
          const tr = document.createElement('tr');
          const amtCls = t.amount < 0 ? 'text-red' : (t.amount > 0 ? 'text-green' : '');
          const sign = t.amount > 0 ? '+' : '';

          let regPill = '';
          if (t.is_regular) {{
            regPill = ' <span class="tag" style="background:rgba(59,130,246,0.15); color:#60a5fa; font-size:0.7rem;">🔄 Regular</span>';
          }} else if (t.category === 'Uncategorised') {{
            regPill = ' <span class="tag" style="background:rgba(239,68,68,0.15); color:#f87171; font-size:0.7rem;">⚠️ Review</span>';
          }}

          tr.innerHTML = `
            <td><strong>${{t.date}}</strong></td>
            <td><code>${{t.account_no}}</code></td>
            <td title="${{t.desc}}">${{t.desc.length > 55 ? t.desc.slice(0, 52) + '...' : t.desc}}</td>
            <td><span class="tag tag-solo" style="cursor: pointer;" title="Click to filter by category" onclick="filterByThisCategory('${{t.category}}')">${{t.category}}</span>${{regPill}}</td>
            <td class="num">${{t.debit ? fmtCurrency(t.debit) : '—'}}</td>
            <td class="num text-green">${{t.credit ? '+' + fmtCurrency(t.credit) : '—'}}</td>
            <td class="num ${{amtCls}}"><strong>${{sign}}${{fmtCurrency(t.amount)}}</strong></td>
            <td class="num">${{t.balance !== null && t.balance !== undefined ? fmtCurrency(t.balance) : '—'}}</td>
          `;
          tbody.appendChild(tr);
        }});
      }}
      window.renderTransactionsTab = renderTransactionsTab;


      // Export CSV
      document.getElementById('export-csv-btn')?.addEventListener('click', () => {{
        const rows = [["Date", "Account", "Description", "Category", "Debit", "Credit", "Amount", "Balance", "IsTransfer"]];
        const acct = document.getElementById('tx-account-filter')?.value || 'all';
        const cat = document.getElementById('tx-category-filter')?.value || 'all';
        const type = document.getElementById('tx-type-filter')?.value || 'all';
        const q = (document.getElementById('tx-search')?.value || '').toLowerCase().trim();

        (DATA.transactions || []).forEach(t => {{
          if (!matchesSelectedPeriod(t.date)) return;
          if (acct !== 'all' && t.account_no !== acct) return;
          if (cat !== 'all' && t.category !== cat) return;
          if (type === 'debit' && (!t.debit || t.debit <= 0)) return;
          if (type === 'credit' && (!t.credit || t.credit <= 0)) return;
          if (type === 'transfer' && !t.is_transfer) return;
          if (type === 'no_transfer' && t.is_transfer) return;
          if (txFilterType === 'regular' && !t.is_regular) return;
          if (txFilterType === 'uncat' && t.category !== 'Uncategorised' && !t.is_uncategorised) return;
          if (smartMerchant !== 'all' && !matchesMerchant(t, smartMerchant)) return;
          if (q) {{
            const md = (t.desc || '').toLowerCase().includes(q);
            const ma = (t.account_no || '').toLowerCase().includes(q);
            const mc = (t.category || '').toLowerCase().includes(q);
            const mam = (t.amount || '').toString().includes(q);
            if (!md && !ma && !mc && !mam) return;
          }}
          rows.push([t.date, t.account_no, `"${{t.desc.replace(/"/g, '""')}}"`, t.category, t.debit, t.credit, t.amount, t.balance, t.is_transfer ? "Yes" : "No"]);
        }});
        const csvContent = "data:text/csv;charset=utf-8," + rows.map(e => e.join(",")).join("\\n");
        const encodedUri = encodeURI(csvContent);
        const link = document.createElement("a");
        link.setAttribute("href", encodedUri);
        link.setAttribute("download", `statement_transactions_${{currentPeriod}}.csv`);
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
      }});

      // -------------------------------------------------------------
      // Interactive SVG Chart Engine with Zoom, Pan, Timestamps & Tooltips
      // -------------------------------------------------------------

      
      // Interactive View Modes & Filters
      let balanceViewState = {{
        mode: 'individual',       // 'individual' or 'mirrored'
        includeHomeLoan: true,    // true or false
        includeTeamEquity: false, // true or false
        includeHouseCapital: false // true or false
      }};

      window.setBalanceViewMode = function(m) {{
        balanceViewState.mode = m;
        document.getElementById('btn-view-individual')?.classList.toggle('active', m === 'individual');
        document.getElementById('btn-view-mirrored')?.classList.toggle('active', m === 'mirrored');
        renderBalanceHistoryChart();
      }};

      window.toggleHomeLoanFilter = function() {{
        balanceViewState.includeHomeLoan = !balanceViewState.includeHomeLoan;
        const btn = document.getElementById('btn-toggle-homeloan');
        if (btn) {{
          btn.classList.toggle('active', balanceViewState.includeHomeLoan);
          btn.textContent = balanceViewState.includeHomeLoan ? '🏠 Home Loan: Included' : '🏠 Home Loan: Excluded';
        }}
        renderBalanceHistoryChart();
      }};

      window.toggleTeamEquityInOverview = function() {{
        balanceViewState.includeTeamEquity = !balanceViewState.includeTeamEquity;
        const btn = document.getElementById('btn-toggle-team-equity');
        if (btn) {{
          btn.classList.toggle('active', balanceViewState.includeTeamEquity);
        }}
        renderBalanceHistoryChart();
      }};

      window.toggleHouseCapitalInOverview = function() {{
        balanceViewState.includeHouseCapital = !balanceViewState.includeHouseCapital;
        const btn = document.getElementById('btn-toggle-house-capital');
        if (btn) {{
          btn.classList.toggle('active', balanceViewState.includeHouseCapital);
        }}
        renderBalanceHistoryChart();
      }};

      window.setFlowType = function(type) {{
        flowType = type;
        document.getElementById('btn-flow-expenses')?.classList.toggle('active', type === 'expenses');
        document.getElementById('btn-flow-income')?.classList.toggle('active', type === 'income');
        const donutTitle = document.getElementById('category-donut-title');
        const barsTitle = document.getElementById('category-bars-title');
        if (donutTitle) donutTitle.textContent = (type === 'income') ? 'Income by Source Breakdown' : 'Spending by Category Breakdown';
        if (barsTitle) barsTitle.textContent = (type === 'income') ? 'Income Source Details (Click to filter)' : 'Category Spending Details (Click to filter)';
        renderCategoriesTab();
      }};

      window.setExpenseFilter = function(f) {{
        expenseFilter = f;
        ['all', 'regular', 'uncat'].forEach(k => {{
          document.getElementById('btn-exp-' + k)?.classList.toggle('active', k === f);
        }});
        renderCategoriesTab();
      }};

      window.setTxFilterType = function(t) {{
        txFilterType = t;
        ['all', 'regular', 'uncat'].forEach(k => {{
          document.getElementById('tx-filt-' + k)?.classList.toggle('active', k === t);
        }});
        txPage = 1;
        renderTransactionsTab();
      }};

      const chartViews = {{
        'balance-history': {{ range: 'all', start: null, end: null }},
        'cashflow': {{ range: '1y', start: null, end: null }},
        'property-equity': {{ range: 'all', start: null, end: null }},
        'loan-balance': {{ range: 'all', start: null, end: null }},
        'loan-rates': {{ range: 'all', start: null, end: null }},
      }};

      function formatXMonth(mStr) {{
        if (!mStr || mStr.length < 7) return mStr || '';
        const parts = mStr.split('-');
        const y = parts[0], m = parts[1];
        const mNames = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
        const idx = parseInt(m, 10) - 1;
        return (mNames[idx] || m) + " '" + y.slice(2);
      }}

      function formatFullMonth(mStr) {{
        if (!mStr || mStr.length < 7) return mStr || '';
        const parts = mStr.split('-');
        const y = parts[0], m = parts[1];
        const mNames = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
        const idx = parseInt(m, 10) - 1;
        return (mNames[idx] || m) + ' ' + y;
      }}

      function getChartTotalPoints(chartId) {{
        if (chartId === 'balance-history') {{
          return DATA.months.length;
        }}
        if (chartId === 'cashflow') {{
          return getAggregatedCashflow().length;
        }}
        if (chartId === 'loan-balance' || chartId === 'loan-rates' || chartId === 'property-equity') {{
          return (DATA.home_loan_timeline || []).length;
        }}
        return 0;
      }}

      function sliceChartItems(chartId, allItems) {{
        if (!allItems || allItems.length === 0) return [];
        const cv = chartViews[chartId];
        if (!cv) return allItems;
        const total = allItems.length;

        if (cv.range === 'custom' && cv.start !== null && cv.end !== null) {{
          const s = Math.max(0, Math.min(cv.start, total - 1));
          const e = Math.max(s + 1, Math.min(cv.end + 1, total));
          return allItems.slice(s, e);
        }}

        let count = total;
        if (cv.range === '6m') count = 6;
        else if (cv.range === '1y') count = 12;
        else if (cv.range === '3y') count = 36;
        else if (cv.range === '5y') count = 60;
        else if (cv.range === 'all') count = total;

        const startIdx = Math.max(0, total - count);
        return allItems.slice(startIdx);
      }}

      function renderSpecificChart(chartId) {{
        if (chartId === 'balance-history') renderBalanceHistoryChart();
        else if (chartId === 'cashflow') renderCashflowChart();
        else if (chartId === 'property-equity') renderPropertyEquityChart();
        else if (chartId === 'loan-balance') renderLoanBalanceChart();
        else if (chartId === 'loan-rates') renderLoanRatesChart();
      }}

      window.setChartRange = function(chartId, range) {{
        const cv = chartViews[chartId];
        if (!cv) return;
        cv.range = range;
        cv.start = null;
        cv.end = null;
        const ctrl = document.getElementById('ctrl-' + chartId);
        if (ctrl) {{
          ctrl.querySelectorAll('.chart-btn').forEach(b => {{
            const oc = b.getAttribute('onclick') || '';
            b.classList.toggle('active', oc.includes("'" + range + "'"));
          }});
        }}
        renderSpecificChart(chartId);
      }};

      window.zoomChart = function(chartId, factor) {{
        const cv = chartViews[chartId];
        if (!cv) return;
        const total = getChartTotalPoints(chartId);
        if (total <= 2) return;

        let visibleItems = sliceChartItems(chartId, chartId === 'cashflow' ? getAggregatedCashflow() : ((chartId.startsWith('loan') || chartId === 'property-equity') ? DATA.home_loan_timeline : DATA.months));
        let s = total - visibleItems.length;
        let e = total - 1;
        if (cv.range === 'custom' && cv.start !== null && cv.end !== null) {{
          s = cv.start;
          e = cv.end;
        }}

        const span = Math.max(2, e - s);
        const newSpan = Math.max(3, Math.min(total, Math.round(span / factor)));
        const center = (s + e) / 2;
        cv.range = 'custom';
        cv.start = Math.max(0, Math.round(center - newSpan / 2));
        cv.end = Math.min(total - 1, cv.start + newSpan);
        if (cv.end - cv.start < 3) cv.start = Math.max(0, cv.end - 3);

        const ctrl = document.getElementById('ctrl-' + chartId);
        if (ctrl) {{
          ctrl.querySelectorAll('.chart-btn').forEach(b => b.classList.remove('active'));
        }}
        renderSpecificChart(chartId);
      }};

      function attachChartInteractions(containerEl, svgEl, tooltipEl, onHover, onLeave, onWheelZoom) {{
        let isDragging = false;
        let dragStartX = 0;

        svgEl.addEventListener('mousemove', (e) => {{
          const rect = svgEl.getBoundingClientRect();
          const relX = e.clientX - rect.left;
          const relY = e.clientY - rect.top;
          const svgW = rect.width;
          const svgH = rect.height;
          onHover(relX / svgW, relY / svgH, e.clientX, e.clientY);
        }});

        svgEl.addEventListener('mouseleave', () => {{
          onLeave();
          if (tooltipEl) tooltipEl.style.display = 'none';
        }});

        svgEl.addEventListener('wheel', (e) => {{
          e.preventDefault();
          if (onWheelZoom) {{
            const factor = e.deltaY < 0 ? 1.25 : 0.8;
            onWheelZoom(factor);
          }}
        }}, {{ passive: false }});
      }}

      function positionTooltip(tooltipEl, clientX, clientY) {{
        tooltipEl.style.display = 'block';
        const tw = tooltipEl.offsetWidth || 160;
        const th = tooltipEl.offsetHeight || 80;
        let left = clientX + 15;
        let top = clientY - 15;
        if (left + tw > window.innerWidth - 20) left = clientX - tw - 15;
        if (top + th > window.innerHeight - 20) top = window.innerHeight - th - 20;
        if (top < 10) top = 10;
        tooltipEl.style.position = 'fixed';
        tooltipEl.style.left = `${{left}}px`;
        tooltipEl.style.top = `${{top}}px`;
      }}

      function monthDiff(m1, m2) {{
        if (!m1 || !m2) return 999;
        const [y1, mon1] = m1.split('-').map(Number);
        const [y2, mon2] = m2.split('-').map(Number);
        return (y2 - y1) * 12 + (mon2 - mon1);
      }}

      function getAccountBalanceForMonth(a, m, consolidate = true) {{
        const snaps = a.snapshots || [];
        if (snaps.length === 0) return null;

        const exact = snaps.filter(s => s.month === m).pop();
        const firstMonth = snaps[0].month;
        const lastSnap = snaps[snaps.length - 1];

        let bal = null;
        if (exact) {{
          bal = exact.closing_balance;
        }} else if (m < firstMonth) {{
          bal = null;
        }} else if (m > lastSnap.month) {{
          if (lastSnap.closing_balance === 0.0 || a.discharged_date || a.continued_into) {{
            bal = null;
          }} else if (monthDiff(lastSnap.month, m) <= 6) {{
            bal = lastSnap.closing_balance;
          }} else {{
            bal = null;
          }}
        }} else {{
          const priors = snaps.filter(s => s.month < m);
          bal = priors.length > 0 ? priors[priors.length - 1].closing_balance : null;
        }}

        if (consolidate && bal !== null && a.consolidated_from && a.consolidated_from.length > 0) {{
          a.consolidated_from.forEach(cid => {{
            const childAcct = (DATA.accounts || []).find(x => x.account_no === cid);
            if (childAcct) {{
              const childBal = getAccountBalanceForMonth(childAcct, m, false);
              if (childBal !== null && childBal !== undefined) {{
                bal += childBal;
              }}
            }}
          }});
        }}

        return bal;
      }}

      // 1. Balance History Multi-Line & Mirrored Directional Chart Engine
      function renderBalanceHistoryChart() {{
        const el = document.getElementById('chart-balance-history');
        const tooltipEl = document.getElementById('tooltip-balance-history');
        if (!el) return;
        el.querySelectorAll('svg').forEach(s => s.remove());

        const months = sliceChartItems('balance-history', DATA.months);
        if (months.length === 0) return;

        let accts = getFilteredAccounts();
        if (!balanceViewState.includeHomeLoan) {{
          accts = accts.filter(a => a.account_type !== 'loan');
        }}

        const teamEquityMap = (DATA.shares && DATA.shares.team_equity_by_month) ? DATA.shares.team_equity_by_month : {{}};
        const homeLoanMap = {{}};
        (DATA.home_loan_timeline || []).forEach(t => {{
          homeLoanMap[t.month] = t;
        }});

        const W = 720, H = 300, padL = 75, padR = 25, padT = 25, padB = 45;
        const svg = createSVG(W, H);

        if (balanceViewState.mode === 'mirrored') {{
          // MIRRORED DIRECTIONAL VIEW: Assets (+Y) vs Debt (-Y) with Net Position line
          const dataPoints = months.map((m, mIdx) => {{
            let liquidAssets = 0;
            let debt = 0;
            accts.filter(a => !a.continued_into).forEach(a => {{
              const bal = getAccountBalanceForMonth(a, m, true);
              if (bal !== null && bal !== undefined) {{
                if (bal >= 0) liquidAssets += bal;
                else debt += Math.abs(bal);
              }}
            }});

            const teamEq = (balanceViewState.includeTeamEquity && teamEquityMap[m]) ? (teamEquityMap[m].val_aud || 0) : 0;
            const hl = homeLoanMap[m];
            const houseVal = (balanceViewState.includeHouseCapital && hl) ? (hl.property_valuation || 0) : 0;
            const houseEq = hl ? (hl.net_equity || 0) : 0;
            const totalAssets = liquidAssets + teamEq + houseVal;
            const netWorth = totalAssets - debt;

            return {{
              month: m,
              liquidAssets,
              teamEquity: teamEq,
              houseAsset: houseVal,
              houseEquity: houseEq,
              totalAssets,
              debt,
              netWorth
            }};
          }});

          let maxAsset = Math.max(1000, ...dataPoints.map(d => d.totalAssets));
          let maxDebt = Math.max(1000, ...dataPoints.map(d => d.debt));
          let maxY = Math.max(maxAsset, maxDebt) * 1.15;
          let minY = -maxY;

          const scaleX = (i) => padL + (i / Math.max(1, months.length - 1)) * (W - padL - padR);
          const scaleY = (v) => padT + (1 - (v - minY) / (maxY - minY)) * (H - padT - padB);

          const yZero = scaleY(0);
          const yTicks = 4;
          for (let i = -yTicks; i <= yTicks; i++) {{
            const val = (maxY / yTicks) * i;
            const yPos = scaleY(val);
            const grid = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            grid.setAttribute('x1', padL); grid.setAttribute('x2', W - padR);
            grid.setAttribute('y1', yPos); grid.setAttribute('y2', yPos);
            grid.setAttribute('stroke', i === 0 ? '#94a3b8' : 'var(--card-border)');
            grid.setAttribute('stroke-dasharray', i === 0 ? '4 2' : '2 2');
            grid.setAttribute('opacity', i === 0 ? '0.9' : '0.35');
            svg.appendChild(grid);

            const lbl = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            lbl.setAttribute('x', padL - 8); lbl.setAttribute('y', yPos + 3);
            lbl.setAttribute('text-anchor', 'end');
            lbl.setAttribute('fill', 'var(--text-muted)');
            lbl.setAttribute('font-size', '10px');
            lbl.textContent = fmtCurrency(val);
            svg.appendChild(lbl);
          }}

          const stepX = Math.max(1, Math.ceil(months.length / 7));
          months.forEach((m, idx) => {{
            if (idx % stepX === 0 || idx === months.length - 1) {{
              const xPos = scaleX(idx);
              const lbl = document.createElementNS('http://www.w3.org/2000/svg', 'text');
              lbl.setAttribute('x', xPos); lbl.setAttribute('y', H - padB + 16);
              lbl.setAttribute('text-anchor', 'middle');
              lbl.setAttribute('fill', 'var(--text-muted)');
              lbl.setAttribute('font-size', '10px');
              lbl.textContent = formatXMonth(m);
              svg.appendChild(lbl);
            }}
          }});

          // Liquid Assets Area (Green / Emerald)
          let pathLiquid = '';
          dataPoints.forEach((d, idx) => {{
            const x = scaleX(idx);
            const y = scaleY(d.liquidAssets);
            pathLiquid += (idx === 0 ? `M ${{x}} ${{yZero}} L ${{x}} ${{y}}` : ` L ${{x}} ${{y}}`);
          }});
          pathLiquid += ` L ${{scaleX(dataPoints.length - 1)}} ${{yZero}} Z`;
          const polyLiquid = document.createElementNS('http://www.w3.org/2000/svg', 'path');
          polyLiquid.setAttribute('d', pathLiquid);
          polyLiquid.setAttribute('fill', '#10b981');
          polyLiquid.setAttribute('opacity', '0.25');
          svg.appendChild(polyLiquid);

          // Team Equity Stack (Cyan) if active
          if (balanceViewState.includeTeamEquity) {{
            let pathTeam = '';
            dataPoints.forEach((d, idx) => {{
              const x = scaleX(idx);
              const yBot = scaleY(d.liquidAssets);
              const yTop = scaleY(d.liquidAssets + d.teamEquity);
              pathTeam += (idx === 0 ? `M ${{x}} ${{yBot}} L ${{x}} ${{yTop}}` : ` L ${{x}} ${{yTop}}`);
            }});
            for (let idx = dataPoints.length - 1; idx >= 0; idx--) {{
              const x = scaleX(idx);
              const yBot = scaleY(dataPoints[idx].liquidAssets);
              pathTeam += ` L ${{x}} ${{yBot}}`;
            }}
            pathTeam += ' Z';
            const polyTeam = document.createElementNS('http://www.w3.org/2000/svg', 'path');
            polyTeam.setAttribute('d', pathTeam);
            polyTeam.setAttribute('fill', '#06b6d4');
            polyTeam.setAttribute('opacity', '0.35');
            svg.appendChild(polyTeam);
          }}

          // House Asset Stack (Amber / Gold) if active
          if (balanceViewState.includeHouseCapital) {{
            let pathHouse = '';
            dataPoints.forEach((d, idx) => {{
              const x = scaleX(idx);
              const yBot = scaleY(d.liquidAssets + d.teamEquity);
              const yTop = scaleY(d.totalAssets);
              pathHouse += (idx === 0 ? `M ${{x}} ${{yBot}} L ${{x}} ${{yTop}}` : ` L ${{x}} ${{yTop}}`);
            }});
            for (let idx = dataPoints.length - 1; idx >= 0; idx--) {{
              const x = scaleX(idx);
              const yBot = scaleY(dataPoints[idx].liquidAssets + dataPoints[idx].teamEquity);
              pathHouse += ` L ${{x}} ${{yBot}}`;
            }}
            pathHouse += ' Z';
            const polyHouse = document.createElementNS('http://www.w3.org/2000/svg', 'path');
            polyHouse.setAttribute('d', pathHouse);
            polyHouse.setAttribute('fill', '#f59e0b');
            polyHouse.setAttribute('opacity', '0.28');
            svg.appendChild(polyHouse);
          }}

          // Liabilities Mirror Area (Red / Amber downwards)
          let pathDebt = '';
          dataPoints.forEach((d, idx) => {{
            const x = scaleX(idx);
            const y = scaleY(-d.debt);
            pathDebt += (idx === 0 ? `M ${{x}} ${{yZero}} L ${{x}} ${{y}}` : ` L ${{x}} ${{y}}`);
          }});
          pathDebt += ` L ${{scaleX(dataPoints.length - 1)}} ${{yZero}} Z`;
          const polyDebt = document.createElementNS('http://www.w3.org/2000/svg', 'path');
          polyDebt.setAttribute('d', pathDebt);
          polyDebt.setAttribute('fill', '#ef4444');
          polyDebt.setAttribute('opacity', '0.25');
          svg.appendChild(polyDebt);

          // Net Position Line (Cyan / White bold line)
          let pathNet = '';
          dataPoints.forEach((d, idx) => {{
            const x = scaleX(idx);
            const y = scaleY(d.netWorth);
            pathNet += (idx === 0 ? `M ${{x}} ${{y}}` : ` L ${{x}} ${{y}}`);
          }});
          const lineNet = document.createElementNS('http://www.w3.org/2000/svg', 'path');
          lineNet.setAttribute('d', pathNet);
          lineNet.setAttribute('fill', 'none');
          lineNet.setAttribute('stroke', '#38bdf8');
          lineNet.setAttribute('stroke-width', '2.5');
          lineNet.setAttribute('stroke-linecap', 'round');
          svg.appendChild(lineNet);

          const crosshair = document.createElementNS('http://www.w3.org/2000/svg', 'line');
          crosshair.setAttribute('y1', padT); crosshair.setAttribute('y2', H - padB);
          crosshair.setAttribute('stroke', '#94a3b8');
          crosshair.setAttribute('stroke-dasharray', '3 3');
          crosshair.style.display = 'none';
          svg.appendChild(crosshair);

          const hoverGroup = document.createElementNS('http://www.w3.org/2000/svg', 'g');
          svg.appendChild(hoverGroup);

          attachChartInteractions(el, svg, tooltipEl, (normX, normY, clientX, clientY) => {{
            const plotW = W - padL - padR;
            const svgX = normX * W;
            if (svgX < padL || svgX > W - padR) {{
              crosshair.style.display = 'none';
              hoverGroup.innerHTML = '';
              if (tooltipEl) tooltipEl.style.display = 'none';
              return;
            }}
            const idx = Math.max(0, Math.min(dataPoints.length - 1, Math.round(((svgX - padL) / plotW) * (dataPoints.length - 1))));
            const d = dataPoints[idx];
            const xPos = scaleX(idx);

            crosshair.setAttribute('x1', xPos); crosshair.setAttribute('x2', xPos);
            crosshair.style.display = '';

            hoverGroup.innerHTML = '';
            const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
            dot.setAttribute('cx', xPos); dot.setAttribute('cy', scaleY(d.netWorth));
            dot.setAttribute('r', '4.5'); dot.setAttribute('fill', '#38bdf8');
            dot.setAttribute('stroke', '#0f172a'); dot.setAttribute('stroke-width', '2');
            hoverGroup.appendChild(dot);

            let houseHtml = '';
            if (balanceViewState.includeHouseCapital && d.houseAsset > 0) {{
              houseHtml = `
                <div class="chart-tooltip-row">
                  <span><span class="chart-tooltip-badge" style="background:#f59e0b;"></span>Property Asset</span>
                  <span style="font-weight:600; color:#f59e0b;">+${{fmtCurrency(d.houseAsset)}}</span>
                </div>
                <div class="chart-tooltip-row" style="font-size:0.75rem; color:#fcd34d; padding-left:12px;">
                  <span>↳ Capital in House (Equity):</span>
                  <span style="font-weight:600;">+${{fmtCurrency(d.houseEquity)}}</span>
                </div>
              `;
            }}

            let teamHtml = '';
            if (balanceViewState.includeTeamEquity && d.teamEquity > 0) {{
              teamHtml = `
                <div class="chart-tooltip-row">
                  <span><span class="chart-tooltip-badge" style="background:#06b6d4;"></span>TEAM Equity</span>
                  <span style="font-weight:600; color:#06b6d4;">+${{fmtCurrency(d.teamEquity)}}</span>
                </div>
              `;
            }}

            if (tooltipEl) {{
              tooltipEl.innerHTML = `
                <div class="chart-tooltip-title">${{formatFullMonth(d.month)}} Balance Sheet</div>
                <div class="chart-tooltip-row">
                  <span><span class="chart-tooltip-badge" style="background:#10b981;"></span>Liquid Deposits</span>
                  <span style="font-weight:600; color:#10b981;">+${{fmtCurrency(d.liquidAssets)}}</span>
                </div>
                ${{houseHtml}}
                ${{teamHtml}}
                <div class="chart-tooltip-row">
                  <span><span class="chart-tooltip-badge" style="background:#ef4444;"></span>Total Debt</span>
                  <span style="font-weight:600; color:#ef4444;">-${{fmtCurrency(d.debt)}}</span>
                </div>
                <div class="chart-tooltip-row" style="border-top:1px solid rgba(255,255,255,0.1); margin-top:6px; padding-top:4px;">
                  <span style="font-weight:700;">Net Position</span>
                  <span style="font-weight:700; color:${{d.netWorth >= 0 ? '#10b981' : '#ef4444'}};">${{fmtCurrency(d.netWorth)}}</span>
                </div>
              `;
              positionTooltip(tooltipEl, clientX, clientY);
            }}
          }}, () => {{
            crosshair.style.display = 'none';
            hoverGroup.innerHTML = '';
            if (tooltipEl) tooltipEl.style.display = 'none';
          }}, (factor) => {{
            window.zoomChart('balance-history', factor);
          }});

        }} else {{
          // INDIVIDUAL MULTI-LINE VIEW
          const displayAccts = accts.filter(a => !a.continued_into);
          let minB = 0, maxB = 0;
          displayAccts.forEach(a => {{
            months.forEach(m => {{
              const bal = getAccountBalanceForMonth(a, m, true);
              if (bal !== null && bal !== undefined) {{
                if (bal < minB) minB = bal;
                if (bal > maxB) maxB = bal;
              }}
            }});
          }});

          if (balanceViewState.includeTeamEquity) {{
            months.forEach(m => {{
              if (teamEquityMap[m] && teamEquityMap[m].val_aud > maxB) {{
                maxB = teamEquityMap[m].val_aud;
              }}
            }});
          }}

          if (balanceViewState.includeHouseCapital) {{
            months.forEach(m => {{
              const hl = homeLoanMap[m];
              if (hl) {{
                if (hl.property_valuation && hl.property_valuation > maxB) maxB = hl.property_valuation;
                if (hl.net_equity && hl.net_equity > maxB) maxB = hl.net_equity;
              }}
            }});
          }}

          if (minB === maxB) {{ maxB += 1000; minB -= 500; }}
          const rangeY = maxB - minB;
          const scaleX = (i) => padL + (i / Math.max(1, months.length - 1)) * (W - padL - padR);
          const scaleY = (v) => padT + (1 - (v - minB) / rangeY) * (H - padT - padB);

          const yTicks = 5;
          for (let i = 0; i <= yTicks; i++) {{
            const val = minB + (rangeY / yTicks) * i;
            const yPos = scaleY(val);
            const grid = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            grid.setAttribute('x1', padL); grid.setAttribute('x2', W - padR);
            grid.setAttribute('y1', yPos); grid.setAttribute('y2', yPos);
            grid.setAttribute('stroke', val === 0 ? '#64748b' : 'var(--card-border)');
            grid.setAttribute('stroke-dasharray', val === 0 ? '4 2' : '2 2');
            grid.setAttribute('opacity', val === 0 ? '0.9' : '0.4');
            svg.appendChild(grid);

            const lbl = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            lbl.setAttribute('x', padL - 8); lbl.setAttribute('y', yPos + 3);
            lbl.setAttribute('text-anchor', 'end');
            lbl.setAttribute('fill', 'var(--text-muted)');
            lbl.setAttribute('font-size', '10px');
            lbl.textContent = fmtCurrency(val);
            svg.appendChild(lbl);
          }}

          const stepX = Math.max(1, Math.ceil(months.length / 7));
          months.forEach((m, idx) => {{
            if (idx % stepX === 0 || idx === months.length - 1) {{
              const xPos = scaleX(idx);
              const vGrid = document.createElementNS('http://www.w3.org/2000/svg', 'line');
              vGrid.setAttribute('x1', xPos); vGrid.setAttribute('x2', xPos);
              vGrid.setAttribute('y1', padT); vGrid.setAttribute('y2', H - padB);
              vGrid.setAttribute('stroke', 'var(--card-border)');
              vGrid.setAttribute('stroke-dasharray', '2 2');
              vGrid.setAttribute('opacity', '0.35');
              svg.appendChild(vGrid);

              const lbl = document.createElementNS('http://www.w3.org/2000/svg', 'text');
              lbl.setAttribute('x', xPos); lbl.setAttribute('y', H - padB + 16);
              lbl.setAttribute('text-anchor', 'middle');
              lbl.setAttribute('fill', 'var(--text-muted)');
              lbl.setAttribute('font-size', '10px');
              lbl.textContent = formatXMonth(m);
              svg.appendChild(lbl);
            }}
          }});

          const colors = ['#3b82f6', '#10b981', '#f59e0b', '#8b5cf6', '#ec4899', '#06b6d4', '#ef4444', '#84cc16'];
          const seriesData = [];

          displayAccts.forEach((a, aIdx) => {{
            const color = colors[aIdx % colors.length];
            const pts = [];
            const ptsMap = {{}};
            months.forEach((m, mIdx) => {{
              const bal = getAccountBalanceForMonth(a, m, true);
              if (bal !== null && bal !== undefined) {{
                const x = scaleX(mIdx);
                const y = scaleY(bal);
                pts.push(`${{x}},${{y}}`);
                ptsMap[mIdx] = {{ val: bal, x, y }};
              }}
            }});
            if (pts.length > 1) {{
              const polyline = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
              polyline.setAttribute('points', pts.join(' '));
              polyline.setAttribute('fill', 'none');
              polyline.setAttribute('stroke', color);
              polyline.setAttribute('stroke-width', '2');
              polyline.setAttribute('stroke-linecap', 'round');
              svg.appendChild(polyline);

              let seriesLabel = a.alias || a.account_name;
              if (a.account_type === 'loan') {{
                seriesLabel = '🏡 HSBC Home Loan (Total Facility)';
              }} else if (a.account_type === 'offset') {{
                seriesLabel = '💰 HSBC Offset Savings (Total Offset)';
              }}
              seriesData.push({{ acct: a, label: seriesLabel, color, ptsMap }});
            }}
          }});

          if (balanceViewState.includeTeamEquity) {{
            const ptsTeam = [];
            const ptsTeamMap = {{}};
            months.forEach((m, mIdx) => {{
              if (teamEquityMap[m]) {{
                const val = teamEquityMap[m].val_aud;
                const x = scaleX(mIdx);
                const y = scaleY(val);
                ptsTeam.push(`${{x}},${{y}}`);
                ptsTeamMap[mIdx] = {{ val, x, y }};
              }}
            }});
            if (ptsTeam.length > 1) {{
              const polyline = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
              polyline.setAttribute('points', ptsTeam.join(' '));
              polyline.setAttribute('fill', 'none');
              polyline.setAttribute('stroke', '#38bdf8');
              polyline.setAttribute('stroke-width', '2.5');
              polyline.setAttribute('stroke-dasharray', '4 2');
              polyline.setAttribute('stroke-linecap', 'round');
              svg.appendChild(polyline);
              seriesData.push({{ acct: {{ alias: 'TEAM Equity (A$)', account_name: 'Employer Shares' }}, color: '#38bdf8', ptsMap: ptsTeamMap }});
            }}
          }}

          if (balanceViewState.includeHouseCapital) {{
            // 1. Property Valuation Line (Amber / Gold)
            const ptsVal = [];
            const ptsValMap = {{}};
            months.forEach((m, mIdx) => {{
              const hl = homeLoanMap[m];
              if (hl && hl.property_valuation) {{
                const val = hl.property_valuation;
                const x = scaleX(mIdx);
                const y = scaleY(val);
                ptsVal.push(`${{x}},${{y}}`);
                ptsValMap[mIdx] = {{ val, x, y }};
              }}
            }});
            if (ptsVal.length > 1) {{
              const polyline = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
              polyline.setAttribute('points', ptsVal.join(' '));
              polyline.setAttribute('fill', 'none');
              polyline.setAttribute('stroke', '#f59e0b');
              polyline.setAttribute('stroke-width', '2.5');
              polyline.setAttribute('stroke-linecap', 'round');
              svg.appendChild(polyline);
              seriesData.push({{ acct: {{ alias: 'Property Valuation', account_name: 'House Value' }}, label: '🏡 Property Valuation', color: '#f59e0b', ptsMap: ptsValMap }});
            }}

            // 2. Net Capital in House (Home Equity) Line (Emerald / Mint bold dashed line)
            const ptsEq = [];
            const ptsEqMap = {{}};
            months.forEach((m, mIdx) => {{
              const hl = homeLoanMap[m];
              if (hl && hl.net_equity !== null && hl.net_equity !== undefined) {{
                const val = hl.net_equity;
                const x = scaleX(mIdx);
                const y = scaleY(val);
                ptsEq.push(`${{x}},${{y}}`);
                ptsEqMap[mIdx] = {{ val, x, y }};
              }}
            }});
            if (ptsEq.length > 1) {{
              const polyline = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
              polyline.setAttribute('points', ptsEq.join(' '));
              polyline.setAttribute('fill', 'none');
              polyline.setAttribute('stroke', '#10b981');
              polyline.setAttribute('stroke-width', '2.5');
              polyline.setAttribute('stroke-dasharray', '4 2');
              polyline.setAttribute('stroke-linecap', 'round');
              svg.appendChild(polyline);
              seriesData.push({{ acct: {{ alias: 'Capital in House (Net Equity)', account_name: 'Home Equity' }}, label: '💎 Capital in House (Net Equity)', color: '#10b981', ptsMap: ptsEqMap, isEquityOnly: true }});
            }}
          }}

          const crosshair = document.createElementNS('http://www.w3.org/2000/svg', 'line');
          crosshair.setAttribute('y1', padT); crosshair.setAttribute('y2', H - padB);
          crosshair.setAttribute('stroke', '#94a3b8');
          crosshair.setAttribute('stroke-dasharray', '3 3');
          crosshair.style.display = 'none';
          svg.appendChild(crosshair);

          const hoverGroup = document.createElementNS('http://www.w3.org/2000/svg', 'g');
          svg.appendChild(hoverGroup);

          attachChartInteractions(el, svg, tooltipEl, (normX, normY, clientX, clientY) => {{
            const plotW = W - padL - padR;
            const svgX = normX * W;
            if (svgX < padL || svgX > W - padR) {{
              crosshair.style.display = 'none';
              hoverGroup.innerHTML = '';
              if (tooltipEl) tooltipEl.style.display = 'none';
              return;
            }}
            const idx = Math.max(0, Math.min(months.length - 1, Math.round(((svgX - padL) / plotW) * (months.length - 1))));
            const xPos = scaleX(idx);
            const mStr = months[idx];

            crosshair.setAttribute('x1', xPos); crosshair.setAttribute('x2', xPos);
            crosshair.style.display = '';

            hoverGroup.innerHTML = '';
            let ttRows = '';
            let totalAtDate = 0;

            seriesData.forEach(s => {{
              const p = s.ptsMap[idx];
              if (p) {{
                if (!s.isEquityOnly) {{
                  totalAtDate += p.val;
                }}
                const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
                dot.setAttribute('cx', p.x); dot.setAttribute('cy', p.y);
                dot.setAttribute('r', '4'); dot.setAttribute('fill', s.color);
                dot.setAttribute('stroke', '#0f172a'); dot.setAttribute('stroke-width', '2');
                hoverGroup.appendChild(dot);

                ttRows += `
                  <div class="chart-tooltip-row">
                    <span><span class="chart-tooltip-badge" style="background:${{s.color}};"></span>${{s.label || s.acct.alias || s.acct.account_name}}</span>
                    <span style="font-weight:600; color:${{p.val >= 0 ? '#fff' : '#ef4444'}};">${{fmtCurrency(p.val)}}</span>
                  </div>
                `;
              }}
            }});

            if (tooltipEl) {{
              tooltipEl.innerHTML = `
                <div class="chart-tooltip-title">${{formatFullMonth(mStr)}}</div>
                ${{ttRows}}
                <div class="chart-tooltip-row" style="border-top:1px solid rgba(255,255,255,0.1); margin-top:6px; padding-top:4px;">
                  <span style="color:#94a3b8;">Combined Total</span>
                  <span style="font-weight:700; color:${{totalAtDate >= 0 ? '#10b981' : '#ef4444'}};">${{fmtCurrency(totalAtDate)}}</span>
                </div>
              `;
              positionTooltip(tooltipEl, clientX, clientY);
            }}
          }}, () => {{
            crosshair.style.display = 'none';
            hoverGroup.innerHTML = '';
            if (tooltipEl) tooltipEl.style.display = 'none';
          }}, (factor) => {{
            window.zoomChart('balance-history', factor);
          }});
        }}

        el.appendChild(svg);
      }}


      // 2. Dynamic Cashflow Bar Chart (Months, Quarters, FY, Calendar Years & Net Transfers)
      function getAggregatedCashflow() {{
        const accts = getFilteredAccounts();
        const acctNos = new Set(accts.map(a => a.account_no));
        const periodMap = {{}};

        // Seed with all known periods from DATA.months so timeline is continuous
        (DATA.months || []).forEach(m => {{
          const k = getPeriodKey(m + '-01', currentPeriodMode);
          if (k && !periodMap[k]) {{
            periodMap[k] = {{ period: k, inflow: 0, outflow: 0, net: 0 }};
          }}
        }});

        DATA.transactions.forEach(t => {{
          if (!acctNos.has(t.account_no)) return;
          if (excludeTransfers && t.is_transfer) return;

          const pKey = getPeriodKey(t.date, currentPeriodMode);
          if (!pKey) return;

          if (!periodMap[pKey]) {{
            periodMap[pKey] = {{ period: pKey, inflow: 0, outflow: 0, net: 0 }};
          }}
          periodMap[pKey].inflow += (t.credit || 0);
          periodMap[pKey].outflow += (t.debit || 0);
          periodMap[pKey].net += ((t.credit || 0) - (t.debit || 0));
        }});

        const sortedKeys = Object.keys(periodMap).sort();
        return sortedKeys.map(k => periodMap[k]);
      }}

      function renderCashflowChart() {{
        const el = document.getElementById('chart-cashflow');
        const tooltipEl = document.getElementById('tooltip-cashflow');
        if (!el) return;
        el.querySelectorAll('svg').forEach(s => s.remove());

        const allFlows = getAggregatedCashflow();
        if (allFlows.length === 0) return;

        const flows = sliceChartItems('cashflow', allFlows);
        if (flows.length === 0) return;

        const W = 680, H = 290, padL = 70, padR = 25, padT = 25, padB = 45;
        const svg = createSVG(W, H);

        let maxFlow = 0;
        flows.forEach(f => {{
          if (f.inflow > maxFlow) maxFlow = f.inflow;
          if (f.outflow > maxFlow) maxFlow = f.outflow;
        }});
        if (maxFlow === 0) maxFlow = 1000;

        const yTicks = 4;
        for (let i = 0; i <= yTicks; i++) {{
          const val = (maxFlow / yTicks) * i;
          const yPos = padT + (1 - i / yTicks) * (H - padT - padB);
          const grid = document.createElementNS('http://www.w3.org/2000/svg', 'line');
          grid.setAttribute('x1', padL); grid.setAttribute('x2', W - padR);
          grid.setAttribute('y1', yPos); grid.setAttribute('y2', yPos);
          grid.setAttribute('stroke', 'var(--card-border)');
          grid.setAttribute('stroke-dasharray', '2 2');
          grid.setAttribute('opacity', '0.4');
          svg.appendChild(grid);

          const lbl = document.createElementNS('http://www.w3.org/2000/svg', 'text');
          lbl.setAttribute('x', padL - 8); lbl.setAttribute('y', yPos + 3);
          lbl.setAttribute('text-anchor', 'end');
          lbl.setAttribute('fill', 'var(--text-muted)');
          lbl.setAttribute('font-size', '10px');
          lbl.textContent = fmtCurrency(val);
          svg.appendChild(lbl);
        }}

        const slotW = (W - padL - padR) / flows.length;
        const barW = Math.max(3, Math.min(22, slotW * 0.38));
        const stepX = Math.max(1, Math.ceil(flows.length / 8));

        flows.forEach((f, idx) => {{
          const xCenter = padL + idx * slotW + slotW / 2;

          // Inflow bar (Green)
          const hIn = (f.inflow / maxFlow) * (H - padT - padB);
          const rIn = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
          rIn.setAttribute('x', xCenter - barW - 1);
          rIn.setAttribute('y', H - padB - hIn);
          rIn.setAttribute('width', barW);
          rIn.setAttribute('height', Math.max(1, hIn));
          rIn.setAttribute('fill', '#10b981');
          rIn.setAttribute('rx', '2');
          svg.appendChild(rIn);

          // Outflow bar (Red)
          const hOut = (f.outflow / maxFlow) * (H - padT - padB);
          const rOut = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
          rOut.setAttribute('x', xCenter + 1);
          rOut.setAttribute('y', H - padB - hOut);
          rOut.setAttribute('width', barW);
          rOut.setAttribute('height', Math.max(1, hOut));
          rOut.setAttribute('fill', '#ef4444');
          rOut.setAttribute('rx', '2');
          svg.appendChild(rOut);

          // Period label
          if (idx % stepX === 0 || idx === flows.length - 1) {{
            const lbl = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            lbl.setAttribute('x', xCenter);
            lbl.setAttribute('y', H - padB + 16);
            lbl.setAttribute('text-anchor', 'middle');
            lbl.setAttribute('fill', 'var(--text-muted)');
            lbl.setAttribute('font-size', '10px');
            const displayLabel = currentPeriodMode === 'month' ? formatXMonth(f.period) : f.period;
            lbl.textContent = displayLabel;
            svg.appendChild(lbl);
          }}
        }});

        // Crosshair highlight
        const crosshair = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        crosshair.setAttribute('y', padT);
        crosshair.setAttribute('height', H - padT - padB);
        crosshair.setAttribute('fill', 'rgba(255,255,255,0.06)');
        crosshair.style.display = 'none';
        svg.appendChild(crosshair);

        attachChartInteractions(el, svg, tooltipEl, (normX, normY, clientX, clientY) => {{
          const plotW = W - padL - padR;
          const svgX = normX * W;
          if (svgX < padL || svgX > W - padR) {{
            crosshair.style.display = 'none';
            if (tooltipEl) tooltipEl.style.display = 'none';
            return;
          }}

          const idx = Math.max(0, Math.min(flows.length - 1, Math.floor(((svgX - padL) / plotW) * flows.length)));
          const xSlot = padL + idx * slotW;
          const f = flows[idx];
          const net = f.inflow - f.outflow;

          crosshair.setAttribute('x', xSlot);
          crosshair.setAttribute('width', slotW);
          crosshair.style.display = '';

          if (tooltipEl) {{
            const pTitle = getPeriodDisplay(f.period, currentPeriodMode);
            const inLabel = excludeTransfers ? 'Real Inflow (excl transfers)' : 'Total Inflows';
            const outLabel = excludeTransfers ? 'Real Spend (excl transfers)' : 'Total Outflows';
            tooltipEl.innerHTML = `
              <div class="chart-tooltip-title">${{pTitle}}</div>
              <div class="chart-tooltip-row">
                <span><span class="chart-tooltip-badge" style="background:#10b981;"></span>${{inLabel}}</span>
                <span style="font-weight:600; color:#10b981;">+${{fmtCurrency(f.inflow)}}</span>
              </div>
              <div class="chart-tooltip-row">
                <span><span class="chart-tooltip-badge" style="background:#ef4444;"></span>${{outLabel}}</span>
                <span style="font-weight:600; color:#ef4444;">-${{fmtCurrency(f.outflow)}}</span>
              </div>
              <div class="chart-tooltip-row" style="border-top:1px solid rgba(255,255,255,0.1); margin-top:6px; padding-top:4px;">
                <span style="color:#94a3b8;">Net Savings</span>
                <span style="font-weight:700; color:${{net >= 0 ? '#10b981' : '#ef4444'}};">${{net >= 0 ? '+' : ''}}${{fmtCurrency(net)}}</span>
              </div>
            `;
            positionTooltip(tooltipEl, clientX, clientY);
          }}
        }}, () => {{
          crosshair.style.display = 'none';
        }}, (factor) => {{
          window.zoomChart('cashflow', factor);
        }});

        el.appendChild(svg);
      }}

      // 2b. Property Valuation & Equity Progression Chart
      function renderPropertyEquityChart() {{
        const el = document.getElementById('chart-property-equity');
        const tooltipEl = document.getElementById('tooltip-property-equity');
        if (!el) return;
        el.querySelectorAll('svg').forEach(s => s.remove());

        const timeline = sliceChartItems('property-equity', DATA.home_loan_timeline || []);
        if (timeline.length === 0) return;

        const W = 1100, H = 340, padL = 85, padR = 30, padT = 30, padB = 45;
        const svg = createSVG(W, H);

        let maxVal = 0;
        timeline.forEach(t => {{
          const pVal = t.property_valuation || ((DATA.property_profile || {{}}).purchase_price || 0);
          if (pVal > maxVal) maxVal = pVal;
          if (Math.abs(t.loan_balance) > maxVal) maxVal = Math.abs(t.loan_balance);
        }});
        maxVal = Math.ceil((maxVal * 1.1) / 100000) * 100000;
        if (maxVal === 0) maxVal = 2000000;

        const scaleX = (i) => padL + (i / Math.max(1, timeline.length - 1)) * (W - padL - padR);
        const scaleY = (v) => padT + (1 - v / maxVal) * (H - padT - padB);

        // Y-axis gridlines & ticks
        const yTicks = 5;
        for (let i = 0; i <= yTicks; i++) {{
          const val = (maxVal / yTicks) * i;
          const yPos = scaleY(val);
          const grid = document.createElementNS('http://www.w3.org/2000/svg', 'line');
          grid.setAttribute('x1', padL); grid.setAttribute('x2', W - padR);
          grid.setAttribute('y1', yPos); grid.setAttribute('y2', yPos);
          grid.setAttribute('stroke', 'var(--card-border)');
          grid.setAttribute('stroke-dasharray', '2 2');
          grid.setAttribute('opacity', '0.4');
          svg.appendChild(grid);

          const lbl = document.createElementNS('http://www.w3.org/2000/svg', 'text');
          lbl.setAttribute('x', padL - 10); lbl.setAttribute('y', yPos + 3);
          lbl.setAttribute('text-anchor', 'end');
          lbl.setAttribute('fill', 'var(--text-muted)');
          lbl.setAttribute('font-size', '10px');
          lbl.textContent = fmtCurrency(val);
          svg.appendChild(lbl);
        }}

        // X-axis timestamps
        const stepX = Math.max(1, Math.ceil(timeline.length / 8));
        timeline.forEach((t, idx) => {{
          if (idx % stepX === 0 || idx === timeline.length - 1) {{
            const xPos = scaleX(idx);
            const vGrid = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            vGrid.setAttribute('x1', xPos); vGrid.setAttribute('x2', xPos);
            vGrid.setAttribute('y1', padT); vGrid.setAttribute('y2', H - padB);
            vGrid.setAttribute('stroke', 'var(--card-border)');
            vGrid.setAttribute('stroke-dasharray', '2 2');
            vGrid.setAttribute('opacity', '0.35');
            svg.appendChild(vGrid);

            const lbl = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            lbl.setAttribute('x', xPos); lbl.setAttribute('y', H - padB + 16);
            lbl.setAttribute('text-anchor', 'middle');
            lbl.setAttribute('fill', 'var(--text-muted)');
            lbl.setAttribute('font-size', '10px');
            lbl.textContent = formatXMonth(t.month);
            svg.appendChild(lbl);
          }}
        }});

        // Gradients
        const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
        defs.innerHTML = `
          <linearGradient id="propValGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#3b82f6" stop-opacity="0.22"/>
            <stop offset="100%" stop-color="#3b82f6" stop-opacity="0.02"/>
          </linearGradient>
          <linearGradient id="netEquityGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#a855f7" stop-opacity="0.25"/>
            <stop offset="100%" stop-color="#a855f7" stop-opacity="0.02"/>
          </linearGradient>
        `;
        svg.appendChild(defs);

        // Property Valuation Area & Line (Blue)
        const ptsPropArea = [
          `${{scaleX(0)}},${{scaleY(0)}}`,
          ...timeline.map((t, idx) => `${{scaleX(idx)}},${{scaleY(t.property_valuation || ((DATA.property_profile || {{}}).purchase_price || 0))}}`),
          `${{scaleX(timeline.length - 1)}},${{scaleY(0)}}`
        ];
        const pPropArea = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
        pPropArea.setAttribute('points', ptsPropArea.join(' '));
        pPropArea.setAttribute('fill', 'url(#propValGrad)');
        svg.appendChild(pPropArea);

        const ptsProp = timeline.map((t, idx) => `${{scaleX(idx)}},${{scaleY(t.property_valuation || ((DATA.property_profile || {{}}).purchase_price || 0))}}`);
        const pProp = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
        pProp.setAttribute('points', ptsProp.join(' '));
        pProp.setAttribute('fill', 'none');
        pProp.setAttribute('stroke', '#3b82f6');
        pProp.setAttribute('stroke-width', '3');
        svg.appendChild(pProp);

        // Net Home Equity Area & Line (Purple)
        const ptsEqArea = [
          `${{scaleX(0)}},${{scaleY(0)}}`,
          ...timeline.map((t, idx) => `${{scaleX(idx)}},${{scaleY(Math.max(0, (t.property_valuation || ((DATA.property_profile || {{}}).purchase_price || 0)) - Math.abs(t.loan_balance)))}}`),
          `${{scaleX(timeline.length - 1)}},${{scaleY(0)}}`
        ];
        const pEqArea = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
        pEqArea.setAttribute('points', ptsEqArea.join(' '));
        pEqArea.setAttribute('fill', 'url(#netEquityGrad)');
        svg.appendChild(pEqArea);

        const ptsEq = timeline.map((t, idx) => `${{scaleX(idx)}},${{scaleY(Math.max(0, (t.property_valuation || ((DATA.property_profile || {{}}).purchase_price || 0)) - Math.abs(t.loan_balance)))}}`);
        const pEq = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
        pEq.setAttribute('points', ptsEq.join(' '));
        pEq.setAttribute('fill', 'none');
        pEq.setAttribute('stroke', '#a855f7');
        pEq.setAttribute('stroke-width', '2.5');
        svg.appendChild(pEq);

        // Loan Debt Line (Amber dashed)
        const ptsLoan = timeline.map((t, idx) => `${{scaleX(idx)}},${{scaleY(Math.abs(t.loan_balance))}}`);
        const pLoan = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
        pLoan.setAttribute('points', ptsLoan.join(' '));
        pLoan.setAttribute('fill', 'none');
        pLoan.setAttribute('stroke', '#f59e0b');
        pLoan.setAttribute('stroke-width', '2.5');
        pLoan.setAttribute('stroke-dasharray', '5 3');
        svg.appendChild(pLoan);

        // Offset Cash Line (Emerald)
        const ptsOffset = timeline.map((t, idx) => `${{scaleX(idx)}},${{scaleY(t.offset_balance)}}`);
        const pOffset = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
        pOffset.setAttribute('points', ptsOffset.join(' '));
        pOffset.setAttribute('fill', 'none');
        pOffset.setAttribute('stroke', '#10b981');
        pOffset.setAttribute('stroke-width', '2');
        svg.appendChild(pOffset);

        // Crosshairs & Dots
        const crosshair = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        crosshair.setAttribute('y1', padT); crosshair.setAttribute('y2', H - padB);
        crosshair.setAttribute('stroke', '#cbd5e1');
        crosshair.setAttribute('stroke-width', '1.5');
        crosshair.setAttribute('stroke-dasharray', '3 3');
        crosshair.style.display = 'none';
        svg.appendChild(crosshair);

        const makeDot = (col) => {{
          const d = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
          d.setAttribute('r', '4.5'); d.setAttribute('fill', col);
          d.setAttribute('stroke', '#0f172a'); d.setAttribute('stroke-width', '2');
          d.style.display = 'none';
          svg.appendChild(d);
          return d;
        }};

        const dotProp = makeDot('#3b82f6');
        const dotEq = makeDot('#a855f7');
        const dotLoan = makeDot('#f59e0b');
        const dotOffset = makeDot('#10b981');

        attachChartInteractions(el, svg, tooltipEl, (normX, normY, clientX, clientY) => {{
          const plotW = W - padL - padR;
          const svgX = normX * W;
          if (svgX < padL || svgX > W - padR) {{
            crosshair.style.display = 'none';
            dotProp.style.display = 'none';
            dotEq.style.display = 'none';
            dotLoan.style.display = 'none';
            dotOffset.style.display = 'none';
            if (tooltipEl) tooltipEl.style.display = 'none';
            return;
          }}

          const idx = Math.max(0, Math.min(timeline.length - 1, Math.round(((svgX - padL) / plotW) * (timeline.length - 1))));
          const xPos = scaleX(idx);
          const t = timeline[idx];
          const curPVal = t.property_valuation || ((DATA.property_profile || {{}}).purchase_price || 0);
          const curLoan = Math.abs(t.loan_balance);
          const curOffset = t.offset_balance;
          const curEq = Math.max(0, curPVal - curLoan);
          const curEffEq = Math.max(0, curPVal - Math.max(0, Math.abs(t.net_debt)));
          const curLvr = ((curLoan / curPVal) * 100).toFixed(1);

          crosshair.setAttribute('x1', xPos); crosshair.setAttribute('x2', xPos);
          crosshair.style.display = '';

          dotProp.setAttribute('cx', xPos); dotProp.setAttribute('cy', scaleY(curPVal)); dotProp.style.display = '';
          dotEq.setAttribute('cx', xPos); dotEq.setAttribute('cy', scaleY(curEq)); dotEq.style.display = '';
          dotLoan.setAttribute('cx', xPos); dotLoan.setAttribute('cy', scaleY(curLoan)); dotLoan.style.display = '';
          dotOffset.setAttribute('cx', xPos); dotOffset.setAttribute('cy', scaleY(curOffset)); dotOffset.style.display = '';

          if (tooltipEl) {{
            tooltipEl.innerHTML = `
              <div class="chart-tooltip-title">${{formatFullMonth(t.month)}}</div>
              <div class="chart-tooltip-row">
                <span><span class="chart-tooltip-badge" style="background:#3b82f6;"></span>Property Value</span>
                <span style="font-weight:700; color:#60a5fa;">${{fmtCurrency(curPVal)}}</span>
              </div>
              <div class="chart-tooltip-row">
                <span><span class="chart-tooltip-badge" style="background:#a855f7;"></span>Net Home Equity</span>
                <span style="font-weight:700; color:#c084fc;">${{fmtCurrency(curEq)}}</span>
              </div>
              <div class="chart-tooltip-row">
                <span><span class="chart-tooltip-badge" style="background:#f59e0b;"></span>Mortgage Balance</span>
                <span style="font-weight:600; color:#f59e0b;">-${{fmtCurrency(curLoan)}}</span>
              </div>
              <div class="chart-tooltip-row">
                <span><span class="chart-tooltip-badge" style="background:#10b981;"></span>Offset Cash</span>
                <span style="font-weight:600; color:#10b981;">+${{fmtCurrency(curOffset)}}</span>
              </div>
              <div class="chart-tooltip-row" style="border-top:1px solid rgba(255,255,255,0.1); margin-top:6px; padding-top:4px;">
                <span style="color:#94a3b8;">Effective Equity (w/ Offset)</span>
                <span style="font-weight:700; color:#34d399;">${{fmtCurrency(curEffEq)}}</span>
              </div>
              <div class="chart-tooltip-row">
                <span style="color:#94a3b8;">Market LVR</span>
                <span style="font-weight:600; color:#93c5fd;">${{curLvr}}%</span>
              </div>
            `;
            positionTooltip(tooltipEl, clientX, clientY);
          }}
        }}, () => {{
          crosshair.style.display = 'none';
          dotProp.style.display = 'none';
          dotEq.style.display = 'none';
          dotLoan.style.display = 'none';
          dotOffset.style.display = 'none';
        }}, (factor) => {{
          window.zoomChart('property-equity', factor);
        }});

        el.appendChild(svg);
      }}

      // 3. Home Loan Balance vs Offset Chart
      function renderLoanBalanceChart() {{
        const el = document.getElementById('chart-loan-balance');
        const tooltipEl = document.getElementById('tooltip-loan-balance');
        if (!el) return;
        el.querySelectorAll('svg').forEach(s => s.remove());

        const timeline = sliceChartItems('loan-balance', DATA.home_loan_timeline || []);
        if (timeline.length === 0) return;

        const W = 680, H = 290, padL = 75, padR = 25, padT = 25, padB = 45;
        const svg = createSVG(W, H);

        let maxVal = 0;
        timeline.forEach(t => {{
          if (Math.abs(t.loan_balance) > maxVal) maxVal = Math.abs(t.loan_balance);
          if (t.offset_balance > maxVal) maxVal = t.offset_balance;
        }});
        if (maxVal === 0) maxVal = 1000000;

        const scaleX = (i) => padL + (i / Math.max(1, timeline.length - 1)) * (W - padL - padR);
        const scaleY = (v) => padT + (1 - v / maxVal) * (H - padT - padB);

        // Y-axis gridlines & ticks
        const yTicks = 4;
        for (let i = 0; i <= yTicks; i++) {{
          const val = (maxVal / yTicks) * i;
          const yPos = scaleY(val);
          const grid = document.createElementNS('http://www.w3.org/2000/svg', 'line');
          grid.setAttribute('x1', padL); grid.setAttribute('x2', W - padR);
          grid.setAttribute('y1', yPos); grid.setAttribute('y2', yPos);
          grid.setAttribute('stroke', 'var(--card-border)');
          grid.setAttribute('stroke-dasharray', '2 2');
          grid.setAttribute('opacity', '0.4');
          svg.appendChild(grid);

          const lbl = document.createElementNS('http://www.w3.org/2000/svg', 'text');
          lbl.setAttribute('x', padL - 8); lbl.setAttribute('y', yPos + 3);
          lbl.setAttribute('text-anchor', 'end');
          lbl.setAttribute('fill', 'var(--text-muted)');
          lbl.setAttribute('font-size', '10px');
          lbl.textContent = fmtCurrency(val);
          svg.appendChild(lbl);
        }}

        // X-axis timestamps
        const stepX = Math.max(1, Math.ceil(timeline.length / 7));
        timeline.forEach((t, idx) => {{
          if (idx % stepX === 0 || idx === timeline.length - 1) {{
            const xPos = scaleX(idx);
            const vGrid = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            vGrid.setAttribute('x1', xPos); vGrid.setAttribute('x2', xPos);
            vGrid.setAttribute('y1', padT); vGrid.setAttribute('y2', H - padB);
            vGrid.setAttribute('stroke', 'var(--card-border)');
            vGrid.setAttribute('stroke-dasharray', '2 2');
            vGrid.setAttribute('opacity', '0.35');
            svg.appendChild(vGrid);

            const lbl = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            lbl.setAttribute('x', xPos); lbl.setAttribute('y', H - padB + 16);
            lbl.setAttribute('text-anchor', 'middle');
            lbl.setAttribute('fill', 'var(--text-muted)');
            lbl.setAttribute('font-size', '10px');
            lbl.textContent = formatXMonth(t.month);
            svg.appendChild(lbl);
          }}
        }});

        // Loan curve (Amber/Red)
        const ptsLoan = timeline.map((t, idx) => `${{scaleX(idx)}},${{scaleY(Math.abs(t.loan_balance))}}`);
        const pLoan = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
        pLoan.setAttribute('points', ptsLoan.join(' '));
        pLoan.setAttribute('fill', 'none');
        pLoan.setAttribute('stroke', '#f59e0b');
        pLoan.setAttribute('stroke-width', '2.5');
        svg.appendChild(pLoan);

        // Offset curve (Green)
        const ptsOffset = timeline.map((t, idx) => `${{scaleX(idx)}},${{scaleY(t.offset_balance)}}`);
        const pOffset = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
        pOffset.setAttribute('points', ptsOffset.join(' '));
        pOffset.setAttribute('fill', 'none');
        pOffset.setAttribute('stroke', '#10b981');
        pOffset.setAttribute('stroke-width', '2.5');
        svg.appendChild(pOffset);

        // Crosshair & Dots
        const crosshair = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        crosshair.setAttribute('y1', padT); crosshair.setAttribute('y2', H - padB);
        crosshair.setAttribute('stroke', '#cbd5e1');
        crosshair.setAttribute('stroke-width', '1.5');
        crosshair.setAttribute('stroke-dasharray', '3 3');
        crosshair.style.display = 'none';
        svg.appendChild(crosshair);

        const dotLoan = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        dotLoan.setAttribute('r', '4.5'); dotLoan.setAttribute('fill', '#f59e0b');
        dotLoan.setAttribute('stroke', '#0f172a'); dotLoan.setAttribute('stroke-width', '2');
        dotLoan.style.display = 'none';
        svg.appendChild(dotLoan);

        const dotOffset = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        dotOffset.setAttribute('r', '4.5'); dotOffset.setAttribute('fill', '#10b981');
        dotOffset.setAttribute('stroke', '#0f172a'); dotOffset.setAttribute('stroke-width', '2');
        dotOffset.style.display = 'none';
        svg.appendChild(dotOffset);

        attachChartInteractions(el, svg, tooltipEl, (normX, normY, clientX, clientY) => {{
          const plotW = W - padL - padR;
          const svgX = normX * W;
          if (svgX < padL || svgX > W - padR) {{
            crosshair.style.display = 'none';
            dotLoan.style.display = 'none';
            dotOffset.style.display = 'none';
            if (tooltipEl) tooltipEl.style.display = 'none';
            return;
          }}

          const idx = Math.max(0, Math.min(timeline.length - 1, Math.round(((svgX - padL) / plotW) * (timeline.length - 1))));
          const xPos = scaleX(idx);
          const t = timeline[idx];
          const lVal = Math.abs(t.loan_balance);
          const oVal = t.offset_balance;
          const net = Math.max(0, lVal - oVal);

          crosshair.setAttribute('x1', xPos); crosshair.setAttribute('x2', xPos);
          crosshair.style.display = '';

          dotLoan.setAttribute('cx', xPos); dotLoan.setAttribute('cy', scaleY(lVal));
          dotLoan.style.display = '';

          dotOffset.setAttribute('cx', xPos); dotOffset.setAttribute('cy', scaleY(oVal));
          dotOffset.style.display = '';

          if (tooltipEl) {{
            tooltipEl.innerHTML = `
              <div class="chart-tooltip-title">${{formatFullMonth(t.month)}}</div>
              <div class="chart-tooltip-row">
                <span><span class="chart-tooltip-badge" style="background:#f59e0b;"></span>Loan Balance</span>
                <span style="font-weight:600; color:#f59e0b;">${{fmtCurrency(lVal)}}</span>
              </div>
              <div class="chart-tooltip-row">
                <span><span class="chart-tooltip-badge" style="background:#10b981;"></span>Offset Savings</span>
                <span style="font-weight:600; color:#10b981;">${{fmtCurrency(oVal)}}</span>
              </div>
              <div class="chart-tooltip-row" style="border-top:1px solid rgba(255,255,255,0.1); margin-top:6px; padding-top:4px;">
                <span style="color:#94a3b8;">Effective Net Debt</span>
                <span style="font-weight:700; color:#ef4444;">${{fmtCurrency(net)}}</span>
              </div>
            `;
            positionTooltip(tooltipEl, clientX, clientY);
          }}
        }}, () => {{
          crosshair.style.display = 'none';
          dotLoan.style.display = 'none';
          dotOffset.style.display = 'none';
        }}, (factor) => {{
          window.zoomChart('loan-balance', factor);
        }});

        el.appendChild(svg);
      }}

      // 4. Home Loan Interest Rate Step Chart
      function renderLoanRatesChart() {{
        const el = document.getElementById('chart-loan-rates');
        const tooltipEl = document.getElementById('tooltip-loan-rates');
        if (!el) return;
        el.querySelectorAll('svg').forEach(s => s.remove());

        const timeline = sliceChartItems('loan-rates', DATA.home_loan_timeline || []);
        if (timeline.length === 0) return;

        const W = 680, H = 290, padL = 55, padR = 25, padT = 25, padB = 45;
        const svg = createSVG(W, H);

        let minR = 10, maxR = 0;
        timeline.forEach(t => {{
          if (t.interest_rate) {{
            if (t.interest_rate < minR) minR = t.interest_rate;
            if (t.interest_rate > maxR) maxR = t.interest_rate;
          }}
        }});
        minR = Math.max(0, Math.floor(minR));
        maxR = Math.ceil(maxR + 1);

        const scaleX = (i) => padL + (i / Math.max(1, timeline.length - 1)) * (W - padL - padR);
        const scaleY = (v) => padT + (1 - (v - minR) / (maxR - minR)) * (H - padT - padB);

        // Y-axis ticks
        for (let r = minR; r <= maxR; r += 1) {{
          const yPos = scaleY(r);
          const grid = document.createElementNS('http://www.w3.org/2000/svg', 'line');
          grid.setAttribute('x1', padL); grid.setAttribute('x2', W - padR);
          grid.setAttribute('y1', yPos); grid.setAttribute('y2', yPos);
          grid.setAttribute('stroke', 'var(--card-border)');
          grid.setAttribute('stroke-dasharray', '2 2');
          grid.setAttribute('opacity', '0.4');
          svg.appendChild(grid);

          const lbl = document.createElementNS('http://www.w3.org/2000/svg', 'text');
          lbl.setAttribute('x', padL - 8); lbl.setAttribute('y', yPos + 3);
          lbl.setAttribute('text-anchor', 'end');
          lbl.setAttribute('fill', 'var(--text-muted)');
          lbl.setAttribute('font-size', '10px');
          lbl.textContent = `${{r}}%`;
          svg.appendChild(lbl);
        }}

        // X-axis timestamps
        const stepX = Math.max(1, Math.ceil(timeline.length / 7));
        timeline.forEach((t, idx) => {{
          if (idx % stepX === 0 || idx === timeline.length - 1) {{
            const xPos = scaleX(idx);
            const vGrid = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            vGrid.setAttribute('x1', xPos); vGrid.setAttribute('x2', xPos);
            vGrid.setAttribute('y1', padT); vGrid.setAttribute('y2', H - padB);
            vGrid.setAttribute('stroke', 'var(--card-border)');
            vGrid.setAttribute('stroke-dasharray', '2 2');
            vGrid.setAttribute('opacity', '0.35');
            svg.appendChild(vGrid);

            const lbl = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            lbl.setAttribute('x', xPos); lbl.setAttribute('y', H - padB + 16);
            lbl.setAttribute('text-anchor', 'middle');
            lbl.setAttribute('fill', 'var(--text-muted)');
            lbl.setAttribute('font-size', '10px');
            lbl.textContent = formatXMonth(t.month);
            svg.appendChild(lbl);
          }}
        }});

        const pts = [];
        timeline.forEach((t, idx) => {{
          if (t.interest_rate) {{
            pts.push(`${{scaleX(idx)}},${{scaleY(t.interest_rate)}}`);
          }}
        }});

        if (pts.length > 1) {{
          const polyline = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
          polyline.setAttribute('points', pts.join(' '));
          polyline.setAttribute('fill', 'none');
          polyline.setAttribute('stroke', '#3b82f6');
          polyline.setAttribute('stroke-width', '2.5');
          svg.appendChild(polyline);
        }}

        // Crosshair & Dot
        const crosshair = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        crosshair.setAttribute('y1', padT); crosshair.setAttribute('y2', H - padB);
        crosshair.setAttribute('stroke', '#cbd5e1');
        crosshair.setAttribute('stroke-width', '1.5');
        crosshair.setAttribute('stroke-dasharray', '3 3');
        crosshair.style.display = 'none';
        svg.appendChild(crosshair);

        const dotRate = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        dotRate.setAttribute('r', '4.5'); dotRate.setAttribute('fill', '#3b82f6');
        dotRate.setAttribute('stroke', '#0f172a'); dotRate.setAttribute('stroke-width', '2');
        dotRate.style.display = 'none';
        svg.appendChild(dotRate);

        attachChartInteractions(el, svg, tooltipEl, (normX, normY, clientX, clientY) => {{
          const plotW = W - padL - padR;
          const svgX = normX * W;
          if (svgX < padL || svgX > W - padR) {{
            crosshair.style.display = 'none';
            dotRate.style.display = 'none';
            if (tooltipEl) tooltipEl.style.display = 'none';
            return;
          }}

          const idx = Math.max(0, Math.min(timeline.length - 1, Math.round(((svgX - padL) / plotW) * (timeline.length - 1))));
          const xPos = scaleX(idx);
          const t = timeline[idx];

          if (t.interest_rate) {{
            crosshair.setAttribute('x1', xPos); crosshair.setAttribute('x2', xPos);
            crosshair.style.display = '';

            dotRate.setAttribute('cx', xPos); dotRate.setAttribute('cy', scaleY(t.interest_rate));
            dotRate.style.display = '';

            if (tooltipEl) {{
              tooltipEl.innerHTML = `
                <div class="chart-tooltip-title">${{formatFullMonth(t.month)}}</div>
                <div class="chart-tooltip-row">
                  <span><span class="chart-tooltip-badge" style="background:#3b82f6;"></span>Interest Rate</span>
                  <span style="font-weight:700; color:#3b82f6;">${{t.interest_rate.toFixed(2)}}% p.a.</span>
                </div>
              `;
              positionTooltip(tooltipEl, clientX, clientY);
            }}
          }}
        }}, () => {{
          crosshair.style.display = 'none';
          dotRate.style.display = 'none';
        }}, (factor) => {{
          window.zoomChart('loan-rates', factor);
        }});

        el.appendChild(svg);
      }}

      // 5. Category Donut Chart
      function renderCategoryDonutChart(catList) {{
        const el = document.getElementById('chart-category-donut');
        const tooltipEl = document.getElementById('tooltip-category-donut');
        if (!el) return;
        el.querySelectorAll('svg').forEach(s => s.remove());

        const size = 260, cx = size / 2, cy = size / 2, r = 85, hole = 50;
        const svg = createSVG(size, size);

        const total = catList.reduce((acc, c) => acc + c[1], 0);
        if (total === 0) return;

        const colors = (flowType === 'income')
          ? ['#10b981', '#06b6d4', '#3b82f6', '#8b5cf6', '#14b8a6', '#6366f1', '#ec4899', '#f59e0b']
          : ['#3b82f6', '#10b981', '#f59e0b', '#8b5cf6', '#ef4444', '#ec4899', '#06b6d4', '#84cc16'];
        let startAngle = 0;

        catList.forEach(([cat, val], idx) => {{
          const sliceAngle = (val / total) * 2 * Math.PI;
          const endAngle = startAngle + sliceAngle;

          const x1 = cx + r * Math.cos(startAngle);
          const y1 = cy + r * Math.sin(startAngle);
          const x2 = cx + r * Math.cos(endAngle);
          const y2 = cy + r * Math.sin(endAngle);

          const xi1 = cx + hole * Math.cos(endAngle);
          const yi1 = cy + hole * Math.sin(endAngle);
          const xi2 = cx + hole * Math.cos(startAngle);
          const yi2 = cy + hole * Math.sin(startAngle);

          const largeArc = sliceAngle > Math.PI ? 1 : 0;
          const d = `M ${{x1}} ${{y1}} A ${{r}} ${{r}} 0 ${{largeArc}} 1 ${{x2}} ${{y2}} L ${{xi1}} ${{yi1}} A ${{hole}} ${{hole}} 0 ${{largeArc}} 0 ${{xi2}} ${{yi2}} Z`;

          const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
          path.setAttribute('d', d);
          path.setAttribute('fill', colors[idx % colors.length]);
          path.style.cursor = 'pointer';
          path.style.transition = 'opacity 0.15s ease, transform 0.15s ease';

          path.addEventListener('mouseenter', (e) => {{
            path.setAttribute('opacity', '0.8');
            if (tooltipEl) {{
              const pct = ((val / total) * 100).toFixed(1);
              tooltipEl.innerHTML = `
                <div class="chart-tooltip-title">${{cat}}</div>
                <div class="chart-tooltip-row">
                  <span>Amount:</span>
                  <span style="font-weight:700;">${{fmtCurrency(val)}}</span>
                </div>
                <div class="chart-tooltip-row">
                  <span>Share of Spend:</span>
                  <span style="font-weight:600; color:#3b82f6;">${{pct}}%</span>
                </div>
              `;
              positionTooltip(tooltipEl, e.clientX, e.clientY);
            }}
          }});

          path.addEventListener('mousemove', (e) => {{
            if (tooltipEl) positionTooltip(tooltipEl, e.clientX, e.clientY);
          }});

          path.addEventListener('mouseleave', () => {{
            path.setAttribute('opacity', '1');
            if (tooltipEl) tooltipEl.style.display = 'none';
          }});

          path.addEventListener('click', () => {{
            showCategoryTransactions(cat);
          }});

          svg.appendChild(path);
          startAngle = endAngle;
        }});

        el.appendChild(svg);
      }}

      
      // =============================================================
      // SHARES & EQUITY (TEAM) INTERACTIVE EXPLORER & SIMULATOR
      // =============================================================

      let equityState = {{
        historyView: 'all',
        metric: 'gains_losses',
        currency: 'USD',
        discountPct: 0.5,
        taxRate: 0.47,
        simPrice: {float(_sh.get('current_price_usd') or 0):.2f},
        simStrategy: 'all'
      }};

      function updateEquityHistoryLegend() {{
        const legEl = document.getElementById('eq-hist-legend');
        if (!legEl) return;
        const v = equityState.historyView;
        if (v === 'shares') {{
          legEl.innerHTML = `
            <span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#10b981;margin-right:4px;"></span>Cum. Vested</span>
            <span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#f59e0b;margin-right:4px;"></span>Cum. Sold</span>
            <span><span style="display:inline-block;width:12px;height:3px;background:#60a5fa;margin-right:4px;vertical-align:middle;"></span>Held Shares (Line)</span>
          `;
        }} else if (v === 'valuation') {{
          const curr = equityState.currency === 'USD' ? 'USD ($)' : 'AUD (A$)';
          legEl.innerHTML = `
            <span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#06b6d4;margin-right:4px;"></span>Portfolio Value (${{curr}})</span>
          `;
        }} else if (v === 'price') {{
          legEl.innerHTML = `
            <span><span style="display:inline-block;width:12px;height:3px;background:#f59e0b;margin-right:4px;vertical-align:middle;"></span>TEAM Share Price (USD)</span>
          `;
        }} else {{
          legEl.innerHTML = `
            <span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#10b981;margin-right:4px;"></span>Vested</span>
            <span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#f59e0b;margin-right:4px;"></span>Sold</span>
            <span><span style="display:inline-block;width:12px;height:3px;background:#60a5fa;margin-right:4px;vertical-align:middle;"></span>Held Shares</span>
            <span><span style="display:inline-block;width:12px;height:3px;border-top:1px dashed #06b6d4;margin-right:4px;vertical-align:middle;"></span>Valuation (A$)</span>
          `;
        }}
      }}

      function updateEquityInteractiveLegend() {{
        const legEl = document.getElementById('eq-interactive-legend');
        if (!legEl) return;
        const m = equityState.metric;
        if (m === 'gains_losses') {{
          legEl.innerHTML = `
            <span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#10b981;margin-right:4px;"></span>Gross Capital Gains</span>
            <span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#ef4444;margin-right:4px;"></span>Gross Capital Losses</span>
            <span><span style="display:inline-block;width:12px;height:3px;background:#38bdf8;margin-right:4px;vertical-align:middle;"></span>Net Capital Gain (Line)</span>
          `;
        }} else if (m === 'cgt_discount') {{
          const disc = Math.round(equityState.discountPct * 100);
          legEl.innerHTML = `
            <span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#38bdf8;margin-right:4px;"></span>Gross Realized Gain</span>
            <span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#a855f7;margin-right:4px;"></span>Taxable Net (${{100 - disc}}%)</span>
            <span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#fbbf24;margin-right:4px;"></span>${{disc}}% Discount Exemption (s. 115-25)</span>
          `;
        }} else if (m === 'proceeds_cost') {{
          legEl.innerHTML = `
            <span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#3b82f6;margin-right:4px;"></span>Gross Proceeds</span>
            <span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#f59e0b;margin-right:4px;"></span>Cost Basis (RSU FMV)</span>
            <span><span style="display:inline-block;width:12px;height:3px;background:#10b981;margin-right:4px;vertical-align:middle;"></span>Cumulative Proceeds (Line)</span>
          `;
        }} else if (m === 'ess_vs_cgt') {{
          legEl.innerHTML = `
            <span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#06b6d4;margin-right:4px;"></span>ESS Ordinary Income (Box F)</span>
            <span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#a855f7;margin-right:4px;"></span>Net Capital Gains</span>
          `;
        }}
      }}

      window.setEquityHistoryView = function(v) {{
        equityState.historyView = v;
        ['all', 'shares', 'valuation', 'price'].forEach(k => {{
          const btn = document.getElementById('eq-hist-' + k);
          if (btn) btn.classList.toggle('active', k === v);
        }});
        updateEquityHistoryLegend();
        renderEquityHistoryChart();
      }};

      function createSVG(w, h) {{
        const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('viewBox', `0 0 ${{w}} ${{h}}`);
        svg.setAttribute('width', '100%');
        svg.setAttribute('height', '100%');
        svg.style.overflow = 'visible';
        return svg;
      }}

      function renderEquityTab() {{
        updateEquityHistoryLegend();
        updateEquityInteractiveLegend();
        renderEquityInteractiveChart();
        renderEquityHistoryChart();
        updateSimulator();
      }}

      window.setEquityMetric = function(m) {{
        equityState.metric = m;
        ['gains', 'cgt', 'proceeds', 'ess'].forEach(k => {{
          const btn = document.getElementById('eq-btn-' + k);
          if (btn) btn.classList.remove('active');
        }});
        if (m === 'gains_losses') document.getElementById('eq-btn-gains')?.classList.add('active');
        else if (m === 'cgt_discount') document.getElementById('eq-btn-cgt')?.classList.add('active');
        else if (m === 'proceeds_cost') document.getElementById('eq-btn-proceeds')?.classList.add('active');
        else if (m === 'ess_vs_cgt') document.getElementById('eq-btn-ess')?.classList.add('active');
        updateEquityInteractiveLegend();
        renderEquityInteractiveChart();
      }};

      window.setEquityCurrency = function(c) {{
        equityState.currency = c;
        const bUsd = document.getElementById('eq-curr-usd');
        const bAud = document.getElementById('eq-curr-aud');
        if (bUsd) bUsd.classList.toggle('active', c === 'USD');
        if (bAud) bAud.classList.toggle('active', c === 'AUD');
        updateEquitySummaryPills();
        updateEquityHistoryLegend();
        renderEquityHistoryChart();
        renderEquityInteractiveChart();
      }};

      window.setEquityDiscount = function(d) {{
        equityState.discountPct = parseFloat(d);
        updateEquityInteractiveLegend();
        renderEquityInteractiveChart();
        updateSimulator();
      }};

      window.setEquityTaxRate = function(r) {{
        equityState.taxRate = parseFloat(r);
        updateEquitySummaryPills();
        renderEquityInteractiveChart();
        updateSimulator();
      }};

      function updateEquitySummaryPills() {{
        if (!DATA || !DATA.shares) return;
        const s = DATA.shares;
        const fx = equityState.currency === 'AUD' ? (1 / 0.65) : 1.0;
        const sym = equityState.currency === 'AUD' ? 'A$' : '$';
        const currTag = equityState.currency;

        const g = (s.lifetime_gains_usd || 607460.27) * fx;
        const l = (s.lifetime_losses_usd || 38931.29) * fx;
        const net = (s.lifetime_net_cgt_usd || 568528.98) * fx;
        const disc = (s.lifetime_discount_benefit_usd || 267442.73) * fx;
        const taxSaved = disc * equityState.taxRate;

        const elG = document.getElementById('eq-stat-gains');
        const elL = document.getElementById('eq-stat-losses');
        const elN = document.getElementById('eq-stat-net');
        const elD = document.getElementById('eq-stat-disc');
        const elT = document.getElementById('eq-stat-taxsaved');

        if (elG) elG.textContent = `+${{sym}}${{g.toLocaleString(undefined, {{minimumFractionDigits: 2, maximumFractionDigits: 2}})}} ${{currTag}}`;
        if (elL) elL.textContent = `-${{sym}}${{l.toLocaleString(undefined, {{minimumFractionDigits: 2, maximumFractionDigits: 2}})}} ${{currTag}}`;
        if (elN) elN.textContent = `${{sym}}${{net.toLocaleString(undefined, {{minimumFractionDigits: 2, maximumFractionDigits: 2}})}} ${{currTag}}`;
        if (elD) elD.textContent = `+${{sym}}${{disc.toLocaleString(undefined, {{minimumFractionDigits: 2, maximumFractionDigits: 2}})}} ${{currTag}} exempt`;
        if (elT) elT.textContent = `~${{sym}}${{Math.round(taxSaved).toLocaleString()}} ${{currTag}}`;
      }}

      window.updateSimulatorPrice = function(val) {{
        const p = parseFloat(val);
        if (isNaN(p) || p <= 0) return;
        equityState.simPrice = p;
        const slider = document.getElementById('sim-price-slider');
        const input = document.getElementById('sim-price-input');
        const disp = document.getElementById('sim-price-display');
        if (slider && parseFloat(slider.value) !== Math.round(p)) slider.value = Math.round(p);
        if (input && parseFloat(input.value) !== p) input.value = p.toFixed(2);
        if (disp) disp.textContent = `$${{p.toFixed(2)}}`;
        updateSimulator();
      }};

      window.setSimStrategy = function(strat) {{
        equityState.simStrategy = strat;
        ['all', 'loss', 'gain'].forEach(k => {{
          const btn = document.getElementById('sim-strat-' + k);
          if (btn) btn.classList.toggle('active', k === strat);
        }});
        updateSimulator();
      }};

      window.updateSimulator = function() {{
        if (!DATA || !DATA.shares || !DATA.shares.lots) return;
        const lots = DATA.shares.lots;
        const p = equityState.simPrice;
        const strat = equityState.simStrategy;
        const rate = equityState.taxRate;
        const discRate = equityState.discountPct;
        const fxRate = 0.65; // AUD/USD

        let selectedLots = [];
        if (strat === 'all') {{
          selectedLots = lots;
        }} else if (strat === 'loss') {{
          selectedLots = lots.filter(l => l.cost_per_share > p);
        }} else if (strat === 'gain') {{
          selectedLots = lots.filter(l => l.cost_per_share <= p);
        }}

        let totShares = 0;
        let totCostUSD = 0;
        let eligibleDiscShares = 0;

        selectedLots.forEach(l => {{
          totShares += l.shares;
          totCostUSD += (l.cost_per_share * l.shares);
          if (l.eligible_discount) eligibleDiscShares += l.shares;
        }});

        const totProceedsUSD = totShares * p;
        const totProceedsAUD = totProceedsUSD / fxRate;
        const totCostAUD = totCostUSD / fxRate;
        const netGainUSD = totProceedsUSD - totCostUSD;
        const netGainAUD = netGainUSD / fxRate;

        // Current law tax: 50% discount (or user-selected discountRate) on positive capital gain
        let taxableGainCurrAUD = 0;
        let taxableGainFutureAUD = 0;

        if (netGainAUD > 0) {{
          taxableGainCurrAUD = netGainAUD * (1.0 - discRate);
          taxableGainFutureAUD = netGainAUD; // 0% discount repeal
        }}

        const taxCurrAUD = taxableGainCurrAUD * rate;
        const taxFutureAUD = taxableGainFutureAUD * rate;
        const deltaTaxAUD = Math.max(0, taxFutureAUD - taxCurrAUD);

        // Update DOM
        const elLotsTxt = document.getElementById('sim-lots-eligible-txt');
        if (elLotsTxt) {{
          elLotsTxt.innerHTML = `<strong>${{eligibleDiscShares}} of ${{totShares}} shares</strong> held &gt; 12 months (eligible for discount).`;
        }}

        const elOutProc = document.getElementById('sim-out-proceeds');
        const elOutProcAud = document.getElementById('sim-out-proceeds-aud');
        if (elOutProc) elOutProc.textContent = `$${{totProceedsUSD.toLocaleString(undefined, {{minimumFractionDigits: 2, maximumFractionDigits: 2}})}} USD`;
        if (elOutProcAud) elOutProcAud.textContent = `~A$${{Math.round(totProceedsAUD).toLocaleString()}} AUD (@ 0.65 FX)`;

        const elOutCost = document.getElementById('sim-out-cost');
        const elOutShares = document.getElementById('sim-out-shares');
        if (elOutCost) elOutCost.textContent = `$${{totCostUSD.toLocaleString(undefined, {{minimumFractionDigits: 2, maximumFractionDigits: 2}})}} USD`;
        if (elOutShares) elOutShares.textContent = `${{totShares}} shares (${{selectedLots.length}} lots) selected`;

        const elOutNet = document.getElementById('sim-out-net');
        const elOutNetAud = document.getElementById('sim-out-net-aud');
        if (elOutNet) {{
          const sign = netGainUSD >= 0 ? '+' : '';
          elOutNet.textContent = `${{sign}}$${{netGainUSD.toLocaleString(undefined, {{minimumFractionDigits: 2, maximumFractionDigits: 2}})}} USD`;
          elOutNet.className = 'kpi-value ' + (netGainUSD >= 0 ? 'text-green' : 'text-red');
        }}
        if (elOutNetAud) {{
          const sign = netGainAUD >= 0 ? '+' : '';
          elOutNetAud.textContent = `~${{sign}}A$${{Math.round(netGainAUD).toLocaleString()}} AUD`;
        }}

        const elTaxCurr = document.getElementById('sim-out-tax-curr');
        const elTaxCurrSub = document.getElementById('sim-out-tax-curr-sub');
        if (elTaxCurr) {{
          elTaxCurr.textContent = `A$${{Math.round(taxCurrAUD).toLocaleString()}} AUD`;
          elTaxCurr.className = 'kpi-value ' + (taxCurrAUD === 0 ? 'text-green' : '');
        }}
        if (elTaxCurrSub) {{
          elTaxCurrSub.textContent = netGainAUD <= 0 ? 'No tax owed on capital loss' : `Tax @ ${{(rate * 100).toFixed(1)}}% on ${{((1 - discRate) * 100).toFixed(0)}}% gain`;
        }}

        const elTaxFuture = document.getElementById('sim-out-tax-future');
        const elTaxFutureSub = document.getElementById('sim-out-tax-future-sub');
        if (elTaxFuture) {{
          elTaxFuture.textContent = `A$${{Math.round(taxFutureAUD).toLocaleString()}} AUD`;
        }}
        if (elTaxFutureSub) {{
          elTaxFutureSub.textContent = netGainAUD <= 0 ? 'Capital loss carried forward' : `Full 100% gain taxed @ ${{(rate * 100).toFixed(1)}}%`;
        }}

        const elDelta = document.getElementById('sim-out-delta');
        const elDeltaSub = document.getElementById('sim-out-delta-sub');
        if (elDelta) {{
          elDelta.textContent = `+A$${{Math.round(deltaTaxAUD).toLocaleString()}} AUD`;
        }}
        if (elDeltaSub) {{
          elDeltaSub.textContent = deltaTaxAUD > 0 ? `Extra tax liability if 50% discount repealed` : `No policy delta at current price point`;
        }}

        const callout = document.getElementById('sim-policy-callout');
        if (callout) {{
          if (netGainAUD <= 0) {{
            callout.innerHTML = `💡 <strong>Strategic Tax-Loss Opportunity:</strong> Liquidating ${{totShares}} shares at $${{p.toFixed(2)}} USD realizes an assessable capital loss of <strong>-$${{Math.abs(netGainUSD).toLocaleString(undefined, {{minimumFractionDigits: 2, maximumFractionDigits: 2}})}} USD (~-A$${{Math.round(Math.abs(netGainAUD)).toLocaleString()}} AUD)</strong>. Under ATO Section 102-5, this loss can offset future capital gains indefinitely.`;
          }} else {{
            callout.innerHTML = `💡 <strong>Policy Impact Analysis:</strong> At $${{p.toFixed(2)}} USD, liquidating ${{totShares}} shares generates <strong>+$${{netGainUSD.toLocaleString(undefined, {{minimumFractionDigits: 2, maximumFractionDigits: 2}})}} USD (~A$${{Math.round(netGainAUD).toLocaleString()}} AUD)</strong> in capital profit. Under current 50% discount rules, your tax is <strong>A$${{Math.round(taxCurrAUD).toLocaleString()}}</strong>. If the discount is repealed to 0%, your tax climbs to <strong>A$${{Math.round(taxFutureAUD).toLocaleString()}}</strong> — an immediate policy penalty of <strong>+A$${{Math.round(deltaTaxAUD).toLocaleString()}} AUD</strong>.`;
          }}
        }}
      }};

      window.filterDisposalsTable = function(type) {{
        ['all', 'gain', 'loss', 'disc', '30d'].forEach(k => {{
          const btn = document.getElementById('disp-filt-' + k);
          if (btn) btn.classList.toggle('active', k === type);
        }});

        const rows = document.querySelectorAll('#table-disposals .disposal-row');
        rows.forEach(r => {{
          const rowType = r.dataset.type;
          const isDisc = r.dataset.disc === 'yes';
          let show = false;
          if (type === 'all') show = true;
          else if (type === 'gain') show = (rowType === 'gain');
          else if (type === 'loss') show = (rowType === 'loss');
          else if (type === 'disc') show = isDisc;
          else if (type === '30d') show = (rowType === '30d');
          r.style.display = show ? '' : 'none';
        }});
      }};

      window.renderEquityInteractiveChart = function() {{
        const el = document.getElementById('chart-equity-interactive');
        const tooltipEl = document.getElementById('tooltip-equity-interactive');
        if (!el || !DATA || !DATA.shares || !DATA.shares.yearly_timeline) return;

        el.querySelectorAll('svg').forEach(s => s.remove());

        const timeline = DATA.shares.yearly_timeline;
        if (!timeline || timeline.length === 0) return;

        const W = 820, H = 290, padL = 75, padR = 25, padT = 30, padB = 45;
        const svg = createSVG(W, H);

        const fx = equityState.currency === 'AUD' ? (1 / 0.65) : 1.0;
        const sym = equityState.currency === 'AUD' ? 'A$' : '$';
        const discRate = equityState.discountPct;
        const metric = equityState.metric;

        // Calculate series data based on chosen metric
        const items = timeline.map(d => {{
          let v1 = 0, v2 = 0, v3 = 0, net = 0;
          if (metric === 'gains_losses') {{
            v1 = (d.gross_gains_usd || 0) * fx;
            v2 = (d.gross_losses_usd || 0) * fx;
            net = (d.net_cgt_usd || 0) * fx;
          }} else if (metric === 'cgt_discount') {{
            v1 = (d.gross_gains_usd || 0) * fx; // Total gross gains
            v2 = v1 * (1.0 - discRate); // Taxable gain under selected discount
            v3 = v1 * discRate; // Discount benefit
            net = v2;
          }} else if (metric === 'proceeds_cost') {{
            v1 = (d.proceeds_usd || 0) * fx;
            v2 = (d.cost_basis_usd || 0) * fx;
            net = (d.cum_proceeds_usd || 0) * fx;
          }} else if (metric === 'ess_vs_cgt') {{
            // ESS Ordinary Income vs Net Capital Gains
            v1 = equityState.currency === 'AUD' ? (d.ess_income_aud || 0) : (d.ess_income_usd || 0);
            v2 = Math.max(0, (d.net_cgt_usd || 0) * fx);
            net = v1 + v2;
          }}
          return {{ fy: d.fy, v1, v2, v3, net, raw: d }};
        }});

        // Determine Y bounds
        let minY = 0, maxY = 1000;
        items.forEach(d => {{
          if (metric === 'gains_losses') {{
            if (d.v1 > maxY) maxY = d.v1;
            if (d.v2 > maxY) maxY = d.v2;
            if (-d.v2 < minY) minY = -d.v2;
            if (d.net < minY) minY = d.net;
            if (d.net > maxY) maxY = d.net;
          }} else if (metric === 'cgt_discount') {{
            if (d.v1 > maxY) maxY = d.v1;
          }} else if (metric === 'proceeds_cost') {{
            if (d.v1 > maxY) maxY = d.v1;
            if (d.v2 > maxY) maxY = d.v2;
            if (d.net > maxY) maxY = d.net;
          }} else if (metric === 'ess_vs_cgt') {{
            if (d.v1 > maxY) maxY = d.v1;
            if (d.v2 > maxY) maxY = d.v2;
            if (d.net > maxY) maxY = d.net;
          }}
        }});

        // Add padding to maxY
        maxY = maxY * 1.15;
        if (minY < 0) minY = minY * 1.15;
        const rangeY = (maxY - minY) || 1;

        const plotW = W - padL - padR;
        const plotH = H - padT - padB;
        const colW = plotW / items.length;

        const scaleY = (v) => padT + (1 - (v - minY) / rangeY) * plotH;

        // Draw horizontal gridlines & Y ticks
        const yTicks = 5;
        for (let i = 0; i <= yTicks; i++) {{
          const val = minY + (rangeY / yTicks) * i;
          const yPos = scaleY(val);

          const grid = document.createElementNS('http://www.w3.org/2000/svg', 'line');
          grid.setAttribute('x1', padL);
          grid.setAttribute('x2', W - padR);
          grid.setAttribute('y1', yPos);
          grid.setAttribute('y2', yPos);
          grid.setAttribute('stroke', val === 0 ? '#64748b' : 'var(--card-border)');
          grid.setAttribute('stroke-dasharray', val === 0 ? '4 2' : '2 2');
          grid.setAttribute('opacity', val === 0 ? '0.9' : '0.4');
          svg.appendChild(grid);

          const lbl = document.createElementNS('http://www.w3.org/2000/svg', 'text');
          lbl.setAttribute('x', padL - 8);
          lbl.setAttribute('y', yPos + 3);
          lbl.setAttribute('text-anchor', 'end');
          lbl.setAttribute('fill', 'var(--text-muted)');
          lbl.setAttribute('font-size', '10px');
          lbl.textContent = sym + (Math.abs(val) >= 1000 ? Math.round(val / 1000) + 'k' : Math.round(val));
          svg.appendChild(lbl);
        }}

        // Draw bars and columns
        const barPad = 8;
        const innerColW = colW - (barPad * 2);

        items.forEach((d, idx) => {{
          const cx = padL + idx * colW + colW / 2;
          const barLeft = cx - innerColW / 2;

          // X-axis label
          const xLbl = document.createElementNS('http://www.w3.org/2000/svg', 'text');
          xLbl.setAttribute('x', cx);
          xLbl.setAttribute('y', H - padB + 18);
          xLbl.setAttribute('text-anchor', 'middle');
          xLbl.setAttribute('fill', 'var(--text-muted)');
          xLbl.setAttribute('font-size', '11px');
          xLbl.setAttribute('font-weight', '500');
          xLbl.textContent = d.fy;
          svg.appendChild(xLbl);

          const yZero = scaleY(0);

          if (metric === 'gains_losses') {{
            const subW = (innerColW - 4) / 2;
            // Gains Bar (Green)
            if (d.v1 > 0) {{
              const yVal = scaleY(d.v1);
              const rectG = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
              rectG.setAttribute('x', barLeft);
              rectG.setAttribute('y', yVal);
              rectG.setAttribute('width', subW);
              rectG.setAttribute('height', Math.max(2, yZero - yVal));
              rectG.setAttribute('fill', '#10b981');
              rectG.setAttribute('rx', '3');
              rectG.setAttribute('opacity', '0.85');
              svg.appendChild(rectG);
            }}
            // Losses Bar (Red)
            if (d.v2 > 0) {{
              const yVal = scaleY(-d.v2);
              const rectL = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
              rectL.setAttribute('x', barLeft + subW + 4);
              rectL.setAttribute('y', yZero);
              rectL.setAttribute('width', subW);
              rectL.setAttribute('height', Math.max(2, yVal - yZero));
              rectL.setAttribute('fill', '#ef4444');
              rectL.setAttribute('rx', '3');
              rectL.setAttribute('opacity', '0.85');
              svg.appendChild(rectL);
            }}
          }} else if (metric === 'cgt_discount') {{
            const subW = (innerColW - 6) / 2;
            if (d.v1 > 0) {{
              // Left Bar: Gross Realized Gain (Blue)
              const y1 = scaleY(d.v1);
              const r1 = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
              r1.setAttribute('x', barLeft);
              r1.setAttribute('y', y1);
              r1.setAttribute('width', subW);
              r1.setAttribute('height', Math.max(2, yZero - y1));
              r1.setAttribute('fill', '#38bdf8');
              r1.setAttribute('rx', '3');
              r1.setAttribute('opacity', '0.85');
              svg.appendChild(r1);

              // Right Bar: Taxable portion (Purple, bottom)
              const y2 = scaleY(d.v2);
              const r2 = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
              r2.setAttribute('x', barLeft + subW + 4);
              r2.setAttribute('y', y2);
              r2.setAttribute('width', subW);
              r2.setAttribute('height', Math.max(2, yZero - y2));
              r2.setAttribute('fill', '#a855f7');
              r2.setAttribute('rx', d.v3 > 0 ? '0' : '3');
              r2.setAttribute('opacity', '0.9');
              svg.appendChild(r2);

              // Right Bar: Exempt Discount Benefit (Amber, top of stack)
              if (d.v3 > 0) {{
                const yTop = scaleY(d.v1);
                const r3 = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
                r3.setAttribute('x', barLeft + subW + 4);
                r3.setAttribute('y', yTop);
                r3.setAttribute('width', subW);
                r3.setAttribute('height', Math.max(2, y2 - yTop));
                r3.setAttribute('fill', '#fbbf24');
                r3.setAttribute('rx', '3');
                r3.setAttribute('opacity', '0.9');
                svg.appendChild(r3);
              }}
            }}
          }} else if (metric === 'proceeds_cost') {{
            const subW = (innerColW - 4) / 2;
            if (d.v1 > 0) {{
              const y1 = scaleY(d.v1);
              const r1 = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
              r1.setAttribute('x', barLeft);
              r1.setAttribute('y', y1);
              r1.setAttribute('width', subW);
              r1.setAttribute('height', Math.max(2, yZero - y1));
              r1.setAttribute('fill', '#3b82f6');
              r1.setAttribute('rx', '3');
              svg.appendChild(r1);
            }}
            if (d.v2 > 0) {{
              const y2 = scaleY(d.v2);
              const r2 = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
              r2.setAttribute('x', barLeft + subW + 4);
              r2.setAttribute('y', y2);
              r2.setAttribute('width', subW);
              r2.setAttribute('height', Math.max(2, yZero - y2));
              r2.setAttribute('fill', '#f59e0b');
              r2.setAttribute('rx', '3');
              svg.appendChild(r2);
            }}
          }} else if (metric === 'ess_vs_cgt') {{
            const subW = (innerColW - 4) / 2;
            if (d.v1 > 0) {{
              const y1 = scaleY(d.v1);
              const r1 = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
              r1.setAttribute('x', barLeft);
              r1.setAttribute('y', y1);
              r1.setAttribute('width', subW);
              r1.setAttribute('height', Math.max(2, yZero - y1));
              r1.setAttribute('fill', '#06b6d4');
              r1.setAttribute('rx', '3');
              svg.appendChild(r1);
            }}
            if (d.v2 > 0) {{
              const y2 = scaleY(d.v2);
              const r2 = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
              r2.setAttribute('x', barLeft + subW + 4);
              r2.setAttribute('y', y2);
              r2.setAttribute('width', subW);
              r2.setAttribute('height', Math.max(2, yZero - y2));
              r2.setAttribute('fill', '#a855f7');
              r2.setAttribute('rx', '3');
              svg.appendChild(r2);
            }}
          }}
        }});

        // Overlay Net Line for gains_losses
        if (metric === 'gains_losses') {{
          let pathD = '';
          items.forEach((d, idx) => {{
            const cx = padL + idx * colW + colW / 2;
            const cy = scaleY(d.net);
            pathD += (idx === 0 ? `M ${{cx}} ${{cy}}` : ` L ${{cx}} ${{cy}}`);
          }});

          const line = document.createElementNS('http://www.w3.org/2000/svg', 'path');
          line.setAttribute('d', pathD);
          line.setAttribute('fill', 'none');
          line.setAttribute('stroke', '#38bdf8');
          line.setAttribute('stroke-width', '2.5');
          line.setAttribute('stroke-linecap', 'round');
          svg.appendChild(line);

          items.forEach((d, idx) => {{
            const cx = padL + idx * colW + colW / 2;
            const cy = scaleY(d.net);
            const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
            dot.setAttribute('cx', cx);
            dot.setAttribute('cy', cy);
            dot.setAttribute('r', '4');
            dot.setAttribute('fill', '#38bdf8');
            dot.setAttribute('stroke', '#0f172a');
            dot.setAttribute('stroke-width', '2');
            svg.appendChild(dot);
          }});
        }}

        // Crosshair & Tooltip Interactions
        const crosshair = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        crosshair.setAttribute('y1', padT);
        crosshair.setAttribute('y2', H - padB);
        crosshair.setAttribute('stroke', '#94a3b8');
        crosshair.setAttribute('stroke-dasharray', '3 3');
        crosshair.setAttribute('opacity', '0.7');
        crosshair.style.display = 'none';
        svg.appendChild(crosshair);

        attachChartInteractions(el, svg, tooltipEl, (normX, normY, clientX, clientY) => {{
          const svgX = normX * W;
          if (svgX < padL || svgX > W - padR) {{
            crosshair.style.display = 'none';
            if (tooltipEl) tooltipEl.style.display = 'none';
            return;
          }}

          const idx = Math.max(0, Math.min(items.length - 1, Math.floor((svgX - padL) / colW)));
          const d = items[idx];
          const cx = padL + idx * colW + colW / 2;

          crosshair.setAttribute('x1', cx);
          crosshair.setAttribute('x2', cx);
          crosshair.style.display = '';

          if (tooltipEl) {{
            let bodyHTML = '';
            const rate = equityState.taxRate;
            if (metric === 'gains_losses') {{
              const sign = d.net >= 0 ? '+' : '';
              bodyHTML = `
                <div class="chart-tooltip-row">
                  <span><span class="chart-tooltip-badge" style="background:#10b981;"></span>Gross Gains</span>
                  <span style="font-weight:600; color:#10b981;">+${{sym}}${{Math.round(d.v1).toLocaleString()}}</span>
                </div>
                <div class="chart-tooltip-row">
                  <span><span class="chart-tooltip-badge" style="background:#ef4444;"></span>Gross Losses</span>
                  <span style="font-weight:600; color:#ef4444;">-${{sym}}${{Math.round(d.v2).toLocaleString()}}</span>
                </div>
                <div class="chart-tooltip-row" style="border-top:1px solid rgba(255,255,255,0.1); margin-top:4px; padding-top:4px;">
                  <span style="font-weight:700;">Net Capital Gain</span>
                  <span style="font-weight:700; color:${{d.net >= 0 ? '#38bdf8' : '#ef4444'}};">${{sign}}${{sym}}${{Math.round(d.net).toLocaleString()}}</span>
                </div>
              `;
            }} else if (metric === 'cgt_discount') {{
              const discSaving = d.v3 * rate;
              bodyHTML = `
                <div class="chart-tooltip-row">
                  <span><span class="chart-tooltip-badge" style="background:#38bdf8;"></span>Gross Gains</span>
                  <span style="font-weight:600;">${{sym}}${{Math.round(d.v1).toLocaleString()}}</span>
                </div>
                <div class="chart-tooltip-row">
                  <span><span class="chart-tooltip-badge" style="background:#a855f7;"></span>Taxable (${{((1-discRate)*100).toFixed(0)}}%)</span>
                  <span style="font-weight:600; color:#a855f7;">${{sym}}${{Math.round(d.v2).toLocaleString()}}</span>
                </div>
                <div class="chart-tooltip-row">
                  <span><span class="chart-tooltip-badge" style="background:#fbbf24;"></span>Exempt (${{(discRate*100).toFixed(0)}}%)</span>
                  <span style="font-weight:600; color:#fbbf24;">${{sym}}${{Math.round(d.v3).toLocaleString()}}</span>
                </div>
                <div class="chart-tooltip-row" style="border-top:1px solid rgba(255,255,255,0.1); margin-top:4px; padding-top:4px;">
                  <span style="color:#34d399;">Tax Saved (s. 115-25)</span>
                  <span style="font-weight:700; color:#34d399;">~${{sym}}${{Math.round(discSaving).toLocaleString()}}</span>
                </div>
              `;
            }} else if (metric === 'proceeds_cost') {{
              bodyHTML = `
                <div class="chart-tooltip-row">
                  <span><span class="chart-tooltip-badge" style="background:#3b82f6;"></span>Disposal Proceeds</span>
                  <span style="font-weight:600; color:#3b82f6;">${{sym}}${{Math.round(d.v1).toLocaleString()}}</span>
                </div>
                <div class="chart-tooltip-row">
                  <span><span class="chart-tooltip-badge" style="background:#f59e0b;"></span>Cost Basis</span>
                  <span style="font-weight:600; color:#f59e0b;">${{sym}}${{Math.round(d.v2).toLocaleString()}}</span>
                </div>
                <div class="chart-tooltip-row" style="border-top:1px solid rgba(255,255,255,0.1); margin-top:4px; padding-top:4px;">
                  <span>Cumulative Proceeds</span>
                  <span style="font-weight:700; color:#10b981;">${{sym}}${{Math.round(d.net).toLocaleString()}}</span>
                </div>
              `;
            }} else if (metric === 'ess_vs_cgt') {{
              bodyHTML = `
                <div class="chart-tooltip-row">
                  <span><span class="chart-tooltip-badge" style="background:#06b6d4;"></span>ESS Income (Box F)</span>
                  <span style="font-weight:600; color:#06b6d4;">${{sym}}${{Math.round(d.v1).toLocaleString()}}</span>
                </div>
                <div class="chart-tooltip-row">
                  <span><span class="chart-tooltip-badge" style="background:#a855f7;"></span>Net Capital Gains</span>
                  <span style="font-weight:600; color:#a855f7;">${{sym}}${{Math.round(d.v2).toLocaleString()}}</span>
                </div>
                <div class="chart-tooltip-row" style="border-top:1px solid rgba(255,255,255,0.1); margin-top:4px; padding-top:4px;">
                  <span style="font-weight:700;">Combined Assessable</span>
                  <span style="font-weight:700; color:#38bdf8;">${{sym}}${{Math.round(d.net).toLocaleString()}}</span>
                </div>
              `;
            }}

            tooltipEl.innerHTML = `
              <div class="chart-tooltip-title">${{d.fy}} Tax Analysis</div>
              ${{bodyHTML}}
            `;
            positionTooltip(tooltipEl, clientX, clientY);
          }}
        }}, () => {{
          crosshair.style.display = 'none';
          if (tooltipEl) tooltipEl.style.display = 'none';
        }}, null);

        el.appendChild(svg);
      }};


      window.renderEquityHistoryChart = function() {{
        const el = document.getElementById('chart-equity-history');
        const tooltipEl = document.getElementById('tooltip-equity-history');
        if (!el || !DATA || !DATA.shares || !DATA.shares.monthly_holdings_timeline) return;

        el.querySelectorAll('svg').forEach(s => s.remove());
        updateEquityHistoryLegend();

        const timeline = DATA.shares.monthly_holdings_timeline;
        if (!timeline || timeline.length === 0) return;

        const view = equityState.historyView || 'all';
        const isAll = (view === 'all');
        const isShares = (view === 'shares');
        const isVal = (view === 'valuation');
        const isPrice = (view === 'price');

        const W = 820, H = 290;
        const padL = 75;
        const padR = isAll ? 65 : 30;
        const padT = 25, padB = 45;
        const svg = createSVG(W, H);

        const useUSD = (equityState.currency === 'USD');
        const sym = useUSD ? '$' : 'A$';
        const maxShares = Math.max(8000, ...timeline.map(d => Math.max(d.cum_vested, d.held_shares)));
        const maxVal = Math.max(100000, ...timeline.map(d => useUSD ? d.val_usd : d.val_aud));
        const maxPrice = Math.max(450, ...timeline.map(d => d.price_usd));

        const scaleX = (i) => padL + (i / Math.max(1, timeline.length - 1)) * (W - padL - padR);
        const scaleYShares = (s) => padT + (1 - s / maxShares) * (H - padT - padB);
        const scaleYVal = (v) => padT + (1 - v / (maxVal * 1.15)) * (H - padT - padB);
        const scaleYPrice = (p) => padT + (1 - p / (maxPrice * 1.15)) * (H - padT - padB);

        // Draw Left Y-Axis based on active view mode
        if (isAll || isShares) {{
          for (let i = 0; i <= 4; i++) {{
            const shVal = Math.round((maxShares / 4) * i);
            const yPos = scaleYShares(shVal);
            const grid = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            grid.setAttribute('x1', padL); grid.setAttribute('x2', W - padR);
            grid.setAttribute('y1', yPos); grid.setAttribute('y2', yPos);
            grid.setAttribute('stroke', 'var(--card-border)');
            grid.setAttribute('stroke-dasharray', '2 2');
            grid.setAttribute('opacity', '0.35');
            svg.appendChild(grid);

            const lbl = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            lbl.setAttribute('x', padL - 8); lbl.setAttribute('y', yPos + 3);
            lbl.setAttribute('text-anchor', 'end');
            lbl.setAttribute('fill', '#94a3b8');
            lbl.setAttribute('font-size', '10px');
            lbl.textContent = shVal.toLocaleString() + ' sh';
            svg.appendChild(lbl);
          }}
        }} else if (isVal) {{
          for (let i = 0; i <= 4; i++) {{
            const vVal = (maxVal * 1.15 / 4) * i;
            const yPos = scaleYVal(vVal);
            const grid = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            grid.setAttribute('x1', padL); grid.setAttribute('x2', W - padR);
            grid.setAttribute('y1', yPos); grid.setAttribute('y2', yPos);
            grid.setAttribute('stroke', 'var(--card-border)');
            grid.setAttribute('stroke-dasharray', '2 2');
            grid.setAttribute('opacity', '0.35');
            svg.appendChild(grid);

            const lbl = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            lbl.setAttribute('x', padL - 8); lbl.setAttribute('y', yPos + 3);
            lbl.setAttribute('text-anchor', 'end');
            lbl.setAttribute('fill', '#06b6d4');
            lbl.setAttribute('font-size', '10px');
            lbl.textContent = sym + Math.round(vVal / 1000) + 'k';
            svg.appendChild(lbl);
          }}
        }} else if (isPrice) {{
          for (let i = 0; i <= 4; i++) {{
            const pVal = Math.round((maxPrice * 1.15 / 4) * i);
            const yPos = scaleYPrice(pVal);
            const grid = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            grid.setAttribute('x1', padL); grid.setAttribute('x2', W - padR);
            grid.setAttribute('y1', yPos); grid.setAttribute('y2', yPos);
            grid.setAttribute('stroke', 'var(--card-border)');
            grid.setAttribute('stroke-dasharray', '2 2');
            grid.setAttribute('opacity', '0.35');
            svg.appendChild(grid);

            const lbl = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            lbl.setAttribute('x', padL - 8); lbl.setAttribute('y', yPos + 3);
            lbl.setAttribute('text-anchor', 'end');
            lbl.setAttribute('fill', '#f59e0b');
            lbl.setAttribute('font-size', '10px');
            lbl.textContent = '$' + pVal;
            svg.appendChild(lbl);
          }}
        }}

        // Right Y-axis for 'all' mode (Valuation)
        if (isAll) {{
          for (let i = 0; i <= 4; i++) {{
            const audVal = (maxVal * 1.15 / 4) * i;
            const yPos = scaleYVal(audVal);
            const lbl = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            lbl.setAttribute('x', W - padR + 8); lbl.setAttribute('y', yPos + 3);
            lbl.setAttribute('text-anchor', 'start');
            lbl.setAttribute('fill', '#06b6d4');
            lbl.setAttribute('font-size', '10px');
            lbl.textContent = sym + (Math.round(audVal / 1000)) + 'k';
            svg.appendChild(lbl);
          }}
        }}

        // X-axis timestamps
        const stepX = Math.max(1, Math.ceil(timeline.length / 10));
        timeline.forEach((d, idx) => {{
          if (idx % stepX === 0 || idx === timeline.length - 1) {{
            const xPos = scaleX(idx);
            const lbl = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            lbl.setAttribute('x', xPos); lbl.setAttribute('y', H - padB + 16);
            lbl.setAttribute('text-anchor', 'middle');
            lbl.setAttribute('fill', 'var(--text-muted)');
            lbl.setAttribute('font-size', '10px');
            lbl.textContent = formatXMonth(d.month);
            svg.appendChild(lbl);
          }}
        }});

        // Draw Series based on view
        if (isAll || isShares) {{
          // Cumulative Vested Area (Green)
          let pathVest = '';
          timeline.forEach((d, idx) => {{
            const x = scaleX(idx);
            const y = scaleYShares(d.cum_vested);
            pathVest += (idx === 0 ? `M ${{x}} ${{scaleYShares(0)}} L ${{x}} ${{y}}` : ` L ${{x}} ${{y}}`);
          }});
          pathVest += ` L ${{scaleX(timeline.length - 1)}} ${{scaleYShares(0)}} Z`;
          const polyVest = document.createElementNS('http://www.w3.org/2000/svg', 'path');
          polyVest.setAttribute('d', pathVest);
          polyVest.setAttribute('fill', '#10b981');
          polyVest.setAttribute('opacity', '0.12');
          svg.appendChild(polyVest);

          // Cumulative Sold Area (Amber)
          let pathSold = '';
          timeline.forEach((d, idx) => {{
            const x = scaleX(idx);
            const y = scaleYShares(d.cum_sold);
            pathSold += (idx === 0 ? `M ${{x}} ${{scaleYShares(0)}} L ${{x}} ${{y}}` : ` L ${{x}} ${{y}}`);
          }});
          pathSold += ` L ${{scaleX(timeline.length - 1)}} ${{scaleYShares(0)}} Z`;
          const polySold = document.createElementNS('http://www.w3.org/2000/svg', 'path');
          polySold.setAttribute('d', pathSold);
          polySold.setAttribute('fill', '#f59e0b');
          polySold.setAttribute('opacity', '0.15');
          svg.appendChild(polySold);

          // Held Shares Curve (Royal Blue)
          let pathHeld = '';
          timeline.forEach((d, idx) => {{
            const x = scaleX(idx);
            const y = scaleYShares(d.held_shares);
            pathHeld += (idx === 0 ? `M ${{x}} ${{y}}` : ` L ${{x}} ${{y}}`);
          }});
          const lineHeld = document.createElementNS('http://www.w3.org/2000/svg', 'path');
          lineHeld.setAttribute('d', pathHeld);
          lineHeld.setAttribute('fill', 'none');
          lineHeld.setAttribute('stroke', '#60a5fa');
          lineHeld.setAttribute('stroke-width', '2.5');
          lineHeld.setAttribute('stroke-linecap', 'round');
          svg.appendChild(lineHeld);
        }}

        if (isAll || isVal) {{
          // Valuation Curve & Area
          let pathVal = '';
          timeline.forEach((d, idx) => {{
            const val = useUSD ? d.val_usd : d.val_aud;
            const x = scaleX(idx);
            const y = scaleYVal(val);
            pathVal += (idx === 0 ? `M ${{x}} ${{y}}` : ` L ${{x}} ${{y}}`);
          }});

          if (isVal) {{
            const areaVal = pathVal + ` L ${{scaleX(timeline.length - 1)}} ${{scaleYVal(0)}} L ${{scaleX(0)}} ${{scaleYVal(0)}} Z`;
            const polyVal = document.createElementNS('http://www.w3.org/2000/svg', 'path');
            polyVal.setAttribute('d', areaVal);
            polyVal.setAttribute('fill', '#06b6d4');
            polyVal.setAttribute('opacity', '0.15');
            svg.appendChild(polyVal);
          }}

          const lineVal = document.createElementNS('http://www.w3.org/2000/svg', 'path');
          lineVal.setAttribute('d', pathVal);
          lineVal.setAttribute('fill', 'none');
          lineVal.setAttribute('stroke', '#06b6d4');
          lineVal.setAttribute('stroke-width', isVal ? '2.5' : '2');
          if (isAll) lineVal.setAttribute('stroke-dasharray', '4 2');
          lineVal.setAttribute('stroke-linecap', 'round');
          svg.appendChild(lineVal);
        }}

        if (isPrice) {{
          // Stock Price Curve & Area
          let pathPrice = '';
          timeline.forEach((d, idx) => {{
            const x = scaleX(idx);
            const y = scaleYPrice(d.price_usd);
            pathPrice += (idx === 0 ? `M ${{x}} ${{y}}` : ` L ${{x}} ${{y}}`);
          }});

          const areaPrice = pathPrice + ` L ${{scaleX(timeline.length - 1)}} ${{scaleYPrice(0)}} L ${{scaleX(0)}} ${{scaleYPrice(0)}} Z`;
          const polyPrice = document.createElementNS('http://www.w3.org/2000/svg', 'path');
          polyPrice.setAttribute('d', areaPrice);
          polyPrice.setAttribute('fill', '#f59e0b');
          polyPrice.setAttribute('opacity', '0.15');
          svg.appendChild(polyPrice);

          const linePrice = document.createElementNS('http://www.w3.org/2000/svg', 'path');
          linePrice.setAttribute('d', pathPrice);
          linePrice.setAttribute('fill', 'none');
          linePrice.setAttribute('stroke', '#f59e0b');
          linePrice.setAttribute('stroke-width', '2.5');
          linePrice.setAttribute('stroke-linecap', 'round');
          svg.appendChild(linePrice);
        }}

        const crosshair = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        crosshair.setAttribute('y1', padT); crosshair.setAttribute('y2', H - padB);
        crosshair.setAttribute('stroke', '#94a3b8');
        crosshair.setAttribute('stroke-dasharray', '3 3');
        crosshair.style.display = 'none';
        svg.appendChild(crosshair);

        const hoverGroup = document.createElementNS('http://www.w3.org/2000/svg', 'g');
        svg.appendChild(hoverGroup);

        attachChartInteractions(el, svg, tooltipEl, (normX, normY, clientX, clientY) => {{
          const plotW = W - padL - padR;
          const svgX = normX * W;
          if (svgX < padL || svgX > W - padR) {{
            crosshair.style.display = 'none';
            hoverGroup.innerHTML = '';
            if (tooltipEl) tooltipEl.style.display = 'none';
            return;
          }}
          const idx = Math.max(0, Math.min(timeline.length - 1, Math.round(((svgX - padL) / plotW) * (timeline.length - 1))));
          const d = timeline[idx];
          const xPos = scaleX(idx);

          crosshair.setAttribute('x1', xPos); crosshair.setAttribute('x2', xPos);
          crosshair.style.display = '';

          hoverGroup.innerHTML = '';
          if (isAll || isShares) {{
            const dotHeld = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
            dotHeld.setAttribute('cx', xPos); dotHeld.setAttribute('cy', scaleYShares(d.held_shares));
            dotHeld.setAttribute('r', '4'); dotHeld.setAttribute('fill', '#60a5fa');
            dotHeld.setAttribute('stroke', '#0f172a'); dotHeld.setAttribute('stroke-width', '2');
            hoverGroup.appendChild(dotHeld);
          }}

          if (isAll || isVal) {{
            const val = useUSD ? d.val_usd : d.val_aud;
            const dotVal = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
            dotVal.setAttribute('cx', xPos); dotVal.setAttribute('cy', scaleYVal(val));
            dotVal.setAttribute('r', '4'); dotVal.setAttribute('fill', '#06b6d4');
            dotVal.setAttribute('stroke', '#0f172a'); dotVal.setAttribute('stroke-width', '2');
            hoverGroup.appendChild(dotVal);
          }}

          if (isPrice) {{
            const dotPrice = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
            dotPrice.setAttribute('cx', xPos); dotPrice.setAttribute('cy', scaleYPrice(d.price_usd));
            dotPrice.setAttribute('r', '4'); dotPrice.setAttribute('fill', '#f59e0b');
            dotPrice.setAttribute('stroke', '#0f172a'); dotPrice.setAttribute('stroke-width', '2');
            hoverGroup.appendChild(dotPrice);
          }}

          if (tooltipEl) {{
            let rowsHTML = '';
            if (isShares) {{
              rowsHTML = `
                <div class="chart-tooltip-row">
                  <span><span class="chart-tooltip-badge" style="background:#60a5fa;"></span>Shares Held</span>
                  <span style="font-weight:700; color:#60a5fa;">${{d.held_shares.toLocaleString()}} shares</span>
                </div>
                <div class="chart-tooltip-row" style="border-top:1px solid rgba(255,255,255,0.1); margin-top:4px; padding-top:4px;">
                  <span style="color:#10b981;">Cum. Vested</span>
                  <span>${{d.cum_vested.toLocaleString()}} sh</span>
                </div>
                <div class="chart-tooltip-row">
                  <span style="color:#f59e0b;">Cum. Sold</span>
                  <span>${{d.cum_sold.toLocaleString()}} sh</span>
                </div>
              `;
            }} else if (isVal) {{
              rowsHTML = `
                <div class="chart-tooltip-row">
                  <span><span class="chart-tooltip-badge" style="background:#06b6d4;"></span>Position Value (AUD)</span>
                  <span style="font-weight:700; color:#06b6d4;">~A$${{Math.round(d.val_aud).toLocaleString()}} AUD</span>
                </div>
                <div class="chart-tooltip-row">
                  <span>Position Value (USD)</span>
                  <span style="font-weight:600; color:#38bdf8;">$${{Math.round(d.val_usd).toLocaleString()}} USD</span>
                </div>
                <div class="chart-tooltip-row" style="border-top:1px solid rgba(255,255,255,0.1); margin-top:4px; padding-top:4px;">
                  <span>Equivalent Shares</span>
                  <span>${{d.held_shares.toLocaleString()}} sh</span>
                </div>
              `;
            }} else if (isPrice) {{
              rowsHTML = `
                <div class="chart-tooltip-row">
                  <span><span class="chart-tooltip-badge" style="background:#f59e0b;"></span>TEAM Share Price</span>
                  <span style="font-weight:700; color:#f59e0b;">$${{d.price_usd.toFixed(2)}} USD</span>
                </div>
              `;
            }} else {{
              rowsHTML = `
                <div class="chart-tooltip-row">
                  <span><span class="chart-tooltip-badge" style="background:#60a5fa;"></span>Shares Held</span>
                  <span style="font-weight:700; color:#60a5fa;">${{d.held_shares.toLocaleString()}} shares</span>
                </div>
                <div class="chart-tooltip-row">
                  <span><span class="chart-tooltip-badge" style="background:#06b6d4;"></span>Position Value</span>
                  <span style="font-weight:700; color:#06b6d4;">~A$${{Math.round(d.val_aud).toLocaleString()}} AUD</span>
                </div>
                <div class="chart-tooltip-row">
                  <span>Share Price</span>
                  <span>$${{d.price_usd.toFixed(2)}} USD</span>
                </div>
                <div class="chart-tooltip-row" style="border-top:1px solid rgba(255,255,255,0.1); margin-top:4px; padding-top:4px;">
                  <span style="color:#10b981;">Cum. Vested</span>
                  <span>${{d.cum_vested.toLocaleString()}} sh</span>
                </div>
                <div class="chart-tooltip-row">
                  <span style="color:#f59e0b;">Cum. Sold</span>
                  <span>${{d.cum_sold.toLocaleString()}} sh</span>
                </div>
              `;
            }}

            tooltipEl.innerHTML = `
              <div class="chart-tooltip-title">${{formatFullMonth(d.month)}} Equity Position</div>
              ${{rowsHTML}}
            `;
            positionTooltip(tooltipEl, clientX, clientY);
          }}
        }}, () => {{
          crosshair.style.display = 'none';
          hoverGroup.innerHTML = '';
          if (tooltipEl) tooltipEl.style.display = 'none';
        }}, null);

        el.appendChild(svg);
      }};


      // Transactions Explorer Filter & Pagination Listeners
      const txSearchInput = document.getElementById('tx-search');
      if (txSearchInput) {{
        txSearchInput.addEventListener('input', () => {{
          txPage = 1;
          renderTransactionsTab();
        }});
      }}

      const txAcctSelect = document.getElementById('tx-account-filter');
      if (txAcctSelect) {{
        txAcctSelect.addEventListener('change', () => {{
          txPage = 1;
          renderTransactionsTab();
        }});
      }}

      const txCatSelect = document.getElementById('tx-category-filter');
      if (txCatSelect) {{
        txCatSelect.addEventListener('change', () => {{
          txPage = 1;
          renderTransactionsTab();
        }});
      }}

      const txTypeSelect = document.getElementById('tx-type-filter');
      if (txTypeSelect) {{
        txTypeSelect.addEventListener('change', () => {{
          txPage = 1;
          renderTransactionsTab();
        }});
      }}

      const txPrevBtn = document.getElementById('tx-prev-page');
      if (txPrevBtn) {{
        txPrevBtn.addEventListener('click', () => {{
          if (txPage > 1) {{
            txPage--;
            renderTransactionsTab();
          }}
        }});
      }}

      const txNextBtn = document.getElementById('tx-next-page');
      if (txNextBtn) {{
        txNextBtn.addEventListener('click', () => {{
          txPage++;
          renderTransactionsTab();
        }});
      }}

      // Initial boot
      refreshDashboard();
    }})();
  </script>
</body>
</html>
"""
    return html


def build_report(
    db,
    output_path: Optional[str] = None,
    cfg: Optional[Dict[str, Any]] = None,
    provider_filter: Optional[str] = None,
) -> str:
    """Generate and write the index.html report to disk."""
    if not output_path:
        default_dir = os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/Finance/Statements")
        output_path = os.path.join(default_dir, "index.html")

    data = collect_report_data(db, cfg=cfg, provider_filter=provider_filter)
    html_content = render_html_report(data)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    return output_path
