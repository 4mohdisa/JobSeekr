"""Which platforms are rotting, and which elements to fix before they fail.

The canary warns per event: this element was missing this morning. That is the
right thing to send at the time and the wrong thing to reason from, because a
selector that breaks once is a redesign and a selector that breaks every fortnight
is a selector that should not be trusted. Only the second is worth acting on, and
nothing put the two apart.

Two sources, deliberately:

* the **failure ledger** — time-windowed, so it answers "how fast is this
  platform changing" and "did last week's fix hold";
* the **knowledge files** — lifetime counters, so they answer "how reliable is
  this element" without a window's worth of luck in it.

Neither answers both. A platform can have a perfect ledger this week because
nothing ran, and an element can have a fine lifetime record while having failed
every time since Tuesday.

WHY THIS IS NOT IN backend/failures.py
    It reads the knowledge files, and ``backend/siteknowledge/__init__.py`` is
    deliberately session-free — it is imported by every adapter and must never
    open a transaction to look up a button. This module is the other side: it
    takes a session and never resolves anything.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from backend.config import settings
from backend.logging_setup import get_logger
from backend.models import FailureEvent, FailureType
from backend.siteknowledge import load

log = get_logger(__name__)

__all__ = [
    "DEGRADED_CONFIDENCE",
    "MIN_OBSERVATIONS",
    "DegradingElement",
    "PlatformChurn",
    "degrading",
    "digest_lines",
    "pending_proposals",
    "platform_churn",
]


DEGRADED_CONFIDENCE = 0.75
"""Below this an element is called degrading rather than working.

An element resolving three times in four is not broken — every one of those
applications went out — and it is not healthy either: the fourth is a parked job
and the trend only goes one way. 0.75 is the point where the failures stop
looking like noise, and it is deliberately well above the point where anyone
would notice from outcomes.
"""

MIN_OBSERVATIONS = 4
"""How many resolutions before the confidence number means anything.

Laplace smoothing puts an untried element at 0.5, which is below the threshold
above — so without this every element in a fresh install would be reported as
degrading on the first digest. Four is the smallest number at which one failure
does not by itself cross the line.
"""

_DRIFT_TYPES = {FailureType.SELECTOR_DRIFT, FailureType.ELEMENT_UNRESOLVED}


# --------------------------------------------------------------------------
# From the ledger: how fast a platform is changing
# --------------------------------------------------------------------------


@dataclass
class PlatformChurn:
    """One platform's rate of change, this window against the last."""

    platform: str
    events: int
    previous_events: int
    elements: list[str] = field(default_factory=list)
    """Which elements moved, most recent first. The churn, itemised."""

    healed: list[str] = field(default_factory=list)
    """Elements that drifted in the previous window and not in this one.

    The honest name for it is "quiet since", not "fixed": nothing here knows
    whether someone edited the file or the platform simply was not visited. The
    digest says so.
    """

    @property
    def accelerating(self) -> bool:
        return self.events > self.previous_events


def platform_churn(session: Session, *, hours: int = 168) -> list[PlatformChurn]:
    """Which platform changes fastest, and what held. Busiest first.

    The window is compared against the one immediately before it, because the
    question is not "how many failures" — it is "more or fewer than last time".
    A platform with four drifts a week for a year is stable in the only sense
    that matters here; a platform that went from zero to four is not.
    """
    now = datetime.now(UTC)
    since = now - timedelta(hours=hours)
    previous_since = since - timedelta(hours=hours)

    events = list(
        session.exec(
            select(FailureEvent).where(FailureEvent.occurred_at >= previous_since)
        ).all()
    )

    current: dict[str, list[FailureEvent]] = defaultdict(list)
    previous: dict[str, list[FailureEvent]] = defaultdict(list)
    for event in events:
        if event.failure_type not in _DRIFT_TYPES:
            continue
        occurred = _aware(event.occurred_at)
        (current if occurred >= since else previous)[event.platform].append(event)

    report = []
    for platform in set(current) | set(previous):
        rows = sorted(
            current.get(platform, []),
            key=lambda event: _aware(event.occurred_at),
            reverse=True,
        )
        now_elements = [row.element_id for row in rows if row.element_id]
        then_elements = {
            row.element_id for row in previous.get(platform, []) if row.element_id
        }
        report.append(
            PlatformChurn(
                platform=platform,
                events=len(rows),
                previous_events=len(previous.get(platform, [])),
                elements=list(dict.fromkeys(now_elements)),
                healed=sorted(then_elements - set(now_elements)),
            )
        )

    report.sort(key=lambda churn: (churn.events, churn.previous_events), reverse=True)
    return report


