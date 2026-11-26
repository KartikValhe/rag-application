"""
app/services/rag_answer.py
──────────────────────────
One RAG answer (docs/phase6-plan.md 2.1 steps E-I, 4, 5, 6, 7.4-7.7, 10.3; step 6a-10c). The SSE route is a thin adapter
around it (app/services/chat_messages.py); the evaluation harness of 6a-13 calls it the same way.

RagAnswer.run() yields what the client is sent (Token, Replace, Ping) and fills `result` (AnswerResult) as it goes:

  D  (6a-11) only when LLM_CONDENSE_CHAIN is set and the question looks like a follow-up (chat_condense): a short
     transaction reads the history and the tenant's LLM settings; with history (never the first turn) and a condense
     model the tenant allows, the condense call rewrites the question for RETRIEVAL ONLY (the masked question and
     history in, one cleaned line out, stored as messages.retrieval_query). Same chain, token window and budget as
     the answer, usage rows of purpose "condense". Any failure: the original question; condense never fails a turn;
  E  the query model is loaded and the CLEANED (or condensed) question embedded with NO transaction open (process
     cap: search_busy);
  F  T2, a short chat transaction: search_for_chat with that vector only (the scope is the one T1 rebuilt and narrowed
     to the conversation), plus the history, the institution name and the tenant's token-budget override.
     Nothing above the threshold (or every hit excluded as flagged): the fixed 4.4 text, NO model call;
  H  the prompt (chat_prompt, rag-v1): the PII-MASKED question and history go into the prompt and nowhere else;
     The tenant's allowed_llm_providers narrows the chain (6a-11): nothing left gives the retrieval-only answer
     (fallback_retrieval_only) with no call and nothing reserved; a provider the tenant does not allow is never called;
  G  the daily token budget is reserved (estimated prompt + max_output_tokens). Exhausted: the retrieval-only answer
     (budget_retrieval_only), no model call;
  I  the chain call (the request type has no field for tools), through the 6a-10a stream filter: no verdict line, no
     invented label; NOT_IN_SOURCES closes the provider stream at once; a retracted answer becomes a Replace with
     the fixed refusal. Every model failing before its first token: the retrieval-only answer
     (fallback_retrieval_only), or answer_unavailable when CHAT_FALLBACK_RETRIEVAL_ONLY is off. A failure after the
     first token: interrupted (the partial text stands). finish_reason "length" or the 8,000-character cap: truncated.

The budget is settled exactly once to the best known count: the provider-reported prompt + completion tokens (hidden
reasoning is inside the completion count); for a NOT_IN_SOURCES stream closed by us before the provider's usage
arrived, the prompt estimate + REFUSAL_COMPLETION_TOKENS_ESTIMATE (estimated); for a stop without usage (cancel,
interruption, cap), the prompt estimate + the estimate of the model text received; nothing at all when no model had
started answering (the 6a-10b rule for the token window). usage_rows() gives every llm_usage_events row of the turn
(6a-11, owner decision 3): the condense call's rows, ONE ROW PER FAILED ATTEMPT (a provider called and failing before
its first token: outcome only, 0 tokens, estimated) and the row of the call that answered or refused (result.usage);
a skipped model made no call and has no row. chat_messages writes them in T3 with the row and the citations.

Citations come from this turn's hits only (chat_citations); students get the student fields only (8.2).
Logs: ids, codes, counts and error class names only. Never the question, the masked question, a source, a title, a
heading or the answer (plan 9.5).
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import anyio
import structlog
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.config import settings
from app.core.tenant_db import chat_session_context
from app.db.models.branding import TenantBranding
from app.db.models.chat import MESSAGE_ROLE_ASSISTANT, MESSAGE_ROLE_USER, OUTCOME_PENDING, Message
from app.llm import chain as llm_chain
from app.llm.chain import (
    Attempt,
    ChainStream,
    LLMChain,
    LLMChainExhausted,
    LLMStreamInterrupted,
    TenantLLMContext,
)
from app.llm.errors import LLMAuthError, LLMBadRequest, LLMContentBlocked, LLMError
from app.llm.providers import split_model_key
from app.llm.types import DoneEvent, FinishReason, LLMRequest, LLMTimeouts, TokenEvent, UsageEvent
from app.services import (
    chat_citations,
    chat_condense,
    chat_prompt,
    chat_sse,
    llm_budget,
    pii,
    query_embedding,
    retrieval,
    tenant_settings,
)
from app.services.chat_citations import CitationSnapshot, PromptSource
from app.services.chat_stream_filter import StreamFilter

logger = structlog.get_logger(__name__)

# Owner decision (6a-10 brief, answer 3): the completion tokens of a NOT_IN_SOURCES answer whose stream we closed
# before the provider reported usage. Source: the 6a-6 real runs (refusals reported 66-95 completion tokens with the
# hidden reasoning; maximum 95).
REFUSAL_COMPLETION_TOKENS_ESTIMATE: Final = 128
RETRIEVAL_ONLY_PASSAGES: Final = 3  # plan 7.5: the top 3 retrieved passages
CHAIN_MODEL_PLACEHOLDER: Final = "chain:answer"  # replaced by every model the chain tries
EMBEDDER_LOG_EVENT: Final = "chat_embedder_unavailable"
REASON_FLAGGED_EXCLUDED: Final = "flagged_excluded"
HISTORY_OUTCOMES_SKIPPED: Final = (OUTCOME_PENDING, "error")

# Plan 4.4: the same text for "nothing found" and for every retracted answer (NOT_IN_SOURCES, no valid citation, the
# leak cut). The Marathi and Hindi texts need the native review of 4.4 before real students see them (pilot gate).
NOT_FOUND_TEXTS: Final[dict[str, str]] = {
    "en": "I couldn't find this in your class material. Try asking in a different way, or ask your teacher.",
    "mr": "हे तुमच्या वर्गाच्या साहित्यात सापडले नाही. प्रश्न वेगळ्या शब्दांत विचारा किंवा तुमच्या शिक्षकांना विचारा.",
    "hi": "यह आपकी कक्षा की सामग्री में नहीं मिला। प्रश्न दूसरे शब्दों में पूछें या अपने शिक्षक से पूछें।",
}
# Plan 7.5 (the honest second sentence is there because of B108, C9). The Marathi text is NOT a plan text: written
# here, it needs the native review of 4.4 (pilot gate). No Hindi text: Hindi falls back to English.
RETRIEVAL_ONLY_TEXTS: Final[dict[str, str]] = {
    "en": "I can't write an answer right now. These parts of your class material matched your question best; they "
          "may not answer it fully.",
    "mr": "मी आत्ता उत्तर लिहू शकत नाही. तुमच्या वर्गाच्या साहित्यातील हे भाग तुमच्या प्रश्नाशी सर्वात जास्त जुळले; "
          "ते तुमच्या प्रश्नाचे पूर्ण उत्तर देतीलच असे नाही.",
}

_LABEL_GROUP_RE: Final = re.compile(r"\[(S\d+(?:\s*,\s*S\d+)*)\]")
_LABEL_RE: Final = re.compile(r"S\d+")

# Provider errors the chain never falls back on (raised before the first token): the turn ends with
# answer_unavailable. Their usage-row outcome.
_REFUSED_CALL_OUTCOMES: Final[tuple[tuple[type[LLMError], str], ...]] = (
    (LLMBadRequest, "bad_request"),
    (LLMContentBlocked, "blocked"),
    (LLMAuthError, "auth_error"),
)


def refusal_text(language: str) -> str:
    return NOT_FOUND_TEXTS.get(language, NOT_FOUND_TEXTS["en"])


def retrieval_only_text(language: str, labels: Sequence[str]) -> str:
    """Our fixed sentence and the labels of the passages; the passages themselves are the citations (title,
    location, heading, snippet), so no chunk text is copied into the answer."""
    sentence = RETRIEVAL_ONLY_TEXTS.get(language, RETRIEVAL_ONLY_TEXTS["en"])
    return f"{sentence}\n\n{' '.join(f'[{label}]' for label in labels)}"


def labels_cited_in(text: str, table: Sequence[str]) -> tuple[str, ...]:
    """The table labels in the label groups of `text` (released by the stream filter, so in ASCII brackets), in the
    order of first citation. Used for an answer that stopped before the filter's end (cancelled, interrupted)."""
    known = set(table)
    out: list[str] = []
    for group in _LABEL_GROUP_RE.finditer(text):
        for label in _LABEL_RE.findall(group.group(1)):
            if label in known and label not in out:
                out.append(label)
    return tuple(out)


