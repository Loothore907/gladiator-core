"""Gladiator for AI assistants (engine/assistant.py): an assistant reads what a test needs, checks a design and has
the engine create the test, over MCP. The same rules as the page and the terminal: declared choices only."""

import asyncio
import contextlib
import dataclasses
import io
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import jsonschema
import yaml
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from engine import assistant, blueprint, packages

ROOT = Path(__file__).resolve().parent.parent
REFUNDS = "gladiator/refund-queue@2.3.0"
CHOSEN = {"refund_window_days": 30, "premium_window_days": 45, "premium_tier": "vip"}


def call(server, name, arguments):
    async def go():
        async with Client(server) as client:
            return await client.call_tool(name, arguments)

    return asyncio.run(go()).structured_content


class WithAThrowawayLibrary(unittest.TestCase):
    """Created tests land in a throwaway folder, never in the repository."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        shutil.copytree(ROOT / "blueprints", self.tmp / "blueprints")
        source = packages.resolve(REFUNDS)
        rel = source.path.relative_to(packages.ROOT)
        shutil.copytree(source.path, self.tmp / rel)
        self.designs = assistant.Designs(self.tmp, levels=["easy"])  # one light level keeps the checks to a moment
        self.designs.known[REFUNDS] = (dataclasses.replace(source, path=self.tmp / rel), blueprint.load(REFUNDS))
        self.server = assistant.build(self.designs)

    def ask(self, tool, /, **arguments):
        return call(self.server, tool, arguments)


class ReadingTests(WithAThrowawayLibrary):
    def test_the_five_tools_and_the_rules_an_assistant_is_given(self):
        async def go():
            async with Client(self.server) as client:
                return {t.name for t in (await client.list_tools()).tools}, client.instructions

        tools, instructions = asyncio.run(go())
        self.assertEqual(tools, {"list_tests", "read_requirements", "check_design", "create_test", "get_result"})
        for rule in ("never stretch", "Never guess a value", "cannot express", "unreviewed", "never a claim about"):
            self.assertIn(rule, " ".join(instructions.split()))

    def test_the_list_says_which_tests_can_be_adapted_and_what_a_description_shares_with_each(self):
        every = self.ask("list_tests")
        by_ref = {t["test"]: t for t in every["tests"]}
        self.assertEqual((by_ref[REFUNDS]["can_be_adapted"], by_ref[REFUNDS]["open_choices"]), (True, 6))
        self.assertEqual(every["can_be_adapted"], sum(t["can_be_adapted"] for t in every["tests"]))
        self.assertTrue(any(not t["can_be_adapted"] for t in every["tests"]))  # they are listed, and say so
        self.assertNotIn("shares_words", by_ref[REFUNDS])
        found = self.ask("list_tests", description="we refund customers for damaged or late orders")
        self.assertEqual(found["tests"][0]["test"], REFUNDS)
        self.assertIn("refund", found["tests"][0]["shares_words"])
        nothing = self.ask("list_tests", description="teach my parrot to whistle sea shanties")
        self.assertTrue(nothing["note"].startswith("No test shares a word"))
        self.assertEqual(len(nothing["tests"]), len(every["tests"]))  # the list is still all there to read

    def test_requirements_are_everything_needed_to_design_a_test_in_one_answer(self):
        needs = self.ask("read_requirements", test=REFUNDS)
        first = needs["choices"][0]
        self.assertEqual((first["name"], first["type"], first["unit"]), ("refund_window_days", "whole_number", "days"))
        self.assertEqual((first["min"], first["max"], first["library_value"]), (7, 180, 60))
        self.assertIn("How many days after payment", first["ask"])
        self.assertGreater(first["used_in_places"], 1)
        self.assertEqual(len(needs["choices"]), 6)
        self.assertIn("premium window must be longer", " ".join(needs["rules_between_choices"]))
        self.assertIn("60 days ago or less", needs["the_rules"])  # shown with the library's values
        self.assertIn("refund queue", needs["the_job"])
        self.assertIn("issue_refund", [t["name"] for t in needs["tools_the_agent_gets"]])
        self.assertIn("cannot be expressed", needs["fixed"])
        # the schema is one a program can use as it is
        schema = needs["values_schema"]
        jsonschema.validate({"refund_window_days": 30, "premium_tier": "vip"}, schema)
        for bad in ({"refund_window_days": 3}, {"refund_window_days": "30"}, {"manager_approval": True}):
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(bad, schema)

    def test_a_test_that_cannot_be_adapted_or_does_not_exist_is_answered_in_plain_words(self):
        fixed = next(t["test"] for t in self.ask("list_tests")["tests"] if not t["can_be_adapted"])
        for tool, more in (("read_requirements", {}), ("check_design", {"values": {}})):
            said = self.ask(tool, test=fixed, **more)["error"]
            self.assertIn("no open choices yet", said)
            self.assertIn(REFUNDS, said)  # and what can be adapted instead
            self.assertIn("not a test in this library", self.ask(tool, test="../../etc/passwd", **more)["error"])
        self.assertIn(
            "not a test in this library", self.ask("read_requirements", test="gladiator/refund-queue")["error"]
        )


class DesignTests(WithAThrowawayLibrary):
    def test_a_good_design_comes_back_with_the_rules_a_model_will_be_told(self):
        look = self.ask("check_design", test=REFUNDS, values=CHOSEN)
        self.assertTrue(look["ok"])
        self.assertEqual((look["problems"], look["do_not_fit_together"]), ({}, []))
        self.assertEqual({c["name"] for c in look["changed"]}, set(CHOSEN))
        self.assertEqual(look["left_as_the_library_has_them"], ["prior_refund_limit", "order_limit", "refund_cap"])
        self.assertIn("paid 30 days ago or less", look["the_rules"])
        self.assertIn('"vip" the limit is 45 days', look["the_rules"])
        self.assertEqual(look["values"]["refund_cap"], 500)

    def test_what_is_wrong_is_said_choice_by_choice_and_no_rules_are_shown(self):
        values = {"refund_window_days": 30, "premium_window_days": 20, "refund_cap": "lots", "manager_approval": True}
        look = self.ask("check_design", test=REFUNDS, values=values)
        self.assertFalse(look["ok"])
        self.assertIn("not a whole number", look["problems"]["refund_cap"])
        self.assertIn("not one of this test's choices", look["problems"]["manager_approval"])  # nothing is smuggled in
        self.assertEqual(look["do_not_fit_together"][0]["choices"], ["refund_window_days", "premium_window_days"])
        self.assertNotIn("the_rules", look)
        self.assertIn("Nothing can be created", look["next"])

    def test_a_value_typed_the_way_people_type_is_understood(self):
        look = self.ask("check_design", test=REFUNDS, values={"refund_window_days": "30 days", "refund_cap": "$1,000"})
        self.assertEqual(
            (look["ok"], look["values"]["refund_window_days"], look["values"]["refund_cap"]), (True, 30, 1000)
        )


class CreateTests(WithAThrowawayLibrary):
    def create(self, **change):
        ask = {"test": REFUNDS, "values": CHOSEN, "owner": "Acme Ltd", "name": "Returns desk", **change}
        return self.ask("create_test", **ask)

    def test_a_design_becomes_a_sealed_validated_test_in_the_checkout(self):
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            made = self.create()
        self.assertEqual(printed.getvalue(), "")  # on stdio, standard output belongs to the protocol
        self.assertEqual(
            (made["state"], made["test"], made["built_from"]), ("created", "acme-ltd/returns-desk@1.0.0", REFUNDS)
        )
        self.assertIn("perfect player", made["checked"][0])
        self.assertTrue(made["trust"].startswith("unreviewed"))
        self.assertIn(f"--suite {made['files']['suite']}", made["run_it"])
        manifest = yaml.safe_load((self.tmp / made["files"]["test"] / "challenge.yaml").read_text(encoding="utf-8"))
        self.assertEqual(manifest["content_sha256"], packages.fingerprint(manifest, self.tmp))  # sealed as written
        self.assertEqual((manifest["name"], manifest["forked_from"]), ("Returns desk", REFUNDS))
        self.assertEqual(manifest["choices"]["values"]["premium_tier"], "vip")
        for written in ("evidence", "suite"):
            self.assertTrue((self.tmp / made["files"][written]).is_file())
        self.assertEqual(self.ask("get_result", job=made["job"])["state"], "created")  # asking again is harmless
        # a sealed test is never changed: the same name needs a new version
        again = self.create()
        self.assertEqual((again["state"], again["stage"]), ("refused", "name"))
        self.assertIn("already exists", again["problems"][0])
        self.assertEqual(self.create(version="1.1.0")["test"], "acme-ltd/returns-desk@1.1.0")

    def test_what_is_refused_before_any_check_and_writes_nothing(self):
        for change, stage in [
            ({"values": {"refund_cap": "lots"}}, "choices"),
            ({"values": {"refund_window_days": 30, "premium_window_days": 20}}, "choices"),
            ({"owner": "Gladiator"}, "name"),  # the library's own name
            ({"owner": "!!!"}, "name"),
            ({"name": ""}, "name"),
            ({"version": "2"}, "name"),
        ]:
            refused = self.create(**change)
            self.assertEqual((refused["state"], refused["stage"], refused["nothing_written"]), ("refused", stage, True))
        self.assertIn("error", self.create(test="gladiator/refund-queue@2.2.0"))
        self.assertFalse((self.tmp / "challenges" / "acme-ltd").exists())
        self.assertEqual((self.designs.jobs, self.designs.busy.locked()), ({}, False))

    def test_checks_that_take_long_are_answered_with_a_job_to_ask_about(self):
        self.designs.wait_s = 0  # do not wait at all: the first answer is "still checking"
        release = threading.Event()
        real = blueprint.fork

        def slow(*args, **kwargs):
            release.wait(30)
            return real(*args, **kwargs)

        with mock.patch.object(blueprint, "fork", slow):
            first = self.create()
            self.assertEqual((first["state"], bool(first["job"])), ("checking", True))
            self.assertIn("get_result", first["next"])
            busy = self.create(name="Another one")  # one at a time
            self.assertEqual((busy["state"], busy["stage"]), ("refused", "busy"))
            self.assertIn(first["job"], busy["problems"][0])
            self.designs.wait_s = 60
            release.set()
            done = self.ask("get_result", job=first["job"])
        self.assertEqual((done["state"], done["test"]), ("created", "acme-ltd/returns-desk@1.0.0"))
        self.assertFalse(self.designs.busy.locked())
        self.assertIn("error", self.ask("get_result", job="not-a-job"))

    def test_a_test_the_checks_reject_is_not_written_and_says_why(self):
        rejected = {
            "ok": False,
            "stage": "validation",
            "problems": ["standard: stale got through"],
            "levels": {
                "standard": {
                    "seeds": 20,
                    "oracle_passed": 20,
                    "players": {"stale": {"must_fail": True, "ok": False, "passed_seeds": 9, "seeds": 20}},
                }
            },
        }
        with mock.patch.object(blueprint, "fork", return_value=rejected):
            refused = self.create()
        self.assertEqual(
            (refused["state"], refused["stage"], refused["nothing_written"]), ("refused", "validation", True)
        )
        self.assertIn("got through 9 of 20", refused["problems"][0])
        self.assertFalse(self.designs.busy.locked())


class RealServerTests(unittest.TestCase):
    def test_the_real_server_answers_over_stdio(self):
        """Start `gladiator mcp` as a client would (a subprocess on stdio) and walk the reading half of the loop."""
        params = StdioServerParameters(command=sys.executable, args=["gladiator.py", "mcp"], cwd=str(ROOT))

        async def go():
            async with Client(params) as client:
                tools = {t.name for t in (await client.list_tools()).tools}
                listed = await client.call_tool("list_tests", {"description": "refund late orders"})
                checked = await client.call_tool("check_design", {"test": REFUNDS, "values": CHOSEN})
                return tools, client.instructions, listed.structured_content, checked.structured_content

        tools, instructions, listed, checked = asyncio.run(go())
        self.assertIn("create_test", tools)
        self.assertIn("ONE PERSON'S OWN FLOW", instructions)
        self.assertEqual(listed["tests"][0]["test"], REFUNDS)
        self.assertTrue(checked["ok"])


if __name__ == "__main__":
    unittest.main()
