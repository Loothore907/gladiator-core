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
        self.assertEqual(self.races.ledger.state["by_code"], {"judge-1": 1})
        self.assertEqual(self.races.ledger.state["races"], 1)
        self.assertFalse(self.races.busy.locked())

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
