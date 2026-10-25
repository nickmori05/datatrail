from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from datatrail.errors import Conflict, InvalidInput
from datatrail.ingest import parse_csv
from datatrail.rules import MAX_ALLOWED_VALUES, MAX_PATTERN_LENGTH, Checks, check_ranges, parse_rules, saved_rules
from datatrail.store import Store


EMPTY = {"required": [], "allowed": {}, "patterns": {}, "minimum": {}, "maximum": {}}


class RuleSpecTests(unittest.TestCase):
    def test_rules_are_trimmed_deduplicated_and_sorted(self):
        rules = parse_rules([" status ", "email", "status"], ["status: paused", "status:active", "status:active",
                                                              "opens:09:00"], ["code:S[0-9]{3}", " email : .+@.+ "])
        self.assertEqual(rules, {"required": ["email", "status"],
                                 "allowed": {"opens": ["09:00"], "status": ["active", "paused"]},
                                 "patterns": {"code": "S[0-9]{3}", "email": ".+@.+"}, "minimum": {}, "maximum": {}})
        self.assertEqual(parse_rules(), EMPTY)
        self.assertEqual(saved_rules({}), EMPTY)
        self.assertEqual(saved_rules(rules), rules)

    def test_malformed_specs_are_rejected(self):
        for arguments, message in (
                (("email",), "Required columns must be a list"),
                (([" "],), "nonempty column names"),
                (([3],), "nonempty column names"),
                (([], "status:active"), "Allowed values must be a list"),
                (([], ["status"]), "COLUMN:VALUE"),
                (([], [":active"]), "COLUMN:VALUE"),
                (([], ["status: "]), "COLUMN:VALUE"),
                (([], [None]), "COLUMN:VALUE"),
                (([], [], "code:x"), "Patterns must be a list"),
                (([], [], ["code"]), "COLUMN:PATTERN"),
                (([], [], ["code:"]), "COLUMN:PATTERN"),
                (([], [], ["code:a", "code :b"]), "more than one pattern"),
                (([], [], ["code:[A-Z"]), "not a valid regular expression"),
                (([], [], ["code:" + "a" * (MAX_PATTERN_LENGTH + 1)]), f"at most {MAX_PATTERN_LENGTH}")):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(InvalidInput, message):
                parse_rules(*arguments)
        values = [f"code:{number}" for number in range(MAX_ALLOWED_VALUES)]
        self.assertEqual(len(parse_rules([], values)["allowed"]["code"]), MAX_ALLOWED_VALUES)
        with self.assertRaisesRegex(InvalidInput, f"more than {MAX_ALLOWED_VALUES} allowed values"):
            parse_rules([], [*values, "code:extra"])


class CheckTests(unittest.TestCase):
    def setUp(self):
        rules = parse_rules(["email"], ["region:east", "region:west", "qty:1", "qty:2"], ["code:S[0-9]{3}"])
        self.checks = Checks({"qty": "integer"}, rules)

    def row(self, **values):
        return {"email": "a@example.test", "region": "east", "qty": "1", "code": "S001", **values}

    def test_clean_values_and_empty_optional_cells_pass(self):
        self.assertEqual(self.checks.columns, ["code", "email", "qty", "region"])
        self.assertEqual(self.checks.failures(self.row()), {})
        self.assertEqual(self.checks.failures(self.row(region="", qty="", code="")), {})

    def test_each_rule_reports_the_columns_and_rules_it_fails(self):
        self.assertEqual(self.checks.failures(self.row(email="")), {"email": ["required"]})
        self.assertEqual(self.checks.failures(self.row(region="East")), {"region": ["allowed"]})
        for code in ("S0011", "xS001", "S01"):
            with self.subTest(code=code):
                self.assertEqual(self.checks.failures(self.row(code=code)), {"code": ["pattern"]})
        self.assertEqual(self.checks.failures(self.row(qty="3")), {"qty": ["allowed"]})
        self.assertEqual(self.checks.failures(self.row(qty="x", email="", code="s001")),
                         {"code": ["pattern"], "email": ["required"], "qty": ["type", "allowed"]})


