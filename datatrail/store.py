"""SQLite storage for datasets, imports and records, and the queries that compare them."""

from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from .compare import compare, iter_changes, key_history
from .errors import Conflict, InvalidInput, NotFound
from .ingest import ISSUE_CODES, parse_csv
from .rules import parse_column_types


CHANGE_KINDS = ("added", "removed", "changed")
DEFAULT_DATABASE = ".local/datatrail.sqlite3"
IMPORT_FIELDS = """i.id, d.name AS dataset, d.key_column, d.column_types_json, i.version, i.source_name,
    i.sha256, i.imported_at, i.headers_json, i.row_count, i.accepted_count, i.rejected_count"""
# Columns added after the first release. Older databases gain them, with their defaults, on startup.
ADDED_COLUMNS = (("datasets", "column_types_json", "TEXT NOT NULL DEFAULT '{}'"),
                 ("records", "invalid_columns_json", "TEXT NOT NULL DEFAULT '[]'"))


def default_database() -> Path:
    """The database path from DATATRAIL_DB, or the local default relative to the working directory."""
    return Path(os.environ.get("DATATRAIL_DB", DEFAULT_DATABASE))


def check_page(limit: int, offset: int):
    """Reject pagination values outside the documented bounds."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise InvalidInput("Limit must be between 1 and 1000.")
    if type(offset) is not int or offset < 0:
        raise InvalidInput("Offset must be a nonnegative integer.")


def change_kinds(kinds) -> list[str]:
    """Validate requested change kinds; none means all of them. Returns them in canonical order."""
    if not kinds:
        return list(CHANGE_KINDS)
    if any(kind not in CHANGE_KINDS for kind in kinds):
        raise InvalidInput("Change kinds must be added, removed or changed.")
    return [kind for kind in CHANGE_KINDS if kind in kinds]


def issue_codes(codes) -> list[str]:
    """Validate requested issue codes and return them in canonical order."""
    if any(code not in ISSUE_CODES for code in codes):
        raise InvalidInput(f"Issue codes must be one of {', '.join(ISSUE_CODES)}.")
    return [code for code in ISSUE_CODES if code in codes]


def import_dict(row) -> dict:
    """Turn an import row into its JSON shape."""
    result = dict(row)
    result["headers"] = json.loads(result.pop("headers_json"))
    result["column_types"] = json.loads(result.pop("column_types_json"))
    return result


def record_dict(row) -> dict:
    """Turn a record row into its JSON shape, decoding the stored JSON columns."""
    result = dict(row)
    for name in ("raw", "data", "issues", "invalid_columns"):
        value = result.pop(f"{name}_json")
        result[name] = json.loads(value) if value is not None else None
    result.pop("issue_count", None)
    return result


class Store:
    """All reads and writes for one SQLite database file. Each call opens its own connection."""

    def __init__(self, database: Path):
        self.database = Path(database)

    def connect(self):
        """Open a connection with row access by column name and foreign keys enforced."""
        connection = sqlite3.connect(self.database, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self):
        """Create the database and tables if needed, and add columns missing from older databases."""
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection:
            connection.executescript(Path(__file__).with_name("schema.sql").read_text())
            connection.execute("BEGIN IMMEDIATE")
            for table, column, definition in ADDED_COLUMNS:
                if column not in {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            connection.commit()

    def _import(self, connection, identifier: int) -> dict:
        """Import details with per-code issue counts, or NotFound."""
        row = connection.execute(
            f"SELECT {IMPORT_FIELDS} FROM imports i JOIN datasets d ON d.id = i.dataset_id WHERE i.id = ?",
            (identifier,),
        ).fetchone()
        if row is None:
            raise NotFound(f"Import {identifier} does not exist.")
        result = import_dict(row)
        # Break flagged rows down by issue code; a row counts once for each code it carries.
        counts = dict(connection.execute(
            """SELECT j.value, COUNT(*) FROM records r, json_each(r.issues_json) j
               WHERE r.import_id = ? AND r.issue_count > 0 GROUP BY j.value""", (identifier,)
        ).fetchall())
        result["issue_counts"] = {code: counts.get(code, 0) for code in ISSUE_CODES}
        return result

    def import_csv(self, dataset: str, key_column: str, content: bytes, source_name: str, column_types=None) -> dict:
        """Save a CSV file as the dataset's next version, or return the existing import for identical bytes.

        The dataset is created on first use with this key column and column types; later imports must match
        them. The result includes `replayed`, which is true for an exact retry.
        """
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", dataset):
            raise InvalidInput("Dataset names must be 1–64 letters, digits, hyphens or underscores, "
                               "starting with a letter or digit.")
        if not source_name or len(source_name) > 255 or any(ord(char) < 32 for char in source_name):
            raise InvalidInput("Source name must be 1–255 characters without control characters.")
        # Omitted column types mean "use the dataset's saved types"; a new dataset then has none.
        types = parse_column_types(column_types) if column_types else None
        if types is None:
            with closing(self.connect()) as connection:
                saved = connection.execute(
                    "SELECT column_types_json FROM datasets WHERE name = ?", (dataset,)
                ).fetchone()
            types = json.loads(saved["column_types_json"]) if saved else {}
        headers, rows = parse_csv(content, key_column, types)
        digest = hashlib.sha256(content).hexdigest()
        accepted = sum(not row.issues for row in rows)
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM datasets WHERE name = ?", (dataset,)).fetchone()
            if existing is None:
                dataset_id = connection.execute(
                    "INSERT INTO datasets (name, key_column, column_types_json) VALUES (?, ?, ?)",
                    (dataset, key_column, json.dumps(types)),
                ).lastrowid
            else:
                if existing["key_column"] != key_column:
                    raise Conflict("A dataset's key column cannot change. Use a new dataset name.")
                if json.loads(existing["column_types_json"]) != types:
                    raise Conflict("A dataset's column types cannot change. Use a new dataset name.")
                dataset_id = existing["id"]
            previous = connection.execute(
                "SELECT id FROM imports WHERE dataset_id = ? AND sha256 = ?", (dataset_id, digest)
            ).fetchone()
            if previous is not None:
                return {**self._import(connection, previous["id"]), "replayed": True}
            version = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM imports WHERE dataset_id = ?", (dataset_id,)
            ).fetchone()[0]
            identifier = connection.execute(
                """INSERT INTO imports (dataset_id, version, source_name, sha256, imported_at,
                   headers_json, row_count, accepted_count, rejected_count, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (dataset_id, version, source_name, digest, datetime.now(UTC).isoformat(),
                 json.dumps(headers), len(rows), accepted, len(rows) - accepted, content),
            ).lastrowid
            connection.executemany(
                """INSERT INTO records (import_id, number, source_line, entity_key, raw_json,
                   data_json, issues_json, issue_count, invalid_columns_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ((identifier, row.number, row.line, row.key, json.dumps(row.raw),
                  json.dumps(row.data) if row.data is not None else None,
                  json.dumps(row.issues), len(row.issues), json.dumps(row.invalid)) for row in rows),
            )
            return {**self._import(connection, identifier), "replayed": False}

    def datasets(self, limit=100, offset=0) -> list[dict]:
        """List datasets by name with their import count and latest version."""
        check_page(limit, offset)
        with closing(self.connect()) as connection:
            return [{**dict(row), "column_types": json.loads(row["column_types"])} for row in connection.execute(
                """SELECT d.name, d.key_column, d.column_types_json AS column_types, COUNT(i.id) AS import_count,
                   MAX(i.version) AS latest_version
                   FROM datasets d LEFT JOIN imports i ON i.dataset_id = d.id
                   GROUP BY d.id ORDER BY d.name LIMIT ? OFFSET ?""", (limit, offset)
            )]

    def imports(self, dataset: str, limit=100, offset=0) -> list[dict]:
        """List a dataset's imports, newest version first."""
        check_page(limit, offset)
        with closing(self.connect()) as connection:
            self._dataset(connection, dataset)
            return [import_dict(row) for row in connection.execute(
                f"""SELECT {IMPORT_FIELDS} FROM imports i JOIN datasets d ON d.id = i.dataset_id
                    WHERE d.name = ? ORDER BY i.version DESC LIMIT ? OFFSET ?""", (dataset, limit, offset)
            )]

    def _dataset(self, connection, dataset: str):
        """Raise NotFound unless the dataset exists."""
        if connection.execute("SELECT id FROM datasets WHERE name = ?", (dataset,)).fetchone() is None:
            raise NotFound(f"Dataset '{dataset}' does not exist.")

    def get_import(self, identifier: int) -> dict:
        """One import's details and quality counts."""
        with closing(self.connect()) as connection:
            return self._import(connection, identifier)

    def records(self, identifier: int, *, flagged=False, issues=None, limit=100, offset=0) -> list[dict]:
        """An import's records in source order, optionally only flagged ones or those with given issue codes."""
        check_page(limit, offset)
        issues = issue_codes(issues or [])
        with closing(self.connect()) as connection:
            self._import(connection, identifier)
            query = "SELECT * FROM records WHERE import_id = ?"
            params = [identifier]
            if flagged or issues:
                query += " AND issue_count > 0"
            if issues:
                # Rows matching any requested code are listed once, in source order.
                marks = ", ".join("?" * len(issues))
                query += f" AND EXISTS (SELECT 1 FROM json_each(issues_json) WHERE value IN ({marks}))"
                params += issues
            query += " ORDER BY number LIMIT ? OFFSET ?"
            return [record_dict(row) for row in connection.execute(query, (*params, limit, offset))]

    def source(self, identifier: int) -> bytes:
        """The exact bytes that were imported."""
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT source FROM imports WHERE id = ?", (identifier,)).fetchone()
            if row is None:
                raise NotFound(f"Import {identifier} does not exist.")
            return bytes(row["source"])

    def trace(self, dataset: str, key: str, limit=100, offset=0) -> list[dict]:
        """Every record with this exact normalized key across the dataset's versions, newest first."""
        check_page(limit, offset)
        with closing(self.connect()) as connection:
            self._dataset(connection, dataset)
            return [record_dict(row) for row in connection.execute(
                """SELECT r.*, i.version, i.source_name, i.sha256, i.imported_at FROM records r
                   JOIN imports i ON i.id = r.import_id JOIN datasets d ON d.id = i.dataset_id
                   WHERE d.name = ? AND r.entity_key = ? ORDER BY i.version DESC, r.number
                   LIMIT ? OFFSET ?""", (dataset, key, limit, offset)
            )]

    def history(self, dataset: str, key: str, limit=100, offset=0) -> dict:
        """How one exact normalized key's values changed across the dataset's clean versions, newest first.

        Flagged imports are not compared but are listed in `skipped_flagged` with the key's rows there, since
        a value that changed in a flagged import only shows up in the next clean one.
        """
        check_page(limit, offset)
        if not isinstance(key, str) or not key:
            raise InvalidInput("Key must be a nonempty string.")
        with closing(self.connect()) as connection:
            self._dataset(connection, dataset)
            snapshots = [dict(row) for row in connection.execute(
                """SELECT i.id, i.version, i.source_name, i.sha256, i.imported_at, i.rejected_count
                   FROM imports i JOIN datasets d ON d.id = i.dataset_id WHERE d.name = ? ORDER BY i.version""",
                (dataset,)
            )]
            rows = [record_dict(row) for row in connection.execute(
                """SELECT r.* FROM records r JOIN imports i ON i.id = r.import_id JOIN datasets d ON d.id = i.dataset_id
                   WHERE d.name = ? AND r.entity_key = ? ORDER BY r.import_id, r.number""", (dataset, key)
            )]
        clean = [snapshot for snapshot in snapshots if not snapshot["rejected_count"]]
        records = {row["import_id"]: row for row in rows if not row["issues"]}
        events = key_history(clean, records)[::-1]
        latest = None
        if clean and clean[-1]["id"] in records:
            record = records[clean[-1]["id"]]
            latest = {"version": clean[-1]["version"], "import_id": clean[-1]["id"], "record": record["number"],
                      "data": record["data"]}
        # The key's rows in each flagged import, which may be why its latest change is missing.
        skipped = [{"id": snapshot["id"], "version": snapshot["version"], "rejected_count": snapshot["rejected_count"],
                    "key_records": [{name: row[name] for name in ("number", "source_line", "issues")}
                                    for row in rows if row["import_id"] == snapshot["id"]]}
                   for snapshot in reversed(snapshots) if snapshot["rejected_count"]]
        return {"dataset": dataset, "key": key, "clean_versions": len(clean),
                "present_in": sum(snapshot["id"] in records for snapshot in clean), "latest": latest,
                "skipped_flagged": skipped, "events": events[offset:offset + limit], "total_events": len(events),
                "limit": limit, "offset": offset}

    def _version(self, connection, dataset: str, version: int, option: str, role: str) -> dict:
        """A clean import chosen by version; `option` and `role` only shape error messages."""
        if type(version) is not int or version < 1:
            raise InvalidInput(f"{option} must be a positive dataset version.")
        row = connection.execute(
            """SELECT i.id, i.version, i.rejected_count FROM imports i JOIN datasets d ON d.id = i.dataset_id
               WHERE d.name = ? AND i.version = ?""", (dataset, version)
        ).fetchone()
        if row is None:
            raise NotFound(f"Dataset '{dataset}' has no version {version}.")
        if row["rejected_count"]:
            raise Conflict(f"Version {version} has flagged records and cannot be {role}.")
        return row

    def _clean(self, connection, dataset: str, above=0, below=None, count=1) -> list:
        """Up to `count` clean imports between two versions (exclusive), newest first."""
        query = """SELECT i.id, i.version FROM imports i JOIN datasets d ON d.id = i.dataset_id
                   WHERE d.name = ? AND i.rejected_count = 0 AND i.version > ?"""
        params = [dataset, above]
        if below is not None:
            query += " AND i.version < ?"
            params.append(below)
        return connection.execute(query + " ORDER BY i.version DESC LIMIT ?", (*params, count)).fetchall()

    def select_versions(self, dataset: str, since=None, until=None) -> dict:
        """Pick two clean snapshots by version: the two newest by default, or `since`/`until`.

        Returns their IDs and versions and `skipped_flagged`, the flagged imports inside the compared range.
        """
        with closing(self.connect()) as connection:
            self._dataset(connection, dataset)
            if since is not None:
                baseline = self._version(connection, dataset, since, "Since", "a baseline")
            if until is not None:
                # A chosen end version replaces the newest clean snapshot as the newer side.
                newest = self._version(connection, dataset, until, "Until", "an end version")
                if since is None:
                    older = self._clean(connection, dataset, below=until)
                    if not older:
                        raise Conflict(f"Dataset '{dataset}' has no clean snapshot older than version {until}.")
                    baseline = older[0]
                elif since >= until:
                    raise InvalidInput("Since must be an older version than until.")
                clean = [newest, baseline]
            elif since is None:
                clean = self._clean(connection, dataset, count=2)
                if len(clean) < 2:
                    raise Conflict(f"Dataset '{dataset}' needs two clean snapshots to compare.")
            else:
                # A chosen baseline is compared with the newest clean snapshot after it.
                newest = self._clean(connection, dataset, above=since)
                if not newest:
                    raise Conflict(f"Dataset '{dataset}' has no clean snapshot newer than version {since}.")
                clean = [newest[0], baseline]
            # Flagged snapshots after the baseline are skipped, but callers should know they exist.
            # With a chosen end version, only those inside the compared range are listed.
            query = """SELECT i.id, i.version, i.rejected_count FROM imports i JOIN datasets d ON d.id = i.dataset_id
                       WHERE d.name = ? AND i.rejected_count > 0 AND i.version > ?"""
            params = [dataset, clean[1]["version"]]
            if until is not None:
                query += " AND i.version < ?"
                params.append(until)
            skipped = [dict(row) for row in connection.execute(query + " ORDER BY i.version DESC", params)]
        return {"dataset": dataset, "before_id": clean[1]["id"], "after_id": clean[0]["id"],
                "before_version": clean[1]["version"], "after_version": clean[0]["version"],
                "skipped_flagged": skipped}

    def latest_changes(self, dataset: str, limit=100, offset=0, kinds=None, since=None, until=None) -> dict:
        """Diff the clean snapshots chosen by `select_versions`, adding their versions and `skipped_flagged`."""
        check_page(limit, offset)
        kinds = change_kinds(kinds)
        selected = self.select_versions(dataset, since, until)
        result = self.diff(selected["before_id"], selected["after_id"], limit, offset, kinds)
        return {"dataset": dataset, "before_version": selected["before_version"],
                "after_version": selected["after_version"], "skipped_flagged": selected["skipped_flagged"], **result}

    def diff(self, before_id: int, after_id: int, limit=100, offset=0, kinds=None) -> dict:
        """Compare two clean imports of one dataset by key, with full counts and a page of field-level changes."""
        check_page(limit, offset)
        kinds = change_kinds(kinds)
        before, after, old, new = self._snapshots(before_id, after_id)
        result = compare(old, new, kinds, limit, offset)
        return {"before_id": before_id, "after_id": after_id, "counts": result["counts"],
                "schema": {"added": sorted(set(after["headers"]) - set(before["headers"])),
                           "removed": sorted(set(before["headers"]) - set(after["headers"]))},
                "kinds": kinds, "changes": result["changes"], "total_changes": result["total_changes"],
                "limit": limit, "offset": offset}

    def diff_changes(self, before_id: int, after_id: int, kinds=None) -> Iterator[dict]:
        """Every change of the requested kinds between two clean imports, in key order, without pagination.

        Inputs are checked before this returns. Changes are built one key at a time as the result is consumed.
        """
        kinds = change_kinds(kinds)
        _, _, old, new = self._snapshots(before_id, after_id)
        return iter_changes(old, new, kinds)

    def _snapshots(self, before_id: int, after_id: int) -> tuple[dict, dict, dict, dict]:
        """Two clean imports of one dataset and their records by key, or a Conflict saying why not."""
        with closing(self.connect()) as connection:
            before = self._import(connection, before_id)
            after = self._import(connection, after_id)
            if before["dataset"] != after["dataset"]:
                raise Conflict("Compare imports from the same dataset.")
            if before["rejected_count"] or after["rejected_count"]:
                raise Conflict("Resolve flagged records and import a clean file before comparing snapshots.")
            def load(identifier):
                return {row["entity_key"]: record_dict(row) for row in connection.execute(
                    "SELECT * FROM records WHERE import_id = ?", (identifier,)
                )}
            return before, after, load(before_id), load(after_id)
