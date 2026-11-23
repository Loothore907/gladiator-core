"""A race runner: start one match over HTTP and watch it happen.

`gladiator serve` answers three requests:

  GET  /options   what a race can be made of: the tasks, models, difficulty levels and scoring profiles on offer
  GET  /health    whether a race is running, and how much of the spending limit is left
  POST /race      {"task", "a", "b", "difficulty", "profile", "code"}: plays one fresh scenario of that task between
                  the two models and streams what happens (text/event-stream: the runner's events, one per message),
                  ending with a `record` message that carries the sealed record and its private half.

A race is a suite built from safe choices, never new content: the task comes from a base suite's locked
challenges, and every value in the request must be one this server offers (PRODUCT.md: "configurable, not
editable"). Free text never reaches the engine.

Money is guarded three times: every race has a hard cap (`--max-usd` semantics of a normal match), a race may only
start if that cap still fits in what is left of the server's total, and only one race runs at a time. With no
access codes set the server only listens on this machine.
"""

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

LIVE_LEVELS = ("easy", "standard")  # harder levels take too long and cost too much to watch live
HEARTBEAT_S = 10  # hosts cut a stream that stays silent; a comment line keeps it open
MAX_BODY = 4096


class Refused(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


class Ledger:
    """How much the server has spent, kept in a file so a restart cannot reset the limit."""

    def __init__(self, path: Path, total_usd: float, runs_per_code: int):
        self.path, self.total_usd, self.runs_per_code = Path(path), total_usd, runs_per_code
        self.lock = threading.Lock()
        self.state = {"spent_usd": 0.0, "held_usd": 0.0, "races": 0, "by_code": {}}
        if self.path.exists():
            self.state.update(json.loads(self.path.read_text(encoding="utf-8")))
            self.state["held_usd"] = 0.0  # nothing is running at start-up
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

    def left(self) -> float:
        return round(self.total_usd - self.state["spent_usd"] - self.state["held_usd"], 6)

    def hold(self, cap: float, code: str):
        """Reserve a race's whole cap before it starts: it can never push the total over."""
        with self.lock:
            if self.state["by_code"].get(code, 0) >= self.runs_per_code:
                raise Refused(429, f"This access code has used its {self.runs_per_code} live races.")
            if cap > self.left() + 1e-9:
                raise Refused(402, "The live demo's spending limit has been reached. Recorded races still play.")
            self.state["held_usd"] += cap
            self.state["by_code"][code] = self.state["by_code"].get(code, 0) + 1
            self._save()

    def settle(self, cap: float, spent: float):
        with self.lock:
            self.state["held_usd"] = max(0.0, self.state["held_usd"] - cap)
            self.state["spent_usd"] = round(self.state["spent_usd"] + spent, 6)
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

    def options(self) -> dict:
        return {
            "tasks": [
                {"id": k, **{f: v[f] for f in ("name", "challenge", "status", "levels")}} for k, v in self.tasks.items()
            ],
            "models": self.models,
            "profiles": sorted(PROFILES),
            "cap_usd_per_race": self.max_usd,
            "needs_code": self.codes is not None,
        }

    def health(self) -> dict:
        return {
            "ok": True,
            "busy": self.busy.locked(),
            "left_usd": max(0.0, self.ledger.left()),
            "races": self.ledger.state["races"],
        }

    def check(self, ask: dict) -> dict:
        """Every value must be one this server offers. Returns the cleaned request."""
        if not isinstance(ask, dict):
            raise Refused(400, "Send a JSON object.")
        code = str(ask.get("code") or "")
        if self.codes is not None and not any(hmac.compare_digest(code, known) for known in self.codes):
            raise Refused(403, "That access code is not valid.")
        task = ask.get("task")
        if task not in self.tasks:
            raise Refused(400, f"Unknown task. Choose one of: {', '.join(self.tasks)}.")
        a, b = ask.get("a"), ask.get("b")
        if a not in self.models or b not in self.models:
            raise Refused(400, "Both models must come from the list this server offers.")
        if a == b:
            raise Refused(400, "Choose two different models.")
        level = ask.get("difficulty") or "standard"
        if level not in self.tasks[task]["levels"]:
            raise Refused(400, f"Difficulty must be one of: {', '.join(self.tasks[task]['levels'])}.")
        profile = ask.get("profile") or "budget"
        if profile not in PROFILES:
            raise Refused(400, f"Profile must be one of: {', '.join(sorted(PROFILES))}.")
        return {"task": task, "a": a, "b": b, "difficulty": level, "profile": profile, "code": code or "local"}

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
            self.ledger.hold(self.max_usd, ask["code"])
        except BaseException:
            self.busy.release()
            raise
        spent = self.max_usd  # if the match dies we cannot know what it cost: count the whole cap
        try:
            run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
            body, operator = runner.run_match(suite, ask["a"], ask["b"], self.max_usd, repeats=1, on_event=on_event)
            body["run_id"] = run_id
            body["live"] = True  # a race started from the live view, not part of a calibrated suite run
            body["verdict"] = components.verdict(body)
            spent = float(body["budget"]["spent_usd"])
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
            return sealed, operator
        finally:
            self.ledger.settle(self.max_usd, spent)
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
            else:
                self._json(404, {"error": "Not found."})

        def do_POST(self):
            if self.path.split("?")[0] != "/race":
                return self._json(404, {"error": "Not found."})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 < length <= MAX_BODY:
                    raise Refused(400, "Send a small JSON object.")
                try:
                    ask = json.loads(self.rfile.read(length))
                except ValueError:
                    raise Refused(400, "That is not JSON.") from None
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
