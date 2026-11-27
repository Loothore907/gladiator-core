"""One interface for every model provider.

The runner talks to a model like this:

    chat = provider.open_chat(system=..., first_message=..., settings=...)
    reply = chat.ask()                     # model answers the first message
    reply = chat.answer_tool(observation)  # send the tool result, model answers again
    ...

Each Chat is a fresh conversation: one per model per case, never reused.

The model always has exactly one tool, `submit_action(action_json: string)`.
A Reply carries the action the model submitted (or None if it did not call
the tool), plus token usage so the budget can be settled.
"""

import json
import re
from dataclasses import dataclass, field

TOOL_NAME = "submit_action"
TOOL_DESCRIPTION = (
    "Submit exactly one action or answer for the case. "
    "action_json must be a JSON-encoded object, as described in the case."
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {"action_json": {"type": "string", "description": "One JSON-encoded action or answer object."}},
    "required": ["action_json"],
    "additionalProperties": False,
}


def extract_action(args) -> str:
    """Turn the tool-call arguments into the action string the simulator receives.

    The contract is submit_action(action_json="<JSON string>"). Anthropic and OpenAI enforce that
    with strict schemas; Gemini does not, and may send the action as an object instead of a string.
    The intent is unambiguous either way, so all providers are normalised the same way here:
      {"action_json": "<string>"} -> the string, unchanged
      {"action_json": {...}}      -> that object, JSON-encoded
      anything else               -> the whole arguments object, JSON-encoded (the simulator judges it)
    """
    if isinstance(args, dict) and "action_json" in args:
        value = args["action_json"]
        return value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
    return json.dumps(args, separators=(",", ":"))


@dataclass(frozen=True)
class ToolSpec:
    """A tool the model may call, in a provider-neutral form. Each adapter translates it.

    strict=True asks providers that support it to enforce the schema exactly. Only use it when the
    schema lists every property as required and sets additionalProperties: false.
    """

    name: str
    description: str
    parameters: dict
    strict: bool = False


#: The single generic tool the v1 simulators use.
SUBMIT_ACTION = ToolSpec(TOOL_NAME, TOOL_DESCRIPTION, TOOL_PARAMETERS, strict=True)


def interpret_call(name: str, args, raw: str | None = None):
    """Normalise one tool call -> (tool_name, tool_args, action_json) for every provider alike.

    tool_args is the parsed argument object (None if the model sent arguments that aren't a JSON object).
    action_json is what the record shows and what submit_action passes to a v1 simulator.
    """
    if not isinstance(args, dict):
        return name, None, raw if raw is not None else json.dumps(args)
    if name == TOOL_NAME:
        return name, args, extract_action(args)
    return name, args, json.dumps(args, separators=(",", ":"), sort_keys=True)


@dataclass
class Usage:
    input_tokens: int = 0  # all input tokens, including cached ones
    cached_input_tokens: int = 0  # the subset of input served from cache
    cache_write_tokens: int = 0  # the subset of input written to the cache (Anthropic bills these above input)
    output_tokens: int = 0  # visible output + reasoning/thinking tokens
    # What the provider says it charged for this call, in USD, when it says. This is the cost of record: a price
    # list times a token count is only an estimate (a router can bill the same model at different rates
    # depending on which host served the call). None means the provider did not say, and the estimate stands.
    billed_usd: float | None = None


@dataclass
class Reply:
    action_json: str | None  # the submitted action, or None if the model did not call the tool
    text: str  # any visible text the model wrote (kept for the record)
    stop_reason: str  # provider's own stop reason, for diagnosis
    usage: Usage
    extra_tool_calls: int = 0  # calls beyond the first (ignored; only one action per turn)
    response_id: str = ""
    tool_name: str | None = None  # which tool the model called (None = it called none)
    tool_args: dict | None = None  # the parsed arguments (None if they weren't a JSON object)


@dataclass
class Settings:
    """Identical for both contestants. Each provider maps these to its own API."""

    max_output_tokens: int = 4096
    effort: str | None = "low"  # low | medium | high; None = provider default
    timeout_s: float = 180.0
    extra: dict = field(default_factory=dict)


OUT_OF_CREDIT_SIGNS = ("insufficient_quota", "exceeded your current quota", "credit balance", "insufficient credits")


