"""
Step 6a-2 (docs/phase6-plan.md 7.2, 7.6, 11.1): the start-up policy guard and the provider allow-list.

The app refuses to start
  * with a model that trains on inputs, unless ENVIRONMENT=development AND LLM_ALLOW_TRAINING_PROVIDERS=true,
  * with Groq outside development unless LLM_ZDR_CONFIRMED=true (the operator checked the console),
  * with a chain key that is not in models.json, is not "provider:model", is outside the allow-list or is
    not an answer model.
"""

import pytest

import app.main as app_main
from app.core.config import Settings
from app.llm.errors import LLMPolicyError
from app.llm.models import ModelRegistry, load_registry, parse_registry
from app.llm.policy import check_llm_policy
from app.llm.providers import (
    ALLOWED_PROVIDERS,
    PROVIDER_HOSTS,
    is_allowed_provider,
    split_model_key,
)

pytestmark = pytest.mark.usefixtures("no_network_at_all")

SETTING_NAMES = (
    "LLM_ANSWER_CHAIN",
    "LLM_ZDR_CONFIRMED",
    "LLM_ALLOW_TRAINING_PROVIDERS",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "GEMINI_API_KEY",
    "ENVIRONMENT",
)


def registry() -> ModelRegistry:
    def model(provider: str, model_id: str, *, trains: bool, purposes: list[str] | None = None) -> dict[str, object]:
        return {
            "provider": provider,
            "model_id": model_id,
            "purposes": purposes or ["answer"],
            "trains_on_inputs": trains,
            "rpm": 10,
            "rpd": 100,
            "pinned_on": "2026-11-24",
        }

    return parse_registry(
        {
            "schema_version": 1,
            "models": [
                model("groq", "big", trains=False),
                model("groq", "small", trains=False, purposes=["condense"]),
                model("openrouter", "vendor/free:free", trains=False),
                model("gemini", "flash", trains=True),
            ],
        }
    )


def settings_for(monkeypatch: pytest.MonkeyPatch, env: str = "development", **overrides: object) -> Settings:
    for name in SETTING_NAMES:
        monkeypatch.delenv(name, raising=False)
    fields: dict[str, object] = {"ENVIRONMENT": env}
    if env != "development":
        fields["REDIS_PASSWORD"] = "a-redis-password"  # the existing rule for every non-development environment
    fields.update(overrides)
    return Settings(_env_file=None, **fields)  # type: ignore[call-arg]


def problems(s: Settings, reg: ModelRegistry | None = None) -> tuple[str, ...]:
    with pytest.raises(LLMPolicyError) as caught:
        check_llm_policy(s, reg if reg is not None else registry())
    return caught.value.problems


# ── the allow-list ───────────────────────────────────────────────────────────────────────────────────


def test_the_allow_list_is_exactly_the_three_plan_providers() -> None:
    assert set(ALLOWED_PROVIDERS) == {"groq", "openrouter", "gemini"}
    assert {info.name for info in ALLOWED_PROVIDERS.values()} == set(ALLOWED_PROVIDERS)
    assert frozenset(info.host for info in ALLOWED_PROVIDERS.values()) == PROVIDER_HOSTS
    assert all(host == host.lower() and "/" not in host and ":" not in host for host in PROVIDER_HOSTS)


@pytest.mark.parametrize("name", ["groq", "openrouter", "gemini"])
def test_allowed_providers_pass(name: str) -> None:
    assert is_allowed_provider(name)


@pytest.mark.parametrize("name", ["fake", "openai", "anthropic", "Groq", "GROQ", " groq", "groq ", "", "groq:x", "*", None, 7])
def test_everything_else_is_refused_including_the_fake_and_odd_spellings(name: object) -> None:
    assert not is_allowed_provider(name)  # type: ignore[arg-type]


def test_a_model_key_splits_on_the_first_colon_only() -> None:
    assert split_model_key("groq:llama-x") == ("groq", "llama-x")
    assert split_model_key("openrouter:vendor/m:free") == ("openrouter", "vendor/m:free")


@pytest.mark.parametrize("key", ["", "nocolon", ":model", "groq:", "groq: x", " groq:x", "groq:x ", "gro q:x", "a" * 300, 5])
def test_a_malformed_model_key_is_refused(key: object) -> None:
    with pytest.raises(ValueError):
        split_model_key(key)  # type: ignore[arg-type]


# ── the guard ────────────────────────────────────────────────────────────────────────────────────────


def test_the_shipped_defaults_start_everywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    for env in ("development", "staging", "production"):
        check_llm_policy(settings_for(monkeypatch, env), load_registry())  # no chain: retrieval-only, no provider


