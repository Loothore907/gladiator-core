"""The assistant connection on a hosted runner: the calls of engine/assistant.py behind an address.

On a person's own machine, `gladiator mcp` serves one checkout over stdio. Here the same calls are served over
HTTP to many callers, and each caller reaches only a space of its own:

  POST /mcp               MCP (streamable HTTP, stateless): what an assistant connects to
  GET  /api               what can be called and with what, as data (needs no key)
  POST /api/<call>        the same calls as plain JSON, for a program
  GET  /space/download    one test kept for a caller, through a link that works for a short while

Who may call. Every request carries a key: `Authorization: Bearer <key>`, or, for an assistant whose settings
take only an address, the key as the last part of it (`/mcp/<key>`). A key is an organisation's own
(`GLADIATOR_API_KEYS`, "name:key" pairs) or one of the runner's invites; a browser's session token works too.

What a key opens. A private space: a folder on the runner's own disk, laid out like a checkout, that holds the
tests that key created and nothing else. A space never joins the library, and one key cannot see or name
another's space: the space is worked out from the key on every request, never taken from the request.

What it costs. Nothing in model spend: the caller's own model does the reading, and this server calls none.
Creating a test takes a minute or two of processor, so it shares the one-thing-at-a-time rule with races, a key
may create a limited number, and a space holds a limited number of tests.

MCP is served by the MCP library's own web app, on an inner port of this machine that only this process can
reach; the runner checks the key and passes each request on with the caller's space. No protocol is hand-made.
"""

import asyncio
import base64
import hmac
import http.client
import json
import logging
import secrets
import threading
import time
from pathlib import Path

from engine import assistant, blueprint, runner, session
from engine.serve import Races, Refused

LINK_MINUTES = 20  # how long a download link works
WAIT_S = 30.0  # how long one call waits for the checks: under the minute at which hosts cut a silent request
MAX_MCP_BODY = 262144
MAX_API_BODY = 16384
PASSED_ON = ("Content-Type", "Accept", "Mcp-Protocol-Version", "Mcp-Session-Id", "Last-Event-ID")
PASSED_BACK = ("Content-Type", "Mcp-Session-Id", "Allow", "Cache-Control")
CALLS = ("list_tests", "read_requirements", "check_design", "create_test", "get_result", "get_download_link")


