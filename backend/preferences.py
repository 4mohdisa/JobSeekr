"""What the user prefers — learned carefully, applied only once confirmed.

THE HARD RULE
    An inferred preference is a PROPOSAL. It is written with
    ``status=PROPOSED``, it does not affect a single decision, and it becomes
    active only when the user confirms it over Telegram.

    That is not caution for its own sake. The evidence here is behavioural —
    "you skipped five jobs at this company" — and behaviour is ambiguous. Five
    skips might mean "never show me this company" or might mean those five ads
    were badly written. Acting on the first reading without asking would
    silently narrow the search on a guess, and the user would have no way to
    see why the jobs stopped arriving.

FACTS ARE NEVER INFERRED
    Work rights, licences, certifications, dates. These may only ever be
    ``USER_SET`` or ``ASKED``. Hard rule 1: facts about the user come from the
    profile verbatim or not at all, and an inferred fact is a fabricated one no
    matter how strong the evidence looked. ``set`` refuses, loudly, rather than
    downgrading to a proposal — a fabricated fact that merely needs confirming
    is still a fabricated fact sitting in front of the user asking to be waved
    through.

NOT SPAM
    Proposals batch into the evening digest. At most ``DAILY_PROPOSAL_CAP`` a
    day. A proposal ignored twice retires itself, because a question the user
    has silently declined twice is a question that should stop arriving.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import Session, select

from backend.logging_setup import get_logger
from backend.models import (
    AnswerType,
    Preference,
    PreferenceScope,
    PreferenceSource,
    PreferenceStatus,
)

log = get_logger(__name__)

__all__ = [
    "DAILY_PROPOSAL_CAP",
    "FACT_KEY_PATTERNS",
    "IGNORES_BEFORE_RETIREMENT",
    "SKIPS_BEFORE_PROPOSAL",
    "FactInferenceRefused",
    "Proposal",
    "active",
    "confirm",
    "digest_lines",
    "get",
    "is_fact_key",
    "mark_ignored",
    "propose",
    "propose_from_skips",
    "reject",
    "set",
]


SKIPS_BEFORE_PROPOSAL = 5
"""Skips of the same company or keyword before proposing an exclusion.

Five, not three. Three is the threshold used for *trusting something that has
been working* (form maps, failure recurrence); this is the threshold for
*narrowing the search on the user's behalf*, which is the more expensive
mistake — a wrongly excluded employer is invisible, because the jobs simply
stop arriving and nothing says why.
"""

DAILY_PROPOSAL_CAP = 3
"""Proposals put to the user in one day. Beyond this they wait."""

IGNORES_BEFORE_RETIREMENT = 2
"""Times a proposal may be ignored before it stops being asked."""


FACT_KEY_PATTERNS: tuple[str, ...] = (
    # "work rights", "working rights", "right to work" — the same fact, and the
    # single most consequential one to get wrong on an application.
    r"work\w*\s*rights?",
    r"rights?\s*to\s*work",
    r"visa",
    r"citizen",
    r"resident",
    r"licence|license",
    r"certificat",
    r"clearance",
    r"qualification",
    r"degree",
    r"birth",
    r"\bage\b",
    # Written in both orders: "current salary" and "salary expectations".
    r"salary",
    r"remuneration",
    r"notice\s*period",
    r"start\s*date",
    r"availab",
    r"referee|reference",
)
"""Keys that name a fact about the user rather than a preference.

