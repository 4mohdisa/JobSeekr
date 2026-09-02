"""The answer-bank loop, driven all the way round.

``Claude.md`` hard rule 2 is "abstain, park the job, ask via Telegram, save the
answer, retry". Every piece existed before this file; the *asking* was never
called, so the loop stopped at "park" and the bank could never self-populate.

These tests walk the whole circuit — abstain, escalate, store, re-queue, resolve
— against the real ``session_scope`` database so the two halves that run in
different processes in production (the apply pass parks and asks; the Telegram
bot answers, minutes or hours later) meet through the same storage they meet
through for real. Only the Telegram HTTP call is faked.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlmodel import select

from backend.apply import run as apply_run
from backend.apply.draft import FormField
from backend.base import ApplyOutcome
from backend.config import settings
from backend.db import session_scope
from backend.integrations import telegram
from backend.models import (
    AnswerBank,
    AnswerType,
    Application,
    Campaign,
    Document,
    DocumentKind,
    Job,
    JobStatus,
    MatchType,
    Profile,
    Score,
)
from tests.test_flow import FakeAdapter, FakePage

RIGHTS_QUESTION = "Do you have full working rights in Australia?"


@pytest.fixture(autouse=True)
def _neutral_clock(monkeypatch):
    """The loop is about sequence, not the hour the suite happens to run at."""
    monkeypatch.setattr(settings, "apply_window_start", "00:00")
    monkeypatch.setattr(settings, "apply_window_end", "23:59")


@pytest.fixture
def sent(monkeypatch) -> list[str]:
    """Capture Telegram sends without configuring or contacting Telegram."""
    outbox: list[str] = []

    def fake_send(text: str, priority: Any = None) -> bool:
        outbox.append(text)
        return True

    monkeypatch.setattr(telegram, "send_message", fake_send)
    return outbox


@pytest.fixture
def parked_job(tmp_path, monkeypatch):
    """A job whose only screening question maps to a blank answer-bank row.

    A *blank* row rather than no row at all, because that is the shape the
    seeded bank ships in: 21 questions with empty answers, waiting to be filled.
    It is also the shape that used to break the loop, so it is the one worth
    walking.
    """
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    with session_scope() as session:
        # Children before parents: Application points at both job and document,
        # and SQLite enforces the foreign keys.
        for table in (Application, Document, Score, Job, Campaign, AnswerBank, Profile):
            for row in session.exec(select(table)).all():
                session.delete(row)
        session.flush()

        session.add(
            Profile(
                version=1,
                identity={"name": "Jordan Fitzgerald", "email": "jordan@example.com"},
            )
        )
        campaign = Campaign(
            name="loop",
            active=True,
            search_terms=["dev"],
            locations=["Adelaide SA"],
            score_floor=60.0,
            score_auto_apply=80.0,
        )
        session.add(campaign)
        session.flush()

        job = Job(
            source="seek",
            source_job_id="loop-1",
            url="https://example.com/loop-1",
            title="Developer",
            company="Acme",
            dedupe_hash="loop-h1",
            campaign_id=campaign.id,
            status=JobStatus.DOCUMENTS_READY,
        )
        session.add(job)
        session.flush()

        for kind, name in (
            (DocumentKind.RESUME, "resume.pdf"),
            (DocumentKind.COVER_LETTER, "cover_letter.pdf"),
            (DocumentKind.COMBINED, "combined.pdf"),
        ):
            session.add(
                Document(
                    job_id=job.id,
                    kind=kind,
                    path=str(tmp_path / f"job_{job.id}/{name}"),
                    sha256="deadbeef",
                    parse_check_passed=True,
                    parse_report={"cover_letter_text": "Dear Hiring Team."},
                )
            )
        session.add(
            Score(job_id=job.id, profile_version=1, rubric_version=1, final=95.0)
        )
        # Seeded-and-blank: matches the question, cannot answer it.
        session.add(
            AnswerBank(
                question_pattern=r"(?i)\b(working rights|right to work)\b",
                match_type=MatchType.REGEX,
                answer_value="",
                answer_type=AnswerType.TEXT,
            )
        )
        session.flush()
        job_id = job.id

    yield job_id


def steps_with_rights() -> list[list[FormField]]:
    return [
        [
            FormField(identifier="name", label="Full name"),
            FormField(identifier="rights", label=RIGHTS_QUESTION, choices=["Yes", "No"]),
            FormField(identifier="resume", label="Resume", kind="file"),
        ]
    ]


def _apply_once(job_id: int) -> tuple[Any, FakeAdapter]:
    """One apply attempt against the real database, no browser."""
    from backend.apply import flow

    adapter = FakeAdapter(steps=steps_with_rights())
    with session_scope() as session:
        result = flow.run_apply(
            FakePage(),
            session,
            session.get(Job, job_id),
            adapter=adapter,
            is_authenticated=lambda platform: True,
        )
    return result, adapter


# =========================================================================
# The loop
# =========================================================================


def test_the_full_answer_bank_loop_closes(parked_job, sent):
    """abstain -> escalate -> answer stored -> job re-queued -> resolves.

    The assertion that matters is the last one. Every earlier step passed before
    this change too, in the sense that nothing raised; what did not happen was
    the question ever reaching the user, and what could not happen was the same
    question resolving on the retry.
    """
    job_id = parked_job

    # 1 — abstain. The question is unanswerable, so the job parks.
    result, adapter = _apply_once(job_id)
    assert result.outcome is ApplyOutcome.ABSTAINED, result.failure_reason
    assert adapter.submitted is False

    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job.status == JobStatus.NEEDS_ANSWER
        assert job.needs_answer_question, "the parked question must be recorded on the job"

    # 2 — escalate. This is the half that was never called.
    apply_run._escalate_parked([(job_id, result.needs_answer, result.needs_answer_choices)])
    assert len(sent) == 1, "the user was never asked"
    message = sent[0]
    assert "Question needed" in message
    assert f"/answer {job_id}" in message
    assert "Yes / No" in message, "the form's own options must reach the user"

    # 3 — the user answers. 4 — the job is re-queued.
    reply = telegram.handle_command(f"/answer {job_id} Yes")
    assert "Job re-queued." in reply, reply

    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job.status == JobStatus.DOCUMENTS_READY
        assert job.needs_answer_question is None, "a re-queued job must not stay parked"

        rows = session.exec(select(AnswerBank)).all()
        assert len(rows) == 1, (
            "the answer must fill the row that matched, not add a rival to it: "
            f"{[(r.question_pattern, r.answer_value) for r in rows]}"
        )
        assert rows[0].answer_value == "Yes"
        assert rows[0].verified_at is not None

    # 5 — resolves. The retry answers the question instead of re-parking.
    result, adapter = _apply_once(job_id)
    assert result.outcome is not ApplyOutcome.ABSTAINED, (
        f"the job re-parked on a question it was just told the answer to: "
        f"{result.failure_reason}"
    )
    assert adapter.filled.get("rights") == "Yes"


def test_a_second_parked_job_does_not_steal_the_first_ones_answer(parked_job, sent):
    """Two jobs parked at once must each get their own answer.

    The old ``/answer`` picked the oldest blank row in the bank regardless of
    which job was being answered, so with two jobs parked the reply was filed
    against the wrong question — leaving the real one unresolved and
    overwriting an unrelated row.
    """
    job_id = parked_job

    with session_scope() as session:
        first = session.get(Job, job_id)
        second = Job(
            source="seek",
            source_job_id="loop-2",
            url="https://example.com/loop-2",
            title="Analyst",
            company="Beta",
            dedupe_hash="loop-h2",
            campaign_id=first.campaign_id,
            status=JobStatus.NEEDS_ANSWER,
            needs_answer_question="do you hold a forklift licence",
        )
        session.add(second)
        session.flush()
        second_id = second.id

        session.add(
            AnswerBank(
                question_pattern="do you hold a forklift licence",
                match_type=MatchType.FUZZY,
                answer_value="",
                answer_type=AnswerType.TEXT,
            )
        )

    _apply_once(job_id)
    telegram.handle_command(f"/answer {second_id} No")

    with session_scope() as session:
        by_pattern = {
            row.question_pattern: row.answer_value
            for row in session.exec(select(AnswerBank)).all()
        }
        assert by_pattern["do you hold a forklift licence"] == "No"
        assert by_pattern[r"(?i)\b(working rights|right to work)\b"] == "", (
            "answering the forklift job wrote into the working-rights row"
        )

        assert session.get(Job, job_id).status == JobStatus.NEEDS_ANSWER
        assert session.get(Job, second_id).status == JobStatus.DOCUMENTS_READY


def test_a_failed_send_leaves_the_job_parked_and_says_so(parked_job, monkeypatch, caplog):
    """A Telegram outage must not look like a delivered question."""
    monkeypatch.setattr(telegram, "send_message", lambda *a, **k: False)

    result, _ = _apply_once(parked_job)
    apply_run._escalate_parked([(parked_job, result.needs_answer, [])])

    with session_scope() as session:
        assert session.get(Job, parked_job).status == JobStatus.NEEDS_ANSWER

    assert "escalation_not_delivered" in caplog.text


def test_an_escalation_that_raises_does_not_end_the_pass(parked_job, monkeypatch, caplog):
    """One unreachable job must not stop the others being asked about."""

    def explode(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("telegram is down")

    monkeypatch.setattr(telegram, "escalate_question", explode)
    apply_run._escalate_parked([(parked_job, "some question", [])])

    assert "escalation_failed" in caplog.text
