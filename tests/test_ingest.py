import unittest
from unittest.mock import patch

from datatrail.errors import InvalidInput
from datatrail.ingest import MAX_BYTES, parse_csv


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
