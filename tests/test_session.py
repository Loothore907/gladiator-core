"""Offline tests for the guided conversation (engine/session.py, `gladiator chat`; docs/PRODUCT.md, section 4a).

The model that may read a description is replaced by a fake that answers through the same interface, so nothing
here needs a key or costs anything.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import packages, session
from providers.base import ProviderError, Reply, Usage

REFUNDS = "gladiator/refund-queue@2.3.0"
DESCRIPTION = "We refund customers for damaged or late orders within 30 days. VIPs get 45. Never more than $250."


class FakeChat:
    def __init__(self, first, tool, answer):
        self.first, self.tool, self.answer = first, tool, answer

    def approx_input_chars(self):
        return len(self.first)

    def ask(self):
        if isinstance(self.answer, Exception):
            raise self.answer
        usage = Usage(input_tokens=800, output_tokens=60)
        if self.answer is None:  # the model wrote text and called nothing
            return Reply(None, "I think it is the refund one.", "end_turn", usage)
        return Reply(json.dumps(self.answer), "", "tool_use", usage, tool_name=self.tool, tool_args=self.answer)


class FakeProvider:
    """Answers each tool with what the test says a model sent, and keeps what it was shown."""

    def __init__(self, **answers):
        self.answers, self.seen = answers, []

    def open_chat(self, system, first_message, settings, tools=None):
        self.seen.append({"rules": system, "message": json.loads(first_message), "tool": tools[0]})
        return FakeChat(first_message, tools[0].name, self.answers.get(tools[0].name))


def reader(provider, max_usd=0.05):
    price = {"input": 0.10, "output": 0.50}  # USD per million tokens, as in providers/prices.yaml
    return session.ModelReader("openai:gpt-6-luna", max_usd, provider=provider, price=price)


class WithoutAModelTests(unittest.TestCase):
    """The conversation with nobody reading the description: keyword match, every choice from the test's value."""

    def setUp(self):
        self.talk = session.ForkSession()

    def test_the_closest_adaptable_test_is_proposed_with_what_matched(self):
        found = self.talk.describe(DESCRIPTION)
        self.assertEqual(found["proposed"], REFUNDS)
        self.assertIn("refund", found["why"])
        self.assertEqual((found["read_by"], found["read_cost_usd"], found["note"]), (None, 0.0, None))

    def test_a_description_nothing_matches_proposes_nothing(self):
        found = self.talk.describe("teach my parrot to whistle sea shanties")
        self.assertEqual((found["proposed"], found["forkable"]), (None, []))

    def test_each_open_choice_is_a_question_starting_from_the_tests_own_value(self):
        self.talk.describe(DESCRIPTION)
        chosen = self.talk.choose(REFUNDS)
        first = chosen["questions"][0]
        self.assertEqual(len(chosen["questions"]), 5)
        self.assertEqual((first["name"], first["value"], first["source"]), ("refund_window_days", 60, None))
        self.assertEqual((first["min"], first["max"], first["unit"]), (7, 180, "days"))
        self.assertIn("How many days after payment", first["ask"])

    def test_answers_are_checked_one_at_a_time_and_typed_the_way_people_type(self):
        self.talk.describe(DESCRIPTION)
        self.talk.choose(REFUNDS)
        self.assertEqual(self.talk.answer("refund_window_days", "30 days")["value"], 30)
        self.assertEqual(self.talk.answer("refund_cap", "$1,250")["value"], 1250)
        self.assertEqual(self.talk.answer("premium_tier", " VIP ")["value"], "vip")
        self.assertEqual(self.talk.answer("prior_refund_limit", "")["value"], 3)  # Enter keeps the value shown
        for name, typed, expected in (
            ("refund_window_days", "a month", "is not a whole number"),
            ("refund_window_days", "30 weeks", "is not a whole number"),
            ("refund_window_days", "400", "outside the allowed range, 7 to 180"),
            ("premium_tier", "gold'] or True or ['", "one plain word"),
            ("page_size", "50", "not one of this test's choices"),
        ):
            given = self.talk.answer(name, typed)
            self.assertFalse(given["ok"])
            self.assertIn(expected, given["problem"])
        self.assertEqual(self.talk.values["refund_window_days"], 30)  # a refused answer changes nothing

    def test_the_review_shows_the_rules_a_model_will_be_told_with_the_chosen_values(self):
        self.talk.describe(DESCRIPTION)
        self.talk.choose(REFUNDS)
        for name, typed in (("refund_window_days", "30"), ("premium_window_days", "45"), ("refund_cap", "250")):
            self.talk.answer(name, typed)
        look = self.talk.review()
        self.assertEqual(look["broken"], [])
        self.assertIn("paid 30 days ago or less", look["rules"])
        self.assertIn("the limit is 45 days", look["rules"])
        self.assertIn("never more than 250", look["rules"])
        self.assertEqual(
            [c["name"] for c in look["changed"]], ["refund_window_days", "premium_window_days", "refund_cap"]
        )

    def test_a_rule_between_choices_names_the_choices_to_ask_again(self):
        self.talk.describe(DESCRIPTION)
        self.talk.choose(REFUNDS)
        self.talk.answer("refund_window_days", "90")
        self.talk.answer("premium_window_days", "60")
        look = self.talk.review()
        self.assertEqual(look["rules"], "")  # nothing is shown as the rules while a rule between choices is broken
        self.assertEqual(look["broken"][0]["choices"], ["refund_window_days", "premium_window_days"])
        self.assertIn("must be longer than the standard", look["broken"][0]["say"])

    def test_create_writes_a_sealed_named_fork_only_after_validation(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        talk = session.ForkSession(root=tmp)
        shutil.copytree(ROOT / "blueprints", tmp / "blueprints")
        talk.describe(DESCRIPTION)
        talk.choose(REFUNDS)
        rel = talk.pkg.path.relative_to(packages.ROOT)
        shutil.copytree(talk.pkg.path, tmp / rel)
        talk.pkg.path = tmp / rel  # pretend the library lives in tmp, so nothing is written into the repository
        result = talk.create("acme/returns-desk", name="Returns desk", only_levels=["expert"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["ref"], "acme/returns-desk@1.0.0")
        self.assertTrue((tmp / result["written"]["suite"]).is_file())
        self.assertEqual(session.slug("  Returns Desk #2! "), "returns-desk-2")


class ReaderTests(unittest.TestCase):
    """A model reads the description. What it says is offered to the person, never taken on trust."""

    def read(self, **answers):
        self.provider = FakeProvider(**answers)
        self.talk = session.ForkSession(reader=reader(self.provider))
        self.found = self.talk.describe(DESCRIPTION)
        return self.found

    def test_the_model_can_only_answer_inside_a_schema_built_from_the_declared_choices(self):
        self.read(pick_test={"ref": REFUNDS, "why": "Both decide refund requests."}, fill_choices={"not_covered": []})
        pick, fill = self.provider.seen[0]["tool"], self.provider.seen[1]["tool"]
        self.assertEqual(pick.parameters["properties"]["ref"]["enum"], [REFUNDS, "none"])
        self.assertTrue(pick.strict)
        window = fill.parameters["properties"]["refund_window_days"]
        self.assertEqual((window["type"], window["minimum"], window["maximum"]), ("integer", 7, 180))
        self.assertEqual(
            set(fill.parameters["properties"]),
            {
                "not_covered",
                "refund_window_days",
                "premium_window_days",
                "premium_tier",
                "prior_refund_limit",
                "refund_cap",
            },
        )
        self.assertEqual(self.provider.seen[0]["message"]["description"], " ".join(DESCRIPTION.split()))
        shown = self.provider.seen[1]["message"]["choices"][0]
        self.assertIn("ask", shown)
        self.assertNotIn("default", shown)  # it reports what the description says, so it never sees the test's value

    def test_what_it_understood_prefills_the_choices_and_is_marked_as_such(self):
        found = self.read(
            pick_test={"ref": REFUNDS, "why": "Both decide refund requests against a policy."},
            fill_choices={
                "refund_window_days": 30,
                "premium_window_days": 45,
                "premium_tier": "vip",
                "refund_cap": 250,
                "not_covered": ["refunds as store credit"],
            },
        )
        self.assertEqual((found["proposed"], found["read_by"]), (REFUNDS, "openai:gpt-6-luna"))
        self.assertEqual(found["why"], "Both decide refund requests against a policy.")
        chosen = self.talk.choose(REFUNDS)
        by_name = {q["name"]: q for q in chosen["questions"]}
        self.assertEqual(
            (by_name["refund_window_days"]["value"], by_name["refund_window_days"]["source"]), (30, "your description")
        )
        self.assertEqual((by_name["prior_refund_limit"]["value"], by_name["prior_refund_limit"]["source"]), (3, None))
        self.assertEqual(chosen["not_covered"], ["refunds as store credit"])
        # the person still has the last word on a value the model read
        self.talk.answer("refund_window_days", "21")
        self.assertEqual(self.talk.values["refund_window_days"], 21)

    def test_values_it_gets_wrong_are_dropped_and_said_never_used(self):
        self.read(
            pick_test={"ref": REFUNDS, "why": "x"},
            fill_choices={
                "refund_window_days": 900,  # outside the declared range
                "refund_cap": "plenty",  # not a number
                "queue_size": 500,  # not a choice of this test
                "premium_tier": "vip",
                "not_covered": [f"thing {i}" for i in range(9)] + ["x" * 400],
            },
        )
        chosen = self.talk.choose(REFUNDS)
        self.assertEqual(self.talk.values["refund_window_days"], 60)
        self.assertEqual(self.talk.values["refund_cap"], 500)
        self.assertEqual(self.talk.values["premium_tier"], "vip")
        self.assertEqual(len(chosen["unusable"]), 2)
        self.assertTrue(any("outside the allowed range" in u for u in chosen["unusable"]))
        self.assertEqual(len(chosen["not_covered"]), session.MAX_NOT_COVERED)

    def test_it_may_say_no_test_is_the_same_kind_of_job(self):
        found = self.read(pick_test={"ref": "none", "why": "Yours schedules deliveries; none of these does."})
        self.assertIsNone(found["proposed"])
        self.assertIn("schedules deliveries", found["why"])
        self.assertEqual(len(self.provider.seen), 1)  # nothing to fill, so it is not asked again

    def test_a_test_it_makes_up_is_not_proposed(self):
        found = self.read(pick_test={"ref": "gladiator/store-credit@9.9.9", "why": "x"})
        self.assertIsNone(found["proposed"])

    def test_a_model_that_calls_no_tool_proposes_nothing_and_fills_nothing(self):
        found = self.read(pick_test=None)
        self.assertIsNone(found["proposed"])
        self.assertEqual(self.talk.reading["values"], {})

    def test_if_the_model_cannot_be_reached_the_conversation_carries_on_without_it(self):
        found = self.read(pick_test=ProviderError("HTTP 503: overloaded"))
        self.assertEqual(found["proposed"], REFUNDS)  # the keyword match
        self.assertIn("was not read by a model", found["note"])
        self.assertIn("503", found["note"])
        self.assertEqual(found["read_cost_usd"], 0.0)

    def test_reading_is_paid_from_its_own_small_cap_and_stops_when_that_is_used(self):
        provider = FakeProvider(pick_test={"ref": REFUNDS, "why": "x"}, fill_choices={"not_covered": []})
        paid = reader(provider)
        session.ForkSession(reader=paid).describe(DESCRIPTION)
        # two calls of 800 tokens in and 60 out at $0.10 and $0.50 per million
        self.assertAlmostEqual(paid.spent_usd, 2 * (800 * 0.10 + 60 * 0.50) / 1_000_000)
        broke = reader(FakeProvider(pick_test={"ref": REFUNDS, "why": "x"}), max_usd=0.000001)
        found = session.ForkSession(reader=broke).describe(DESCRIPTION)
        self.assertIn("spending cap", found["note"])
        self.assertEqual((found["proposed"], broke.spent_usd), (REFUNDS, 0.0))  # carried on, and nothing was spent
        self.assertEqual(broke.provider.seen and broke.provider.seen[0]["tool"].name, "pick_test")


class ChatCommandTests(unittest.TestCase):
    def chat(self, typed: str):
        return subprocess.run(
            [sys.executable, str(ROOT / "gladiator.py"), "chat"],
            input=typed,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def test_a_whole_conversation_up_to_the_last_question(self):
        typed = "\n".join(
            ["we refund customers for damaged or late orders", "y", "30", "45 days", "vip", "", "$250", "returns desk"]
            + ["demo", "n"]
        )
        out = self.chat(typed + "\n")
        self.assertEqual(out.returncode, 0, out.stderr)
        said = out.stdout
        self.assertIn("Closest test: Refund queue (gladiator/refund-queue@2.3.0)", said)
        self.assertIn("This test lets you change 5 things", said)
        self.assertIn("1 of 5  How many days after payment can an order still be refunded?", said)
        self.assertIn("(2) The order was paid 30 days ago or less", said)  # the answers, in the rules a model reads
        self.assertIn('tier is "vip" the limit is 45 days', said)
        self.assertIn("refund_cap 500 -> 250", said)
        self.assertIn("Create demo/returns-desk?", said)
        self.assertIn("Stopped. Nothing was written.", said)
        self.assertFalse((ROOT / "challenges" / "demo").exists())

    def test_a_wrong_answer_and_a_broken_rule_are_explained_and_asked_again(self):
        typed = "\n".join(["refund requests", "y", "90", "60", "", "", "lots", "", "", "120", "x", "y", "n"])
        said = self.chat(typed + "\n").stdout
        self.assertIn("'lots' is not a whole number. Try again, or press Enter to keep 500.", said)
        self.assertIn("The premium window must be longer than the standard refund window.", said)
        self.assertIn("Again:  How many days do customers on your premium tier get instead?", said)
        self.assertIn("the limit is 120 days", said)

    def test_walking_away_mid_conversation_writes_nothing(self):
        out = self.chat("refund requests\ny\n30\n")
        self.assertEqual(out.returncode, 0)
        self.assertIn("Stopped. Nothing was written.", out.stdout)

    def test_a_flow_nothing_matches_is_told_so(self):
        out = self.chat("teach my parrot to whistle sea shanties\n")
        self.assertIn("No test in the library that you can adapt matches that description.", out.stdout)


if __name__ == "__main__":
    unittest.main()
