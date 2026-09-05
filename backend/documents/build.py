"""Build the three documents for one job, and let none of them through unchecked.

    uv run python -m backend.documents.build --job-id 42

Order of operations, and why:

1. Deterministic context from the database — free, repeatable, incapable of
   invention.
2. AI slots generated under a word cap, then **validated against the profile**
   and regenerated once on violation. Anything still unsupported fails the
   build; nothing unvalidated reaches a document.
3. pdflatex, two passes, aux files cleaned up.
4. resume.pdf, cover_letter.pdf, and combined.pdf (the default for forms with
   a single attachment slot).
5. The parse gate on every PDF. A failure marks the job failed with the full
   report attached and produces no usable document.

There is deliberately **no fallback to a previously built PDF**. A stale resume
that passes the gate is worse than no resume: it is the wrong document sent
confidently, and the readback check downstream cannot catch what was never
rebuilt.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pypdf import PdfWriter
from sqlmodel import Session, select

from backend import telemetry
from backend.config import settings
from backend.db import session_scope
from backend.documents.engine import (
    BULLETS_SLOT,
    SLOT_SPECS,
    AISlot,
    RawLatex,
    find_ai_slots,
    render_string,
    template_root,
)
from backend.documents.fabrication import Violation, validate_no_fabrication
from backend.documents.latex import escape_latex
from backend.documents.verify import ParseExpectations, ParseReport, verify_pdf
from backend.llm.client import llm
from backend.logging_setup import configure_logging, get_logger
from backend.models import (
    Campaign,
    Document,
    DocumentKind,
    Job,
    JobStatus,
    Profile,
    Score,
    Stage,
    Template,
    TemplateKind,
)

log = get_logger(__name__)

__all__ = [
    "BuildResult",
    "build_documents",
    "expected_verbatim",
    "generate_ai_slots",
    "generate_role_bullets",
    "render_pdf",
]


class DocumentBuildError(RuntimeError):
    """A build that must not produce a document."""


@dataclass
class BuildResult:
    job_id: int
    ok: bool
    documents: dict[str, Document] = field(default_factory=dict)
    reports: dict[str, ParseReport] = field(default_factory=dict)
    failure_reason: str | None = None
    violations: list[Violation] = field(default_factory=list)


# --------------------------------------------------------------------------
# Context
# --------------------------------------------------------------------------


# Optional per-row fields the templates reference. The engine runs with
# StrictUndefined — deliberately, because it is what catches a typo like
# `job.compnay` before a document reaches an employer — but that strictness
# also means `\BLOCK{if role.location}` RAISES when the key is simply absent
# rather than evaluating false. Every row is therefore normalised to carry the
# full key set, so an optional field that the user left blank stays optional.
#
# Found by compiling the real template against a profile shaped the way the
# Profile page actually produces one: its experience editor has no location
# field at all, so before this every resume build failed outright.
_ROW_DEFAULTS: dict[str, tuple[str, ...]] = {
    "experience": ("title", "company", "start", "end", "location", "highlights"),
    "projects": ("name", "stack", "description", "url", "status"),
    "education": ("qualification", "institution", "year", "location"),
    "certifications": ("name", "issuer", "year"),
}


def _normalise_rows(rows: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    """Give every row the full key set, so absent optionals read as empty."""
    out: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        filled = {key: row.get(key, "") for key in keys}
        # Preserve anything extra the user stored; templates ignore it.
        filled.update({k: v for k, v in row.items() if k not in filled})
        if "highlights" in keys and not filled.get("highlights"):
            filled["highlights"] = []
        out.append(filled)
    return out


# Characters whose escaping can silently corrupt a fact on its way into the PDF.
# Any highlight token containing one of these is worth asserting verbatim.
_RISKY_CHARS = frozenset("&#%$_~^{}+<>'\"\\")

# Bounds on a harvested fact. Too short and it matches by accident; too long and
# LaTeX may hyphenate it across a line break, which is a formatting artefact
# rather than a corruption.
_VERBATIM_MIN = 2
_VERBATIM_MAX = 60


def expected_verbatim(profile: Profile) -> list[str]:
    """Every fact the resume asserts, for the gate to confirm survived extraction.

    Harvested rather than hand-listed. The gate previously took a
    ``claimed_keywords`` list supplied by the caller, which meant its coverage
    was only as good as whoever wrote that list: a resume shipped with "C#"
    typeset as the literal "C\\#" and passed, because the keywords that day
    happened to be Python, SQL and FastAPI — none containing a character the
    escaper could corrupt.

    Anything an ATS would search for goes in: skills, employers, institutions,
    certifications, project names, and any highlight token carrying a character
    that escaping can mangle.
    """
    facts: list[str] = []

    def add(value: object) -> None:
        text = str(value or "").strip()
        if _VERBATIM_MIN <= len(text) <= _VERBATIM_MAX and "\n" not in text:
            facts.append(text)

    for skill in profile.skills or []:
        add(skill)

    for role in profile.experience or []:
        if isinstance(role, dict):
            add(role.get("company"))
            add(role.get("title"))
            # Individual tokens, not whole bullets: a wrapped sentence can be
            # hyphenated by LaTeX, which is formatting rather than corruption.
            for highlight in role.get("highlights") or []:
                for token in str(highlight).split():
                    stripped = token.strip(".,;:()[]")
                    if set(stripped) & _RISKY_CHARS:
                        add(stripped)

    for row in profile.education or []:
        if isinstance(row, dict):
            add(row.get("institution"))
            add(row.get("qualification"))

    for row in profile.certifications or []:
        if isinstance(row, dict):
            add(row.get("name"))
            add(row.get("issuer"))

    for row in profile.projects or []:
        if isinstance(row, dict):
            add(row.get("name"))

    # Stable order, no duplicates — the report reads better and is diffable.
    return list(dict.fromkeys(facts))


def _profile_context(profile: Profile) -> dict[str, Any]:
    """Flatten the profile's JSON into the template vocabulary.

    Templates read ``profile.name``, not ``profile.identity['name']``, so the
    mapping lives here — in one place — rather than in each template.
    """
    identity = profile.identity or {}
    work_rights = profile.work_rights or {}
    return {
        "name": identity.get("name", ""),
        "email": identity.get("email", ""),
        "phone": identity.get("phone", ""),
        "location": identity.get("location", ""),
        "headline": identity.get("headline", ""),
        "summary": identity.get("summary", ""),
        "linkedin": identity.get("linkedin", ""),
        "github": identity.get("github", ""),
        "website": identity.get("website", ""),
        "work_rights": work_rights.get("statement", ""),
        "references": identity.get("references", ""),
        "experience": _normalise_rows(profile.experience, _ROW_DEFAULTS["experience"]),
        "projects": _normalise_rows(profile.projects, _ROW_DEFAULTS["projects"]),
        "education": _normalise_rows(profile.education, _ROW_DEFAULTS["education"]),
        "certifications": _normalise_rows(
            profile.certifications, _ROW_DEFAULTS["certifications"]
        ),
        "skills": profile.skills or [],
    }


def _job_context(job: Job) -> dict[str, Any]:
    salary = ""
    if job.salary_min or job.salary_max:
        salary = f"{job.salary_min or ''}-{job.salary_max or ''}".strip("-")
    return {
        "title": job.title,
        "company": job.company,
        "location": job.location or "",
        "url": job.url,
        "source": job.source,
        "salary": salary,
        "contact_email": job.ad_contact_email or "",
    }


def _today_context() -> dict[str, str]:
    now = datetime.now(UTC)
    return {"iso": now.date().isoformat(), "long": now.strftime("%d %B %Y")}


# --------------------------------------------------------------------------
# AI slots
# --------------------------------------------------------------------------

_SLOT_SYSTEM = (
    "You write one short passage of an Australian job application for a "
    "specific candidate.\n"
    "ABSOLUTE RULE: you may not invent facts about the candidate. Employers, "
    "dates, job titles, certifications, licences, degrees, visa status, "
    "salary and any metric must appear in the CANDIDATE FACTS below or must "
    "not appear at all. If you cannot support a claim, write around it.\n"
    "You may phrase freely. You may not assert anything new.\n"
    "Return only the passage text: no preamble, no quotes, no markdown."
)


def _slot_prompt(
    slot: AISlot,
    *,
    profile_text: str,
    job: Job,
    requirements: dict[str, Any] | None = None,
    violations: list[Violation] | None = None,
) -> str:
    prompt = (
        f"CANDIDATE FACTS (the only facts you may assert)\n{profile_text}\n\n"
        f"THE ROLE\n"
        f"Title: {job.title}\nCompany: {job.company}\n"
        f"Location: {job.location or 'not stated'}\n"
        f"Advertisement:\n{(job.description or '')[: settings.scoring_prompt_char_budget]}\n\n"
        + _requirements_block(requirements)
        + f"WRITE: {slot.instruction}\n"
        f"TONE: {slot.tone}\n"
        f"HARD LIMIT: {slot.max_words} words.\n"
    )
    if violations:
        problems = "\n".join(f"- {v}" for v in violations)
        prompt += (
            "\nYour previous attempt asserted things the candidate's profile does "
            f"not support:\n{problems}\n"
            "Rewrite without those claims. Do not substitute different invented "
            "facts — write around the gap.\n"
        )
    return prompt


def _requirements_for(session: Session, job_id: int) -> dict[str, Any] | None:
    """The must-haves, nice-to-haves and tone extracted when this job was scored.

    Reads the newest Score for the job. Returns None rather than an empty dict
    when nothing was extracted, so the prompt builder can tell "scoring has not
    run" from "scoring ran and found no stated requirements".
    """
    row = session.exec(
        select(Score).where(Score.job_id == job_id).order_by(Score.id.desc())  # type: ignore[union-attr]
    ).first()
    requirements = getattr(row, "requirements", None) if row else None
    return requirements or None


def _requirements_block(requirements: dict[str, Any] | None) -> str:
    """What the employer asked for, extracted during scoring.

    Empty when scoring has not run or extracted nothing, in which case the
    prompt is exactly what it was before — the ad's own text is still there and
    the model can read it. This adds emphasis, not information the prompt
    lacked.
    """
    if not requirements:
        return ""

    must = requirements.get("must_haves") or []
    nice = requirements.get("nice_to_haves") or []
    tone = requirements.get("tone")

    lines = ["WHAT THIS EMPLOYER ACTUALLY ASKED FOR"]
    if must:
        lines.append("Non-negotiable: " + "; ".join(str(item) for item in must))
    if nice:
        lines.append("Desirable: " + "; ".join(str(item) for item in nice))
    if tone:
        lines.append(f"The ad's register: {tone}")
    lines.append(
        "Address the non-negotiable items FIRST, and only where the candidate "
        "facts above support it. Where they do not, say nothing about it — do "
        "not gesture at it."
    )
    return "\n".join(lines) + "\n\n"


def _truncate_words(text: str, limit: int) -> str:
    words = text.split()
    if len(words) <= limit:
        return text.strip()
    log.warning("ai_slot_over_word_limit", limit=limit, actual=len(words))
    return " ".join(words[:limit]).rstrip(",;:") + "."


def generate_ai_slots(
    slots: list[AISlot],
    *,
    profile: Profile,
    job: Job,
    profile_text: str,
    requirements: dict[str, Any] | None = None,
) -> tuple[dict[str, str], list[Violation]]:
    """Generate each slot, validating that it invented nothing.

    One regeneration is allowed, with the specific violations fed back. Slots
    that still fail are returned as violations and the caller must fail the
    build — never quietly accept them.
    """
    generated: dict[str, str] = {}
    unresolved: list[Violation] = []

    for slot in slots:
        violations: list[Violation] = []
        text = ""
        for attempt in (1, 2):
            # Several candidates, then pick. A single generation has nothing to
            # lose to — the model's first attempt is accepted however flat it
            # is. Generating a few and judging them against what the ad actually
            # asked for is where the cheap-model budget is worth spending.
            candidates = [
                _truncate_words(
                    llm.complete(
                        _slot_prompt(
                            slot,
                            profile_text=profile_text,
                            job=job,
                            requirements=requirements,
                            violations=violations if attempt == 2 else None,
                        ),
                        model=settings.llm_model_writing,
                        purpose=f"document_{slot.name}",
                        system=_SLOT_SYSTEM,
                        job_id=job.id,
                        # Rising temperature across variants: three samples at
                        # one temperature tend to be three phrasings of the same
                        # sentence, which is nothing to choose between.
                        temperature=0.4 + 0.2 * index,
                    ).strip(),
                    slot.max_words,
                )
                for index in range(max(1, settings.document_variants))
            ]

            # Fabrication first, quality second. A better-written variant that
            # invented something is not a candidate at all, so filtering before
            # judging means the judge never gets the chance to prefer it.
            clean = [
                (candidate, validate_no_fabrication(candidate, profile, job))
                for candidate in candidates
            ]
            honest = [candidate for candidate, found in clean if not found]

            if honest:
                text = _pick_best(honest, slot=slot, job=job, requirements=requirements)
                violations = []
                break

            # Every variant fabricated. Feed back the violations from the first
            # one and try again — the same single retry as before.
            text, violations = clean[0]
            log.warning(
                "ai_slot_regenerating",
                slot=slot.name,
                attempt=attempt,
                variants=len(candidates),
                violations=[str(v) for v in violations],
            )

        if violations:
            unresolved.extend(violations)
        generated[slot.name] = text

    return generated, unresolved


def _pick_best(
    candidates: list[str],
    *,
    slot: AISlot,
    job: Job,
    requirements: dict[str, Any] | None,
) -> str:
    """Choose the variant that best answers what the ad asked for.

    Judged on the extracted requirements rather than on generic "quality",
    because generic quality is what produces a beautifully written letter that
    addresses none of the must-haves.

    Returns the first candidate on any failure. A judge that cannot run must not
    be able to block a build — the candidates have all already passed the
    fabrication check, so the fallback is a correct document rather than a
    missing one.
    """
    if len(candidates) == 1:
        return candidates[0]

    must = (requirements or {}).get("must_haves") or []
    nice = (requirements or {}).get("nice_to_haves") or []
    tone = (requirements or {}).get("tone") or "not stated"

    numbered = "\n\n".join(
        f"VARIANT {index + 1}:\n{candidate}"
        for index, candidate in enumerate(candidates)
    )
    prompt = (
        f"The employer's advertisement asks for:\n"
        f"MUST HAVE: {must or 'not extracted'}\n"
        f"NICE TO HAVE: {nice or 'not extracted'}\n"
        f"THE AD'S REGISTER: {tone}\n\n"
        f"ROLE: {job.title} at {job.company}\n\n"
        f"Here are {len(candidates)} versions of one section of an application:\n\n"
        f"{numbered}\n\n"
        "Which single variant speaks most directly to the MUST HAVE items, in a "
        "register matching the ad? Judge substance, not polish. Reply with the "
        "number only."
    )

    try:
        answer = llm.complete(
            prompt,
            # The cheap model: this is a comparison, not composition.
            model=settings.llm_model_classify,
            purpose=f"document_pick_{slot.name}",
            system=(
                "You choose between drafts. You never rewrite them and you never "
                "explain. Reply with a single digit."
            ),
            job_id=job.id,
            temperature=0.0,
            max_tokens=8,
        )
        index = (
            int("".join(character for character in answer if character.isdigit())[:2])
            - 1
        )
    except Exception as exc:  # noqa: BLE001 - a judge must never block a build
        log.warning("variant_pick_failed", slot=slot.name, error=str(exc)[:150])
        return candidates[0]

    if not 0 <= index < len(candidates):
        log.warning("variant_pick_out_of_range", slot=slot.name, answer=answer[:40])
        return candidates[0]

    log.info("variant_picked", slot=slot.name, chosen=index + 1, of=len(candidates))
    return candidates[index]


# --------------------------------------------------------------------------
# Experience bullets
# --------------------------------------------------------------------------

_BULLETS_SYSTEM = (
    "You rewrite a candidate's own resume bullet points so they speak to one "
    "specific job advertisement.\n"
    "ABSOLUTE RULE: every fact in your output must already be present in the "
    "bullets you are given. Reorder, re-emphasise and rephrase freely. Never add "
    "an employer, a date, a technology, a tool, a metric, a scale or a seniority "
    "the bullets do not already state, and never drop one either.\n"
    "Return exactly one rewritten bullet per input bullet, one per line, in the "
    "same order. No numbering, no bullet characters, no preamble, no blank lines."
)


def _bullets_prompt(
    highlights: list[str],
    *,
    role: dict[str, Any],
    job: Job,
    requirements: dict[str, Any] | None,
) -> str:
    numbered = "\n".join(f"{index}. {text}" for index, text in enumerate(highlights, 1))
    return (
        f"THE CANDIDATE'S OWN BULLETS for {role.get('title', '')} at "
        f"{role.get('company', '')} — the only facts you may assert:\n{numbered}\n\n"
        f"THE ROLE THEY ARE APPLYING FOR\n"
        f"Title: {job.title}\nCompany: {job.company}\n"
        f"Advertisement:\n{(job.description or '')[: settings.scoring_prompt_char_budget]}\n\n"
        + _requirements_block(requirements)
        + f"WRITE: {SLOT_SPECS[BULLETS_SLOT].instruction}\n"
        f"TONE: {SLOT_SPECS[BULLETS_SLOT].tone}\n"
        f"HARD LIMIT: {SLOT_SPECS[BULLETS_SLOT].max_words} words per bullet.\n"
        f"Return exactly {len(highlights)} lines.\n"
    )


def _role_highlights(role: dict[str, Any]) -> list[str]:
    """The user's own bullet points for one role, blanks dropped."""
    return [
        str(highlight).strip()
        for highlight in (role.get("highlights") or [])
        if str(highlight).strip()
    ]


