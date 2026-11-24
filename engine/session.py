"""The guided conversation from a description of your flow to a test of your own (docs/PRODUCT.md, section 4a):
describe, match, settle the open choices, look at the rules, create.

The conversation is bound to the choices. Everything a person can settle is a choice the matched test declares
(engine/blueprint.py). The engine asks for each one, checks each answer, and does the validating and sealing.

A model may read the description (`ModelReader`), and only that. It says which of the tests the engine found is
the same kind of job, and which choices the description already settles. Both answers come back through a tool
whose schema is built from the declared choices; the engine checks them again; and the person confirms every value
before anything is created. Without a model the conversation is the same, with the keyword match and every choice
starting from the test's own value.

`ForkSession` holds the conversation as plain data in and out, so any front end can drive it: the terminal
(`gladiator chat`), a page, or an assistant.
"""

import json
import re
from pathlib import Path

import yaml

from engine import blueprint, packages
from engine.budget import Budget, BudgetExhausted, call_cost, next_input_tokens, price_for
from providers.base import FatalProviderError, ProviderError, Settings, ToolSpec

MAX_CANDIDATES = 5  # how many matched tests a reader is shown
MAX_NOT_COVERED = 5

PICK_RULES = """\
You help a person find a ready-made test for a workflow they want an AI agent to do. You get their description and \
a short list of tests. Call pick_test exactly once.
- Pick a test only if its job is the same kind of job as theirs: the same sort of items to decide and the same \
sort of decisions. Shared words are not enough.
- If none is, pick "none". Never stretch a match.
- `why` is one plain sentence the person will read."""

FILL_RULES = """\
A person described a workflow, and a test was chosen for it. The test has a few open choices. Call fill_choices \
exactly once.
- Give a value only for a choice their description states. Leave every other choice out. Never guess, and never \
fill in a value just because the test has one.
- `not_covered`: things they asked for that none of these choices can express, as short phrases in their own words \
(at most five). Use an empty list when there are none."""


class ReaderUnavailable(RuntimeError):
    """The model could not be asked (no price, no money left under the cap, or the provider failed). The
    conversation carries on without it."""


class ModelReader:
    """Reads a description with one model, in at most two calls, under a hard spending cap."""

    def __init__(self, contestant: str, max_usd: float = 0.05, provider=None, price: dict | None = None):
        import providers

        name, model = providers.parse(contestant)
        self.contestant = contestant
        self.provider = provider or providers.create(contestant)
        self.price = dict(price or price_for(name, model))  # no price, no use: the cap needs one
        self.price.setdefault("cached_input", self.price["input"])
        self.budget = Budget(max_usd)
        self.settings = Settings(max_output_tokens=1500, effort="low", timeout_s=60.0)

    @property
    def spent_usd(self) -> float:
        return self.budget.spent

    def _call(self, rules: str, message: dict, tool: ToolSpec) -> dict | None:
        """One capped call. Returns the tool's arguments, or None when the model did not call it properly."""
        text = json.dumps(message, sort_keys=True, ensure_ascii=False)
        try:
            chat = self.provider.open_chat(rules, text, self.settings, [tool])
            estimate = next_input_tokens(chat.approx_input_chars(), None, 0)
            held = self.budget.hold(
                self.budget.worst_case(self.price, 0, self.settings.max_output_tokens, input_tokens=estimate)
            )
        except BudgetExhausted as e:
            raise ReaderUnavailable(f"the spending cap for reading descriptions is used up ({e})") from None
        except (ProviderError, FatalProviderError) as e:
            raise ReaderUnavailable(str(e)) from None
        try:
            reply = chat.ask()
        except (ProviderError, FatalProviderError) as e:
            self.budget.release(held)
            raise ReaderUnavailable(str(e)) from None
        self.budget.settle(held, call_cost(self.price, reply.usage)[0])
        if reply.tool_name != tool.name or not isinstance(reply.tool_args, dict):
            return None
        return reply.tool_args

    def read(self, description: str, candidates: list[dict], root: Path = packages.ROOT) -> dict:
        """{"ref": one of the candidates' refs or None, "why", "values": {choice: raw value}, "not_covered"}.
        Nothing here is trusted: the session checks the ref and every value again."""
        refs = [c["ref"] for c in candidates]
        pick = ToolSpec(
            "pick_test",
            "Say which test, if any, is the same kind of job as the person's workflow.",
            {
                "type": "object",
                "properties": {"ref": {"type": "string", "enum": [*refs, "none"]}, "why": {"type": "string"}},
                "required": ["ref", "why"],
                "additionalProperties": False,
            },
            strict=True,
        )
        tests = [{"ref": c["ref"], "name": c["name"], "what_it_tests": c["description"]} for c in candidates]
        picked = self._call(PICK_RULES, {"description": description, "tests": tests}, pick) or {}
        ref = picked.get("ref") if picked.get("ref") in refs else None
        out = {"ref": ref, "why": str(picked.get("why") or "")[:300], "values": {}, "not_covered": []}
        bp = blueprint.load(ref, root) if ref else None
        if not bp:
            return out
        properties = {"not_covered": {"type": "array", "items": {"type": "string"}}}
        for c in blueprint.choices(bp):
            if c["type"] == "whole_number":
                properties[c["name"]] = {"type": "integer", "description": c["ask"]}
                for bound, key in (("min", "minimum"), ("max", "maximum")):
                    if bound in c:
                        properties[c["name"]][key] = c[bound]
            else:
                properties[c["name"]] = {"type": "string", "description": f"{c['ask']} One lowercase word."}
        fill = ToolSpec(
            "fill_choices",
            "Give the values the description states for this test's open choices, and what it cannot express.",
            {"type": "object", "properties": properties, "required": ["not_covered"]},
        )
        name = next(c["name"] for c in candidates if c["ref"] == ref)
        # The reader is not shown the test's own values: it reports what the description says, nothing else.
        asked = [{k: v for k, v in c.items() if k != "default"} for c in blueprint.choices(bp)]
        message = {"description": description, "test": name, "choices": asked}
        filled = self._call(FILL_RULES, message, fill) or {}
        out["values"] = {k: v for k, v in filled.items() if k in bp["variables"] and v is not None}
        notes = filled.get("not_covered")
        if isinstance(notes, list):
            out["not_covered"] = [str(n)[:160] for n in notes if str(n).strip()][:MAX_NOT_COVERED]
        return out


