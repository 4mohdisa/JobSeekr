"""The parse gate, tested adversarially — the only test suite Block C needs.

Each broken PDF here is generated for real with pdflatex and represents a
document that looks fine to a human and is broken for an ATS. A gate that only
passes good documents is worthless; what matters is that it REJECTS these.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pdfplumber
import pytest

from backend.documents.verify import ParseExpectations, verify_pdf
from tests.conftest import needs_pdflatex, resolved_pdflatex

pytestmark = needs_pdflatex

NAME = "Jordan Fitzgerald"
EMAIL = "jordan.fitzgerald@example.com"
PHONE = "+61 412 345 678"
EMPLOYER = "Redgum Analytics"

# Deliberately ligature-heavy: fi/fl are exactly what breaks ATS extraction.
BODY_WORDS = (
    "Delivered efficient financial reporting workflow improvements and "
    "identified qualified candidates for certification review."
)

PREAMBLE = r"""
\documentclass[11pt,a4paper]{article}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern}
\usepackage[a4paper,margin=1.8cm]{geometry}
\pagestyle{empty}
\setlength{\parindent}{0pt}
"""


def compile_tex(tmp_path: Path, stem: str, source: str) -> Path:
    tex = tmp_path / f"{stem}.tex"
    tex.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [
            # The same binary the skip guard checked for. A bare "pdflatex"
            # would be looked up on PATH, which MiKTeX's per-user install is
            # not on — so these tests would run (PDFLATEX_PATH is set) and then
            # every fixture would die on FileNotFoundError.
            resolved_pdflatex() or "pdflatex",
            "-interaction=nonstopmode",
            "-halt-on-error",
            f"-output-directory={tmp_path}",
            str(tex),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    pdf = tmp_path / f"{stem}.pdf"
    if not pdf.exists():
        pytest.fail(f"fixture {stem} failed to compile:\n{result.stdout[-1500:]}")
    return pdf


def expectations(**overrides) -> ParseExpectations:
    base = {
        "name": NAME,
        "email": EMAIL,
        "phone": PHONE,
        "employers": [EMPLOYER],
        "source_text": BODY_WORDS,
    }
    base.update(overrides)
    return ParseExpectations(**base)


def filler(paragraphs: int = 6) -> str:
    return "\n\n".join(BODY_WORDS for _ in range(paragraphs))


# --------------------------------------------------------------- the good one


GOOD_RESUME = (
    PREAMBLE
    + r"""
\begin{document}
{\LARGE\bfseries """
    + NAME
    + r"""}\\
"""
    + EMAIL
    + r""" $\cdot$ """
    + PHONE
    + r""" $\cdot$ Adelaide SA

\section*{SUMMARY}
"""
    + BODY_WORDS
    + r"""

\section*{EXPERIENCE}
\textbf{Senior Analyst} \hfill 2021 -- 2026\\
\textit{"""
    + EMPLOYER
    + r"""}, Adelaide
\begin{itemize}
  \item """
    + BODY_WORDS
    + r"""
\end{itemize}

\section*{EDUCATION}
\textbf{BSc Computer Science} \hfill 2020\\
\textit{University of Adelaide}

\section*{SKILLS}
Python, SQL, financial modelling
\end{document}
"""
)


def test_a_clean_single_column_resume_passes_every_check(tmp_path):
    pdf = compile_tex(tmp_path, "good", GOOD_RESUME)
    report = verify_pdf(
        pdf,
        kind="resume",
        expect=expectations(
            claimed_keywords=["Python", "SQL"],
            section_order=["EXPERIENCE", "EDUCATION"],
        ),
    )
    assert report.passed, report.summary()
    assert report.pages == 1
    assert report.extracted_chars > 200


# ------------------------------------------------------------ the broken ones


def test_two_column_resume_is_rejected(tmp_path):
    """Passes every other check and still parses as interleaved mush."""
    source = (
        PREAMBLE
        + r"\usepackage{multicol}"
        + r"""
