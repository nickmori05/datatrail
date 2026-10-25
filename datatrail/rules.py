"""Optional per-dataset column rules: value types, required columns, allowed values, patterns and ranges."""

from datetime import date
from decimal import Context, Decimal
import re

from .errors import InvalidInput


COLUMN_TYPES = ("integer", "decimal", "date")
PATTERNS = {
    "integer": re.compile(r"[+-]?[0-9]+"),
    "decimal": re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)"),
    "date": re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}"),
}
# The names `failed_rules` uses for each kind of check, in the order they are reported.
RULE_NAMES = ("type", "required", "allowed", "pattern", "min", "max")
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


def parse_bounds(specs, label: str) -> dict[str, str]:
    """Turn COLUMN:VALUE bound strings into a trimmed column-to-bound mapping, one bound per column."""
    bounds = {}
    for spec in specs_list(specs, f"{label.capitalize()} values"):
        column, value = split_spec(spec, "COLUMN:VALUE")
        if column in bounds:
            raise InvalidInput(f"Column '{column}' has more than one {label}.")
        bounds[column] = value.strip()
    return dict(sorted(bounds.items()))


def parse_rules(required=(), allowed=(), patterns=(), minimum=(), maximum=()) -> dict:
    """Normalize value rules into the shape saved with a dataset, ordered so equal rule sets compare equal.

    `required` lists column names, `allowed` COLUMN:VALUE strings (one allowed value each), `patterns`
    COLUMN:PATTERN strings, and `minimum` and `maximum` COLUMN:VALUE inclusive bounds. Columns, values and
    patterns are trimmed, since they are matched against trimmed cells. Bounds are only checked against
    column types by `check_ranges`, once the dataset's types are known.
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
            "patterns": dict(sorted(expressions.items())),
            "minimum": parse_bounds(minimum, "minimum"), "maximum": parse_bounds(maximum, "maximum")}


def saved_rules(saved: dict) -> dict:
    """A dataset's stored rules in full shape. Datasets saved before value rules or ranges existed lack keys."""
    return {"required": [], "allowed": {}, "patterns": {}, "minimum": {}, "maximum": {}, **saved}


def rule_columns(types: dict, rules: dict) -> list[str]:
    """Every column that a type or value rule refers to, by name."""
    return sorted({*types, *rules["required"], *rules["allowed"], *rules["patterns"], *rules["minimum"],
                   *rules["maximum"]})


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


def ordered(value: str, kind: str) -> Decimal | date:
    """A valid value of a column type as something that compares by size: a Decimal, or a date."""
    return date.fromisoformat(value) if kind == "date" else Decimal(value)


def canonical(value: str, kind: str) -> str:
    """One spelling for each bound, so `0`, `+0` and `0.00` save as the same rule."""
    if kind == "date":
        return value
    number = Decimal(value)
    # Enough precision that dropping trailing zeros never rounds a long bound.
    return "0" if number == 0 else format(number.normalize(Context(prec=len(value))), "f")


def check_ranges(types: dict[str, str], rules: dict) -> dict:
    """Check each minimum and maximum against its column's type and return the rules with canonical bounds.

    A bound needs an integer, decimal or date column and must be a valid value of that type, and a column's
    minimum cannot be above its maximum. Raises InvalidInput otherwise.
    """
    bounds = {}
    for side in ("minimum", "maximum"):
        bounds[side] = {}
        for column, value in rules[side].items():
            kind = types.get(column)
            if kind is None:
                raise InvalidInput(f"Column '{column}' needs a column type ({', '.join(COLUMN_TYPES)}) to have a "
                                   f"{side}.")
            if not matches(value, kind):
                raise InvalidInput(f"The {side} for column '{column}' must be a valid {kind}, not '{value}'.")
            bounds[side][column] = canonical(value, kind)
    for column, low in bounds["minimum"].items():
        high = bounds["maximum"].get(column)
        if high is not None and ordered(low, types[column]) > ordered(high, types[column]):
            raise InvalidInput(f"The minimum for column '{column}' is above its maximum.")
    return {**rules, **bounds}


class Checks:
    """A dataset's types and value rules, prepared once and applied to each row's normalized cells."""

    def __init__(self, types: dict[str, str], rules: dict):
        rules = check_ranges(types, rules)
        self.types = types
        self.required = set(rules["required"])
        self.allowed = {column: frozenset(values) for column, values in rules["allowed"].items()}
        self.patterns = {column: re.compile(pattern) for column, pattern in rules["patterns"].items()}
        self.minimum = {column: ordered(value, types[column]) for column, value in rules["minimum"].items()}
        self.maximum = {column: ordered(value, types[column]) for column, value in rules["maximum"].items()}
        self.columns = rule_columns(types, rules)

    def failures(self, data: dict[str, str]) -> dict[str, list[str]]:
        """Each column whose value breaks a rule, by name, with the rules it breaks.

        An empty cell can only break `required`; types, allowed values, patterns and ranges check nonempty
        cells. Ranges are only checked for values that are valid for the column's type.
        """
        failed = {}
        for column in self.columns:
            value = data[column]
            if not value:
                broken = ["required"] if column in self.required else []
            else:
                typed = column not in self.types or matches(value, self.types[column])
                ranged = column in self.minimum or column in self.maximum
                size = ordered(value, self.types[column]) if typed and ranged else None
                broken = [name for name, passed in (
                    ("type", typed),
                    ("allowed", column not in self.allowed or value in self.allowed[column]),
                    ("pattern", column not in self.patterns or self.patterns[column].fullmatch(value)),
                    ("min", size is None or column not in self.minimum or size >= self.minimum[column]),
                    ("max", size is None or column not in self.maximum or size <= self.maximum[column]),
                ) if not passed]
            if broken:
                failed[column] = broken
        return failed
