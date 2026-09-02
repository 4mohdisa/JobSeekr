"""The parse gate. Claude.md hard rule 3: nothing attaches unless this passes.

The premise is that a PDF which *looks* right to a human can be unreadable to
the ATS that actually decides whether the application is seen. Every check here
corresponds to a real, silent failure mode:

1. no text layer          — an image-only PDF is a blank resume to a parser
2. extractor disagreement — fragile extraction that will differ in their stack
3. ligature corruption    — "efficient" extracting as "e cient" loses keywords
4. name/section order     — a parser reads top-down; order is the structure
5. missing sections       — no EXPERIENCE heading, no parsed work history
6. missing contact        — the resume that cannot be replied to
7. page limits            — length rules are real screening criteria
8. missing claimed keywords — the generator said it matched; prove it survived
9. two-column layout      — passes every other check and still parses as mush

A corrupt or malformed PDF produces a FAILED REPORT, never an exception. The
caller's job is to record the failure loudly and stop; it should not have to
wrap this in a try/except to do that.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pdfplumber
import pypdf
from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from backend.logging_setup import get_logger

log = get_logger(__name__)

__all__ = ["CheckResult", "ParseExpectations", "ParseReport", "verify_pdf"]


MIN_TEXT_CHARS = 200
EXTRACTOR_AGREEMENT_THRESHOLD = 90.0
NAME_WITHIN_CHARS = 200

PAGE_LIMITS = {"resume": 2, "cover_letter": 1, "combined": 3}

# Words whose extraction proves the fi/fl ligature handling is intact. These
# are checked only when they appear in the source document — and only in the
# part of it that actually typesets. See _strip_latex_comments.
LIGATURE_CANARIES = (
    "efficient",
    "financial",
    "certification",
    "identify",
    "workflow",
    "qualified",
)

STANDARD_SECTIONS = ("experience", "education", "skills")


class CheckResult(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class ParseExpectations(BaseModel):
    """What the gate should be able to find in the extracted text."""

    name: str | None = None
    email: str | None = None
    phone: str | None = None
    employers: list[str] = Field(default_factory=list)
    claimed_keywords: list[str] = Field(default_factory=list)

    verbatim: list[str] = Field(default_factory=list)
    """Facts that must survive extraction EXACTLY as the profile states them.

    This exists because ``claimed_keywords`` was a hand-picked list, and a
    hand-picked list only catches what whoever wrote it happened to think of.
    A resume once shipped with the skill "C#" typeset as the literal text
    "C\\#" — the PDF looked right and this gate passed, because the keywords
    supplied that day were Python, SQL and FastAPI, none of which contain a
    character the escaper could corrupt.

    ``expected_verbatim`` harvests these from the profile automatically, so
    coverage no longer depends on anybody remembering.
    """

    section_order: list[str] = Field(default_factory=list)
    source_text: str | None = None
    """The rendered source, used to know which canary words to expect."""


class ParseReport(BaseModel):
    passed: bool
    kind: str
    path: str
    pages: int = 0
    extracted_chars: int = 0
    extracted_text: str = ""
    """Exactly what came out of the PDF — what an ATS reads.

    Kept, not just counted. Downstream needs the cover letter's text to paste
    into a textarea and to check the letter is real, and re-extracting it later
    would risk reading a different file than the one that passed the gate.
    """
    checks: list[CheckResult] = Field(default_factory=list)

    @property
    def failures(self) -> list[CheckResult]:
        return [check for check in self.checks if not check.passed]

    def summary(self) -> str:
        if self.passed:
            return f"{self.kind} passed all {len(self.checks)} checks"
        return f"{self.kind} FAILED: " + "; ".join(
            f"{c.name} ({c.detail})" for c in self.failures
        )


# A LaTeX comment runs from an unescaped % to end of line and never reaches the
# page. Harvesting a canary from one asks the gate to find a word that no
# correct build could contain: the shipped resume template explains the
# ligature rule in a comment that contains the word "efficient", so every
# profile that did not happen to use that word failed no_ligature_corruption.
# The suite missed it because its fixture profile was written around the canary
# list ("Efficient financial reporting...") rather than the other way round.
_LATEX_COMMENT = re.compile(r"(?<!\\)%.*")

_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October"
    "|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec"
)

# Dates as any template is likely to render them: "01 September 2026",
# "1 Sep 2026", "2026-09-01", "01/09/2026".
#
# The year is OPTIONAL after a month name, because the contact line can wrap:
# a slightly longer address pushes "2026" onto the next extracted line, leaving
# "... Adelaide SA 01 September" behind. That is just as corrupt a location
# field, and requiring the full date silently missed it.
#
# Month names are spelled out rather than matched as [A-Z][a-z]+, which would
# make "12 Regent Street" — a legitimate contact line — look like a date. A
# bare year is likewise not matched: "Adelaide SA 5000" postcodes and company
# names carrying years are normal contact-line content.
_DATE_ON_CONTACT_LINE = re.compile(
    rf"\b(?:\d{{1,2}}\s+(?:{_MONTHS})\b"
    rf"|(?:{_MONTHS})\s+\d{{1,2}}\b"
    rf"|\d{{4}}-\d{{2}}-\d{{2}}"
    rf"|\d{{1,2}}/\d{{1,2}}/\d{{2,4}})\b"
)


def _strip_latex_comments(source: str) -> str:
    """Drop the parts of a .tex that will never appear in the PDF."""
    return _LATEX_COMMENT.sub("", source)


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().casefold()


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def _extract_pypdf(path: Path) -> tuple[str, int, str | None]:
    try:
        reader = pypdf.PdfReader(str(path))
        pages = len(reader.pages)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        return text, pages, None
    except Exception as exc:  # noqa: BLE001 - a broken PDF is a report, not a crash
        return "", 0, f"{type(exc).__name__}: {exc}"[:200]


def _extract_pdfplumber(path: Path) -> tuple[str, list[list[dict[str, Any]]], str | None]:
    try:
        words_per_page: list[list[dict[str, Any]]] = []
        chunks: list[str] = []
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages:
                chunks.append(page.extract_text() or "")
                words_per_page.append(page.extract_words() or [])
        return "\n".join(chunks), words_per_page, None
    except Exception as exc:  # noqa: BLE001
        return "", [], f"{type(exc).__name__}: {exc}"[:200]


MIN_GUTTER_POINTS = 8.0
"""How wide an empty vertical band must be to count as a column gutter.

