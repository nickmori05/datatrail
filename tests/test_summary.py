from contextlib import closing
import csv
import io
from pathlib import Path
import random
import tempfile
import unittest

from datatrail.errors import InvalidInput, NotFound
from datatrail.ingest import ISSUE_CODES
from datatrail.store import Store
from datatrail.summary import change_counts


def csv_bytes(headers, rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    writer.writerows(rows)
    return buffer.getvalue().encode()


class SummaryTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "data.sqlite3")
        self.store.initialize()

    def ingest(self, content, dataset="suppliers", **rules):
        return self.store.import_csv(dataset, "id", content, "feed.csv", None, **rules)

    def counts(self, before, after, ignored=()):
        with closing(self.store.connect()) as connection:
            return change_counts(connection, before["id"], after["id"], ignored)


class ChangeCountTests(SummaryTestCase):
    def assertMatchesDiff(self, before, after, ignored=()):
        self.assertEqual(self.counts(before, after, ignored),
                         self.store.diff(before["id"], after["id"], ignore=list(ignored))["counts"])

    def test_counts_match_diff_for_schema_order_and_empty_value_changes(self):
        pairs = (
            (b"id,a,b\nK1,1,2\nK2,3,4\n", b"id,b,a\nK2,4,3\nK1,2,1\n"),
            (b"id,a\nK1,1\nK2,\n", b"id,a,b\nK1,1,\nK2,,\n"),
            (b"id,a,b\nK1,1,\nK2,2,x\n", b"id,a\nK1,1\nK2,2\n"),
            (b'id,a\nK1,"{""x"": 1}"\nK2,\xc3\xa9\nK3,"a,b"\n', b'id,a\nK1,"{""x"": 2}"\nK2,e\nK3,"a,b"\n'),
            (b"id,a\nK1,1\nK2,2\n", b"id,a\n"),
            (b"id,a\n", b"id,a\nK1,1\n"),
            (b"id,a\nK1,1\n", b"id,a\nK2,1\n"),
        )
        for number, (old, new) in enumerate(pairs):
            with self.subTest(pair=number):
                dataset = f"pair{number}"
                self.assertMatchesDiff(self.ingest(old, dataset), self.ingest(new, dataset))
        changed = self.counts(self.ingest(b"id,a,b\nK1,1,2\n", "order"), self.ingest(b"id,b,a\nK1,2,1\n", "order"))
        self.assertEqual(changed, {"added": 0, "removed": 0, "changed": 0, "unchanged": 1})

    def test_counts_match_diff_for_random_snapshots(self):
        generator = random.Random(20261022)
        for number in range(25):
            with self.subTest(pair=number):
                imports = []
                for _ in range(2):
                    headers = ["id", *generator.sample(["a", "b", "c", "d"], generator.randint(0, 4))]
                    keys = generator.sample(range(40), generator.randint(0, 30))
                    rows = [[f"K{key}", *(generator.choice(["", "x", "y", "z"]) for _ in headers[1:])] for key in keys]
                    imports.append(self.ingest(csv_bytes(headers, rows), f"random{number}"))
                if imports[0]["id"] != imports[1]["id"]:
                    self.assertMatchesDiff(*imports)
                    columns = sorted({*imports[0]["headers"], *imports[1]["headers"]} - {"id"})
                    ignored = generator.sample(columns, generator.randint(0, len(columns)))
                    with self.subTest(ignored=ignored):
                        self.assertMatchesDiff(*imports, ignored=sorted(ignored))

    def test_counts_skip_ignored_fields_on_either_side(self):
        pairs = (
            (b"id,a,t\nK1,1,x\nK2,2,x\n", b"id,a,t\nK1,1,y\nK2,3,y\n", ["t"]),
            (b"id,a\nK1,1\n", b"id,a,t\nK1,1,\n", ["t"]),
            (b"id,a,t\nK1,1,x\n", b"id,a\nK1,1\n", ["t"]),
            (b"id,a,t,u\nK1,1,x,y\n", b"id,u,a,t\nK1,z,1,w\n", ["t", "u"]),
            (b'id,a,t\nK1,1,"{""x"": 1}"\n', b'id,t,a\nK1,"{""x"": 2}",1\n', ["t"]),
        )
        for number, (old, new, ignored) in enumerate(pairs):
            with self.subTest(pair=number):
                dataset = f"ignored{number}"
                before, after = self.ingest(old, dataset), self.ingest(new, dataset)
                self.assertMatchesDiff(before, after, ignored)
                self.assertNotEqual(self.counts(before, after), self.counts(before, after, ignored))


