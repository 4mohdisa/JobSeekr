"""Guardrails — every check with a passing and a failing case.

The single most important test in this file is
``test_a_perfect_application_is_still_refused_by_default``: with everything
else satisfied, the default configuration must still refuse to submit. That is
the property that makes it safe to build and run the whole engine before the
user has decided to turn it on.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import Session, SQLModel, create_engine

from backend.apply import guardrails
from backend.apply.pacing import next_interval_seconds
from backend.config import settings
from backend.models import (
    Application,
    ApplicationOutcome,
    Campaign,
    GrayZoneAction,
    Job,
)

ADELAIDE = ZoneInfo("Australia/Adelaide")

# A Wednesday, 11:00 Adelaide time — comfortably inside every window.
GOOD_TIME = datetime(2026, 8, 26, 11, 0, tzinfo=ADELAIDE).astimezone(UTC)


@dataclass
class FakeDocument:
    kind: str = "resume"
    parse_check_passed: bool = True


@dataclass
class FakeAbstain:
    question: str = "Do you have a forklift licence?"


@dataclass
class FakeDraft:
    platform: str = "seek"
    campaign: Any = None
    score: float | None = 95.0
    documents: list[Any] = field(default_factory=lambda: [FakeDocument()])
    cover_letter_text: str = "Dear Hiring Team, I would like to apply. Kind regards."
    abstentions: list[Any] = field(default_factory=list)


@pytest.fixture
def session(tmp_path, monkeypatch):
    """A DB plus an isolated data_dir, so STOP files never leak between tests."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def campaign():
    return Campaign(
        id=1,
        name="test",
        active=True,
        search_terms=["dev"],
        locations=["Adelaide SA"],
        score_floor=60.0,
        score_auto_apply=80.0,
        gray_zone_action=GrayZoneAction.QUEUE,
        daily_caps={"seek": 10, "linkedin": 5},
    )


@pytest.fixture
def job():
    return Job(
        id=1,
        source="seek",
        source_job_id="1",
        url="https://example.com/1",
        title="Developer",
        company="Acme",
        location="Adelaide SA",
        dedupe_hash="h1",
        campaign_id=1,
    )


def add_submitted(session, *, job_id: int, when: datetime, platform: str = "seek") -> None:
    """Insert a submitted application, creating its job first.

    backend.db enables PRAGMA foreign_keys for every connection, so an
    application row needs a real job to point at — the same constraint that
    holds in production.
    """
    if session.get(Job, job_id) is None:
        session.add(
            Job(
                id=job_id,
                source=platform,
                source_job_id=f"s{job_id}",
                url=f"https://example.com/{job_id}",
                title="Developer",
                company="Acme",
                dedupe_hash=f"h{job_id}",
            )
        )
        session.flush()
    session.add(
        Application(
            job_id=job_id,
            outcome=ApplicationOutcome.SUBMITTED,
            applied_at=when,
            platform=platform,
        )
    )
    session.commit()


def run(session, job, draft, **kwargs):
    kwargs.setdefault("now", GOOD_TIME)
    kwargs.setdefault("is_authenticated", lambda platform: True)
    return guardrails.check_can_submit(session, job, draft, **kwargs)


def named(result, name):
    return next(c for c in result.checks if c.name == name)


# =========================================================================
# THE test
# =========================================================================


def test_a_perfect_application_is_still_refused_by_default(session, job, campaign):
    """Everything else satisfied, and the answer is still no.

    This is what makes the whole apply engine safe to build and run before the
    user has decided to enable it.
    """
    assert settings.allow_live_submit is False, "the default must never be true"

    result = run(session, job, FakeDraft(campaign=campaign))

    assert result.allowed is False
    assert result.blocked_by == "allow_live_submit"
    # And it is the ONLY thing standing in the way.
    assert [c.name for c in result.failures] == ["allow_live_submit"], result.summary()


