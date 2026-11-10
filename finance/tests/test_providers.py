"""Unit tests for finance providers and classifier."""
import os
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_FINANCE = os.path.dirname(_HERE)
if _FINANCE not in sys.path:
    sys.path.insert(0, _FINANCE)

from statements.providers import AVAILABLE_PROVIDERS
from statements.providers.base import classify_transaction


class TestFinanceProviders(unittest.TestCase):
    def test_provider_self_tests(self):
        for p in AVAILABLE_PROVIDERS:
            res = p.run_self_test()
            self.assertTrue(
                res.get("ok", False) or res.get("status") == "ok",
                f"Self-test failed for {p.name}: {res}",
            )

    def test_classify_transaction_defaults(self):
        # Hermetic: ignore any user config.json / category_rules.json on this machine.
        with mock.patch("statements.providers.base.load_custom_category_rules", return_value={}):
            self.assertEqual(classify_transaction("WOOLWORTHS 1234"), "Groceries")
            self.assertEqual(classify_transaction("COLES SUPERMARKET"), "Groceries")
            self.assertEqual(classify_transaction("BP SERVICE STATION"), "Auto & Transport")

    def test_unverified_documents_are_held(self):
        from statements.ingest import hold_reason
        self.assertEqual(hold_reason({"reconciled": True}), "no_document_date")
        self.assertEqual(hold_reason({"period_end": "2026-01-31", "reconciled": False}), "not_reconciled")
        self.assertIsNone(hold_reason({"period_end": "2026-01-31", "reconciled": True}))

    def test_employer_name_alone_is_not_a_payslip(self):
        aapt = next(p for p in AVAILABLE_PROVIDERS if p.name == "aapt")
        self.assertFalse(aapt.detect("Tax Invoice\nBill To: AAPT Ltd\nLevel 1 Example St\nTotal $10.00", "invoice.pdf"))

    def test_payslip_name_qualifiers_are_kept(self):
        wd = next(p for p in AVAILABLE_PROVIDERS if p.name == "workday")
        data = {"period_end": "2020-01-15", "period_end_compact": "20200115"}
        std = wd.generate_filename(dict(data), "/x/anything.pdf")
        self.assertEqual(std, "Workday_Payslip_20200115.pdf")
        for keep in ("Workday_Payslip_20200115_Bonus.pdf", "Workday_Payslip_20200115_01.pdf",
                     "Workday_Payslip_20200115_Bonus_01.pdf", "Workday_Payslip_20200115_summary.pdf"):
            self.assertEqual(wd.generate_filename(dict(data), "/x/" + keep), keep)
        self.assertEqual(wd.generate_filename(dict(data, filename_variant="Bonus"), "/x/a.pdf"),
                         "Workday_Payslip_20200115_Bonus.pdf")
        self.assertEqual(wd.generate_filename(dict(data), "/x/Workday_Payslip_20200115_final.pdf"), std)


if __name__ == "__main__":
    unittest.main()
