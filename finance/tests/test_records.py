"""Data lives in <records>/*.json, not in config.json."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from statements import config, paths  # noqa: E402


class RecordsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "cloud", "Finance", "Data"))
        self.cfg = os.path.join(self.tmp, "config.json")
        json.dump({"directories": {"icloud": os.path.join(self.tmp, "cloud"), "data": os.path.join(self.tmp, "d")},
                   "personal": {"legacy_key": 1, "remittances": ["inline"]}}, open(self.cfg, "w"))
        self.old = os.environ.get("PDF_AUDIT_CONFIG")
        os.environ["PDF_AUDIT_CONFIG"] = self.cfg
        paths.directories(refresh=True)

    def tearDown(self):
        if self.old is None:
            os.environ.pop("PDF_AUDIT_CONFIG", None)
        else:
            os.environ["PDF_AUDIT_CONFIG"] = self.old
        paths._CACHE = None
        shutil.rmtree(self.tmp)

    def test_files_override_inline_and_legacy_still_read(self):
        with open(os.path.join(self.tmp, "cloud", "Finance", "Data", "remittances.json"), "w") as f:
            json.dump([{"aud": 1}], f)
        p = config.load()["personal"]
        self.assertEqual(p["remittances"], [{"aud": 1}])
        self.assertEqual(p["legacy_key"], 1)

    def test_bad_data_file_fails_loudly(self):
        with open(os.path.join(self.tmp, "cloud", "Finance", "Data", "bad.json"), "w") as f:
            f.write("{nope")
        with self.assertRaises(RuntimeError):
            config.load()


if __name__ == "__main__":
    unittest.main()
