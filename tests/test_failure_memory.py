"""Failure memory: the questions a circuit breaker cannot answer.

The breaker stops repeated failure and then forgets it happened, so every
failure looks like the first. These pin the four questions the ledger exists
to answer, and the rule that it surfaces trends rather than events.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from backend import failures
from backend.failures import RECURRENCE_THRESHOLD
from backend.models import FailureEvent, FailureType


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def add(session, *, hours_ago: float = 0, **kwargs):
    kwargs.setdefault("platform", "linkedin")
    kwargs.setdefault("failure_type", FailureType.SELECTOR_DRIFT)
    event = failures.record(session, **kwargs)
    if hours_ago:
        event.occurred_at = datetime.now(UTC) - timedelta(hours=hours_ago)
    session.flush()
    return event


# ------------------------------------------------------------------- recording


def test_a_failure_is_written_with_its_dimensions(session):
    failures.record(
        session,
        platform="seek",
        failure_type=FailureType.ELEMENT_UNRESOLVED,
        element_id="submit_button",
        company="Acme",
        job_id=None,
        detail="tried 4 strategies",
    )
    session.flush()

    row = session.exec(select(FailureEvent)).one()
    assert row.platform == "seek"
    assert row.element_id == "submit_button"
    assert row.company == "Acme"
    assert row.resolved_at is None


def test_detail_is_truncated_rather_than_unbounded(session):
    failures.record(
        session,
        platform="seek",
        failure_type=FailureType.SUBMIT_FAILED,
        detail="x" * 5000,
    )
    session.flush()
    assert len(session.exec(select(FailureEvent)).one().detail) <= 500


# --------------------------------------------------------- the four questions


def test_it_answers_which_selectors_drift_most(session):
    for _ in range(4):
        add(session, element_id="resume_file_input")
    for _ in range(2):
        add(session, element_id="submit_button")
    add(session, element_id="modal")  # once only

    report = failures.trends(session)
    labels = [trend.label for trend in report.drifting_elements]

    assert labels[0] == "resume_file_input", "most frequent first"
    assert "submit_button" in labels
    assert "modal" not in labels, "a single occurrence is not a trend"


def test_it_answers_which_employers_consistently_abstain(session):
    for _ in range(3):
        add(
            session,
            failure_type=FailureType.ANSWER_ABSTAINED,
            company="Globex",
            question="Do you hold a current forklift licence?",
        )
    add(session, failure_type=FailureType.ANSWER_ABSTAINED, company="Initech", question="q")

    report = failures.trends(session)
    assert [t.label for t in report.abstaining_companies] == ["Globex"]
    assert report.abstaining_companies[0].count == 3
    assert report.abstaining_companies[0].recurring


def test_it_answers_which_questions_keep_arriving_unanswered(session):
    for company in ("A", "B", "C"):
        add(
            session,
            failure_type=FailureType.ANSWER_ABSTAINED,
            company=company,
            question="Do you have a current driver's licence?",
        )

    report = failures.trends(session)
    assert report.unanswered_questions[0].label == "Do you have a current driver's licence?"
    assert report.unanswered_questions[0].count == 3, (
        "the same question across three employers is one trend, not three"
    )


def test_it_answers_whether_a_parse_gate_failure_is_new_or_recurring(session):
    assert not failures.is_recurring(
        session, platform="seek", failure_type=FailureType.PARSE_GATE
    )

    for _ in range(RECURRENCE_THRESHOLD):
        add(session, platform="seek", failure_type=FailureType.PARSE_GATE)

    assert failures.is_recurring(
        session, platform="seek", failure_type=FailureType.PARSE_GATE
    )
    assert not failures.is_recurring(
        session, platform="linkedin", failure_type=FailureType.PARSE_GATE
    ), "recurrence is per platform"


# ------------------------------------------------------------------ resolution


def test_resolving_removes_a_failure_from_the_trends(session):
    for _ in range(3):
        add(session, element_id="resume_file_input")

    assert failures.trends(session).drifting_elements

    closed = failures.resolve(
        session,
        platform="linkedin",
        failure_type=FailureType.SELECTOR_DRIFT,
        element_id="resume_file_input",
        resolution="strategy re-promoted after the redesign",
    )
    session.flush()

    assert closed == 3
    assert not failures.trends(session).drifting_elements, (
        "a resolved failure must stop voting, or every old failure trends forever"
    )


def test_resolving_leaves_other_elements_alone(session):
    add(session, element_id="a")
    add(session, element_id="b")

    failures.resolve(
        session,
        platform="linkedin",
        failure_type=FailureType.SELECTOR_DRIFT,
        element_id="a",
    )
    session.flush()

    still_open = [
        row for row in session.exec(select(FailureEvent)).all() if row.resolved_at is None
    ]
    assert [row.element_id for row in still_open] == ["b"]


def test_resolved_events_are_still_counted_in_the_total(session):
    for _ in range(2):
        add(session, element_id="a")
    failures.resolve(
        session, platform="linkedin", failure_type=FailureType.SELECTOR_DRIFT, element_id="a"
    )
    session.flush()

    report = failures.trends(session)
    assert report.total == 2
    assert report.resolved == 2


# ---------------------------------------------------------------------- window


def test_events_outside_the_window_do_not_trend(session):
    for _ in range(5):
        add(session, element_id="old", hours_ago=400)
    session.flush()

    assert not failures.trends(session, hours=168).drifting_elements
    assert failures.trends(session, hours=1000).drifting_elements


# ---------------------------------------------------------------------- digest


def test_the_digest_says_nothing_when_there_is_nothing_to_say(session):
    """A section that appears every evening saying nothing stops being read."""
    assert failures.digest_lines(session) == []

    add(session, element_id="one_off")
    assert failures.digest_lines(session) == [], "a single event is not a trend"


def test_the_digest_reports_trends_not_individual_events(session):
    for _ in range(4):
        add(session, element_id="resume_file_input")

    lines = failures.digest_lines(session)
    body = "\n".join(lines)

    assert "Failure trends" in body
    assert "resume_file_input" in body
    assert "4×" in body
    assert len([line for line in lines if "resume_file_input" in line]) == 1, (
        "four occurrences must be one line, not four"
    )


def test_the_digest_names_the_platform_and_recency(session):
    for _ in range(3):
        add(session, platform="seek", element_id="apply_button", hours_ago=2)
    session.flush()

    body = "\n".join(failures.digest_lines(session))
    assert "seek" in body
    assert "ago" in body


def test_the_digest_does_not_repeat_an_itemised_type_as_a_total(session):
    """Drift is already listed per element; a 'selector drift: 4x' line adds noise."""
    for _ in range(4):
        add(session, element_id="resume_file_input")

    lines = failures.digest_lines(session)

    itemised = [line for line in lines if "resume_file_input" in line]
    assert len(itemised) == 1, "the element gets exactly one line"

    summary = [
        line
        for line in lines
        if "selector drift" in line.lower() and "resume_file_input" not in line
    ]
    assert not summary, f"the type total duplicates the itemised line: {summary}"


def test_a_type_with_no_itemised_line_still_reaches_the_digest(session):
    """The suppression must be narrow: only types already listed per element.

    Without this the previous test would pass just as happily if the recurring
    -type section were deleted outright.
    """
    for _ in range(RECURRENCE_THRESHOLD):
        add(session, platform="seek", failure_type=FailureType.PARSE_GATE)

    body = "\n".join(failures.digest_lines(session))
    assert "parse gate" in body.lower()


def test_a_naive_timestamp_from_sqlite_does_not_crash_the_digest(session):
    """Rows read back from SQLite are naive; the schema stores UTC.

    Subtracting a naive datetime from an aware one raises, and it would raise
    inside the evening digest — the one message the user actually reads.
    """
    for _ in range(3):
        event = add(session, element_id="x")
        event.occurred_at = datetime.now(UTC).replace(tzinfo=None)
    session.flush()

    assert failures.digest_lines(session), "must render, not raise"


# ------------------------------------------------------------------- wiring


def test_an_unresolvable_element_lands_in_the_ledger(tmp_path, monkeypatch):
    """The flow's park path must write the row, not just log it."""
    from backend.apply import flow
    from backend.config import settings
    from backend.models import Job, JobStatus

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    from backend.siteknowledge import ElementNotFound

    with Session(engine) as session:
        job = Job(
            id=1,
            source="seek",
            source_job_id="1",
            url="https://example.com/1",
            title="Developer",
            company="Acme",
            location="Adelaide SA",
            dedupe_hash="h1",
            status=JobStatus.DOCUMENTS_READY,
        )
        session.add(job)
        session.flush()

        flow._park_unresolvable(
            session, job, "seek", ElementNotFound("seek", "submit_button", ["a", "b"])
        )
        session.flush()

        row = session.exec(select(FailureEvent)).one()
        assert row.failure_type is FailureType.ELEMENT_UNRESOLVED
        assert row.element_id == "submit_button"
        assert row.company == "Acme"
        assert row.job_id == 1