def _tailor_one_role(
    highlights: list[str],
    *,
    role: dict[str, Any],
    profile: Profile,
    job: Job,
    requirements: dict[str, Any] | None,
) -> list[str]:
    """Rewrite one role's bullets toward this ad, or return them unchanged.

    Unlike the cover letter's slots, an unusable result here is NOT a failed
    build. The cover letter has nothing truthful to fall back to — its
    paragraphs exist only because a model wrote them — but a resume bullet
    already has a correct version: the one the user wrote. Falling back to it is
    the safe answer, not a degraded one, so a missing API key or a model outage
    produces the user's own resume rather than no resume.

    Every fallback is logged. Claude.md hard rule 9: never fail silently.
    """
    spec = SLOT_SPECS[BULLETS_SLOT]

    def fall_back(reason: str, **fields: Any) -> list[str]:
        log.warning(
            "role_bullets_fell_back_to_verbatim",
            company=role.get("company"),
            reason=reason,
            **fields,
        )
        return highlights

    try:
        answer = llm.complete(
            _bullets_prompt(highlights, role=role, job=job, requirements=requirements),
            model=settings.llm_model_writing,
            purpose="document_role_bullets",
            system=_BULLETS_SYSTEM,
            job_id=job.id,
            temperature=0.3,
        )
    except Exception as exc:  # noqa: BLE001 - see the docstring
        return fall_back("generation_unavailable", error=str(exc)[:150])

    rewritten = [
        _truncate_words(line.strip().lstrip("-*\u2022 ").strip(), spec.max_words)
        for line in (answer or "").splitlines()
        if line.strip()
    ]

    # One bullet in, one bullet out. A model that merged two bullets or invented
    # a third has changed what the resume claims, whatever the words say.
    if len(rewritten) != len(highlights):
        return fall_back(
            "wrong_bullet_count", wanted=len(highlights), got=len(rewritten)
        )

    violations = validate_no_fabrication(" ".join(rewritten), profile, job)
    if violations:
        return fall_back(
            "fabrication", violations=[str(violation) for violation in violations][:5]
        )

    return rewritten