def test_flipping_only_that_switch_allows_the_same_application(
    session, job, campaign, monkeypatch
):
    """Proves the switch is the single gate and everything else genuinely passed."""
    monkeypatch.setattr(settings, "allow_live_submit", True)
    result = run(session, job, FakeDraft(campaign=campaign))
    assert result.allowed is True, result.summary()


@pytest.fixture
def live(monkeypatch):
    """Enable live submit for the checks below, which test the OTHER gates."""
    monkeypatch.setattr(settings, "allow_live_submit", True)


# =========================================================================
# Each check, blocked
# =========================================================================


def test_stop_file_blocks_everything(session, job, campaign, live):
    settings.stop_file.parent.mkdir(parents=True, exist_ok=True)
    settings.stop_file.write_text("halted", encoding="utf-8")

    result = run(session, job, FakeDraft(campaign=campaign))
    assert result.allowed is False
    assert named(result, "stop_file_absent").passed is False


def test_paused_campaign_blocks(session, job, campaign, live):
    campaign.active = False
    result = run(session, job, FakeDraft(campaign=campaign))
    assert named(result, "campaign_active").passed is False


def test_existing_application_blocks_one_per_job_ever(session, job, campaign, live):
    # The job carries campaign_id=1, and foreign keys are enforced, so the
    # campaign has to exist before the job does.
    session.add(campaign)
    session.add(job)
    session.commit()
    add_submitted(session, job_id=1, when=GOOD_TIME)

    result = run(session, job, FakeDraft(campaign=campaign))
    assert named(result, "not_already_applied").passed is False


def test_daily_cap_counts_the_local_day_not_utc(session, job, campaign, live):
    """A cap is a human-day cap; UTC boundaries would roll it over mid-evening."""
    campaign.daily_caps = {"seek": 2}
    # 23:30 Adelaide is already the NEXT UTC day.
    late_local = datetime(2026, 8, 26, 23, 30, tzinfo=ADELAIDE)
    for offset in range(2):
        add_submitted(session, job_id=90 + offset, when=late_local.astimezone(UTC))

    result = run(
        session,
        job,
        FakeDraft(campaign=campaign),
        now=datetime(2026, 8, 26, 23, 45, tzinfo=ADELAIDE).astimezone(UTC),
    )
    cap_check = named(result, "platform_daily_cap")
    assert cap_check.passed is False, cap_check.detail


def test_under_the_cap_passes(session, job, campaign, live):
    campaign.daily_caps = {"seek": 10}
    result = run(session, job, FakeDraft(campaign=campaign))
    assert named(result, "platform_daily_cap").passed is True


def test_score_below_auto_apply_threshold_blocks(session, job, campaign, live):
    result = run(session, job, FakeDraft(campaign=campaign, score=70.0))
    assert named(result, "score_threshold").passed is False


def test_missing_score_blocks(session, job, campaign, live):
    result = run(session, job, FakeDraft(campaign=campaign, score=None))
    assert named(result, "score_threshold").passed is False


def test_excluded_company_blocks(session, job, campaign, live):
    campaign.exclusions = {"companies": ["Acme Pty Ltd"]}
    result = run(session, job, FakeDraft(campaign=campaign))
    assert named(result, "company_not_excluded").passed is False


def test_a_document_that_failed_the_parse_gate_blocks(session, job, campaign, live):
    draft = FakeDraft(campaign=campaign, documents=[FakeDocument(parse_check_passed=False)])
    result = run(session, job, draft)
    assert named(result, "documents_parse_checked").passed is False


def test_no_documents_at_all_blocks(session, job, campaign, live):
    result = run(session, job, FakeDraft(campaign=campaign, documents=[]))
    assert named(result, "documents_parse_checked").passed is False


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "Dear {{job.company}}, I would like to apply.",
        r"Dear \VAR{job.company}, hello.",
        "Dear [COMPANY], TODO finish this",
        "lorem ipsum dolor sit amet",
    ],
)
def test_an_unrendered_or_empty_cover_letter_blocks(session, job, campaign, live, text):
    result = run(session, job, FakeDraft(campaign=campaign, cover_letter_text=text))
    assert named(result, "cover_letter_clean").passed is False, text


