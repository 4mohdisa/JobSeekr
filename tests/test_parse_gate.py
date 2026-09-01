"""The parse gate, tested adversarially — the only test suite Block C needs.

Each broken PDF here is generated for real with pdflatex and represents a
document that looks fine to a human and is broken for an ATS. A gate that only
passes good documents is worthless; what matters is that it REJECTS these.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.documents.verify import ParseExpectations, verify_pdf
from tests.conftest import needs_pdflatex

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
            "pdflatex",
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
            claimed_keywords=["Python", "SQL"], section_order=["EXPERIENCE", "EDUCATION"]
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
        pdf, kind="resume", expect=expectations(name=NAME, email="not.in.body@example.com")
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
    assert "standard_sections_present" in [c.name for c in report.failures], report.summary()


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
    report = verify_pdf(pdf, kind="cover_letter", expect=ParseExpectations(name=NAME, email=EMAIL))
    assert not report.passed
    assert "page_limit" in [c.name for c in report.failures]
