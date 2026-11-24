# Gladiator

Head-to-head model evaluation. Two models run the same suite at the same time
with identical settings; a winner is picked by the suite's success rule.

## Quick start

Setup uses [uv](https://docs.astral.sh/uv/). Full first-time steps, including Windows, are in
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

```bash
uv sync                      # exact Python + package versions from uv.lock
cp .env.example .env         # then paste in the API keys you need
uv run gladiator match --suite suites/six_case_mvp.yaml \
    --a openai:gpt-6-luna --b anthropic:claude-sonnet-5-5 --max-usd 1
```

Or as a Linux container (the benchmark / enterprise form; see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)):

```bash
docker build -t gladiator .
docker run --rm --env-file .env -v "$PWD/runs:/app/runs" gladiator match --suite suites/six_case_mvp.yaml --a ... --b ... --max-usd 1
```

The run goes to completion with no prompts. Results land in `runs/<run_id>/`:

| File | What it is |
|---|---|
| `report.md` | The match table: winner, per-case pass/fail, task vs workflow, calls, cost, time |
| `record.json` | Everything the models saw and did, grades, costs and exact API settings, sealed with a SHA-256 |
| `operator.json` | Private simulator records (hidden faults, answer state). Never shown to contestants |

`uv run gladiator verify runs/<run_id>` checks that neither file was edited after the run.

**Try it for free first.** Fake offline models: `scripted:golden` plays known-correct answers, `scripted:invalid` sends junk, and `scripted:silent` never answers.

```bash
uv run gladiator match --suite suites/ten_case.yaml --a scripted:golden --b scripted:invalid --max-usd 1
```

## Choosing models

Contestants are written `provider:model`. Providers: `anthropic`, `openai`, `google`, `openrouter`.
A model needs a price so the $ cap can be enforced:
- `anthropic`, `openai`, `google`: the price comes from `providers/prices.yaml`. Add a line there for any
  new model. `uv run gladiator list` shows what's priced, and `uv run gladiator models` shows which
  models your keys can actually use.
- `openrouter` (hundreds of open-source and hosted models, one key): the price is read live from
  OpenRouter's published price list and saved in the run record. Example:
  `openrouter:qwen/qwen3.7-flash`.

Both models get the suite's settings (`max_output_tokens`, `effort`). Override for both at once
with `--effort minimal|low|medium|high|xhigh|max|none` or `--max-output-tokens N`. Which effort levels a
model accepts is model-dependent; an unsupported level fails with the provider's error (`suites/probe.yaml`
is a cheap one-case suite for checking). Raise `--max-output-tokens` with high effort levels: the cap
includes reasoning tokens.

**How sure.** Both models play the same world on each attempt, so every report opens with a plain-English
"How sure" line: it counts the attempts where one model passed and the other failed, and says whether the
gap is a real difference or could still be luck. Pass rates come with a 95% range.

**Repeats.** One run of a case is a single coin flip. `--repeats 5` plays every case 5 times, each
with a fresh session, and the report shows pass rates (e.g. `4/5`). A provider failure that survives
the retries (an outage, "high demand") is left out of the scoring instead of counting as a loss. Use `--effort none` for models
that don't support a reasoning-effort setting (for example older Gemini models).

## The $ cap

`--max-usd` is the most the whole run can spend, split evenly: each model gets half. Before
every call, the engine works out the most that call could cost (input size rounded up, plus the
full output allowance). If that could push the model past its half, the call isn't sent. That
model's remaining cases are marked `not_run`, the run finishes, and the report is still written.
The winner is decided only on cases both models finished. Actual cost comes from each
provider's reported token usage.

## Layout