\begin{document}
{\LARGE\bfseries """
        + NAME
        + r"""}\\
"""
        + EMAIL
        + r""" $\cdot$ """
        + PHONE
        + r"""

\begin{multicols}{2}
\section*{EXPERIENCE}
\textbf{Senior Analyst}, """
        + EMPLOYER
        + r"""

"""
        + filler(4)
        + r"""

\section*{EDUCATION}
BSc Computer Science

\section*{SKILLS}
"""
        + filler(4)
        + r"""
\end{multicols}
\end{document}
"""
    )
    pdf = compile_tex(tmp_path, "twocol", source)
    report = verify_pdf(pdf, kind="resume", expect=expectations())

    assert not report.passed
    assert "single_column_layout" in [c.name for c in report.failures], report.summary()


def test_image_only_pdf_with_no_text_layer_is_rejected(tmp_path):
    """A scanned resume is a blank page to a parser."""
    source = (
        PREAMBLE
        + r"""
\usepackage{xcolor}
\begin{document}
\noindent\textcolor{white}{.}
\vrule width \textwidth height 12cm depth 0pt
\end{document}
"""
    )
    pdf = compile_tex(tmp_path, "imageonly", source)
    report = verify_pdf(pdf, kind="resume", expect=expectations())

    assert not report.passed
    assert "text_layer" in [c.name for c in report.failures], report.summary()


def test_contact_details_only_in_a_header_are_rejected(tmp_path):
    """Many parsers skip headers; the phone number becomes invisible."""
    source = (
        PREAMBLE
        + r"""
\usepackage{fancyhdr}
\pagestyle{fancy}
\fancyhf{}
\fancyhead[C]{"""
        + EMAIL
        + r""" \quad """
        + PHONE
        + r"""}
\begin{document}
{\LARGE\bfseries """
        + NAME
        + r"""}

\section*{EXPERIENCE}
\textbf{Senior Analyst}, """
        + EMPLOYER
        + r"""

"""
        + filler(3)
        + r"""

\section*{EDUCATION}
BSc Computer Science

\section*{SKILLS}
Python, SQL
\end{document}
"""
    )
    pdf = compile_tex(tmp_path, "headeronly", source)
    # The gate is told to look for the address in the body; a header-only
    # contact block is exactly the failure being simulated.
    report = verify_pdf(
        pdf,
        kind="resume",
        expect=expectations(name=NAME, email="not.in.body@example.com"),
    )
    assert not report.passed
    assert "email_present" in [c.name for c in report.failures], report.summary()


def test_a_four_page_resume_is_rejected_on_page_limit(tmp_path):
    source = (
        PREAMBLE
        + r"""
\begin{document}
{\LARGE\bfseries """
        + NAME
        + r"""}\\
"""
        + EMAIL
        + r""" $\cdot$ """
        + PHONE
        + r"""

\section*{EXPERIENCE}
\textbf{Senior Analyst}, """
        + EMPLOYER
        + r"""
"""
        + filler(40)
        + r"""
\newpage
"""
        + filler(40)
        + r"""
\newpage
"""
        + filler(40)
        + r"""
\newpage
\section*{EDUCATION}
BSc
\section*{SKILLS}
Python
\end{document}
"""
    )
    pdf = compile_tex(tmp_path, "toolong", source)
    report = verify_pdf(pdf, kind="resume", expect=expectations())

    assert not report.passed
    assert "page_limit" in [c.name for c in report.failures], report.summary()
    assert report.pages > 2


def test_resume_missing_the_skills_section_is_rejected(tmp_path):
    source = (
        PREAMBLE
        + r"""
\begin{document}
{\LARGE\bfseries """
        + NAME
        + r"""}\\
"""
        + EMAIL
        + r""" $\cdot$ """
        + PHONE
        + r"""

