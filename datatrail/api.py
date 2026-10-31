"""HTTP API over the same store and database as the CLI."""

from contextlib import asynccontextmanager, closing
import hashlib
from pathlib import Path
import sqlite3
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from . import __version__
from .errors import Conflict, InvalidInput, NotFound
from .export import csv_chunks
from .ingest import DELIMITERS, ISSUE_CODES, MAX_BYTES
from .store import CHANGE_KINDS, Store, default_database


TOO_LARGE = f"CSV files must be at most {MAX_BYTES // (1024 * 1024)} MiB."
CSV_RESPONSE = {200: {"content": {"text/csv": {"schema": {"type": "string"}}},
                      "description": "JSON by default; with format=csv, one row per changed field"}}
Spreadsheet = Annotated[bool, Query(description="With format=csv, add a byte order mark and escape values that "
                                                 "spreadsheets would run as formulas")]


async def read_csv(request: Request) -> bytes:
    """Read a text/csv request body, stopping as soon as it exceeds the size limit."""
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "text/csv":
        raise HTTPException(415, "Send the CSV body with Content-Type: text/csv.")
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError:
            raise HTTPException(400, "Invalid Content-Length.") from None
        if length < 0:
            raise HTTPException(400, "Invalid Content-Length.")
        if length > MAX_BYTES:
            raise HTTPException(413, TOO_LARGE)
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BYTES:
            raise HTTPException(413, TOO_LARGE)
        chunks.append(chunk)
    return b"".join(chunks)


def check_export(request: Request, output: str, spreadsheet: bool):
    """Reject options that do not apply to the chosen format.

    CSV exports are never paginated, so explicit limit or offset parameters are a mistake, and only CSV
    exports can be prepared for spreadsheets.
    """
    if output == "json":
        if spreadsheet:
            raise HTTPException(422, "spreadsheet only applies to format=csv.")
    elif {"limit", "offset"} & request.query_params.keys():
        raise HTTPException(422, "CSV exports include every change; leave out limit and offset.")


def csv_response(changes, filename: str, spreadsheet: bool, headers=None) -> StreamingResponse:
    """Stream changes as a CSV attachment. Errors must be raised before this, since the status is sent first."""
    return StreamingResponse(csv_chunks(changes, spreadsheet), media_type="text/csv; charset=utf-8", headers={
        **(headers or {}), "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Content-Type-Options": "nosniff",
    })


