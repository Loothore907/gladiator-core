"""The race runner (engine/serve.py): what it offers, what it refuses, and a whole race over HTTP."""

import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from engine import record, serve

SIX = str(Path(__file__).resolve().parent.parent / "suites" / "six_case_mvp.yaml")
MODELS = ["scripted:golden", "scripted:silent", "scripted:invalid"]


def make(folder, total=1.0, per_code=3, codes=None, max_usd=0.2):
    ledger = serve.Ledger(Path(folder) / "ledger.json", total, per_code)
    return serve.Races(SIX, MODELS, ledger, max_usd, codes, {"https://page.example"}, runs_dir=Path(folder) / "runs")


class RulesTests(unittest.TestCase):
    def test_only_offered_values_reach_the_engine(self):
        with TemporaryDirectory() as tmp:
            races = make(tmp, codes={"judge-1"})
            task = next(iter(races.tasks))
            good = {"task": task, "a": MODELS[0], "b": MODELS[1], "code": "judge-1"}
            self.assertEqual(races.check(good)["difficulty"], "standard")
            for change, status in [
                ({"code": "nope"}, 403),
                ({"task": "../../etc/passwd"}, 400),
                ({"a": "openai:gpt-6.2-astra"}, 400),  # priced, but not on this server's list
                ({"b": MODELS[0]}, 400),  # the same model twice
                ({"difficulty": "marathon"}, 400),
                ({"profile": "whatever"}, 400),
            ]:
                with self.assertRaises(serve.Refused) as refused:
                    races.check({**good, **change})
                self.assertEqual(refused.exception.status, status, change)

    def test_a_race_must_fit_in_what_is_left_and_a_code_has_a_limit(self):
        with TemporaryDirectory() as tmp:
            ledger = serve.Ledger(Path(tmp) / "l.json", total_usd=0.5, runs_per_code=2)
            ledger.hold(0.2, "a")
            ledger.settle(0.2, 0.05)  # the race cost less than its cap: the rest comes back
            self.assertAlmostEqual(ledger.left(), 0.45)
            ledger.hold(0.2, "a")
            with self.assertRaises(serve.Refused) as third:  # this code's third race
                ledger.hold(0.2, "a")
            self.assertEqual(third.exception.status, 429)
            with self.assertRaises(serve.Refused) as over:  # 0.25 is free, 0.3 does not fit
                ledger.hold(0.3, "b")
            self.assertEqual(over.exception.status, 402)
            # the total survives a restart; a hold from a run that died with the server does not
            again = serve.Ledger(Path(tmp) / "l.json", total_usd=0.5, runs_per_code=2)
            self.assertAlmostEqual(again.left(), 0.45)

    def test_each_provider_has_its_own_budget_and_a_spent_one_takes_its_models_off_the_list(self):
        with TemporaryDirectory() as tmp:
            # "scripted" stands in for a provider here; 0.3 covers one race at a 0.2 cap, not two
            ledger = serve.Ledger(Path(tmp) / "l.json", None, 5, by_provider={"scripted": 0.3})
            races = serve.Races(SIX, MODELS, ledger, 0.2, {"judge-1"}, set(), runs_dir=Path(tmp) / "runs")
            self.assertEqual(races.options()["models"], MODELS)
            self.assertEqual(races.options()["budgets"], [{"provider": "scripted", "limit_usd": 0.3, "left_usd": 0.3}])
            ledger.hold(0.2, "x", ("scripted", "scripted"))  # two models of one provider: one hold, not two
            self.assertAlmostEqual(ledger.left_for("scripted"), 0.1)
            with self.assertRaises(serve.Refused) as full:
                ledger.hold(0.2, "y", ("scripted",))
            self.assertEqual(full.exception.status, 402)
            ledger.settle(0.2, 0.15, ("scripted", "scripted"), {"scripted": 0.15})
            self.assertAlmostEqual(ledger.left_for("scripted"), 0.15)
            # what is left cannot cover one more race: every model of that provider leaves the list
            options = races.options()
            self.assertEqual((options["models"], options["models_out"]), ([], MODELS))
            task = next(iter(races.tasks))
            with self.assertRaises(serve.Refused) as out:
                races.check({"task": task, "a": MODELS[0], "b": MODELS[1], "code": "judge-1"})
            self.assertEqual(out.exception.status, 402)
            # a provider with no budget has nothing to spend when budgets are the only limits
            self.assertEqual(ledger.left_for("openai"), 0.0)
            # a match that died is counted in full, split over its providers
            two = serve.Ledger(Path(tmp) / "m.json", None, 5, by_provider={"openai": 1.0, "google": 1.0})
            two.hold(0.2, "x", ("openai", "google"))
            two.settle(0.2, 0.2, ("openai", "google"), None)
            self.assertAlmostEqual(two.left_for("openai"), 0.9)
            self.assertAlmostEqual(two.left_for("google"), 0.9)
            with self.assertRaises(SystemExit):
                serve.Ledger(Path(tmp) / "n.json", None, 5)  # no limit of any kind

    def test_an_invite_is_exchanged_for_a_session_and_nothing_else_gets_in(self):
        with TemporaryDirectory() as tmp:
            ledger = serve.Ledger(Path(tmp) / "l.json", 1.0, 3, limits={serve.who_of("owner"): 7})
            races = serve.Races(SIX, MODELS, ledger, 0.2, {"judge-1", "owner"}, set(), runs_dir=Path(tmp) / "runs")
            session = races.sign_in({"invite": "judge-1"})
            self.assertEqual(session["races_left"], 3)
            self.assertEqual(races.sign_in({"invite": "owner"})["races_left"], 7)  # an invite with its own limit
            task = next(iter(races.tasks))
            ask = {"task": task, "a": MODELS[0], "b": MODELS[1]}
            # the session alone is enough for a race, and it is the same sign-in as the invite
            self.assertEqual(races.check({**ask, "token": session["token"]})["who"], serve.who_of("judge-1"))
            self.assertEqual(races.sign_in({"token": session["token"]})["races_left"], 3)  # renewing it
            packed, signature = session["token"].split(".")
            forged = packed + "." + ("0" if signature[0] != "0" else "1") + signature[1:]
            for bad in ({}, {"token": forged}, {"token": "x.y"}, {"token": "nonsense"}, {"invite": "nope"}):
                with self.assertRaises(serve.Refused) as refused:
                    races.check({**ask, **bad})
                self.assertEqual(refused.exception.status, 403, bad)
            # it expires, and it dies with its invite
            late = session["expires_unix"] + 1
            self.assertIsNone(races.who_holds(session["token"], now=late))
            fewer = serve.Races(SIX, MODELS, ledger, 0.2, {"owner"}, set(), runs_dir=Path(tmp) / "runs")
            self.assertIsNone(fewer.who_holds(session["token"]))
            # neither the invite nor the token is ever written to the ledger file
            ledger.hold(0.2, serve.who_of("judge-1"))
            written = (Path(tmp) / "l.json").read_text()
            self.assertNotIn("judge-1", written)
            self.assertNotIn(signature, written)

    def test_it_will_not_start_if_it_cannot_record_what_it_spends(self):
        with TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "a-file"
            blocker.write_text("not a folder")
            with self.assertRaises(SystemExit) as stopped:
                serve.Ledger(blocker / "ledger.json", total_usd=1.0, runs_per_code=1)
            self.assertIn("spending ledger", str(stopped.exception))

    def test_without_codes_it_listens_on_this_machine_only(self):
        with TemporaryDirectory() as tmp, self.assertRaises(SystemExit):
            serve.serve(make(tmp), "0.0.0.0", 0)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.races = make(self.tmp.name, codes={"judge-1"})
        self.server = serve.serve(self.races, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def post(self, ask):
        request = urllib.request.Request(
            f"{self.url}/race",
            json.dumps(ask).encode(),
            {"Content-Type": "application/json", "Origin": "https://page.example"},
        )
        return urllib.request.urlopen(request, timeout=60)

    def test_options_and_health(self):
        options = json.load(urllib.request.urlopen(f"{self.url}/options"))
        self.assertEqual(options["models"], MODELS)
        self.assertTrue(options["needs_code"] and options["tasks"])
        self.assertEqual(json.load(urllib.request.urlopen(f"{self.url}/health"))["busy"], False)

    def test_a_bad_request_is_a_plain_refusal_and_costs_nothing(self):
        with self.assertRaises(urllib.error.HTTPError) as refused:
            self.post({"task": "nope", "a": MODELS[0], "b": MODELS[1], "code": "judge-1"})
        self.assertEqual(refused.exception.code, 400)
        self.assertIn("Unknown task", json.load(refused.exception)["error"])
        self.assertEqual(self.races.ledger.state["spent_usd"], 0.0)
        self.assertEqual(self.races.ledger.state["by_code"], {})

    def test_a_race_streams_every_step_then_hands_over_a_sealed_record(self):
        task = next(iter(self.races.tasks))
        with mock.patch("builtins.print"):
            reply = self.post({"task": task, "a": MODELS[0], "b": MODELS[1], "code": "judge-1"})
            self.assertEqual(reply.headers["Content-Type"], "text/event-stream; charset=utf-8")
            self.assertEqual(reply.headers["Access-Control-Allow-Origin"], "https://page.example")
            messages = []
            for block in reply.read().decode().split("\n\n"):
                lines = dict(
                    line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(":")
                )
                if "event" in lines:
                    messages.append((lines["event"], json.loads(lines["data"])))
        kinds = [k for k, _ in messages]
        self.assertEqual((kinds[0], kinds[-2], kinds[-1]), ("match_start", "match_end", "record"))
        self.assertEqual(kinds.count("attempt_start"), 2)  # one scenario, both models
        final = messages[-1][1]
        body = final["record"]["body"]
        # the record is sealed, tied to its private half, marked live, and holds exactly the steps that were streamed
        self.assertEqual(record.sha256(body), final["record"]["sha256"])
        self.assertEqual(record.sha256(final["operator"]), body["operator_sha256"])
        self.assertTrue(body["live"] and body["settings"]["repeats"] == 1)
        self.assertEqual(body["suite"]["order"], [task])
        streamed = sum(k == "step" for k in kinds)
        self.assertEqual(streamed, sum(len(r["turns"]) for c in body["contestants"].values() for r in c["results"]))
        # it was filed, counted against the code, and the server is free again
        self.assertTrue((Path(self.tmp.name) / "runs" / body["run_id"] / "record.json").exists())
        self.assertEqual(self.races.ledger.state["by_code"], {serve.who_of("judge-1"): 1})
        # its cost was charged to the provider of the two models, and nothing is still held
        charged = self.races.ledger.state["providers"]["scripted"]
        self.assertEqual((charged["spent_usd"], charged["held_usd"]), (body["budget"]["spent_usd"], 0.0))
        self.assertEqual(self.races.ledger.state["races"], 1)
        self.assertFalse(self.races.busy.locked())
        # the race is listed with its public facts, and a restarted server reads the same list back from disk
        listed = json.load(urllib.request.urlopen(f"{self.url}/races"))["races"]
        self.assertEqual(len(listed), 1)
        one = listed[0]
        self.assertEqual((one["run_id"], one["seal"], one["task"]), (body["run_id"], final["record"]["sha256"], task))
        self.assertEqual((one["a"], one["b"]), (MODELS[0], MODELS[1]))
        self.assertEqual(one["winner"], {"A": MODELS[0], "B": MODELS[1]}.get(body["verdict"]["winner"]))
        self.assertEqual(one["a_passed"], body["contestants"]["A"]["results"][0]["passed"])
        self.assertNotIn("operator", json.dumps(one))
        again = make(self.tmp.name, codes={"judge-1"})
        self.assertEqual(again.races_run()["races"], listed)

    def test_signing_in_over_http_then_racing_with_the_session_only(self):
        def signin(ask):
            request = urllib.request.Request(
                f"{self.url}/signin", json.dumps(ask).encode(), {"Content-Type": "application/json"}
            )
            return urllib.request.urlopen(request, timeout=10)

        with self.assertRaises(urllib.error.HTTPError) as refused:
            signin({"invite": "not-an-invite"})
        self.assertEqual(refused.exception.code, 403)
        session = json.load(signin({"invite": "judge-1"}))
        self.assertEqual(session["races_left"], 3)
        task = next(iter(self.races.tasks))
        with mock.patch("builtins.print"):
            reply = self.post({"task": task, "a": MODELS[0], "b": MODELS[1], "token": session["token"]})
            self.assertIn("event: record", reply.read().decode())
        self.assertEqual(json.load(signin({"token": session["token"]}))["races_left"], 2)

    def test_one_race_at_a_time(self):
        task = next(iter(self.races.tasks))
        self.races.busy.acquire()
        try:
            with self.assertRaises(urllib.error.HTTPError) as busy:
                self.post({"task": task, "a": MODELS[0], "b": MODELS[1], "code": "judge-1"})
            self.assertEqual(busy.exception.code, 409)
            self.assertEqual(self.races.ledger.state["by_code"], {})  # a refused race uses nothing up
        finally:
            self.races.busy.release()


if __name__ == "__main__":
    unittest.main()
