"""One hard dollar cap for the whole run, split evenly between the contestants.

Each contestant may spend at most half the cap. (A shared pot would let the
faster model use up the money before the slower one got its turn.)

How it works, in plain terms:
  1. Before every model call, estimate the WORST-CASE cost of that call:
     (estimated input tokens x input price) + (max output tokens x output price).
  2. If money already spent + money held for in-flight calls + this worst case
     would go over the cap, the call is NOT made. The run winds down cleanly.
  3. After the call, replace the hold with the actual cost from the provider's
     reported token usage.

Because every call is checked against its worst case before it is sent, the
total should never cross the cap. (The one estimate is input size, which is
deliberately rounded up: 1 token per 2.5 characters.)
"""

import datetime
import threading
from pathlib import Path

import yaml

PRICES_FILE = Path(__file__).resolve().parent.parent / "providers" / "prices.yaml"
CHARS_PER_TOKEN = 2.5  # conservative; real JSON is usually ~3-4 chars/token
OVERHEAD_TOKENS = 600  # tool definition + message framing


def load_prices():
    return yaml.safe_load(PRICES_FILE.read_text(encoding="utf-8")) or {}


def price_for(provider: str, model: str, today: datetime.date | None = None) -> dict:
    """Price for a model on a given day (default: today, UTC).

    An entry can change on a date:   {input, output, until: 2026-11-30, then: {input, output}}
    or stop being usable:            {input, output, until: 2026-11-30, then: retired}
    """
    table = load_prices().get(provider) or {}
    if model not in table and provider == "openrouter":
        # OpenRouter publishes exact per-token prices; read them live instead of copying them by hand.
        from providers.openrouter_api import live_price

        live = live_price(model)
        live.setdefault("cached_input", live["input"])
        return live
    if model not in table:
        raise ValueError(
            f"No price for {provider}:{model}. Add it to providers/prices.yaml so the $ cap can be enforced."
        )
    today = today or datetime.datetime.now(datetime.UTC).date()
    p = dict(table[model])
    while "until" in p and today > _as_date(p["until"]):
        nxt = p.get("then")
        if nxt == "retired":
            raise ValueError(
                f"{provider}:{model} is retired as of {_as_date(p['until']) + datetime.timedelta(days=1)}."
            )
        if not isinstance(nxt, dict):
            raise ValueError(f"providers/prices.yaml: {provider}:{model} has 'until' without a valid 'then'.")
        p = dict(nxt)
    p.pop("until", None)
    p.pop("then", None)
    p.setdefault("cached_input", p["input"])
    return p


def _as_date(value) -> datetime.date:
    return value if isinstance(value, datetime.date) else datetime.date.fromisoformat(str(value))


def cost_usd(
    price: dict, input_tokens: int, cached_input_tokens: int, output_tokens: int, cache_write_tokens: int = 0
) -> float:
    """Input tokens split three ways: read from cache, written to cache (`cache_write` price, default the
    input price), and plain; plus output tokens (which include reasoning)."""
    plain = max(0, input_tokens - cached_input_tokens - cache_write_tokens)
    return (
        plain * price["input"]
        + cached_input_tokens * price["cached_input"]
        + cache_write_tokens * price.get("cache_write", price["input"])
        + output_tokens * price["output"]
    ) / 1_000_000


def call_cost(price: dict, usage) -> tuple[float, float]:
    """(cost of record, list-price estimate) for one call. The cost of record is what the provider says it
    billed, when it says; otherwise the estimate. Both are kept, so a record shows any gap between them."""
    estimate = cost_usd(
        price, usage.input_tokens, usage.cached_input_tokens, usage.output_tokens, usage.cache_write_tokens
    )
    billed = getattr(usage, "billed_usd", None)
    return (estimate if billed is None else float(billed)), estimate


def next_input_tokens(input_chars: int, last_usage=None, new_chars: int = 0) -> int:
    """Estimate the next call's input tokens.

    Before the first call: characters in the conversation / CHARS_PER_TOKEN (conservative).
    After it: the provider's own count of the previous call's input, plus that call's output (it becomes
    history), plus the new message, plus overhead. Character counting badly overestimates long agent
    conversations on some providers (OpenAI's history carries encrypted reasoning that isn't billed as
    input: 113k estimated vs 9k billed on 2026-11-21), which made the cap stop attempts early. Never above
    the character-based estimate.
    """
    by_chars = int((input_chars + new_chars) / CHARS_PER_TOKEN) + OVERHEAD_TOKENS
    if last_usage is None:
        return by_chars
    by_count = last_usage.input_tokens + last_usage.output_tokens + int(new_chars / CHARS_PER_TOKEN) + OVERHEAD_TOKENS
    return min(by_chars, by_count)


class BudgetExhausted(Exception):
    pass


class Budget:
    """The budget for ONE contestant (its half of the run cap)."""

    def __init__(self, cap_usd: float):
        self.cap = float(cap_usd)
        self.spent = 0.0
        self.held = 0.0
        self.refused = False
        self._lock = threading.Lock()

    def worst_case(
        self, price: dict, input_chars: int, max_output_tokens: int, input_tokens: int | None = None
    ) -> float:
        """Most the next call could cost. `input_tokens`, when known, is a better estimate of the input than
        counting characters (see next_input_tokens); the character count is the fallback."""
        est_input = input_tokens if input_tokens is not None else int(input_chars / CHARS_PER_TOKEN) + OVERHEAD_TOKENS
        # Worst case: every input token billed at the dearest input rate (a cache write can cost more than input).
        dearest = {**price, "input": max(price["input"], price.get("cache_write", price["input"]))}
        return cost_usd(dearest, est_input, 0, max_output_tokens)

    def hold(self, amount: float) -> float:
        with self._lock:
            if self.refused or self.spent + self.held + amount > self.cap:
                self.refused = True  # once this contestant stops, it stays stopped
                raise BudgetExhausted(
                    f"next call could cost up to ${amount:.4f}; spent ${self.spent:.4f} "
                    f"+ in-flight ${self.held:.4f} of ${self.cap:.2f} cap"
                )
            self.held += amount
            return amount

    def settle(self, held: float, actual: float):
        with self._lock:
            self.held -= held
            self.spent += actual

    def release(self, held: float):
        """The call failed before the provider charged anything we can see."""
        with self._lock:
            self.held -= held
