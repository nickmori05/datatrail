# Datatrail

[![Tests](https://github.com/nickmori05/datatrail/actions/workflows/tests.yml/badge.svg)](https://github.com/nickmori05/datatrail/actions/workflows/tests.yml)

Investigate what changed between CSV exports and trace each record back to its source.

The sample workflow compares two supplier exports: one supplier moved cities,
one disappeared, and one was added. A third export contains ambiguous IDs and
malformed rows. Datatrail keeps those rows visible and blocks comparisons until
you import a clean file.

## Try it

Python 3.11 or later. The CLI uses only the standard library.

```sh
git clone https://github.com/nickmori05/datatrail.git
cd datatrail
python3 scripts/demo.py
```

The demo uses a temporary database, prints the comparison, flagged records, and
the history of supplier `S002`, then removes its temporary files. All example
suppliers are fictional.

For a persistent workspace:

```sh
python3 -m datatrail import suppliers examples/suppliers-before.csv --key supplier_id
python3 -m datatrail import suppliers examples/suppliers-after.csv --key supplier_id
python3 -m datatrail datasets
python3 -m datatrail imports suppliers
python3 -m datatrail diff 1 2
python3 -m datatrail trace suppliers S002
python3 -m datatrail import suppliers examples/suppliers-messy.csv --key supplier_id
python3 -m datatrail records 3 --flagged
```

The numeric IDs above assume a fresh database; use the IDs returned by your
imports. The default database is `.local/datatrail.sqlite3` relative to the
working directory. Set `DATATRAIL_DB` or put `--database PATH` before the command
to use another file. Local data is excluded from Git.

## HTTP API

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn datatrail.api:app --host 127.0.0.1 --port 8000
```

Interactive API docs are at `http://127.0.0.1:8000/docs`. The API and CLI use the
same database setting. This version is for one trusted local workspace: there
is no authentication or per-user isolation. Keep it bound to localhost.

```sh
curl -i -X POST 'http://127.0.0.1:8000/datasets/suppliers/imports?key=supplier_id&source_name=before.csv' \
  -H 'Content-Type: text/csv' --data-binary @examples/suppliers-before.csv
curl 'http://127.0.0.1:8000/datasets/suppliers/trace?key=S002'
```

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/datasets/{name}/imports?key=column&source_name=file.csv` | Upload raw CSV bytes |
| GET | `/datasets` | List datasets |
| GET | `/datasets/{name}/imports` | List snapshots, newest first |
| GET | `/imports/{id}` | Import metadata and quality counts |
| GET | `/imports/{id}/records?flagged=true` | Inspect questionable rows |
| GET | `/imports/{id}/source` | Download exact original bytes |
| GET | `/datasets/{name}/trace?key=value` | Trace one normalized entity key |
| GET | `/diff?before=1&after=2` | Compare clean snapshots |
| GET | `/health/live` | Check that the API is responding |
| GET | `/health/ready` | Check that the import table is readable |

Readiness does not prove writes will succeed. Lists and diffs accept `limit` and
`offset` with the same bounds as the CLI. Uploads return `201`, or `200` for an
exact retry, with a `Location` header pointing to the import. Flagged rows still
produce a saved import; inspect `rejected_count` in the response.

Invalid CSV returns `400`, missing resources `404`, conflicting dataset keys or
unclean comparisons `409`, oversized uploads `413`, wrong content type `415`,
and invalid query parameters `422`. Storage errors return `503`. The upload
size is checked as bytes arrive, even without a reliable Content-Length.
The API never fetches remote URLs or opens a client-supplied server file path.

## Docker

```sh
docker compose up --build -d --wait
curl http://127.0.0.1:8000/health/ready
docker compose exec api python -m datatrail datasets
docker compose down
```

The API binds to localhost on port 8000. Set `DATATRAIL_PORT=8001` if that port
is already used. The container runs as a non-root user with a read-only root
filesystem and a writable `/data` volume. The CLI inside the container shares
the API's database. Your ordinary local Python database is separate.

The named `datatrail-data` volume survives container recreation and
`docker compose down`. Adding `--volumes` to `down` deletes that stored data.
There is no background daemon or watcher outside the API container.

## Import rules

- UTF-8 CSV, with an optional BOM; comma delimiter and ordinary CSV quoting.
- At most 5 MiB, 10,000 data records, and 64 columns. Column names must be nonempty,
  unique after trimming, and no longer than 128 characters. Oversized fields are
  also subject to Python's CSV parser limit.
- Header and cell whitespace is trimmed for comparison. Original bytes and cells
  are retained. Values stay strings: `001` and `1` are different keys. Matching
  is case-sensitive; there is no fuzzy matching or automatic type inference.
- A dataset has one fixed key column. All occurrences of duplicate normalized
  keys are flagged, including the first. Missing keys and incorrect column counts
  are flagged too. No arbitrary winner is selected.
- Structurally invalid CSV, invalid encoding, or exceeded limits reject the entire
  file. Row-level issues are saved with their raw cells for investigation.
- Blank physical rows are skipped. A header-only file is a valid empty snapshot.
  Comparing it against a populated snapshot reports removals.

Every import is a full snapshot of one dataset, not a patch or event stream.
Importing the exact same bytes into the same dataset returns the original import
with `replayed: true`, even if the filename changed. Its original source label
and timestamp remain. Whitespace-only file changes create a new snapshot but may
produce no normalized value changes. Versions reflect database commit order,
not dates inside the supplied files.

## Investigation

`records` shows raw cells, normalized data, issue codes, record numbers, and the
starting physical line of each CSV record. Multiline fields retain correct line
references. `trace` finds an exact normalized key across all dataset versions,
including flagged duplicates. Absence from a snapshot is shown by `diff`, not
as a synthetic row in `trace`.

`diff` requires two imports from the same dataset with zero flagged rows. It
reports added, removed, changed, and unchanged counts, plus field-level changes
and the original record numbers. Missing fields are JSON `null`; empty strings
remain `""`. Schema additions and removals are reported separately. Column and
row order do not affect comparison.

Lists accept `--limit` (1–1,000, default 100) and `--offset`. Diffs keep full
summary counts while paginating changed records in key order. Trace results
are newest-version first; records retain source order. An empty result page
means there are no further matching rows.

Exit codes: `0` success, `1` an import saved flagged rows, `2` invalid input,
missing resources, conflicts or storage errors, `130` interrupted.

## Design

`ingest.py` parses and classifies records before opening a write transaction.
`store.py` writes a dataset, import, original file, and all records in one SQLite
transaction. A failure rolls everything back. `BEGIN IMMEDIATE` plus uniqueness
constraints prevents concurrent retries from creating duplicate versions.
Only supplied IDs determine identity; similar names are never silently merged.

Read operations expose saved imports without edit or delete endpoints. This is
application-level history, not tamper-proof storage: someone with direct access
to the database can alter it. Source hashes identify imported bytes; they do not
authenticate the source.

This first version is a local investigation backend. It loads bounded snapshots
into memory for comparisons. It has no users, permissions, background queue,
cross-source entity resolution, or dashboard yet. Larger deployments will need
those boundaries, a database migration strategy, and retention controls.

## Tests

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -v
```

Tests use temporary databases and synthetic fixtures. They exercise duplicate
handling, source preservation, multiline CSVs, atomic rollback, concurrent
retries, concurrent version allocation, schema changes, pagination, and the
documented CLI demo.

API tests cover upload limits, exact source downloads, response codes, storage
failures, shared CLI/API data, and persistence after restarting the application.

With a local Docker runtime and Compose available:

```sh
python3 scripts/docker_smoke.py
```

The integration check creates its own temporary Compose project, image, database
volume, and dynamically assigned localhost port. It exercises the real HTTP
server, duplicate upload handling, quality reports, diffs, exact source downloads,
CLI/API interoperability, and persistence across container recreation. It removes
its containers, volume, network, and image afterward. Regular workspace data is
not used. GitHub Actions runs this check plus the Python suite on 3.11 and 3.14.
