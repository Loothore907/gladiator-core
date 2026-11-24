"""Offline tests for announced changes that cannot miss (`when: first_read`; docs/M2C_MULTI_OUTCOME_DESIGN.md,
section 4). refund-queue 2.3.0 is the first challenge to use them. No network, no cost.
"""

import copy
import json
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import packages
from engine.validate import play, validate
from modules.workflow import Workflow

REF = "gladiator/refund-queue@2.3.0"


def spec_of(ref: str) -> dict:
    pkg = packages.resolve(ref)
    return yaml.safe_load((ROOT / pkg.manifest["params"]["spec"]).read_text(encoding="utf-8"))


class FirstReadChangeTests(unittest.TestCase):
    def setUp(self):
        self.spec = spec_of(REF)

    def lookup(self, module, change):
        table, key = change["on_read"]
        tool = next(t for t in self.spec["tools"] if t["op"] == "get" and t["table"] == table)
        return module.act(tool["name"], {tool["key"]: key}, "{}")

    def test_every_announced_change_overturns_an_item_that_depends_on_the_record(self):
        for level in ("standard", "expert"):
            for seed in range(1, 16):
                m = Workflow(self.spec, seed=seed, difficulty=level)
                self.assertTrue(m.change_plan, (level, seed))  # every world has at least one
                records = [tuple(ch["on_read"]) for ch in m.change_plan]
                self.assertEqual(len(records), len(set(records)))  # never the same record twice
                for ch in m.change_plan:
                    after = copy.deepcopy(m.world0)
                    after[ch["table"]][ch["index"]].update(ch["set"])
                    new, _ = m._truth(after)
                    self.assertTrue([k for k in ch["touches"] if new[k] != m.expected[k]], (level, seed))
                    # and it leaves alone anything a planted instruction sits on
                    self.assertFalse(set(ch["touches"]) & m.injected_targets)

    def test_the_notice_comes_with_the_first_lookup_which_still_shows_the_old_record(self):
        m = Workflow(self.spec, seed=3, difficulty="standard")
        change = m.change_plan[0]
        before = dict(m.world[change["table"]][change["index"]])
        self.assertNotIn("notices", m.act("list_refund_requests", {"page": 1}, "{}"))  # nothing until the lookup
        first = self.lookup(m, change)
        self.assertEqual(len(first["notices"]), 1)
        again = self.lookup(m, change)
        self.assertNotIn("notices", again)
        for field, value in change["set"].items():
            self.assertEqual(first[field], before[field])  # the result that carries the notice is already out of date
            self.assertEqual(again[field], value)

    def test_changes_are_announced_in_the_order_the_agent_reaches_them(self):
        m = Workflow(self.spec, seed=1, difficulty="expert")
        self.assertGreaterEqual(len(m.change_plan), 2)
        last = dict(m.change_plan[-1])
        self.assertIn("notices", self.lookup(m, last))
        # "the changes announced so far" is what grading by the facts at the time is built on
        self.assertEqual((m.epoch, m.change_plan[0]["on_read"]), (1, last["on_read"]))

    def test_a_careful_player_passes_and_one_that_ignores_the_notices_never_does(self):
        for level in ("standard", "hard"):
            for seed in range(1, 31):
                self.assertTrue(play(Workflow(self.spec, seed=seed, difficulty=level), "oracle").passed, (level, seed))
                self.assertFalse(play(Workflow(self.spec, seed=seed, difficulty=level), "stale").passed, (level, seed))

    def test_planned_timeouts_fall_on_calls_no_change_can_remove(self):
        # A change can turn an approval into a denial, so a timeout planned for "the third refund" could wait for a
        # refund that never happens. Timeouts are planned among the calls that are certain.
        for seed in range(1, 31):
            m = Workflow(self.spec, seed=seed, difficulty="standard")
            self.assertTrue(m.fault_plan)
            for fault in m.fault_plan:
                self.assertLessEqual(max(fault["at"]), m._sure_calls(fault["tool"]), seed)
            retried = play(Workflow(self.spec, seed=seed, difficulty="standard"), "blind_retry")
            self.assertFalse(retried.passed, seed)

    def test_it_refuses_the_combinations_it_cannot_guarantee_yet(self):
        mixed = copy.deepcopy(self.spec)
        del mixed["hazards"]["changes"][0]["when"]  # one timed change beside the first-read ones
        with self.assertRaises(ValueError) as caught:
            Workflow(mixed, seed=1, difficulty="standard")
        self.assertIn("cannot yet be mixed", str(caught.exception))

    def test_challenges_with_timed_changes_are_planned_as_before(self):
        m = Workflow(spec_of("gladiator/refund-queue@2.2.0"), seed=1, difficulty="standard")
        self.assertTrue(m.change_plan)
        self.assertTrue(all("on_read" not in ch for ch in m.change_plan))
        self.assertEqual([ch["at"] for ch in m.change_plan], [10, 16, 22, 30])  # after N calls, as sealed

    def test_refund_queue_2_3_0_passes_automatic_validation_and_its_record_is_for_this_content(self):
        self.assertEqual(validate(self.spec, only_levels=["standard", "expert"]), [])
        record = json.loads((ROOT / "validations" / f"{REF}.json").read_text(encoding="utf-8"))["body"]
        self.assertTrue(record["valid"])
        self.assertEqual(record["challenge"]["content_sha256"], packages.resolve(REF).manifest["content_sha256"])
        for level in ("standard", "hard", "expert", "marathon"):
            self.assertEqual(record["levels"][level]["players"]["stale"]["passed_seeds"], 0, level)


