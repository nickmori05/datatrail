"""Reject value-rule patterns that can stall an import by backtracking.

Python's `re` has no time limit. A pattern such as `(a+)+` tries every way to split a value before deciding it
does not match, and on a long cell that holds up the whole import. `repetition_risk` reads the standard
library's own parse tree and reports that shape before any value is checked. A repeat of one piece, such as
`S[0-9]{3}` or `[0-9]+(\\.[0-9]+)?`, is left alone.
"""

def _parser():
    """The standard-library regex parser and its opcode module.

    3.12 and later keep them in `re`; 3.11 still exposes the same objects under the `sre_*` names.
    """
    try:
        import re._constants as constants
        from re._parser import parse as parse_pattern
    except ImportError:
        import sre_constants as constants
        from sre_parse import parse as parse_pattern
    return constants, parse_pattern


_CONSTANTS, parse_pattern = _parser()


def _opcodes(*names):
    """Opcodes that exist in this Python, ignoring names an older release does not define."""
    return frozenset(opcode for name in names if (opcode := getattr(_CONSTANTS, name, None)) is not None)


# A nest whose upper bounds multiply past this still fans out. 32 leaves room for an address-shaped pattern
# such as (\d{1,3}\.){3} and rejects nests such as (a{1,8}){1,8}.
_BOUND = 32
_UNBOUNDED = object()
_LEAVES = _opcodes(
    "ANY", "ANY_ALL", "AT", "CATEGORY", "IN", "IN_IGNORE", "IN_LOC_IGNORE", "IN_UNI_IGNORE", "LITERAL",
    "LITERAL_IGNORE", "LITERAL_LOC_IGNORE", "LITERAL_UNI_IGNORE", "NOT_LITERAL", "NOT_LITERAL_IGNORE",
    "NOT_LITERAL_LOC_IGNORE", "NOT_LITERAL_UNI_IGNORE",
)
_BACKREFS = _opcodes(
    "GROUPREF", "GROUPREF_EXISTS", "GROUPREF_IGNORE", "GROUPREF_LOC_IGNORE", "GROUPREF_UNI_IGNORE",
)
_REPEATS = _opcodes("MAX_REPEAT", "MIN_REPEAT")
_MAXREPEAT = _CONSTANTS.MAXREPEAT
_POSSESSIVE = _CONSTANTS.POSSESSIVE_REPEAT
_BRANCH = _CONSTANTS.BRANCH
_SUBPATTERN = _CONSTANTS.SUBPATTERN
_ASSERT = _CONSTANTS.ASSERT
_ASSERT_NOT = _CONSTANTS.ASSERT_NOT
_ATOMIC = _CONSTANTS.ATOMIC_GROUP
_NESTED = "it nests repetition, as in (a+)+"
_BACKREF = "it contains a backreference"
_UNKNOWN = "it uses a construct this check does not recognize"


def _over(container, upper) -> bool:
    """Whether nesting one more repeat under `container` fans out past `_BOUND`. An unlimited repeat always does."""
    if container is _UNBOUNDED or upper is _UNBOUNDED:
        return True
    return container * max(upper, 1) > _BOUND


def _upper(value):
    """The repeat's maximum as an int, or `_UNBOUNDED` when the pattern puts no ceiling on it."""
    return _UNBOUNDED if value == _MAXREPEAT else value


def _walk(node, container):
    """The first risk inside `node`, or None.

    `container` is the product of the upper bounds of the backtracking repeats wrapped around this node, or
    `_UNBOUNDED` when one of them has no ceiling. None means this node is not inside such a repeat. Possessive
    repeats and atomic groups do not give characters back, so they do not add to that product; a risk inside
    them is still reported.
    """
    for op, argument in node:
        if op in _LEAVES:
            continue
        if op in _BACKREFS:
            return _BACKREF
        if op in _REPEATS:
            upper = _upper(argument[1])
            if container is not None and _over(container, upper):
                return _NESTED
            if upper is _UNBOUNDED:
                child = _UNBOUNDED
            elif upper > 1:
                child = upper if container is None else container * upper
            else:
                child = container
            risk = _walk(argument[2], child)
        elif op is _POSSESSIVE:
            risk = _walk(argument[2], container)
        elif op is _BRANCH:
            branches = argument[1]
            # An empty alternative is the optional `?` the parser rewrites `(a|aa)` into.
            if container is not None and any(len(branch) == 0 for branch in branches) and _over(container, 1):
                return _NESTED
            risk = None
            for branch in branches:
                risk = _walk(branch, container)
                if risk:
                    break
        elif op is _SUBPATTERN:
            risk = _walk(argument[-1], container)
        elif op in (_ASSERT, _ASSERT_NOT):
            # The assertion is checked on its own; a safe lookahead does not multiply with the parent repeat.
            risk = _walk(argument[1], None)
        elif op is _ATOMIC:
            risk = _walk(argument, None)
        else:
            return _UNKNOWN
        if risk:
            return risk
    return None


def repetition_risk(pattern: str) -> str | None:
    """Why `pattern` can take far too long on some values, or None when it cannot.

    The pattern must already be one `re` accepts. Nested repetition and backreferences are reported; anything
    the parse tree uses that this check does not know is reported too, so a future syntax fails closed.
    """
    return _walk(parse_pattern(pattern), None)