def generate_role_bullets(
    *,
    profile: Profile,
    job: Job,
    requirements: dict[str, Any] | None = None,
) -> list[list[str]]:
    """One list of bullet points per experience row, in profile order.

    The list is positional: the resume template indexes it by the loop counter,
    so a row that is skipped still has to occupy its slot.
    """
    # THE SAME normalised rows the template iterates, from the same function.
    # ai.bullets is indexed by loop position, so if this list and the one the
    # resume loops over disagree about which rows exist, every role after the
    # disagreement silently gets the previous employer's bullet points — a
    # fabrication with no invented words in it, which nothing downstream would
    # catch. _normalise_rows drops non-dict rows; iterating profile.experience
    # directly kept them, and that one-row offset was the whole bug.
    out: list[list[str]] = []
    for role in _normalise_rows(profile.experience, _ROW_DEFAULTS["experience"]):
        highlights = _role_highlights(role)
        if not highlights:
            out.append([])
            continue
        out.append(
            _tailor_one_role(
                highlights,
                role=role,
                profile=profile,
                job=job,
                requirements=requirements,
            )
        )
    return out


# --------------------------------------------------------------------------
# LaTeX
# --------------------------------------------------------------------------

_AUX_SUFFIXES = (".aux", ".log", ".out", ".fls", ".fdb_latexmk", ".synctex.gz", ".toc")

