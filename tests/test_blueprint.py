"""Offline tests for forking by choices (engine/blueprint.py; docs/PRODUCT.md, "Forking by choices").

Describe a flow, find the closest test, change only what it leaves open, get a sealed test. No network, no cost.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import blueprint, packages
from engine.validate import plain_problems, plain_summary, validate

REFUNDS = "gladiator/refund-queue@2.3.0"


def sealed_spec(ref: str) -> dict:
    pkg = packages.resolve(ref)
    return yaml.safe_load((ROOT / pkg.manifest["params"]["spec"]).read_text(encoding="utf-8"))


class BlueprintTests(unittest.TestCase):
    def setUp(self):
        self.bp = blueprint.load(REFUNDS)

    def test_every_blueprint_is_exactly_its_sealed_test_at_the_defaults(self):
        # The promise a blueprint makes: its choices describe that sealed test and nothing else.
        known = blueprint.library()
        self.assertIn(REFUNDS, known)
        for ref, bp in known.items():
            self.assertEqual(blueprint.problems(bp, sealed_spec(ref)), [], ref)

    def test_a_blueprint_that_drifts_from_its_sealed_test_is_reported(self):
        drifted = dict(self.bp, template=self.bp["template"].replace("page_size: 5", "page_size: 6"))
        self.assertIn("tools", " ".join(blueprint.problems(drifted, sealed_spec(REFUNDS))))
        unused = dict(
            self.bp, variables={**self.bp["variables"], "spare": {"ask": "?", "type": "word", "default": "x"}}
        )
        self.assertIn("never used", " ".join(blueprint.problems(unused, sealed_spec(REFUNDS))))

    def test_one_value_lands_in_the_rule_the_wording_and_the_near_misses(self):
        values, bad = blueprint.check_values(self.bp, {"refund_window_days": "30", "premium_window_days": 45})
        self.assertEqual(bad, [])
        spec = yaml.safe_load(blueprint.render(self.bp, values))
        task = spec["task"]
        self.assertIn("days_since(order.paid_on) <= 30 or", task["policy"])  # what grades
        self.assertIn("paid 30 days ago or less", task["policy_text"])  # what the model reads
        for wording in spec["wordings"]:  # and every rewording of it
            self.assertIn("30 days", wording["policy_text"])
            self.assertNotIn("60 days", wording["policy_text"])
        near = spec["hazards"]["distractor_variants"]
        self.assertIn({"paid_on": {"days_ago": 30}}, near)  # the last day that still passes
        self.assertIn({"paid_on": {"days_ago": 31}}, near)  # the first day that fails
        self.assertEqual(spec["validation"]["naive_policy"].count("<= 30"), 1)  # the corner-cutting rule too
        self.assertEqual(
            blueprint.changed(self.bp, values),
            [
                {"name": "refund_window_days", "from": 60, "to": 30},
                {"name": "premium_window_days", "from": 90, "to": 45},
            ],
        )

    def test_only_the_declared_choices_are_accepted_and_each_refusal_is_a_sentence(self):
        cases = {
            "not one of this test's choices": {"page_size": 50},
            "outside the allowed range": {"refund_cap": 5},
            "not a whole number": {"refund_cap": "lots"},
            "one plain word": {"premium_tier": "gold'] or True or ['"},
            "cannot be used as a name": {"premium_tier": "yes"},
            "must be longer than the standard": {"refund_window_days": 90, "premium_window_days": 60},
            "different name from the standard tier": {"premium_tier": "standard"},
            # the payout cap and the order limit are separate choices, within limits of each other
            "at most 4 times the payout cap": {"refund_cap": 100},  # the order limit stays at 500
            "at most twice the order limit": {"refund_cap": 1200},
        }
        for expected, given in cases.items():
            _, bad = blueprint.check_values(self.bp, given)
            self.assertEqual(len(bad), 1, given)
            self.assertIn(expected, bad[0])

    def test_a_placeholder_cannot_run_code(self):
        risky = dict(self.bp, template="x: {{__import__('os').getcwd()}}")
        with self.assertRaises(Exception) as caught:
            blueprint.render(risky, blueprint.defaults(self.bp))
        self.assertIn("allowed", str(caught.exception))

    def test_changed_choices_still_pass_automatic_validation(self):
        # Values well away from the defaults, and the two ends of a range: the fork is still solvable, and every
        # corner-cutting player is still caught, including the one that ignores an announced change.
        for given in (
            {"refund_window_days": 14, "premium_window_days": 30, "prior_refund_limit": 2, "refund_cap": 100}
            | {"order_limit": 100},
            {"refund_window_days": 7, "premium_window_days": 8},
            {"refund_cap": 50000, "order_limit": 50000, "prior_refund_limit": 6},
            # the two amounts apart, at each end of what is allowed: a cap well under the order limit, and over it
            {"refund_cap": 250, "order_limit": 1000},
            {"refund_cap": 500, "order_limit": 250},
        ):
            values, bad = blueprint.check_values(self.bp, given)
            self.assertEqual(bad, [])
            report: dict = {}
            spec = yaml.safe_load(blueprint.render(self.bp, values))
            self.assertEqual(validate(spec, only_levels=["standard", "hard"], report=report), [], given)
            self.assertEqual(report["hard"]["players"]["stale"]["passed_seeds"], 0, given)
        self.assertIn("A perfect player got everything right in 40 of 40", plain_summary(report)[0])

    def test_the_payout_cap_and_the_order_limit_are_separate_choices(self):
        # One value used to set both. A person who meant only "never pay out more than 250" also moved the order
        # amount above which only damaged or undelivered orders are refunded.
        values, bad = blueprint.check_values(self.bp, {"refund_cap": 250})
        self.assertEqual(bad, [])
        spec = yaml.safe_load(blueprint.render(self.bp, values))
        self.assertEqual(spec["task"]["amount_rule"], "min(order.amount, 250)")
        self.assertIn("order.amount <= 500 or request.reason in", spec["task"]["policy"])
        rules = " ".join(spec["task"]["policy_text"].split())
        self.assertIn("(4) The order amount is 500 or less.", rules)
        self.assertIn("an order over 500 may still be approved", rules)
        self.assertIn("never more than 250 (an approved order of 390 is refunded 250)", rules)
        self.assertIn("order.amount <= 500", spec["validation"]["naive_policy"])
        for wording in spec["wordings"]:  # every rewording says the same two numbers
            text = " ".join(wording["policy_text"].split())
            self.assertEqual((text.count("500"), text.count("250")), (2, 2), text)
        # the near-miss records sit at the order limit, and one damaged order is over both amounts
        amounts = [v.get("amount") for v in spec["hazards"]["distractor_variants"] if "amount" in v]
        self.assertEqual(amounts[:3], [501, 640, 500])
        self.assertEqual(spec["world"]["orders"]["fields"]["amount"], {"range": [5, 750]})
        self.assertEqual(blueprint.changed(self.bp, values), [{"name": "refund_cap", "from": 500, "to": 250}])

    def test_a_failed_check_is_said_in_plain_words(self):
        levels = {
            "hard": {
                "seeds": 20,
                "oracle_passed": 19,
                "players": {
                    "stale": {"must_fail": True, "passed_seeds": 25, "seeds": 100, "allowed": 0.15, "ok": False}
                },
            }
        }
        said = plain_problems(levels)
        self.assertIn("a perfect player could not finish 1 of 20", said[0])
        self.assertIn("keeps using records from before an announced change got through 25 of 100", said[1])
        self.assertIn("clearly more than the 15% allowed", said[1])


class DescribeThenMatchTests(unittest.TestCase):
    def test_a_described_flow_finds_the_closest_test_and_says_what_matched(self):
        found = blueprint.find("an agent that handles refund requests for damaged orders")
        self.assertEqual(found[0]["ref"], REFUNDS)
        self.assertIn("refund", found[0]["matched"])
        self.assertEqual(found[0]["choices"], len(blueprint.load(REFUNDS)["variables"]))

    def test_nothing_is_invented_when_nothing_matches(self):
        self.assertEqual(blueprint.find("teach my parrot to whistle sea shanties"), [])

    def test_retired_versions_are_never_offered(self):
        for row in blueprint.find("refund queue access review alert triage warehouse stock job queue"):
            self.assertNotEqual(row["status"], "retired")


class ForkByChoicesTests(unittest.TestCase):
    """The fork itself, in a throwaway copy of the library (so nothing is written into the repository)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = packages.resolve(REFUNDS)
        rel = self.src.path.relative_to(packages.ROOT)
        shutil.copytree(self.src.path, self.tmp / rel)
        self.src.path = self.tmp / rel  # pretend the repo lives in tmp
        self.bp = blueprint.load(REFUNDS)

    def test_refused_values_write_nothing(self):
        result = blueprint.fork(self.src, self.bp, "myteam/returns", given={"refund_cap": 1}, root=self.tmp)
        self.assertEqual((result["ok"], result["stage"]), (False, "choices"))
        self.assertFalse((self.tmp / "challenges" / "myteam").exists())

    def test_a_fork_is_sealed_with_its_lineage_choices_evidence_and_suite(self):
        # With the blueprint's own values the fork is the sealed test under a new name, so validation passes exactly
        # as it does for the source (one level here, to keep the test quick).
        result = blueprint.fork(
            self.src, self.bp, "myteam/returns", root=self.tmp, only_levels=["expert"], name="Returns desk"
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["ref"], result["changed"]), ("myteam/returns@1.0.0", []))
        manifest = yaml.safe_load((self.tmp / result["written"]["test"] / "challenge.yaml").read_text(encoding="utf-8"))
        self.assertEqual(manifest["forked_from"], REFUNDS)
        self.assertEqual(manifest["name"], "Returns desk")
        self.assertIn(f"({REFUNDS}) with these choices changed: none", manifest["description"])
        self.assertEqual(manifest["choices"], {"of": REFUNDS, "values": blueprint.defaults(self.bp)})
        self.assertEqual(manifest["content_sha256"], packages.fingerprint(manifest, self.tmp))  # sealed as written
        spec_file = self.tmp / manifest["params"]["spec"]
        self.assertEqual(yaml.safe_load(spec_file.read_text(encoding="utf-8")), sealed_spec(REFUNDS))
        evidence = json.loads((self.tmp / result["written"]["evidence"]).read_text(encoding="utf-8"))
        self.assertTrue(evidence["body"]["valid"])
        self.assertEqual(evidence["body"]["challenge"]["content_sha256"], manifest["content_sha256"])
        suite = yaml.safe_load((self.tmp / result["written"]["suite"]).read_text(encoding="utf-8"))
        self.assertEqual(suite["nodes"], [{"id": "returns", "challenge": "myteam/returns@1.0.0"}])
        self.assertEqual(suite["defaults"]["difficulty"], "standard")


class CommandTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "gladiator.py"), *args], capture_output=True, text=True, encoding="utf-8"
        )

    def test_choices_lists_what_can_change_in_plain_words(self):
        out = self.run_cli("choices", REFUNDS)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("How many days after payment can an order still be refunded?", out.stdout)
        self.assertIn("now: 60 days, 7 to 180", out.stdout)
        as_json = json.loads(self.run_cli("choices", REFUNDS, "--json").stdout)
        self.assertEqual([c["name"] for c in as_json["choices"]], list(blueprint.load(REFUNDS)["variables"]))

    def test_blueprints_matches_a_description(self):
        out = self.run_cli("blueprints", "refund", "requests", "for", "damaged", "orders", "--json")
        matches = json.loads(out.stdout)["matches"]
        # Two refund tests match these words equally well; the one that can be forked by choices is listed first.
        self.assertEqual(matches[0]["ref"], REFUNDS)
        self.assertEqual(matches[0]["score"], matches[1]["score"])
        self.assertEqual((matches[0]["choices"] > 0, matches[1]["choices"]), (True, 0))

    def test_fork_refuses_a_value_outside_the_choices_before_doing_anything(self):
        out = self.run_cli("fork", REFUNDS, "--to", "myteam/never-made", "--set", "refund_cap=1")
        self.assertEqual(out.returncode, 1)
        self.assertIn("outside the allowed range, 20 to 50000", out.stdout)
        self.assertIn("Nothing was written.", out.stdout)
        self.assertFalse((ROOT / "challenges" / "myteam").exists())


if __name__ == "__main__":
    unittest.main()
