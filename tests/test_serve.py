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


def make(folder, total=1.0, per_code=3, codes=None, max_usd=0.2, batch_sizes=(4,)):
    ledger = serve.Ledger(Path(folder) / "ledger.json", total, per_code)
    return serve.Races(
        SIX,
        MODELS,
        ledger,
        max_usd,
        codes,
        {"https://page.example"},
        runs_dir=Path(folder) / "runs",
        batch_sizes=batch_sizes,
        batch_usd_per_run=0.05,
        batch_parallel=3,
    )


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

    def test_after_a_race_the_import_workflow_is_started_and_a_failure_never_breaks_anything(self):
        with TemporaryDirectory() as tmp:
            races = make(tmp)
            races.after_race = "someone/results/import-live.yml"
            sent = []

            class Reply:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            def fake(request, timeout):
                sent.append(request)
                return Reply()

            with mock.patch("builtins.print"), mock.patch("urllib.request.urlopen", fake):
                with mock.patch.dict("os.environ", {"GLADIATOR_GITHUB_TOKEN": ""}):
                    self.assertFalse(races.announce_filed())  # no token: said, not raised, and nothing is sent
                self.assertEqual(sent, [])
                with mock.patch.dict("os.environ", {"GLADIATOR_GITHUB_TOKEN": "t0ken"}):
                    self.assertTrue(races.announce_filed())
            self.assertEqual(
                sent[0].full_url,
                "https://api.github.com/repos/someone/results/actions/workflows/import-live.yml/dispatches",
            )
            self.assertEqual((sent[0].get_method(), json.loads(sent[0].data)), ("POST", {"ref": "main"}))
            self.assertEqual(sent[0].get_header("Authorization"), "Bearer t0ken")
            with mock.patch("builtins.print"), mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
                with mock.patch.dict("os.environ", {"GLADIATOR_GITHUB_TOKEN": "t0ken"}):
                    self.assertFalse(races.announce_filed())

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