class RangeTests(unittest.TestCase):
    TYPES = {"due": "date", "price": "decimal", "qty": "integer"}

    def ranges(self, minimum=(), maximum=(), types=TYPES):
        return check_ranges(types, parse_rules(minimum=minimum, maximum=maximum))

    def test_bounds_are_trimmed_and_saved_in_one_spelling(self):
        rules = self.ranges([" price : 0.50 ", "qty:-0", "due:2026-01-01"], ["price:100.00", "qty:+003"])
        self.assertEqual((rules["minimum"], rules["maximum"]), (
            {"due": "2026-01-01", "price": "0.5", "qty": "0"}, {"price": "100", "qty": "3"}))
        self.assertEqual(self.ranges(["price:0.000"])["minimum"], {"price": "0"})
        self.assertEqual(self.ranges(["price:12."])["minimum"], {"price": "12"})
        long = "1234567890" * 5 + ".500"
        self.assertEqual(self.ranges(["price:" + long])["minimum"], {"price": long[:-2]})
        self.assertEqual(self.ranges(), parse_rules())
        self.assertEqual(check_ranges(self.TYPES, rules), rules)

    def test_bounds_must_fit_the_column_type_and_order(self):
        for minimum, maximum, message in (
                (["name:a"], [], "Column 'name' needs a column type"),
                ([], ["name:1"], "to have a maximum"),
                (["qty:1.5"], [], "minimum for column 'qty' must be a valid integer, not '1.5'"),
                (["price:1e3"], [], "valid decimal"),
                ([], ["due:2026-02-30"], "maximum for column 'due' must be a valid date"),
                (["price:10"], ["price:9.99"], "minimum for column 'price' is above its maximum"),
                (["due:2026-02-01"], ["due:2026-01-31"], "above its maximum")):
            with self.subTest(minimum=minimum, maximum=maximum), self.assertRaisesRegex(InvalidInput, message):
                self.ranges(minimum, maximum)
        for arguments, message in (({"minimum": "qty:1"}, "Minimum values must be a list"),
                                   ({"maximum": ["qty"]}, "COLUMN:VALUE"),
                                   ({"minimum": ["qty:1", "qty :2"]}, "more than one minimum")):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(InvalidInput, message):
                parse_rules(**arguments)
        self.assertEqual(self.ranges(["price:5"], ["price:5.0"])["maximum"], {"price": "5"})

    def test_values_outside_a_range_fail_min_or_max(self):
        rules = parse_rules(minimum=["qty:1", "price:-.5", "due:2026-01-01"], maximum=["qty:10", "due:2026-12-31"])
        checks = Checks(self.TYPES, rules)
        self.assertEqual(checks.columns, ["due", "price", "qty"])
        for values, failed in (
                ({"qty": "1", "price": "-0.50", "due": "2026-12-31"}, {}),
                ({"qty": "+010", "price": "1e9", "due": ""}, {"price": ["type"]}),
                ({"qty": "0", "price": "-0.51", "due": "2027-01-01"},
                 {"due": ["max"], "price": ["min"], "qty": ["min"]}),
                ({"qty": "11", "price": "", "due": "2025-12-31"}, {"due": ["min"], "qty": ["max"]}),
                ({"qty": "x", "price": "99999999999999999999999999999999.5", "due": "2026-13-01"},
                 {"due": ["type"], "qty": ["type"]})):
            with self.subTest(values=values):
                self.assertEqual(checks.failures(values), failed)
        allowed = Checks({"qty": "integer"}, parse_rules([], ["qty:20", "qty:5"], minimum=["qty:10"]))
        self.assertEqual(allowed.failures({"qty": "5"}), {"qty": ["min"]})
        self.assertEqual(allowed.failures({"qty": "12"}), {"qty": ["allowed"]})
        with self.assertRaisesRegex(InvalidInput, "needs a column type"):
            Checks({}, parse_rules(minimum=["qty:1"]))