# ── What the caller injects (tests replace these) ─────────────────────────────


async def load_query_embedder() -> retrieval.QueryEmbedder:
    """The process-wide query model (loaded on first use, B96)."""
    return await query_embedding.process_query_embedder()


def get_chain() -> LLMChain:
    return llm_chain.default_chain()


def get_budget() -> llm_budget.TokenBudget:
    return llm_budget.default_budget


# ── The items and the result ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Ping:
    """Nothing to send yet: the caller sends a heartbeat."""


@dataclass(frozen=True, slots=True)
class Token:
    text: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class Replace:
    """Replace everything shown with this fixed text (plan 8.3)."""

    text: str = field(repr=False)


RagItem = Ping | Token | Replace
PING: Final = Ping()


@dataclass(frozen=True, slots=True)
class UsageRow:
    """One llm_usage_events row: a provider call of the turn (answer or condense). Numbers and names only."""

    provider: str
    model_key: str
    prompt_tokens: int
    completion_tokens: int
    estimated: bool
    latency_ms: int
    outcome: str
    attempt: int
    purpose: str = "answer"


@dataclass(frozen=True)
class RagInput:
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    conversation_id: uuid.UUID
    user_message_id: uuid.UUID
    message_id: uuid.UUID
    question: str  # cleaned (clean_message), as stored
    language: str  # detected
    template_language: str  # the language of the fixed texts
    scope: retrieval.RetrievalScope  # rebuilt in T1 and narrowed to the conversation's class and subject
    own_name: str | None
    own_email: str | None


