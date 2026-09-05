"""Turn a source's ``RawJob`` into the shape the ``job`` table expects.

Every source disagrees about everything: Seek writes "Adelaide SA 5000",
jobspy writes "Adelaide, South Australia, AU", one gives an ISO timestamp and
another says "3 days ago". This module is the only place allowed to make those
judgement calls, so a source adapter stays a thin transport and the same
company never gets two different canonical names depending on where it was
found.

The canonicalisers here are also what :mod:`backend.discovery.dedupe` hashes.
That is deliberate: dedupe must not re-implement "what counts as the same
company", or the two definitions drift and duplicates start leaking through.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, date, datetime, timedelta
from typing import Any

from bs4 import BeautifulSoup

from backend.base import RawJob
from backend.logging_setup import get_logger
from backend.models import ApplyType, Job

log = get_logger(__name__)

__all__ = [
    "SalaryGuess",
    "canonical_company",
    "canonical_suburb",
    "clean_description",
    "normalize_job",
    "parse_posted_at",
    "parse_salary",
]


# Legal-entity suffixes stripped before comparing company names. "BHP Pty Ltd"
# and "BHP" are one employer; leaving the suffix in means two rows.
_COMPANY_SUFFIXES = (
    "pty ltd",
    "pty. ltd.",
    "pty limited",
    "pty",
    "proprietary limited",
    "limited",
    "ltd",
    "llc",
    "inc",
    "incorporated",
    "corporation",
    "corp",
    "group",
    "holdings",
    "australia",
    "australia and new zealand",
    "anz",
    "aust",
    "au",
    "nz",
)

# Recruiters append these to titles; they are noise for dedupe purposes.
_TITLE_NOISE = re.compile(
    r"\s*[\-–—|(\[]\s*(?:urgent|immediate start|hiring now|new|full[- ]time|part[- ]time"
    r"|casual|contract|permanent|remote|hybrid|wfh|apply now|multiple positions?)\s*[)\]]?\s*",
    re.IGNORECASE,
)

# A trailing " - Adelaide" is the same ad, but a trailing " - Backend" is a
# different role. Only a KNOWN PLACE is stripped, never an arbitrary trailing
# segment — over-stripping here would collapse genuinely different jobs at one
# employer, which is the expensive direction to be wrong in.
_AU_PLACES = (
    "adelaide|melbourne|sydney|brisbane|perth|hobart|darwin|canberra"
    "|gold coast|sunshine coast|newcastle|wollongong|geelong|townsville|cairns"
    "|toowoomba|ballarat|bendigo|launceston|mackay|rockhampton|bunbury"
    "|australian capital territory|new south wales|northern territory|queensland"
    "|south australia|tasmania|victoria|western australia"
    "|act|nsw|nt|qld|sa|tas|vic|wa|australia|aus"
)
_TITLE_LOCATION_TAIL = re.compile(
    rf"\s*[\-–—|(\[,]\s*(?:{_AU_PLACES})(?:\s+(?:cbd|metro|region|area|based))?"
    rf"\s*[)\]]?\s*$",
    re.IGNORECASE,
)

_AU_STATES = {
    "australian capital territory": "ACT",
    "new south wales": "NSW",
    "northern territory": "NT",
    "queensland": "QLD",
    "south australia": "SA",
    "tasmania": "TAS",
    "victoria": "VIC",
    "western australia": "WA",
    "act": "ACT",
    "nsw": "NSW",
    "nt": "NT",
    "qld": "QLD",
    "sa": "SA",
    "tas": "TAS",
    "vic": "VIC",
    "wa": "WA",
}

_WHITESPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")


def _fold(text: str) -> str:
    """Casefold, strip accents and collapse whitespace.

    Accent stripping matters for AU company names carrying European spellings
    ("Nestlé"); without it the same employer hashes two ways.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _WHITESPACE.sub(" ", stripped.casefold()).strip()


