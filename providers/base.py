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


class FatalProviderError(RuntimeError):
    """Problems that will not fix themselves (bad key, unknown model). Stops that contestant."""


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


class Provider:
    name = "base"

    def __init__(self, model: str):
        self.model = model

    def open_chat(self, system: str, first_message: str, settings: Settings, tools=None) -> Chat:
        """tools: list of ToolSpec offered to the model (default: the single submit_action tool)."""
        raise NotImplementedError

    def sent_settings(self, settings: Settings) -> dict:
        """Exactly which API parameters this provider sends, for the run record."""
        return {}

    def list_models(self) -> list[str]:
        """Model IDs this account can use, straight from the provider."""
        raise NotImplementedError
