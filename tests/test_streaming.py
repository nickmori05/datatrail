from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import csv
import io
from pathlib import Path
import random
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

from datatrail.compare import merge_by_key
from datatrail.errors import Conflict, InvalidInput
from datatrail.export import csv_chunks
from datatrail.ingest import MAX_ROWS, parse_csv
from datatrail.store import Store
from datatrail.summary import change_counts


def csv_bytes(headers, rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    writer.writerows(rows)
    return buffer.getvalue().encode()


def reference_changes(old, new):
    """Every key's change worked out from whole snapshots in memory, as diff did before it streamed."""
    changes = []
    for key in sorted(old.keys() | new.keys()):
        before, after = old.get(key), new.get(key)
        if before != after:
            kind = "added" if before is None else "removed" if after is None else "changed"
            changes.append((key, kind))
    return changes


class MergeTests(unittest.TestCase):
    def test_merge_pairs_records_by_key_in_order(self):
        old = [("a", 1), ("c", 3), ("d", 4)]
        new = [("b", 20), ("c", 30), ("e", 50)]
        self.assertEqual(list(merge_by_key(old, new)), [
            ("a", 1, None), ("b", None, 20), ("c", 3, 30), ("d", 4, None), ("e", None, 50)])

    def test_merge_handles_empty_sides(self):
        self.assertEqual(list(merge_by_key([], [])), [])
        self.assertEqual(list(merge_by_key([("a", 1)], [])), [("a", 1, None)])
        self.assertEqual(list(merge_by_key([], [("a", 1)])), [("a", None, 1)])

    def test_merge_reads_each_side_lazily(self):
        pairs = merge_by_key(iter([("a", 1), ("b", 2)]), iter(range(0)))
        self.assertEqual(next(pairs), ("a", 1, None))


class StreamingDiffTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "data.sqlite3")
        self.store.initialize()

    def ingest(self, content, dataset="suppliers"):
        return self.store.import_csv(dataset, "id", content, "feed.csv")

    def test_batched_diffs_match_whole_snapshot_comparison(self):
        generator = random.Random(20261027)
        for number in range(20):
            headers = ["id", *generator.sample(["a", "b", "c"], generator.randint(0, 3))]
            sides, imports = [], []
            for _ in range(2):
                keys = generator.sample(range(60), generator.randint(0, 40))
                rows = [[f"K{key}", *(generator.choice(["", "x", "y"]) for _ in headers[1:])] for key in keys]
                sides.append({row[0]: dict(zip(headers, row, strict=True)) for row in rows})
                imports.append(self.ingest(csv_bytes(headers, rows), f"random{number}"))
            if imports[0]["id"] == imports[1]["id"]:
                continue
            expected = reference_changes(*sides)
            for batch in (1, 3, 1000):
                with self.subTest(pair=number, batch=batch), patch("datatrail.store.RECORD_BATCH", batch):
                    result = self.store.diff(imports[0]["id"], imports[1]["id"], limit=1000)
                    self.assertEqual([(item["key"], item["kind"]) for item in result["changes"]], expected)
                    exported = self.store.diff_changes(imports[0]["id"], imports[1]["id"])
                    self.assertEqual([(item["key"], item["kind"]) for item in exported], expected)
                    with closing(self.store.connect()) as connection:
                        counts = change_counts(connection, imports[0]["id"], imports[1]["id"])
                    self.assertEqual(result["counts"], counts)

    def test_key_order_matches_python_sorting_for_non_ascii_keys(self):
        keys = ["b", "B", "é", "e", "日本", "Z", "😀", "a b", "a", "ä", "10", "9", " x"]
        before = self.ingest(csv_bytes(["id", "v"], [[key, "1"] for key in keys]))
        after = self.ingest(csv_bytes(["id", "v"], [[key, "2"] for key in keys]))
        with patch("datatrail.store.RECORD_BATCH", 2):
            listed = [item["key"] for item in self.store.diff_changes(before["id"], after["id"])]
        self.assertEqual(listed, sorted(key.strip() for key in keys))

    def test_partly_read_exports_do_not_block_imports_or_tie_to_one_thread(self):
        before = self.ingest(csv_bytes(["id", "v"], [[f"K{key}", "1"] for key in range(10)]))
        after = self.ingest(csv_bytes(["id", "v"], [[f"K{key}", "2"] for key in range(10)]))
        with patch("datatrail.store.RECORD_BATCH", 2):
            changes = self.store.diff_changes(before["id"], after["id"])
            first = next(changes)
            # A connection left reading between batches would make this commit wait for its lock and fail.
            self.assertEqual(self.ingest(b"id,v\nK0,3\n")["version"], 3)
            with ThreadPoolExecutor(1) as pool:
                rest = pool.submit(list, changes).result()
        self.assertEqual([first["key"], *(item["key"] for item in rest)], sorted(f"K{key}" for key in range(10)))

    def test_exports_keep_memory_flat_for_large_snapshots(self):
        rows = 10_000
        before = self.ingest(csv_bytes(["id", "a", "b"], [[f"K{key:05d}", "x", "y"] for key in range(rows)]))
        after = self.ingest(csv_bytes(["id", "a", "b"],
                                      [[f"K{key:05d}", "x", "z" if key % 3 else "y"] for key in range(1, rows + 1)]))
        tracemalloc.start()
        self.addCleanup(tracemalloc.stop)
        size = sum(len(chunk) for chunk in csv_chunks(self.store.diff_changes(before["id"], after["id"])))
        export_peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.reset_peak()
        result = self.store.diff(before["id"], after["id"])
        diff_peak = tracemalloc.get_traced_memory()[1]
        self.assertGreater(size, 200_000)
        self.assertEqual(result["counts"], {"added": 1, "removed": 1, "changed": 6_666, "unchanged": 3_333})
        # Loading both snapshots whole peaked above 20 MiB here; batches keep it near 1 MiB.
        self.assertLess(max(export_peak, diff_peak), 8 * 1024 * 1024)

    def test_invalid_comparisons_fail_before_any_record_is_read(self):
        clean = self.ingest(b"id,v\nA,1\n")
        flagged = self.ingest(b"id,v\nA,1\nA,2\n")
        other = self.ingest(b"id,v\nA,2\n", "other")
        with patch.object(Store, "_clean_records", side_effect=AssertionError("records read")):
            for before, after in ((clean, flagged), (clean, other)):
                with self.subTest(after=after["id"]), self.assertRaises(Conflict):
                    self.store.diff_changes(before["id"], after["id"])


class RecordLimitTests(unittest.TestCase):
    def test_files_up_to_the_record_limit_are_accepted(self):
        self.assertEqual(MAX_ROWS, 50_000)
        content = csv_bytes(["id"], [[f"K{key}"] for key in range(MAX_ROWS)])
        _, rows = parse_csv(content, "id")
        self.assertEqual(len(rows), MAX_ROWS)
        with self.assertRaisesRegex(InvalidInput, "at most 50000 data records"):
            parse_csv(content + b"extra\n", "id")
