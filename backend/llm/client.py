"""The one door every LLM call in JobSeekr goes through.

Nothing else may import ``litellm``. One door is what makes the $25/month cap
real: budget is checked before the call, spend is written after it whether the
call worked or not, and transient provider failures are retried in exactly one
place. Model ids are never written in this file — they come from
``settings.llm_model_*``, which is the only place they are allowed to exist.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

# Importing litellm otherwise downloads its pricing table from GitHub, which
# every CLI entry point (discovery, scoring, apply) would pay for at startup and
# which stalls on an offline or throttled machine. The copy bundled with the
# pinned litellm is what we price against. setdefault, so a user who wants live
# pricing can still export LITELLM_LOCAL_MODEL_COST_MAP=false.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import litellm
from litellm.exceptions import (
    APIConnectionError,
    BadGatewayError,
    InternalServerError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from backend.base import LLMClient
from backend.config import settings
from backend.logging_setup import get_logger

__all__ = [
    "LLMBudgetExceeded",
    "LLMGateway",
    "budget_status",
    "complete",
    "complete_json",
    "embed",
    "llm",
    "spend_this_month",
]

log = get_logger(__name__)

# LiteLLM writes its own banners straight to stderr on unrecognised models.
# structlog owns this application's output; anything worth seeing we log.
litellm.suppress_debug_info = True

# Worth another go: the provider was busy, slow or briefly broken. Everything
# else (auth, bad request, context length, content policy) fails the same way
# on attempt two, so retrying it just spends the user's money twice.
_TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    APIConnectionError,
    BadGatewayError,
    InternalServerError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)

# LiteLLM resolves credentials from os.environ, but ours live in .env behind
# pydantic-settings, so the key is passed explicitly per call.
_PROVIDER_KEY_FIELDS = {
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "gemini": "gemini_api_key",
}

_SCHEMA_HINT = (
    "Reply with a single JSON object and nothing else — no prose, no code "
    "fence. It must satisfy this JSON schema:\n{schema}"
)
_REPAIR_HINT = "That reply was not usable JSON ({error}). Send the JSON object only."

# Warn about the budget once per process, not once per call — a scoring run
# makes dozens of calls and forty identical warnings hide the real log.
_budget_warned = False
# Same idea for off-settings model ids: complain once per distinct model.
_unconfigured_models_warned: set[str] = set()


class LLMBudgetExceeded(RuntimeError):
    """This UTC month's spend has reached the configured cap.

    Raised instead of calling the provider so LLM work halts hard rather than
    quietly overspending. Discovery makes no LLM calls and keeps running, so
    the pipeline still collects jobs while scoring and writing are stopped.
    """

    def __init__(self, spent_usd: float, cap_usd: float) -> None:
        super().__init__(
            f"LLM budget exhausted: ${spent_usd:.4f} spent of ${cap_usd:.2f} cap this month"
        )
        self.spent_usd = spent_usd
        self.cap_usd = cap_usd


# --------------------------------------------------------------------- budget


def _month_start() -> datetime:
    """First instant of the current UTC calendar month.

    Calendar months, not rolling 30 days: the cap is a monthly budget the user
    reasons about the same way their card statement does.
    """
    now = datetime.now(UTC)
    return datetime(now.year, now.month, 1, tzinfo=UTC)


def spend_this_month() -> float:
    """Total USD recorded against llm_spend for the current UTC month."""
    from sqlmodel import func, select

    from backend.db import session_scope
    from backend.models import LLMSpend

    with session_scope() as session:
        total = session.exec(
            select(func.coalesce(func.sum(LLMSpend.cost_usd), 0.0)).where(
                LLMSpend.called_at >= _month_start()
            )
        ).one()
    return float(total or 0.0)


def budget_status() -> dict[str, Any]:
    """Current month's budget picture, for the dashboard and the API."""
    spent = spend_this_month()
    cap = settings.llm_monthly_cap_usd
    return {
        "month": f"{datetime.now(UTC):%Y-%m}",
        "spent_usd": round(spent, 6),
        "cap_usd": cap,
        "remaining_usd": round(max(cap - spent, 0.0), 6),
        "fraction": round(spent / cap, 6) if cap > 0 else 1.0,
        "warn_fraction": settings.llm_warn_fraction,
        "warned": _budget_warned,
        "exceeded": spent >= cap,
    }


