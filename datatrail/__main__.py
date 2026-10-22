"""Command-line interface: python -m datatrail COMMAND."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .errors import Conflict, InvalidInput, NotFound
from .export import csv_chunks
from .ingest import ISSUE_CODES, MAX_BYTES
from .store import CHANGE_KINDS, Store, default_database


def parser():
    """Build the argument parser for every command."""
    root = argparse.ArgumentParser(description="Import CSV snapshots and investigate record changes.")
    root.add_argument("--database", type=Path, default=default_database())
    commands = root.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("import", help="Save a CSV snapshot and its quality report")
    ingest.add_argument("dataset")
    ingest.add_argument("file", type=Path)
    ingest.add_argument("--key", required=True)
    ingest.add_argument("--type", action="append", dest="column_types", metavar="COLUMN:TYPE",
                        help="Flag rows whose COLUMN is not a TYPE (integer, decimal or date); repeatable, "
                             "fixed for the dataset")
    ingest.add_argument("--required", action="append", metavar="COLUMN",
                        help="Flag rows where COLUMN is empty; repeatable, fixed for the dataset with the other "
                             "value rules")
    ingest.add_argument("--allow", action="append", dest="allowed", metavar="COLUMN:VALUE",
                        help="Accept VALUE in COLUMN; repeat for each value, and other values are flagged")
    ingest.add_argument("--pattern", action="append", dest="patterns", metavar="COLUMN:PATTERN",
                        help="Flag rows whose COLUMN does not fully match the Python regular expression PATTERN")
    datasets = commands.add_parser("datasets", help="List datasets")
    imports = commands.add_parser("imports", help="List dataset snapshots")
    imports.add_argument("dataset")
    summary = commands.add_parser("summary", help="Show a dataset's quality and change counts for each version")
    summary.add_argument("dataset")
    show = commands.add_parser("show", help="Show one import's source details and quality counts")
    show.add_argument("import_id", type=int)
    records = commands.add_parser("records", help="Inspect original and normalized values")
    records.add_argument("import_id", type=int)
    records.add_argument("--flagged", action="store_true")
    records.add_argument("--issue", action="append", choices=ISSUE_CODES,
                         help="Only list flagged rows with this issue code; repeatable")
    trace = commands.add_parser("trace", help="Trace an exact normalized key through snapshots")
    trace.add_argument("dataset")
    trace.add_argument("key")
    history = commands.add_parser("history", help="Show how one key's values changed across clean snapshots")
    history.add_argument("dataset")
    history.add_argument("key")
    diff = commands.add_parser("diff", help="Compare two clean snapshots of the same dataset")
    diff.add_argument("before", type=int)
    diff.add_argument("after", type=int)
    changes = commands.add_parser("changes", help="Compare a dataset's two newest clean snapshots")
    changes.add_argument("dataset")
    changes.add_argument("--since", type=int, help="Compare from this clean version instead of the second newest")
    changes.add_argument("--until", type=int, help="Compare up to this clean version instead of the newest")
    for command in (diff, changes):
        command.add_argument("--kind", action="append", choices=CHANGE_KINDS,
                             help="Only list this kind of change; repeatable")
        command.add_argument("--format", choices=("json", "csv"), default="json",
                             help="csv writes every listed change, one row per changed field, without counts")
        command.add_argument("--spreadsheet", action="store_true",
                             help="With --format csv, add a byte order mark and escape values that spreadsheets "
                                  "would run as formulas")
    for command in (datasets, imports, summary, records, trace, history, diff, changes):
        # None means "not given", so a CSV export can reject explicit pagination.
        command.add_argument("--limit", type=int, help="Results per page, 1–1000 (default 100)")
        command.add_argument("--offset", type=int, help="Results to skip (default 0)")
    return root


def export(store, args):
    """Write every requested change as CSV to stdout, warning on stderr about skipped flagged versions."""
    if args.limit is not None or args.offset is not None:
        raise InvalidInput("CSV exports include every change; leave out --limit and --offset.")
    if args.command == "diff":
        changes = store.diff_changes(args.before, args.after, kinds=args.kind)
    else:
        selected = store.select_versions(args.dataset, since=args.since, until=args.until)
        changes = store.diff_changes(selected["before_id"], selected["after_id"], kinds=args.kind)
        if selected["skipped_flagged"]:
            versions = ", ".join(str(item["version"]) for item in selected["skipped_flagged"])
            print(f"Warning: comparing versions {selected['before_version']} and {selected['after_version']}; "
                  f"skipped flagged versions {versions}.", file=sys.stderr)
    chunks = csv_chunks(changes, spreadsheet=args.spreadsheet)
    # Write UTF-8 bytes, so the console's encoding can neither reject a value nor rewrite the CRLF line endings.
    output = getattr(sys.stdout, "buffer", None)
    if output is None:
        sys.stdout.writelines(chunks)
    else:
        sys.stdout.flush()
        output.writelines(chunk.encode() for chunk in chunks)
        output.flush()


def main(argv=None):
    """Run one command and print its JSON result. Returns the process exit code."""
    args = parser().parse_args(argv)
    try:
        store = Store(args.database)
        store.initialize()
        if getattr(args, "format", "json") == "csv":
            export(store, args)
            return 0
        if getattr(args, "spreadsheet", False):
            raise InvalidInput("--spreadsheet only applies to --format csv.")
        if "limit" in args:
            args.limit = 100 if args.limit is None else args.limit
            args.offset = 0 if args.offset is None else args.offset
        code = 0
        if args.command == "import":
            with args.file.open("rb") as source:
                content = source.read(MAX_BYTES + 1)
            result = store.import_csv(args.dataset, args.key, content, args.file.name, args.column_types,
                                      args.required, args.allowed, args.patterns)
            code = 1 if result["rejected_count"] else 0
        elif args.command == "datasets":
            result = store.datasets(args.limit, args.offset)
        elif args.command == "imports":
            result = store.imports(args.dataset, args.limit, args.offset)
        elif args.command == "summary":
            result = store.summary(args.dataset, args.limit, args.offset)
        elif args.command == "show":
            result = store.get_import(args.import_id)
        elif args.command == "records":
            result = store.records(args.import_id, flagged=args.flagged, issues=args.issue, limit=args.limit,
                                   offset=args.offset)
        elif args.command == "trace":
            result = store.trace(args.dataset, args.key, args.limit, args.offset)
        elif args.command == "history":
            result = store.history(args.dataset, args.key, args.limit, args.offset)
        elif args.command == "changes":
            result = store.latest_changes(args.dataset, args.limit, args.offset, kinds=args.kind, since=args.since,
                                          until=args.until)
        else:
            result = store.diff(args.before, args.after, args.limit, args.offset, kinds=args.kind)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return code
    except (InvalidInput, NotFound, Conflict, OSError, sqlite3.Error) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
