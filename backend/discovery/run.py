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
from sqlmodel import Session, select

from backend.base import RawJob, Source
from backend.boards import source_boards
from backend.config import settings
from backend.db import session_scope
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
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run discovery for the given campaigns. Returns (counts, errors)."""
    sources = sources if sources is not None else build_sources()
    counts: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []

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

            raws = _search_safely(
                source,
                terms=terms,
                locations=locations,
                hours_old=hours_old,
                limit=limit,
                errors=errors,
            )
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

    return counts, errors


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
    hours_old = hours_old if hours_old is not None else settings.discovery_default_hours_old

    with session_factory() as session:
        query = select(Campaign).where(Campaign.active == True)
        if campaign_id is not None:
            query = select(Campaign).where(Campaign.id == campaign_id)
        campaigns = list(session.exec(query).all())

        if not campaigns:
            log.warning("no_active_campaigns")

        counts, errors = discover(
            session,
            campaigns,
            sources=sources,
            hours_old=hours_old,
            limit=limit,
            dry_run=dry_run,
        )

        run = Run(
            started_at=started,
            ended_at=datetime.now(UTC),
            phase=RunPhase.DISCOVERY,
            counts={"campaigns": len(campaigns), "dry_run": dry_run, "sources": counts},
            errors=errors,
            ok=not errors,
        )
        session.add(run)
        session.flush()
        session.refresh(run)
        summary = run.model_dump()
        # Detach the loaded row before the scope commits. session_scope commits
        # on exit, and a commit expires every instance it still tracks, so the
        # caller's first attribute read (`run.ok` in main) would fire a lazy
        # load against a closed session and raise DetachedInstanceError. The
        # INSERT is already flushed, so expunging changes nothing that is
        # persisted — it only stops SQLAlchemy expiring the values we just read.
        session.expunge(run)

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
