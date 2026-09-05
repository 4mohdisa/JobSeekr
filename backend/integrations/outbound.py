"""Draft an email to an address the advertiser published. Send only on approval.

LEGAL BOUNDARY — read before changing anything here.

The Australian Spam Act 2003 prohibits unsolicited commercial electronic
messages, and separately prohibits address-harvesting software and
harvested-address lists. This module is defensible only because of three
properties, and every one of them is load-bearing:

1. **The address came from the ad itself.** ``ad_contact_email`` is populated by
   ``backend.discovery.contacts``, which reads only what the advertiser
   published in their own listing. This module refuses to send anywhere else —
   it does not accept a recipient argument.
2. **A human approves every message.** ``draft_for_job`` produces a draft;
   ``send_draft`` requires an ``approved_by`` token that only arrives from an
   explicit user action. There is no auto-send path and no scheduled retry.
3. **No follow-ups.** One message per job, ever. No chasing, no sequences, no
   "just bumping this to the top of your inbox".

Do not add a recipient parameter, a bulk mode, a follow-up scheduler, or a
harvester. Those are not features to implement carefully; they are outside what
this project is allowed to do.
"""

from __future__ import annotations

import smtplib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from backend.config import settings
from backend.logging_setup import get_logger
from backend.models import Campaign, Document, Job, Profile, Template, TemplateKind

log = get_logger(__name__)

__all__ = [
    "OutboundDraft",
    "approve_and_send",
    "draft_for_job",
    "record_draft",
    "send_draft",
    "skip_message",
]


class OutboundRefused(RuntimeError):
    """A send that must not happen."""


@dataclass
class OutboundDraft:
    """A message the user may choose to send. Not sent until they say so."""

    job_id: int
    to_address: str
    subject: str
    body: str
    attachments: list[Path] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def preview(self) -> str:
        files = ", ".join(path.name for path in self.attachments) or "none"
        return (
            f"To: {self.to_address}\nSubject: {self.subject}\n"
            f"Attachments: {files}\n\n{self.body}"
        )


def draft_for_job(session: Session, job_id: int) -> OutboundDraft:
    """Build the draft for one job. Raises when the preconditions are not met."""
    job = session.get(Job, job_id)
    if job is None:
        raise OutboundRefused(f"no job {job_id}")

    # Property 1: the address must have come from the ad.
    if not job.ad_contact_email:
        raise OutboundRefused(
            f"job {job_id} published no contact address in its ad. "
            "Addresses are never guessed or looked up — see the module docstring."
        )

    profile = session.exec(select(Profile).order_by(Profile.version.desc())).first()  # type: ignore[union-attr]
    if profile is None:
        raise OutboundRefused("no profile to write from")

    campaign = session.get(Campaign, job.campaign_id) if job.campaign_id else None

    documents = [
        document
        for document in session.exec(
            select(Document).where(Document.job_id == job_id)
        ).all()
        if document.parse_check_passed
    ]
    if not documents:
        # Same rule as the apply path: nothing ungated is ever attached.
        raise OutboundRefused(
            f"job {job_id} has no documents that passed the parse gate; refusing to attach"
        )

    body_template, _ = _email_template(session, campaign)

    from backend.documents.build import _job_context, _profile_context, _today_context
    from backend.documents.engine import render_string

    context = {
        "profile": _profile_context(profile),
        "job": _job_context(job),
        "campaign": {"name": campaign.name if campaign else ""},
        "today": _today_context(),
        "ai": _ai_context(session, job, profile),
    }

    # The email template is plain text: render with escaping off.
    rendered = render_string(body_template, context, latex=False)
    subject, _, body = rendered.partition("\n")
    subject = subject.removeprefix("Subject:").strip()

    attachments = [
        Path(d.path) for d in documents if d.kind.value in {"resume", "cover_letter"}
    ]
    if not attachments:
        attachments = [Path(documents[0].path)]

    return OutboundDraft(
        job_id=job_id,
        to_address=job.ad_contact_email,
        subject=subject or f"Application — {job.title}",
        body=body.strip(),
        attachments=attachments,
    )


