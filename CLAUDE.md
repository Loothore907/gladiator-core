# Working on Gladiator (instructions for Claude sessions)

Gladiator is an open (Apache-2.0) head-to-head model evaluation engine: two models play the same suite under
identical settings, and the result is a claim about that task, with how sure we are.

## Product direction
Read `docs/PRODUCT.md` before designing features: vision (community library of trusted tests), MVP scope,
challenge packages, trust tiers and validation, run records, contamination policy, fairness rules and the
build order. It wins over `docs/V2_DESIGN.md` where they differ.

## How work lands
- One feature per branch, PR into `main`, CI must pass (lint, tests on Linux + Windows, Linux container).
- Linux is the official target; Windows is supported for local development.
- Setup, test levels and commands: `docs/DEVELOPMENT.md`. Hosting rules: `docs/DEPLOYMENT.md`.
- This repository holds no API keys and its CI is offline. Real matches need your own keys in a local `.env`.

## Rules
- Never edit `simulators/`: those files are pinned by SHA-256 to v1's exact bytes.
- Never edit a published challenge package in `challenges/`: changes go out as a new version or a fork
  (`forked_from` set), then `gladiator fingerprint --write` re-seals it. Trust status is not in the package:
  promote/demote/retire by editing `challenges/catalog.yaml` (with evidence), never the manifest.
- Dependencies change only through `uv.lock` (`uv lock`, commit the lock).
- No keys in code, commits, logs or chat. The local `.env` uses the providers' standard `..._API_KEY` names.
- Never commit `operator.json` (private simulator records) or anything under `runs/`.
- A model can only be used if it has a price (that's how the $ cap works): from `providers/prices.yaml`, or
  for `openrouter:` models, read live from OpenRouter's published price list (recorded in each run record).
- Never use floating aliases (models that silently route to another model): the report would name the wrong
  model.
- Private or holdout test cases must never run through a key enrolled in a provider's data-sharing program.
- A check that passes without testing anything is a bug (for example, the live smoke must fail, not skip).

## Fast checks before pushing
```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```