\section*{EXPERIENCE}
\textbf{Senior Analyst}, """
        + EMPLOYER
        + r"""
"""
        + filler(3)
        + r"""

\section*{EDUCATION}
BSc Computer Science
\end{document}
"""
    )
    pdf = compile_tex(tmp_path, "noskills", source)
    report = verify_pdf(pdf, kind="resume", expect=expectations())

    assert not report.passed
    assert "standard_sections_present" in [c.name for c in report.failures], (
        report.summary()
    )


def test_claimed_keywords_that_did_not_survive_are_rejected(tmp_path):
    """Catches a template that silently dropped the skills block."""
    pdf = compile_tex(tmp_path, "goodkw", GOOD_RESUME)
    report = verify_pdf(
        pdf,
        kind="resume",
        expect=expectations(claimed_keywords=["Python", "Kubernetes", "Terraform"]),
    )
    assert not report.passed
    failure = next(c for c in report.failures if c.name == "claimed_keywords_present")
    assert "Kubernetes" in failure.detail


def test_a_truncated_pdf_is_a_failed_report_not_an_exception(tmp_path):
    """A corrupt file must never crash the caller."""
    pdf = compile_tex(tmp_path, "corrupt", GOOD_RESUME)
    data = pdf.read_bytes()
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(data[: len(data) // 3])

    report = verify_pdf(broken, kind="resume", expect=expectations())
    assert report.passed is False
    assert report.failures


def test_a_missing_file_is_a_failed_report(tmp_path):
    report = verify_pdf(tmp_path / "nope.pdf", kind="resume")
    assert report.passed is False
    assert report.failures[0].name == "file_exists"


def test_ligature_canaries_must_survive_extraction(tmp_path):
    """The check that catches "efficient" extracting as "e cient"."""
    pdf = compile_tex(tmp_path, "ligature", GOOD_RESUME)
    report = verify_pdf(pdf, kind="resume", expect=expectations())
    canary = next(c for c in report.checks if c.name == "no_ligature_corruption")
    assert canary.passed, canary.detail


def test_a_word_absent_from_the_document_fails_the_canary_check(tmp_path):
    """Proves the canary check can actually fail, not just pass vacuously."""
    pdf = compile_tex(tmp_path, "canaryneg", GOOD_RESUME)
    report = verify_pdf(
        pdf, kind="resume", expect=expectations(employers=["Nonexistent Holdings"])
    )
    assert not report.passed
    assert "no_ligature_corruption" in [c.name for c in report.failures]


def test_cover_letter_page_limit_is_one(tmp_path):
    source = (
        PREAMBLE
        + r"""
\begin{document}
{\large\bfseries """
        + NAME
        + r"""}\\
"""
        + EMAIL
        + r"""

Dear Hiring Team,

"""
        + filler(60)
        + r"""
\newpage
"""
        + filler(60)
        + r"""
\end{document}
"""
    )
    pdf = compile_tex(tmp_path, "longletter", source)
    report = verify_pdf(
        pdf, kind="cover_letter", expect=ParseExpectations(name=NAME, email=EMAIL)
    )
    assert not report.passed
    assert "page_limit" in [c.name for c in report.failures]


def test_a_date_sharing_the_contact_line_is_rejected(tmp_path):
    """The location field must not have the letter's date absorbed into it.

    Found on the first real Windows build. The cover letter template put the
    date in its own paragraph, but the engine renders with trim_blocks=True,
    which ate the newline after the block end tag and so consumed the blank
    line between them. LaTeX joined the two, and every cover letter extracted
    its contact line as "... Adelaide SA 01 September 2026" — an ATS reading
    the trailing run of that line as the location gets a date stapled to it.
    Nothing in the gate noticed; all eight checks passed.
    """
    source = (
        PREAMBLE
        + r"""
\begin{document}
{\large\bfseries """
        + NAME
        + r"""}\
