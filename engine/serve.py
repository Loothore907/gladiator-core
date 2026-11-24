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
import queue
import secrets
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

import providers
from engine import packages, record, runner
from engine import suite as suites
from engine.budget import price_for
from scorers import components
from scorers.efficiency import PROFILES

SESSION_DAYS = 30  # how long a sign-in lasts


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
        "difficulty": (body.get("settings") or {}).get("defaults", {}).get("difficulty"),
    }


LIVE_LEVELS = ("easy", "standard")  # harder levels take too long and cost too much to watch live
HEARTBEAT_S = 10  # hosts cut a stream that stays silent; a comment line keeps it open
MAX_BODY = 4096


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
    ):
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
                self.results.append(summary(json.loads(path.read_text(encoding="utf-8"))))
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

    def affordable(self, model: str) -> bool:
        left = self.ledger.left_for(providers.parse(model)[0])
        return left is None or left + 1e-9 >= self.max_usd

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
            "needs_code": self.codes is not None,
        }

    def races_run(self) -> dict:
        return {"races": list(self.results)}

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
        if a == b:
            raise Refused(400, "Choose two different models.")
        for model in (a, b):
            if not self.affordable(model):
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
        return {"task": task, "a": a, "b": b, "difficulty": level, "profile": profile, "who": who}

    def suite_for(self, ask: dict) -> dict:
        """The one-scenario suite a race plays: the base suite's settings and locked challenge, configured."""
        data = {
            "suite": "live-race",
            "version": 1,
            "description": f"One live scenario of {ask['task']} ({ask['difficulty']}, {ask['profile']} profile).",
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
        """Play the race. Returns (sealed record, private half). Raises Refused before any spending."""
        ask = self.check(ask)
        if not self.busy.acquire(blocking=False):
            raise Refused(409, "Another race is running. Try again in a minute.")
        try:
            suite = self.suite_for(ask)
            sides = {"A": providers.parse(ask["a"])[0], "B": providers.parse(ask["b"])[0]}
            self.ledger.hold(self.max_usd, ask["who"], tuple(sides.values()))
        except BaseException:
            self.busy.release()
            raise
        spent = self.max_usd  # if the match dies we cannot know what it cost: count the whole cap
        spent_by = None
        try:
            run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
            body, operator = runner.run_match(suite, ask["a"], ask["b"], self.max_usd, repeats=1, on_event=on_event)
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
            self.results.append(summary(sealed))
            return sealed, operator
        finally:
            self.ledger.settle(self.max_usd, spent, tuple(sides.values()), spent_by)
            self.busy.release()


def handler_for(races: Races):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # one short line per request; never the body (it carries the code)
            runner.say(f"  [serve] {self.command} {self.path.split('?')[0]} {args[1] if len(args) > 1 else ''}")

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

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/options":
                self._json(200, races.options())
            elif path == "/health":
                self._json(200, races.health())
            elif path == "/races":
                self._json(200, races.races_run())
            else:
                self._json(404, {"error": "Not found."})

        def do_POST(self):
            path = self.path.split("?")[0]
            if path not in ("/race", "/signin"):
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
            while event is not None:
                if listening:
                    listening = send(event["event"], event)
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
            elif "sealed" in result:
                send("record", {"record": result["sealed"], "operator": result["operator"]})

    return Handler


def serve(races: Races, host: str, port: int) -> ThreadingHTTPServer:
    if races.codes is None and host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("Refusing to listen beyond this machine without access codes (set GLADIATOR_RACE_CODES).")
    return ThreadingHTTPServer((host, port), handler_for(races))