def canonical_company(name: str | None) -> str:
    """Reduce a company name to a form safe to compare and hash.

    Drops legal suffixes, punctuation and case. Used by dedupe; changing it
    changes every future ``dedupe_hash``, so treat it as a stable contract.
    """
    if not name:
        return ""
    folded = _fold(name)
    folded = folded.replace("&", " and ")
    folded = _PUNCT.sub(" ", folded)
    folded = _WHITESPACE.sub(" ", folded).strip()

    # Strip suffixes repeatedly: "Acme Australia Pty Ltd" sheds two.
    changed = True
    while changed:
        changed = False
        for suffix in _COMPANY_SUFFIXES:
            if folded.endswith(" " + suffix):
                folded = folded[: -(len(suffix) + 1)].strip()
                changed = True
    return folded


def canonical_suburb(location: str | None) -> str:
    """Reduce a location string to ``suburb state`` in a comparable form.

    Sources spell the same place many ways; only the suburb and state carry
    signal for "is this the same ad".
    """
    if not location:
        return ""

    parts = [p.strip() for p in re.split(r"[,/|]", _fold(location)) if p.strip()]

    # Drop the country, but only as a whole part. Stripping the substring
    # "australia" would eat the state out of "South Australia".
    parts = [
        p for p in parts if _PUNCT.sub("", p).strip() not in {"australia", "au", "aus"}
    ]
    if not parts:
        return ""

    def _clean_part(part: str) -> str:
        return _WHITESPACE.sub(
            " ", re.sub(r"\b\d{4}\b", "", _PUNCT.sub(" ", part))
        ).strip()

    suburb = _clean_part(parts[0])
    state = ""

    # A later part that is wholly a state name: "Adelaide, South Australia".
    for part in parts[1:]:
        token = _clean_part(part)
        if token in _AU_STATES:
            state = _AU_STATES[token]
            break

    if not state:
        # Or the state trails the suburb in one part: "Adelaide SA 5000".
        tokens = suburb.split()
        for i in range(len(tokens) - 1, 0, -1):
            tail = " ".join(tokens[i:])
            if tail in _AU_STATES:
                state = _AU_STATES[tail]
                suburb = " ".join(tokens[:i])
                break

    return f"{suburb} {state}".strip()


def canonical_title(title: str | None) -> str:
    """Reduce a job title for fuzzy comparison, dropping recruiter noise.

    Normalising harder here is what lets the fuzzy threshold stay strict: a
    cross-post that differs only by " - Adelaide" becomes an exact match
    instead of relying on a similarity score loose enough to also catch
    genuinely different roles.
    """
    if not title:
        return ""
    cleaned = _TITLE_LOCATION_TAIL.sub("", title)
    cleaned = _TITLE_NOISE.sub(" ", cleaned)
    cleaned = _fold(cleaned)
    cleaned = _PUNCT.sub(" ", cleaned)
    return _WHITESPACE.sub(" ", cleaned).strip()


def clean_description(raw: str | None) -> str:
    """Strip markup from an ad body while keeping paragraph structure.

    Both scoring and cover-letter generation read this text, so blank lines
    between sections are worth preserving — a wall of text scores worse and
    reads worse.
    """
    if not raw:
        return ""

    text = raw
    if "<" in raw and ">" in raw:
        soup = BeautifulSoup(raw, "lxml")
        for tag in soup(["script", "style"]):
            tag.decompose()
        # Turn block boundaries into newlines before flattening.
        for tag in soup.find_all(
            ["br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"]
        ):
            tag.append("\n")
        text = soup.get_text()

    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\xa0", " ")
    lines = [_WHITESPACE.sub(" ", line).strip() for line in text.splitlines()]

    out: list[str] = []
    for line in lines:
        if line:
            out.append(line)
        elif out and out[-1] != "":
            out.append("")
    return "\n".join(out).strip()


# --------------------------------------------------------------------------
# Salary
# --------------------------------------------------------------------------

_MONEY = r"\$?\s*(\d{1,3}(?:[,\s]\d{3})+|\d+(?:\.\d+)?)\s*([kK])?"
_RANGE_RE = re.compile(
    _MONEY + r"\s*(?:-|–|—|to|and)\s*" + _MONEY,
    re.IGNORECASE,
)
_SINGLE_RE = re.compile(_MONEY)

