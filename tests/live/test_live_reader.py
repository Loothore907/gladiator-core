"""Live check of the one model step in `gladiator chat`: a real model reads a description (engine/session.py).

Run:  GLADIATOR_LIVE=1 uv run pytest tests/live -v -s
Models: GLADIATOR_SMOKE_MODELS (comma-separated), as for the live smoke. The test FAILS if a listed model's
provider key is not set (it never silently skips). Each model makes two small calls, capped at 5 cents in all.

It checks that the reader's two tools work with the real provider (the schemas are accepted, the answers come back
inside them, the cost is counted) and the two things the step is useless without: a plain refund description is
matched to the refund test, and the numbers it states are read off correctly. What each model understood is
printed, to be read.
"""

import os
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from engine import session  # noqa: E402

DEFAULT_MODELS = "anthropic:claude-haiku-5-5,openai:gpt-6-luna"
KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}
MODELS = [m.strip() for m in os.environ.get("GLADIATOR_SMOKE_MODELS", DEFAULT_MODELS).split(",") if m.strip()]
CAP = 0.05

REFUNDS = "gladiator/refund-queue@2.3.0"
DESCRIPTION = (
    "We run an online shop. An agent goes through our refund requests: it looks up the order and the customer, then "
    "approves or denies each request. A refund is allowed up to 30 days after payment, or 45 days for customers on "
    "our vip tier. A single refund never pays out more than 250. We would also like refunds to go out as store "
    "credit."
)


@pytest.mark.live
@pytest.mark.parametrize("contestant", MODELS)
def test_a_real_model_reads_a_description(contestant):
    provider = contestant.split(":", 1)[0]
    if not os.environ.get(KEYS.get(provider, "")):
        # Fail, never skip: a live check that passes without calling anything is worse than none.
        pytest.fail(f"{KEYS.get(provider)} is not set, so {contestant} was not tested.")
    reader = session.ModelReader(contestant, CAP)
    talk = session.ForkSession(reader=reader)
    found = talk.describe(DESCRIPTION)
    print(f"\n  {contestant}: proposed {found['proposed']}; why: {found['why']}; cost ${reader.spent_usd:.5f}")
    assert found["note"] is None, found["note"]  # the model was reached and answered
    assert found["read_by"] == contestant
    assert 0 < reader.spent_usd <= CAP
    assert found["proposed"] == REFUNDS

    chosen = talk.choose(REFUNDS)
    understood = {q["name"]: q["value"] for q in chosen["questions"] if q["source"] == "your description"}
    print(f"    understood: {understood}")
    print(f"    not covered: {chosen['not_covered']}")
    print(f"    could not use: {chosen['unusable']}")
    assert chosen["unusable"] == []  # every value it gave was inside the declared choices
    assert understood.get("refund_window_days") == 30
    assert understood.get("premium_window_days") == 45
    assert understood.get("refund_cap") == 250
    # "never pays out more than 250" is the payout cap. It says nothing about which orders can be refunded at all.
    assert "order_limit" not in understood
    assert "prior_refund_limit" not in understood  # the description does not state it, so it must not be filled in