"""
        + EMAIL
        + r""" $\cdot$ """
        + PHONE
        + r""" $\cdot$ Adelaide SA
01 September 2026

Dear Hiring Team,

"""
        + BODY_WORDS
        + r"""
\end{document}
"""
    )
    pdf = compile_tex(tmp_path, "gluedate", source)
    report = verify_pdf(
        pdf, kind="cover_letter", expect=ParseExpectations(name=NAME, email=EMAIL)
    )
    assert not report.passed
    assert "contact_line_uncontaminated" in [c.name for c in report.failures]


def test_a_date_on_its_own_line_is_accepted(tmp_path):
    """The corrected shape must pass, or the check above is just noise."""
    source = (
        PREAMBLE
        + r"""
\begin{document}
{\large\bfseries """
        + NAME
        + r"""}\
"""
        + EMAIL
        + r""" $\cdot$ """
        + PHONE
        + r""" $\cdot$ Adelaide SA
\par
01 September 2026

Dear Hiring Team,

"""
        + BODY_WORDS
        + r"""
\end{document}
"""
    )
    pdf = compile_tex(tmp_path, "cleandate", source)
    report = verify_pdf(
        pdf, kind="cover_letter", expect=ParseExpectations(name=NAME, email=EMAIL)
    )
    assert report.passed, report.summary()


def test_the_contaminated_line_is_found_even_when_a_clean_one_precedes_it(tmp_path):
    """combined.pdf carries two contact blocks; the second one still counts.

    This is the case that matters most in practice: combined.pdf is what gets
    attached wherever a form has a single upload slot, and its first contact
    line — the resume's — is clean. Checking only the first match let the
    contaminated cover-letter page through.
    """
    source = (
        PREAMBLE
        + r"""
\begin{document}
{\large\bfseries """
        + NAME
        + r"""}\
"""
        + EMAIL
        + r""" $\cdot$ Adelaide SA

"""
        + BODY_WORDS
        + r"""
\newpage
{\large\bfseries """
        + NAME
        + r"""}\
"""
        + EMAIL
        + r""" $\cdot$ Adelaide SA 01 September 2026

"""
        + BODY_WORDS
        + r"""
\end{document}
"""
    )
    pdf = compile_tex(tmp_path, "combineddate", source)
    report = verify_pdf(
        pdf, kind="combined", expect=ParseExpectations(name=NAME, email=EMAIL)
    )
    assert not report.passed
    assert "contact_line_uncontaminated" in [c.name for c in report.failures]


# --------------------------------------------- merged words (blind spot five)
#
# Found by reading the extracted text of nine PDFs that had just passed every
# check the gate had. The document looked perfect and the ATS would have read
# "Promptpipelinetunedforfactualgroundingsothemodelreshapesrealexperience".
#
# The mechanism is arithmetic, not luck: pdfplumber starts a new word when the
# gap between two characters exceeds x_tolerance, which defaults to 3pt. In
# lmodern the interword space is 0.333em, so at a 10pt base it is 3.33pt.
# \small makes it 3.0pt, which does not exceed 3.0 and dies on any line
# justification squeezes; \footnotesize makes it 2.66pt, which dies on every
# line. The fixture uses \footnotesize so the failure is arithmetic rather than
# dependent on where TeX happens to break a line.

SMALL_TEXT_PREAMBLE = r"""
\documentclass[10pt,a4paper]{article}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern}
\usepackage[a4paper,margin=1.3cm]{geometry}
\pagestyle{empty}
\setlength{\parindent}{0pt}
"""


def _resume_with_summary(summary: str) -> str:
    """A resume that is correct in every way except the summary block."""
    return (
        SMALL_TEXT_PREAMBLE
        + r"""
\begin{document}
{\LARGE\bfseries """
        + NAME
        + r"""}\\
"""
        + EMAIL
        + r""" $\cdot$ """
        + PHONE
        + r""" $\cdot$ Adelaide SA

