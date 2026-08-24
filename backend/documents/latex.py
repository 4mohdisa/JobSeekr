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
    for source, target in _UNICODE_FIXUPS:
        text = text.replace(source, target)
    for source, target in _REPLACEMENTS:
        text = text.replace(source, target)
    return text.replace(_BACKSLASH_SENTINEL, r"\textbackslash{}")


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
    for source, target in (("\\", r"\\"), ("%", r"\%"), ("#", r"\#"), ("{", r"\{"), ("}", r"\}")):
        text = text.replace(source, target)
    return text