class RuleParsingTests(unittest.TestCase):
    def test_rule_failures_set_issue_codes_alongside_other_issues(self):
        rules = parse_rules(["email"], ["region:east", "region:west"], ["code:S[0-9]{3}"])
        content = (b"id,email,region,code\nA,a@x, east ,S001\nB,,west,S002\nC,c@x,north,S003\nD,,north,S4\n"
                   b"D,d@x,west,S005\nE,,\n")
        _, rows = parse_csv(content, "id", {}, rules)
        self.assertEqual([row.issues for row in rows], [
            [], ["missing_value"], ["invalid_value"], ["duplicate_key", "invalid_value", "missing_value"],
            ["duplicate_key"], ["column_count"]])
        self.assertEqual([row.failed for row in rows], [
            {}, {"email": ["required"]}, {"region": ["allowed"]},
            {"code": ["pattern"], "email": ["required"], "region": ["allowed"]}, {}, {}])
        self.assertEqual(rows[3].invalid, ["code", "email", "region"])

    def test_types_and_rules_on_one_column_report_each_failure(self):
        _, rows = parse_csv(b"id,qty\nA,7\nB,x\nC,8\n", "id", {"qty": "integer"}, parse_rules([], ["qty:7", "qty:x"]))
        self.assertEqual([(row.issues, row.failed) for row in rows], [
            ([], {}), (["invalid_value"], {"qty": ["type"]}), (["invalid_value"], {"qty": ["allowed"]})])

    def test_rule_columns_must_exist_and_the_key_cannot_be_listed_as_required(self):
        for rules, message in ((parse_rules(["email"]), "Rule column 'email' is missing"),
                               (parse_rules([], ["region:east"]), "Rule column 'region' is missing"),
                               (parse_rules([], [], ["code:x"]), "Rule column 'code' is missing"),
                               (parse_rules(["id"]), "key column is always required")):
            with self.subTest(rules=rules), self.assertRaisesRegex(InvalidInput, message):
                parse_csv(b"id,name\nA,Ada\n", "id", {}, rules)
        with self.assertRaisesRegex(InvalidInput, "Typed column 'qty' is missing"):
            parse_csv(b"id,name\nA,Ada\n", "id", {"qty": "integer"}, parse_rules(["qty"]))


