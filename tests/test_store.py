from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier
import unittest

from datatrail.errors import Conflict, InvalidInput, NotFound
from datatrail.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "data.sqlite3"
        self.store = Store(self.database)
        self.store.initialize()

    def ingest(self, content=b"id,name\nA,Ada\n", dataset="suppliers", key="id", types=None):
        return self.store.import_csv(dataset, key, content, "feed.csv", types)

    def test_import_retains_original_bytes_hash_and_provenance_after_restart(self):
        content = b"id,name\r\n A , Ada \r\n"
        result = self.ingest(content)
        reopened = Store(self.database)
        reopened.initialize()
        self.assertEqual(reopened.source(result["id"]), content)
        self.assertEqual(result["sha256"], hashlib.sha256(content).hexdigest())
        row, = reopened.records(result["id"])
        self.assertEqual(row["source_line"], 2)
        self.assertEqual(row["raw"], [" A ", " Ada "])
        self.assertEqual(row["data"], {"id": "A", "name": "Ada"})
        self.assertEqual(reopened.trace("suppliers", "A")[0]["sha256"], result["sha256"])

    def test_exact_file_retry_returns_original_import_and_source_label(self):
        first = self.ingest()
        retry = self.store.import_csv("suppliers", "id", b"id,name\nA,Ada\n", "renamed.csv")
        self.assertEqual(retry, {**first, "replayed": True})
        self.assertEqual(len(self.store.imports("suppliers")), 1)

    def test_dataset_key_cannot_change(self):
        self.ingest()
        with self.assertRaises(Conflict):
            self.ingest(key="name")
        self.assertEqual(len(self.store.imports("suppliers")), 1)

    def test_column_types_flag_invalid_values_and_apply_to_later_imports(self):
        first = self.ingest(b"id,qty,due\nA,2,2026-01-31\nB,two,2026-02-30\nC,,\n", "orders",
                            types=["qty:integer", "due:date"])
        self.assertEqual(first["column_types"], {"due": "date", "qty": "integer"})
        counts = (first["accepted_count"], first["rejected_count"], first["issue_counts"]["invalid_value"])
        self.assertEqual(counts, (2, 1, 1))
        row, = self.store.records(first["id"], issues=["invalid_value"])
        self.assertEqual((row["number"], row["issues"], row["invalid_columns"]), (2, ["invalid_value"], ["due", "qty"]))
        self.assertEqual(self.store.records(first["id"])[0]["invalid_columns"], [])
        self.assertEqual(self.ingest(b"id,qty,due\nA,x,2026-01-31\n", "orders")["rejected_count"], 1)
        repeated = self.ingest(b"id,qty,due\nA,3,\n", "orders", types=["due:date", "qty:integer"])
        self.assertEqual(repeated["rejected_count"], 0)
        self.assertEqual(self.store.datasets()[0]["column_types"], {"due": "date", "qty": "integer"})

    def test_column_types_are_fixed_per_dataset_and_bad_rules_save_nothing(self):
        self.ingest()
        with self.assertRaisesRegex(Conflict, "column types cannot change"):
            self.ingest(b"id,name\nA,1\n", types=["name:integer"])
        self.ingest(b"id,qty\nA,1\n", "orders", types=["qty:integer"])
        for types in (["qty:decimal"], ["qty:integer", "id:integer"]):
            with self.assertRaisesRegex(Conflict, "column types cannot change"):
                self.ingest(b"id,qty\nA,1\n", "orders", types=types)
        for content, dataset, types, message in (
                (b"id,qty\nA,1\n", "fresh", ["qty:money"], "COLUMN:TYPE"),
                (b"id,name\nA,1\n", "fresh", ["qty:integer"], "Typed column 'qty' is missing"),
                (b"id,name\nA,Ada\n", "orders", None, "Typed column 'qty' is missing")):
            with self.subTest(types=types), self.assertRaisesRegex(InvalidInput, message):
                self.ingest(content, dataset, types=types)
        self.assertEqual([dataset["name"] for dataset in self.store.datasets()], ["orders", "suppliers"])
        self.assertEqual(len(self.store.imports("orders")), 1)

    def test_databases_created_before_column_types_are_upgraded_in_place(self):
        legacy = self.database.with_name("legacy.sqlite3")
        content = b"id,name\nA,Ada\n"
        with closing(sqlite3.connect(legacy)) as connection:
            connection.executescript((Path(__file__).parent / "fixtures" / "schema-0.1.0.sql").read_text())
            connection.execute("INSERT INTO datasets VALUES (1, 'suppliers', 'id')")
            connection.execute("INSERT INTO imports VALUES (1, 1, 1, 'old.csv', ?, '2026-01-01T00:00:00+00:00', "
                               "'[\"id\", \"name\"]', 1, 1, 0, ?)", (hashlib.sha256(content).hexdigest(), content))
            connection.execute("""INSERT INTO records VALUES (1, 1, 2, 'A', '["A", "Ada"]',
                                  '{"id": "A", "name": "Ada"}', '[]', 0)""")
            connection.commit()
        store = Store(legacy)
        store.initialize()
        store.initialize()
        old = store.get_import(1)
        self.assertEqual((old["column_types"], old["issue_counts"]["invalid_value"]), ({}, 0))
        self.assertEqual(store.records(1)[0]["invalid_columns"], [])
        self.assertEqual(store.import_csv("suppliers", "id", content, "again.csv")["replayed"], True)
        new = store.import_csv("suppliers", "id", b"id,name\nA,Ada Lovelace\n", "new.csv")
        self.assertEqual(store.latest_changes("suppliers")["counts"]["changed"], 1)
        self.assertEqual(new["column_types"], {})
        with self.assertRaisesRegex(Conflict, "column types"):
            store.import_csv("suppliers", "id", b"id,name\nA,1\n", "typed.csv", ["name:integer"])
        typed = store.import_csv("orders", "id", b"id,qty\nA,x\n", "typed.csv", ["qty:integer"])
        self.assertEqual(store.records(typed["id"])[0]["invalid_columns"], ["qty"])

    def test_invalid_csv_never_creates_a_dataset(self):
        with self.assertRaises(InvalidInput):
            self.ingest(b"name\nAda")
        self.assertEqual(self.store.datasets(), [])

    def test_insert_failure_rolls_back_dataset_import_and_records(self):
        with closing(self.store.connect()) as connection:
            connection.execute("""CREATE TRIGGER fail_second BEFORE INSERT ON records WHEN NEW.number = 2
                                  BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
            connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.ingest(b"id,name\nA,Ada\nB,Bob\n")
        self.assertEqual(self.store.datasets(), [])
        with closing(self.store.connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM imports").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM records").fetchone()[0], 0)

    def test_concurrent_retries_create_one_import(self):
        barrier = Barrier(4)
        def submit(_):
            barrier.wait(timeout=5)
            return self.ingest()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(submit, range(4)))
        self.assertEqual(len({result["id"] for result in results}), 1)
        self.assertEqual(sum(not result["replayed"] for result in results), 1)
        self.assertEqual(len(self.store.records(results[0]["id"])), 1)

    def test_concurrent_distinct_imports_get_unique_versions(self):
        barrier = Barrier(4)
        def submit(number):
            barrier.wait(timeout=5)
            return self.ingest(f"id,name\nA,Name {number}\n".encode())
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(submit, range(4)))
        self.assertEqual(sorted(result["version"] for result in results), [1, 2, 3, 4])

    def test_bad_rows_are_retained_and_filtered_before_pagination(self):
        result = self.ingest(b"id,name\nA,Ada\nB,Bob\nB,Bill\n,Missing\nC\n")
        self.assertEqual((result["row_count"], result["accepted_count"], result["rejected_count"]), (5, 1, 4))
        flagged = self.store.records(result["id"], flagged=True, limit=2, offset=1)
        self.assertEqual([row["number"] for row in flagged], [3, 4])
        self.assertEqual(len(self.store.trace("suppliers", "B")), 2)

    def test_issue_counts_break_down_flagged_rows_by_code(self):
        clean = self.ingest()
        self.assertEqual(clean["issue_counts"], {"column_count": 0, "missing_key": 0, "duplicate_key": 0,
                                                 "invalid_value": 0})
        dirty = self.ingest(b"id,name\nA,Ada\nB,Bob\nB,Bill\nB,Ben\n,Missing\nC\nD,x,y\n")
        expected = {"column_count": 2, "missing_key": 1, "duplicate_key": 3, "invalid_value": 0}
        self.assertEqual(dirty["issue_counts"], expected)
        self.assertEqual(sum(expected.values()), dirty["rejected_count"])
        self.assertEqual(self.store.get_import(dirty["id"])["issue_counts"], expected)
        self.assertEqual(self.ingest(b"id,name\nA,Ada\nB,Bob\nB,Bill\nB,Ben\n,Missing\nC\nD,x,y\n"),
                         {**dirty, "replayed": True})

    def test_records_filter_by_issue_code_before_pagination(self):
        clean = self.ingest()
        dirty = self.ingest(b"id,name\nA,Ada\nB,Bob\n,Missing\nB,Bill\nC\nB,Ben\n")
        duplicates = self.store.records(dirty["id"], issues=["duplicate_key"])
        self.assertEqual([row["number"] for row in duplicates], [2, 4, 6])
        self.assertTrue(all(row["issues"] == ["duplicate_key"] for row in duplicates))
        page = self.store.records(dirty["id"], issues=["duplicate_key"], limit=1, offset=1)
        self.assertEqual([row["number"] for row in page], [4])
        both = self.store.records(dirty["id"], issues=["missing_key", "column_count"])
        self.assertEqual([row["number"] for row in both], [3, 5])
        self.assertEqual(self.store.records(dirty["id"], flagged=False, issues=["missing_key"])[0]["number"], 3)
        self.assertEqual(self.store.records(clean["id"], issues=["duplicate_key"]), [])
        for issues in (["unknown"], ["duplicate_key", "Missing_key"], "duplicate_key"):
            with self.assertRaisesRegex(InvalidInput, "Issue codes"):
                self.store.records(dirty["id"], issues=issues)
        with self.assertRaises(NotFound):
            self.store.records(99, issues=["missing_key"])

    def test_diff_counts_and_field_changes_include_source_record_numbers(self):
        before = self.ingest(b"id,name,city\nA,Ada,Boston\nB,Bob,Denver\nC,Cam,Austin\n")
        after = self.ingest(b"id,name,city\nB,Bob,Chicago\nA,Ada,Boston\nD,Dee,Reno\n")
        diff = self.store.diff(before["id"], after["id"])
        self.assertEqual(diff["counts"], {"added": 1, "removed": 1, "changed": 1, "unchanged": 1})
        changed = diff["changes"][0]
        self.assertEqual(changed["key"], "B")
        self.assertEqual(changed["fields"], {"city": {"before": "Denver", "after": "Chicago"}})
        self.assertEqual((changed["before_record"], changed["after_record"]), (2, 1))
        page = self.store.diff(before["id"], after["id"], limit=1, offset=1)
        self.assertEqual(page["counts"], diff["counts"])
        self.assertEqual(page["changes"][0]["key"], "C")
        self.assertEqual(page["total_changes"], 3)

    def test_diff_kind_filter_keeps_full_counts_and_paginates_matching_changes(self):
        before = self.ingest(b"id,name,city\nA,Ada,Boston\nB,Bob,Denver\nC,Cam,Austin\nE,Eve,Omaha\n")
        after = self.ingest(b"id,name,city\nA,Ada,Boston\nB,Bob,Chicago\nD,Dee,Reno\nF,Fay,Tulsa\n")
        full = self.store.diff(before["id"], after["id"])
        added = self.store.diff(before["id"], after["id"], kinds=["added"])
        self.assertEqual(added["counts"], full["counts"])
        self.assertEqual(added["kinds"], ["added"])
        self.assertEqual([change["key"] for change in added["changes"]], ["D", "F"])
        self.assertEqual(added["total_changes"], 2)
        page = self.store.diff(before["id"], after["id"], limit=1, offset=1, kinds=["removed", "changed"])
        self.assertEqual(page["kinds"], ["removed", "changed"])
        self.assertEqual(page["total_changes"], 3)
        self.assertEqual(page["changes"][0]["key"], "C")
        self.assertEqual(full["kinds"], ["added", "removed", "changed"])
        for kinds in (["unchanged"], ["moved"]):
            with self.assertRaisesRegex(InvalidInput, "Change kinds"):
                self.store.diff(before["id"], after["id"], kinds=kinds)

    def test_latest_changes_compares_newest_clean_snapshots_and_reports_skipped_flags(self):
        self.ingest(b"id,name\nA,Ada\n")
        before = self.ingest(b"id,name\nA,Ada\nB,Bob\n")
        after = self.ingest(b"id,name\nA,Ada Lovelace\nB,Bob\n")
        dirty = self.ingest(b"id,name\nA,Ada\nA,Other\n")
        result = self.store.latest_changes("suppliers")
        self.assertEqual((result["before_id"], result["after_id"]), (before["id"], after["id"]))
        self.assertEqual((result["before_version"], result["after_version"]), (2, 3))
        self.assertEqual(result["skipped_flagged"], [{"id": dirty["id"], "version": 4, "rejected_count": 2}])
        self.assertEqual(result["counts"], {"added": 0, "removed": 0, "changed": 1, "unchanged": 1})
        self.assertEqual(result, {**self.store.diff(before["id"], after["id"]), "dataset": "suppliers",
                                  "before_version": 2, "after_version": 3,
                                  "skipped_flagged": result["skipped_flagged"]})
        self.assertEqual(self.store.latest_changes("suppliers", kinds=["added"])["changes"], [])

    def test_latest_changes_needs_two_clean_snapshots(self):
        with self.assertRaises(NotFound):
            self.store.latest_changes("suppliers")
        self.ingest()
        self.ingest(b"id,name\nA,Ada\nA,Other\n")
        with self.assertRaisesRegex(Conflict, "two clean snapshots"):
            self.store.latest_changes("suppliers")
        with self.assertRaises(InvalidInput):
            self.store.latest_changes("suppliers", kinds=["unchanged"])

    def test_changes_since_compares_a_chosen_baseline_with_the_newest_clean_snapshot(self):
        first = self.ingest(b"id,name\nA,Ada\n")
        self.ingest(b"id,name\nA,Ada\nB,Bob\n")
        dirty = self.ingest(b"id,name\nA,Ada\nA,Other\n")
        latest = self.ingest(b"id,name\nA,Ada Lovelace\nB,Bob\nC,Cam\n")
        result = self.store.latest_changes("suppliers", since=1)
        self.assertEqual((result["before_id"], result["after_id"]), (first["id"], latest["id"]))
        self.assertEqual((result["before_version"], result["after_version"]), (1, 4))
        self.assertEqual(result["counts"], {"added": 2, "removed": 0, "changed": 1, "unchanged": 0})
        self.assertEqual(result["skipped_flagged"], [{"id": dirty["id"], "version": 3, "rejected_count": 2}])
        self.assertEqual(self.store.latest_changes("suppliers", since=2)["skipped_flagged"][0]["version"], 3)
        self.assertEqual(self.store.latest_changes("suppliers", since=1, kinds=["changed"])["total_changes"], 1)

    def test_changes_since_rejects_missing_flagged_newest_and_invalid_baselines(self):
        self.ingest()
        self.ingest(b"id,name\nA,Ada\nA,Other\n")
        with self.assertRaisesRegex(NotFound, "no version 9"):
            self.store.latest_changes("suppliers", since=9)
        with self.assertRaisesRegex(Conflict, "cannot be a baseline"):
            self.store.latest_changes("suppliers", since=2)
        with self.assertRaisesRegex(Conflict, "newer than version 1"):
            self.store.latest_changes("suppliers", since=1)
        for since in (0, -1, True, "1"):
            with self.assertRaisesRegex(InvalidInput, "positive"):
                self.store.latest_changes("suppliers", since=since)
        with self.assertRaises(NotFound):
            self.store.latest_changes("missing", since=1)

    def test_changes_until_compares_up_to_a_chosen_end_version(self):
        first = self.ingest(b"id,name\nA,Ada\n")
        dirty = self.ingest(b"id,name\nA,Ada\nA,Other\n")
        middle = self.ingest(b"id,name\nA,Ada\nB,Bob\n")
        self.ingest(b"id,name\nA,Ada\nB,Bob\nB,Bill\n")
        self.ingest(b"id,name\nA,Ada Lovelace\nB,Bob\n")
        result = self.store.latest_changes("suppliers", until=3)
        self.assertEqual((result["before_id"], result["after_id"]), (first["id"], middle["id"]))
        self.assertEqual((result["before_version"], result["after_version"]), (1, 3))
        self.assertEqual(result["counts"], {"added": 1, "removed": 0, "changed": 0, "unchanged": 1})
        self.assertEqual(result["skipped_flagged"], [{"id": dirty["id"], "version": 2, "rejected_count": 2}])
        ranged = self.store.latest_changes("suppliers", since=3, until=5)
        self.assertEqual((ranged["before_version"], ranged["after_version"]), (3, 5))
        self.assertEqual([item["version"] for item in ranged["skipped_flagged"]], [4])
        self.assertEqual(self.store.latest_changes("suppliers", since=1, until=3, kinds=["changed"])["changes"], [])

    def test_changes_until_rejects_missing_flagged_first_and_invalid_end_versions(self):
        self.ingest()
        self.ingest(b"id,name\nA,Ada\nA,Other\n")
        self.ingest(b"id,name\nA,Ada\nB,Bob\n")
        with self.assertRaisesRegex(NotFound, "no version 9"):
            self.store.latest_changes("suppliers", until=9)
        with self.assertRaisesRegex(Conflict, "cannot be an end version"):
            self.store.latest_changes("suppliers", until=2)
        with self.assertRaisesRegex(Conflict, "older than version 1"):
            self.store.latest_changes("suppliers", until=1)
        for since in (1, 3):
            with self.assertRaisesRegex(InvalidInput, "older version than until"):
                self.store.latest_changes("suppliers", since=since, until=1 if since == 1 else 3)
        with self.assertRaisesRegex(Conflict, "cannot be a baseline"):
            self.store.latest_changes("suppliers", since=2, until=3)
        for until in (0, -1, True, "3"):
            with self.assertRaisesRegex(InvalidInput, "Until must be a positive"):
                self.store.latest_changes("suppliers", until=until)
        with self.assertRaises(NotFound):
            self.store.latest_changes("missing", until=1)

    def test_diff_blocks_flagged_inputs_instead_of_reporting_false_deletions(self):
        clean = self.ingest()
        dirty = self.ingest(b"id,name\nA,Ada\nA,Other\n")
        for before, after in ((clean, dirty), (dirty, clean)):
            with self.assertRaisesRegex(Conflict, "flagged"):
                self.store.diff(before["id"], after["id"])

    def test_diff_rejects_cross_dataset_comparison(self):
        first = self.ingest()
        second = self.ingest(dataset="other")
        with self.assertRaisesRegex(Conflict, "same dataset"):
            self.store.diff(first["id"], second["id"])

    def test_normalization_and_column_order_do_not_create_false_changes(self):
        first = self.ingest(b"id,name\nA,Ada\n")
        second = self.ingest(b"name,id\n Ada , A \n")
        self.assertEqual(self.store.diff(first["id"], second["id"])["total_changes"], 0)

    def test_schema_changes_distinguish_missing_fields_from_empty_strings(self):
        first = self.ingest(b"id,name\nA,\n")
        second = self.ingest(b"id,city\nA,\n")
        result = self.store.diff(first["id"], second["id"])
        self.assertEqual(result["schema"], {"added": ["city"], "removed": ["name"]})
        self.assertEqual(result["changes"][0]["fields"], {
            "city": {"before": None, "after": ""}, "name": {"before": "", "after": None}})

    def test_empty_snapshot_removes_all_records(self):
        before = self.ingest()
        after = self.ingest(b"id,name\n")
        self.assertEqual(self.store.diff(before["id"], after["id"])["counts"]["removed"], 1)

    def test_trace_returns_newest_versions_first_without_merging_entities(self):
        self.ingest()
        self.ingest(b"id,name\nA,Ada Lovelace\nB,Ada\n")
        trace = self.store.trace("suppliers", "A")
        self.assertEqual([row["version"] for row in trace], [2, 1])
        self.assertEqual([row["data"]["name"] for row in trace], ["Ada Lovelace", "Ada"])
        self.assertEqual(self.store.trace("suppliers", "a"), [])

    def test_unknown_resources_and_invalid_pages(self):
        for call in (lambda: self.store.get_import(99), lambda: self.store.records(99),
                     lambda: self.store.imports("missing"), lambda: self.store.trace("missing", "A"),
                     lambda: self.store.source(99)):
            with self.assertRaises(NotFound):
                call()
        for limit, offset in ((0, 0), (1001, 0), (True, 0), (1, -1)):
            with self.assertRaises(InvalidInput):
                self.store.datasets(limit, offset)

    def test_dataset_and_source_names_are_validated(self):
        for dataset in ("", "../data", "a b", "x" * 65):
            with self.assertRaises(InvalidInput):
                self.ingest(dataset=dataset)
        with self.assertRaises(InvalidInput):
            self.store.import_csv("data", "id", b"id\nA", "bad\nname.csv")
        self.assertEqual(self.store.datasets(), [])
