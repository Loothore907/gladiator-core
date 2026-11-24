"""Gladiator for AI assistants: build a test for a person's own flow, over MCP.

Start it with `gladiator mcp` (stdio). On the build page and in `gladiator chat` the engine leads: it asks about one
choice at a time and a person answers. Here the assistant leads. It reads what a library test needs (its job and
rules, every open choice with its allowed range, the rules between choices, and what the test cannot express),
collects the values from the person or from their own documents and systems in its own way, checks the design, and
has the engine create the sealed, validated test.

The assistant's own model does the reading, so this server never calls a model, needs no key and costs nothing.
Everything that shapes a test is still one of the matched test's declared choices, checked by the engine
(engine/blueprint.py) with the conversation code the terminal and the page use (engine/session.py).

`Designs` is the adapter as plain data in and out: the five calls, with no transport in them, so an HTTP API or a
hosted MCP endpoint can carry the same calls later. `build` puts them behind MCP.
"""

import contextlib
import re
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any

import yaml

from engine import blueprint, packages, session
from engine.validate import plain_problems, plain_summary

WAIT_S = 40.0  # how long one call waits for the checks before it answers "still checking" (clients time out near 60)
MAX_JOBS = 20
VERSION = re.compile(r"^\d+\.\d+\.\d+$")

INSTRUCTIONS = """\
Gladiator tests whether an AI model can do a tool-using job reliably. Two models play the same freshly generated
cases under the same rules, and a model passes a case only when every decision in it is right. This server builds
a test for ONE PERSON'S OWN FLOW.

The library holds ready-made tests. Some declare OPEN CHOICES (a refund window, a spending cap, what a customer
tier is called). A person's own test is a copy of one library test with their values for its open choices,
validated and sealed by the engine. Nothing else about a test can be changed here.

Walk the person through this, in order:
1. list_tests: find the library test that is the same kind of job as their flow: the same sort of items to decide
   and the same sort of decisions. Shared words are not enough. If none is, say so plainly and stop: never stretch
   a match. A test whose can_be_adapted is false has no open choices yet; say that, and do not offer it.
2. read_requirements: read the test's job, its rules and every open choice. Before collecting anything, tell the
   person what the test covers and what it has no choice for.
3. Collect a value for each choice from the person, or from their own documents and systems if they point you to
   them. Ask in their terms (each choice has an `ask`). Never guess a value, and never pass the library's value
   as if it were theirs: a choice they have not settled stays out of `values`, and you tell them it stays as the
   library has it.
4. check_design: fix what it reports together with the person. When it is ok, show them `the_rules` exactly as
   returned: that is what a model will be told. Get their yes before creating anything.
5. create_test, then get_result while the state is "checking". The engine plays the test with a perfect player
   and with players that cut corners, and creates it only if the perfect player gets through and the
   corner-cutters do not. Report `checked` as returned, where the files are, and `run_it`.

Rules that are not optional:
- Everything that shapes the test is a declared choice. If the person's flow needs something no choice covers (a
  different decision, another record, an approval step), say that this test cannot express it. Never claim the
  created test covers it, and never try to carry it in through a name or a value.
- A created test is new and unreviewed: it passed automatic validation, and no person has reviewed it. Say so.
- A test result is a claim about that task under those rules. It is never a claim about a model in general.
- When a call returns `error` or `problems`, relay them as they are: they are written for the person.
"""

FIXED = (
    "Everything else about this test is fixed: its records, the tools the agent is given, the decisions it takes, "
    "the difficulty levels and the obstacles built into its cases. A flow that needs any of those to be different "
    "is a different test, and it cannot be expressed through these choices."
)
UNREVIEWED = (
    "unreviewed: it passed the engine's automatic validation and is sealed, and no person has reviewed it. "
    "Reports name that status wherever the test is used."
)


def one_line(text) -> str:
    return " ".join(str(text or "").split())


