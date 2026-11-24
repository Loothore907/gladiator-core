"""Offline tests. No network, no keys, no cost.

python -m unittest discover -s tests -v
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import modules
from engine import record, runner
from engine import suite as suites
from engine.budget import Budget, BudgetExhausted
from providers.base import Settings
from reports import markdown
from scorers import components

TEN = str(ROOT / "suites" / "ten_case.yaml")
SIX = str(ROOT / "suites" / "six_case_mvp.yaml")


def quiet(fn, *a, **k):
    with mock.patch("builtins.print"):
        return fn(*a, **k)


class SimulatorTests(unittest.TestCase):
    def test_golden_actions_pass_every_case(self):
        golden = json.loads((ROOT / "tests" / "fixtures" / "golden_actions.json").read_text(encoding="utf-8"))
        self.assertEqual(len(golden), 10)
        for case, actions in golden.items():
            m = modules.load("v1_case", {"case": case})
            m.start()
            for a in actions:
                m.step(a)
            g = m.grade()
            self.assertTrue(g.passed and g.task_correct and g.workflow_correct in (True, None), case)

    def test_public_packet_has_no_private_files(self):
        for case in json.loads((ROOT / "simulators" / "PINS.json").read_text(encoding="utf-8")):
            text = json.dumps(modules.load("v1_case", {"case": case}).start())
            self.assertNotIn("PRIVATE", text, case)

    def test_edited_simulator_is_refused(self):
        from modules import v1_case

        v1_case._kernels.pop("transaction_state_v01", None)
        real = Path.read_bytes
        with mock.patch.object(Path, "read_bytes", lambda p: real(p) + b"#" if p.name == "case.py" else real(p)):
            with self.assertRaises(RuntimeError):
                v1_case.kernel("transaction_state_v01")


class BilledCostTests(unittest.TestCase):
    """The cost of record is what the provider billed, when it says; the list-price sum is only an estimate."""

    PRICE = {"input": 0.15, "cached_input": 0.003, "output": 0.6}

    def test_a_billed_cost_wins_over_the_estimate_and_both_are_kept(self):
        from engine.budget import call_cost
        from providers.base import Usage

        estimate = (50_000 * 0.15 + 950_000 * 0.003 + 10_000 * 0.6) / 1_000_000
        quiet_provider = Usage(input_tokens=1_000_000, cached_input_tokens=950_000, output_tokens=10_000)
        self.assertEqual(call_cost(self.PRICE, quiet_provider), (estimate, estimate))
        # the host did not give the cache discount the price list promises: the bill is the truth
        billed = Usage(input_tokens=1_000_000, cached_input_tokens=950_000, output_tokens=10_000, billed_usd=0.156)
        self.assertEqual(call_cost(self.PRICE, billed), (0.156, estimate))
        self.assertEqual(call_cost(self.PRICE, Usage(billed_usd=0.0)), (0.0, 0.0))  # a free call is still a bill

    def test_openrouter_usage_carries_the_charge_it_reports(self):
        from types import SimpleNamespace as NS

        from providers.openrouter_api import usage_from

        details = NS(cached_tokens=900)
        known = usage_from(NS(prompt_tokens=1000, completion_tokens=50, prompt_tokens_details=details, cost=0.00042))
        self.assertEqual((known.input_tokens, known.cached_input_tokens, known.output_tokens), (1000, 900, 50))
        self.assertEqual(known.billed_usd, 0.00042)
        # an SDK that does not know the field keeps it among the extras
        extra = usage_from(
            NS(prompt_tokens=10, completion_tokens=1, prompt_tokens_details=None, model_extra={"cost": 0.5})
        )
        self.assertEqual(extra.billed_usd, 0.5)
        # no charge reported: the estimate will stand, and nothing is invented
        silent = usage_from(NS(prompt_tokens=10, completion_tokens=1, prompt_tokens_details=None))
        self.assertIsNone(silent.billed_usd)
        self.assertIsNone(
            usage_from(NS(prompt_tokens=1, completion_tokens=1, prompt_tokens_details=None, cost="n/a")).billed_usd
        )
        self.assertEqual(usage_from(None).input_tokens, 0)

    def test_a_match_record_keeps_both_figures_for_every_call(self):
        body, _ = quiet(runner.run_match, suites.load(SIX), "scripted:golden", "scripted:invalid", 1.0)
        turns = [t for c in body["contestants"].values() for r in c["results"] for t in r["turns"]]
        self.assertTrue(turns)
        for t in turns:
            self.assertEqual(t["cost_usd"], t["list_cost_usd"])  # the scripted players report no bill
            self.assertIsNone(t["usage"]["billed_usd"])


class MatchTests(unittest.TestCase):
    def test_golden_beats_invalid(self):
        body, operator = quiet(runner.run_match, suites.load(TEN), "scripted:golden", "scripted:invalid", 1.0)
        v = components.verdict(body)
        self.assertEqual(v["winner"], "A")
        self.assertEqual(v["totals"]["A"]["passed"], 10)
        self.assertEqual(v["totals"]["B"]["passed"], 0)
        self.assertEqual(len(operator["A"]), 10)

    def test_a_listener_hears_every_step_as_it_happens_and_cannot_change_the_match(self):
        events = []
        body, operator = quiet(
            runner.run_match,
            suites.load(SIX),
            "scripted:golden",
            "scripted:silent",
            1.0,
            None,
            2,
            on_event=events.append,
        )
        kinds = [e["event"] for e in events]
        self.assertEqual((kinds[0], kinds[-1]), ("match_start", "match_end"))
        self.assertEqual(events[0]["models"], {"A": "scripted:golden", "B": "scripted:silent"})
        for side in ("A", "B"):
            results = body["contestants"][side]["results"]
            mine = [e for e in events if e.get("side") == side]
            # one start and one end per attempt, and one step per recorded turn, in the order they were played
            self.assertEqual(sum(e["event"] == "attempt_start" for e in mine), len(results))
            ends = [e for e in mine if e["event"] == "attempt_end"]
            self.assertEqual(
                [(e["node"], e["attempt"], e["passed"]) for e in ends],
                [(r["node"], r["attempt"], bool(r["passed"])) for r in results],
            )
            steps = [(e["node"], e["attempt"], e["turn"], e["tool_name"]) for e in mine if e["event"] == "step"]
            turns = [(r["node"], r["attempt"], t["turn"], t["tool_name"]) for r in results for t in r["turns"]]
            self.assertEqual(steps, turns)
            # the end of an attempt carries the public grade only, exactly as the record holds it
            self.assertEqual([e["grade_details"] for e in ends], [r["grade_details"] for r in results])
        self.assertTrue(any(e["event"] == "step" and e["tool_name"] is None for e in events))  # the silent model
        # an event carries a fixed set of public fields and nothing else: the answer side has no way in
        allowed = {
            "match_start": {"suite", "models", "nodes", "repeats", "cap_usd"},
            "attempt_start": {"side", "model", "node", "attempt", "name", "challenge"},
            "step": {
                "side",
                "model",
                "node",
                "attempt",
                "turn",
                "tool_name",
                "tool_args",
                "error",
                "call_s",
                "cost_usd",
                "tokens",
                "elapsed_s",
            },
            "attempt_end": {
                "side",
                "model",
                "node",
                "attempt",
                "passed",
                "termination",
                "calls",
                "cost_usd",
                "wall_s",
                "grade_details",
            },
            "match_end": {"spent_usd", "wall_s"},
        }
        for e in events:
            self.assertEqual(set(e) - {"event", "t"}, allowed[e["event"]], e["event"])
        private = {k for op in operator["A"] + operator["B"] for k in op["grade_details"]} - {
            k
            for r in body["contestants"]["A"]["results"] + body["contestants"]["B"]["results"]
            for k in r["grade_details"]
        }
        told = {k for e in events if e["event"] == "attempt_end" for k in e["grade_details"]}
        self.assertFalse(private & told)

        # a listener that fails every time changes nothing about the result
        def broken(event):
            raise RuntimeError("listener down")

        again, _ = quiet(
            runner.run_match, suites.load(SIX), "scripted:golden", "scripted:silent", 1.0, None, 2, on_event=broken
        )
        self.assertEqual(
            [r["passed"] for r in again["contestants"]["A"]["results"]],
            [r["passed"] for r in body["contestants"]["A"]["results"]],
        )

    def test_same_model_both_sides_is_a_draw(self):
        body, _ = quiet(runner.run_match, suites.load(SIX), "scripted:golden", "scripted:golden", 1.0)
        v = components.verdict(body)
        # Identical play: only the last tie-breaker (speed, to 0.1s) can separate them.
        self.assertEqual(v["totals"]["A"]["passed"], v["totals"]["B"]["passed"])
        self.assertIn(v["decided_by"], (None, "wall_s"))

    def test_model_that_never_calls_the_tool(self):
        body, _ = quiet(runner.run_match, suites.load(SIX), "scripted:silent", "scripted:golden", 1.0)
        ends = {r["termination"] for r in body["contestants"]["A"]["results"]}
        self.assertEqual(ends, {"no_action"})

    def test_repeats_play_each_case_n_times(self):
        body, operator = quiet(runner.run_match, suites.load(SIX), "scripted:golden", "scripted:invalid", 1.0, None, 3)
        v = components.verdict(body)
        self.assertEqual((v["totals"]["A"]["passed"], v["totals"]["A"]["cases"]), (18, 18))
        self.assertEqual(v["totals"]["B"]["passed"], 0)
        self.assertEqual([r["attempt"] for r in body["contestants"]["A"]["results"][:4]], [1, 2, 3, 1])
        groups = components.by_node(body["contestants"]["A"]["results"])
        self.assertEqual((len(groups), groups[0]["passed"], groups[0]["attempts"]), (6, 3, 3))
        from reports import markdown

        report = markdown.render("t", body, v)
        self.assertIn("Attempts passed | 18/18 | 0/18", report)
        self.assertIn("| 3/3 |", report)

    def test_unequal_settings_are_recorded_and_flagged(self):
        body, _ = quiet(
            runner.run_match,
            suites.load(SIX),
            "scripted:golden",
            "scripted:invalid",
            1.0,
            None,
            1,
            {"A": {"effort": "high"}, "B": {"effort": "medium"}},
        )
        self.assertFalse(body["settings"]["equal"])
        self.assertEqual(body["settings"]["per_contestant"]["A"], {"effort": "high"})
        from reports import markdown

        self.assertIn("Settings differ between contestants", markdown.render("t", body, components.verdict(body)))
        same, _ = quiet(runner.run_match, suites.load(SIX), "scripted:golden", "scripted:invalid", 1.0)
        self.assertTrue(same["settings"]["equal"])

    def test_provider_outages_are_not_scored(self):
        body, _ = quiet(runner.run_match, suites.load(SIX), "scripted:outage", "scripted:golden", 1.0)
        v = components.verdict(body)
        self.assertEqual(v["winner"], "no contest")  # golden can't "win" against an outage
        self.assertEqual(len(v["excluded"]), 6)
        self.assertTrue(all("provider_error" in x for x in v["excluded"]))

    def test_budget_cap_stops_cleanly_and_is_never_exceeded(self):
        pricey = {"input": 1000.0, "cached_input": 1000.0, "output": 1000.0}  # $0.001 per token
        with mock.patch.object(runner, "price_for", return_value=pricey):
            body, _ = quiet(
                runner.run_match,
                suites.load(TEN),
                "scripted:golden",
                "scripted:golden",
                5.0,
                {"max_output_tokens": 100},
            )
        b = body["budget"]
        self.assertTrue(b["cap_reached"])
        self.assertLessEqual(b["spent_usd"], 5.0)
        for side in "AB":  # each side gets its own half, so both get to play
            self.assertLessEqual(b["spent_by"][side], 2.5)
            ends = [r["termination"] for r in body["contestants"][side]["results"]]
            self.assertIn("budget_stop", ends)
            self.assertEqual(ends[-1], "not_run")
        v = components.verdict(body)
        self.assertTrue(v["excluded"])  # cut-off cases don't count toward the winner

    def test_missing_price_refuses_before_spending(self):
        with self.assertRaises(ValueError):
            quiet(runner.run_match, suites.load(SIX), "openai:no-such-model", "scripted:golden", 1.0)


class PriceScheduleTests(unittest.TestCase):
    def test_dated_price_changes_and_retirement(self):
        import datetime as dt

        from engine.budget import price_for

        before, after = dt.date(2026, 11, 20), dt.date(2027, 1, 1)
        self.assertEqual(price_for("google", "gemini-3.8-flash", before)["output"], 3.75)
        self.assertEqual(price_for("google", "gemini-3.8-flash", after)["output"], 7.50)
        self.assertEqual(
            price_for("google", "gemini-3.8-flash", dt.date(2026, 12, 31))["input"], 0.75
        )  # last promo day
        self.assertEqual(price_for("google", "gemini-2.5-pro", before)["input"], 1.25)
        with self.assertRaisesRegex(ValueError, "retired"):
            price_for("google", "gemini-2.5-pro", dt.date(2026, 12, 1))
        self.assertNotIn("until", price_for("google", "gemini-4-argon", before))


class BudgetTests(unittest.TestCase):
    def test_hold_and_settle(self):
        b = Budget(1.0)
        h = b.hold(0.6)
        b.settle(h, 0.1)
        self.assertAlmostEqual(b.spent, 0.1)
        b.hold(0.8)
        with self.assertRaises(BudgetExhausted):
            b.hold(0.2)
        with self.assertRaises(BudgetExhausted):  # stays stopped
            b.hold(0.0)


class RecordTests(unittest.TestCase):
    def test_tampering_is_detected(self):
        body, operator = quiet(runner.run_match, suites.load(SIX), "scripted:golden", "scripted:invalid", 1.0)
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / "run"
            record.write(folder, body, operator, "report")
            self.assertEqual(record.verify(folder), [])
            rec = json.loads((folder / "record.json").read_text(encoding="utf-8"))
            rec["body"]["contestants"]["B"]["results"][0]["passed"] = True
            (folder / "record.json").write_text(json.dumps(rec), encoding="utf-8")
            self.assertTrue(record.verify(folder))


class PackageTests(unittest.TestCase):
    """Challenge packages are locked: configurable by suites, never editable (docs/PRODUCT.md section 4)."""

    def copy_catalog(self, edit=None):
        import shutil

        import yaml

        from engine import packages

        tmp = Path(tempfile.mkdtemp())
        shutil.copytree(packages.CHALLENGES_DIR, tmp / "challenges")
        if edit:
            path = tmp / "challenges" / "gladiator" / "transaction-state" / "challenge.yaml"
            m = yaml.safe_load(path.read_text(encoding="utf-8"))
            edit(m)
            path.write_text(yaml.safe_dump(m, sort_keys=False), encoding="utf-8")
        return mock.patch.object(packages, "CHALLENGES_DIR", tmp / "challenges")

    def test_all_official_packages_are_intact(self):
        from engine import packages

        cat = packages.catalog()
        self.assertGreaterEqual(len(cat), 11)
        for pkg in cat.values():
            self.assertTrue(pkg.intact, (pkg.ref, pkg.problems))
            if pkg.manifest["test_type"] == "v1-simulator@1":
                self.assertNotEqual(pkg.status["status"], "official")  # v1 cases saturate: never official

    def test_status_lives_outside_the_sealed_content(self):
        from engine import packages

        pkg = packages.catalog()["gladiator/transaction-state"]
        self.assertNotIn("trust", pkg.manifest)
        # Promoting a challenge edits catalog.yaml only; the content fingerprint is unaffected.
        with mock.patch.object(packages, "statuses", return_value={pkg.ref: {"status": "official"}}):
            promoted = packages.catalog()["gladiator/transaction-state"]
        self.assertEqual(promoted.status["status"], "official")
        self.assertEqual(promoted.fingerprint, pkg.fingerprint)

    def test_edited_package_is_refused(self):
        from engine import packages

        def soften(m):
            m["instructions"] = ["Take as many tries as you like."]

        with self.copy_catalog(soften):
            with self.assertRaisesRegex(packages.PackageError, "content changed"):
                packages.resolve("gladiator/transaction-state@1.0.1")

    def test_changed_simulator_file_breaks_the_fingerprint(self):
        from engine import packages

        pkg = packages.catalog()["gladiator/transaction-state"]
        real = Path.read_bytes
        with mock.patch.object(Path, "read_bytes", lambda p: real(p) + b"#" if p.name == "case.py" else real(p)):
            self.assertNotEqual(packages.fingerprint(pkg.manifest), pkg.manifest["content_sha256"])

    def test_versions_must_be_pinned_and_match(self):
        from engine import packages

        with self.assertRaisesRegex(packages.PackageError, "pin a version"):
            packages.resolve("gladiator/transaction-state")
        with self.assertRaisesRegex(packages.PackageError, "version 9.9.9"):
            packages.resolve("gladiator/transaction-state@9.9.9")

    def test_suites_cannot_edit_a_challenge(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        f.write(
            "suite: t\nnodes:\n  - id: a\n    challenge: gladiator/transaction-state@1.0.1\n"
            "    instructions: [be lenient]\n"
        )
        f.close()
        with self.assertRaisesRegex(suites.SuiteError, "configurable, not editable"):
            suites.load(f.name)

    def test_provenance_reaches_the_record(self):
        body, _ = quiet(runner.run_match, suites.load(SIX), "scripted:golden", "scripted:invalid", 1.0)
        prov = body["suite"]["challenges"]["transaction"]
        self.assertEqual(
            (prov["id"], prov["version"], prov["status"]), ("gladiator/transaction-state", "1.0.1", "community")
        )
        self.assertFalse(prov["modified"])
        self.assertEqual(body["contestants"]["A"]["results"][0]["challenge"]["id"], "gladiator/snapshot-scan-recovery")


class SuiteTests(unittest.TestCase):
    def write(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        f.write(text)
        f.close()
        return f.name

    def test_links_set_order(self):
        s = suites.load(
            self.write(
                "suite: t\nnodes:\n  - {id: a, module: v1_case}\n  - {id: b, module: v1_case}\n"
                "links:\n  - {from: b, to: a}\n"
            )
        )
        self.assertEqual(s["order"], ["b", "a"])

    def test_loop_is_rejected(self):
        with self.assertRaises(suites.SuiteError):
            suites.load(
                self.write(
                    "suite: t\nnodes:\n  - {id: a, module: v1_case}\n  - {id: b, module: v1_case}\n"
                    "links:\n  - {from: a, to: b}\n  - {from: b, to: a}\n"
                )
            )

    def test_json_suites_work_too(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"suite": "t", "nodes": [{"id": "a", "module": "v1_case"}]}, f)
        f.close()
        self.assertEqual(suites.load(f.name)["order"], ["a"])


class OutOfCreditTests(unittest.TestCase):
    def test_spotted_across_providers(self):
        from providers.base import out_of_credit

        self.assertTrue(out_of_credit(429, "insufficient_quota", "You exceeded your current quota"))  # OpenAI grant
        self.assertTrue(out_of_credit(402, None, "Payment required"))  # OpenRouter
        self.assertTrue(out_of_credit(400, None, "Your credit balance is too low"))  # Anthropic
        self.assertFalse(out_of_credit(429, "rate_limit_exceeded", "Rate limit reached"))  # temporary: retry
        self.assertFalse(out_of_credit(503, None, "high demand"))

    def test_openai_quota_stops_the_contestant(self):
        import httpx2
        import openai

        from providers.base import FatalProviderError
        from providers.openai_api import OpenAIProvider

        req = httpx2.Request("POST", "https://api.openai.com/v1/responses")
        err = openai.RateLimitError(
            "You exceeded your current quota",
            response=httpx2.Response(429, request=req),
            body={"code": "insufficient_quota", "message": "You exceeded your current quota"},
        )
        fake = mock.MagicMock()
        fake.with_options.return_value.responses.create.side_effect = err
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test"}):
            p = OpenAIProvider("gpt-test")
        p.client = fake
        with self.assertRaisesRegex(FatalProviderError, "out of credit"):
            p.open_chat("s", "start", Settings()).ask()


class CacheBillingTests(unittest.TestCase):
    """Prompt caching: reads are cheap, writes can cost more than input, and the cap's hold covers the worst case."""

    def test_cache_reads_and_writes_are_priced_separately(self):
        from engine.budget import cost_usd

        price = {"input": 4.0, "cached_input": 0.2, "cache_write": 5.0, "output": 20.0}
        # 1M input: 600k read from cache, 300k written, 100k plain; 10k output.
        self.assertAlmostEqual(cost_usd(price, 1_000_000, 600_000, 10_000, 300_000), 0.12 + 1.5 + 0.4 + 0.2)
        self.assertAlmostEqual(cost_usd({"input": 2.0, "cached_input": 0.1, "output": 10.0}, 1000, 0, 0, 1000), 0.002)

    def test_worst_case_hold_uses_the_dearest_input_rate(self):
        from engine.budget import Budget

        b = Budget(10)
        cheap = b.worst_case({"input": 4.0, "cached_input": 0.2, "output": 20.0}, 40_000, 0)
        dear = b.worst_case({"input": 4.0, "cached_input": 0.2, "cache_write": 5.0, "output": 20.0}, 40_000, 0)
        self.assertAlmostEqual(dear / cheap, 1.25)

    def test_anthropic_asks_for_caching_and_reports_cache_writes(self):
        import anthropic

        from providers.anthropic_api import AnthropicProvider

        resp = anthropic.types.Message.model_validate(
            {
                "id": "m1",
                "type": "message",
                "role": "assistant",
                "model": "m",
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "content": [{"type": "tool_use", "id": "t1", "name": "submit_action", "input": {"action_json": "{}"}}],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 700,
                    "cache_creation_input_tokens": 300,
                },
            }
        )
        fake = mock.MagicMock()
        fake.with_options.return_value.messages.create.return_value = resp
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test"}):
            p = AnthropicProvider("claude-test")
        p.client = fake
        u = p.open_chat("sys", "start", Settings(max_output_tokens=100)).ask().usage
        self.assertEqual((u.input_tokens, u.cached_input_tokens, u.cache_write_tokens), (1010, 700, 300))
        sent = fake.with_options.return_value.messages.create.call_args.kwargs
        self.assertEqual(sent["cache_control"], {"type": "ephemeral"})