def _check_budget(purpose: str) -> None:
    """Gate every provider call on the monthly cap.

    ``spent >= cap`` is the whole rule, which also gives a cap of 0 the useful
    reading: "I am not spending anything this month" stops LLM work outright.
    Failures reading the spend table propagate rather than defaulting to zero —
    a budget that fails open is not a budget.
    """
    global _budget_warned

    spent = spend_this_month()
    cap = settings.llm_monthly_cap_usd

    if spent >= cap:
        log.error("llm_budget_exceeded", spent_usd=spent, cap_usd=cap, purpose=purpose)
        raise LLMBudgetExceeded(spent, cap)

    if not _budget_warned and cap > 0 and spent / cap >= settings.llm_warn_fraction:
        _budget_warned = True
        log.warning(
            "llm_budget_warning",
            spent_usd=spent,
            cap_usd=cap,
            fraction=round(spent / cap, 4),
            purpose=purpose,
        )


def _record_spend(
    *,
    model: str,
    purpose: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    job_id: int | None,
    ok: bool,
    error: str | None,
) -> None:
    """Write one llm_spend row per provider round-trip.

    Never raises: the call has already been paid for, so losing the bookkeeping
    must not also lose the answer. It does under-count the cap, so it is logged
    at error level rather than swallowed.
    """
    try:
        from backend.db import session_scope
        from backend.models import LLMSpend

        with session_scope() as session:
            session.add(
                LLMSpend(
                    called_at=datetime.now(UTC),
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd,
                    purpose=purpose,
                    job_id=job_id,
                    ok=ok,
                    error=error,
                )
            )
    except Exception:
        log.exception(
            "llm_spend_not_recorded",
            model=model,
            purpose=purpose,
            cost_usd=cost_usd,
            job_id=job_id,
        )


# ---------------------------------------------------------------- call helpers


def _configured_models() -> set[str]:
    return {
        settings.llm_model_scoring,
        settings.llm_model_writing,
        settings.llm_model_classify,
        settings.llm_model_formmap,
        settings.llm_model_embedding,
    }


def _warn_if_unconfigured_model(model: str) -> None:
    """Flag a model id that did not come from settings.

    A literal model string at a call site is a rule breach that is otherwise
    invisible. Warn rather than raise, so a deliberate one-off still runs.
    """
    if model in _configured_models() or model in _unconfigured_models_warned:
        return
    _unconfigured_models_warned.add(model)
    log.warning("llm_model_not_from_settings", model=model)


def _api_key_for(model: str) -> str | None:
    """The settings key for a ``provider/model`` id, or None to let LiteLLM look."""
    provider = model.split("/", 1)[0].lower()
    field = _PROVIDER_KEY_FIELDS.get(provider)
    return getattr(settings, field) if field else None


def _messages(prompt: str, system: str | None) -> list[dict[str, str]]:
    messages = [{"role": "user", "content": prompt}]
    if system:
        messages.insert(0, {"role": "system", "content": system})
    return messages


# From Gemini 3 on, LiteLLM warns that a temperature below 1.0 causes infinite
# loops and degraded reasoning, and that temperature, top_p and top_k are
# deprecated outright. So the knob is pinned for those models here, in the one
# door, rather than at nine call sites that would each have to know the rule.
#
# Pinning throws away what a low temperature MEANT, though — every explicit
# temperature in this codebase is below the default and says "be deterministic".
# That intent moves to the only channel the model still listens on: words.
_GEMINI_MAJOR_VERSION = re.compile(r"^gemini/gemini-(\d+)")
_PINNED_TEMPERATURE = 1.0
_DETERMINISM_INSTRUCTION = (
    "Answer deterministically: give the single most likely answer, keep the "
    "wording plain and consistent between runs, and never vary phrasing for "
    "the sake of variety."
)


