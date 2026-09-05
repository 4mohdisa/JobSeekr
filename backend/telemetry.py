"""How long things take, how often the caches hit, and what an application costs.

Nothing in this system measured its own speed. ``Run`` records when a pass
started and when it ended, which is browser startup plus every application plus
every pacing wait as one number — a regression in field enumeration and a slow
morning are indistinguishable in it.

WHAT IS MEASURED
    Six stages of one application: the page load, enumerating the fields,
    resolving the answers, building the documents, uploading them, and the
    submit itself. Plus the caches — form maps, site knowledge, embeddings here,
    the answer bank and the facts layer from the question ledger — and the LLM
    spend attributable to each job.

PACING IS MEASURED AND NEVER SUMMED
    The randomised wait between submissions protects the user's account. It is
    recorded so that an unexpectedly long one is visible rather than felt, and
    it is kept out of every work total by construction: ``Stage.PACING`` is not
    in ``WORK_STAGES``, and :class:`StageProfile` hands the caller two separate
    fields rather than one list to sum. A chart that let a safety delay read as
    latency would invite someone to shorten it.

RUN ATTRIBUTION IS BY TIME WINDOW
    A ``Run`` row is written at the END of a pass, so a timing cannot carry a
    run id. Timings are attributed to the run whose window contains them. That
    is exact here because this is a single-user machine that runs one pass at a
    time — ``Claude.md``: single user, Windows, one logged-in session — and it
    would be wrong the moment two passes overlapped. Nothing overlaps them
    today, and the alternative is writing the Run row up front, which changes
    what a crashed pass leaves behind.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from backend.logging_setup import get_logger
from backend.models import (
    WORK_STAGES,
    Application,
    ApplicationOutcome,
    CacheEvent,
    CacheName,
    LLMSpend,
    QuestionEvent,
    QuestionResolution,
    Run,
    RunPhase,
    Stage,
    StageTiming,
)

log = get_logger(__name__)

__all__ = [
    "CacheRate",
    "CostPoint",
    "RunProfile",
    "StageProfile",
    "StageStat",
    "cache_rates",
    "cost_per_application",
    "digest_lines",
    "record_cache",
    "run_profiles",
    "stage_profile",
    "time_stage",
]


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------


@contextmanager
def time_stage(
    session: Session,
    stage: Stage,
    *,
    job_id: int | None = None,
    platform: str | None = None,
) -> Iterator[None]:
    """Time a block and file it, however the block ends.

    The row is written in a ``finally``, so a stage that raised is still
    measured. That is deliberate: a stage that got slow and then started failing
    is the shape of a real regression, and dropping the timing on the failure
    path would hide exactly the runs worth looking at.

    ``time.monotonic`` rather than the wall clock — a system clock adjustment
    mid-application must not produce a negative duration.

    Never raises on its own account. A bookkeeping write that aborted an
    application would be a worse system than one with a gap in its statistics,
    the rule ``failures.record`` and ``questions.record`` already follow.
    """
    started = time.monotonic()
    try:
        yield
    finally:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        try:
            session.add(
                StageTiming(
                    stage=stage,
                    duration_ms=elapsed_ms,
                    platform=platform,
                    job_id=job_id,
                )
            )
            session.flush()
        except Exception as exc:  # noqa: BLE001 - telemetry must not break apply
            log.warning("stage_timing_failed", stage=stage.value, error=str(exc)[:200])
        else:
            log.debug(
                "stage_timed",
                stage=stage.value,
                ms=elapsed_ms,
                job_id=job_id,
                platform=platform,
            )


def record_cache(
    session: Session,
    cache: CacheName,
    *,
    hit: bool,
    platform: str | None = None,
    job_id: int | None = None,
    count: int = 1,
) -> None:
    """File ``count`` lookups of one cache with the same outcome.

    ``count`` because embeddings resolve in batches: one call reports "forty
    hits, six misses", and writing forty rows individually would make the
    cheapest cache the most expensive thing to measure.

    Never raises, for the reason :func:`time_stage` gives.
    """
    if count <= 0:
        return
    try:
        for _ in range(count):
            session.add(
                CacheEvent(cache=cache, hit=hit, platform=platform, job_id=job_id)
            )
        session.flush()
    except Exception as exc:  # noqa: BLE001 - telemetry must not break the caller
        log.warning("cache_event_failed", cache=cache.value, error=str(exc)[:200])


# --------------------------------------------------------------------------
# Stage profile
# --------------------------------------------------------------------------


@dataclass
class StageStat:
    """One stage, summarised over a window."""

    stage: str
    observations: int
    total_ms: int
    mean_ms: int
    median_ms: int
    slowest_ms: int


@dataclass
class StageProfile:
    """Where the time went. Work and pacing are separate fields, not one list.

    Separate so that no caller can sum them by accident. ``work`` answers "is it
    getting faster"; ``pacing`` answers "is the wait behaving", and they must
    never end up in the same total.
    """

    work: list[StageStat] = field(default_factory=list)
    pacing: StageStat | None = None

    @property
    def work_total_ms(self) -> int:
        return sum(stat.total_ms for stat in self.work)

    @property
    def slowest(self) -> StageStat | None:
        """The stage taking the most total time. The regression flag."""
        return max(self.work, key=lambda stat: stat.total_ms, default=None)


def _summarise(stage: Stage, rows: list[StageTiming]) -> StageStat:
    durations = sorted(row.duration_ms for row in rows)
    middle = len(durations) // 2
    median = (
        durations[middle]
        if len(durations) % 2
        else (durations[middle - 1] + durations[middle]) // 2
    )
    return StageStat(
        stage=stage.value,
        observations=len(durations),
        total_ms=sum(durations),
        mean_ms=sum(durations) // len(durations),
        median_ms=median,
        slowest_ms=durations[-1],
    )


def _timings(
    session: Session,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[StageTiming]:
    statement = select(StageTiming)
    if since is not None:
        statement = statement.where(StageTiming.occurred_at >= since)
    if until is not None:
        statement = statement.where(StageTiming.occurred_at <= until)
    return list(session.exec(statement).all())


def stage_profile(
    session: Session,
    *,
    hours: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> StageProfile:
    """Summarise where the time went, work and pacing kept apart."""
    if hours is not None and since is None:
        since = datetime.now(UTC) - timedelta(hours=hours)

    grouped: dict[Stage, list[StageTiming]] = defaultdict(list)
    for row in _timings(session, since=since, until=until):
        grouped[row.stage].append(row)

    work = [
        _summarise(stage, rows)
        for stage, rows in grouped.items()
        if stage in WORK_STAGES and rows
    ]
    work.sort(key=lambda stat: stat.total_ms, reverse=True)

    pacing_rows = grouped.get(Stage.PACING) or []
    return StageProfile(
        work=work,
        pacing=_summarise(Stage.PACING, pacing_rows) if pacing_rows else None,
    )


@dataclass
class RunProfile:
    """One apply pass, and which stage cost it the most."""

    run_id: int
    started_at: datetime
    ended_at: datetime | None
    applications: int
    work_ms: int
    pacing_ms: int
    slowest_stage: str | None
    slowest_stage_ms: int


def run_profiles(session: Session, *, limit: int = 12) -> list[RunProfile]:
    """The last few apply passes with their slowest stage, newest first.

    Per run rather than per week because a regression appears in one run and a
    weekly mean hides it behind the runs either side. Attribution is by time
    window — see the module docstring for why that is exact here and what would
    break it.
    """
    runs = [
        run
        for run in session.exec(select(Run).where(Run.phase == RunPhase.APPLY)).all()
        if run.ended_at is not None
    ]
    runs.sort(key=lambda run: run.started_at, reverse=True)

    profiles = []
    for run in runs[:limit]:
        profile = stage_profile(
            session, since=_aware(run.started_at), until=_aware(run.ended_at)
        )
        slowest = profile.slowest
        profiles.append(
            RunProfile(
                run_id=run.id or 0,
                started_at=run.started_at,
                ended_at=run.ended_at,
                applications=int((run.counts or {}).get("considered", 0) or 0),
                work_ms=profile.work_total_ms,
                pacing_ms=profile.pacing.total_ms if profile.pacing else 0,
                slowest_stage=slowest.stage if slowest else None,
                slowest_stage_ms=slowest.total_ms if slowest else 0,
            )
        )
    return profiles


def _aware(moment: datetime | None) -> datetime | None:
    """Rows read back from SQLite are naive; the schema stores UTC."""
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


# --------------------------------------------------------------------------
# Cache rates
# --------------------------------------------------------------------------


@dataclass
class CacheRate:
    """One cache over one week, and what a lookup of it even is."""

    cache: str
    unit: str
    """What one lookup counts — a form, an element, a text, a question. The
    denominators genuinely differ, and a chart that did not say so would invite
    comparing a per-form rate against a per-question one."""

    week: str
    lookups: int
    hits: int
    rate: float


#: What one lookup means for each cache. Stated rather than implied: the caches
#: are consulted at different granularities, and the answer bank and the facts
#: layer are consulted in SEQUENCE, so their denominators are not the same
#: population.
CACHE_UNITS = {
    "answer_bank": "per screening question",
    "facts": "per question the bank missed",
    CacheName.FORM_MAP.value: "per form shape",
    CacheName.SITE_KNOWLEDGE.value: "per element lookup",
    CacheName.EMBEDDING.value: "per text embedded",
}


def cache_rates(session: Session, *, weeks: int = 8) -> list[CacheRate]:
    """Weekly hit rate for all five caches, oldest first.

    Three come from ``cache_event``, recorded at the lookup. Two come from the
    question ledger, because their lookups ARE screening questions and
    ``question_event`` already records the outcome of every one — writing them
    to a second table would be the same fact stored twice.

    The answer bank and the facts layer are consulted in sequence: facts only
    see the questions the bank could not answer, so their denominator is
    smaller on purpose. A facts hit rate computed over ALL questions would fall
    every time the answer bank improved, which is the opposite of the truth.
    """
    since = datetime.now(UTC) - timedelta(weeks=weeks)

    buckets: dict[tuple[str, str], list[bool]] = defaultdict(list)

    for event in session.exec(
        select(CacheEvent).where(CacheEvent.occurred_at >= since)
    ).all():
        buckets[(event.cache.value, _week_start(event.occurred_at))].append(event.hit)

    for event in session.exec(
        select(QuestionEvent).where(QuestionEvent.occurred_at >= since)
    ).all():
        week = _week_start(event.occurred_at)
        buckets[("answer_bank", week)].append(
            event.resolution is QuestionResolution.BANK
        )
        if event.resolution is not QuestionResolution.BANK:
            buckets[("facts", week)].append(event.resolution is QuestionResolution.FACT)

    rates = [
        CacheRate(
            cache=cache,
            unit=CACHE_UNITS.get(cache, "per lookup"),
            week=week,
            lookups=len(outcomes),
            hits=sum(outcomes),
            rate=round(sum(outcomes) / len(outcomes), 4),
        )
        for (cache, week), outcomes in buckets.items()
        if outcomes
    ]
    rates.sort(key=lambda rate: (rate.cache, rate.week))
    return rates


def _week_start(moment: datetime) -> str:
    aware = _aware(moment)
    assert aware is not None
    return (aware.astimezone(UTC).date() - timedelta(days=aware.weekday())).isoformat()


# --------------------------------------------------------------------------
# Cost
# --------------------------------------------------------------------------


@dataclass
class CostPoint:
    """One week's LLM spend per application."""

    week: str
    applications: int
    total_usd: float
    per_application_usd: float


