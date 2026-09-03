"""Remember failures, so repetition is visible.

THE GAP THIS FILLS
    ``apply/guardrails.py`` has a circuit breaker: enough consecutive failures
    on a platform and it stops. That is the right immediate response and it has
    no memory at all — once it resets, the failures are gone. Every failure
    therefore looks like the first one, and the questions that actually matter
    are unanswerable:

        which selectors drift most
        which employers consistently abstain
        which screening questions keep arriving unanswered
        whether this parse-gate failure is new or the fourth this week

    Those are all questions about *repetition over time*, which is exactly what
    a circuit breaker throws away.

TRENDS, NOT EVENTS
    Nothing here notifies. A failure that deserves an immediate alert already
    gets one from the layer that detected it — a restriction halts everything, an
    unresolvable element parks the job and pings. Adding a second notification
    per failure would train the user to ignore the channel.

    What this contributes is the shape over time, folded into the evening
    digest: "resume_file_input has drifted 4 times this week" is worth reading;
    four separate messages saying "resume_file_input drifted" are not.

RESOLUTION CLOSES THE LOOP
    An unresolved row keeps voting in every trend forever. ``resolve`` marks the
    ones that were dealt with, which is what keeps "this keeps happening"
    distinguishable from "this happened once in March".
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import Session, select

from backend.logging_setup import get_logger
from backend.models import FailureEvent, FailureType

log = get_logger(__name__)

__all__ = [
    "RECURRENCE_THRESHOLD",
    "Trend",
    "TrendReport",
    "digest_lines",
    "is_recurring",
    "record",
    "resolve",
    "trends",
]


RECURRENCE_THRESHOLD = 3
"""Occurrences before something is called recurring rather than incidental.

