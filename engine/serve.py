"""A race runner: start one match over HTTP and watch it happen.

`gladiator serve` answers five requests:

  GET  /options   what a race can be made of: the tasks, the models whose provider can still pay for a race,
                  each provider's budget and what is left of it, difficulty levels and scoring profiles
  GET  /health    whether a race is running, and how much of each spending limit is left
  GET  /races     the live races this server has run, newest last: for each its sealed record's id and seal,
                  the task, the two models, who won, who passed and what each cost. Public facts only
  POST /signin    {"invite"} (or {"token"} to renew): a session token and the races it has left. An invite is
                  typed or followed once; after that the session is all a browser needs
  POST /race      {"task", "a", "b", "difficulty", "profile", "token"}: plays one fresh scenario of that task between
                  the two models and streams what happens (text/event-stream: the runner's events, one per message),
                  ending with a `record` message that carries the sealed record and its private half.

It also serves the page where a person builds a test of their own (GET /build), and the conversation behind it
(docs/PRODUCT.md, section 4a; engine/session.py). Each request carries everything it needs, so the server keeps no
conversation:

  GET  /fork/tests      the tests that declare open choices
  POST /fork/status     {"token"}: what this visitor may do here (have a model read, create)
  POST /fork/describe   {"description", "token"}: the closest tests, the one proposed, and its questions
  POST /fork/choose     {"ref"}: the questions of a test the visitor picked instead
  POST /fork/review     {"ref", "values"}: every value checked, and the rules a model will be told with them
  POST /fork/create     {"ref", "values", "name", "owner", "token"}: starts the checks; answers with a job id
  GET  /fork/job?id=    how far the checks are, then what they found
  GET  /fork/download?id=   the created test as a zip (sealed package, validation record, suite)

A description is the only free text, and it never reaches the engine: it is matched by its words and, for a
signed-in visitor on a server with a reader, read by one model whose answers come back inside a schema built from
the declared choices. A created test is the visitor's to take away: it is built in a temporary folder, handed over
as a download and not kept.

A race is a suite built from safe choices, never new content: the task comes from a base suite's locked
challenges, and every value in the request must be one this server offers (PRODUCT.md: "configurable, not
editable"). Free text never reaches the engine.

Money is guarded three times: every race has a hard cap (`--max-usd` semantics of a normal match), a race may only
start if that cap still fits in what is left of the server's limits (a total, a budget per provider, or both),
and only one race runs at a time. With no invites set the server only listens on this machine.
"""

import base64
import hashlib
import hmac
import json
import math
import queue
import re
import secrets
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

import providers
from engine import blueprint, packages, record, runner, session
from engine import suite as suites
from engine.budget import price_for
from engine.validate import plain_problems, plain_summary
from scorers import components
from scorers.efficiency import PROFILES

SESSION_DAYS = 30  # how long a sign-in lasts
PAGES = Path(__file__).resolve().parent / "pages"  # the build page's three static files
JOB_MINUTES = 20  # how long a created test waits to be downloaded
MAX_JOBS = 20
MAX_DESCRIPTION = 1200


def who_of(code: str) -> str:
    """The id a sign-in is recorded under: never the code itself (the ledger is a file on a disk)."""
    return hashlib.sha256(("gladiator-invite\n" + code).encode()).hexdigest()[:16]


def summary(sealed: dict) -> dict:
    """The public facts of one live race, from its sealed record: what a viewer may know, nothing private."""
    body = sealed["body"]
    sides = {side: body["contestants"][side] for side in ("A", "B")}
    first = {side: (c["results"] or [{}])[0] for side, c in sides.items()}
    winner = (body.get("verdict") or {}).get("winner")
    return {
        "run_id": body["run_id"],
        "seal": sealed["sha256"],
        "task": (body["suite"].get("order") or [None])[0],
        "a": sides["A"]["id"],
        "b": sides["B"]["id"],
        "winner": sides[winner]["id"] if winner in sides else None,
        "a_passed": bool(first["A"].get("passed")),
        "b_passed": bool(first["B"].get("passed")),
        "a_cost_usd": float(first["A"].get("cost_usd") or 0),
        "b_cost_usd": float(first["B"].get("cost_usd") or 0),
        # what the grader found for each side (counts, and which items it decided wrongly), how each attempt
        # ended, and how long it took: enough for a page to write the match up without the full record
        "a_details": first["A"].get("grade_details") or {},
        "b_details": first["B"].get("grade_details") or {},
        "a_termination": first["A"].get("termination"),
        "b_termination": first["B"].get("termination"),
        "a_seconds": float(first["A"].get("wall_s") or 0),
        "b_seconds": float(first["B"].get("wall_s") or 0),
        "difficulty": (body.get("settings") or {}).get("defaults", {}).get("difficulty"),
    }


