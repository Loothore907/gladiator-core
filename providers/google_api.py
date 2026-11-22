"""Google Gemini via the official `google-genai` SDK. Key: GEMINI_API_KEY (or GOOGLE_API_KEY)."""

from google import genai
from google.genai import errors, types

from providers.base import (
    SUBMIT_ACTION,
    TOOL_NAME,
    Chat,
    FatalProviderError,
    Provider,
    ProviderError,
    Reply,
    Settings,
    Usage,
    interpret_call,
)


def _strip(schema):
    """Gemini's schema dialect rejects additionalProperties; drop it at every level."""
    if isinstance(schema, dict):
        return {k: _strip(v) for k, v in schema.items() if k != "additionalProperties"}
    if isinstance(schema, list):
        return [_strip(v) for v in schema]
    return schema


def format_tools(tools) -> list:
    return [
        types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=_strip(t.parameters))
        for t in tools or [SUBMIT_ACTION]
    ]


# Retry temporary failures (rate limits, "high demand", server errors) like the Anthropic and OpenAI
# libraries do by default, but more patiently (5 attempts, up to ~2 minutes), because Gemini returns
# "high demand" errors far more often. Google's library does not retry unless asked.
RETRY = types.HttpRetryOptions(
    attempts=5, initial_delay=2.0, max_delay=60.0, http_status_codes=[408, 429, 500, 502, 503, 504]
)

# Gemini 3+ models take a thinking *level*; Gemini 2.x models take a thinking *token budget* instead.
# The suite's effort setting maps onto whichever the model understands.
THINKING_BUDGET = {"low": 1024, "medium": 4096, "high": 8192}


def thinking_config(model: str, effort: str | None) -> types.ThinkingConfig | None:
    if not effort:
        return None
    if model.startswith("gemini-2"):
        if effort not in THINKING_BUDGET:
            raise ValueError(f"{model} takes effort {sorted(THINKING_BUDGET)}, not '{effort}'")
        return types.ThinkingConfig(thinking_budget=THINKING_BUDGET[effort])
    return types.ThinkingConfig(thinking_level=effort.upper())


class GoogleProvider(Provider):
    name = "google"

    def __init__(self, model: str):
        super().__init__(model)
        self.client = genai.Client()

    def config(self, system: str, settings: Settings, tools=None) -> types.GenerateContentConfig:
        functions = format_tools(tools)
        kwargs = dict(
            system_instruction=system,
            max_output_tokens=settings.max_output_tokens,
            tools=[types.Tool(function_declarations=functions)],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode="ANY", allowed_function_names=[f.name for f in functions]
                )
            ),
            http_options=types.HttpOptions(timeout=int(settings.timeout_s * 1000), retry_options=RETRY),
        )
        thinking = thinking_config(self.model, settings.effort)
        if thinking:
            kwargs["thinking_config"] = thinking
        kwargs.update(settings.extra)
        return types.GenerateContentConfig(**kwargs)

    def sent_settings(self, settings):
        return {
            "max_output_tokens": settings.max_output_tokens,
            "tools": [TOOL_NAME],
            "function_calling_mode": "ANY",
            "thinking": (
                t.model_dump(exclude_none=True) if (t := thinking_config(self.model, settings.effort)) else None
            ),
            **settings.extra,
        }

    def open_chat(self, system, first_message, settings, tools=None):
        return GoogleChat(self, system, first_message, settings, tools)

    def list_models(self):
        return sorted(
            m.name.removeprefix("models/")
            for m in self.client.models.list()
            if not m.supported_actions or "generateContent" in m.supported_actions
        )


class GoogleChat(Chat):
    def __init__(self, provider, system, first_message, settings, tools=None):
        self.p = provider
        self.system = system
        self.settings = settings
        self.tools = tools
        self.contents = [types.Content(role="user", parts=[types.Part.from_text(text=first_message)])]
        self.pending_calls = []

    def approx_input_chars(self):
        return len(self.system) + sum(len(c.model_dump_json()) for c in self.contents)

    def _call(self):
        try:
            resp = self.p.client.models.generate_content(
                model=self.p.model, contents=self.contents, config=self.p.config(self.system, self.settings, self.tools)
            )
        except errors.ClientError as e:
            if e.code in (401, 403, 404):
                raise FatalProviderError(f"google HTTP {e.code}: {e.message}") from None
            raise ProviderError(f"google HTTP {e.code}: {e.message}") from None
        except errors.APIError as e:
            raise ProviderError(f"google HTTP {e.code}: {e.message}") from None

        cand = resp.candidates[0] if resp.candidates else None
        parts = (cand.content.parts or []) if cand and cand.content else []
        if cand and cand.content:
            self.contents.append(cand.content)  # keeps thought signatures, as the API requires
        calls = [p.function_call for p in parts if p.function_call]
        self.pending_calls = calls
        text = "".join(p.text for p in parts if p.text and not p.thought)
        name = args = action = None
        if calls:
            name, args, action = interpret_call(calls[0].name, dict(calls[0].args or {}))
        m = resp.usage_metadata
        usage = Usage(
            input_tokens=(m.prompt_token_count or 0) if m else 0,
            cached_input_tokens=(m.cached_content_token_count or 0) if m else 0,
            output_tokens=((m.candidates_token_count or 0) + (m.thoughts_token_count or 0)) if m else 0,
        )
        stop = str(cand.finish_reason) if cand and cand.finish_reason else ""
        return Reply(action, text, stop, usage, max(0, len(calls) - 1), resp.response_id or "", name, args)

    def ask(self):
        return self._call()

    def answer_tool(self, observation_text):
        if not self.pending_calls:
            raise ProviderError("no pending tool call to answer")
        first, *rest = self.pending_calls
        parts = [types.Part.from_function_response(name=first.name, response={"result": observation_text})]
        ignored = {"error": "Ignored: only one action per turn."}
        parts += [types.Part.from_function_response(name=c.name, response=ignored) for c in rest]
        self.contents.append(types.Content(role="user", parts=parts))
        return self._call()
