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

    def ingest(self, content=b"id,name\nA,Ada\n", dataset="suppliers", key="id"):
        return self.store.import_csv(dataset, key, content, "feed.csv")

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