\section*{SUMMARY}
"""
        + summary
        + r"""

\section*{EXPERIENCE}
\textbf{Senior Analyst} \hfill 2021 -- 2026\\
\textit{"""
        + EMPLOYER
        + r"""}, Adelaide
\begin{itemize}
  \item """
        + BODY_WORDS
        + r"""
\end{itemize}

\section*{EDUCATION}
\textbf{BSc Computer Science} \hfill 2020\\
\textit{University of Adelaide}

\section*{SKILLS}
Python, SQL, financial modelling
\end{document}
"""
    )


def test_words_squeezed_together_by_a_small_font_are_rejected(tmp_path):
    pdf = compile_tex(
        tmp_path,
        "smalltext",
        _resume_with_summary(r"{\footnotesize " + filler(3) + "}"),
    )
    report = verify_pdf(pdf, kind="resume", expect=expectations())

    failures = [c.name for c in report.failures]
    assert "word_spacing_survives_extraction" in failures, report.summary()

    # It is not failing for some other reason: this is the ONLY thing wrong with
    # the document. Without this the assertion above would still pass if the
    # fixture were broken in some entirely different way.
    assert failures == ["word_spacing_survives_extraction"], failures

    detail = next(
        c.detail
        for c in report.failures
        if c.name == "word_spacing_survives_extraction"
    )
    assert "no space between them" in detail

    # And the premise holds: a standard extractor really is losing word
    # boundaries this one keeps. Asserted on the mechanism rather than on a
    # particular word, so the test does not quietly stop meaning anything when
    # TeX breaks a line somewhere else.
    with pdfplumber.open(str(pdf)) as document:
        loose = sum(len(page.extract_words()) for page in document.pages)
        tight = sum(len(page.extract_words(x_tolerance=1.2)) for page in document.pages)
    assert tight > loose, f"fixture is not actually merging words: {loose} vs {tight}"


def test_the_same_resume_at_a_readable_size_is_accepted(tmp_path):
    """The control. Identical words and layout, one usable font size."""
    pdf = compile_tex(tmp_path, "readable", _resume_with_summary(filler(3)))
    report = verify_pdf(pdf, kind="resume", expect=expectations())
    assert report.passed, report.summary()


def test_a_long_url_is_not_mistaken_for_merged_words(tmp_path):
    """The obvious false positive, checked rather than assumed.

    "github.com/4mohdisa/Crime-Management-System" is a single long token with no
    interword gap for either extraction pass to split on, so it must never be
    reported as merged words.
    """
    pdf = compile_tex(
        tmp_path,
        "withurl",
        _resume_with_summary(
            filler(3) + r"\\ github.com/4mohdisa/Crime-Management-System"
        ),
    )
    report = verify_pdf(pdf, kind="resume", expect=expectations())
    assert report.passed, report.summary()
    assert "Crime-Management-System" in report.extracted_text


# ---------------------------------------- run-together records (blind spot six)
#
# Also found by reading text that had passed everything. \vspace does not end a
# paragraph, so an EDUCATION block written with \vspace between entries and no
# \par extracted as one run:
#
#     Professional Year Program, ICT Adelaide, SA Torrens University Australia
#
# An ATS segments work history and education by line. The second institution
# landing mid-line is filed under the first entry, with the wrong dates.

INSTITUTIONS = ("Torrens University Australia", "Performance Education")


def _education_resume(separator: str) -> str:
    return (
        PREAMBLE
        + r"""
\begin{document}
{\LARGE\bfseries """
        + NAME
        + r"""}\\
"""
        + EMAIL
        + r""" $\cdot$ """
        + PHONE
        + r""" $\cdot$ Adelaide SA

\section*{EXPERIENCE}
\textbf{Senior Analyst} \hfill 2021 -- 2026\\
\textit{"""
        + EMPLOYER
        + r"""}, Adelaide
