from pathlib import Path
import tempfile
import unittest

from datatrail.compare import change_kind, changed_fields, key_history
from datatrail.errors import InvalidInput, NotFound
from datatrail.store import Store


class CompareTests(unittest.TestCase):
    def test_changed_fields_keep_missing_and_empty_values_apart(self):
        self.assertEqual(changed_fields({"a": "", "b": "1"}, {"b": "1", "c": ""}),
                         {"a": {"before": "", "after": None}, "c": {"before": None, "after": ""}})
        self.assertEqual(changed_fields(None, {"a": "x"}), {"a": {"before": None, "after": "x"}})
        self.assertEqual(changed_fields({"a": "x"}, {"a": "x"}), {})

    def test_change_kind_covers_each_transition(self):
        same, other = {"data": {"a": "1"}}, {"data": {"a": "2"}}
        self.assertEqual([change_kind(None, same), change_kind(same, None), change_kind(same, other),
                          change_kind(same, {"data": {"a": "1"}})], ["added", "removed", "changed", "unchanged"])

    def test_key_history_skips_versions_without_a_change(self):
        snapshots = [{"id": number, "version": number, "source_name": f"{number}.csv", "sha256": "x",
                      "imported_at": "t"} for number in range(1, 5)]
        record = {"number": 1, "source_line": 2, "data": {"id": "A"}}
        events = key_history(snapshots, {2: record, 3: dict(record)})
        self.assertEqual([(event["version"], event["kind"], event["previous_version"]) for event in events],
                         [(2, "added", 1), (4, "removed", 3)])
        self.assertEqual(key_history(snapshots, {}), [])


class HistoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "data.sqlite3")
        self.store.initialize()

    def ingest(self, content, dataset="suppliers"):
        return self.store.import_csv(dataset, "id", content, "feed.csv")

    def build(self):
        imports = [self.ingest(content) for content in (
            b"id,name,city\nA,Ada,Boston\nB,Bob,Denver\n",
            b"id,name,city\nA,Ada,Boston\nB,Bob,Chicago\n",
            b"id,name,city\nB,Bob,Reno\nB,Bill,Reno\nA,Ada,Boston\n",
            b"id,name,city\nA,Ada,Boston\nC,Cam,Austin\n",
            b"id,name,city\nA,Ada,Boston\nB,Bob,Reno\n",
            b"id,name,city\nB,Bob,Reno\nA,Ada,Boston\n",
        )]
        self.assertEqual([item["rejected_count"] for item in imports], [0, 0, 2, 0, 0, 0])
        return imports

    def test_history_lists_each_change_newest_first_with_its_source(self):
        imports = self.build()
        result = self.store.history("suppliers", "B")
        summary = [(event["version"], event["kind"], event["previous_version"]) for event in result["events"]]
        self.assertEqual(summary, [(5, "added", 4), (4, "removed", 2), (2, "changed", 1), (1, "added", None)])
        self.assertEqual((result["clean_versions"], result["present_in"], result["total_events"]), (5, 4, 4))
        changed = result["events"][2]
        self.assertEqual(changed["fields"], {"city": {"before": "Denver", "after": "Chicago"}})
        self.assertEqual((changed["before_record"], changed["before_line"]), (2, 3))
        self.assertEqual((changed["after_record"], changed["after_line"]), (2, 3))
        self.assertEqual((changed["import_id"], changed["sha256"]), (imports[1]["id"], imports[1]["sha256"]))
        removed = result["events"][1]
        self.assertEqual(removed["fields"], {"city": {"before": "Chicago", "after": None},
                                             "id": {"before": "B", "after": None},
                                             "name": {"before": "Bob", "after": None}})
        self.assertEqual((removed["after_record"], removed["after_line"]), (None, None))
        self.assertEqual(result["latest"], {"version": 6, "import_id": imports[5]["id"], "record": 1,
                                            "data": {"id": "B", "name": "Bob", "city": "Reno"}})

    def test_history_lists_the_keys_rows_in_skipped_flagged_imports(self):
        imports = self.build()
        flagged = {"id": imports[2]["id"], "version": 3, "rejected_count": 2}
        b = self.store.history("suppliers", "B")["skipped_flagged"]
        self.assertEqual(b, [{**flagged, "key_records": [
            {"number": 1, "source_line": 2, "issues": ["duplicate_key"]},
            {"number": 2, "source_line": 3, "issues": ["duplicate_key"]}]}])
        a = self.store.history("suppliers", "A")
        self.assertEqual(a["skipped_flagged"], [{**flagged, "key_records": [
            {"number": 3, "source_line": 4, "issues": []}]}])
        self.assertEqual([(event["version"], event["kind"]) for event in a["events"]], [(1, "added")])

    def test_history_paginates_events_and_keeps_totals(self):
        self.build()
        page = self.store.history("suppliers", "B", limit=2, offset=1)
        self.assertEqual([event["version"] for event in page["events"]], [4, 2])
        self.assertEqual((page["total_events"], page["limit"], page["offset"]), (4, 2, 1))
        self.assertEqual(self.store.history("suppliers", "B", offset=4)["events"], [])

    def test_unknown_keys_and_flagged_only_datasets_have_empty_histories(self):
        self.build()
        for key in ("Z", "b", " B"):
            result = self.store.history("suppliers", key)
            self.assertEqual((result["events"], result["present_in"], result["latest"]), ([], 0, None))
            self.assertEqual(result["skipped_flagged"][0]["key_records"], [])
        self.ingest(b"id,name\nA,Ada\nA,Other\n", "broken")
        result = self.store.history("broken", "A")
        self.assertEqual((result["clean_versions"], result["events"], result["latest"]), (0, [], None))
        self.assertEqual(len(result["skipped_flagged"][0]["key_records"]), 2)

    def test_schema_changes_show_up_as_field_changes(self):
        self.ingest(b"id,name\nA,Ada\n")
        self.ingest(b"id,name,city\nA,Ada,\n")
        event = self.store.history("suppliers", "A")["events"][0]
        self.assertEqual((event["kind"], event["fields"]), ("changed", {"city": {"before": None, "after": ""}}))

    def test_ignored_columns_drop_events_where_only_they_changed(self):
        self.ingest(b"id,name,seen\nA,Ada,1\n")
        self.ingest(b"id,name,seen\nA,Ada,2\n")
        self.ingest(b"id,name,seen\nA,Ada Lovelace,3\n")
        full = self.store.history("suppliers", "A")
        self.assertEqual([event["version"] for event in full["events"]], [3, 2, 1])
        result = self.store.history("suppliers", "A", ignore=["seen"])
        self.assertEqual(result["ignored"], ["seen"])
        self.assertEqual([(event["version"], event["previous_version"]) for event in result["events"]],
                         [(3, 2), (1, None)])
        self.assertEqual(result["events"][0]["fields"], {"name": {"before": "Ada", "after": "Ada Lovelace"}})
        self.assertEqual(result["latest"]["data"], {"id": "A", "name": "Ada Lovelace"})
        for ignore in (["id"], ["missing"]):
            with self.subTest(ignore=ignore), self.assertRaises(InvalidInput):
                self.store.history("suppliers", "A", ignore=ignore)

    def test_history_rejects_missing_datasets_empty_keys_and_bad_pages(self):
        with self.assertRaises(NotFound):
            self.store.history("missing", "A")
        self.ingest(b"id,name\nA,Ada\n")
        for key in ("", None, 1):
            with self.assertRaisesRegex(InvalidInput, "Key must be"):
                self.store.history("suppliers", key)
        for limit, offset in ((0, 0), (1001, 0), (1, -1)):
            with self.assertRaises(InvalidInput):
                self.store.history("suppliers", "A", limit, offset)