def _pins_temperature(model: str) -> bool:
    """True for Gemini 3 and later, where the sampling parameters are deprecated.

    Matched on the major version rather than a list of model ids, so
    ``gemini-3.1-flash-lite`` and next year's ``gemini-4-*`` are both covered
    and ``gemini-2.5-flash-lite`` is correctly left alone. Non-Gemini providers
    never match, which is the point: Anthropic still honours temperature.
    """
    match = _GEMINI_MAJOR_VERSION.match(model.strip().lower())
    return match is not None and int(match.group(1)) >= 3


def _with_determinism_instruction(
    messages: Sequence[dict[str, str]],
) -> list[dict[str, str]]:
    """Copy of ``messages`` whose system message carries the determinism ask.

    Copied rather than mutated because ``complete_json``'s repair loop reuses
    the caller's list across two round trips, and appending in place would send
    the instruction twice.
    """
    rewritten = [dict(message) for message in messages]
    for message in rewritten:
        if message.get("role") == "system":
            message["content"] = f"{message['content']}\n\n{_DETERMINISM_INSTRUCTION}"
            return rewritten
    return [{"role": "system", "content": _DETERMINISM_INSTRUCTION}, *rewritten]


def _usage_tokens(response: Any) -> tuple[int, int]:
    """(input, output) tokens, tolerant of providers that report neither."""
    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    return input_tokens, output_tokens


def _cost_usd(response: Any, call_type: str) -> float:
    """Cost of a response, or 0.0 when LiteLLM has no price for the model."""
    try:
        return float(
            litellm.completion_cost(completion_response=response, call_type=call_type)
        )
    except Exception as exc:  # noqa: BLE001 - pricing must never break a paid-for call
        log.warning("llm_cost_unavailable", call_type=call_type, error=str(exc))
        return 0.0