class StoredRuleTests(unittest.TestCase):
    CONTENT = b"id,email,region\nA,a@x,east\nB,,west\nC,c@x,north\n"
    RULES = {"required": ["email"], "allowed": ["region:east", "region:west"], "patterns": ["email:[^@]+@[^@]+"]}

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "data.sqlite3"
        self.store = Store(self.database)
        self.store.initialize()

    def ingest(self, content=CONTENT, dataset="contacts", **rules):
        return self.store.import_csv(dataset, "id", content, "contacts.csv", None, **rules)

    def test_rules_are_saved_with_the_dataset_and_flag_rows(self):
        result = self.ingest(**self.RULES)
        expected = parse_rules(**self.RULES)
        self.assertEqual(result["rules"], expected)
        counts = result["issue_counts"]
        self.assertEqual((result["rejected_count"], counts["missing_value"], counts["invalid_value"]), (2, 1, 1))
        missing, = self.store.records(result["id"], issues=["missing_value"])
        self.assertEqual((missing["number"], missing["invalid_columns"], missing["failed_rules"]),
                         (2, ["email"], {"email": ["required"]}))
        invalid, = self.store.records(result["id"], issues=["invalid_value"])
        self.assertEqual((invalid["number"], invalid["failed_rules"]), (3, {"region": ["allowed"]}))
        self.assertEqual(self.store.records(result["id"])[0]["failed_rules"], {})
        self.assertEqual(self.store.get_import(result["id"])["rules"], expected)
        self.assertEqual(self.store.datasets()[0]["rules"], expected)

    def test_later_imports_reuse_saved_rules_and_cannot_change_them(self):
        self.ingest(**self.RULES)
        self.assertEqual(self.ingest(b"id,email,region\nA,,east\n")["issue_counts"]["missing_value"], 1)
        reordered = {"required": ["email"], "allowed": ["region:west", "region:east", "region:east"],
                     "patterns": ["email:[^@]+@[^@]+"]}
        self.assertEqual(self.ingest(b"id,email,region\nA,a@x,west\n", **reordered)["rejected_count"], 0)
        for rules in ({"required": ["email"]}, {**self.RULES, "allowed": ["region:east"]},
                      {**self.RULES, "required": ["email", "region"]}):
            with self.subTest(rules=rules), self.assertRaisesRegex(Conflict, "value rules cannot change"):
                self.ingest(b"id,email,region\nA,a@x,east\n", **rules)
        with self.assertRaisesRegex(InvalidInput, "Rule column 'region' is missing"):
            self.ingest(b"id,email\nA,a@x\n")
        self.assertEqual(len(self.store.imports("contacts")), 3)

    def test_bad_rules_save_nothing_and_rules_combine_with_types(self):
        for rules in ({"patterns": ["email:("]}, {"allowed": ["region"]}, {"required": ["phone"]},
                      {"required": ["id"]}):
            with self.subTest(rules=rules), self.assertRaises(InvalidInput):
                self.ingest(**rules)
        self.assertEqual(self.store.datasets(), [])
        typed = self.store.import_csv("orders", "id", b"id,qty\nA,1\nB,5\nC,\n", "orders.csv", ["qty:integer"],
                                      required=["qty"], allowed=["qty:1", "qty:2"])
        failed = [row["failed_rules"] for row in self.store.records(typed["id"], flagged=True)]
        self.assertEqual(failed, [{"qty": ["allowed"]}, {"qty": ["required"]}])
        again = self.store.import_csv("orders", "id", b"id,qty\nA,\n", "orders.csv", ["qty:integer"])
        self.assertEqual(again["issue_counts"]["missing_value"], 1)

    def test_ranges_flag_rows_and_are_fixed_like_other_rules(self):
        content = b"id,amount,due\nA,10.00,2026-03-01\nB,-5,2026-03-01\nC,20,2027-01-01\nD,,\n"
        result = self.store.import_csv("invoices", "id", content, "invoices.csv", ["amount:decimal", "due:date"],
                                       minimum=["amount:0.00"], maximum=["due:2026-12-31"])
        self.assertEqual(result["rules"]["minimum"], {"amount": "0"})
        self.assertEqual(result["rules"]["maximum"], {"due": "2026-12-31"})
        self.assertEqual((result["rejected_count"], result["issue_counts"]["invalid_value"]), (2, 2))
        failed = [(row["number"], row["failed_rules"]) for row in self.store.records(result["id"], flagged=True)]
        self.assertEqual(failed, [(2, {"amount": ["min"]}), (3, {"due": ["max"]})])
        same = self.store.import_csv("invoices", "id", b"id,amount,due\nA,1,\n", "next.csv", minimum=["amount:+0"],
                                     maximum=["due:2026-12-31"])
        self.assertEqual(same["rejected_count"], 0)
        saved = self.store.import_csv("invoices", "id", b"id,amount,due\nA,-1,\n", "again.csv")
        self.assertEqual(self.store.records(saved["id"])[0]["failed_rules"], {"amount": ["min"]})
        with self.assertRaisesRegex(Conflict, "value rules cannot change"):
            self.store.import_csv("invoices", "id", b"id,amount,due\nA,1,\n", "next.csv", minimum=["amount:1"])
        with self.assertRaisesRegex(InvalidInput, "needs a column type"):
            self.store.import_csv("other", "id", content, "other.csv", minimum=["amount:0"])
        self.assertEqual([item["name"] for item in self.store.datasets()], ["invoices"])

    def test_databases_created_before_value_rules_are_upgraded_in_place(self):
        legacy = self.database.with_name("legacy.sqlite3")
        content = b"id,qty\nA,1\nB,x\n"
        with closing(sqlite3.connect(legacy)) as connection:
            connection.executescript((Path(__file__).parent / "fixtures" / "schema-column-types.sql").read_text())
            connection.execute("""INSERT INTO datasets VALUES (1, 'orders', 'id', '{"qty": "integer"}')""")
            connection.execute("INSERT INTO imports VALUES (1, 1, 1, 'old.csv', ?, '2026-01-01T00:00:00+00:00', "
                               "'[\"id\", \"qty\"]', 2, 1, 1, ?)", (hashlib.sha256(content).hexdigest(), content))
            connection.execute("""INSERT INTO records VALUES (1, 1, 2, 'A', '["A", "1"]', '{"id": "A", "qty": "1"}',
                                  '[]', 0, '[]')""")
            connection.execute("""INSERT INTO records VALUES (1, 2, 3, 'B', '["B", "x"]', '{"id": "B", "qty": "x"}',
                                  '["invalid_value"]', 1, '["qty"]')""")
            connection.commit()
        store = Store(legacy)
        store.initialize()
        store.initialize()
        self.assertEqual(store.get_import(1)["rules"], parse_rules())
        self.assertEqual([row["failed_rules"] for row in store.records(1)], [{}, {"qty": ["type"]}])
        self.assertEqual(store.import_csv("orders", "id", content, "again.csv")["replayed"], True)
        self.assertEqual(store.import_csv("orders", "id", b"id,qty\nA,2\n", "new.csv")["rejected_count"], 0)
        with self.assertRaisesRegex(Conflict, "value rules"):
            store.import_csv("orders", "id", b"id,qty\nA,2\n", "new.csv", required=["qty"])
        fresh = store.import_csv("contacts", "id", self.CONTENT, "contacts.csv", **self.RULES)
        self.assertEqual(fresh["issue_counts"]["missing_value"], 1)
