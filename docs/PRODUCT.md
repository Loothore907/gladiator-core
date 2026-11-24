# Gladiator: product direction

Status: agreed with the owner on 2026-11-20. This is the source of truth for *what* we are building and
*why*. `V2_DESIGN.md` describes the engine as built for Milestone 1. When the two disagree, this file wins.

---

## 1. Vision

An **open, community-driven library of trusted tests** that anyone can draw on, plus the analytics to show
what models can actually do.

- Two models that look comparable on standard benchmarks meet **head to head** on a chosen suite, under
  identical, disclosed conditions. The result says which one is better, and **how sure we are**.
- Anyone can **build tests that mirror their real workflows**, run them against specific models, and
  publish them so others can use, fork, or reconfigure them.
- Over time the library can **emulate any industry-accepted benchmark** the scaffolding can handle.
- **Trust is the product.** A result is only worth something if people believe the test is sound, the run
  was fair, and the record wasn't altered.

Two front ends come later (an enterprise view and a gamified "battle" mode) on the same engine.

### What wins: success, then efficiency (owner decisions, 2026-11-21)

The question Gladiator answers is **which model is best at a given workflow**. Both models play the same
world on every attempt, and each pair of attempts is judged in this order (`scorers/outcome.py`):

1. **Success first.** If one model succeeds and the other fails, that is a clear win.
2. **Both succeed:** the more **efficient** one wins: fewest mistakes, smallest cost, fewest tokens, shortest
   time (`scorers/efficiency.py`; per pair each component is divided by the pair's mean and weighted by the
   suite's profile: `balanced`, `budget` or `latency`).
3. **Neither succeeds:** the one that **got furthest** wins (the larger share of items decided correctly);
   equal progress goes to efficiency.
4. **No real difference** (efficiency within 1%; times within 2 seconds count as equal, so machine and
   network jitter never decide): a tie, stated as a tie. No winner is made up.

Most pairs won takes the match; level pair wins go to mean efficiency, and if that is within 1% too the match
is a draw. For capable models that both succeed, the question becomes **what it takes**: mistakes, money,
tokens and time. Cheapness never beats a correct result.

- **The sweet spot** is small and mid-tier models on a specific flow with limited compute and a hard budget
  ("which model does this flow, within my budget, most cheaply and reliably?"). Suites can set a hard
  per-attempt budget (`attempt_budget_usd`, `attempt_budget_tokens`); running out counts as not finishing.
- **No arms race.** Most tests are not tuned to stump the newest frontier model: deep complexity is there so
  differences show up in mistakes, tokens, cost and time. That caps maintenance and keeps Gladiator in its
  lane rather than chasing frontier-capability benchmarks.
- **A frontier track still exists.** A few tests (at least one) are kept hard enough to stump the top
  models; they carry the `frontier` badge (section 6). Top-tier difficulty is one badge, not the success
  metric for every test.

## 2. Core concepts

| Term | Meaning |
|---|---|
| **Test type** | A kind of test the engine knows how to run, e.g. interactive simulator, multi-tool workflow, single question with checks, code with executable tests. **Core code**, reviewed by maintainers, added rarely. Emulating a new benchmark family usually means adding a test type. |
| **Challenge** | One test: a **package** of data *for* a test type (cases or a generator recipe, simulated tools, checks, answer keys). **Community content**, contributed by anyone, never contains code we have to trust. |
| **Suite** | A list of challenge references plus run configuration. The thing you actually run in a match. |
| **Instance** | One concrete copy of a challenge played in one attempt. Fixed challenges always produce the same instance; generator challenges produce a fresh one each time. |
| **Match** | Two contestants (provider:model) play the same suite under the same settings and caps. |
| **Run record** | The sealed, versioned record of a match: what was played, by whom, under which settings, with every score. |

The key split is **test types (few, trusted code) vs challenges (many, community data)**. It keeps the library
safe (no stranger's code runs) and scalable (thousands of challenges, a few dozen test types), and it lets us
add benchmark families without refactoring.

## 3. MVP scope

Deliberately narrow, but built on the formats the full library needs.

- **Identity: agent reliability.** Three starting categories, stored as **tags**, not a fixed list, so new
  areas never require a schema change:
  - `coding-repair`: change code in a bounded environment and pass executable checks (modelled on the
    SWE-bench / HumanEval family).
  - `stateful-workflow`: use tools to do a job in a simulated system; the grader checks what actually
    changed, not what the model says (modelled on the tau-bench family).
  - `recovery`: something goes wrong mid-task; notice, recover, and finish without duplicating work or
    corrupting state (our own; no well-known benchmark covers it).
- **Target: 12 official challenges, 4 per category.** Plus custom examples showing the build loop.
  Custom challenges are not a fourth capability score; they carry whatever tags fit.
- **Prompt injection** (text inside the data that tries to give the model orders) is tested as hostile
  *variants* of stateful-workflow challenges, not as a separate category.

### Where the existing cases stand (honest inventory)

| Category | Challenges we have | Notes |
|---|---|---|
| coding-repair | interval-union SQL, dependency reachability SQL, quota-prefix SQL | need 1 more |
| recovery | snapshot scan, batch checkpoint, ticket revocation | need 1 more |
| stateful-workflow | none yet | the single-answer cases below are not tool-using workflows |
| (state reasoning) | transaction, release plan, release rollback, watermark | one answer from a log; keep as official challenges tagged `state-reasoning`, but they don't fill the workflow category |

All ten are **fixed instances**, and they are public, so assume
they may end up in training data. **Calibration (2026-11-20) settled it:** gpt-6.2-sol (high effort) and gpt-6.2-astra (medium) both
passed all ten, and gpt-6-luna passed every attempt it made. They are saturated at the top, so all ten are
now `community` **warm-ups**, not official candidates. The official catalog will be filled by harder,
generator-based challenges (section 5) that must pass the difficulty gate.

## 4. Challenge packages

Every challenge, official or community, is a package with a manifest. Sketch (field names may still change
before the first release; the *concepts* are fixed):

```yaml
id: gladiator/snapshot-scan-recovery     # namespace/name; namespace = author or org
version: 1.0.0                           # semver; any content change = new version
test_type: interactive-simulator@1       # which test type, and its interface version
tags: [recovery]
emulates: null                           # or e.g. "tau-bench (style)", disclosed openly
instances: fixed                         # fixed | generator
difficulty: [standard]                   # generators may offer several levels
forked_from: null                        # e.g. gladiator/snapshot-scan-recovery@1.0.0
license: Apache-2.0                      # contributor licence; respects emulated benchmarks' licences
authors: [gladiator]
limits: {max_actions: 16, max_output_tokens: 4096}
components: [task_correct, workflow_correct]
content_sha256: <fingerprint of every file in the package>
```

Rules:
- **Locked content.** The fingerprint covers every file. If anything changes, it's a new version or a
  fork, never a silent edit. The engine refuses to run a package whose files don't match.
- **Configurable, not editable.** A suite can choose challenges, a subset of cases, difficulty, repeats,
  caps and model settings. It can never change a challenge's content.
- **Forks never change the original.** Editing an official challenge produces a new package with
  `forked_from` set and trust reset to `unreviewed`. Lineage stays traceable forever.
- **Public vs private parts.** What the model sees (the public packet) is kept separate from answer keys and
  hidden state (operator data). Validation checks that nothing private leaks into the public part.

## 4a. A test for your own flow: describe, match, choose, fork

How someone gets a test for *their* workflow without writing one (owner decisions, 2026-11-24). It extends the
loop Gladiator Ledger already walks (describe your flow, find the closest tested workflow, pick two models, read
the verdict, take the spec):

1. **Describe.** The person says what their flow is, in their own words.
2. **Match.** The closest tests in the library are found from the tests' own words (name, brief, rules). What
   matched is shown, and when nothing matches that is said. A match is never invented.
3. **Exact fit: use the test as it is.** Its results already exist.
4. **Close fit: fork it by its choices.** A test can declare its **open choices** (a *blueprint*): named values
   such as a time limit, an amount cap or what a customer tier is called, each with a question in plain words, a
   default and limits. The person is asked only for the choices their description did not already settle.
5. **Validate and seal.** The fork is produced from the blueprint and the chosen values, validated automatically
   (section 6) and sealed as a new versioned package, with `forked_from` and the chosen values recorded. Nothing
   is written unless validation passes. Like any new package it starts as `unreviewed`, with its validation
   record as evidence.
6. **Rejoin the loop.** Two models race on it, and the verdict and the spec follow as for any test.

Rules:
- **Users change values; structure is ours.** A fork by choices cannot add or remove a record type, a tool, a
  decision or an obstacle: those are the blueprint's structure and stay exactly as tested. A flow that needs a
  different structure needs a new blueprint, which is authored and reviewed like any test (section 6). The
  library grows by platform-authored blueprints for common flows.
- **One value, written once.** A chosen value lands everywhere it matters: the rule that grades, the wording the
  model reads, every rewording of it and the near-miss records. The wording and the grading rule cannot drift
  apart on a chosen value.
- **A blueprint is not sealed content.** It sits beside the library, like trust status and validation records.
  With its defaults it must reproduce the sealed test it names exactly, and that is checked, so the choices
  provably describe that test and nothing else.
- **The conversation is bound to the choices.** A model may run the conversation (read the description, propose
  the match, suggest values, ask for what is missing), but it works through the same three operations as
  everyone else: find, list the choices, fork with values. The engine refuses anything outside the declared
  choices and does the validating and sealing itself, so nothing said in a conversation can change a test's
  structure or skip a check.
- **Private by default.** A person's own forks stay theirs unless they choose to share them. Sharing is
  encouraged, never required. Platform-authored blueprints are public.

## 5. Fixed cases and generators

Official challenges may be either kind:

- **Fixed:** the same instance every run. Simple, easy to review, but memorisable and contaminable.
- **Generator:** the *recipe* is frozen and versioned, and each run produces a fresh, checked-solvable
  instance (seeded, so any run can be reproduced exactly). Repeats then give real statistical confidence,
  and memorising one instance doesn't help.

"Frozen" therefore means **same package version, same recipe, same difficulty**, not necessarily the same
data. Generators earn more trust and are preferred for anything that decides a published result.

### Difficulty levels

A generator offers named levels. The names mean the same thing across every challenge, so a suite can
say "expert" and know what it gets:

| Level | Purpose | What it must contain |
|---|---|---|
| `easy` | warm-up, smoke tests, weak-model comparisons | the core job only; no hazards required |
| `standard` | everyday comparison | at least one of each hazard the challenge has; a few near misses |
| `hard` | separates mid from strong models | every hazard several times; near misses on every rule |
| `expert` | the competitive level: separates comparable strong/frontier models | the largest world, the most faults and planted instructions, near misses on every rule *and exception*, a budget near what a careful agent needs |
| `marathon` | long-horizon consistency | 2-3x expert's size, several announced mid-task changes, more faults; tests whether models stay careful over hundreds of actions (expensive to run) |

Rules:
- **Levels live inside the sealed package.** Changing what a level means is a new version.
- **The gate applies to the top level.** A challenge's top level must pass the difficulty gate (section
  6) before the challenge can be `candidate`. Lower levels are useful but never the evidence.
- **Validation runs at every level.** The oracle must pass everywhere. Each bad player must fail wherever
  its hazard is present, including a *naive* player that applies the rule without its exceptions.
- **When the top saturates, add a level or a version, never a quiet edit.** The saturated version is
  `retired` and stays runnable (several versions of a challenge live side by side in the library).
- **Announced changes are graded by the facts at the time.** A decision made before a change's notice is
  graded on the old facts, one made after on the new ones; silent changes are never used.
- **What makes a level hard should be skill, not trickery.** Every rule a model is graded on is stated in
  the brief or policy. Difficulty comes from scale, exceptions, near misses, faults and budgets.

## 6. Trust status, validation and admission

**Trust is not sealed content.** A challenge's status is a judgement that changes as evidence comes in, so it
lives in `challenges/catalog.yaml` (keyed by `id@version`, with notes and links to evidence), never in the
sealed manifest. Promoting, demoting or retiring a challenge edits only that file; the content and its
fingerprint stay the same.

| Status | Meaning, and how a challenge gets there |
|---|---|
| `unreviewed` | Anyone can create or fork one. Runs locally; clearly labelled. The default when a challenge has no catalog entry. |
| `community` | Passed **automated validation** (below). Public and usable by anyone. |
| `candidate` | Proposed for the official catalog; **calibration** and human review in progress. |
| `official` | Passed calibration **and** review; maintained by the project, part of the curated catalog. |
| `retired` | Outgrown (saturated) or found flawed. Still runnable for history, always flagged. |

**Automated validation** (machines do most of the work):
1. The manifest and files are valid for the declared test type.
2. A **reference solution passes**, so the challenge is solvable as stated.
3. Known-bad behaviour **fails** (e.g. invalid actions, skipping a required step), so the grader actually
   grades.
   A corner-cutting player can get lucky in a generated world, so how often it gets through is measured on
   enough worlds that luck cannot pass or fail a challenge (20 first; a player that got through at least once
   is played on 100, then 300 if that still cannot tell). The allowance is 10% (15% for the player that ignores
   announced changes), and a challenge fails when its share is clearly over it. Details: `docs/RUN_RECORD.md`, "Validation records".
4. **No leaks**: nothing from operator data appears in the public packet.
5. Generators: a sample of instances is valid and solvable.

**Calibration: the efficiency gate.** We only canonise tests that **separate models on what matters for a
workflow**. A candidate runs against a **calibration ladder** of models with repeats (centred on small and
mid-tier models, with frontier models as the reference rung):

| Rung | Examples (Nov 2026) |
|---|---|
| Cheap | glm-5.3-flash, gpt-oss-20b |
| Mid | gpt-6-luna, claude-haiku-5-5 |
| Strong | gpt-6.2-sol (high effort), claude-sonnet-5-5 |
| Frontier | gpt-6.2-astra, claude-opus-5-5 / fable |

Admission to `official` requires all of:
- **Separates models:** paired attempts show a consistent winner under the victory rule (success, then
  efficiency or progress) between models or settings
  (the report's sign test reaches "a real difference" over the calibration repeats), and better models
  generally score better. A test everyone passes still qualifies if it separates them on mistakes, cost,
  tokens and time; a test where everyone ties on everything doesn't.
- **Stable:** results hold across repeats.
- **Clean:** no grading surprises in transcripts, and no **shared mistakes** (both models making the same
  wrong call on the same item usually means an ambiguous rule; see incident-triage 1.0.0).

**The `frontier` badge** (catalog note, alongside the status): the test's top level makes the best models
fail a real share of attempts (roughly 30-80% pass). It is earned by calibration against the frontier rung
and kept for at least one test per category where feasible; it is not required for `official`.

Challenges whose top level everyone aces at a fraction of the cost differences that matter are still useful;
they are retired only if they stop separating models on efficiency or are found flawed. Unequal per-contestant settings (e.g. each model at its best
effort) are allowed for calibration and are always flagged in the record and report.

**Human review** checks what machines can't: does the challenge measure what it claims, are the
instructions fair, is the licence right.

Every report states the status and exact version of everything it ran. Unreviewed or candidate content is
never presented as official.

## 7. Run records and analytics

Run records are **versioned and permanent**, so analytics built later can use results collected today.

Every record stores:
- the suite and each challenge's id, version, fingerprint, tier, and instance seed
- each contestant's exact model id, provider, prices used, and the exact API settings sent
- every component score per attempt: first-pass correctness, eventual success, task vs workflow, time, cost
- why each attempt ended, and what was excluded from the verdict and why (cap, outage, out of credit)
- a SHA-256 seal, plus the hash of the private operator record

Analytics this enables, built later on top of the same records:
- **Head-to-head verdicts with confidence** ("A is better at X%" or "no real difference yet").
- **Capability profiles** per model and tag.
- **Ratings** across many head-to-heads (chess-rating style).
- **Trends**: how a model changes across versions and over time.
- **Cost per solved challenge**, not just price per token.

## 8. Contamination policy

Public tests end up in training data eventually; for an open library that is unavoidable. We manage it:
- **Generators** keep instances fresh.
- **Private holdout tier**: challenges that are never published and only run through keys that don't share
  data with a model provider. Results we publish lean on these.
- **Retirement**: challenges whose scores suggest memorisation (e.g. a sudden jump on fixed instances
  that generators don't show) are retired to a lower tier.
- Holdout challenges never run through a key enrolled in a provider's data-sharing program.

## 9. Fairness rules (all matches)

- Both contestants get the same challenge version, instances, limits, instructions and caps, disclosed
  in the record.
- Each contestant gets a fresh session per attempt; nothing carries over.
- Settings parity: **identical settings** by default. An optional "each at its vendor-recommended
  settings" mode is allowed if it's recorded and labelled.
- No floating model aliases: the report must name the model that actually ran.
- Provider outages, cap cut-offs and out-of-credit stops are **excluded** from the verdict and listed,
  never counted as a loss.

## 10. Build order

| Step | What | Why first |
|---|---|---|
| **M1** (done) | Match engine, 10 v1 simulators, 4 providers + OpenRouter, CI/CD, Match and smoke workflows, repeats, fair outage handling | proves the loop |
| **M2a** (done) | Package format + manifest; repackage the 10 v1 cases as the first official packages (no behaviour change); suites become package references + config | every later step builds on it |
| **M2b** (done: confidence statements; run-record format version 2 with first-pass vs eventual success, `docs/RUN_RECORD.md`) | Versioned run-record schema (section 7); first-pass vs eventual success; confidence statement in reports | results collected from now on stay usable |
| **M2a.1** (done) | Trust status moved out of sealed content into `challenges/catalog.yaml`; v1 cases re-tiered to `candidate`; per-contestant settings for calibration | honest labels before anything is canonised |
| **Calibration** (done for v1) | Ran the v1 candidates up the ladder (gpt-6.2-sol high, gpt-6.2-astra medium): all 10/10, so all ten are warm-ups. Real cost: about $0.11 (sol) and $0.39 (astra) per ten-case pass | sets the bar: new challenges must make these models fail |
| **M2c** (design: `docs/M2C_WORKFLOW_DESIGN.md`; in progress: `workflow@1`, `refund-queue` 2.0.0 and `access-review` 1.0.0 built with `expert` levels. Calibration 2026-11-21: strong models make every per-item decision correctly even with exceptions; only recovery after faults separates them, and access-review saturates at the top. Multi-outcome decisions built with `incident-triage` 1.0.0 and coupled resources with `inventory-transfer` 1.0.0, completing the four M2c themes; then announced mid-task changes and a `marathon` level in new versions of all four (refund-queue 2.1.0, access-review 1.1.0, incident-triage 1.1.0, inventory-transfer 1.1.0), `docs/M2C_MULTI_OUTCOME_DESIGN.md`) | **Multi-tool workflow test type** (named tools, simulated tool data, task + workflow checks), written as files, **generator-first with difficulty levels**; the first test type the community can author for | fills `stateful-workflow`; the custom-workflow path for testers |
| **M2d** (done: `gladiator validate` for workflow@1, `gladiator fork`) | Local validation pipeline (section 6 automated checks) + `gladiator fork` | makes "unreviewed → community" mechanical |
| **M3** (in progress: recovery family built as `job-queue-recovery`; from `docs/RESEARCH_2026-11.md`: reliability (pass^k) in reports, wording variants in every workflow test, an escalation outcome in `refund-reconciliation` 1.2.0. Next: December calibration on small and mid-tier models, then promotions; the coding family needs a code sandbox) | Harder generator-based versions of the coding and recovery families; calibration runs; fill the official catalog to 4/4/4; prompt-injection variants | decisive head-to-heads between comparable models |
| **M4** (design agreed 2026-11-24, section 4a; first slice done: `engine/blueprint.py`, `gladiator blueprints`, `choices` and `fork --set`, with `refund-queue` 2.3.0 as the first blueprint; and the conversation that drives them, `gladiator chat` (`engine/session.py`), in the terminal, with an optional model that only reads the description; and the same conversation as a page on the runner, `/build` (`engine/serve.py`, `engine/pages/`), which hands the created test over as a download. Next: blueprints for the other workflows, a person's own space for their forks) | A test for your own flow: describe, match to the closest test, fork it by its declared choices, validated and sealed | lets a person test *their* flow without authoring, on structure that is already trusted |
| **Later** | Hosted registry and publishing, accounts, ratings and dashboards, sandboxed code-execution test types, the two front ends | all sit on top of packages + run records |

## 11. Decisions log

| Date | Decision |
|---|---|
| 2026-11-20 | Product is a community library with trust tiers; the MVP is narrow but built on extensible formats. |
| 2026-11-20 | Test types (core code) vs challenges (community data) is the extension boundary. |
| 2026-11-20 | Categories are tags; MVP starts with coding-repair, stateful-workflow, recovery. |
| 2026-11-20 | Official challenges may be fixed or generators; generators preferred for published results. |
| 2026-11-20 | "Emulate" means original challenges in a benchmark's style, labelled as such; real benchmark data only where its licence allows. |
| 2026-11-20 | Configurable, not editable: suites configure; edits create versioned forks with lineage. |
| 2026-11-20 | Trust status lives outside sealed content (`challenges/catalog.yaml`); v1 cases are candidates, not official. |
| 2026-11-20 | Difficulty gate: official challenges must not saturate strong/frontier models (calibration ladder). Generators and difficulty levels come forward. |
| 2026-11-21 | Efficiency is the core score (fewest mistakes, least cost, tokens and time), with suite profiles and hard per-attempt budgets; the official gate is "separates models on efficiency"; top-tier difficulty becomes the `frontier` badge for a few tests. The sweet spot is small and mid-tier models on a flow under a hard budget. |
| 2026-11-21 | Victory rule: success first; both succeed, efficiency; neither, furthest progress; ties broken by efficiency; no real difference is a declared tie. The frontier test combines conflicting sources with a hidden running quota (A + C in `docs/FRONTIER_DESIGN.md`). |
| 2026-11-21 | Four standard difficulty levels (easy, standard, hard, expert) with shared meanings; the gate applies to the top level; retired versions stay runnable side by side. |
| 2026-11-24 | A person gets a test for their own flow by describing it, matching it to the closest test in the library and forking that test by its declared open choices (a blueprint); the fork is validated and sealed as a new versioned test (section 4a). |
| 2026-11-24 | Users change a blueprint's declared values only. Structure (records, tools, decisions, obstacles) changes only by authoring a new blueprint. A person's forks may stay private; platform-authored blueprints are public. |
| 2026-11-24 | Validation must not be decided by luck: a corner-cutting player that gets through at least once in the first 20 generated worlds is played on more (100, then 300), and the challenge fails when that share is clearly over the allowance. Announced changes must always matter (`when: first_read`); refund-queue 2.3.0 is the first version built that way. Existing versions and their records are left as they are. |
