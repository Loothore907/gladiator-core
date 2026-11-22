"""Write and verify run records.

Every run writes a folder runs/<run_id>/ with:
  record.json    everything contestants saw and did, grades, costs, settings,
                 plus a SHA-256 over the whole body ("hashed run record")
  operator.json  PRIVATE simulator records (hidden faults, answer state).
                 Its hash is stored in record.json so the two are tied together.
  report.md      the human-readable match table

`python gladiator.py verify runs/<run_id>` re-computes both hashes. The schema is described in
docs/RUN_RECORD.md; load() reads a record of any version and upgrades it to the current one in memory (the
file and its seal stay untouched).
"""

import hashlib
import json
from pathlib import Path


def canonical_bytes(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False, default=str
    ).encode("utf-8")


def sha256(value) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def write(folder: Path, body: dict, operator: dict, report_md: str):
    folder.mkdir(parents=True, exist_ok=False)
    body = dict(body, operator_sha256=sha256(operator))
    (folder / "operator.json").write_text(
        json.dumps(operator, indent=1, ensure_ascii=False, default=str), encoding="utf-8"
    )
    (folder / "record.json").write_text(
        json.dumps({"sha256": sha256(body), "body": body}, indent=1, ensure_ascii=False, default=str), encoding="utf-8"
    )
    (folder / "report.md").write_text(report_md, encoding="utf-8")


def verify(folder: Path) -> list[str]:
    problems = []
    rec = json.loads((folder / "record.json").read_text(encoding="utf-8"))
    if sha256(rec["body"]) != rec["sha256"]:
        problems.append("record.json body does not match its sha256 (edited after the run)")
    op = json.loads((folder / "operator.json").read_text(encoding="utf-8"))
    if sha256(op) != rec["body"].get("operator_sha256"):
        problems.append("operator.json does not match the hash stored in record.json")
    return problems


def load(folder: Path) -> dict:
    """A record's body, upgraded to the current schema in memory (old fields kept, new ones derived)."""
    from engine.runner import FAULT_ERRORS, RECORD_VERSION

    body = json.loads((Path(folder) / "record.json").read_text(encoding="utf-8"))["body"]
    return upgrade(body, RECORD_VERSION, FAULT_ERRORS)


def upgrade(body: dict, version: int, fault_errors: set) -> dict:
    """Version 1 -> 2: derive first_pass and model_errors per attempt from the stored turns."""
    body = json.loads(json.dumps(body))  # never change the caller's copy
    if body.get("record_version", 1) < 2:
        for side in body.get("contestants", {}).values():
            for r in side.get("results", []):
                errors = sum(
                    1
                    for t in r.get("turns", [])
                    if isinstance(t.get("observation"), dict)
                    and t["observation"].get("error")
                    and t["observation"]["error"] not in fault_errors
                )
                r.setdefault("model_errors", errors)
                first_report = (r.get("grade_details") or {}).get("first_report_correct")
                ok = bool(r.get("passed")) and (first_report if first_report is not None else errors == 0)
                r.setdefault("first_pass", bool(ok))
        body["record_version"] = 2
        body.setdefault("engine_commit", None)
    return body
