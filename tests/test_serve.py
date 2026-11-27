"""The race runner (engine/serve.py): what it offers, what it refuses, and a whole race over HTTP."""

import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import providers
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
            # The total survives a restart, and so does the hold of the race that was running when the server
            # stopped: nobody knows what it cost, so its whole cap is counted, as for a match that dies.
            again = serve.Ledger(Path(tmp) / "l.json", total_usd=0.5, runs_per_code=2)
            self.assertAlmostEqual(again.left(), 0.25)
            self.assertEqual(again.recovered, (1, 0.2))
            self.assertEqual((again.state["held_usd"], again.state["open"], again.state["races"]), (0.0, [], 2))
            # and it is counted once: the next start finds nothing in flight
            third_start = serve.Ledger(Path(tmp) / "l.json", total_usd=0.5, runs_per_code=2)
            self.assertAlmostEqual(third_start.left(), 0.25)
            self.assertEqual(third_start.recovered, (0, 0.0))

    def test_a_stop_in_the_middle_of_a_race_never_hands_money_back_to_a_budget(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "l.json"
            budgets = {"openai": 1.0, "google": 1.0, "anthropic": 1.0}
            ledger = serve.Ledger(path, None, 5, by_provider=budgets)
            ledger.hold(0.4, "judges", ("openai", "google"))  # a race between two providers, in flight
            ledger.hold_small(0.01, "anthropic")  # and a model reading a description
            self.assertEqual([h["kind"] for h in ledger.state["open"]], ["race", "read"])
            self.assertAlmostEqual(ledger.left_for("openai"), 0.6)
            # the process stops here: nothing is settled. The next start counts both holds in full, the race's
            # split over its two providers exactly as a match that died in a living server is.
            again = serve.Ledger(path, None, 5, by_provider=budgets)
            self.assertEqual(again.recovered, (2, 0.41))
            self.assertAlmostEqual(again.left_for("openai"), 0.8)
            self.assertAlmostEqual(again.left_for("google"), 0.8)
            self.assertAlmostEqual(again.left_for("anthropic"), 0.99)
            self.assertAlmostEqual(again.state["spent_usd"], 0.41)
            self.assertEqual((again.state["held_usd"], again.state["open"]), (0.0, []))
            # a race that ends normally leaves nothing open, so a restart after it counts nothing
            again.hold(0.4, "judges", ("openai", "google"))
            again.settle(0.4, 0.03, ("openai", "google"), {"openai": 0.01, "google": 0.02})
            self.assertEqual(again.state["open"], [])
            after = serve.Ledger(path, None, 5, by_provider=budgets)
            self.assertEqual(after.recovered, (0, 0.0))
            self.assertAlmostEqual(after.left_for("google"), 0.78)
            # a ledger written before holds were listed: what it still holds is counted the same way
            old = json.loads(path.read_text())
            old.pop("open")
            old["held_usd"] = 0.4
            old["providers"]["openai"]["held_usd"] = 0.4
            path.write_text(json.dumps(old))
            before = after.left_for("openai")
            from_old = serve.Ledger(path, None, 5, by_provider=budgets)
            self.assertEqual(from_old.recovered, (1, 0.4))
            self.assertAlmostEqual(from_old.left_for("openai"), before - 0.4)

    def test_the_ledger_is_never_left_half_written_and_an_unreadable_one_stops_the_start(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "l.json"
            ledger = serve.Ledger(path, total_usd=1.0, runs_per_code=5)
            ledger.hold(0.2, "a")
            ledger.settle(0.2, 0.05)
            whole = path.read_text()
            # the write that is cut off: the new text never replaces the old file
            with mock.patch("os.replace", side_effect=OSError("the machine stopped")), self.assertRaises(OSError):
                ledger.hold(0.2, "a")
            self.assertEqual(path.read_text(), whole)
            self.assertAlmostEqual(serve.Ledger(path, total_usd=1.0, runs_per_code=5).left(), 0.95)
            # a file that cannot be read is not taken for an empty one: that would hand every limit back
            path.write_text(whole[: len(whole) // 2])
            with self.assertRaises(SystemExit) as stopped:
                serve.Ledger(path, total_usd=1.0, runs_per_code=5)
            self.assertIn("cannot be read", str(stopped.exception))

    def test_a_price_list_that_does_not_answer_leaves_its_models_out_and_the_rest_are_offered(self):
        from providers.base import PriceUnavailable

        listed = {"up": False}

        def price(provider, model, today=None):
            if provider == "openrouter" and not listed["up"]:
                raise PriceUnavailable("OpenRouter's model list could not be read (URLError).")
            if model == "no-such-model":
                raise ValueError("No price for scripted:no-such-model.")
            return {"input": 1.0, "output": 1.0}

        with TemporaryDirectory() as tmp, mock.patch.object(serve, "price_for", price), mock.patch("builtins.print"):
            ledger = serve.Ledger(Path(tmp) / "l.json", 1.0, 3)
            wanted = [MODELS[0], "openrouter:some/model", MODELS[1]]
            with mock.patch.object(serve.Races, "_keep_pricing"):  # asked again by hand below
                races = serve.Races(SIX, wanted, ledger, 0.2, {"judge-1"}, set(), runs_dir=Path(tmp) / "runs")
            options = races.options()
            self.assertEqual(options["models"], [MODELS[0], MODELS[1]])
            self.assertEqual(options["models_unpriced"], ["openrouter:some/model"])
            task = next(iter(races.tasks))
            with self.assertRaises(serve.Refused):  # a model without a price cannot be raced
                races.check({"task": task, "a": MODELS[0], "b": "openrouter:some/model", "code": "judge-1"})
            # the list answers again: the model is offered, in the place it was named in
            listed["up"] = True
            with mock.patch("providers.openrouter_api.refresh_catalog"), mock.patch.object(serve.time, "sleep"):
                races._keep_pricing()
            self.assertEqual(races.options()["models"], wanted)
            self.assertEqual(races.options()["models_unpriced"], [])
            # a model that can never be priced is a mistake in how the server was started: it still refuses
            with self.assertRaises(ValueError):
                serve.Races(SIX, [*MODELS, "scripted:no-such-model"], ledger, 0.2, None, set())

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

    def test_an_invite_can_carry_its_own_number_of_races_or_its_own_budget_and_days(self):
        day = 86400
        dec10 = 1796860800  # 2026-12-10 00:00 UTC
        read = serve.read_invites("plain, counted:40 ,judges:usd=40:from=2026-12-10:until=2026-12-17,,both:usd=5:12")
        self.assertEqual(read["plain"], {"races": None, "usd": None, "from": None, "until": None})
        self.assertEqual(read["counted"]["races"], 40)
        # `until` is the first moment after its day: the invite works through the whole of 17 December
        self.assertEqual(read["judges"], {"races": None, "usd": 40.0, "from": dec10, "until": dec10 + 8 * day})
        self.assertEqual((read["both"]["usd"], read["both"]["races"]), (5.0, 12))
        self.assertEqual(serve.day_of(read["judges"]["from"]), "10 December 2026")
        self.assertEqual(serve.day_of(read["judges"]["until"], last=True), "17 December 2026")
        self.assertEqual(serve.read_invites(""), {})
        # a mistake stops the start, and says which entry without a character of the invite
        for bad in (
            "s3cret-invite:usd=lots",
            "s3cret-invite:usd=0",
            "s3cret-invite:from=10-12-2026",
            "s3cret-invite:until=tomorrow",
            "s3cret-invite:budget=40",
            "s3cret-invite:from=2026-12-17:until=2026-12-10",
            ":usd=40",
        ):
            with self.assertRaises(SystemExit) as stopped:
                serve.read_invites("fine:3," + bad)
            self.assertIn("entry 2", str(stopped.exception), bad)
            self.assertNotIn("s3cret", str(stopped.exception), bad)

    def test_an_invite_with_a_budget_is_limited_by_money_and_not_by_a_number_of_races(self):
        with TemporaryDirectory() as tmp:
            judges, owner = serve.who_of("judges"), serve.who_of("owner")
            path = Path(tmp) / "l.json"

            def start():
                return serve.Ledger(path, 100.0, runs_per_code=2, limits={owner: 7}, code_usd={judges: 1.0})

            ledger = start()
            races = serve.Races(SIX, MODELS, ledger, 0.2, {"judges", "owner"}, set(), runs_dir=Path(tmp) / "runs")
            session = races.sign_in({"invite": "judges"})
            # five single races still fit in a dollar at a 20 cent cap; no number of races is counted against it
            self.assertEqual((session["budget_usd"], session["left_usd"], session["races_left"]), (1.0, 1.0, 5))
            self.assertNotIn("until_unix", session)
            self.assertNotIn("budget_usd", races.sign_in({"invite": "owner"}))
            self.assertEqual(races.sign_in({"invite": "owner"})["races_left"], 7)
            for _ in range(6):  # far more races than --runs-per-code allows a counted invite
                ledger.hold(0.2, judges)
                ledger.settle(0.2, 0.01)
            self.assertAlmostEqual(ledger.left_for_code(judges), 0.94)
            self.assertEqual(races.sign_in({"token": session["token"]})["races_left"], 4)
            # a race starts only if its whole cap fits in what is left of the invite's budget
            ledger.hold(0.9, judges)  # a batch in flight: 4 cents are left to set aside
            with self.assertRaises(serve.Refused) as over:
                ledger.hold(0.2, judges)
            self.assertEqual(over.exception.status, 402)
            self.assertIn("$0.04 is left of $1.00", over.exception.reason)
            # a model reading a description for that sign-in is held against the same budget
            with self.assertRaises(serve.Refused):
                ledger.hold_small(0.05, "openai", judges)
            ledger.hold_small(0.01, "openai", judges)
            ledger.settle_small(0.01, 0.002, "openai")
            self.assertAlmostEqual(ledger.left_for_code(judges), 0.038)
            self.assertEqual(races.sign_in({"token": session["token"]})["races_left"], 0)
            # the other invite is untouched by any of it, and still has its number
            ledger.hold(0.2, owner)
            ledger.settle(0.2, 0.2)
            self.assertEqual(ledger.races_left(owner), 6)
            # the server stops with the batch in flight: its whole cap stays counted against the invite
            again = start()
            self.assertAlmostEqual(again.left_for_code(judges), 0.038)
            self.assertEqual(again.state["codes"][judges]["held_usd"], 0.0)
            # an invite may have both: then whichever runs out first stops it
            both = serve.Ledger(Path(tmp) / "b.json", 100.0, 2, limits={judges: 1}, code_usd={judges: 1.0})
            both.hold(0.2, judges)
            both.settle(0.2, 0.01)
            with self.assertRaises(serve.Refused) as counted:
                both.hold(0.2, judges)
            self.assertEqual(counted.exception.status, 429)

    def test_an_invite_works_only_on_its_days_and_a_session_never_outlives_it(self):
        with TemporaryDirectory() as tmp:
            start, until = 1796860800, 1796860800 + 8 * 86400  # 10 December 2026 through 17 December, UTC
            judges = serve.who_of("judges")
            ledger = serve.Ledger(Path(tmp) / "l.json", 1.0, 3, code_usd={judges: 1.0})
            races = serve.Races(SIX, MODELS, ledger, 0.2, {"judges", "owner"}, set(), windows={judges: (start, until)})
            task = next(iter(races.tasks))
            ask = {"task": task, "a": MODELS[0], "b": MODELS[1]}
            # before its first day: refused, and told when (only the holder of the invite is ever told)
            with self.assertRaises(serve.Refused) as early:
                races.identify({"invite": "judges"}, now=start - 1)
            self.assertEqual(early.exception.status, 403)
            self.assertIn("opens on 10 December 2026", early.exception.reason)
            self.assertEqual(races.identify({"invite": "judges"}, now=start), judges)
            self.assertEqual(races.identify({"invite": "judges"}, now=until - 1), judges)
            with self.assertRaises(serve.Refused) as late:
                races.identify({"invite": "judges"}, now=until)
            self.assertIn("ended with 17 December 2026", late.exception.reason)
            # a session made on the last day ends with the invite, not thirty days later
            session = races.session_for(judges, now=until - 3600)
            self.assertEqual((session["expires_unix"], session["until_unix"]), (until, until))
            self.assertEqual(races.identify({"token": session["token"]}, now=until - 1), judges)
            with self.assertRaises(serve.Refused):
                races.identify({"token": session["token"]}, now=until + 1)
            # a session made early in the window is still refused once the window has closed
            long = races.session_for(judges, now=start)
            self.assertLess(long["expires_unix"], start + 31 * 86400)
            # an invite without days works any day, as before
            self.assertEqual(races.identify({"invite": "owner"}, now=until + 999), serve.who_of("owner"))
            with mock.patch.object(serve.time, "time", return_value=start - 5), self.assertRaises(serve.Refused):
                races.check({**ask, "invite": "judges"})
            # an invite in another alphabet is a plain refusal, like any wrong one
            for odd in ("clé-inconnue", "\u2603"):
                with self.assertRaises(serve.Refused) as refused:
                    races.identify({"invite": odd})
                self.assertEqual(refused.exception.status, 403)
                with self.assertRaises(serve.Refused):
                    races.identify({"token": odd + "." + odd})

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

    def test_a_page_on_this_machine_may_call_a_runner_with_no_invites_and_no_other_page_may(self):
        with TemporaryDirectory() as tmp:
            own = make(tmp)  # no invites: its owner's, with the owner's own keys
            for page in ("http://localhost:8000", "http://127.0.0.1:8790", "http://[::1]:3000", "http://localhost"):
                self.assertTrue(own.allows(page), page)
            self.assertTrue(own.allows("https://page.example"))  # named at the start
            for page in (
                "https://evil.example",
                "http://localhost.evil.example",
                "http://evil.example:8787",
                "http://127.0.0.1.evil.example:8000",
                "http://localhost:8000/",
                "https://localhost:8000@evil.example",
                "null",
            ):
                self.assertFalse(own.allows(page), page)
            # a hosted runner, which has invites, allows only the pages it was started with
            hosted = make(Path(tmp) / "hosted", codes={"judge-1"})
            self.assertTrue(hosted.allows("https://page.example"))
            self.assertFalse(hosted.allows("http://localhost:8000"))


class OwnKeyTests(unittest.TestCase):
    """A runner on a person's own machine, with their own key: `gladiator serve` and nothing else."""

    def test_one_key_of_any_provider_is_enough_to_race_a_pair(self):
        import gladiator
        from engine.budget import load_prices

        prices = load_prices()
        for provider, names in gladiator.PROVIDER_KEYS.items():
            mine = [m for m in gladiator.DEFAULT_RACE_MODELS if providers.parse(m)[0] == provider]
            self.assertGreaterEqual(len(mine), 2, f"a {names[0]} alone would leave nothing to race")
            if provider != "openrouter":  # its prices are read from its published list when a runner starts
                for model in mine:
                    self.assertIn(providers.parse(model)[1], prices[provider], f"{model} has no price")

    def test_with_one_key_it_starts_and_offers_that_providers_models_and_with_none_it_says_where_a_key_goes(self):
        import os
        import subprocess
        import sys

        import gladiator

        root = Path(__file__).resolve().parent.parent
        keys = [name for names in gladiator.PROVIDER_KEYS.values() for name in names]
        bare = {k: v for k, v in os.environ.items() if k not in keys and k != "GLADIATOR_RACE_CODES"}
        with TemporaryDirectory() as tmp:
            # A developer's own .env must not decide this test: every key is set here, to nothing unless the
            # test gives it a value, and a value already set is never replaced by the one in .env.
            def start(env):
                blank = dict.fromkeys(keys, "")
                return subprocess.Popen(
                    [sys.executable, str(root / "gladiator.py"), "serve", "--port", "0", "--state", tmp],
                    env={**bare, **blank, **env, "PYTHONUNBUFFERED": "1"},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )

            none = start({})
            said, _ = none.communicate(timeout=60)
            self.assertNotEqual(none.returncode, 0)
            self.assertIn(".env", said)
            for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY"):
                self.assertIn(name, said)

            for key, provider in (("ANTHROPIC_API_KEY", "anthropic"), ("OPENAI_API_KEY", "openai")):
                one = start({key: "not-a-real-key"})
                try:
                    lines = []
                    for line in one.stdout:  # the start-up message ends with the build page's line
                        lines.append(line)
                        if "build page" in line:
                            break
                    text = "".join(lines)
                    offered = [x for x in lines if x.strip().startswith("models:")]
                    self.assertTrue(offered, f"with only {key} set the runner did not start:\n{text}")
                    models = [m.strip() for m in offered[0].split(":", 1)[1].split(",")]
                    self.assertEqual(len(models), 2, text)
                    self.assertTrue(all(m.startswith(f"{provider}:") for m in models), text)
                    self.assertIn("this machine only", text)
                    self.assertNotIn("not-a-real-key", text)
                finally:
                    one.terminate()
                    one.wait(timeout=30)


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

    def test_the_viewer_of_a_race_on_generated_worlds_is_handed_its_answer_key_with_the_record(self):
        ask = {"task": next(iter(self.races.tasks)), "a": "scripted:silent", "b": "scripted:invalid"}
        request = urllib.request.Request(f"{self.url}/race", json.dumps(ask).encode(), {"Content-Type": "text/json"})
        with mock.patch("builtins.print"):
            last = urllib.request.urlopen(request, timeout=120).read().decode().strip().split("\n\n")[-1]
        self.assertTrue(last.startswith("event: record\n"))
        final = json.loads(last.split("data: ", 1)[1])
        self.assertEqual(record.sha256(final["operator"]), final["record"]["body"]["operator_sha256"])

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
        # what a race can cost and what the runner may still spend in all: a page shows both before a race
        self.assertEqual(options["cap_usd_per_race"], 0.2)
        self.assertEqual(options["total"], {"limit_usd": 1.0, "left_usd": 1.0})
        health = json.load(urllib.request.urlopen(f"{self.url}/health"))
        self.assertEqual(health["busy"], False)
        self.assertIn("engine_commit", health)  # which build is answering

    def test_a_caller_that_connects_and_then_says_nothing_is_dropped(self):
        import socket

        with mock.patch.object(self.server.RequestHandlerClass, "timeout", 0.3), mock.patch("builtins.print"):
            quiet = socket.create_connection(self.server.server_address, timeout=5)
            try:
                quiet.sendall(b"POST /signin HTTP/1.1\r\nHost: x\r\nContent-Length: 50\r\n\r\n{")  # and no more
                quiet.settimeout(5)
                self.assertEqual(quiet.recv(1024), b"")  # closed by the server, not held open
            finally:
                quiet.close()
            # and the server still answers everyone else
            self.assertTrue(json.load(urllib.request.urlopen(f"{self.url}/health"))["ok"])

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
        # these are fixed tests (the same case every time): the stream hands over the record and keeps the key
        self.assertIsNone(final["operator"])
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

    def test_a_race_started_with_a_budget_invite_is_counted_against_that_budget_at_what_it_cost(self):
        judges = serve.who_of("judges")
        self.races.codes.add("judges")
        self.races.ledger.code_usd[judges] = 0.5

        def signin(ask):
            request = urllib.request.Request(
                f"{self.url}/signin", json.dumps(ask).encode(), {"Content-Type": "application/json"}
            )
            return json.load(urllib.request.urlopen(request, timeout=10))

        before = signin({"invite": "judges"})
        self.assertEqual((before["budget_usd"], before["left_usd"], before["races_left"]), (0.5, 0.5, 2))
        task = next(iter(self.races.tasks))
        with mock.patch("builtins.print"):
            text = self.post({"task": task, "a": MODELS[0], "b": MODELS[1], "invite": "judges"}).read().decode()
        cost = json.loads(text.strip().split("\n\n")[-1].split("data: ", 1)[1])["record"]["body"]["budget"]["spent_usd"]
        after = signin({"invite": "judges"})
        self.assertAlmostEqual(after["left_usd"], 0.5 - cost)
        used = self.races.ledger.state["codes"][judges]
        self.assertEqual((used["held_usd"], used["spent_usd"]), (0.0, round(cost, 6)))
        # a batch whose cap does not fit what is left of the invite's budget is refused before anything is spent
        self.races.ledger.code_usd[judges] = round(cost + 0.1, 6)
        with self.assertRaises(urllib.error.HTTPError) as refused:
            self.post({"task": task, "a": MODELS[0], "b": MODELS[1], "invite": "judges", "runs": 4})
        self.assertEqual(refused.exception.code, 402)
        self.assertIn("invitation's budget", json.load(refused.exception)["error"])
        self.assertFalse(self.races.busy.locked())

    def test_a_page_from_elsewhere_cannot_start_a_race_on_a_runner_with_no_invites(self):
        """A runner on its owner's machine holds the owner's keys and asks for no sign-in. A browser sends a plain
        POST from any page without asking first, so the runner must refuse one that names a page it does not allow."""
        tmp = TemporaryDirectory()
        races = make(tmp.name)  # no invites
        server = serve.serve(races, "127.0.0.1", 0)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        task = next(iter(races.tasks))
        ask = json.dumps({"task": task, "a": MODELS[0], "b": MODELS[1]}).encode()

        def post(headers):
            return urllib.request.urlopen(urllib.request.Request(f"{url}/race", ask, headers), timeout=60)

        try:
            with mock.patch("builtins.print"):
                # what a foreign page can send without the browser asking the runner first: no JSON content type
                for page in ("https://evil.example", "http://evil.example:8787", "null"):
                    with self.assertRaises(urllib.error.HTTPError) as refused:
                        post({"Content-Type": "text/plain", "Origin": page})
                    self.assertEqual(refused.exception.code, 403, page)
                    self.assertNotIn("Access-Control-Allow-Origin", refused.exception.headers)
                self.assertEqual(races.ledger.state["spent_usd"], 0.0)
                self.assertEqual(races.results, [])  # nothing was run
                self.assertFalse(list((Path(tmp.name) / "runs").glob("*/record.json")))
                # a page served from this machine, and a program (which names no page), are served
                for headers in ({"Content-Type": "application/json", "Origin": "http://localhost:8000"}, {}):
                    reply = post(headers)
                    self.assertEqual(reply.status, 200)
                    self.assertIn("event: record", reply.read().decode())
                    if "Origin" in headers:
                        self.assertEqual(reply.headers["Access-Control-Allow-Origin"], "http://localhost:8000")
                self.assertEqual(len(races.results), 2)
        finally:
            server.shutdown()
            server.server_close()
            tmp.cleanup()

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
