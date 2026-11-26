"""
tests/test_chat_settings_pure.py
────────────────────────────────
The validation and the change record of the chat settings route (app/services/chat_settings.py, step 6a-12a), with
no database (owner decision 2 of the 6a-12 brief; codes from the 6a-12a brief, recorded in plan 8.4):

  - unknown fields, a body that is not a JSON object, an empty object, a wrong type and an out-of-range value are
    422 invalid_setting;
  - allowed_llm_providers is null (the platform chain) or a list of EXACT provider names of models.json (no case
    folding, no trimming, no duplicates); any other name is provider_not_allowed; [] means retrieval-only;
  - the budget keeps the 6a-5 codes: token_budget_invalid, token_budget_above_platform_ceiling;
  - the change record holds field names with old and new values only.
"""

from typing import Any

import pytest

from app.core.config import settings
from app.llm.models import load_registry
from app.services import chat_settings
from app.services.chat_settings import (
    DEFAULTS,
    ChatSettings,
    SettingsRefused,
    changes,
    parse_update,
)

PROVIDERS = frozenset({"groq", "openrouter"})


def _code(body: object, providers: frozenset[str] = PROVIDERS) -> str:
    with pytest.raises(SettingsRefused) as err:
        parse_update(body, providers)
    return err.value.code


def test_the_known_providers_are_exactly_the_provider_names_of_models_json() -> None:
    expected = frozenset(m.provider for m in load_registry().models)
    assert expected, "models.json names no provider"
    assert chat_settings.known_providers() == expected


@pytest.mark.parametrize("body", [None, [], "x", 1, True, [{"chat_enabled": True}]])
def test_a_body_that_is_not_an_object_is_invalid_setting(body: object) -> None:
    assert _code(body) == "invalid_setting"


def test_an_empty_object_is_invalid_setting() -> None:
    assert _code({}) == "invalid_setting"


@pytest.mark.parametrize("body", [
    {"bot_name": "x"}, {"chat_retention_days": 30}, {"tenant_id": "x"}, {"Chat_enabled": True},
    {"chat_enabled": True, "extra": 1}, {"effective_daily_token_budget": 5},
])
def test_unknown_fields_are_invalid_setting(body: dict[str, Any]) -> None:
    assert _code(body) == "invalid_setting"


@pytest.mark.parametrize("value", [None, 1, 0, "true", "yes", [], {}])
def test_chat_enabled_must_be_a_boolean(value: object) -> None:
    assert _code({"chat_enabled": value}) == "invalid_setting"


@pytest.mark.parametrize("value", [0, 731, -1, None, True, False, 30.0, "30", [30], 10**20])
def test_retention_days_must_be_a_whole_number_from_1_to_730(value: object) -> None:
    assert _code({"retention_days": value}) == "invalid_setting"


@pytest.mark.parametrize("value", [1, 90, 730])
def test_retention_days_in_range_are_accepted(value: int) -> None:
    assert parse_update({"retention_days": value}, PROVIDERS) == {"retention_days": value}


@pytest.mark.parametrize("value", ["groq", {"groq": True}, 1, True, [1], [None], [["groq"]], ["groq", "groq"]])
def test_a_provider_list_of_the_wrong_shape_is_invalid_setting(value: object) -> None:
    assert _code({"allowed_llm_providers": value}) == "invalid_setting"


@pytest.mark.parametrize("name", ["Groq", "GROQ", " groq", "groq ", "groq\n", "gemini", "openai", "", "groq:x"])
def test_a_provider_name_must_match_exactly(name: str) -> None:
    assert _code({"allowed_llm_providers": [name]}) == "provider_not_allowed"
    assert _code({"allowed_llm_providers": ["groq", name]}) == "provider_not_allowed"


def test_a_provider_outside_models_json_is_refused_even_when_the_platform_knows_it() -> None:
    # "openrouter" is on the platform allow-list (app/llm/providers.py) but the registry given here names only groq
    assert _code({"allowed_llm_providers": ["openrouter"]}, frozenset({"groq"})) == "provider_not_allowed"


@pytest.mark.parametrize(("value", "stored"), [
    (None, None), ([], ()), (["groq"], ("groq",)), (["openrouter", "groq"], ("groq", "openrouter")),
])
def test_accepted_provider_lists_are_stored_sorted(value: object, stored: object) -> None:
    assert parse_update({"allowed_llm_providers": value}, PROVIDERS) == {"allowed_llm_providers": stored}


@pytest.mark.parametrize("value", [0, -1, True, 1.5, 1.0, "5", [5]])
def test_a_bad_budget_keeps_the_6a5_code(value: object) -> None:
    assert _code({"daily_token_budget": value}) == "token_budget_invalid"


def test_a_budget_above_the_platform_ceiling_keeps_the_6a5_code() -> None:
    ceiling = settings.LLM_PLATFORM_DAILY_TOKENS
    assert _code({"daily_token_budget": ceiling + 1}) == "token_budget_above_platform_ceiling"
    assert parse_update({"daily_token_budget": ceiling}, PROVIDERS) == {"daily_token_budget": ceiling}
    assert parse_update({"daily_token_budget": 1}, PROVIDERS) == {"daily_token_budget": 1}
    assert parse_update({"daily_token_budget": None}, PROVIDERS) == {"daily_token_budget": None}


def test_an_unknown_field_wins_over_a_bad_value_and_nothing_is_partly_accepted() -> None:
    assert _code({"retention_days": 0, "zzz": 1}) == "invalid_setting"
    assert _code({"chat_enabled": False, "retention_days": 0}) == "invalid_setting"


def test_every_refusal_has_a_fixed_text_without_the_value() -> None:
    for body, providers in (
        ({"zzz": "CANARY"}, PROVIDERS), ({"allowed_llm_providers": ["CANARY"]}, PROVIDERS),
        ({"daily_token_budget": "CANARY"}, PROVIDERS), ({"retention_days": "CANARY"}, PROVIDERS),
    ):
        with pytest.raises(SettingsRefused) as err:
            parse_update(body, providers)
        assert "CANARY" not in err.value.message and "CANARY" not in str(err.value)
        assert err.value.message == chat_settings.error_message(err.value.code)


def test_the_change_record_holds_only_changed_fields_with_old_and_new_values() -> None:
    old = ChatSettings(chat_enabled=True, retention_days=90, allowed_llm_providers=None, daily_token_budget=None)
    update = {"chat_enabled": True, "retention_days": 30, "allowed_llm_providers": ("groq",), "daily_token_budget": None}
    assert changes(old, update) == {
        "retention_days": {"old": 90, "new": 30},
        "allowed_llm_providers": {"old": None, "new": ["groq"]},
    }
    assert changes(old, {"chat_enabled": True}) == {}


def test_the_defaults_are_the_column_defaults_and_the_effective_budget_follows_the_override() -> None:
    expected = ChatSettings(chat_enabled=True, retention_days=90, allowed_llm_providers=None, daily_token_budget=None)
    assert expected == DEFAULTS
    assert DEFAULTS.effective_daily_token_budget == settings.LLM_TENANT_DAILY_TOKENS
    custom = ChatSettings(chat_enabled=True, retention_days=90, allowed_llm_providers=(), daily_token_budget=1234)
    assert custom.effective_daily_token_budget == 1234
