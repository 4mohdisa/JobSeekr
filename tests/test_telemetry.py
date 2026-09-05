"""Speed, cache hit rates and cost — and the one number that must never be work.

The assertion this file exists for is the pacing separation. The randomised
delay between submissions protects the user's LinkedIn account; if it ever lands
in a work total, the chart says the system is slow and the obvious fix is to
shorten the thing keeping the account alive. Everything else here guards a
number that would be quietly wrong rather than obviously missing.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from backend import telemetry
from backend.models import (
    WORK_STAGES,
    Application,
    ApplicationOutcome,
    CacheEvent,
    CacheName,
    Job,
    JobStatus,
    LLMSpend,
    QuestionEvent,
    QuestionResolution,
    Run,
    RunPhase,
    Stage,
    StageTiming,
)
from backend.siteknowledge import ElementNotFound, Strategy, drain_resolutions


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def add_timing(
    session: Session,
    stage: Stage,
    ms: int,
    *,
    days_ago: float = 0,
    at: datetime | None = None,
) -> StageTiming:
    row = StageTiming(stage=stage, duration_ms=ms)
    session.add(row)
    session.flush()
    if at is not None:
        row.occurred_at = at
    elif days_ago:
        row.occurred_at = datetime.now(UTC) - timedelta(days=days_ago)
    session.flush()
    return row


def add_job(session: Session, job_id: int) -> Job:
    job = Job(
        id=job_id,
        source="seek",
        source_job_id=str(job_id),
        url=f"https://example.com/{job_id}",
        title="Developer",
        company="Acme",
        dedupe_hash=f"h{job_id}",
        status=JobStatus.DISCOVERED,
    )
    session.add(job)
    session.flush()
    return job


# --------------------------------------------------------------------------
# Pacing is not work
# --------------------------------------------------------------------------


def test_pacing_is_not_a_work_stage() -> None:
    """The structural guarantee, asserted rather than assumed.

    WORK_STAGES is every stage minus pacing, so a stage added later is work by
    default. This pins the one exclusion that matters.
    """
    assert Stage.PACING not in WORK_STAGES
    assert WORK_STAGES == set(Stage) - {Stage.PACING}


def test_a_long_pacing_wait_does_not_enter_the_work_total(session: Session) -> None:
    """The whole point. A ten-minute safety wait must not read as latency."""
    add_timing(session, Stage.PAGE_LOAD, 2_000)
    add_timing(session, Stage.PACING, 600_000)

    profile = telemetry.stage_profile(session)

    assert profile.work_total_ms == 2_000
    assert [stat.stage for stat in profile.work] == [Stage.PAGE_LOAD.value]


def test_pacing_is_still_measured(session: Session) -> None:
    """Excluded from work, not discarded — an unmeasured wait hides a hang."""
    add_timing(session, Stage.PACING, 600_000)

    profile = telemetry.stage_profile(session)

    assert profile.pacing is not None
    assert profile.pacing.total_ms == 600_000


def test_pacing_is_never_flagged_as_the_slowest_stage(session: Session) -> None:
    """It is always the longest thing in a real pass, and it is not a problem."""
    add_timing(session, Stage.PAGE_LOAD, 2_000)
    add_timing(session, Stage.PACING, 600_000)

    slowest = telemetry.stage_profile(session).slowest

    assert slowest is not None
    assert slowest.stage == Stage.PAGE_LOAD.value


# --------------------------------------------------------------------------
# Stage timing
# --------------------------------------------------------------------------


def test_a_stage_that_raised_is_still_timed(session: Session) -> None:
    """A stage that got slow and then started failing is a real regression.

    Dropping the timing on the failure path would hide exactly the runs worth
    looking at.
    """
    with pytest.raises(RuntimeError), telemetry.time_stage(session, Stage.SUBMIT):
        raise RuntimeError("the form exploded")

    [row] = session.exec(select(StageTiming)).all()
    assert row.stage is Stage.SUBMIT


def test_the_timer_measures_elapsed_time(session: Session) -> None:
    with telemetry.time_stage(session, Stage.PAGE_LOAD):
        time.sleep(0.02)

    [row] = session.exec(select(StageTiming)).all()
    assert row.duration_ms >= 15


def test_the_slowest_stage_is_by_total_time_not_by_one_outlier(
    session: Session,
) -> None:
    """One slow page load is weather; a stage that is always slow is the cost."""
    add_timing(session, Stage.PAGE_LOAD, 9_000)
    for _ in range(10):
        add_timing(session, Stage.FIELD_ENUMERATION, 2_000)

    slowest = telemetry.stage_profile(session).slowest

    assert slowest is not None
    assert slowest.stage == Stage.FIELD_ENUMERATION.value


def test_the_median_is_not_the_mean(session: Session) -> None:
    """A single outlier must not move the number the user reads."""
    for ms in (100, 100, 100, 100, 10_000):
        add_timing(session, Stage.UPLOAD, ms)

    [stat] = telemetry.stage_profile(session).work

    assert stat.median_ms == 100
    assert stat.mean_ms > 2_000


def test_the_window_excludes_older_timings(session: Session) -> None:
    add_timing(session, Stage.PAGE_LOAD, 1_000, days_ago=0)
    add_timing(session, Stage.PAGE_LOAD, 9_000, days_ago=30)

    [stat] = telemetry.stage_profile(session, hours=24 * 7).work

    assert stat.observations == 1
    assert stat.total_ms == 1_000


# --------------------------------------------------------------------------
# Per-run profile
# --------------------------------------------------------------------------


def test_a_run_is_flagged_with_its_own_slowest_stage(session: Session) -> None:
    started = datetime.now(UTC) - timedelta(hours=2)
    ended = started + timedelta(minutes=30)
    session.add(
        Run(
            started_at=started,
            ended_at=ended,
            phase=RunPhase.APPLY,
            counts={"considered": 3},
        )
    )
    add_timing(session, Stage.PAGE_LOAD, 1_000, at=started + timedelta(minutes=1))
    add_timing(session, Stage.SUBMIT, 8_000, at=started + timedelta(minutes=2))
    session.flush()

    [profile] = telemetry.run_profiles(session)

    assert profile.slowest_stage == Stage.SUBMIT.value
    assert profile.applications == 3


def test_timings_outside_a_run_window_belong_to_no_run(session: Session) -> None:
    """Attribution is by window; a timing from before the pass is not its cost."""
    started = datetime.now(UTC) - timedelta(hours=2)
    ended = started + timedelta(minutes=30)
    session.add(
        Run(started_at=started, ended_at=ended, phase=RunPhase.APPLY, counts={})
    )
    add_timing(session, Stage.SUBMIT, 8_000, at=started - timedelta(hours=1))
    session.flush()

    [profile] = telemetry.run_profiles(session)

    assert profile.slowest_stage is None
    assert profile.work_ms == 0


def test_a_run_reports_pacing_apart_from_work(session: Session) -> None:
    started = datetime.now(UTC) - timedelta(hours=2)
    session.add(
        Run(
            started_at=started,
            ended_at=started + timedelta(minutes=30),
            phase=RunPhase.APPLY,
            counts={},
        )
    )
    add_timing(session, Stage.SUBMIT, 5_000, at=started + timedelta(minutes=1))
    add_timing(session, Stage.PACING, 300_000, at=started + timedelta(minutes=2))
    session.flush()

    [profile] = telemetry.run_profiles(session)

    assert profile.work_ms == 5_000
    assert profile.pacing_ms == 300_000


# --------------------------------------------------------------------------
# Cache rates
# --------------------------------------------------------------------------


def test_a_cache_rate_is_hits_over_lookups(session: Session) -> None:
    telemetry.record_cache(session, CacheName.FORM_MAP, hit=True, count=3)
    telemetry.record_cache(session, CacheName.FORM_MAP, hit=False, count=1)

    [rate] = telemetry.cache_rates(session)

    assert (rate.lookups, rate.hits, rate.rate) == (4, 3, 0.75)


def test_a_batch_records_one_row_per_lookup(session: Session) -> None:
    """Embeddings resolve in batches; the rate needs the individual lookups."""
    telemetry.record_cache(session, CacheName.EMBEDDING, hit=True, count=40)

    assert len(session.exec(select(CacheEvent)).all()) == 40


def test_recording_zero_lookups_writes_nothing(session: Session) -> None:
    telemetry.record_cache(session, CacheName.EMBEDDING, hit=True, count=0)

    assert session.exec(select(CacheEvent)).all() == []


def test_the_answer_bank_rate_comes_from_the_question_ledger(
    session: Session,
) -> None:
    """Not recorded twice: the bank's lookups ARE screening questions."""
    for resolution in (
        QuestionResolution.BANK,
        QuestionResolution.BANK,
        QuestionResolution.ABSTAINED,
    ):
        session.add(
            QuestionEvent(
                question="do you have full working rights",
                question_text="Do you have full working rights?",
                resolution=resolution,
                platform="seek",
            )
        )
    session.flush()

    bank = next(
        rate for rate in telemetry.cache_rates(session) if rate.cache == "answer_bank"
    )

    assert (bank.lookups, bank.hits) == (3, 2)
    assert session.exec(select(CacheEvent)).all() == []


