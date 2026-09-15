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
        request(base, f"/diff?before={before['id']}&after={messy['id']}", expected=409)
        trail = request(base, "/datasets/suppliers/trace?key=S002")
        assert [row["version"] for row in trail] == [3, 3, 2, 1], trail
        datasets = json.loads(run("exec", "-T", "api", "python", "-m", "datatrail", "datasets"))
        assert datasets[0]["import_count"] == 3, datasets
        print("HTTP imports, retries, quality checks, diffs, provenance and CLI reads passed.", flush=True)

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
