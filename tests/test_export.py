from pathlib import Path
import tempfile
import unittest

from datatrail.errors import Conflict, InvalidInput, NotFound
from datatrail.store import Store


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