def test_the_facts_rate_is_measured_over_what_the_bank_missed(
    session: Session,
) -> None:
    """The caches are consulted in sequence, so the denominators differ.

    A facts rate computed over ALL questions would fall every time the answer
    bank improved — the opposite of the truth.
    """
    for resolution in (
        QuestionResolution.BANK,
        QuestionResolution.BANK,
        QuestionResolution.FACT,
        QuestionResolution.ABSTAINED,
    ):
        session.add(
            QuestionEvent(
                question="do you hold a licence",
                question_text="Do you hold a licence?",
                resolution=resolution,
                platform="seek",
            )
        )
    session.flush()

    facts_rate = next(
        rate for rate in telemetry.cache_rates(session) if rate.cache == "facts"
    )

    assert (facts_rate.lookups, facts_rate.hits) == (2, 1)


def test_every_cache_says_what_one_lookup_counts(session: Session) -> None:
    """A per-form rate next to a per-question rate is a trap without the unit."""
    telemetry.record_cache(session, CacheName.FORM_MAP, hit=True)
    telemetry.record_cache(session, CacheName.SITE_KNOWLEDGE, hit=True)
    telemetry.record_cache(session, CacheName.EMBEDDING, hit=True)

    for rate in telemetry.cache_rates(session):
        assert rate.unit