Calibrated against real output rather than guessed. LaTeX's default
``\\columnsep`` is 10pt, so a two-column document's gutter measures ~9pt in the
extracted word extents — a 24pt threshold chosen for "looks like a column gap"
missed it entirely. Measured on the test fixtures: a clean single-column
resume's widest empty band is 3pt (the ragged edge around right-aligned dates),
a two-column one is 9pt. 8pt sits between them with room on both sides.
"""

GUTTER_SIDE_SHARE = 0.25
"""Fraction of the page's words that must sit each side of a candidate gutter.

Stops a single wide gap in an otherwise normal document from reading as a
column break: a real two-column layout puts roughly half the text on each side.
"""


def _detect_two_columns(words_per_page: list[list[dict[str, Any]]]) -> tuple[bool, str]:
    """Detect a two-column layout, which parses as interleaved nonsense.

    A two-column resume can pass every other check here and still be unreadable
    to an ATS, because extractors read straight across the page and splice the
    columns together line by line.

    The signature is a *vertical gutter*: a band of the page that no word
    overlaps, running the full height, with substantial text on both sides.
    Detection works on word extents rather than word start positions — wrapping
    scatters the starts across each column, but nothing ever intrudes into a
    real gutter.

    A single-column resume with right-aligned dates (``\\hfill 2021 -- 2026``)
    leaves a gap on those lines only; its full-width paragraphs cover the same
    band, so the union of extents has no gutter and this correctly stays quiet.
    """
    for index, words in enumerate(words_per_page):
        if len(words) < 40:
            continue

        extents = [(float(w["x0"]), float(w["x1"])) for w in words]
        page_left = min(x0 for x0, _ in extents)
        page_right = max(x1 for _, x1 in extents)
        span = page_right - page_left
        if span < 200:
            continue

        # 1pt-resolution occupancy across the text area.
        width = int(span) + 1
        occupied = bytearray(width)
        for x0, x1 in extents:
            start = max(0, int(x0 - page_left))
            end = min(width, int(x1 - page_left) + 1)
            for position in range(start, end):
                occupied[position] = 1

        # Widest empty band that is not against either margin.
        best_gap = (0, 0, 0.0)  # start, end, width
        run_start: int | None = None
        for position in range(width):
            if not occupied[position]:
                if run_start is None:
                    run_start = position
            elif run_start is not None:
                gap_width = position - run_start
                if gap_width > best_gap[2]:
                    best_gap = (run_start, position, float(gap_width))
                run_start = None

        gap_start, gap_end, gap_width = best_gap
        if gap_width < MIN_GUTTER_POINTS:
            continue

        # A gutter sits inside the text block, not at its edges.
        centre = (gap_start + gap_end) / 2
        if not (0.2 * width < centre < 0.8 * width):
            continue

        left_words = sum(1 for _, x1 in extents if x1 - page_left <= gap_start)
        right_words = sum(1 for x0, _ in extents if x0 - page_left >= gap_end)
        share = len(words) * GUTTER_SIDE_SHARE
        if left_words >= share and right_words >= share:
            return True, (
                f"page {index + 1}: {gap_width:.0f}pt empty gutter with "
                f"{left_words} words left and {right_words} right — two-column "
                f"layouts extract as interleaved text"
            )
    return False, ""


def verify_pdf(
    path: str | Path,
    *,
    kind: str,
    expect: ParseExpectations | None = None,
) -> ParseReport:
    """Run every gate check against a built PDF. Never raises."""
    path = Path(path)
    expect = expect or ParseExpectations()
    checks: list[CheckResult] = []

    if not path.exists():
        return ParseReport(
            passed=False,
            kind=kind,
            path=str(path),
            checks=[CheckResult(name="file_exists", passed=False, detail="no such file")],
        )

    pypdf_text, pages, pypdf_error = _extract_pypdf(path)
    plumber_text, words_per_page, plumber_error = _extract_pdfplumber(path)

    if pypdf_error or plumber_error:
        checks.append(
            CheckResult(
                name="pdf_readable",
                passed=False,
                detail=f"pypdf: {pypdf_error or 'ok'}; pdfplumber: {plumber_error or 'ok'}",
            )
        )
        return ParseReport(
            passed=False, kind=kind, path=str(path), pages=pages, checks=checks
        )
    checks.append(CheckResult(name="pdf_readable", passed=True))

    text = plumber_text if len(plumber_text) >= len(pypdf_text) else pypdf_text
    normalised = _normalise(text)

    # 1 — a real text layer
    checks.append(
        CheckResult(
            name="text_layer",
            passed=len(text.strip()) > MIN_TEXT_CHARS,
            detail=f"{len(text.strip())} chars extracted (need >{MIN_TEXT_CHARS})",
        )
    )

    # 2 — the two extractors agree
    agreement = fuzz.ratio(_normalise(pypdf_text), _normalise(plumber_text))
    checks.append(
        CheckResult(
            name="extractor_agreement",
            passed=agreement > EXTRACTOR_AGREEMENT_THRESHOLD,
            detail=f"pypdf vs pdfplumber similarity {agreement:.1f}% "
            f"(need >{EXTRACTOR_AGREEMENT_THRESHOLD})",
        )
    )

    # 3 — ligatures survived
    source = _normalise(_strip_latex_comments(expect.source_text or ""))
    canaries = [word for word in LIGATURE_CANARIES if word in source] if source else []
    canaries += [_normalise(e) for e in expect.employers if e]
    if expect.name:
        canaries.append(_normalise(expect.name))
    missing_canaries = [word for word in dict.fromkeys(canaries) if word and word not in normalised]
    checks.append(
        CheckResult(
            name="no_ligature_corruption",
            passed=not missing_canaries,
            detail=(
                f"words present in the source did not survive extraction: {missing_canaries}"
                if missing_canaries
                else f"{len(canaries)} canary words survived"
            ),
        )
    )

    # 4 — name near the top, sections in source order
    if expect.name:
        head = _normalise(text[:NAME_WITHIN_CHARS])
        checks.append(
            CheckResult(
                name="name_near_top",
                passed=_normalise(expect.name) in head,
                detail=f"'{expect.name}' within first {NAME_WITHIN_CHARS} chars",
            )
        )

    if expect.section_order:
        positions = [(s, normalised.find(_normalise(s))) for s in expect.section_order]
        found = [(s, p) for s, p in positions if p >= 0]
        ordered = all(found[i][1] < found[i + 1][1] for i in range(len(found) - 1))
        checks.append(
            CheckResult(
                name="section_order_preserved",
                passed=ordered and len(found) == len(positions),
                detail=f"found {[s for s, _ in found]} at {[p for _, p in found]}",
            )
        )

    # 5 — standard section headers present (resumes only)
    if kind == "resume":
        missing = [s for s in STANDARD_SECTIONS if s not in normalised]
        checks.append(
            CheckResult(
                name="standard_sections_present",
                passed=not missing,
                detail=f"missing section headers: {missing}" if missing else "all present",
            )
        )

    # 6 — contact details reachable
    if expect.email:
        checks.append(
            CheckResult(
                name="email_present",
                passed=_normalise(expect.email) in normalised,
                detail=expect.email,
            )
        )
    if expect.phone:
        wanted = _digits(expect.phone)
        # Australian numbers are written +61 4xx / 04xx; compare the national
        # significant digits so formatting differences do not fail the check.
        national = wanted[-9:] if len(wanted) >= 9 else wanted
        checks.append(
            CheckResult(
                name="phone_present",
                passed=bool(national) and national in _digits(text),
                detail=f"looking for national digits {national}",
            )
        )

    # 6b — nothing else shares the contact line
    #
    # An ATS reads the contact block positionally: the line carrying the email
    # is where it expects to find the phone and the location, and it takes the
    # trailing run of that line as the location. Anything else landing on that
    # line is silently absorbed into a field the employer then filters on.
    #
    # This is not hypothetical. The cover letter template put the date in its
    # own paragraph, but Jinja runs with trim_blocks=True, which eats the
    # newline after \BLOCK{endif} and so consumed the blank line separating
    # them. LaTeX joined the two into one paragraph and every cover letter
    # extracted as "... Adelaide SA 01 September 2026" — a location field with
    # a date stapled to it. All eight checks passed.
    if expect.email:
        # Every line carrying the email, not just the first. combined.pdf holds
        # the resume's contact block *and* the cover letter's, and it is the
        # artifact used wherever a form has a single attachment slot — checking
        # only the first match passed the contaminated cover-letter page.
        contact_lines = [
            line
            for line in text.splitlines()
            if _normalise(expect.email) in _normalise(line)
        ]
        contaminated = [
            (line, match)
            for line, match in (
                (line, _DATE_ON_CONTACT_LINE.search(line)) for line in contact_lines
            )
            if match is not None
        ]
        checks.append(
            CheckResult(
                name="contact_line_uncontaminated",
                passed=not contaminated,
                detail=(
                    f"date {contaminated[0][1].group(0)!r} shares a contact line: {contaminated[0][0]!r}"
                    if contaminated
                    else f"{len(contact_lines)} contact line(s) carry contact details only"
                ),
            )
        )

    # 7 — page limits
    limit = PAGE_LIMITS.get(kind)
    if limit:
        checks.append(
            CheckResult(
                name="page_limit",
                passed=0 < pages <= limit,
                detail=f"{pages} pages (limit {limit} for {kind})",
            )
        )

    # 8 — claimed keywords actually made it in
    # 8b — every fact the profile states survived extraction character for
    # character. Whitespace is collapsed and case folded so a line wrap or a
    # small-caps heading does not fail it, but punctuation is preserved: that
    # is what catches an escaping regression such as "C#" typesetting as "C\\#".
    if expect.verbatim:
        missing_verbatim = [
            fact for fact in expect.verbatim if _normalise(fact) not in normalised
        ]
        checks.append(
            CheckResult(
                name="verbatim_facts_present",
                passed=not missing_verbatim,
                detail=(
                    f"stated in the profile but not extractable: {missing_verbatim[:12]}"
                    if missing_verbatim
                    else f"all {len(expect.verbatim)} profile facts survived extraction"
                ),
            )
        )

    if expect.claimed_keywords:
        missing_keywords = [
            kw for kw in expect.claimed_keywords if _normalise(kw) not in normalised
        ]
        checks.append(
            CheckResult(
                name="claimed_keywords_present",
                passed=not missing_keywords,
                detail=f"claimed but not found: {missing_keywords}"
                if missing_keywords
                else f"all {len(expect.claimed_keywords)} present",
            )
        )

    # 9 — single column
    two_col, detail = _detect_two_columns(words_per_page)
    checks.append(
        CheckResult(
            name="single_column_layout",
            passed=not two_col,
            detail=detail or "no column gutter detected",
        )
    )

    report = ParseReport(
        passed=all(check.passed for check in checks),
        kind=kind,
        path=str(path),
        pages=pages,
        extracted_chars=len(text.strip()),
        extracted_text=text.strip(),
        checks=checks,
    )

    if report.passed:
        log.info("parse_gate_passed", path=str(path), kind=kind, pages=pages)
    else:
        log.error(
            "parse_gate_failed",
            path=str(path),
            kind=kind,
            failures=[c.name for c in report.failures],
            detail=report.summary(),
            recurring=_gate_failure_is_recurring(kind, report.summary()),
        )
    return report


def _gate_failure_is_recurring(kind: str, summary: str) -> bool:
    """Record this gate failure and say whether it has happened before.

    "The parse gate failed" and "the parse gate has failed four times this week"
    call for completely different responses — the first is a bad document, the
    second is a broken template. Answering it on the log line means the
    distinction is visible where the failure is read, not only in the digest.

    Never raises and never blocks the gate: bookkeeping attached to an already
    failing path must not turn a diagnosable failure into a confusing one.
    """
    from backend.db import session_scope
    from backend.failures import is_recurring, record
    from backend.models import FailureType

    try:
        with session_scope() as session:
            record(
                session,
                platform="documents",
                failure_type=FailureType.PARSE_GATE,
                element_id=kind,
                detail=summary,
            )
            return is_recurring(
                session,
                platform="documents",
                failure_type=FailureType.PARSE_GATE,
                element_id=kind,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("parse_gate_failure_not_recorded", error=str(exc)[:150])
        return False
