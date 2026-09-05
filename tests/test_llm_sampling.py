"""What the gateway sends the provider, and what it stops sending.

From Gemini 3 on, a temperature below 1.0 is not merely ignored: LiteLLM warns
that it causes infinite loops and degraded reasoning, and that temperature,
top_p and top_k are deprecated for those models. So the gateway pins the knob —
and because every explicit temperature in this codebase is *below* the default
and means "be deterministic", it says that in words instead, which is the only
channel a Gemini 3 model still listens on.

The tests assert on the kwargs that reach ``litellm.completion``, because that
is the only place the rule is observable. Everything above it — nine call sites
across scoring, writing, classification, form mapping, derivation, the variant
judge and the fabrication self-check — routes through this one function, which
is why the rule lives here and not at any of them.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from backend.config import Settings, settings
from backend.llm import client as llm_client

GEMINI_3 = "gemini/gemini-3.1-flash-lite"
GEMINI_2 = "gemini/gemini-2.5-flash-lite"
ANTHROPIC = "anthropic/claude-opus-5"


def _response(text: str = "ok") -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )


@pytest.fixture
def calls(monkeypatch) -> list[dict[str, Any]]:
    """Record the kwargs of every provider round trip, answering ``ok``."""
    recorded: list[dict[str, Any]] = []

    def fake_completion(**kwargs: Any) -> Any:
        recorded.append(kwargs)
        return _response()

    monkeypatch.setattr(llm_client.litellm, "completion", fake_completion)
    return recorded


def _system_of(call: dict[str, Any]) -> str:
    return "\n".join(
        message["content"]
        for message in call["messages"]
        if message.get("role") == "system"
    )


# =========================================================================
# The pin
# =========================================================================


@pytest.mark.parametrize("asked", [0.0, 0.2, 0.3, 0.8])
def test_a_gemini_3_model_is_never_sent_a_temperature_below_one(calls, asked):
    """The warning is explicit: below 1.0 the model loops and reasons worse."""
    llm_client.llm.complete("hello", model=GEMINI_3, purpose="test", temperature=asked)

    assert calls[0]["temperature"] == 1.0


def test_gemini_2_keeps_the_temperature_it_was_asked_for(calls):
    """The deprecation starts at 3. Pinning 2.5 would change a model that works."""
    llm_client.llm.complete("hello", model=GEMINI_2, purpose="test", temperature=0.0)

    assert calls[0]["temperature"] == 0.0


def test_a_non_gemini_provider_keeps_the_temperature_it_was_asked_for(calls):
    """Anthropic still honours temperature; this is not a blanket change."""
    llm_client.llm.complete("hello", model=ANTHROPIC, purpose="test", temperature=0.3)

    assert calls[0]["temperature"] == 0.3


def test_the_pin_follows_the_major_version_not_a_list_of_model_ids():
    """A hand-maintained list of ids is a list that goes stale on release day."""
    assert llm_client._pins_temperature("gemini/gemini-3.1-flash-lite")
    assert llm_client._pins_temperature("gemini/gemini-4-something-unreleased")
    assert not llm_client._pins_temperature("gemini/gemini-2.5-flash-lite")
    assert not llm_client._pins_temperature("gemini/gemini-embedding-001")
    assert not llm_client._pins_temperature("anthropic/claude-opus-5")


def test_no_deprecated_sampling_parameter_is_sent_at_all(calls):
    """top_p and top_k are deprecated alongside temperature from Gemini 3 on."""
    llm_client.llm.complete("hello", model=GEMINI_3, purpose="test", temperature=0.0)

    assert "top_p" not in calls[0]
    assert "top_k" not in calls[0]


# =========================================================================
# What the temperature used to say, said in words
# =========================================================================


def test_a_pinned_call_is_told_in_words_to_be_deterministic(calls):
    """Pinning throws away the caller's intent unless it moves channel."""
    llm_client.llm.complete(
        "hello", model=GEMINI_3, purpose="test", system="You judge.", temperature=0.0
    )

    assert llm_client._DETERMINISM_INSTRUCTION in _system_of(calls[0])


