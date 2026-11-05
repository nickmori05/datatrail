"""Check saved imports against their original bytes: the hash, and what parsing those bytes again produces."""

from contextlib import closing
import hashlib
import json

from .errors import InvalidInput
from .ingest import parse_csv


# Record numbers listed for an import whose stored records differ from a fresh parse; the count covers all.
MAX_EXAMPLES = 10


def stored_records(connect, identifier: int, batch: int):
    """An import's stored records in number order, as tuples shaped like `parsed_record`, `batch` rows a query.

    Each batch uses its own short connection, so checking a large import never holds a lock that would make an
    import wait.
    """
    after = 0
    while True:
        with closing(connect()) as connection:
            rows = connection.execute(
                """SELECT number, source_line, entity_key, raw_json, data_json, issues_json, invalid_columns_json,
                   failed_rules_json FROM records WHERE import_id = ? AND number > ? ORDER BY number LIMIT ?""",
                (identifier, after, batch)).fetchall()
        for row in rows:
            yield (row["number"], row["source_line"], row["entity_key"], json.loads(row["raw_json"]),
                   json.loads(row["data_json"]) if row["data_json"] is not None else None,
                   json.loads(row["issues_json"]), json.loads(row["invalid_columns_json"]),
                   json.loads(row["failed_rules_json"]))
        if len(rows) < batch:
            return
        after = rows[-1]["number"]


def parsed_record(row) -> tuple:
    """A freshly parsed row in the shape `stored_records` yields."""
    return row.number, row.line, row.key, row.raw, row.data, row.issues, row.invalid, row.failed


def record_problems(stored, parsed) -> dict | None:
    """Compare stored records with parsed ones by record number; a problem entry if any differ."""
    parsed = {row[0]: row for row in parsed}
    seen = set()
    mismatched = []
    for row in stored:
        seen.add(row[0])
        if parsed.get(row[0]) != row:
            mismatched.append(row[0])
    # Records the file has but the database lost.
    mismatched += [number for number in parsed if number not in seen]
    if not mismatched:
        return None
    mismatched.sort()
    return {"check": "records", "detail": f"{len(mismatched)} stored records differ from the source file.",
            "count": len(mismatched), "records": mismatched[:MAX_EXAMPLES]}


def verify_import(connect, saved: dict, source: bytes, key_column: str, types: dict, rules: dict,
                  batch: int) -> list[dict]:
    """Every way one saved import disagrees with its source bytes; an empty list means it verified.

    `saved` holds the import's stored `sha256`, `headers` and record counts. The source is parsed again with the
    dataset's key column, types and value rules, which cannot change, so a faithful import parses identically.
    """
    problems = []
    digest = hashlib.sha256(source).hexdigest()
    if digest != saved["sha256"]:
        problems.append({"check": "sha256", "detail": f"The source bytes hash to {digest}, not the saved SHA-256."})
    try:
        headers, rows = parse_csv(source, key_column, types, rules)
    except InvalidInput as error:
        return [*problems, {"check": "parse", "detail": f"The source file no longer imports: {error}"}]
    if headers != saved["headers"]:
        problems.append({"check": "headers", "detail": f"The source header is {headers}, not {saved['headers']}."})
    accepted = sum(not row.issues for row in rows)
    counts = {"row_count": len(rows), "accepted_count": accepted, "rejected_count": len(rows) - accepted}
    different = {name: {"saved": saved[name], "source": value} for name, value in counts.items()
                 if saved[name] != value}
    if different:
        problems.append({"check": "counts", "detail": "Saved record counts differ from the source file.",
                         "counts": different})
    problem = record_problems(stored_records(connect, saved["id"], batch), map(parsed_record, rows))
    if problem:
        problems.append(problem)
    return problems
