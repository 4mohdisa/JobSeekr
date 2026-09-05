"""Outbound follow-up, wired — with every guard still standing.

This is the one action in the project whose blast radius is someone else's
inbox, and the module is defensible under the Spam Act only because of three
properties. Wiring it means each of those has to survive being reachable:

1. the address came from the ad — no recipient parameter, anywhere
2. a human approves every message — no auto-send, no scheduled caller
3. no follow-ups — one message per job, ever

Plus the two rules the rest of the project already has: nothing ungated is
attached, and the whole thing is off until the user turns it on.
"""

from __future__ import annotations

import inspect
import pathlib

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from backend.config import settings
from backend.integrations import outbound
from backend.models import (
    Document,
    DocumentKind,
    Job,
    JobStatus,
    OutboundMessage,
    OutboundStatus,
    Profile,
)


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        # A profile, because draft_for_job checks for one BEFORE it checks the
        # parse gate. Without it the gate test passes on "no profile to write
        # from" and never reaches the guard it is named for.
        s.add(
            Profile(
                version=1,
                identity={"name": "Jordan", "email": "jordan@example.com"},
            )
        )
        job = Job(
            id=1,
            source="seek",
            source_job_id="1",
            url="https://example.com/1",
            title="Data Analyst",
            company="Acme",
            location="Adelaide SA",
            dedupe_hash="h",
            status=JobStatus.APPLIED,
            ad_contact_email="hiring@acme.example",
        )
        s.add(job)
        s.flush()
        resume = tmp_path / "combined.pdf"
        resume.write_bytes(b"%PDF-1.4 fake")
        s.add(
            Document(
                job_id=1,
                kind=DocumentKind.COMBINED,
                path=str(resume),
                sha256="d",
                parse_check_passed=True,
            )
        )
        s.flush()
        yield s


def draft(job_id: int = 1, **overrides) -> outbound.OutboundDraft:
    from pathlib import Path

    values = {
        "job_id": job_id,
        "to_address": "hiring@acme.example",
        "subject": "Following up",
        "body": "Hello.",
        "attachments": [Path("combined.pdf")],
    }
    values.update(overrides)
    return outbound.OutboundDraft(**values)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "outbound_enabled", True)
    monkeypatch.setattr(settings, "gmail_address", "me@example.com")
    monkeypatch.setattr(settings, "gmail_app_password", "app-password")


# =========================================================================
# Property 1 — the address comes from the ad
# =========================================================================


def test_no_function_here_takes_a_recipient():
    """The property, enforced structurally rather than by reading carefully.

    A recipient parameter is not a feature to add cautiously; it is the thing
    that turns this from a reply into a mail-out.
    """
    for name, function in inspect.getmembers(outbound, inspect.isfunction):
        params = set(inspect.signature(function).parameters)
        for forbidden in ("to", "to_address", "recipient", "recipients", "email"):
            assert forbidden not in params, f"{name} takes {forbidden}"


def test_the_edit_endpoint_has_no_recipient_field():
    """Editing a draft must not become the recipient parameter by the back door."""
    from backend.api.schemas import OutboundEditIn

    assert set(OutboundEditIn.model_fields) == {"subject", "body"}


def test_a_job_whose_ad_published_no_address_gets_no_draft(session):
    job = session.get(Job, 1)
    job.ad_contact_email = None
    session.flush()

    with pytest.raises(outbound.OutboundRefused, match="published no contact address"):
        outbound.draft_for_job(session, 1)


def test_sending_uses_the_stored_address_not_a_supplied_one(
    session, enabled, monkeypatch
):
    """approve_and_send rebuilds the draft from the row.

    So the address that gets used is the one recorded when the ad was read,
    never something an API request handed over.
    """
    sent: list[outbound.OutboundDraft] = []
    monkeypatch.setattr(
        outbound, "send_draft", lambda d, *, approved_by: sent.append(d) or True
    )

    row = outbound.record_draft(session, draft())
    session.flush()
    outbound.approve_and_send(session, row.id, approved_by="me")

    assert sent[0].to_address == "hiring@acme.example"


# =========================================================================
# Property 2 — a human approves every message
# =========================================================================


def test_sending_without_an_approval_token_is_refused(enabled):
    with pytest.raises(outbound.OutboundRefused, match="no auto-send"):
        outbound.send_draft(draft(), approved_by="")