def batch_summary(sealed: dict) -> dict:
    """The public facts of one live batch (one test played many times), from its sealed record: how often each
    side passed, which faults showed and how often, who won each scenario, and what it cost."""
    from scorers.confidence import sign_test
    from scorers.outcome import pair_outcome

    body = sealed["body"]
    sides = {side: body["contestants"][side] for side in ("A", "B")}
    profile = (body["suite"].get("success") or {}).get("profile", "balanced")
    out = {
        "run_id": body["run_id"],
        "seal": sealed["sha256"],
        "task": (body["suite"].get("order") or [None])[0],
        "a": sides["A"]["id"],
        "b": sides["B"]["id"],
        "same_model": sides["A"]["id"] == sides["B"]["id"],
        "runs": int((body.get("settings") or {}).get("repeats") or len(sides["A"]["results"])),
        "difficulty": (body.get("settings") or {}).get("defaults", {}).get("difficulty"),
        "profile": profile,
        "spent_usd": float((body.get("budget") or {}).get("spent_usd") or 0),
    }
    for side, key in (("A", "a"), ("B", "b")):
        played = [r for r in sides[side]["results"] if r.get("termination") not in components.NOT_COMPARABLE]
        faults: dict[str, int] = {}
        endings: dict[str, int] = {}
        for r in played:
            for name, value in (r.get("grade_details") or {}).items():
                if (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and value
                    and name not in ("items", "correct")
                ):
                    faults[name] = faults.get(name, 0) + 1  # attempts the fault showed in, not how many times
            if not r.get("passed"):
                endings[r.get("termination")] = endings.get(r.get("termination"), 0) + 1
        out[f"{key}_attempts"] = len(played)
        out[f"{key}_passed"] = sum(1 for r in played if r.get("passed"))
        out[f"{key}_not_run"] = len(sides[side]["results"]) - len(played)
        out[f"{key}_cost_usd"] = round(sum(float(r.get("cost_usd") or 0) for r in played), 6)
        out[f"{key}_seconds"] = (
            round(sum(float(r.get("wall_s") or 0) for r in played) / len(played), 1) if played else 0.0
        )
        out[f"{key}_faults"] = faults
        out[f"{key}_endings"] = endings
    # scenario by scenario: the same world for both sides, judged by the engine's own victory rule
    scenarios = []
    for ra, rb in zip(sides["A"]["results"], sides["B"]["results"], strict=True):
        bad = [r for r in (ra, rb) if r.get("termination") in components.NOT_COMPARABLE]
        won = None if bad else pair_outcome(ra, rb, profile)["winner"]
        scenarios.append(
            {
                "a_passed": None if bad else bool(ra.get("passed")),
                "b_passed": None if bad else bool(rb.get("passed")),
                "winner": won,
            }
        )
    judged = [x for x in scenarios if x["winner"]]
    out["scenarios"] = scenarios
    out["pairs"] = len(judged)
    out["a_wins"] = sum(1 for x in judged if x["winner"] == "A")
    out["b_wins"] = sum(1 for x in judged if x["winner"] == "B")
    out["ties"] = sum(1 for x in judged if x["winner"] == "tie")
    out["same_result"] = sum(1 for x in judged if x["a_passed"] == x["b_passed"])  # both passed, or both did not
    out["p_value"] = sign_test(out["a_wins"], out["b_wins"])
    out["early_stop"] = body.get("early_stop")  # why the batch ended before its last scenario, if it did
    return out


def is_batch(sealed: dict) -> bool:
    return int((sealed["body"].get("settings") or {}).get("repeats") or 1) > 1


RUN_ID = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}")  # the only shape a run id has: nothing else reaches the disk
LIVE_LEVELS = ("easy", "standard")  # harder levels take too long and cost too much to watch live
HEARTBEAT_S = 10  # hosts cut a stream that stays silent; a comment line keeps it open
MAX_BODY = 8192  # a description of 1,200 characters, in any alphabet, with the rest of the request


