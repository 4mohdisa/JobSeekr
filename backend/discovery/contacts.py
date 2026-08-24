"""Read a contact address out of an ad the advertiser wrote.

LEGAL BOUNDARY — read before extending this module.

The Australian Spam Act 2003 prohibits sending unsolicited commercial
electronic messages and, separately, prohibits *address-harvesting software*
and harvested-address lists (Schedule 1). This module exists only to read an
address the advertiser **chose to publish in their own job ad**, which is a
conspicuous publication carrying an implied invitation to apply.

That boundary is what makes the outbound-email path defensible, so:

* Never extend this to crawl company websites, WHOIS, or social profiles.
* Never guess address patterns (``first.last@company.com.au``) — a guessed
  address was never published, and a bounce is not the worst outcome; sending
  to an unrelated person is.
* Never accumulate addresses into a list for reuse across jobs.

If a future change would break any of those, it is out of scope for this
project, not a feature to be implemented carefully.
"""

from __future__ import annotations

import re

from backend.logging_setup import get_logger

log = get_logger(__name__)

__all__ = ["extract_contact_email"]


# Written to also catch the light obfuscation real ads use, because an ad
# saying "careers (at) acme.com.au" published that address just as plainly.
_AT = r"(?:@|\s*[\(\[\{]\s*(?:at|@)\s*[\)\]\}]\s*|\s+at\s+)"
_DOT = r"(?:\.|\s*[\(\[\{]\s*dot\s*[\)\]\}]\s*|\s+dot\s+)"
_LOCAL = r"[A-Za-z0-9._%+\-]{1,64}"
_LABEL = r"[A-Za-z0-9\-]{1,63}"

_EMAIL_RE = re.compile(
    rf"(?<![A-Za-z0-9._%+\-])({_LOCAL}){_AT}({_LABEL}(?:{_DOT}{_LABEL})+)(?![A-Za-z0-9\-])",
    re.IGNORECASE,
)

# Platform plumbing, not the employer. Writing to these reaches a robot at
# best and an unrelated ATS vendor at worst.
_BLOCKED_DOMAINS = (
    "seek.com.au",
    "linkedin.com",
    "indeed.com",
    "au.indeed.com",
    "glassdoor.com",
    "ziprecruiter.com",
    "pageuppeople.com",
    "jobadder.com",
    "smartrecruiters.com",
    "greenhouse.io",
    "lever.co",
    "myworkdayjobs.com",
    "workday.com",
    "bamboohr.com",
    "applytojob.com",
    "recruitee.com",
    "workable.com",
    "jobvite.com",
    "taleo.net",
    "icims.com",
    "example.com",
    "sentry.io",
    "wixpress.com",
)

_BLOCKED_LOCALS = (
    "no-reply",
    "noreply",
    "donotreply",
    "do-not-reply",
    "postmaster",
    "mailer-daemon",
    "abuse",
    "privacy",
    "unsubscribe",
    "webmaster",
    "support@wix",
)

# Image and asset filenames survive HTML stripping and look email-shaped.
_ASSET_TAIL = re.compile(r"\.(png|jpe?g|gif|svg|webp|css|js|woff2?|ico)$", re.IGNORECASE)

_VALID_TLD = re.compile(r"\.[A-Za-z]{2,}$")


def _clean(local: str, domain: str) -> str | None:
    """Undo the obfuscation and return a plain address, or None if implausible."""
    domain = re.sub(r"\s*[\(\[\{]\s*dot\s*[\)\]\}]\s*", ".", domain, flags=re.IGNORECASE)
    domain = re.sub(r"\s+dot\s+", ".", domain, flags=re.IGNORECASE)
    domain = re.sub(r"\s+", "", domain).strip(".").lower()
    local = re.sub(r"\s+", "", local).strip(".").lower()

    if not local or not domain or ".." in domain:
        return None
    if not _VALID_TLD.search(domain):
        return None
    if _ASSET_TAIL.search(domain):
        return None
    return f"{local}@{domain}"


def _is_usable(address: str, *, source_host: str | None) -> bool:
    local, _, domain = address.partition("@")

    if any(domain == blocked or domain.endswith("." + blocked) for blocked in _BLOCKED_DOMAINS):
        return False
    if any(local.startswith(blocked) for blocked in _BLOCKED_LOCALS):
        return False
    # An address on the board's own domain is the board, not the employer.
    return not (
        source_host and (domain == source_host or source_host.endswith("." + domain))
    )


def extract_contact_email(description: str | None, *, source_url: str | None = None) -> str | None:
    """Return the employer address published in the ad body, or None.

    Returns None whenever the answer is not obvious. An unmatched ad costs the
    user one optional outbound draft; a wrongly matched one sends their resume
    to a stranger.
    """
    if not description:
        return None

    source_host = None
    if source_url:
        match = re.search(r"https?://([^/]+)", source_url, re.IGNORECASE)
        if match:
            source_host = match.group(1).lower().removeprefix("www.")

    for match in _EMAIL_RE.finditer(description):
        address = _clean(match.group(1), match.group(2))
        if address and _is_usable(address, source_host=source_host):
            return address

    return None