def test_one_abstention_blocks(session, job, campaign, live):
    result = run(session, job, FakeDraft(campaign=campaign, abstentions=[FakeAbstain()]))
    check = named(result, "no_abstentions")
    assert check.passed is False
    assert "forklift" in check.detail


def test_an_unauthenticated_session_blocks(session, job, campaign, live):
    result = run(session, job, FakeDraft(campaign=campaign), is_authenticated=lambda p: False)
    assert named(result, "session_authenticated").passed is False


def test_a_missing_auth_predicate_is_not_evaluated_and_never_passes(
    session, job, campaign, live
):
    """An unevaluated check must never be reported as passed."""
    result = guardrails.check_can_submit(
        session, job, FakeDraft(campaign=campaign), now=GOOD_TIME
    )
    check = named(result, "session_authenticated")
    assert check.evaluated is False
    assert check.passed is False
    assert result.allowed is False


# ------------------------------------------------------------------ windows


def test_linkedin_is_blocked_on_a_weekend(session, campaign, live):
    linkedin_job = Job(
        id=2,
        source="linkedin",
        source_job_id="2",
        url="u",
        title="Dev",
        company="Acme",
        dedupe_hash="h2",
        campaign_id=1,
    )
    saturday = datetime(2026, 8, 29, 11, 0, tzinfo=ADELAIDE).astimezone(UTC)
    result = run(
        session,
        linkedin_job,
        FakeDraft(platform="linkedin", campaign=campaign),
        now=saturday,
    )
    check = named(result, "inside_window")
    assert check.passed is False
    assert "weekend" in check.detail


def test_after_five_pm_adelaide_is_outside_the_window(session, job, campaign, live):
    evening = datetime(2026, 8, 26, 17, 30, tzinfo=ADELAIDE).astimezone(UTC)
    result = run(session, job, FakeDraft(campaign=campaign), now=evening)
    assert named(result, "inside_window").passed is False


def test_the_window_is_evaluated_across_a_dst_transition(session, job, campaign, live):
    """Adelaide observes DST; a fixed UTC offset would drift by an hour."""
    # First Sunday in October 2026 is the DST start; check the Monday after.
    after_dst = datetime(2026, 10, 5, 11, 0, tzinfo=ADELAIDE).astimezone(UTC)
    before_dst = datetime(2026, 9, 28, 11, 0, tzinfo=ADELAIDE).astimezone(UTC)

    for moment in (before_dst, after_dst):
        result = run(session, job, FakeDraft(campaign=campaign), now=moment)
        assert named(result, "inside_window").passed is True, moment.isoformat()

    # The two instants are a different number of hours from UTC, proving the
    # zone (not a fixed offset) is doing the work.
    assert before_dst.astimezone(ADELAIDE).utcoffset() != after_dst.astimezone(
        ADELAIDE
    ).utcoffset()


# ------------------------------------------------------------------ interval


def test_min_interval_blocks_a_too_soon_submit(session, job, campaign, live):
    add_submitted(session, job_id=99, when=GOOD_TIME - timedelta(seconds=30))
    result = run(session, job, FakeDraft(campaign=campaign))
    assert named(result, "min_interval_elapsed").passed is False


def test_min_interval_passes_once_enough_time_has_passed(session, job, campaign, live):
    add_submitted(session, job_id=99, when=GOOD_TIME - timedelta(seconds=600))
    result = run(session, job, FakeDraft(campaign=campaign))
    assert named(result, "min_interval_elapsed").passed is True


# ------------------------------------------------------------------- warm-up


def test_warmup_week_one_allows_three_a_day(session, job, campaign, live, monkeypatch):
    monkeypatch.setattr(settings, "apply_warmup_start_date", date(2026, 8, 24))
    for offset in range(3):
        add_submitted(session, job_id=80 + offset, when=GOOD_TIME)

    result = run(session, job, FakeDraft(campaign=campaign))
    check = named(result, "warmup_ramp")
    assert check.passed is False
    assert "3/3" in check.detail


