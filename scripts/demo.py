"""Run the README workflow against a temporary database and print the results as JSON."""

import csv
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory() as directory:
        database = str(Path(directory) / "demo.sqlite3")

        def cli(*arguments, expected=0, parse=json.loads):
            result = subprocess.run(
                [sys.executable, "-m", "datatrail", "--database", database, *arguments],
                cwd=ROOT, capture_output=True, text=True, timeout=30,
            )
            if result.returncode != expected:
                raise RuntimeError(result.stderr or result.stdout)
            return parse(result.stdout)

        before = cli("import", "suppliers", "examples/suppliers-before.csv", "--key", "supplier_id")
        after = cli("import", "suppliers", "examples/suppliers-after.csv", "--key", "supplier_id")
        dirty = cli("import", "suppliers", "examples/suppliers-messy.csv", "--key", "supplier_id", expected=1)
        comparison = cli("diff", str(before["id"]), str(after["id"]))
        flags = cli("records", str(dirty["id"]), "--flagged")
        flagged_csv = cli("records", str(dirty["id"]), "--flagged", "--format", "csv",
                          parse=lambda text: list(csv.reader(io.StringIO(text))))
        issues = cli("show", str(dirty["id"]))["issue_counts"]
        trail = cli("trace", "suppliers", "S002")
        history = cli("history", "suppliers", "S002")
        latest = cli("changes", "suppliers", "--kind", "changed")
        summary = cli("summary", "suppliers")
        export = cli("changes", "suppliers", "--format", "csv", parse=lambda text: list(csv.reader(io.StringIO(text))))
        invoices = cli("import", "invoices", "examples/invoices.csv", "--key", "invoice_id",
                       "--type", "amount:decimal", "--type", "due_date:date", expected=1)
        invalid = cli("records", str(invoices["id"]), "--issue", "invalid_value")
        contacts = cli("import", "contacts", "examples/contacts.csv", "--key", "contact_id", "--required", "email",
                       "--allow", "region:east", "--allow", "region:west", "--pattern", "supplier_id:S[0-9]{3}",
                       expected=1)
        failures = cli("records", str(contacts["id"]), "--flagged")
        print(json.dumps({"comparison": comparison, "flagged_records": flags, "issue_counts": issues,
                          "supplier_trail": trail, "supplier_history": history, "latest_changes": latest,
                          "supplier_summary": summary,
                          "invalid_values": invalid, "rule_failures": failures, "changes_csv": export,
                          "flagged_csv": flagged_csv}, indent=2))


if __name__ == "__main__":
    main()
