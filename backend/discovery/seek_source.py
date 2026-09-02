"""Seek (seek.com.au) discovery.

WHAT IS AND IS NOT VERIFIED
---------------------------
Seek publishes no documented job-search API. The endpoint this module targets
by default is the one its own web front end calls, but it was **not verifiable
from the machine this code was written on**: ``www.seek.com.au`` is blocked
there by network policy, so no request could be made and no traffic inspected.

Rather than hard-code an unverified guess and hope, this module is built so the
guess cannot silently rot:

1. Every part of the request — URL, site key, source system, locale, page size
   — is a setting, correctable in ``.env`` without a code change.
2. ``python -m backend.discovery.verify_seek`` probes each variant from the
   user's own machine and prints the exact ``.env`` lines to paste.
3. Three strategies are tried in order, so one contract change degrades rather
   than kills discovery:
       JSON search API  ->  server-rendered page state  ->  JSON-LD JobPosting
4. Field reading is tolerant: unknown payload shapes are logged once and
   skipped, never raised.

Discovery is HTTP only — this module must never drive a browser (Claude.md).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from bs4 import BeautifulSoup

from backend.base import RawJob, SourceUnavailable
from backend.config import settings
from backend.discovery.http import build_client, get_with_retry
from backend.logging_setup import get_logger

log = get_logger(__name__)

__all__ = ["SeekSource", "parse_json_payload", "parse_jsonld", "parse_page_state"]

_SOURCE = "seek"

# Where the job array lives, most specific first. Dotted, so one list covers
# both the JSON API (``data``) and the page-state blob (``results.results.jobs``).
_RECORD_PATHS: tuple[str, ...] = (
    "data",
    "results.results.jobs",
    "results.jobs",
    "jobs",
    "results",
    "items",
    "hits",
    "searchResults.data",
    "searchResults.jobs",
    "searchResults.results",
    "props.data",
    "props.jobs",
)

# Tolerant field lookup: the same value has been seen under several spellings
# across Seek's payload versions, so each field names every plausible key.
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "jobId", "solMetadata.jobId", "listingId"),
    "title": ("title", "jobTitle", "name"),
    "company": (
        "advertiser.description",
        "advertiser.name",
        "companyName",
        "company.name",
        "company",
        "hiringOrganization.name",
    ),
    "location": (
        # What the live jobsearch/v5 payload actually uses: a list of location
        # objects. Verified 2026-09-01 — without this every job came back with
        # location None, because none of the singular spellings below matched.
        "locations.0.label",
        "location",
        "locationLabel",
        "jobLocation.label",
        "area",
        "suburb",
    ),
    "url": ("url", "jobUrl", "shareLink"),
    "description": (
        "teaser",
        "abstract",
        "jobDescription",
        "content",
        "description",
    ),
    "posted": ("listingDate", "datePosted", "postedDate", "createdAt"),
    "salary": ("salary", "salaryLabel", "compensation", "baseSalary"),
    "apply": ("isQuickApply", "quickApply", "applyType", "applicationType"),
}


def _dig(payload: dict[str, Any], path: str) -> Any:
    """Read a dotted path without raising on a missing or wrongly typed level.

    A numeric segment indexes a list, so ``locations.0.label`` reaches into the
    list-of-objects shape Seek actually returns.
    """
    node: Any = payload
    for part in path.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list):
            if not part.isdigit():
                return None
            index = int(part)
            node = node[index] if index < len(node) else None
        else:
            return None
        if node is None:
            return None
    return node


def _first(payload: dict[str, Any], field: str) -> Any:
    for path in _FIELD_ALIASES[field]:
        value = _dig(payload, path)
        if value not in (None, "", []):
            return value
    return None


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("label", "name", "description", "text", "value"):
            inner = value.get(key)
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
        return None
    return str(value)


def _apply_type(value: Any) -> str:
    """Seek's quick-apply signal, read conservatively.

    An ad is only called ``quick_apply`` when the payload actually says so.
    Guessing here would send the apply engine down a flow the ad does not
    support, so anything unclear stays ``unknown`` and the apply layer confirms.
    """
    if value is True:
        return "quick_apply"
    if isinstance(value, str):
        lowered = value.casefold()
        if "quick" in lowered:
            return "quick_apply"
        if "external" in lowered or "redirect" in lowered:
            return "external"
    return "unknown"


def _record_to_rawjob(record: dict[str, Any]) -> RawJob | None:
    """Map one Seek record. Returns None (with a log) rather than raising."""
    job_id = _as_text(_first(record, "id"))
    title = _as_text(_first(record, "title"))
    if not job_id or not title:
        log.debug("seek_record_unmappable", keys=sorted(record)[:12])
        return None

    # Search records carry no url of their own — it is built from the id. Uses
    # the configured base so it points at the live host directly instead of
    # taking a redirect on every fetch.
    base = settings.seek_base_url.rstrip("/")
    url = _as_text(_first(record, "url")) or f"{base}/job/{job_id}"
    if url.startswith("/"):
        url = f"{base}{url}"

    description = _as_text(_first(record, "description"))
    # The teaser is one sentence. bulletPoints carries the actual requirements
    # and is the only substantive text a search record has, so scoring gets a
    # far better signal when both are kept.
    bullets = record.get("bulletPoints")
    if isinstance(bullets, list):
        lines = [b.strip() for b in bullets if isinstance(b, str) and b.strip()]
        if lines:
            joined = "\n".join(f"- {line}" for line in lines)
            description = f"{description}\n\n{joined}" if description else joined

    salary_text = _as_text(_first(record, "salary"))
    if salary_text and description:
        # Keep the salary label with the body so normalise can read it.
        description = f"{description}\n\n{salary_text}"
    elif salary_text:
        description = salary_text

    return RawJob(
        source=_SOURCE,
        source_job_id=job_id,
        url=url,
        title=title,
        company=_as_text(_first(record, "company")) or "Unknown",
        location=_as_text(_first(record, "location")),
        description=description,
        posted_at=None,  # normalise parses whatever shape this is
        apply_type=_apply_type(_first(record, "apply")),
        raw={**record, "_posted_raw": _first(record, "posted")},
    )


# --------------------------------------------------------------------------
# Strategy 1 — the JSON search endpoint
# --------------------------------------------------------------------------


def parse_json_payload(payload: dict[str, Any]) -> list[RawJob]:
    """Pull records out of a search response without assuming the envelope.

    One list of dotted paths rather than two hand-rolled loops, so the JSON API
    and the page-state blob are read by the same code. ``results.results.jobs``
    is the redux store the search page embeds: its ``results`` key is a *dict*,
    which the previous top-level-only scan skipped, so the HTML fallback logged
    "shape unknown" and recovered nothing.
    """
    records: Iterable[Any] | None = None
    matched_empty = False
    for path in _RECORD_PATHS:
        candidate = _dig(payload, path)
        if not isinstance(candidate, list):
            continue
        if candidate:
            records = candidate
            break
        # A real search that matched nothing. Keep looking in case another
        # path holds the real list, but do not report the shape as unknown.
        matched_empty = True

    if records is None:
        if not matched_empty:
            log.warning("seek_payload_shape_unknown", top_level_keys=sorted(payload)[:15])
        return []

    out: list[RawJob] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        mapped = _record_to_rawjob(record)
        if mapped is not None:
            out.append(mapped)
    return out


# --------------------------------------------------------------------------
# Strategy 2 — server-rendered page state
# --------------------------------------------------------------------------

_STATE_PATTERNS = (
    re.compile(r"window\.SEEK_REDUX_DATA\s*=\s*(\{.*?\});", re.DOTALL),
    re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});", re.DOTALL),
    re.compile(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>', re.DOTALL | re.IGNORECASE
    ),
)


def parse_page_state(html: str) -> list[RawJob]:
    """Recover listings from the state blob a server-rendered page embeds."""
    for pattern in _STATE_PATTERNS:
        match = pattern.search(html)
        if not match:
            continue
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            log.debug("seek_state_blob_unparseable", pattern=pattern.pattern[:40])
            continue
        jobs = parse_json_payload(payload)
        if jobs:
            return jobs
    return []


# --------------------------------------------------------------------------
# Strategy 3 — JSON-LD
# --------------------------------------------------------------------------


def parse_jsonld(html: str) -> list[RawJob]:
    """Recover listings from schema.org JobPosting blocks.

    The thinnest of the three (JSON-LD rarely carries the full body) but it is
    a stable, documented standard, which makes it the right last resort.
    """
    soup = BeautifulSoup(html, "lxml")
    out: list[RawJob] = []

    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            payload = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        blocks = payload if isinstance(payload, list) else [payload]
        for block in blocks:
            if not isinstance(block, dict) or block.get("@type") != "JobPosting":
                continue
            url = block.get("url") or ""
            identifier = block.get("identifier")
            job_id = ""
            if isinstance(identifier, dict):
                job_id = str(identifier.get("value") or "")
            job_id = job_id or re.sub(r"\D", "", url)[-12:] or url
            if not job_id or not block.get("title"):
                continue

            hiring = block.get("hiringOrganization") or {}
            location = block.get("jobLocation") or {}
            if isinstance(location, list):
                location = location[0] if location else {}
            address = (location or {}).get("address") or {}

            out.append(
                RawJob(
                    source=_SOURCE,
                    source_job_id=str(job_id),
                    url=url or f"{settings.seek_base_url.rstrip('/')}/job/{job_id}",
                    title=str(block["title"]),
                    company=str((hiring or {}).get("name") or "Unknown"),
                    location=" ".join(
                        str(part)
                        for part in (
                            address.get("addressLocality"),
                            address.get("addressRegion"),
                        )
                        if part
                    )
                    or None,
                    description=block.get("description"),
                    posted_at=None,
                    apply_type="unknown",
                    raw={**block, "_posted_raw": block.get("datePosted")},
                )
            )
    return out


# --------------------------------------------------------------------------
# The source
# --------------------------------------------------------------------------


class SeekSource:
    """Implements :class:`backend.base.Source` for seek.com.au."""

    name = _SOURCE

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    # -- request builders ---------------------------------------------------

    def _json_params(
        self, *, term: str, where: str, page: int, hours_old: int | None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "siteKey": settings.seek_site_key,
            "sourcesystem": settings.seek_source_system,
            "keywords": term,
            "page": page,
            "pageSize": settings.seek_page_size,
            "locale": settings.seek_locale,
        }
        if where:
            params["where"] = where
        if hours_old:
            # Seek's own filter is coarse; the client-side cut below is what
            # actually enforces the window.
            params["daterange"] = max(1, round(hours_old / 24))
        return params

    def _fetch_json(
        self, client: httpx.Client, url: str, params: dict[str, Any]
    ) -> list[RawJob] | None:
        """Return jobs, or None when this endpoint is not usable at all."""
        try:
            response = get_with_retry(client, url, params=params)
        except httpx.HTTPError as exc:
            log.warning("seek_json_transport_failed", url=url, error=str(exc)[:200])
            return None

        if response.status_code != 200:
            log.warning("seek_json_status", url=url, status=response.status_code)
            return None
        try:
            payload = response.json()
        except ValueError:
            log.warning("seek_json_not_json", url=url)
            return None
        if not isinstance(payload, dict):
            return None
        return parse_json_payload(payload)

    def _fetch_html(
        self, client: httpx.Client, *, term: str, where: str, page: int
    ) -> list[RawJob] | None:
        """Return jobs, or None when the page could not be fetched at all.

        None and ``[]`` mean different things and the caller depends on the
        difference: None is "Seek never answered", ``[]`` is "Seek answered and
        there was nothing on the page". Collapsing them is what made a blocked
        endpoint look like an empty result set.
        """
        params = {"keywords": term, "page": page}
        if where:
            params["where"] = where
        try:
            response = get_with_retry(
                client,
                settings.seek_html_search_url,
                params=params,
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
        except httpx.HTTPError as exc:
            log.warning("seek_html_transport_failed", error=str(exc)[:200])
            return None
        if response.status_code != 200:
            log.warning("seek_html_status", status=response.status_code)
            return None

        jobs = parse_page_state(response.text)
        if jobs:
            log.info("seek_recovered_via_page_state", count=len(jobs))
            return jobs
        jobs = parse_jsonld(response.text)
        if jobs:
            log.info("seek_recovered_via_jsonld", count=len(jobs))
        return jobs

    # -- Source protocol ----------------------------------------------------

    def search(
        self,
        *,
        terms: list[str],
        locations: list[str],
        hours_old: int | None = None,
        limit: int | None = None,
    ) -> list[RawJob]:
        """Search Seek. Best effort: partial results beat an exception."""
        client = self._client or build_client()
        collected: dict[str, RawJob] = {}
        attempts = 0
        unreachable = 0
        cutoff = (
            datetime.now(UTC) - timedelta(hours=hours_old) if hours_old else None
        )

        try:
            for term in terms or [""]:
                for where in locations or [""]:
                    for page in range(1, settings.discovery_max_pages + 1):
                        if limit and len(collected) >= limit:
                            break

                        params = self._json_params(
                            term=term, where=where, page=page, hours_old=hours_old
                        )
                        jobs = self._fetch_json(client, settings.seek_search_url, params)
                        if jobs is None:
                            jobs = self._fetch_json(
                                client, settings.seek_search_url_fallback, params
                            )
                        if jobs is None:
                            jobs = self._fetch_html(
                                client, term=term, where=where, page=page
                            )

                        attempts += 1
                        if jobs is None:
                            # All three endpoints refused. Not an empty page —
                            # no page at all.
                            unreachable += 1
                            break
                        if not jobs:
                            break

                        for job in jobs:
                            collected.setdefault(job.source_job_id, job)

                        if page < settings.discovery_max_pages:
                            time.sleep(settings.discovery_request_delay_seconds)
        finally:
            if self._owns_client:
                client.close()

        # Nothing answered and nothing was fetched; see SourceUnavailable.
        if attempts and unreachable == attempts and not collected:
            raise SourceUnavailable(
                f"seek: {attempts} search(es), every endpoint unreachable"
            )

        results = list(collected.values())
        if cutoff is not None:
            results = [j for j in results if _within(j, cutoff)]
        if limit:
            results = results[:limit]

        log.info("seek_search_complete", terms=terms, locations=locations, found=len(results))
        return results


def _within(job: RawJob, cutoff: datetime) -> bool:
    """Client-side recency filter, tolerant of an unparseable date.

    A job whose date cannot be read is KEPT: dropping it would silently lose
    real ads whenever the payload changes shape.
    """
    from backend.discovery.normalize import parse_posted_at

    posted = parse_posted_at(job.raw.get("_posted_raw"))
    return posted is None or posted >= cutoff