def cost_per_application(session: Session, *, weeks: int = 8) -> list[CostPoint]:
    """Weekly mean LLM spend per submitted application, oldest first.

    Should fall as the caches fill: the second application to a known form shape
    costs no mapping call, and a question already in the bank costs no
    derivation.

    Counts SUBMITTED applications only, matching the funnel — an aborted attempt
    that still burned tokens inflates the numerator and never reaches the
    denominator, which would read as a cost increase when the truth is a failed
    run.

    Spend with no ``job_id`` is excluded and cannot be attributed: the campaign
    summary embedding is per campaign, not per job. It is a real cost and it is
    not part of this number.
    """
    since = datetime.now(UTC) - timedelta(weeks=weeks)

    applications = [
        row
        for row in session.exec(
            select(Application).where(Application.applied_at >= since)
        ).all()
        if row.outcome is ApplicationOutcome.SUBMITTED
    ]
    if not applications:
        return []

    spend_by_job: dict[int, float] = defaultdict(float)
    for row in session.exec(select(LLMSpend)).all():
        if row.job_id is not None:
            spend_by_job[row.job_id] += row.cost_usd

    weekly: dict[str, list[float]] = defaultdict(list)
    for application in applications:
        weekly[_week_start(application.applied_at)].append(
            spend_by_job.get(application.job_id, 0.0)
        )

    return [
        CostPoint(
            week=week,
            applications=len(costs),
            total_usd=round(sum(costs), 4),
            per_application_usd=round(sum(costs) / len(costs), 4),
        )
        for week, costs in sorted(weekly.items())
    ]