# pdflatex gets its own process group / session on POSIX so the whole group can
# be signalled at once. On Windows the equivalent is taskkill /T, which walks
# the parent-child tree and needs no creation flag.
_NEW_PROCESS_GROUP: dict[str, Any] = (
    {} if os.name == "nt" else {"start_new_session": True}
)

# What to tell the user to install when pdflatex is missing. Keyed by
# sys.platform because os.name cannot tell macOS from Linux and they need
# different answers. Anything unrecognised falls back to the generic TeX Live
# line, which is true everywhere pdflatex exists at all.
_PDFLATEX_INSTALL_HINT: dict[str, str] = {
    "win32": "Install MiKTeX (https://miktex.org)",
    "darwin": "Install BasicTeX (brew install --cask basictex)",
    "linux": "Install TeX Live (e.g. apt install texlive-latex-recommended)",
}
_GENERIC_INSTALL_HINT = "Install a TeX distribution that provides pdflatex"


def _install_hint() -> str:
    """The platform-appropriate 'how to get pdflatex' line."""
    return _PDFLATEX_INSTALL_HINT.get(sys.platform, _GENERIC_INSTALL_HINT)


def _kill_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Kill the process *and everything it spawned*.

    ``process.kill()`` only kills the direct child. MiKTeX's pdflatex spawns a
    package installer, so killing pdflatex alone leaves that installer running.
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            log.warning("pdflatex_taskkill_failed", pid=process.pid)
    else:
        with suppress(OSError):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    with suppress(OSError):
        process.kill()