def _first_text(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise ValueError("LLM response contained no choices")
    return getattr(choices[0].message, "content", None) or ""


def _embedding_vectors(response: Any) -> list[list[float]]:
    vectors: list[list[float]] = []
    for item in getattr(response, "data", None) or []:
        raw = item["embedding"] if isinstance(item, dict) else item.embedding
        vectors.append([float(value) for value in raw])
    return vectors


def _schema_name(schema: dict[str, Any]) -> str:
    """A response_format name providers accept: ``[A-Za-z0-9_-]`` only."""
    title = str(schema.get("title") or "response")
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", title).strip("_")
    return cleaned or "response"


def _json_response_format(model: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Native schema enforcement where the model has it, JSON mode otherwise.

    ``supports_response_schema`` also returns False for models LiteLLM has never
    heard of, which is the right answer — sending a json_schema block to one
    would be rejected outright.
    """
    try:
        supported = bool(litellm.supports_response_schema(model=model))
    except Exception:  # noqa: BLE001 - unknown model means "assume not supported"
        supported = False
    if supported:
        return {
            "type": "json_schema",
            "json_schema": {"name": _schema_name(schema), "schema": schema},
        }
    return {"type": "json_object"}


def _strip_code_fence(text: str) -> str:
    """Unwrap ```json fences, which models add even when told not to."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
    return body.rstrip().removesuffix("```").strip()


def _parse_json_object(text: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Parse and check required keys. Raises ValueError on anything unusable.

    Deliberately not a full JSON-Schema validation: the required-key check is
    what stops a downstream ``result["score"]`` from raising KeyError three
    layers away from the model that omitted it.
    """
    payload = json.loads(_strip_code_fence(text))
    if not isinstance(payload, dict):
        # ValueError, not TypeError: a model returning an array is a bad value
        # from a remote service, and complete_json's repair loop catches
        # ValueError (which JSONDecodeError already subclasses).
        raise ValueError(  # noqa: TRY004
            f"expected a JSON object, got {type(payload).__name__}"
        )
    missing = [key for key in schema.get("required", []) if key not in payload]
    if missing:
        raise ValueError(f"missing required keys: {sorted(missing)}")
    return payload


# ------------------------------------------------------------------- gateway


class LLMGateway:
    """LiteLLM behind budget enforcement, spend accounting and retries.

    Implements :class:`backend.base.LLMClient`. Use the module singleton
    ``llm`` rather than constructing another one; it holds no state, but a
    second instance invites a second set of call sites.
    """

    def complete(
        self,
        prompt: str,
        *,
        model: str,
        purpose: str,
        system: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        job_id: int | None = None,
    ) -> str:
        response = self._completion(
            messages=_messages(prompt, system),
            model=model,
            purpose=purpose,
            job_id=job_id,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return _first_text(response)

    def complete_json(
        self,
        prompt: str,
        *,
        model: str,
        purpose: str,
        schema: dict[str, Any],
        system: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        job_id: int | None = None,
    ) -> dict[str, Any]:
        response_format = _json_response_format(model, schema)
        messages = _messages(prompt, system)
        if response_format["type"] == "json_object":
            # Plain JSON mode constrains the syntax but not the shape, and
            # OpenAI additionally requires the word "json" in the prompt.
            messages.append(
                {
                    "role": "user",
                    "content": _SCHEMA_HINT.format(schema=json.dumps(schema)),
                }
            )

        last_error = ""
        # One corrective round-trip: models occasionally answer with prose
        # around the object even under response_format, and re-asking with the
        # parse error costs less than failing the whole scoring run.
        for attempt in (1, 2):
            response = self._completion(
                messages=messages,
                model=model,
                purpose=purpose,
                job_id=job_id,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
            )
            text = _first_text(response)
            try:
                return _parse_json_object(text, schema)
            except ValueError as exc:
                last_error = str(exc)
                log.warning(
                    "llm_json_malformed",
                    model=model,
                    purpose=purpose,
                    job_id=job_id,
                    attempt=attempt,
                    error=last_error,
                )
                messages = [
                    *messages,
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": _REPAIR_HINT.format(error=last_error)},
                ]

        raise ValueError(
            f"model {model} returned unusable JSON for purpose={purpose}: {last_error}"
        )

    def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
        purpose: str = "embedding",
    ) -> list[list[float]]:
        if not texts:
            return []

        chosen = model or settings.llm_model_embedding
        _check_budget(purpose)
        _warn_if_unconfigured_model(chosen)

        kwargs: dict[str, Any] = {
            "model": chosen,
            "input": list(texts),
            "timeout": settings.llm_timeout_seconds,
        }
        api_key = _api_key_for(chosen)
        if api_key:
            kwargs["api_key"] = api_key

        response = self._with_retries(
            litellm.embedding,
            kwargs,
            model=chosen,
            purpose=purpose,
            job_id=None,
            call_type="embedding",
        )
        return _embedding_vectors(response)

    # ------------------------------------------------------------- internals

    def _completion(
        self,
        *,
        messages: Sequence[dict[str, str]],
        model: str,
        purpose: str,
        job_id: int | None,
        temperature: float,
        max_tokens: int | None,
        response_format: dict[str, Any] | None = None,
    ) -> Any:
        _check_budget(purpose)
        _warn_if_unconfigured_model(model)

        sent = list(messages)
        if _pins_temperature(model):
            if temperature < _PINNED_TEMPERATURE:
                sent = _with_determinism_instruction(sent)
            temperature = _PINNED_TEMPERATURE

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": sent,
            "temperature": temperature,
            "timeout": settings.llm_timeout_seconds,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if response_format is not None:
            kwargs["response_format"] = response_format
        api_key = _api_key_for(model)
        if api_key:
            kwargs["api_key"] = api_key

        return self._with_retries(
            litellm.completion,
            kwargs,
            model=model,
            purpose=purpose,
            job_id=job_id,
            call_type="completion",
        )

    def _with_retries(
        self,
        provider_call: Callable[..., Any],
        kwargs: dict[str, Any],
        *,
        model: str,
        purpose: str,
        job_id: int | None,
        call_type: str,
    ) -> Any:
        """Retry transient provider failures, backing off exponentially.

        Built per call rather than as a decorator so a changed
        ``llm_max_retries`` takes effect without a reimport. LLMBudgetExceeded
        is raised before this runs and is not in ``_TRANSIENT_ERRORS``, so a
        exhausted budget can never be retried into more spending.
        """
        retryer = Retrying(
            stop=stop_after_attempt(1 + max(settings.llm_max_retries, 0)),
            wait=wait_exponential_jitter(initial=1.0, max=30.0),
            retry=retry_if_exception_type(_TRANSIENT_ERRORS),
            reraise=True,
        )
        return retryer(
            self._attempt,
            provider_call,
            kwargs,
            model=model,
            purpose=purpose,
            job_id=job_id,
            call_type=call_type,
        )

    def _attempt(
        self,
        provider_call: Callable[..., Any],
        kwargs: dict[str, Any],
        *,
        model: str,
        purpose: str,
        job_id: int | None,
        call_type: str,
    ) -> Any:
        """One provider round-trip, recorded whether it succeeds or fails.

        Recording per attempt rather than per logical call keeps the ledger
        honest: a retried call may have been billed more than once, and a run
        of failures is exactly what the user needs to see in the spend table.
        """
        started = time.monotonic()
        try:
            response = provider_call(**kwargs)
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            _record_spend(
                model=model,
                purpose=purpose,
                input_tokens=0,
                output_tokens=0,
                cost_usd=0.0,
                job_id=job_id,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
            )
            log.error(
                "llm_call_failed",
                model=model,
                purpose=purpose,
                job_id=job_id,
                duration_ms=duration_ms,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise

        duration_ms = int((time.monotonic() - started) * 1000)
        input_tokens, output_tokens = _usage_tokens(response)
        cost = _cost_usd(response, call_type)
        _record_spend(
            model=model,
            purpose=purpose,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            job_id=job_id,
            ok=True,
            error=None,
        )
        log.info(
            "llm_call",
            model=model,
            purpose=purpose,
            job_id=job_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=round(cost, 6),
            duration_ms=duration_ms,
        )
        return response


# The annotation is the conformance check: if LLMGateway ever drifts from the
# protocol, type checking fails here rather than at some call site.
llm: LLMClient = LLMGateway()


def complete(
    prompt: str,
    *,
    model: str,
    purpose: str,
    system: str | None = None,
    temperature: float = 0.2,
    max_tokens: int | None = None,
    job_id: int | None = None,
) -> str:
    """Module-level shortcut for ``llm.complete``."""
    return llm.complete(
        prompt,
        model=model,
        purpose=purpose,
        system=system,
        temperature=temperature,
        max_tokens=max_tokens,
        job_id=job_id,
    )


def complete_json(
    prompt: str,
    *,
    model: str,
    purpose: str,
    schema: dict[str, Any],
    system: str | None = None,
    temperature: float = 0.2,
    max_tokens: int | None = None,
    job_id: int | None = None,
) -> dict[str, Any]:
    """Module-level shortcut for ``llm.complete_json``."""
    return llm.complete_json(
        prompt,
        model=model,
        purpose=purpose,
        schema=schema,
        system=system,
        temperature=temperature,
        max_tokens=max_tokens,
        job_id=job_id,
    )


def embed(
    texts: list[str],
    *,
    model: str | None = None,
    purpose: str = "embedding",
) -> list[list[float]]:
    """Module-level shortcut for ``llm.embed``."""
    return llm.embed(texts, model=model, purpose=purpose)