Matched loosely and on purpose. A false positive costs one preference the user
has to set by hand; a false negative lets the system invent a claim about their
visa status and put it on an application. Those are not comparable, so the list
errs heavily towards refusing.
"""


class FactInferenceRefused(ValueError):
    """An attempt to infer a fact about the user. Never permitted."""


def is_fact_key(key: str) -> bool:
    """Whether this key names a fact about the user."""
    normalised = key.replace("_", " ").replace("-", " ").casefold()
    return any(re.search(pattern, normalised) for pattern in FACT_KEY_PATTERNS)


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def set(
    session: Session,
    *,
    key: str,
    value: str,
    source: PreferenceSource,
    value_type: AnswerType = AnswerType.TEXT,
    campaign_id: int | None = None,
    confidence: float = 1.0,
    evidence: str | None = None,
) -> Preference:
    """Write a preference. The one door in, and where the hard rule lives.

    Raises ``FactInferenceRefused`` for an inferred fact. Not "degrade it to a
    proposal": a fabricated fact awaiting confirmation is still fabricated, and
    presenting it for approval invites the user to wave through something the
    system had no business deriving.
    """
    if source is PreferenceSource.INFERRED and is_fact_key(key):
        log.error(
            "fact_inference_refused",
            key=key,
            note="facts about the user are user_set or asked, never inferred",
        )
        raise FactInferenceRefused(
            f"{key!r} names a fact about the user; facts may only be user_set "
            "or asked (Claude.md hard rule 1)"
        )

    scope = PreferenceScope.CAMPAIGN if campaign_id is not None else PreferenceScope.GLOBAL
    status = (
        PreferenceStatus.PROPOSED
        if source is PreferenceSource.INFERRED
        else PreferenceStatus.ACTIVE
    )

    row = session.exec(
        select(Preference).where(
            Preference.key == key, Preference.campaign_id == campaign_id
        )
    ).first()

    if row is None:
        row = Preference(
            key=key,
            value=value,
            value_type=value_type,
            scope=scope,
            campaign_id=campaign_id,
            source=source,
            status=status,
            confidence=confidence,
            evidence=evidence,
            confirmed_at=(
                datetime.now(UTC) if status is PreferenceStatus.ACTIVE else None
            ),
        )
    else:
        # A user's own statement always wins over an inference, including one
        # already confirmed — they are correcting it.
        if source is PreferenceSource.INFERRED and row.source in {
            PreferenceSource.USER_SET,
            PreferenceSource.ASKED,
        }:
            log.info("inference_declined_user_set_wins", key=key)
            return row
        row.value = value
        row.value_type = value_type
        row.source = source
        row.status = status
        row.confidence = confidence
        row.evidence = evidence
        if status is PreferenceStatus.ACTIVE:
            row.confirmed_at = datetime.now(UTC)

    session.add(row)
    log.info(
        "preference_set",
        key=key,
        source=source.value,
        status=status.value,
        campaign_id=campaign_id,
    )
    return row


def propose(
    session: Session,
    *,
    key: str,
    value: str,
    evidence: str,
    confidence: float = 0.0,
    campaign_id: int | None = None,
    value_type: AnswerType = AnswerType.TEXT,
) -> Preference:
    """Suggest a preference. Changes nothing until the user confirms."""
    return set(
        session,
        key=key,
        value=value,
        source=PreferenceSource.INFERRED,
        value_type=value_type,
        campaign_id=campaign_id,
        confidence=confidence,
        evidence=evidence,
    )


def confirm(session: Session, preference_id: int) -> Preference | None:
    """The user said yes. Now it takes effect."""
    row = session.get(Preference, preference_id)
    if row is None:
        return None
    row.status = PreferenceStatus.ACTIVE
    # The source becomes ASKED, not USER_SET: the system raised it and the user
    # agreed, which is a weaker thing than the user stating it unprompted, and
    # the difference matters when reviewing what the system decided for itself.
    row.source = PreferenceSource.ASKED
    row.times_confirmed += 1
    row.confirmed_at = datetime.now(UTC)
    session.add(row)
    log.info("preference_confirmed", key=row.key, preference_id=preference_id)
    return row


def reject(session: Session, preference_id: int) -> Preference | None:
    """The user said no. It stays recorded so it is not proposed again."""
    row = session.get(Preference, preference_id)
    if row is None:
        return None
    row.status = PreferenceStatus.REJECTED
    session.add(row)
    log.info("preference_rejected", key=row.key, preference_id=preference_id)
    return row


def mark_ignored(session: Session, preference_id: int) -> Preference | None:
    """The user did not answer. Twice and it stops asking."""
    row = session.get(Preference, preference_id)
    if row is None:
        return None
    row.times_ignored += 1
    if row.times_ignored >= IGNORES_BEFORE_RETIREMENT:
        row.status = PreferenceStatus.RETIRED
        log.info(
            "preference_retired",
            key=row.key,
            ignored=row.times_ignored,
            note="silence twice is an answer",
        )
    session.add(row)
    return row


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def active(session: Session, *, campaign_id: int | None = None) -> dict[str, str]:
    """Every preference that may affect behaviour. Proposals are excluded.

    Campaign-scoped wins over global, matching the answer bank's rule so there
    is one scoping story in the system rather than two.
    """
    rows = session.exec(
        select(Preference).where(Preference.status == PreferenceStatus.ACTIVE)
    ).all()

    resolved: dict[str, str] = {}
    for row in sorted(rows, key=lambda r: r.campaign_id is not None):
        if row.campaign_id is not None and row.campaign_id != campaign_id:
            continue
        resolved[row.key] = row.value
    return resolved


def get(
    session: Session, key: str, *, campaign_id: int | None = None, default: str | None = None
) -> str | None:
    """One active preference, or ``default``. A proposal reads as absent."""
    return active(session, campaign_id=campaign_id).get(key, default)


# --------------------------------------------------------------------------
# Inference
# --------------------------------------------------------------------------


@dataclass
class Proposal:
    """A candidate preference, before anything is written."""

    key: str
    value: str
    evidence: str
    confidence: float


def propose_from_skips(
    session: Session, *, campaign_id: int | None = None, hours: int = 720
) -> list[Preference]:
    """Look at what the user skipped and suggest exclusions.

    Companies and title keywords only. Both are things the user can sensibly
    confirm — "you skipped five jobs at Globex, exclude them?" is a question
    with an obvious answer, where "your implied salary floor is $95k" is a
    number they would have to reverse-engineer to check.

    Nothing here is applied. Every return value is a PROPOSED row.
    """
    from backend.models import Job, JobStatus

    since = datetime.now(UTC) - timedelta(hours=hours)
    skipped = [
        job
        for job in session.exec(
            select(Job).where(Job.status == JobStatus.SKIPPED)
        ).all()
        if _aware(job.discovered_at or since) >= since
        and (campaign_id is None or job.campaign_id == campaign_id)
    ]

    proposals: list[Proposal] = []

    companies = Counter(job.company for job in skipped if job.company)
    for company, count in companies.items():
        if count >= SKIPS_BEFORE_PROPOSAL:
            proposals.append(
                Proposal(
                    key=f"exclude_company:{company}",
                    value=company,
                    evidence=f"you skipped {count} jobs at {company}",
                    confidence=min(0.9, count / 10),
                )
            )

    keywords = Counter(
        word
        for job in skipped
        for word in re.findall(r"[a-z]{4,}", (job.title or "").casefold())
        if word not in _TITLE_STOPWORDS
    )
    for keyword, count in keywords.items():
        if count >= SKIPS_BEFORE_PROPOSAL:
            proposals.append(
                Proposal(
                    key=f"exclude_keyword:{keyword}",
                    value=keyword,
                    evidence=f'you skipped {count} jobs with "{keyword}" in the title',
                    confidence=min(0.8, count / 12),
                )
            )

    written: list[Preference] = []
    for proposal in proposals:
        existing = session.exec(
            select(Preference).where(
                Preference.key == proposal.key, Preference.campaign_id == campaign_id
            )
        ).first()
        if existing is not None:
            # Already asked, already answered, or already retired. Re-proposing
            # something the user rejected is exactly the spam this guards.
            continue
        written.append(
            propose(
                session,
                key=proposal.key,
                value=proposal.value,
                evidence=proposal.evidence,
                confidence=proposal.confidence,
                campaign_id=campaign_id,
            )
        )

    log.info(
        "skip_proposals",
        skipped_jobs=len(skipped),
        candidates=len(proposals),
        written=len(written),
    )
    return written


_TITLE_STOPWORDS = frozenset(
    {
        "senior",
        "junior",
        "lead",
        "with",
        "from",
        "this",
        "that",
        "role",
        "team",
        "work",
        "full",
        "part",
        "time",
        "level",
        "adelaide",
        "australia",
    }
)
"""Words too common in job titles to mean anything as an exclusion.

