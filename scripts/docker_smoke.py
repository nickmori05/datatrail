"""Build the Docker image, run the API in an isolated Compose project and exercise it over HTTP."""

import json
import os
from pathlib import Path
import subprocess
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]


def main():
    project = f"datatrail-smoke-{uuid4().hex[:12]}"
    image = f"datatrail-smoke:{project}"
    environment = {**os.environ, "DATATRAIL_IMAGE": image, "DATATRAIL_PORT": "0"}
    compose = ["docker", "compose", "--project-name", project, "--file", str(ROOT / "compose.yml")]

    def run(*arguments, timeout=120):
        result = subprocess.run(compose + list(arguments), cwd=ROOT, env=environment,
                                text=True, capture_output=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(f"{arguments}\n{result.stdout}\n{result.stderr}")
        return result.stdout

    def endpoint():
        return "http://" + run("port", "api", "8000").strip()

    def request(base, path, content=None, expected=200, raw=False):
        headers = {"Content-Type": "text/csv"} if content is not None else {}
        message = Request(base + path, data=content, headers=headers)
        try:
            response = urlopen(message, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            body = response.read()
            if response.status != expected:
                raise RuntimeError(f"{path}: expected {expected}, got {response.status}: {body!r}")
            return body if raw else json.loads(body)

    try:
        print("Building and starting the isolated API...", flush=True)
        run("build", "api", timeout=300)
        run("up", "--detach", "--wait", "--wait-timeout", "45", "api")
        base = endpoint()
        assert run("exec", "-T", "api", "id", "-u").strip() == "10001"
        before_bytes = (ROOT / "examples/suppliers-before.csv").read_bytes()
        after_bytes = (ROOT / "examples/suppliers-after.csv").read_bytes()
        messy_bytes = (ROOT / "examples/suppliers-messy.csv").read_bytes()
        upload = "/datasets/suppliers/imports?key=supplier_id"
        before = request(base, upload, before_bytes, expected=201)
        retry = request(base, upload, before_bytes)
        assert retry["replayed"] and retry["id"] == before["id"], retry
        after = request(base, upload, after_bytes, expected=201)
        comparison = request(base, f"/diff?before={before['id']}&after={after['id']}")
        assert comparison["counts"] == {"added": 1, "removed": 1, "changed": 1, "unchanged": 1}, comparison
        assert request(base, f"/imports/{before['id']}/source", raw=True) == before_bytes
        messy = request(base, upload, messy_bytes, expected=201)
        assert messy["rejected_count"] == 4, messy
        assert len(request(base, f"/imports/{messy['id']}/records?flagged=true")) == 4
        assert messy["issue_counts"] == {"column_count": 1, "missing_key": 1, "duplicate_key": 2,
                                         "invalid_value": 0, "missing_value": 0}, messy
        duplicates = request(base, f"/imports/{messy['id']}/records?issue=duplicate_key")
        assert [row["number"] for row in duplicates] == [2, 3], duplicates
        request(base, f"/imports/{messy['id']}/records?issue=unknown", expected=422)
        invoice_bytes = (ROOT / "examples/invoices.csv").read_bytes()
        typed = "/datasets/invoices/imports?key=invoice_id&type=amount:decimal&type=due_date:date"
        invoices = request(base, typed, invoice_bytes, expected=201)
        assert (invoices["rejected_count"], invoices["issue_counts"]["invalid_value"]) == (2, 2), invoices
        invalid = request(base, f"/imports/{invoices['id']}/records?issue=invalid_value")
        assert [row["invalid_columns"] for row in invalid] == [["amount"], ["due_date"]], invalid
        request(base, "/datasets/invoices/imports?key=invoice_id&type=amount:integer", invoice_bytes, expected=409)
        request(base, "/datasets/other/imports?key=invoice_id&type=amount:money", invoice_bytes, expected=400)
        contact_bytes = (ROOT / "examples/contacts.csv").read_bytes()
        ruled = ("/datasets/contacts/imports?key=contact_id&required=email&allow=region:east&allow=region:west"
                 "&pattern=supplier_id:S%5B0-9%5D%7B3%7D")
        contacts = request(base, ruled, contact_bytes, expected=201)
        counts = contacts["issue_counts"]
        assert (counts["missing_value"], counts["invalid_value"]) == (1, 2), contacts
        failed = request(base, f"/imports/{contacts['id']}/records?flagged=true")
        assert [row["failed_rules"] for row in failed] == [{"email": ["required"]}, {"supplier_id": ["pattern"]},
                                                           {"region": ["allowed"]}], failed
        request(base, "/datasets/contacts/imports?key=contact_id&required=region", contact_bytes, expected=409)
        request(base, "/datasets/other/imports?key=contact_id&pattern=email:(", contact_bytes, expected=400)
        request(base, f"/diff?before={before['id']}&after={messy['id']}", expected=409)
        latest = request(base, "/datasets/suppliers/changes?kind=changed")
        assert (latest["before_id"], latest["after_id"]) == (before["id"], after["id"]), latest
        assert [item["id"] for item in latest["skipped_flagged"]] == [messy["id"]], latest
        since = request(base, f"/datasets/suppliers/changes?since={before['version']}")
        assert since["after_id"] == after["id"], since
        request(base, f"/datasets/suppliers/changes?since={messy['version']}", expected=409)
        until = request(base, f"/datasets/suppliers/changes?until={after['version']}")
        assert (until["before_id"], until["skipped_flagged"]) == (before["id"], []), until
        request(base, f"/datasets/suppliers/changes?until={messy['version']}", expected=409)
        export = request(base, "/datasets/suppliers/changes?format=csv&kind=changed", raw=True).decode()
        assert export == ("key,kind,field,before,after,before_record,after_record\r\n"
                          "S002,changed,city,Boston,Chicago,2,2\r\n"), export
        request(base, "/datasets/suppliers/changes?format=csv&limit=5", expected=422)
        notes = "/datasets/notes/imports?key=id"
        request(base, notes, b"id,note\nN1,ok\nN2,-5\n", expected=201)
        request(base, notes, 'id,note\nN1,=HYPERLINK("http://example.invalid")\nN2,Zo\u00eb\n'.encode(), expected=201)
        sheet = request(base, "/datasets/notes/changes?format=csv&spreadsheet=true", raw=True)
        assert sheet == ("\ufeffkey,kind,field,before,after,before_record,after_record\r\n"
                         "N1,changed,note,ok,\"'=HYPERLINK(\"\"http://example.invalid\"\")\",1,1\r\n"
                         "N2,changed,note,-5,Zo\u00eb,2,2\r\n").encode(), sheet
        request(base, "/datasets/notes/changes?spreadsheet=true", expected=422)
        request(base, f"/diff?before={before['id']}&after={messy['id']}&format=csv", expected=409)
        trail = request(base, "/datasets/suppliers/trace?key=S002")
        assert [row["version"] for row in trail] == [3, 3, 2, 1], trail
        history = request(base, "/datasets/suppliers/history?key=S002")
        assert [(event["version"], event["kind"]) for event in history["events"]] == [(2, "changed"), (1, "added")], \
            history
        assert [row["number"] for row in history["skipped_flagged"][0]["key_records"]] == [2, 3], history
        request(base, "/datasets/missing/history?key=S002", expected=404)
        datasets = json.loads(run("exec", "-T", "api", "python", "-m", "datatrail", "datasets"))
        assert {item["name"]: item["import_count"] for item in datasets} == {
            "contacts": 1, "invoices": 1, "notes": 2, "suppliers": 3}, datasets
        print("HTTP imports, retries, quality, type and value rule checks, diffs, plain and spreadsheet CSV exports, "
              "latest changes, provenance, key history and CLI reads passed.", flush=True)

        run("up", "--detach", "--force-recreate", "--wait", "--wait-timeout", "45", "api")
        base = endpoint()
        assert request(base, f"/imports/{before['id']}/source", raw=True) == before_bytes
        assert request(base, upload, before_bytes)["id"] == before["id"]
        created = json.loads(run("exec", "-T", "api", "python", "-m", "datatrail", "import",
                                 "cli-feed", "examples/suppliers-before.csv", "--key", "supplier_id"))
        assert request(base, f"/imports/{created['id']}")["dataset"] == "cli-feed"
        print("Container recreation, retained source bytes and shared CLI/API writes passed.", flush=True)
    finally:
        run("down", "--volumes", "--remove-orphans")
        subprocess.run(["docker", "image", "rm", image], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30, check=False)


if __name__ == "__main__":
    main()