class InputEstimateTests(unittest.TestCase):
    """The cap's worst-case hold uses the provider's own token count once it has one."""

    def test_first_call_counts_characters(self):
        from engine.budget import CHARS_PER_TOKEN, OVERHEAD_TOKENS, next_input_tokens

        self.assertEqual(next_input_tokens(25_000), int(25_000 / CHARS_PER_TOKEN) + OVERHEAD_TOKENS)

    def test_later_calls_use_the_reported_count(self):
        from engine.budget import next_input_tokens
        from providers.base import Usage

        # Long OpenAI-style history: lots of characters (encrypted reasoning), few billed input tokens.
        est = next_input_tokens(280_000, Usage(input_tokens=8_977, output_tokens=41), new_chars=500)
        self.assertLess(est, 10_000)
        # Never above the character estimate.
        self.assertEqual(
            next_input_tokens(1_000, Usage(input_tokens=50_000, output_tokens=0)), next_input_tokens(1_000)
        )


class ExtractActionTests(unittest.TestCase):
    def test_all_shapes_normalise_the_same_way(self):
        from providers.base import extract_action

        self.assertEqual(extract_action({"action_json": '{"op":"inspect"}'}), '{"op":"inspect"}')
        self.assertEqual(extract_action({"action_json": {"op": "inspect"}}), '{"op":"inspect"}')  # Gemini object form
        self.assertEqual(extract_action({"op": "inspect"}), '{"op":"inspect"}')


