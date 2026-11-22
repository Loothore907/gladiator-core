"""OpenAI via the official `openai` SDK (Responses API). Key: OPENAI_API_KEY.

Matches how v1 called OpenAI: one function tool, tool_choice "required",
no parallel calls, store=False, reasoning items passed back each turn.
"""

import json

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


def format_tools(tools) -> list[dict]:
    return [
        {
            "type": "function",
            "name": t.name,
            "description": t.description,
            "parameters": t.parameters,
            "strict": t.strict,
        }
        for t in tools or [SUBMIT_ACTION]
    ]


class OpenAIProvider(Provider):
    name = "openai"

    def __init__(self, model: str):
        super().__init__(model)
        self.client = openai.OpenAI()

    def request_params(self, settings: Settings, tools=None) -> dict:
        params = {
            "max_output_tokens": settings.max_output_tokens,
            "tools": format_tools(tools),
            "tool_choice": "required",
            "parallel_tool_calls": False,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        if settings.effort:
            params["reasoning"] = {"effort": settings.effort}
        params.update(settings.extra)
        return params

    def sent_settings(self, settings):
        p = self.request_params(settings)
        p["tools"] = [t["name"] for t in p["tools"]]
        return p

    def open_chat(self, system, first_message, settings, tools=None):
        return OpenAIChat(self, system, first_message, settings, tools)

    def list_models(self):
        return sorted(m.id for m in self.client.models.list())


class OpenAIChat(Chat):
    def __init__(self, provider, system, first_message, settings, tools=None):
        self.p = provider
        self.system = system
        self.settings = settings
        self.tools = tools
        self.items = [{"role": "user", "content": first_message}]
        self.pending_call_ids = []

    def approx_input_chars(self):
        return len(self.system) + len(json.dumps(self.items, default=str))

    def _call(self):
        try:
            resp = self.p.client.with_options(timeout=self.settings.timeout_s).responses.create(
                model=self.p.model,
                instructions=self.system,
                input=self.items,
                **self.p.request_params(self.settings, self.tools),
            )
        except (openai.AuthenticationError, openai.PermissionDeniedError, openai.NotFoundError) as e:
            raise FatalProviderError(f"openai {type(e).__name__}: {e.message}") from None
        except openai.APIStatusError as e:
            if out_of_credit(e.status_code, getattr(e, "code", None), e.message):
                raise FatalProviderError(f"openai: out of credit ({e.message})") from None
            raise ProviderError(f"openai HTTP {e.status_code}: {e.message}") from None
        except openai.APIConnectionError as e:
            raise ProviderError(f"openai connection error: {e}") from None

        output = [item.to_dict(exclude_none=True) for item in resp.output]
        self.items.extend(output)
        calls = [i for i in output if i.get("type") == "function_call"]
        self.pending_call_ids = [c["call_id"] for c in calls]
        text = "".join(
            part.get("text", "") for i in output if i.get("type") == "message" for part in i.get("content", [])
        )
        name = args = action = None
        if calls:
            raw = calls[0]["arguments"]
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None  # not JSON at all: passed on as-is and rejected downstream
            name, args, action = interpret_call(calls[0]["name"], parsed, raw)
        u = resp.usage
        details = getattr(u, "input_tokens_details", None)
        cached = (getattr(details, "cached_tokens", 0) or 0) if details else 0
        usage = Usage(
            input_tokens=u.input_tokens if u else 0,
            cached_input_tokens=cached,
            output_tokens=u.output_tokens if u else 0,
        )
        stop = resp.status or ""
        if resp.incomplete_details is not None:
            stop += f" ({resp.incomplete_details.reason})"
        return Reply(action, text, stop, usage, max(0, len(calls) - 1), resp.id, name, args)

    def ask(self):
        return self._call()

    def answer_tool(self, observation_text):
        if not self.pending_call_ids:
            raise ProviderError("no pending tool call to answer")
        first, *rest = self.pending_call_ids
        self.items.append({"type": "function_call_output", "call_id": first, "output": observation_text})
        for cid in rest:
            self.items.append(
                {"type": "function_call_output", "call_id": cid, "output": "Ignored: only one action per turn."}
            )
        return self._call()
