"""
app/llm/chain.py
────────────────
The one way the rest of the app reaches a language model (docs/phase6-plan.md 7.1, 7.4, 7.7):

    answer_stream(purpose, req, tenant_ctx) -> ChainStream   (an AsyncIterator[LLMEvent])

It picks the models for the purpose (LLM_ANSWER_CHAIN or LLM_CONDENSE_CHAIN, in order), applies the policy,
the cooldowns, the breaker and the provider RPM/RPD limits, retries and falls back. No other module imports an
adapter (tests/test_llm_chain_imports.py scans for it).

Rules (plan 7.4):
  * `req.model_key` is replaced by each model tried; everything else of the request is sent unchanged;
  * before every call: the key passes the start-up policy again (`policy.key_problems`), the model is not
    cooling down, the provider's breaker admits it, and one atomic SlotManager acquire takes a slot on both
    `llm:rpm:{model_key}` (60 s) and `llm:rpd:{model_key}` (86,400 s). A full slot consumes nothing on the
    other key. Any of these refusing means: skip the model, go to the next one;
  * token window (step 6a-10b): a model whose models.json entry has a `tpm` first reserves the estimated prompt
    plus max_output_tokens (the budget service's estimator) in `llm:tpm:{model_key}` with one atomic script
    (app/llm/tpm.py). That comes BEFORE the RPM/RPD acquire, so a full window takes nothing from them, and a
    reservation that a full RPM/RPD slot then refuses is given back. A full window skips the model (`tpm_limit`,
    the attempt carries the retry-after) and never waits; no cooldown is set, because a lighter request may fit. A
    request heavier than the model's whole `tpm` can never fit: `tpm_oversize`, at once, no Redis call for it. Each
    model is checked against its OWN limit; a model without `tpm` gets no check and no Redis call. After the call
    the window is corrected to the provider-reported prompt + completion tokens; a failure before the first token
    refunds the whole reservation; a cancel or an interruption after tokens corrects to the best known count (the
    reported usage if it arrived, else the prompt estimate plus the estimate of the text sent);
  * retry at most once on the same model, and ONLY before the first token: after full jitter of 0.5-2 s, or
    after Retry-After when it is 5 s or less. A 429 with a longer Retry-After goes straight to the next model.
    The retry is a new request: it passes the checks above again (and takes its own slot);
  * fall back on LLMRateLimited, LLMUnavailable, LLMTimeout (and every skip above). NEVER on LLMBadRequest,
    LLMContentBlocked or LLMAuthError: the error goes to the caller as it is. An auth error also opens that
    provider's breaker and is logged at error level for the operator;
  * a failure after the first token is neither retried nor failed over: LLMStreamInterrupted, which carries
    the provider's error. The student already saw text;
  * breaker failures are LLMUnavailable and LLMTimeout (before or after the first token). A 429 is a cooldown,
    not a breaker failure. A half-open probe is reported as soon as the first token arrives (the provider is
    answering), or by its error; a probe that ends with neither gives its permit back;
  * a 429 sets the cooldown `llm:cooldown:{model_key}` for Retry-After (also when the retry waits it out
    itself, so other requests skip the model meanwhile). A 429 without Retry-After sets none;
  * timeouts: the request's four limits (LLMTimeouts.from_settings at the caller). `total` is the whole
    answer: it runs from the first attempt to the last token, so every later attempt gets only what is left
    (each of its limits cut to that), and no attempt starts once it is used up. The chain also enforces first
    token, idle and total itself around every event, whatever the client does;
  * every model failing (or skipped) ends in LLMChainExhausted, which carries only model keys and fixed result
    codes. An empty ANSWER chain raises it with `not_configured=True` (the caller answers retrieval-only, plan
    7.5). An empty CONDENSE chain is not an error: the stream yields nothing, `configured` is False and the
    caller uses the raw question;
  * Redis down: outside development the breaker and cooldown checks, the token window and SlotManager raise the
    503 `service_unavailable` before any call. In development the call goes ahead without breaker or cooldown
    (SlotManager and the token window use their per-process limits) and an error is logged;
  * nothing of the question, the system prompt or the answer is written to Redis or to a log: keys hold model
    keys or provider names, log lines hold ids, model keys and result codes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import random
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from contextlib import aclosing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, get_args

import structlog

from app.core import config
from app.core.slots import SlotLimit, SlotManager, SlotRejected, default_manager
from app.llm.breaker import Admission, Breaker, Cooldowns, Permit
from app.llm.client import LLMClient
from app.llm.errors import (
    LLMAuthError,
    LLMBadRequest,
    LLMContentBlocked,
    LLMError,
    LLMRateLimited,
    LLMTimeout,
    LLMUnavailable,
)
from app.llm.gemini import GeminiClient
from app.llm.groq import GroqClient
from app.llm.models import ModelRegistry, Purpose, RegisteredModel, load_registry
from app.llm.openrouter import OpenRouterClient
from app.llm.policy import chain_keys, key_problems
from app.llm.providers import split_model_key
from app.llm.tpm import TpmReservation, TpmWindow
from app.llm.types import LLMEvent, LLMRequest, LLMTimeouts, TokenEvent, UsageEvent
from app.services.llm_budget import estimate_prompt_tokens, estimate_text_tokens, reservation_amount

if TYPE_CHECKING:
    from app.core.config import Settings

logger = structlog.get_logger(__name__)

RETRY_AFTER_MAX_S: Final = 5.0
JITTER_MIN_S: Final = 0.5
JITTER_MAX_S: Final = 2.0
RPM_WINDOW_S: Final = 60.0
RPD_WINDOW_S: Final = 86_400.0

Sleep = Callable[[float], Awaitable[None]]


def retry_jitter_s() -> float:
    """Full jitter for the one retry (plan 7.4). Not a security value."""
    return random.uniform(JITTER_MIN_S, JITTER_MAX_S)  # noqa: S311


def rpm_key(model_key: str) -> str:
    return f"llm:rpm:{model_key}"


def rpd_key(model_key: str) -> str:
    return f"llm:rpd:{model_key}"


# ── what the caller passes and gets back ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TenantLLMContext:
    """Who the call is for. Ids only: used in log lines, never in a Redis key or a request to a provider."""

    tenant_id: uuid.UUID

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, uuid.UUID):
            raise TypeError("tenant_id must be a UUID")


@dataclass(frozen=True, slots=True)
class Attempt:
    """One model the chain tried or skipped, and a fixed result code (never text from a provider).
    `retry_after_s` is set only by `tpm_limit`: the seconds until the model's token window would take this request."""

    model_key: str
    result: str
    retry_after_s: int | None = None


