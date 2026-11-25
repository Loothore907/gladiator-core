"""The assistant connection on a hosted runner (engine/hosted.py): who may call, what each key can reach, and the
same calls over HTTP, as plain JSON and as MCP."""

import asyncio
import http.client
import io
import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

from mcp.client import Client, streamable_http

from engine import hosted, runner, serve

SIX = str(Path(__file__).resolve().parent.parent / "suites" / "six_case_mvp.yaml")
REFUNDS = "gladiator/refund-queue@2.3.0"
CHOSEN = {"refund_cap": 250, "premium_tier": "vip"}
ACME, OTHER, INVITE = "acme-key-0123456789", "other-key-9876543210", "judge-invite-1"


def make(folder, codes=frozenset({INVITE}), **more):
    ledger = serve.Ledger(Path(folder) / "ledger.json", 1.0, 3)
    races = serve.Races(SIX, ["scripted:golden", "scripted:silent"], ledger, 0.2, set(codes) if codes else None, set())
    keys = {"Acme Ltd": ACME, "other": OTHER}
    return hosted.Spaces(races, Path(folder), keys, levels=["easy"], **more)  # one light level: checks take a moment


class WithARunner(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.spaces = make(self.tmp)

    def create(self, key=ACME, **change):
        ask = {"test": REFUNDS, "values": CHOSEN, "owner": "Acme", "name": "Returns desk", **change}
        return self.spaces.call(self.spaces.space_of(key), "create_test", ask)


class KeyTests(WithARunner):
    def test_a_key_opens_one_space_and_an_unknown_key_opens_none(self):
        space = self.spaces.space_of
        self.assertEqual((space(ACME), space(OTHER)), ("org-acme-ltd", "org-other"))
        self.assertEqual(space(INVITE), serve.who_of(INVITE))  # an invite is a key too, with a space of its own
        session = self.spaces.races.session_for(serve.who_of(INVITE))["token"]
        self.assertEqual(space(session), serve.who_of(INVITE))  # and so is a browser's sign-in, to the same space
        for unknown in ("", "nope", ACME + "x", ACME[:-1], "clé-non-ascii", None):
            self.assertIsNone(space(unknown), unknown)

    def test_on_your_own_machine_there_are_no_keys_and_one_space(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = make(tmp, codes=None)
            self.assertEqual((local.space_of(""), local.space_of("anything")), ("local", "local"))
            self.assertEqual(local.space_of(ACME), "org-acme-ltd")  # a named key still opens its own space


class SpaceTests(WithARunner):
    def test_a_created_test_is_kept_for_its_key_and_for_nobody_else(self):
        made = self.create()
        self.assertEqual((made["state"], made["test"]), ("created", "acme/returns-desk@1.0.0"))
        self.assertIn("private space", made["kept"])
        self.assertTrue((self.tmp / "spaces" / "org-acme-ltd" / made["files"]["test"] / "challenge.yaml").is_file())
        mine = self.spaces.call("org-acme-ltd", "list_tests", {})
        self.assertEqual([t["test"] for t in mine["yours"]], ["acme/returns-desk@1.0.0"])
        self.assertEqual(mine["yours"][0]["with_values"]["premium_tier"], "vip")
        self.assertNotIn("acme/returns-desk@1.0.0", [t["test"] for t in mine["tests"]])  # never part of the library
        # another key sees nothing of it, cannot ask for it, and may use the same name in its own space
        theirs = self.spaces.call("org-other", "list_tests", {})
        self.assertEqual(theirs["yours"], [])
        self.assertIn(
            "not one of your tests", self.spaces.call("org-other", "get_download_link", {"test": made["test"]})["error"]
        )
        self.assertEqual(self.create(OTHER)["state"], "created")
        self.assertEqual(self.create()["stage"], "name")  # in its own space the name is taken

    def test_the_download_link_works_for_a_while_and_cannot_be_changed(self):
        made = self.create()
        token = made["download"].split("t=", 1)[1]
        name, data = self.spaces.open_link(token)
        self.assertEqual(name, "acme-returns-desk-1.0.0.zip")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.assertIn("challenges/acme/returns-desk/1.0.0/challenge.yaml", z.namelist())
        fresh = self.spaces.call("org-acme-ltd", "get_download_link", {"test": made["test"]})["download"]
        self.assertEqual(self.spaces.open_link(fresh.split("t=", 1)[1])[0], name)
        packed, signature = token.split(".")
        moved = hosted._packed(hosted._unpacked(packed).replace("org-acme-ltd", "org-other"))  # point it elsewhere
        for bad in (token[:-2] + "00", f"{moved}.{signature}", packed, "", "a.b"):
            with self.assertRaises(serve.Refused, msg=bad) as gone:
                self.spaces.open_link(bad)
            self.assertEqual(gone.exception.status, 404)
        with self.assertRaises(serve.Refused):  # and it stops working
            self.spaces.open_link(token, now=time.time() + hosted.LINK_MINUTES * 60 + 5)

    def test_a_key_has_a_number_of_tests_it_may_create_and_a_space_a_number_it_may_hold(self):
        tight = make(tempfile.mkdtemp(dir=self.tmp), creates_per_key=1, max_kept=1)
        space = tight.space_of(ACME)
        ask = {"test": REFUNDS, "values": CHOSEN, "owner": "Acme", "name": "One"}
        self.assertEqual(tight.call(space, "create_test", ask)["state"], "created")
        full = tight.call(space, "create_test", {**ask, "name": "Two"})
        self.assertEqual((full["state"], full["stage"]), ("refused", "space"))
        roomy = make(tempfile.mkdtemp(dir=self.tmp), creates_per_key=1)
        self.assertEqual(roomy.call(space, "create_test", ask)["state"], "created")
        over = roomy.call(space, "create_test", {**ask, "name": "Two"})
        self.assertEqual((over["state"], over["stage"]), ("refused", "limit"))
        self.assertIn("its limit", over["problems"][0])
        self.assertFalse(roomy.races.busy.locked())  # a refusal leaves the runner free

    def test_the_checks_share_the_one_thing_at_a_time_rule_with_races(self):
        self.spaces.races.busy.acquire()
        try:
            busy = self.create()
            self.assertEqual((busy["state"], busy["stage"]), ("refused", "busy"))
        finally:
            self.spaces.races.busy.release()
        self.assertEqual(self.spaces.call("org-acme-ltd", "list_tests", {})["yours"], [])

    def test_what_can_be_called_is_described_from_the_tools_themselves(self):
        said = self.spaces.describe()
        self.assertEqual([c["call"] for c in said["calls"]], list(hosted.CALLS))
        create = next(c for c in said["calls"] if c["call"] == "create_test")
        self.assertEqual(set(create["values"]["required"]), {"test", "values", "owner", "name"})
        self.assertIn("Never guess a value", " ".join(said["instructions"].split()))
        with self.assertRaises(serve.Refused) as unknown:
            self.spaces.call("org-acme-ltd", "delete_everything", {})
        self.assertEqual(unknown.exception.status, 404)


class OverHttpTests(WithARunner):
    def setUp(self):
        super().setUp()
        self.printed = []
        said = mock.patch.object(runner, "say", self.printed.append)
        said.start()
        self.addCleanup(said.stop)
        self.server = serve.serve(self.spaces.races, "127.0.0.1", 0, None, self.spaces)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def api(self, call, key=ACME, **ask):
        headers = {"Content-Type": "application/json"} | ({"Authorization": f"Bearer {key}"} if key else {})
        request = urllib.request.Request(f"{self.url}/api/{call}", json.dumps(ask).encode(), headers)
        return json.load(urllib.request.urlopen(request, timeout=60))

    def test_a_program_walks_the_whole_path_with_its_key(self):
        said = json.load(urllib.request.urlopen(f"{self.url}/api", timeout=20))  # readable without a key
        self.assertIn("create_test", [c["call"] for c in said["calls"]])
        needs = self.api("read_requirements", test=REFUNDS)
        self.assertEqual(len(needs["choices"]), 6)
        self.assertTrue(self.api("check_design", test=REFUNDS, values=CHOSEN)["ok"])
        made = self.api("create_test", test=REFUNDS, values=CHOSEN, owner="Acme", name="Returns desk")
        self.assertEqual(made["state"], "created")
        self.assertTrue(made["download"].startswith(f"{self.url}/space/download?t="))  # at this runner's own address
        download = urllib.request.urlopen(made["download"], timeout=20)  # the link is the permission: no key
        self.assertEqual(download.headers["Content-Type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(download.read())) as z:
            self.assertIsNone(z.testzip())
        self.assertEqual(self.api("list_tests", key=INVITE)["yours"], [])

    def test_without_a_key_nothing_but_the_description_answers(self):
        for key in (None, "nope"):
            with self.assertRaises(urllib.error.HTTPError) as refused:
                self.api("list_tests", key=key)
            self.assertEqual(refused.exception.code, 401)
            self.assertIn("Bearer", refused.exception.headers["WWW-Authenticate"])
        with self.assertRaises(urllib.error.HTTPError) as forged:
            urllib.request.urlopen(f"{self.url}/space/download?t=made.up", timeout=20)
        self.assertEqual(forged.exception.code, 404)

    def test_an_assistant_connects_over_mcp_with_the_key_in_a_header_or_in_the_address(self):
        async def by_header(key):
            http = streamable_http.create_mcp_http_client(headers={"Authorization": f"Bearer {key}"})
            async with Client(streamable_http.streamable_http_client(f"{self.url}/mcp", http_client=http)) as client:
                tools = {t.name for t in (await client.list_tools()).tools}
                made = await client.call_tool(
                    "create_test", {"test": REFUNDS, "values": CHOSEN, "owner": "Acme", "name": "By assistant"}
                )
                mine = await client.call_tool("list_tests", {})
                return tools, client.instructions, made.structured_content, mine.structured_content

        async def by_address(key):
            async with Client(f"{self.url}/mcp/{key}") as client:
                return (await client.call_tool("list_tests", {})).structured_content

        tools, instructions, made, mine = asyncio.run(by_header(ACME))
        self.assertEqual(tools, set(hosted.CALLS))
        self.assertIn("ONE PERSON'S OWN FLOW", instructions)
        self.assertEqual((made["state"], made["test"]), ("created", "acme/by-assistant@1.0.0"))
        self.assertEqual([t["test"] for t in mine["yours"]], ["acme/by-assistant@1.0.0"])
        self.assertEqual([t["test"] for t in asyncio.run(by_address(ACME))["yours"]], ["acme/by-assistant@1.0.0"])
        self.assertEqual(asyncio.run(by_address(OTHER))["yours"], [])  # another key, another space
        for url in (f"{self.url}/mcp", f"{self.url}/mcp/not-a-key"):
            with self.assertRaises(BaseException):  # noqa: B017  the client fails to connect, whatever it raises
                asyncio.run(by_address("") if url.endswith("/mcp") else by_address("not-a-key"))

    def test_a_caller_cannot_name_its_own_space(self):
        # The space is worked out from the key. A header that claims another space, sent from outside, is dropped.
        self.api("create_test", test=REFUNDS, values=CHOSEN, owner="Acme", name="Returns desk")
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_tests", "arguments": {}}}
        )
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=30)
        connection.request(
            "POST",
            "/mcp",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {OTHER}",
                "X-Gladiator-Space": "org-acme-ltd",
                "X-Gladiator-Inner": "guess",
            },
        )
        answer = json.loads(connection.getresponse().read())
        self.assertEqual(answer["result"]["structuredContent"]["yours"], [])
        # and the inner app answers nobody who is not this runner
        inner = http.client.HTTPConnection("127.0.0.1", self.spaces.port, timeout=30)
        inner.request(
            "POST", "/mcp", body=body, headers={"Content-Type": "application/json", "X-Gladiator-Space": "org-acme-ltd"}
        )
        self.assertEqual(inner.getresponse().status, 403)

    def test_nothing_secret_reaches_the_log_even_from_a_refused_request(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=30)
        for path, body, headers in [
            ("/mcp", json.dumps({"note": "CANARY-body-1"}), {"Content-Type": "application/json"}),
            ("/mcp/CANARY-key-in-address", "{}", {"Content-Type": "application/json"}),
            ("/api/list_tests?key=CANARY-query", "{}", {"Authorization": "Bearer CANARY-bearer"}),
            ("/nowhere", "CANARY-body-2 " * 40, {}),
        ]:
            try:
                connection.request("POST", path, body=body, headers=headers)
                connection.getresponse().read()
            except (OSError, http.client.HTTPException):  # the server closed it: open another
                connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=30)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=30)
        connection.request("GET", "/health")
        self.assertEqual(connection.getresponse().status, 200)  # and the runner still answers the next caller
        self.api("list_tests")
        log = "\n".join(self.printed)
        self.assertNotIn("CANARY", log)
        self.assertNotIn(ACME, log)
        self.assertIn("POST /mcp/(key) 401", log)
        self.assertIn("POST /api/list_tests 200", log)

    def test_a_runner_without_the_connection_does_not_answer_for_it(self):
        bare = serve.serve(self.spaces.races, "127.0.0.1", 0)
        threading.Thread(target=bare.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as missing:
                urllib.request.urlopen(f"http://127.0.0.1:{bare.server_address[1]}/api", timeout=10)
            self.assertEqual(missing.exception.code, 404)
        finally:
            bare.shutdown()
            bare.server_close()


if __name__ == "__main__":
    unittest.main()
