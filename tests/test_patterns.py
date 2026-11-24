import unittest
from unittest.mock import patch

from datatrail.patterns import repetition_risk


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


if __name__ == "__main__":
    unittest.main()