# --------------------------------------------------------------------------
# Cost
# --------------------------------------------------------------------------


def test_cost_per_application_divides_spend_by_submitted_applications(
    session: Session,
) -> None:
    for job_id in (1, 2):
        add_job(session, job_id)
        session.add(Application(job_id=job_id, outcome=ApplicationOutcome.SUBMITTED))
        session.add(
            LLMSpend(
                model="gemini",
                input_tokens=100,
                output_tokens=50,
                cost_usd=0.02,
                purpose="document_closing",
                job_id=job_id,
            )
        )
    session.flush()

    [point] = telemetry.cost_per_application(session)

    assert point.applications == 2
    assert point.per_application_usd == 0.02


def test_an_aborted_attempt_is_not_an_application(session: Session) -> None:
    """It burned tokens and never reached an employer.

    Counting it would put spend in the numerator and nothing in the denominator,
    reading as a cost increase when the truth is a failed run.
    """
    add_job(session, 1)
    session.add(Application(job_id=1, outcome=ApplicationOutcome.ABORTED))
    session.add(
        LLMSpend(
            model="gemini",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.50,
            purpose="document_closing",
            job_id=1,
        )
    )
    session.flush()

    assert telemetry.cost_per_application(session) == []


def test_another_job_s_spend_does_not_land_on_this_application(
    session: Session,
) -> None:
    """Cost is keyed by job. Pooling it would report the same figure for every
    application and hide the expensive one."""
    add_job(session, 1)
    add_job(session, 2)
    session.add(Application(job_id=1, outcome=ApplicationOutcome.SUBMITTED))
    session.add(
        LLMSpend(
            model="gemini",
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.01,
            purpose="document_closing",
            job_id=1,
        )
    )
    session.add(
        LLMSpend(
            model="gemini",
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.99,
            purpose="document_closing",
            job_id=2,
        )
    )
    session.flush()

    [point] = telemetry.cost_per_application(session)

    assert point.per_application_usd == 0.01


