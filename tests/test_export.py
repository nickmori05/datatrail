import csv
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from datatrail.errors import Conflict, InvalidInput, NotFound
from datatrail.export import CSV_COLUMNS, change_rows, csv_chunks
from datatrail.store import Store


def parse(text):
    return list(csv.reader(io.StringIO(text, newline="")))


class CsvTests(unittest.TestCase):
    changes = [
        {"key": "A", "kind": "changed", "fields": {"note": {"before": 'say "hi", then\nleave', "after": ""}},
         "before_record": 1, "after_record": 2},
        {"key": "B", "kind": "added", "fields": {"id": {"before": None, "after": "B"},
                                                 "note": {"before": None, "after": "=1+1"}},
         "before_record": None, "after_record": 3},
    ]

    def test_rows_have_one_line_per_changed_field_and_blank_missing_values(self):
        self.assertEqual(list(change_rows(self.changes)), [
            ["A", "changed", "note", 'say "hi", then\nleave', "", 1, 2],
            ["B", "added", "id", "", "B", "", 3],
            ["B", "added", "note", "", "=1+1", "", 3]])

    def test_csv_quotes_values_uses_crlf_and_round_trips(self):
        text = "".join(csv_chunks(self.changes))
        self.assertTrue(text.startswith("key,kind,field,before,after,before_record,after_record\r\n"))
        self.assertIn('"say ""hi"", then\nleave"', text)
        rows = parse(text)
        self.assertEqual(rows[0], list(CSV_COLUMNS))
        self.assertEqual(rows[1], ["A", "changed", "note", 'say "hi", then\nleave', "", "1", "2"])
        self.assertEqual(rows[3][4], "=1+1")
        self.assertEqual("".join(csv_chunks([])), "key,kind,field,before,after,before_record,after_record\r\n")

    def test_chunks_are_bounded_and_consume_changes_lazily(self):
        consumed = []
        def changes():
            for number in range(50):
                consumed.append(number)
                yield {"key": f"K{number}", "kind": "added", "fields": {"id": {"before": None, "after": "x" * 20}},
                       "before_record": None, "after_record": number + 1}
        with patch("datatrail.export.CHUNK_SIZE", 100):
            chunks = csv_chunks(changes())
            first = next(chunks)
            self.assertLess(len(consumed), 50)
            rest = list(chunks)
        self.assertGreater(len(rest), 5)
        self.assertTrue(all(len(chunk) < 200 for chunk in [first, *rest]))
        self.assertEqual(len(parse(first + "".join(rest))), 51)


class ChangeStreamTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "data.sqlite3")
        self.store.initialize()

    def ingest(self, content, dataset="suppliers"):
        return self.store.import_csv(dataset, "id", content, "feed.csv")

    def test_diff_changes_lists_every_change_that_diff_pages_through(self):
        before = self.ingest(b"id,name,city\nA,Ada,Boston\nB,Bob,Denver\nC,Cam,Austin\n")
        after = self.ingest(b"id,name,city\nB,Bob,Chicago\nA,Ada,Boston\nD,Dee,Reno\n")
        stream = self.store.diff_changes(before["id"], after["id"])
        self.assertIs(iter(stream), stream)
        changes = list(stream)
        self.assertEqual(changes, self.store.diff(before["id"], after["id"], limit=1000)["changes"])
        self.assertEqual([(change["key"], change["kind"]) for change in changes],
                         [("B", "changed"), ("C", "removed"), ("D", "added")])
        added = list(self.store.diff_changes(before["id"], after["id"], kinds=["added"]))
        self.assertEqual([change["key"] for change in added], ["D"])

    def test_diff_changes_is_not_capped_by_the_page_limit(self):
        before = self.ingest(b"id\n")
        after = self.ingest(("id\n" + "".join(f"K{number:04}\n" for number in range(1500))).encode())
        changes = list(self.store.diff_changes(before["id"], after["id"]))
        self.assertEqual((len(changes), changes[-1]["key"]), (1500, "K1499"))

    def test_diff_changes_checks_inputs_before_returning(self):
        clean = self.ingest(b"id,name\nA,Ada\n")
        dirty = self.ingest(b"id,name\nA,Ada\nA,Other\n")
        other = self.ingest(b"id,name\nA,Ada\n", "other")
        for before, after, error in ((clean["id"], dirty["id"], Conflict), (clean["id"], other["id"], Conflict),
                                     (clean["id"], 99, NotFound)):
            with self.assertRaises(error):
                self.store.diff_changes(before, after)
        with self.assertRaisesRegex(InvalidInput, "Change kinds"):
            self.store.diff_changes(clean["id"], clean["id"], kinds=["unchanged"])

    def test_select_versions_matches_the_versions_latest_changes_compares(self):
        self.ingest(b"id,name\nA,Ada\n")
        self.ingest(b"id,name\nA,Ada\nA,Other\n")
        self.ingest(b"id,name\nA,Ada\nB,Bob\n")
        self.ingest(b"id,name\nA,Ada Lovelace\nB,Bob\n")
        for options in ({}, {"since": 1}, {"until": 3}, {"since": 1, "until": 4}):
            with self.subTest(**options):
                selected = self.store.select_versions("suppliers", **options)
                changes = self.store.latest_changes("suppliers", **options)
                self.assertEqual(selected, {name: changes[name] for name in selected})
        self.assertEqual([item["version"] for item in self.store.select_versions("suppliers", since=1)
                          ["skipped_flagged"]], [2])
        with self.assertRaisesRegex(Conflict, "cannot be a baseline"):
            self.store.select_versions("suppliers", since=2)
        with self.assertRaises(NotFound):
            self.store.select_versions("missing")
