"""LinkedIn and Indeed discovery via python-jobspy.

One class serves both boards: they differ in configuration, not in shape, and
two near-identical classes would be the duplication Claude.md forbids.

Everything here is coded against what jobspy **actually returns**, verified by
inspecting the installed package (1.1.82) rather than assuming:

* ``scrape_jobs`` returns a ``pandas.DataFrame``, not objects, and the column
  set varies by board — Indeed fills ``company_industry``, LinkedIn often does
  not. Missing values arrive as ``NaN``, not ``None``, which is why every read
  goes through :func:`_cell`.
* ``job_url_direct`` is the employer's own application URL when the board knows
  it. On Indeed that is exactly the "this ad redirects off-site" signal.
* ``emails`` already carries addresses jobspy found in the body — reused rather
  than re-extracted.
* There is no "is easy apply" column. LinkedIn exposes ``easy_apply`` only as a
  *search filter*, so the only honest way to know is to have asked for it; see
  :attr:`JobSpySource.easy_apply_only`.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlparse

from backend.base import RawJob, SourceUnavailable
from backend.config import settings
from backend.logging_setup import get_logger

log = get_logger(__name__)

__all__ = ["JobSpySource", "dataframe_to_rawjobs"]

# Hosts that mean "Indeed is sending you somewhere else to apply".
_INDEED_HOSTS = ("indeed.com", "au.indeed.com")
_LINKEDIN_HOSTS = ("linkedin.com", "www.linkedin.com")


def _cell(row: Any, column: str) -> Any:
    """Read a DataFrame cell defensively.

    Returns None for a missing column, a NaN, or a blank string. jobspy's
    column set is not stable across boards or versions, so a KeyError here
    would mean one thin listing kills a whole discovery run.
    """
    try:
        value = row[column]
    except (KeyError, IndexError, TypeError):
        return None
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _text(row: Any, column: str) -> str | None:
    value = _cell(row, column)
    return str(value).strip() if value is not None else None


def _number(row: Any, column: str) -> float | None:
    value = _cell(row, column)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _host(url: str | None) -> str:
    if not url:
        return ""
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def _apply_type(row: Any, *, site: str, easy_apply_only: bool) -> str:
    """Classify how the ad is applied to, without over-claiming.

    Indeed: a ``job_url_direct`` pointing off Indeed is a redirect to the
    employer's own system, which the apply engine must treat as external.

    LinkedIn: there is no per-row Easy Apply flag in this version. Claiming
    ``easy_apply`` without evidence would send the apply engine into a modal
    that is not there, so it is only claimed when the search itself was
    filtered to Easy Apply listings. Otherwise the apply layer confirms.
    """
    direct = _text(row, "job_url_direct")

    if site == "indeed":
        direct_host = _host(direct)
        if direct_host and not any(
            direct_host == h or direct_host.endswith("." + h) for h in _INDEED_HOSTS
        ):
            return "external"
        return "unknown"

    if site == "linkedin":
        direct_host = _host(direct)
        if direct_host and not any(
            direct_host == h or direct_host.endswith("." + h) for h in _LINKEDIN_HOSTS
        ):
            return "external"
        if easy_apply_only:
            return "easy_apply"
        return "unknown"

    return "unknown"


def _salary(row: Any) -> tuple[float | None, float | None, str | None]:
    """Read jobspy's parsed compensation, keeping the stated interval."""
    low = _number(row, "min_amount")
    high = _number(row, "max_amount")
    interval = _text(row, "interval")
    basis = {
        "yearly": "annual",
        "annual": "annual",
        "monthly": "monthly",
        "weekly": "weekly",
        "daily": "daily",
        "hourly": "hourly",
    }.get((interval or "").lower())
    return low, high, basis


def dataframe_to_rawjobs(
    frame: Any, *, site: str, easy_apply_only: bool = False
) -> list[RawJob]:
    """Convert a jobspy DataFrame into RawJobs, skipping anything unusable."""
    out: list[RawJob] = []
    if frame is None or getattr(frame, "empty", True):
        return out

    for _, row in frame.iterrows():
        title = _text(row, "title")
        url = _text(row, "job_url")
        if not title or not url:
            log.debug("jobspy_row_unmappable", site=site)
            continue

        job_id = _text(row, "id") or url
        low, high, basis = _salary(row)

        emails = _cell(row, "emails")
        contact: str | None = None
        if isinstance(emails, str):
            contact = emails.split(",")[0].strip() or None
        elif isinstance(emails, (list, tuple)) and emails:
            contact = str(emails[0]).strip() or None

        out.append(
            RawJob(
                source=site,
                source_job_id=str(job_id),
                url=url,
                title=title,
                company=_text(row, "company") or "Unknown",
                location=_text(row, "location"),
                description=_text(row, "description"),
                salary_min=low,
                salary_max=high,
                posted_at=_cell(row, "date_posted"),
                apply_type=_apply_type(row, site=site, easy_apply_only=easy_apply_only),
                ad_contact_email=contact,
                raw={
                    "site": site,
                    "job_url_direct": _text(row, "job_url_direct"),
                    "is_remote": _cell(row, "is_remote"),
                    "job_type": _text(row, "job_type"),
                    "salary_interval": basis,
                    "listing_type": _text(row, "listing_type"),
                },
            )
        )
    return out


