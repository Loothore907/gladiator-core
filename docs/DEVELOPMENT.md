# Development

Where things run:

| Place | Role |
|---|---|
| **Linux** | The official target. Benchmark environments, CI, containers, and the future enterprise build all run Linux. |
| **Windows** | Supported for local development, and checked in CI so it stays working. |
| **Mac** | Should work the same way as Linux. Not checked in CI. |

## First-time setup (Windows)

Run these once, in PowerShell.

1. **Install Git** from https://git-scm.com/download/win if you don't have it. The defaults are fine:
   the repo's `.gitattributes` file controls line endings, so Git's own setting doesn't matter.
2. **Install uv**, which manages Python and the packages:
   ```bash
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```
   Close and reopen PowerShell afterwards.
3. **Get the code**, in a normal folder (not OneDrive or another synced folder):
   ```bash
   git clone https://github.com/Loothore907/gladiator.git
   ```
   ```bash
   cd gladiator
   ```
4. **Install everything.** uv downloads the right Python (3.12) and the exact package versions in `uv.lock`:
   ```bash
   uv sync
   ```
5. **Check it works.** This is free and takes about 10 seconds:
   ```bash
   uv run pytest
   ```
   ```bash
   uv run gladiator match --suite suites/ten_case.yaml --a scripted:golden --b scripted:invalid --max-usd 1
   ```
   You should see `Winner: scripted:golden` and `10/10 | 0/10`.
6. **Add your keys.** Copy `.env.example` to `.env` and fill in the keys you have. `.env` is never committed.

## Everyday commands

| What | Command |
|---|---|
| Run the offline tests (free) | `uv run pytest` |
| Check style | `uv run ruff check .` then `uv run ruff format .` |
| Live smoke test (a few cents) | `GLADIATOR_LIVE=1 uv run pytest tests/live -v -s` (PowerShell: `$env:GLADIATOR_LIVE=1; uv run pytest tests/live -v -s`) |
| Real match | `uv run gladiator match --suite suites/six_case_mvp.yaml --a ... --b ... --max-usd 1` |
| Check a run wasn't edited | `uv run gladiator verify runs/<run_id>` |
| Update packages | `uv lock --upgrade`, then run the tests, then commit `uv.lock` |

## The three test levels

1. **Offline** (`tests/`): uses fake models, so no keys and no cost. CI runs it on every push. Any new feature
   needs a test here.
2. **Live smoke** (`tests/live/`, `suites/smoke.yaml`): two cases against each real provider, at most $0.10
   per model. Run it before a release, after upgrading packages, or when a provider changes its API. It
   checks the connection works, not how good the model is.
3. **Live match**: the real evaluation, whenever you choose.

## How changes land

1. Make a branch for one feature: `git switch -c feature/short-name`.
2. Commit, push, and open a pull request into `main`.
3. CI must pass: lint, the tests on Linux and Windows, and the Linux container. Linux is required.
4. Merge. `main` should always be in a working state.

### One-time GitHub setting: protect `main`

GitHub → repo **Settings → Branches → Add branch ruleset** (or "Add rule") for `main`:

- Require a pull request before merging.
- Require status checks to pass, and pick **Lint and format**, **Tests (ubuntu-latest)** and **Linux container**.
  Add **Tests (windows-latest)** too if you want Windows to block merges.
- Block force pushes.

### Live runs need your own keys

This repository's CI is offline: it never calls a model and holds no API keys. To run the live smoke test or a real
match, put your keys in a local `.env` (the providers' standard names: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
`GEMINI_API_KEY`, `OPENROUTER_API_KEY`) and use the commands above. The live smoke fails if a key is missing for any
model you list, so a pass always means real calls were made.

The project's own calibration matches run from a separate private operations repository, which holds the keys and
keeps each run's private simulator record (`operator.json`).

## Rules that keep things stable

- **Never edit `simulators/`.** The files are checked against v1's SHA-256 fingerprints and the engine refuses
  to load a changed file. New tests go in `modules/`.
- **Dependencies only change through `uv.lock`.** Commit it with any package change. CI uses
  `uv sync --frozen`, so a stale lock fails instead of quietly installing different versions.
- **No keys in code, commits, logs or chat.** Keys go in `.env` locally, GitHub secrets in CI, or the host's
  secret store when deployed.
