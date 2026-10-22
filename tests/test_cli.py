from contextlib import redirect_stderr, redirect_stdout
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from datatrail.__main__ import main
from datatrail.ingest import ISSUE_CODES


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

    def import_example(self, fixture):
        return self.call("import", "suppliers", str(ROOT / "examples" / fixture), "--key", "supplier_id")

    def test_clean_and_flagged_import_exit_codes(self):
        for fixture, code in (("suppliers-before.csv", 0), ("suppliers-messy.csv", 1)):
            actual, output, errors = self.import_example(fixture)
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
        for fixture, expected_counts, issues in (("suppliers-before.csv", (3, 3, 0), (0, 0, 0, 0, 0)),
                                                 ("suppliers-messy.csv", (5, 1, 4), (1, 1, 2, 0, 0))):
            with self.subTest(fixture=fixture):
                _, output, _ = self.import_example(fixture)
                imported = json.loads(output)
                code, output, errors = self.call("show", str(imported["id"]))
                self.assertEqual(code, 0, errors)
                details = json.loads(output)
                self.assertEqual(details, {key: value for key, value in imported.items() if key != "replayed"})
                counts = (details["row_count"], details["accepted_count"], details["rejected_count"])
                self.assertEqual(counts, expected_counts)
                self.assertEqual(details["issue_counts"], dict(zip(ISSUE_CODES, issues, strict=True)))
                self.assertEqual(details["source_name"], fixture)
                self.assertEqual(details["key_column"], "supplier_id")
                self.assertNotIn("source", details)
        _, output, _ = self.call("imports", "suppliers")
        self.assertEqual(len(json.loads(output)), 2)

    def test_records_issue_option_lists_rows_with_that_code(self):
        _, output, _ = self.import_example("suppliers-messy.csv")
        identifier = str(json.loads(output)["id"])
        code, output, errors = self.call("records", identifier, "--issue", "duplicate_key")
        self.assertEqual(code, 0, errors)
        self.assertEqual([(row["number"], row["source_line"]) for row in json.loads(output)], [(2, 3), (3, 4)])
        code, output, _ = self.call("records", identifier, "--issue", "missing_key", "--issue", "column_count")
        self.assertEqual([row["issues"] for row in json.loads(output)], [["missing_key"], ["column_count"]])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(["--database", str(self.database), "records", identifier, "--issue", "bad_key"])
        self.assertEqual(error.exception.code, 2)
        code, output, errors = self.call("records", "999", "--issue", "missing_key")
        self.assertEqual((code, output), (2, ""))
        self.assertIn("does not exist", errors)

    def test_import_type_option_flags_invalid_values(self):
        source = self.database.with_name("orders.csv")
        source.write_text("id,qty\nA,2\nB,two\n")
        code, output, errors = self.call("import", "orders", str(source), "--key", "id", "--type", "qty:integer")
        self.assertEqual(code, 1, errors)
        result = json.loads(output)
        self.assertEqual((result["column_types"], result["issue_counts"]["invalid_value"]), ({"qty": "integer"}, 1))
        _, output, _ = self.call("records", str(result["id"]), "--issue", "invalid_value")
        self.assertEqual([(row["number"], row["invalid_columns"]) for row in json.loads(output)], [(2, ["qty"])])
        for spec, message in (("qty:money", "COLUMN:TYPE"), ("qty:decimal", "cannot change")):
            code, output, errors = self.call("import", "orders", str(source), "--key", "id", "--type", spec)
            self.assertEqual((code, output), (2, ""))
            self.assertIn(message, errors)

    def test_diff_kind_option_limits_listed_changes(self):
        ids = []
        for fixture in ("suppliers-before.csv", "suppliers-after.csv"):
            _, output, _ = self.import_example(fixture)
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
            self.import_example(fixture)
        code, output, errors = self.call("changes", "suppliers", "--kind", "removed")
        self.assertEqual(code, 0, errors)
        result = json.loads(output)
        self.assertEqual((result["before_version"], result["after_version"]), (1, 2))
        self.assertEqual([item["version"] for item in result["skipped_flagged"]], [3])
        self.assertEqual([change["key"] for change in result["changes"]], ["S003"])

    def test_changes_since_picks_an_older_baseline(self):
        for fixture in ("suppliers-before.csv", "suppliers-messy.csv", "suppliers-after.csv"):
            self.import_example(fixture)
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
            self.import_example(fixture)
        code, output, errors = self.call("changes", "suppliers", "--until", "2", "--kind", "changed")
        self.assertEqual(code, 0, errors)
        result = json.loads(output)
        self.assertEqual((result["before_version"], result["after_version"]), (1, 2))
        self.assertEqual((result["skipped_flagged"], [change["key"] for change in result["changes"]]), ([], ["S002"]))
        for args, message in ((("--until", "3"), "cannot be an end version"),
                              (("--until", "1"), "older than version 1"),
                              (("--since", "2", "--until", "1"), "older version than until")):
            code, output, errors = self.call("changes", "suppliers", *args)
            self.assertEqual((code, output), (2, ""))
            self.assertIn(message, errors)

    def test_history_shows_a_keys_changes_and_flagged_rows(self):
        for fixture in ("suppliers-before.csv", "suppliers-after.csv", "suppliers-messy.csv"):
            self.import_example(fixture)
        code, output, errors = self.call("history", "suppliers", "S002")
        self.assertEqual(code, 0, errors)
        result = json.loads(output)
        summary = [(event["version"], event["kind"]) for event in result["events"]]
        self.assertEqual(summary, [(2, "changed"), (1, "added")])
        self.assertEqual(result["events"][0]["fields"], {"city": {"before": "Boston", "after": "Chicago"}})
        self.assertEqual([row["number"] for row in result["skipped_flagged"][0]["key_records"]], [2, 3])
        code, output, _ = self.call("history", "suppliers", "S003", "--limit", "1")
        self.assertEqual([event["kind"] for event in json.loads(output)["events"]], ["removed"])
        for args, message in ((("missing", "S002"), "does not exist"), (("suppliers", ""), "Key must be"),
                              (("suppliers", "S002", "--limit", "0"), "Limit")):
            code, output, errors = self.call("history", *args)
            self.assertEqual((code, output), (2, ""))
            self.assertIn(message, errors)

    def test_diff_and_changes_export_csv(self):
        for fixture in ("suppliers-before.csv", "suppliers-after.csv", "suppliers-messy.csv"):
            self.import_example(fixture)
        code, output, errors = self.call("diff", "1", "2", "--format", "csv", "--kind", "changed")
        self.assertEqual((code, errors), (0, ""))
        rows = list(csv.reader(io.StringIO(output, newline="")))
        self.assertEqual(rows, [["key", "kind", "field", "before", "after", "before_record", "after_record"],
                                ["S002", "changed", "city", "Boston", "Chicago", "2", "2"]])
        code, output, errors = self.call("changes", "suppliers", "--format", "csv")
        self.assertEqual(code, 0, errors)
        rows = list(csv.reader(io.StringIO(output, newline="")))
        self.assertEqual(sorted({(row[0], row[1]) for row in rows[1:]}),
                         [("S002", "changed"), ("S003", "removed"), ("S004", "added")])
        self.assertEqual(len(rows), 10)
        self.assertIn("skipped flagged versions 3", errors)
        for args, message in ((("diff", "1", "2", "--format", "csv", "--limit", "5"), "leave out --limit"),
                              (("changes", "suppliers", "--format", "csv", "--offset", "0"), "leave out --limit"),
                              (("diff", "1", "3", "--format", "csv"), "flagged"),
                              (("changes", "suppliers", "--format", "csv", "--until", "1"), "older than version 1")):
            code, output, errors = self.call(*args)
            self.assertEqual((code, output), (2, ""))
            self.assertIn(message, errors)
        code, output, _ = self.call("diff", "1", "2", "--limit", "1")
        self.assertEqual((code, len(json.loads(output)["changes"])), (0, 1))

    def test_csv_export_is_utf8_with_crlf_whatever_the_console_encoding(self):
        for name, content in (("before.csv", "id,name\nA,Ada\n"), ("after.csv", "id,name\nA,Zoë\n")):
            source = self.database.with_name(name)
            source.write_text(content, encoding="utf-8")
            self.call("import", "people", str(source), "--key", "id")
        result = subprocess.run([sys.executable, "-m", "datatrail", "--database", str(self.database), "diff", "1",
                                 "2", "--format", "csv"], cwd=ROOT, capture_output=True, timeout=30,
                                env={**os.environ, "PYTHONIOENCODING": "ascii"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode(), "key,kind,field,before,after,before_record,after_record\r\n"
                                                 "A,changed,name,Ada,Zoë,1,1\r\n")

    def test_spreadsheet_option_escapes_formulas_and_adds_a_byte_order_mark(self):
        for name, content in (("before.csv", "id,note\nA,ok\nB,-5\n"), ("after.csv", "id,note\nA,=1+1\nB,-6\n")):
            source = self.database.with_name(name)
            source.write_text(content, encoding="utf-8")
            self.call("import", "notes", str(source), "--key", "id")
        for args in (("diff", "1", "2"), ("changes", "notes")):
            code, output, errors = self.call(*args, "--format", "csv", "--spreadsheet")
            self.assertEqual((code, errors), (0, ""))
            self.assertEqual(output, "\ufeffkey,kind,field,before,after,before_record,after_record\r\n"
                                     "A,changed,note,ok,'=1+1,1,1\r\nB,changed,note,-5,-6,2,2\r\n")
        code, output, _ = self.call("diff", "1", "2", "--format", "csv")
        self.assertEqual((code, output.splitlines()[1]), (0, "A,changed,note,ok,=1+1,1,1"))
        for args in (("diff", "1", "2", "--spreadsheet"), ("changes", "notes", "--format", "json", "--spreadsheet")):
            code, output, errors = self.call(*args)
            self.assertEqual((code, output), (2, ""))
            self.assertIn("--spreadsheet only applies to --format csv", errors)

    def test_demo_runs_the_documented_workflow_in_an_isolated_database(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "demo.py")],
                                cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        demo = json.loads(result.stdout)
        self.assertEqual(demo["comparison"]["counts"], {"added": 1, "removed": 1, "changed": 1, "unchanged": 1})
        self.assertEqual(len(demo["flagged_records"]), 4)
        self.assertEqual(demo["issue_counts"], {"column_count": 1, "missing_key": 1, "duplicate_key": 2,
                                                "invalid_value": 0, "missing_value": 0})
        self.assertEqual([row["version"] for row in demo["supplier_trail"]], [3, 3, 2, 1])
        history = demo["supplier_history"]
        self.assertEqual([(event["version"], event["kind"]) for event in history["events"]],
                         [(2, "changed"), (1, "added")])
        self.assertEqual([item["version"] for item in history["skipped_flagged"]], [3])
        latest = demo["latest_changes"]
        self.assertEqual([item["version"] for item in latest["skipped_flagged"]], [3])
        self.assertEqual([change["key"] for change in latest["changes"]], ["S002"])
        self.assertEqual([(row["number"], row["invalid_columns"]) for row in demo["invalid_values"]],
                         [(3, ["amount"]), (4, ["due_date"])])
        self.assertEqual(demo["changes_csv"][:2], [["key", "kind", "field", "before", "after", "before_record",
                                                    "after_record"], ["S002", "changed", "city", "Boston", "Chicago",
                                                                      "2", "2"]])