class Refused(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


class Ledger:
    """How much the server has spent, in all and per provider, kept in a file so a restart cannot reset a limit.

    `by_provider` gives each provider its own budget ({"openrouter": 30.0, ...}); a race reserves its whole cap
    against the provider of each of its two models, so neither budget can be pushed over. `total_usd` is the limit
    on everything together (None: the provider budgets are the only limits). `limits` gives one sign-in its own
    number of races; the others get `runs_per_code`. Sign-ins are recorded by an id, never by the code itself.
    """

    def __init__(
        self,
        path: Path,
        total_usd: float | None,
        runs_per_code: int,
        by_provider: dict[str, float] | None = None,
        limits: dict[str, int] | None = None,
    ):
        self.path, self.total_usd, self.runs_per_code = Path(path), total_usd, runs_per_code
        self.by_provider, self.limits = dict(by_provider or {}), dict(limits or {})
        if total_usd is None and not self.by_provider:
            raise SystemExit("The runner needs a spending limit: a total, a budget per provider, or both.")
        self.lock = threading.Lock()
        self.state = {"spent_usd": 0.0, "held_usd": 0.0, "races": 0, "by_code": {}, "providers": {}}
        if self.path.exists():
            self.state.update(json.loads(self.path.read_text(encoding="utf-8")))
            self.state["held_usd"] = 0.0  # nothing is running at start-up
            for entry in self.state["providers"].values():
                entry["held_usd"] = 0.0
        try:
            self._save()  # a limit that cannot be written down is not a limit: fail now, not after the first race
        except OSError as e:
            raise SystemExit(
                f"Cannot write the spending ledger at {self.path}: {e}. "
                "The runner will not start without a place to record what it spends."
            ) from None

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, indent=1), encoding="utf-8")

    def _provider(self, name: str) -> dict:
        return self.state["providers"].setdefault(name, {"spent_usd": 0.0, "held_usd": 0.0})

    def left(self) -> float:
        """What is left of the limit on everything together (the sum of the provider budgets if there is no total)."""
        if self.total_usd is None:
            return round(sum(self.left_for(p) for p in self.by_provider), 6)
        return round(self.total_usd - self.state["spent_usd"] - self.state["held_usd"], 6)

    def left_for(self, provider: str) -> float | None:
        """What is left of one provider's budget; None when that provider has no budget of its own."""
        if provider not in self.by_provider:
            return None if self.total_usd is not None else 0.0  # budgets only: a provider without one has nothing
        entry = self.state["providers"].get(provider) or {}
        return round(self.by_provider[provider] - entry.get("spent_usd", 0.0) - entry.get("held_usd", 0.0), 6)

    def races_left(self, who: str) -> int:
        return max(0, self.limits.get(who, self.runs_per_code) - self.state["by_code"].get(who, 0))

    def hold(self, cap: float, who: str, providers: tuple[str, ...] = ()):
        """Reserve a race's whole cap before it starts: it can never push a limit over."""
        with self.lock:
            if self.races_left(who) <= 0:
                raise Refused(429, f"This sign-in has used its {self.limits.get(who, self.runs_per_code)} live races.")
            if self.total_usd is not None and cap > self.left() + 1e-9:
                raise Refused(402, "The live demo's spending limit has been reached. Recorded races still play.")
            for name in sorted(set(providers)):
                left = self.left_for(name)
                if left is not None and cap > left + 1e-9:
                    raise Refused(
                        402, f"The live budget for {name} models is used up. Choose models from another provider."
                    )
            self.state["held_usd"] += cap
            for name in set(providers):
                self._provider(name)["held_usd"] += cap
            self.state["by_code"][who] = self.state["by_code"].get(who, 0) + 1
            self._save()

    def settle(
        self, cap: float, spent: float, providers: tuple[str, ...] = (), spent_by: dict[str, float] | None = None
    ):
        """Release the hold and count what the race cost. `spent_by`: the cost per provider; without it (the match
        died and nobody knows) the whole amount is split evenly over the race's providers."""
        with self.lock:
            self.state["held_usd"] = max(0.0, self.state["held_usd"] - cap)
            self.state["spent_usd"] = round(self.state["spent_usd"] + spent, 6)
            names = sorted(set(providers))
            for name in names:
                entry = self._provider(name)
                entry["held_usd"] = max(0.0, entry["held_usd"] - cap)
                share = spent / len(names) if spent_by is None else spent_by.get(name, 0.0)
                entry["spent_usd"] = round(entry["spent_usd"] + share, 6)
            self.state["races"] += 1
            self._save()

    def uses_left(self, kind: str, who: str, limit: int) -> int:
        return max(0, limit - (self.state.get(kind) or {}).get(who, 0))

    def use(self, kind: str, who: str, limit: int):
        """Count one use of something each sign-in may do a limited number of times ("reads", "creates")."""
        with self.lock:
            used = self.state.setdefault(kind, {})
            if used.get(who, 0) >= limit:
                raise Refused(429, f"This sign-in has used its {limit} {kind} here.")
            used[who] = used.get(who, 0) + 1
            self._save()

    def hold_small(self, cap: float, provider: str):
        """Reserve the cap of something that is not a race (a model reading a description) against the same
        limits: the total and that provider's budget."""
        with self.lock:
            if self.total_usd is not None and cap > self.left() + 1e-9:
                raise Refused(402, "The live demo's spending limit has been reached.")
            left = self.left_for(provider)
            if left is not None and cap > left + 1e-9:
                raise Refused(402, f"The live budget for {provider} models is used up.")
            self.state["held_usd"] += cap
            self._provider(provider)["held_usd"] += cap
            self._save()

    def settle_small(self, cap: float, spent: float, provider: str):
        with self.lock:
            self.state["held_usd"] = max(0.0, self.state["held_usd"] - cap)
            self.state["spent_usd"] = round(self.state["spent_usd"] + spent, 6)
            entry = self._provider(provider)
            entry["held_usd"] = max(0.0, entry["held_usd"] - cap)
            entry["spent_usd"] = round(entry["spent_usd"] + spent, 6)
            self.state["read_usd"] = round(self.state.get("read_usd", 0.0) + spent, 6)
            self._save()


