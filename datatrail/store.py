from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .errors import Conflict, InvalidInput, NotFound
from .ingest import parse_csv


IMPORT_FIELDS = """i.id, d.name AS dataset, d.key_column, i.version, i.source_name,
    i.sha256, i.imported_at, i.headers_json, i.row_count, i.accepted_count, i.rejected_count"""


def page_args(limit: int, offset: int):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise InvalidInput("Limit must be between 1 and 1000.")
    if type(offset) is not int or offset < 0:
        raise InvalidInput("Offset must be a nonnegative integer.")


def import_dict(row) -> dict:
    result = dict(row)
    result["headers"] = json.loads(result.pop("headers_json"))
    return result


def record_dict(row) -> dict:
    result = dict(row)
    for name in ("raw", "data", "issues"):
        value = result.pop(f"{name}_json")
        result[name] = json.loads(value) if value is not None else None
    result.pop("issue_count", None)
    return result


class Store:
    def __init__(self, database: Path):
        self.database = Path(database)

    def connect(self):
        connection = sqlite3.connect(self.database, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self):
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection:
            connection.executescript(Path(__file__).with_name("schema.sql").read_text())
            connection.commit()

    def _import(self, connection, identifier: int) -> dict:
        row = connection.execute(
            f"SELECT {IMPORT_FIELDS} FROM imports i JOIN datasets d ON d.id = i.dataset_id WHERE i.id = ?",
            (identifier,),
        ).fetchone()
        if row is None:
            raise NotFound(f"Import {identifier} does not exist.")
        return import_dict(row)

    def import_csv(self, dataset: str, key_column: str, content: bytes, source_name: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", dataset):
            raise InvalidInput("Dataset names must be 1–64 letters, digits, hyphens or underscores, starting with a letter or digit.")
        if not source_name or len(source_name) > 255 or any(ord(char) < 32 for char in source_name):
            raise InvalidInput("Source name must be 1–255 characters without control characters.")
        headers, rows = parse_csv(content, key_column)
        digest = hashlib.sha256(content).hexdigest()
        accepted = sum(not row.issues for row in rows)
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM datasets WHERE name = ?", (dataset,)).fetchone()
            if existing is None:
                dataset_id = connection.execute(
                    "INSERT INTO datasets (name, key_column) VALUES (?, ?)", (dataset, key_column)
                ).lastrowid
            else:
                if existing["key_column"] != key_column:
                    raise Conflict("A dataset's key column cannot change. Use a new dataset name.")
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
                (dataset_id, version, source_name, digest, datetime.now(timezone.utc).isoformat(),
                 json.dumps(headers), len(rows), accepted, len(rows) - accepted, content),
            ).lastrowid
            connection.executemany(
                """INSERT INTO records (import_id, number, source_line, entity_key, raw_json,
                   data_json, issues_json, issue_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ((identifier, row.number, row.line, row.key, json.dumps(row.raw),
                  json.dumps(row.data) if row.data is not None else None,
                  json.dumps(row.issues), len(row.issues)) for row in rows),
            )
            return {**self._import(connection, identifier), "replayed": False}

    def datasets(self, limit=100, offset=0) -> list[dict]:
        page_args(limit, offset)
        with closing(self.connect()) as connection:
            return [dict(row) for row in connection.execute(
                """SELECT d.name, d.key_column, COUNT(i.id) AS import_count, MAX(i.version) AS latest_version
                   FROM datasets d LEFT JOIN imports i ON i.dataset_id = d.id
                   GROUP BY d.id ORDER BY d.name LIMIT ? OFFSET ?""", (limit, offset)
            )]

    def imports(self, dataset: str, limit=100, offset=0) -> list[dict]:
        page_args(limit, offset)
        with closing(self.connect()) as connection:
            if connection.execute("SELECT id FROM datasets WHERE name = ?", (dataset,)).fetchone() is None:
                raise NotFound(f"Dataset '{dataset}' does not exist.")
            return [import_dict(row) for row in connection.execute(
                f"""SELECT {IMPORT_FIELDS} FROM imports i JOIN datasets d ON d.id = i.dataset_id
                    WHERE d.name = ? ORDER BY i.version DESC LIMIT ? OFFSET ?""", (dataset, limit, offset)
            )]

    def get_import(self, identifier: int) -> dict:
        with closing(self.connect()) as connection:
            return self._import(connection, identifier)

    def records(self, identifier: int, *, flagged=False, limit=100, offset=0) -> list[dict]:
        page_args(limit, offset)
        with closing(self.connect()) as connection:
            self._import(connection, identifier)
            query = "SELECT * FROM records WHERE import_id = ?"
            if flagged:
                query += " AND issue_count > 0"
            query += " ORDER BY number LIMIT ? OFFSET ?"
            return [record_dict(row) for row in connection.execute(query, (identifier, limit, offset))]

    def source(self, identifier: int) -> bytes:
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT source FROM imports WHERE id = ?", (identifier,)).fetchone()
            if row is None:
                raise NotFound(f"Import {identifier} does not exist.")
            return bytes(row["source"])

    def trace(self, dataset: str, key: str, limit=100, offset=0) -> list[dict]:
        page_args(limit, offset)
        with closing(self.connect()) as connection:
            if connection.execute("SELECT id FROM datasets WHERE name = ?", (dataset,)).fetchone() is None:
                raise NotFound(f"Dataset '{dataset}' does not exist.")
            return [record_dict(row) for row in connection.execute(
                """SELECT r.*, i.version, i.source_name, i.sha256, i.imported_at FROM records r
                   JOIN imports i ON i.id = r.import_id JOIN datasets d ON d.id = i.dataset_id
                   WHERE d.name = ? AND r.entity_key = ? ORDER BY i.version DESC, r.number
                   LIMIT ? OFFSET ?""", (dataset, key, limit, offset)
            )]

    def diff(self, before_id: int, after_id: int, limit=100, offset=0) -> dict:
        page_args(limit, offset)
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
            old, new = load(before_id), load(after_id)
        counts = {"added": 0, "removed": 0, "changed": 0, "unchanged": 0}
        changes = []
        total = 0
        for key in sorted(old.keys() | new.keys()):
            left, right = old.get(key), new.get(key)
            kind = "added" if left is None else "removed" if right is None else (
                "unchanged" if left["data"] == right["data"] else "changed"
            )
            counts[kind] += 1
            if kind == "unchanged":
                continue
            if offset <= total < offset + limit:
                old_data = left["data"] if left else {}
                new_data = right["data"] if right else {}
                fields = {name: {"before": old_data.get(name), "after": new_data.get(name)}
                          for name in sorted(old_data.keys() | new_data.keys())
                          if old_data.get(name) != new_data.get(name)}
                changes.append({"key": key, "kind": kind, "fields": fields,
                                "before_record": left["number"] if left else None,
                                "after_record": right["number"] if right else None})
            total += 1
        return {"before_id": before_id, "after_id": after_id, "counts": counts,
                "schema": {"added": sorted(set(after["headers"]) - set(before["headers"])),
                           "removed": sorted(set(before["headers"]) - set(after["headers"]))},
                "changes": changes, "total_changes": total, "limit": limit, "offset": offset}
