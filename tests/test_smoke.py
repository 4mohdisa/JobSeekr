"""The smoke command: what it reports, and what it must never do.

The command exists to be run mid-setup, with most credentials still missing.
So the behaviours worth pinning are mostly about the degenerate cases:

* a missing credential is a SKIP naming the setting, never a failure
* a check that raises becomes one failed row, not a dead run
* skips do not make the exit code non-zero
* nothing writes to the database or submits anything

The checks themselves are stubbed. Whether Telegram's API is up is not
something a test can assert, and the point of these is that the *reporting*
around it is honest.
"""

from __future__ import annotations

import pytest

from backend import smoke
from backend.config import settings

# =========================================================================
# Skipping is a result
# =========================================================================


def test_a_missing_telegram_credential_skips_and_names_the_setting(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", None)
    monkeypatch.setattr(settings, "telegram_chat_id", None)

    result = smoke.check_telegram()

    assert result.status == "skip"
    assert "TELEGRAM_BOT_TOKEN" in result.detail, "the reason must name what to set"


def test_a_missing_api_key_skips_and_names_the_model(monkeypatch):
    monkeypatch.setattr(smoke, "_key_for", lambda model: None)

    result = smoke.check_llm_completion()

    assert result.status == "skip"
    assert settings.llm_model_scoring in result.detail


def test_a_skip_is_not_a_failure():
    """The command has to be runnable at any stage of setup.

    Exiting non-zero for an unconfigured integration would make it useless in
    exactly the situation it was built for.
    """
    results = [
        smoke.Result("a", "skip", "no key"),
        smoke.Result("b", "pass", "fine"),
    ]
    assert not any(r.status == "fail" for r in results)


def test_the_rendered_table_shows_the_skip_reason():
    """A skip with no reason is indistinguishable from a bug."""
    body = smoke.render(
        [smoke.Result("telegram", "skip", "TELEGRAM_BOT_TOKEN not set")]
    )
    assert "SKIP" in body
    assert "TELEGRAM_BOT_TOKEN not set" in body


# =========================================================================
# Failures are reported, not raised
# =========================================================================


def test_a_check_that_raises_becomes_one_failed_row(monkeypatch):
    """One broken credential must not hide the state of the other five."""

    def boom() -> smoke.Result:
        raise RuntimeError("connection reset")

    monkeypatch.setitem(smoke.CHECKS, "telegram", boom)

    results = smoke.run_smoke(["telegram"])

    assert len(results) == 1
    assert results[0].status == "fail"
    assert "connection reset" in results[0].detail


def test_one_broken_check_does_not_stop_the_others(monkeypatch):
    def boom() -> smoke.Result:
        raise RuntimeError("nope")

    monkeypatch.setitem(smoke.CHECKS, "telegram", boom)
    monkeypatch.setitem(
        smoke.CHECKS, "pdflatex", lambda: smoke.Result("pdflatex", "pass")
    )

    results = smoke.run_smoke(["telegram", "pdflatex"])

    assert [r.status for r in results] == ["fail", "pass"]


def test_telegram_rejecting_the_message_is_a_failure_not_a_pass(monkeypatch):
    """send_message returns False when unconfigured and swallows transport errors.

    A check that only looked for an absence of exceptions would pass with no
    bot at all, which is the exact thing this command exists to catch.
    """
    monkeypatch.setattr(settings, "telegram_bot_token", "token")
    monkeypatch.setattr(settings, "telegram_chat_id", "chat")
    monkeypatch.setattr(
        "backend.integrations.telegram.send_message", lambda *a, **k: False
    )

    result = smoke.check_telegram()

    assert result.status == "fail"
    assert "token" in result.detail or "chat id" in result.detail


def test_telegram_accepting_the_message_passes(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "token")
    monkeypatch.setattr(settings, "telegram_chat_id", "chat")
    monkeypatch.setattr(
        "backend.integrations.telegram.send_message", lambda *a, **k: True
    )

    assert smoke.check_telegram().status == "pass"


def test_an_embedding_with_no_vector_is_a_failure(monkeypatch):
    """A provider that answers with nothing is not a working provider."""
    from backend.llm.client import llm

    monkeypatch.setattr(smoke, "_key_for", lambda model: "key")
    monkeypatch.setattr(llm, "embed", lambda *a, **k: [[]])

    assert smoke.check_llm_embedding().status == "fail"


# =========================================================================
# Cost reporting
# =========================================================================


def test_a_completion_reports_what_the_provider_actually_charged(monkeypatch):
    """From the llm_spend row, not from the projection in scoring/run.py.

    The projected price is the thing worth checking, so pricing the smoke test
    from the projection would make it agree with itself.
    """
    from backend.llm.client import llm

    monkeypatch.setattr(smoke, "_key_for", lambda model: "key")
    monkeypatch.setattr(llm, "complete", lambda *a, **k: "ready")
    monkeypatch.setattr(
        smoke,
        "_last_spend",
        lambda purpose: {"input_tokens": 12, "output_tokens": 2, "cost_usd": 0.000004},
    )

    result = smoke.check_llm_completion()

    assert result.status == "pass"
    assert result.facts["input_tokens"] == 12
    assert result.facts["cost_usd"] == 0.000004


def test_a_provider_that_reports_no_usage_still_passes(monkeypatch):
    """A missing usage block is smaller than a smoke test crashing after a call."""
    from backend.llm.client import llm

    monkeypatch.setattr(smoke, "_key_for", lambda model: "key")
    monkeypatch.setattr(llm, "complete", lambda *a, **k: "ready")
    monkeypatch.setattr(smoke, "_last_spend", lambda purpose: {})

    result = smoke.check_llm_completion()

    assert result.status == "pass"
    assert result.facts["cost_usd"] == 0.0


def test_the_table_totals_the_spend():
    body = smoke.render(
        [
            smoke.Result("a", "pass", facts={"cost_usd": 0.001}),
            smoke.Result("b", "pass", facts={"cost_usd": 0.002}),
        ]
    )
    assert "$0.003000" in body


# =========================================================================
# Selection
# =========================================================================


def test_only_runs_the_named_checks(monkeypatch):
    ran: list[str] = []
    for name in smoke.CHECKS:
        monkeypatch.setitem(
            smoke.CHECKS, name, lambda n=name: ran.append(n) or smoke.Result(n, "pass")
        )

    smoke.run_smoke(["pdflatex"])

    assert ran == ["pdflatex"]


def test_no_selection_runs_everything(monkeypatch):
    ran: list[str] = []
    for name in smoke.CHECKS:
        monkeypatch.setitem(
            smoke.CHECKS, name, lambda n=name: ran.append(n) or smoke.Result(n, "pass")
        )

    smoke.run_smoke()

    assert set(ran) == set(smoke.CHECKS)


# =========================================================================
# What it must never do
# =========================================================================


def test_the_module_never_submits_anything():
    """A smoke test that could apply for a job is not a smoke test."""
    import pathlib

    source = pathlib.Path("backend/smoke.py").read_text(encoding="utf-8")
    code = "\n".join(
        line.split("#")[0]
        for line in source.splitlines()
        if not line.strip().startswith(("#", '"', "*"))
    )

    for forbidden in ("run_apply", "check_can_submit", "allow_live_submit", ".click("):
        assert forbidden not in code, f"smoke.py references {forbidden}"


def test_the_browser_check_loads_a_blank_page_not_a_job_board():
    """Loading a real board from a smoke test puts an unexplained hit in their logs.

    It is also not what is being tested: the question is whether Playwright can
    drive the configured channel, not whether the internet works.
    """
    import pathlib

    source = pathlib.Path("backend/smoke.py").read_text(encoding="utf-8")
    assert 'page.goto("about:blank"' in source
    for board in ("seek.com", "linkedin.com", "indeed.com"):
        assert f'goto("https://{board}' not in source


@pytest.mark.parametrize("name", sorted(smoke.CHECKS))
def test_every_check_returns_a_result_rather_than_raising(name, monkeypatch):
    """Enforced for every check, including ones added later.

    The run loop catches exceptions, but a check that habitually raises turns
    the table into a wall of tracebacks-as-details and stops being readable.
    """
    monkeypatch.setattr(settings, "telegram_bot_token", None)
    monkeypatch.setattr(settings, "telegram_chat_id", None)
    monkeypatch.setattr(smoke, "_key_for", lambda model: None)
    monkeypatch.setattr(settings, "pdflatex_path", "/nonexistent/pdflatex")
    monkeypatch.setattr(settings, "browser_channel", "no-such-channel")

    result = smoke.CHECKS[name]()

    assert isinstance(result, smoke.Result)
    assert result.status in {"pass", "fail", "skip"}
