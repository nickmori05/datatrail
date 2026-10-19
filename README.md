# Datatrail

[![CI](https://github.com/nickmori05/datatrail/actions/workflows/tests.yml/badge.svg)](https://github.com/nickmori05/datatrail/actions/workflows/tests.yml)

Datatrail is a small backend for investigating what changed between CSV
exports of the same dataset. Each import is saved as a versioned snapshot with
its exact original bytes and a row-level quality report. Rows with duplicate,
missing or malformed keys, wrong column counts or values that fail declared
column types are kept and labelled instead of being dropped, and snapshots
with flagged rows are never used for comparisons. Clean snapshots can be
compared field by field, and any key can be traced back through every version
to its source file and line. It runs as a command-line tool (standard library
only) or a FastAPI service, both backed by one SQLite file.

## Quick start

Requires Python 3.11 or later and nothing else:

```sh
git clone https://github.com/nickmori05/datatrail.git
cd datatrail
python3 scripts/demo.py
```

The demo imports the fictional files in `examples/` into a temporary database
and prints the results as JSON. Two supplier exports differ by one moved, one
removed and one added supplier. A third, messy export is flagged and left out
of the comparison. An invoice file has an amount and a due date that fail
their column types.

## Usage

### Command line

```sh
python3 -m datatrail import suppliers examples/suppliers-before.csv --key supplier_id
python3 -m datatrail import suppliers examples/suppliers-after.csv --key supplier_id
python3 -m datatrail datasets
python3 -m datatrail imports suppliers
python3 -m datatrail show 1
python3 -m datatrail diff 1 2
python3 -m datatrail changes suppliers
python3 -m datatrail changes suppliers --since 1
python3 -m datatrail changes suppliers --until 2
python3 -m datatrail trace suppliers S002
python3 -m datatrail import suppliers examples/suppliers-messy.csv --key supplier_id
python3 -m datatrail records 3 --flagged
python3 -m datatrail records 3 --issue duplicate_key
python3 -m datatrail import invoices examples/invoices.csv --key invoice_id \
  --type amount:decimal --type due_date:date
python3 -m datatrail records 4 --issue invalid_value
```

The IDs above assume a fresh database; use the IDs your imports return. Every
command prints JSON. `python3 -m datatrail COMMAND --help` lists the options.

The database is `.local/datatrail.sqlite3` in the working directory, which Git
ignores. Set `DATATRAIL_DB`, or put `--database PATH` before the command, to use
another file.

Exit codes: `0` success, `1` an import was saved with flagged rows, `2` invalid
input, a missing resource, a conflict or a storage error, `130` interrupted.

### HTTP API

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn datatrail.api:app --host 127.0.0.1 --port 8000
```

In another terminal:

```sh
curl -i -X POST 'http://127.0.0.1:8000/datasets/suppliers/imports?key=supplier_id&source_name=before.csv' \
  -H 'Content-Type: text/csv' --data-binary @examples/suppliers-before.csv
curl 'http://127.0.0.1:8000/datasets/suppliers/trace?key=S002'
```

Interactive docs are at `http://127.0.0.1:8000/docs`. The API reads the same
`DATATRAIL_DB` setting as the CLI, so both see the same data.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/datasets/{name}/imports?key=column&source_name=file.csv` | Upload raw CSV bytes |
| POST | `/datasets/{name}/imports?key=column&type=amount:decimal` | Upload with column type checks |
| GET | `/datasets` | List datasets |
| GET | `/datasets/{name}/imports` | List snapshots, newest first |
| GET | `/imports/{id}` | Import metadata and quality counts |
| GET | `/imports/{id}/records?flagged=true` | Inspect flagged rows |
| GET | `/imports/{id}/records?issue=invalid_value` | Inspect rows with one kind of issue |
| GET | `/imports/{id}/source` | Download the exact original bytes |
| GET | `/datasets/{name}/trace?key=value` | Trace one normalized key |
| GET | `/diff?before=1&after=2&kind=changed` | Compare two clean snapshots |
| GET | `/datasets/{name}/changes?kind=added` | Compare the two newest clean snapshots |
| GET | `/datasets/{name}/changes?since=1` | Compare a baseline version with the newest clean snapshot |
| GET | `/datasets/{name}/changes?until=2` | Compare an end version with the newest clean snapshot before it |
| GET | `/health/live` | Check that the API is responding |
| GET | `/health/ready` | Check that the import table is readable |

Uploads return `201`, or `200` for an exact retry, with a `Location` header for
the import. An import with flagged rows is still saved; check `rejected_count`
and `issue_counts` in the response. Lists and diffs accept `limit` and `offset`.

| Status | Meaning |
| --- | --- |
| `400` | Invalid CSV or column type rules |
| `404` | Missing dataset, import or version |
| `409` | Changed key column or column types, or a comparison involving flagged snapshots |
| `413` | Upload larger than 5 MiB, checked while streaming even without a reliable Content-Length |
| `415` | Body is not `Content-Type: text/csv` |
| `422` | Invalid query parameters |
| `503` | Storage unavailable |

The API never fetches remote URLs or opens server file paths supplied by
clients. Readiness does not prove writes will succeed.

### Docker

```sh
docker compose up --build -d --wait
curl http://127.0.0.1:8000/health/ready
docker compose exec api python -m datatrail datasets
docker compose down
```

The API listens on localhost port 8000; set `DATATRAIL_PORT=8001` if that port
is taken. The container runs as a non-root user with a read-only root
filesystem. Data lives in the `datatrail-data` volume, which survives
`docker compose down` and is deleted by `docker compose down --volumes`. The CLI
inside the container shares the API's database, which is separate from your
local one.

## Import rules

- UTF-8 CSV with an optional BOM, comma delimiter and standard quoting.
- At most 5 MiB, 10,000 data records and 64 columns. Column names must be
  nonempty, unique after trimming and at most 128 characters. Very large fields
  also hit Python's CSV field size limit.
- Header and cell whitespace is trimmed for comparison; the original bytes and
  cells are kept. Values stay strings, so `001` and `1` are different keys, and
  matching is case-sensitive.
- Each dataset has one key column, fixed when the dataset is created.
- Structurally invalid CSV, a bad encoding, an exceeded limit, or a missing key
  or typed column rejects the whole file and saves nothing.
- Row-level problems are saved with the raw cells and one or more issue codes:

  | Code | Meaning |
  | --- | --- |
  | `column_count` | The row has more or fewer cells than the header |
  | `missing_key` | The key cell is empty |
  | `duplicate_key` | The key appears more than once; every occurrence is flagged, none is picked as the winner |
  | `invalid_value` | A cell does not match its column type |

- Blank lines are skipped. A header-only file is a valid empty snapshot.
- Importing identical bytes into the same dataset again returns the original
  import with `replayed: true`, keeping its first source name and timestamp.
  Any other change, even whitespace, creates a new version. Versions follow
  import order, not dates inside the files.

## Column types

`import --type COLUMN:TYPE` (repeatable; `type=` in the API) declares that a
column holds one kind of value. Rows with a value that does not match are
flagged with `invalid_value`.

| Type | Accepts | Rejects |
| --- | --- | --- |
| `integer` | `42`, `-7`, `+003` | `4.0`, `1e3`, `1,000` |
| `decimal` | `9.50`, `-.5`, `12.` | `9.5.1`, `1e3`, `$9`, `1,000` |
| `date` | Calendar-valid ISO dates such as `2026-02-28` | `2026-02-30`, `02/28/2026`, `2026-2-28` |

Checks use the trimmed value and ASCII digits only. Empty cells are not
checked, so a type does not make a column required. Key columns can be typed.
`records` lists each row's failing columns in `invalid_columns`.

Types are saved with the dataset when it is created, like the key column. Later
imports can leave out `--type` and are still checked against the saved types, or
repeat the same set. A different set is a conflict, so every version of a
dataset is checked by the same rules. `datasets`, `show` and import responses
include `column_types`. Databases created before column types existed are
upgraded in place on startup; their datasets have no types.

## Investigating changes

- `show ID` prints one import's source name, SHA-256, timestamp, columns, column
  types, record counts and `issue_counts`: how many rows carry each issue code,
  with zeros for codes that did not occur. It never prints the file contents.
- `records ID` shows raw cells, normalized values, issue codes, record numbers
  and the starting line of each record, including after multiline fields.
  `--flagged` lists only flagged rows, and `--issue CODE` (repeatable) only rows
  with any of those codes.
- `trace DATASET KEY` finds an exact normalized key in every version, newest
  first, including flagged duplicates.
- `diff BEFORE AFTER` compares two clean imports of the same dataset by key. It
  reports added, removed, changed and unchanged counts, field-level before and
  after values with the original record numbers, and added or removed columns.
  A missing field is `null`; an empty one is `""`. Column and row order do not
  matter. `--kind added|removed|changed` (repeatable) lists only those changes;
  the counts still cover every key.
- `changes DATASET` runs the same comparison by version instead of import ID.
  By default it compares the two newest clean snapshots. `--since VERSION` picks
  the older side and `--until VERSION` the newer side; with only one of them, the
  other side is the nearest clean snapshot. The result lists `skipped_flagged`,
  the flagged imports inside the compared range, so a broken latest export is
  not mistaken for "nothing changed". An unknown version is not found (`404`),
  a flagged version or a missing clean counterpart is a conflict (`409`), and a
  `--since` that is not older than `--until` is invalid (`400`).

Lists accept `--limit` (1–1,000, default 100) and `--offset`. Filters apply
before pagination, and an empty page means there are no more results.

## Design

| Module | Responsibility |
| --- | --- |
| `datatrail/ingest.py` | Parse CSV bytes and assign issue codes, before any write |
| `datatrail/rules.py` | Column type specs and the values each type accepts |
| `datatrail/store.py` | SQLite schema setup, imports, queries and snapshot comparison |
| `datatrail/__main__.py` | Command-line interface |
| `datatrail/api.py` | FastAPI app and HTTP error mapping |

Imports are parsed and classified first. The dataset, the import, the original
file and all records are then written in one SQLite transaction, so a failure
leaves nothing behind. `BEGIN IMMEDIATE` and uniqueness constraints stop
concurrent retries from creating duplicate versions. Identity comes only from
the key column; similar keys are never merged.

There are no edit or delete operations. Hashes identify imported bytes but do
not authenticate their source, and anyone with direct access to the database
file can change it.

## Development

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check .
```

Tests use temporary databases and synthetic fixtures. They cover parsing edge
cases, duplicate handling, source preservation, atomic rollback, concurrent
retries and version allocation, schema upgrades, pagination, CLI and API error
paths, and the demo.

With Docker and Compose available, `python3 scripts/docker_smoke.py` builds the
image and runs the API in its own Compose project, volume and random localhost
port. It checks uploads, retries, quality and type checks, diffs, version
lookups, source downloads, CLI and API sharing one database, and persistence
across container recreation, then removes everything it created.

GitHub Actions runs ruff, the test suite on Python 3.11 and 3.14, and the Docker
check.

## Limitations

- Built for one trusted local workspace: no authentication, users or
  per-user isolation. Keep the API bound to localhost.
- Snapshots are bounded (5 MiB, 10,000 records) and compared in memory. There
  is no background queue, retention policy or migration framework beyond the
  additive upgrade on startup.
- Keys are matched exactly. There is no fuzzy matching, cross-source entity
  resolution or automatic type inference.
- Column types cover integers, decimals and ISO dates only; there are no
  required columns, enums, ranges or locale-specific number formats. A dataset's
  key column and types cannot change; use a new dataset name.
- Versions reflect import order, not dates inside the files.
