"""Choose which clean snapshots of a dataset to compare, by version number."""

from .errors import Conflict, InvalidInput, NotFound


def chosen_version(connection, dataset: str, version: int, option: str, role: str):
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


def clean_between(connection, dataset: str, above=0, below=None, count=1) -> list:
    """Up to `count` clean imports between two versions (exclusive), newest first."""
    query = """SELECT i.id, i.version FROM imports i JOIN datasets d ON d.id = i.dataset_id
               WHERE d.name = ? AND i.rejected_count = 0 AND i.version > ?"""
    params = [dataset, above]
    if below is not None:
        query += " AND i.version < ?"
        params.append(below)
    return connection.execute(query + " ORDER BY i.version DESC LIMIT ?", (*params, count)).fetchall()


def select_versions(connection, dataset: str, since=None, until=None) -> dict:
    """Pick two clean snapshots of an existing dataset: the two newest by default, or `since`/`until`.

    Returns their IDs and versions and `skipped_flagged`, the flagged imports inside the compared range.
    """
    if since is not None:
        baseline = chosen_version(connection, dataset, since, "Since", "a baseline")
    if until is not None:
        # A chosen end version replaces the newest clean snapshot as the newer side.
        newest = chosen_version(connection, dataset, until, "Until", "an end version")
        if since is None:
            older = clean_between(connection, dataset, below=until)
            if not older:
                raise Conflict(f"Dataset '{dataset}' has no clean snapshot older than version {until}.")
            baseline = older[0]
        elif since >= until:
            raise InvalidInput("Since must be an older version than until.")
        clean = [newest, baseline]
    elif since is None:
        clean = clean_between(connection, dataset, count=2)
        if len(clean) < 2:
            raise Conflict(f"Dataset '{dataset}' needs two clean snapshots to compare.")
    else:
        # A chosen baseline is compared with the newest clean snapshot after it.
        newest = clean_between(connection, dataset, above=since)
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
