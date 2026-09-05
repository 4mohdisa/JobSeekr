"""The characters that break a resume on the way into a PDF.

Phase 2 exists because the parse gate passed a resume where the skill "C#" was
typeset as the literal text ``C\\#``. It looked almost right to a human and an
ATS searching for "C#" found nothing. It passed only because that day's
``claimed_keywords`` happened to be Python, SQL and FastAPI — three strings
containing no character the escaper could corrupt.

So this file assumes more blind spots exist and goes looking. Two corpora, for
two different failure modes:

``BREAKING`` — characters LaTeX treats as *syntax*. Unescaped they abort the
    build or swallow the rest of a line; double-escaped they typeset the escape
    itself as content. This is where the C# incident lived.

``TRANSFORMING`` — characters LaTeX silently *rewrites*. Nothing errors, the
    PDF looks correct, and the extracted text is a different string: ``it's``
    becomes ``it’s`` and ``10 -- 20`` becomes ``10 – 20``. An ATS matching a
    literal finds nothing. Both of these were real bugs, found here.

The compile tests are the ones with teeth: they render the shipped template,
run pdflatex, extract with both extractors, and diff the extracted text against
the intended strings — every string, not a sample.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.documents.build import (
    _job_context,
    _profile_context,
    _today_context,
    expected_verbatim,
    render_pdf,
)
from backend.documents.engine import render_string, template_root
from backend.documents.latex import escape_latex
from backend.documents.verify import (
    ParseExpectations,
    _extract_pdfplumber,
    _extract_pypdf,
    verify_pdf,
)
from backend.models import Job, Profile
from tests.conftest import needs_pdflatex

# =========================================================================
# Corpus 1 — characters LaTeX treats as syntax
# =========================================================================

BREAKING = [
    # Languages and trademarks an ATS matches literally.
    "C++",
    "C#",
    ".NET",
    "F#",
    "R&D",
    "AT&T",
    # Numbers, which carry the achievement.
    "50%",
    "$80k",
    "30% uplift",
    "~5 years",
    # Employers and people.
    "Smith & Wesson Pty Ltd",
    "Ångström Labs",
    "José Müller & Co",
    # Project and issue identifiers.
    "data_pipeline_v2",
    "issue #42",
    "config{nested}",
    # Typography pasted in from a word processor.
    "em—dash",
    "en–dash",
    "curly’quote",
    "curly“double”quote",
]

LONG_BULLET = (
    "A deliberately very long single-line bullet that runs on and on to force "
    "LaTeX to wrap it across multiple lines in the output so that we can confirm "
    "the extractor still returns the whole sentence intact without dropping or "
    "reordering any of the words in the middle of the wrapped region"
)

BREAKING_PROFILE = Profile(
    version=1,
    identity={
        "name": "José Müller-Ångström",
        "headline": "C++ / C# Engineer",
        "email": "jose.muller@example.com",
        "phone": "+61 412 345 678",
        "location": "Adelaide SA 5000",
        "linkedin": "linkedin.com/in/josemuller",
        "website": "",
        "summary": "Built .NET and C++ systems; 50% faster, 30% uplift, ~5 years in R&D.",
    },
    work_rights={"statement": "Australian citizen — full working rights."},
    skills=[
        "C++",
        "C#",
        ".NET",
        "F#",
        "R&D",
        "AT&T systems",
        "50% automation",
        "$80k budgets",
    ],
    experience=[
        {
            "title": "Senior Engineer — Platform",
            "company": "Smith & Wesson Pty Ltd",
            "start": "2022",
            "end": "Present",
            "location": "Adelaide SA",
            "highlights": [
                "Cut latency 50% and delivered 30% uplift across the C++ core",
                "Owned data_pipeline_v2 and closed issue #42 with config{nested} overrides",
                (
                    "Negotiated $80k of tooling spend over ~5 years — em—dash, "
                    "en–dash, curly’quote inside"
                ),
                LONG_BULLET,
            ],
        },
        {
            "title": "Engineer",
            "company": "Ångström Labs",
            "start": "2019",
            "end": "2022",
            "location": "",
            "highlights": ["Shipped F# services for AT&T integrations"],
        },
    ],
    education=[
        {
            "qualification": "BSc Computer Science",
            "institution": "José Müller & Co Institute",
            "year": "2019",
        }
    ],
    certifications=[
        {"name": "AWS Certified — Developer", "issuer": "AT&T Training", "year": "2023"}
    ],
    projects=[
        {
            "name": "data_pipeline_v2",
            "stack": "C# / .NET",
            "description": "Handles curly“double”quote and config{nested} shapes at 50% cost.",
        }
    ],
)


# =========================================================================
# Corpus 2 — characters LaTeX silently rewrites
# =========================================================================

TRANSFORMING = [
    'said "hello" plainly',  # straight double quotes: LaTeX curls them
    "it's a plain apostrophe",  # straight single quote -> U+2019
    "a < b and c > d",  # angle brackets
    "100% ^ 2 ~ 3",  # lone specials
    "back\\slash literal",  # a literal backslash
    "a_b^c",  # subscript and superscript characters
    "flow off finally",  # ff, ffi, ffl ligatures
    "10 -- 20 and 30 --- 40",  # hyphen runs -> en dash, em dash
]

TRANSFORMING_PROFILE = Profile(
    version=1,
    identity={
        "name": "Test User",
        "headline": "QA",
        "email": "t@example.com",
        "phone": "+61 412 345 678",
        "location": "Adelaide SA",
        "linkedin": "",
        "website": "",
        "summary": "probe",
    },
    work_rights={"statement": "citizen"},
    skills=TRANSFORMING,
    experience=[
        {
            "title": "Engineer",
            "company": "Acme",
            "start": "2020",
            "end": "2024",
            "location": "",
            "highlights": list(TRANSFORMING),
        }
    ],
    education=[],
    certifications=[],
    projects=[],
)


# =========================================================================
# Helpers
# =========================================================================


def _resume_tex(profile: Profile) -> str:
    job = Job(
        id=1,
        source="seek",
        source_job_id="1",
        url="https://example.com/1",
        title="C++ Engineer",
        company="Smith & Wesson Pty Ltd",
        location="Adelaide SA",
        dedupe_hash="h1",
        description="C++, C#, .NET, R&D.",
    )
    context = {
        "profile": _profile_context(profile),
        "job": _job_context(job),
        "campaign": {"name": "hostile"},
        "today": _today_context(),
        "ai": {
            slot: f"[{slot}]"
            for slot in ("opening_hook", "closing", "why_company", "skills_bridge")
        },
    }
    source = (template_root() / "resume.tex.j2").read_text(encoding="utf-8")
    return render_string(source, context)


def _compile_and_extract(
    profile: Profile, tmp_path: Path, stem: str
) -> tuple[str, str]:
    """Render the shipped template, compile it, and read the text back."""
    pdf = render_pdf(_resume_tex(profile), tmp_path, stem)
    return _extract_pypdf(pdf)[0], _extract_pdfplumber(pdf)[0]


def _missing(intended: list[str], text: str) -> list[str]:
    return [fact for fact in intended if fact not in text]


# =========================================================================
# Escaping, in isolation — fast, no pdflatex
# =========================================================================


def test_an_ascii_apostrophe_survives_as_an_ascii_apostrophe():
    """``Dan Murphy's`` must not extract as ``Dan Murphy’s``.

    LaTeX's ``'`` ligature produces a curly U+2019. Typographically correct,
    and wrong for a resume: the profile is the source of truth for facts, and
    an ATS searching the literal employer name finds nothing.
    """
    assert escape_latex("Dan Murphy's") == r"Dan Murphy\textquotesingle{}s"


def test_a_hyphen_run_survives_as_hyphens():
    """``2020--2024`` must not extract with an en dash."""
    assert escape_latex("2020--2024") == "2020-{}-2024"
    assert escape_latex("a---b") == "a-{}-{}-b"
    # A single hyphen is not a ligature and must be left alone.
    assert escape_latex("well-known") == "well-known"


def test_real_unicode_punctuation_still_round_trips():
    """The asymmetry is deliberate: ASCII is pinned, Unicode is preserved.

    A curly quote pasted from Word is mapped INTO ligature source so it comes
    back out as the same character. Only ASCII the user typed is held to the
    letter.
    """
    assert escape_latex("Murphy’s") == "Murphy's"
    assert escape_latex("a—b") == "a---b"
    assert escape_latex("a–b") == "a--b"


def test_the_escaper_is_idempotent_in_the_sense_that_matters():
    """Escaping twice must be visibly different from escaping once.

    This is not a claim that escaping is idempotent — it is not, and must not
    be. It is the tripwire for the C# incident: if a second pass ever became
    invisible, double-escaping would stop being detectable.
    """
    once = escape_latex("C#")
    twice = escape_latex(once)
    assert once == r"C\#"
    assert twice != once and r"\textbackslash" in twice


@pytest.mark.parametrize("fact", BREAKING + TRANSFORMING)
def test_no_escaped_fact_leaks_a_raw_special_character(fact: str):
    """Whatever the escaper emits must be syntactically valid LaTeX.

    Checked structurally rather than by compiling: every ``&``, ``%``, ``$``,
    ``#`` and ``_`` in the output must be preceded by a backslash.
    """
    escaped = escape_latex(fact)
    for index, char in enumerate(escaped):
        if char in "&%$#_":
            assert index > 0 and escaped[index - 1] == "\\", (
                f"unescaped {char!r} at {index} in {escaped!r}"
            )


# =========================================================================
# Compile, extract, diff — every string, both extractors
# =========================================================================


@needs_pdflatex
def test_every_syntax_breaking_fact_survives_a_real_build(tmp_path):
    pypdf_text, plumber_text = _compile_and_extract(
        BREAKING_PROFILE, tmp_path, "breaking"
    )

    assert _missing(BREAKING, pypdf_text) == [], pypdf_text
    assert _missing(BREAKING, plumber_text) == [], plumber_text


@needs_pdflatex
def test_every_silently_rewritten_fact_survives_a_real_build(tmp_path):
    """The round that found two real bugs: ``it's`` and ``10 -- 20``."""
    pypdf_text, plumber_text = _compile_and_extract(
        TRANSFORMING_PROFILE, tmp_path, "transforming"
    )

    assert _missing(TRANSFORMING, pypdf_text) == [], pypdf_text
    assert _missing(TRANSFORMING, plumber_text) == [], plumber_text