def create_app(database: Path | None = None) -> FastAPI:
    """Build the API for a database path, defaulting to the CLI's database setting."""
    store = Store(database if database is not None else default_database())

    @asynccontextmanager
    async def lifespan(app):
        await run_in_threadpool(store.initialize)
        yield

    app = FastAPI(title="Datatrail", version=__version__, lifespan=lifespan,
                  description="Local CSV snapshot investigation with original-source provenance.")

    @app.exception_handler(InvalidInput)
    async def invalid_input(request, error):
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(NotFound)
    async def not_found(request, error):
        return JSONResponse({"detail": str(error)}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request, error):
        return JSONResponse({"detail": "Storage is unavailable."}, status_code=503)

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        with closing(store.connect()) as connection:
            connection.execute("SELECT id FROM imports LIMIT 1").fetchone()
        return {"status": "ready"}

    @app.post("/datasets/{dataset}/imports", status_code=201, openapi_extra={
        "requestBody": {"required": True, "content": {"text/csv": {
            "schema": {"type": "string"}, "example": "supplier_id,name\nS001,Northline Parts\n"
        }}}
    })
    async def import_csv(dataset: str, request: Request, response: Response,
                         key: str = Query(min_length=1, max_length=128),
                         source_name: str = Query(default="upload.csv", min_length=1, max_length=255),
                         delimiter: Literal[tuple(DELIMITERS)] = Query(
                             default="comma", description="The character between fields in this file"),
                         column_types: list[str] = Query(default=[], alias="type"),
                         required: list[str] = Query(default=[], description="A column that must not be empty"),
                         allowed: list[str] = Query(default=[], alias="allow",
                                                    description="COLUMN:VALUE, one accepted value per parameter"),
                         patterns: list[str] = Query(default=[], alias="pattern",
                                                     description="COLUMN:PATTERN, a Python regular expression that "
                                                                 "nonempty values must fully match")):
        content = await read_csv(request)
        result = await run_in_threadpool(store.import_csv, dataset, key, content, source_name, column_types,
                                         required, allowed, patterns, delimiter)
        response.status_code = 200 if result["replayed"] else 201
        response.headers["Location"] = f"/imports/{result['id']}"
        return result

    @app.get("/datasets")
    def datasets(limit: int = Query(default=100, ge=1, le=1000), offset: int = Query(default=0, ge=0)):
        return store.datasets(limit, offset)

    @app.get("/datasets/{dataset}/imports")
    def imports(dataset: str, limit: int = Query(default=100, ge=1, le=1000), offset: int = Query(default=0, ge=0)):
        return store.imports(dataset, limit, offset)

    @app.get("/datasets/{dataset}/summary")
    def summary(dataset: str, limit: int = Query(default=100, ge=1, le=1000), offset: int = Query(default=0, ge=0)):
        return store.summary(dataset, limit, offset)

    @app.get("/imports/{identifier}")
    def get_import(identifier: int):
        return store.get_import(identifier)

    @app.get("/imports/{identifier}/records")
    def records(identifier: int, flagged: bool = False, limit: int = Query(default=100, ge=1, le=1000),
                offset: int = Query(default=0, ge=0),
                issue: list[Literal[ISSUE_CODES]] = Query(default=[])):
        return store.records(identifier, flagged=flagged, issues=issue, limit=limit, offset=offset)

    @app.get("/imports/{identifier}/source")
    def source(identifier: int):
        content = store.source(identifier)
        return Response(content, media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="import-{identifier}.csv"',
            "X-Content-Type-Options": "nosniff", "ETag": f'"{hashlib.sha256(content).hexdigest()}"',
        })

    @app.get("/datasets/{dataset}/trace")
    def trace(dataset: str, key: str = Query(min_length=1), limit: int = Query(default=100, ge=1, le=1000),
              offset: int = Query(default=0, ge=0)):
        return store.trace(dataset, key, limit, offset)

    @app.get("/datasets/{dataset}/history")
    def history(dataset: str, key: str = Query(min_length=1), limit: int = Query(default=100, ge=1, le=1000),
                offset: int = Query(default=0, ge=0)):
        return store.history(dataset, key, limit, offset)

    @app.get("/datasets/{dataset}/changes", responses=CSV_RESPONSE)
    def changes(dataset: str, request: Request, limit: int = Query(default=100, ge=1, le=1000),
                offset: int = Query(default=0, ge=0), kind: list[Literal[CHANGE_KINDS]] = Query(default=[]),
                since: int | None = Query(default=None, ge=1), until: int | None = Query(default=None, ge=1),
                output: Literal["json", "csv"] = Query(default="json", alias="format"),
                spreadsheet: Spreadsheet = False):
        check_export(request, output, spreadsheet)
        if output == "json":
            return store.latest_changes(dataset, limit, offset, kinds=kind, since=since, until=until)
        selected = store.select_versions(dataset, since=since, until=until)
        before, after = selected["before_version"], selected["after_version"]
        skipped = ",".join(str(item["version"]) for item in selected["skipped_flagged"])
        headers = {"X-Datatrail-Before-Version": str(before), "X-Datatrail-After-Version": str(after),
                   "X-Datatrail-Skipped-Flagged": skipped}
        return csv_response(store.diff_changes(selected["before_id"], selected["after_id"], kinds=kind),
                            f"{dataset}-v{before}-v{after}.csv", spreadsheet, headers)

    @app.get("/diff", responses=CSV_RESPONSE)
    def diff(before: int, after: int, request: Request, limit: int = Query(default=100, ge=1, le=1000),
             offset: int = Query(default=0, ge=0), kind: list[Literal[CHANGE_KINDS]] = Query(default=[]),
             output: Literal["json", "csv"] = Query(default="json", alias="format"), spreadsheet: Spreadsheet = False):
        check_export(request, output, spreadsheet)
        if output == "json":
            return store.diff(before, after, limit, offset, kinds=kind)
        return csv_response(store.diff_changes(before, after, kinds=kind), f"diff-{before}-{after}.csv",
                            spreadsheet)

    return app


app = create_app()