Three, matching ``formmaps.TRUST_THRESHOLD`` — one is chance, two is a
coincidence, three is a pattern. Sharing the number is deliberate: two different
thresholds for "enough times to mean something" would be two things to explain.
"""


def record(
    session: Session,
    *,
    platform: str,
    failure_type: FailureType,
    element_id: str | None = None,
    flow_variant: str | None = None,
    company: str | None = None,
    question: str | None = None,
    job_id: int | None = None,
    detail: str | None = None,
) -> FailureEvent:
    """Write one failure to the ledger.

    Never raises. This is bookkeeping attached to a path that is already
    failing, and an exception from the bookkeeping would replace a diagnosable
    failure with a confusing one.
    """
    event = FailureEvent(
        platform=platform,
        failure_type=failure_type,
        element_id=element_id,
        flow_variant=flow_variant,
        company=company,
        question=question,
        job_id=job_id,
        detail=(detail or "")[:500] or None,
    )
    session.add(event)
    log.info(
        "failure_recorded",
        platform=platform,
        failure_type=failure_type.value,
        element_id=element_id,
        company=company,
        job_id=job_id,
    )
    return event


def resolve(
    session: Session,
    *,
    platform: str,
    failure_type: FailureType,
    element_id: str | None = None,
    question: str | None = None,
    resolution: str = "",
) -> int:
    """Close every open matching failure. Returns how many were closed.

    Called when the underlying cause is dealt with — a strategy resolves again,
    an answer arrives for a question that kept abstaining. Without this the
    ledger only grows and a fix is indistinguishable from a lull.
    """
    statement = select(FailureEvent).where(
        FailureEvent.platform == platform,
        FailureEvent.failure_type == failure_type,
        FailureEvent.resolved_at.is_(None),  # type: ignore[union-attr]
    )
    if element_id is not None:
        statement = statement.where(FailureEvent.element_id == element_id)
    if question is not None:
        statement = statement.where(FailureEvent.question == question)

    now = datetime.now(UTC)
    closed = 0
    for event in session.exec(statement).all():
        event.resolved_at = now
        event.resolution = resolution or "resolved"
        session.add(event)
        closed += 1

    if closed:
        log.info(
            "failures_resolved",
            platform=platform,
            failure_type=failure_type.value,
            element_id=element_id,
            closed=closed,
            resolution=resolution,
        )
    return closed


# --------------------------------------------------------------------------
# Trends
# --------------------------------------------------------------------------


@dataclass
class Trend:
    """One thing that has failed more than once, and how often."""

    label: str
    count: int
    platform: str = ""
    last_seen: datetime | None = None

    @property
    def recurring(self) -> bool:
        return self.count >= RECURRENCE_THRESHOLD


@dataclass
class TrendReport:
    """The four questions the ledger exists to answer."""

    window_hours: int
    drifting_elements: list[Trend] = field(default_factory=list)
    abstaining_companies: list[Trend] = field(default_factory=list)
    unanswered_questions: list[Trend] = field(default_factory=list)
    recurring_types: list[Trend] = field(default_factory=list)
    total: int = 0
    resolved: int = 0

    @property
    def quiet(self) -> bool:
        """Whether there is anything worth putting in a digest."""
        return not (
            self.drifting_elements
            or self.abstaining_companies
            or self.unanswered_questions
            or self.recurring_types
        )


def _rank(
    events: list[FailureEvent],
    key: Any,
    *,
    minimum: int = 2,
    limit: int = 5,
) -> list[Trend]:
    """Group, count, and keep only what repeated.

    ``minimum=2`` because a trend report of one-offs is a log, and the whole
    point of folding this into a digest instead of alerting per event is that
    only repetition gets the user's attention.
    """
    counter: Counter[tuple[str, str]] = Counter()
    latest: dict[tuple[str, str], datetime] = {}

    for event in events:
        value = key(event)
        if not value:
            continue
        bucket = (str(value), event.platform)
        counter[bucket] += 1
        occurred = event.occurred_at
        if bucket not in latest or occurred > latest[bucket]:
            latest[bucket] = occurred

    return [
        Trend(label=label, count=count, platform=platform, last_seen=latest[(label, platform)])
        for (label, platform), count in counter.most_common(limit)
        if count >= minimum
    ]


def trends(session: Session, *, hours: int = 168) -> TrendReport:
    """Summarise the ledger over a window. Default is a week.

    A week rather than the digest's 24 hours: a selector that drifts twice in
    seven days is the signal, and a 24-hour window would show it as two
    unrelated single events on different evenings.
    """
    since = datetime.now(UTC) - timedelta(hours=hours)
    events = list(
        session.exec(select(FailureEvent).where(FailureEvent.occurred_at >= since)).all()
    )
    open_events = [event for event in events if event.resolved_at is None]

    report = TrendReport(
        window_hours=hours,
        total=len(events),
        resolved=len(events) - len(open_events),
    )

    report.drifting_elements = _rank(
        [
            event
            for event in open_events
            if event.failure_type
            in {FailureType.SELECTOR_DRIFT, FailureType.ELEMENT_UNRESOLVED}
        ],
        lambda event: event.element_id,
    )
    report.abstaining_companies = _rank(
        [
            event
            for event in open_events
            if event.failure_type is FailureType.ANSWER_ABSTAINED
        ],
        lambda event: event.company,
    )
    report.unanswered_questions = _rank(
        [
            event
            for event in open_events
            if event.failure_type is FailureType.ANSWER_ABSTAINED
        ],
        lambda event: event.question,
    )
    report.recurring_types = _rank(
        open_events,
        lambda event: event.failure_type.value,
        minimum=RECURRENCE_THRESHOLD,
    )

    log.debug(
        "failure_trends",
        hours=hours,
        total=report.total,
        resolved=report.resolved,
        drifting=len(report.drifting_elements),
        companies=len(report.abstaining_companies),
        questions=len(report.unanswered_questions),
    )
    return report


def is_recurring(
    session: Session,
    *,
    platform: str,
    failure_type: FailureType,
    element_id: str | None = None,
    hours: int = 168,
) -> bool:
    """Whether this exact failure has happened enough times to be a pattern.

    Answers "is this parse-gate failure new, or the fourth this week?" at the
    point of failure, so the log line itself can say which.
    """
    since = datetime.now(UTC) - timedelta(hours=hours)
    statement = select(FailureEvent).where(
        FailureEvent.platform == platform,
        FailureEvent.failure_type == failure_type,
        FailureEvent.occurred_at >= since,
    )
    if element_id is not None:
        statement = statement.where(FailureEvent.element_id == element_id)
    return len(list(session.exec(statement).all())) >= RECURRENCE_THRESHOLD


# --------------------------------------------------------------------------
# Digest
# --------------------------------------------------------------------------


def _ago(moment: datetime | None) -> str:
    if moment is None:
        return ""
    if moment.tzinfo is None:
        # Rows read back from SQLite are naive; the schema stores UTC.
        moment = moment.replace(tzinfo=UTC)
    hours = (datetime.now(UTC) - moment).total_seconds() / 3600
    if hours < 1:
        return "just now"
    if hours < 24:
        return f"{int(hours)}h ago"
    return f"{int(hours / 24)}d ago"


def digest_lines(session: Session, *, hours: int = 168) -> list[str]:
    """Trend lines for the evening digest. Empty when there is nothing to say.

    Empty rather than "no failures this week": a digest section that appears
    every evening saying nothing is a section people stop reading, and this one
    needs to be read on the evening it is not empty.
    """
    report = trends(session, hours=hours)
    if report.quiet:
        return []

    days = max(1, report.window_hours // 24)
    lines = [f"\n*Failure trends* — last {days}d"]

    for trend in report.drifting_elements:
        lines.append(
            f"· `{trend.label}` on {trend.platform}: {trend.count}× "
            f"(last {_ago(trend.last_seen)})"
        )
    for trend in report.abstaining_companies:
        lines.append(f"· {trend.label} keeps abstaining: {trend.count}×")
    for trend in report.unanswered_questions:
        question = trend.label if len(trend.label) <= 60 else trend.label[:57] + "..."
        lines.append(f'· still unanswered: "{question}" ({trend.count}×)')
    for trend in report.recurring_types:
        if trend.label in {
            FailureType.SELECTOR_DRIFT.value,
            FailureType.ELEMENT_UNRESOLVED.value,
            FailureType.ANSWER_ABSTAINED.value,
        }:
            # Already itemised above; repeating the total adds noise.
            continue
        lines.append(f"· {trend.label.replace('_', ' ')} on {trend.platform}: {trend.count}×")

    if report.resolved:
        lines.append(f"_{report.resolved} of {report.total} resolved._")
    return lines
