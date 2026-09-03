"""One template engine, three artifact kinds.

Resume, cover letter and outbound email all render through the environment
built here. Writing three engines would mean three escaping rules, three
placeholder vocabularies and three places for a bug to hide.

LaTeX already owns ``{``, ``}`` and ``%``, so Jinja's defaults would collide
with the document syntax. The delimiters below are the ones the spec fixes:

    \\BLOCK{for job in experience} ... \\BLOCK{endfor}
    \\VAR{profile.name}
    \\#{a comment}

Two kinds of placeholder, and the difference matters:

``\\VAR{profile.name}``, ``\\VAR{job.company}``
    Deterministic substitution from the database. Free, repeatable, and
    incapable of inventing anything.

``\\VAR{ai.opening_hook}``
    Generated per job by the LLM, under a word limit and a tone, and validated
    against the profile before it is allowed into a document.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateSyntaxError

from backend.documents.latex import escape_latex, latex_safe_url
from backend.logging_setup import get_logger

log = get_logger(__name__)

__all__ = [
    "SLOT_SPECS",
    "AISlot",
    "PlaceholderIssue",
    "build_environment",
    "find_ai_slots",
    "render_string",
    "template_root",
    "validate_placeholders",
]


def template_root() -> Path:
    """Where the shipped .j2 templates live."""
    return Path(__file__).resolve().parent.parent.parent / "templates"


@dataclass(frozen=True)
class AISlot:
    """One LLM-generated passage in a template.

    ``max_words`` is enforced on the output, not merely requested in the
    prompt — a cover letter that runs to three pages because the model was
    chatty fails the parse gate's page limit, which is a confusing way to
    discover a prompt problem.
    """

    name: str
    max_words: int
    tone: str
    instruction: str


SLOT_SPECS: dict[str, AISlot] = {
    "opening_hook": AISlot(
        name="opening_hook",
        max_words=45,
        tone="direct, specific, no throat-clearing",
        instruction=(
            "Open the cover letter by connecting one concrete thing the candidate has "
            "actually done to what this role needs. Name the thing. Do not open with "
            "'I am writing to apply for' or 'I was excited to see'."
        ),
    ),
    "why_company": AISlot(
        name="why_company",
        max_words=60,
        tone="genuine, specific to this employer",
        instruction=(
            "Say why this employer specifically, using only what the job ad itself "
            "reveals about them. If the ad reveals nothing distinctive, write about the "
            "work rather than inventing praise for the company."
        ),
    ),
    "skills_bridge": AISlot(
        name="skills_bridge",
        max_words=90,
        tone="evidence-led",
        instruction=(
            "Connect the candidate's demonstrated experience to the ad's main "
            "requirements. Reference only experience present in the profile. Where "
            "there is a gap, either omit it or name adjacent real experience — never "
            "imply the gap is filled."
        ),
    ),
    "closing": AISlot(
        name="closing",
        max_words=35,
        tone="warm, brief, not servile",
        instruction=(
            "Close with a short, confident line about next steps. No 'thank you for "
            "your time and consideration'."
        ),
    ),
}


_VAR_RE = re.compile(r"\\VAR\{\s*([^}]+?)\s*\}")
# A stray Jinja-default placeholder: almost always a typo by someone who forgot
# this project's delimiters. Reported rather than silently rendered as text.
_STRAY_MUSTACHE_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")

# The deterministic namespaces a template may read.
KNOWN_ROOTS = {"profile", "job", "campaign", "today", "ai", "documents"}

# Fields the UI offers as autocomplete and validates typos against. Kept here
# so the API, the frontend and the templates cannot disagree about the
# vocabulary.
KNOWN_FIELDS: dict[str, tuple[str, ...]] = {
    "profile": (
        "name",
        "email",
        "phone",
        "location",
        "headline",
        "summary",
        "linkedin",
        "website",
        "work_rights",
        "experience",
        "projects",
        "education",
        "certifications",
        "skills",
    ),
    "job": ("title", "company", "location", "url", "source", "salary", "contact_email"),
    "campaign": ("name",),
    "today": ("iso", "long"),
    "ai": tuple(SLOT_SPECS),
}


@dataclass(frozen=True)
class PlaceholderIssue:
    """A problem found in a template body, for the editor to display inline."""

    placeholder: str
    kind: str  # unknown_root | unknown_field | wrong_delimiters | syntax_error
    detail: str


def build_environment(searchpath: Path | None = None) -> Environment:
    """The one Jinja environment. LaTeX-safe delimiters, autoescape OFF.

    ``autoescape`` is False because Jinja's HTML escaping is meaningless here;
    LaTeX escaping is applied instead, automatically, via a finalize hook so a
    template author cannot forget the filter.
    """
    env = Environment(
        loader=FileSystemLoader(str(searchpath or template_root())),
        block_start_string=r"\BLOCK{",
        block_end_string="}",
        variable_start_string=r"\VAR{",
        variable_end_string="}",
        comment_start_string=r"\#{",
        comment_end_string="}",
        line_statement_prefix="%%",
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    # Every one of these filters RETURNS ALREADY-ESCAPED LaTeX, so each must be
    # marked RawLatex or the finalize hook escapes it a second time. That second
    # pass turns the escape itself into content: a skill of "C#" is escaped to
    # "C\#", escaped again to "C\textbackslash{}\#", and typesets as the literal
    # text "C\#". The PDF looks almost right, the parse gate passes, and an ATS
    # searching for "C#" finds nothing — which is the precise failure this whole
    # pipeline exists to prevent.
    env.filters["latex"] = lambda value: RawLatex(escape_latex(value))
    env.filters["url"] = lambda value: RawLatex(latex_safe_url(value))
    env.filters["join_latex"] = lambda items, sep=", ": RawLatex(
        sep.join(escape_latex(item) for item in (items or []))
    )
    return env


def _escaping_finalize(value: Any) -> str:
    """Escape every substituted value unless it is already marked raw."""
    if isinstance(value, RawLatex):
        return str(value)
    return escape_latex(value)


class RawLatex(str):
    """Text that is already valid LaTeX and must not be escaped again.

    Used for the rendered AI passages, which are escaped once when they are
    validated and would otherwise be double-escaped on substitution.
    """

    __slots__ = ()


def _env_for_latex(searchpath: Path | None = None) -> Environment:
    env = build_environment(searchpath)
    env.finalize = _escaping_finalize
    return env


def render_string(body: str, context: dict[str, Any], *, latex: bool = True) -> str:
    """Render a template body held in the database (the usual case).

    Templates are edited in the dashboard and stored in the ``template`` table,
    so most rendering starts from a string rather than a file.
    """
    env = _env_for_latex() if latex else build_environment()
    return env.from_string(body).render(**context)



def find_ai_slots(body: str) -> list[AISlot]:
    """The AI slots a template actually uses, in declaration order.

    Only slots present in the body are generated — a template that does not use
    ``why_company`` must not pay for it.
    """
    seen: list[str] = []
    for match in _VAR_RE.finditer(body):
        expression = match.group(1)
        root, _, rest = expression.partition(".")
        if root.strip() != "ai":
            continue
        field = rest.split("|")[0].split("[")[0].strip()
        if field and field not in seen:
            seen.append(field)

    slots: list[AISlot] = []
    for name in seen:
        spec = SLOT_SPECS.get(name)
        if spec is None:
            log.warning("unknown_ai_slot", slot=name, known=sorted(SLOT_SPECS))
            continue
        slots.append(spec)
    return slots


def validate_placeholders(body: str) -> list[PlaceholderIssue]:
    """Find placeholder mistakes in a template body.

    Returns structured issues rather than printing, because the dashboard's
    template editor renders them inline next to the offending line. This is
    what catches ``\\VAR{job.compnay}`` before it reaches a real application.
    """
    issues: list[PlaceholderIssue] = []

    try:
        env = build_environment()
        env.parse(body)
    except TemplateSyntaxError as exc:
        issues.append(
            PlaceholderIssue(
                placeholder=(exc.source or "").splitlines()[exc.lineno - 1][:80]
                if exc.source and exc.lineno
                else "",
                kind="syntax_error",
                detail=f"line {exc.lineno}: {exc.message}",
            )
        )

    for match in _STRAY_MUSTACHE_RE.finditer(body):
        issues.append(
            PlaceholderIssue(
                placeholder="{{" + match.group(1) + "}}",
                kind="wrong_delimiters",
                detail=(
                    "This project uses LaTeX-safe delimiters. Write "
                    rf"\VAR{{{match.group(1)}}} instead."
                ),
            )
        )

    for match in _VAR_RE.finditer(body):
        expression = match.group(1).strip()
        # Ignore anything that is an expression rather than a plain field path.
        base = expression.split("|")[0].strip()
        root, _, rest = base.partition(".")
        root = root.strip()
        if not rest or any(ch in base for ch in "()' \""):
            continue
        if root not in KNOWN_ROOTS:
            issues.append(
                PlaceholderIssue(
                    placeholder=base,
                    kind="unknown_root",
                    detail=f"'{root}' is not a known namespace. Known: {sorted(KNOWN_ROOTS)}",
                )
            )
            continue
        known = KNOWN_FIELDS.get(root)
        if known is None:
            continue
        field = rest.split(".")[0].split("[")[0].strip()
        if field and field not in known:
            issues.append(
                PlaceholderIssue(
                    placeholder=base,
                    kind="unknown_field",
                    detail=f"'{root}' has no field '{field}'. Known: {list(known)}",
                )
            )

    return issues
