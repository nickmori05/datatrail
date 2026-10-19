"""Optional per-dataset column types and the values each one accepts."""

from datetime import date
import re

from .errors import InvalidInput


COLUMN_TYPES = ("integer", "decimal", "date")
PATTERNS = {
    "integer": re.compile(r"[+-]?[0-9]+"),
    "decimal": re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)"),
    "date": re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}"),
}


def parse_column_types(specs) -> dict[str, str]:
    """Turn COLUMN:TYPE strings into a column-to-type mapping ordered by column name."""
    if isinstance(specs, str):
        raise InvalidInput("Column types must be a list of COLUMN:TYPE values.")
    types = {}
    for spec in specs:
        column, separator, kind = spec.rpartition(":") if isinstance(spec, str) else ("", "", "")
        column = column.strip()
        if not separator or not column or kind not in COLUMN_TYPES:
            raise InvalidInput(f"Column types must look like COLUMN:TYPE, where TYPE is {', '.join(COLUMN_TYPES)}.")
        if column in types:
            raise InvalidInput(f"Column '{column}' has more than one type.")
        types[column] = kind
    return dict(sorted(types.items()))


def matches(value: str, kind: str) -> bool:
    """Whether a normalized, nonempty cell is a valid value of the given type."""
    if not PATTERNS[kind].fullmatch(value):
        return False
    if kind == "date":
        try:
            date.fromisoformat(value)
        except ValueError:
            return False
    return True


def invalid_columns(data: dict[str, str], types: dict[str, str]) -> list[str]:
    """Typed columns whose value does not match; empty cells are not checked."""
    return [column for column, kind in types.items() if data[column] and not matches(data[column], kind)]