class DatasetSummaryTests(SummaryTestCase):
    def setUp(self):
        super().setUp()
        rules = {"required": ["email"], "allowed": ["region:east", "region:west"]}
        self.v1 = self.ingest(b"id,email,region\nA,a@x,east\nB,b@x,west\n", **rules)
        self.v2 = self.ingest(b"id,email,region\nA,a@x,west\nB,b@x,west\nC,c@x,east\n")
        self.v3 = self.ingest(b"id,email,region\nA,,west\nB,,north\nC,c@x,east\nC,c@y,east\n")
        self.v4 = self.ingest(b"id,email,region,phone\nB,b@x,west,1\nC,c@x,east,2\n")

    def test_summary_reports_settings_and_version_counts(self):
        summary = self.store.summary("suppliers")
        settings = {name: summary[name] for name in ("dataset", "key_column", "column_types", "import_count",
                                                     "clean_versions", "flagged_versions", "latest_version",
                                                     "latest_clean_version", "limit", "offset")}
        self.assertEqual(settings, {"dataset": "suppliers", "key_column": "id", "column_types": {},
                                    "import_count": 4, "clean_versions": 3, "flagged_versions": 1,
                                    "latest_version": 4, "latest_clean_version": 4, "limit": 100, "offset": 0})
        self.assertEqual(summary["rules"]["required"], ["email"])

    def test_each_version_has_quality_schema_and_change_counts_newest_first(self):
        versions = self.store.summary("suppliers")["versions"]
        self.assertEqual([(item["version"], item["id"], item["clean"]) for item in versions],
                         [(4, self.v4["id"], True), (3, self.v3["id"], False), (2, self.v2["id"], True),
                          (1, self.v1["id"], True)])
        latest, flagged, second, first = versions
        self.assertEqual(latest["changes"], {"compared_with": 2, "added": 0, "removed": 1, "changed": 2,
                                             "unchanged": 0})
        self.assertEqual(latest["schema"], {"previous_version": 3, "added": ["phone"], "removed": []})
        self.assertEqual(second["changes"], {"compared_with": 1, "added": 1, "removed": 0, "changed": 1,
                                             "unchanged": 1})
        self.assertEqual((flagged["changes"], first["changes"], first["schema"]), (None, None, None))
        expected = dict.fromkeys(ISSUE_CODES, 0) | {"duplicate_key": 2, "invalid_value": 1, "missing_value": 2}
        self.assertEqual(flagged["issue_counts"], expected)
        self.assertEqual(flagged["invalid_columns"], {"email": 2, "region": 1})
        self.assertEqual((flagged["row_count"], flagged["accepted_count"], flagged["rejected_count"]), (4, 0, 4))
        self.assertEqual((latest["invalid_columns"], latest["issue_counts"]), ({}, dict.fromkeys(ISSUE_CODES, 0)))
        self.assertEqual({key for key in latest} - {"schema", "changes", "clean", "issue_counts", "invalid_columns"},
                         {"id", "version", "source_name", "sha256", "imported_at", "row_count", "accepted_count",
                          "rejected_count"})

    def test_ignored_columns_are_left_out_of_schema_and_change_counts(self):
        summary = self.store.summary("suppliers", ignore=["phone", " region"])
        self.assertEqual(summary["ignored"], ["phone", "region"])
        latest, flagged, second, _ = summary["versions"]
        self.assertEqual(latest["schema"], {"previous_version": 3, "added": [], "removed": []})
        self.assertEqual(latest["changes"], {"compared_with": 2, "added": 0, "removed": 1, "changed": 0,
                                             "unchanged": 2})
        self.assertEqual(second["changes"], {"compared_with": 1, "added": 1, "removed": 0, "changed": 0,
                                             "unchanged": 2})
        self.assertEqual(flagged["invalid_columns"], {"email": 2, "region": 1})
        self.assertEqual(self.store.summary("suppliers")["ignored"], [])
        for ignore in (["id"], ["fax"], "phone"):
            with self.subTest(ignore=ignore), self.assertRaises(InvalidInput):
                self.store.summary("suppliers", ignore=ignore)
        with self.assertRaises(NotFound):
            self.store.summary("missing", ignore=["phone"])

    def test_pages_compare_with_versions_outside_the_page(self):
        page = self.store.summary("suppliers", limit=1, offset=0)
        self.assertEqual([item["version"] for item in page["versions"]], [4])
        self.assertEqual(page["versions"][0]["changes"]["compared_with"], 2)
        self.assertEqual(page["import_count"], 4)
        self.assertEqual([item["version"] for item in self.store.summary("suppliers", 2, 1)["versions"]], [3, 2])
        self.assertEqual(self.store.summary("suppliers", offset=4)["versions"], [])

    def test_datasets_without_clean_versions_have_no_change_counts(self):
        self.ingest(b"id,name\nA,x\nA,y\n", "broken")
        self.ingest(b"id,name\n,x\n", "broken")
        summary = self.store.summary("broken")
        self.assertEqual((summary["clean_versions"], summary["latest_clean_version"]), (0, None))
        self.assertEqual([item["changes"] for item in summary["versions"]], [None, None])
        self.assertEqual(summary["versions"][0]["schema"], {"previous_version": 1, "added": [], "removed": []})

    def test_unknown_datasets_and_bad_pages_are_rejected(self):
        with self.assertRaisesRegex(NotFound, "Dataset 'missing' does not exist"):
            self.store.summary("missing")
        for limit, offset in ((0, 0), (1001, 0), (1, -1), ("1", 0)):
            with self.subTest(limit=limit, offset=offset), self.assertRaises(InvalidInput):
                self.store.summary("suppliers", limit, offset)