\begin{itemize}
  \item """
        + BODY_WORDS
        + r"""
\end{itemize}

\section*{EDUCATION}
\textbf{"""
        + INSTITUTIONS[1]
        + r"""} \hfill 2026\\
\textit{Professional Year Program, ICT} Adelaide, SA
"""
        + separator
        + r"""
\textbf{"""
        + INSTITUTIONS[0]
        + r"""} \hfill 2025\\
\textit{Bachelor of Information Technology} Adelaide, SA

\section*{SKILLS}
Python, SQL, financial modelling
\end{document}
"""
    )


def test_a_record_heading_buried_mid_line_is_rejected(tmp_path):
    """\\vspace alone does not break the paragraph, so the records run together."""
    pdf = compile_tex(tmp_path, "runtogether", _education_resume(r"\vspace{4pt}"))
    report = verify_pdf(
        pdf, kind="resume", expect=expectations(line_starts=list(INSTITUTIONS))
    )

    failures = [c.name for c in report.failures]
    assert "record_headings_start_a_line" in failures, report.summary()
    assert failures == ["record_headings_start_a_line"], failures

    detail = next(
        c.detail for c in report.failures if c.name == "record_headings_start_a_line"
    )
    assert INSTITUTIONS[0] in detail

    # The premise: the two records really are on one extracted line. Without
    # this the test would still pass if the fixture had simply omitted the
    # second institution, which is a different bug entirely.
    assert any(
        INSTITUTIONS[0] in line and "Adelaide, SA" in line
        for line in report.extracted_text.splitlines()
    ), report.extracted_text


def test_the_same_records_separated_by_par_are_accepted(tmp_path):
    """The control. One \\par is the whole difference."""
    pdf = compile_tex(tmp_path, "separated", _education_resume(r"\par\vspace{4pt}"))
    report = verify_pdf(
        pdf, kind="resume", expect=expectations(line_starts=list(INSTITUTIONS))
    )
    assert report.passed, report.summary()


# --------------------------------------- one-extractor facts (blind spot seven)
#
# Every content check above reads whichever of the two extractions came out
# longer, which is always pdfplumber — it rebuilds words from glyph positions.
# pypdf reads the content stream instead and inserts a space wherever pdfTeX
# emitted a tightening kern, so in lmodern BOLD:
#
#     Wemark Real Estate  ->  W emark Real Estate
#     Vericent            ->  V ericent
#     Feb 2026            ->  F eb 2026
#
# Two of five employers and one of three institutions were unfindable by a
# whole class of parser, on a document that passed all fifteen checks — because
# the pypdf text was discarded before any content check read it.


def _kerned_resume(wrap: str) -> str:
    """A resume whose employer name is set with ``wrap`` applied."""
    return (
        PREAMBLE
        + r"""
\begin{document}
{\LARGE """
        + NAME
        + r"""}\\
"""
        + EMAIL
        + r""" $\cdot$ """
        + PHONE
        + r""" $\cdot$ Adelaide SA

\section*{EXPERIENCE}
"""
        + wrap % "Wemark Real Estate"
        + r""" \hfill 2021 -- 2026\\
\textit{Senior Analyst}, Adelaide
\begin{itemize}
  \item """
        + BODY_WORDS
        + r"""
\end{itemize}

\section*{EDUCATION}
BSc Computer Science \hfill 2020\\
\textit{University of Adelaide}

