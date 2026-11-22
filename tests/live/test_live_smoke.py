"""Live smoke test: does each real provider still work end to end?

Run:  GLADIATOR_LIVE=1 uv run pytest tests/live -v
Models: GLADIATOR_SMOKE_MODELS (comma-separated), default below. The test
FAILS if a listed model's provider key is not set (it never silently skips). Total spend is capped at
GLADIATOR_SMOKE_MAX_USD (default $0.10) per model.

This checks plumbing (keys, network, request/response shapes, tool-result
threading, usage and cost accounting), NOT model quality: the model may
pass or fail the cases.
"""

import os
import sys
from pathlib import Path
from unittest import mock

import pytest
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from engine import runner  # noqa: E402
from engine import suite as suites  # noqa: E402

DEFAULT_MODELS = "anthropic:claude-haiku-5-5,openai:gpt-6-luna"
KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}
MODELS = [m.strip() for m in os.environ.get("GLADIATOR_SMOKE_MODELS", DEFAULT_MODELS).split(",") if m.strip()]
CAP = float(os.environ.get("GLADIATOR_SMOKE_MAX_USD", "0.10"))


@pytest.mark.live
@pytest.mark.parametrize("contestant", MODELS)
def test_provider_end_to_end(contestant):
    provider = contestant.split(":", 1)[0]
    if not os.environ.get(KEYS.get(provider, "")):
        # Fail, never skip: a smoke test that passes without calling anything is worse than none.
        pytest.fail(
            f"{KEYS.get(provider)} is not set, so {contestant} was not tested. "
            "Set the key, or remove this model from GLADIATOR_SMOKE_MODELS."
        )
    with mock.patch("builtins.print"):
        # The real model plays side A; the free offline golden player is side B.
        body, _ = runner.run_match(suites.load(str(ROOT / "suites" / "smoke.yaml")), contestant, "scripted:golden", CAP)
    results = body["contestants"]["A"]["results"]
    for r in results:  # show what the model sent when a case ended early (diagnosis, not a failure)
        if r["termination"] != "session_ended" and r["turns"]:
            last = r["turns"][-1]
            print(f"\n  {contestant} / {r['name']}: ended by {r['termination']} after {r['calls']} calls")
            print(f"    last action: {str(last.get('action_json'))[:400]}")
            print(f"    observation: {str(last.get('observation'))[:400]}")
    for r in results:
        assert r["termination"] not in ("provider_error", "not_run", "budget_stop"), (r["name"], r["error"])
        assert r["calls"] >= 1
        assert r["input_tokens"] > 0 and r["output_tokens"] > 0, "provider did not report usage"
    assert results[0]["calls"] >= 2 or results[0]["termination"] != "session_ended", "multi-turn path not exercised"
    spent = body["budget"]["spent_by"]["A"]
    assert 0 < spent <= CAP / 2
    print(f"{contestant}: OK, ${spent:.5f}, " + ", ".join(f"{r['name']}={r['termination']}" for r in results))
