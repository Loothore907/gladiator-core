"""The build page on the runner (engine/serve.py `Forks`, engine/pages): describe a flow, settle the open choices,
create a test of one's own. What each visitor may do, what is counted, and the whole walk over HTTP."""

import io
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from engine import packages, serve

SIX = str(Path(__file__).resolve().parent.parent / "suites" / "six_case_mvp.yaml")
MODELS = ["scripted:golden", "scripted:silent"]
REFUNDS = "gladiator/refund-queue@2.3.0"
DESCRIPTION = "We refund customers for damaged or late orders within 30 days. VIP customers get 45 days."
READER = "openai:gpt-6-luna"
CHOSEN = {"refund_window_days": "30", "premium_window_days": "45", "premium_tier": "vip"}


class StubReader:
    """Stands in for the model that reads a description: answers what the test says, and reports a cost."""

    contestant = READER

    def __init__(self, answer, cost=0.0004):
        self.answer, self.cost, self.spent_usd, self.asked = answer, cost, 0.0, []

    def read(self, description, candidates, root):
        self.asked.append(description)
        self.spent_usd = self.cost
        return self.answer


def make(folder, codes=None, reader=None, **more):
    ledger = serve.Ledger(Path(folder) / "ledger.json", None, 3, by_provider={"scripted": 1.0, "openai": 0.05})
    races = serve.Races(SIX, MODELS, ledger, 0.2, codes, set(), runs_dir=Path(folder) / "runs")
    return serve.Forks(races, reader, levels=["easy"], **more)  # one light level keeps the checks to a moment


def finished(forks, job_id, seconds=120):
    until = time.time() + seconds
    while time.time() < until:
        job = forks.job(job_id)
        if job["state"] != "checking":
            return job
        time.sleep(0.2)
    raise AssertionError("the checks did not finish")


class WhoMayDoWhatTests(unittest.TestCase):
    def test_on_your_own_machine_everything_is_open_and_nobody_reads(self):
        with TemporaryDirectory() as tmp:
            status = make(tmp).status({})
            self.assertEqual((status["needs_invite"], status["can_create"], status["reads"]), (False, True, False))

    def test_a_visitor_who_is_not_signed_in_can_look_but_not_create_or_spend(self):
        with TemporaryDirectory() as tmp:
            forks = make(tmp, codes={"judge-1"}, reader=READER)
            read = StubReader({"ref": REFUNDS, "why": "Same job.", "values": {}, "not_covered": []})
            forks.make_reader = lambda: read
            status = forks.status({})
            self.assertEqual((status["signed_in"], status["can_create"], status["reads"]), (False, False, False))
            self.assertEqual((status["reads_when_signed_in"], status["reader"]), (True, None))
            found = forks.describe({"description": DESCRIPTION})
            self.assertEqual((found["proposed"], found["read_by"]), (REFUNDS, None))  # matched by its words
            self.assertEqual(read.asked, [])  # no model was asked: nothing was spent
            self.assertEqual(forks.review({"ref": REFUNDS, "values": CHOSEN})["problems"], {})
            with self.assertRaises(serve.Refused) as refused:
                forks.create({"ref": REFUNDS, "values": CHOSEN, "name": "Returns", "owner": "acme"})
            self.assertEqual(refused.exception.status, 403)
            self.assertFalse(forks.races.busy.locked())

    def test_creating_can_be_opened_to_everyone(self):
        with TemporaryDirectory() as tmp:
            self.assertTrue(make(tmp, codes={"judge-1"}, open_create=True).status({})["can_create"])

    def test_a_reader_without_a_price_is_refused_at_start_up(self):
        with TemporaryDirectory() as tmp, self.assertRaises(Exception):  # noqa: B017  no price, no cap, no use
            make(tmp, reader="openai:a-model-with-no-price")


