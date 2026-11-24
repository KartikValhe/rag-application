"""
app/llm/policy.py
─────────────────
The start-up policy guard (docs/phase6-plan.md 7.2, 7.6, 11.1). The app calls `check_llm_policy` before it
does anything else; a ModelRegistryError or LLMPolicyError stops the start-up.

Rules, for every key in LLM_ANSWER_CHAIN (purpose "answer") and LLM_CONDENSE_CHAIN (purpose "condense", step 6a-4):
  * it must read '<provider>:<model id>', the provider must be on the allow-list and the key must be in
    models.json as a model for that purpose (an unknown key is a typo, or a model nobody verified);
  * a model with trains_on_inputs=true needs ENVIRONMENT=development AND LLM_ALLOW_TRAINING_PROVIDERS=true;
  * a Groq model outside development needs LLM_ZDR_CONFIRMED=true (the operator checked the console).

An empty chain is valid: no provider is ever called. An empty answer chain means retrieval-only answers; an
empty condense chain means no condense (the raw question is used).
`key_problems` is the check for one key; the chain (app/llm/chain.py) runs it again before every call, so a
setting changed after start-up can never reach a model the start-up check would have refused.
Messages name keys and settings, never a key value.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from app.llm.errors import LLMPolicyError
from app.llm.models import ModelRegistry, Purpose, load_registry
from app.llm.providers import is_allowed_provider, split_model_key

if TYPE_CHECKING:
    from app.core.config import Settings

# The setting that lists the chain of each purpose, in the order they are checked.
CHAIN_SETTINGS: Final[Mapping[Purpose, str]] = MappingProxyType(
    {"answer": "LLM_ANSWER_CHAIN", "condense": "LLM_CONDENSE_CHAIN"}
)


def chain_keys(settings: Settings, purpose: Purpose) -> tuple[str, ...]:
    if purpose == "answer":
        return settings.llm_answer_chain
    if purpose == "condense":
        return settings.llm_condense_chain
    raise ValueError("unknown purpose")


def key_problems(settings: Settings, registry: ModelRegistry, key: str, purpose: Purpose) -> list[str]:
    """Every rule `key` breaks as a member of the chain for `purpose`; empty when it may be called."""
    setting = CHAIN_SETTINGS[purpose]
    try:
        provider, _ = split_model_key(key)
    except ValueError:
        return [f"{setting}: {key!r} is not of the form 'provider:model-id'"]
    if not is_allowed_provider(provider):
        return [f"{setting}: provider of {key} is not on the allow-list"]
    model = registry.get(key)
    if model is None:
        return [f"{setting}: {key} is not in models.json"]
    problems: list[str] = []
    if purpose not in model.purposes:
        article = "an" if purpose[0] in "aeiou" else "a"
        problems.append(f"{setting}: {key} is not {article} {purpose} model in models.json")
    if model.trains_on_inputs and not (settings.is_development and settings.LLM_ALLOW_TRAINING_PROVIDERS):
        problems.append(
            f"{setting}: {key} trains on inputs; it needs ENVIRONMENT=development "
            "and LLM_ALLOW_TRAINING_PROVIDERS=true"
        )
    if provider == "groq" and not settings.is_development and not settings.LLM_ZDR_CONFIRMED:
        problems.append(
            f"{setting}: {key} is a Groq model; outside development it needs "
            "LLM_ZDR_CONFIRMED=true (zero data retention checked in the Groq console)"
        )
    return problems


def check_llm_policy(settings: Settings, registry: ModelRegistry | None = None) -> None:
    """Raise LLMPolicyError listing every problem, or return when the configuration is allowed."""
    registry = load_registry() if registry is None else registry
    problems: list[str] = []
    for purpose in CHAIN_SETTINGS:
        for key in chain_keys(settings, purpose):
            problems.extend(key_problems(settings, registry, key, purpose))
    if problems:
        raise LLMPolicyError(problems)