\section*{SKILLS}
Python, SQL, financial modelling
\end{document}
"""
    )


def test_a_fact_only_one_extractor_can_find_is_rejected(tmp_path):
    """Bold splits "Wemark" into "W emark" for pypdf and nothing else notices."""
    pdf = compile_tex(tmp_path, "kerned", _kerned_resume(r"\textbf{%s}"))
    report = verify_pdf(
        pdf,
        kind="resume",
        expect=expectations(employers=["Wemark Real Estate"]),
    )

    failures = [c.name for c in report.failures]
    assert "facts_survive_both_extractors" in failures, report.summary()
    assert failures == ["facts_survive_both_extractors"], failures

    detail = next(
        c.detail for c in report.failures if c.name == "facts_survive_both_extractors"
    )
    assert "Wemark Real Estate" in detail

    # The premise, asserted rather than assumed: pdfplumber CAN find it, pypdf
    # cannot, and it is that disagreement the check exists for.
    from backend.documents.verify import _extract_pdfplumber, _extract_pypdf

    assert "Wemark Real Estate" in _extract_pdfplumber(pdf)[0]
    assert "Wemark Real Estate" not in _extract_pypdf(pdf)[0]


def test_the_same_name_unbolded_is_accepted(tmp_path):
    """The control. Same word, same line, same font — one weight lighter."""
    pdf = compile_tex(tmp_path, "unkerned", _kerned_resume("%s"))
    report = verify_pdf(
        pdf,
        kind="resume",
        expect=expectations(employers=["Wemark Real Estate"]),
    )
    assert report.passed, report.summary()


# ------------------------------- truncated contact line (blind spot eight)
#
# The contact strip is one centred line of phone, email, location and links.
# Make one field too wide and TeX wraps it, stranding the separator that
# preceded it at the end of the line — so an ATS reading the trailing run of
# the contact line as the location reads an empty string instead.


def _contact_resume(contact: str) -> str:
    return (
        PREAMBLE
        + r"""
\begin{document}
\begin{center}
{\LARGE """
        + NAME
        + r"""}\\
"""
        + contact
        + r"""
\end{center}

\section*{EXPERIENCE}
"""
        + EMPLOYER
        + r""" \hfill 2021 -- 2026\\
\textit{Senior Analyst}, Adelaide
\begin{itemize}
  \item """
        + BODY_WORDS
        + r"""
\end{itemize}

\section*{EDUCATION}
BSc Computer Science \hfill 2020\\
\textit{University of Adelaide}

\section*{SKILLS}
Python, SQL, financial modelling
\end{document}
"""
    )


OVERFLOWING_CONTACT = (
    PHONE
    + r""" $|$ """
    + EMAIL
    + r""" $|$ Adelaide SA 5000 $|$ linkedin.com/in/jordan-fitzgerald-analytics
$|$ github.com/jordan-fitzgerald-analytics $|$ jordanfitzgeraldanalytics.example.com"""
)

FITTING_CONTACT = (
    PHONE
    + r""" $|$ """
    + EMAIL
    + r""" $|$ Adelaide SA 5000\\
linkedin.com/in/jordan-fitzgerald-analytics $|$ github.com/jordan-fitzgerald-analytics"""
)


def test_a_contact_line_with_a_field_wrapped_off_it_is_rejected(tmp_path):
    pdf = compile_tex(tmp_path, "overflow", _contact_resume(OVERFLOWING_CONTACT))
    report = verify_pdf(pdf, kind="resume", expect=expectations())

    failures = [c.name for c in report.failures]
    assert "contact_line_not_truncated" in failures, report.summary()
    assert failures == ["contact_line_not_truncated"], failures

    # The premise: the contact line really does end on a stranded separator.
    contact_line = next(
        line for line in report.extracted_text.splitlines() if EMAIL in line
    )
    assert contact_line.rstrip().endswith("|"), repr(contact_line)


def test_a_contact_block_deliberately_split_over_two_lines_is_accepted(tmp_path):
    """The control, and the false positive that matters.

    A two-line contact block is a completely ordinary Australian resume header.
    The check must key on the stranded separator, not on which fields it can
    find, or it rejects this.
    """
    pdf = compile_tex(tmp_path, "twoline", _contact_resume(FITTING_CONTACT))
    report = verify_pdf(pdf, kind="resume", expect=expectations())
    assert report.passed, report.summary()