@needs_pdflatex
def test_a_wrapped_bullet_is_not_reordered_or_truncated(tmp_path):
    """A long bullet wraps across lines; the words must come back in order."""
    pypdf_text, _ = _compile_and_extract(BREAKING_PROFILE, tmp_path, "wrapped")

    # Extraction may re-break the lines, so compare on collapsed whitespace.
    flat = " ".join(pypdf_text.split())
    assert " ".join(LONG_BULLET.split()) in flat


# =========================================================================
# The gate itself — it now checks the facts, not a hand-picked keyword list
# =========================================================================


def test_the_harvester_collects_the_facts_a_keyword_list_would_have_missed():
    facts = expected_verbatim(BREAKING_PROFILE)

    for fact in ("C++", "C#", ".NET", "F#", "R&D", "data_pipeline_v2"):
        assert fact in facts, f"{fact!r} not harvested from the profile"
    # Employers, institutions and certifications, not just skills.
    assert "Smith & Wesson Pty Ltd" in facts
    assert "José Müller & Co Institute" in facts
    # Risky tokens lifted out of free-text bullets.
    assert "issue" not in facts, "plain words must not be harvested"
    assert any("#42" in fact for fact in facts)
    # The long bullet is formatting-fragile, so it is not asserted verbatim.
    assert LONG_BULLET not in facts


