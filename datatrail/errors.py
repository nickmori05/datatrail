"""Domain errors shared by the store, CLI and API.

The CLI reports each one with exit code 2; the API maps them to HTTP statuses.
"""


class InvalidInput(ValueError):
    """The request or file is malformed (HTTP 400)."""


class NotFound(LookupError):
    """A dataset, import or version does not exist (HTTP 404)."""


class Conflict(ValueError):
    """The request contradicts saved state, such as a dataset's key column (HTTP 409)."""