```
gladiator.py          the command
suites/               suite files (nodes + links), YAML or JSON
challenges/           locked, versioned challenge packages (the 10 v1 cases are the first official ones)
blueprints/           the open choices of a challenge version (what a fork may change), beside the sealed package
modules/              test types; v1_case.py (v1-simulator@1) runs the ten v1 simulators
simulators/           the v1 case simulators, byte-for-byte, with SHA-256 pins (PINS.json)
providers/            anthropic / openai / google adapters, one shared interface, prices.yaml
engine/               runner (parallel contestants), budget, suite loader, run records
scorers/              totals + winner rule
reports/              markdown report
tests/                offline tests (uv run pytest); tests/live = real-API smoke test, off by default
reference/            v1's Nov 14 match result, for comparison
docs/                 PRODUCT (vision, roadmap), V2_DESIGN (engine), DEVELOPMENT (setup, testing, PR flow), DEPLOYMENT (hosting)
.github/workflows/    CI on every push (lint, tests on Linux + Windows, container)
Dockerfile            the Linux container
```

## Challenges and test types

- A **challenge** is one locked, versioned test package in `challenges/<namespace>/<name>/challenge.yaml`
  (id, version, tags, trust tier, licence, instructions, and a fingerprint over every file it uses).
  `uv run gladiator challenges` lists them and checks each is intact.
- Several versions of a challenge can live side by side (`challenges/<namespace>/<name>/<version>/`), so a
  retired version stays runnable for history.
- Suites reference challenges as `namespace/name@version` and can only **configure** them (settings,
  repeats). Changing a challenge's content means a new version or a fork; the engine refuses an edited
  package. After an intended change, `uv run gladiator fingerprint <challenge.yaml> --write` re-seals it.
- `uv run gladiator fork <namespace/name@version> --to <namespace/new-name>` (a fork, `forked_from` set) or
  `--version X.Y.Z` (a new version) copies a challenge as the starting point for an edit; it starts as
  `unreviewed`. Edit, re-seal with `fingerprint --write`, then `validate`.
- **Forking by choices** (no editing): a test can declare its open choices in a blueprint
  (`blueprints/<namespace>/<name>@<version>.yaml`): named values such as a time limit or an amount cap, each with
  a question, a default and limits. `uv run gladiator blueprints "your flow in your own words"` finds the closest
  tests, `uv run gladiator choices <namespace/name@version>` lists what one lets you change, and
  `uv run gladiator fork <namespace/name@version> --to <yourname/your-test> --set <choice>=<value>` writes a new
  sealed test, its validation record and a one-test suite. A value is written once and lands in the grading rule,
  the wording the model reads and the near-miss records together. Values outside the declared choices are
  refused, and nothing is written unless automatic validation passes. Structure (records, tools, decisions,
  obstacles) cannot be changed this way.
- **`uv run gladiator chat`** walks the same path as a conversation: you describe your flow, it shows the closest
  test and asks about each thing that test lets you change (press Enter to keep a value), shows the rules a model
  will be told with your answers in them, and then creates the sealed, validated test. With
  `--model <provider:model>` a model reads your description first: it says which test is the same kind of job and
  which choices your words already settle. That is all it does. Its answers come back inside a schema built from
  the declared choices, the engine checks them again, and you confirm every value. Reading is capped at 5 cents
  (`--max-usd`); without `--model` the chat needs no key and costs nothing.
- **The same conversation as a page:** `uv run gladiator serve` and open `http://127.0.0.1:8787/build`. Four
  steps: describe your flow, confirm the closest test, make your choices, name it and create it. Beside the
  choices the page shows the rules a model will be told, with each of your values marked where it lands, and it
  updates as you type. Creating runs the same automatic validation and hands you the sealed test, its validation
  record and a suite file as one download; nothing is added to the library. On a hosted runner, creating is for
  signed-in visitors unless the runner is started with `--open-create`; anyone else is shown the exact
  `gladiator fork` command for their choices.
- `uv run gladiator validate <namespace/name@version>` checks a `workflow@1` challenge automatically: an
  oracle player must pass it and a set of deliberately bad players must fail it, at every difficulty.
- A **test type** is the engine code that runs a kind of challenge (`modules/`, registered in
  `modules/__init__.py`, e.g. `v1-simulator@1`). New test types are core work; new challenges are data.

See `docs/PRODUCT.md` for the full model: trust tiers, validation, forks and the build order.

## License

Apache-2.0. See [LICENSE](LICENSE).
