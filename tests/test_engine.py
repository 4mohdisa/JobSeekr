# =========================================================================
# Double-escaping regression
# =========================================================================


def test_filters_that_escape_do_not_get_escaped_again():
    """A skill of "C#" must reach the PDF as C#, not as a literal C\\#.

    Found by compiling the real resume and reading the extracted text: the
    escaping filters returned plain str, so the finalize hook escaped their
    output a second time and the escape itself became content. The PDF looked
    almost right and the parse gate passed, but an ATS searching for "C#"
    found nothing — the exact failure the document pipeline exists to prevent.
    """
    from backend.documents.engine import render_string

    rendered = render_string(
        r"\VAR{skills|join_latex(', ')}",
        {"skills": ["C#", "R&D", "100% remote", "Smith & Co"]},
    )

    # Escaped exactly once: a single backslash before each special character.
    assert r"C\#" in rendered
    assert r"R\&D" in rendered
    assert r"100\% remote" in rendered
    # Never twice — that is what typesets a visible backslash.
    assert r"\textbackslash" not in rendered
    assert "\\\\#" not in rendered


def test_the_latex_filter_is_not_double_escaped():
    from backend.documents.engine import render_string

    rendered = render_string(r"\VAR{value|latex}", {"value": "Smith & Co"})
    assert r"Smith \& Co" in rendered
    assert r"\textbackslash" not in rendered


def test_plain_substitution_is_still_escaped_once():
    """The finalize hook must keep protecting unfiltered values."""
    from backend.documents.engine import render_string

    rendered = render_string(r"\VAR{value}", {"value": "Smith & Co"})
    assert r"Smith \& Co" in rendered
