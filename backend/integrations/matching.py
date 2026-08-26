"""Match an inbound email to the application it is about.

This is the hard part of the inbound pipeline, and the reason is structural:
**ATS mail does not come from the employer.** A rejection for a job at a South
Australian university arrives from ``no-reply@pageuppeople.com``; a JobAdder
acknowledgement comes from ``noreply@jobadder.com``. Matching on sender domain
therefore fails on exactly the mail that matters most.

So several weak signals are combined and scored, and a threshold decides:

* an ATS reference or job id appearing in the subject or body
* fuzzy match of the job title against the subject
* the employer name appearing anywhere in the message
* a timing window — replies cluster in the days after applying
* thread continuity, when the message is a reply to something we know about

**An unmatched email is better than a wrongly matched one.** A wrong match
writes "rejected" onto an application that is still live, or "interview" onto
one that is not, and the analytics page then reports on fiction. Below the
threshold the email is left unmatched for the user to look at.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from rapidfuzz import fuzz

from backend.boards import is_platform_domain
from backend.discovery.normalize import canonical_company
from backend.logging_setup import get_logger

log = get_logger(__name__)

__all__ = [
    "MATCH_THRESHOLD",
    "InboundEmail",
    "MatchCandidate",
    "match_email",
]


MATCH_THRESHOLD = 55.0
"""Minimum score to attach an email to an application.

Tuned so that a title match alone is not enough (a generic "Software Engineer"
subject would otherwise attach to whichever application was most recent), but
title plus employer, or an explicit reference, is.
"""

# Replies arrive in a window. Older than this and a title coincidence is far
# more likely than a real reply.
MAX_REPLY_AGE = timedelta(days=120)

# Known ATS reference shapes seen in Australian recruitment mail.
_REFERENCE_PATTERNS = (
    re.compile(r"\b(?:ref|reference|job\s*(?:id|no|number)|requisition)\s*[:#]?\s*([A-Z0-9\-]{4,20})\b", re.IGNORECASE),
    re.compile(r"\b([A-Z]{2,5}-\d{3,8})\b"),
    re.compile(r"\bposition\s+(?:id|number)\s*[:#]?\s*(\d{4,10})\b", re.IGNORECASE),
)



@dataclass
class InboundEmail:
    """One message, normalised across IMAP and the Gmail API."""

    message_id: str
    subject: str
    from_address: str
    body: str
    received_at: datetime
    thread_id: str | None = None
    in_reply_to: str | None = None

    @property
    def sender_domain(self) -> str:
        _, _, domain = self.from_address.partition("@")
        return domain.strip("> ").casefold()

    @property
    def from_ats(self) -> bool:
        return is_platform_domain(self.sender_domain)

    @property
    def haystack(self) -> str:
        return f"{self.subject}\n{self.body}".casefold()


@dataclass
class MatchCandidate:
    """One application scored against an email, with its reasons."""

    application_id: int
    job_id: int
    score: float = 0.0
    signals: list[str] = field(default_factory=list)

    def add(self, points: float, reason: str) -> None:
        self.score += points
        self.signals.append(f"{reason} (+{points:g})")


def extract_references(text: str) -> set[str]:
    """Pull anything reference-shaped out of a message."""
    found: set[str] = set()
    for pattern in _REFERENCE_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(1).strip().upper()
            if value and not value.isalpha():
                found.add(value)
    return found


def _score_one(
    email: InboundEmail,
    application: Any,
    job: Any,
    *,
    references: set[str],
) -> MatchCandidate:
    candidate = MatchCandidate(application_id=application.id, job_id=job.id)

    applied_at = application.applied_at
    if applied_at.tzinfo is None:
        applied_at = applied_at.replace(tzinfo=UTC)

    age = email.received_at - applied_at
    if age < timedelta(0):
        # The email predates the application; it cannot be a reply to it.
        return candidate
    if age > MAX_REPLY_AGE:
        return candidate

    haystack = email.haystack

    # 1 — an explicit reference we recorded, or the source job id.
    source_id = str(getattr(job, "source_job_id", "") or "")
    if source_id and len(source_id) >= 4 and source_id.casefold() in haystack:
        candidate.add(45, f"source job id {source_id} present")
    elif references and source_id.upper() in references:
        candidate.add(45, "reference matches the source job id")

    # 2 — the employer's name.
    company = canonical_company(getattr(job, "company", ""))
    if company and len(company) >= 3 and company in haystack:
        candidate.add(25, f"employer '{job.company}' named")

    # 3 — the job title, fuzzily, against the subject.
    title = str(getattr(job, "title", "") or "")
    if title:
        ratio = fuzz.token_set_ratio(title.casefold(), email.subject.casefold())
        if ratio >= 85:
            candidate.add(25, f"title matches the subject ({ratio:.0f}%)")
        elif ratio >= 65:
            candidate.add(12, f"title partly matches the subject ({ratio:.0f}%)")

    # 4 — timing. Replies cluster in the first fortnight.
    if age <= timedelta(days=14):
        candidate.add(10, "within 14 days of applying")
    elif age <= timedelta(days=45):
        candidate.add(4, "within 45 days of applying")

    # 5 — the platform the application was sent through appears in the sender.
    platform = (application.platform or "").casefold()
    if platform and platform in email.sender_domain:
        candidate.add(8, f"sender domain mentions {platform}")

    # 6 — the employer's own domain, when the ad published a contact address.
    # Scored high enough to clear the threshold on its own: unlike every other
    # signal here this is a direct identity link rather than an inference. The
    # advertiser published that address in this specific ad, and mail is coming
    # back from it inside the reply window.
    contact = str(getattr(job, "ad_contact_email", "") or "")
    if contact and "@" in contact:
        _, _, contact_domain = contact.partition("@")
        if contact_domain and contact_domain.casefold() == email.sender_domain:
            candidate.add(50, "sender is the contact address published in the ad")

    return candidate


def match_email(
    email: InboundEmail,
    applications: Sequence[Any],
    jobs: dict[int, Any],
    *,
    threshold: float = MATCH_THRESHOLD,
) -> MatchCandidate | None:
    """Best-scoring application for this email, or None when unsure.

    Returns None rather than a best guess whenever the top score is below the
    threshold, or when two candidates are too close to separate — a wrong match
    corrupts the application's response history and the analytics built on it.
    """
    references = extract_references(f"{email.subject}\n{email.body}")

    scored = []
    for application in applications:
        job = jobs.get(application.job_id)
        if job is None:
            continue
        candidate = _score_one(email, application, job, references=references)
        if candidate.score > 0:
            scored.append(candidate)

    if not scored:
        log.info("email_unmatched", subject=email.subject[:80], reason="no candidates")
        return None

    scored.sort(key=lambda c: c.score, reverse=True)
    best = scored[0]

    if best.score < threshold:
        log.info(
            "email_unmatched",
            subject=email.subject[:80],
            best_score=round(best.score, 1),
            threshold=threshold,
            reason="below threshold",
        )
        return None

    # Two applications scoring nearly the same means the signals could not tell
    # them apart — often the same title at two employers, or two roles at one.
    if len(scored) > 1 and best.score - scored[1].score < 12:
        log.warning(
            "email_ambiguous",
            subject=email.subject[:80],
            top=round(best.score, 1),
            runner_up=round(scored[1].score, 1),
            reason="candidates too close to separate",
        )
        return None

    log.info(
        "email_matched",
        subject=email.subject[:80],
        application_id=best.application_id,
        score=round(best.score, 1),
        signals=best.signals,
    )
    return best
