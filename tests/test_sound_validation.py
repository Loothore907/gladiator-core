"""Offline tests for how automatic validation judges a corner-cutting player (docs/RUN_RECORD.md, "Validation
records"): on enough generated queues that luck can neither pass nor fail a challenge. No network, no cost.
"""

import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.validate import (
    MORE_SEEDS,
    SEEDS,
    VALIDATION_RECORD_VERSION,
    allowed_share,
    validate,
    validation_record,
    wilson,
    within_allowance,
)


def spec_at(path: str) -> dict:
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


class RuleTests(unittest.TestCase):
    def test_never_getting_through_is_caught_at_once(self):
        self.assertIs(within_allowance(0, 20, 0.10), True)

    def test_twenty_queues_cannot_tell_a_small_share_from_a_large_one(self):
        for got_through in (1, 2, 3, 6):  # 3 of 20 used to pass and 4 of 20 used to fail
            self.assertIsNone(within_allowance(got_through, 20, 0.15), got_through)
        self.assertIs(within_allowance(7, 20, 0.15), False)  # so often that even 20 queues are enough

    def test_more_queues_can(self):
        self.assertIs(within_allowance(8, 100, 0.15), True)  # the whole 95% range is within 15%
        self.assertIs(within_allowance(24, 100, 0.15), False)  # the whole range is above it
        self.assertIsNone(within_allowance(13, 100, 0.15))  # close to the line: play more

    def test_after_the_last_round_only_a_share_clearly_over_the_allowance_fails(self):
        self.assertIsNone(within_allowance(35, 300, 0.10))  # 11.7%, range 8.5% to 15.8%: could be chance
        self.assertIs(within_allowance(35, 300, 0.10, final=True), True)
        self.assertIs(within_allowance(58, 300, 0.15, final=True), False)  # 19.3%, range 15.3% to 24.2%

    def test_the_range_narrows_as_queues_are_added(self):
        narrow, wide = wilson(30, 300), wilson(2, 20)
        self.assertLess(narrow[1] - narrow[0], wide[1] - wide[0])
        self.assertTrue(narrow[0] < 0.10 < narrow[1])
        self.assertEqual((allowed_share("stale"), allowed_share("naive")), (0.15, 0.10))


class StagedSamplingTests(unittest.TestCase):
    def test_a_lucky_first_look_no_longer_passes(self):
        # refund-queue 2.2.0 at the standard level: the player that ignores announced changes got through 2 of the
        # first 20 generated queues, which the old rule accepted (3 allowed). Played on 100 it gets through 25.
        spec = spec_at("challenges/gladiator/refund-queue/2.2.0/workflow.yaml")
        report: dict = {}
        problems = validate(spec, only_levels=["standard"], report=report)
        self.assertEqual(
            problems, ["standard: bad player 'stale' passed 25/100 seeds, over the 15% allowed (grader too lenient)"]
        )
        players = report["standard"]["players"]
        self.assertEqual(
            players["stale"], {"must_fail": True, "passed_seeds": 25, "seeds": 100, "allowed": 0.15, "ok": False}
        )
        # A player that never got through is not played again, and one that did is played until it is clear.
        self.assertEqual((players["no_reading"]["seeds"], players["no_reading"]["ok"]), (len(SEEDS), True))
        self.assertEqual((players["blind_retry"]["seeds"], players["blind_retry"]["ok"]), (MORE_SEEDS[0], True))
        self.assertEqual(report["standard"]["oracle_passed"], report["standard"]["seeds"])  # still solvable

    def test_the_version_built_to_fix_it_passes(self):
        spec = spec_at("challenges/gladiator/refund-queue/2.3.0/workflow.yaml")
        report: dict = {}
        self.assertEqual(validate(spec, only_levels=["standard"], report=report), [])
        self.assertEqual(report["standard"]["players"]["stale"]["passed_seeds"], 0)

    def test_records_say_how_many_queues_each_player_was_judged_on(self):
        spec = spec_at("challenges/gladiator/refund-queue/2.3.0/workflow.yaml")
        provenance = {"id": "gladiator/refund-queue", "version": "x", "content_sha256": "y"}
        levels: dict = {}
        problems = validate(spec, only_levels=["standard"], report=levels)
        record = validation_record(spec, provenance, "abc", "2026-11-24T00:00:00Z", results=(problems, levels))
        self.assertEqual((record["record_version"], VALIDATION_RECORD_VERSION), (2, 2))
        for player in record["levels"]["standard"]["players"].values():
            self.assertIn("allowed", player)
            self.assertEqual(
                player["seeds"] >= len(SEEDS), player["must_fail"]
            )  # not played at all unless it must fail


if __name__ == "__main__":
    unittest.main()
