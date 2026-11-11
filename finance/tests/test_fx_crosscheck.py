import os
import sqlite3
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from statements import fx  # noqa: E402
import apple_crosscheck as cc  # noqa: E402

RATES = {"JPY": {"2022-09-16": {"aud": 0.0104, "source": "test"}, "2022-10": 0.01}}


class FxTests(unittest.TestCase):
    def test_day_beats_month_and_missing_is_none(self):
        self.assertEqual(fx.to_aud(10000, "JPY", "2022-09-16", RATES)["aud"], Decimal("104.00"))
        self.assertEqual(fx.to_aud(10000, "JPY", "2022-10-05", RATES)["aud"], Decimal("100.00"))
        self.assertIsNone(fx.to_aud(10000, "JPY", "2023-01-01", RATES))
        self.assertEqual(fx.to_aud("5.50", "AUD", "2023-01-01", RATES)["aud"], Decimal("5.50"))

    def test_candidates_window_tolerance_and_description(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE transactions (tx_date TEXT, description TEXT, debit TEXT, amount TEXT, account_name TEXT)")
        db.executemany("INSERT INTO transactions VALUES (?,?,?,?,?)", [
            ("2022-09-17", "APPLE.COM/BILL SYDNEY", "85.00", "-85.00", "Card"),   # within 6% of 83.00
            ("2022-09-17", "COFFEE SHOP", "83.00", "-83.00", "Card"),            # right amount, wrong merchant
            ("2022-09-30", "APPLE.COM/BILL SYDNEY", "83.00", "-83.00", "Card"),  # outside the window
            ("2022-09-16", "APPLE.COM/BILL SYDNEY", "120.00", "-120.00", "Card"),  # wrong amount
        ])
        got = cc.candidates(db, "2022-09-16", 7, Decimal("83.00"), 0.06, False)
        self.assertEqual([c["amount"] for c in got], ["85.00"])
        self.assertEqual(len(cc.candidates(db, "2022-09-16", 7, Decimal("83.00"), 0.06, True)), 2)


if __name__ == "__main__":
    unittest.main()
