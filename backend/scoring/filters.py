"""Hard filters, applied before a cent is spent.

Everything here is a pure function returning both the survivors and *why* each
rejection happened. The reason strings end up in the run record and the
dashboard, because "discovery found 200 jobs and scored 3" is only debuggable
if the other 197 can say what dropped them.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from backend.discovery.normalize import canonical_company
from backend.logging_setup import get_logger
from backend.models import Job
from backend.regions import currency_for

log = get_logger(__name__)

__all__ = ["FilterOutcome", "Rejection", "apply_hard_filters"]


@dataclass(frozen=True)
class Rejection:
    job_id: int | None
    reason: str
    detail: str = ""


@dataclass
class FilterOutcome:
    kept: list[Job] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for rejection in self.rejected:
            counts[rejection.reason] = counts.get(rejection.reason, 0) + 1
        return counts


def _excluded_companies(campaign: Any) -> set[str]:
    exclusions = getattr(campaign, "exclusions", None) or {}
    raw = exclusions.get("companies") or []
    return {canonical_company(str(name)) for name in raw if str(name).strip()}


def _excluded_title_words(campaign: Any) -> list[str]:
    exclusions = getattr(campaign, "exclusions", None) or {}
    raw = exclusions.get("title_keywords") or []
    return [str(word).casefold() for word in raw if str(word).strip()]


def _salary_below_floor(
    job: Job,
    floor: int | None,
    *,
    keep_unstated: bool,
    floor_currency: str | None = None,
) -> bool:
    """Whether a job is dropped on salary.

    An ad that states no salary is KEPT by default. Most Australian ads omit
    salary entirely, so dropping the unstated ones would discard the majority
    of the market to enforce a floor that was never tested. The behaviour is a
    campaign setting (``exclusions.drop_unstated_salary``) for users who would
    rather trade recall for precision.

    CURRENCIES ARE NEVER COMPARED. Seek AU and Seek NZ both print a bare "$"
    and neither returns a currency field, so "$81,083" is AUD or NZD depending
    only on which market the ad came from. Comparing across them is not a
    rounding error — at roughly 0.9 NZD to the AUD it silently keeps NZ jobs
    that fall below an AUD floor.

    So a mismatch is treated as "cannot compare", and cannot-compare keeps the
    job: dropping an ad because its currency is unknown would hide real work,
    while keeping it costs one manual look. An unconverted comparison is the
    only outcome ruled out entirely.
    """
    if not floor:
        return False
    if job.salary_max is None and job.salary_min is None:
        return not keep_unstated

    # salary_currency is set by the sources that know it. Where it is absent,
    # job.region gives it — that column is NOT NULL, so a currency is always
    # derivable and the floor never quietly stops filtering. Only a job whose
    # region is genuinely unknowable (a duck-typed object in a test, a future
    # source that sets neither) reaches the None case below.
    job_currency = getattr(job, "salary_currency", None) or currency_for(
        getattr(job, "region", None)
    )

    if floor_currency and job_currency and job_currency != floor_currency:
        log.info(
            "salary_floor_not_comparable",
            job_id=getattr(job, "id", None),
            job_currency=job_currency,
            floor_currency=floor_currency,
            note="different currencies; keeping the job rather than comparing",
        )
        return False

    ceiling = job.salary_max if job.salary_max is not None else job.salary_min
    return ceiling is not None and ceiling < floor


def apply_hard_filters(
    jobs: Iterable[Job],
    campaign: Any,
    *,
    already_applied_job_ids: set[int] | None = None,
) -> FilterOutcome:
    """Drop what cannot possibly be worth scoring. Never raises."""
    outcome = FilterOutcome()
    applied = already_applied_job_ids or set()
    excluded_companies = _excluded_companies(campaign)
    excluded_words = _excluded_title_words(campaign)
    work_types = {
        str(w).casefold() for w in (getattr(campaign, "work_types", None) or [])
    }
    exclusions = getattr(campaign, "exclusions", None) or {}
    keep_unstated = not bool(exclusions.get("drop_unstated_salary", False))

    for job in jobs:
        if job.id is not None and job.id in applied:
            outcome.rejected.append(Rejection(job.id, "already_applied"))
            continue

        if canonical_company(job.company) in excluded_companies:
            outcome.rejected.append(
                Rejection(job.id, "company_excluded", job.company or "")
            )
            continue

        title = (job.title or "").casefold()
        hit = next((word for word in excluded_words if word in title), None)
        if hit:
            outcome.rejected.append(Rejection(job.id, "title_excluded", hit))
            continue

        if _salary_below_floor(
            job,
            getattr(campaign, "salary_floor", None),
            keep_unstated=keep_unstated,
            floor_currency=currency_for(getattr(campaign, "region", None)),
        ):
            outcome.rejected.append(
                Rejection(
                    job.id,
                    "below_salary_floor",
                    f"{job.salary_min}-{job.salary_max} < {campaign.salary_floor}",
                )
            )
            continue

        if work_types:
            raw = (
                (job.raw_work_type or "").casefold()
                if hasattr(job, "raw_work_type")
                else ""
            )
            # Work type is not a first-class column; the ad text is the only
            # evidence available, so this only rejects on an explicit mismatch
            # rather than on absence.
            if raw and raw not in work_types:
                outcome.rejected.append(Rejection(job.id, "work_type_mismatch", raw))
                continue

        outcome.kept.append(job)

    if outcome.rejected:
        log.info(
            "hard_filters_applied",
            campaign=getattr(campaign, "name", None),
            kept=len(outcome.kept),
            rejected=len(outcome.rejected),
            reasons=outcome.summary,
        )
    return outcome
