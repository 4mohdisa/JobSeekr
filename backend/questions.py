"""The question ledger: what employers keep asking, and what it costs.

The answer bank stores answers one at a time and nothing aggregated them, so
"which screening questions actually cost me applications" was unanswerable. The
numbers here are the ones that change what to do next:

**Frequency** — which questions arrive most, across how many *employers* and
platforms. Employers rather than encounters: one company asking eleven times is
not eleven companies asking, and a question only worth pre-answering if several
employers ask it.

**Friction** — which questions park the most jobs, ranked by jobs parked. That
ranking is the pre-answer worklist, in order.

**Coverage** — the share of questions resolved without asking the user, by week.
If the learning loop works this climbs. If it does not, that is the number that
says so, and it is the only one here that can fall.

**Fact leverage** lives in ``facts.leverage`` rather than here. The
derived-answer table has one reader by design — the hash check that decides
whether a cached answer is still true — and a guard enforces it. Keeping the
four numbers in one file is not worth widening that.

CLUSTERING IS NOT OPTIONAL
    "What is your notice period?", "Notice period?" and "How much notice are you
    required to give?" are one question. Counting them separately puts three
    small numbers where one large one belongs, and the friction ranking — the
    whole point — comes out in the wrong order. Clustering goes through
    ``answers.same_question``, which is the resolution matcher's own comparison
    including its disqualifiers, so "available for part-time" and "available for
    full-time" stay two questions.

WHAT THESE NUMBERS ARE NOT
    A ``QuestionEvent`` row is one *encounter*. A job that parks and is retried
    after the answer arrives contributes two rows for the same question, which
    is the learning loop being visible rather than double counting — but it is
    why reach is reported over distinct employers and cost over distinct jobs,
    never over raw row counts.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from backend.apply.answers import normalise_question, same_question
from backend.config import settings
from backend.logging_setup import get_logger
from backend.models import QuestionEvent, QuestionResolution

log = get_logger(__name__)

__all__ = [
    "CoveragePoint",
    "QuestionCluster",
    "clusters",
    "coverage",
    "digest_lines",
    "frequency",
    "friction",
    "record",
]


MAX_CLUSTER_MEMBERS = 400
"""How many distinct phrasings the clusterer will consider.

Clustering is O(distinct phrasings x clusters) because every phrasing is
compared against each existing representative. That is fine for the hundreds of
distinct questions a year of applying produces and would not be fine for an
unbounded table, so it is bounded rather than left to become slow later. The
cut takes the most-asked phrasings first, so what is dropped is the long tail
of one-offs — and :func:`clusters` says in the log when it dropped anything,
because a silently truncated aggregate reads exactly like a complete one.
"""


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------


def record(
    session: Session,
    *,
    question: str,
    question_text: str,
    resolution: QuestionResolution,
    platform: str,
    company: str | None = None,
    job_id: int | None = None,
    source_row_id: int | None = None,
) -> QuestionEvent | None:
    """File one encounter with one screening question.

    Never raises. This is bookkeeping on the apply path, and a ledger write that
    aborts an application would be a strictly worse system than one with a gap
    in its statistics — the same rule ``failures.record`` follows.

    Returns None when the question normalises to nothing, which is what a
    label-less field does. A row keyed on the empty string would join with every
    other label-less field on the site.
    """
    key = normalise_question(question)
    if not key:
        log.debug("question_event_skipped", reason="blank question", job_id=job_id)
        return None

    try:
        event = QuestionEvent(
            question=key,
            question_text=(question_text or question)[:400],
            resolution=resolution,
            platform=platform,
            company=company,
            job_id=job_id,
            source_row_id=source_row_id,
        )
        session.add(event)
        session.flush()
    except Exception as exc:  # noqa: BLE001 - bookkeeping must not break apply
        log.warning("question_event_failed", error=str(exc)[:200], job_id=job_id)
        return None

    log.debug(
        "question_event",
        question=key[:80],
        resolution=resolution.value,
        platform=platform,
        job_id=job_id,
    )
    return event


# --------------------------------------------------------------------------
# Clustering
# --------------------------------------------------------------------------


@dataclass
class QuestionCluster:
    """One question, however many ways it was worded."""

    question: str
    """The most-asked phrasing, used as the cluster's name."""

    variants: list[str] = field(default_factory=list)
    """Every distinct normalised phrasing folded in, most-asked first."""

    asked: int = 0
    """Encounters. Inflated by retries; use ``employers`` for reach."""

    employers: int = 0
    platforms: int = 0
    resolved: int = 0
    abstained: int = 0
    jobs_parked: int = 0
    """Distinct jobs this question parked. The cost, and the friction ranking."""

    last_seen: datetime | None = None

    @property
    def coverage(self) -> float | None:
        """Share of encounters resolved. None when never encountered."""
        return round(self.resolved / self.asked, 4) if self.asked else None