class DescribeTests(unittest.TestCase):
    def test_the_closest_test_comes_back_with_its_questions_and_where_each_choice_lands(self):
        with TemporaryDirectory() as tmp:
            found = make(tmp).describe({"description": DESCRIPTION})
            test = found["test"]
            self.assertEqual((found["proposed"], test["ref"], len(test["questions"])), (REFUNDS, REFUNDS, 5))
            first = test["questions"][0]
            self.assertEqual(
                (first["name"], first["value"], first["min"], first["max"]), ("refund_window_days", 60, 7, 180)
            )
            self.assertGreater(test["places"]["refund_window_days"], 1)
            self.assertEqual(found["closer"], [])

    def test_nothing_is_proposed_for_a_flow_no_test_resembles(self):
        with TemporaryDirectory() as tmp:
            found = make(tmp).describe({"description": "teach my parrot to whistle sea shanties"})
            self.assertEqual((found["proposed"], found["test"], found["matches"]), (None, None, []))

    def test_a_closer_test_that_cannot_be_adapted_is_named_not_hidden(self):
        with TemporaryDirectory() as tmp:
            found = make(tmp).describe({"description": "review access requests for employees"})
            self.assertEqual(found["proposed"], REFUNDS)  # the only match with open choices, on one shared word
            self.assertEqual(found["closer"][0]["ref"].split("@")[0], "gladiator/access-review")

    def test_an_empty_or_endless_description_is_refused(self):
        with TemporaryDirectory() as tmp:
            forks = make(tmp)
            for text in ("", "   ", "refund " * 400):
                with self.assertRaises(serve.Refused) as refused:
                    forks.describe({"description": text})
                self.assertEqual(refused.exception.status, 400)

    def test_a_signed_in_visitors_description_is_read_by_the_model_counted_and_paid_from_its_budget(self):
        with TemporaryDirectory() as tmp:
            forks = make(tmp, codes={"judge-1"}, reader=READER, reads_per_code=2)
            answer = {
                "ref": REFUNDS,
                "why": "Both decide refund requests against a policy.",
                "values": {"refund_window_days": 30, "premium_tier": "vip", "refund_cap": 9},  # 9 is out of range
                "not_covered": ["store credit instead of money"],
            }
            readers = []

            def make_reader():
                readers.append(StubReader(answer))
                return readers[-1]

            forks.make_reader = make_reader
            found = forks.describe({"description": DESCRIPTION, "code": "judge-1"})
            self.assertEqual((found["read_by"], found["why"]), (READER, answer["why"]))
            by_name = {q["name"]: q for q in found["test"]["questions"]}
            self.assertEqual((by_name["refund_window_days"]["value"], by_name["premium_tier"]["value"]), (30, "vip"))
            self.assertEqual(by_name["refund_window_days"]["source"], "your description")
            self.assertEqual(by_name["refund_cap"]["value"], 500)  # a value outside the range is not taken
            self.assertEqual(len(found["test"]["unusable"]), 1)
            self.assertEqual(found["test"]["not_covered"], ["store credit instead of money"])
            ledger = forks.ledger
            self.assertAlmostEqual(ledger.state["read_usd"], 0.0004)
            self.assertAlmostEqual(ledger.left_for("openai"), 0.05 - 0.0004)  # the cap came back, the cost did not
            # the sign-in's reads run out: the description is still matched, by its words, and nothing is spent
            forks.describe({"description": DESCRIPTION, "code": "judge-1"})
            third = forks.describe({"description": DESCRIPTION, "code": "judge-1"})
            self.assertEqual((len(readers), third["read_by"], third["proposed"]), (2, None, REFUNDS))
            self.assertAlmostEqual(ledger.left_for("openai"), 0.05 - 0.0008)

    def test_a_reader_whose_budget_is_spent_is_not_asked(self):
        with TemporaryDirectory() as tmp:
            forks = make(tmp, codes={"judge-1"}, reader=READER, read_usd=0.06)  # the budget is 0.05
            forks.make_reader = lambda: self.fail("a read that does not fit in the budget must not start")
            found = forks.describe({"description": DESCRIPTION, "code": "judge-1"})
            self.assertEqual((found["read_by"], found["proposed"]), (None, REFUNDS))
            self.assertEqual(forks.ledger.uses_left("reads", serve.who_of("judge-1"), forks.reads_per_code), 30)


class ChoicesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.forks = make(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_each_value_is_checked_and_the_rules_come_back_with_the_values_marked(self):
        look = self.forks.review({"ref": REFUNDS, "values": CHOSEN})
        self.assertEqual((look["problems"], look["broken"]), ({}, []))
        self.assertEqual({c["name"] for c in look["changed"]}, set(CHOSEN))
        marked = [(p["choice"], p["text"]) for p in look["shown"]["rules"] if p.get("choice")]
        for pair in (("refund_window_days", "30"), ("premium_window_days", "45"), ("premium_tier", "vip")):
            self.assertIn(pair, marked)
        self.assertIn("--set refund_window_days=30", look["command"])
        self.assertTrue(look["command"].startswith(f"uv run gladiator fork {REFUNDS} --to "))

    def test_a_value_that_is_not_allowed_is_named_in_plain_words(self):
        look = self.forks.review({"ref": REFUNDS, "values": {"refund_cap": "lots", "prior_refund_limit": "40"}})
        self.assertIn("not a whole number", look["problems"]["refund_cap"])
        self.assertIn("allowed range", look["problems"]["prior_refund_limit"])
        self.assertEqual(look["values"]["refund_cap"], 500)  # the test's own value stands

    def test_values_that_do_not_fit_together_show_no_rules_until_they_do(self):
        look = self.forks.review({"ref": REFUNDS, "values": {"refund_window_days": "30", "premium_window_days": "20"}})
        self.assertIsNone(look["shown"])
        self.assertEqual(set(look["broken"][0]["choices"]), {"refund_window_days", "premium_window_days"})
        self.assertIn("premium", look["broken"][0]["say"].lower())

    def test_only_what_the_test_declares_can_be_set_and_only_offered_tests_can_be_forked(self):
        look = self.forks.review({"ref": REFUNDS, "values": {"simulator": "mine", "refund_cap": "250"}})
        self.assertIn("simulator", look["problems"])
        for ref in ("gladiator/refund-queue@2.2.0", "../../etc/passwd", "", None):
            with self.assertRaises(serve.Refused) as refused:
                self.forks.choose({"ref": ref})
            self.assertEqual(refused.exception.status, 400)
        with self.assertRaises(serve.Refused):
            self.forks.review({"ref": REFUNDS, "values": "refund_cap=250"})


class CreateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.forks = make(self.tmp.name, codes={"judge-1"}, creates_per_code=2)
        self.ask = {"ref": REFUNDS, "values": CHOSEN, "name": "Returns desk", "owner": "Acme", "code": "judge-1"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_checked_test_is_handed_over_as_a_download_and_nothing_joins_the_library(self):
        before = set(packages.catalog())
        job_id = self.forks.create(self.ask)["job"]
        job = finished(self.forks, job_id)
        self.assertEqual(
            (job["state"], job["ref"], job["forked_from"]), ("created", "acme/returns-desk@1.0.0", REFUNDS)
        )
        self.assertTrue(job["download"] and job["checked"])
        self.assertIn("perfect player", job["checked"][0])
        self.assertNotIn("archive", job)
        name, data = self.forks.download(job_id)
        self.assertEqual(name, "acme-returns-desk-1.0.0.zip")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.assertEqual(
                set(z.namelist()),
                {
                    "README.txt",
                    "challenges/acme/returns-desk/1.0.0/challenge.yaml",
                    "challenges/acme/returns-desk/1.0.0/workflow.yaml",
                    "validations/acme/returns-desk@1.0.0.json",
                    "suites/acme-returns-desk-1.0.0.yaml",
                },
            )
            self.assertIn(REFUNDS, z.read("challenges/acme/returns-desk/1.0.0/challenge.yaml").decode())
            self.assertIn("vip", z.read("challenges/acme/returns-desk/1.0.0/workflow.yaml").decode())
        self.assertEqual(set(packages.catalog()), before)
        self.assertFalse((packages.ROOT / "challenges" / "acme").exists())
        self.assertFalse(self.forks.races.busy.locked())  # the runner is free again
        self.assertEqual(self.forks.status({"code": "judge-1"})["creates_left"], 1)

    def test_what_is_refused_before_any_check_starts(self):
        for change, status in [
            ({"values": {"refund_cap": "lots"}}, 400),
            ({"values": {"refund_window_days": "30", "premium_window_days": "20"}}, 400),
            ({"owner": "Gladiator"}, 400),  # the library's own name
            ({"owner": "!!!"}, 400),
            ({"name": ""}, 400),
            ({"ref": "gladiator/refund-queue@2.2.0"}, 400),
            ({"code": "not-an-invite"}, 403),
        ]:
            with self.assertRaises(serve.Refused, msg=change) as refused:
                self.forks.create({**self.ask, **change})
            self.assertEqual(refused.exception.status, status, change)
        self.assertEqual((self.forks.jobs, self.forks.races.busy.locked()), ({}, False))
        self.assertEqual(self.forks.status({"code": "judge-1"})["creates_left"], 2)  # none of that used one up

    def test_one_thing_at_a_time_shared_with_races(self):
        self.forks.races.busy.acquire()
        try:
            with self.assertRaises(serve.Refused) as busy:
                self.forks.create(self.ask)
            self.assertEqual(busy.exception.status, 409)
            self.assertEqual(self.forks.status({"code": "judge-1"})["creates_left"], 2)
        finally:
            self.forks.races.busy.release()

    def test_a_sign_in_has_a_number_of_tests_it_may_create(self):
        self.forks.ledger.use("creates", serve.who_of("judge-1"), 2)
        self.forks.ledger.use("creates", serve.who_of("judge-1"), 2)
        with self.assertRaises(serve.Refused) as over:
            self.forks.create(self.ask)
        self.assertEqual(over.exception.status, 429)
        self.assertFalse(self.forks.races.busy.locked())

    def test_a_created_test_waits_for_its_download_for_a_while_then_is_gone(self):
        job_id = self.forks.create(self.ask)["job"]
        finished(self.forks, job_id)
        self.forks.jobs[job_id]["started"] -= serve.JOB_MINUTES * 60 + 1
        for look in (self.forks.job, self.forks.download):
            with self.assertRaises(serve.Refused) as gone:
                look(job_id)
            self.assertEqual(gone.exception.status, 404)


class PageTests(unittest.TestCase):
    """The page itself, over HTTP: what a browser is sent and the walk a visitor takes."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.forks = make(self.tmp.name, codes={"judge-1"})
        self.server = serve.serve(self.forks.races, "127.0.0.1", 0, self.forks)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def get(self, path):
        return urllib.request.urlopen(self.url + path, timeout=30)

    def post(self, path, ask):
        request = urllib.request.Request(
            self.url + path, json.dumps(ask).encode(), {"Content-Type": "application/json"}
        )
        return json.load(urllib.request.urlopen(request, timeout=30))

    def test_the_page_is_served_locked_down_and_loads_nothing_from_elsewhere(self):
        page = self.get("/build")
        html = page.read().decode()
        policy = page.headers["Content-Security-Policy"]
        for rule in ("default-src 'none'", "script-src 'self'", "connect-src 'self'", "frame-ancestors 'none'"):
            self.assertIn(rule, policy)
        self.assertEqual(page.headers["Referrer-Policy"], "no-referrer")  # an invitation link must not leak onward
        self.assertIn("Build a test for your own flow", html)
        # the policy forbids inline script and style, so the page must not depend on any
        for inline in ("<style", "style=", "onclick=", "onsubmit=", "javascript:"):
            self.assertNotIn(inline, html)
        self.assertEqual(html.count("<script"), 1)
        self.assertIn('<script src="/build.js">', html)
        script = self.get("/build.js").read().decode()
        self.assertIn("javascript", self.get("/build.js").headers["Content-Type"])
        self.assertIn("text/css", self.get("/build.css").headers["Content-Type"])
        # what the server says is put on the page as text, never as markup
        for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(unsafe, script)
        for path in ("/fork/status", "/fork/describe", "/fork/choose", "/fork/review", "/fork/create", "/fork/job"):
            self.assertIn(path, script)  # the page and the server agree on every address

    def test_a_runner_without_the_build_page_does_not_answer_for_it(self):
        bare = serve.serve(self.forks.races, "127.0.0.1", 0)
        threading.Thread(target=bare.serve_forever, daemon=True).start()
        try:
            for path in ("/build", "/fork/tests"):
                with self.assertRaises(urllib.error.HTTPError) as missing:
                    urllib.request.urlopen(f"http://127.0.0.1:{bare.server_address[1]}{path}", timeout=10)
                self.assertEqual(missing.exception.code, 404)
        finally:
            bare.shutdown()
            bare.server_close()

    def test_the_whole_walk_from_a_description_to_a_download(self):
        self.assertEqual([t["ref"] for t in json.load(self.get("/fork/tests"))["tests"]], [REFUNDS])
        self.assertFalse(self.post("/fork/status", {})["can_create"])
        token = self.post("/signin", {"invite": "judge-1"})["token"]
        self.assertTrue(self.post("/fork/status", {"token": token})["can_create"])
        found = self.post("/fork/describe", {"description": DESCRIPTION, "token": token})
        self.assertEqual(found["test"]["ref"], REFUNDS)
        self.assertEqual(self.post("/fork/choose", {"ref": REFUNDS})["name"], found["test"]["name"])
        look = self.post("/fork/review", {"ref": REFUNDS, "values": CHOSEN, "token": token})
        self.assertEqual((look["problems"], look["broken"]), ({}, []))
        with self.assertRaises(urllib.error.HTTPError) as refused:  # no sign-in, no creating
            self.post("/fork/create", {"ref": REFUNDS, "values": CHOSEN, "name": "Returns", "owner": "acme"})
        self.assertEqual(refused.exception.code, 403)
        self.assertIn("signed-in", json.load(refused.exception)["error"])
        ask = {"ref": REFUNDS, "values": CHOSEN, "name": "Returns desk", "owner": "acme", "token": token}
        job_id = self.post("/fork/create", ask)["job"]
        levels = set()
        for _ in range(600):
            job = json.load(self.get(f"/fork/job?id={job_id}"))
            if job["state"] != "checking":
                break
            levels.add(job["level"])
            time.sleep(0.2)
        self.assertEqual((job["state"], job["ref"]), ("created", "acme/returns-desk@1.0.0"))
        download = self.get(f"/fork/download?id={job_id}")
        self.assertEqual(download.headers["Content-Type"], "application/zip")
        self.assertIn("acme-returns-desk-1.0.0.zip", download.headers["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(download.read())) as z:
            self.assertIsNone(z.testzip())
        with self.assertRaises(urllib.error.HTTPError) as unknown:
            self.get("/fork/job?id=not-a-job")
        self.assertEqual(unknown.exception.code, 404)

    def test_a_long_description_in_any_alphabet_fits_in_one_request(self):
        found = self.post("/fork/describe", {"description": ("возврат refund 退款 " * 80)[:1190]})
        self.assertEqual(found["proposed"], REFUNDS)


if __name__ == "__main__":
    unittest.main()
