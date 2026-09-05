"""The one place that knows what a job board is.

Adding a board used to mean editing seven files: the discovery registry, the
login selectors, the applier list, the canary's URLs, the canary's watched
selectors, the apply-window policy, and two hand-maintained lists of "domains
that belong to a platform rather than an employer". Miss one and the failure is
quiet — a board that discovers jobs but never applies to them, or an apply pass
that runs a LinkedIn-strictness policy against Seek.

Now a board is one entry here and one adapter file. Everything below reads from
``BOARDS``; nothing else hardcodes a board name.

The factories import lazily on purpose. Discovery is HTTP only and must never
pull the apply layer — which touches a live browser session — into its import
graph. A module-level ``from backend.apply.seek import SeekApplier`` here would
do exactly that to every discovery run.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "BOARDS",
    "Board",
    "BoardSession",
    "applier_boards",
    "board",
    "is_platform_domain",
    "platform_domains",
    "session_boards",
    "source_boards",
]


@dataclass(frozen=True)
class BoardSession:
    """What session handling needs to know about one board.

    Boards discovered over plain HTTP have no entry: there is no session to
    keep alive, and inventing login selectors for one would imply the agent
    signs in somewhere it does not.
    """

    login_url: str
    home_url: str
    #: Selectors that only exist once signed in. Any one matching is enough.
    logged_in: tuple[str, ...]
    #: Selectors for account-restriction interstitials. Any match halts everything.
    restriction_notice: tuple[str, ...] = ()


@dataclass(frozen=True)
class Board:
    """One job board, end to end."""

    key: str
    """Stable identifier. Written into ``RawJob.source`` and ``Application.platform``."""

    label: str

    domains: tuple[str, ...]
    """Hosts belonging to the board itself. Mail from these is platform
    plumbing, and an address found on one of them is never the employer."""

    weekdays_only: bool = False
    """Whether the apply window is restricted to business days.

    LinkedIn is the account most costly to lose, so it gets the strictest
    pattern. Seek does not.
    """

    session: BoardSession | None = None
    """Absent for boards reached over plain HTTP."""

    canary_url: str | None = None
    """A search-results page the daily canary loads. Results rather than a
    specific job, because job postings expire."""

    canary_selectors: tuple[str, ...] = ()
    """Site-knowledge element keys whose disappearance means the adapter is
    about to fail."""

    make_source: Callable[..., Any] | None = None
    """Builds the discovery Source. Absent when the board is not discovered."""

    make_applier: Callable[[], Any] | None = None
    """Builds the Applier. Absent when applications go through another path."""

    selectors: Callable[[], dict[str, tuple[str, ...]]] | None = None
    """The adapter's selector table, for the canary to sample."""

    har_variants: tuple[tuple[str, str], ...] = ()
    """``(key, description)`` for each flow shape worth capturing to a HAR file.

    These describe the adapter's branches, not the board's identity, but they
    live here so that adding a board stays a one-file change.
    """

    notes: str = field(default="")


# --------------------------------------------------------------------------
# Lazy factories — see the module docstring on why these are not top-level
# imports.
# --------------------------------------------------------------------------


def _seek_source(region: Any = None) -> Any:
    from backend.discovery.seek_source import SeekSource
    from backend.models import Region

    return SeekSource(region=region or Region.AU)


def _jobspy_source(board_key: str, **kwargs: Any) -> Callable[..., Any]:
    def build(region: Any = None) -> Any:
        from backend.discovery.jobspy_source import JobSpySource
        from backend.models import Region

        return JobSpySource(board_key, region=region or Region.AU, **kwargs)

    return build


def _seek_applier() -> Any:
    from backend.apply.seek import SeekApplier

    return SeekApplier()


def _linkedin_applier() -> Any:
    from backend.apply.linkedin import LinkedInApplier

    return LinkedInApplier()


def _knowledge_selectors(platform: str) -> Callable[[], dict[str, tuple[str, ...]]]:
    """Expose a platform's strategies in the shape the canary watches.

    The canary asks "are the elements the adapter depends on still on the
    page?", which is every strategy for a key, flattened. Reading it from site
    knowledge rather than re-listing selectors is the same rule as before —
    re-listing is how the canary came to watch selectors an adapter had already
    renamed — except the source of truth is now a JSON file the user can edit.
    """

    def build() -> dict[str, tuple[str, ...]]:
        from backend.siteknowledge import load

        knowledge = load(platform)
        return {
            key: tuple(strategy.selector for strategy in element.ordered())
            for key, element in knowledge.elements.items()
        }

    return build


def _seek_domains() -> tuple[str, ...]:
    """Every host Seek serves, across both markets, from one source.

    ``backend.regions`` is that source: its hosts were verified against the live
    site, and the redirect behaviour is recorded there. This keeps the registry
    accurate without a second list to forget to update.
    """
    from backend.regions import REGIONS

    hosts: list[str] = []
    for config in REGIONS.values():
        for host in config.domains:
            if host not in hosts:
                hosts.append(host)
    return tuple(hosts)


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------