def _packed(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _unpacked(text: str) -> str:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode()


class Spaces:
    """Who may call, each caller's private space, and the calls themselves. One instance per runner.

    `keys`: {organisation name: its key}. `state`: the runner's own folder; spaces live in `state/spaces/`.
    `levels` limits validation to some difficulty levels (tests use it to stay quick)."""

    def __init__(
        self,
        races: Races,
        state: Path,
        keys: dict[str, str] | None = None,
        levels: list[str] | None = None,
        wait_s: float = WAIT_S,
        max_kept: int = 50,
        creates_per_key: int = 30,
        public_url: str = "",
    ):
        self.races, self.root = races, Path(state) / "spaces"
        self.keys = {session.slug(name): key for name, key in (keys or {}).items() if session.slug(name) and key}
        self.levels, self.wait_s = levels, wait_s
        self.max_kept, self.creates_per_key = max_kept, creates_per_key
        self.base = public_url.rstrip("/")
        self.by_space: dict[str, assistant.Designs] = {}
        self.lock = threading.Lock()
        self.inner = None  # the MCP web app, started when the first assistant connects
        self.port = None
        self.inner_secret = secrets.token_urlsafe(24)
        self.described: dict | None = None

    # ---- who is calling, and their space
    def space_of(self, key: str) -> str | None:
        """The space a key opens, or None for a key this runner does not know. An organisation's key opens the
        space named after it; an invite or a session opens the space of that sign-in."""
        key = str(key or "").strip()
        usable = bool(key) and key.isascii()
        for name, known in self.keys.items() if usable else ():
            if hmac.compare_digest(key.encode(), known.encode()):
                return f"org-{name}"
        if self.races.codes is None:
            return "local"  # a runner without invites listens on this machine only: everyone on it is the owner
        if not usable:
            return None
        try:
            return self.races.identify({"token": key, "code": key})
        except Refused:
            return None

    def designs(self, space: str) -> assistant.Designs:
        with self.lock:
            if space not in self.by_space:

                def allow(who=space):
                    try:
                        self.races.ledger.use("space_creates", who, self.creates_per_key)
                    except Refused:
                        return f"This key has created {self.creates_per_key} tests here, which is its limit."
                    return None

                self.by_space[space] = assistant.Designs(
                    self.root / space,
                    self.levels,
                    self.wait_s,
                    busy=self.races.busy,
                    allow=allow,
                    max_kept=self.max_kept,
                    link=lambda ref, who=space: self.link(who, ref),
                )
            return self.by_space[space]

    # ---- a test leaves its space only through a link its owner asked for
    def link(self, space: str, ref: str) -> str:
        payload = f"{space}|{ref}|{int(time.time() + LINK_MINUTES * 60)}"
        return f"{self.base}/space/download?t={_packed(payload)}.{self.races._sign('download' + chr(10) + payload)}"

    def open_link(self, token: str, now: float | None = None) -> tuple[str, bytes]:
        """The file a download link stands for. Refused for a link that is forged, changed or too old."""
        gone = Refused(404, "This download link is not valid any more. Ask for a new one.")
        try:
            packed, signature = str(token).split(".")
            payload = _unpacked(packed)
            space, ref, expires = payload.split("|")
            if not hmac.compare_digest(signature, self.races._sign("download\n" + payload)):
                raise gone
            if int(expires) < (time.time() if now is None else now):
                raise gone
        except (ValueError, UnicodeDecodeError):
            raise gone from None
        data = blueprint.archive(self.root / space, ref)
        if data is None:
            raise gone
        return ref.replace("/", "-").replace("@", "-") + ".zip", data

    # ---- the calls, as plain JSON
    def call(self, space: str, name: str, ask: dict) -> dict:
        if name not in CALLS:
            raise Refused(404, f"No such call. The calls are: {', '.join(CALLS)}.")
        if not isinstance(ask, dict):
            raise Refused(400, "Send the call's values as a JSON object.")
        d = self.designs(space)
        if name == "list_tests":
            return d.tests(str(ask.get("description") or ""))
        if name == "read_requirements":
            return d.requirements(ask.get("test"))
        if name == "check_design":
            return d.check(ask.get("test"), ask.get("values"))
        if name == "create_test":
            owner, test_name = str(ask.get("owner") or ""), str(ask.get("name") or "")
            return d.create(ask.get("test"), ask.get("values"), owner, test_name, str(ask.get("version") or "1.0.0"))
        if name == "get_result":
            return d.result(ask.get("job"))
        return d.download(ask.get("test"))

    def describe(self) -> dict:
        """What can be called and with what: the MCP tools' own names, descriptions and schemas, so the JSON API
        and the MCP connection cannot drift apart."""
        if self.described is None:
            tools = asyncio.run(self._server().list_tools())
            self.described = {
                "what": "Build a Gladiator test for a person's own flow, from a library test's open choices.",
                "instructions": assistant.INSTRUCTIONS,
                "mcp": "POST /mcp (streamable HTTP). Send the key as 'Authorization: Bearer <key>'.",
                "api": "POST /api/<call> with a JSON object of the call's values and the same header.",
                "calls": [
                    {"call": t.name, "what_it_does": " ".join((t.description or "").split()), "values": t.input_schema}
                    for t in tools
                ],
            }
        return self.described

    # ---- MCP: the library's own web app on an inner port, reached only through this runner
    def _server(self):
        return assistant.build(per_request=lambda headers: self.designs(headers.get("x-gladiator-space") or "nobody"))

    def start(self) -> None:
        with self.lock:
            if self.inner is not None:
                return
            import uvicorn

            for noisy in ("mcp", "uvicorn", "uvicorn.error"):
                logging.getLogger(noisy).setLevel(logging.WARNING)
            app = self._server().streamable_http_app(json_response=True, stateless_http=True)
            secret = self.inner_secret.encode()

            async def guarded(scope, receive, send):
                """Only this process may speak to the inner app: it alone says whose space a request is for."""
                if scope["type"] == "http":
                    given = dict(scope.get("headers") or []).get(b"x-gladiator-inner", b"")
                    if not hmac.compare_digest(given, secret):
                        await send({"type": "http.response.start", "status": 403, "headers": []})
                        await send({"type": "http.response.body", "body": b""})
                        return
                await app(scope, receive, send)

            config = uvicorn.Config(guarded, host="127.0.0.1", port=0, log_level="warning", access_log=False)
            inner = uvicorn.Server(config)
            threading.Thread(target=inner.run, daemon=True).start()
            until = time.time() + 20
            while not inner.started and time.time() < until:
                time.sleep(0.02)
            if not inner.started:
                raise Refused(503, "The assistant connection could not start. Try again in a minute.")
            self.port = inner.servers[0].sockets[0].getsockname()[1]
            self.inner = inner

    def forward(self, method: str, headers, body: bytes, space: str) -> tuple[int, dict, bytes]:
        """One MCP request passed to the inner app with the caller's space, and its answer."""
        self.start()
        send = {name: headers.get(name) for name in PASSED_ON if headers.get(name)}
        send.update({"X-Gladiator-Space": space, "X-Gladiator-Inner": self.inner_secret})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.wait_s + 30)
        try:
            conn.request(method, "/mcp", body=body or None, headers=send)
            answer = conn.getresponse()
            data = answer.read()
            back = {name: answer.getheader(name) for name in PASSED_BACK if answer.getheader(name)}
            return answer.status, back, data
        finally:
            conn.close()

    # ---- the runner's request handler hands these paths over
    @staticmethod
    def handles(path: str) -> bool:
        return path in ("/api", "/mcp", "/space/download") or path.startswith(("/api/", "/mcp/"))

    def answer(self, handler, method: str) -> None:
        """Answer one request through the runner's request handler (engine/serve.py): it supplies the headers,
        the body and the two ways to reply (`_json`, `_bytes`)."""
        _answer(self, handler, method)