def _email_template(session: Session, campaign: Campaign | None) -> tuple[str, int]:
    if campaign is not None:
        chosen = (campaign.template_ids or {}).get("email")
        if chosen:
            row = session.get(Template, int(chosen))
            if row is not None:
                return row.body, row.version

    row = session.exec(
        select(Template).where(
            Template.kind == TemplateKind.EMAIL,
            Template.is_default == True,
        )
    ).first()
    if row is not None:
        return row.body, row.version

    from backend.documents.engine import template_root

    return (template_root() / "email.txt.j2").read_text(encoding="utf-8"), 0


def _ai_context(session: Session, job: Job, profile: Profile) -> dict[str, Any]:
    """Reuse the cover letter's generated passages rather than paying twice.

    The letter for this job has already been generated and validated against
    the profile; generating a second set for the email would cost another call
    and produce prose the parse gate never saw.
    """
    letter = session.exec(
        select(Document).where(
            Document.job_id == job.id, Document.kind == "cover_letter"
        )
    ).first()
    report = (letter.parse_report or {}) if letter else {}
    slots = report.get("ai_slots") or {}
    if slots:
        return slots

    from backend.documents.build import generate_ai_slots
    from backend.documents.engine import SLOT_SPECS
    from backend.documents.fabrication import profile_fact_index

    wanted = [SLOT_SPECS[name] for name in ("opening_hook", "skills_bridge", "closing")]
    generated, violations = generate_ai_slots(
        wanted,
        profile=profile,
        job=job,
        profile_text=profile_fact_index(profile)[:6000],
    )
    if violations:
        raise OutboundRefused(
            "generated email text asserted unsupported facts: "
            + "; ".join(str(v) for v in violations[:3])
        )
    return generated


def send_draft(draft: OutboundDraft, *, approved_by: str) -> bool:
    """Send a draft the user approved. Refuses without an approval token.

    ``approved_by`` is not decoration: it is the only way into this function,
    and it is set exclusively where a human pressed something. There is no
    scheduled caller and no retry.
    """
    if not approved_by:
        raise OutboundRefused(
            "send_draft requires an explicit approval; there is no auto-send"
        )

    # The master switch, checked here rather than at the call site so there is
    # exactly one place that can send and exactly one place that can be off.
    # Same shape as ALLOW_LIVE_SUBMIT and for the same reason: an approval
    # given before the feature was enabled must not become a send afterwards.
    if not settings.outbound_enabled:
        raise OutboundRefused(
            "OUTBOUND_ENABLED is false — this system cannot send email until "
            "you turn it on"
        )

    if not settings.gmail_address or not settings.gmail_app_password:
        raise OutboundRefused(
            "GMAIL_ADDRESS and GMAIL_APP_PASSWORD are required to send"
        )

    message = EmailMessage()
    message["From"] = settings.gmail_address
    message["To"] = draft.to_address
    message["Subject"] = draft.subject
    message.set_content(draft.body)

    for path in draft.attachments:
        if not path.exists():
            raise OutboundRefused(f"attachment missing on disk: {path}")
        message.add_attachment(
            path.read_bytes(),
            maintype="application",
            subtype="pdf",
            filename=path.name,
        )

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(settings.gmail_address, settings.gmail_app_password)
            smtp.send_message(message)
    except Exception as exc:
        log.exception(
            "outbound_send_failed",
            job_id=draft.job_id,
            to=draft.to_address,
            error=str(exc)[:200],
        )
        return False

    log.info(
        "outbound_sent",
        job_id=draft.job_id,
        to=draft.to_address,
        subject=draft.subject,
        approved_by=approved_by,
        attachments=[p.name for p in draft.attachments],
    )
    return True


# --------------------------------------------------------------------------
# One message per job, enforced
# --------------------------------------------------------------------------