_HOURLY_HINT = re.compile(
    r"(per\s+hour|/\s*h(?:r|our)?\b|\bp\.?h\.?\b|\bhourly\b|an\s+hour)", re.IGNORECASE
)
_DAILY_HINT = re.compile(r"(per\s+day|/\s*day\b|\bdaily\b|\bper diem\b)", re.IGNORECASE)
_MONTHLY_HINT = re.compile(
    r"(per\s+month|/\s*month\b|\bmonthly\b|\bpcm\b)", re.IGNORECASE
)
_ANNUAL_HINT = re.compile(
    r"(per\s+annum|\bp\.?\s?a\.?\b|annual(?:ly)?|per\s+year|/\s*(?:yr|year)\b"
    r"|\bsalary\s+package\b|\bpackage\b|\bbase\s+salary\b|\+\s*super)",
    re.IGNORECASE,
)

# Australian full-time equivalents, used only to make figures comparable.
_HOURS_PER_YEAR = 38 * 52
_DAYS_PER_YEAR = 260


class SalaryGuess:
    """A salary reading, with the basis it was stated on kept intact.

    ``annual_min``/``annual_max`` are always comparable figures so campaign
    salary floors can filter on one scale. ``basis`` and ``estimated`` say
    whether that figure was actually stated as an annual number or derived
    from an hourly rate — the system must never *claim* an annual salary the
    advertiser did not state, and the dashboard shows the difference.
    """

    __slots__ = ("annual_max", "annual_min", "basis", "estimated", "raw_max", "raw_min")

    def __init__(
        self,
        raw_min: float | None,
        raw_max: float | None,
        basis: str | None,
        estimated: bool,
    ) -> None:
        self.raw_min = raw_min
        self.raw_max = raw_max
        self.basis = basis
        self.estimated = estimated
        factor = {
            "hourly": _HOURS_PER_YEAR,
            "daily": _DAYS_PER_YEAR,
            "monthly": 12,
            "annual": 1,
        }.get(basis or "", 1)
        self.annual_min = round(raw_min * factor) if raw_min is not None else None
        self.annual_max = round(raw_max * factor) if raw_max is not None else None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"SalaryGuess(annual={self.annual_min}-{self.annual_max}, "
            f"basis={self.basis}, estimated={self.estimated})"
        )


def _money(value: str, k_suffix: str | None) -> float | None:
    cleaned = value.replace(",", "").replace(" ", "")
    try:
        amount = float(cleaned)
    except ValueError:
        return None
    if k_suffix:
        amount *= 1000
    # "120" in a salary range means 120k; "60" means $60/hr. Disambiguated by
    # the basis detection below, so only expand the unambiguous case here.
    return amount


def _detect_basis(text: str, amounts: list[float]) -> str:
    """What the advertiser said the figure was per.

    Explicit wording always beats the magnitude heuristic below: "$120 - $140
    per annum" is a six-figure salary written in shorthand, and reading it as
    an hourly rate would put a $237k figure in front of the user.
    """
    if _HOURLY_HINT.search(text):
        return "hourly"
    if _DAILY_HINT.search(text):
        return "daily"
    if _MONTHLY_HINT.search(text):
        return "monthly"
    if _ANNUAL_HINT.search(text):
        return "annual"
    if amounts:
        biggest = max(amounts)
        if biggest < 250:
            return "hourly"
        if biggest < 2000:
            return "daily"
    return "annual"


def parse_salary(text: str | None) -> SalaryGuess:
    """Read a salary out of free text. Never raises; returns empty on failure.

    Handles the forms that actually turn up in Australian ads: ``$120,000 -
    $140,000``, ``120k-140k``, ``$60 per hour``, ``$60/hr``, ``up to $95,000 +
    super``, and bare ``$85,000``.
    """
    if not text:
        return SalaryGuess(None, None, None, False)

    window = text[:400]

    match = _RANGE_RE.search(window)
    if match:
        low = _money(match.group(1), match.group(2))
        high = _money(match.group(3), match.group(4))
        amounts = [a for a in (low, high) if a is not None]
        basis = _detect_basis(window, amounts)
        # "120 - 140" with an annual basis means thousands.
        if basis == "annual":
            low = low * 1000 if low is not None and low < 1000 else low
            high = high * 1000 if high is not None and high < 1000 else high
        if low is not None and high is not None and low > high:
            low, high = high, low
        return SalaryGuess(low, high, basis, estimated=basis != "annual")

    match = _SINGLE_RE.search(window)
    if match:
        amount = _money(match.group(1), match.group(2))
        if amount is None:
            return SalaryGuess(None, None, None, False)
        basis = _detect_basis(window, [amount])
        if basis == "annual" and amount < 1000:
            amount *= 1000
        # "up to $95,000" is a ceiling, "from $95,000" a floor.
        lowered = window.lower()
        if re.search(r"\b(up to|max(?:imum)?|to)\b", lowered[: match.start() + 1]):
            return SalaryGuess(None, amount, basis, estimated=basis != "annual")
        return SalaryGuess(amount, None, basis, estimated=basis != "annual")

    return SalaryGuess(None, None, None, False)


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------

