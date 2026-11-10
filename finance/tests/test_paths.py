"""Location rules: every path derives from config "directories"; unsafe destinations are refused."""
import json
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_FINANCE = os.path.dirname(_HERE)
if _FINANCE not in sys.path:
    sys.path.insert(0, _FINANCE)

from statements import paths


class TestPaths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "config.json")
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump({"directories": {
                "icloud": os.path.join(self.tmp, "cloud"),
                "data": os.path.join(self.tmp, "local-data"),
                "reports": "{finance}/Out",
            }}, f)
        self._old = os.environ.get("PDF_AUDIT_CONFIG")
        os.environ["PDF_AUDIT_CONFIG"] = self.cfg
        paths.directories(refresh=True)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("PDF_AUDIT_CONFIG", None)
        else:
            os.environ["PDF_AUDIT_CONFIG"] = self._old
        paths.directories(refresh=True)

    def test_everything_derives_from_directories(self):
        cloud = os.path.join(self.tmp, "cloud")
        self.assertEqual(paths.get("finance"), os.path.join(cloud, "Finance"))
        self.assertEqual(paths.statements("HSBC"), os.path.join(cloud, "Finance", "Statements", "HSBC"))
        self.assertEqual(paths.get("reports"), os.path.join(cloud, "Finance", "Out"))
        self.assertEqual(paths.get("pdf"), os.path.join(cloud, "PDF"))

    def test_documents_need_a_named_subfolder(self):
        for loose in (paths.under("finance", "x.pdf"), paths.statements("x.pdf"), paths.payslips("x.pdf")):
            with self.assertRaises(paths.UnsafeLocation):
                paths.check_destination(loose)
        self.assertTrue(paths.check_destination(paths.statements("HSBC", "x.pdf")))
        self.assertTrue(paths.check_destination(paths.under("pdf", "x.pdf")))

    def test_reports_not_loose_in_statements_and_not_in_repo(self):
        with self.assertRaises(paths.UnsafeLocation):
            paths.check_destination(paths.statements("index.html"), kind="report")
        with self.assertRaises(paths.UnsafeLocation):
            paths.check_destination(os.path.join(paths.REPO_DIR, "index.html"), kind="report")
        self.assertTrue(paths.check_destination(paths.under("reports", "index.html"), kind="report"))

    def test_database_never_in_icloud(self):
        with self.assertRaises(paths.UnsafeLocation):
            paths.check_destination(paths.under("finance", "pdf_audit.db"), kind="database")
        self.assertTrue(paths.check_destination(paths.under("data", "pdf_audit.db"), kind="database"))


if __name__ == "__main__":
    unittest.main()
