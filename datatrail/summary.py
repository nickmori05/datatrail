"""Per-version quality and change counts for one dataset, computed in SQLite without loading snapshots."""

import json

from .errors import NotFound
from .ingest import ISSUE_CODES
from .rules import saved_rules


# Counts how keys moved between two clean imports, as `compare` would, without loading either one. Two
# records are unchanged when they have the same fields with the same values, whatever the column order:
# identical JSON text settles most pairs, and the rest are compared field by field.
CHANGE_COUNTS = """
SELECT
  (SELECT COUNT(*) FROM records n WHERE n.import_id = :after AND n.issue_count = 0 AND NOT EXISTS (
     SELECT 1 FROM records o WHERE o.import_id = :before AND o.issue_count = 0 AND o.entity_key = n.entity_key))
  AS added,
  (SELECT COUNT(*) FROM records o WHERE o.import_id = :before AND o.issue_count = 0 AND NOT EXISTS (
     SELECT 1 FROM records n WHERE n.import_id = :after AND n.issue_count = 0 AND n.entity_key = o.entity_key))
  AS removed,
  (SELECT COUNT(*) FROM records o JOIN records n
     ON n.import_id = :after AND n.issue_count = 0 AND n.entity_key = o.entity_key
   WHERE o.import_id = :before AND o.issue_count = 0 AND o.data_json IS NOT n.data_json AND (
     EXISTS (SELECT 1 FROM json_each(o.data_json) f WHERE NOT EXISTS (
       SELECT 1 FROM json_each(n.data_json) g WHERE g.key = f.key AND g.value IS f.value))
     OR EXISTS (SELECT 1 FROM json_each(n.data_json) g WHERE NOT EXISTS (
       SELECT 1 FROM json_each(o.data_json) f WHERE f.key = g.key))))
  AS changed,
  (SELECT COUNT(*) FROM records WHERE import_id = :after AND issue_count = 0) AS after_count
"""


def change_counts(connection, before_id: int, after_id: int) -> dict:
    """Added, removed, changed and unchanged key counts between two clean imports of one dataset."""
    row = connection.execute(CHANGE_COUNTS, {"before": before_id, "after": after_id}).fetchone()
    return {"added": row["added"], "removed": row["removed"], "changed": row["changed"],
            "unchanged": row["after_count"] - row["added"] - row["changed"]}


def grouped_counts(connection, column: str, identifiers: list[int]) -> dict:
    """For each import, how many rows carry each value of a stored JSON list column."""
    marks = ", ".join("?" * len(identifiers))
    counts = {}
    for row in connection.execute(
            f"""SELECT r.import_id, j.value, COUNT(*) FROM records r, json_each(r.{column}) j
                WHERE r.import_id IN ({marks}) AND r.issue_count > 0 GROUP BY r.import_id, j.value""", identifiers):
        counts.setdefault(row[0], {})[row[1]] = row[2]
    return counts


def version_summaries(connection, dataset_id: int, limit: int, offset: int) -> list[dict]:
    """One page of a dataset's versions, newest first, each with its quality counts and what changed.

    `schema` compares the header with the previous version, flagged or not. `changes` compares a clean
    version with the clean version before it and is null for flagged versions and the first clean one.
    """
    imports = [dict(row) for row in connection.execute(
        """SELECT id, version, source_name, sha256, imported_at, headers_json, row_count, accepted_count,
           rejected_count FROM imports WHERE dataset_id = ? ORDER BY version DESC LIMIT ? OFFSET ?""",
        (dataset_id, limit, offset))]
    if not imports:
        return []
    identifiers = [item["id"] for item in imports]
    issues = grouped_counts(connection, "issues_json", identifiers)
    invalid = grouped_counts(connection, "invalid_columns_json", identifiers)
    summaries = []
    for item in imports:
        headers = json.loads(item.pop("headers_json"))
        previous = connection.execute(
            "SELECT version, headers_json FROM imports WHERE dataset_id = ? AND version < ? ORDER BY version DESC "
            "LIMIT 1", (dataset_id, item["version"])).fetchone()
        schema = None
        if previous is not None:
            before = json.loads(previous["headers_json"])
            schema = {"previous_version": previous["version"], "added": sorted(set(headers) - set(before)),
                      "removed": sorted(set(before) - set(headers))}
        changes = None
        if not item["rejected_count"]:
            baseline = connection.execute(
                """SELECT id, version FROM imports WHERE dataset_id = ? AND version < ? AND rejected_count = 0
                   ORDER BY version DESC LIMIT 1""", (dataset_id, item["version"])).fetchone()
            if baseline is not None:
                changes = {"compared_with": baseline["version"],
                           **change_counts(connection, baseline["id"], item["id"])}
        found = issues.get(item["id"], {})
        summaries.append({**item, "clean": not item["rejected_count"],
                          "issue_counts": {code: found.get(code, 0) for code in ISSUE_CODES},
                          "invalid_columns": dict(sorted(invalid.get(item["id"], {}).items())),
                          "schema": schema, "changes": changes})
    return summaries


def dataset_summary(connection, dataset: str, limit: int, offset: int) -> dict:
    """A dataset's settings and version counts, with one page of `version_summaries`, or NotFound."""
    row = connection.execute(
        """SELECT d.id, d.key_column, d.column_types_json, d.rules_json, COUNT(i.id) AS import_count,
           COUNT(CASE WHEN i.rejected_count = 0 THEN 1 END) AS clean_versions, MAX(i.version) AS latest_version,
           MAX(CASE WHEN i.rejected_count = 0 THEN i.version END) AS latest_clean_version
           FROM datasets d LEFT JOIN imports i ON i.dataset_id = d.id WHERE d.name = ? GROUP BY d.id""", (dataset,)
    ).fetchone()
    if row is None:
        raise NotFound(f"Dataset '{dataset}' does not exist.")
    return {"dataset": dataset, "key_column": row["key_column"],
            "column_types": json.loads(row["column_types_json"]), "rules": saved_rules(json.loads(row["rules_json"])),
            "import_count": row["import_count"], "clean_versions": row["clean_versions"],
            "flagged_versions": row["import_count"] - row["clean_versions"], "latest_version": row["latest_version"],
            "latest_clean_version": row["latest_clean_version"],
            "versions": version_summaries(connection, row["id"], limit, offset), "limit": limit, "offset": offset}
