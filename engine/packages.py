"""Challenge packages: locked, versioned tests (see docs/PRODUCT.md, section 4).

A package is a folder under challenges/<namespace>/<name>/ with a challenge.yaml manifest. The manifest
lists every file the challenge depends on; its `content_sha256` is a fingerprint over the manifest itself
plus those files. If anything changes, the fingerprint no longer matches and the engine refuses to run the
package, so "official" content can never be edited silently. Changing a challenge means a new version or a
fork (with `forked_from`).

Suites reference packages as `namespace/name@version` and may configure how they run (settings, repeats),
never what they contain.

Trust is NOT part of the sealed content. A challenge's status (unreviewed, community, candidate, official,
retired) is a judgement that changes over time as evidence comes in (validation, calibration runs,
review, retirement). It lives in challenges/catalog.yaml, keyed by `id@version`, together with that
evidence, so promoting or retiring a challenge never forces a new content version.
"""

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CHALLENGES_DIR = ROOT / "challenges"
MANIFEST = "challenge.yaml"

REQUIRED = (
    "id",
    "version",
    "name",
    "test_type",
    "tags",
    "instances",
    "license",
    "authors",
    "instructions",
    "files",
    "content_sha256",
)
CATALOG = "catalog.yaml"
# Ordered from least to most trusted, plus retired (outgrown or found flawed; still runnable, clearly flagged).
STATUSES = ("unreviewed", "community", "candidate", "official", "retired")
INSTANCE_KINDS = ("fixed", "generator")


class PackageError(ValueError):
    pass


