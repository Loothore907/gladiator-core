# Run record format (version 2)

Every match writes `runs/<run_id>/` with `record.json` (public, sealed), `operator.json` (private: hidden
faults, answer state) and `report.md`. Records are **permanent and versioned**: analytics built later read
records collected today (`docs/PRODUCT.md` section 7). `engine.record.load()` reads any version and upgrades it
in memory; the file and its seal never change.

## The seal
`record.json` is `{"sha256": ..., "body": {...}}`. The hash covers the canonical JSON of `body`, and
`body.operator_sha256` ties in `operator.json`. `python gladiator.py verify runs/<run_id>` re-checks both.

## `body`
| Field | What |
|---|---|
| `record_version` | Schema version (2). Absent = 1. |
| `engine_commit` | Git commit of the engine checkout that ran the match (the CI commit only when the engine has no checkout of its own). |
| `run_id`, `started_unix`, `wall_s` | Identity and timing. |
| `suite` | Name, version, path, sha256, node order, success rule, and each challenge's provenance (id, version, `content_sha256`, trust status at run time, tags, test type, licence). |
| `settings` | Suite defaults, overrides, per-contestant settings (and whether they were equal), repeats, the instance salt (both sides get identical worlds), per-node settings. |
| `contestants.A/B` | Exact model id, prices used per million tokens, the exact API parameters sent, and `results` (one per attempt, below). |
| `budget` | Run cap, per-contestant cap, spent in total and per side, whether the cap was reached. |
| `verdict` | Winner and how it was decided, pair wins by reason (success / progress / efficiency), the efficiency summary, reliability per side (tests passed on every repeat, pass^k), confidence (sign test), totals, and which attempts were excluded and why. |
| `operator_sha256` | Hash of the private operator record. |

## One attempt (`contestants.X.results[]`)
| Field | What |
|---|---|
| `node`, `attempt`, `name`, `module`, `challenge`, `instance` | Which test, which repeat, its provenance, and the instance (seed, difficulty). |
| `passed` | **Eventual success**: the grader's verdict at the end. |
| `first_pass` | **First-pass success**: passed, and no action of the model's own was rejected along the way (planted faults don't count against it). New in version 2. |
| `model_errors` | Tool results that were errors caused by the model's action (unknown record, missing fields, not enough stock). New in version 2. |
| `task_correct`, `workflow_correct` | End result right; valid process (None when not graded). |
| `termination` | Why it ended: `session_ended`, `action_limit`, `over_budget`, `time_limit`, `invalid_action`, `no_action`, or excluded: `budget_stop` (run cap), `provider_error` (outage), `not_run` (e.g. out of credit). |
| `calls`, `cost_usd`, `input_tokens`, `output_tokens`, `wall_s` | Effort. |
| `grade_details` | The grader's public details (mistake counts, confusions, per-item mistakes). |
| `public_packet`, `turns` | Everything the model saw and did, per turn: tool, arguments, observation, usage, cost, timing. |

**Cost of a call.** Each turn carries `cost_usd`, the cost of record, and `list_cost_usd`, tokens times the listed
price. Where a provider reports what it billed (`usage.billed_usd`; OpenRouter does), that is the cost of record, because
a router can bill the same model at different rates depending on the host that served the call. Otherwise the two are
equal. Records written before 2026-11-23 have no `list_cost_usd` and no `billed_usd`: their `cost_usd` is the list-price
estimate, which understated OpenRouter spend and overstated Claude Haiku 5.5 (cache reads were priced as plain input).

## Validation records
`gladiator validate <namespace>/<name>@<version> --write` saves `validations/<namespace>/<name>@<version>.json`:
the evidence behind a challenge version's trust (docs/PRODUCT.md section 6). Same seal as a run record
(`{"sha256": ..., "body": {...}}`). Validation makes no model calls, so the records are free to regenerate and
are committed to the repository.

| Field | What |
|---|---|
| `kind`, `record_version` | `"validation"`, schema version (2; see below for 1). |
| `challenge` | id, version and `content_sha256` of the exact package validated. |
| `engine_commit`, `validated_at` | Engine commit and UTC time. |
| `valid`, `problems` | The verdict and every problem found (empty when valid). |
| `levels.<level>` | `seeds` (the first look: the queues the reference player and every corner-cutting player were played on), `oracle_passed` (the reference player must pass every one), and `players`: for each corner-cutting player, `must_fail` (its hazard is present at this level), `seeds` (the queues it was played on), `passed_seeds` (how often it got through anyway), `allowed` (the share it may get through: 0.10, or 0.15 for `stale`) and `ok`. |

**How a corner-cutting player is judged (record version 2, 2026-11-24).** A player may get lucky in a generated
queue (a fault never fires, a near miss is not in its path), so what matters is how often. Twenty queues cannot tell
a player that gets through 1 time in 10 from one that gets through 3 times in 10, so luck used to pass or fail a
challenge. Now: every player is played on the first 20 queues. One that never got through is caught. One that got
through at least once is played on 100 queues, and on 300 if that still cannot tell: it is `ok` when the 95% range of
its share lies within the allowance and not `ok` when the whole range lies above it. After 300 queues a share that is
still not clearly above the allowance is `ok`: a challenge fails this check on evidence, not on a difference that
could be chance (at 300 queues that means a measured share about 3 to 4 points over the allowance). **Version 1 records** (before 2026-11-24) have no `seeds` or `allowed` per player: every player was judged
on the first 20 queues only (`ok` when at most 2 of 20, or 3 of 20 for `stale`). They are kept as written; a
challenge can be validated again at any time, for free.

## Changing the format
Add fields; never rename or drop one. Bump `RECORD_VERSION` in `engine/runner.py`, describe the change here,
and teach `engine.record.upgrade()` to derive the new fields from older records.