class Designs:
    """The five calls an assistant makes, as plain data in and out. One instance serves one checkout: created
    tests are written into it (`challenges/`, `validations/`, `suites/`), exactly as `gladiator fork` writes them.

    A bad request is answered, not raised: `{"error": sentence}`, written for the person the assistant is
    helping. `levels` limits validation to some difficulty levels (tests use it to stay quick)."""

    def __init__(self, root: Path = packages.ROOT, levels: list[str] | None = None, wait_s: float = WAIT_S):
        self.root, self.levels, self.wait_s = Path(root), levels, wait_s
        # tests already read from disk: the library's own tests do not change under a running server
        self.known: dict = {}
        self.adaptable: dict | None = None
        self.jobs: dict[str, dict] = {}
        self.busy = threading.Lock()  # the checks use a whole processor: one test at a time

    # ---- reading the library
    def _adaptable(self) -> dict[str, dict]:
        if self.adaptable is None:
            self.adaptable = blueprint.library(self.root)
        return self.adaptable

    def _talk(self, ref: str) -> tuple[session.ForkSession | None, dict | None]:
        """A conversation opened on one test, or what is wrong with the reference."""
        ref = str(ref or "").strip()
        can = sorted(self._adaptable())
        if ref not in can:
            try:
                packages.resolve(ref)
                reason = "has no open choices yet, so it cannot be adapted"
            except packages.PackageError:
                reason = "is not a test in this library (a reference looks like namespace/name@version)"
            return None, {"error": f"'{ref}' {reason}. Tests that can be adapted: {', '.join(can) or 'none'}."}
        talk = session.ForkSession(self.root, known=self.known)
        talk.choose(ref)
        return talk, None

    def tests(self, description: str = "") -> dict:
        """Every current test in the library, and which can be adapted. With a description, the tests that share
        words with it come first and say which words: a hint for the reader, never a match by itself."""
        can = self._adaptable()
        wanted = one_line(description)
        hits = {m["ref"]: m for m in blueprint.find(wanted, self.root)} if wanted else {}
        out = []
        for pkg in packages.catalog().values():
            if pkg.status.get("status") == "retired":
                continue
            m = pkg.manifest
            entry = {
                "test": pkg.ref,
                "name": m["name"],
                "what_it_tests": one_line(m.get("description")),
                "trust": pkg.status.get("status"),
                "can_be_adapted": pkg.ref in can,
                "open_choices": len(can[pkg.ref]["variables"]) if pkg.ref in can else 0,
            }
            made = m.get("choices") or {}
            if made.get("of"):  # a test someone already built from a library test
                entry["built_from"], entry["with_values"] = made["of"], made.get("values")
            if wanted:
                entry["shares_words"] = hits.get(pkg.ref, {}).get("matched", [])
            out.append((-(hits.get(pkg.ref, {}).get("score") or 0), pkg.ref, entry))
        tests = [entry for _, _, entry in sorted(out, key=lambda t: t[:2])]
        note = (
            "Pick a test only if its job is the same kind of job as the person's flow. "
            "Only a test with can_be_adapted true can be made their own."
        )
        if wanted and not hits:
            note = "No test shares a word with that description. Read what each test is for before deciding. " + note
        return {"tests": tests, "can_be_adapted": sum(t["can_be_adapted"] for t in tests), "note": note}

    def requirements(self, ref: str) -> dict:
        """Everything needed to design a test from this one, in one answer."""
        talk, wrong = self._talk(ref)
        if wrong:
            return wrong
        pkg, bp = talk.pkg, talk.bp
        spec = yaml.safe_load(blueprint.render(bp, blueprint.defaults(bp)))
        used = blueprint.places(bp)
        choices = []
        for c in blueprint.choices(bp):
            choice = {k: c[k] for k in ("name", "ask", "type", "unit", "min", "max") if k in c}
            choice["library_value"] = c["default"]
            choice["used_in_places"] = used.get(c["name"], 0)
            choices.append(choice)
        return {
            "test": pkg.ref,
            "name": pkg.manifest["name"],
            "what_it_tests": one_line(pkg.manifest.get("description")),
            "trust": pkg.status.get("status"),
            "the_job": one_line(spec.get("brief")),
            "the_rules": one_line((spec.get("task") or {}).get("policy_text")),
            "tools_the_agent_gets": [
                {"name": t["name"], "what_it_does": one_line(t.get("description"))} for t in spec.get("tools") or []
            ],
            "difficulty_levels": list(spec.get("difficulty_levels") or []),
            "choices": choices,
            "rules_between_choices": [rule["say"] for rule in bp.get("require") or []],
            "values_schema": blueprint.values_schema(bp),
            "fixed": FIXED,
            "next": (
                "the_job and the_rules are shown with the library's values. Collect the person's value for each "
                "choice (leave out any they have not settled), then call check_design with this test and `values`."
            ),
        }

    # ---- a design: values for one test's choices
    def check(self, ref: str, values: dict | None) -> dict:
        """Check a design without creating anything: each value, the rules between choices, and the test's rules
        as a model will be told them with these values."""
        talk, wrong = self._talk(ref)
        if wrong:
            return wrong
        if not isinstance(values, dict):
            return {"error": "Send `values` as an object: {choice name: value}. An empty object keeps every value."}
        problems = talk.fill(values)
        look = talk.review()
        ok = not problems and not look["broken"]
        settled = {str(name) for name in values} - set(problems)
        out = {
            "ok": ok,
            "test": talk.pkg.ref,
            "problems": problems,
            "do_not_fit_together": look["broken"],
            "values": look["values"],
            "changed": look["changed"],
            "left_as_the_library_has_them": [c["name"] for c in blueprint.choices(talk.bp) if c["name"] not in settled],
        }
        if ok:
            out["the_job"], out["the_rules"] = look["brief"], look["rules"]
            out["next"] = (
                "Show the person the_rules and which choices stay as the library has them. "
                "With their yes, call create_test with this test and these values."
            )
        else:
            out["next"] = "Nothing can be created from these values. Settle what is reported with the person."
        return out

    def create(self, ref: str, values: dict | None, owner: str, name: str, version: str = "1.0.0") -> dict:
        """Check the design, then validate and write the sealed test. Waits for the checks up to `wait_s`."""
        checked = self.check(ref, values)
        if "error" in checked:
            return checked
        if not checked["ok"]:
            return {"state": "refused", "stage": "choices", "nothing_written": True, **checked}
        folder, label = session.slug(owner)[:40], session.slug(name)[:40]
        version = str(version or "1.0.0").strip()
        problem = None
        if not folder or not label:
            problem = "Give the test a name, and the person's name or team: each needs a letter or a digit."
        elif folder in session.RESERVED_OWNERS:
            problem = f"\"{folder}\" is the library's own name. File the test under the person's own name or team."
        elif not VERSION.match(version):
            problem = f"'{version}' is not a version. Versions look like 1.0.0."
        elif (self.root / "challenges" / folder / label / version).exists():
            have = sorted(p.name for p in (self.root / "challenges" / folder / label).iterdir() if p.is_dir())
            problem = (
                f"{folder}/{label}@{version} already exists and a sealed test is never changed "
                f"(versions here: {', '.join(have)}). Use a higher version, or another name."
            )
        if problem:
            return {"state": "refused", "stage": "name", "nothing_written": True, "problems": [problem]}
        if not self.busy.acquire(blocking=False):
            running = next((k for k, j in self.jobs.items() if j["state"] == "checking"), None)
            return {
                "state": "refused",
                "stage": "busy",
                "nothing_written": True,
                "problems": [f"Another test is being checked (job {running}). Ask for its result first."],
            }
        talk, _ = self._talk(ref)
        talk.fill(values)
        job_id = secrets.token_urlsafe(9)
        job = {"state": "checking", "level": None, "number": 0, "of": 0, "started": time.time()}
        job["done"] = threading.Event()
        while len(self.jobs) >= MAX_JOBS:
            del self.jobs[min(self.jobs, key=lambda k: self.jobs[k]["started"])]
        self.jobs[job_id] = job

        def work():
            try:
                # the engine may print as it works; on stdio, standard output belongs to the protocol
                with contextlib.redirect_stdout(sys.stderr):
                    result = talk.create(
                        f"{folder}/{label}",
                        one_line(name)[:60] or None,
                        self.levels,
                        version,
                        lambda level, number, of: job.update(level=level, number=number, of=of),
                    )
                if result["ok"]:
                    job.update(
                        state="created",
                        test=result["ref"],
                        built_from=result["forked_from"],
                        values=result["values"],
                        changed=result["changed"],
                        fingerprint=result["fingerprint"],
                        checked=plain_summary(result["levels"]),
                        files=result["written"],
                        trust=UNREVIEWED,
                        run_it=(
                            f"uv run gladiator match --suite {result['written']['suite']} "
                            "--a <provider:model> --b <provider:model> --max-usd 1"
                        ),
                    )
                else:
                    found = plain_problems(result.get("levels") or {}) or result["problems"]
                    job.update(state="refused", stage=result["stage"], nothing_written=True, problems=found)
            except Exception as e:  # noqa: BLE001  the assistant must hear what happened
                job.update(state="failed", problems=[f"{type(e).__name__}: {e}"])
            finally:
                job["seconds"] = round(time.time() - job["started"], 1)
                self.busy.release()
                job["done"].set()

        threading.Thread(target=work, daemon=True).start()
        return self.result(job_id)

    def result(self, job_id: str) -> dict:
        """What became of a test being created. Waits for the checks up to `wait_s`."""
        job = self.jobs.get(str(job_id or ""))
        if not job:
            return {"error": "No test is being created under that job here. Call create_test again."}
        job["done"].wait(self.wait_s)
        out = {k: v for k, v in job.items() if k not in ("done", "started")}
        out["job"] = job_id
        if job["state"] == "checking":
            out["seconds"] = round(time.time() - job["started"], 1)
            out["next"] = "The checks are still running (they take a minute or two). Call get_result with this job."
        return out


