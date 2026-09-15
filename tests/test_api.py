from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from datatrail.api import create_app, read_csv
from datatrail.store import Store


class ApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "data.sqlite3"
        self.client = self.enterContext(TestClient(create_app(self.database)))

    def upload(self, content=b"id,name\nA,Ada\n", dataset="suppliers", **params):
        return self.client.post(f"/datasets/{dataset}/imports", params={"key": "id", **params},
                                content=content, headers={"Content-Type": "text/csv"})

    def test_import_replay_location_and_exact_source_download(self):
        content = b"id,name\r\n A , Ada \r\n"
        response = self.upload(content, source_name="source.csv")
        self.assertEqual(response.status_code, 201, response.text)
        identifier = response.json()["id"]
        self.assertEqual(response.headers["location"], f"/imports/{identifier}")
        replay = self.upload(content, source_name="renamed.csv")
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(replay.json()["source_name"], "source.csv")
        source = self.client.get(f"/imports/{identifier}/source")
        self.assertEqual(source.content, content)
        self.assertEqual(source.headers["etag"], f'"{response.json()["sha256"]}"')
        self.assertIn("attachment", source.headers["content-disposition"])
        self.assertEqual(self.client.get(response.headers["location"]).json()["id"], identifier)

    def test_diff_and_trace_share_cli_storage(self):
        store = Store(self.database)
        before = store.import_csv("suppliers", "id", b"id,name\nA,Ada\n", "before.csv")
        after = self.upload(b"id,name\nA,Ada Lovelace\nB,Bob\n").json()
        diff = self.client.get("/diff", params={"before": before["id"], "after": after["id"], "limit": 1}).json()
        self.assertEqual(diff["counts"], {"added": 1, "removed": 0, "changed": 1, "unchanged": 0})
        self.assertEqual(diff["total_changes"], 2)
        self.assertEqual(len(diff["changes"]), 1)
        trail = self.client.get("/datasets/suppliers/trace", params={"key": "A"}).json()
        self.assertEqual([row["version"] for row in trail], [2, 1])
        self.assertEqual(len(store.imports("suppliers")), 2)

    def test_flagged_import_is_saved_but_cannot_be_compared(self):
        clean = self.upload().json()
        dirty = self.upload(b"id,name\nA,Ada\nB,Bob\nB,Bill\n")
        self.assertEqual(dirty.status_code, 201)
        self.assertEqual(dirty.json()["rejected_count"], 2)
        rows = self.client.get(f"/imports/{dirty.json()['id']}/records", params={"flagged": True, "limit": 1, "offset": 1}).json()
        self.assertEqual(rows[0]["number"], 3)
        response = self.client.get("/diff", params={"before": clean["id"], "after": dirty.json()["id"]})
        self.assertEqual(response.status_code, 409)
        self.assertIn("flagged", response.json()["detail"])

    def test_bad_encoding_csv_and_dataset_names_are_rejected_without_imports(self):
        for content in (b"name\nAda", b"id,name\nA,\xff", b'id,name\nA,"unfinished'):
            with self.subTest(content=content):
                self.assertEqual(self.upload(content).status_code, 400)
        self.assertEqual(self.upload(dataset="has spaces").status_code, 400)
        self.assertEqual(self.client.get("/datasets").json(), [])

    def test_missing_resources_and_validation_use_distinct_statuses(self):
        self.assertEqual(self.client.get("/imports/999").status_code, 404)
        self.assertEqual(self.client.get("/datasets/missing/imports").status_code, 404)
        self.assertEqual(self.client.get("/datasets?limit=0").status_code, 422)
        self.assertEqual(self.client.post("/datasets/suppliers/imports", content=b"id\nA").status_code, 422)
        self.upload()
        self.assertEqual(self.upload(key="name").status_code, 409)

    def test_content_type_and_declared_size_are_checked_before_storage(self):
        self.assertEqual(self.client.post("/datasets/suppliers/imports?key=id", json={"id": "A"}).status_code, 415)
        response = self.client.post("/datasets/suppliers/imports?key=id", content=b"id\nA",
                                    headers={"Content-Type": "text/csv", "Content-Length": str(6 * 1024 * 1024)})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.client.get("/datasets").json(), [])

    def test_database_failure_does_not_leak_paths_and_liveness_stays_up(self):
        with patch.object(Store, "connect", side_effect=sqlite3.OperationalError("private/path/data.sqlite3")):
            self.assertEqual(self.client.get("/health/live").status_code, 200)
            ready = self.client.get("/health/ready")
            self.assertEqual(ready.status_code, 503)
            self.assertEqual(ready.json(), {"detail": "Storage is unavailable."})
        self.assertEqual(self.client.get("/health/ready").status_code, 200)

    def test_restart_retains_uploaded_snapshots(self):
        identifier = self.upload().json()["id"]
        with TestClient(create_app(self.database)) as restarted:
            self.assertEqual(restarted.get(f"/imports/{identifier}").status_code, 200)
            self.assertEqual(len(restarted.get("/datasets/suppliers/imports").json()), 1)

    def test_openapi_documents_csv_request_body(self):
        operation = self.client.get("/openapi.json").json()["paths"]["/datasets/{dataset}/imports"]["post"]
        self.assertIn("text/csv", operation["requestBody"]["content"])


class UploadStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_limit_is_enforced_without_a_trustworthy_content_length(self):
        for headers in ([(b"content-type", b"text/csv")],
                        [(b"content-type", b"text/csv"), (b"content-length", b"1")]):
            with self.subTest(headers=headers):
                chunks = iter([b"id\n", b"AAAA", b"BBBB"])
                async def receive():
                    return {"type": "http.request", "body": next(chunks), "more_body": True}
                request = Request({"type": "http", "headers": headers}, receive=receive)
                with patch("datatrail.api.MAX_BYTES", 8):
                    with self.assertRaises(HTTPException) as error:
                        await read_csv(request)
                self.assertEqual(error.exception.status_code, 413)

    async def test_invalid_content_lengths_are_rejected(self):
        for value in (b"-1", b"nonsense"):
            request = Request({"type": "http", "headers": [(b"content-type", b"text/csv"), (b"content-length", value)]})
            with self.assertRaises(HTTPException) as error:
                await read_csv(request)
            self.assertEqual(error.exception.status_code, 400)