class ProviderAdapterTests(unittest.TestCase):
    """Feed each adapter fake API responses and check what it sends back."""

    def test_anthropic_threads_tool_results(self):
        import anthropic

        from providers.anthropic_api import AnthropicProvider

        def msg(i, action):
            return anthropic.types.Message.model_validate(
                {
                    "id": f"msg_{i}",
                    "type": "message",
                    "role": "assistant",
                    "model": "m",
                    "stop_reason": "tool_use",
                    "stop_sequence": None,
                    "content": [
                        {"type": "text", "text": "ok"},
                        {
                            "type": "tool_use",
                            "id": f"toolu_{i}",
                            "name": "submit_action",
                            "input": {"action_json": action},
                        },
                    ],
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                }
            )

        sent = []
        fake = mock.MagicMock()

        def create(**kw):
            sent.append(json.loads(json.dumps(kw["messages"], default=str)))
            return msg(len(sent), '{"op":"x"}')

        fake.with_options.return_value.messages.create.side_effect = create
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test"}):
            p = AnthropicProvider("claude-test")
        p.client = fake
        chat = p.open_chat("sys", "start", Settings(max_output_tokens=100, effort="low"))
        r1 = chat.ask()
        self.assertEqual(r1.action_json, '{"op":"x"}')
        self.assertEqual(r1.usage.input_tokens, 100)
        chat.answer_tool('{"obs":1}')
        last = sent[-1][-1]
        self.assertEqual(last["role"], "user")
        self.assertEqual(last["content"][0], {"type": "tool_result", "tool_use_id": "toolu_1", "content": '{"obs":1}'})
        kw = fake.with_options.return_value.messages.create.call_args.kwargs
        self.assertEqual(kw["tool_choice"]["type"], "auto")
        self.assertEqual(kw["output_config"], {"effort": "low"})

    def test_openai_threads_function_outputs(self):
        from providers.openai_api import OpenAIProvider

        def item(d):
            return SimpleNamespace(to_dict=lambda **_: dict(d))

        def response(i):
            return SimpleNamespace(
                id=f"resp_{i}",
                status="completed",
                incomplete_details=None,
                output=[
                    item({"type": "reasoning", "id": f"rs_{i}", "summary": [], "encrypted_content": "x"}),
                    item(
                        {
                            "type": "function_call",
                            "call_id": f"call_{i}",
                            "name": "submit_action",
                            "arguments": json.dumps({"action_json": '{"op":"y"}'}),
                        }
                    ),
                ],
                usage=SimpleNamespace(
                    input_tokens=50, output_tokens=10, input_tokens_details=SimpleNamespace(cached_tokens=20)
                ),
            )

        sent = []
        fake = mock.MagicMock()
        fake.with_options.return_value.responses.create.side_effect = lambda **kw: (
            sent.append(list(kw["input"])),
            response(len(sent)),
        )[1]
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test"}):
            p = OpenAIProvider("gpt-test")
        p.client = fake
        chat = p.open_chat("sys", "start", Settings(max_output_tokens=100, effort="low"))
        r = chat.ask()
        self.assertEqual((r.action_json, r.usage.cached_input_tokens), ('{"op":"y"}', 20))
        chat.answer_tool("obs")
        self.assertEqual(sent[-1][-1], {"type": "function_call_output", "call_id": "call_1", "output": "obs"})
        self.assertEqual(sent[-1][1]["type"], "reasoning")  # reasoning passed back
        kw = fake.with_options.return_value.responses.create.call_args.kwargs
        self.assertEqual(
            (kw["tool_choice"], kw["parallel_tool_calls"], kw["reasoning"]), ("required", False, {"effort": "low"})
        )

    def test_google_threads_function_responses(self):
        from google.genai import types

        from providers.google_api import GoogleProvider

        def response(i):
            return types.GenerateContentResponse.model_validate(
                {
                    "response_id": f"r{i}",
                    "candidates": [
                        {
                            "finish_reason": "STOP",
                            "content": {
                                "role": "model",
                                "parts": [
                                    {"function_call": {"name": "submit_action", "args": {"action_json": '{"op":"z"}'}}}
                                ],
                            },
                        }
                    ],
                    "usage_metadata": {
                        "prompt_token_count": 40,
                        "candidates_token_count": 5,
                        "thoughts_token_count": 7,
                    },
                }
            )

        sent = []
        fake = mock.MagicMock()
        fake.models.generate_content.side_effect = lambda **kw: (
            sent.append(list(kw["contents"])),
            response(len(sent)),
        )[1]
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "test"}):
            p = GoogleProvider("gemini-test")
        p.client = fake
        chat = p.open_chat("sys", "start", Settings(max_output_tokens=100, effort="low"))
        r = chat.ask()
        self.assertEqual((r.action_json, r.usage.output_tokens), ('{"op":"z"}', 12))
        chat.answer_tool("obs")
        last = sent[-1][-1]
        self.assertEqual(last.role, "user")
        self.assertEqual(last.parts[0].function_response.response, {"result": "obs"})
        cfg = fake.models.generate_content.call_args.kwargs["config"]
        self.assertEqual(cfg.tool_config.function_calling_config.mode, "ANY")
        self.assertIn(503, cfg.http_options.retry_options.http_status_codes)  # "high demand" gets retried

    def test_openrouter_prices_come_from_its_catalog(self):
        from engine.budget import price_for
        from providers import openrouter_api

        fake = {
            "qwen/qwen3-32b": {
                "pricing": {"prompt": "0.0000001", "completion": "0.0000003"},
                "supported_parameters": ["tools", "tool_choice"],
            },
            "no/tools": {"pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": []},
        }
        with mock.patch.object(openrouter_api, "_catalog", fake):
            p = price_for("openrouter", "qwen/qwen3-32b")
            self.assertAlmostEqual(p["input"], 0.10)
            self.assertAlmostEqual(p["output"], 0.30)
            with self.assertRaisesRegex(ValueError, "tool calling"):
                price_for("openrouter", "no/tools")
            with self.assertRaisesRegex(ValueError, "does not list"):
                price_for("openrouter", "nope/nothing")
            with self.assertRaisesRegex(ValueError, "floating alias"):
                price_for("openrouter", "~z-ai/glm-flash-latest")

    def test_openrouter_threads_tool_messages(self):
        from openai.types.chat import ChatCompletion

        from providers.openrouter_api import OpenRouterProvider

        def response(i):
            return ChatCompletion.model_validate(
                {
                    "id": f"gen-{i}",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "qwen/qwen3-32b",
                    "provider": "SomeHost",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": f"call_{i}",
                                        "type": "function",
                                        "function": {
                                            "name": "submit_action",
                                            "arguments": json.dumps({"action_json": {"op": "inspect"}}),
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 30, "completion_tokens": 8, "total_tokens": 38},
                }
            )

        sent = []
        fake = mock.MagicMock()

        def create(**kw):
            sent.append(json.loads(json.dumps(kw["messages"], default=str)))
            return response(len(sent))

        fake.with_options.return_value.chat.completions.create.side_effect = create
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "test"}):
            p = OpenRouterProvider("qwen/qwen3-32b")
        p.client = fake
        chat = p.open_chat("sys", "start", Settings(max_output_tokens=100, effort="low"))
        r = chat.ask()
        self.assertEqual((r.action_json, r.usage.input_tokens), ('{"op":"inspect"}', 30))
        self.assertIn("SomeHost", r.stop_reason)  # which host served it is recorded
        chat.answer_tool("obs")
        self.assertEqual(sent[-1][-1], {"role": "tool", "tool_call_id": "call_1", "content": "obs"})
        self.assertEqual(sent[-1][0]["role"], "system")
        kw = fake.with_options.return_value.chat.completions.create.call_args.kwargs
        self.assertNotIn("tool_choice", kw)  # many hosts reject "required"; instructions ask for the call
        self.assertNotIn("parallel_tool_calls", kw)
        self.assertEqual(kw["extra_body"]["reasoning"], {"effort": "low"})
        self.assertTrue(kw["extra_body"]["provider"]["require_parameters"])

    def test_google_effort_maps_to_level_or_budget(self):
        from providers.google_api import thinking_config

        self.assertEqual(thinking_config("gemini-3.5-flash-lite", "low").thinking_level.value, "LOW")
        self.assertIsNone(thinking_config("gemini-3.5-flash-lite", "low").thinking_budget)
        self.assertEqual(thinking_config("gemini-2.5-flash-lite", "low").thinking_budget, 1024)
        self.assertIsNone(thinking_config("gemini-2.5-flash-lite", "low").thinking_level)
        self.assertIsNone(thinking_config("gemini-3-flash", None))


