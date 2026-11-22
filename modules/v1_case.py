"""Wraps the ten stateful case simulators carried over from v1.

The simulator code in simulators/ is byte-for-byte what ran the Nov 14 match.
Before loading, every file is checked against the SHA-256 fingerprints in
simulators/PINS.json, so an accidental edit is caught instead of silently
changing the test.

Usage in a suite node:
    module: v1_case
    params: {case: snapshot_scan_recovery_v01}
"""

import builtins
import hashlib
import json
import types
from pathlib import Path

from modules.base import Grade, Module

SIM_DIR = Path(__file__).resolve().parent.parent / "simulators"
PINS = json.loads((SIM_DIR / "PINS.json").read_text(encoding="utf-8"))

# How each case's own grading fields split into "task" vs "workflow".
# `passed` is always the case's original combined verdict (unchanged from v1).
# Cases not listed grade only the final answer, so task_correct = passed and
# workflow is not graded separately.
COMPONENTS = {
    "snapshot_scan_recovery_v01": {
        "task": ["report_correct", "source_state_preserved"],
        "workflow": ["knowledge_workflow_correct", "fresh_complete_scan", "interface_correct"],
    },
    "ticket_revocation_recovery_v01": {
        "task": ["remaining_authorized_targets_closed", "expected_state_preserved"],
        "workflow": [
            "knowledge_workflow_correct",
            "discovery_complete",
            "fresh_revocation_observed",
            "interface_correct",
            "policy_correct",
        ],
    },
    "batch_checkpoint_recovery_v01": {
        "task": ["task_correct"],
        "workflow": ["knowledge_workflow_correct", "recovery_covered", "interface_correct", "policy_correct"],
    },
}


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


_kernels = {}


def kernel(case_id: str):
    """Load (once) the simulator for a case after verifying its fingerprints.

    This mirrors v1's loader: files are executed from the verified bytes, and
    native cases get their own copy of json_contract so cases can't interfere.
    """
    if case_id not in PINS:
        raise ValueError(f"Unknown v1 case '{case_id}'. Known: {sorted(PINS)}")
    if case_id in _kernels:
        return _kernels[case_id]
    entry = PINS[case_id]
    raw = {}
    for rel, expected in entry["sha256"].items():
        data = (SIM_DIR / rel).read_bytes()
        if _sha(data) != expected:
            raise RuntimeError(f"Simulator file changed since v1: simulators/{rel}")
        raw[rel] = data

    loaded = {}

    def bound_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "json_contract" and level == 0 and name in loaded:
            return loaded[name]
        return builtins.__import__(name, globals, locals, fromlist, level)

    files = [f"{case_id}/json_contract.py", entry["entry"]] if entry["kind"] == "native" else [entry["entry"]]
    module = None
    for rel in files:
        module = types.ModuleType(f"_sim_{case_id}_{Path(rel).stem}")
        module.__file__ = str(SIM_DIR / rel)
        module.__dict__["__builtins__"] = {**vars(builtins), "__import__": bound_import}
        exec(compile(raw[rel], module.__file__, "exec"), module.__dict__)
        loaded[Path(rel).stem] = module
    _kernels[case_id] = module
    return module


class V1Case(Module):
    def __init__(self, case: str, session_id: str = "gladiator"):
        self.case_id = case
        self.k = kernel(case)
        self.max_actions = PINS[case]["max_actions"]
        self.manifest_sha = _sha(_canonical(self.k.manifest()))
        self.session = self.k.Session(self.manifest_sha, session_id)

    def fingerprint(self) -> str:
        return self.manifest_sha

    def start(self) -> dict:
        return self.session.public_start()

    def step(self, action_json: str) -> dict:
        return self.session.step(action_json)

    @property
    def finished(self) -> bool:
        return self.session.terminal is not None

    @property
    def last_action_valid(self) -> bool:
        events = self.session.events
        return not events or events[-1].get("interface_valid", True)

    def grade(self) -> Grade:
        raw = dict(self.session.grade())
        passed = bool(raw.get("passed"))
        spec = COMPONENTS.get(self.case_id)
        if spec:
            task = all(bool(raw.get(f)) for f in spec["task"])
            workflow = all(bool(raw.get(f)) for f in spec["workflow"])
        else:
            task, workflow = passed, None
        return Grade(passed=passed, task_correct=task, workflow_correct=workflow, details=raw)

    def operator_record(self) -> dict:
        return self.session.record()
