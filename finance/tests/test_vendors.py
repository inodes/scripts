"""Vendor providers: fabricated samples only (no real data)."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from statements.providers import AVAILABLE_PROVIDERS, detect_provider

NAMES = ("aussiebb", "telstra", "vodafone", "bunnings", "google_play")


class VendorSelfTests(unittest.TestCase):
    def test_self_tests_pass(self):
        found = {p.name: p for p in AVAILABLE_PROVIDERS if p.name in NAMES}
        self.assertEqual(set(found), set(NAMES))
        for name, p in found.items():
            res = p.run_self_test()
            self.assertTrue(res["ok"], name)

    def test_detection_is_exclusive(self):
        for p in AVAILABLE_PROVIDERS:
            if p.name not in NAMES:
                continue
            for ver, text in p.sample_variants.items():
                self.assertEqual(detect_provider(text, "x.pdf").name, p.name, (p.name, ver))

    def test_google_play_sept_and_gmt_date(self):
        g = [p for p in AVAILABLE_PROVIDERS if p.name == "google_play"][0]
        r = g.parse("v_order_receipt", "Order number: SOP.1\nOrder date: 22 Sept 2026 15:17:46 GMT+10\nTotal: $32.99")
        self.assertEqual(str(r["issue"]), "2026-09-22")

    def test_abb_credit_invoice_total(self):
        a = [p for p in AVAILABLE_PROVIDERS if p.name == "aussiebb"][0]
        r = a.parse("v_invoice", "Invoice #: 1\nIssue Date: 02/11/2019\nCustomer: X Invoice Number: 1 Total Due: $-50.00")
        self.assertEqual(str(r["total"]), "-50.00")

    def test_vodafone_2019_uses_email_date(self):
        v = [p for p in AVAILABLE_PROVIDERS if p.name == "vodafone"][0]
        text = "Hi SAMPLE,\nVodafone bill\nAccount number\n000000000\nInvoice number\n0000000004\nTotal balance due\n$162.21DR\n"
        d = v.extract(text, "rendered_2025-07-18_x.pdf")
        self.assertTrue(d["reconciled"])
        self.assertEqual(v.default_filename(d, "x.pdf"), "Vodafone_Bill_Notice_2025-07-18_0000000004.pdf")
        self.assertFalse(v.extract(text, "x.pdf")["reconciled"])  # no date anywhere -> held


if __name__ == "__main__":
    unittest.main()
