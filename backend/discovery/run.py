"""The discovery pass: active campaigns in, new ``job`` rows out.

Source failures are contained here rather than in each adapter. A board being
down, rate-limiting, or changing its payload shape is normal operating
weather; the run must record it loudly and keep going with the other boards,
because a LinkedIn outage that also silently stopped Seek discovery would cost
the user a day of applications for no reason.

    uv run python -m backend.discovery.run
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from backend.base import RawJob, Source
from backend.boards import source_boards
from backend.config import settings
from backend.db import persist_detached, session_scope
from backend.discovery.dedupe import find_duplicate
from backend.discovery.normalize import normalize_job
from backend.logging_setup import configure_logging, get_logger
from backend.models import Campaign, Job, Run, RunPhase

log = get_logger(__name__)

__all__ = ["build_sources", "discover", "run_discovery"]


def build_sources() -> list[Source]:
    """The boards discovery reads, from the one registry that defines them.

    Adding a board is an entry in ``backend.boards`` plus an adapter file —
    never a change to the runner's logic, which stays source-agnostic.
    """
    return [entry.make_source() for entry in source_boards()]


def _search_safely(
    source: Source,
    *,
    terms: list[str],
    locations: list[str],
    hours_old: int | None,
    limit: int | None,
    errors: list[dict[str, Any]],
) -> list[RawJob]:
    """Run one source. Never propagates — a dead board is not a dead run."""
    try:
        return source.search(
            terms=terms, locations=locations, hours_old=hours_old, limit=limit
        )
    except Exception as exc:
        log.exception(
            "source_failed",
            source=getattr(source, "name", type(source).__name__),
            error=str(exc)[:300],
        )
        errors.append(
            {
                "source": getattr(source, "name", type(source).__name__),
                "error": f"{type(exc).__name__}: {exc}"[:300],
            }
        )
        return []


def _store(session: Session, raw: RawJob, *, campaign_id: int | None) -> str:
    """Insert one discovered ad. Returns 'new', 'duplicate' or 'error'."""
    existing = session.exec(
        select(Job).where(
            Job.source == raw.source, Job.source_job_id == str(raw.source_job_id)
        )
    ).first()
    if existing is not None:
        return "duplicate"

    job = normalize_job(raw, campaign_id=campaign_id)

    if find_duplicate(session, job) is not None:
        return "duplicate"

    session.add(job)
    try:
        session.flush()
    except IntegrityError:
        # Two sources racing on the same ad, or a UNIQUE we did not anticipate.
        session.rollback()
        log.debug("job_insert_conflict", source=raw.source, source_job_id=raw.source_job_id)
        return "duplicate"
    return "new"


def discover(
    session: Session,
    campaigns: Iterable[Campaign],
    *,
    sources: list[Source] | None = None,
    hours_old: int | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]], set[str]]:
    """Run discovery for the given campaigns.

    Returns ``(counts, errors, succeeded)``, where ``succeeded`` names the
    sources that answered at least once. The caller needs that third value
    because "no ads" alone cannot tell a dead board from a quiet one.
    """
    sources = sources if sources is not None else build_sources()
    counts: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    succeeded: set[str] = set()

    for campaign in campaigns:
        terms = [str(t) for t in (campaign.search_terms or []) if str(t).strip()]
        locations = [str(loc) for loc in (campaign.locations or []) if str(loc).strip()]
        if not terms:
            log.warning("campaign_has_no_search_terms", campaign=campaign.name)
            continue

        for source in sources:
            name = getattr(source, "name", type(source).__name__)
            bucket = counts.setdefault(
                name, {"fetched": 0, "new": 0, "duplicate": 0, "error": 0}
            )

            before = len(errors)
            raws = _search_safely(
                source,
                terms=terms,
                locations=locations,
                hours_old=hours_old,
                limit=limit,
                errors=errors,
            )
            if len(errors) > before:
                # The source itself failed, as opposed to a single ad failing to
                # store. Counted here so the per-source bucket shows it: a
                # bucket reading all zeros used to be the only trace a board had
                # died, and zeros are also what a quiet day looks like.
                bucket["error"] += 1
            else:
                succeeded.add(name)

            bucket["fetched"] += len(raws)
            if not raws:
                continue
            if dry_run:
                log.info("dry_run_would_store", source=name, count=len(raws))
                continue

            for raw in raws:
                try:
                    outcome = _store(session, raw, campaign_id=campaign.id)
                except Exception as exc:
                    session.rollback()
                    bucket["error"] += 1
                    log.exception(
                        "job_store_failed",
                        source=name,
                        source_job_id=raw.source_job_id,
                        error=str(exc)[:300],
                    )
                    errors.append(
                        {"source": name, "job": raw.source_job_id, "error": str(exc)[:200]}
                    )
                    continue
                bucket[outcome] += 1

    return counts, errors, succeeded


def _window_for(session: Session, incremental_hours: int) -> int:
    """Widen the window when there is nothing to be incremental *from*.

    The 8-hour default is right for a populated database on a 4-hourly
    schedule and actively misleading on an empty one: a first run asks three
    boards what they posted in the last eight hours, stores a handful of ads,
    and reports success. Nothing in the output says the window was the reason,
    so the natural conclusion is that discovery is broken or the boards are
    blocking — which is what happened on the first real machine.

    An explicit ``--hours-old`` always wins; this only fills in the default.
    """
    existing = session.exec(select(func.count()).select_from(Job)).one()
    if existing >= settings.discovery_backfill_threshold:
        return incremental_hours

    log.warning(
        "discovery_backfilling",
        reason="jobs table is below the backfill threshold",
        jobs_in_db=existing,
        threshold=settings.discovery_backfill_threshold,
        incremental_hours=incremental_hours,
        backfill_hours=settings.discovery_backfill_hours,
        note=(
            "widening the window for this run so the first population is not "
            "limited to the incremental window; pass --hours-old to override"
        ),
    )
    return settings.discovery_backfill_hours


def run_discovery(
    *,
    campaign_id: int | None = None,
    hours_old: int | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    sources: list[Source] | None = None,
    session_factory: Callable[[], Any] = session_scope,
) -> Run:
    """Run discovery and record the ``Run`` row. Returns it, detached."""
    started = datetime.now(UTC)
    explicit_window = hours_old is not None
    hours_old = hours_old if explicit_window else settings.discovery_default_hours_old

    with session_factory() as session:
        if not explicit_window:
            hours_old = _window_for(session, hours_old)
        query = select(Campaign).where(Campaign.active == True)
        if campaign_id is not None:
            query = select(Campaign).where(Campaign.id == campaign_id)
        campaigns = list(session.exec(query).all())

        if not campaigns:
            log.warning("no_active_campaigns")

        counts, errors, succeeded = discover(
            session,
            campaigns,
            sources=sources,
            hours_old=hours_old,
            limit=limit,
            dry_run=dry_run,
        )

        # A run is ok only if at least one board actually answered. Previously
        # this was `not errors`, and because each source swallows its own
        # transport failures and returns [], a total outage produced no errors
        # at all: every bucket zero, ok=True, indistinguishable from a quiet
        # day. Both halves are needed — `succeeded` catches the silent outage,
        # `not errors` still catches a board that failed loudly or an ad that
        # would not store.
        ok = bool(succeeded) and not errors
        if not succeeded:
            # Two very different problems, and they need different fixes from
            # the user, so name which one it is rather than logging one vague
            # line for both.
            log.error(
                "discovery_no_source_succeeded",
                attempted=sorted(counts),
                campaigns=len(campaigns),
                reason="no_active_campaign" if not campaigns else "every_source_failed",
                note=(
                    "no campaign is active, so no board was asked anything — "
                    "activate one in the dashboard"
                    if not campaigns
                    else "every board failed; this is an outage, not an empty market"
                ),
            )

        run = Run(
            started_at=started,
            ended_at=datetime.now(UTC),
            phase=RunPhase.DISCOVERY,
            counts={
                "campaigns": len(campaigns),
                "dry_run": dry_run,
                "sources": counts,
                "sources_succeeded": sorted(succeeded),
            },
            errors=errors,
            ok=ok,
        )
        persist_detached(session, run)
        summary = run.model_dump()

    log.info("discovery_complete", **{k: v for k, v in summary.items() if k != "errors"})
    if errors:
        log.error("discovery_had_errors", count=len(errors), errors=errors[:5])
    return run


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI wiring
    parser = argparse.ArgumentParser(prog="python -m backend.discovery.run")
    parser.add_argument("--campaign", type=int, default=None, help="restrict to one campaign id")
    parser.add_argument(
        "--hours-old",
        type=int,
        default=None,
        help="only ads newer than this many hours (incremental runs)",
    )
    parser.add_argument("--limit", type=int, default=None, help="cap ads per source")
    parser.add_argument(
        "--dry-run", action="store_true", help="fetch and report, store nothing"
    )
    args = parser.parse_args(argv)

    configure_logging()
    run = run_discovery(
        campaign_id=args.campaign,
        hours_old=args.hours_old,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    return 0 if run.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