# --------------------------------------------------------------------------
# Digest
# --------------------------------------------------------------------------


def digest_lines(session: Session, *, hours: int = 168) -> list[str]:
    """The weekly performance section. Empty when nothing has been measured.

    Empty rather than a row of zeroes, the rule the other digest sections
    follow: a section that appears every week saying nothing stops being read.
    """
    profile = stage_profile(session, hours=hours)
    costs = cost_per_application(session, weeks=2)
    if not profile.work and not costs:
        return []

    lines = [f"\n*Performance* — last {max(1, hours // 24)}d"]

    slowest = profile.slowest
    if slowest is not None:
        lines.append(
            f"· slowest stage: {slowest.stage.replace('_', ' ')} — "
            f"{slowest.median_ms / 1000:.1f}s median over {slowest.observations}"
        )
    if profile.pacing is not None:
        # Named as a wait, in its own line, never added to the work total.
        lines.append(
            f"· pacing (deliberate, not work): "
            f"{profile.pacing.total_ms / 60000:.0f} min across "
            f"{profile.pacing.observations} waits"
        )

    if costs:
        latest = costs[-1]
        direction = ""
        if len(costs) > 1:
            previous = costs[-2].per_application_usd
            if previous:
                delta = (latest.per_application_usd - previous) / previous
                direction = f" ({delta:+.0%} on last week)"
        lines.append(
            f"· ${latest.per_application_usd:.3f} per application "
            f"over {latest.applications}{direction}"
        )

    return lines