def test_warmup_week_three_allows_more(session, job, campaign, live, monkeypatch):
    monkeypatch.setattr(settings, "apply_warmup_start_date", date(2026, 8, 10))
    for offset in range(3):
        add_submitted(session, job_id=80 + offset, when=GOOD_TIME)

    result = run(session, job, FakeDraft(campaign=campaign))
    assert named(result, "warmup_ramp").passed is True


# ----------------------------------------------------------- circuit breaker


def test_breaker_trips_at_exactly_three_consecutive_failures(session, tmp_path):
    assert guardrails.record_failure("linkedin", "timeout") is False
    assert guardrails.record_failure("linkedin", "timeout") is False
    assert guardrails.record_failure("linkedin", "timeout") is True

    status = guardrails.breaker_status()["linkedin"]
    assert status["disabled"] is True
    assert status["consecutive_failures"] == 3


def test_success_resets_the_breaker(session, tmp_path):
    guardrails.record_failure("seek", "boom")
    guardrails.record_failure("seek", "boom")
    guardrails.record_success("seek")
    assert guardrails.breaker_status()["seek"]["consecutive_failures"] == 0

    # And the count starts again rather than continuing.
    assert guardrails.record_failure("seek", "boom") is False


def test_breaker_state_survives_a_restart(session, tmp_path):
    guardrails.record_failure("seek", "boom")
    guardrails.record_failure("seek", "boom")
    # A fresh read, as a new process would do.
    assert guardrails.breaker_status()["seek"]["consecutive_failures"] == 2


def test_an_open_breaker_blocks_submission(session, job, campaign, live):
    for _ in range(3):
        guardrails.record_failure("seek", "boom", now=GOOD_TIME)
    result = run(session, job, FakeDraft(campaign=campaign))
    assert named(result, "circuit_breaker_closed").passed is False


def test_a_restriction_notice_trips_the_global_halt_and_writes_the_stop_file(
    session, tmp_path
):
    notices: list[tuple[str, str]] = []
    guardrails.on_notify = lambda title, body: notices.append((title, body))
    try:
        guardrails.trip_global_halt("LinkedIn account restriction notice detected")
    finally:
        guardrails.on_notify = None

    assert settings.stop_file.exists()
    assert "restriction" in settings.stop_file.read_text(encoding="utf-8")
    assert notices and notices[0][0] == "GLOBAL HALT"


# ------------------------------------------------------------------- pacing


def test_pacing_respects_the_floor_and_is_never_constant():
    rng = random.Random(1234)
    draws = [next_interval_seconds(rng) for _ in range(400)]

    assert min(draws) >= settings.apply_min_interval_floor_seconds
    assert len({round(d, 3) for d in draws}) > 100, "a fixed cadence is a bot signature"
    # Right-skewed: the mean sits above the median for a lognormal draw.
    ordered = sorted(draws)
    median = ordered[len(ordered) // 2]
    assert sum(draws) / len(draws) > median * 0.95


def test_pacing_is_deterministic_for_a_seeded_rng():
    assert next_interval_seconds(random.Random(7)) == next_interval_seconds(random.Random(7))


# --------------------------------------------------------------- reporting


def test_all_checks_are_reported_not_just_the_first_failure(session, job, campaign, live):
    campaign.active = False
    draft = FakeDraft(campaign=campaign, score=10.0, cover_letter_text="", abstentions=[FakeAbstain()])
    result = run(session, job, draft)

    failed = {c.name for c in result.failures}
    assert {"campaign_active", "score_threshold", "cover_letter_clean", "no_abstentions"} <= failed


def test_there_is_no_bypass_parameter():
    """A 'force' or 'skip_checks' argument would defeat the whole module."""
    import inspect

    params = set(inspect.signature(guardrails.check_can_submit).parameters)
    for forbidden in ("force", "skip", "skip_checks", "bypass", "override"):
        assert forbidden not in params