def slug(text: str) -> str:
    """A name as it appears in a test's id: lowercase words joined by hyphens."""
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


class ForkSession:
    """One conversation. Call the steps in order; each returns what to show or ask next, as plain data.

    describe(text)  the closest tests, the one proposed, and what the description already settles
    choose(ref)     the test to fork: its open choices as questions, each with the value it starts from
    answer(n, v)    one answer, checked: {"ok", "problem"}
    review()        the values, what changed, any rule between choices that is broken, and the test's rules as
                    a model will be told them with these values
    create(id)      validate, then write the sealed test, its evidence and its suite (engine.blueprint.fork)
    """

    def __init__(self, root: Path = packages.ROOT, reader: ModelReader | None = None):
        self.root, self.reader = root, reader
        self.description = ""
        self.reading: dict = {}
        self.pkg = self.bp = None
        self.values: dict = {}
        self.sources: dict = {}

    def describe(self, text: str) -> dict:
        self.description = " ".join(str(text).split())
        matches = blueprint.find(self.description, self.root)
        forkable = [m for m in matches if m["choices"]][:MAX_CANDIDATES]
        proposed = forkable[0]["ref"] if forkable else None
        why = f"matched on: {', '.join(forkable[0]['matched'])}" if forkable else ""
        self.reading, note = {}, None
        if self.reader and forkable:
            try:
                self.reading = self.reader.read(self.description, forkable, self.root)
                proposed, why = self.reading["ref"], self.reading["why"] or why
            except ReaderUnavailable as e:
                note = f"The description was not read by a model ({e}). Carrying on with the keyword match."
        return {
            "matches": matches,
            "forkable": forkable,
            "proposed": proposed,
            "why": why,
            "read_by": self.reader.contestant if self.reader and self.reading else None,
            "read_cost_usd": round(self.reader.spent_usd, 6) if self.reader else 0.0,
            "note": note,
        }

    def choose(self, ref: str) -> dict:
        self.pkg = packages.resolve(ref)
        self.bp = blueprint.load(ref, self.root)
        if not self.bp:
            raise blueprint.BlueprintError(f"{ref} declares no open choices, so it cannot be forked by choices")
        self.values, self.sources = blueprint.defaults(self.bp), {}
        unusable = []
        if self.reading.get("ref") == ref:
            for name, raw in self.reading["values"].items():
                value, problem = blueprint.check_one(self.bp, name, raw)
                if problem:
                    unusable.append(problem)
                elif value != self.values[name]:
                    self.values[name], self.sources[name] = value, "your description"
        return {
            "ref": ref,
            "name": self.pkg.manifest["name"],
            "description": " ".join(str(self.pkg.manifest.get("description", "")).split()),
            "questions": self.questions(),
            "not_covered": self.reading.get("not_covered", []) if self.reading.get("ref") == ref else [],
            "unusable": unusable,
        }

    def questions(self, only: list[str] | None = None) -> list[dict]:
        out = []
        for c in blueprint.choices(self.bp):
            if only is None or c["name"] in only:
                out.append({**c, "value": self.values[c["name"]], "source": self.sources.get(c["name"])})
        return out

    def answer(self, name: str, raw) -> dict:
        """An empty answer keeps the value shown."""
        if raw is None or not str(raw).strip():
            return {"ok": True, "problem": None, "value": self.values[name]}
        value, problem = blueprint.check_one(self.bp, name, raw)
        if problem:
            return {"ok": False, "problem": problem, "value": self.values.get(name)}
        self.values[name] = value
        self.sources[name] = "you" if value != self.bp["variables"][name]["default"] else None
        return {"ok": True, "problem": None, "value": value}

    def review(self) -> dict:
        broken = blueprint.broken_rules(self.bp, self.values)
        rules = brief = ""
        if not broken:
            spec = yaml.safe_load(blueprint.render(self.bp, self.values))
            brief, rules = spec.get("brief", ""), (spec.get("task") or {}).get("policy_text", "")
        return {
            "values": dict(self.values),
            "changed": blueprint.changed(self.bp, self.values),
            "broken": broken,
            "brief": " ".join(brief.split()),
            "rules": " ".join(rules.split()),
        }

    def create(self, new_id: str, name: str | None = None, only_levels: list[str] | None = None) -> dict:
        return blueprint.fork(self.pkg, self.bp, new_id, None, self.values, self.root, only_levels, name=name)
