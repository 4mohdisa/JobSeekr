"""Recognise the same job seen twice.

Two different problems, two different mechanisms:

* The *same ad on the same board* is handled by ``UNIQUE(source,
  source_job_id)`` in the schema — no code needed.
* The *same role cross-posted* to Seek and LinkedIn under different ids is
  what this module is for. A stable hash catches the identical spellings; a
  fuzzy title match catches "Senior Python Developer" vs "Senior Python
  Developer - Adelaide".

The fuzzy pass is scoped to one canonical company **on purpose**. Titles are
not distinctive: half the market advertises "Software Engineer", and matching
those across employers would collapse unrelated jobs into one row and silently
cost the user applications. Company scope makes a false positive require both
the same employer and a near-identical title, which is what a genuine
cross-post actually looks like.
"""

from __future__ import annotations

import hashlib

from rapidfuzz import fuzz
from sqlmodel import Session, select

from backend.discovery.normalize import (
    canonical_company,
    canonical_suburb,
    canonical_title,
)
from backend.logging_setup import get_logger
from backend.models import Job

log = get_logger(__name__)

__all__ = ["FUZZY_TITLE_THRESHOLD", "dedupe_hash", "find_duplicate"]


FUZZY_TITLE_THRESHOLD = 90.0
"""Percent similarity above which two titles at one company are the same role.

The spec calls for >0.9. rapidfuzz scores 0-100, so this is that number in the
library's units. Strict on purpose: a missed duplicate costs one wasted score,
a false duplicate costs a real application that never gets sent.
"""


def dedupe_hash(company: str | None, title: str | None, suburb: str | None) -> str:
    """Stable identity for a role, independent of which board it came from.

    Hashes the *canonical* triple, so "Acme Pty Ltd" in "Adelaide SA 5000"
    and "Acme" in "Adelaide, South Australia" produce one hash.

    Changing this function invalidates every stored hash, so treat the output
    as a persisted format rather than an implementation detail.
    """
    parts = (
        canonical_company(company),
        canonical_title(title),
        canonical_suburb(suburb),
    )
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def find_duplicate(session: Session, job: Job) -> Job | None:
    """Return an already-stored job that is the same role as ``job``.

    Exact hash first (cheap, indexed), then a fuzzy title pass within the same
    canonical company. Never mutates anything.
    """
    exact = session.exec(
        select(Job).where(Job.dedupe_hash == job.dedupe_hash, Job.id != job.id)
    ).first()
    if exact is not None:
        return exact

    company_key = canonical_company(job.company)
    if not company_key:
        return None

    incoming_title = canonical_title(job.title)
    if not incoming_title:
        return None

    # Only rows that could plausibly match: same canonical company. Company is
    # not indexed in canonical form, so filter in Python over the candidates
    # sharing the raw company string first, then fall back to a scan scoped by
    # the first word — enough for a single-user database.
    candidates = session.exec(
        select(Job).where(Job.company.is_not(None), Job.id != job.id)  # type: ignore[union-attr]
    ).all()

    for candidate in candidates:
        if canonical_company(candidate.company) != company_key:
            continue
        score = fuzz.token_sort_ratio(incoming_title, canonical_title(candidate.title))
        if score > FUZZY_TITLE_THRESHOLD:
            log.debug(
                "fuzzy_duplicate",
                incoming=job.title,
                existing=candidate.title,
                company=job.company,
                score=round(score, 1),
            )
            return candidate
    return None