@dataclass
class AnswerResult:
    """What the turn ended with (plan 10.3). `outcome` stays pending until run() decides it."""

    outcome: str = OUTCOME_PENDING
    text: str = ""  # the text the client ends with (the stored content)
    error_code: str | None = None  # set: the stream ends with this `error` event instead of `done`
    partial: bool = False
    citations: tuple[CitationSnapshot, ...] = ()
    usage: UsageRow | None = None
    retrieval: dict[str, Any] | None = None  # ids and numbers only
    prompt_version: str | None = None
    invalid_citation_count: int = 0
    flagged_sources_used: int = 0
    needs_review: bool = False
    verdict_form: str | None = None
    hits: int = 0
    flagged_hits: int = 0
    candidate_count: int = 0
    reason: str | None = None
    attempts: int = 0
    latency: dict[str, int] = field(default_factory=dict)
    retrieval_query: str | None = None  # the condensed question, when one was used for retrieval (plan 9.1)
    condense: str | None = None  # what the condense step did: a fixed code (log only), None when it was not a candidate

    def row_values(self) -> dict[str, Any]:
        """The message columns of the answer (T3 or the closing step), besides content, outcome and partial."""
        u = self.usage
        return {
            "retrieval_query": self.retrieval_query,
            "retrieval": self.retrieval,
            "prompt_version": self.prompt_version,
            "provider": u.provider if u else None,
            "model_key": u.model_key if u else None,
            "prompt_tokens": u.prompt_tokens if u else None,
            "completion_tokens": u.completion_tokens if u else None,
            "tokens_estimated": u.estimated if u else False,
            "invalid_citation_count": self.invalid_citation_count,
            "flagged_sources_used": self.flagged_sources_used,
            "needs_review": self.needs_review,
        }

    def usage_event(self) -> dict[str, Any]:
        """The `usage` of the `done` event: the client's own token counts."""
        u = self.usage
        if u is None:
            return {"prompt_tokens": 0, "completion_tokens": 0, "estimated": False}
        return {"prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens, "estimated": u.estimated}

    def log_fields(self) -> dict[str, Any]:
        """The RAG part of the chat_turn log line (plan 10.1): ids, codes and numbers only."""
        u = self.usage
        return {
            "provider": u.provider if u else None,
            "model_key": u.model_key if u else None,
            "prompt_version": self.prompt_version,
            "attempts": self.attempts,
            "hits": self.hits,
            "flagged_hits": self.flagged_hits,
            "candidate_count": self.candidate_count,
            "reason": self.reason,
            "invalid_citation_count": self.invalid_citation_count,
            "prompt_tokens": u.prompt_tokens if u else None,
            "completion_tokens": u.completion_tokens if u else None,
            "tokens_estimated": u.estimated if u else None,
            "verdict_form": self.verdict_form,
            "condensed": self.retrieval_query is not None,
            "condense": self.condense,
        }


@dataclass(frozen=True)
class _Context:
    """What the turn reads besides the search: the history, the institution name, the tenant's LLM settings."""

    history: tuple[chat_prompt.HistoryTurn, ...]
    institution: str
    llm: tenant_settings.TenantLLMSettings


@dataclass(frozen=True)
class _Found:
    result: retrieval.RetrievalResult
    context: _Context | None  # None only when nothing was found and no context had been read before


# Usage-row outcome of a chain attempt that called a provider and failed before its first token (owner decision 3
# of the 6a-11 brief: one row per failed attempt). Skips (cooldown, breaker, limits, tenant_not_allowed, ...) made no
# call and have no row; "interrupted" and "ok" are the call that answered, which has its own row.
_FAILED_ATTEMPT_OUTCOMES: Final[dict[str, str]] = {
    "rate_limited": "rate_limited",
    "unavailable": "unavailable",
    "timeout": "timeout",
    "bad_request": "bad_request",
    "content_blocked": "blocked",
    "auth_error": "auth_error",
}
CONDENSE_RAW_MAX_CHARS: Final = 2000  # stop reading a condense answer past this (the output cap is far lower)


_END: Final = object()


async def _next_event(stream: ChainStream) -> object:
    try:
        return await anext(stream)
    except StopAsyncIteration:
        return _END


def _ms(since: float) -> int:
    return max(0, int((time.monotonic() - since) * 1000))


class _Stop(Exception):  # noqa: N818 (a signal inside run())
    """The turn has its final state; run() ends."""


# ── The answer ────────────────────────────────────────────────────────────────


class RagAnswer:
    def __init__(
        self,
        inp: RagInput,
        *,
        chain: LLMChain | None = None,
        load_embedder: query_embedding.QueryEmbedderLoader | None = None,
        budget: llm_budget.TokenBudget | None = None,
        config: retrieval.RetrievalConfig | None = None,
    ) -> None:
        self._in = inp
        self._chain = chain if chain is not None else get_chain()
        self._load = load_embedder if load_embedder is not None else load_query_embedder
        self._budget = budget if budget is not None else get_budget()
        self._config = config if config is not None else retrieval.RetrievalConfig.from_settings()
        self.result = AnswerResult()
        self._sources: tuple[PromptSource, ...] = ()
        self._req: LLMRequest | None = None
        self._reservation: llm_budget.Reservation | None = None
        self._stream: ChainStream | None = None
        self._reported: UsageEvent | None = None
        self._raw: list[str] = []  # model text received, before the filter (for the estimate)
        self._released: list[str] = []  # what the filter released (what the client was given)
        self._llm_started: float | None = None
        self._context: _Context | None = None  # read before the search when the condense step needs it
        self._condense_rows: list[UsageRow] = []  # the usage rows of the condense call (6a-11)
        self._refused: Attempt | None = None  # the answer attempt that refused the request (its row is result.usage)

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(s.label for s in self._sources)

    def citations_for(self, text: str) -> tuple[CitationSnapshot, ...]:
        """The citation rows of a text the client received (an answer that stopped early): its table labels only."""
        return chat_citations.build_snapshots(labels_cited_in(text, self.labels), self._sources)

    # ── the stream ──

    async def run(self, *, deadline: float, heartbeat: float) -> AsyncGenerator[RagItem]:
        """The items to send, in order. Ends normally once `result` has its final state. A cancel (the client went
        away) or an early close by the caller leaves `result.outcome` pending; the clean-up then still closes the
        provider stream and settles the budget (shielded), and `result.usage` holds the call's usage if a model
        had started answering."""
        try:
            async for item in self._run(deadline, heartbeat):
                yield item
        except _Stop:
            pass
        except chat_sse.StreamDeadline:
            self._end_unfinished("interrupted", "stream_interrupted")
        except retrieval.SearchUnavailable as exc:
            self._fail(exc.code)
        except Exception as exc:
            logger.error("chat_rag_error", error_class=type(exc).__name__, **self._ids())
            self._fail("internal_error")
        finally:
            with anyio.CancelScope(shield=True):
                await self._clean_up()

    def _ids(self) -> dict[str, str]:
        return {"tenant_id": str(self._in.tenant_id), "conversation_id": str(self._in.conversation_id),
                "message_id": str(self._in.message_id)}

    async def _wait[T](self, work: Awaitable[T], box: list[T], deadline: float, interval: float) -> AsyncIterator[Ping]:
        """Await one piece of work, a heartbeat every `interval`, stopped at the stream deadline."""
        async with chat_sse.heartbeat_while(work, box, interval=interval, deadline=deadline) as beats:
            async for _ in beats:
                yield PING

    async def _run(self, deadline: float, interval: float) -> AsyncIterator[RagItem]:
        inp, res = self._in, self.result

        # D: a follow-up is condensed into a standalone question for RETRIEVAL only (6a-11). Any failure: the original.
        retrieval_text = inp.question
        if self._chain.is_configured("condense") and chat_condense.looks_like_follow_up(inp.question):
            context_box: list[_Context] = []
            async for beat in self._wait(self._read_context(), context_box, deadline, interval):
                yield beat
            self._context = context_box[0]
            if not self._context.history:
                res.condense = "first_turn"
            else:
                t = time.monotonic()
                condensed: list[str | None] = []
                async for beat in self._wait(self._condense(self._context), condensed, deadline, interval):
                    yield beat
                res.latency["condense"] = _ms(t)
                if condensed[0] is not None:
                    retrieval_text = res.retrieval_query = condensed[0]

        # E: the cleaned (or condensed) question, embedded with no transaction open
        query = retrieval.normalize_query(retrieval_text, self._config.max_query_chars)
        t = time.monotonic()
        embedded: list[retrieval.QueryEmbedder] = []
        async for beat in self._wait(self._embed(query), embedded, deadline, interval):
            yield beat
        res.latency["embed"] = _ms(t)

        # F: T2
        t = time.monotonic()
        found_box: list[_Found] = []
        async for beat in self._wait(self._search(query, embedded[0]), found_box, deadline, interval):
            yield beat
        res.latency["retrieve"] = _ms(t)
        found = found_box[0]
        self._record_retrieval(found.result)
        if not found.result.hits or found.context is None:
            yield self._fixed_answer("not_found", refusal_text(inp.template_language))
            raise _Stop
        context = found.context

        # H: the prompt, with the masked ORIGINAL question and history (never the condensed text)
        mask = pii.masker(own_name=inp.own_name, own_email=inp.own_email)
        try:
            prompt = chat_prompt.build_prompt(
                hits=found.result.hits, question=inp.question, bot_name="", institution=context.institution,
                language=inp.language, estimate_tokens=llm_budget.estimate_text_tokens, mask=mask,
                history=context.history, context_max_tokens=settings.CHAT_CONTEXT_MAX_TOKENS,
                flagged_mode=settings.CHAT_FLAGGED_SOURCES, history_turns=settings.CHAT_HISTORY_TURNS,
                history_chars=settings.CHAT_HISTORY_CHARS,
            )
        except chat_prompt.NoSources:
            res.reason = REASON_FLAGGED_EXCLUDED
            if res.retrieval is not None:
                res.retrieval["reason"] = REASON_FLAGGED_EXCLUDED
            yield self._fixed_answer("not_found", refusal_text(inp.template_language))
            raise _Stop from None
        self._sources = prompt.sources
        res.prompt_version = prompt.prompt_version
        res.flagged_sources_used = prompt.flagged_sources_used
        req = LLMRequest(
            model_key=CHAIN_MODEL_PLACEHOLDER, system=prompt.system, messages=prompt.messages,
            max_output_tokens=settings.CHAT_MAX_OUTPUT_TOKENS, timeouts=LLMTimeouts.from_settings(settings),
            request_id=str(inp.message_id),
        )
        self._req = req

        # The tenant's allowed_llm_providers narrows the chain (6a-11). Nothing left: the retrieval-only answer of
        # 7.5 (C32), with no call and nothing reserved; a disallowed provider is never used instead.
        ctx = TenantLLMContext(tenant_id=inp.tenant_id, allowed_providers=context.llm.allowed_llm_providers)
        if not self._chain.is_configured("answer", ctx):
            logger.info("chat_llm_chain_empty", narrowed=ctx.allowed_providers is not None, **self._ids())
            for item in self._retrieval_only("fallback_retrieval_only"):
                yield item
            raise _Stop

        # G: the daily token budget, reserved before the call
        reserved = await self._budget.reserve(inp.tenant_id, llm_budget.reservation_amount(req),
                                              tenant_override=context.llm.token_budget_override)
        if isinstance(reserved, llm_budget.BudgetExhausted):
            logger.info("chat_budget_exhausted", scope=reserved.scope, **self._ids())
            for item in self._retrieval_only("budget_retrieval_only"):
                yield item
            raise _Stop
        self._reservation = reserved

        # I: the chain, through the stream filter
        async for item in self._answer(req, prompt, ctx, deadline, interval):
            yield item

    async def _embed(self, query: str) -> retrieval.QueryEmbedder:
        embedder = await query_embedding.guarded_query_embedder(self._load, log_event=EMBEDDER_LOG_EVENT)
        if self._in.scope.pairs:  # an empty scope has nothing to compare with: nothing is embedded
            embedder = await retrieval.embed_query(embedder, query)
        return embedder

    async def _search(self, query: str, embedder: retrieval.QueryEmbedder) -> _Found:
        inp = self._in
        async with chat_session_context(inp.tenant_id, inp.user_id) as db:
            result = await retrieval.search_for_chat(db, inp.scope, query, embedder, self._config)
            if not result.hits:
                return _Found(result, self._context)
            context = self._context if self._context is not None else await self._context_in(db)
        return _Found(result, context)

    async def _read_context(self) -> _Context:
        """A short chat transaction of its own, before the condense call (no transaction is open during that call)."""
        inp = self._in
        async with chat_session_context(inp.tenant_id, inp.user_id) as db:
            return await self._context_in(db)

    async def _context_in(self, db: AsyncSession) -> _Context:
        inp = self._in
        history = await self._history(db)
        institution = (await db.execute(
            select(TenantBranding.display_name).where(TenantBranding.tenant_id == inp.tenant_id)
        )).scalar_one_or_none() or ""
        llm = await tenant_settings.read_llm_settings(db, inp.tenant_id)
        return _Context(history, institution, llm)

    # ── the condense call (6a-11) ──

    async def _condense(self, context: _Context) -> str | None:
        """The standalone question for retrieval, or None: the original question is used then. NEVER raises (only a
        cancel passes): every failure, timeout, refusal, empty or unusable output, an exhausted budget or a tenant
        that allows no condense provider gives None. Same chain, token window and daily budget as the answer; the
        usage rows (purpose "condense") are kept for T3. Called with no transaction open."""
        inp, res = self._in, self.result
        ctx = TenantLLMContext(tenant_id=inp.tenant_id, allowed_providers=context.llm.allowed_llm_providers)
        if not self._chain.is_configured("condense", ctx):
            res.condense = "not_allowed"
            return None
        mask = pii.masker(own_name=inp.own_name, own_email=inp.own_email)
        req = chat_condense.build_request(question=inp.question, history=context.history, mask=mask,
                                          request_id=str(inp.message_id), settings=settings)
        try:
            reserved = await self._budget.reserve(inp.tenant_id, llm_budget.reservation_amount(req),
                                                  tenant_override=context.llm.token_budget_override)
        except Exception as exc:
            logger.warning("chat_condense_budget_error", error_class=type(exc).__name__, **self._ids())
            res.condense = "error"
            return None
        if isinstance(reserved, llm_budget.BudgetExhausted):
            res.condense = "budget"
            return None
        stream = self._chain.answer_stream("condense", req, ctx)
        started = time.monotonic()
        raw: list[str] = []
        usage: UsageEvent | None = None
        final: str | None = None  # the outcome of the call that answered: ok, interrupted or cancelled
        try:
            async for event in stream:
                if isinstance(event, TokenEvent):
                    raw.append(event.text)
                    if sum(len(t) for t in raw) > CONDENSE_RAW_MAX_CHARS:
                        break  # far past the output cap: what came so far decides
                elif isinstance(event, UsageEvent):
                    usage = event
            final = "ok"
        except LLMStreamInterrupted as exc:
            final = "interrupted"
            logger.info("chat_condense_failed", error_class=type(exc.error).__name__, **self._ids())
        except (LLMChainExhausted, LLMError) as exc:
            logger.info("chat_condense_failed", error_class=type(exc).__name__, **self._ids())
        except Exception as exc:
            logger.warning("chat_condense_failed", error_class=type(exc).__name__, **self._ids())
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await stream.aclose()
                except Exception as exc:
                    logger.error("chat_llm_close_error", error_class=type(exc).__name__, **self._ids())
                if final is None and stream.model_key is not None:
                    final = "cancelled"  # the client went away after the model had started answering
                await self._close_condense(stream, req, reserved, raw, usage, final, started)
        if final != "ok":
            res.condense = "failed"
            return None
        text = chat_condense.clean_output("".join(raw))
        res.condense = "used" if text is not None else "unusable"
        return text

    async def _close_condense(
        self, stream: ChainStream, req: LLMRequest, reserved: llm_budget.Reservation, raw: list[str],
        usage: UsageEvent | None, final: str | None, started: float,
    ) -> None:
        """The usage rows of the condense call and the budget settled to the best known count (never raises)."""
        rows = self._failed_rows(stream.attempts, "condense")
        used = 0
        if final is not None and stream.model_key is not None:
            if usage is not None:
                prompt, completion, estimated = usage.prompt_tokens, usage.completion_tokens, usage.estimated
            else:
                prompt = llm_budget.estimate_prompt_tokens(req)
                completion, estimated = llm_budget.estimate_text_tokens("".join(raw)), True
            used = prompt + completion
            provider, _ = split_model_key(stream.model_key)
            number = max((a.number for a in stream.attempts if a.model_key == stream.model_key), default=1)
            rows.append(UsageRow(provider=provider, model_key=stream.model_key, prompt_tokens=prompt,
                                 completion_tokens=completion, estimated=estimated, latency_ms=_ms(started),
                                 outcome=final, attempt=min(number, 2), purpose="condense"))
        self._condense_rows = rows
        try:
            if used:
                await self._budget.settle(reserved, used)
            else:
                await self._budget.release(reserved)
        except Exception as exc:
            logger.error("chat_budget_settle_error", error_class=type(exc).__name__, **self._ids())

    @staticmethod
    def _failed_rows(attempts: Sequence[Attempt], purpose: str, skip: Attempt | None = None) -> list[UsageRow]:
        """One row per provider call that failed before its first token (owner decision 3): outcome only, no
        tokens (the provider reported none), estimated. A skipped model made no call and has no row."""
        rows: list[UsageRow] = []
        for a in attempts:
            outcome = _FAILED_ATTEMPT_OUTCOMES.get(a.result)
            if outcome is None or a is skip:
                continue
            provider, _ = split_model_key(a.model_key)
            rows.append(UsageRow(provider=provider, model_key=a.model_key, prompt_tokens=0, completion_tokens=0,
                                 estimated=True, latency_ms=a.latency_ms or 0, outcome=outcome,
                                 attempt=min(a.number, 2), purpose=purpose))
        return rows

    def usage_rows(self) -> tuple[UsageRow, ...]:
        """Every llm_usage_events row of the turn, for T3 or the closing step: the condense call's rows, one row per
        failed answer attempt, and the row of the answer call that answered or refused (result.usage)."""
        rows = list(self._condense_rows)
        if self._stream is not None:
            rows += self._failed_rows(self._stream.attempts, "answer", skip=self._refused)
        if self.result.usage is not None:
            rows.append(self.result.usage)
        return tuple(rows)

    async def _history(self, db: AsyncSession) -> tuple[chat_prompt.HistoryTurn, ...]:
        """The last CHAT_HISTORY_TURNS finished exchanges of this conversation before this question."""
        inp = self._in
        if settings.CHAT_HISTORY_TURNS <= 0:
            return ()
        question = aliased(Message)
        answer = aliased(Message)
        rows = (await db.execute(
            select(question.content, answer.content)
            .select_from(question)
            .join(answer, and_(answer.tenant_id == question.tenant_id, answer.conversation_id == question.conversation_id,
                               answer.reply_to_id == question.id, answer.role == MESSAGE_ROLE_ASSISTANT))
            .where(question.tenant_id == inp.tenant_id, question.user_id == inp.user_id,
                   question.conversation_id == inp.conversation_id, question.role == MESSAGE_ROLE_USER,
                   question.id != inp.user_message_id, answer.outcome.notin_(HISTORY_OUTCOMES_SKIPPED),
                   answer.content != "")
            .order_by(question.created_at.desc())
            .limit(settings.CHAT_HISTORY_TURNS)
        )).all()
        return tuple(chat_prompt.HistoryTurn(question=q, answer=a) for q, a in reversed(rows))

    def _record_retrieval(self, r: retrieval.RetrievalResult) -> None:
        res = self.result
        res.hits = len(r.hits)
        res.flagged_hits = sum(1 for h in r.hits if h.flagged)
        res.candidate_count = r.candidate_count
        res.reason = r.reason
        res.retrieval = {
            "chunk_ids": [str(h.chunk_id) for h in r.hits],
            "similarities": [h.vector_similarity for h in r.hits],
            "threshold": r.threshold,
            "threshold_calibrated": r.threshold_calibrated,
            "model": r.model,
            "candidate_count": r.candidate_count,
            "path": r.path,
            "reason": r.reason,
            "max_similarity": r.max_similarity,
        }

    def _fixed_answer(self, outcome: str, text: str) -> Token:
        self.result.outcome, self.result.text = outcome, text
        return Token(text)

    def _fail(self, code: str) -> None:
        """An error event ends the stream; the client keeps what it was shown (normally nothing)."""
        res = self.result
        res.outcome, res.error_code, res.partial = "error", code, False
        res.text = "".join(self._released)

    def _end_unfinished(self, outcome: str, code: str) -> None:
        res = self.result
        res.outcome, res.error_code, res.partial = outcome, code, True
        res.text = "".join(self._released)
        res.citations = chat_citations.build_snapshots(labels_cited_in(res.text, self.labels), self._sources)

    def _retrieval_only(self, outcome: str) -> list[RagItem]:
        """Plan 7.5: the best passages and the honest sentence, or answer_unavailable when that is switched off."""
        if not settings.CHAT_FALLBACK_RETRIEVAL_ONLY:
            self._fail("answer_unavailable")
            return []
        labels = self.labels[:RETRIEVAL_ONLY_PASSAGES]
        self.result.citations = chat_citations.build_snapshots(labels, self._sources)
        return [self._fixed_answer(outcome, retrieval_only_text(self._in.template_language, labels))]

    # ── the model ──

    async def _answer(
        self, req: LLMRequest, prompt: chat_prompt.BuiltPrompt, ctx: TenantLLMContext, deadline: float,
        interval: float,
    ) -> AsyncIterator[RagItem]:
        res = self.result
        filt = StreamFilter(self.labels, system_prompt=prompt.system, require_citation=settings.CHAT_REQUIRE_CITATION)
        stream = self._chain.answer_stream("answer", req, ctx)
        self._stream = stream
        self._llm_started = time.monotonic()
        finish: FinishReason = "stop"
        closed_early = False
        try:
            while True:
                box: list[object] = []
                async for beat in self._wait(_next_event(stream), box, deadline, interval):
                    yield beat
                event = box[0]
                if event is _END:
                    break
                if isinstance(event, TokenEvent):
                    if not self._raw:
                        res.latency["llm_first_token"] = _ms(self._llm_started)
                    self._raw.append(event.text)
                    step = filt.feed(event.text)
                    if step.text:
                        self._released.append(step.text)
                        yield Token(step.text)
                    if step.stop is not None:
                        closed_early = True  # NOT_IN_SOURCES, the cap or the leak cut: close the provider stream now
                        break
                elif isinstance(event, UsageEvent):
                    self._reported = event
                elif isinstance(event, DoneEvent):
                    finish = event.finish_reason
        except LLMChainExhausted as exc:
            res.attempts = len(exc.attempts)
            await self._release_budget()
            for item in self._retrieval_only("fallback_retrieval_only"):
                yield item
            return
        except LLMStreamInterrupted as exc:
            res.attempts = len(stream.attempts)
            logger.warning("chat_llm_interrupted", error_class=type(exc.error).__name__, model_key=exc.model_key,
                           **self._ids())
            self._end_unfinished("interrupted", "stream_interrupted")
            await self._settle_usage(exc.model_key, "interrupted")
            return
        except LLMError as exc:
            # never falls back (bad request, a tool call, blocked, auth): the fixed error, the budget back
            res.attempts = len(stream.attempts)
            outcome = next((code for kind, code in _REFUSED_CALL_OUTCOMES if isinstance(exc, kind)), None)
            logger.warning("chat_llm_refused", error_class=type(exc).__name__, **self._ids())
            await self._release_budget()
            if outcome is not None and stream.attempts:
                self._refused = stream.attempts[-1]  # its row is result.usage, not a second "failed attempt" row
                self._set_usage(stream.attempts[-1].model_key, 0, 0, True, outcome)
            self._fail("answer_unavailable")
            return
        finally:
            with anyio.CancelScope(shield=True):
                await stream.aclose()
        res.attempts = len(stream.attempts)
        res.latency["llm"] = _ms(self._llm_started)

        final = filt.finish(finish)
        res.invalid_citation_count = final.invalid_citation_count
        res.verdict_form = final.verdict_form
        res.outcome = final.outcome
        model_key = stream.model_key or (stream.attempts[-1].model_key if stream.attempts else None)
        if closed_early and final.outcome == "refused_by_model" and self._reported is None:
            # Owner decision: the stream was closed at the verdict, so the provider's usage never came
            await self._settle(model_key, llm_budget.estimate_prompt_tokens(req), REFUSAL_COMPLETION_TOKENS_ESTIMATE,
                               True, "ok")
        else:
            await self._settle_usage(model_key, "ok")
        if final.retract:
            res.text = refusal_text(self._in.template_language)
            yield Replace(res.text)
            return
        if final.text:
            self._released.append(final.text)
            yield Token(final.text)
        res.text = "".join(self._released)
        res.citations = chat_citations.build_snapshots(final.cited_labels, self._sources)
        res.needs_review = chat_citations.needs_review(final.cited_labels, self._sources)

    # ── usage and the budget ──

    def _set_usage(self, model_key: str, prompt: int, completion: int, estimated: bool, outcome: str) -> None:
        provider, _ = split_model_key(model_key)
        stream = self._stream
        calls = sum(1 for a in (stream.attempts if stream else []) if a.model_key == model_key) or 1
        self.result.usage = UsageRow(
            provider=provider, model_key=model_key, prompt_tokens=prompt, completion_tokens=completion,
            estimated=estimated, latency_ms=_ms(self._llm_started) if self._llm_started else 0, outcome=outcome,
            attempt=min(calls, 2),
        )

    async def _settle(self, model_key: str | None, prompt: int, completion: int, estimated: bool, outcome: str) -> None:
        if model_key is not None:
            self._set_usage(model_key, prompt, completion, estimated, outcome)
        if self._reservation is not None and not self._reservation.settled:
            await self._budget.settle(self._reservation, prompt + completion)

    async def _settle_usage(self, model_key: str | None, outcome: str) -> None:
        """The best known count: the reported usage, else our estimate of the prompt and of the text received."""
        if self._reported is not None:
            await self._settle(model_key, self._reported.prompt_tokens, self._reported.completion_tokens,
                               self._reported.estimated, outcome)
            return
        assert self._req is not None
        await self._settle(model_key, llm_budget.estimate_prompt_tokens(self._req),
                           llm_budget.estimate_text_tokens("".join(self._raw)), True, outcome)

    async def _release_budget(self) -> None:
        if self._reservation is not None and not self._reservation.settled:
            await self._budget.release(self._reservation)

    async def _clean_up(self) -> None:
        """Runs however run() ends. A turn stopped before its end (the client went away): the provider stream is
        closed, and the budget settled to what was used (cancelled) or given back when no model had answered yet."""
        if self._stream is not None:
            try:
                await self._stream.aclose()
            except Exception as exc:
                logger.error("chat_llm_close_error", error_class=type(exc).__name__, **self._ids())
        if self._reservation is None or self._reservation.settled:
            return
        try:
            model_key = self._stream.model_key if self._stream is not None else None
            if model_key is None:
                await self._release_budget()
            else:
                # the stream cap stopped it (interrupted), or the client went away (cancelled)
                outcome = "interrupted" if self.result.outcome == "interrupted" else "cancelled"
                await self._settle_usage(model_key, outcome)
        except Exception as exc:
            logger.error("chat_budget_settle_error", error_class=type(exc).__name__, **self._ids())