def record_draft(session: Session, draft: OutboundDraft) -> Any:
    """Store a draft, or return the existing row for this job.

    "One message per job, ever" was one of the three properties this module
    rests on and the only one that was documented rather than enforced —
    nothing recorded what had been sent, so nothing could refuse a second.
    UNIQUE(job_id) is the enforcement, mirroring UNIQUE(job_id) on applications.

    Returns the existing row unchanged when there is one, whatever its status.
    A SENT job must not be re-drafted, and neither must a SKIPPED one: declining
    to write to an employer is a decision, and re-offering the draft next week
    would quietly overturn it.
    """
    from backend.models import OutboundMessage, OutboundStatus

    existing = session.exec(
        select(OutboundMessage).where(OutboundMessage.job_id == draft.job_id)
    ).first()
    if existing is not None:
        log.info(
            "outbound_already_exists",
            job_id=draft.job_id,
            status=existing.status.value,
            note="one message per job; not re-drafting",
        )
        return existing

    row = OutboundMessage(
        job_id=draft.job_id,
        to_address=draft.to_address,
        subject=draft.subject,
        body=draft.body,
        attachments=[path.name for path in draft.attachments],
        status=OutboundStatus.DRAFTED,
    )
    session.add(row)
    log.info("outbound_drafted", job_id=draft.job_id, to=draft.to_address)
    return row


def approve_and_send(session: Session, message_id: int, *, approved_by: str) -> bool:
    """Send a stored draft. The only path from DRAFTED to SENT.

    Rebuilds the draft from the stored row rather than trusting a caller to
    hand one over, so the recipient that gets used is the one recorded when the
    ad was read — not something an API request supplied.
    """
    from backend.models import OutboundMessage, OutboundStatus

    row = session.get(OutboundMessage, message_id)
    if row is None:
        raise OutboundRefused(f"no outbound message {message_id}")
    if row.status is not OutboundStatus.DRAFTED:
        raise OutboundRefused(
            f"message {message_id} is {row.status.value}; one message per job"
        )

    documents = _attachment_paths(session, row.job_id, row.attachments)

    sent = send_draft(
        OutboundDraft(
            job_id=row.job_id,
            to_address=row.to_address,
            subject=row.subject,
            body=row.body,
            attachments=documents,
        ),
        approved_by=approved_by,
    )
    if not sent:
        # Left DRAFTED on purpose: a transport failure is not a decision, and
        # the user may retry. Marking it SENT would consume the one slot this
        # job gets without anything having arrived.
        return False

    row.status = OutboundStatus.SENT
    row.approved_by = approved_by
    row.sent_at = datetime.now(UTC)
    session.add(row)
    return True


def skip_message(session: Session, message_id: int) -> Any:
    """The user declined. Terminal, and it keeps the job's one slot."""
    from backend.models import OutboundMessage, OutboundStatus

    row = session.get(OutboundMessage, message_id)
    if row is None:
        raise OutboundRefused(f"no outbound message {message_id}")
    if row.status is OutboundStatus.SENT:
        raise OutboundRefused("that message has already been sent")

    row.status = OutboundStatus.SKIPPED
    session.add(row)
    log.info("outbound_skipped", job_id=row.job_id)
    return row


def _attachment_paths(session: Session, job_id: int, names: list[Any]) -> list[Path]:
    """Resolve stored filenames back to gated documents on disk.

    Re-checks the parse gate rather than trusting that it passed when the draft
    was written. A document can be rebuilt between drafting and approval, and
    hard rule 3 is about what is attached at send time, not what was attached
    when someone first looked at it.
    """
    wanted = {str(name) for name in names}
    paths: list[Path] = []
    for document in session.exec(
        select(Document).where(Document.job_id == job_id)
    ).all():
        if not document.parse_check_passed:
            continue
        path = Path(document.path)
        if path.name in wanted:
            paths.append(path)
    return paths
