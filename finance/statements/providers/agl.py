"""AGL electricity and gas bills: detect, read and file them.

The reading itself lives in agl_parse.AGLBillParser (shared with ~/repos/tesla, which imports it
to get usage and rates out of the same bills). This class only decides what is an AGL bill and where
and under what name it is filed: {invoices}/AGL/AGL_Statement_<service>_<issue date>.pdf

Leaflets and offers from AGL are not bills and are left for triage.
"""

import datetime
import os
import re
from decimal import Decimal
from typing import Any, Dict, Optional

from .agl_parse import AGLBillParser
from .vendors import VendorProvider, _date

_BILL_WORDS = re.compile(r"Supply period|Bill period|Total amount due|Direct Debit amount|National Metering Identifier"
                         r"|Account balance|Here's your monthly electricity bill", re.I)


def _iso(s: Optional[str]) -> Optional[datetime.date]:
    return _date(int(s[:4]), int(s[5:7]), int(s[8:10])) if s else None


class AGLProvider(VendorProvider):
    name = "agl"
    display_name = "AGL"
    doc_type = "agl_statement"
    vendor_dir = "AGL"
    source_domains = ["agl.com.au"]
    format_versions = ["v_statement"]
    parser = AGLBillParser()
    sample_variants = {
        "v_statement": """
Call 131 245 Visit agl.com.au
Account name Sample Person
Supply period 22 Dec 2011 to 20 Mar 2012
Your account number 0000 0000
Total amount due $121.76
Pay by 12 Apr 2012
TAX INVOICE
Issued: 21 Mar 2012
AGL Retail Energy Limited ABN 21 074 839 464
""",
    }
    sample_expect = {"v_statement": "AGL_Statement_electricity_2012-03-21.pdf"}

    def versions(self, t):
        path = getattr(self, "_path", "")
        if not (re.search(r"\bAGL\b", t) and _BILL_WORDS.search(t)):
            return None
        if self.parser.detect(t, path):
            return "v_statement"
        # emailed copies carry none of the usual markers but still hold a billing period and an amount
        p = self.parser.extract(t, path)
        return "v_statement" if p.get("period_start") and p.get("total_amount") is not None else None

    def parse(self, ver, t):
        p = self.parser.extract(t, getattr(self, "_path", ""))
        self._parsed = p
        total = p.get("total_amount")
        issue = p.get("issue_date")
        m = re.search(r"Issued:\s*(\d{1,2} [A-Za-z]{3,9} \d{4})", t)  # paper-style bills print "Issued: 21 Mar 2012"
        if m:
            issue = self.parser._parse_flexible_date(m.group(1)) or issue
        if issue:
            p["issue_date"] = issue  # the name uses this single date
        return {"issue": _iso(issue or p.get("period_end")),
                "ref": (p.get("period_start") or "") + "_" + (p.get("period_end") or ""),
                "total": Decimal(str(total)) if total is not None else None, "stem": "AGL_Statement"}

    def extract(self, text: str, pdf_path: str = "") -> Dict[str, Any]:
        self._path = pdf_path or ""
        data = super().extract(text, pdf_path)
        if data.get("format_version"):
            data["agl"] = dict(getattr(self, "_parsed", {}))  # everything read from the bill, for other tools
        return data

    def default_filename(self, data: Dict[str, Any], original_path: str) -> str:
        current = os.path.basename(original_path or "")
        name = self.parser.standard_name(data.get("agl") or {}, original_path)
        if name == current or re.fullmatch(re.escape(name[:-4]) + r"_\d{2}\.pdf", current):
            return current  # already named (or a duplicate suffix of that name)
        return name