def test_the_instruction_is_added_to_the_callers_system_message_not_instead(calls):
    """Replacing the system prompt would drop every rule the call site set."""
    llm_client.llm.complete(
        "hello",
        model=GEMINI_3,
        purpose="test",
        system="You never invent facts about the candidate.",
        temperature=0.0,
    )

    system = _system_of(calls[0])
    assert "You never invent facts about the candidate." in system
    assert llm_client._DETERMINISM_INSTRUCTION in system


def test_a_call_with_no_system_message_gets_one(calls):
    """Several call sites pass no system prompt; they still asked for 0.2."""
    llm_client.llm.complete("hello", model=GEMINI_3, purpose="test", temperature=0.0)

    assert calls[0]["messages"][0]["role"] == "system"
    assert llm_client._DETERMINISM_INSTRUCTION in _system_of(calls[0])


def test_a_caller_already_at_full_temperature_is_not_told_to_be_deterministic(calls):
    """Nothing was taken away from that caller, so nothing has to be given back."""
    llm_client.llm.complete("hello", model=GEMINI_3, purpose="test", temperature=1.0)

    assert llm_client._DETERMINISM_INSTRUCTION not in _system_of(calls[0])


def test_an_unpinned_model_is_not_told_to_be_deterministic(calls):
    """It got the temperature it asked for. The instruction would be noise."""
    llm_client.llm.complete(
        "hello", model=ANTHROPIC, purpose="test", system="You judge.", temperature=0.0
    )

    assert llm_client._DETERMINISM_INSTRUCTION not in _system_of(calls[0])


def test_the_repair_round_trip_does_not_stack_the_instruction(monkeypatch):
    """complete_json reuses the caller's message list across two round trips.

    Appending in place rather than to a copy would send the instruction twice on
    the second attempt, and three times if the loop ever grew a third.
    """
    recorded: list[dict[str, Any]] = []
    replies = iter(["not json at all", '{"answer": "yes"}'])

    def fake_completion(**kwargs: Any) -> Any:
        recorded.append(kwargs)
        return _response(next(replies))

    monkeypatch.setattr(llm_client.litellm, "completion", fake_completion)

    llm_client.llm.complete_json(
        "hello",
        model=GEMINI_3,
        purpose="test",
        schema={"type": "object", "required": ["answer"]},
        system="You answer questions.",
        temperature=0.0,
    )

    assert len(recorded) == 2, "the malformed first reply should have been repaired"
    second = _system_of(recorded[1])
    assert second.count(llm_client._DETERMINISM_INSTRUCTION) == 1


# =========================================================================
# One key for the whole pipeline
# =========================================================================


def _default(name: str) -> str:
    """The value config.py SHIPS, not the one this machine's .env resolved to.

    ``settings`` reads .env, so asserting on it tests the developer's machine.
    The first version of the test below did exactly that and passed against a
    mutation that put the OpenAI model back in config.py — the local .env
    satisfied the assertion instead of the default under test.
    """
    return str(Settings.model_fields[name].default)


def test_the_shipped_embedding_model_runs_on_the_scoring_provider():
    """Stage 1 does not fail loudly without its key — it silently stops ranking.

    A second provider is a second thing to be unset, and the failure mode is
    invisible: every job scores, none is prefiltered, and nothing says so.
    """
    provider = _default("llm_model_embedding").split("/", 1)[0]
    assert provider == _default("llm_model_scoring").split("/", 1)[0]


def test_every_embedding_model_in_play_has_a_price():
    """estimate_cost silently prices an unknown model at zero, so a missing
    entry reads as "stage 1 is free" rather than as a missing entry."""
    prices = settings.llm_prices_per_m_tokens
    assert _default("llm_model_embedding") in prices
    assert settings.llm_model_embedding in prices, "this machine's .env model"