def _events(session: Session, *, hours: int | None) -> list[QuestionEvent]:
    statement = select(QuestionEvent)
    if hours is not None:
        since = datetime.now(UTC) - timedelta(hours=hours)
        statement = statement.where(QuestionEvent.occurred_at >= since)
    return list(session.exec(statement).all())


def clusters(
    session: Session, *, hours: int | None = None, limit: int = 20
) -> list[QuestionCluster]:
    """Group the ledger into questions, most-asked phrasing naming each group.

    Greedy single-pass assignment against the representative of each existing
    cluster, taking phrasings in descending order of how often they were asked.
    Greedy rather than exhaustive because the representative is then always the
    most common wording — which is the one worth showing the user, and the one
    most likely to match the next arrival.
    """
    events = _events(session, hours=hours)
    if not events:
        return []

    by_phrasing: dict[str, list[QuestionEvent]] = defaultdict(list)
    for event in events:
        by_phrasing[event.question].append(event)

    ranked = sorted(by_phrasing.items(), key=lambda item: len(item[1]), reverse=True)
    if len(ranked) > MAX_CLUSTER_MEMBERS:
        log.warning(
            "question_clustering_truncated",
            distinct=len(ranked),
            considered=MAX_CLUSTER_MEMBERS,
            note="the long tail of one-off phrasings is not represented",
        )
        ranked = ranked[:MAX_CLUSTER_MEMBERS]

    groups: list[tuple[str, list[str]]] = []
    for phrasing, _rows in ranked:
        for representative, members in groups:
            if same_question(representative, phrasing):
                members.append(phrasing)
                break
        else:
            groups.append((phrasing, [phrasing]))

    built = [
        _build_cluster(representative, members, by_phrasing)
        for representative, members in groups
    ]
    built.sort(key=lambda c: (c.employers, c.asked), reverse=True)
    return built[:limit]


def _build_cluster(
    representative: str,
    members: list[str],
    by_phrasing: dict[str, list[QuestionEvent]],
) -> QuestionCluster:
    rows = [event for phrasing in members for event in by_phrasing[phrasing]]
    abstained = [
        event for event in rows if event.resolution is QuestionResolution.ABSTAINED
    ]
    return QuestionCluster(
        question=representative,
        variants=members,
        asked=len(rows),
        employers=len({event.company for event in rows if event.company}),
        platforms=len({event.platform for event in rows if event.platform}),
        resolved=len(rows) - len(abstained),
        abstained=len(abstained),
        # Distinct jobs, not distinct abstentions: a multi-step form that parks
        # on the same question twice cost one application, not two.
        jobs_parked=len({event.job_id for event in abstained if event.job_id}),
        last_seen=max((event.occurred_at for event in rows), default=None),
    )


def frequency(
    session: Session, *, hours: int | None = None, limit: int = 20
) -> list[QuestionCluster]:
    """Questions by reach — how many employers ask them. Already the sort order."""
    return clusters(session, hours=hours, limit=limit)


