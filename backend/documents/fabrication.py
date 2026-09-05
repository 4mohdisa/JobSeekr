"""Prove generated narrative invented nothing about the user.

Claude.md hard rule 1: employers, dates, titles, certifications, licences,
visa status, salary and metrics come from the profile verbatim or not at all.
Narrative phrasing is free; facts are locked.

Telling the model not to fabricate is necessary and insufficient. This module
is the part that actually protects the user, because it does not depend on the
model having complied. Everything it checks is a *claim a human would read as
a fact about the candidate*:

* a year the profile never mentions
* an organisation the candidate never worked for
* a metric ("increased revenue 340%") absent from the profile
* a credential word ("certified", "licenced", "clearance") with no backing

A violation regenerates once with the specific problems fed back. A second
violation fails the build loudly. Nothing unvalidated reaches a document.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from backend.logging_setup import get_logger

log = get_logger(__name__)

__all__ = ["Violation", "profile_fact_index", "validate_no_fabrication"]


@dataclass(frozen=True)
class Violation:
    kind: str
    value: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - used in prompts and logs
        return f"{self.kind}: {self.value} ({self.detail})"


_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")

# A metric a reader takes as a factual claim: percentages, money, multipliers,
# durations, and bare counts with a unit.
_METRIC_RE = re.compile(
    r"(\d[\d,]*\.?\d*\s*%"
    r"|[\$£€]\s?\d[\d,]*\.?\d*\s*[kmb]?"
    r"|\b\d[\d,]*\.?\d*\s*(?:x|times)\b"
    r"|\b\d[\d,]*\.?\d*\s*(?:years?|months?)\b"
    r"|\b\d[\d,]*\.?\d*\s*(?:million|billion|thousand)\b)",
    re.IGNORECASE,
)

# Capitalised multi-word tokens that read as an organisation.
_ORG_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9&.\-]*(?:\s+(?:of|for|and|the))?"
    r"(?:\s+[A-Z][A-Za-z0-9&.\-]*){1,4})\b"
)

# Words that assert a credential. Allowed only when the same claim is in the
# profile — "certified" is exactly the kind of word that turns a cover letter
# into a misrepresentation.
_CREDENTIAL_WORDS = (
    "certified",
    "certification",
    "certificate",
    "accredited",
    "licenced",
    "licensed",
    "licence",
    "license",
    "registered",
    "chartered",
    "clearance",
    "vetted",
    "qualified in",
    "degree in",
    "diploma",
    "bachelor",
    "master",
    "phd",
    "doctorate",
    "visa",
    "citizen",
    "permanent resident",
)

# Sentence words that a capitalised run picks up at its edges. Deliberately
# generic English, not domain terms: a technology or skill that appears in the
# profile is already allowed by the per-token check in validate_no_fabrication,
# so listing them here would only mask real fabrications elsewhere.
_ORG_STOPWORDS = {
    "i",
    "my",
    "the",
    "this",
    "that",
    "dear",
    "hiring",
    "team",
    "kind",
    "regards",
    "sincerely",
    "yours",
    "faithfully",
    "hiring team",
    "kind regards",
    "dear hiring",
    "dear hiring team",
    "re",
    "at",
    "in",
    "on",
    "for",
    "with",
    "as",
    "and",
    "of",
    "our",
    "we",
    "available",
    "delivered",
    "built",
    "led",
    "australia",
    "australian",
    "english",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
}


def _trim_org(phrase: str) -> str | None:
    """Strip the sentence words a capitalised run picks up at either end.

    "At Redgum Analytics I built..." matches as "At Redgum Analytics I" — the
    sentence-initial preposition and the pronoun "I" are both capitalised. Left
    in, the phrase never matches the profile and every truthful sentence gets
    flagged. Returns None when nothing organisation-shaped remains.
    """
    tokens = phrase.split()
    while tokens and (len(tokens[0]) < 2 or tokens[0].casefold() in _ORG_STOPWORDS):
        tokens.pop(0)
    while tokens and (len(tokens[-1]) < 2 or tokens[-1].casefold() in _ORG_STOPWORDS):
        tokens.pop()
    # A single capitalised word is too noisy to treat as an organisation claim:
    # it is usually a sentence start, a product, or a technology.
    if len(tokens) < 2:
        return None
    return " ".join(tokens)


def _flatten(value: Any, out: list[str]) -> None:
    if value is None:
        return
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _flatten(item, out)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            _flatten(item, out)
    else:
        out.append(str(value))


def profile_fact_index(profile: Any, job: Any = None) -> str:
    """Every string the profile (and the target job) contains, as one blob.

    Membership in this blob is the definition of "the profile says so". Built
    by flattening rather than by naming fields, so a profile field added later
    is covered automatically instead of silently becoming un-assertable.
    """
    parts: list[str] = []
    for attribute in (
        "identity",
        "work_rights",
        "experience",
        "projects",
        "education",
        "certifications",
        "skills",
        "preferences",
    ):
        _flatten(getattr(profile, attribute, None), parts)

    if job is not None:
        for attribute in ("title", "company", "location", "description"):
            _flatten(getattr(job, attribute, None), parts)

    return re.sub(r"\s+", " ", " ".join(parts)).casefold()


def _forward_looking(text: str, year: str) -> bool:
    """Whether a year is used as a future intention rather than a claim.

    "available from 2027" is not a claim about the candidate's history. Only
    the current and next year qualify, and only next to forward-looking words.
    """
    now = datetime.now(UTC).year
    if int(year) not in (now, now + 1):
        return False
    window = re.search(rf"([^.]*\b{year}\b[^.]*)", text, re.IGNORECASE)
    if not window:
        return False
    sentence = window.group(1).casefold()
    return any(
        word in sentence
        for word in (
            "available",
            "start",
            "commence",
            "from",
            "notice",
            "graduat",
            "expect",
        )
    )


def validate_no_fabrication(
    generated: str,
    profile: Any,
    job: Any = None,
    *,
    extra_allowed: Iterable[str] = (),
) -> list[Violation]:
    """Return every unsupported factual claim in ``generated``.

    Empty list means the passage asserts nothing the profile does not support.
    """
    if not generated or not generated.strip():
        return []

    facts = profile_fact_index(profile, job)
    for allowed in extra_allowed:
        facts += " " + str(allowed).casefold()

    violations: list[Violation] = []

    for match in _YEAR_RE.finditer(generated):
        year = match.group(0)
        if year in facts:
            continue
        if _forward_looking(generated, year):
            continue
        violations.append(Violation("year", year, "not present in the profile"))

    for match in _METRIC_RE.finditer(generated):
        metric = match.group(0).strip()
        compact = re.sub(r"[\s,]", "", metric).casefold()
        digits = re.sub(r"[^\d.]", "", compact)
        if compact in re.sub(r"[\s,]", "", facts) or (digits and digits in facts):
            continue
        violations.append(Violation("metric", metric, "no such figure in the profile"))

    for match in _ORG_RE.finditer(generated):
        phrase = _trim_org(match.group(1))
        if phrase is None:
            continue
        # Allowed when every significant word of the phrase appears somewhere in
        # the profile. Whole-phrase matching is too strict — "Python and SQL"
        # is two real skills that never appear as one contiguous string — while
        # per-token matching still catches "Acme Corporation" and "Stanford
        # University", where the distinctive word is simply absent.
        tokens = [
            token
            for token in re.split(r"[^A-Za-z0-9]+", phrase.casefold())
            if len(token) > 2 and token not in _ORG_STOPWORDS
        ]
        if not tokens or all(token in facts for token in tokens):
            continue
        violations.append(
            Violation(
                "organisation",
                phrase,
                "not an employer, school or issuer in the profile",
            )
        )

    lowered = generated.casefold()
    for word in _CREDENTIAL_WORDS:
        if word not in lowered:
            continue
        if word in facts:
            continue
        violations.append(
            Violation(
                "credential",
                word,
                "credential claim with no matching entry in the profile",
            )
        )

    if violations:
        log.warning(
            "fabrication_detected",
            count=len(violations),
            violations=[str(v) for v in violations][:10],
        )
    return violations
