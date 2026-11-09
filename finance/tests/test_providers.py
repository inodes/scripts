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


if __name__ == "__main__":
    unittest.main()
