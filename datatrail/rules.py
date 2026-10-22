"""Optional per-dataset column rules: value types, required columns, allowed values and patterns."""

from datetime import date
import re

from .errors import InvalidInput


COLUMN_TYPES = ("integer", "decimal", "date")
PATTERNS = {
    "integer": re.compile(r"[+-]?[0-9]+"),
    "decimal": re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)"),
    "date": re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}"),
}
# The names `failed_rules` uses for each kind of check, in the order they are reported.
RULE_NAMES = ("type", "required", "allowed", "pattern")
MAX_ALLOWED_VALUES = 1000
MAX_PATTERN_LENGTH = 256


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


def split_spec(spec, form: str) -> tuple[str, str]:
    """Split COLUMN:VALUE at the first colon, so values may contain colons. Both parts must be nonempty."""
    column, separator, value = spec.partition(":") if isinstance(spec, str) else ("", "", "")
    if not separator or not column.strip() or not value.strip():
        raise InvalidInput(f"Use {form}, with a nonempty column and {form.partition(':')[2].lower()}.")
    return column.strip(), value


def specs_list(specs, label: str) -> list:
    """Reject a bare string where a list of specs is expected, which would otherwise be read letter by letter."""
    if isinstance(specs, str):
        raise InvalidInput(f"{label} must be a list.")
    return list(specs)


def parse_rules(required=(), allowed=(), patterns=()) -> dict:
    """Normalize value rules into the shape saved with a dataset, ordered so equal rule sets compare equal.

    `required` lists column names, `allowed` COLUMN:VALUE strings (one allowed value each) and `patterns`
    COLUMN:PATTERN strings. Columns, values and patterns are trimmed, since they are matched against trimmed
    cells.
    """
    columns = set()
    for column in specs_list(required, "Required columns"):
        if not isinstance(column, str) or not column.strip():
            raise InvalidInput("Required columns must be nonempty column names.")
        columns.add(column.strip())
    choices = {}
    for spec in specs_list(allowed, "Allowed values"):
        column, value = split_spec(spec, "COLUMN:VALUE")
        choices.setdefault(column, set()).add(value.strip())
        if len(choices[column]) > MAX_ALLOWED_VALUES:
            raise InvalidInput(f"Column '{column}' has more than {MAX_ALLOWED_VALUES} allowed values.")
    expressions = {}
    for spec in specs_list(patterns, "Patterns"):
        column, pattern = split_spec(spec, "COLUMN:PATTERN")
        pattern = pattern.strip()
        if column in expressions:
            raise InvalidInput(f"Column '{column}' has more than one pattern.")
        if len(pattern) > MAX_PATTERN_LENGTH:
            raise InvalidInput(f"Patterns must be at most {MAX_PATTERN_LENGTH} characters.")
        try:
            re.compile(pattern)
        except re.error as error:
            raise InvalidInput(f"Pattern for column '{column}' is not a valid regular expression: {error}.") from None
        expressions[column] = pattern
    return {"required": sorted(columns),
            "allowed": {column: sorted(values) for column, values in sorted(choices.items())},
            "patterns": dict(sorted(expressions.items()))}


def saved_rules(saved: dict) -> dict:
    """A dataset's stored rules in full shape. Datasets saved before value rules existed store {}."""
    return {"required": [], "allowed": {}, "patterns": {}, **saved}


def rule_columns(types: dict, rules: dict) -> list[str]:
    """Every column that a type or value rule refers to, by name."""
    return sorted({*types, *rules["required"], *rules["allowed"], *rules["patterns"]})


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


class Checks:
    """A dataset's types and value rules, prepared once and applied to each row's normalized cells."""

    def __init__(self, types: dict[str, str], rules: dict):
        self.types = types
        self.required = set(rules["required"])
        self.allowed = {column: frozenset(values) for column, values in rules["allowed"].items()}
        self.patterns = {column: re.compile(pattern) for column, pattern in rules["patterns"].items()}
        self.columns = rule_columns(types, rules)

    def failures(self, data: dict[str, str]) -> dict[str, list[str]]:
        """Each column whose value breaks a rule, by name, with the rules it breaks.

        An empty cell can only break `required`; types, allowed values and patterns check nonempty cells.
        """
        failed = {}
        for column in self.columns:
            value = data[column]
            if not value:
                broken = ["required"] if column in self.required else []
            else:
                broken = [name for name, passed in (
                    ("type", column not in self.types or matches(value, self.types[column])),
                    ("allowed", column not in self.allowed or value in self.allowed[column]),
                    ("pattern", column not in self.patterns or self.patterns[column].fullmatch(value)),
                ) if not passed]
            if broken:
                failed[column] = broken
        return failed
