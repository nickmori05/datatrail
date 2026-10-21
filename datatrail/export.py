"""Write snapshot comparisons as CSV, one row per changed field."""

from collections.abc import Iterable, Iterator
import csv
import io


CSV_COLUMNS = ("key", "kind", "field", "before", "after", "before_record", "after_record")
CHUNK_SIZE = 64 * 1024


def change_rows(changes: Iterable[dict]) -> Iterator[list]:
    """One row per changed field, in the order of CSV_COLUMNS. Missing values and record numbers become ""."""
    for change in changes:
        for name, values in change["fields"].items():
            yield [change["key"], change["kind"], name, *("" if value is None else value for value in (
                values["before"], values["after"], change["before_record"], change["after_record"]))]


def csv_chunks(changes: Iterable[dict]) -> Iterator[str]:
    """The header and rows as RFC 4180 text with CRLF line endings, in chunks of about CHUNK_SIZE characters.

    Changes are consumed as the chunks are, so only one chunk is held in memory at a time.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)

    def flush():
        text = buffer.getvalue()
        buffer.seek(0)
        buffer.truncate()
        return text

    writer.writerow(CSV_COLUMNS)
    for row in change_rows(changes):
        writer.writerow(row)
        if buffer.tell() >= CHUNK_SIZE:
            yield flush()
    yield flush()