def out_of_credit(status: int | None, code: str | None, message: str) -> bool:
    """True when the provider refused because the account or credit grant has no money left.

    That never fixes itself mid-run, so the contestant stops at once with a clear message instead of
    every remaining case failing one by one (e.g. the owner's OpenAI key stops working when its
    monthly credit grant is used up).
    """
    text = f"{code or ''} {message or ''}".lower()
    return status == 402 or code == "insufficient_quota" or any(sign in text for sign in OUT_OF_CREDIT_SIGNS)


def refusal(provider: str, error: Exception) -> str:
    """Why a key could not be checked, said from the kind of failure and its HTTP status alone."""
    status = getattr(error, "status_code", None) or getattr(error, "code", None)
    status = status if isinstance(status, int) else None
    kind = type(error).__name__
    if status in (400, 401, 403):  # the question carries nothing but the key (Google answers a bad one with 400)
        return f"{provider} did not accept this key ({kind}, HTTP {status})."
    return f"This key could not be checked with {provider} ({kind}{f', HTTP {status}' if status else ''})."


class FatalProviderError(RuntimeError):
    """Problems that will not fix themselves (bad key, unknown model). Stops that contestant."""


class PriceUnavailable(ValueError):
    """A price that is read from a provider's public list cannot be read right now: the list did not answer, or
    does not name the model. Unlike a model that can never be priced (not in the price table, a floating alias,
    no tool calling), this can pass: a server that stays up may ask again instead of refusing to start."""


class ProviderError(RuntimeError):
    """A single failed call. Ends the current case for that contestant."""


class Chat:
    def ask(self) -> Reply:
        raise NotImplementedError

    def answer_tool(self, observation_text: str) -> Reply:
        raise NotImplementedError

    def approx_input_chars(self) -> int:
        """Rough size of what the next request will send. Used for the pre-call budget check."""
        raise NotImplementedError


class KeyRefused(RuntimeError):
    """A key given for one race did not pass its check, before anything was run or spent. The text is made here,
    from the provider's name, the kind of failure and its HTTP status only: never from the provider's own
    message, which can repeat part of the key."""

    def __init__(self, provider: str, reason: str):
        super().__init__(reason)
        self.provider, self.reason = provider, reason


# What a key may be written as once part of it has been hidden: letters, digits and the marks keys and their
# masked forms are made of. A run of these that holds a piece of a key is replaced whole.
_KEYISH = re.compile(r"[A-Za-z0-9_\-.*…•]+")


def hide_key(text: str, key: str | None) -> str:
    """`text` with `key` taken out: the key itself, and any word that carries its first or last characters (a
    provider that refuses a key often repeats it with the middle starred out). For text about to be shown,
    logged or written into a record."""
    text = str(text)
    if not key or len(key) < 8:
        return text
    text = text.replace(key, "(the key)")
    head, tail = key[:8], key[-4:]

    def masked(word: re.Match) -> str:
        found = word.group(0)
        starred = any(mark in found for mark in ("*", "…", "•", ".."))
        if head in found or (starred and found.rstrip(".…").endswith(tail)):
            return "(part of the key)"
        return found

    return _KEYISH.sub(masked, text)


class Provider:
    name = "base"

    def __init__(self, model: str, key: str | None = None):
        self.model = model
        # The key this provider calls with, when it was handed one for this match instead of reading the
        # environment. Kept only to take it out of text (`hide`); a provider that reads the environment sets
        # it too where its client says which key it found.
        self.key = key

    def hide(self, text) -> str:
        """Any text about a failed call, with this provider's key taken out of it."""
        return hide_key(text, self.key)

    def check_key(self) -> None:
        """Ask the provider, without spending anything, whether it accepts this key and serves this model to
        it. Raises KeyRefused saying which of the two failed. The provider's own words are never passed on."""
        try:
            served = self.list_models()
        except Exception as e:  # noqa: BLE001  whatever the SDK raised: only its kind and status are said
            raise KeyRefused(self.name, refusal(self.name, e)) from None
        if self.model not in served:
            raise KeyRefused(self.name, f"{self.name} accepts this key but does not serve {self.model} to it.")

    def open_chat(self, system: str, first_message: str, settings: Settings, tools=None) -> Chat:
        """tools: list of ToolSpec offered to the model (default: the single submit_action tool)."""
        raise NotImplementedError

    def sent_settings(self, settings: Settings) -> dict:
        """Exactly which API parameters this provider sends, for the run record."""
        return {}

    def list_models(self) -> list[str]:
        """Model IDs this account can use, straight from the provider."""
        raise NotImplementedError