class SeveralOutcomeTests(unittest.TestCase):
    """The same guarantee where an item has several possible outcomes with details (incident-triage 1.3.0): a
    change may overturn the outcome (a service enters maintenance) or only the detail (who the alert is routed to)."""

    REF = "gladiator/incident-triage@1.3.0"

    def setUp(self):
        self.spec = spec_of(self.REF)

    def test_every_announced_change_overturns_an_outcome_or_its_details(self):
        kinds = set()
        for level in ("standard", "expert"):
            for seed in range(1, 16):
                m = Workflow(self.spec, seed=seed, difficulty=level)
                self.assertTrue(m.change_plan, (level, seed))
                for ch in m.change_plan:
                    after = copy.deepcopy(m.world0)
                    after[ch["table"]][ch["index"]].update(ch["set"])
                    new, new_args = m._truth(after)
                    outcome = [k for k in ch["touches"] if new[k] != m.expected[k]]
                    details = [k for k in ch["touches"] if new_args.get(k, {}) != m.expected_args.get(k, {})]
                    self.assertTrue(outcome or details, (level, seed))
                    kinds.add("outcome" if outcome else "details")
        self.assertEqual(kinds, {"outcome", "details"})  # both kinds of change really occur

    def test_a_careful_player_passes_and_one_that_ignores_the_notices_never_does(self):
        for level in ("standard", "hard"):
            for seed in range(1, 31):
                self.assertTrue(play(Workflow(self.spec, seed=seed, difficulty=level), "oracle").passed, (level, seed))
                self.assertFalse(play(Workflow(self.spec, seed=seed, difficulty=level), "stale").passed, (level, seed))

    def test_it_passes_automatic_validation_and_its_record_is_for_this_content(self):
        self.assertEqual(validate(self.spec, only_levels=["hard", "expert"]), [])  # every level: the record below
        record = json.loads((ROOT / "validations" / f"{self.REF}.json").read_text(encoding="utf-8"))["body"]
        self.assertTrue(record["valid"])
        self.assertEqual(record["challenge"]["content_sha256"], packages.resolve(self.REF).manifest["content_sha256"])
        for level in ("standard", "hard", "expert", "marathon"):
            self.assertEqual(record["levels"][level]["players"]["stale"]["passed_seeds"], 0, level)


if __name__ == "__main__":
    unittest.main()