def _run_pdflatex(argv: list[str], timeout: int) -> tuple[int, str]:
    """Run pdflatex under a timeout that actually holds, and return (rc, output).

    Two deliberate choices, both learned from a run that hung for hours on a
    cold MiKTeX despite passing ``timeout=`` to ``subprocess.run``:

    **Output goes to a file, never a pipe.** ``subprocess.run(capture_output=
    True, timeout=N)`` is not safe here. pdflatex spawns MiKTeX's package
    installer on first use of a package, and that grandchild *inherits the
    stdout and stderr pipe handles*. When the timeout fires, ``run`` kills
    pdflatex — but not the installer — and then calls ``communicate()`` again
    with no timeout to drain the pipes. Those pipes never reach EOF, because
    the surviving installer still holds the write end. The documented timeout
    therefore provides no protection whatsoever, and the build blocks forever.
    Redirecting to a real file removes the deadlock at the root: there are no
    reader threads and no inherited pipe ends, so ``wait()`` returns the moment
    pdflatex itself exits, whatever its children are doing.

    **stdin is closed.** ``-interaction=nonstopmode`` covers LaTeX's own error
    prompts, but not everything that can ask a question — a package installer
    reading from the console will block on a terminal that never answers.
    DEVNULL turns any such read into an immediate EOF.
    """
    # Binary mode on purpose: the log carries non-ASCII from package banners and
    # file names, and decoding is done explicitly below rather than by the
    # locale (cp1252 on Windows).
    with tempfile.TemporaryFile(prefix="pdflatex-", suffix=".log") as console:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=console,
            stderr=subprocess.STDOUT,
            **_NEW_PROCESS_GROUP,
        )
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_tree(process)
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=30)
            console.seek(0)
            tail = console.read().decode("utf-8", errors="replace")[-600:]
            log.error("pdflatex_timeout", timeout_seconds=timeout, tail=tail)
            raise DocumentBuildError(
                f"pdflatex timed out after {timeout}s and was killed. If this is "
                "a fresh MiKTeX, it was probably fetching a package: run "
                '`initexmf --set-config-value "[MPM]AutoInstall=1"` so it '
                f"installs without prompting. Last output:\n{tail}"
            ) from None

        console.seek(0)
        return returncode, console.read().decode("utf-8", errors="replace")


