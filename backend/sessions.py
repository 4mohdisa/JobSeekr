"""Per-site session health. The silent failure, made loud.

THE FAILURE THIS EXISTS FOR
    A session expires. The adapter navigates, lands on a login page, cannot
    find the form, and parks the job. Nothing in that sequence says "you are
    signed out of PageUp" — so the symptom is a pile of parked jobs several days
    later and no stated cause. This checks before any application is attempted,
    and names the specific site when it fails.

HOW A DEAD SESSION IS RECOGNISED
    Primarily by a password field. That is deliberate and it is the reason this
    needs almost no per-site configuration: every login page on the internet has
    one, and "a password input is visible" is a far more robust signal than "a
    signed-in element is present" — the latter needs a selector per site and
    breaks whenever a site reshuffles its header.

    Site knowledge refines it where it exists. A platform can declare a
    ``logged_in`` element and that is taken as positive confirmation, but no
    platform is required to.

THREE OUTCOMES, NOT TWO
    LIVE, DEAD, and UNKNOWN. A page showing neither a login form nor a
    recognised signed-in element tells us nothing, and reporting that as DEAD
    would page the user about a healthy session every time a site changes its
    markup. UNKNOWN is recorded and shown; it does not alert.

NEVER RE-LOGIN
    Hard rule 8. This module has no credential parameters and never navigates a
    login form. It reports; the user signs in themselves, in a visible browser.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import Session, select

from backend.logging_setup import get_logger
from backend.models import SessionHealth, SessionStatus

log = get_logger(__name__)

__all__ = [
    "LOGIN_MARKERS",
    "SiteCheck",
    "check_all",
    "check_site",
    "digest_lines",
    "known_sites",
    "on_session_dead",
    "record",
    "sites_with_cookies",
    "stale_sites",
]


# Set by the integrations layer, same convention as canary.on_drift.
on_session_dead: Any = None


LOGIN_MARKERS: tuple[str, ...] = (
    # A password field is the signal. Present on every login page, absent from
    # every signed-in page, and it needs no per-site selector at all.
    "input[type='password']",
    # Some sites split email and password across two steps, so the first screen
    # has no password field. These cover that first screen.
    "form[action*='login' i] input[type='email']",
    "form[action*='signin' i] input[type='email']",
)
"""Generic evidence that this is a login page rather than site knowledge.

