"""
app/llm/policy.py
─────────────────
The start-up policy guard (docs/phase6-plan.md 7.2, 7.6, 11.1). The app calls `check_llm_policy` before it
does anything else; a ModelRegistryError or LLMPolicyError stops the start-up.

Rules, for every key in LLM_ANSWER_CHAIN:
  * it must read '<provider>:<model id>', the provider must be on the allow-list and the key must be in
    models.json as an answer model (an unknown key is a typo, or a model nobody verified);
  * a model with trains_on_inputs=true needs ENVIRONMENT=development AND LLM_ALLOW_TRAINING_PROVIDERS=true;
  * a Groq model outside development needs LLM_ZDR_CONFIRMED=true (the operator checked the console).

An empty chain is valid: no provider is ever called and chat falls back to retrieval-only answers.
Messages name keys and settings, never a key value.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.llm.errors import LLMPolicyError
from app.llm.models import ModelRegistry, load_registry
from app.llm.providers import is_allowed_provider, split_model_key

if TYPE_CHECKING:
    from app.core.config import Settings


def check_llm_policy(settings: Settings, registry: ModelRegistry | None = None) -> None:
    """Raise LLMPolicyError listing every problem, or return when the configuration is allowed."""
    registry = load_registry() if registry is None else registry
    problems: list[str] = []
    for key in settings.llm_answer_chain:
        try:
            provider, _ = split_model_key(key)
        except ValueError:
            problems.append(f"LLM_ANSWER_CHAIN: {key!r} is not of the form 'provider:model-id'")
            continue
        if not is_allowed_provider(provider):
            problems.append(f"LLM_ANSWER_CHAIN: provider of {key} is not on the allow-list")
            continue
        model = registry.get(key)
        if model is None:
            problems.append(f"LLM_ANSWER_CHAIN: {key} is not in models.json")
            continue
        if "answer" not in model.purposes:
            problems.append(f"LLM_ANSWER_CHAIN: {key} is not an answer model in models.json")
        if model.trains_on_inputs and not (settings.is_development and settings.LLM_ALLOW_TRAINING_PROVIDERS):
            problems.append(
                f"LLM_ANSWER_CHAIN: {key} trains on inputs; it needs ENVIRONMENT=development "
                "and LLM_ALLOW_TRAINING_PROVIDERS=true"
            )
        if provider == "groq" and not settings.is_development and not settings.LLM_ZDR_CONFIRMED:
            problems.append(
                f"LLM_ANSWER_CHAIN: {key} is a Groq model; outside development it needs "
                "LLM_ZDR_CONFIRMED=true (zero data retention checked in the Groq console)"
            )
    if problems:
        raise LLMPolicyError(problems)
