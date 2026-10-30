"""Write snapshot comparisons and flagged records as CSV."""

from collections.abc import Iterable, Iterator
import csv
import io
import json

from .rules import PATTERNS


CSV_COLUMNS = ("key", "kind", "field", "before", "after", "before_record", "after_record")
# Columns a record export writes before the record's own cells.
RECORD_COLUMNS = ("record", "line", "issues", "failed_rules")
CHUNK_SIZE = 64 * 1024
# Leading characters that make spreadsheets read a cell as a formula (OWASP's CSV injection list, plus line feed).
FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r", "\n")
BYTE_ORDER_MARK = "﻿"


def change_rows(changes: Iterable[dict]) -> Iterator[list]:
    """One row per changed field, in the order of CSV_COLUMNS. Missing values and record numbers become ""."""
    for change in changes:
        for name, values in change["fields"].items():
            yield [change["key"], change["kind"], name, *("" if value is None else value for value in (
                values["before"], values["after"], change["before_record"], change["after_record"]))]


def record_columns(headers: list[str], width: int) -> list[str]:
    """The header of a record export: RECORD_COLUMNS, the import's columns, then blank names for extra cells."""
    return [*RECORD_COLUMNS, *headers, *[""] * (width - len(headers))]


def record_rows(records: Iterable[dict], width: int) -> Iterator[list]:
    """One row per record: its number, source line, issue codes, failed rules and original cells, padded to width.

    Issue codes are separated by spaces and failed rules are compact JSON, so both survive any column name.
    """
    for record in records:
        failed = json.dumps(record["failed_rules"], ensure_ascii=False, separators=(",", ":")) \
            if record["failed_rules"] else ""
        yield [record["number"], record["source_line"], " ".join(record["issues"]), failed,
               *record["raw"], *[""] * (width - len(record["raw"]))]


def escape_formula(value):
    """Prefix an apostrophe to text a spreadsheet would run as a formula. Numbers such as -5 are left alone."""
    if isinstance(value, str) and value.startswith(FORMULA_STARTS) and not PATTERNS["decimal"].fullmatch(value):
        return "'" + value
    return value


def csv_chunks(changes: Iterable[dict], spreadsheet=False) -> Iterator[str]:
    """Changes as CSV text, one row per changed field, in chunks; see `table_chunks`."""
    return table_chunks(CSV_COLUMNS, change_rows(changes), spreadsheet)


def table_chunks(columns, rows: Iterable[list], spreadsheet=False) -> Iterator[str]:
    """The header and rows as RFC 4180 text with CRLF line endings, in chunks of about CHUNK_SIZE characters.

    With `spreadsheet`, the text starts with a byte order mark and formula-like cells are escaped, so
    spreadsheet programs detect UTF-8 and show those values as text. Rows are consumed as the chunks
    are, so only one chunk is held in memory at a time.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)

    def flush():
        text = buffer.getvalue()
        buffer.seek(0)
        buffer.truncate()
        return text

    if spreadsheet:
        buffer.write(BYTE_ORDER_MARK)
        columns = [escape_formula(name) for name in columns]
    writer.writerow(columns)
    for row in rows:
        writer.writerow([escape_formula(cell) for cell in row] if spreadsheet else row)
        if buffer.tell() >= CHUNK_SIZE:
            yield flush()
    yield flush()