def build(designs: Designs | None = None):
    """The MCP server over one `Designs`. The MCP library is imported here, so the engine's other commands do not
    load it."""
    import asyncio

    from mcp.server.mcpserver import MCPServer

    designs = designs or Designs()
    server = MCPServer(
        "gladiator",
        title="Gladiator: build a test for your own flow",
        description="Turn a person's own workflow into a sealed, validated test, from a library test's open choices.",
        instructions=INSTRUCTIONS,
        website_url="https://github.com/Loothore907/gladiator-core",
    )

    @server.tool(title="List the library's tests, and which can be adapted")
    def list_tests(description: str = "") -> dict[str, Any]:
        """Every current test: what it tests, its trust status, and whether it declares open choices
        (`can_be_adapted`). Pass the person's `description` to put the tests that share words with it first
        (`shares_words`); that is a hint, and the judgement of "same kind of job" is yours. A test with
        `built_from` is one somebody already made from a library test, with the values it was given."""
        return designs.tests(description)

    @server.tool(title="Read what a test needs: its job, its rules and every open choice")
    def read_requirements(test: str) -> dict[str, Any]:
        """`test` is a reference such as gladiator/refund-queue@2.3.0 with can_be_adapted true. Returns the job
        and the rules a model is told (with the library's values), the tools the agent gets, every open choice
        (`ask`, type, unit, allowed range, the library's value), the rules between choices, a JSON Schema for
        `values`, and what is fixed and cannot be expressed."""
        return designs.requirements(test)

    @server.tool(title="Check a design: values for a test's open choices")
    def check_design(test: str, values: dict[str, Any]) -> dict[str, Any]:
        """Checks without creating anything. `values` maps choice names to the person's values; leave a choice
        out to keep the library's value. Returns `ok`, what is wrong with each value (`problems`), which values
        do not fit together, which choices stay as the library has them, and, when ok, `the_rules` exactly as a
        model will be told them."""
        return designs.check(test, values)

    @server.tool(title="Create the test: validate it, seal it and write it")
    async def create_test(
        test: str, values: dict[str, Any], owner: str, name: str, version: str = "1.0.0"
    ) -> dict[str, Any]:
        """Only after the person has seen the rules and said yes. `owner` is the person's name or team and `name`
        what they call the test: it is filed as owner/name@version. Runs the engine's automatic validation, then
        writes the sealed test, its validation record and a one-test suite into this checkout. Nothing is written
        unless validation passes. The checks take a minute or two: when the answer's state is "checking", call
        get_result with its `job`."""
        return await asyncio.to_thread(designs.create, test, values, owner, name, version)

    @server.tool(title="The result of a test being created")
    async def get_result(job: str) -> dict[str, Any]:
        """state is "checking" (ask again), "created" (the test's reference, what the checks found, the files
        written and the command that runs a match on it), "refused" (the checks found a problem, or the design
        was not allowed; nothing was written) or "failed"."""
        return await asyncio.to_thread(designs.result, job)

    @server.prompt(title="Build a test for my flow")
    def build_a_test(flow: str = "") -> str:
        """Start the loop: describe a flow, find the closest library test, settle its choices, create the test."""
        about = f"My flow: {flow}\n\n" if flow else ""
        return (
            f"{about}Help me build a Gladiator test for this flow. Find the library test that is the same kind of "
            "job, tell me what it covers and what it cannot express, ask me for each open choice, show me the "
            "rules a model will be told with my values, and create the test once I say yes."
        )

    return server


def serve() -> None:
    """Run the server on stdio until the client disconnects."""
    build().run("stdio")
