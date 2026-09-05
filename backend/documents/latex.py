"""LaTeX escaping — the single implementation in this codebase.

A user's real employer is called "Smith & Co", their achievement is "100%
uptime", their language is "C#", and their team did "R&D". Every one of those
characters is LaTeX syntax. Unescaped, they do not produce a slightly wrong
PDF — they produce a build that fails, or worse, one that silently swallows
the rest of a line.

Nothing else may reimplement this. The engine applies it automatically to
every substituted value, so a template author cannot forget it.
"""

from __future__ import annotations

import re

__all__ = ["escape_latex", "latex_safe_url"]


# The backslash is handled via a sentinel rather than replaced in place.
# Replacing it first with its final form (\textbackslash{}) does not work: the
# braces that form introduces are themselves escaped by the { and } rules
# below, yielding \textbackslash\{\}. Replacing it last does not work either,
# because every other rule introduces a backslash. A sentinel that contains no
# special character sidesteps both.
_BACKSLASH_SENTINEL = "\x00BACKSLASH\x00"

# LaTeX's own ligatures rewrite ASCII the user typed literally: ' becomes a
# curly U+2019 and -- becomes an en-dash. Typographically that is correct, and
# for prose it is what we want. For a resume it is a liability: an employer
# named "Dan Murphy's" extracts as "Dan Murphy’s", and a date range written
# "2020--2024" extracts with an en-dash, so an ATS searching the literal string
# finds nothing. The profile is the source of truth for facts, so ASCII the
# user typed must extract as ASCII.
#
# Note the deliberate asymmetry with _UNICODE_FIXUPS below: a real U+2019 or
# U+2014 in the input is mapped INTO LaTeX ligature source, so it round-trips
# back to the same character. Only ASCII is pinned. These sentinels are
# restored last because their replacements contain braces and backslashes that
# the escaping rules would otherwise mangle.
_APOSTROPHE_SENTINEL = "\x00APOS\x00"
_HYPHEN_BREAK_SENTINEL = "\x00HYPHENBREAK\x00"

_ASCII_APOSTROPHE = re.compile(r"'")
_ASCII_HYPHEN_RUN = re.compile(r"-{2,}")

_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("\\", _BACKSLASH_SENTINEL),
    ("&", r"\&"),
    ("%", r"\%"),
    ("$", r"\$"),
    ("#", r"\#"),
    ("_", r"\_"),
    ("{", r"\{"),
    ("}", r"\}"),
    ("~", r"\textasciitilde{}"),
    ("^", r"\textasciicircum{}"),
)

# Characters that arrive from web-scraped ad text and have no pdflatex glyph in
# the T1 encoding. Mapped to equivalents rather than dropped, because a missing
# dash changes meaning and a "Missing character" warning is easy to miss.
_UNICODE_FIXUPS: tuple[tuple[str, str], ...] = (
    ("‘", "`"),
    ("’", "'"),
    ("“", "``"),
    ("”", "''"),
    ("–", "--"),
    ("—", "---"),
    ("…", r"\ldots{}"),
    (" ", "~"),
    ("•", r"\textbullet{}"),
    ("→", r"$\rightarrow$"),
    ("·", r"\textperiodcentered{}"),
    ("−", "-"),
    ("﻿", ""),
)

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def escape_latex(value: object) -> str:
    """Make any value safe to substitute into a LaTeX document.

    Non-strings are stringified first so a template can interpolate an int or a
    date without special-casing. ``None`` becomes an empty string rather than
    the literal "None", which is what a missing optional profile field should
    render as.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)

    text = _CONTROL_CHARS.sub("", text)

    # Pin ASCII apostrophes and hyphen runs BEFORE the Unicode fixups, which
    # deliberately produce ligature source of their own.
    text = _ASCII_APOSTROPHE.sub(_APOSTROPHE_SENTINEL, text)
    text = _ASCII_HYPHEN_RUN.sub(
        lambda match: _HYPHEN_BREAK_SENTINEL.join("-" * len(match.group())), text
    )

    for source, target in _UNICODE_FIXUPS:
        text = text.replace(source, target)
    for source, target in _REPLACEMENTS:
        text = text.replace(source, target)
    text = text.replace(_BACKSLASH_SENTINEL, r"\textbackslash{}")
    # \textquotesingle is the upright U+0027; {} merely breaks the -- ligature.
    text = text.replace(_APOSTROPHE_SENTINEL, r"\textquotesingle{}")
    return text.replace(_HYPHEN_BREAK_SENTINEL, "{}")


def latex_safe_url(value: object) -> str:
    """Escape a URL for ``\\href``.

    URLs need different treatment: ``%`` and ``#`` are meaningful inside them,
    and ``~`` is common in paths, so they are escaped for LaTeX without the
    text-mode substitutions that would corrupt the address.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = _CONTROL_CHARS.sub("", text).strip()
    for source, target in (
        ("\\", r"\\"),
        ("%", r"\%"),
        ("#", r"\#"),
        ("{", r"\{"),
        ("}", r"\}"),
    ):
        text = text.replace(source, target)
    return text