def test_the_harvester_is_deterministic_and_deduplicated():
    facts = expected_verbatim(BREAKING_PROFILE)
    assert facts == expected_verbatim(BREAKING_PROFILE)
    assert len(facts) == len(set(facts))


@needs_pdflatex
def test_the_gate_confirms_every_harvested_fact_survived(tmp_path):
    tex = _resume_tex(BREAKING_PROFILE)
    pdf = render_pdf(tex, tmp_path, "gategood")
    report = verify_pdf(
        pdf,
        kind="resume",
        expect=ParseExpectations(
            name="José Müller-Ångström",
            email="jose.muller@example.com",
            verbatim=expected_verbatim(BREAKING_PROFILE),
            source_text=tex,
        ),
    )
    check = next(c for c in report.checks if c.name == "verbatim_facts_present")
    assert check.passed, check.detail


@needs_pdflatex
def test_the_gate_rejects_the_exact_bug_that_shipped(tmp_path):
    """Reintroduce the double-escape and prove the gate now catches it.

    This is the regression the whole phase is for. Before ``verbatim``, this
    build passed every check.
    """
    tex = _resume_tex(BREAKING_PROFILE)
    expect = ParseExpectations(
        name="José Müller-Ångström",
        email="jose.muller@example.com",
        verbatim=expected_verbatim(BREAKING_PROFILE),
        source_text=tex,
    )

    broken = render_pdf(tex.replace(r"\#", r"\textbackslash{}\#"), tmp_path, "gatebad")
    report = verify_pdf(broken, kind="resume", expect=expect)

    assert report.passed is False, report.summary()
    check = next(c for c in report.failures if c.name == "verbatim_facts_present")
    assert "C#" in check.detail