class AnswerKeyTests(unittest.TestCase):
    """A finished race's answer key (owner, 2026-11-27): handed out for generated worlds only, and only as sealed."""

    SMALL = str(Path(__file__).resolve().parent.parent / "suites" / "small_tier.yaml")

    def setUp(self):
        self.tmp = TemporaryDirectory()
        ledger = serve.Ledger(Path(self.tmp.name) / "ledger.json", 1.0, 3)
        players = ["scripted:silent", "scripted:invalid"]  # they play no test well, and every test all the same
        self.races = serve.Races(self.SMALL, players, ledger, 0.2, None, set(), runs_dir=Path(self.tmp.name) / "runs")
        self.server = serve.serve(self.races, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        with mock.patch("builtins.print"):
            ask = {"task": next(iter(self.races.tasks)), "a": players[0], "b": players[1]}
            self.sealed, self.operator = self.races.run(ask, lambda event: None)
        self.run_id = self.sealed["body"]["run_id"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_the_answer_key_of_a_finished_race_on_generated_worlds_can_be_fetched_and_matches_its_record(self):
        results = [r for side in self.sealed["body"]["contestants"].values() for r in side["results"]]
        self.assertEqual({r["challenge"]["instances"] for r in results}, {"generator"})
        fetched = json.load(urllib.request.urlopen(f"{self.url}/records/{self.run_id}/operator"))
        self.assertEqual(record.sha256(fetched), self.sealed["body"]["operator_sha256"])
        self.assertEqual(record.sha256(fetched), record.sha256(self.operator))
        # the public record is still served as filed, and holds none of it
        public = json.load(urllib.request.urlopen(f"{self.url}/records/{self.run_id}"))
        self.assertEqual((public["sha256"], record.sha256(public["body"])), (self.sealed["sha256"],) * 2)
        self.assertNotIn("native_record", json.dumps(public))

    def test_an_answer_key_that_is_not_the_one_sealed_is_never_handed_out(self):
        path = Path(self.tmp.name) / "runs" / self.run_id / "operator.json"
        kept = path.read_text(encoding="utf-8")
        for content in ('{"A": [], "B": []}', "{}"):
            path.write_text(content, encoding="utf-8")
            with self.assertRaises(urllib.error.HTTPError) as refused:
                urllib.request.urlopen(f"{self.url}/records/{self.run_id}/operator")
            self.assertEqual(refused.exception.code, 404)
        path.unlink()
        with self.assertRaises(serve.Refused):
            self.races.answer_key(self.run_id)
        path.write_text(kept, encoding="utf-8")
        self.assertEqual(record.sha256(self.races.answer_key(self.run_id)), self.sealed["body"]["operator_sha256"])


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
        # its sealed public record can be fetched as filed (so a results store can count it); nothing else can
        fetched = json.load(urllib.request.urlopen(f"{self.url}/records/{body['run_id']}"))
        self.assertEqual(fetched, final["record"])
        self.assertNotIn("operator", [k for k in fetched if k != "body"])
        for bad in ("nope", "..%2F..%2Fledger.json", body["run_id"] + "/operator.json", "20000101-000000-abcdef"):
            with self.assertRaises(urllib.error.HTTPError) as missing:
                urllib.request.urlopen(f"{self.url}/records/{bad}")
            self.assertEqual(missing.exception.code, 404, bad)
        # these are fixed tests (the same case every time): their answer key is on file and never handed out
        self.assertTrue((Path(self.tmp.name) / "runs" / body["run_id"] / "operator.json").exists())
        for bad in (body["run_id"], "nope", "20000101-000000-abcdef", "..%2F" + body["run_id"]):
            with self.assertRaises(urllib.error.HTTPError) as kept:
                urllib.request.urlopen(f"{self.url}/records/{bad}/operator")
            self.assertEqual(kept.exception.code, 404, bad)
        self.assertEqual(len(listed), 1)
        one = listed[0]
        self.assertEqual((one["run_id"], one["seal"], one["task"]), (body["run_id"], final["record"]["sha256"], task))
        self.assertEqual((one["a"], one["b"]), (MODELS[0], MODELS[1]))
        self.assertEqual(one["winner"], {"A": MODELS[0], "B": MODELS[1]}.get(body["verdict"]["winner"]))
        self.assertEqual(one["a_passed"], body["contestants"]["A"]["results"][0]["passed"])
        # each side's grade comes with it, so a page can write the race up: counts, and which items went wrong
        self.assertEqual(one["a_details"], body["contestants"]["A"]["results"][0]["grade_details"])
        self.assertTrue(one["a_details"])
        self.assertEqual(one["b_termination"], body["contestants"]["B"]["results"][0]["termination"])
        self.assertNotIn("operator", json.dumps(one))
        again = make(self.tmp.name, codes={"judge-1"})
        self.assertEqual(again.races_run()["races"], listed)

    def test_a_batch_plays_one_test_many_times_and_reports_how_steady_each_side_was(self):
        task = next(iter(self.races.tasks))
        ask = {"task": task, "a": MODELS[0], "b": MODELS[1], "code": "judge-1"}
        options = json.load(urllib.request.urlopen(f"{self.url}/options"))
        self.assertEqual(options["batches"], [{"runs": 4, "cap_usd": 0.2}])
        # only the sizes on offer; one model against itself needs a batch (one run cannot show steadiness)
        for bad in ({"runs": 7}, {"runs": "4"}, {"b": MODELS[0]}):
            with self.assertRaises(urllib.error.HTTPError) as refused:
                self.post({**ask, **bad})
            self.assertEqual(refused.exception.code, 400, bad)
        with mock.patch("builtins.print"):
            text = self.post({**ask, "runs": 4, "early_stop": False}).read().decode()
            same = self.post({**ask, "b": MODELS[0], "runs": 4}).read().decode()

        def events(stream):
            out = []
            for block in stream.split("\n\n"):
                lines = dict(
                    line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(":")
                )
                if "event" in lines:
                    out.append((lines["event"], json.loads(lines["data"])))
            return out

        messages = events(text)
        kinds = [k for k, _ in messages]
        # every attempt is announced as it ends; steps come bare (a viewer sees movement, not the calls), and the
        # whole record is not sent
        self.assertEqual((kinds.count("attempt_end"), kinds.count("record")), (8, 0))
        steps = [e for k, e in messages if k == "step"]
        self.assertTrue(steps)
        self.assertTrue(all(set(e) == {"event", "side", "attempt", "turn", "tool_name"} for e in steps))
        self.assertEqual(sorted({e["attempt"] for k, e in messages if k == "attempt_end"}), [1, 2, 3, 4])
        batch = messages[-1][1]["batch"]
        self.assertEqual(kinds[-1], "batch")
        self.assertEqual((batch["runs"], batch["same_model"], batch["a"], batch["b"]), (4, False, MODELS[0], MODELS[1]))
        self.assertEqual((batch["a_attempts"], batch["b_attempts"], len(batch["scenarios"])), (4, 4, 4))
        self.assertEqual(batch["pairs"], batch["a_wins"] + batch["b_wins"] + batch["ties"])
        # the golden player passes every time; the silent one never does, and its faults are counted by attempt
        self.assertEqual((batch["a_passed"], batch["b_passed"], batch["a_wins"]), (4, 0, 4))
        self.assertTrue(batch["b_faults"] or batch["b_endings"])
        self.assertTrue(all(count <= 4 for count in batch["b_faults"].values()))
        # the sealed record is on file with all eight attempts, in order, and each side's worlds line up
        sealed = json.loads((Path(self.tmp.name) / "runs" / batch["run_id"] / "record.json").read_text())
        self.assertEqual(record.sha256(sealed["body"]), batch["seal"])
        self.assertEqual(sealed["body"]["settings"]["repeats"], 4)
        for side in ("A", "B"):
            self.assertEqual([r["attempt"] for r in sealed["body"]["contestants"][side]["results"]], [1, 2, 3, 4])
        # one model on both sides: it agrees with itself on every scenario
        own = events(same)[-1][1]["batch"]
        self.assertEqual((own["same_model"], own["a_passed"], own["b_passed"], own["same_result"]), (True, 4, 4, 4))
        # the hold was the batch's cap, nothing is still held, and each batch used one of the sign-in's goes
        charged = self.races.ledger.state["providers"]["scripted"]
        self.assertEqual(charged["held_usd"], 0.0)
        self.assertEqual(self.races.ledger.state["by_code"], {serve.who_of("judge-1"): 2})
        # batches are listed apart from single races, and a restarted server reads them back from disk
        listed = json.load(urllib.request.urlopen(f"{self.url}/races"))
        self.assertEqual(
            (len(listed["races"]), [b["run_id"] for b in listed["batches"]]), (0, [batch["run_id"], own["run_id"]])
        )
        again = make(self.tmp.name, codes={"judge-1"})
        key = lambda b: b["run_id"]  # noqa: E731  (two batches filed in the same second have no order of their own)
        self.assertEqual(sorted(again.races_run()["batches"], key=key), sorted(listed["batches"], key=key))
        # Early stop (on unless turned off): the silent player cannot pass 80% of 4 after its first failure, so no
        # further scenario starts on either side; the ones never played are recorded as not run, with the reason.
        with mock.patch("builtins.print"):
            cut = events(self.post({**ask, "runs": 4}).read().decode())
        short = cut[-1][1]["batch"]
        self.assertEqual(short["early_stop"]["model"], MODELS[1])
        self.assertEqual(
            (short["early_stop"]["side"], short["early_stop"]["runs"], short["early_stop"]["pass_rate"]), ("B", 4, 0.8)
        )
        self.assertGreaterEqual(short["b_not_run"], 1)  # three play at once here, so the fourth never starts
        self.assertLess(short["pairs"], 4)
        self.assertEqual(sum(1 for k, e in cut if k == "attempt_end"), 8)  # the unplayed ones are said aloud too
        self.assertTrue(any(k == "attempt_end" and "stopped early" in (e.get("error") or "") for k, e in cut))
        filed = json.loads((Path(self.tmp.name) / "runs" / short["run_id"] / "record.json").read_text())
        self.assertIn("stopped early", filed["body"]["contestants"]["B"]["results"][-1]["error"])
        self.assertIsNone(batch["early_stop"])  # the first batch asked for every scenario and got them
        # a server with no sizes offers no batches at all
        with self.assertRaises(serve.Refused):
            make(self.tmp.name, codes={"judge-1"}, batch_sizes=()).check({**ask, "runs": 4})

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
