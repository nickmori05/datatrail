from collections import Counter
import csv
from dataclasses import dataclass
import io

from .errors import InvalidInput


MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 10_000
MAX_COLUMNS = 64


@dataclass
class ParsedRow:
    number: int
    line: int
    key: str | None
    raw: list[str]
    data: dict[str, str] | None
    issues: list[str]


def parse_csv(content: bytes, key_column: str) -> tuple[list[str], list[ParsedRow]]:
    if len(content) > MAX_BYTES:
        raise InvalidInput("CSV files must be at most 5 MiB.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise InvalidInput("CSV files must use UTF-8 encoding.") from error
    if "\x00" in text:
        raise InvalidInput("CSV files must not contain null characters.")
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    rows = []
    try:
        headers = [value.strip() for value in next(reader, [])]
        if not headers or len(headers) > MAX_COLUMNS or any(not name or len(name) > 128 for name in headers):
            raise InvalidInput("Use 1–64 nonempty column names, each at most 128 characters.")
        if len(set(headers)) != len(headers):
            raise InvalidInput("Column names must be unique after trimming whitespace.")
        if key_column not in headers:
            raise InvalidInput(f"Key column '{key_column}' is missing.")
        while True:
            line = reader.line_num + 1
            raw = next(reader, None)
            if raw is None:
                break
            if not raw:
                continue
            if len(rows) >= MAX_ROWS:
                raise InvalidInput("CSV files must contain at most 10000 data records.")
            issues = []
            data = None
            key = None
            if len(raw) != len(headers):
                issues.append("column_count")
            else:
                data = dict(zip(headers, (value.strip() for value in raw)))
                key = data[key_column]
                if not key:
                    issues.append("missing_key")
            rows.append(ParsedRow(len(rows) + 1, line, key, raw, data, issues))
    except csv.Error as error:
        raise InvalidInput(f"Invalid CSV near line {reader.line_num}: {error}") from error
    counts = Counter(row.key for row in rows if row.key)
    for row in rows:
        if row.key and counts[row.key] > 1:
            row.issues.append("duplicate_key")
    return headers, rows
