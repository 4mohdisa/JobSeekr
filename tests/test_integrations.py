"""Integrations, offline. The matching tests carry the most weight.

Attaching an email to the wrong application writes "rejected" onto a live
application or "interview" onto a dead one, and every number on the analytics
page is then built on that. So the tests that assert the matcher REFUSES are
the important ones.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from backend.config import settings
from backend.integrations import inbound, outbound, telegram
from backend.integrations.matching import (
    MATCH_THRESHOLD,
    InboundEmail,
    extract_references,
    match_email,
)
from backend.integrations.notify import Priority, notify, set_sender
from backend.integrations.scheduler import SCHEDULE, describe_schedule
from backend.models import (
    AnswerBank,
    AnswerType,
    Application,
    ApplicationOutcome,
    Campaign,
    Document,
    DocumentKind,
    GrayZoneAction,
    Job,
    JobStatus,
    MatchType,
    Profile,
    ResponseStatus,
)

APPLIED_AT = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


def email(**kwargs) -> InboundEmail:
    base = {
        "message_id": "m1",
        "subject": "Your application",
        "from_address": "no-reply@pageuppeople.com",
        "body": "",
        "received_at": APPLIED_AT + timedelta(days=3),
    }
    base.update(kwargs)
    return InboundEmail(**base)


class FakeApplication:
    def __init__(self, id_: int, job_id: int, platform: str = "seek"):
        self.id = id_
        self.job_id = job_id
        self.platform = platform
        self.applied_at = APPLIED_AT


class FakeJob:
    def __init__(self, id_: int, title: str, company: str, source_job_id: str = "", contact=""):
        self.id = id_
        self.title = title
        self.company = company
        self.source_job_id = source_job_id
        self.ad_contact_email = contact


# =========================================================================
# Matching — the part that must refuse
# =========================================================================


def test_an_ats_sender_does_not_prevent_a_match():
    """The whole problem: rejections come from the ATS, not the employer."""
    applications = [FakeApplication(1, 1)]
    jobs = {1: FakeJob(1, "Data Analyst", "Wattle Group")}

    result = match_email(
        email(
            subject="Your application for Data Analyst at Wattle Group",
            from_address="no-reply@pageuppeople.com",
        ),
        applications,
        jobs,
    )
    assert result is not None
    assert result.application_id == 1


def test_a_generic_subject_alone_is_not_enough_to_match():
    """A title coincidence must not attach to whichever application is newest."""
    applications = [FakeApplication(1, 1)]
    jobs = {1: FakeJob(1, "Software Engineer", "Wattle Group")}

    result = match_email(
        email(subject="Software Engineer", from_address="jobs@random-newsletter.com"),
        applications,
        jobs,
    )
    assert result is None


def test_two_similar_applications_that_cannot_be_separated_abstain():
    """Same title at two employers, neither named — better unmatched."""
    applications = [FakeApplication(1, 1), FakeApplication(2, 2)]
    jobs = {
        1: FakeJob(1, "Data Analyst", "Wattle Group"),
        2: FakeJob(2, "Data Analyst", "Redgum Analytics"),
    }

    result = match_email(
        email(subject="Update on your Data Analyst application"), applications, jobs
    )
    assert result is None


def test_naming_the_employer_separates_them():
    applications = [FakeApplication(1, 1), FakeApplication(2, 2)]
    jobs = {
        1: FakeJob(1, "Data Analyst", "Wattle Group"),
        2: FakeJob(2, "Data Analyst", "Redgum Analytics"),
    }

    result = match_email(
        email(subject="Your Data Analyst application at Wattle Group"), applications, jobs
    )
    assert result is not None
    assert result.application_id == 1


def test_an_email_predating_the_application_never_matches():
    applications = [FakeApplication(1, 1)]
    jobs = {1: FakeJob(1, "Data Analyst", "Wattle Group")}

    result = match_email(
        email(
            subject="Data Analyst at Wattle Group",
            received_at=APPLIED_AT - timedelta(days=1),
        ),
        applications,
        jobs,
    )
    assert result is None


def test_a_very_old_email_does_not_match():
    applications = [FakeApplication(1, 1)]
    jobs = {1: FakeJob(1, "Data Analyst", "Wattle Group")}

    result = match_email(
        email(
            subject="Data Analyst at Wattle Group",
            received_at=APPLIED_AT + timedelta(days=200),
        ),
        applications,
        jobs,
    )
    assert result is None


def test_the_contact_address_published_in_the_ad_is_a_strong_signal():
    applications = [FakeApplication(1, 1)]
    jobs = {1: FakeJob(1, "Data Analyst", "Wattle Group", contact="careers@wattle.com.au")}

    result = match_email(
        email(subject="Re: your application", from_address="careers@wattle.com.au"),
        applications,
        jobs,
    )
    assert result is not None
    assert any("contact address" in signal for signal in result.signals)


def test_the_source_job_id_matches_strongly():
    applications = [FakeApplication(1, 1)]
    jobs = {1: FakeJob(1, "Data Analyst", "Wattle Group", source_job_id="84213977")}

    result = match_email(
        email(subject="Application received", body="Reference: 84213977"), applications, jobs
    )
    assert result is not None
    assert result.score >= MATCH_THRESHOLD


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Reference: ABC-12345", "ABC-12345"),
        ("Job ID: 4471829", "4471829"),
        ("your requisition REQ-99812 has been received", "REQ-99812"),
    ],
)
def test_reference_extraction(text, expected):
    assert expected in extract_references(text)


# =========================================================================
# Classification
# =========================================================================


def test_classification_failure_degrades_to_irrelevant(monkeypatch):
    """One unparseable email must not stop the sweep."""

    def boom(*args, **kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(inbound.llm, "complete_json", boom)
    result = inbound.classify_email(email())
    assert result["category"] == "irrelevant"


def test_classification_maps_onto_response_statuses():
    """Every category the model may return has a home, except 'irrelevant'."""
    categories = set(
        inbound.CLASSIFICATION_SCHEMA["properties"]["category"]["enum"]
    ) - {"irrelevant"}
    assert categories == set(inbound.CATEGORY_TO_STATUS)


# =========================================================================
# Outbound — the legal boundary
# =========================================================================


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Profile(version=1, identity={"name": "Jordan", "email": "j@example.com"}))
        campaign = Campaign(
            id=1,
            name="c",
            search_terms=["dev"],
            locations=["Adelaide SA"],
            score_floor=60,
            score_auto_apply=80,
            gray_zone_action=GrayZoneAction.QUEUE,
        )
        s.add(campaign)
        s.flush()
        s.add(
            Job(
                id=1,
                source="seek",
                source_job_id="1",
                url="u",
                title="Developer",
                company="Acme",
                dedupe_hash="h1",
                campaign_id=1,
                ad_contact_email=None,  # no published address
            )
        )
        s.add(
            Job(
                id=2,
                source="seek",
                source_job_id="2",
                url="u2",
                title="Developer",
                company="Wattle",
                dedupe_hash="h2",
                campaign_id=1,
                ad_contact_email="careers@wattle.com.au",
            )
        )
        s.commit()
        yield s


def test_no_published_address_means_no_draft(session):
    """Addresses are never guessed. This is a legal constraint, not a policy."""
    with pytest.raises(outbound.OutboundRefused, match="published no contact address"):
        outbound.draft_for_job(session, 1)


def test_a_job_with_no_gated_documents_is_refused(session):
    with pytest.raises(outbound.OutboundRefused, match="parse gate"):
        outbound.draft_for_job(session, 2)


def test_sending_without_an_approval_token_is_refused():
    draft = outbound.OutboundDraft(
        job_id=1, to_address="careers@wattle.com.au", subject="s", body="b"
    )
    with pytest.raises(outbound.OutboundRefused, match="no auto-send"):
        outbound.send_draft(draft, approved_by="")


def test_the_outbound_api_has_no_recipient_parameter():
    """A recipient argument is how a draft-only path becomes a mail merge."""
    import inspect

    signature = inspect.signature(outbound.draft_for_job)
    for forbidden in ("to", "to_address", "recipient", "recipients", "email"):
        assert forbidden not in signature.parameters


def test_there_is_no_followup_or_bulk_path():
    import pathlib

    source = pathlib.Path(outbound.__file__).read_text(encoding="utf-8")
    for forbidden in ("def send_bulk", "def schedule_followup", "def follow_up", "def harvest"):
        assert forbidden not in source


# =========================================================================
# Telegram
# =========================================================================


def test_stop_command_writes_the_file_the_guardrails_read(session):
    reply = telegram.handle_command("/stop")
    assert "STOPPED" in reply
    assert settings.stop_file.exists()

    telegram.handle_command("/resume")
    assert not settings.stop_file.exists()


def test_stopping_one_campaign_leaves_the_others_running(session, monkeypatch):
    monkeypatch.setattr(telegram, "session_scope", lambda: _Scope(session))

    reply = telegram.handle_command("/stop c")
    assert "paused" in reply
    assert session.get(Campaign, 1).active is False
    assert not settings.stop_file.exists(), "a campaign stop is not a global stop"


def test_an_unknown_command_lists_the_real_ones(session):
    assert "/status" in telegram.handle_command("/nonsense")


def test_a_failing_command_reports_rather_than_crashing(session, monkeypatch):
    monkeypatch.setattr(
        telegram, "_cmd_status", lambda _: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    telegram.COMMANDS["/status"] = telegram._cmd_status
    assert "boom" in telegram.handle_command("/status")


def test_saving_an_answer_fills_a_blank_row_rather_than_duplicating(session, monkeypatch):
    monkeypatch.setattr(telegram, "session_scope", lambda: _Scope(session))
    session.add(
        AnswerBank(
            id=1,
            question_pattern="What is your notice period?",
            match_type=MatchType.FUZZY,
            answer_value="",
            answer_type=AnswerType.TEXT,
        )
    )
    session.commit()

    row_id = telegram.save_answer("What is your notice period?", "4 weeks")

    assert row_id == 1
    rows = session.exec(select(AnswerBank)).all()
    assert len(rows) == 1, "an answered question must not create a second row"
    assert rows[0].answer_value == "4 weeks"
    assert rows[0].verified_at is not None


def test_requeue_moves_a_parked_job_back_into_line(session, monkeypatch):
    monkeypatch.setattr(telegram, "session_scope", lambda: _Scope(session))
    job = session.get(Job, 1)
    job.status = JobStatus.NEEDS_ANSWER
    session.add(job)
    session.commit()

    assert telegram.requeue_job(1) is True
    assert session.get(Job, 1).status == JobStatus.DOCUMENTS_READY


class _Scope:
    """Hand the module the test's session instead of opening a real one."""

    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, *args):
        self.session.commit()
        return False


