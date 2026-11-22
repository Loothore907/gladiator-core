"""Load a suite file (YAML or JSON) and work out the node order from its links.

Suite shape:

    suite: six-case-mvp
    version: 1
    description: ...
    defaults: {max_output_tokens: 4096, effort: low}     # identical for both models
    success: {primary: passed, tiebreak: [task_correct, workflow_correct, cost_usd, wall_s]}
    nodes:
      - id: snapshot
        challenge: gladiator/snapshot-scan-recovery@1.0.0   # a locked challenge package (challenges/)
        settings: {max_output_tokens: 2048}                 # optional per-node config (still identical for both)

      - id: my-test                                         # or an inline, unpackaged node (always "unreviewed"):
        module: v1-simulator@1
        params: {case: snapshot_scan_recovery_v01}
        instructions: ["...", "..."]

    Challenge nodes are configurable, not editable: a suite can't change a challenge's instructions or
    params. To change content, fork the challenge.
    links:
      - {from: snapshot, to: interval}                      # "run interval after snapshot"
"""

import hashlib
import json
from pathlib import Path

import yaml

import modules
from engine import packages

DEFAULT_SUCCESS = {
    "primary": "outcome",
    "profile": "balanced",
    "tiebreak": ["passed", "task_correct", "workflow_correct", "cost_usd", "wall_s"],
}
METRICS = {"outcome", "efficiency", "passed", "task_correct", "workflow_correct", "cost_usd", "wall_s"}


class SuiteError(ValueError):
    pass


def load(path: str) -> dict:
    p = Path(path)
    raw = p.read_bytes()
    data = json.loads(raw) if p.suffix == ".json" else yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise SuiteError(f"{path}: suite must be a mapping")
    for key in ("suite", "nodes"):
        if key not in data:
            raise SuiteError(f"{path}: missing '{key}'")
    data.setdefault("defaults", {})
    data.setdefault("links", [])
    data["success"] = {**DEFAULT_SUCCESS, **(data.get("success") or {})}
    for metric in [data["success"]["primary"], *data["success"]["tiebreak"]]:
        if metric not in METRICS:
            raise SuiteError(f"{path}: unknown success metric '{metric}' (use {sorted(METRICS)})")
    from scorers.efficiency import PROFILES

    profile = data["success"].get("profile", "balanced")
    if profile not in PROFILES:
        raise SuiteError(f"{path}: unknown efficiency profile '{profile}' (use {sorted(PROFILES)})")

    ids = set()
    for n in data["nodes"]:
        if not isinstance(n, dict) or "id" not in n or ("module" not in n and "challenge" not in n):
            raise SuiteError(f"{path}: every node needs an 'id' and either a 'challenge' or a 'module'")
        if n["id"] in ids:
            raise SuiteError(f"{path}: duplicate node id '{n['id']}'")
        ids.add(n["id"])
        n.setdefault("settings", {})
        if "challenge" in n:
            _expand_challenge(path, n)
        else:
            n.setdefault("params", {})
            n.setdefault("instructions", [])
            n.setdefault("name", n["id"])
            if isinstance(n["instructions"], str):
                n["instructions"] = [n["instructions"]]
            try:
                n["module"] = modules.resolve(n["module"])
            except ValueError as e:
                raise SuiteError(f"{path}: node '{n['id']}': {e}") from None
            n["provenance"] = {"id": None, "inline": True, "status": "unreviewed", "test_type": n["module"]}
    for link in data["links"]:
        if link.get("from") not in ids or link.get("to") not in ids:
            raise SuiteError(f"{path}: link {link} points at an unknown node")

    data["order"] = order(data)
    data["sha256"] = hashlib.sha256(raw).hexdigest()
    data["path"] = str(path)
    return data


def _expand_challenge(path, n: dict):
    """Fill a challenge node from its locked package. The suite may configure, never edit."""
    edits = sorted(k for k in ("module", "params", "instructions") if k in n)
    if edits:
        raise SuiteError(
            f"{path}: node '{n['id']}' sets {edits} on a challenge. Challenges are configurable, not editable: "
            "fork the challenge to change its content."
        )
    try:
        pkg = packages.resolve(n["challenge"])
    except packages.PackageError as e:
        raise SuiteError(f"{path}: node '{n['id']}': {e}") from None
    m = pkg.manifest
    n["module"] = modules.resolve(m["test_type"])
    n["params"] = dict(m.get("params") or {})
    n["instructions"] = list(m["instructions"])
    n.setdefault("name", m["name"])
    n["provenance"] = pkg.provenance()


def order(suite: dict) -> list[str]:
    """Run order: respect links; otherwise keep the order nodes are written in."""
    ids = [n["id"] for n in suite["nodes"]]
    before = {i: set() for i in ids}
    for link in suite["links"]:
        before[link["to"]].add(link["from"])
    done, result = set(), []
    while len(result) < len(ids):
        ready = [i for i in ids if i not in done and before[i] <= done]
        if not ready:
            raise SuiteError("links form a loop")
        result.append(ready[0])
        done.add(ready[0])
    return result


def node(suite: dict, node_id: str) -> dict:
    return next(n for n in suite["nodes"] if n["id"] == node_id)
