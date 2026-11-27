"""Pick a provider from a contestant string like 'openai:gpt-6-luna'.

SDK imports are lazy, so you only need the package for providers you use.
"""

import importlib

PROVIDERS = {
    "anthropic": "providers.anthropic_api:AnthropicProvider",
    "openai": "providers.openai_api:OpenAIProvider",
    "google": "providers.google_api:GoogleProvider",
    "openrouter": "providers.openrouter_api:OpenRouterProvider",
    "scripted": "providers.scripted:ScriptedProvider",
}


def parse(contestant: str) -> tuple[str, str]:
    if ":" not in contestant:
        raise ValueError(f"Contestant '{contestant}' must look like provider:model, e.g. openai:gpt-6-luna")
    provider, model = contestant.split(":", 1)
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider '{provider}'. Known: {sorted(PROVIDERS)}")
    return provider, model


def create(contestant: str, key: str | None = None):
    """The provider for one contestant. `key`: the API key to call with, for a match whose keys were handed over
    with it (a race someone pays for themselves); without one the provider reads the environment."""
    provider, model = parse(contestant)
    path, cls = PROVIDERS[provider].split(":")
    return getattr(importlib.import_module(path), cls)(model, key)