def render_pdf(tex_source: str, out_dir: Path, stem: str) -> Path:
    """Write .tex, run pdflatex twice, clean up, return the PDF path.

    Two passes because LaTeX resolves its own references on the second one.
    ``-halt-on-error`` so a broken document fails immediately with a readable
    log rather than dropping into pdflatex's interactive prompt.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    tex_path = out_dir / f"{stem}.tex"
    tex_path.write_text(tex_source, encoding="utf-8")

    last_output = ""
    for pass_number in range(1, max(1, settings.latex_passes) + 1):
        try:
            returncode, last_output = _run_pdflatex(
                [
                    settings.pdflatex_path,
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                    "-file-line-error",
                    f"-output-directory={out_dir}",
                    str(tex_path),
                ],
                timeout=settings.latex_timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise DocumentBuildError(
                f"pdflatex not found at {settings.pdflatex_path!r}. "
                f"{_install_hint()}, or set PDFLATEX_PATH to an existing one."
            ) from exc

        if returncode != 0:
            errors = [
                line
                for line in last_output.splitlines()
                if line.startswith("!") or ":" in line and "Error" in line
            ]
            raise DocumentBuildError(
                f"pdflatex failed on pass {pass_number}: "
                + (" | ".join(errors[:6]) or last_output[-600:])
            )

    pdf_path = out_dir / f"{stem}.pdf"
    if not pdf_path.exists():
        raise DocumentBuildError(
            f"pdflatex reported success but {pdf_path.name} is missing"
        )

    for suffix in _AUX_SUFFIXES:
        aux = out_dir / f"{stem}{suffix}"
        try:
            aux.unlink(missing_ok=True)
        except OSError as exc:
            # Tidying up must never fail a build that already produced a valid
            # PDF. On Windows this is a real intermittent case rather than a
            # theoretical one: Defender (and every other on-access scanner)
            # holds a just-written file open for a moment, and unlink then
            # raises PermissionError. The stale .aux is harmless; the lost
            # document would not be.
            log.debug("aux_cleanup_skipped", path=str(aux), error=str(exc)[:120])
    return pdf_path


def _merge(paths: list[Path], target: Path) -> Path:
    """Combine the PDFs into one, for forms with a single attachment slot."""
    writer = PdfWriter()
    for path in paths:
        writer.append(str(path))
    with target.open("wb") as handle:
        writer.write(handle)
    writer.close()
    return target


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def _template_body(
    session: Session, campaign: Campaign | None, kind: TemplateKind
) -> tuple[str, int]:
    """The campaign's template for a kind, else the default, else the shipped file."""
    if campaign is not None:
        chosen_id = (campaign.template_ids or {}).get(kind.value)
        if chosen_id:
            row = session.get(Template, int(chosen_id))
            if row is not None:
                return row.body, row.version

    row = session.exec(
        select(Template).where(Template.kind == kind, Template.is_default == True)
    ).first()
    if row is not None:
        return row.body, row.version

    filename = {
        TemplateKind.RESUME: "resume.tex.j2",
        TemplateKind.COVER_LETTER: "cover_letter.tex.j2",
        TemplateKind.EMAIL: "email.txt.j2",
    }[kind]
    return (template_root() / filename).read_text(encoding="utf-8"), 0


