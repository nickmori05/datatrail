"""Compare loaded snapshots by key without touching storage."""


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


def compare(old: dict, new: dict, kinds: list[str], limit: int, offset: int) -> dict:
    """Count every key's change kind and list one page of the requested kinds, in key order.

    `old` and `new` map each key to its record in a clean snapshot.
    """
    counts = {"added": 0, "removed": 0, "changed": 0, "unchanged": 0}
    changes = []
    total = 0
    for key in sorted(old.keys() | new.keys()):
        left, right = old.get(key), new.get(key)
        kind = change_kind(left, right)
        counts[kind] += 1
        if kind not in kinds:
            continue
        if offset <= total < offset + limit:
            changes.append({"key": key, "kind": kind,
                            "fields": changed_fields(left and left["data"], right and right["data"]),
                            "before_record": left["number"] if left else None,
                            "after_record": right["number"] if right else None})
        total += 1
    return {"counts": counts, "changes": changes, "total_changes": total}
