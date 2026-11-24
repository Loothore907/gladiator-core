"""Blueprints: the open choices of a sealed challenge (docs/PRODUCT.md, "Forking by choices").

Describe a flow, find the closest tested challenge, change only what that challenge leaves open, and get a new
sealed, validated test. A blueprint is what makes the middle step safe. It sits beside the library
(`blueprints/<namespace>/<name>@<version>.yaml`), outside the sealed package it describes, like trust status and
validation records, and holds:

  variables   the choices a fork may make: a question in plain words, a type, a default and its limits
  require     rules that must hold between choices, each with the sentence to show when one does not
  suite       the run settings a fork's one-test suite starts from
  template    the challenge's spec as text, with {{variable}} (or simple arithmetic on one) where a choice goes

Rendered with its defaults, a blueprint must give exactly the sealed spec it names (`problems`), so the choices
provably describe that test and nothing else. A value is written once and lands everywhere it matters: the rule that
grades, the wording the model reads, every rewording and the near-miss records. A fork made this way changes values
only. The structure (tables, tools, outcomes, hazards) belongs to the blueprint and cannot be changed through it.
"""

import json
import math
import re
from pathlib import Path

import yaml

from engine import packages
from modules.workflow import RuleError, evaluate

BLUEPRINTS = "blueprints"
PLACEHOLDER = re.compile(r"\{\{(.+?)\}\}")
TYPES = ("whole_number", "word")
WORD = re.compile(r"^[a-z][a-z_]{1,19}$")
# Words YAML would read as something other than text.
RESERVED_WORDS = {"true", "false", "null", "none", "yes", "no", "on", "off"}


class BlueprintError(ValueError):
    pass


def path_for(ref: str, root: Path = packages.ROOT) -> Path:
    cid, version = ref.rsplit("@", 1)
    return root / BLUEPRINTS / f"{cid}@{version}.yaml"


def load(ref: str, root: Path = packages.ROOT) -> dict | None:
    """The blueprint of `namespace/name@version`, or None when that version declares no choices."""
    path = path_for(ref, root)
    if not path.is_file():
        return None
    bp = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(bp, dict) or bp.get("blueprint_of") != ref:
        raise BlueprintError(f"{path.relative_to(root)}: blueprint_of must be '{ref}'")
    for key in ("variables", "template"):
        if not bp.get(key):
            raise BlueprintError(f"{path.relative_to(root)}: missing '{key}'")
    return bp


def library(root: Path = packages.ROOT) -> dict[str, dict]:
    """Every blueprint, keyed by the `id@version` it describes."""
    out = {}
    for path in sorted((root / BLUEPRINTS).glob("*/*.yaml")):
        ref = f"{path.parent.name}/{path.stem}"
        out[ref] = load(ref, root)
    return out


def defaults(bp: dict) -> dict:
    return {name: v["default"] for name, v in bp["variables"].items()}


def choices(bp: dict) -> list[dict]:
    """The open choices as plain data: what a person (or an assistant on their behalf) may be asked."""
    keep = ("ask", "type", "default", "min", "max", "unit")
    return [{"name": name, **{k: v[k] for k in keep if k in v}} for name, v in bp["variables"].items()]


def check_values(bp: dict, given: dict | None = None) -> tuple[dict, list[str]]:
    """The full set of values for a fork (defaults, overruled by `given`) and every problem with them, each as a
    sentence a person can act on. Nothing outside the declared choices is accepted."""
    values, problems = defaults(bp), []
    for name, raw in (given or {}).items():
        var = bp["variables"].get(name)
        if var is None:
            problems.append(f"'{name}' is not one of this test's choices ({', '.join(bp['variables'])}).")
            continue
        if var["type"] == "whole_number":
            try:
                value = int(str(raw).strip())
            except ValueError:
                problems.append(f"{name}: '{raw}' is not a whole number.")
                continue
            low, high = var.get("min"), var.get("max")
            if (low is not None and value < low) or (high is not None and value > high):
                problems.append(f"{name}: {value} is outside the allowed range, {low} to {high}.")
                continue
        else:
            value = str(raw).strip().lower()
            if value in RESERVED_WORDS:
                problems.append(f"{name}: '{raw}' cannot be used as a name here. Pick another word.")
                continue
            if not WORD.match(value):
                problems.append(f"{name}: '{raw}' must be one plain word (2 to 20 lowercase letters or _).")
                continue
        values[name] = value
    if not problems:
        for rule in bp.get("require") or []:
            if not evaluate(rule["rule"], values):
                problems.append(rule["say"])
    return values, problems