def test_nothing_schedules_a_send():
    """A scheduled caller would be an auto-send with extra steps."""
    source = pathlib.Path("backend/integrations/scheduler.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("send_draft", "approve_and_send", "outbound"):
        assert forbidden not in source, f"the scheduler references {forbidden}"


def test_the_apply_flow_drafts_but_never_sends():
    """The flow may write a draft. It must not be able to send one."""
    source = pathlib.Path("backend/apply/flow.py").read_text(encoding="utf-8")
    assert "record_draft" in source, "the flow should draft after a confirmed submit"
    for forbidden in ("send_draft", "approve_and_send"):
        assert forbidden not in source, f"flow.py references {forbidden}"


def test_the_telegram_notification_cannot_send(monkeypatch):
    """Send / Skip / Edit happens in the UI, not from a message.

    A message that could send an email with one tap is a message one mistap
    sends an email from.
    """
    from backend.integrations import telegram

    source = inspect.getsource(telegram.notify_followup_draft)
    for forbidden in ("send_draft", "approve_and_send", "smtplib"):
        assert forbidden not in source


# =========================================================================
# Property 3 — one message per job, ever
# =========================================================================


def test_a_second_draft_for_the_same_job_returns_the_first(session):
    """The rule was documented and not enforced — nothing recorded what was
    sent, so nothing could refuse a second."""
    first = outbound.record_draft(session, draft())
    session.flush()
    second = outbound.record_draft(session, draft(subject="Different"))
    session.flush()

    assert second.id == first.id
    assert second.subject == "Following up", "the existing row must not be rewritten"
    assert len(session.exec(select(OutboundMessage)).all()) == 1


def test_a_sent_job_cannot_be_drafted_again(session, enabled, monkeypatch):
    monkeypatch.setattr(outbound, "send_draft", lambda d, *, approved_by: True)

    row = outbound.record_draft(session, draft())
    session.flush()
    outbound.approve_and_send(session, row.id, approved_by="me")
    session.flush()

    again = outbound.record_draft(session, draft())
    assert again.status is OutboundStatus.SENT


def test_a_skipped_job_cannot_be_drafted_again(session):
    """Declining is a decision. Re-offering it next week would overturn it."""
    row = outbound.record_draft(session, draft())
    session.flush()
    outbound.skip_message(session, row.id)
    session.flush()

    again = outbound.record_draft(session, draft())
    assert again.status is OutboundStatus.SKIPPED


def test_sending_twice_is_refused(session, enabled, monkeypatch):
    monkeypatch.setattr(outbound, "send_draft", lambda d, *, approved_by: True)

    row = outbound.record_draft(session, draft())
    session.flush()
    outbound.approve_and_send(session, row.id, approved_by="me")
    session.flush()

    with pytest.raises(outbound.OutboundRefused, match="one message per job"):
        outbound.approve_and_send(session, row.id, approved_by="me")


def test_a_transport_failure_leaves_the_draft_sendable(session, enabled, monkeypatch):
    """A failed send is not a decision.

    Marking it SENT would consume the job's one slot with nothing having
    arrived — the worst of both rules.
    """
    monkeypatch.setattr(outbound, "send_draft", lambda d, *, approved_by: False)

    row = outbound.record_draft(session, draft())
    session.flush()

    assert outbound.approve_and_send(session, row.id, approved_by="me") is False
    assert session.get(OutboundMessage, row.id).status is OutboundStatus.DRAFTED


# =========================================================================
# The switch
# =========================================================================


def test_sending_is_refused_while_the_switch_is_off(monkeypatch):
    monkeypatch.setattr(settings, "outbound_enabled", False)
    monkeypatch.setattr(settings, "gmail_address", "me@example.com")
    monkeypatch.setattr(settings, "gmail_app_password", "pw")

    with pytest.raises(outbound.OutboundRefused, match="OUTBOUND_ENABLED"):
        outbound.send_draft(draft(), approved_by="me")


def test_the_switch_defaults_off():
    """Built and off, same as ALLOW_LIVE_SUBMIT."""
    from backend.config import Settings

    assert Settings.model_fields["outbound_enabled"].default is False


def test_the_switch_is_checked_inside_send_not_at_the_call_site():
    """One place that can send, one place that can be off.

    An approval given before the feature was enabled must not become a send
    afterwards, which is what a call-site check would allow.
    """
    source = inspect.getsource(outbound.send_draft)
    assert "outbound_enabled" in source


def test_drafting_still_works_while_the_switch_is_off(session, monkeypatch):
    """Off means nothing sends, not that nothing is prepared.

    Being able to read the drafts is how someone decides whether to turn it on.
    """
    monkeypatch.setattr(settings, "outbound_enabled", False)

    row = outbound.record_draft(session, draft())
    session.flush()
    assert row.status is OutboundStatus.DRAFTED


# =========================================================================
# Nothing ungated is attached
# =========================================================================


def test_a_job_with_no_gated_document_gets_no_draft(session):
    for document in session.exec(select(Document)).all():
        document.parse_check_passed = False
    session.flush()

    with pytest.raises(outbound.OutboundRefused, match="parse gate"):
        outbound.draft_for_job(session, 1)


def test_the_gate_is_rechecked_at_send_not_trusted_from_drafting(session):
    """A document can be rebuilt between drafting and approval.

    Hard rule 3 is about what is attached at send time, not what was attached
    when someone first looked at the draft.
    """
    row = outbound.record_draft(session, draft())
    session.flush()

    for document in session.exec(select(Document)).all():
        document.parse_check_passed = False
    session.flush()

    paths = outbound._attachment_paths(session, row.job_id, row.attachments)
    assert paths == [], "an ungated document must not be resolved for sending"


def test_a_missing_attachment_refuses_the_send(enabled):
    from pathlib import Path

    with pytest.raises(outbound.OutboundRefused, match="attachment missing"):
        outbound.send_draft(
            draft(attachments=[Path("/nonexistent/x.pdf")]), approved_by="me"
        )
