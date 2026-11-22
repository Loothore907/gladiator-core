# Frontier test: design options (draft for the owner)

Status: **approved 2026-11-21: A + C; built as `gladiator/refund-reconciliation` 1.0.0** (hard, expert, marathon;
validated offline; suite `suites/frontier.yaml`). Probed 2026-11-21: sol and astra at max effort made 0 wrong
decisions (expert and full marathon); only recovery failed. 1.2.0 adds escalation (docs/RESEARCH_2026-11.md),
the current frontier candidate, not yet probed. Probe budget:
set in December from what the work needs (the OpenAI free credits will likely be smaller then).
Follows `docs/PRODUCT.md` ("What wins: efficiency", the `frontier` badge).

## 1. Why one test

The core track is efficiency on flows that strong models can finish (small and mid-tier models under a hard
budget). The owner also wants **one test that can stump the top models** (gpt-6.2-astra, gemini-4-neon,
Claude), so the library has a ceiling and earns the `frontier` badge. One test, not a race: it gets the
same packaging, validation and versioning as the rest, and is retired like any other test if it saturates.

## 2. What the calibration says does *not* stump them

At max effort, top models made every decision right on all four workflow tests, at expert and at marathon,
with announced mid-task changes, coupled stock and planted instructions (catalog ledger, 2026-11-21). More
items, more exceptions and more length did not separate them. Only recovery discipline did (at low effort).
So "harder" has to mean a different kind of skill, not more of the same.

## 3. Options

### A. Conflicting sources (reconciliation)
Two records that should agree sometimes don't (an order total vs the payment ledger, an HR status vs the
directory). The brief states which source wins for which field, and that conflicts must be reported
(`flag_conflict`) before deciding. Conflicts are present from the start (never silent changes), and some
are only visible by joining a third table.
- *Tests:* noticing what nobody points out; applying a source-of-truth rule consistently at scale.
- *Engine work:* small. Generated conflict variants on existing tables, a `flag_conflict` effect, grading
  of flags (missed or false).
- *Risk:* top models may simply be careful enough. Unknown until probed.

### B. Planning under a tight action budget
The action budget is set close to the oracle's minimum (about 1.05x instead of 1.2x), and batch tools
exist (`get_orders(ids=[...])`) that save actions only if the model plans its reads. Running out ends the
attempt (`action_limit`), so wasted reads cost the finish line.
- *Tests:* planning before acting; the efficiency story at its sharpest.
- *Engine work:* medium. Batch read ops, an oracle that computes the minimum plan, validation that a
  careful-but-unplanned player fails.
- *Risk:* it can feel like a puzzle about the tool surface rather than the work.

### C. Decisions that change later rules (running quotas)
Earlier decisions change what's right later: "refunds you issue today count toward the customer's limit of
3", "a reviewer can approve at most 5 access grants per day", with requests in a stated order. Like coupled
stock, but the coupled quantity is implicit (the model has to keep its own tally across 40-100 items).
- *Tests:* keeping state the tools don't show, over a long horizon.
- *Engine work:* small. It reuses `resources`, `consumes` and ordered replay, with the counter kept hidden.
- *Risk:* coupled stock (visible) didn't stump them. A hidden counter is harder, but by how much is unknown.

## 4. Recommendation

**A + C in one test** (a reconciliation queue where today's decisions also count toward a hidden quota),
built as a new challenge at `expert` and `marathon`, validated offline (no spend). Then one probe run on a
top model before deciding whether it earns the badge. B is worth keeping in mind as an efficiency-profile
variant later, not as the frontier test.

Cost: a probe needs a top model at high effort. Budget it like a marathon run (December budget, owner's call).

## 5. Decisions for the owner

1. Which option (A, B, C, or A + C as recommended)?
2. The probe budget in December, and which top model(s) to probe with.
