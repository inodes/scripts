"""AGL bill parsing: the one implementation, shared by the filing pipeline and ~/repos/tesla.

Ported verbatim from ~/repos/tesla (Tools/lib/invoice_common.py, AGLInvoiceExtractor) so the two
repos stop carrying separate copies. Tesla now imports this module; keep changes here.

Pure text parsing: give it a bill's extracted text and path, get fields back (account, issue/due
date, bill period, totals, usage, rates). No filing and no I/O.
"""

import datetime
import os
import re
from typing import Any, Dict, Optional


class AGLBillParser:
    """AGL electricity / gas statements and tax invoices (2011 to date, several layouts)."""

    name = "agl"
    display_name = "AGL Energy"

    @staticmethod
    def _parse_flexible_date(s: str) -> Optional[str]:
        s = s.strip()
        for fmt in (
            "%Y/%m/%d", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y",
            "%d %b %Y", "%d %B %Y", "%d %b %y",
            "%d/%m/%Y %I:%M:%S %p", "%d/%m/%Y %I:%M %p",
            "%Y/%m/%d %I:%M:%S %p", "%Y/%m/%d %I:%M %p",
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
        ):
            try:
                return datetime.datetime.strptime(s, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return None

    def detect(self, text: str, pdf_path: str) -> bool:
        t_low = text.lower()
        p_low = os.path.basename(pdf_path).lower()
        if "agl.com.au" in t_low or "agl sales" in t_low or "agl electricity" in t_low:
            return True
        if "here's your monthly electricity bill" in t_low or "national metering identifier (nmi)" in t_low:
            return True
        if ("electricity bill" in t_low or "gas bill" in t_low) and ("tax invoice" in t_low or "statement" in t_low):
            return True
        if "statement_electricity_" in p_low or "statement_gas_" in p_low:
            return True
        return False

    def extract(self, text: str, pdf_path: str) -> Dict[str, Any]:
        fname = os.path.basename(pdf_path)
        data: Dict[str, Any] = {
            "provider": self.name,
            "provider_display": self.display_name,
            "source_file": fname,
            "source_path": pdf_path,
            "account_number": None,
            "invoice_number": None,
            "nmi": None,
            "issue_date": None,
            "due_date": None,
            "period_start": None,
            "period_end": None,
            "service_type": "electricity",
            "total_amount": None,
            "gst_amount": None,
            "usage_kwh": None,
            "solar_export_kwh": None,
            "peak_demand_kw": None,
        }

 # 1. Service Type
        t_low = text.lower()
        if "gas bill" in t_low or "statement_gas_" in fname.lower() or "natural gas" in t_low:
            data["service_type"] = "gas"
        else:
            data["service_type"] = "electricity"

 # 2. Account Number
 # BUG-126 (2026-09-21): older AGL statements (2020-2022) render this as two separate
 # blocks - a run of labels ("Name: Account number: Supply address:") followed by a run
 # of values in the same order ("<Name> <Surname> <account number> <street address>...") -
 # rather than a label immediately followed by its own value. The old strict-adjacency
 # regex never matched that layout at all. Widened to allow up to 120 characters of
 # intervening text (other labels, newlines) between the label and the actual account
 # number, which AGL formats consistently as three digit groups (4-3-3, e.g.
 # "1234 567 890") - that specific shape keeps this from false-matching an unrelated
 # number (like the NMI) that happens to fall within the window.
        acc_m = re.search(r"Account\s*number\s*:?[\s\S]{0,120}?(\d{4}\s?\d{3}\s?\d{3})\b", text, re.IGNORECASE)
        if acc_m:
            data["account_number"] = re.sub(r"\s+", "", acc_m.group(1))

 # 3. NMI (National Metering Identifier)
        nmi_m = re.search(r"National\s*Metering\s*Identifier\s*\(NMI\)[\s:]*([0-9]{10,11})", text, re.IGNORECASE)
        if nmi_m:
            data["nmi"] = nmi_m.group(1)

 # 4. Issue Date / Bill Date
        dt_m = re.search(r"(?:Issue\s*date|Date\s*of\s*issue|Bill\s*date|Statement\s*date)[\s:]*(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})", text, re.IGNORECASE)
        if dt_m:
            data["issue_date"] = self._parse_flexible_date(dt_m.group(1))

 # Fallback date from existing filename: e.g. Statement_electricity_20260728.pdf
        if not data["issue_date"]:
            fn_dt_m = re.search(r"(\d{4})(\d{2})(\d{2})", fname)
            if fn_dt_m:
                try:
                    data["issue_date"] = f"{fn_dt_m.group(1)}-{fn_dt_m.group(2)}-{fn_dt_m.group(3)}"
                except Exception:
                    pass

 # 5. Due Date / Direct Debit Date
        due_m = re.search(r"(?:Direct\s*Debit\s*date|Due\s*date|Payment\s*due)[\s:]*(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4})", text, re.IGNORECASE)
        if due_m:
            data["due_date"] = self._parse_flexible_date(due_m.group(1))

 # 6b. Bill period (REQ-112, extended REQ-123) - e.g. "Bill period: 25 Jul
 # 2026 to 24 Aug 2026 (31 days)". This is the real billing window the rate
 # figures below apply to - more useful than issue_date for pinning down
 # when a rate change actually took effect. Older (pre-2015) AGL bills use
 # "Supply period 18 Oct 2013 to 20 Jan 2014" instead of "Bill period:" -
 # same date format, different label - so both are matched here.
        period_m = re.search(
            r"(?:Bill|Supply)\s*period\s*:?\s*(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})\s+to\s+(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})",
            text, re.IGNORECASE,
        )
        if period_m:
            data["period_start"] = self._parse_flexible_date(period_m.group(1))
            data["period_end"] = self._parse_flexible_date(period_m.group(2))

 # 6. Total Amount
        amt_m = re.search(r"(?:Direct\s*Debit\s*amount|Total\s*amount\s*due|Total\s*charges|Amount\s*due)[\s:]*\$([0-9,]+\.\d{2})", text, re.IGNORECASE)
        if amt_m:
            try:
                data["total_amount"] = float(amt_m.group(1).replace(",", ""))
            except ValueError:
                pass
        else:
 # BUG-126: a "Payment not required. Your credit will be carried forward" month has no
 # "Direct Debit amount"/"Total amount due" line at all - the only real dollar figure on
 # the bill is "Account balance $X.XXcr". Capture it as a negative amount (credit, nothing
 # owed) rather than leaving total_amount null - null previously made these bills
 # invisible to any downstream cost reconciliation instead of correctly showing $0 owed.
            bal_m = re.search(r"Account\s*balance\s*\$([0-9,]+\.\d{2})(cr)?", text, re.IGNORECASE)
            if bal_m:
                try:
                    val = float(bal_m.group(1).replace(",", ""))
                    data["total_amount"] = -val if bal_m.group(2) else val
                except ValueError:
                    pass

 # 7. GST Amount
        gst_m = re.search(r"(?:Includes\s*GST\s*of|Total\s*GST|GST\s*includes)[\s:]*\$([0-9,]+\.\d{2})", text, re.IGNORECASE)
        if gst_m:
            try:
                data["gst_amount"] = float(gst_m.group(1).replace(",", ""))
            except ValueError:
                pass

 # 8. Invoice / Statement Number
        inv_m = re.search(r"(?:Tax\s*Invoice|Statement\s*No|Invoice\s*number)[\s:]*([0-9]{8,12})", text, re.IGNORECASE)
        if inv_m:
            data["invoice_number"] = inv_m.group(1)
        elif fname.replace(".pdf", "").replace("-1", "").isdigit() and len(fname.replace(".pdf", "").replace("-1", "")) >= 8:
            data["invoice_number"] = fname.replace(".pdf", "").replace("-1", "")

        return data

    _RATE_ROW_RE = re.compile(r"([\d,]+\.?\d*)\s*(kWh|kW|days)\s+\$([\d.]+)\s+\$[\d,.]+([^\n]*)")

 # REQ-126: AGL changed this table's own column ORDER at some point
 # between mid-2022 and mid-2024 (confirmed real, three layouts seen:
 # pre-~2022 flat-rate bills and ~2022-2024 TOU bills both print
 # "$<amount><units><type> $<price><label>" - amount-first, no space
 # before the unit or the label; ~2024-on bills print
 # "<units> <type> $<price> $<amount><label>" - units-first, which is
 # what _RATE_ROW_RE above already matches). This is the amount-first
 # shape's own row pattern - same 4 groups (units, type, price, label)
 # so callers can treat rows from both regexes identically.
    _OLD_ROW_RE = re.compile(r"\$[\d,]+\.\d{2}(?:cr)?([\d,]+\.?\d*)\s?(kWh|kW|days)\s*\$([\d.]+)([^\n]*)")

    def _find_rate_rows(self, block_text: str):
        """Combined (units, type, price, label) rows from BOTH real
        column orderings AGL has used (see _OLD_ROW_RE above) - every
        caller that used to call _RATE_ROW_RE.findall() directly should
        call this instead so both eras of bill are covered."""
        return self._RATE_ROW_RE.findall(block_text) + self._OLD_ROW_RE.findall(block_text)

    def _find_rate_rows_with_pos(self, block_text: str):
        """Like _find_rate_rows(), but keeps each match's (start, end)
        span AND its own same-line label capture (group 4), sorted by
        position - (start, end, units, type, price, same_line_label)
        tuples. REQ-152: a row's own tier LABEL (Peak/Off-peak/Shoulder)
        is not reliably on the same line as its amount - a real bill
        wraps a Peak or Shoulder row's own multi-line time-window
        description (e.g. "2pm-8pm (Mon-Fri excl. public\nholidays)")
        onto several lines, pushing the actual "Peak"/"Shoulder" label
        text onto its OWN following line, so this row's regex-captured
        group 4 (same_line_label - everything up to the first newline
        after the amount) only gets a PARTIAL, keyword-less fragment of
        the window text. Only Off-peak's short "All other times" window,
        and the oldest flat bills' plain "General Usage"/"Controlled
        Load 2" labels, happen to fit on one line - confirmed real on a
        2023-05-25 bill during REQ-152's own regression check (see
        AGENTS.local.md). _extract_block_rates() combines this
        same_line_label with the text gap up to the NEXT row's start to
        cover both cases: same-line labels are already complete in
        group 4 (the gap after them is empty/irrelevant); wrapped labels
        are completed by that trailing gap text."""
        matches = list(self._RATE_ROW_RE.finditer(block_text)) + list(self._OLD_ROW_RE.finditer(block_text))
        rows = [(m.start(), m.end(), m.group(1), m.group(2), m.group(3), m.group(4)) for m in matches]
        rows.sort(key=lambda r: r[0])
        return rows

    _PLAN_CHANGE_RE = re.compile(
        r"Energy\s*plan\s*&\s*price\s*change\s*-\s*"
        r"(\d{1,2}\s+[A-Za-z]{3}\s+\d{2})\s+to\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{2})\s*\((\d+)\s*days?\)",
        re.IGNORECASE,
    )

    def _extract_block_rates(self, block_text: str) -> Dict[str, Any]:
        """One usage/supply sub-table's worth of rates - see extract_rates()
        for the row-order assumption this relies on. Returns None values for
        anything not found in THIS block (e.g. a demand-only block, see
        extract_rates(), has no kWh/days rows at all).

        REQ-152: classifies each kWh row by its own LABEL text (Peak/
        Off-peak/Shoulder/General Usage/Controlled load) rather than
        assuming a fixed row count/order - the old ">=3 rows" assumption
        silently found nothing at all on a genuine 2-tier Peak/Off-peak-
        only bill (no Shoulder line at all - the real shape of AGL's own
        2026-09-07 plan change), which would have meant a future bill
        under that new structure never got its rates captured by
        sync_agl_rates.py.

        The label text is NOT reliably on the same line as its row's own
        amount - a real bill wraps a long multi-line time-window
        description (e.g. Peak's "2pm-8pm (Mon-Fri excl. public\n
        holidays)") between the amount and the tier label, pushing the
        label onto its own following line; only Off-peak's short "All
        other times" window happens to fit on one line, which is why a
        same-line capture only ever seemed to work for that one tier
        (confirmed on a real 2023-05-25 bill during REQ-152's own
        regression check - see AGENTS.local.md). So this uses
        _find_rate_rows_with_pos() and takes each row's label from the
        text SPAN between that row's own end and the next row's start
        (whatever tier keyword appears anywhere in that span), not a
        same-line regex group.

        A kWh row whose span names none of Peak/Shoulder/Off-peak is
        "other" (Controlled Load rows land here and are correctly
        ignored) UNLESS it says "general usage" (the oldest pre-TOU
        flat bills, REQ-126). A period with only ONE of Peak/Shoulder/
        Off-peak actually printed (no second tier to differentiate
        against) is a flat rate for that period, whatever single tier
        label AGL's own bill happened to print on it - confirmed real on
        this account's own 2014-2015 bills, which print a lone "Peak"
        label despite genuine TOU differentiation not starting until
        2022-04-06 (AGENTS.local.md REQ-151)."""
        rows = self._find_rate_rows_with_pos(block_text)
        out: Dict[str, Any] = {
            "usage_rate_per_kwh": None,
            "rate_tou_mismatch": False,
            "supply_charge_per_day": None,
            "peak_rate_per_kwh": None,
            "shoulder_rate_per_kwh": None,
            "offpeak_rate_per_kwh": None,
        }

        by_tier = {"peak": [], "shoulder": [], "offpeak": [], "other": []}
        for idx, (start, end, units, rtype, price, same_line_label) in enumerate(rows):
            if rtype != "kWh":
                continue
            next_start = rows[idx + 1][0] if idx + 1 < len(rows) else len(block_text)
            # Same-line label (group 4, e.g. plain "General Usage") plus
            # whatever text sits between this row's end and the next row's
            # start (where a WRAPPED multi-line Peak/Shoulder window
            # description pushes the real tier keyword) - see
            # _find_rate_rows_with_pos()'s docstring.
            label = (same_line_label + block_text[end:next_start]).lower()
            if "shoulder" in label:
                by_tier["shoulder"].append(price)
            elif "off peak" in label or "off-peak" in label or "offpeak" in label:
                by_tier["offpeak"].append((price, label))
            elif "peak" in label:
                by_tier["peak"].append((price, label))
            else:
                by_tier["other"].append((price, label))

        tiers_found = [k for k in ("peak", "shoulder", "offpeak") if by_tier[k]]
        if len(tiers_found) >= 2:
            def _price(tier):
                v = by_tier[tier][0]
                return float(v[0] if isinstance(v, tuple) else v)
            peak = _price("peak") if by_tier["peak"] else None
            shoulder = _price("shoulder") if by_tier["shoulder"] else None
            offpeak = _price("offpeak") if by_tier["offpeak"] else None
            out["peak_rate_per_kwh"] = peak
            out["shoulder_rate_per_kwh"] = shoulder
            out["offpeak_rate_per_kwh"] = offpeak
            present = [v for v in (peak, shoulder, offpeak) if v is not None]
            out["rate_tou_mismatch"] = len(set(present)) > 1
 # A flat usage_rate_per_kwh is only ever valid here when every
 # tier this bill shows happens to be numerically equal (this
 # account's own real history 2022-04-25 through 2026-09-06) -
 # otherwise there's no single correct flat number to report, so
 # this is left None rather than silently picking one tier
 # (matches the convention already established for the real
 # 2026-09-07 "tou" period in agl_tariff_rates.json).
            out["usage_rate_per_kwh"] = present[0] if len(set(present)) == 1 else None
        elif len(tiers_found) == 1:
 # Only one of Peak/Off-peak/Shoulder actually printed on this
 # bill - nothing to differentiate it against, so it's a flat
 # rate for this period regardless of which single tier label
 # AGL's own bill happened to print (see docstring above).
            only = by_tier[tiers_found[0]][0]
            out["usage_rate_per_kwh"] = float(only[0] if isinstance(only, tuple) else only)
        elif by_tier["other"]:
 # REQ-126: the oldest pre-TOU bills have no Peak/Off-peak/
 # Shoulder split at all - just a single flat "General Usage"
 # row. Fall back to that row by its own label rather than
 # guessing which "other" row is the real usage rate (a
 # Controlled Load row is also kWh and must NOT be mistaken
 # for it).
            general_rows = [v for v in by_tier["other"] if "general usage" in v[1]]
            if general_rows:
                out["usage_rate_per_kwh"] = float(general_rows[0][0])
        days_rows = [r for r in rows if r[3] == "days"]
        if days_rows:
            out["supply_charge_per_day"] = float(days_rows[0][4])
        return out

    def extract_rates(self, text: str) -> Dict[str, Any]:
        """REQ-112: pulls the actual dated TARIFF RATES off an AGL bill's
        own "New charges and credits" table - the CSV usage-export never
        carries this, only the bill PDF does (a household's dollars-per-
        kWh, feed-in tariff, demand rate and daily supply charge are
        plan/pricing facts, not meter readings). Every regex here was
        built and verified against real bills spanning multiple real rate
        changes (see AGENTS.local.md REQ-031/REQ-112) before being
        trusted, per REQ-031's own hard-won lesson about narrow/guessed
        regexes silently missing a real line on the bill.

        A bill whose OWN billing period straddles a real rate change
        prints TWO full usage/supply sub-tables (each its own Peak/
        Off-peak/Shoulder/Supply-charge set), separated by an
        "Energy plan & price change - <date> to <date> (<N> days)"
        marker that names the date range the FOLLOWING sub-table covers -
        confirmed real on the actual 2026-06-30 transition bill, which
        also isolates its Demand line into its own separate 1-day
        sub-block between the two usage tables. Blindly taking "the
        first 3 kWh rows" on a bill like that would silently return the
        SUPERSEDED rate as if it were this bill's own - exactly the kind
        of silent-miss REQ-031 already warned about once, so this is
        handled explicitly rather than assumed away.

        Returns:
          - blocks: list of {date_from, date_to (YYYY-MM-DD strings or
            None if unknown), usage_rate_per_kwh, rate_tou_mismatch,
            supply_charge_per_day, solar_feed_in_tier1_rate_per_kwh,
            solar_feed_in_tier2_rate_per_kwh} - normally ONE entry (the
            whole bill), two only for a genuine straddling bill. A block
            with no usage/supply rows found (nothing to report) is
            omitted rather than returned empty.
          - demand_rate_per_kw_day / demand_confirmed: bool - kept
            OUTSIDE the per-block split deliberately: the one real
            straddling bill seen puts its Demand line in its own
            isolated sub-block whose date range doesn't line up with
            either usage block, so this repo doesn't guess which side of
            a split it belongs to - it's reported once for the bill and
            `demand_period_ambiguous` is set True when a split was
            detected, so a caller can decide whether to trust it as-is.
          - warnings: list of str - anything found but not confidently
            attributable (e.g. a solar-tier occurrence count that didn't
            match the number of usage blocks), surfaced rather than
            silently guessed.
        """
        out: Dict[str, Any] = {
            "blocks": [],
            "demand_rate_per_kw_day": None,
            "demand_confirmed": False,
            "demand_period_ambiguous": False,
            "warnings": [],
        }

 # Bound the search to the actual usage/supply table - from its own
 # header down to "Total charges" - so a coincidental "$X $Y" elsewhere
 # on the bill (e.g. in "Previous balance and payments") can't be
 # mistaken for a rate row.
        table_m = re.search(
            r"Usage\s*and\s*supply\s*charges.*?Total\s*charges\s*\+",
            text, re.IGNORECASE | re.DOTALL,
        )
        table_text = table_m.group(0) if table_m else ""

 # Demand is searched GLOBALLY (not per-block) - see docstring above
 # for why: its own sub-block boundary doesn't reliably line up with
 # either usage rate block on the one real straddling bill checked.
        demand_m = self._RATE_ROW_RE.search(
            "\n".join(ln for ln in table_text.splitlines() if " kW " in ln or ln.strip().endswith("kW"))
            or table_text
        )
 # The line-filter above is a best-effort narrowing; fall back to a
 # direct scan for the one row shaped "<n> kW $<price> $<amount>"
 # (never "kWh") if it didn't isolate anything.
        for r in self._find_rate_rows(table_text):
            if r[1] == "kW":
                out["demand_rate_per_kw_day"] = float(r[2])
                out["demand_confirmed"] = True
                break

        markers = list(self._PLAN_CHANGE_RE.finditer(table_text))
        raw_blocks = []
        if not markers:
            raw_blocks.append((table_text, None, None))
        else:
            out["demand_period_ambiguous"] = True
            raw_blocks.append((table_text[:markers[0].start()], None, None))
            for i, m in enumerate(markers):
                seg_end = markers[i + 1].start() if i + 1 < len(markers) else len(table_text)
                seg_text = table_text[m.end():seg_end]
                raw_blocks.append((seg_text, m.group(1), m.group(2)))

        usage_blocks = []
        for seg_text, date_from, date_to in raw_blocks:
            block = self._extract_block_rates(seg_text)
            if block["usage_rate_per_kwh"] is None and block["supply_charge_per_day"] is None:
                continue  # e.g. the isolated demand-only sub-block - nothing to report
            block["date_from"] = self._parse_flexible_date(date_from) if date_from else None
            block["date_to"] = self._parse_flexible_date(date_to) if date_to else None
            block["solar_feed_in_tier1_rate_per_kwh"] = None
            block["solar_feed_in_tier2_rate_per_kwh"] = None
            usage_blocks.append(block)

 # Solar credit rows can ALSO repeat once per rate change on a
 # straddling bill (confirmed real: two "Standard feed-in tariff*"
 # lines at different rates on the one split bill checked) - findall,
 # not search, and map matches to blocks IN DOCUMENT ORDER, which is
 # also chronological order on every real bill checked (old rate
 # listed before new rate).
        tier1_matches = [float(x) for x in re.findall(
            r"Standard\s*feed-in\s*tariff\*?\s*At\s*all\s*times\s*[\d,.]+\s*kWh\s+\$([\d.]+)",
            text, re.IGNORECASE,
        )]
 # REQ-126: pre-2023 bills print the credit row amount-first with no
 # "At all times" phrase at all - e.g. "$231.68cr1103.235kWh $0.21
 # Standard Feed-in Tariff*" (see _OLD_ROW_RE's docstring for the same
 # amount-first pattern in the usage/supply table above). Only
 # searched when the new-layout pattern found nothing, since a bill
 # can only be one layout or the other, never both.
        if not tier1_matches:
            tier1_matches = [float(x) for x in re.findall(
                r"\$[\d,]+\.\d{2}cr[\d,]+\.?\d*kWh\s*\$([\d.]+)(?:Standard\s*)?Feed-in\s*Tariff",
                text, re.IGNORECASE,
            )]
        tier2_matches = [float(x) for x in re.findall(
            r"\bNext\*?\s*At\s*all\s*times\s*[\d,.]+\s*kWh\s+\$([\d.]+)",
            text, re.IGNORECASE,
        )]

        def _assign(matches, field_name):
            if not matches:
                return
            if len(matches) == 1:
                for b in usage_blocks:
                    b[field_name] = matches[0]
            elif len(matches) == len(usage_blocks):
                for b, v in zip(usage_blocks, matches):
                    b[field_name] = v
            else:
                out["warnings"].append(
                    f"{field_name}: found {len(matches)} occurrence(s) but {len(usage_blocks)} "
                    f"usage block(s) - couldn't confidently attribute, left unset (manual check needed)."
                )

        _assign(tier1_matches, "solar_feed_in_tier1_rate_per_kwh")
        _assign(tier2_matches, "solar_feed_in_tier2_rate_per_kwh")

        out["blocks"] = usage_blocks
        return out

    @staticmethod
    def standard_name(data: Dict[str, Any], original_path: str) -> str:
        """AGL_Statement_<service>_<ISO issue date>.pdf: one date only, like every other provider.
        Falls back to the period end when no issue date was read."""
        svc = data.get("service_type") or "electricity"
        d = data.get("issue_date") or data.get("period_end")
        if d:
            return f"AGL_Statement_{svc}_{d}.pdf"
        return os.path.basename(original_path)