@pytest.mark.parametrize("env", ["development", "production"])
def test_an_unknown_chain_key_is_refused(env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    found = problems(
        settings_for(monkeypatch, env, LLM_ANSWER_CHAIN="groq:big,groq:not-in-the-file", LLM_ZDR_CONFIRMED=True)
    )
    assert any("groq:not-in-the-file" in p for p in found)
    assert not any("groq:big" in p for p in found)


@pytest.mark.parametrize("key", ["nocolon", "groq:", ":x", "evilcorp:model", "fake:script", "Groq:big", "openai:gpt"])
def test_a_malformed_or_foreign_chain_key_is_refused(key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    assert problems(settings_for(monkeypatch, LLM_ANSWER_CHAIN=f"groq:big,{key}"))


def test_an_empty_chain_is_valid_the_app_just_answers_retrieval_only(monkeypatch: pytest.MonkeyPatch) -> None:
    check_llm_policy(settings_for(monkeypatch, "production", LLM_ANSWER_CHAIN=""), registry())


def test_a_model_that_is_not_an_answer_model_is_refused_in_the_answer_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    found = problems(settings_for(monkeypatch, LLM_ANSWER_CHAIN="groq:small"))
    assert any("groq:small" in p and "answer" in p for p in found)


def test_every_problem_is_reported_in_one_go(monkeypatch: pytest.MonkeyPatch) -> None:
    found = problems(settings_for(monkeypatch, "production", LLM_ANSWER_CHAIN="gemini:flash,groq:big,nope:x"))
    assert len(found) == 3  # training provider, Groq without ZDR, unknown provider


# training providers ---------------------------------------------------------------------------------


def test_a_training_model_is_refused_in_development_without_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    found = problems(settings_for(monkeypatch, "development", LLM_ANSWER_CHAIN="gemini:flash"))
    assert any("gemini:flash" in p and "LLM_ALLOW_TRAINING_PROVIDERS" in p for p in found)


def test_a_training_model_is_allowed_in_development_with_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    s = settings_for(monkeypatch, "development", LLM_ANSWER_CHAIN="gemini:flash", LLM_ALLOW_TRAINING_PROVIDERS=True)
    check_llm_policy(s, registry())


@pytest.mark.parametrize("env", ["production", "staging", "test", "prod", "Production"])
def test_a_training_model_is_refused_outside_development_even_with_the_flag(env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    s = settings_for(
        monkeypatch, env, LLM_ANSWER_CHAIN="gemini:flash", LLM_ALLOW_TRAINING_PROVIDERS=True, LLM_ZDR_CONFIRMED=True
    )
    assert any("gemini:flash" in p for p in problems(s))


def test_the_training_check_reads_the_flag_of_the_model_not_the_name_of_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = parse_registry(
        {
            "schema_version": 1,
            "models": [
                {
                    "provider": "openrouter",
                    "model_id": "free-that-trains",
                    "purposes": ["answer"],
                    "trains_on_inputs": True,
                    "rpm": 5,
                    "rpd": 50,
                    "pinned_on": "2026-11-24",
                }
            ],
        }
    )
    s = settings_for(monkeypatch, "production", LLM_ANSWER_CHAIN="openrouter:free-that-trains")
    assert any("trains" in p for p in problems(s, reg))


# Groq and zero data retention --------------------------------------------------------------------------


@pytest.mark.parametrize("env", ["production", "staging", "test"])
def test_groq_outside_development_needs_the_zdr_attestation(env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    found = problems(settings_for(monkeypatch, env, LLM_ANSWER_CHAIN="groq:big"))
    assert any("groq:big" in p and "LLM_ZDR_CONFIRMED" in p for p in found)


@pytest.mark.parametrize("env", ["production", "staging"])
def test_groq_outside_development_starts_once_the_operator_confirms(env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    check_llm_policy(settings_for(monkeypatch, env, LLM_ANSWER_CHAIN="groq:big", LLM_ZDR_CONFIRMED=True), registry())


def test_groq_in_development_needs_no_attestation(monkeypatch: pytest.MonkeyPatch) -> None:
    check_llm_policy(settings_for(monkeypatch, "development", LLM_ANSWER_CHAIN="groq:big"), registry())


def test_zdr_confirmation_does_not_unlock_a_training_model(monkeypatch: pytest.MonkeyPatch) -> None:
    s = settings_for(monkeypatch, "production", LLM_ANSWER_CHAIN="gemini:flash", LLM_ZDR_CONFIRMED=True)
    assert problems(s)


def test_openrouter_outside_development_needs_no_attestation_because_every_request_carries_the_restriction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check_llm_policy(settings_for(monkeypatch, "production", LLM_ANSWER_CHAIN="openrouter:vendor/free:free"), registry())


# no secret in a policy message ------------------------------------------------------------------------


def test_a_policy_message_never_contains_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    canary = "CANARY-POLICY-" + "7d2e91b4" * 4
    s = settings_for(
        monkeypatch,
        "production",
        LLM_ANSWER_CHAIN="gemini:flash,groq:big,nope:x",
        GROQ_API_KEY=canary,
        OPENROUTER_API_KEY=canary,
        GEMINI_API_KEY=canary,
    )
    with pytest.raises(LLMPolicyError) as caught:
        check_llm_policy(s, registry())
    assert canary not in str(caught.value) and canary not in repr(caught.value)
    assert not any(canary in p for p in caught.value.problems)


# the app refuses to start -----------------------------------------------------------------------------


async def test_the_app_refuses_to_start_before_it_touches_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    s = settings_for(monkeypatch, "production", LLM_ANSWER_CHAIN="gemini:flash")
    monkeypatch.setattr(app_main, "settings", s)

    async def redis_reached() -> None:
        raise AssertionError("Redis was reached before the LLM policy check")

    monkeypatch.setattr(app_main, "get_redis", redis_reached)
    with pytest.raises(LLMPolicyError):
        async with app_main.lifespan(app_main.app):
            pytest.fail("the app started with a training provider in production")


async def test_a_valid_configuration_lets_the_app_start(monkeypatch: pytest.MonkeyPatch) -> None:
    s = settings_for(monkeypatch, "development", EMBEDDING_WARM_AT_STARTUP=False)
    monkeypatch.setattr(app_main, "settings", s)
    calls: list[str] = []

    async def fake_get_redis() -> None:
        calls.append("get_redis")

    async def fake_close_redis() -> None:
        calls.append("close_redis")

    monkeypatch.setattr(app_main, "get_redis", fake_get_redis)
    monkeypatch.setattr(app_main, "close_redis", fake_close_redis)
    async with app_main.lifespan(app_main.app):
        assert calls == ["get_redis"]
    assert calls == ["get_redis", "close_redis"]