Kept here rather than in ``data/siteknowledge`` on purpose: this is a fact about
how login pages are built, not a fact about any one site, and nine copies in
nine JSON files would be nine things to fix.
"""


@dataclass
class SiteCheck:
    """The result of checking one site."""

    site: str
    status: SessionStatus
    detail: str = ""
    cookie_count: int = 0

    @property
    def ok(self) -> bool:
        return self.status is SessionStatus.LIVE


def known_sites() -> dict[str, str]:
    """Site key -> a URL to check it at, for every site we know about.

    Boards come from the registry, which already holds a home URL for the two
    with a session concept. ATS platforms have no such URL, so they are checked
    at whatever domain their cookies are stored under — which is the honest
    answer and needs no new configuration.
    """
    from backend.boards import BOARDS

    sites: dict[str, str] = {}
    for entry in BOARDS:
        if entry.session is not None:
            sites[entry.key] = entry.session.home_url
    return sites


def sites_with_cookies(context: Any) -> dict[str, int]:
    """Cookie domain -> cookie count, for the stored browser profile.

    The user signs in to each ATS themselves, so which sites have a session is
    not something this code can know in advance — the cookie jar is the only
    record of it. Reading the jar means a site the user logged into last week is
    checked without anyone adding it to a list.
    """
    try:
        cookies = context.cookies()
    except Exception as exc:  # noqa: BLE001 - no context is not an error here
        log.warning("cookie_read_failed", error=str(exc)[:200])
        return {}

    counts: dict[str, int] = {}
    for cookie in cookies:
        domain = str(cookie.get("domain") or "").lstrip(".")
        if not domain:
            continue
        # The FULL host, not a registrable domain. Collapsing
        # careers.acme.com.au to acme.com.au would send the check to the
        # employer's marketing site, which has no login page and would report a
        # dead careers portal as healthy. Merging the several hosts one platform
        # serves is _site_for_domain's job, and it does it by platform key
        # rather than by guessing at suffix rules.
        counts[domain] = counts.get(domain, 0) + 1
    return counts


def _site_for_domain(domain: str) -> str:
    """Map a cookie domain onto a platform key where one matches.

    Reads the board registry, which now derives Seek's hosts from the region
    configs — the first version of this consulted both lists from here and
    named "seek" to do it, which is exactly the hardcoding
    test_no_module_outside_the_registry_names_a_job_board forbids. Fixing it at
    the registry removed the duplication rather than working around it.
    """
    from backend.boards import BOARDS

    def matches(host: str) -> bool:
        return (
            domain == host or domain.endswith("." + host) or host.endswith("." + domain)
        )

    for entry in BOARDS:
        if any(matches(host) for host in entry.domains):
            return entry.key

    # The ATS registry already knows how to name a platform from a URL, and
    # detect_from_url is what every adapter uses. Reusing it means a cookie
    # domain is named by the same logic that names a job URL, rather than by a
    # second matcher reading a field that turned out not to exist — the first
    # version of this read `platform.domains`, which is spelled `host_patterns`,
    # so it silently matched nothing and every ATS looked like an unknown site.
    from backend.ats.detect import detect_from_url

    detection = detect_from_url(f"https://{domain}/")
    if detection.key and detection.key != "unknown":
        return detection.key
    return domain


def check_site(page: Any, site: str, url: str) -> SiteCheck:
    """Check one site. Navigates, looks, reports. Never signs in.

    A login page wins over a signed-in element: if both somehow match, the safe
    reading is that we are being asked to log in.
    """
    try:
        page.goto(url, wait_until="domcontentloaded")
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "session_check_unreachable", site=site, url=url, error=str(exc)[:200]
        )
        return SiteCheck(site, SessionStatus.UNREACHABLE, f"could not load {url}")

    for selector in LOGIN_MARKERS:
        try:
            if page.locator(selector).first.is_visible(timeout=2000):
                log.warning(
                    "session_dead",
                    site=site,
                    url=url,
                    marker=selector,
                    note="a login page is showing; the stored session has expired",
                )
                return SiteCheck(
                    site, SessionStatus.DEAD, f"a login page is showing at {url}"
                )
        except Exception as exc:  # noqa: BLE001 - absence is the normal case
            log.debug(
                "login_marker_absent",
                site=site,
                selector=selector,
                error=str(exc)[:100],
            )

    # Positive confirmation, where the site declares one. Optional by design:
    # the password check above already carries the load.
    if _logged_in_marker_present(page, site):
        return SiteCheck(site, SessionStatus.LIVE, "signed-in element found")

    log.info(
        "session_status_unknown",
        site=site,
        url=url,
        note="no login page and no recognised signed-in element",
    )
    return SiteCheck(
        site,
        SessionStatus.UNKNOWN,
        "no login page, but nothing confirmed signed in either",
    )


def _logged_in_marker_present(page: Any, site: str) -> bool:
    """Whether a site-specific signed-in element is visible.

    Two sources, checked in order, because the two predate each other: the board
    registry already holds these for Seek and LinkedIn (session checking and
    restriction detection both read them), and site knowledge is where anything
    added since goes. Neither is required.
    """
    from backend.apply.session import PLATFORMS, _any_visible

    board_session = PLATFORMS.get(site)
    if board_session is not None and _any_visible(page, board_session.logged_in):
        return True

    try:
        from backend.siteknowledge import load

        knowledge = load(site)
    except Exception:  # noqa: BLE001 - no knowledge file for this site
        return False

    element = knowledge.elements.get("logged_in")
    if element is None:
        return False
    return knowledge.present(page, "logged_in", timeout_ms=2000)


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------


def record(session: Session, check: SiteCheck) -> SessionHealth:
    """Store a check result. Alerts on a session that has just gone dead.

    Alerts on the TRANSITION, not on every dead check. A session stays dead
    until the user signs in, and paging them on every pass about a thing they
    already know is how the channel stops being read.
    """
    now = datetime.now(UTC)
    row = session.exec(
        select(SessionHealth).where(SessionHealth.site == check.site)
    ).first()

    was_dead = row is not None and row.status is SessionStatus.DEAD

    if row is None:
        row = SessionHealth(site=check.site, status=check.status)

    row.status = check.status
    row.detail = check.detail or None
    row.cookie_count = check.cookie_count
    row.last_checked_at = now

    if check.status is SessionStatus.LIVE:
        row.last_verified_at = now
        row.consecutive_failures = 0
    elif check.status is SessionStatus.DEAD:
        row.consecutive_failures += 1
    session.add(row)

    if check.status is SessionStatus.DEAD and not was_dead:
        log.error("session_expired_alert", site=check.site, detail=check.detail)
        if on_session_dead is not None:
            try:
                on_session_dead(check.site, check.detail, row.last_verified_at)
            except Exception as exc:  # noqa: BLE001 - alerting must not abort
                log.warning("session_alert_failed", error=str(exc)[:150])

    return row


def check_all(session: Session, context: Any, page: Any) -> list[SiteCheck]:
    """Check every site with stored cookies, plus every known board.

    The union on purpose. Cookies alone would miss a board the user has never
    signed into — which is worth reporting as NO_SESSION rather than silently
    omitting, because "LinkedIn is missing from this list" is not something
    anyone notices.
    """
    cookie_counts = sites_with_cookies(context)
    by_site: dict[str, int] = {}
    for domain, count in cookie_counts.items():
        site = _site_for_domain(domain)
        by_site[site] = by_site.get(site, 0) + count

    urls = known_sites()
    for domain in cookie_counts:
        site = _site_for_domain(domain)
        urls.setdefault(site, f"https://{domain}/")

    results: list[SiteCheck] = []
    for site, url in sorted(urls.items()):
        count = by_site.get(site, 0)
        if count == 0:
            # Nothing has ever signed in here. Not a failure, and not worth
            # loading a page to confirm.
            check = SiteCheck(
                site, SessionStatus.NO_SESSION, "no stored cookies", cookie_count=0
            )
        else:
            check = check_site(page, site, url)
            check.cookie_count = count

        record(session, check)
        results.append(check)

    live = sum(1 for c in results if c.status is SessionStatus.LIVE)
    dead = [c.site for c in results if c.status is SessionStatus.DEAD]
    log.info(
        "session_check_complete",
        checked=len(results),
        live=live,
        dead=dead,
        unknown=sum(1 for c in results if c.status is SessionStatus.UNKNOWN),
    )
    return results


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def stale_sites(session: Session, *, hours: int = 48) -> list[SessionHealth]:
    """Sites not confirmed good recently. Dead ones and long-unverified ones."""
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    stale: list[SessionHealth] = []
    for row in session.exec(select(SessionHealth)).all():
        if row.status is SessionStatus.DEAD:
            stale.append(row)
            continue
        if row.status is SessionStatus.NO_SESSION:
            continue
        verified = row.last_verified_at
        if verified is None or _aware(verified) < cutoff:
            stale.append(row)
    return stale


def _aware(moment: datetime) -> datetime:
    """SQLite hands back naive datetimes; the schema stores UTC."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def digest_lines(session: Session) -> list[str]:
    """Session lines for the evening digest. Empty when everything is fine.

    Empty rather than "all sessions healthy": a line that appears every evening
    saying nothing is a line people stop reading, and this one needs to be read
    on the evening it is not empty.
    """
    stale = stale_sites(session)
    if not stale:
        return []

    lines = ["\n*Sessions needing attention*"]
    for row in sorted(stale, key=lambda r: r.site):
        if row.status is SessionStatus.DEAD:
            lines.append(
                f"· *{row.site}* — signed out. "
                f"`uv run python -m backend.apply.session login --platform {row.site}`"
            )
        else:
            last = (
                f"last confirmed {_aware(row.last_verified_at):%d %b}"
                if row.last_verified_at
                else "never confirmed"
            )
            lines.append(f"· {row.site} — {row.status.value}, {last}")
    return lines