def test_spend_with_no_job_is_not_attributed(session: Session) -> None:
    """The campaign summary embedding is per campaign. It is real and not here."""
    add_job(session, 1)
    session.add(Application(job_id=1, outcome=ApplicationOutcome.SUBMITTED))
    session.add(
        LLMSpend(
            model="openai",
            input_tokens=10,
            output_tokens=0,
            cost_usd=0.99,
            purpose="stage1_summary",
            job_id=None,
        )
    )
    session.flush()

    [point] = telemetry.cost_per_application(session)

    assert point.per_application_usd == 0.0


# --------------------------------------------------------------------------
# Digest
# --------------------------------------------------------------------------


def test_the_digest_section_is_empty_when_nothing_was_measured(
    session: Session,
) -> None:
    assert telemetry.digest_lines(session) == []


def test_the_digest_names_pacing_as_deliberate(session: Session) -> None:
    """Read out of context, a ten-minute wait looks like a fault."""
    add_timing(session, Stage.PAGE_LOAD, 2_000)
    add_timing(session, Stage.PACING, 600_000)

    lines = "\n".join(telemetry.digest_lines(session))

    assert "not work" in lines
    assert "slowest stage: page load" in lines


# --------------------------------------------------------------------------
# Site-knowledge lookups
# --------------------------------------------------------------------------


def test_a_healed_element_is_a_cache_miss_not_a_hit() -> None:
    """The element was found; the file's idea of how to find it was wrong.

    Counting a heal as a hit would report a knowledge file that is quietly
    rotting as one that is working perfectly — which is the exact failure this
    number exists to surface before it becomes an outage.
    """
    from tests.test_siteknowledge import FakePage, knowledge_with

    drain_resolutions()
    knowledge = knowledge_with(
        Strategy(type="testid", value="apply", attr="data-automation"),
        Strategy(type="css", value="button.apply"),
    )
    knowledge.resolve(FakePage({"button.apply"}), "apply_button")

    assert drain_resolutions() == (0, 1)


def test_the_top_strategy_working_is_a_hit() -> None:
    from tests.test_siteknowledge import FakePage, knowledge_with

    drain_resolutions()
    knowledge = knowledge_with(
        Strategy(type="testid", value="apply", attr="data-automation"),
        Strategy(type="css", value="button.apply"),
    )
    knowledge.resolve(FakePage({"[data-automation='apply']"}), "apply_button")

    assert drain_resolutions() == (1, 0)


def test_an_element_nothing_resolves_is_a_miss() -> None:
    from tests.test_siteknowledge import FakePage, knowledge_with

    drain_resolutions()
    knowledge = knowledge_with(Strategy(type="css", value="button.apply"))
    with pytest.raises(ElementNotFound):
        knowledge.resolve(FakePage(set()), "apply_button")

    assert drain_resolutions() == (0, 1)


def test_draining_resets_the_tally() -> None:
    """Without the reset every application inherits every earlier one's lookups,
    and the rate becomes a running total that can never fall."""
    from tests.test_siteknowledge import FakePage, knowledge_with

    drain_resolutions()
    knowledge = knowledge_with(
        Strategy(type="testid", value="apply", attr="data-automation")
    )
    knowledge.resolve(FakePage({"[data-automation='apply']"}), "apply_button")

    assert drain_resolutions() == (1, 0)
    assert drain_resolutions() == (0, 0)
