# Gladiator v2: design

Status: approved 2026-11-20; Milestone 1 built. Changes from the original proposal:
- The ten v1 simulators are kept **byte-for-byte** in `simulators/` (checked against v1's
  SHA-256 pins before loading). One wrapper, `modules/v1_case.py`, plugs all ten into the
  engine, instead of ten separate module files. Custom modules still plug in as described below.
- An offline `scripted` provider (golden / invalid / silent) tests the whole pipeline for free.
- Prices live in `providers/prices.yaml`; a model with no price can't be used, since the cap
  couldn't be enforced.
- Max output tokens default to 4096 for every case. v1 used 512-2048, which would cut off
  models whose thinking counts against the same allowance.

## Repo layout

```
gladiator/
  gladiator.py            # the one command: uv run gladiator match ...
  config.example.yaml     # copy to config.yaml; models, $ cap, params
  .env.example            # ANTHROPIC_API_KEY=, OPENAI_API_KEY=, GOOGLE_API_KEY=
  suites/
    six_case_mvp.yaml     # Milestone 1
    ten_case.yaml
  modules/                # test modules = pluggable case simulators
    base.py               # the 3-method interface every module implements
    interval_union_sql.py
    dependency_reachability.py
    ... (one file per simulator, 10 total)
  providers/
    base.py               # one interface: send(messages, params) -> reply + usage + cost
    anthropic.py  openai.py  google.py
    prices.yaml           # $ per million tokens, per model (editable)
  scorers/
    components.py         # task_correct, workflow_correct, combined
  engine/
    runner.py             # runs both models in parallel, enforces $ cap
    budget.py             # one shared spend counter; stops cleanly at the cap
    record.py             # hashed JSON run record
  reports/
    markdown.py           # match table
  runs/                   # output (gitignored): <timestamp>/record.json + report.md
  tests/                  # offline tests using a scripted fake provider
```

## Suite file (node + link format)

```yaml
suite: six-case-mvp
version: 1
description: Stateful recovery workflows.

defaults:                 # identical for both contestants
  temperature: 0
  max_output_tokens: 512
  max_turns: 12

success:                  # how the match winner is decided
  primary: combined       # task_correct AND workflow_correct
  tiebreak: [task_correct, cost_usd, latency_s]

nodes:
  - id: snapshot-recovery
    module: snapshot_scan_recovery     # file in modules/
    params: {seed: 7}                  # module-specific knobs
    instructions:                      # one or more contestant-facing steps
      - Recover the scan from the last durable snapshot. One action per turn.

  - id: ticket-revocation
    module: ticket_revocation
    params: {seed: 3}

links:                    # order / dependencies between nodes
  - from: snapshot-recovery
    to: ticket-revocation
```

Plain English:
- **Node** = one test. It names a module (the simulator), optional settings, and the instructions the model sees.
- **Link** = "run this after that". For Milestone 1 links only set order; later they can pass state between nodes (that is what makes a suite a workflow spec someone can rebuild locally).
- No links = every node is independent.

## Module interface (how custom tests plug in)

```python
class Module:
    def public_packet(self) -> dict        # what the model sees (instructions, data)
    def step(self, action: dict) -> dict   # model's action -> simulator response
    def grade(self, transcript) -> dict    # {"task_correct": bool, "workflow_correct": bool, "notes": ...}
```
The private reference answer lives inside the module and never goes into a prompt
(contestant/operator separation). A new module = one new file in modules/.

## Run rules
- Both models get the same packet, params, and turn limit, run at the same time.
- Fresh conversation per model per case. No shared history.
- One `max_usd` in config. Spend is estimated before each call; if the next call
  could cross the cap, the run stops, marks remaining cases "not run", and still
  writes the report. No prompts, no approval gates.
- Keys from `.env` (gitignored). Never logged.
- Run record = JSON with config, suite hash, every prompt/response, scores, costs,
  plus a SHA-256 over the whole thing so tampering is detectable.

## Command
```
uv run gladiator match --suite suites/six_case_mvp.yaml \
    --a openai:gpt-6-luna --b anthropic:claude-sonnet-5-5 --max-usd 1
```
Output: `runs/2026-11-20T15-04/report.md` + `record.json`.

## Dependencies
Python 3.11+, `pyyaml`, `httpx` (or each provider's official SDK — see open question).