@needs_pdflatex
def test_the_gate_would_have_caught_a_curled_apostrophe(tmp_path):
    """The second bug class, expressed as a gate failure rather than a diff."""
    profile = Profile(
        version=1,
        identity={
            "name": "Test User",
            "headline": "Engineer",
            "email": "t@example.com",
            "phone": "+61 412 345 678",
            "location": "Adelaide SA",
            "linkedin": "",
            "website": "",
            "summary": "probe",
        },
        work_rights={"statement": "citizen"},
        skills=["Dan Murphy's stack"],
        experience=[
            {
                "title": "Engineer",
                "company": "Dan Murphy's",
                "start": "2020",
                "end": "2024",
                "location": "",
                "highlights": ["Worked at Dan Murphy's"],
            }
        ],
        education=[],
        certifications=[],
        projects=[],
    )
    tex = _resume_tex(profile)
    expect = ParseExpectations(
        name="Test User",
        email="t@example.com",
        verbatim=expected_verbatim(profile),
        source_text=tex,
    )

    # The pre-fix behaviour: emit the raw ASCII quote and let LaTeX curl it.
    regressed = tex.replace(r"\textquotesingle{}", "'")
    report = verify_pdf(
        render_pdf(regressed, tmp_path, "curled"), kind="resume", expect=expect
    )

    check = next(c for c in report.checks if c.name == "verbatim_facts_present")
    assert check.passed is False, "a curled apostrophe must fail the gate"
    assert "Murphy" in check.detail


@needs_pdflatex
def test_the_old_keyword_list_passes_a_document_the_new_gate_rejects(tmp_path):
    """Why the keyword list had to go, demonstrated rather than asserted.

    ``build.py`` fed the gate ``claimed_keywords = profile.skills[:12]``, so its
    entire coverage was "the first twelve skills". Corrupt anything else — an
    employer, a project name, a metric inside a bullet — and the gate signed off
    on a broken document. Here the underscore escaping is doubled, which mangles
    the project ``data_pipeline_v2``; no skill contains an underscore, so the
    keyword check sees nothing wrong.
    """
    tex = _resume_tex(BREAKING_PROFILE)
    broken = render_pdf(
        tex.replace(r"\_", r"\textbackslash{}\_"), tmp_path, "underscore"
    )

    common = {
        "name": "José Müller-Ångström",
        "email": "jose.muller@example.com",
        "phone": "+61 412 345 678",
        "employers": ["Smith & Wesson Pty Ltd", "Ångström Labs"],
        "source_text": tex,
    }
    skills = [str(skill) for skill in BREAKING_PROFILE.skills][:12]

    old = verify_pdf(
        broken,
        kind="resume",
        expect=ParseExpectations(claimed_keywords=skills, **common),
    )
    assert old.passed, "the old gate is expected to miss this — that is the point"

    new = verify_pdf(
        broken,
        kind="resume",
        expect=ParseExpectations(
            verbatim=expected_verbatim(BREAKING_PROFILE), **common
        ),
    )
    assert not new.passed
    assert (
        "data_pipeline_v2"
        in next(c for c in new.failures if c.name == "verbatim_facts_present").detail
    )


# =========================================================================
# Canary words must come from the document, not from its comments
# =========================================================================


def test_ligature_canaries_are_not_harvested_from_latex_comments():
    """A comment never typesets, so a canary taken from one can never be found.

    The shipped resume template explains the ligature rule in a comment
    containing the word "efficient". Every profile that did not happen to use
    that word therefore failed ``no_ligature_corruption``. The suite missed it
    because its fixture profile was written around the canary list rather than
    the other way round.
    """
    from backend.documents.verify import _strip_latex_comments

    source = "% the parse gate asserts efficient survives\n\\section{Skills}\nPython\n"
    stripped = _strip_latex_comments(source)
    assert "efficient" not in stripped
    assert "\\section{Skills}" in stripped and "Python" in stripped


def test_an_escaped_percent_is_content_not_a_comment():
    """``100\\% uptime`` must survive comment-stripping intact."""
    from backend.documents.verify import _strip_latex_comments

    assert _strip_latex_comments(r"Delivered 100\% uptime") == r"Delivered 100\% uptime"
    assert (
        _strip_latex_comments(r"Delivered 100\% uptime % aside")
        == r"Delivered 100\% uptime "
    )


@needs_pdflatex
def test_a_profile_that_avoids_the_canary_words_still_passes_the_gate(tmp_path):
    """The regression the comment bug caused: nothing here says "efficient"."""
    tex = _resume_tex(TRANSFORMING_PROFILE)
    report = verify_pdf(
        render_pdf(tex, tmp_path, "nocanary"),
        kind="resume",
        expect=ParseExpectations(
            name="Test User",
            email="t@example.com",
            employers=["Acme"],
            verbatim=expected_verbatim(TRANSFORMING_PROFILE),
            source_text=tex,
        ),
    )
    canary = next(c for c in report.checks if c.name == "no_ligature_corruption")
    assert canary.passed, canary.detail