# =========================================================================
# Notifications
# =========================================================================


def test_notify_without_a_sender_logs_instead_of_raising():
    set_sender(None)
    notify("Something happened", "detail", Priority.IMMEDIATE)  # must not raise


def test_a_failing_sender_does_not_propagate():
    def boom(_message, _priority):
        raise RuntimeError("telegram down")

    set_sender(boom)
    try:
        notify("Interview request", "urgent", Priority.IMMEDIATE)  # must not raise
    finally:
        set_sender(None)


def test_hooks_wire_the_safety_layers_without_importing_telegram():
    from backend.apply import canary, guardrails
    from backend.apply import session as browser_session
    from backend.integrations.notify import register_hooks

    register_hooks()
    assert guardrails.on_notify is not None
    assert browser_session.on_session_expired is not None
    assert canary.on_drift is not None


# =========================================================================
# Scheduler
# =========================================================================


def test_every_scheduled_job_the_spec_asks_for_exists():
    ids = {entry["id"] for entry in SCHEDULE}
    assert {"discovery", "scoring", "inbound", "ghosting", "backup", "digest"} <= ids
    assert len([i for i in ids if i.startswith("apply_")]) == 2, "two apply passes daily"


def test_apply_passes_are_jittered():
    """A fixed daily time is a machine signature no per-submit pacing hides."""
    jittered = {entry["id"] for entry in describe_schedule() if entry["jittered"]}
    assert jittered == {"apply_morning", "apply_afternoon"}


