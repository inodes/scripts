import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(HERE, ".."))

from statements import paths  # noqa: E402


class TriageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.icloud = os.path.join(self.tmp, "cloud")
        os.makedirs(os.path.join(self.icloud, "PDF", "unsorted"))
        cfg = os.path.join(self.tmp, "config.json")
        with open(cfg, "w") as f:
            json.dump({"directories": {"icloud": self.icloud, "logs": os.path.join(self.tmp, "logs")},
                       "triage": {"types": [{"name": "my_landlord", "text": "Acme Property Management"}]}}, f)
        self._old = os.environ.get("PDF_AUDIT_CONFIG")
        os.environ["PDF_AUDIT_CONFIG"] = cfg
        paths._CACHE = None
        from pdfflow import triage
        self.t = triage
        self._read = triage.read_text
        self.text = "x" * 50

    def tearDown(self):
        self.t.read_text = self._read
        if self._old is None:
            os.environ.pop("PDF_AUDIT_CONFIG", None)
        else:
            os.environ["PDF_AUDIT_CONFIG"] = self._old
        paths._CACHE = None
        shutil.rmtree(self.tmp)

    def pdf(self, name, data=b"%PDF-1.4 a", where="unsorted"):
        p = os.path.join(self.icloud, "PDF", where, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(data)
        return p

    def test_classify_order_and_config_rule_first(self):
        self.assertEqual(self.t.classify("Acme Property Management tax invoice", "a.pdf"), "my_landlord")
        self.assertEqual(self.t.classify("Your kWh usage this quarter", "a.pdf"), "energy_utilities")
        self.assertIsNone(self.t.classify("nothing relevant here", "a.pdf"))

    def test_reason_folders(self):
        self.assertEqual(self.t.reason_folder("not_reconciled"), "unreconciled")
        self.assertEqual(self.t.reason_folder("Some Odd/Reason"), "some_odd_reason")

    def test_held_goes_to_needs_review_and_dry_run_moves_nothing(self):
        p = self.pdf("a.pdf")
        r = self.t.place(p, "no_document_date", dry_run=True)
        self.assertEqual(r["action"], "would_move")
        self.assertTrue(r["dest"].endswith(os.path.join("needs_review", "no_date", "a.pdf")))
        self.assertTrue(os.path.exists(p))

    def test_apply_moves_type_unknown_unreadable_and_duplicate(self):
        self.t.read_text = lambda path: "Origin Energy bill " + self.text if "e" in os.path.basename(path)[:1] else (
            "" if path.endswith("blank.pdf") else "some words " + self.text)
        e = self.pdf("e1.pdf", b"%PDF energy")
        u = self.pdf("u1.pdf", b"%PDF other")
        b = self.pdf("blank.pdf", b"%PDF blank")
        self.assertTrue(self.t.place(e, dry_run=False)["dest"].endswith("energy_utilities/e1.pdf"))
        self.assertTrue(self.t.place(u, dry_run=False)["dest"].endswith("unknown_type/u1.pdf"))
        self.assertTrue(self.t.place(b, dry_run=False)["dest"].endswith("needs_review/unreadable/blank.pdf"))
        dup = self.pdf("e2.pdf", b"%PDF energy")
        r = self.t.place(dup, dry_run=False)
        self.assertEqual(r["reason"], "duplicate")
        self.assertTrue(os.path.exists(r["dest"]))
        s = self.t.summary()
        self.assertEqual(s["needs_attention"], 2)

    def test_clash_with_different_content_gets_suffix_and_outside_root_untouched(self):
        self.t.read_text = lambda path: "some words " + self.text
        self.pdf("same.pdf", b"%PDF one", where="unknown_type")
        p = self.pdf("same.pdf", b"%PDF two")
        r = self.t.place(p, dry_run=False)
        self.assertTrue(r["dest"].endswith("unknown_type/same_01.pdf"))
        out = os.path.join(self.tmp, "elsewhere.pdf")
        open(out, "wb").write(b"%PDF z")
        self.assertEqual(self.t.place(out, dry_run=False)["action"], "outside_pdf_root")
        self.assertTrue(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()