BOARDS: tuple[Board, ...] = (
    Board(
        key="seek",
        label="Seek",
        # Derived from the region configs rather than restated. regions.py
        # holds the hosts the 2026-09-03 probe verified live — Seek now serves
        # au.seek.com and nz.seek.com — and a second hand-maintained copy here
        # is how the registry came to be missing the host Seek actually uses.
        domains=_seek_domains(),
        weekdays_only=False,
        session=BoardSession(
            login_url="https://www.seek.com.au/oauth/login/",
            home_url="https://www.seek.com.au/",
            logged_in=(
                "[data-automation='profile-menu']",
                "[data-automation='signed-in-nav']",
                "button[data-automation='navigation-account']",
            ),
            restriction_notice=("text=/account has been suspended/i",),
        ),
        canary_url="https://www.seek.com.au/jobs?keywords=developer&where=Adelaide+SA",
        canary_selectors=("apply_button", "submit_button", "confirmation"),
        make_source=_seek_source,
        make_applier=_seek_applier,
        selectors=_knowledge_selectors("seek"),
        har_variants=(
            (
                "quick_apply",
                "Seek Quick Apply: resume upload plus the editable cover-letter textarea.",
            ),
            (
                "screening_step",
                "Seek with screening questions on their own separate step.",
            ),
        ),
        notes="Largest Australian board. Search endpoint is undocumented — see NOTES.md.",
    ),
    Board(
        key="linkedin",
        label="LinkedIn",
        domains=("linkedin.com",),
        weekdays_only=True,
        session=BoardSession(
            login_url="https://www.linkedin.com/login",
            home_url="https://www.linkedin.com/feed/",
            logged_in=(
                "img.global-nav__me-photo",
                "button.global-nav__primary-link-me-menu-trigger",
                "[data-control-name='identity_welcome_message']",
                "div.global-nav__me",
            ),
            restriction_notice=(
                "text=/account has been restricted/i",
                "text=/unusual activity/i",
                "text=/we've restricted your account/i",
                "text=/verify your identity/i",
            ),
        ),
        canary_url=(
            "https://www.linkedin.com/jobs/search/?keywords=developer&location=Adelaide"
        ),
        canary_selectors=(
            "easy_apply_button",
            "modal",
            "submit_button",
            "confirmation",
        ),
        make_source=_jobspy_source("linkedin", easy_apply_only=True),
        make_applier=_linkedin_applier,
        selectors=_knowledge_selectors("linkedin"),
        har_variants=(
            (
                "two_step",
                "Short Easy Apply: contact details then submit. The common case.",
            ),
            (
                "five_step",
                (
                    "Long Easy Apply with screening questions across several steps — "
                    "proves the step loop terminates on Submit rather than a step count."
                ),
            ),
            (
                "with_cover_letter",
                "Two upload slots: resume and cover letter uploaded separately.",
            ),
            ("without_cover_letter", "One upload slot: combined.pdf is used instead."),
            (
                "offsite_redirect",
                "Claims Easy Apply then redirects off-site — must be marked manual_only.",
            ),
        ),
        notes="The account most costly to lose, hence the strictest apply window.",
    ),
    Board(
        key="indeed",
        label="Indeed",
        domains=("indeed.com", "au.indeed.com"),
        make_source=_jobspy_source("indeed"),
        notes="Discovery only. Indeed's apply flow is an external ATS more often than not.",
    ),
)

_BY_KEY: dict[str, Board] = {entry.key: entry for entry in BOARDS}


def board(key: str) -> Board | None:
    return _BY_KEY.get(key)


def source_boards() -> tuple[Board, ...]:
    """Boards jobs are discovered from, in the order discovery runs them."""
    return tuple(entry for entry in BOARDS if entry.make_source is not None)


def applier_boards() -> tuple[Board, ...]:
    """Boards with their own apply flow, in the order the apply pass tries them."""
    return tuple(entry for entry in BOARDS if entry.make_applier is not None)


def session_boards() -> tuple[Board, ...]:
    """Boards that hold a browser session a human has to establish."""
    return tuple(entry for entry in BOARDS if entry.session is not None)


# --------------------------------------------------------------------------
# Platform domains
# --------------------------------------------------------------------------

# Boards and vendors we recognise but have no adapter for. Kept separate from
# BOARDS and from ATS_REGISTRY because both of those imply "we can act here",
# and these only mean "mail from this address is not the employer".
_UNADAPTED_PLATFORM_DOMAINS: tuple[str, ...] = (
    "glassdoor.com",
    "ziprecruiter.com",
    "taleo.net",
    "icims.com",
    "jobvite.com",
    "applytojob.com",
)


def platform_domains() -> tuple[str, ...]:
    """Every host that belongs to a job board or an ATS rather than an employer.

    Assembled from the two registries that already hold this data, because the
    hand-maintained copies of it drifted: the contact scraper knew about
    BambooHR and Glassdoor and the reply matcher did not, so the same address
    was platform plumbing in one module and a real employer in the other.
    """
    from backend.ats.detect import ATS_REGISTRY

    domains: list[str] = []
    for entry in BOARDS:
        domains.extend(entry.domains)
    for platform in ATS_REGISTRY:
        # Some host patterns carry a path ("docs.google.com/forms"); the domain
        # is the part before the first slash.
        domains.extend(pattern.split("/", 1)[0] for pattern in platform.host_patterns)
    domains.extend(_UNADAPTED_PLATFORM_DOMAINS)
    return tuple(dict.fromkeys(domains))


def is_platform_domain(domain: str) -> bool:
    """Whether a host is platform plumbing, matching subdomains too.

    One implementation because it was written twice, identically, in the
    contact scraper and the reply matcher.
    """
    domain = (domain or "").strip().casefold().rstrip(".")
    if not domain:
        return False
    return any(
        domain == known or domain.endswith("." + known) for known in platform_domains()
    )