class Races:
    """What this server offers and the rules it enforces. One instance per server."""

    def __init__(
        self,
        base_suite: str,
        models: list[str],
        ledger: Ledger,
        max_usd: float,
        codes: set[str] | None = None,
        origins: set[str] | None = None,
        runs_dir: Path | None = None,
        batch_sizes: tuple[int, ...] = (),
        batch_usd_per_run: float = 0.06,
        batch_parallel: int = 5,
        batch_pass_rate: float = 0.8,
    ):
        # Early stop: a batch ends once a model has failed so often that it can no longer pass this share of the
        # scenarios asked for. The question is answered, and the rest of the money is not spent.
        self.batch_pass_rate = float(batch_pass_rate)
        # Batches: one test played `runs` times in one go (two models, or one model on both sides to see how
        # steadily it does the job). Off unless sizes are given. A batch's cap is `batch_usd_per_run` x runs.
        self.batch_sizes = tuple(sorted({int(n) for n in batch_sizes if int(n) > 1}))
        self.batch_usd_per_run, self.batch_parallel = float(batch_usd_per_run), max(1, int(batch_parallel))
        self.batches: list[dict] = []
        self.base_path = base_suite
        self.base = yaml.safe_load(Path(base_suite).read_bytes())
        loaded = suites.load(base_suite)
        self.tasks = {}
        for node in loaded["nodes"]:
            if "challenge" not in node:
                continue  # only locked challenge packages can be raced
            manifest = packages.resolve(node["challenge"]).manifest
            offered = manifest.get("difficulty") or ["standard"]
            self.tasks[node["id"]] = {
                "name": node["name"],
                "challenge": node["challenge"],
                "status": node["provenance"].get("status"),
                "levels": [lv for lv in LIVE_LEVELS if lv in offered] or ["standard"],
            }
        for model in models:
            price_for(*providers.parse(model))  # a model without a price cannot be capped: refuse now
        self.models = list(models)
        self.ledger, self.max_usd = ledger, max_usd
        self.codes, self.origins, self.runs_dir = codes, origins or set(), runs_dir
        self.busy = threading.Lock()
        # The live races already on file, read back from their sealed records: the folder is the truth, so a
        # restart forgets nothing and nothing can be listed that was not really run.
        self.results: list[dict] = []
        for path in sorted(Path(runs_dir).glob("*/record.json")) if runs_dir else []:
            try:
                sealed = json.loads(path.read_text(encoding="utf-8"))
                if is_batch(sealed):
                    self.batches.append(batch_summary(sealed))
                else:
                    self.results.append(summary(sealed))
            except (ValueError, KeyError, TypeError):
                continue  # not a race record: leave it out, never guess
        # Sessions are signed with a key made from the invites themselves: nothing more to store or to leak,
        # and changing the invites signs everyone out.
        self.session_key = hashlib.sha256(("gladiator-session\n" + "\n".join(sorted(codes or []))).encode()).digest()

    # ---- signing in: an invite is exchanged once for a session, so nothing is typed again ------------------
    def _sign(self, payload: str) -> str:
        return hmac.new(self.session_key, payload.encode(), hashlib.sha256).hexdigest()

    def session_for(self, who: str, now: float | None = None) -> dict:
        expires = int((time.time() if now is None else now) + SESSION_DAYS * 86400)
        payload = f"{who}.{expires}"
        token = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=") + "." + self._sign(payload)
        return {"token": token, "expires_unix": expires, "races_left": self.ledger.races_left(who)}

    def who_holds(self, token: str, now: float | None = None) -> str | None:
        """The sign-in a session token belongs to, or None if it is forged, expired or its invite is gone."""
        try:
            packed, signature = str(token).split(".")
            payload = base64.urlsafe_b64decode(packed + "=" * (-len(packed) % 4)).decode()
            who, expires = payload.split(".")
            if not hmac.compare_digest(signature, self._sign(payload)):
                return None
            if int(expires) < (time.time() if now is None else now):
                return None
        except (ValueError, UnicodeDecodeError):
            return None
        return who if who in {who_of(c) for c in self.codes or []} else None

    def identify(self, ask: dict) -> str:
        """Who is asking: a session token, or an invite sent directly. Raises Refused(403) for anyone else."""
        if self.codes is None:
            return "local"
        who = self.who_holds(ask.get("token") or "")
        if who:
            return who
        code = str(ask.get("code") or ask.get("invite") or "")
        if code and any(hmac.compare_digest(code, known) for known in self.codes):
            return who_of(code)
        raise Refused(403, "You are not signed in for live races, or the sign-in has expired.")

    def sign_in(self, ask: dict) -> dict:
        if not isinstance(ask, dict):
            raise Refused(400, "Send a JSON object.")
        if self.codes is None:
            return {"token": None, "expires_unix": None, "races_left": self.ledger.races_left("local")}
        return self.session_for(self.identify(ask))

    # ---- budgets: a model is on offer only while its provider can cover one more race ----------------------
    def budgets(self) -> list[dict]:
        return [
            {"provider": name, "limit_usd": limit, "left_usd": max(0.0, self.ledger.left_for(name))}
            for name, limit in sorted(self.ledger.by_provider.items())
        ]

    def cap_for(self, runs: int) -> float:
        """The hard cap of one request: a race's, or a batch's (so much per run)."""
        return self.max_usd if runs <= 1 else round(self.batch_usd_per_run * runs, 6)

    def affordable(self, model: str, runs: int = 1) -> bool:
        left = self.ledger.left_for(providers.parse(model)[0])
        return left is None or left + 1e-9 >= self.cap_for(runs)

    def options(self) -> dict:
        return {
            "tasks": [
                {"id": k, **{f: v[f] for f in ("name", "challenge", "status", "levels")}} for k, v in self.tasks.items()
            ],
            "models": [m for m in self.models if self.affordable(m)],
            # offered, but their provider's live budget cannot cover one more race
            "models_out": [m for m in self.models if not self.affordable(m)],
            "budgets": self.budgets(),
            "profiles": sorted(PROFILES),
            "cap_usd_per_race": self.max_usd,
            # batches on offer: how many runs, and the cap of each size
            "batches": [{"runs": n, "cap_usd": self.cap_for(n)} for n in self.batch_sizes],
            "batch_pass_rate": self.batch_pass_rate,
            "needs_code": self.codes is not None,
        }

    def races_run(self) -> dict:
        return {"races": list(self.results), "batches": list(self.batches)}

    def sealed_record(self, run_id: str) -> dict:
        """The sealed public record of one live race or batch on file, exactly as filed: what a results store
        needs to count it. Never the private half (the answer key of its generated worlds)."""
        if not self.runs_dir or not RUN_ID.fullmatch(run_id or ""):
            raise Refused(404, "No such record.")
        path = Path(self.runs_dir) / run_id / "record.json"
        if not path.is_file():
            raise Refused(404, "No such record.")
        return json.loads(path.read_text(encoding="utf-8"))

    def health(self) -> dict:
        return {
            "ok": True,
            "busy": self.busy.locked(),
            "left_usd": max(0.0, self.ledger.left()),
            "budgets": self.budgets(),
            "races": self.ledger.state["races"],
        }

    def check(self, ask: dict) -> dict:
        """Every value must be one this server offers. Returns the cleaned request."""
        if not isinstance(ask, dict):
            raise Refused(400, "Send a JSON object.")
        who = self.identify(ask)
        task = ask.get("task")
        if task not in self.tasks:
            raise Refused(400, f"Unknown task. Choose one of: {', '.join(self.tasks)}.")
        a, b = ask.get("a"), ask.get("b")
        if a not in self.models or b not in self.models:
            raise Refused(400, "Both models must come from the list this server offers.")
        runs = ask.get("runs") or 1
        if runs != 1 and runs not in self.batch_sizes:
            offered = ", ".join(str(n) for n in self.batch_sizes) or "none on this server"
            raise Refused(400, f"A batch must be one of these sizes: {offered}.")
        if a == b and runs == 1:  # one model against itself is a steadiness check, and one run cannot show that
            raise Refused(400, "Choose two different models.")
        for model in (a, b):
            if not self.affordable(model, runs):
                name = providers.parse(model)[0]
                raise Refused(
                    402, f"The live budget for {name} models is used up. Choose models from another provider."
                )
        level = ask.get("difficulty") or "standard"
        if level not in self.tasks[task]["levels"]:
            raise Refused(400, f"Difficulty must be one of: {', '.join(self.tasks[task]['levels'])}.")
        profile = ask.get("profile") or "budget"
        if profile not in PROFILES:
            raise Refused(400, f"Profile must be one of: {', '.join(sorted(PROFILES))}.")
        early = ask.get("early_stop", True) is not False  # on unless the request turns it off
        return {
            "task": task,
            "a": a,
            "b": b,
            "difficulty": level,
            "profile": profile,
            "who": who,
            "runs": runs,
            "early_stop": early and runs > 1,
        }

    def suite_for(self, ask: dict) -> dict:
        """The one-scenario suite a race plays: the base suite's settings and locked challenge, configured."""
        how_many = "One live scenario" if ask.get("runs", 1) == 1 else f"{ask['runs']} live scenarios"
        data = {
            "suite": "live-race" if ask.get("runs", 1) == 1 else "live-batch",
            "version": 1,
            "description": f"{how_many} of {ask['task']} ({ask['difficulty']}, {ask['profile']} profile).",
            "defaults": {**(self.base.get("defaults") or {}), "difficulty": ask["difficulty"]},
            "success": {**(self.base.get("success") or {}), "profile": ask["profile"]},
            "nodes": [{"id": ask["task"], "challenge": self.tasks[ask["task"]]["challenge"]}],
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "live-race.yaml"
            path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
            loaded = suites.load(str(path))
        loaded["path"] = f"live-race (from {self.base_path})"
        return loaded

    def run(self, ask: dict, on_event) -> tuple[dict, dict]:
        """Play the race (or the batch). Returns (sealed record, private half). Raises Refused before any spending."""
        ask = self.check(ask)
        runs = ask["runs"]
        cap = self.cap_for(runs)
        if not self.busy.acquire(blocking=False):
            raise Refused(409, "The runner is busy with another race or a test being checked. Try again in a minute.")
        try:
            suite = self.suite_for(ask)
            sides = {"A": providers.parse(ask["a"])[0], "B": providers.parse(ask["b"])[0]}
            self.ledger.hold(cap, ask["who"], tuple(sides.values()))
        except BaseException:
            self.busy.release()
            raise
        spent = cap  # if the match dies we cannot know what it cost: count the whole cap
        spent_by = None
        try:
            run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
            stopped: dict = {}
            failed = {"A": 0, "B": 0}
            counting = threading.Lock()
            need = math.ceil(self.batch_pass_rate * runs - 1e-9)  # passes a model needs out of `runs`

            def after(side: str, result: dict) -> str | None:
                if result.get("passed") or result.get("termination") in components.NOT_COMPARABLE:
                    return None
                with counting:
                    failed[side] += 1
                    if failed[side] <= runs - need or stopped:
                        return None
                    stopped.update(
                        side=side,
                        model=ask[side.lower()],
                        failed=failed[side],
                        runs=runs,
                        pass_rate=self.batch_pass_rate,
                    )
                return (
                    f"stopped early: {ask[side.lower()]} did not pass {failed[side]} of {runs}, "
                    f"so it could no longer pass {self.batch_pass_rate:.0%}"
                )

            body, operator = runner.run_match(
                suite,
                ask["a"],
                ask["b"],
                cap,
                repeats=runs,
                on_event=on_event,
                parallel=self.batch_parallel if runs > 1 else 1,
                after_attempt=after if ask["early_stop"] else None,
            )
            if stopped:
                body["early_stop"] = dict(stopped)
            body["run_id"] = run_id
            body["live"] = True  # a race started from the live view, not part of a calibrated suite run
            body["verdict"] = components.verdict(body)
            spent = float(body["budget"]["spent_usd"])
            spent_by = {}
            for side, name in sides.items():
                cost = sum(float(r.get("cost_usd") or 0) for r in body["contestants"][side]["results"])
                spent_by[name] = spent_by.get(name, 0.0) + cost
            body = dict(body, operator_sha256=record.sha256(operator))
            sealed = {"sha256": record.sha256(body), "body": body}
            if self.runs_dir:
                folder = Path(self.runs_dir) / run_id
                folder.mkdir(parents=True, exist_ok=False)
                (folder / "record.json").write_text(
                    json.dumps(sealed, indent=1, ensure_ascii=False, default=str), encoding="utf-8"
                )
                (folder / "operator.json").write_text(
                    json.dumps(operator, indent=1, ensure_ascii=False, default=str), encoding="utf-8"
                )
            if runs > 1:
                self.batches.append(batch_summary(sealed))
            else:
                self.results.append(summary(sealed))
            return sealed, operator
        finally:
            self.ledger.settle(cap, spent, tuple(sides.values()), spent_by)
            self.busy.release()


def first_sentence(text: str) -> str:
    return " ".join(str(text).split()).split(". ", 1)[0].rstrip(".") + "."


class Forks:
    """The conversation from a description to a test of one's own, one request at a time (engine/session.py).

    `reader`: the model that reads a signed-in visitor's description (None: descriptions are matched by their
    words only). Each read is capped at `read_usd`, held against that provider's budget like a race, and counted
    per sign-in. Creating a test costs no money but a minute or so of this machine, so it shares the one-thing-
    at-a-time rule with races and is counted per sign-in too. `open_create` lets anyone create, signed in or not.
    """

    def __init__(
        self,
        races: Races,
        reader: str | None = None,
        read_usd: float = 0.01,
        reads_per_code: int = 30,
        creates_per_code: int = 10,
        open_create: bool = False,
        levels: list[str] | None = None,
    ):
        self.races, self.ledger = races, races.ledger
        self.reader, self.read_usd = reader, read_usd
        self.reads_per_code, self.creates_per_code = reads_per_code, creates_per_code
        self.open_create, self.levels = open_create, levels
        if reader:
            price_for(*providers.parse(reader))  # a reader without a price cannot be capped: refuse now
        self.make_reader = lambda: session.ModelReader(self.reader, self.read_usd)
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()
        # The library is read once: it is part of the image and does not change while the server runs.
        self.known: dict = {}
        self.offered: dict | None = None

    # ---- who is asking, and what they may do
    def who(self, ask: dict) -> str | None:
        try:
            return self.races.identify(ask)
        except Refused:
            return None

    def status(self, ask: dict) -> dict:
        who = self.who(ask if isinstance(ask, dict) else {})
        return {
            "needs_invite": self.races.codes is not None,
            "signed_in": who is not None,
            "reads": bool(self.reader and who),
            "reads_when_signed_in": bool(self.reader),
            "reader": self.reader if self.reader and who else None,
            "can_create": bool(who or self.open_create),
            "creates_left": self.ledger.uses_left("creates", who or "public", self.creates_per_code),
        }

    # ---- the steps
    def tests(self) -> dict:
        if self.offered is None:
            known = blueprint.library()
            out = []
            for pkg in packages.catalog().values():
                if pkg.ref in known and pkg.status.get("status") != "retired":
                    about = first_sentence(pkg.manifest.get("description", ""))
                    out.append({"ref": pkg.ref, "name": pkg.manifest["name"], "about": about})
            self.offered = {"tests": out}
        return self.offered

    def _questions(self, talk: session.ForkSession, ref: str) -> dict:
        chosen = talk.choose(ref)
        return {
            "ref": ref,
            "name": chosen["name"],
            "about": first_sentence(chosen["description"]),
            "questions": chosen["questions"],
            "not_covered": chosen["not_covered"],
            "unusable": chosen["unusable"],
            "places": blueprint.places(talk.bp),
        }

    def describe(self, ask: dict) -> dict:
        text = " ".join(str(ask.get("description") or "").split())
        if not text:
            raise Refused(400, "Describe the flow you want to test, in a sentence or two.")
        if len(text) > MAX_DESCRIPTION:
            raise Refused(400, f"Keep the description under {MAX_DESCRIPTION} characters.")
        who, reader, provider = self.who(ask), None, None
        if self.reader and who:
            provider = providers.parse(self.reader)[0]
            try:  # no budget, no reads left or no reader: the description's words still match
                self.ledger.hold_small(self.read_usd, provider)
                try:
                    self.ledger.use("reads", who, self.reads_per_code)
                    reader = self.make_reader()
                except Exception:
                    self.ledger.settle_small(self.read_usd, 0.0, provider)
                    raise
            except Exception as e:  # noqa: BLE001  the page works without a reader
                if not isinstance(e, Refused):
                    runner.say(f"  [serve] the reader could not start: {type(e).__name__}")
        talk = session.ForkSession(reader=reader, known=self.known)
        try:
            found = talk.describe(text)
        finally:
            if reader:
                self.ledger.settle_small(self.read_usd, reader.spent_usd, provider)
        # Tests that match the description's words better than any test with open choices, but declare none
        # themselves: the page names them, and does not pass an adaptable test off as the closest one.
        closer = []
        for m in found["matches"]:
            if m["choices"]:
                break
            closer.append({"ref": m["ref"], "name": m["name"], "matched": m["matched"]})
        out = {
            "matches": [
                {"ref": m["ref"], "name": m["name"], "about": first_sentence(m["description"]), "matched": m["matched"]}
                for m in found["forkable"]
            ],
            "closer": closer[:3],
            "proposed": found["proposed"],
            "why": found["why"],
            "read_by": found["read_by"],
            "note": found["note"],
            "test": None,
        }
        if found["proposed"]:
            out["test"] = self._questions(talk, found["proposed"])
        return out

    def _forkable(self, ask: dict) -> str:
        ref = str(ask.get("ref") or "")
        if ref not in {t["ref"] for t in self.tests()["tests"]}:
            raise Refused(400, "Choose one of the tests this page offers.")
        return ref

    def choose(self, ask: dict) -> dict:
        return self._questions(session.ForkSession(known=self.known), self._forkable(ask))

    def _settle(self, ask: dict) -> tuple[session.ForkSession, dict]:
        """A conversation brought to the values in the request: the session, and what is wrong with any value."""
        talk = session.ForkSession(known=self.known)
        talk.choose(self._forkable(ask))
        values = ask.get("values")
        if not isinstance(values, dict):
            raise Refused(400, "Send the values as an object.")
        return talk, talk.fill(values)

    def review(self, ask: dict) -> dict:
        talk, problems = self._settle(ask)
        look = talk.review()
        sets = " ".join(f"--set {c['name']}={c['to']}" for c in look["changed"])
        return {
            "values": look["values"],
            "problems": problems,
            "broken": look["broken"],
            "changed": look["changed"],
            "shown": None if look["broken"] else blueprint.shown(talk.bp, talk.values),
            "command": f"uv run gladiator fork {talk.pkg.ref} --to yourname/your-test {sets}".strip(),
        }

    def create(self, ask: dict) -> dict:
        who = self.who(ask)
        if not (who or self.open_create):
            raise Refused(403, "Creating a test here is open to signed-in visitors for now.")
        who = who or "public"
        talk, problems = self._settle(ask)
        if problems or talk.review()["broken"]:
            raise Refused(400, "Some values are not allowed. Go back and check your choices.")
        owner, name = session.slug(ask.get("owner") or "")[:40], session.slug(ask.get("name") or "")[:40]
        if not owner or not name:
            raise Refused(400, "Give the test a name, and your name or team: each needs a letter or a digit.")
        if owner in session.RESERVED_OWNERS:
            raise Refused(400, f'"{owner}" is the library\'s own name. Use your own name or team.')
        if not self.races.busy.acquire(blocking=False):
            raise Refused(409, "The runner is busy with a race or another test being checked. Try again in a minute.")
        try:
            self.ledger.use("creates", who, self.creates_per_code)
        except Refused:
            self.races.busy.release()
            raise
        job_id = secrets.token_urlsafe(12)
        job = {
            "state": "checking",
            "level": None,
            "number": 0,
            "of": 0,
            "started": time.time(),
            "id": f"{owner}/{name}",
        }
        with self.lock:
            self._forget_old()
            while len(self.jobs) >= MAX_JOBS:
                del self.jobs[min(self.jobs, key=lambda k: self.jobs[k]["started"])]
            self.jobs[job_id] = job

        def on_level(level, number, of):
            job.update(level=level, number=number, of=of)

        def work():
            try:
                label = " ".join(str(ask.get("name") or "").split())[:60] or None
                result, archive = blueprint.fork_archive(
                    talk.pkg, talk.bp, f"{owner}/{name}", talk.values, label, self.levels, on_level
                )
                if result["ok"]:
                    job.update(
                        state="created",
                        ref=result["ref"],
                        forked_from=result["forked_from"],
                        changed=result["changed"],
                        fingerprint=result["fingerprint"][:12],
                        checked=plain_summary(result["levels"]),
                        suite=result["written"]["suite"],
                        archive=archive,
                    )
                else:
                    found = plain_problems(result.get("levels") or {}) or result["problems"]
                    job.update(state="refused", problems=found)
            except Exception as e:  # noqa: BLE001  tell the visitor; nothing was written anywhere
                job.update(state="failed", problems=[f"{type(e).__name__}: {e}"])
            finally:
                job["seconds"] = round(time.time() - job["started"], 1)
                self.races.busy.release()

        threading.Thread(target=work, daemon=True).start()
        return {"job": job_id}

    def _forget_old(self):
        """A created test waits here for its download for JOB_MINUTES, then it is gone from this server."""
        now = time.time()
        for old in [k for k, j in self.jobs.items() if now - j["started"] > JOB_MINUTES * 60]:
            del self.jobs[old]

    def job(self, job_id: str) -> dict:
        with self.lock:
            self._forget_old()
        job = self.jobs.get(job_id)
        if not job:
            raise Refused(404, "That test is no longer waiting here. Create it again.")
        out = {k: v for k, v in job.items() if k not in ("archive", "started")}
        out["seconds"] = job.get("seconds", round(time.time() - job["started"], 1))
        out["download"] = bool(job.get("archive"))
        out["kept_minutes"] = JOB_MINUTES
        return out

    def download(self, job_id: str) -> tuple[str, bytes]:
        with self.lock:
            self._forget_old()
        job = self.jobs.get(job_id) or {}
        if not job.get("archive"):
            raise Refused(404, "That test is no longer waiting here. Create it again.")
        return job["ref"].replace("/", "-").replace("@", "-") + ".zip", job["archive"]


FORK_POSTS = ("/fork/status", "/fork/describe", "/fork/choose", "/fork/review", "/fork/create")
STATIC = {
    "/build": ("build.html", "text/html; charset=utf-8"),
    "/build.css": ("build.css", "text/css; charset=utf-8"),
    "/build.js": ("build.js", "text/javascript; charset=utf-8"),
}
# The page loads nothing from anywhere else and may only talk to this server.
PAGE_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def handler_for(races: Races, forks: Forks | None = None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # one short line per request; never the body (it carries the code)
            path = self.path.split("?")[0]  # nor the query: an invitation link carries its invite there
            if path != "/fork/job":  # the page asks every second while a test is checked
                runner.say(f"  [serve] {self.command} {path} {args[1] if len(args) > 1 else ''}")

        def _cors(self):
            origin = self.headers.get("Origin")
            if origin and (origin in races.origins or "*" in races.origins):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

        def _json(self, status: int, value: dict):
            data = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _bytes(self, data: bytes, content_type: str, extra: dict | None = None):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")  # an invitation link must not leak onward
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path, _, query = self.path.partition("?")
            asked = dict(pair.split("=", 1) for pair in query.split("&") if "=" in pair)
            if path == "/options":
                self._json(200, races.options())
            elif path == "/health":
                self._json(200, races.health())
            elif path == "/races":
                self._json(200, races.races_run())
            elif path.startswith("/records/"):  # one filed race, public half only
                try:
                    self._json(200, races.sealed_record(path[len("/records/") :]))
                except Refused as no:
                    self._json(no.status, {"error": no.reason})
            elif forks and path in STATIC:
                name, content_type = STATIC[path]
                self._bytes((PAGES / name).read_bytes(), content_type, {"Content-Security-Policy": PAGE_POLICY})
            elif forks and path == "/fork/tests":
                self._json(200, forks.tests())
            elif forks and path in ("/fork/job", "/fork/download"):
                try:
                    if path == "/fork/job":
                        return self._json(200, forks.job(asked.get("id", "")))
                    name, data = forks.download(asked.get("id", ""))
                    self._bytes(data, "application/zip", {"Content-Disposition": f'attachment; filename="{name}"'})
                except Refused as no:
                    self._json(no.status, {"error": no.reason})
            else:
                self._json(404, {"error": "Not found."})

        def do_POST(self):
            path = self.path.split("?")[0]
            if path not in ("/race", "/signin") and not (forks and path in FORK_POSTS):
                return self._json(404, {"error": "Not found."})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 < length <= MAX_BODY:
                    raise Refused(400, "Send a small JSON object.")
                try:
                    ask = json.loads(self.rfile.read(length))
                except ValueError:
                    raise Refused(400, "That is not JSON.") from None
                if path == "/signin":  # an invite (or a session to renew) for a session token
                    return self._json(200, races.sign_in(ask))
                if path in FORK_POSTS:
                    if not isinstance(ask, dict):
                        raise Refused(400, "Send a JSON object.")
                    return self._json(200, getattr(forks, path.rsplit("/", 1)[1])(ask))
                races.check(ask)  # refuse a bad request with a plain answer, before any stream opens
            except Refused as no:
                return self._json(no.status, {"error": no.reason})

            events: queue.Queue = queue.Queue()
            result: dict = {}

            def play():
                try:
                    result["sealed"], result["operator"] = races.run(ask, events.put)
                except Refused as no:
                    result["refused"] = no
                except Exception as e:  # noqa: BLE001  report it to the viewer; the ledger already counted the cap
                    result["error"] = f"{type(e).__name__}: {e}"
                finally:
                    events.put(None)

            worker = threading.Thread(target=play, daemon=True)
            worker.start()
            # wait for the first sign of life so a refusal (busy, limit reached) is still a plain answer
            first = events.get()
            if first is None and "refused" in result:
                return self._json(result["refused"].status, {"error": result["refused"].reason})

            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            def send(kind: str, value) -> bool:
                try:
                    data = json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)
                    self.wfile.write(f"event: {kind}\ndata: {data}\n\n".encode())
                    self.wfile.flush()
                    return True
                except OSError:
                    return False  # the viewer left; the match still finishes and is still counted

            listening, event = True, first
            batch = (ask.get("runs") or 1) > 1  # a batch streams its attempts, not every step of each
            quiet_since = time.time()
            while event is not None:
                if listening and batch and event["event"] == "step":
                    # a batch is too much to watch call by call, but a viewer must see it move: the bare step
                    slim = {k: event.get(k) for k in ("event", "side", "attempt", "turn", "tool_name")}
                    listening = send("step", slim)
                    quiet_since = time.time()
                elif listening:
                    listening = send(event["event"], event)
                    quiet_since = time.time()
                elif listening and time.time() - quiet_since > HEARTBEAT_S:
                    # steps are happening but a batch does not send them: still keep the stream from going silent
                    try:
                        self.wfile.write(b": still running\n\n")
                        self.wfile.flush()
                    except OSError:
                        listening = False
                    quiet_since = time.time()
                while True:
                    try:
                        event = events.get(timeout=HEARTBEAT_S)
                        break
                    except queue.Empty:  # a model is thinking: keep the stream open
                        if listening:
                            try:
                                self.wfile.write(b": still running\n\n")
                                self.wfile.flush()
                            except OSError:
                                listening = False
            if "error" in result:
                send("failed", {"error": result["error"]})
            elif "sealed" in result and batch:  # the facts of the batch; its sealed record stays on the runner
                send("batch", {"batch": batch_summary(result["sealed"])})
            elif "sealed" in result:
                send("record", {"record": result["sealed"], "operator": result["operator"]})

    return Handler


def serve(races: Races, host: str, port: int, forks: Forks | None = None) -> ThreadingHTTPServer:
    if races.codes is None and host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("Refusing to listen beyond this machine without access codes (set GLADIATOR_RACE_CODES).")
    return ThreadingHTTPServer((host, port), handler_for(races, forks))