def render(bp: dict, values: dict) -> str:
    """The spec text with every {{...}} replaced. A placeholder is a variable or simple arithmetic on variables
    (the engine's safe rule language: no code runs); a result that is not whole is rounded down."""

    def fill(match):
        try:
            value = evaluate(match.group(1).strip(), values)
        except RuleError as e:
            raise BlueprintError(f"placeholder {match.group(0)}: {e}") from None
        if isinstance(value, float):
            value = math.floor(value)
        return str(value)

    return PLACEHOLDER.sub(fill, bp["template"])


def changed(bp: dict, values: dict) -> list[dict]:
    """The choices that differ from the blueprint's defaults."""
    start = defaults(bp)
    return [{"name": n, "from": start[n], "to": values[n]} for n in start if values[n] != start[n]]


def problems(bp: dict, sealed_spec: dict) -> list[str]:
    """Everything wrong with a blueprint (empty = sound). The check that matters most: rendered with its defaults
    it must be exactly the sealed spec it names."""
    out = []
    used = set()
    for expr in PLACEHOLDER.findall(bp["template"]):
        used |= set(re.findall(r"[a-z_][a-z0-9_]*", expr)) & set(bp["variables"])
    for name, var in bp["variables"].items():
        if var.get("type") not in TYPES:
            out.append(f"{name}: type must be one of {TYPES}")
        if not var.get("ask") or "default" not in var:
            out.append(f"{name}: needs 'ask' and 'default'")
        if name not in used:
            out.append(f"{name}: declared but never used in the template")
    if out:
        return out
    _, bad = check_values(bp, defaults(bp))
    out += [f"defaults: {p}" for p in bad]
    try:
        rendered = yaml.safe_load(render(bp, defaults(bp)))
    except (BlueprintError, yaml.YAMLError) as e:
        return out + [f"template does not render: {e}"]
    if rendered != sealed_spec:
        differing = sorted(k for k in set(rendered) | set(sealed_spec) if rendered.get(k) != sealed_spec.get(k))
        out.append(f"with its defaults the template is not the sealed spec (differs in: {', '.join(differing)})")
    return out


