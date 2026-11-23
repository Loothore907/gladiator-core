"""OpenRouter: one key, many open-source and hosted models. Key: OPENROUTER_API_KEY.

OpenRouter speaks the OpenAI Chat Completions format, so this uses the official `openai`
SDK pointed at OpenRouter's address. Contestants look like `openrouter:qwen/qwen3-32b`.

Prices: OpenRouter publishes the exact per-token price of every model it serves. If a model
isn't in providers/prices.yaml, the engine reads its price from OpenRouter at the start of the
match (see live_price) and records it in the run record, so the $ cap still works.

Fairness: OpenRouter can route one model to different hosting companies. We ask it to only use
hosts that support everything we send (tools, reasoning), and record which host served each call.
"""

import json
import os
import urllib.request

import openai

from providers.base import (
    SUBMIT_ACTION,
    Chat,
    FatalProviderError,
    Provider,
    ProviderError,
    Reply,
    Settings,
    Usage,
    interpret_call,
    out_of_credit,
)

BASE_URL = "https://openrouter.ai/api/v1"


def format_tools(tools) -> list[dict]:
    return [
        {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
        for t in tools or [SUBMIT_ACTION]
    ]


_catalog: dict | None = None


def catalog() -> dict:
    """OpenRouter's public model list (no key needed): id -> model info. Fetched once per run."""
    global _catalog
    if _catalog is None:
        with urllib.request.urlopen(f"{BASE_URL}/models", timeout=30) as resp:
            _catalog = {m["id"]: m for m in json.load(resp)["data"]}
    return _catalog


def live_price(model: str) -> dict:
    """USD per 1M tokens from OpenRouter's published per-token prices."""
    if model.startswith("~") or model.endswith("-latest"):
        raise ValueError(
            f"openrouter:{model} is a floating alias that silently changes which model runs; "
            "name a specific model instead so the report says what was actually tested."
        )
    info = catalog().get(model)
    if info is None:
        raise ValueError(f"OpenRouter does not list a model called '{model}'.")
    if "tools" not in (info.get("supported_parameters") or []):
        raise ValueError(f"openrouter:{model} does not support tool calling, which every Gladiator case needs.")
    p = info["pricing"]
    price = {"input": float(p["prompt"]) * 1e6, "output": float(p["completion"]) * 1e6}
    if p.get("input_cache_read") not in (None, "", "0"):
        price["cached_input"] = float(p["input_cache_read"]) * 1e6
    price["source"] = "openrouter.ai/api/v1/models (live)"
    return price


def usage_from(u) -> Usage:
    """Token counts and the charge for one call, from the response's usage block. OpenRouter reports what it
    actually billed (`cost`, in USD) when asked to (`usage: {include: true}` in the request): that is the cost of
    record, since the same model is billed at different rates depending on the host the call was routed to."""
    if not u:
        return Usage()
    details = getattr(u, "prompt_tokens_details", None)
    billed = getattr(u, "cost", None)
    if billed is None:  # an SDK that does not know the field keeps it among the extras
        billed = (getattr(u, "model_extra", None) or {}).get("cost")
    return Usage(
        input_tokens=u.prompt_tokens or 0,
        cached_input_tokens=(getattr(details, "cached_tokens", 0) or 0) if details else 0,
        output_tokens=u.completion_tokens or 0,
        billed_usd=float(billed) if isinstance(billed, (int, float)) and not isinstance(billed, bool) else None,
    )


class OpenRouterProvider(Provider):
    name = "openrouter"

    def __init__(self, model: str):
        super().__init__(model)
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY is not set")
        self.client = openai.OpenAI(base_url=BASE_URL, api_key=key)

    def request_params(self, settings: Settings, tools=None) -> dict:
        extra = {
            # Only route to hosts that support every parameter we send (tools, tool_choice).
            "provider": {"require_parameters": True},
            # Asks for the charge in the response. OpenRouter now always includes it and ignores this field
            # (docs, 2026-11-23); it is kept so the request still says what the record relies on.
            "usage": {"include": True},
        }
        if settings.effort:
            extra["reasoning"] = {"effort": settings.effort}
        extra.update(settings.extra)
        # No tool_choice / parallel_tool_calls: many OpenRouter hosts reject tool_choice="required" or
        # don't support parallel_tool_calls, and with require_parameters that leaves no host at all
        # (seen live 2026-11-20 on qwen3.7-flash and gpt-oss-20b). The instructions already say "call
        # submit_action exactly once per turn" (same approach as Anthropic), and extra calls are ignored.
        return {
            "max_tokens": settings.max_output_tokens,
            "tools": format_tools(tools),
            "extra_body": extra,
        }

    def sent_settings(self, settings):
        p = self.request_params(settings)
        p["tools"] = [t["function"]["name"] for t in p["tools"]]
        return p

    def open_chat(self, system, first_message, settings, tools=None):
        return OpenRouterChat(self, system, first_message, settings, tools)

    def list_models(self):
        return sorted(m for m, info in catalog().items() if "tools" in (info.get("supported_parameters") or []))


class OpenRouterChat(Chat):
    def __init__(self, provider, system, first_message, settings, tools=None):
        self.p = provider
        self.settings = settings
        self.tools = tools
        self.messages = [{"role": "system", "content": system}, {"role": "user", "content": first_message}]
        self.pending_call_ids = []

    def approx_input_chars(self):
        return len(json.dumps(self.messages, default=str))

    def _call(self):
        try:
            resp = self.p.client.with_options(timeout=self.settings.timeout_s).chat.completions.create(
                model=self.p.model, messages=self.messages, **self.p.request_params(self.settings, self.tools)
            )
        except (openai.AuthenticationError, openai.PermissionDeniedError, openai.NotFoundError) as e:
            raise FatalProviderError(f"openrouter {type(e).__name__}: {e.message}") from None
        except openai.APIStatusError as e:
            if out_of_credit(e.status_code, getattr(e, "code", None), e.message):
                raise FatalProviderError(f"openrouter: out of credit ({e.message})") from None
            raise ProviderError(f"openrouter HTTP {e.status_code}: {e.message}") from None
        except openai.APIConnectionError as e:
            raise ProviderError(f"openrouter connection error: {e}") from None

        if not resp.choices:
            raise ProviderError(f"openrouter returned no choices: {getattr(resp, 'error', None)}")
        choice = resp.choices[0]
        msg = choice.message.to_dict(exclude_none=True)  # keeps reasoning fields for the next turn
        self.messages.append(msg)
        calls = msg.get("tool_calls") or []
        self.pending_call_ids = [c["id"] for c in calls]
        name = args = action = None
        if calls:
            raw = calls[0]["function"].get("arguments") or "{}"
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None  # not JSON at all: passed on as-is and rejected downstream
            name, args, action = interpret_call(calls[0]["function"]["name"], parsed, raw)
        usage = usage_from(resp.usage)
        host = getattr(resp, "provider", None) or ""
        stop = (choice.finish_reason or "") + (f" via {host}" if host else "")
        return Reply(action, msg.get("content") or "", stop, usage, max(0, len(calls) - 1), resp.id or "", name, args)

    def ask(self):
        return self._call()

    def answer_tool(self, observation_text):
        if not self.pending_call_ids:
            raise ProviderError("no pending tool call to answer")
        first, *rest = self.pending_call_ids
        self.messages.append({"role": "tool", "tool_call_id": first, "content": observation_text})
        for cid in rest:
            self.messages.append({"role": "tool", "tool_call_id": cid, "content": "Ignored: only one action per turn."})
        return self._call()