Without these, "senior" reaches five skips almost immediately and the user is
asked whether to exclude every senior role they have ever scrolled past.
"""


# --------------------------------------------------------------------------
# Asking, without spamming
# --------------------------------------------------------------------------


def pending(session: Session, *, limit: int = DAILY_PROPOSAL_CAP) -> list[Preference]:
    """Proposals to put to the user now, honouring the daily cap.

    Highest confidence first, and anything asked in the last 24 hours is
    excluded — a proposal that already went out and has not been answered is not
    made more answerable by sending it again.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    rows = [
        row
        for row in session.exec(
            select(Preference).where(Preference.status == PreferenceStatus.PROPOSED)
        ).all()
        if row.last_asked_at is None or _aware(row.last_asked_at) < cutoff
    ]
    rows.sort(key=lambda row: row.confidence, reverse=True)
    return rows[:limit]


def _aware(moment: datetime) -> datetime:
    """SQLite hands back naive datetimes; the schema stores UTC."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def digest_lines(session: Session) -> list[str]:
    """Proposal lines for the evening digest, and mark them asked.

    Batched here rather than sent as they are inferred: an inference is never
    urgent, and interrupting the user the moment a fifth skip lands would make
    the channel one they mute.

    Empty when there is nothing to propose — a section that shows up every
    evening saying nothing stops being read.
    """
    proposals = pending(session)
    if not proposals:
        return []

    now = datetime.now(UTC)
    lines = ["\n*Noticed a pattern* — confirm or ignore"]
    for row in proposals:
        assert row.id is not None
        lines.append(f"· {row.evidence}. Exclude it? `/yes {row.id}` · `/no {row.id}`")
        row.last_asked_at = now
        session.add(row)

    log.info("preference_proposals_sent", count=len(proposals), cap=DAILY_PROPOSAL_CAP)
    return lines


def sweep_ignored(session: Session, *, hours: int = 48) -> int:
    """Count a proposal as ignored once it has gone unanswered long enough.

    Called before the digest is built. Without it ``times_ignored`` never moves,
    ``IGNORES_BEFORE_RETIREMENT`` never trips, and a proposal the user has been
    scrolling past for a fortnight keeps reappearing forever.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    swept = 0
    for row in session.exec(
        select(Preference).where(Preference.status == PreferenceStatus.PROPOSED)
    ).all():
        if row.last_asked_at is not None and _aware(row.last_asked_at) < cutoff:
            mark_ignored(session, row.id)  # type: ignore[arg-type]
            swept += 1
    return swept


def observed_field(
    session: Session, *, key: str, value: str, campaign_id: int | None = None
) -> Preference | None:
    """Record a non-screening form field the user filled, as a proposal.

    Preferred start date, referral source, notice period. Useful to remember —
    and every one of them is squarely in fact territory, so this refuses the
    fact-shaped ones rather than proposing them. The user sets those on the
    Preferences page, where the value is theirs rather than something the
    system watched them type once.
    """
    if is_fact_key(key):
        log.info(
            "observed_field_not_inferred",
            key=key,
            note="fact-shaped; the user sets this directly",
        )
        return None
    return propose(
        session,
        key=key,
        value=value,
        evidence=f'you entered "{value}" for "{key}" on an application',
        confidence=0.5,
        campaign_id=campaign_id,
    )