def _answer(spaces: Spaces, handler, method: str) -> None:
    path, _, query = handler.path.partition("?")
    asked = dict(pair.split("=", 1) for pair in query.split("&") if "=" in pair)
    if not spaces.base:
        # No address was given at start (`gladiator serve --public-url`): links are made for the address this
        # request was sent to. Only the Host header says that; a forwarding header is whatever its sender wrote,
        # and a link built from one would send a person's download to somebody else's server.
        host = handler.headers.get("Host") or ""
        local = host.split(":")[0] in ("127.0.0.1", "localhost")
        spaces.base = f"{'http' if local else 'https'}://{host}"

    # The body is read before anything is decided. A request refused with its body unread would leave that body
    # on the connection, where it is taken for the next request and ends up in the log.
    limit = MAX_MCP_BODY if path.startswith("/mcp") else MAX_API_BODY
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        length = -1
    if length < 0 or length > limit or handler.headers.get("Transfer-Encoding"):
        handler.close_connection = True
        return handler._json(413, {"error": "That request is too large, or does not say how long it is."})
    raw = handler.rfile.read(length) if length else b""

    def key() -> str:
        said = handler.headers.get("Authorization") or ""
        return said[7:].strip() if said[:7].lower() == "bearer " else ""

    def space_for(given: str) -> str:
        space = spaces.space_of(given)
        if space is None:
            raise Refused(401, "This needs a key: send it as 'Authorization: Bearer <key>'.")
        return space

    try:
        if path == "/api" and method == "GET":
            return handler._json(200, spaces.describe())
        if path == "/space/download" and method == "GET":
            name, data = spaces.open_link(asked.get("t", ""))
            return handler._bytes(data, "application/zip", {"Content-Disposition": f'attachment; filename="{name}"'})
        if path.startswith("/api/") and method == "POST":
            space = space_for(key())
            try:
                ask = json.loads(raw or b"{}")
            except ValueError:
                raise Refused(400, "That is not JSON.") from None
            return handler._json(200, spaces.call(space, path[5:], ask))
        if path == "/mcp" or path.startswith("/mcp/"):
            space = space_for(path[5:] or key())  # the key in the address, for a client that can send no header
            status, back, data = spaces.forward(method, handler.headers, raw, space)
            handler.send_response(status)
            for name, value in back.items():
                handler.send_header(name, value)
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
            return None
        raise Refused(404, "Not found.")
    except Refused as no:
        if no.status == 401:
            handler.send_response(401)
            data = json.dumps({"error": no.reason}).encode()
            handler.send_header("WWW-Authenticate", 'Bearer realm="gladiator"')
            handler.send_header("Content-Type", "application/json; charset=utf-8")
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
            return None
        return handler._json(no.status, {"error": no.reason})
    except (OSError, http.client.HTTPException) as e:
        runner.say(f"  [serve] the assistant connection failed: {type(e).__name__}")
        return handler._json(502, {"error": "The assistant connection is not answering. Try again in a minute."})
