"""The question ledger, and the four numbers read off it.

Every assertion here exists because a plausible wrong implementation produces a
plausible wrong number, and a wrong number on a dashboard is acted on. The three
that matter most:

* clustering that folds two questions with OPPOSITE answers into one, which
  tells the user they have already answered something they have not;
* a coverage denominator padded with profile-filled identity fields, which can
  only ever climb toward 100%;
* a funnel whose "acknowledged" and "replied" are the same set, which draws two
  bars that can never disagree.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from backend import facts, questions
from backend.apply import flow
from backend.apply.answers import same_question
from backend.apply.draft import FormField
from backend.config import settings
from backend.models import (
    AnswerBank,
    AnswerType,
    Application,
    ApplicationOutcome,
    Campaign,
    DerivedAnswer,
    Fact,
    FactCategory,
    GrayZoneAction,
    Job,
    JobStatus,
    MatchType,
    Profile,
    QuestionEvent,
    QuestionResolution,
    ResponseStatus,
    Score,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def add_event(
    session: Session,
    question: str,
    *,
    resolution: QuestionResolution = QuestionResolution.BANK,
    company: str = "Acme",
    platform: str = "seek",
    job_id: int | None = None,
    days_ago: float = 0,
) -> QuestionEvent:
    """File one encounter, creating the job it refers to if it does not exist.

    The job has to be real: ``PRAGMA foreign_keys=ON`` is installed on every
    engine by ``backend.db``, so an event pointing at a job id that was never
    inserted is rejected rather than stored — which is the correct behaviour and
    is why these helpers insert one.
    """
    if job_id is not None and session.get(Job, job_id) is None:
        _job(session, job_id, None, company=company)
        session.flush()
    event = questions.record(
        session,
        question=question,
        question_text=question,
        resolution=resolution,
        platform=platform,
        company=company,
        job_id=job_id,
    )
    assert event is not None
    if days_ago:
        event.occurred_at = datetime.now(UTC) - timedelta(days=days_ago)
        session.flush()
    return event


# --------------------------------------------------------------------------
# Clustering — the part that can report a wrong belief
# --------------------------------------------------------------------------


def test_the_same_question_worded_differently_is_one_question() -> None:
    """Casing, numbering and trailing punctuation are not different questions."""
    assert same_question("What is your notice period?", "1. what is your notice period")


def test_a_longer_wording_of_the_same_question_still_matches() -> None:
    """The whole point: eleven phrasings of one question count once."""
    assert same_question(
        "What is your notice period?",
        "What is your notice period if you were to accept an offer?",
    )


def test_opposite_qualifiers_are_never_one_question() -> None:
    """part-time vs full-time scores 88.9 — above the fuzzy threshold.

    If clustering used the score alone it would merge them, and the dashboard
    would report a question the user has answered when they have answered its
    opposite. This is the disqualifier the resolver already applies, reused.
    """
    assert not same_question(
        "Are you available for part-time work?",
        "Are you available for full-time work?",
    )


def test_a_negated_question_is_never_its_positive() -> None:
    assert not same_question(
        "Do you require visa sponsorship?",
        "Do you not require visa sponsorship?",
    )


def test_two_unrelated_questions_are_not_clustered() -> None:
    assert not same_question(
        "Do you hold a current driver's licence?",
        "What are your salary expectations?",
    )


def test_clustering_folds_wordings_and_counts_employers_not_encounters(
    session: Session,
) -> None:
    """One employer asking three times is one employer, not three."""
    add_event(session, "What is your notice period?", company="Acme")
    add_event(session, "1. what is your notice period", company="Acme")
    add_event(
        session,
        "What is your notice period if you were to accept an offer?",
        company="Acme",
    )
    add_event(session, "What is your notice period?", company="Borden")

    [cluster] = questions.clusters(session)

    assert cluster.asked == 4
    assert cluster.employers == 2
    assert len(cluster.variants) == 2


def test_clustering_keeps_conflicting_questions_apart(session: Session) -> None:
    """The disqualifier reaching all the way through to the aggregate."""
    add_event(session, "Are you available for part-time work?", company="Acme")
    add_event(session, "Are you available for full-time work?", company="Borden")

    assert len(questions.clusters(session)) == 2


def test_the_cluster_is_named_by_its_most_asked_wording(session: Session) -> None:
    """The name shown is the wording employers actually use most."""
    for _ in range(3):
        add_event(session, "What is your notice period?", company="Acme")
    add_event(
        session,
        "What is your notice period if you were to accept an offer?",
        company="Borden",
    )

    [cluster] = questions.clusters(session)

    assert cluster.question == "what is your notice period"


# --------------------------------------------------------------------------
# Friction — the ranking that decides what to answer next
# --------------------------------------------------------------------------


def test_friction_ranks_by_jobs_parked_not_by_how_often_asked(
    session: Session,
) -> None:
    """A question asked ten times and answered every time costs nothing."""
    for index in range(10):
        add_event(
            session,
            "Do you have full working rights in Australia?",
            resolution=QuestionResolution.BANK,
            company=f"Company {index}",
            job_id=index + 1,
        )
    for index in range(2):
        add_event(
            session,
            "What is your expected hourly rate?",
            resolution=QuestionResolution.ABSTAINED,
            company=f"Other {index}",
            job_id=100 + index,
        )

    ranked = questions.report(session).friction

    assert [cluster.question for cluster in ranked] == [
        "what is your expected hourly rate"
    ]
    assert ranked[0].jobs_parked == 2


def test_the_costlier_question_outranks_the_more_frequent_one(
    session: Session,
) -> None:
    """The ranking itself, with the never-parked filter taken out of play.

    Both questions here park jobs, so the filter cannot decide the order. The
    frequently-asked one parks one job; the rarely-asked one parks three, and
    three lost applications is the more expensive problem however often the
    question arrives.
    """
    for index in range(8):
        add_event(
            session,
            "Do you have full working rights in Australia?",
            resolution=(
                QuestionResolution.ABSTAINED if index == 0 else QuestionResolution.BANK
            ),
            company=f"Company {index}",
            job_id=index + 1,
        )
    for index in range(3):
        add_event(
            session,
            "What is your expected hourly rate?",
            resolution=QuestionResolution.ABSTAINED,
            company=f"Other {index}",
            job_id=50 + index,
        )

    ranked = questions.report(session).friction

    assert [cluster.jobs_parked for cluster in ranked] == [3, 1]
    assert ranked[0].question == "what is your expected hourly rate"
    assert ranked[0].asked < ranked[1].asked


def test_one_job_parking_twice_on_a_question_costs_one_job(
    session: Session,
) -> None:
    """A multi-step form that re-asks is one lost application, not two."""
    add_event(
        session,
        "What is your expected hourly rate?",
        resolution=QuestionResolution.ABSTAINED,
        job_id=7,
    )
    add_event(
        session,
        "What is your expected hourly rate?",
        resolution=QuestionResolution.ABSTAINED,
        job_id=7,
    )

    [cluster] = questions.report(session).friction

    assert cluster.abstained == 2
    assert cluster.jobs_parked == 1


# --------------------------------------------------------------------------
# Coverage — the only number here that is allowed to fall
# --------------------------------------------------------------------------


def test_coverage_is_the_resolved_share_of_one_week(session: Session) -> None:
    for index in range(9):
        add_event(session, f"Question number {index} please", job_id=index + 1)
    add_event(
        session,
        "An unanswerable question",
        resolution=QuestionResolution.ABSTAINED,
        job_id=99,
    )

    [point] = questions.coverage(session, minimum=2)

    assert point.asked == 10
    assert point.resolved == 9
    assert point.rate == 0.9


def test_a_week_below_the_minimum_reports_no_rate(session: Session) -> None:
    """The greying rule, applied to a trend line where one wrong point lies."""
    add_event(session, "Do you have a current licence?")

    [point] = questions.coverage(session, minimum=8)

    assert point.asked == 1
    assert point.sufficient_data is False
    assert point.rate is None


def test_coverage_splits_by_week_and_omits_empty_weeks(session: Session) -> None:
    add_event(session, "A question asked this week", days_ago=0)
    add_event(session, "A question asked three weeks ago", days_ago=21)

    points = questions.coverage(session, weeks=8, minimum=1)

    assert len(points) == 2
    assert points[0].week < points[1].week


# --------------------------------------------------------------------------
# Recording — what the flow files, and what it deliberately does not
# --------------------------------------------------------------------------


@pytest.fixture
def flow_session(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(
            Profile(
                version=1,
                identity={"name": "Jordan Fitzgerald", "email": "jordan@example.com"},
            )
        )
        s.add(
            Campaign(
                id=1,
                name="test",
                active=True,
                search_terms=["dev"],
                locations=["Adelaide SA"],
                score_floor=60.0,
                score_auto_apply=80.0,
                gray_zone_action=GrayZoneAction.QUEUE,
                daily_caps={"seek": 10},
            )
        )
        s.flush()
        s.add(
            Job(
                id=1,
                source="seek",
                source_job_id="1",
                url="https://example.com/1",
                title="Developer",
                company="Acme",
                dedupe_hash="h1",
                campaign_id=1,
                status=JobStatus.DOCUMENTS_READY,
            )
        )
        s.add(
            AnswerBank(
                question_pattern="Do you have full working rights in Australia?",
                match_type=MatchType.FUZZY,
                answer_value="Yes",
                answer_type=AnswerType.BOOLEAN,
            )
        )
        s.commit()
        yield s


FORM = [
    FormField(identifier="name", label="Full name"),
    FormField(
        identifier="rights",
        label="Do you have full working rights in Australia?",
        choices=["Yes", "No"],
    ),
    FormField(identifier="rate", label="What is your expected hourly rate?"),
    FormField(identifier="resume", label="Resume", kind="file"),
]


def test_building_a_draft_files_every_screening_question(
    flow_session: Session,
) -> None:
    job = flow_session.get(Job, 1)

    flow.build_draft(flow_session, job, platform="seek", fields=list(FORM))

    filed = {
        event.question: event.resolution
        for event in flow_session.exec(select(QuestionEvent)).all()
    }
    assert filed == {
        "do you have full working rights in australia": QuestionResolution.BANK,
        "what is your expected hourly rate": QuestionResolution.ABSTAINED,
    }


def test_profile_filled_identity_fields_are_not_screening_questions(
    flow_session: Session,
) -> None:
    """The coverage denominator must not contain fields that cannot fail.

    "Full name" is answered from the profile on every form. Counting it would
    add a question the system always gets right, and coverage would climb
    toward 100% while nothing about the answer bank had improved.
    """
    job = flow_session.get(Job, 1)

    flow.build_draft(flow_session, job, platform="seek", fields=list(FORM))

    filed = [event.question for event in flow_session.exec(select(QuestionEvent)).all()]
    assert "full name" not in filed


def test_an_upload_slot_is_not_a_question(flow_session: Session) -> None:
    job = flow_session.get(Job, 1)

    flow.build_draft(flow_session, job, platform="seek", fields=list(FORM))

    filed = [event.question for event in flow_session.exec(select(QuestionEvent)).all()]
    assert "resume" not in filed


def test_the_bank_row_that_answered_is_recorded(flow_session: Session) -> None:
    job = flow_session.get(Job, 1)
    row = flow_session.exec(select(AnswerBank)).one()

    flow.build_draft(flow_session, job, platform="seek", fields=list(FORM))

    answered = flow_session.exec(
        select(QuestionEvent).where(QuestionEvent.resolution == QuestionResolution.BANK)
    ).one()
    assert answered.source_row_id == row.id


def test_a_blank_question_is_not_filed(session: Session) -> None:
    """A row keyed on "" would join with every other label-less field."""
    assert (
        questions.record(
            session,
            question="   ",
            question_text="",
            resolution=QuestionResolution.ABSTAINED,
            platform="seek",
        )
        is None
    )
    assert session.exec(select(QuestionEvent)).all() == []


# --------------------------------------------------------------------------
# Fact leverage
# --------------------------------------------------------------------------


def test_fact_leverage_counts_confirmed_and_stale_separately(
    session: Session,
) -> None:
    fact = facts.set_fact(
        session,
        key="licence",
        text="Full SA driver's licence, class C",
        category=FactCategory.LICENCE,
    )
    session.flush()
    session.add(
        DerivedAnswer(
            question_key="do you hold a licence",
            question_text="Do you hold a licence?",
            answer_value="Yes",
            answer_type=AnswerType.BOOLEAN,
            fact_id=fact.id,
            fact_text_hash=facts.fact_hash(fact.text),
            confirmed_at=datetime.now(UTC),
        )
    )
    session.add(
        DerivedAnswer(
            question_key="can you drive a manual",
            question_text="Can you drive a manual?",
            answer_value="Yes",
            answer_type=AnswerType.BOOLEAN,
            fact_id=fact.id,
            fact_text_hash="stale-hash-value",
        )
    )
    session.flush()

    [leverage] = facts.leverage(session)

    assert leverage.derived == 2
    assert leverage.confirmed == 1
    assert leverage.stale == 1


def test_a_fact_answering_nothing_is_still_reported(session: Session) -> None:
    """A fact supporting nothing is a finding, not a row to hide."""
    facts.set_fact(
        session, key="police_check", text="Cleared 2025", category=FactCategory.CHECKS
    )
    session.flush()

    [leverage] = facts.leverage(session)

    assert leverage.derived == 0


# --------------------------------------------------------------------------
# Per-campaign funnel
# --------------------------------------------------------------------------


def _campaign(session: Session, campaign_id: int, name: str) -> None:
    session.add(
        Campaign(
            id=campaign_id,
            name=name,
            active=True,
            search_terms=["dev"],
            locations=["Adelaide SA"],
            score_floor=60.0,
            score_auto_apply=80.0,
            gray_zone_action=GrayZoneAction.QUEUE,
        )
    )


def _job(
    session: Session, job_id: int, campaign_id: int | None, company: str = "Acme"
) -> None:
    session.add(
        Job(
            id=job_id,
            source="seek",
            source_job_id=str(job_id),
            url=f"https://example.com/{job_id}",
            title="Developer",
            company=company,
            dedupe_hash=f"h{job_id}",
            campaign_id=campaign_id,
            status=JobStatus.DISCOVERED,
        )
    )


def _funnels(session: Session, minimum: int = 1):
    from backend.api.routers.work import _campaign_funnels

    applications = list(session.exec(select(Application)).all())
    jobs = {job.id: job for job in session.exec(select(Job)).all()}
    return {f.name: f for f in _campaign_funnels(session, applications, jobs, minimum)}


def test_discovered_counts_every_job_of_the_campaign_whatever_its_status(
    session: Session,
) -> None:
    """Status is overwritten as a job advances, so filtering it shrinks the top.

    A funnel whose first bar falls as the pipeline succeeds is worse than no
    funnel: it reads as the campaign finding fewer ads.
    """
    _campaign(session, 1, "graduate")
    _job(session, 1, 1)
    _job(session, 2, 1)
    session.flush()
    session.get(Job, 2).status = JobStatus.REJECTED
    session.flush()

    assert _funnels(session)["graduate"].discovered == 2


def test_scored_counts_jobs_once_however_many_score_rows_they_have(
    session: Session,
) -> None:
    """One job legitimately has a Score row per profile/rubric combination."""
    _campaign(session, 1, "graduate")
    _job(session, 1, 1)
    session.flush()
    session.add(Score(job_id=1, profile_version=1, rubric_version=1, final=80.0))
    session.add(Score(job_id=1, profile_version=1, rubric_version=2, final=82.0))
    session.flush()

    assert _funnels(session)["graduate"].scored == 1


def test_a_score_with_no_number_is_not_a_scored_job(session: Session) -> None:
    """A stage-2 failure still writes a row, with nothing usable in it."""
    _campaign(session, 1, "graduate")
    _job(session, 1, 1)
    session.flush()
    session.add(Score(job_id=1, profile_version=1, rubric_version=1, final=None))
    session.flush()

    assert _funnels(session)["graduate"].scored == 0


def test_applied_counts_submitted_applications_only(session: Session) -> None:
    """An aborted attempt never reached an employer.

    Counting it puts a job in the denominator of every reply rate below it,
    which drags a campaign's interview rate down for attempts nobody ever saw.
    """
    _campaign(session, 1, "graduate")
    _job(session, 1, 1)
    _job(session, 2, 1)
    session.flush()
    session.add(Application(job_id=1, outcome=ApplicationOutcome.SUBMITTED))
    session.add(Application(job_id=2, outcome=ApplicationOutcome.ABORTED))
    session.flush()

    assert _funnels(session)["graduate"].applied == 1


def test_acknowledged_and_replied_are_different_numbers(session: Session) -> None:
    """They were the same set, so the two bars could never disagree.

    An automated acknowledgement is being heard; a rejection or an interview
    request is a person replying. A funnel that cannot tell them apart says
    nothing about whether applications reach a human.
    """
    _campaign(session, 1, "graduate")
    for job_id in (1, 2):
        _job(session, job_id, 1)
    session.flush()
    session.add(
        Application(
            job_id=1,
            outcome=ApplicationOutcome.SUBMITTED,
            response_status=ResponseStatus.ACKNOWLEDGED,
        )
    )
    session.add(
        Application(
            job_id=2,
            outcome=ApplicationOutcome.SUBMITTED,
            response_status=ResponseStatus.REJECTED,
        )
    )
    session.flush()

    funnel = _funnels(session)["graduate"]

    assert funnel.acknowledged == 2
    assert funnel.replied == 1


def test_an_interview_counts_at_every_stage_above_it(session: Session) -> None:
    """response_status holds the latest state, so the stages nest."""
    _campaign(session, 1, "graduate")
    _job(session, 1, 1)
    session.flush()
    session.add(
        Application(
            job_id=1,
            outcome=ApplicationOutcome.SUBMITTED,
            response_status=ResponseStatus.INTERVIEW_REQUEST,
        )
    )
    session.flush()

    funnel = _funnels(session)["graduate"]

    assert (funnel.acknowledged, funnel.replied, funnel.interviews) == (1, 1, 1)


def test_jobs_with_no_campaign_are_shown_not_hidden(session: Session) -> None:
    _job(session, 1, None)
    session.flush()

    assert _funnels(session)["unassigned"].discovered == 1


def test_the_funnel_rate_is_suppressed_below_the_minimum(session: Session) -> None:
    _campaign(session, 1, "graduate")
    _job(session, 1, 1)
    session.flush()
    session.add(
        Application(
            job_id=1,
            outcome=ApplicationOutcome.SUBMITTED,
            response_status=ResponseStatus.INTERVIEW_REQUEST,
        )
    )
    session.flush()

    funnel = _funnels(session, minimum=8)["graduate"]

    assert funnel.interviews == 1
    assert funnel.sufficient_data is False
    assert funnel.interview_rate is None


# --------------------------------------------------------------------------
# Digest
# --------------------------------------------------------------------------


def test_the_digest_section_is_empty_when_nothing_was_asked(
    session: Session,
) -> None:
    assert questions.digest_lines(session) == []


def test_the_digest_names_the_question_that_cost_the_most(
    session: Session,
) -> None:
    for job_id in (1, 2, 3):
        add_event(
            session,
            "What is your expected hourly rate?",
            resolution=QuestionResolution.ABSTAINED,
            company=f"Company {job_id}",
            job_id=job_id,
        )

    lines = "\n".join(questions.digest_lines(session))

    assert "expected hourly rate" in lines
    assert "parked 3 jobs" in lines


def test_the_facts_table_is_untouched_by_the_question_ledger(
    session: Session,
) -> None:
    """Recording a question must not create facts or derivations."""
    add_event(session, "Do you hold a current licence?")

    assert session.exec(select(Fact)).all() == []
    assert session.exec(select(DerivedAnswer)).all() == []