_RELATIVE_RE = re.compile(
    r"(?:(\d+)\+?\s*)?(minute|min|hour|hr|day|week|month|year)s?\s*ago", re.IGNORECASE
)
_UNIT_DELTA = {
    "minute": timedelta(minutes=1),
    "min": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "hr": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
    "month": timedelta(days=30),
    "year": timedelta(days=365),
}


def parse_posted_at(value: Any, *, now: datetime | None = None) -> datetime | None:
    """Coerce whatever a source calls a posting date into aware UTC.

    Accepts datetimes, dates, ISO strings, epoch seconds/milliseconds and the
    relative English ("3 days ago", "just posted") that job boards render.
    Returns None rather than guessing when the value is unreadable — a wrong
    date silently breaks incremental runs.
    """
    if value is None:
        return None
    now = now or datetime.now(UTC)

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)

    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e11:  # milliseconds
            seconds /= 1000
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None

    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    lowered = text.lower()
    if lowered in {"just posted", "today", "just now", "new"}:
        return now
    if lowered == "yesterday":
        return now - timedelta(days=1)

    match = _RELATIVE_RE.search(lowered)
    if match:
        count = int(match.group(1) or 1)
        delta = _UNIT_DELTA.get(match.group(2).lower())
        if delta:
            return now - delta * count

    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d %b %Y", "%d %B %Y"):
            try:
                parsed = datetime.strptime(text, fmt).replace(tzinfo=UTC)
                break
            except ValueError:
                continue
        else:
            log.debug("posted_at_unparsed", value=text[:60])
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def _coerce_apply_type(value: str | None) -> ApplyType:
    if not value:
        return ApplyType.UNKNOWN
    try:
        return ApplyType(value)
    except ValueError:
        log.debug("apply_type_unrecognised", value=value)
        return ApplyType.UNKNOWN


def normalize_job(
    raw: RawJob,
    *,
    campaign_id: int | None = None,
    now: datetime | None = None,
) -> Job:
    """Build an unsaved ``Job`` row from a source's ``RawJob``.

    ``dedupe_hash`` is filled by :mod:`backend.discovery.dedupe` rather than
    here, so there is exactly one implementation of the hash.
    """
    from backend.discovery.contacts import extract_contact_email
    from backend.discovery.dedupe import dedupe_hash

    description = clean_description(raw.description)

    salary = SalaryGuess(raw.salary_min, raw.salary_max, "annual", False)
    if raw.salary_min is None and raw.salary_max is None:
        salary = parse_salary(description[:600] or raw.title)

    contact = raw.ad_contact_email or extract_contact_email(
        description, source_url=raw.url
    )

    return Job(
        source=raw.source,
        source_job_id=str(raw.source_job_id),
        url=raw.url,
        title=(raw.title or "").strip(),
        company=(raw.company or "").strip(),
        location=(raw.location or "").strip() or None,
        description=description or None,
        salary_min=salary.annual_min,
        salary_max=salary.annual_max,
        salary_basis=salary.basis,
        salary_is_estimated=salary.estimated,
        posted_at=parse_posted_at(raw.posted_at, now=now),
        dedupe_hash=dedupe_hash(raw.company, raw.title, raw.location),
        apply_type=_coerce_apply_type(raw.apply_type),
        ad_contact_email=contact,
        campaign_id=campaign_id,
    )