def friction(
    session: Session, *, hours: int | None = None, limit: int = 20
) -> list[QuestionCluster]:
    """Questions that parked jobs, most expensive first.

    Ranked by jobs parked rather than by abstentions, because the cost of a
    question is the applications it stopped, and it is what to pre-answer next.
    Questions that never parked anything are excluded: a complete list sorted by
    a mostly-zero column buries the five rows worth acting on.
    """
    ranked = [
        cluster
        for cluster in clusters(session, hours=hours, limit=MAX_CLUSTER_MEMBERS)
        if cluster.jobs_parked
    ]
    ranked.sort(key=lambda c: (c.jobs_parked, c.abstained), reverse=True)
    return ranked[:limit]


# --------------------------------------------------------------------------
# Coverage
# --------------------------------------------------------------------------


@dataclass
class CoveragePoint:
    """One week of the ledger: how much of it the system answered itself."""

    week: str
    """ISO date of that week's Monday, in UTC."""

    asked: int
    resolved: int
    sufficient_data: bool
    rate: float | None = None
    """Resolved share, or None below the reporting minimum.

    Suppressed rather than zeroed, the same rule the rest of the analytics page
    follows: 100% coverage from one question is not an encouraging trend, it is
    a wrong number, and a trend line is exactly where a wrong point does damage.
    """


def coverage(
    session: Session, *, weeks: int = 8, minimum: int | None = None
) -> list[CoveragePoint]:
    """Weekly resolved share, oldest first.

    Weeks rather than days because the apply passes run twice a day and a daily
    rate swings between 0 and 1 on the volume this system produces. Weeks that
    saw no questions at all are omitted, not plotted as zero — nothing was asked
    is not the same as nothing was answered.
    """
    floor = settings.analytics_min_sample if minimum is None else minimum
    events = _events(session, hours=weeks * 7 * 24)

    buckets: dict[str, list[QuestionEvent]] = defaultdict(list)
    for event in events:
        buckets[_week_start(event.occurred_at)].append(event)

    points = []
    for week in sorted(buckets):
        rows = buckets[week]
        resolved = sum(
            1 for row in rows if row.resolution is not QuestionResolution.ABSTAINED
        )
        enough = len(rows) >= floor
        points.append(
            CoveragePoint(
                week=week,
                asked=len(rows),
                resolved=resolved,
                sufficient_data=enough,
                rate=round(resolved / len(rows), 4) if enough else None,
            )
        )
    return points


def _week_start(moment: datetime) -> str:
    """The Monday of ``moment``'s week, as an ISO date.

    Rows read back from SQLite are naive; the schema stores UTC, so a naive one
    is stamped rather than converted.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    monday = moment.astimezone(UTC).date() - timedelta(days=moment.weekday())
    return monday.isoformat()


# --------------------------------------------------------------------------
# Digest
# --------------------------------------------------------------------------


def digest_lines(session: Session, *, hours: int = 168) -> list[str]:
    """The weekly question section. Empty when the ledger has nothing to say.

    Empty rather than "no questions this week", for the reason
    ``failures.digest_lines`` gives: a section that appears every time saying
    nothing is a section that stops being read.
    """
    worklist = friction(session, hours=hours, limit=5)
    trend = coverage(session, weeks=2)
    if not worklist and not trend:
        return []

    days = max(1, hours // 24)
    lines = [f"\n*Questions* — last {days}d"]

    for cluster in worklist:
        question = (
            cluster.question
            if len(cluster.question) <= 60
            else cluster.question[:57] + "..."
        )
        lines.append(
            f'· "{question}" parked {cluster.jobs_parked} '
            f"job{'s' if cluster.jobs_parked != 1 else ''}"
        )

    if trend:
        latest = trend[-1]
        if latest.rate is None:
            lines.append(
                f"· coverage: {latest.resolved}/{latest.asked} answered without you "
                f"(too few to report a rate)"
            )
        else:
            previous = trend[-2] if len(trend) > 1 else None
            direction = ""
            if previous is not None and previous.rate is not None:
                delta = latest.rate - previous.rate
                direction = f" ({delta:+.0%} on last week)"
            lines.append(
                f"· coverage: {latest.rate:.0%} answered without you{direction}"
            )

    return lines