def build_documents(
    session: Session,
    job_id: int,
    *,
    force: bool = False,
) -> BuildResult:
    """Build and gate every document for one job."""
    with telemetry.time_stage(session, Stage.DOCUMENT_BUILD, job_id=job_id):
        return _build_documents(session, job_id, force=force)


def _build_documents(
    session: Session,
    job_id: int,
    *,
    force: bool = False,
) -> BuildResult:
    job = session.get(Job, job_id)
    if job is None:
        return BuildResult(job_id=job_id, ok=False, failure_reason="no such job")

    profile = session.exec(select(Profile).order_by(Profile.version.desc())).first()  # type: ignore[union-attr]
    if profile is None:
        return BuildResult(job_id=job_id, ok=False, failure_reason="no profile row")

    campaign = session.get(Campaign, job.campaign_id) if job.campaign_id else None

    existing = session.exec(select(Document).where(Document.job_id == job_id)).all()
    if existing and not force:
        log.info("documents_already_built", job_id=job_id, count=len(existing))
        return BuildResult(
            job_id=job_id,
            ok=all(d.parse_check_passed for d in existing),
            documents={d.kind.value: d for d in existing},
        )

    resume_body, resume_version = _template_body(session, campaign, TemplateKind.RESUME)
    letter_body, letter_version = _template_body(
        session, campaign, TemplateKind.COVER_LETTER
    )

    profile_ctx = _profile_context(profile)
    job_ctx = _job_context(job)

    from backend.documents.fabrication import profile_fact_index

    profile_text = profile_fact_index(profile)[:6000]

    # Both templates, not just the letter. The resume reads ai.bullets, and a
    # slot that is used but not generated is a StrictUndefined failure at render
    # time rather than a missing paragraph.
    used = {slot.name for slot in find_ai_slots(resume_body)}
    # The "letter uses no slots at all" fallback generates every prose slot, but
    # never the bullets: those cost one model call per role and are wasted unless
    # a template actually reads them.
    used |= {slot.name for slot in find_ai_slots(letter_body)} or (
        set(SLOT_SPECS) - {BULLETS_SLOT}
    )
    # ai.bullets is a list of lists and has its own generator below; everything
    # else is a paragraph of prose.
    slots = [
        spec
        for name, spec in SLOT_SPECS.items()
        if name in used and name != BULLETS_SLOT
    ]
    # What the employer asked for, extracted during scoring. None when scoring
    # has not run, in which case generation is exactly what it was before — the
    # ad's own text is still in the prompt.
    requirements = _requirements_for(session, job_id)

    ai_values, unresolved = generate_ai_slots(
        slots,
        profile=profile,
        job=job,
        profile_text=profile_text,
        requirements=requirements,
    )
    if unresolved:
        job.status = JobStatus.FAILED
        session.add(job)
        reason = "generated text asserted unsupported facts: " + "; ".join(
            str(v) for v in unresolved[:5]
        )
        log.error("document_build_failed_fabrication", job_id=job_id, detail=reason)
        return BuildResult(
            job_id=job_id, ok=False, failure_reason=reason, violations=unresolved
        )

    # AI text is escaped once here and marked raw so substitution does not
    # double-escape it.
    ai_ctx: dict[str, Any] = {
        name: RawLatex(escape_latex(text)) for name, text in ai_values.items()
    }
    if BULLETS_SLOT in used:
        ai_ctx[BULLETS_SLOT] = [
            [RawLatex(escape_latex(bullet)) for bullet in bullets]
            for bullets in generate_role_bullets(
                profile=profile, job=job, requirements=requirements
            )
        ]
    context = {
        "profile": profile_ctx,
        "job": job_ctx,
        "campaign": {"name": campaign.name if campaign else ""},
        "today": _today_context(),
        "ai": ai_ctx,
    }

    out_dir = settings.documents_dir / f"job_{job_id}"
    if force and out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)

    try:
        resume_tex = render_string(resume_body, context)
        letter_tex = render_string(letter_body, context)
        resume_pdf = render_pdf(resume_tex, out_dir, "resume")
        letter_pdf = render_pdf(letter_tex, out_dir, "cover_letter")
        combined_pdf = _merge([resume_pdf, letter_pdf], out_dir / "combined.pdf")
    except DocumentBuildError as exc:
        job.status = JobStatus.FAILED
        session.add(job)
        log.error("document_build_failed", job_id=job_id, error=str(exc))
        return BuildResult(job_id=job_id, ok=False, failure_reason=str(exc))

    employers = [
        str(role.get("company"))
        for role in (profile.experience or [])
        if isinstance(role, dict) and role.get("company")
    ]
    claimed = [str(skill) for skill in (profile.skills or [])][:12]
    verbatim = expected_verbatim(profile)
    # Every record heading the resume prints, for the gate to confirm each one
    # still begins its own line. Employers and institutions, because those are
    # the two things an ATS segments a document by.
    institutions = [
        str(row.get("institution"))
        for row in (profile.education or [])
        if isinstance(row, dict) and row.get("institution")
    ]
    line_starts = list(dict.fromkeys(employers + institutions))

    expectations = {
        DocumentKind.RESUME: ParseExpectations(
            name=profile_ctx["name"],
            email=profile_ctx["email"],
            phone=profile_ctx["phone"],
            employers=employers,
            claimed_keywords=claimed,
            verbatim=verbatim,
            line_starts=line_starts,
            section_order=["Experience", "Education"],
            source_text=resume_tex,
        ),
        DocumentKind.COVER_LETTER: ParseExpectations(
            name=profile_ctx["name"],
            email=profile_ctx["email"],
            source_text=letter_tex,
        ),
        DocumentKind.COMBINED: ParseExpectations(
            name=profile_ctx["name"],
            email=profile_ctx["email"],
            employers=employers,
            claimed_keywords=claimed,
            verbatim=verbatim,
            # The combined PDF contains the resume, so its record headings must
            # still be separable there — this is the artifact that gets attached
            # wherever a form has a single upload slot.
            line_starts=line_starts,
            source_text=resume_tex + letter_tex,
        ),
    }
    paths = {
        DocumentKind.RESUME: (resume_pdf, resume_version),
        DocumentKind.COVER_LETTER: (letter_pdf, letter_version),
        DocumentKind.COMBINED: (combined_pdf, resume_version),
    }

    result = BuildResult(job_id=job_id, ok=True)

    for kind, (path, version) in paths.items():
        report = verify_pdf(
            path, kind=kind.value, expect=expectations[kind], profile=profile
        )
        result.reports[kind.value] = report

        stored_report = report.model_dump()
        if kind is DocumentKind.COVER_LETTER:
            # The key three readers expect and nothing produced: the guardrail
            # that checks the letter is real, the dashboard review screen, and
            # Seek's cover-letter textarea all read
            # parse_report["cover_letter_text"]. Nothing ever wrote it, so
            # cover_letter_clean failed on every application and the textarea
            # would have been filled with an empty string. The unit tests missed
            # it because their fixtures set the key by hand, which encoded the
            # expectation without ever checking the producer met it.
            stored_report["cover_letter_text"] = report.extracted_text

        document = Document(
            job_id=job_id,
            kind=kind,
            path=str(path),
            sha256=_sha256(path),
            parse_check_passed=report.passed,
            parse_report=stored_report,
            template_version=version,
        )
        session.add(document)
        result.documents[kind.value] = document

        if not report.passed:
            result.ok = False
            result.failure_reason = report.summary()

    if result.ok:
        job.status = JobStatus.DOCUMENTS_READY
        log.info("documents_built", job_id=job_id, kinds=sorted(result.documents))
    else:
        # NEVER fall back to an older PDF here. A stale document that passes
        # the gate is the wrong document sent confidently.
        job.status = JobStatus.FAILED
        log.error(
            "documents_failed_parse_gate", job_id=job_id, detail=result.failure_reason
        )

    session.add(job)
    return result


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI wiring
    parser = argparse.ArgumentParser(prog="python -m backend.documents.build")
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument(
        "--force", action="store_true", help="rebuild even if documents exist"
    )
    args = parser.parse_args(argv)

    configure_logging()
    with session_scope() as session:
        result = build_documents(session, args.job_id, force=args.force)

    if result.ok:
        log.info("build_ok", job_id=result.job_id, documents=sorted(result.documents))
        return 0
    log.error("build_failed", job_id=result.job_id, reason=result.failure_reason)
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