class LLMChainExhausted(Exception):
    """No model could answer before the first token. Not an LLMError: nothing may fall back on it."""

    def __init__(self, attempts: tuple[Attempt, ...], *, not_configured: bool = False) -> None:
        self.attempts = attempts
        self.not_configured = not_configured
        super().__init__("no model is configured" if not_configured else "no model could answer")


class LLMStreamInterrupted(Exception):
    """A model failed after its first token. The partial answer stands; nothing was retried or failed over."""

    def __init__(self, error: LLMError, model_key: str) -> None:
        self.error = error
        self.model_key = model_key
        super().__init__(f"answer interrupted after the first token ({type(error).__name__})")


_RESULT_CODES: Final = (
    (LLMRateLimited, "rate_limited"),
    (LLMUnavailable, "unavailable"),
    (LLMTimeout, "timeout"),
    (LLMBadRequest, "bad_request"),
    (LLMContentBlocked, "content_blocked"),
    (LLMAuthError, "auth_error"),
)


def _result_code(error: LLMError) -> str:
    for kind, code in _RESULT_CODES:
        if isinstance(error, kind):
            return code
    return "error"


class ChainStream:
    """The events of one answer, plus what happened: `model_key` (the model that answered, set at its first
    token), `attempts`, and `configured` (False only for an empty condense chain)."""

    def __init__(self, purpose: Purpose, *, configured: bool) -> None:
        self.purpose = purpose
        self.configured = configured
        self.model_key: str | None = None
        self.attempts: list[Attempt] = []
        self._gen: AsyncGenerator[LLMEvent, None] | None = None

    def _bind(self, gen: AsyncGenerator[LLMEvent, None]) -> None:
        self._gen = gen

    def __aiter__(self) -> ChainStream:
        return self

    async def __anext__(self) -> LLMEvent:
        assert self._gen is not None
        return await self._gen.__anext__()

    async def aclose(self) -> None:
        if self._gen is not None:
            await self._gen.aclose()


