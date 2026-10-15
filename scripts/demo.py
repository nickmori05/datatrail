import json
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory() as directory:
        database = str(Path(directory) / "demo.sqlite3")

        def cli(*arguments, expected=0):
            result = subprocess.run(
                [sys.executable, "-m", "datatrail", "--database", database, *arguments],
                cwd=ROOT, capture_output=True, text=True, timeout=30,
            )
            if result.returncode != expected:
                raise RuntimeError(result.stderr or result.stdout)
            return json.loads(result.stdout)

        before = cli("import", "suppliers", "examples/suppliers-before.csv", "--key", "supplier_id")
        after = cli("import", "suppliers", "examples/suppliers-after.csv", "--key", "supplier_id")
        dirty = cli("import", "suppliers", "examples/suppliers-messy.csv", "--key", "supplier_id", expected=1)
        comparison = cli("diff", str(before["id"]), str(after["id"]))
        flags = cli("records", str(dirty["id"]), "--flagged")
        issues = cli("show", str(dirty["id"]))["issue_counts"]
        trail = cli("trace", "suppliers", "S002")
        latest = cli("changes", "suppliers", "--kind", "changed")
        print(json.dumps({"comparison": comparison, "flagged_records": flags, "issue_counts": issues,
                          "supplier_trail": trail, "latest_changes": latest}, indent=2))


if __name__ == "__main__":
    main()
