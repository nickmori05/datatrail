from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from datatrail.__main__ import main
from datatrail.api import create_app
from datatrail.errors import InvalidInput
from datatrail.patterns import repetition_risk
from datatrail.rules import Checks, parse_rules
from datatrail.store import Store


class RepetitionRiskTests(unittest.TestCase):
    def test_single_repeats_and_small_bounded_nests_are_safe(self):
        safe = (
            r"S[0-9]{3}", r".+@.+", r"[^@]+@[^@]+", r"(?i)s[0-9]{3}", r"[0-9]+(\.[0-9]+)?",
            r"(\d{1,3}\.){3}\d{1,3}", r"(abc)+", r"(?:abc)+", r"foo|bar", r"(foo|bar)+", r"^S[0-9]{3}$",
            r"\b\w+\b", r"(?P<sku>P-[0-9]{3})", r"a{2,4}", r"(a+)?", r"(a?)?", r"(a++)+", r"(?>a+)+",
            r"S[0-9]{3}|T[0-9]{2,4}", r"(a{1,3}){1,3}", r"(a{1,4}){1,4}", r"(a{1,5}){1,5}",
        )
        for pattern in safe:
            with self.subTest(pattern=pattern):
                self.assertIsNone(repetition_risk(pattern))

    def test_nested_repetition_and_backreferences_are_rejected(self):
        nested = (
            r"(a+)+", r"(a+)+$", r"(a*)*", r"(a+)*", r"(?:a+)+", r"(a|aa)+", r"(ab|a)+", r"(a{1,3})+",
            r"(a+){2}", r"(a+){2,4}", r"(a{1,8}){1,8}", r"(a{1,6}){1,6}", r"((?:a+)+)+", r"(a?){40}",
        )
        for pattern in nested:
            with self.subTest(pattern=pattern):
                self.assertEqual(repetition_risk(pattern), "it nests repetition, as in (a+)+")
        for pattern in (r"(a)\1", r"(a|aa)\1"):
            with self.subTest(pattern=pattern):
                self.assertEqual(repetition_risk(pattern), "it contains a backreference")

    def test_an_unrecognized_construct_fails_closed(self):
        with patch("datatrail.patterns.parse_pattern", return_value=[(object(), None)]):
            self.assertEqual(repetition_risk("a"), "it uses a construct this check does not recognize")


class SavedPatternTests(unittest.TestCase):
    def test_new_patterns_are_rejected_and_ordinary_ones_are_stored_unchanged(self):
        rules = parse_rules(patterns=["code:S[0-9]{3}", "email:.+@.+"])
        self.assertEqual(rules["patterns"], {"code": "S[0-9]{3}", "email": ".+@.+"})
        for pattern, message in ((r"(a+)+", "nests repetition"), ("[A-Z", "not a valid regular expression"),
                                 (r"(a)\1", "backreference")):
            with self.subTest(pattern=pattern), self.assertRaisesRegex(InvalidInput, message):
                parse_rules(patterns=[f"code:{pattern}"])

    def test_a_pattern_already_saved_with_the_dataset_is_not_rechecked(self):
        # Rules cannot be edited, so a pattern stored before this check still has to run.
        checks = Checks({}, {"required": [], "allowed": {}, "patterns": {"code": "(a+)+"}})
        self.assertEqual(checks.failures({"code": "b"}), {"code": ["pattern"]})
        self.assertEqual(checks.failures({"code": "aa"}), {})

    def test_a_rejected_pattern_saves_nothing(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = Store(Path(directory.name) / "data.sqlite3")
        store.initialize()
        with self.assertRaisesRegex(InvalidInput, "can take far too long"):
            store.import_csv("parts", "id", b"id,code\nA,aa\n", "parts.csv", patterns=[r"code:(a+)+"])
        self.assertEqual(store.datasets(), [])
        saved = store.import_csv("parts", "id", b"id,code\nA,S001\nB,nope\n", "parts.csv",
                                 patterns=["code:S[0-9]{3}"])
        self.assertEqual(saved["rules"]["patterns"], {"code": "S[0-9]{3}"})
        flagged, = store.records(saved["id"], flagged=True)
        self.assertEqual(flagged["failed_rules"], {"code": ["pattern"]})


class PatternInterfaceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "data.sqlite3"
        self.file = Path(directory.name) / "parts.csv"
        self.file.write_bytes(b"id,code\nA,aa\n")

    def test_cli_and_api_reject_a_nested_pattern_before_saving(self):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(["--database", str(self.database), "import", "parts", str(self.file), "--key", "id",
                         "--pattern", r"code:(a+)+"])
        self.assertEqual((code, output.getvalue()), (2, ""))
        self.assertIn("can take far too long", errors.getvalue())
        client = self.enterContext(TestClient(create_app(self.database)))
        response = client.post("/datasets/parts/imports", params={"key": "id", "pattern": r"code:(a+)+"},
                               content=self.file.read_bytes(), headers={"Content-Type": "text/csv"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("nests repetition", response.json()["detail"])
        self.assertEqual(client.get("/datasets").json(), [])


if __name__ == "__main__":
    unittest.main()
