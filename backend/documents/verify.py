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
10. merged words          — a squeezed line loses every space on it, so
                            "cover letter" reaches the ATS as "coverletter"
11. run-together records  — the next employer flows onto the previous entry's
                            last line, and the ATS files it under the wrong job
12. one-extractor facts   — a fact only the friendlier extractor can find
13. truncated contact line — a field wrapped off the end, leaving a dangling
                            separator and an empty field where the ATS looks

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

from backend.config import settings
from backend.llm.client import llm
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

    line_starts: list[str] = Field(default_factory=list)
    """Record headings that must each begin an extracted line.

    An ATS segments work history and education by line: the line a heading
    starts is the record, and everything after it belongs to that record. A
    heading that lands mid-line is filed under the record before it.

    This is not hypothetical either. ``\\vspace`` does not end a paragraph, so
    an education block written with ``\\vspace`` between entries and no
    ``\\par`` extracted as::

        Professional Year Program, ICT Adelaide, SA Torrens University Australia May 2023 to May 2025

    — the second institution glued to the first entry's qualification, with the
    dates of one attached to the name of the other. Every check passed.

    Only supplied for artifacts that contain a resume; a cover letter names
    employers inside sentences, where mid-line is exactly right.
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


# Separators a contact strip is built from. A field that wraps off the end of
# the strip leaves the separator that preceded it stranded, so a leading,
# trailing or doubled separator is the signature of a truncated contact line —
# whichever field it was that fell off.
_CONTACT_SEPARATORS = "|·•–—"
_DANGLING_SEPARATOR = re.compile(
    rf"(?:^[{_CONTACT_SEPARATORS}])"
    rf"|(?:[{_CONTACT_SEPARATORS}]$)"
    rf"|(?:[{_CONTACT_SEPARATORS}]\s*[{_CONTACT_SEPARATORS}])"
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


GLUED_PROBE_TOLERANCE = 1.2
"""Word-gap tolerance for the second, tighter extraction pass.

pdfplumber starts a new word when the horizontal gap exceeds ``x_tolerance``,
which defaults to 3pt — and 3pt is not a comfortable margin. At a 10pt base,
lmodern's interword space is 3.33pt and ``\\small`` makes it 3.0pt, so any line
TeX has to squeeze drops under the threshold and the extractor silently glues
that whole line into one token. Extracting a second time at 1.2pt — below any
kerning gap, above nothing — says which words the default pass lost.
"""

GLUED_MIN_CHARS = 6
"""Ignore short splits. A 1.2pt gap inside a short token is more likely an
italic correction or a thin space than a destroyed word boundary."""


def _glued_words(page: Any, default_words: list[dict[str, Any]]) -> list[str]:
    """Tokens the default extraction merged that a tighter pass separates.

    Each returned string is a run of words an ATS will never match: the profile
    says "cover letter" and the resume delivers "coverletter".

    A token counts only when the tight pass splits it into at least two pieces
    of two or more characters each. That is what separates a lost space from a
    hyphenation artefact or a stray thin space, and it is why a URL — which has
    no internal gap for either pass to split on — is never reported.
    """
    try:
        tight = page.extract_words(x_tolerance=GLUED_PROBE_TOLERANCE) or []
    except Exception as exc:  # noqa: BLE001 - a probe must not break the gate
        log.debug("glued_word_probe_failed", error=str(exc)[:120])
        return []

    glued: list[str] = []
    for word in default_words:
        text = str(word["text"])
        if len(text) < GLUED_MIN_CHARS:
            continue
        # Same line and inside this word's own box. Both, not just the x range:
        # keying on the horizontal position alone matches words from every other
        # line of the page that happen to start at the same indent, which makes
        # every word on the document look glued.
        top, x0, x1 = float(word["top"]), float(word["x0"]), float(word["x1"])
        pieces = [
            piece["text"]
            for piece in tight
            if abs(float(piece["top"]) - top) <= 1.0
            and float(piece["x0"]) >= x0 - 0.2
            and float(piece["x1"]) <= x1 + 0.2
            and len(str(piece["text"])) >= 2
        ]
        if len(pieces) >= 2:
            glued.append(text)
    return glued


def _extract_pdfplumber(
    path: Path,
) -> tuple[str, list[list[dict[str, Any]]], list[str], str | None]:
    try:
        words_per_page: list[list[dict[str, Any]]] = []
        chunks: list[str] = []
        glued: list[str] = []
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages:
                chunks.append(page.extract_text() or "")
                words = page.extract_words() or []
                words_per_page.append(words)
                glued.extend(_glued_words(page, words))
        return "\n".join(chunks), words_per_page, glued, None
    except Exception as exc:  # noqa: BLE001
        return "", [], [], f"{type(exc).__name__}: {exc}"[:200]


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


_SELF_CHECK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "unsupported": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Claims the document makes that the profile does not support. "
                "Quote the document's own words. Empty when everything checks out."
            ),
        }
    },
    "required": ["unsupported"],
    "additionalProperties": False,
}

_SELF_CHECK_SYSTEM = """\
You audit a job application document against the facts it is allowed to assert.