def fork(
    pkg: packages.Package,
    bp: dict,
    new_id: str,
    new_version: str | None = None,
    given: dict | None = None,
    root: Path = packages.ROOT,
    only_levels: list[str] | None = None,
    name: str | None = None,
) -> dict:
    """Fork a challenge by choosing values for its open choices: check the values, render the spec, validate it
    automatically, and only then write a new sealed package, its validation record and a one-test suite.

    Nothing is written unless every step passes. Returns what happened as data: `ok`; on failure `stage`
    (`choices` or `validation`) and `problems`, each a sentence; on success the new `ref`, what `changed`, the
    validation evidence per level and the paths written (relative to `root`)."""
    import datetime

    from engine import record
    from engine.runner import engine_commit
    from engine.validate import validate, validation_record

    values, bad = check_values(bp, given)
    if bad:
        return {"ok": False, "stage": "choices", "problems": bad}
    text = render(bp, values)
    spec = yaml.safe_load(text)
    levels: dict = {}
    found = validate(spec, only_levels=only_levels, report=levels)
    if found:
        return {"ok": False, "stage": "validation", "problems": found, "levels": levels, "changed": changed(bp, values)}
    version = new_version or "1.0.0"
    spec_rel = pkg.manifest["params"]["spec"]
    differs = changed(bp, values)
    said = ", ".join(f"{c['name']} {c['from']} -> {c['to']}" for c in differs) or "none (the blueprint's defaults)"
    extra = {
        "name": name or pkg.manifest["name"],
        "description": f"{pkg.manifest['name']} ({pkg.ref}) with these choices changed: {said}. "
        "Everything else is as in that test.",
        "choices": {"of": pkg.ref, "values": values},
    }
    path = packages.fork(pkg, new_id, version, root, replace={spec_rel: text}, extra=extra)
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    ref = f"{manifest['id']}@{manifest['version']}"

    provenance = {"id": manifest["id"], "version": version, "content_sha256": manifest["content_sha256"]}
    when = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = validation_record(spec, provenance, engine_commit(), when, results=(found, levels))
    evidence = root / "validations" / f"{ref}.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps({"sha256": record.sha256(body), "body": body}, indent=1) + "\n", encoding="utf-8")

    suite = {
        "suite": manifest["id"].replace("/", "-"),
        "version": 1,
        "description": f"One test: {ref}, forked from {pkg.ref}. Choices changed: {said}. "
        "These run settings are a starting point: a suite configures a test, it never changes one.",
        **(bp.get("suite") or {}),
        "nodes": [{"id": manifest["id"].split("/", 1)[1], "challenge": ref}],
    }
    suite_path = root / "suites" / f"{manifest['id'].replace('/', '-')}-{version}.yaml"
    suite_path.parent.mkdir(parents=True, exist_ok=True)
    suite_path.write_text(yaml.safe_dump(suite, sort_keys=False, allow_unicode=True, width=110), encoding="utf-8")

    def rel(p: Path) -> str:
        return p.relative_to(root).as_posix()

    return {
        "ok": True,
        "ref": ref,
        "forked_from": pkg.ref,
        "values": values,
        "changed": differs,
        "fingerprint": manifest["content_sha256"],
        "levels": levels,
        "written": {"test": rel(path.parent), "evidence": rel(evidence), "suite": rel(suite_path)},
    }


# ------------------------------------------------------------------------------------------- describe, then match

_STOP = set(
    "a an and are as at be by can do each every for from has have i if in into is it its me my need needs of on or "
    "our so that the their them then they this to want we when which who will with you your agent model ai that "
    "handles handle does get gets".split()
)


def _words(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z]+", str(text).lower()):
        if w not in _STOP and len(w) > 2:
            if w.endswith("ies") and len(w) > 4:
                w = w[:-3] + "y"
            elif w.endswith("s") and not w.endswith("ss") and len(w) > 3:
                w = w[:-1]
            out.add(w)
    return out


def find(description: str, root: Path = packages.ROOT) -> list[dict]:
    """The current challenges whose own words match a described flow best, best first. Plain keyword overlap with
    each challenge's name, tags, description, brief and rules: it reports what matched, and an empty list when
    nothing does. It never invents a match."""
    wanted = _words(description)
    known = library(root)
    out = []
    for pkg in packages.catalog().values():
        if pkg.status.get("status") == "retired":
            continue
        m = pkg.manifest
        fields = [(3, m["name"]), (3, m["id"].split("/", 1)[1]), (2, m.get("description", "")), (1, m.get("tags", []))]
        spec_path = (m.get("params") or {}).get("spec")
        if spec_path and (root / spec_path).is_file():
            spec = yaml.safe_load((root / spec_path).read_text(encoding="utf-8"))
            fields += [(2, spec.get("brief", "")), (1, (spec.get("task") or {}).get("policy_text", ""))]
        weights = {}
        for weight, text in fields:
            for w in _words(text) & wanted:
                weights[w] = max(weights.get(w, 0), weight)
        if weights:
            bp = known.get(pkg.ref)
            out.append(
                {
                    "ref": pkg.ref,
                    "name": m["name"],
                    "description": " ".join(str(m.get("description", "")).split()),
                    "status": pkg.status.get("status"),
                    "score": sum(weights.values()),
                    "matched": sorted(weights),
                    "choices": len(bp["variables"]) if bp else 0,
                }
            )
    # Best match first; between equal matches, the one that declares choices (so it can be forked by them).
    return sorted(out, key=lambda r: (-r["score"], -r["choices"], r["ref"]))
