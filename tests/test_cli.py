from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from datatrail.__main__ import main


ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "test.sqlite3"

    def call(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(["--database", str(self.database), *args])
        return code, output.getvalue(), errors.getvalue()

    def test_clean_and_flagged_import_exit_codes(self):
        for fixture, code in (("suppliers-before.csv", 0), ("suppliers-messy.csv", 1)):
            actual, output, errors = self.call("import", "suppliers", str(ROOT / "examples" / fixture), "--key", "supplier_id")
            self.assertEqual(actual, code, errors)
            self.assertEqual(bool(json.loads(output)["rejected_count"]), bool(code))
        code, output, _ = self.call("imports", "suppliers")
        self.assertEqual(code, 0)
        self.assertEqual([item["version"] for item in json.loads(output)], [2, 1])

    def test_missing_file_resource_and_invalid_limits_exit_two(self):
        for args in (("import", "suppliers", "/missing/feed.csv", "--key", "id"),
                     ("records", "999"), ("show", "999"), ("datasets", "--limit", "0")):
            code, output, errors = self.call(*args)
            self.assertEqual(code, 2)
            self.assertEqual(output, "")
            self.assertTrue(errors.startswith("Error:"))

    def test_show_reports_saved_metadata_for_clean_and_flagged_imports(self):
        for fixture, expected_counts in (("suppliers-before.csv", (3, 3, 0)),
                                         ("suppliers-messy.csv", (5, 1, 4))):
            with self.subTest(fixture=fixture):
                _, output, _ = self.call("import", "suppliers", str(ROOT / "examples" / fixture), "--key", "supplier_id")
                imported = json.loads(output)
                code, output, errors = self.call("show", str(imported["id"]))
                self.assertEqual(code, 0, errors)
                details = json.loads(output)
                self.assertEqual(details, {key: value for key, value in imported.items() if key != "replayed"})
                self.assertEqual((details["row_count"], details["accepted_count"], details["rejected_count"]), expected_counts)
                self.assertEqual(details["source_name"], fixture)
                self.assertEqual(details["key_column"], "supplier_id")
                self.assertNotIn("source", details)
        _, output, _ = self.call("imports", "suppliers")
        self.assertEqual(len(json.loads(output)), 2)

    def test_diff_kind_option_limits_listed_changes(self):
        ids = []
        for fixture in ("suppliers-before.csv", "suppliers-after.csv"):
            _, output, _ = self.call("import", "suppliers", str(ROOT / "examples" / fixture), "--key", "supplier_id")
            ids.append(str(json.loads(output)["id"]))
        code, output, errors = self.call("diff", *ids, "--kind", "changed")
        self.assertEqual(code, 0, errors)
        result = json.loads(output)
        self.assertEqual([change["kind"] for change in result["changes"]], ["changed"])
        self.assertEqual(result["counts"], {"added": 1, "removed": 1, "changed": 1, "unchanged": 1})
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(["--database", str(self.database), "diff", *ids, "--kind", "unchanged"])
        self.assertEqual(error.exception.code, 2)

    def test_changes_compares_latest_clean_snapshots_by_dataset_name(self):
        code, _, errors = self.call("changes", "suppliers")
        self.assertEqual(code, 2)
        self.assertIn("does not exist", errors)
        for fixture in ("suppliers-before.csv", "suppliers-after.csv", "suppliers-messy.csv"):
            self.call("import", "suppliers", str(ROOT / "examples" / fixture), "--key", "supplier_id")
        code, output, errors = self.call("changes", "suppliers", "--kind", "removed")
        self.assertEqual(code, 0, errors)
        result = json.loads(output)
        self.assertEqual((result["before_version"], result["after_version"]), (1, 2))
        self.assertEqual([item["version"] for item in result["skipped_flagged"]], [3])
        self.assertEqual([change["key"] for change in result["changes"]], ["S003"])

    def test_changes_since_picks_an_older_baseline(self):
        for fixture in ("suppliers-before.csv", "suppliers-messy.csv", "suppliers-after.csv"):
            self.call("import", "suppliers", str(ROOT / "examples" / fixture), "--key", "supplier_id")
        code, output, errors = self.call("changes", "suppliers", "--since", "1")
        self.assertEqual(code, 0, errors)
        result = json.loads(output)
        self.assertEqual((result["before_version"], result["after_version"]), (1, 3))
        self.assertEqual([item["version"] for item in result["skipped_flagged"]], [2])
        for since, message in (("2", "cannot be a baseline"), ("3", "newer than version 3"), ("7", "no version 7")):
            code, output, errors = self.call("changes", "suppliers", "--since", since)
            self.assertEqual((code, output), (2, ""))
            self.assertIn(message, errors)

    def test_changes_until_picks_an_older_end_version(self):
        for fixture in ("suppliers-before.csv", "suppliers-after.csv", "suppliers-messy.csv"):
            self.call("import", "suppliers", str(ROOT / "examples" / fixture), "--key", "supplier_id")
        code, output, errors = self.call("changes", "suppliers", "--until", "2", "--kind", "changed")
        self.assertEqual(code, 0, errors)
        result = json.loads(output)
        self.assertEqual((result["before_version"], result["after_version"]), (1, 2))
        self.assertEqual((result["skipped_flagged"], [change["key"] for change in result["changes"]]), ([], ["S002"]))
        for args, message in ((("--until", "3"), "cannot be an end version"), (("--until", "1"), "older than version 1"),
                              (("--since", "2", "--until", "1"), "older version than until")):
            code, output, errors = self.call("changes", "suppliers", *args)
            self.assertEqual((code, output), (2, ""))
            self.assertIn(message, errors)

    def test_demo_runs_the_documented_workflow_in_an_isolated_database(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "demo.py")],
                                cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        demo = json.loads(result.stdout)
        self.assertEqual(demo["comparison"]["counts"], {"added": 1, "removed": 1, "changed": 1, "unchanged": 1})
        self.assertEqual(len(demo["flagged_records"]), 4)
        self.assertEqual([row["version"] for row in demo["supplier_trail"]], [3, 3, 2, 1])
        latest = demo["latest_changes"]
        self.assertEqual([item["version"] for item in latest["skipped_flagged"]], [3])
        self.assertEqual([change["key"] for change in latest["changes"]], ["S002"])
