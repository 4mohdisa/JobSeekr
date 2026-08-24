"""Work out which ATS a careers page actually is.

The priority order here is **Australian**, not the US default every scraping
guide assumes:

1. **JobAdder** — the leading AU/NZ-native ATS, and the one most small and
   mid-size Australian employers actually use.
2. **PageUp** — an Australian company; government, universities and large
   enterprise run on it.
3. **SmartRecruiters** — common in AU mid-market.
4. **Greenhouse / Lever** — tech-heavy, over-represented in US guidance and
   under-represented in the Australian market outside startups.
5. **Workday LAST.** Not because it is rare but because it demands a separate
   account per company, which makes it the most expensive flow to automate and
   the least worth doing first.

Plus the form builders. A great many "bespoke" careers pages are a Google Form,
Typeform or JotForm in an iframe, and recognising that is far cheaper than
treating the page as unknown.

URL patterns are checked first because they are unambiguous and free. The HTML
fingerprint is the fallback for white-labelled deployments served from the
employer's own domain, which is exactly how PageUp and JobAdder usually appear.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from backend.logging_setup import get_logger

log = get_logger(__name__)

__all__ = ["ATS_REGISTRY", "AtsPlatform", "detect", "detect_from_html", "detect_from_url"]


@dataclass(frozen=True)
class AtsPlatform:
    key: str
    label: str
    #: Lower is tried first. Australian-market weighting, see the module docstring.
    priority: int
    #: Host suffixes that identify this platform unambiguously.
    host_patterns: tuple[str, ...] = ()
    #: Path fragments that identify it when the host is the employer's own.
    path_patterns: tuple[str, ...] = ()
    #: Markers in the page source. Used only when the URL is inconclusive.
    html_markers: tuple[str, ...] = ()
    #: True when applying requires creating an account with that employer.
    requires_account: bool = False
    notes: str = ""


ATS_REGISTRY: tuple[AtsPlatform, ...] = (
    AtsPlatform(
        key="jobadder",
        label="JobAdder",
        priority=1,
        host_patterns=("jobadder.com", "job-adder.com"),
        path_patterns=("/jobadder/",),
        html_markers=("jobadder", "ja-apply", "data-jobadder"),
        notes="Leading AU/NZ-native ATS. Usually white-labelled onto the employer's domain.",
    ),
    AtsPlatform(
        key="pageup",
        label="PageUp",
        priority=2,
        host_patterns=("pageuppeople.com", "pageup.com.au", "dc2.pageuppeople.com"),
        path_patterns=("/caw/en/job/", "/psp/", "?pipelineid="),
        html_markers=("pageup", "pageuppeople", "PageUpPeople"),
        notes="Australian. Government, universities, large enterprise.",
    ),
    AtsPlatform(
        key="smartrecruiters",
        label="SmartRecruiters",
        priority=3,
        host_patterns=("smartrecruiters.com", "smartapply.io"),
        html_markers=("smartrecruiters", "sr-application"),
    ),
    AtsPlatform(
        key="greenhouse",
        label="Greenhouse",
        priority=4,
        host_patterns=("greenhouse.io", "boards.greenhouse.io", "job-boards.greenhouse.io"),
        html_markers=("greenhouse.io", "grnhse", "gh_jid"),
    ),
    AtsPlatform(
        key="lever",
        label="Lever",
        priority=4,
        host_patterns=("lever.co", "jobs.lever.co"),
        html_markers=("lever.co", "lever-application", "postings-btn"),
    ),
    AtsPlatform(
        key="google_forms",
        label="Google Forms",
        priority=5,
        host_patterns=("docs.google.com/forms", "forms.gle"),
        html_markers=("docs.google.com/forms", "freebirdFormviewer"),
        notes="Often embedded in an iframe on an otherwise bespoke careers page.",
    ),
    AtsPlatform(
        key="typeform",
        label="Typeform",
        priority=5,
        host_patterns=("typeform.com",),
        html_markers=("typeform", "tf-v1-widget"),
    ),
    AtsPlatform(
        key="jotform",
        label="JotForm",
        priority=5,
        host_patterns=("jotform.com", "form.jotform.com"),
        html_markers=("jotform", "jotform-form"),
    ),
    AtsPlatform(
        key="workable",
        label="Workable",
        priority=6,
        host_patterns=("workable.com", "apply.workable.com"),
        html_markers=("workable", "whr-"),
    ),
    AtsPlatform(
        key="bamboohr",
        label="BambooHR",
        priority=6,
        host_patterns=("bamboohr.com", "bamboohr.co.uk"),
        html_markers=("bamboohr",),
    ),
    AtsPlatform(
        key="recruitee",
        label="Recruitee",
        priority=6,
        host_patterns=("recruitee.com",),
        html_markers=("recruitee",),
    ),
    AtsPlatform(
        key="workday",
        label="Workday",
        priority=9,  # last, deliberately
        host_patterns=("myworkdayjobs.com", "workday.com", "wd1.myworkdaysite.com"),
        path_patterns=("/wday/",),
        html_markers=("workday", "wd-browser", "data-automation-id"),
        requires_account=True,
        notes=(
            "Requires an account per employer, so it is the most expensive flow to "
            "automate and the least worth doing first."
        ),
    ),
)

_BY_KEY = {platform.key: platform for platform in ATS_REGISTRY}


@dataclass
class Detection:
    platform: AtsPlatform | None
    confidence: str  # url | html | none
    evidence: str = ""
    iframe_src: str | None = None
    signals: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.platform.key if self.platform else "unknown"


def detect_from_url(url: str) -> Detection:
    """Identify the platform from the URL alone. Free and unambiguous."""
    if not url:
        return Detection(platform=None, confidence="none")

    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower()
    full = url.lower()

    for platform in sorted(ATS_REGISTRY, key=lambda p: p.priority):
        for pattern in platform.host_patterns:
            if "/" in pattern:
                if pattern in full:
                    return Detection(platform, "url", f"url contains {pattern}")
            elif host == pattern or host.endswith("." + pattern):
                return Detection(platform, "url", f"host matches {pattern}")

        for pattern in platform.path_patterns:
            if pattern in full:
                return Detection(platform, "url", f"path contains {pattern}")

    return Detection(platform=None, confidence="none")


_IFRAME = re.compile(r"<iframe[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)


def detect_from_html(html: str) -> Detection:
    """Identify a white-labelled deployment from the page source.

    The common Australian case: PageUp or JobAdder served from
    ``careers.employer.com.au`` with nothing in the URL to give it away.
    """
    if not html:
        return Detection(platform=None, confidence="none")

    lowered = html.lower()

    # An embedded form builder is the platform, whatever the wrapper page is.
    for match in _IFRAME.finditer(html):
        nested = detect_from_url(match.group(1))
        if nested.platform is not None:
            nested.confidence = "html"
            nested.iframe_src = match.group(1)
            nested.evidence = f"iframe to {nested.platform.label}"
            return nested

    scored: list[tuple[int, AtsPlatform, list[str]]] = []
    for platform in ATS_REGISTRY:
        hits = [marker for marker in platform.html_markers if marker.lower() in lowered]
        if hits:
            scored.append((len(hits), platform, hits))

    if not scored:
        return Detection(platform=None, confidence="none")

    # Most markers wins; ties break on the Australian priority order.
    scored.sort(key=lambda item: (-item[0], item[1].priority))
    count, platform, hits = scored[0]
    return Detection(
        platform,
        "html",
        f"{count} marker(s): {', '.join(hits[:3])}",
        signals=hits,
    )


def detect(url: str, html: str | None = None) -> Detection:
    """URL first, then the page source. Returns unknown rather than guessing."""
    result = detect_from_url(url)
    if result.platform is not None:
        log.debug("ats_detected", platform=result.key, via="url", evidence=result.evidence)
        return result

    if html:
        result = detect_from_html(html)
        if result.platform is not None:
            log.info("ats_detected", platform=result.key, via="html", evidence=result.evidence)
            return result

    log.info("ats_unknown", url=url[:120])
    return Detection(platform=None, confidence="none")


def platform_by_key(key: str) -> AtsPlatform | None:
    return _BY_KEY.get(key)
