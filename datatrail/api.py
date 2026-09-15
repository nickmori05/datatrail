from contextlib import asynccontextmanager, closing
import hashlib
import os
from pathlib import Path
import sqlite3

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .errors import Conflict, InvalidInput, NotFound
from .ingest import MAX_BYTES
from .store import Store


async def read_csv(request: Request) -> bytes:
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
            raise HTTPException(413, "CSV files must be at most 5 MiB.")
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BYTES:
            raise HTTPException(413, "CSV files must be at most 5 MiB.")
        chunks.append(chunk)
    return b"".join(chunks)


def create_app(database: Path | None = None) -> FastAPI:
    store = Store(database if database is not None else Path(os.environ.get("DATATRAIL_DB", ".local/datatrail.sqlite3")))

    @asynccontextmanager
    async def lifespan(app):
        await run_in_threadpool(store.initialize)
        yield

    app = FastAPI(title="Datatrail", version="0.1.0", lifespan=lifespan,
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
                         source_name: str = Query(default="upload.csv", min_length=1, max_length=255)):
        content = await read_csv(request)
        result = await run_in_threadpool(store.import_csv, dataset, key, content, source_name)
        response.status_code = 200 if result["replayed"] else 201
        response.headers["Location"] = f"/imports/{result['id']}"
        return result

    @app.get("/datasets")
    def datasets(limit: int = Query(default=100, ge=1, le=1000), offset: int = Query(default=0, ge=0)):
        return store.datasets(limit, offset)

    @app.get("/datasets/{dataset}/imports")
    def imports(dataset: str, limit: int = Query(default=100, ge=1, le=1000), offset: int = Query(default=0, ge=0)):
        return store.imports(dataset, limit, offset)

    @app.get("/imports/{identifier}")
    def get_import(identifier: int):
        return store.get_import(identifier)

    @app.get("/imports/{identifier}/records")
    def records(identifier: int, flagged: bool = False, limit: int = Query(default=100, ge=1, le=1000),
                offset: int = Query(default=0, ge=0)):
        return store.records(identifier, flagged=flagged, limit=limit, offset=offset)

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

    @app.get("/diff")
    def diff(before: int, after: int, limit: int = Query(default=100, ge=1, le=1000), offset: int = Query(default=0, ge=0)):
        return store.diff(before, after, limit, offset)

    return app


app = create_app()
