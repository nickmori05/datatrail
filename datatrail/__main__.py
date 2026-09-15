import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

from .errors import Conflict, InvalidInput, NotFound
from .ingest import MAX_BYTES
from .store import Store


def parser():
    root = argparse.ArgumentParser(description="Import CSV snapshots and investigate record changes.")
    root.add_argument("--database", type=Path, default=Path(os.environ.get("DATATRAIL_DB", ".local/datatrail.sqlite3")))
    commands = root.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("import", help="Save a CSV snapshot and its quality report")
    ingest.add_argument("dataset")
    ingest.add_argument("file", type=Path)
    ingest.add_argument("--key", required=True)
    datasets = commands.add_parser("datasets", help="List datasets")
    imports = commands.add_parser("imports", help="List dataset snapshots")
    imports.add_argument("dataset")
    records = commands.add_parser("records", help="Inspect original and normalized values")
    records.add_argument("import_id", type=int)
    records.add_argument("--flagged", action="store_true")
    trace = commands.add_parser("trace", help="Trace an exact normalized key through snapshots")
    trace.add_argument("dataset")
    trace.add_argument("key")
    diff = commands.add_parser("diff", help="Compare two clean snapshots of the same dataset")
    diff.add_argument("before", type=int)
    diff.add_argument("after", type=int)
    for command in (datasets, imports, records, trace, diff):
        command.add_argument("--limit", type=int, default=100)
        command.add_argument("--offset", type=int, default=0)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        store = Store(args.database)
        store.initialize()
        code = 0
        if args.command == "import":
            with args.file.open("rb") as source:
                content = source.read(MAX_BYTES + 1)
            result = store.import_csv(args.dataset, args.key, content, args.file.name)
            code = 1 if result["rejected_count"] else 0
        elif args.command == "datasets":
            result = store.datasets(args.limit, args.offset)
        elif args.command == "imports":
            result = store.imports(args.dataset, args.limit, args.offset)
        elif args.command == "records":
            result = store.records(args.import_id, flagged=args.flagged, limit=args.limit, offset=args.offset)
        elif args.command == "trace":
            result = store.trace(args.dataset, args.key, args.limit, args.offset)
        else:
            result = store.diff(args.before, args.after, args.limit, args.offset)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return code
    except (InvalidInput, NotFound, Conflict, OSError, sqlite3.Error) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