List ONLY claims the profile does not support. Be strict about substance and
indifferent to style:

* A claim of experience, seniority, scale or duration that the profile does not
  evidence is unsupported, even when it names nothing specific. "Extensive
  experience leading cross-functional teams" is unsupported unless the profile
  shows it.
* Rephrasing something the profile does contain is FINE. "Built the reporting
  pipeline" for "developed reporting infrastructure" is the same claim.
* Enthusiasm, motivation and interest in the role are not factual claims. Ignore
  them.
* Contact details, section headings and formatting are not claims.

An empty list is the normal and expected answer. Do not invent problems to look
thorough.\
"""


def _fabrication_self_check(text: str, profile: Any, kind: str) -> tuple[bool, str]:
    """Read the document back against the profile. Returns (passed, detail).

    PASSES on any failure to run. A model outage, a missing API key or a
    malformed response must not fail a document that every deterministic check
    accepted — that would make the gate depend on a third party being up, and
    the gate's whole job is to be the thing you can rely on.
    """
    from backend.documents.fabrication import profile_fact_index

    try:
        facts_text = profile_fact_index(profile)
    except Exception as exc:  # noqa: BLE001
        log.warning("self_check_profile_unreadable", error=str(exc)[:150])
        return True, "skipped: profile could not be summarised"

    try:
        # Through the module-level `llm`, not an inline import. That object is
        # the seam the rehearsal and the document tests replace with a stub —
        # an inline `from backend.llm.client import complete_json` bypasses it
        # and attempts a real call, which is what turned a 27-second suite into
        # a 3.5-minute one on LiteLLM's retry backoff.
        result = llm.complete_json(
            f"PROFILE (the only facts assertable):\n{facts_text}\n\n"
            f"DOCUMENT ({kind}):\n{text[:6000]}",
            model=settings.llm_model_classify,
            purpose="document_self_check",
            schema=_SELF_CHECK_SCHEMA,
            system=_SELF_CHECK_SYSTEM,
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001 - see the docstring
        log.warning("self_check_unavailable", kind=kind, error=str(exc)[:200])
        return True, "skipped: the check could not run"

    unsupported = [
        str(item).strip()
        for item in (result.get("unsupported") or [])
        if str(item).strip()
    ]
    if not unsupported:
        return True, ""

    log.error(
        "unsupported_claims_in_document",
        kind=kind,
        count=len(unsupported),
        claims=unsupported[:5],
    )
    return False, "; ".join(unsupported[:3])


def verify_pdf(
    path: str | Path,
    *,
    kind: str,
    expect: ParseExpectations | None = None,
    profile: Any = None,
) -> ParseReport:
    """Run every gate check against a built PDF. Never raises.

    ``profile`` enables the model-read fabrication check: the extracted text is
    read back against the profile and any unsupported claim fails the gate.
    Optional because the gate must still work with no API key — and because
    every other check here is deterministic, so a model outage should not be
    able to stop a build that the deterministic checks pass.
    """
    path = Path(path)
    expect = expect or ParseExpectations()
    checks: list[CheckResult] = []

    if not path.exists():
        return ParseReport(
            passed=False,
            kind=kind,
            path=str(path),
            checks=[
                CheckResult(name="file_exists", passed=False, detail="no such file")
            ],
        )

    pypdf_text, pages, pypdf_error = _extract_pypdf(path)
    plumber_text, words_per_page, glued_words, plumber_error = _extract_pdfplumber(path)

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
    missing_canaries = [
        word for word in dict.fromkeys(canaries) if word and word not in normalised
    ]
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

    # 3b — the spaces survived too
    #
    # Ligature corruption loses characters inside a word; this loses the
    # boundaries between words, which is just as fatal and much harder to see.
    # The document looks perfect. The extracted text reads
    # "Promptpipelinetunedforfactualgroundingsothemodelreshapesrealexperience",
    # and an ATS searching for "prompt pipeline" finds nothing.
    checks.append(
        CheckResult(
            name="word_spacing_survives_extraction",
            passed=not glued_words,
            detail=(
                f"{len(glued_words)} run(s) of words extracted with no space "
                f"between them: {glued_words[:5]}"
                if glued_words
                else "every word boundary survived extraction"
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

    # 4b — each record heading begins its own line
    #
    # section_order proves the sections are in the right order; this proves the
    # records inside them are separable. An employer or institution that lands
    # mid-line is read as part of the record above it, so a qualification ends
    # up filed under the wrong university with the wrong dates.
    if expect.line_starts:
        starts = [_normalise(line) for line in text.splitlines()]
        buried = [
            heading
            for heading in expect.line_starts
            if not any(line.startswith(_normalise(heading)) for line in starts if line)
        ]
        checks.append(
            CheckResult(
                name="record_headings_start_a_line",
                passed=not buried,
                detail=(
                    f"never begins an extracted line, so an ATS files it under the "
                    f"record above: {buried[:6]}"
                    if buried
                    else f"all {len(expect.line_starts)} record headings begin a line"
                ),
            )
        )

    # 5 — standard section headers present (resumes only)
    if kind == "resume":
        missing = [s for s in STANDARD_SECTIONS if s not in normalised]
        checks.append(
            CheckResult(
                name="standard_sections_present",
                passed=not missing,
                detail=f"missing section headers: {missing}"
                if missing
                else "all present",
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
        # 6c — no field wrapped off the end of the contact strip
        #
        # The strip is one centred line of phone, email, location and links. Set
        # one field too wide and TeX wraps it, leaving the separator that
        # preceded it stranded at the end of the line and an empty field where
        # an ATS reads the location. Checking for the stranded separator rather
        # than for a list of expected fields is what keeps a deliberately
        # two-line contact block, and the cover letter's signature line, legal.
        truncated = [
            (line, match)
            for line, match in (
                (line, _DANGLING_SEPARATOR.search(line.strip()))
                for line in contact_lines
            )
            if match is not None
        ]
        checks.append(
            CheckResult(
                name="contact_line_not_truncated",
                passed=not truncated,
                detail=(
                    f"separator {truncated[0][1].group(0)!r} with no field beside it — "
                    f"something wrapped off the line: {truncated[0][0]!r}"
                    if truncated
                    else "no stranded separators on any contact line"
                ),
            )
        )

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

    # 8c — the fact survived BOTH extractors, not just the friendlier one
    #
    # Everything above reads `text`, which is whichever of the two extractions
    # came out longer — in practice always pdfplumber, because it reconstructs
    # words from glyph positions. pypdf reads the content stream instead and
    # inserts a space wherever pdfTeX emitted a tightening kern, so lmodern's
    # BOLD kerning turned "Wemark Real Estate" into "W emark Real Estate",
    # "Vericent" into "V ericent", "Torrens University Australia" into
    # "T orrens ..." and "Feb 2026" into "F eb 2026". Two of five employers and
    # one of three institutions were unfindable by a whole class of parser, and
    # every check here passed, because the pypdf text was thrown away before
    # any content check read it.
    #
    # Checked against the profile's own facts rather than the whole text: the
    # two extractors are allowed to disagree about layout (that is what
    # extractor_agreement's 90% tolerance is for), but not about whether the
    # user's employer appears in their resume.
    if expect.verbatim or expect.employers:
        must_survive = list(dict.fromkeys([*expect.verbatim, *expect.employers]))
        normalised_pypdf = _normalise(pypdf_text)
        normalised_plumber = _normalise(plumber_text)
        one_sided = [
            fact
            for fact in must_survive
            if (_normalise(fact) in normalised_plumber)
            != (_normalise(fact) in normalised_pypdf)
        ]
        checks.append(
            CheckResult(
                name="facts_survive_both_extractors",
                passed=not one_sided,
                detail=(
                    f"only one of the two extractors can find these, so a parser "
                    f"using the other will not: {one_sided[:8]}"
                    if one_sided
                    else f"all {len(must_survive)} facts found by both extractors"
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

    # The last check, and the only one that reads the document with a model.
    #
    # documents/fabrication.py catches specific shapes — an employer name, a
    # date, a number the profile does not contain. What it cannot catch is the
    # fluent paraphrase that claims a capability without naming anything
    # checkable: "extensive experience leading cross-functional teams" contains
    # no fact to match against and is exactly what a writing model produces
    # when the profile is thin. This reads the finished text against the
    # profile and asks what is not supported.
    if profile is not None and settings.document_fabrication_check:
        supported, detail = _fabrication_self_check(text, profile, kind)
        checks.append(
            CheckResult(name="no_unsupported_claims", passed=supported, detail=detail)
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
