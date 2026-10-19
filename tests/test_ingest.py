import unittest
from unittest.mock import patch

from datatrail.errors import InvalidInput
from datatrail.ingest import MAX_BYTES, parse_csv
from datatrail.rules import parse_column_types


class ParsingTests(unittest.TestCase):
    def test_preserves_raw_values_and_normalizes_surrounding_whitespace(self):
        headers, rows = parse_csv(b" id , name\r\n 001 , Ada \r\n", "id")
        self.assertEqual(headers, ["id", "name"])
        self.assertEqual(rows[0].raw, [" 001 ", " Ada "])
        self.assertEqual(rows[0].data, {"id": "001", "name": "Ada"})
        self.assertEqual(rows[0].key, "001")

    def test_flags_every_duplicate_instead_of_picking_a_winner(self):
        _, rows = parse_csv(b"id,name\nA,first\n A ,second\nB,third\n", "id")
        self.assertEqual([row.issues for row in rows], [["duplicate_key"], ["duplicate_key"], []])

    def test_missing_keys_and_mismatched_columns_keep_original_cells(self):
        _, rows = parse_csv(b"id,name\n ,blank\nB\nC,extra,cell\n", "id")
        self.assertEqual([row.issues for row in rows], [["missing_key"], ["column_count"], ["column_count"]])
        self.assertEqual(rows[2].raw, ["C", "extra", "cell"])
        self.assertIsNone(rows[2].data)

    def test_line_numbers_account_for_multiline_fields_and_blank_lines(self):
        _, rows = parse_csv(b'id,note\nA,"first\nsecond"\n\nB,last\n', "id")
        self.assertEqual([(row.number, row.line) for row in rows], [(1, 2), (2, 5)])
        self.assertEqual(rows[0].data["note"], "first\nsecond")

    def test_utf8_bom_and_unicode_values(self):
        _, rows = parse_csv("\ufeffid,name\n1,Renée\n".encode(), "id")
        self.assertEqual(rows[0].data["name"], "Renée")

    def test_header_only_file_is_an_empty_snapshot(self):
        self.assertEqual(parse_csv(b"id,name\n", "id"), (["id", "name"], []))

    def test_invalid_headers_encoding_and_quoting_are_rejected(self):
        for content in (b"", b"id,id\nA,B", b"id, id \nA,B", b"id,\nA,B", b"name\nAda",
                        b'id,name\nA,"unfinished', b"id,name\nA,\xff", b"id,name\nA,\x00"):
            with self.subTest(content=content), self.assertRaises(InvalidInput):
                parse_csv(content, "id")

    def test_size_and_record_limits_are_enforced(self):
        with self.assertRaisesRegex(InvalidInput, "5 MiB"):
            parse_csv(b"x" * (MAX_BYTES + 1), "id")
        with patch("datatrail.ingest.MAX_ROWS", 2), self.assertRaisesRegex(InvalidInput, "records"):
            parse_csv(b"id\nA\nB\nC\n", "id")


class ColumnTypeTests(unittest.TestCase):
    def test_typed_columns_flag_rows_with_values_that_do_not_match(self):
        content = (b"id,qty,price,due\nA,3,9.50,2026-01-31\nB,-4,+.5,2024-02-29\nC,,,\n"
                   b"D,3.0,1e3,2026-02-30\nE,x,9.5.1,01/02/2026\nF,\xd9\xa3,1,2026-1-2\n")
        types = {"qty": "integer", "price": "decimal", "due": "date"}
        _, rows = parse_csv(content, "id", types)
        self.assertEqual([row.invalid for row in rows], [
            [], [], [], ["qty", "price", "due"], ["qty", "price", "due"], ["qty", "due"]])
        self.assertEqual([row.issues for row in rows], [[], [], [], *[["invalid_value"]] * 3])

    def test_type_checks_use_normalized_cells_and_combine_with_other_issues(self):
        _, rows = parse_csv(b"id,qty\n 7 , 2 \n7,two\n8\n,x\n", "id", {"id": "integer", "qty": "integer"})
        self.assertEqual([row.issues for row in rows], [
            ["duplicate_key"], ["duplicate_key", "invalid_value"], ["column_count"], ["missing_key", "invalid_value"]])
        self.assertEqual([row.invalid for row in rows], [[], ["qty"], [], ["qty"]])

    def test_typed_columns_must_exist_in_the_header(self):
        with self.assertRaisesRegex(InvalidInput, "Typed column 'amount' is missing"):
            parse_csv(b"id,name\nA,Ada\n", "id", {"amount": "decimal"})


class ColumnTypeSpecTests(unittest.TestCase):
    def test_specs_are_parsed_by_the_last_colon_and_sorted(self):
        self.assertEqual(parse_column_types(["total:decimal", " a:b :integer"]), {"a:b": "integer", "total": "decimal"})
        self.assertEqual(parse_column_types([]), {})

    def test_malformed_unknown_and_repeated_specs_are_rejected(self):
        for specs in (["amount"], [":integer"], ["amount:money"], ["amount:Integer"], [3], "amount:decimal"):
            with self.subTest(specs=specs), self.assertRaisesRegex(InvalidInput, "COLUMN:TYPE"):
                parse_column_types(specs)
        with self.assertRaisesRegex(InvalidInput, "more than one type"):
            parse_column_types(["amount:decimal", "amount :integer"])
