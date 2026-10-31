"""Parse CSV bytes and classify each record before anything is stored."""

from collections import Counter
import csv
from dataclasses import dataclass, field
import io

from .errors import InvalidInput
from .rules import Checks, saved_rules


MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 50_000
MAX_COLUMNS = 64
ISSUE_CODES = ("column_count", "missing_key", "duplicate_key", "invalid_value", "missing_value")
# Field separators by the name used on the command line and in the API, where ";" and tabs are awkward to type.
DELIMITERS = {"comma": ",", "semicolon": ";", "tab": "\t", "pipe": "|"}


@dataclass
class ParsedRow:
    """One CSV record: original cells, trimmed values when the shape is valid, and its issue codes.

    `failed` maps each column that breaks a type or value rule to the rules it breaks; `invalid` lists those
    columns by name.
    """

    number: int
    line: int
    key: str | None
    raw: list[str]
    data: dict[str, str] | None
    issues: list[str]
    failed: dict[str, list[str]] = field(default_factory=dict)

    @property
    def invalid(self) -> list[str]:
        """Columns that break a rule, by name."""
        return list(self.failed)


def delimiter_hint(headers: list[str], delimiter: str) -> str:
    """A suggestion when the whole header was read as one column but contains another delimiter, else ""."""
    if len(headers) != 1:
        return ""
    others = [name for name, character in DELIMITERS.items() if name != delimiter and character in headers[0]]
    if not others:
        return ""
    return (" The header was read as one column; if the file uses another separator, set the delimiter to "
            f"{' or '.join(others)}.")


def parse_csv(content: bytes, key_column: str, column_types: dict[str, str] | None = None,
              rules: dict | None = None, delimiter: str = "comma") -> tuple[list[str], list[ParsedRow]]:
    """Parse a whole CSV file and flag row-level problems.

    Returns the trimmed header and every nonblank record in source order. Problems that make the
    file unusable (encoding, quoting, limits, header, missing key, typed or rule column) raise InvalidInput.
    `delimiter` names the field separator, one of DELIMITERS; quoting works the same with any of them.
    """
    if delimiter not in DELIMITERS:
        raise InvalidInput(f"Delimiters must be one of {', '.join(DELIMITERS)}.")
    column_types = column_types or {}
    rules = saved_rules(rules or {})
    if key_column in rules["required"]:
        raise InvalidInput("The key column is always required; leave it out of the required columns.")
    checks = Checks(column_types, rules)
    if len(content) > MAX_BYTES:
        raise InvalidInput(f"CSV files must be at most {MAX_BYTES // (1024 * 1024)} MiB.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise InvalidInput("CSV files must use UTF-8 encoding.") from error
    if "\x00" in text:
        raise InvalidInput("CSV files must not contain null characters.")
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=DELIMITERS[delimiter], strict=True)
    rows = []
    try:
        headers = [value.strip() for value in next(reader, [])]
        if not headers or len(headers) > MAX_COLUMNS or any(not name or len(name) > 128 for name in headers):
            raise InvalidInput(f"Use 1–{MAX_COLUMNS} nonempty column names, each at most 128 characters.")
        if len(set(headers)) != len(headers):
            raise InvalidInput("Column names must be unique after trimming whitespace.")
        if key_column not in headers:
            raise InvalidInput(f"Key column '{key_column}' is missing.{delimiter_hint(headers, delimiter)}")
        for column in checks.columns:
            if column not in headers:
                kind = "Typed" if column in column_types else "Rule"
                raise InvalidInput(f"{kind} column '{column}' is missing.")
        while True:
            line = reader.line_num + 1
            raw = next(reader, None)
            if raw is None:
                break
            if not raw:
                continue
            if len(rows) >= MAX_ROWS:
                raise InvalidInput(f"CSV files must contain at most {MAX_ROWS} data records.")
            issues = []
            data = None
            key = None
            failed = {}
            if len(raw) != len(headers):
                issues.append("column_count")
            else:
                data = dict(zip(headers, (value.strip() for value in raw), strict=True))
                key = data[key_column]
                if not key:
                    issues.append("missing_key")
                failed = checks.failures(data)
            rows.append(ParsedRow(len(rows) + 1, line, key, raw, data, issues, failed))
    except csv.Error as error:
        raise InvalidInput(f"Invalid CSV near line {reader.line_num}: {error}") from error
    counts = Counter(row.key for row in rows if row.key)
    for row in rows:
        if row.key and counts[row.key] > 1:
            row.issues.append("duplicate_key")
        broken = {rule for rules in row.failed.values() for rule in rules}
        if broken - {"required"}:
            row.issues.append("invalid_value")
        if "required" in broken:
            row.issues.append("missing_value")
    return headers, rows