def _aware(moment: datetime) -> datetime:
    """Rows read back from SQLite are naive; the schema stores UTC."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


# --------------------------------------------------------------------------
# From the files: which elements are going, before they are gone
# --------------------------------------------------------------------------


@dataclass
class DegradingElement:
    """One element that still works and is on its way to not working."""

    platform: str
    key: str
    confidence: float
    success_count: int
    fail_count: int
    required: bool
    best_strategy: str | None
    """The strategy currently carrying it, if any. When this is a shared
    vocabulary candidate the platform's own selectors have all stopped
    working, which is the loudest version of this warning."""


def _platforms() -> list[str]:
    root = settings.siteknowledge_dir
    if not root.is_dir():
        return []
    return sorted(path.name for path in root.iterdir() if path.is_dir())


def degrading() -> list[DegradingElement]:
    """Elements whose record says they are failing, worst first.

    Reads the lifetime counters that have existed since this layer was written
    and that nothing ever looked at. An element only appears once it has enough
    observations for the number to mean something — see ``MIN_OBSERVATIONS``.
    """
    report = []
    for platform in _platforms():
        try:
            knowledge = load(platform)
        except Exception as exc:  # noqa: BLE001 - one bad file must not hide the rest
            log.warning(
                "site_knowledge_health_unreadable",
                platform=platform,
                error=str(exc)[:200],
            )
            continue

        for key, element in sorted(knowledge.elements.items()):
            if element.observations < MIN_OBSERVATIONS:
                continue
            if element.confidence >= DEGRADED_CONFIDENCE:
                continue
            best = next(iter(element.ordered()), None)
            report.append(
                DegradingElement(
                    platform=platform,
                    key=key,
                    confidence=round(element.confidence, 3),
                    success_count=element.success_count,
                    fail_count=element.fail_count,
                    required=element.required,
                    best_strategy=best.selector if best else None,
                )
            )

    report.sort(key=lambda item: (item.confidence, -item.fail_count))
    return report


def pending_proposals() -> list[tuple[str, str, str]]:
    """(platform, element key, selector) for every derived strategy awaiting a yes.

    A proposal that nobody is told about is a parked job with extra steps, so
    this is what the digest reads to keep asking until it is answered.
    """
    pending = []
    for platform in _platforms():
        try:
            knowledge = load(platform)
        except Exception as exc:  # noqa: BLE001 - one bad file must not hide the rest
            log.warning(
                "site_knowledge_health_unreadable",
                platform=platform,
                error=str(exc)[:200],
            )
            continue
        for key, element in sorted(knowledge.elements.items()):
            for proposal in element.proposals:
                pending.append((platform, key, proposal.selector))
    return pending


# --------------------------------------------------------------------------
# Digest
# --------------------------------------------------------------------------


def digest_lines(session: Session, *, hours: int = 168) -> list[str]:
    """The weekly site-knowledge section. Empty when there is nothing to say.

    Ordered by what needs a decision: proposals first, because each one is a
    question the system is waiting on; then elements about to fail; then the
    churn, which is background.
    """
    proposals = pending_proposals()
    failing = degrading()
    churn = [row for row in platform_churn(session, hours=hours) if row.events]

    if not (proposals or failing or churn):
        return []

    days = max(1, hours // 24)
    lines = [f"\n*Site knowledge* — last {days}d"]

    for platform, key, selector in proposals[:5]:
        lines.append(
            f"· `{platform}/{key}`: suggested `{selector}` "
            f"(`/usefix {platform} {key}` · `/nofix {platform} {key}`)"
        )

    for element in failing[:5]:
        lines.append(
            f"· `{element.platform}/{element.key}` degrading: "
            f"{element.success_count} ok / {element.fail_count} failed "
            f"({element.confidence:.0%} confidence)"
        )

    for row in churn[:3]:
        direction = "faster than last week" if row.accelerating else "steady"
        lines.append(
            f"· {row.platform} moved {row.events}× ({direction}): "
            f"{', '.join(row.elements[:3])}"
        )
        if row.healed:
            lines.append(
                f"  _quiet since last week: {', '.join(row.healed[:3])} — "
                f"nothing here knows whether that is a fix or an unvisited site._"
            )

    return lines
