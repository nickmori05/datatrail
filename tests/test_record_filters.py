from itertools import chain, combinations
from pathlib import Path
import tempfile
import unittest

from datatrail.errors import InvalidInput, NotFound
from datatrail.rules import RULE_NAMES
from datatrail.store import Store


CONTENT = b"""id,qty,region,email,code
A,1,east,a@example.com,S001
B,two,east,b@example.com,S002
C,3,north,,S003
D,x,south,d@example.com,bad
F,6,west,f@example.com,S006
F,7,west,f@example.com,S007
G,8
"""


def subsets(values):
    return chain.from_iterable(combinations(values, size) for size in range(len(values) + 1))


class RecordFilterTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "data.sqlite3")
        self.store.initialize()
        self.id = self.store.import_csv("orders", "id", CONTENT, "orders.csv", ["qty:integer"], ["email"],
                                        ["region:east", "region:west"], ["code:S[0-9]{3}"])["id"]

    def keys(self, **options):
        return [record["raw"][0] for record in self.store.records(self.id, **options)]

    def test_columns_and_rules_select_rows_that_broke_them(self):
        for options, expected in (({"columns": ["qty"]}, ["B", "D"]),
                                  ({"columns": ["region"]}, ["C", "D"]),
                                  ({"columns": ["email", "code"]}, ["C", "D"]),
                                  ({"rules": ["allowed"]}, ["C", "D"]),
                                  ({"rules": ["required"]}, ["C"]),
                                  ({"rules": ["type", "pattern"]}, ["B", "D"]),
                                  ({"columns": ["region", "qty"], "rules": ["type"]}, ["B", "D"]),
                                  ({"columns": ["id"]}, []),
                                  ({"columns": [" qty "], "rules": ["type", "type"]}, ["B", "D"])):
            with self.subTest(**options):
                self.assertEqual(self.keys(**options), expected)

    def test_a_column_and_a_rule_must_fail_together(self):
        # D broke `type` in qty and `allowed` in region, but not `allowed` in qty.
        self.assertEqual(self.keys(columns=["qty"], rules=["allowed"]), [])
        self.assertEqual(self.keys(columns=["region"], rules=["allowed"]), ["C", "D"])

    def test_column_and_rule_filters_combine_with_issue_codes_and_pages(self):
        self.assertEqual(self.keys(columns=["code"], issues=["invalid_value"]), ["D"])
        self.assertEqual(self.keys(columns=["qty"], issues=["missing_value"]), [])
        self.assertEqual(self.keys(rules=["required"], flagged=True), ["C"])
        self.assertEqual(self.keys(columns=["qty", "region"], limit=2, offset=1), ["C", "D"])
        # Duplicate keys and short rows are flagged but broke no value rule, so a rule filter leaves them out.
        self.assertEqual(self.keys(flagged=True), ["B", "C", "D", "F", "F", "G"])
        self.assertEqual(self.keys(rules=list(RULE_NAMES)), ["B", "C", "D"])

    def test_filters_match_a_python_reference_for_every_combination(self):
        everything = self.store.records(self.id, limit=1000)
        for columns in subsets(["id", "qty", "region", "email", "code"]):
            for rules in subsets(RULE_NAMES):
                with self.subTest(columns=columns, rules=rules):
                    expected = [record["number"] for record in everything if not (columns or rules) or any(
                        (not columns or column in columns) and (not rules or set(rules) & set(broken))
                        for column, broken in record["failed_rules"].items())]
                    options = {"columns": list(columns), "rules": list(rules)}
                    self.assertEqual([record["number"] for record in self.store.records(self.id, **options)],
                                     expected)
                    _, _, exported = self.store.record_export(self.id, **options)
                    self.assertEqual([record["number"] for record in exported], expected)

    def test_unknown_columns_and_rules_are_errors(self):
        for options, message in (({"columns": ["regoin"]}, "not one of this import's columns"),
                                 ({"columns": [""]}, "nonempty"),
                                 ({"columns": [7]}, "nonempty"),
                                 ({"columns": "qty"}, "list of names"),
                                 ({"rules": ["range"]}, "Rules must be one of"),
                                 ({"issues": ["bad_key"], "columns": ["qty"]}, "Issue codes")):
            for call in (self.store.records, self.store.record_export):
                with self.subTest(call=call.__name__, **options), self.assertRaisesRegex(InvalidInput, message):
                    call(self.id, **options)
        with self.assertRaises(NotFound):
            self.store.records(999, columns=["qty"])
        with self.assertRaisesRegex(InvalidInput, "Rules must be"):
            self.store.records(999, rules=["range"])

    def test_columns_are_checked_against_each_imports_own_header(self):
        other = self.store.import_csv("orders", "id", b"id,qty,region,email,code,note\nA,1,east,a@example.com,S001,x\n",
                                      "orders.csv")
        self.assertEqual(self.store.records(other["id"], columns=["note"]), [])
        with self.assertRaisesRegex(InvalidInput, "'note'"):
            self.store.records(self.id, columns=["note"])


if __name__ == "__main__":
    unittest.main()
