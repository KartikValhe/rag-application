"""
Step 6a-3 (docs/phase6-plan.md 7.6, F17): a logging processor drops every event key that is named like a
credential, and the httpx and httpcore loggers stay at WARNING so request lines are not written.
"""

import logging
from collections.abc import Generator, Iterator
from contextlib import contextmanager

import pytest
import structlog
from pydantic import SecretStr
from structlog.testing import capture_logs

from app.core import logging as app_logging
from app.core.logging import (
    CREDENTIAL_KEY_NAMES,
    configure_logging,
    drop_credential_keys,
    quiet_http_loggers,
)

CANARY = "sk-canary-logging-9d41c2e7"


def run(event: dict[str, object]) -> dict[str, object]:
    return dict(drop_credential_keys(None, "info", dict(event)))


@pytest.mark.parametrize(
    "name",
    [
        "authorization", "Authorization", "AUTHORIZATION", "proxy-authorization", "x-goog-api-key", "X-Goog-Api-Key",
        "api_key", "apikey", "API_KEY", "openrouter_api_key", "groq_api_key", "x-api-key", "cookie", "Cookie",
        "set-cookie", "password", "client_secret", "secret", "headers_authorization",
    ],
)
def test_keys_named_like_a_credential_are_dropped(name: str) -> None:
    out = run({"event": "provider_call", name: CANARY, "status": 200})
    assert name not in out
    assert out == {"event": "provider_call", "status": 200}


@pytest.mark.parametrize(
    "name",
    [
        "event", "status", "max_output_tokens", "prompt_tokens", "completion_tokens", "tokens", "model_key",
        "request_id", "tenant_id", "provider", "error_type", "keyword", "author",
    ],
)
def test_ordinary_keys_are_kept(name: str) -> None:
    assert run({name: "value"}) == {name: "value"}


def test_credentials_inside_a_nested_dict_or_list_are_dropped_too() -> None:
    event = {"event": "x", "request": {"headers": {"Authorization": CANARY, "accept": "x"}, "items": [{"api_key": CANARY, "ok": 1}]}}
    out = run(event)
    assert CANARY not in repr(out)
    assert out["request"] == {"headers": {"accept": "x"}, "items": [{"ok": 1}]}


def test_a_secret_str_value_never_shows_its_text() -> None:
    out = run({"event": "x", "something": SecretStr(CANARY)})
    assert CANARY not in repr(out) and CANARY not in str(out)


def test_the_list_of_names_is_the_plan_list() -> None:
    assert {"authorization", "api_key", "x-goog-api-key", "cookie"} <= set(CREDENTIAL_KEY_NAMES)


@contextmanager
def restored_logging() -> Generator[None]:
    """Put logging back as it was. structlog is restored to its OWN earlier configuration with the SAME processor
    list instance (6a-10c fix): a logger cached before (cache_logger_on_first_use) holds that instance, and
    capture_logs edits it in place, so the old structlog.reset_defaults() (a new list) left every logger cached
    before this file out of all later captures (tests/test_query_embedding.py went red once the chat tests used
    that module's logger first)."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    named = {n: logging.getLogger(n).level for n in ("httpx", "httpcore")}
    config = structlog.get_config()
    processors = config["processors"]
    saved = list(processors)
    try:
        yield
    finally:
        root.handlers = handlers
        root.setLevel(level)
        for n, lvl in named.items():
            logging.getLogger(n).setLevel(lvl)
        processors[:] = saved
        structlog.configure(
            processors=processors, wrapper_class=config["wrapper_class"], context_class=config["context_class"],
            logger_factory=config["logger_factory"], cache_logger_on_first_use=config["cache_logger_on_first_use"],
        )


@pytest.fixture
def restore_logging() -> Iterator[None]:
    with restored_logging():
        yield


def test_a_logger_cached_before_a_reconfiguration_is_still_captured_after_it() -> None:
    with restored_logging():  # the outer block puts the caching flag back too
        structlog.configure(cache_logger_on_first_use=True)
        cached =structlog.get_logger("tests.cached_before_reconfigure")
        cached.debug("first_use")  # cached now
        with restored_logging():
            configure_logging()
        with capture_logs() as logs:
            cached.warning("after_restore")
    assert [e["event"] for e in logs] == ["after_restore"]


def test_quiet_http_loggers_sets_warning(restore_logging: None) -> None:
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    quiet_http_loggers()
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).level == logging.WARNING
        assert not logging.getLogger(name).isEnabledFor(logging.INFO)
    assert not logging.getLogger("httpcore.http11").isEnabledFor(logging.DEBUG)  # children follow the parent


def test_configure_logging_applies_both(
    restore_logging: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(app_logging.settings, "LOG_LEVEL", "DEBUG")
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    configure_logging()
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
    logging.getLogger("httpx").info("HTTP Request: POST https://api.groq.com/openai/v1/chat/completions")
    structlog.get_logger("llm_test").info("provider_call", authorization=f"Bearer {CANARY}", api_key=CANARY, status=200)
    logging.getLogger("some.other.logger").info("plain stdlib line")
    out = capsys.readouterr().out
    assert CANARY not in out
    assert "HTTP Request" not in out  # httpx request lines are not written
    assert "provider_call" in out and "plain stdlib line" in out  # ordinary logging still works


def test_configure_logging_renders_json_without_the_key(
    restore_logging: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(app_logging.settings, "ENVIRONMENT", "production", raising=False)
    configure_logging()
    structlog.get_logger("llm_test").info("provider_call", **{"x-goog-api-key": CANARY, "cookie": CANARY, "model_key": "groq:x"})
    out = capsys.readouterr().out
    assert CANARY not in out and "groq:x" in out
