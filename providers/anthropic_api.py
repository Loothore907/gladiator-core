"""Anthropic (Claude) via the official `anthropic` SDK. Key: ANTHROPIC_API_KEY."""

import json

import anthropic

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


def format_tools(tools) -> list[dict]:
    out = []
    for t in tools or [SUBMIT_ACTION]:
        d = {"name": t.name, "description": t.description, "input_schema": t.parameters}
        if t.strict:
            d["strict"] = True
        out.append(d)
    return out


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, model: str):
        super().__init__(model)
        self.client = anthropic.Anthropic()

    def request_params(self, settings: Settings, tools=None) -> dict:
        params = {
            "max_tokens": settings.max_output_tokens,
            # Prompt caching: the API caches the conversation up to the latest message, so each turn re-reads
            # the earlier turns from cache (about a tenth of the input price) instead of paying full price for
            # the whole history again. Same request content either way; only billing changes.
            "cache_control": {"type": "ephemeral"},
            "tools": format_tools(tools),
            # Current Claude models reject forced tool use, so we use "auto" and
            # the system prompt tells the model to call the tool every turn.
            "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
        }
        if settings.effort:
            params["output_config"] = {"effort": settings.effort}
        params.update(settings.extra)
        return params

    def sent_settings(self, settings: Settings) -> dict:
        p = self.request_params(settings)
        p["tools"] = [t["name"] for t in p["tools"]]
        return p

    def open_chat(self, system, first_message, settings, tools=None):
        return AnthropicChat(self, system, first_message, settings, tools)

    def list_models(self):
        return sorted(m.id for m in self.client.models.list())


class AnthropicChat(Chat):
    def __init__(self, provider, system, first_message, settings, tools=None):
        self.p = provider
        self.system = system
        self.settings = settings
        self.tools = tools
        self.messages = [{"role": "user", "content": first_message}]
        self.pending_tool_ids = []

    def approx_input_chars(self):
        return len(self.system) + len(json.dumps(self.messages, default=str))

    def _call(self) -> Reply:
        try:
            resp = self.p.client.with_options(timeout=self.settings.timeout_s).messages.create(
                model=self.p.model,
                system=self.system,
                messages=self.messages,
                **self.p.request_params(self.settings, self.tools),
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError) as e:
            raise FatalProviderError(f"anthropic {type(e).__name__}: {e.message}") from None
        except anthropic.APIStatusError as e:
            if out_of_credit(e.status_code, None, e.message):
                raise FatalProviderError(f"anthropic: out of credit ({e.message})") from None
            raise ProviderError(f"anthropic HTTP {e.status_code}: {e.message}") from None
        except anthropic.APIConnectionError as e:
            raise ProviderError(f"anthropic connection error: {e}") from None

        # Keep the full content (including thinking blocks) in history, as the API requires.
        self.messages.append({"role": "assistant", "content": [b.to_dict(exclude_none=True) for b in resp.content]})
        calls = [b for b in resp.content if b.type == "tool_use"]
        self.pending_tool_ids = [b.id for b in calls]
        text = "".join(b.text for b in resp.content if b.type == "text")
        name = args = action = None
        if calls:
            name, args, action = interpret_call(calls[0].name, calls[0].input)
        u = resp.usage
        cached = u.cache_read_input_tokens or 0
        written = u.cache_creation_input_tokens or 0
        usage = Usage(
            input_tokens=u.input_tokens + cached + written,
            cached_input_tokens=cached,
            cache_write_tokens=written,
            output_tokens=u.output_tokens,
        )
        return Reply(action, text, resp.stop_reason or "", usage, max(0, len(calls) - 1), resp.id, name, args)

    def ask(self):
        return self._call()

    def answer_tool(self, observation_text):
        if not self.pending_tool_ids:
            raise ProviderError("no pending tool call to answer")
        first, *rest = self.pending_tool_ids
        results = [{"type": "tool_result", "tool_use_id": first, "content": observation_text}]
        results += [
            {"type": "tool_result", "tool_use_id": i, "is_error": True, "content": "Ignored: only one action per turn."}
            for i in rest
        ]
        self.messages.append({"role": "user", "content": results})
        return self._call()
