from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from datatrail import verify
from datatrail.errors import InvalidInput, NotFound
from datatrail.store import Store


CLEAN = b"id,name,qty\nA,Ada,1\nB,Bob,2\nC,Cy,3\n"
FLAGGED = b"id,name,qty\nA,Ada,1\nA,Al,x\n,Nobody,2\nB,Bob\nC,,3\n"


class VerifyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "data.sqlite3"
        self.store = Store(self.database)
        self.store.initialize()
        self.clean = self.store.import_csv("parts", "id", CLEAN, "clean.csv", ["qty:integer"], required=["name"])
        self.flagged = self.store.import_csv("parts", "id", FLAGGED, "flagged.csv")

    def execute(self, statement, *params):
        with closing(self.store.connect()) as connection, connection:
            connection.execute(statement, params)

    def problems(self, identifier):
        result, = [item for item in self.store.verify("parts")["imports"] if item["id"] == identifier]
        self.assertEqual(result["verified"], not result["problems"])
        return result["problems"]

    def test_untouched_imports_verify_including_flagged_rows_and_failed_rules(self):
        result = self.store.verify("parts")
        self.assertEqual((result["dataset"], result["checked"], result["failed"]), ("parts", 2, 0))
        self.assertEqual([(item["version"], item["verified"], item["problems"]) for item in result["imports"]],
                         [(2, True, []), (1, True, [])])
        self.assertEqual(result["imports"][0]["source_name"], "flagged.csv")
        self.assertEqual(self.flagged["rejected_count"], 5)

    def test_a_changed_source_fails_its_hash_and_reparse(self):
        self.execute("UPDATE imports SET source = ? WHERE id = ?", CLEAN.replace(b"Bob", b"Rob"), self.clean["id"])
        sha, records = self.problems(self.clean["id"])
        self.assertEqual(sha["check"], "sha256")
        self.assertIn(hashlib.sha256(CLEAN.replace(b"Bob", b"Rob")).hexdigest(), sha["detail"])
        self.assertEqual((records["check"], records["count"], records["records"]), ("records", 1, [2]))
        self.assertEqual(self.problems(self.flagged["id"]), [])

    def test_a_changed_hash_alone_is_reported(self):
        self.execute("UPDATE imports SET sha256 = ? WHERE id = ?", "0" * 64, self.clean["id"])
        self.assertEqual([problem["check"] for problem in self.problems(self.clean["id"])], ["sha256"])

    def test_edited_deleted_and_added_records_are_listed_by_number(self):
        self.execute("UPDATE records SET data_json = ? WHERE import_id = ? AND number = 3",
                     json.dumps({"id": "C", "name": "Cy", "qty": "30"}), self.clean["id"])
        self.execute("UPDATE records SET issues_json = '[]', issue_count = 0 WHERE import_id = ? AND number = 4",
                     self.flagged["id"])
        self.execute("DELETE FROM records WHERE import_id = ? AND number = 1", self.flagged["id"])
        self.execute("""INSERT INTO records (import_id, number, source_line, entity_key, raw_json, data_json,
                        issues_json, issue_count) VALUES (?, 9, 9, 'Z', '["Z"]', NULL, '[]', 0)""", self.clean["id"])
        clean, = self.problems(self.clean["id"])
        self.assertEqual((clean["count"], clean["records"]), (2, [3, 9]))
        flagged, = self.problems(self.flagged["id"])
        self.assertEqual((flagged["count"], flagged["records"]), (2, [1, 4]))

    def test_changed_rules_are_visible_as_record_differences(self):
        # Rules cannot change through Datatrail, so a direct edit makes the saved quality report disagree.
        self.execute("UPDATE datasets SET column_types_json = '{}' WHERE name = 'parts'")
        self.execute("UPDATE imports SET rejected_count = 4 WHERE id = ?", self.flagged["id"])
        problems = self.problems(self.flagged["id"])
        self.assertEqual([problem["check"] for problem in problems], ["counts", "records"])
        self.assertEqual(problems[0]["counts"], {"rejected_count": {"saved": 4, "source": 5}})
        self.assertEqual(problems[1]["records"], [2])

    def test_header_and_unparseable_sources_are_reported(self):
        self.execute("UPDATE imports SET headers_json = ? WHERE id = ?", '["id", "name", "quantity"]', self.clean["id"])
        self.assertEqual([problem["check"] for problem in self.problems(self.clean["id"])], ["headers"])
        self.execute("UPDATE imports SET source = ? WHERE id = ?", b"\xff\xfe", self.flagged["id"])
        sha, parse = self.problems(self.flagged["id"])
        self.assertEqual((sha["check"], parse["check"]), ("sha256", "parse"))
        self.assertIn("UTF-8", parse["detail"])

    def test_record_examples_are_capped_but_counted(self):
        content = b"id\n" + b"".join(f"K{number:02d}\n".encode() for number in range(30))
        imported = self.store.import_csv("keys", "id", content, "keys.csv")
        self.execute("UPDATE records SET source_line = source_line + 1 WHERE import_id = ?", imported["id"])
        problem, = self.store.verify("keys")["imports"][0]["problems"]
        self.assertEqual((problem["count"], problem["records"]), (30, list(range(1, verify.MAX_EXAMPLES + 1))))

    def test_records_are_read_in_batches_on_short_connections(self):
        content = b"id,v\n" + b"".join(f"K{number:04d},{number}\n".encode() for number in range(250))
        imported = self.store.import_csv("bulk", "id", content, "bulk.csv")
        with patch("datatrail.store.RECORD_BATCH", 7):
            self.assertEqual(self.store.verify("bulk")["failed"], 0)
            batches = list(verify.stored_records(self.store.connect, imported["id"], 7))
        self.assertEqual([row[0] for row in batches], list(range(1, 251)))
        # A write between batches is not blocked by the reader.
        reader = verify.stored_records(self.store.connect, imported["id"], 7)
        next(reader)
        self.store.import_csv("bulk", "id", content + b"K9999,x\n", "more.csv")
        self.assertEqual(sum(1 for _ in reader), 249)

    def test_pages_are_newest_first_and_counted_per_page(self):
        self.execute("UPDATE imports SET sha256 = ? WHERE id = ?", "0" * 64, self.clean["id"])
        first = self.store.verify("parts", limit=1)
        self.assertEqual(([item["version"] for item in first["imports"]], first["failed"]), ([2], 0))
        second = self.store.verify("parts", limit=1, offset=1)
        self.assertEqual(([item["version"] for item in second["imports"]], second["failed"]), ([1], 1))
        self.assertEqual(self.store.verify("parts", offset=2)["imports"], [])

    def test_databases_upgraded_from_before_column_types_verify(self):
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
        self.assertEqual(store.verify("suppliers")["imports"][0]["problems"], [])

    def test_unknown_datasets_and_bad_pages_are_rejected(self):
        with self.assertRaisesRegex(NotFound, "Dataset 'missing' does not exist"):
            self.store.verify("missing")
        for limit, offset in ((0, 0), (1001, 0), (1, -1)):
            with self.subTest(limit=limit, offset=offset), self.assertRaises(InvalidInput):
                self.store.verify("parts", limit, offset)


if __name__ == "__main__":
    unittest.main()