def test_discovery_runs_every_four_hours():
    discovery = next(e for e in SCHEDULE if e["id"] == "discovery")
    assert discovery["trigger"] == "interval"
    assert discovery["hours"] == 4


def test_the_scheduled_apply_pass_follows_the_master_switch(monkeypatch):
    """With the switch off, the scheduled pass must be a dry run."""
    import backend.integrations.scheduler as scheduler_module

    captured = {}

    def fake_pass(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(settings, "allow_live_submit", False)
    monkeypatch.setitem(
        __import__("sys").modules, "backend.apply.run", type("M", (), {"run_apply_pass": fake_pass})
    )
    scheduler_module._apply_job()
    assert captured["dry_run"] is True


# =========================================================================
# Ghosting
# =========================================================================


def test_ghosting_marks_only_old_silent_applications(session, monkeypatch):
    now = datetime.now(UTC)
    session.add(
        Document(
            job_id=1,
            kind=DocumentKind.RESUME,
            path="x",
            sha256="s",
            parse_check_passed=True,
        )
    )
    session.add(
        Application(
            id=1,
            job_id=1,
            outcome=ApplicationOutcome.SUBMITTED,
            applied_at=now - timedelta(days=45),
            response_status=ResponseStatus.NONE,
        )
    )
    session.add(
        Application(
            id=2,
            job_id=2,
            outcome=ApplicationOutcome.SUBMITTED,
            applied_at=now - timedelta(days=5),
            response_status=ResponseStatus.NONE,
        )
    )
    session.commit()

    changed = inbound.sweep_ghosted(days=30, session_factory=lambda: _Scope(session))

    assert changed == 1
    assert session.get(Application, 1).response_status == ResponseStatus.GHOSTED
    assert session.get(Application, 2).response_status == ResponseStatus.NONE


def test_ghosting_leaves_answered_applications_alone(session):
    session.add(
        Application(
            id=3,
            job_id=1,
            outcome=ApplicationOutcome.SUBMITTED,
            applied_at=datetime.now(UTC) - timedelta(days=90),
            response_status=ResponseStatus.REJECTED,
        )
    )
    session.commit()

    inbound.sweep_ghosted(days=30, session_factory=lambda: _Scope(session))
    assert session.get(Application, 3).response_status == ResponseStatus.REJECTED
