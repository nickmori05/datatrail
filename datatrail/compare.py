"""Compare snapshots by key without touching storage."""

from collections.abc import Iterable, Iterator


def without(record: dict, ignored) -> dict:
    """A record whose data leaves out the ignored columns, so they can neither change nor be listed."""
    if not ignored or record["data"] is None:
        return record
    return {**record, "data": {name: value for name, value in record["data"].items() if name not in ignored}}


def changed_fields(before: dict | None, after: dict | None) -> dict:
    """Fields whose values differ, by name. A field missing on one side is None there; an empty one is ""."""
    before, after = before or {}, after or {}
    return {name: {"before": before.get(name), "after": after.get(name)}
            for name in sorted(before.keys() | after.keys()) if before.get(name) != after.get(name)}


def change_kind(left: dict | None, right: dict | None) -> str:
    """How one key's record moved between two snapshots: added, removed, changed or unchanged."""
    if left is None:
        return "added"
    if right is None:
        return "removed"
    return "unchanged" if left["data"] == right["data"] else "changed"


def change_entry(key: str, kind: str, left: dict | None, right: dict | None) -> dict:
    """One key's change between two snapshots: its kind, changed fields and record numbers on each side."""
    return {"key": key, "kind": kind, "fields": changed_fields(left and left["data"], right and right["data"]),
            "before_record": left["number"] if left else None, "after_record": right["number"] if right else None}


def merge_by_key(old: Iterable[tuple[str, dict]], new: Iterable[tuple[str, dict]]) -> Iterator[tuple]:
    """Pair two snapshots' records by key, in key order: (key, old record or None, new record or None).

    Each side yields (key, record) with keys strictly increasing, so only one record per side is held at a time.
    """
    old, new = iter(old), iter(new)
    left, right = next(old, None), next(new, None)
    while left is not None or right is not None:
        if right is None or (left is not None and left[0] < right[0]):
            yield left[0], left[1], None
            left = next(old, None)
        elif left is None or right[0] < left[0]:
            yield right[0], None, right[1]
            right = next(new, None)
        else:
            yield left[0], left[1], right[1]
            left, right = next(old, None), next(new, None)


def compare(pairs: Iterable[tuple], kinds: list[str], limit: int, offset: int) -> dict:
    """Count every key's change kind and list one page of the requested kinds.

    `pairs` come from `merge_by_key`, so only the page is kept in memory.
    """
    counts = {"added": 0, "removed": 0, "changed": 0, "unchanged": 0}
    changes = []
    total = 0
    for key, left, right in pairs:
        kind = change_kind(left, right)
        counts[kind] += 1
        if kind not in kinds:
            continue
        if offset <= total < offset + limit:
            changes.append(change_entry(key, kind, left, right))
        total += 1
    return {"counts": counts, "changes": changes, "total_changes": total}


def iter_changes(pairs: Iterable[tuple], kinds: list[str]) -> Iterator[dict]:
    """Yield every change of the requested kinds in the order of `pairs`, building each one only when it is reached."""
    for key, left, right in pairs:
        kind = change_kind(left, right)
        if kind in kinds:
            yield change_entry(key, kind, left, right)


def key_history(snapshots: list[dict], records: dict) -> list[dict]:
    """One key's added, changed and removed events across clean snapshots, oldest first.

    `snapshots` are a dataset's clean imports in version order. `records` maps an import ID to the key's
    record in that import; a clean import has at most one. Versions where the key's values did not change,
    or where it was absent before and after, produce no event.
    """
    events = []
    previous, previous_version = None, None
    for snapshot in snapshots:
        record = records.get(snapshot["id"])
        if record is not None or previous is not None:
            kind = change_kind(previous, record)
            if kind != "unchanged":
                events.append({
                    "version": snapshot["version"], "import_id": snapshot["id"], "kind": kind,
                    "previous_version": previous_version,
                    "fields": changed_fields(previous and previous["data"], record and record["data"]),
                    "before_record": previous["number"] if previous else None,
                    "before_line": previous["source_line"] if previous else None,
                    "after_record": record["number"] if record else None,
                    "after_line": record["source_line"] if record else None,
                    "source_name": snapshot["source_name"], "sha256": snapshot["sha256"],
                    "imported_at": snapshot["imported_at"],
                })
        previous, previous_version = record, snapshot["version"]
    return events