if __name__ == "__main__":
    unittest.main()


class MultiToolTests(unittest.TestCase):
    """Named, per-test tools (M2c step 1): each provider passes them through and reports which was called."""

    TOOLS = None

    @classmethod
    def setUpClass(cls):
        from providers.base import ToolSpec

        obj = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
        cls.TOOLS = [
            ToolSpec("add", "Add n to the counter.", obj),
            ToolSpec("finish", "Finish the task.", {"type": "object", "properties": {}}),
        ]

    def test_openai_sends_named_tools_and_reports_the_call(self):
        from providers.openai_api import OpenAIProvider

        def item(d):
            return SimpleNamespace(to_dict=lambda **_: dict(d))

        resp = SimpleNamespace(
            id="resp_1",
            status="completed",
            incomplete_details=None,
            output=[item({"type": "function_call", "call_id": "call_1", "name": "add", "arguments": '{"n": 3}'})],
            usage=SimpleNamespace(input_tokens=5, output_tokens=2, input_tokens_details=None),
        )
        fake = mock.MagicMock()
        fake.with_options.return_value.responses.create.return_value = resp
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test"}):
            p = OpenAIProvider("gpt-test")
        p.client = fake
        r = p.open_chat("s", "start", Settings(), self.TOOLS).ask()
        self.assertEqual((r.tool_name, r.tool_args), ("add", {"n": 3}))
        sent = fake.with_options.return_value.responses.create.call_args.kwargs["tools"]
        self.assertEqual([t["name"] for t in sent], ["add", "finish"])

    def test_anthropic_sends_named_tools_and_reports_the_call(self):
        import anthropic

        from providers.anthropic_api import AnthropicProvider

        msg = anthropic.types.Message.model_validate(
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "m",
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "content": [{"type": "tool_use", "id": "toolu_1", "name": "finish", "input": {}}],
                "usage": {"input_tokens": 5, "output_tokens": 2},
            }
        )
        fake = mock.MagicMock()
        fake.with_options.return_value.messages.create.return_value = msg
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test"}):
            p = AnthropicProvider("claude-test")
        p.client = fake
        r = p.open_chat("s", "start", Settings(), self.TOOLS).ask()
        self.assertEqual((r.tool_name, r.tool_args), ("finish", {}))
        sent = fake.with_options.return_value.messages.create.call_args.kwargs["tools"]
        self.assertEqual([t["name"] for t in sent], ["add", "finish"])
        self.assertNotIn("strict", sent[0])  # strict only when a tool asks for it

    def test_google_replies_under_the_called_tools_name(self):
        from google.genai import types

        from providers.google_api import GoogleProvider

        resp = types.GenerateContentResponse.model_validate(
            {
                "candidates": [
                    {"content": {"role": "model", "parts": [{"function_call": {"name": "add", "args": {"n": 2}}}]}}
                ],
                "usage_metadata": {"prompt_token_count": 4, "candidates_token_count": 1},
            }
        )
        sent = []
        fake = mock.MagicMock()
        fake.models.generate_content.side_effect = lambda **kw: (sent.append(list(kw["contents"])), resp)[1]
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "test"}):
            p = GoogleProvider("gemini-test")
        p.client = fake
        chat = p.open_chat("s", "start", Settings(), self.TOOLS)
        self.assertEqual(chat.ask().tool_name, "add")
        chat.answer_tool("ok")
        self.assertEqual(sent[-1][-1].parts[0].function_response.name, "add")
        cfg = fake.models.generate_content.call_args.kwargs["config"]
        self.assertEqual(cfg.tool_config.function_calling_config.allowed_function_names, ["add", "finish"])

    def test_openrouter_sends_named_tools(self):
        from providers.openrouter_api import format_tools

        self.assertEqual([t["function"]["name"] for t in format_tools(self.TOOLS)], ["add", "finish"])

    def run_counter_match(self, plan):
        """A tiny two-tool test played end to end by a fake model that follows `plan`."""
        from modules.base import Grade, Module
        from providers.base import Chat, Provider, Reply, Usage

        tools = self.TOOLS

        class Counter(Module):
            max_actions = 6

            def __init__(self):
                self.total, self.done = 0, False

            def start(self):
                return {"goal": "reach 5"}

            def tools(self):
                return tools

            def act(self, tool_name, tool_args, action_json):
                if tool_name == "add":
                    self.total += tool_args["n"]
                    return {"total": self.total}
                if tool_name == "finish":
                    self.done = True
                    return {"finished": True}
                raise ValueError(f"unknown tool '{tool_name}'")

            @property
            def finished(self):
                return self.done

            def grade(self):
                ok = self.done and self.total == 5
                return Grade(passed=ok, task_correct=ok, workflow_correct=None)

        class PlanChat(Chat):
            def __init__(self):
                self.i = 0

            def approx_input_chars(self):
                return 10

            def _next(self):
                name, args = plan[self.i]
                self.i += 1
                return Reply(json.dumps(args), "", "tool_use", Usage(1, 0, 1), tool_name=name, tool_args=args)

            def ask(self):
                return self._next()

            def answer_tool(self, observation_text):
                return self._next()

        class PlanProvider(Provider):
            def open_chat(self, system, first_message, settings, tools=None):
                assert [t.name for t in tools] == ["add", "finish"]
                return PlanChat()

        node = {
            "id": "c",
            "name": "counter",
            "module": "counter",
            "params": {},
            "instructions": [],
            "settings": {},
            "provenance": {"inline": True, "status": "unreviewed", "test_type": "counter"},
        }
        suite = {"defaults": {}}
        with mock.patch("modules.load", lambda name, params, instance=None: Counter()):
            public, _ = runner.run_node(
                PlanProvider("x"), {"input": 0, "output": 0, "cached_input": 0}, Budget(1.0), suite, node, Settings()
            )
        return public

    def test_runner_plays_a_multi_tool_test(self):
        public = self.run_counter_match([("add", {"n": 2}), ("add", {"n": 3}), ("finish", {})])
        self.assertTrue(public["passed"])
        self.assertEqual([t["tool_name"] for t in public["turns"]], ["add", "add", "finish"])

    def test_unknown_tool_is_an_invalid_action(self):
        public = self.run_counter_match([("add", {"n": 2}), ("delete_everything", {})])
        self.assertEqual(public["termination"], "invalid_action")
        self.assertFalse(public["passed"])


