"""Base Pluggable PDF Provider Class."""
import datetime
import os
import re
from typing import Any, Dict, List, Optional
from ..money import D, fmt

MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}


_CUSTOM_RULES_CACHE = None
_CUSTOM_RULES_MTIMES: Dict[str, float] = {}


def load_custom_category_rules() -> Dict[str, str]:
    """Load user-defined keyword-to-category rules from active iCloud or local config.
    Checks:
    1. Active config.json (e.g. ~/iCloud/Finance/config.json) under 'category_rules'
    2. category_rules.json (e.g. ~/iCloud/Finance/category_rules.json)
    """
    global _CUSTOM_RULES_CACHE, _CUSTOM_RULES_MTIMES
    try:
        from ..config import path as get_cfg_path, get_category_rules_path
        cfg_file = get_cfg_path()
        rules_file = get_category_rules_path()
    except Exception:
        from .. import paths as _paths
        cfg_file = _paths.config_path()
        rules_file = _paths.under("finance", "category_rules.json")

    mtimes: Dict[str, float] = {}
    if cfg_file and os.path.isfile(cfg_file):
        try:
            mtimes[cfg_file] = os.path.getmtime(cfg_file)
        except OSError:
            pass
    if rules_file and os.path.isfile(rules_file):
        try:
            mtimes[rules_file] = os.path.getmtime(rules_file)
        except OSError:
            pass

    if _CUSTOM_RULES_CACHE is not None and mtimes and mtimes == _CUSTOM_RULES_MTIMES:
        return _CUSTOM_RULES_CACHE

    combined_rules: Dict[str, str] = {}
    import json

    if cfg_file and os.path.isfile(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                cdata = json.load(f)
                c_rules = cdata.get("category_rules", {})
                if isinstance(c_rules, dict):
                    combined_rules.update(c_rules)
        except Exception:
            pass

    if rules_file and os.path.isfile(rules_file):
        try:
            with open(rules_file, "r", encoding="utf-8") as f:
                rdata = json.load(f)
                f_rules = rdata.get("rules", rdata) if isinstance(rdata, dict) else {}
                if isinstance(f_rules, dict):
                    combined_rules.update(f_rules)
        except Exception:
            pass

    _CUSTOM_RULES_CACHE = {
        str(k).upper(): str(v)
        for k, v in combined_rules.items()
        if isinstance(k, str) and isinstance(v, str)
    }
    _CUSTOM_RULES_MTIMES = mtimes
    return _CUSTOM_RULES_CACHE


def _renovation_window():
    """Optional (start, end) ISO dates from config.json -> personal.renovation_window."""
    try:
        from ..config import load as _load_cfg
        w = (_load_cfg().get("personal", {}) or {}).get("renovation_window")
        if isinstance(w, (list, tuple)) and len(w) == 2:
            return str(w[0]), str(w[1])
    except Exception:
        pass
    return None


def classify_transaction(description: str, tx_date: str = "") -> str:
    """Automatic merchant/transaction category classifier with expanded expense taxonomy."""
    d = (description or "").upper()
    dt = tx_date or ""

    # 0. User-defined custom overrides from category_rules.json
    for pattern, custom_cat in load_custom_category_rules().items():
        if pattern in d:
            return custom_cat

    # 1. Bank Fees & Loan Package Fees
    if any(k in d for k in [
        "ANNUAL HOME LOAN PACKAGE FEE", "PACKAGE FEE", "MONTHLY FEE", "CARD FEE", "LATE FEE",
        "ANNUAL FEE", "ACCOUNT FEE", "NON-HSBC ATM"
    ]):
        return "Bank Fees"

    # 2. Mortgage & Loans
    if any(k in d for k in [
        "LOAN PAYMENT", "HOME LOAN", "PEXA", "SETTL.PROCEEDS", "CLOSE A/C",
        "HOME SHORTFALL",
        "REDUCE LIMIT", "INCREASE LIMIT"
    ]):
        return "Mortgage & Loans"

    # (Rent / employer-specific patterns are user rules in config.json -> category_rules)
    # 3. Interest
    if any(k in d for k in ["DEBIT INTEREST", "CREDIT INTEREST", "INTEREST CHARGED", "INTEREST CREDIT"]):
        return "Interest"

    # 4. Credit Card Payment
    if any(k in d for k in [
        "HSBC CREDITCARD", "HSBC CREDIT CARD", "HSBC BANK PAYMENT", "DIRECT DEBIT PAYMENT",
        "PAYMENT RECEIVED, THANK YOU", "CARD PAYMENT"
    ]):
        return "Credit Card Payment"

    # 5. Cash Withdrawals & ATMs
    if any(k in d for k in ["WDL ATM", "WITHDRAWAL AT ATM", "CASH W/D", "ATM W/D", "CASH WITHDRAWAL", "ATM CASHCARD"]):
        # Optional renovation cash window, configured privately in config.json -> personal.renovation_window
        dt_str = dt.isoformat() if hasattr(dt, "isoformat") else str(dt or "")
        win = _renovation_window()
        if dt_str and win and (win[0] <= dt_str <= win[1]):
            return "Home & Renovation"
        return "Cash & ATM"

    # 6. Specific Income & Inflow Sources
    if any(k in d for k in ["WISE AUSTRALIA", "TRANSFERWISE", "WISE PAYMENTS", "WISE.COM"]):
        return "Income: Currency Transfers (Wise)"

    if any(k in d for k in ["OZFOREX", "OFX.COM"]):
        return "Income: Currency Transfers (OFX)"

    if any(k in d for k in ["DIVIDEND", "COMPUTERSHARE", "LINK MARKET", "SHARE DIVIDEND"]):
        return "Income: Investments & Dividends"

    if "NDIS" in d:
        return "Income: Government & NDIS"

    if any(k in d for k in ["CREDIT INTEREST", "INTEREST CREDIT"]):
        return "Income: Bank Interest"

    # 7. Internal Transfers & Bank Remittances
    if any(k in d for k in [
        "TRANSFER TO", "TRANSFER FROM", "INTERNAL TRANSFER", "MOVED TO OFFSET", "MOVE TO OFFSET",
        "RTP TRANSFER", "RTP CX8", "RTP SHARES", "OSKO PAYMENT", "OSKO DEPOSIT", "PAY ANYONE",
        "NETBANK TRANSFER", "HSBC BK PLC INTL", "CHQ DEP BRANCH"
    ]):
        return "Transfer"

    # 9. Tax
    if any(k in d for k in ["TAX OFFICE", " ATO ", "ATO PAYMENT", "ATO TRANSFER", "TAX FROM SAVINGS", "H & R BLOCK"]):
        return "Tax"

    # 10. Fitness & Wellbeing
    if any(k in d for k in ["ANYTIME FITNESS", "AUSFIT", "GYM", "FITNESS", "YOGA", "PILATES"]):
        return "Fitness & Wellbeing"

    # 11. Utilities & Rates
    if any(k in d for k in [
        "AGL", "SYDNEY WATER", "AUSSIE BROADBAND", "INTERNODE", "VODAFONE", "TELSTRA",
        "OPTUS", "FOXTEL", "CITY OF RYDE", "ORIGIN", "ENERGYAUSTRALIA", "COUNCIL"
    ]):
        return "Utilities & Rates"

    # 12. Health & Medical Therapy
    if any(k in d for k in [
        "BUPA", "CHEMIST", "PHARMACY", "SPEECH THERAPY", "LUZ ALL ABILITIES", "PHYSIO",
        "MEKERTICHIAN", "HUGHES", "NEUROCALM", "DENTAL", "RAY MEDHORA", "MEDICAL",
        "DOCTOR", "HEALTH", "HOSPITAL", "PATHOLOGY", "IMAGING", "CLINIC", "CINCOTTA"
    ]):
        return "Health & Therapy"

    # 13. Kids & Education
    if any(k in d for k in [
        "SCHOOLS ONLINE", "0SCHOOLSONLINE", "KUMON", "SWIM", "VITALAND", "ZONE BOWLING",
        "CHILDCARE", "DAYCARE", "TUITION", "COLLEGE", "HIGH SCHOOL", "PUBLIC SCHOOL",
        "UNSW", "SYDNEY UNI"
    ]):
        return "Kids & Education"

    # 14. Groceries & Supermarkets
    if any(k in d for k in [
        "WOOLWORTHS", "COLES", "ALDI", "IGA ", "BUTCHERY", "BAKERS DELIGHT", "BOX DIVVY",
        "J MART", "COSTI", "GET FISH", "SEAFOOD", "TOKYO MART", "SUPERMARKET", "GROCER",
        "MARKET PROV", "FRUIT", "VEG", "ABOUT LIFE"
    ]):
        return "Groceries"

    # 15. Dining, Cafes & Takeaway
    if any(k in d for k in [
        "COFFEE", "CAFE", "MCDONALDS", "KFC", "DOMINOS", "GUZMAN", "SUSHI", "RAMEN",
        "HOTEL", "BAR", "CAKES", "PATISSERIE", "GELATO", "GRILLD", "FRANGO", "KOKO BLACK",
        "GOOD PAIR DAYS", "DAN MURPHY", "LIQUORLAND", "STEEL ESPRESSO", "HEY YOU",
        "TOP RYDE FOOD", "MASTER ROLLS", "BURGER PROJECT", "LIL LANE", "RESTAURANT",
        "PIZZA", "BAKERY", "BISTRO", "EATERY", "UBER EATS", "DELIVEROO", "DOORDASH", "5 LOAVES"
    ]):
        return "Dining & Drinks"

    # 16. Auto & Transport
    if any(k in d for k in [
        "TESLA", "MERCEDES", "RYDE AUTOMOTIVE", "RYDE MAZDA", "TRANSPORTFORNSW", "OPAL",
        "ETOLL", "PARKING", "POINTPARKING", "SPEEDWAY", "BP ", "CALTEX", "AMPOL",
        "7-ELEVEN", "SHELL", "MOBIL", "EPARK", "CAR WASH", "TOLL", "RMS ETOLL",
        "SERVICE NSW", "CTX WOW"
    ]):
        return "Auto & Transport"

    # 17. Travel & Holidays
    if any(k in d for k in [
        "EXPEDIA", "QANTAS", "JAL AIRLINE", "AIRBNB", "HOTEL", "BOOKING.COM",
        "VIRGIN", "JETSTAR", "TRIP.COM", "FLIGHT", "AIRLINE", "UKVI", "ACCOR"
    ]):
        return "Travel & Holidays"

    # 18. Insurance
    if any(k in d for k in ["ALLIANZ", "INSURANCE", "INSURE", "NRMA", "GIO", "AAMI"]):
        return "Insurance"

    # 19. Home, Hardware & Renovation (generic retailers; project-specific suppliers belong in config.json category_rules)
    if any(k in d for k in [
        "BUNNINGS", "IKEA", "GOOD GUYS", "ADAIRS", "PLUMB",
        "HARDWARE", "LIGHTING", "ELECTRICAL", "ROOFING", "TILES", "PAVING", "MASONRY",
        "BEACON LIGHTING", "TOTAL TOOLS", "REECE", "FREEDOM FURNITURE",
    ]):
        return "Home & Renovation"

    # Reward Gateway vouchers during late 2019 / early 2020 kitchen renovation (The Good Guys appliances)
    if "REWARD GATEWAY" in d:
        if not dt or ("2019-10" <= dt[:7] <= "2020-02"):
            return "Home & Renovation"
        return "Shopping & Subscriptions"

    # 20. Shopping, Electronics & Subscriptions
    if any(k in d for k in [
        "KMART", "BIG W", "TARGET", "MYER", "AMAZON", "APPLE", "ITUNES", "GOOGLE",
        "OPENAI", "DISNEY", "PLAYSTATION", "PRIME VIDE", "UNIQLO", "BONDS", "COTTON ON",
        "SEED", "KIDSTUFF", "TIFFANY", "GUCCI", "BLIZZARD", "MICROSOFT", "EB GAMES",
        "ZARA", "JB HI-FI", "HARVEY NORMAN", "EVENT CINEMAS", "BLOCKBUSTER", "NESPRESSO",
        "UMART"
    ]):
        return "Shopping & Subscriptions"

    # 21. Bank Fees
    if any(k in d for k in ["FEE", "COMMISSION", "PREV MONTH ACCT FEE", "TRANSACTION FEE"]):
        return "Bank Fees"

    return "Uncategorised"


class BasePDFProvider:
    """Interface for pluggable PDF document providers (statements, invoices, receipts, payslips).

    Architecture & Multi-Version Evolution Rules (see AGENTS.md Section 3):
    1. Provider Holds All Metadata:
       - `name`, `display_name`, `doc_type`, `document_kind` ('statement' | 'invoice' | 'receipt' | 'payslip')
       - `target_dir` & `duplicates_dir` (defaults to `<target_dir>/Duplicates`)
       - `source_domains`: macOS `kMDItemWhereFroms` download domains (e.g. `['hsbc.com.au']`) used for
         fast pre-filtering (`--where-from`) and format-drift detection (`[process_pdf] DRIFT`).
       - `filename_template`: Canonical filename pattern rendered from extracted tokens.
       - `cadence`, `expected_period_days`, `max_gap_days`: Statement continuity / missing-period rules.
    2. Multi-Version Format Evolution (`format_versions`, `detect_version()`, `sample_variants`):
       - Issuers periodically redesign PDF layouts over the years (e.g. 2019/2021 vs 2026).
       - Rather than splitting one institution into separate providers per year, keep ONE provider class
         with `format_versions = ["v2019_classic", "v2025_dual_ccy", ...]`.
       - Implement `detect_version(text, pdf_path) -> Optional[str]` to identify which format era a PDF
         uses, and branch inside `extract()` as needed so all historical and current layouts normalize
         into the same output schema and `filename_template`.
       - Populate `sample_variants = {"v2019_classic": "...", "v2025_dual_ccy": "..."}` with synthetic
         PII-free text fixtures for EVERY supported format version. `run_self_test()` automatically
         iterates over every version in `sample_variants` and verifies detection, extraction, and
         cent-level reconciliation so adding a 2026 format never breaks 2019/2021 PDFs.
    """

    name: str = "base"
    display_name: str = "Base Provider"
    doc_type: str = "generic"
    document_kind: str = "statement"  # statement | invoice | receipt | payslip | document
    enabled: bool = True
    target_dir: Optional[str] = None
    duplicates_dir: Optional[str] = None
    scan_dirs: List[str] = []
    source_domains: List[str] = []
    filename_template: Optional[str] = None
    cadence: str = "monthly"
    expected_period_days: int = 30
    max_gap_days: int = 1
    format_versions: List[str] = ["v1"]
    sample_text: Optional[str] = None
    sample_variants: Dict[str, str] = {}

    def default_config(self) -> Dict[str, Any]:
        """Return this provider's default configuration dictionary."""
        d: Dict[str, Any] = {
            "enabled": self.enabled,
            "display_name": self.display_name,
            "document_kind": self.document_kind,
            "cadence": self.cadence,
            "expected_period_days": self.expected_period_days,
            "max_gap_days": self.max_gap_days,
            "format_versions": list(self.format_versions),
        }
        if self.target_dir:
            d["target_dir"] = self.target_dir
        # duplicates_dir is only emitted when a provider sets one explicitly; otherwise it is
        # derived at run time as <resolved target_dir>/Duplicates, so a config override of
        # target_dir carries its Duplicates folder with it.
        if self.duplicates_dir:
            d["duplicates_dir"] = self.duplicates_dir
        if self.scan_dirs:
            d["scan_dirs"] = list(self.scan_dirs)
        if self.source_domains:
            d["source_domains"] = list(self.source_domains)
        if self.filename_template:
            d["filename_template"] = self.filename_template
        return d

    def matches_source_metadata(
        self, pdf_path: str, cfg: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """Return the matched download source URL if macOS kMDItemWhereFroms matches source_domains."""
        from ..pdf import get_where_froms

        pc = (cfg or {}).get("providers", {}).get(self.name, {})
        domains = pc.get("source_domains") or self.source_domains
        if not domains:
            return None
        urls = get_where_froms(pdf_path)
        for u in urls:
            u_low = u.lower()
            if any(dom.lower() in u_low for dom in domains):
                return u
        return None

    def detect_version(self, text: str, pdf_path: str) -> Optional[str]:
        """Return the matched format version identifier (e.g. 'v2021_legacy', 'v2026_current')
        if this PDF matches the provider, else None."""
        if self.detect(text, pdf_path):
            return self.format_versions[-1] if self.format_versions else "v1"
        return None

    def detect(self, text: str, pdf_path: str) -> bool:
        raise NotImplementedError

    def extract(self, text: str, pdf_path: str) -> Dict[str, Any]:
        raise NotImplementedError

    def generate_filename(
        self, data: Dict[str, Any], original_path: str, cfg: Optional[Dict[str, Any]] = None
    ) -> str:
        tmpl = self._resolve_filename_template(data, cfg)
        name = None
        if tmpl:
            name = self.render_filename_template(tmpl, data)
        if not name:
            return self.default_filename(data, original_path)
        stem, ext = os.path.splitext(name)
        variant = data.get("filename_variant")  # e.g. "Bonus" for a bonus-only payslip
        if variant and not stem.endswith("_" + variant):
            stem = f"{stem}_{variant}"
            name = stem + ext
        # A file that already carries the standard stem plus a recognised qualifier keeps its name:
        # <stem>[_Bonus|_Run<n>][_NN|_summary]. Qualifiers record facts the template cannot derive
        # (a second pay run in the same period, a duplicate copy, a one-page summary).
        current = os.path.basename(original_path or "")
        base_stem = os.path.splitext(self.render_filename_template(tmpl, data) or "")[0]
        if base_stem and re.fullmatch(
            re.escape(base_stem) + r"(_Bonus|_Run\d+)?(_\d{2}|_summary)?" + re.escape(ext), current
        ):
            return current
        return name

    def default_filename(self, data: Dict[str, Any], original_path: str) -> str:
        return os.path.basename(original_path)

    def _resolve_filename_template(
        self, data: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        if cfg and "providers" in cfg:
            provs = cfg["providers"]
            for key in (data.get("doc_type"), data.get("provider"), self.doc_type, self.name):
                if key and key in provs and provs[key].get("filename_template"):
                    return provs[key]["filename_template"]
        return self.filename_template

    @staticmethod
    def render_filename_template(tmpl: str, data: Dict[str, Any]) -> Optional[str]:
        accts = data.get("accounts") or []
        acct_name = (accts[0].get("account_name") or "") if accts else ""
        account_type_tag = "CMA" if "CMA" in acct_name.upper() else ("HomeLoan" if "LOAN" in acct_name.upper() else "Statement")
        acct_no = (accts[0].get("account_no") or "") if accts else (data.get("customer_no") or "")
        last4 = acct_no[-4:] if acct_no else "0000"
        raw_start = data.get("period_start") or ""
        raw_end = data.get("period_end") or data.get("issue_date") or ""
        if not raw_end:
            return None
        raw_issue = data.get("issue_date") or raw_end

        p_start = raw_start.isoformat() if hasattr(raw_start, "isoformat") else str(raw_start or "")
        p_end = raw_end.isoformat() if hasattr(raw_end, "isoformat") else str(raw_end or "")
        issue_dt = raw_issue.isoformat() if hasattr(raw_issue, "isoformat") else str(raw_issue or "")
        tokens = {
            "provider": data.get("provider") or "document",
            "doc_type": data.get("doc_type") or "document",
            "document_kind": data.get("document_kind") or "document",
            "customer_no": (data.get("customer_no") or "").replace("-", ""),
            "account_no": acct_no,
            "account_type_tag": account_type_tag,
            "last4": last4,
            "invoice_no": data.get("invoice_no") or data.get("reference_no") or acct_no,
            "receipt_no": data.get("receipt_no") or data.get("reference_no") or acct_no,
            "reference_no": data.get("reference_no") or data.get("invoice_no") or acct_no,
            "store": (data.get("store") or "").replace(" ", "_"),
            "amount": str(data.get("total_amount") or "").replace("-", ""),
            "period_start": p_start,
            "period_end": p_end,
            "period_start_compact": p_start.replace("-", ""),
            "period_end_compact": p_end.replace("-", ""),
            "issue_date": issue_dt,
            "issue_date_compact": issue_dt.replace("-", ""),
            "date_compact": issue_dt.replace("-", ""),
        }
        try:
            return tmpl.format(**tokens)
        except KeyError:
            return None

    def resolve_target_dir(
        self, data: Dict[str, Any], original_path: str, cfg: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        if cfg:
            provs = cfg.get("providers", {})
            for key in (data.get("doc_type"), data.get("provider"), self.doc_type, self.name):
                if key and key in provs and provs[key].get("target_dir"):
                    return os.path.abspath(os.path.expanduser(provs[key]["target_dir"]))
            tdirs = cfg.get("target_dirs", {})
            for key in (self.name, self.doc_type, data.get("provider"), data.get("doc_type")):
                if key and key in tdirs:
                    return os.path.abspath(os.path.expanduser(tdirs[key]))
        if self.target_dir:
            return os.path.abspath(os.path.expanduser(self.target_dir))
        return os.path.dirname(os.path.abspath(original_path))

    def resolve_duplicates_dir(
        self, data: Dict[str, Any], original_path: str, cfg: Optional[Dict[str, Any]] = None
    ) -> str:
        if cfg:
            provs = cfg.get("providers", {})
            for key in (data.get("doc_type"), data.get("provider"), self.doc_type, self.name):
                if key and key in provs and provs[key].get("duplicates_dir"):
                    return os.path.abspath(os.path.expanduser(provs[key]["duplicates_dir"]))
        if self.duplicates_dir:
            return os.path.abspath(os.path.expanduser(self.duplicates_dir))
        tdir = self.resolve_target_dir(data, original_path, cfg=cfg) or os.path.dirname(os.path.abspath(original_path))
        return os.path.join(tdir, "Duplicates")

    @staticmethod
    def parse_flexible_date(s: str) -> Optional[datetime.date]:
        if not s:
            return None
        s = s.strip().replace(",", "")
        m = re.match(r"^(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{2,4})$", s)
        if m:
            y = int(m.group(3))
            y = y + 2000 if y < 100 else y
            mon = MONTHS.get(m.group(2).lower())
            if mon:
                try:
                    return datetime.date(y, mon, int(m.group(1)))
                except ValueError:
                    return None
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%Y/%m/%d", "%d-%b-%Y"):
            try:
                return datetime.datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        return None

    def check_missing_periods(
        self,
        statements: List[Dict[str, Any]],
        cfg: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Detect missing statement periods & balance shifts for this provider."""
        pc = (cfg or {}).get("providers", {}).get(self.name, {})
        max_gap = int(pc.get("max_gap_days", self.max_gap_days))
        exp_days = float(pc.get("expected_period_days", self.expected_period_days))
        cadence = pc.get("cadence", self.cadence)

        # If statements belong to multiple distinct single accounts, check each account separately
        acct_nos = set()
        for s in statements:
            accts = s.get("accounts") or (s.get("data", {}) if isinstance(s.get("data"), dict) else {}).get("accounts", [])
            if len(accts) == 1 and accts[0].get("account_no"):
                acct_nos.add(accts[0]["account_no"])
        if len(acct_nos) > 1:
            all_gaps = []
            for ano in sorted(acct_nos):
                sub = [
                    s for s in statements
                    if any((a.get("account_no") == ano) for a in (s.get("accounts") or (s.get("data", {}) if isinstance(s.get("data"), dict) else {}).get("accounts", [])))
                ]
                all_gaps.extend(self.check_missing_periods(sub, cfg=cfg))
            return all_gaps

        # Deduplicate statements covering the exact same period_start..period_end
        by_period: Dict[tuple, Dict[str, Any]] = {}
        for s in statements:
            ps = s.get("period_start")
            pe = s.get("period_end")
            if not ps or not pe:
                continue
            key = (ps, pe)
            if key not in by_period or s.get("reconciled"):
                by_period[key] = s

        ordered = sorted(by_period.values(), key=lambda x: (x["period_start"], x["period_end"]))
        gaps: List[Dict[str, Any]] = []
        prev_end: Optional[str] = None
        prev_file: Optional[str] = None
        prev_bal: Dict[str, Any] = {}
        prev_name: Dict[str, str] = {}

        for st in ordered:
            p_start = st["period_start"]
            p_end = st["period_end"]
            src_file = os.path.basename(
                st.get("target_filename") or st.get("source_file") or st.get("file") or ""
            )
            accts = st.get("accounts") or (st.get("data", {}) if isinstance(st.get("data"), dict) else {}).get("accounts", [])

            if prev_end and p_start > prev_end:
                d_prev = datetime.date.fromisoformat(prev_end)
                d_curr = datetime.date.fromisoformat(p_start)
                days = (d_curr - d_prev).days
                if days > max_gap:
                    est_cnt = max(1, round(days / exp_days))
                    est_str = (
                        f"~{est_cnt} {cadence}"
                        if cadence != "daily"
                        else f"{days} day(s)"
                    )
                    b_diffs = []
                    shift_rows = []
                    for a in accts:
                        ano = a.get("account_no")
                        aname = a.get("account_name") or prev_name.get(ano) or ""
                        ob = D(a.get("opening_balance"))
                        if ano in prev_bal and prev_bal[ano] != ob:
                            delta = ob - prev_bal[ano]
                            b_diffs.append(f"{ano}: {fmt(prev_bal[ano])} -> {fmt(ob)}")
                            shift_rows.append(
                                {
                                    "account_no": ano,
                                    "account_name": aname,
                                    "before_balance": fmt(prev_bal[ano]),
                                    "after_balance": fmt(ob),
                                    "delta": fmt(delta),
                                }
                            )
                    gaps.append(
                        {
                            "provider": self.name,
                            "provider_display": self.display_name,
                            "doc_type": st.get("doc_type") or self.doc_type,
                            "missing_start": prev_end,
                            "missing_end": p_start,
                            "days": days,
                            "estimated_missing": est_str,
                            "estimated_count": est_cnt,
                            "before_file": prev_file,
                            "after_file": src_file,
                            "balance_shifts": b_diffs,
                            "shift_rows": shift_rows,
                        }
                    )

            for a in accts:
                ano = a.get("account_no")
                if ano:
                    if a.get("account_name"):
                        prev_name[ano] = a["account_name"]
                    if a.get("closing_balance") is not None:
                        prev_bal[ano] = D(a["closing_balance"])
            prev_end = p_end
            prev_file = src_file

        return gaps

    def run_self_test(self, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Run built-in verification using the provider's sample_text and any sample_variants."""
        samples: Dict[str, str] = {}
        if self.sample_text:
            samples["default"] = self.sample_text
        if self.sample_variants:
            samples.update(self.sample_variants)
        if not samples:
            return {"provider": self.name, "ok": True, "skipped": True}

        variant_results: Dict[str, Any] = {}
        all_ok = True
        for vname, stext in samples.items():
            fake_path = f"/tmp/{self.name}_{vname}_sample.pdf"
            detected = self.detect(stext, fake_path)
            fver = self.detect_version(stext, fake_path)
            data = self.extract(stext, fake_path)
            fname = self.generate_filename(data, fake_path, cfg=cfg)
            tdir = self.resolve_target_dir(data, fake_path, cfg=cfg)
            v_ok = bool(detected and data.get("reconciled") and fname and tdir)
            if not v_ok:
                all_ok = False
            variant_results[vname] = {
                "ok": v_ok,
                "detected": detected,
                "format_version": data.get("format_version") or fver,
                "reconciled": data.get("reconciled"),
                "target_filename": fname,
                "target_dir": tdir,
                "accounts": len(data.get("accounts", [])),
            }

        first = next(iter(variant_results.values()))
        return {
            "provider": self.name,
            "ok": all_ok,
            "detected": first["detected"],
            "reconciled": first["reconciled"],
            "target_filename": first["target_filename"],
            "target_dir": first["target_dir"],
            "accounts": first["accounts"],
            "variants": variant_results,
        }


class GenericPDFProvider(BasePDFProvider):
    """Fallback provider for unclassified PDFs."""

    name = "generic"
    display_name = "Generic PDF"
    doc_type = "generic"

    def detect(self, text: str, pdf_path: str) -> bool:
        return True

    def extract(self, text: str, pdf_path: str) -> Dict[str, Any]:
        fname = os.path.basename(pdf_path)
        dt_m = re.search(r"(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})", text)
        issue_dt = self.parse_flexible_date(dt_m.group(1)) if dt_m else None
        return {
            "provider": self.name,
            "provider_display": self.display_name,
            "doc_type": self.doc_type,
            "source_file": fname,
            "source_path": pdf_path,
            "customer_no": None,
            "period_start": None,
            "period_end": str(issue_dt) if issue_dt else None,
            "issue_date": str(issue_dt) if issue_dt else None,
            "reconciled": False,
            "accounts": [],
            "warnings": ["unclassified generic PDF"],
        }