@dataclass(slots=True)
class _Step:
    """How one attempt ended, for the loop in `_run`."""

    done: bool = False
    retry_delay: float | None = None  # set: the error allows the one retry, after this many seconds


def _cut(timeouts: LLMTimeouts, left: float) -> LLMTimeouts:
    """The request's limits, none of them longer than what is left of the whole answer."""
    return LLMTimeouts(
        connect=min(timeouts.connect, left),
        first_token=min(timeouts.first_token, left),
        idle=min(timeouts.idle, left),
        total=min(timeouts.total, left),
    )


# ── the chain ────────────────────────────────────────────────────────────────────────────────────────


class LLMChain:
    def __init__(
        self,
        *,
        clients: Mapping[str, LLMClient],
        registry: ModelRegistry | None = None,
        settings: Settings | None = None,
        slots: SlotManager | None = None,
        tpm: TpmWindow | None = None,
        breaker: Breaker | None = None,
        cooldowns: Cooldowns | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        jitter: Callable[[], float] = retry_jitter_s,
    ) -> None:
        self._clients = dict(clients)
        self._registry = registry if registry is not None else load_registry()
        self._settings = settings  # None: the app settings, read at every call
        self._slots = slots if slots is not None else default_manager
        self._tpm = tpm if tpm is not None else TpmWindow()
        self._breaker = breaker if breaker is not None else Breaker()
        self._cooldowns = cooldowns if cooldowns is not None else Cooldowns()
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter

    def _current_settings(self) -> Settings:
        return self._settings if self._settings is not None else config.settings

    def client_names(self) -> tuple[str, ...]:
        return tuple(self._clients)

    def is_configured(self, purpose: Purpose) -> bool:
        return bool(chain_keys(self._current_settings(), purpose))

    async def aclose(self) -> None:
        """Close the clients that hold connections (each adapter's shared httpx client)."""
        seen: set[int] = set()
        for client in self._clients.values():
            close = getattr(client, "aclose", None)
            if close is not None and id(client) not in seen:
                seen.add(id(client))
                await close()

    def answer_stream(self, purpose: Purpose, req: LLMRequest, tenant_ctx: TenantLLMContext) -> ChainStream:
        if purpose not in get_args(Purpose):
            raise ValueError("purpose must be 'answer' or 'condense'")
        if not isinstance(req, LLMRequest):
            raise TypeError("req must be an LLMRequest")
        if not isinstance(tenant_ctx, TenantLLMContext):
            raise TypeError("tenant_ctx must be a TenantLLMContext")
        keys = chain_keys(self._current_settings(), purpose)
        stream = ChainStream(purpose, configured=bool(keys))
        stream._bind(self._run(purpose, keys, req, tenant_ctx, stream))  # noqa: SLF001
        return stream

    # ── the loop over models ──

    async def _run(
        self, purpose: Purpose, keys: tuple[str, ...], req: LLMRequest, ctx: TenantLLMContext, report: ChainStream
    ) -> AsyncGenerator[LLMEvent, None]:
        log = logger.bind(purpose=purpose, tenant_id=str(ctx.tenant_id), request_id=req.request_id)
        if not keys:
            if purpose == "condense":
                log.info("llm_condense_not_configured")
                return
            log.warning("llm_chain_not_configured")
            raise LLMChainExhausted((), not_configured=True)
        settings = self._current_settings()
        deadline = self._clock() + req.timeouts.total
        for key in keys:
            if key_problems(settings, self._registry, key, purpose):
                self._record(report, log, key, "not_allowed", 0)
                log.error("llm_key_not_allowed", model_key=key)
                continue
            provider, _ = split_model_key(key)
            model = self._registry.get(key)
            client = self._clients.get(provider)
            if model is None or client is None:
                self._record(report, log, key, "no_client", 0)
                continue
            for attempt in range(2):
                if self._clock() >= deadline:
                    self._record(report, log, key, "deadline", attempt)
                    raise LLMChainExhausted(tuple(report.attempts))
                step = _Step()
                async with aclosing(
                    self._attempt(key, provider, model, client, attempt, req, deadline, step, report, log)
                ) as events:
                    async for event in events:
                        yield event
                if step.done:
                    return
                delay = step.retry_delay
                if delay is None or attempt == 1 or self._clock() + delay >= deadline:
                    break
                await self._sleep(delay)
        log.warning("llm_chain_exhausted", attempts=len(report.attempts))
        raise LLMChainExhausted(tuple(report.attempts))

    def _record(
        self,
        report: ChainStream,
        log: structlog.stdlib.BoundLogger,
        key: str,
        result: str,
        attempt: int,
        retry_after_s: int | None = None,
    ) -> None:
        report.attempts.append(Attempt(model_key=key, result=result, retry_after_s=retry_after_s))
        if retry_after_s is None:
            log.info("llm_attempt", model_key=key, attempt=attempt, result=result)
        else:
            log.info("llm_attempt", model_key=key, attempt=attempt, result=result, retry_after_s=retry_after_s)

    def _retry_delay(self, error: LLMError, attempt: int) -> float | None:
        if attempt > 0:
            return None
        if isinstance(error, LLMRateLimited) and error.retry_after_s is not None:
            return error.retry_after_s if error.retry_after_s <= RETRY_AFTER_MAX_S else None
        return self._jitter()

    # ── one call to one model ──

    async def _attempt(
        self,
        key: str,
        provider: str,
        model: RegisteredModel,
        client: LLMClient,
        attempt: int,
        req: LLMRequest,
        deadline: float,
        step: _Step,
        report: ChainStream,
        log: structlog.stdlib.BoundLogger,
    ) -> AsyncGenerator[LLMEvent, None]:
        if await self._cooldowns.remaining_ms(key) > 0:
            self._record(report, log, key, "cooldown", attempt)
            return
        permit = await self._breaker.admit(provider)
        if permit.state is Admission.OPEN:
            self._record(report, log, key, "breaker_open", attempt)
            return
        reported = False  # whether the probe (if this is one) has been reported or handed back
        window: TpmReservation | None = None  # the token window's reservation, settled exactly once in the finally
        usage: UsageEvent | None = None
        sent_text: list[str] = []
        try:
            if model.tpm is not None:
                outcome = await self._tpm.reserve(key, model.tpm, reservation_amount(req))
                if not isinstance(outcome, TpmReservation):
                    full = outcome.reason == "full"
                    self._record(report, log, key, "tpm_limit" if full else "tpm_oversize", attempt, outcome.retry_after_seconds)
                    return
                window = outcome
            try:
                await self._slots.acquire(
                    [
                        SlotLimit(key=rpm_key(key), limit=model.rpm, ttl_seconds=RPM_WINDOW_S),
                        SlotLimit(key=rpd_key(key), limit=model.rpd, ttl_seconds=RPD_WINDOW_S),
                    ]
                )
            except SlotRejected as rejected:
                self._record(report, log, key, "rpm_limit" if rejected.index == 0 else "rpd_limit", attempt)
                return
            left = deadline - self._clock()
            if left <= 0:
                self._record(report, log, key, "deadline", attempt)
                return
            timeouts = _cut(req.timeouts, left)
            sent = dataclasses.replace(req, model_key=key, timeouts=timeouts)
            tokens = 0
            try:
                async with aclosing(self._timed(client.stream(sent), key, provider, timeouts, deadline)) as events:
                    async for event in events:
                        if isinstance(event, TokenEvent) and tokens == 0:
                            report.model_key = key
                            if permit.state is Admission.PROBE:
                                await self._breaker.record_success(provider, permit)
                                reported = True
                        if isinstance(event, TokenEvent):
                            tokens += 1
                            if window is not None:
                                sent_text.append(event.text)
                        elif isinstance(event, UsageEvent):
                            usage = event
                        yield event
            except LLMError as error:
                reported = await self._after_error(error, key, provider, permit, reported, log) or reported
                if tokens > 0:
                    self._record(report, log, key, "interrupted", attempt)
                    raise LLMStreamInterrupted(error, key) from None
                self._record(report, log, key, _result_code(error), attempt)
                if not error.falls_back:
                    raise
                step.retry_delay = self._retry_delay(error, attempt)
                return
            except Exception:
                self._record(report, log, key, "error", attempt)
                raise
            if permit.state is Admission.PROBE and not reported:  # an answer with no text: still an answer
                await self._breaker.record_success(provider, permit)
                reported = True
            report.model_key = key
            self._record(report, log, key, "ok", attempt)
            step.done = True
        finally:
            if window is not None:
                await self._settle_window(window, req, usage, sent_text, step.done, log)
            if permit.state is Admission.PROBE and not reported:
                await self._breaker.release(provider, permit)

    async def _settle_window(
        self,
        window: TpmReservation,
        req: LLMRequest,
        usage: UsageEvent | None,
        sent_text: list[str],
        completed: bool,
        log: structlog.stdlib.BoundLogger,
    ) -> None:
        """Correct the token window to the best known count, whichever way the attempt ended (never raises):
        the provider's reported prompt + completion tokens when they arrived; else, when the answer completed or
        any text was sent, the prompt estimate plus the estimate of the text sent; else nothing was used (a
        failure, a skip or a cancel before the first token) and the whole reservation is given back."""
        if usage is not None:
            used = usage.prompt_tokens + usage.completion_tokens
        elif completed or sent_text:
            used = estimate_prompt_tokens(req) + estimate_text_tokens("".join(sent_text))
        else:
            used = 0
        await self._tpm.settle(window, used)
        log.info("llm_tpm_settled", model_key=window.model_key, reserved=window.weight, used=used)

    async def _after_error(
        self,
        error: LLMError,
        key: str,
        provider: str,
        permit: Permit,
        reported: bool,
        log: structlog.stdlib.BoundLogger,
    ) -> bool:
        """Cooldown and breaker bookkeeping for an error. Returns True when the probe was reported."""
        if isinstance(error, LLMRateLimited) and error.retry_after_s:
            await self._cooldowns.start(key, error.retry_after_s)
        probe = permit if (permit.state is Admission.PROBE and not reported) else Breaker.untracked()
        if isinstance(error, LLMAuthError):
            log.error("llm_auth_failed", provider=provider, model_key=key)
            await self._breaker.trip(provider)
            return probe.probe_token is not None
        if isinstance(error, (LLMUnavailable, LLMTimeout)):
            await self._breaker.record_failure(provider, probe)
            return probe.probe_token is not None
        if probe.probe_token is not None:  # a 429 or a refusal: the provider is up and answering
            await self._breaker.record_success(provider, probe)
            return True
        return False

    async def _timed(
        self, events: AsyncIterator[LLMEvent], key: str, provider: str, timeouts: LLMTimeouts, deadline: float
    ) -> AsyncGenerator[LLMEvent, None]:
        """The client's events with the first-token, idle and total limits enforced on the chain's clock, and a
        real-time stop for a client that never yields."""
        it = events.__aiter__()
        started = self._clock()
        last_token: float | None = None
        try:
            while True:
                wait_until = started + timeouts.first_token if last_token is None else last_token + timeouts.idle
                limit = min(wait_until, deadline)
                left = limit - self._clock()
                if left <= 0:
                    raise LLMTimeout(provider=provider, model_key=key)
                timed_out = False
                try:
                    async with asyncio.timeout(left):
                        event = await anext(it)
                except StopAsyncIteration:
                    return
                except TimeoutError:
                    timed_out = True
                if timed_out:  # raised here, outside the except, so no TimeoutError hangs on it
                    raise LLMTimeout(provider=provider, model_key=key)
                if isinstance(event, TokenEvent):
                    now = self._clock()
                    if now > limit:  # it came, but too late
                        raise LLMTimeout(provider=provider, model_key=key)
                    last_token = now
                yield event
        finally:
            close = getattr(it, "aclose", None)
            if close is not None:
                await close()


# ── the process-wide chain on the real adapters ──────────────────────────────────────────────────────

_default: LLMChain | None = None


def default_chain() -> LLMChain:
    """One chain per process, so each provider keeps one shared httpx client (plan 7.2)."""
    global _default
    if _default is None:
        s = config.settings
        _default = LLMChain(
            clients={
                "groq": GroqClient.from_settings(s),
                "openrouter": OpenRouterClient.from_settings(s),
                "gemini": GeminiClient.from_settings(s),
            }
        )
    return _default


async def aclose_default_chain() -> None:
    global _default
    if _default is not None:
        chain, _default = _default, None
        await chain.aclose()


def answer_stream(purpose: Purpose, req: LLMRequest, tenant_ctx: TenantLLMContext) -> ChainStream:
    """The app's one entry point to a language model (plan 7.1)."""
    return default_chain().answer_stream(purpose, req, tenant_ctx)