@dataclass
class Package:
    manifest: dict
    path: Path
    fingerprint: str
    problems: list = field(default_factory=list)
    status: dict = field(default_factory=lambda: {"status": "unreviewed"})

    @property
    def ref(self) -> str:
        return f"{self.manifest['id']}@{self.manifest['version']}"

    @property
    def intact(self) -> bool:
        return not self.problems

    def provenance(self) -> dict:
        """What every run record and report states about this challenge."""
        m = self.manifest
        return {
            "id": m["id"],
            "version": m["version"],
            "content_sha256": self.fingerprint,
            "status": self.status["status"],
            "tags": list(m["tags"]),
            "test_type": m["test_type"],
            "instances": m["instances"],
            "emulates": m.get("emulates"),
            "forked_from": m.get("forked_from"),
            "license": m["license"],
            "modified": not self.intact,
        }


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprint(manifest: dict, root: Path = ROOT) -> str:
    """SHA-256 over the manifest (minus its own fingerprint field) plus every listed file's bytes."""
    body = {k: v for k, v in manifest.items() if k != "content_sha256"}
    files = {}
    for rel in sorted(manifest.get("files") or []):
        p = (root / rel).resolve()
        if not p.is_relative_to(root.resolve()) or not p.is_file():
            raise PackageError(f"{manifest.get('id')}: listed file missing or outside the repo: {rel}")
        files[rel] = _sha_file(p)
    canonical = json.dumps({"manifest": body, "files": files}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def read(path: Path) -> Package:
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise PackageError(f"{path}: manifest must be a mapping")
    missing = [k for k in REQUIRED if k not in manifest]
    if missing:
        raise PackageError(f"{path}: missing fields {missing}")
    if "trust" in manifest:
        raise PackageError(f"{path}: trust/status does not belong in the sealed manifest; set it in {CATALOG}")
    if manifest["instances"] not in INSTANCE_KINDS:
        raise PackageError(f"{path}: instances must be one of {INSTANCE_KINDS}")
    if isinstance(manifest["instructions"], str):
        manifest["instructions"] = [manifest["instructions"]]
    fp = fingerprint(manifest)
    problems = []
    if fp != manifest["content_sha256"]:
        problems.append("content changed since this version was fingerprinted (edit = new version or fork)")
    return Package(manifest=manifest, path=path.parent, fingerprint=fp, problems=problems)


def statuses() -> dict:
    """challenges/catalog.yaml: `id@version` -> {status, since, notes, evidence}."""
    path = CHALLENGES_DIR / CATALOG
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    entries = (data or {}).get("challenges") or {}
    for ref, entry in entries.items():
        if (entry or {}).get("status") not in STATUSES:
            raise PackageError(f"{CATALOG}: {ref} has status {entry.get('status')!r}; use one of {STATUSES}")
    return entries


def _version_key(version: str):
    return tuple(int(p) if p.isdigit() else p for p in str(version).split("."))


def library() -> dict[str, Package]:
    """Every package version under challenges/, keyed by `id@version`, with its status from catalog.yaml.

    Several versions of one challenge can live side by side, so retired versions stay runnable for history:
    `challenges/<namespace>/<name>/challenge.yaml` and/or `challenges/<namespace>/<name>/<version>/challenge.yaml`.
    """
    out = {}
    status = statuses()
    paths = sorted(CHALLENGES_DIR.glob(f"*/*/{MANIFEST}")) + sorted(CHALLENGES_DIR.glob(f"*/*/*/{MANIFEST}"))
    for path in paths:
        pkg = read(path)
        if pkg.ref in out:
            raise PackageError(f"duplicate challenge version {pkg.ref}")
        pkg.status = dict(status.get(pkg.ref) or {"status": "unreviewed"})
        out[pkg.ref] = pkg
    return out


def catalog() -> dict[str, Package]:
    """The newest version of every challenge, keyed by id."""
    out = {}
    for pkg in library().values():
        cid = pkg.manifest["id"]
        if cid not in out or _version_key(pkg.manifest["version"]) > _version_key(out[cid].manifest["version"]):
            out[cid] = pkg
    return out


def resolve(ref: str) -> Package:
    """Load `namespace/name@version` and refuse anything modified or mismatched."""
    if "@" not in ref:
        raise PackageError(f"'{ref}': pin a version, e.g. {ref}@1.0.0 (results must say exactly what ran)")
    cid, version = ref.rsplit("@", 1)
    versions = {p.manifest["version"]: p for p in library().values() if p.manifest["id"] == cid}
    if not versions:
        raise PackageError(f"unknown challenge '{cid}'. Known: {sorted(catalog())}")
    if version not in versions:
        have = ", ".join(sorted(versions, key=_version_key))
        raise PackageError(f"{cid}: version {version} requested, but the library has {have}")
    pkg = versions[version]
    if not pkg.intact:
        raise PackageError(f"{pkg.ref}: " + "; ".join(pkg.problems))
    return pkg


def fork(
    pkg: Package,
    new_id: str | None = None,
    new_version: str | None = None,
    root: Path = ROOT,
    replace: dict | None = None,
    extra: dict | None = None,
) -> Path:
    """Copy a package to a new id and/or version, as the starting point for an edit (`gladiator fork`).

    Files inside the source package's folder are copied and every path to them (in `files` and `params`) is
    rewritten to the new folder; files outside it (e.g. pinned v1 simulators) stay shared, unchanged. A new id
    is a fork: `forked_from` names the source. A new version of the same id is a new version: `forked_from`
    is kept as it was. The copy is sealed with a fresh fingerprint and has no catalog entry, so it starts as
    `unreviewed`. Returns the new manifest path.

    A fork by choices (engine/blueprint.py) passes `replace` ({source file path: new text}, written instead of
    copying that file) and `extra` (manifest fields to add, e.g. the choices made)."""
    m = pkg.manifest
    cid = new_id or m["id"]
    version = new_version or m["version"]
    if (cid, version) == (m["id"], m["version"]):
        raise PackageError("a fork needs a new id or a new version")
    if cid.count("/") != 1 or not all(cid.split("/")):
        raise PackageError(f"'{cid}': ids look like namespace/name")
    target = root / "challenges" / cid / version
    if target.exists():
        raise PackageError(f"{target.relative_to(root)} already exists")
    src = pkg.path.resolve().relative_to(root.resolve()).as_posix()
    dst = target.relative_to(root).as_posix()

    def moved(value):
        if isinstance(value, str) and value.startswith(src + "/"):
            return dst + value[len(src) :]
        if isinstance(value, dict):
            return {k: moved(v) for k, v in value.items()}
        if isinstance(value, list):
            return [moved(v) for v in value]
        return value

    target.mkdir(parents=True)
    for rel in m.get("files") or []:
        if rel.startswith(src + "/"):
            out = root / moved(rel)
            out.parent.mkdir(parents=True, exist_ok=True)
            if rel in (replace or {}):
                out.write_text(replace[rel], encoding="utf-8", newline="\n")
            else:
                shutil.copyfile(root / rel, out)
    new = dict(m, id=cid, version=version, files=moved(m.get("files") or []), **(extra or {}))
    if "params" in m:
        new["params"] = moved(m["params"])
    if cid != m["id"]:
        new["forked_from"] = pkg.ref
    new["content_sha256"] = fingerprint(new, root)
    header = "# Challenge package (sealed). Do not edit after publishing: any change breaks content_sha256.\n"
    if replace:
        header += (
            f"# Created by `gladiator fork` from {pkg.ref} by choosing values for its open choices (`choices` below).\n"
            "# To change a choice, fork again with other values: nothing here is edited by hand.\n"
        )
    else:
        header += (
            f"# Created by `gladiator fork` from {pkg.ref}. While drafting, edit freely, then re-seal with\n"
            "# `gladiator fingerprint --write` and check with `gladiator validate`.\n"
        )
    path = target / MANIFEST
    path.write_text(header + yaml.safe_dump(new, sort_keys=False, allow_unicode=True, width=110), encoding="utf-8")
    return path