class WorkflowTests(unittest.TestCase):
    """workflow@1: data-defined multi-tool challenges (docs/M2C_WORKFLOW_DESIGN.md)."""

    SPEC = None

    @classmethod
    def setUpClass(cls):
        import yaml

        cls.SPEC = yaml.safe_load((ROOT / "challenges/gladiator/refund-queue/workflow.yaml").read_text())

    def test_refund_queue_passes_automatic_validation(self):
        from engine.validate import validate

        self.assertEqual(validate(self.SPEC), [])

    def test_validation_record_carries_the_evidence_per_level(self):
        from engine.validate import SEEDS, validation_record

        prov = {"id": "gladiator/refund-queue", "version": "x", "content_sha256": "y"}
        rec = validation_record(self.SPEC, prov, "abc", "2026-11-21T00:00:00Z")
        self.assertTrue(rec["valid"])
        self.assertEqual(rec["kind"], "validation")
        self.assertEqual(rec["challenge"], prov)
        levels = self.SPEC.get("difficulty_levels", ["easy", "standard", "hard"])
        self.assertEqual(set(rec["levels"]), set(levels))
        for level in levels:
            ev = rec["levels"][level]
            self.assertEqual(ev["oracle_passed"], len(SEEDS))
            # A player that has to fail is checked, and a record that is valid means every one was caught.
            must = [p for p in ev["players"].values() if p["must_fail"]]
            self.assertTrue(must and all(p["ok"] for p in must))
            self.assertIn("blind_retry", ev["players"])

    def test_rule_language_refuses_anything_outside_the_safe_subset(self):
        from modules.workflow import RuleError, check_rule, evaluate

        for bad in ("__import__('os')", "order.__class__", "(lambda: 1)()", "[x for x in order]", "open('f')"):
            with self.assertRaises(RuleError, msg=bad):
                check_rule(bad)
        ctx = {"order": {"status": "shipped", "amount": 500, "paid_on": "2026-04-16"}}
        self.assertTrue(evaluate("order.status in ['delivered','shipped'] and order.amount <= 500", ctx))
        self.assertTrue(evaluate("days_since(order.paid_on) <= 60", ctx))  # exactly 60 days before TODAY
        self.assertFalse(evaluate("order.amount < 500", ctx))

    def test_same_seed_same_world_and_different_seeds_differ(self):
        from modules.workflow import Workflow

        a, b = Workflow(self.SPEC, seed=7, difficulty="hard"), Workflow(self.SPEC, seed=7, difficulty="hard")
        self.assertEqual((a.world, a.expected, a.fault_plan), (b.world, b.expected, b.fault_plan))
        self.assertNotEqual(a.world, Workflow(self.SPEC, seed=8, difficulty="hard").world)

    def test_nothing_private_reaches_the_model(self):
        from engine.validate import private_leaks
        from modules.workflow import Workflow

        m = Workflow(self.SPEC, seed=3, difficulty="hard")
        self.assertEqual(private_leaks(m), [])
        self.assertEqual([t.name for t in m.tools()][:2], ["list_refund_requests", "get_order"])

    def test_every_tool_advertises_every_argument_the_engine_reads(self):
        """A tool the model cannot address is a broken test, not a model failure. Every workflow package: each
        tool's parameter list names the arguments the engine reads for it (found 2026-11-22: `check` tools were
        advertised with no parameters, so a model that checked after a timeout was told "not found")."""
        import yaml

        from modules.workflow import Workflow

        seen = 0
        for path in sorted((ROOT / "challenges").rglob("workflow.yaml")):
            spec = yaml.safe_load(path.read_text(encoding="utf-8"))
            level = spec["difficulty_levels"][0]
            schemas = {t.name: t.parameters for t in Workflow(spec, seed=1, difficulty=level).tools()}
            for tool in spec["tools"]:
                reads = {
                    "get": [tool.get("key")],
                    "check": [tool.get("key")],
                    "find": [tool.get("by")],
                    "list": ["page"],
                    "create": [*tool.get("fields", []), tool.get("effect_key")],
                    "update": [*tool.get("fields", []), tool.get("effect_key")],
                    "finish": [],
                }[tool["op"]]
                props = schemas[tool["name"]]["properties"]
                for arg in reads:
                    self.assertIn(arg, props, f"{path.parent.name}: {tool['name']} cannot be given `{arg}`")
                    seen += 1
        self.assertGreater(seen, 100)

    def test_checking_after_a_timeout_clears_it_and_a_check_that_names_nothing_is_refused(self):
        from modules.workflow import Workflow

        m = Workflow(self.SPEC, seed=3, difficulty="hard")
        check = next(t for t in self.SPEC["tools"] if t["op"] == "check")
        issue = next(t for t in self.SPEC["tools"] if t["op"] == "create")
        key = str(m.world[self.SPEC["task"]["decide_each"]][0][self.SPEC["task"]["target_key"]])

        m.unknown_outcomes.add(key)
        self.assertEqual(m._dispatch(check, {}), {"error": "missing_fields", "fields": [check["key"]]})
        self.assertIn(key, m.unknown_outcomes)  # a check that named nothing verified nothing

        self.assertIn("found", m._dispatch(check, {check["key"]: key}))
        self.assertNotIn(key, m.unknown_outcomes)
        before = m.blind_retries
        m._dispatch(issue, {issue["effect_key"]: key, "amount": 1})
        self.assertEqual(m.blind_retries, before)  # checked first, so the retry is not blind

    def test_both_contestants_get_identical_instances(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        f.write(
            "suite: t\ndefaults: {difficulty: hard}\nnodes:\n  - id: r\n    challenge: gladiator/refund-queue@1.0.0\n"
        )
        f.close()
        body, _ = quiet(runner.run_match, suites.load(f.name), "scripted:invalid", "scripted:silent", 1.0, None, 2)
        a = [r["instance"] for r in body["contestants"]["A"]["results"]]
        b = [r["instance"] for r in body["contestants"]["B"]["results"]]
        self.assertEqual(a, b)  # same world for both, attempt by attempt
        self.assertNotEqual(a[0]["seed"], a[1]["seed"])  # fresh world per attempt
        self.assertEqual(a[0]["difficulty"], "hard")


class RefundQueueV2Tests(unittest.TestCase):
    """refund-queue@2.0.0: two lookups per item, a policy with exceptions, computed amounts, expert level."""

    @classmethod
    def setUpClass(cls):
        import yaml

        cls.SPEC = yaml.safe_load((ROOT / "challenges/gladiator/refund-queue/2.0.0/workflow.yaml").read_text())

    def test_passes_automatic_validation_including_the_naive_player(self):
        from engine.validate import play, validate
        from modules.workflow import Workflow

        self.assertEqual(validate(self.SPEC), [])
        # The rule without its exceptions never passes at hard or expert.
        for level in ("hard", "expert"):
            self.assertFalse(
                any(play(Workflow(self.SPEC, seed=s, difficulty=level), "naive").passed for s in range(1, 11))
            )

    def test_both_versions_stay_runnable_side_by_side(self):
        from engine import packages

        old, new = packages.resolve("gladiator/refund-queue@1.0.0"), packages.resolve("gladiator/refund-queue@2.0.0")
        self.assertNotEqual(old.fingerprint, new.fingerprint)
        self.assertEqual(packages.catalog()["gladiator/refund-queue"].manifest["version"], "2.3.0")  # newest
        self.assertEqual(old.status["status"], "retired")
        with self.assertRaisesRegex(packages.PackageError, "has 1.0.0, 2.0.0, 2.1.0, 2.2.0, 2.3.0"):
            packages.resolve("gladiator/refund-queue@3.0.0")

    def _one_approval(self, level="expert"):
        """A world plus one request the policy approves for an order over 500 (amount capped)."""
        from modules.workflow import Workflow

        for seed in range(1, 200):
            m = Workflow(self.SPEC, seed=seed, difficulty=level)
            for t in m.world["refund_requests"]:
                ctx = m._context(m.world, t)
                if m.expected[t["id"]] == "approve" and ctx["order"]["amount"] > 500:
                    return m, t, ctx
        self.fail("no capped approval found")

    def test_acting_without_the_customer_lookup_counts_as_not_reading(self):
        m, t, ctx = self._one_approval()
        m.act("get_order", {"id": t["order_id"]}, "{}")
        m.act("issue_refund", {"request_id": t["id"], "amount": 500}, "{}")
        self.assertEqual(m.grade().details["acted_without_reading"], 1)

    def test_refund_amount_is_capped_by_the_amount_rule(self):
        m, t, ctx = self._one_approval()
        m.act("get_order", {"id": t["order_id"]}, "{}")
        m.act("get_customer", {"id": ctx["customer"]["id"]}, "{}")
        m.act("issue_refund", {"request_id": t["id"], "amount": ctx["order"]["amount"]}, "{}")  # full, not capped
        d = m.grade().details
        self.assertEqual((d["wrong_amounts"], d["acted_without_reading"]), (1, 0))

    def test_expert_level_is_the_largest(self):
        from modules.workflow import Workflow

        sizes = [len(Workflow(self.SPEC, seed=1, difficulty=lv).expected) for lv in self.SPEC["difficulty_levels"]]
        self.assertEqual(sizes, sorted(sizes))
        self.assertEqual(sizes[-1], 40)


class AccessReviewTests(unittest.TestCase):
    """access-review@1.0.0: injections push toward the other outcome ("keep my access")."""

    @classmethod
    def setUpClass(cls):
        import yaml

        cls.SPEC = yaml.safe_load((ROOT / "challenges/gladiator/access-review/workflow.yaml").read_text())

    def test_passes_automatic_validation(self):
        from engine.validate import validate

        self.assertEqual(validate(self.SPEC), [])

    def test_injections_sit_on_accounts_that_must_be_revoked(self):
        from modules.workflow import Workflow

        m = Workflow(self.SPEC, seed=5, difficulty="expert")
        self.assertEqual(len(m.injected_targets), 5)
        self.assertTrue(all(m.expected[k] == "approve" for k in m.injected_targets))  # approve = revoke
        k = sorted(m.injected_targets)[0]
        acct = next(a for a in m.world["accounts"] if a["id"] == k)
        m.act("get_employee", {"id": acct["employee_id"]}, "{}")
        m.act("keep_access", {"account_id": k, "justification": "note says keep"}, "{}")
        self.assertEqual(m.grade().details["injections_obeyed"], 1)


class ConfidenceTests(unittest.TestCase):
    """How sure are we: paired sign test and 95% ranges (scorers/confidence.py)."""

    def test_sign_test_and_wilson_known_values(self):
        from scorers.confidence import sign_test, wilson

        self.assertAlmostEqual(sign_test(5, 0), 0.0625)  # 5-0 split between equals: 1/32 each way
        self.assertAlmostEqual(sign_test(6, 0), 0.03125)
        self.assertEqual(sign_test(0, 0), 1.0)
        lo, hi = wilson(8, 8)
        self.assertEqual(hi, 1.0)
        self.assertAlmostEqual(lo, 0.676, places=3)  # 8/8 still leaves room for ~68%

    def test_only_split_pairs_count(self):
        from scorers.confidence import paired

        def r(passed, term="session_ended"):
            return {"passed": passed, "termination": term}

        a = [r(True), r(True), r(False), r(True, "budget_stop")]
        b = [r(True), r(False), r(False), r(False)]
        s = paired(a, b, lambda x, y: "budget_stop" not in (x["termination"], y["termination"]))
        self.assertEqual((s["pairs"], s["a_only"], s["b_only"]), (3, 1, 0))

    def test_report_states_how_sure(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        f.write("suite: t\nnodes:\n  - id: c\n    challenge: gladiator/transaction-state@1.0.1\n")
        f.close()
        body, _ = quiet(runner.run_match, suites.load(f.name), "scripted:golden", "scripted:invalid", 1.0, None, 3)
        report = markdown.render("t", body, components.verdict(body))
        self.assertIn("How sure:", report)
        self.assertIn("scripted:golden won 3-0", report)
        self.assertIn("Efficiency score", report)
        self.assertIn("Pass rate (95% range)", report)


class IncidentTriageTests(unittest.TestCase):
    """incident-triage@1.0.0: several outcomes per item, duplicate groups, minute-level deadlines."""

    @classmethod
    def setUpClass(cls):
        import yaml

        cls.SPEC = yaml.safe_load((ROOT / "challenges/gladiator/incident-triage/1.0.1/workflow.yaml").read_text())

    def test_passes_automatic_validation(self):
        from engine.validate import validate

        self.assertEqual(validate(self.SPEC), [])

    def test_every_outcome_occurs_and_duplicates_merge_into_the_root(self):
        from modules.workflow import Workflow

        m = Workflow(self.SPEC, seed=2, difficulty="expert")
        self.assertEqual(set(m.expected.values()), {"merge", "close", "escalate", "route"})
        alerts = {a["id"]: a for a in m.world["alerts"]}
        for key, outcome in m.expected.items():
            if outcome == "merge":
                root = alerts[m.expected_args[key]["into"]]
                a = alerts[key]
                self.assertEqual((root["service_id"], root["signature"]), (a["service_id"], a["signature"]))
                self.assertLessEqual(root["opened_at"], a["opened_at"])
                self.assertNotEqual(m.expected[root["id"]], "merge")  # the root is never merged

    def test_wrong_outcome_and_wrong_details_are_graded_separately(self):
        from modules.workflow import Workflow

        m = Workflow(self.SPEC, seed=2, difficulty="standard")  # no planted routing faults at this level
        routes = [k for k, v in m.expected.items() if v == "route"]
        escalations = [k for k, v in m.expected.items() if v == "escalate"]
        for key in (routes[0], escalations[0]):
            m.act("get_service", {"id": next(a for a in m.world["alerts"] if a["id"] == key)["service_id"]}, "{}")
        m.act("route_alert", {"alert_id": routes[0], "team": "nobody-oncall"}, "{}")  # right outcome, wrong team
        m.act("route_alert", {"alert_id": escalations[0], "team": "x"}, "{}")  # should have paged
        d = m.grade().details
        self.assertEqual((d["wrong_args"], d["wrong"]), (1, 1))
        self.assertEqual(d["confusions"], {"escalate->route": 1})

    def test_report_flags_mistakes_both_models_share(self):
        from reports.markdown import shared_mistakes

        a = [
            {
                "name": "Incident triage",
                "attempt": 1,
                "grade_details": {"mistakes": {"ALR-1": "close->merge", "ALR-2": "route->close"}},
            }
        ]
        b = [
            {
                "name": "Incident triage",
                "attempt": 1,
                "grade_details": {"mistakes": {"ALR-1": "close->merge", "ALR-2": "route->escalate"}},
            }
        ]
        self.assertEqual(shared_mistakes(a, b), [("Incident triage", 1, "ALR-1", "close->merge")])

    def test_rule_text_settles_synthetic_duplicates_and_ties(self):
        text = self.SPEC["task"]["policy_text"]
        self.assertIn("synthetic alerts are never", text)
        self.assertIn("same minute", text)

    def test_report_names_the_confusion(self):
        from reports.markdown import why_failed

        text = why_failed({"wrong": 2, "confusions": {"escalate->route": 2}, "explicit_finish": True})
        self.assertIn("route instead of escalate x2", text)

    def test_now_is_given_only_where_times_are_used(self):
        import yaml

        from modules.workflow import Workflow

        refund = yaml.safe_load((ROOT / "challenges/gladiator/refund-queue/2.0.0/workflow.yaml").read_text())
        self.assertIn("now", Workflow(self.SPEC, seed=1, difficulty="easy").start())
        self.assertNotIn("now", Workflow(refund, seed=1, difficulty="easy").start())


class InventoryTransferTests(unittest.TestCase):
    """inventory-transfer@1.0.0: coupled decisions over shared stock, processed in a stated order."""

    @classmethod
    def setUpClass(cls):
        import yaml

        cls.SPEC = yaml.safe_load((ROOT / "challenges/gladiator/inventory-transfer/1.0.1/workflow.yaml").read_text())

    def test_passes_automatic_validation(self):
        from engine.validate import validate

        self.assertEqual(validate(self.SPEC), [])

    def test_shipping_takes_stock_and_running_short_is_refused(self):
        from modules.workflow import Workflow

        m = Workflow(self.SPEC, seed=3, difficulty="easy")  # no planted faults at this level
        order = next(o for o in m.world["orders"] if o["sku"] == "SKU-A")
        row = next(r for r in m.world["inventory"] if r["warehouse"] == "WH-NORTH" and r["sku"] == order["sku"])
        row["qty"] = order["qty"]
        self.assertTrue(m.act("ship_order", {"order_id": order["id"], "warehouse": "WH-NORTH"}, "{}").get("ok"))
        self.assertEqual(row["qty"], 0)
        other = next(o for o in m.world["orders"] if o["id"] != order["id"])
        other["sku"] = order["sku"]
        res = m.act("ship_order", {"order_id": other["id"], "warehouse": "WH-NORTH"}, "{}")
        self.assertEqual(res["error"], "insufficient_stock")

    def test_ground_truth_follows_the_stated_order(self):
        from engine.validate import play
        from modules.workflow import Workflow

        # Ignoring the processing order fails at the top levels; following it (the oracle) passes.
        for seed in range(1, 6):
            self.assertTrue(play(Workflow(self.SPEC, seed=seed, difficulty="expert"), "oracle").passed)
        self.assertLessEqual(
            sum(play(Workflow(self.SPEC, seed=s, difficulty="expert"), "unordered").passed for s in range(1, 11)), 1
        )

    def test_resource_functions_need_resources(self):
        from modules.workflow import RuleError, evaluate

        with self.assertRaises(RuleError):
            evaluate("stock('WH-NORTH', 'SKU-A') > 0", {})


class RefundReconciliationTests(unittest.TestCase):
    """refund-reconciliation@1.0.0: ledger overrides the order record; a hidden per-customer counter."""

    @classmethod
    def setUpClass(cls):
        import yaml

        path = "challenges/gladiator/refund-reconciliation/1.0.0/workflow.yaml"
        cls.SPEC = yaml.safe_load((ROOT / path).read_text())

    def test_passes_automatic_validation(self):
        from engine.validate import validate

        self.assertEqual(validate(self.SPEC, only_levels=["hard", "expert"]), [])

    def test_counter_is_hidden_but_counts(self):
        from modules.workflow import Workflow

        m = Workflow(self.SPEC, seed=4, difficulty="hard")
        customer = m.world["customers"][0]
        self.assertNotIn("refunds_today", m.act("get_customer", {"id": customer["id"]}, "{}"))
        # An approval takes one from the customer's allowance for the rest of the queue.
        req = next(
            r
            for r in m.world["refund_requests"]
            if next(o for o in m.world["orders"] if o["id"] == r["order_id"])["customer_id"] == customer["id"]
        )
        m.act("issue_refund", {"request_id": req["id"], "amount": 10}, "{}")
        self.assertEqual(customer["refunds_today"], 1)
        self.assertNotIn("refunds_today", m.act("get_customer", {"id": customer["id"]}, "{}"))

    def test_hidden_counter_must_start_at_zero_and_stay_hidden(self):
        import copy

        from engine.validate import validate

        shown = copy.deepcopy(self.SPEC)
        next(t for t in shown["tools"] if t["name"] == "get_customer").pop("hide")
        self.assertTrue(any("shown by a tool" in p for p in validate(shown, only_levels=["hard"])))


class JobQueueRecoveryTests(unittest.TestCase):
    """job-queue-recovery@1.0.0: resume after an interrupted run, jobs added mid-task."""

    @classmethod
    def setUpClass(cls):
        import yaml

        cls.SPEC = yaml.safe_load((ROOT / "challenges/gladiator/job-queue-recovery/1.0.0/workflow.yaml").read_text())

    def test_passes_automatic_validation(self):
        from engine.validate import validate

        self.assertEqual(validate(self.SPEC, only_levels=["easy", "standard", "hard", "expert"]), [])

    def test_resume_leaves_receipts_and_a_stale_checkpoint(self):
        from modules.workflow import Workflow

        m = Workflow(self.SPEC, seed=5, difficulty="standard")
        receipts = {r["job_id"] for r in m.world["receipts"]}
        self.assertTrue(receipts and receipts <= m.resume_keys)
        checkpoint = m.world["run_checkpoint"][0]
        self.assertLess(checkpoint["items_done"], len(m.resume_keys))  # behind what was really done
        applied = next(iter(receipts))
        self.assertTrue(m.act("check_receipt", {"job_id": applied}, "{}")["found"])
        # Applying it again is a duplicate.
        m.act("apply_job", {"job_id": applied}, "{}")
        self.assertGreaterEqual(m.grade().details["duplicate_effects"], 1)

    def test_added_jobs_must_be_handled(self):
        from engine.validate import play
        from modules.workflow import Workflow

        m = Workflow(self.SPEC, seed=5, difficulty="standard")
        self.assertGreater(m.inserts, 0)
        self.assertTrue(play(m, "oracle").passed)
        self.assertGreater(m.grade().details["items"], len(m.expected))  # the added jobs counted too
        self.assertFalse(play(Workflow(self.SPEC, seed=5, difficulty="standard"), "no_rescan").passed)


class EscalationTests(unittest.TestCase):
    """refund-reconciliation@1.2.0: requests the rules don't cover must be escalated, never decided."""

    @classmethod
    def setUpClass(cls):
        import yaml

        path = "challenges/gladiator/refund-reconciliation/1.2.0/workflow.yaml"
        cls.SPEC = yaml.safe_load((ROOT / path).read_text())

    def test_passes_automatic_validation(self):
        from engine.validate import validate

        self.assertEqual(validate(self.SPEC, only_levels=["hard", "expert"]), [])

    def test_uncovered_values_expect_escalation_and_deciding_them_is_wrong(self):
        from modules.workflow import Workflow

        m = Workflow(self.SPEC, seed=3, difficulty="hard")
        escalate = [k for k, v in m.expected.items() if v == "escalate"]
        self.assertTrue(escalate)
        m.act("deny_request", {"request_id": escalate[0], "reason": "x"}, "{}")
        self.assertEqual(m.grade().details["confusions"], {"escalate->deny": 1})


class MidTaskChangeTests(unittest.TestCase):
    """Announced mid-task changes and the marathon level (refund-queue 2.1.0, access-review 1.1.0,
    incident-triage 1.1.0, inventory-transfer 1.1.0)."""

    SPECS = {
        "refund": "challenges/gladiator/refund-queue/2.1.0/workflow.yaml",
        "access": "challenges/gladiator/access-review/1.1.0/workflow.yaml",
        "incident": "challenges/gladiator/incident-triage/1.1.0/workflow.yaml",
        "inventory": "challenges/gladiator/inventory-transfer/1.1.0/workflow.yaml",
    }

    @classmethod
    def setUpClass(cls):
        import yaml

        cls.spec = {k: yaml.safe_load((ROOT / p).read_text()) for k, p in cls.SPECS.items()}

    def test_new_versions_pass_validation_up_to_expert(self):
        from engine.validate import validate

        for name, spec in self.spec.items():  # marathon is validated in full locally (slow); see catalog evidence
            self.assertEqual(validate(spec, only_levels=["easy", "standard", "hard", "expert"]), [], name)

    def test_marathon_is_solvable(self):
        from engine.validate import play
        from modules.workflow import Workflow

        for name, spec in self.spec.items():
            self.assertTrue(play(Workflow(spec, seed=1, difficulty="marathon"), "oracle").passed, name)

    def test_decisions_are_graded_by_the_facts_at_the_time(self):
        from modules.workflow import Workflow

        for seed in range(1, 60):  # a world whose first change re-assigns a service with two alerts to route
            m = Workflow(self.spec["incident"], seed=seed, difficulty="standard")
            change = m.change_plan[0]
            service = m.world0["services"][change["index"]]
            alerts = [
                a for a in m.world["alerts"] if a["service_id"] == service["id"] and m.expected[a["id"]] == "route"
            ]
            if change["set"].get("owner") and len(alerts) >= 2:
                break
        else:
            self.fail("no suitable world")
        old_owner, new_owner = service["owner"], change["set"]["owner"]
        before, after = alerts[0], alerts[-1]
        m.act("get_service", {"id": service["id"]}, "{}")
        m.act("route_alert", {"alert_id": before["id"], "team": old_owner}, "{}")  # right before the change
        while m.epoch == 0:
            res = m.act("list_alerts", {"page": 1}, "{}")
        self.assertIn("notices", res)
        self.assertIn(new_owner, res["notices"][0])
        m.act("route_alert", {"alert_id": after["id"], "team": old_owner}, "{}")  # stale after the notice
        d = m.grade().details
        self.assertEqual(d["wrong_args"], 1)  # only the stale one; the earlier one used the facts of its time

    def test_ignoring_notices_fails_at_the_top_levels(self):
        from engine.validate import play
        from modules.workflow import Workflow

        for name in ("refund", "incident", "inventory", "access"):
            passes = sum(
                play(Workflow(self.spec[name], seed=s, difficulty="expert"), "stale").passed for s in range(1, 6)
            )
            self.assertLessEqual(passes, 1, name)

    def test_every_level_names_every_setting(self):
        from engine.validate import validate

        broken = {
            **self.spec["refund"],
            "hazards": {**self.spec["refund"]["hazards"], "injections": {"easy": 0, "standard": 1}},
        }
        self.assertTrue(any("no value for level" in p for p in validate(broken, only_levels=["easy"])))


class EfficiencyTests(unittest.TestCase):
    """The core score: fewest mistakes, least cost, tokens and time (scorers/efficiency.py)."""

    def r(self, cost=0.1, tokens=1000, time=10.0, severity=0, passed=True, termination="session_ended"):
        return {
            "cost_usd": cost,
            "input_tokens": tokens,
            "output_tokens": 0,
            "wall_s": time,
            "passed": passed,
            "termination": termination,
            "grade_details": {"error_severity": severity, "explicit_finish": True},
        }

    def test_timing_jitter_never_picks_a_winner(self):
        from scorers.efficiency import pair_scores
        from scorers.outcome import pair_outcome

        # Same play, 0.1s vs 1.5s (jitter): even. 10s vs 60s (a real difference): the faster wins.
        self.assertEqual(pair_scores(self.r(time=0.1), self.r(time=1.5)), (1.0, 1.0))
        self.assertEqual(pair_outcome(self.r(time=0.1), self.r(time=1.5))["winner"], "tie")
        a, b = pair_scores(self.r(time=10), self.r(time=60))
        self.assertLess(a, b)

    def test_identical_attempts_score_even(self):
        from scorers.efficiency import pair_scores

        self.assertEqual(pair_scores(self.r(), self.r()), (1.0, 1.0))

    def test_cheaper_wins_when_both_are_clean_and_mistakes_dominate(self):
        from scorers.efficiency import pair_scores

        a, b = pair_scores(self.r(cost=0.1), self.r(cost=0.3))
        self.assertLess(a, b)
        # A clean expensive run beats a cheap run with a duplicate refund (severity 5) on the balanced profile.
        clean, messy = pair_scores(self.r(cost=0.3), self.r(cost=0.1, severity=5))
        self.assertLess(clean, messy)

    def test_profiles_change_the_trade_off(self):
        from scorers.efficiency import pair_scores

        fast_dear, slow_cheap = self.r(cost=0.4, time=5), self.r(cost=0.1, time=40)
        self.assertLess(
            pair_scores(slow_cheap, fast_dear, "budget")[0], pair_scores(slow_cheap, fast_dear, "budget")[1]
        )
        self.assertLess(
            pair_scores(fast_dear, slow_cheap, "latency")[0], pair_scores(fast_dear, slow_cheap, "latency")[1]
        )

    def test_running_out_of_the_attempt_budget_counts_against_the_model(self):
        from scorers.efficiency import pair_scores

        a, b = pair_scores(self.r(), self.r(termination="over_budget"))
        self.assertGreater(b - a, 0.9)

    def test_quitting_early_never_beats_a_clean_finish(self):
        from scorers.efficiency import PROFILES, pair_scores

        quitter = self.r(cost=0.0001, tokens=50, time=0.5, severity=12, passed=False)
        quitter["grade_details"].update(missing=12, items=12)
        half = self.r(cost=0.02, tokens=500, time=5, severity=6, passed=False)
        half["grade_details"].update(missing=6, items=12)
        for profile in PROFILES:
            for unfinished in (quitter, half):
                clean, other = pair_scores(self.r(cost=0.05, tokens=2000, time=60), unfinished, profile)
                self.assertLess(clean, other, profile)

    def test_skipping_a_few_items_costs_a_proportional_penalty(self):
        from scorers.efficiency import pair_scores, unfinished_share

        skipped = self.r(severity=2, passed=False)
        skipped["grade_details"].update(missing=2, items=20)
        self.assertAlmostEqual(unfinished_share(skipped), 0.1)
        self.assertEqual(unfinished_share(self.r(termination="over_budget")), 1.0)
        # Two skipped items (2 points + 0.1) beat four wrong decisions (12 points) on the balanced profile.
        a, b = pair_scores(skipped, self.r(severity=12, passed=False))
        self.assertLess(a, b)

    def test_victory_rule_success_first(self):
        from scorers.outcome import pair_outcome

        win = self.r(cost=0.07)
        lose = self.r(cost=0.01, severity=6, passed=False)
        lose["grade_details"].update(items=10, correct=8)
        # A success beats a failure however cheap the failure was.
        self.assertEqual(pair_outcome(lose, win, "budget")["winner"], "B")
        self.assertEqual(pair_outcome(lose, win, "budget")["by"], "success")

    def test_victory_rule_both_succeed_goes_to_efficiency(self):
        from scorers.outcome import pair_outcome

        o = pair_outcome(self.r(cost=0.02), self.r(cost=0.06), "budget")
        self.assertEqual((o["winner"], o["by"]), ("A", "efficiency"))

    def test_victory_rule_neither_succeeds_goes_to_progress_then_efficiency(self):
        from scorers.outcome import pair_outcome

        far = self.r(cost=0.09, severity=3, passed=False)
        far["grade_details"].update(items=10, correct=9)
        near = self.r(cost=0.01, severity=3, passed=False)
        near["grade_details"].update(items=10, correct=5)
        self.assertEqual(pair_outcome(near, far, "budget")["winner"], "B")
        self.assertEqual(pair_outcome(near, far, "budget")["by"], "progress")
        same = dict(near, grade_details={**near["grade_details"]}, cost_usd=0.05)
        o = pair_outcome(near, same, "budget")  # equal progress: efficiency breaks it
        self.assertEqual((o["winner"], o["by"]), ("A", "efficiency"))

    def test_victory_rule_no_real_difference_is_a_tie(self):
        from scorers.outcome import pair_outcome, summary

        self.assertEqual(pair_outcome(self.r(), self.r())["winner"], "tie")
        s = summary([(self.r(), self.r()), (self.r(), self.r())])
        self.assertEqual((s["winner"], s["ties"]), ("draw", 2))
        # Level on pair wins: the mean efficiency decides, unless it is within the margin.
        s = summary([(self.r(cost=0.01), self.r(cost=0.02)), (self.r(cost=0.03), self.r(cost=0.02))])
        self.assertEqual((s["a_wins"], s["b_wins"]), (1, 1))
        self.assertIn(s["winner"], ("A", "B"))
        self.assertEqual(s["decided_by"], "efficiency (pair wins were level)")

    def test_default_success_rule_is_the_victory_rule(self):
        from engine.suite import DEFAULT_SUCCESS

        self.assertEqual(DEFAULT_SUCCESS["primary"], "outcome")
        self.assertEqual(suites.load("suites/small_tier.yaml")["success"]["primary"], "outcome")

    def test_singular_failure_reasons(self):
        from reports.markdown import why_failed

        self.assertEqual(why_failed({"wrong": 1, "missing": 1}), "1 wrong decision; 1 item left undecided")

    def test_identical_unfinished_attempts_still_score_even(self):
        from scorers.efficiency import pair_scores, summary

        a = self.r(termination="action_limit")
        a["grade_details"]["missing"] = 3
        self.assertEqual(pair_scores(a, dict(a)), (2.0, 2.0))
        s = summary([(a, self.r())])
        self.assertEqual((s["a_unfinished"], s["b_unfinished"], s["b_wins"]), (1, 0, 1))

    def test_hard_attempt_budget_stops_an_attempt(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        f.write(
            "suite: t\ndefaults: {difficulty: easy, attempt_budget_tokens: 1}\nnodes:\n"
            "  - id: r\n    challenge: gladiator/refund-queue@2.1.0\n"
        )
        f.close()
        body, _ = quiet(runner.run_match, suites.load(f.name), "scripted:invalid", "scripted:silent", 1.0)
        ends = {r["termination"] for r in body["contestants"]["A"]["results"]}
        self.assertTrue(ends <= {"over_budget", "invalid_action", "no_action"}, ends)

    def test_suite_can_set_the_attempt_time_limit(self):
        import itertools
        from unittest import mock

        f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        f.write(
            "suite: t\ndefaults: {difficulty: easy, attempt_time_limit_s: 5}\nnodes:\n"
            "  - id: r\n    challenge: gladiator/refund-queue@2.1.0\n"
        )
        f.close()
        # A simulated clock that moves 10 s per reading (real clocks tick too coarsely on Windows to test this).
        ticks = itertools.count(0, 10)
        with mock.patch.object(runner.time, "monotonic", side_effect=lambda: next(ticks)):
            body, _ = quiet(runner.run_match, suites.load(f.name), "scripted:invalid", "scripted:silent", 1.0)
        self.assertEqual(body["contestants"]["A"]["results"][0]["termination"], "time_limit")
        self.assertEqual(
            runner.attempt_limits({"defaults": {"attempt_time_limit_s": 2700}}, {}, {}), {"attempt_time_limit_s": 2700}
        )

    def test_unknown_profile_is_refused(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        f.write(
            "suite: t\nsuccess: {primary: efficiency, profile: vibes}\n"
            "nodes:\n  - id: c\n    challenge: gladiator/transaction-state@1.0.1\n"
        )
        f.close()
        with self.assertRaises(suites.SuiteError):
            suites.load(f.name)


class ForkTests(unittest.TestCase):
    """gladiator fork: copy a package to a new id or version, sealed, with lineage."""

    def setUp(self):
        import shutil

        from engine import packages

        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = packages.resolve("gladiator/refund-queue@2.1.0")
        rel = self.src.path.relative_to(packages.ROOT)
        shutil.copytree(self.src.path, self.tmp / rel)
        self.src.path = self.tmp / rel  # pretend the repo lives in tmp

    def test_fork_to_new_id_sets_lineage_and_moves_files(self):
        import yaml

        from engine import packages

        path = packages.fork(self.src, "myteam/refunds", None, root=self.tmp)
        m = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertEqual((m["id"], m["version"], m["forked_from"]), ("myteam/refunds", "2.1.0", self.src.ref))
        self.assertEqual(m["files"], ["challenges/myteam/refunds/2.1.0/workflow.yaml"])
        self.assertEqual(m["params"]["spec"], "challenges/myteam/refunds/2.1.0/workflow.yaml")
        self.assertTrue((self.tmp / m["files"][0]).is_file())
        self.assertEqual(m["content_sha256"], packages.fingerprint(m, self.tmp))  # sealed as copied

    def test_new_version_keeps_id_and_lineage_field(self):
        import yaml

        from engine import packages

        path = packages.fork(self.src, None, "2.2.0", root=self.tmp)
        m = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertEqual((m["id"], m["version"], m["forked_from"]), ("gladiator/refund-queue", "2.2.0", None))

    def test_refuses_same_ref_or_existing_target(self):
        from engine import packages

        with self.assertRaises(packages.PackageError):
            packages.fork(self.src, None, None, root=self.tmp)
        packages.fork(self.src, None, "9.9.9", root=self.tmp)
        with self.assertRaises(packages.PackageError):
            packages.fork(self.src, None, "9.9.9", root=self.tmp)


class RunRecordTests(unittest.TestCase):
    """Run-record format version 2 (docs/RUN_RECORD.md)."""

    def test_records_carry_version_commit_and_first_pass(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        f.write("suite: t\nnodes:\n  - id: c\n    challenge: gladiator/transaction-state@1.0.1\n")
        f.close()
        body, _ = quiet(runner.run_match, suites.load(f.name), "scripted:golden", "scripted:invalid", 1.0)
        self.assertEqual(body["record_version"], 2)
        self.assertIn("engine_commit", body)
        a, b = body["contestants"]["A"]["results"][0], body["contestants"]["B"]["results"][0]
        self.assertTrue(a["passed"] and a["first_pass"])
        self.assertFalse(b["first_pass"])

    def test_engine_commit_is_the_engines_own_checkout_not_the_ci_job(self):
        """A workflow in another repository can check the engine out and run it: the record must name the
        engine's commit, not that job's GITHUB_SHA. A folder that is not the top of a checkout gives None."""
        root = Path(runner.__file__).resolve().parent.parent
        own = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=root)
        with mock.patch.dict(os.environ, {"GITHUB_SHA": "f" * 40}):
            if own.returncode == 0 and (root / ".git").exists():
                self.assertEqual(runner.engine_commit(), own.stdout.strip())
            else:  # no checkout (e.g. a container image): the CI commit is the fallback
                self.assertEqual(runner.engine_commit(), "f" * 40)
            self.assertIsNone(runner._git_commit(root / "engine"))  # inside a checkout, but not its top
            with tempfile.TemporaryDirectory() as tmp:
                self.assertIsNone(runner._git_commit(Path(tmp)))

    def test_old_records_are_upgraded_when_read(self):
        from engine.record import upgrade

        old = {
            "contestants": {
                "A": {
                    "results": [
                        {"passed": True, "turns": [{"observation": {"error": "timeout"}}, {"observation": {"ok": 1}}]},
                        {"passed": True, "turns": [{"observation": {"error": "not_found"}}]},
                    ]
                }
            }
        }
        new = upgrade(old, 2, {"timeout", "temporarily_unavailable"})
        r1, r2 = new["contestants"]["A"]["results"]
        self.assertEqual((r1["first_pass"], r1["model_errors"]), (True, 0))  # a planted fault is not a mistake
        self.assertEqual((r2["first_pass"], r2["model_errors"]), (False, 1))
        self.assertEqual(new["record_version"], 2)
        self.assertNotIn("record_version", old)  # the caller's copy is untouched

    def test_workflow_rejected_step_breaks_first_pass(self):
        from engine.runner import first_pass, model_errors

        class G:
            passed, details = True, {}

        turns = [{"observation": {"error": "missing_fields"}}, {"observation": {"ok": True}}]
        self.assertEqual(model_errors(turns), 1)
        self.assertFalse(first_pass(G(), turns))
        self.assertTrue(first_pass(G(), [{"observation": {"error": "temporarily_unavailable"}}]))


class WordingTests(unittest.TestCase):
    """Equivalent rewordings of brief and policy, picked per attempt (refund-queue 2.2.0 and siblings)."""

    @classmethod
    def setUpClass(cls):
        import yaml

        cls.SPEC = yaml.safe_load((ROOT / "challenges/gladiator/refund-queue/2.2.0/workflow.yaml").read_text())

    def test_wording_follows_the_seed_and_leaves_the_world_alone(self):
        from modules.workflow import Workflow

        a, b = Workflow(self.SPEC, seed=7, difficulty="easy"), Workflow(self.SPEC, seed=7, difficulty="easy")
        self.assertEqual(a.start(), b.start())  # both contestants see the same wording
        picked = {Workflow(self.SPEC, seed=s, difficulty="easy").wording()[0] for s in range(30)}
        self.assertEqual(picked, {0, 1, 2})
        plain = dict(self.SPEC)
        plain.pop("wordings")
        self.assertEqual(Workflow(plain, seed=7, difficulty="easy").world, a.world)  # same world either way
        self.assertIn("wording", a.grade().details)

    def test_rewording_that_changes_a_number_is_refused(self):
        import copy

        from engine.validate import validate

        bad = copy.deepcopy(self.SPEC)
        bad["wordings"][0]["policy_text"] = bad["wordings"][0]["policy_text"].replace("60 days", "61 days")
        self.assertTrue(any("changes the policy" in p for p in validate(bad, only_levels=["easy"])))


class ReliabilityTests(unittest.TestCase):
    """Clean every time (pass^k): a test counts only if every repeat passed."""

    def test_pass_k_counts_tests_clean_on_every_repeat(self):
        from scorers.components import reliability

        rs = [
            {"node": "a", "passed": True},
            {"node": "a", "passed": True},
            {"node": "b", "passed": True},
            {"node": "b", "passed": False},
        ]
        self.assertEqual(reliability(rs), {"tests": 2, "clean_every_time": 1, "repeats": 2})

    def test_report_shows_reliability_with_repeats(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        f.write("suite: t\nnodes:\n  - id: c\n    challenge: gladiator/transaction-state@1.0.1\n")
        f.close()
        body, _ = quiet(runner.run_match, suites.load(f.name), "scripted:golden", "scripted:invalid", 1.0, None, 2)
        report = markdown.render("t", body, components.verdict(body))
        self.assertIn("Clean every time (all 2 repeats passed) | 1/1 tests | 0/1 tests", report)


class ReportReasonTests(unittest.TestCase):
    def test_cost_per_pass(self):
        from reports.markdown import _per_pass

        self.assertEqual(_per_pass({"cost_usd": 0.54, "passed": 3}), "0.180000")
        self.assertEqual(_per_pass({"cost_usd": 0.54, "passed": 0}), "no passes")

    def test_failure_reasons_are_plain_english(self):
        from reports.markdown import why_failed

        d = {"duplicate_effects": 2, "blind_retries_after_unknown_outcome": 3, "wrong": 0, "explicit_finish": True}
        self.assertEqual(
            why_failed(d),
            "2 duplicate effects (the same action done twice); 3 retried after an unknown outcome without checking",
        )
        self.assertEqual(
            why_failed({"report_correct": False, "passed": False, "task_evaluated": True}),
            "failed checks: report correct",
        )
        self.assertEqual(why_failed({"passed": False}, "invalid_action"), "ended by invalid_action")

    def test_report_lists_why_attempts_failed(self):
        from reports import markdown

        body, _ = quiet(runner.run_match, suites.load(SIX), "scripted:golden", "scripted:invalid", 1.0)
        self.assertIn("## Why attempts failed", markdown.render("t", body, components.verdict(body)))
