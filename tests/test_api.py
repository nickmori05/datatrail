import csv
import io
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

    def test_diff_filters_by_repeated_kind_parameters(self):
        before = self.upload(b"id,name\nA,Ada\nB,Bob\n").json()
        after = self.upload(b"id,name\nA,Ada Lovelace\nC,Cam\n").json()
        params = {"before": before["id"], "after": after["id"]}
        response = self.client.get("/diff", params={**params, "kind": ["removed", "added"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([change["kind"] for change in response.json()["changes"]], ["removed", "added"])
        self.assertEqual(response.json()["kinds"], ["added", "removed"])
        self.assertEqual(response.json()["counts"]["changed"], 1)
        self.assertEqual(self.client.get("/diff", params={**params, "kind": "unchanged"}).status_code, 422)

    def test_latest_changes_endpoint_skips_flagged_snapshots(self):
        self.assertEqual(self.client.get("/datasets/suppliers/changes").status_code, 404)
        self.upload(b"id,name\nA,Ada\nB,Bob\n")
        self.assertEqual(self.client.get("/datasets/suppliers/changes").status_code, 409)
        self.upload(b"id,name\nA,Ada\nC,Cam\n")
        dirty = self.upload(b"id,name\nA,Ada\nA,Other\n").json()
        response = self.client.get("/datasets/suppliers/changes", params={"kind": "added"})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual((result["before_version"], result["after_version"]), (1, 2))
        self.assertEqual([item["id"] for item in result["skipped_flagged"]], [dirty["id"]])
        self.assertEqual([change["key"] for change in result["changes"]], ["C"])
        self.assertEqual(result["counts"]["removed"], 1)

    def test_summary_endpoint_lists_versions_newest_first(self):
        self.assertEqual(self.client.get("/datasets/suppliers/summary").status_code, 404)
        self.upload(b"id,name\nA,Ada\nB,Bob\n")
        self.upload(b"id,name\nA,Ada\nA,Other\n")
        self.upload(b"id,name\nA,Ada Lovelace\nC,Cam\n")
        response = self.client.get("/datasets/suppliers/summary", params={"limit": 2})
        self.assertEqual(response.status_code, 200, response.text)
        summary = response.json()
        self.assertEqual([(item["version"], item["clean"]) for item in summary["versions"]], [(3, True), (2, False)])
        self.assertEqual(summary["versions"][0]["changes"], {"compared_with": 1, "added": 1, "removed": 1,
                                                             "changed": 1, "unchanged": 0})
        self.assertEqual(summary["versions"][1]["issue_counts"]["duplicate_key"], 2)
        self.assertEqual(self.client.get("/datasets/suppliers/summary", params={"limit": 0}).status_code, 422)

    def test_latest_changes_since_selects_baseline_version(self):
        self.upload(b"id,name\nA,Ada\n")
        self.upload(b"id,name\nA,Ada\nB,Bob\n")
        self.upload(b"id,name\nA,Ada\nB,Bob\nC,Cam\n")
        response = self.client.get("/datasets/suppliers/changes", params={"since": 1})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual((result["before_version"], result["after_version"]), (1, 3))
        self.assertEqual([change["key"] for change in result["changes"]], ["B", "C"])
        self.assertEqual(self.client.get("/datasets/suppliers/changes", params={"since": 3}).status_code, 409)
        self.assertEqual(self.client.get("/datasets/suppliers/changes", params={"since": 9}).status_code, 404)
        for since in (0, "first"):
            self.assertEqual(self.client.get("/datasets/suppliers/changes", params={"since": since}).status_code, 422)

    def test_latest_changes_until_selects_end_version(self):
        self.upload(b"id,name\nA,Ada\n")
        self.upload(b"id,name\nA,Ada\nB,Bob\n")
        self.upload(b"id,name\nA,Ada\nA,Other\n")
        self.upload(b"id,name\nA,Ada\nB,Bob\nC,Cam\n")
        response = self.client.get("/datasets/suppliers/changes", params={"until": 2})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual((result["before_version"], result["after_version"]), (1, 2))
        self.assertEqual(([change["key"] for change in result["changes"]], result["skipped_flagged"]), (["B"], []))
        ranged = self.client.get("/datasets/suppliers/changes", params={"since": 1, "until": 4}).json()
        self.assertEqual([item["version"] for item in ranged["skipped_flagged"]], [3])
        for params, status in (({"until": 3}, 409), ({"until": 1}, 409), ({"until": 9}, 404),
                               ({"since": 2, "until": 2}, 400), ({"until": 0}, 422), ({"until": "last"}, 422)):
            self.assertEqual(self.client.get("/datasets/suppliers/changes", params=params).status_code, status, params)

    def test_history_endpoint_lists_a_keys_changes(self):
        self.assertEqual(self.client.get("/datasets/suppliers/history", params={"key": "A"}).status_code, 404)
        self.upload(b"id,name\nA,Ada\n")
        self.upload(b"id,name\nA,Ada\nA,Other\n")
        self.upload(b"id,name\nA,Ada Lovelace\n")
        response = self.client.get("/datasets/suppliers/history", params={"key": "A"})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        summary = [(event["version"], event["kind"]) for event in result["events"]]
        self.assertEqual(summary, [(3, "changed"), (1, "added")])
        self.assertEqual(result["latest"]["data"], {"id": "A", "name": "Ada Lovelace"})
        self.assertEqual([item["version"] for item in result["skipped_flagged"]], [2])
        for params in ({}, {"key": ""}, {"key": "A", "limit": 0}, {"key": "A", "offset": -1}):
            self.assertEqual(self.client.get("/datasets/suppliers/history", params=params).status_code, 422, params)

    def test_comparisons_accept_repeated_ignore_parameters(self):
        before = self.upload(b"id,name,synced\nA,Ada,1\nB,Bob,1\n").json()
        after = self.upload(b"id,name,synced\nA,Ada,2\nB,Bill,2\n").json()
        ignore = {"ignore": ["synced", "synced"]}
        diff = self.client.get("/diff", params={"before": before["id"], "after": after["id"], **ignore}).json()
        self.assertEqual((diff["ignored"], diff["counts"]["changed"], diff["counts"]["unchanged"]), (["synced"], 1, 1))
        changes = self.client.get("/datasets/suppliers/changes", params={"format": "csv", **ignore})
        self.assertEqual(changes.text, "key,kind,field,before,after,before_record,after_record\r\n"
                                       "B,changed,name,Bob,Bill,2,2\r\n")
        history = self.client.get("/datasets/suppliers/history", params={"key": "A", **ignore}).json()
        self.assertEqual([event["version"] for event in history["events"]], [1])
        summary = self.client.get("/datasets/suppliers/summary", params=ignore).json()
        self.assertEqual(summary["versions"][0]["changes"]["changed"], 1)
        for path, params in (("/datasets/suppliers/summary", {"ignore": "id"}),
                             ("/datasets/suppliers/changes", {"ignore": "missing"}),
                             ("/datasets/suppliers/history", {"key": "A", "ignore": " "})):
            with self.subTest(path=path, params=params):
                self.assertEqual(self.client.get(path, params=params).status_code, 400)
        self.assertEqual(self.client.get("/datasets/missing/summary", params={"ignore": "name"}).status_code, 404)
        operations = self.client.get("/openapi.json").json()["paths"]
        for path in ("/diff", "/datasets/{dataset}/changes", "/datasets/{dataset}/history",
                     "/datasets/{dataset}/summary"):
            self.assertIn("ignore", {item["name"] for item in operations[path]["get"]["parameters"]})

    def test_diff_and_changes_download_as_csv(self):
        before = self.upload(b"id,name\nA,Ada\nB,Bob\n").json()
        self.upload(b"id,name\nA,Ada\nA,Other\n")
        after = self.upload(b"id,name\nA,Ada Lovelace\nC,Cam\n").json()
        response = self.client.get("/diff", params={"before": before["id"], "after": after["id"], "format": "csv",
                                                    "kind": ["changed", "added"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "text/csv; charset=utf-8")
        self.assertEqual(response.headers["content-disposition"],
                         f'attachment; filename="diff-{before["id"]}-{after["id"]}.csv"')
        rows = list(csv.reader(io.StringIO(response.text, newline="")))
        self.assertEqual(rows[1:], [["A", "changed", "name", "Ada", "Ada Lovelace", "1", "1"],
                                    ["C", "added", "id", "", "C", "", "2"], ["C", "added", "name", "", "Cam", "", "2"]])
        changes = self.client.get("/datasets/suppliers/changes", params={"format": "csv", "kind": "removed"})
        self.assertEqual(changes.status_code, 200, changes.text)
        self.assertEqual([row[0] for row in csv.reader(io.StringIO(changes.text, newline=""))], ["key", "B", "B"])
        self.assertEqual((changes.headers["x-datatrail-before-version"], changes.headers["x-datatrail-after-version"],
                          changes.headers["x-datatrail-skipped-flagged"]), ("1", "3", "2"))
        self.assertIn('filename="suppliers-v1-v3.csv"', changes.headers["content-disposition"])
        self.assertEqual(self.client.get("/diff", params={"before": before["id"], "after": after["id"]})
                         .headers["content-type"], "application/json")
        for path, params, status in (("/diff", {"before": before["id"], "after": 2}, 409),
                                     ("/diff", {"before": before["id"], "after": 99}, 404),
                                     ("/diff", {"before": before["id"], "after": after["id"], "limit": 10}, 422),
                                     ("/datasets/suppliers/changes", {"offset": 0}, 422),
                                     ("/datasets/suppliers/changes", {"until": 2}, 409),
                                     ("/datasets/missing/changes", {}, 404),
                                     ("/datasets/suppliers/changes", {"format": "xlsx"}, 422)):
            response = self.client.get(path, params={"format": "csv", **params})
            self.assertEqual(response.status_code, status, (path, params))
            self.assertNotIn("text/csv", response.headers["content-type"])

    def test_spreadsheet_csv_escapes_formulas_and_adds_a_byte_order_mark(self):
        before = self.upload(b"id,note\nA,ok\nB,-5\n").json()
        after = self.upload(b"id,note\nA,@SUM(1)\nB,-6\n").json()
        expected = ("\ufeffkey,kind,field,before,after,before_record,after_record\r\n"
                    "A,changed,note,ok,'@SUM(1),1,1\r\nB,changed,note,-5,-6,2,2\r\n").encode()
        paths = (("/diff", {"before": before["id"], "after": after["id"]}), ("/datasets/suppliers/changes", {}))
        for path, params in paths:
            response = self.client.get(path, params={"format": "csv", "spreadsheet": "true", **params})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["content-type"], "text/csv; charset=utf-8")
            self.assertEqual(response.content, expected)
            plain = self.client.get(path, params={"format": "csv", "spreadsheet": "false", **params})
            self.assertEqual(plain.content, expected.removeprefix("\ufeff".encode()).replace(b"'@", b"@"))
            for extra in ({"spreadsheet": "true"}, {"format": "json", "spreadsheet": "true"},
                          {"format": "csv", "spreadsheet": "maybe"}):
                rejected = self.client.get(path, params={**params, **extra})
                self.assertEqual(rejected.status_code, 422, extra)
        operation = self.client.get("/openapi.json").json()["paths"]["/diff"]["get"]
        self.assertIn("spreadsheet", {parameter["name"] for parameter in operation["parameters"]})

    def test_flagged_import_is_saved_but_cannot_be_compared(self):
        clean = self.upload().json()
        dirty = self.upload(b"id,name\nA,Ada\nB,Bob\nB,Bill\n")
        self.assertEqual(dirty.status_code, 201)
        self.assertEqual(dirty.json()["rejected_count"], 2)
        details = self.client.get(f"/imports/{dirty.json()['id']}").json()
        self.assertEqual(details["issue_counts"], {"column_count": 0, "missing_key": 0, "duplicate_key": 2,
                                                  "invalid_value": 0, "missing_value": 0})
        rows = self.client.get(f"/imports/{dirty.json()['id']}/records",
                               params={"flagged": True, "limit": 1, "offset": 1}).json()
        self.assertEqual(rows[0]["number"], 3)
        response = self.client.get("/diff", params={"before": clean["id"], "after": dirty.json()["id"]})
        self.assertEqual(response.status_code, 409)
        self.assertIn("flagged", response.json()["detail"])

    def test_upload_type_parameters_flag_invalid_values(self):
        response = self.upload(b"id,price,due\nA,9.50,2026-01-31\nB,9.5.0,2026-13-01\n", dataset="orders",
                               type=["price:decimal", "due:date"])
        self.assertEqual(response.status_code, 201, response.text)
        result = response.json()
        self.assertEqual((result["column_types"], result["rejected_count"]), ({"due": "date", "price": "decimal"}, 1))
        rows = self.client.get(f"/imports/{result['id']}/records", params={"issue": "invalid_value"}).json()
        self.assertEqual([row["invalid_columns"] for row in rows], [["due", "price"]])
        self.assertEqual(self.upload(b"id,price,due\nA,1,\n", dataset="orders").json()["rejected_count"], 0)
        self.assertEqual(self.upload(b"id,price,due\nA,1,\n", dataset="orders", type="price:integer").status_code, 409)
        self.assertEqual(self.upload(b"id,price\nA,1\n", dataset="fresh", type="price:money").status_code, 400)
        self.assertEqual(self.client.get("/datasets").json()[0]["column_types"], {"due": "date", "price": "decimal"})

    def test_upload_rule_parameters_flag_missing_and_unexpected_values(self):
        content = b"id,email,region\nA,a@x,east\nB,,west\nC,c@x,north\nD,d,east\n"
        rules = {"required": "email", "allow": ["region:east", "region:west"], "pattern": "email:[^@]+@[^@]+"}
        response = self.upload(content, dataset="contacts", **rules)
        self.assertEqual(response.status_code, 201, response.text)
        result = response.json()
        self.assertEqual(result["rules"]["allowed"], {"region": ["east", "west"]})
        self.assertEqual((result["issue_counts"]["missing_value"], result["issue_counts"]["invalid_value"]), (1, 2))
        rows = self.client.get(f"/imports/{result['id']}/records", params={"issue": "missing_value"}).json()
        self.assertEqual([(row["number"], row["failed_rules"]) for row in rows], [(2, {"email": ["required"]})])
        self.assertEqual(self.upload(b"id,email,region\nA,,east\n", dataset="contacts").json()["rejected_count"], 1)
        self.assertEqual(self.upload(content, dataset="contacts", required="region").status_code, 409)
        for params in ({"pattern": "email:("}, {"allow": "region"}, {"required": "missing"}):
            with self.subTest(params=params):
                self.assertEqual(self.upload(content, dataset="fresh", **params).status_code, 400)
        self.assertEqual(self.client.get("/datasets").json()[0]["rules"], result["rules"])
        parameters = self.client.get("/openapi.json").json()["paths"]["/datasets/{dataset}/imports"]["post"]
        self.assertTrue({"required", "allow", "pattern"} <= {item["name"] for item in parameters["parameters"]})

    def test_upload_range_parameters_flag_values_outside_the_bounds(self):
        content = b"id,qty,due\nA,5,2026-01-15\nB,0,2026-01-15\nC,12,2025-12-31\nD,,\n"
        params = {"type": ["qty:integer", "due:date"], "min": ["qty:1", "due:2026-01-01"], "max": "qty:10"}
        response = self.upload(content, dataset="orders", **params)
        self.assertEqual(response.status_code, 201, response.text)
        result = response.json()
        self.assertEqual((result["rules"]["minimum"], result["rules"]["maximum"]),
                         ({"due": "2026-01-01", "qty": "1"}, {"qty": "10"}))
        rows = self.client.get(f"/imports/{result['id']}/records", params={"flagged": True}).json()
        self.assertEqual([row["failed_rules"] for row in rows], [{"qty": ["min"]}, {"due": ["min"], "qty": ["max"]}])
        self.assertEqual(self.upload(b"id,qty,due\nA,10,\n", dataset="orders").json()["rejected_count"], 0)
        self.assertEqual(self.upload(content, dataset="orders", **{**params, "max": "qty:+010"}).status_code, 200)
        self.assertEqual(self.upload(content, dataset="orders", **{**params, "max": "qty:11"}).status_code, 409)
        for extra in ({"min": "qty:1"}, {"type": "qty:integer", "min": "qty:x"},
                      {"type": "qty:integer", "min": "qty:9", "max": "qty:1"}):
            with self.subTest(params=extra):
                self.assertEqual(self.upload(content, dataset="fresh", **extra).status_code, 400)
        parameters = self.client.get("/openapi.json").json()["paths"]["/datasets/{dataset}/imports"]["post"]
        self.assertTrue({"min", "max"} <= {item["name"] for item in parameters["parameters"]})

    def test_records_filter_by_repeated_issue_parameters(self):
        dirty = self.upload(b"id,name\nA,Ada\nB,Bob\nB,Bill\n,Missing\nC\n").json()
        path = f"/imports/{dirty['id']}/records"
        response = self.client.get(path, params={"issue": "duplicate_key"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([row["number"] for row in response.json()], [2, 3])
        both = self.client.get(path, params={"issue": ["column_count", "missing_key"], "limit": 1, "offset": 1}).json()
        self.assertEqual([row["issues"] for row in both], [["column_count"]])
        self.assertEqual(self.client.get(path, params={"issue": "unknown"}).status_code, 422)
        self.assertEqual(self.client.get("/imports/999/records", params={"issue": "missing_key"}).status_code, 404)

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
                async def receive(chunks=chunks):
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