@contextmanager
def _watch_jobspy_errors() -> Iterator[list[str]]:
    """Collect the errors jobspy logs instead of raising.

    Its scrapers do not agree with each other about failure. Indeed lets a
    transport error propagate, so the caller sees it. **LinkedIn catches its
    own, logs it, and returns an empty frame** — so a blocked LinkedIn is
    indistinguishable from a LinkedIn with no matching ads, one layer below
    anywhere this module can reach. Its log is the only signal there is, and an
    outage reported as a quiet day is precisely what this is here to stop.

    Reading another library's log is not lovely, and it is scoped as tightly as
    possible: only for the duration of the call, and only for records jobspy
    itself emitted at ERROR. If jobspy ever starts raising from LinkedIn, the
    ``except`` around the call catches it first and this finds nothing — the two
    paths agree rather than double-count.

    Hooking the record *factory* rather than adding a handler is deliberate.
    jobspy's loggers set ``propagate = False``, so a root handler never sees
    them, and attaching to them by name only works if they already exist — true
    after ``import jobspy``, but an ordering dependency that would fail silently
    the day it stopped holding, restoring the very blind spot this closes. Every
    record passes through the factory whichever logger makes it and whenever it
    is created, including from jobspy's own worker threads.
    """
    captured: list[str] = []
    previous = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        # Matched case-insensitively: jobspy is inconsistent with itself,
        # logging under both "JobSpy:LinkedIn" and "JobSpy:Linkedin".
        if record.levelno >= logging.ERROR and record.name.lower().startswith("jobspy"):
            try:
                captured.append(record.getMessage()[:300])
            except Exception:  # noqa: BLE001 - a bad log line is not our failure
                captured.append(str(record.msg)[:300])
        return record

    logging.setLogRecordFactory(factory)
    try:
        yield captured
    finally:
        logging.setLogRecordFactory(previous)


class JobSpySource:
    """Implements :class:`backend.base.Source` for one jobspy-backed board."""

    def __init__(
        self,
        site: str,
        *,
        easy_apply_only: bool = False,
        fetch_description: bool = True,
    ) -> None:
        self.name = site
        self.site = site
        self.easy_apply_only = easy_apply_only
        self.fetch_description = fetch_description

    def search(
        self,
        *,
        terms: list[str],
        locations: list[str],
        hours_old: int | None = None,
        limit: int | None = None,
    ) -> list[RawJob]:
        """Search one board. Best effort: partial results beat an exception."""
        from jobspy import scrape_jobs  # imported lazily; pulls in pandas

        wanted = limit or settings.seek_page_size * settings.discovery_max_pages
        collected: dict[str, RawJob] = {}
        attempts = 0
        failures = 0

        for term in terms or [""]:
            for location in locations or [""]:
                kwargs: dict[str, Any] = {
                    "site_name": [self.site],
                    "search_term": term,
                    "location": location or None,
                    "results_wanted": wanted,
                    # Australia — jobspy defaults to the US and silently
                    # returns the wrong market otherwise.
                    "country_indeed": "Australia",
                    "description_format": "markdown",
                    "verbose": 0,
                }
                if hours_old:
                    kwargs["hours_old"] = hours_old
                if self.site == "linkedin":
                    kwargs["linkedin_fetch_description"] = self.fetch_description
                    if self.easy_apply_only:
                        kwargs["easy_apply"] = True

                attempts += 1
                try:
                    with _watch_jobspy_errors() as reported:
                        frame = scrape_jobs(**kwargs)
                except Exception as exc:
                    failures += 1
                    log.exception(
                        "jobspy_search_failed",
                        site=self.site,
                        term=term,
                        location=location,
                        error=str(exc)[:300],
                    )
                    continue

                rows = list(
                    dataframe_to_rawjobs(
                        frame, site=self.site, easy_apply_only=self.easy_apply_only
                    )
                )
                # Nothing back *and* the scraper logged an error: it failed and
                # returned empty rather than raising. Ads plus an error is a
                # partial result and stays a success.
                if not rows and reported:
                    failures += 1
                    log.error(
                        "jobspy_search_failed_silently",
                        site=self.site,
                        term=term,
                        location=location,
                        errors=reported[:2],
                    )
                    continue

                for job in rows:
                    collected.setdefault(job.source_job_id, job)

        # Every query failed and nothing came back. Returning [] here is what
        # let a dead board report a quiet day; see SourceUnavailable.
        if attempts and failures == attempts and not collected:
            raise SourceUnavailable(
                f"{self.site}: all {attempts} search(es) failed and nothing was fetched"
            )

        results = list(collected.values())
        if limit:
            results = results[:limit]
        log.info(
            "jobspy_search_complete", site=self.site, terms=terms, found=len(results)
        )
        return results
