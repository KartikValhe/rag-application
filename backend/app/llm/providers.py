"""
app/llm/providers.py
────────────────────
The provider allow-list (docs/phase6-plan.md 7.2): the only providers the platform may ever call. A model key
is "<provider>:<model id>"; the provider part must be one of these names. The fake provider used by tests is
deliberately NOT here, so it can never be put in a chain.

`PROVIDER_HOSTS` is what the test-suite socket guard refuses to connect to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

_MODEL_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,159}\Z")
_PROVIDER_PART = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    name: str
    host: str  # the API host the adapter of step 6a-3 will talk to
    always_trains_on_inputs: bool  # a model of this provider may not be marked otherwise in models.json


ALLOWED_PROVIDERS: Final = MappingProxyType(
    {
        "groq": ProviderInfo("groq", "api.groq.com", always_trains_on_inputs=False),
        "openrouter": ProviderInfo("openrouter", "openrouter.ai", always_trains_on_inputs=False),
        # The free tier of the Gemini API trains on inputs (plan 7.2): development only, sample data only.
        "gemini": ProviderInfo("gemini", "generativelanguage.googleapis.com", always_trains_on_inputs=True),
    }
)

PROVIDER_HOSTS: Final = frozenset(info.host for info in ALLOWED_PROVIDERS.values())


def is_allowed_provider(name: object) -> bool:
    """Exact match: no case folding, no whitespace, no prefix."""
    return isinstance(name, str) and name in ALLOWED_PROVIDERS


def split_model_key(key: object) -> tuple[str, str]:
    """'groq:llama' gives ('groq', 'llama'). Splits on the FIRST colon only (OpenRouter ids contain colons).
    Raises ValueError for anything that is not '<provider>:<model id>'. Does not check the allow-list."""
    if not isinstance(key, str) or ":" not in key:
        raise ValueError("a model key looks like 'provider:model-id'")
    provider, model_id = key.split(":", 1)
    if not _PROVIDER_PART.match(provider) or not _MODEL_PART.match(model_id):
        raise ValueError("a model key looks like 'provider:model-id'")
    return provider, model_id
