"""Seek AU and Seek NZ: one platform, two markets, one adapter.

VERIFIED LIVE 2026-09-03, not assumed. The probe:

    www.seek.co.nz  308 ->  nz.seek.com          (mirrors www.seek.com.au -> au.seek.com)
    nz.seek.com/api/jobsearch/v5/search          200, same envelope, jobs under "data"
    siteKey=NZ-Main is the discriminator, NOT the host: au.seek.com with
    siteKey=NZ-Main returns the same NZ results as nz.seek.com does.

That last point is why this is configuration rather than a second source. The
NZ base URL is used anyway — it is the honest one to send, and it is what a
redirect would land on — but the site key is what actually selects the market.

THE CURRENCY TRAP
    Neither market returns a currency field. Both print a bare dollar sign:

        AU  "$75,000 – $90,000 per year"
        NZ  "$81,083 - $110,618 plus 3.5% KiwiSaver"

    Nothing in the payload distinguishes them. A campaign salary floor compared
    against the parsed number is therefore comparing NZD to AUD without knowing
    it — and at roughly 0.9 NZD to the AUD that is a real, silent error in the
    direction of surfacing jobs that pay less than the floor.

    ``locations[].countryCode`` is the reliable per-ad discriminator and is what
    ``currency_for`` reads. It comes from the ad, never from the campaign: a
    campaign searching NZ can still surface an Australian listing, and the ad is
    the authority on where it is.

WORK RIGHTS ARE NOT ONE QUESTION
    "Do you have the right to work in Australia?" and "...in New Zealand?" are
    different questions with independently true answers. There is a reciprocal
    visa arrangement, which makes the wrong guess *plausible* rather than
    obviously absurd — the worst kind. Region-scoped answer rows and the
    abstention in ``answers.py`` exist for exactly this.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.models import Region

__all__ = ["REGIONS", "RegionConfig", "config_for", "currency_for", "region_of_job"]


@dataclass(frozen=True)
class RegionConfig:
    """Everything that differs between the two markets."""

    region: Region
    label: str

    #: Verified live. The www host 308-redirects here.
    base_url: str
    search_url: str
    #: The www host. It 308-redirects to the base host, so this costs one extra
    #: hop and is kept only as the fallback when the direct host misbehaves.
    fallback_search_url: str
    html_search_url: str

    #: The actual market selector. Sending the wrong one returns the wrong
    #: country's jobs from the right host, which is the failure mode that would
    #: otherwise go unnoticed.
    site_key: str
    locale: str

    currency: str
    timezone: str

    #: jobspy's country name for Indeed. LinkedIn is driven by the location
    #: string instead and needs no country parameter.
    jobspy_country: str

    #: Domains belonging to this market, for redirect detection.
    domains: tuple[str, ...]


REGIONS: dict[Region, RegionConfig] = {
    Region.AU: RegionConfig(
        region=Region.AU,
        label="Australia",
        base_url="https://au.seek.com",
        search_url="https://au.seek.com/api/jobsearch/v5/search",
        fallback_search_url="https://www.seek.com.au/api/jobsearch/v5/search",
        html_search_url="https://au.seek.com/jobs",
        site_key="AU-Main",
        locale="en-AU",
        currency="AUD",
        timezone="Australia/Adelaide",
        jobspy_country="Australia",
        domains=("seek.com.au", "au.seek.com"),
    ),
    Region.NZ: RegionConfig(
        region=Region.NZ,
        label="New Zealand",
        base_url="https://nz.seek.com",
        search_url="https://nz.seek.com/api/jobsearch/v5/search",
        fallback_search_url="https://www.seek.co.nz/api/jobsearch/v5/search",
        html_search_url="https://nz.seek.com/jobs",
        site_key="NZ-Main",
        locale="en-NZ",
        currency="NZD",
        # Pacific/Auckland, not a fixed offset. NZ observes DST on a different
        # schedule from South Australia, so the gap between them is 2h or 2h30
        # depending on the month — and the apply window is local time.
        timezone="Pacific/Auckland",
        jobspy_country="New Zealand",
        domains=("seek.co.nz", "nz.seek.com"),
    ),
}


def config_for(region: Region | str | None) -> RegionConfig:
    """The configuration for a region. Defaults to AU."""
    if region is None:
        return REGIONS[Region.AU]
    if isinstance(region, str):
        region = Region(region.upper())
    return REGIONS[region]


def region_of_job(record: dict[str, Any]) -> Region | None:
    """Which market an ad belongs to, from its own countryCode.

    Returns None when the payload does not say. None is not AU: guessing the
    country is guessing the currency, and a wrong currency silently corrupts
    every salary comparison downstream. An unknown region leaves
    ``salary_currency`` NULL, which the floor filter treats as "cannot compare".
    """
    locations = record.get("locations")
    if isinstance(locations, list):
        for entry in locations:
            if not isinstance(entry, dict):
                continue
            code = str(entry.get("countryCode") or "").strip().upper()
            if code in {"AU", "NZ"}:
                return Region(code)

    location = record.get("location")
    if isinstance(location, dict):
        code = str(location.get("countryCode") or "").strip().upper()
        if code in {"AU", "NZ"}:
            return Region(code)

    return None


def currency_for(region: Region | None) -> str | None:
    """The ISO currency for a region, or None when the region is unknown."""
    return REGIONS[region].currency if region is not None else None
