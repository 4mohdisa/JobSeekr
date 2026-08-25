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
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pypdf import PdfWriter
from sqlmodel import Session, select

from backend.config import settings
from backend.db import session_scope
from backend.documents.engine import (
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
    Template,
    TemplateKind,
)

log = get_logger(__name__)

__all__ = ["BuildResult", "build_documents", "generate_ai_slots", "render_pdf"]


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
    "projects": ("name", "stack", "description"),
    "education": ("qualification", "institution", "year"),
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
        "website": identity.get("website", ""),
        "work_rights": work_rights.get("statement", ""),
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
    slot: AISlot, *, profile_text: str, job: Job, violations: list[Violation] | None = None
) -> str:
    prompt = (
        f"CANDIDATE FACTS (the only facts you may assert)\n{profile_text}\n\n"
        f"THE ROLE\n"
        f"Title: {job.title}\nCompany: {job.company}\n"
        f"Location: {job.location or 'not stated'}\n"
        f"Advertisement:\n{(job.description or '')[: settings.scoring_prompt_char_budget]}\n\n"
        f"WRITE: {slot.instruction}\n"
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
            text = llm.complete(
                _slot_prompt(
                    slot,
                    profile_text=profile_text,
                    job=job,
                    violations=violations if attempt == 2 else None,
                ),
                model=settings.llm_model_writing,
                purpose=f"document_{slot.name}",
                system=_SLOT_SYSTEM,
                job_id=job.id,
                temperature=0.4,
            ).strip()
            text = _truncate_words(text, slot.max_words)

            violations = validate_no_fabrication(text, profile, job)
            if not violations:
                break
            log.warning(
                "ai_slot_regenerating",
                slot=slot.name,
                attempt=attempt,
                violations=[str(v) for v in violations],
            )

        if violations:
            unresolved.extend(violations)
        generated[slot.name] = text

    return generated, unresolved


# --------------------------------------------------------------------------
# LaTeX
# --------------------------------------------------------------------------

_AUX_SUFFIXES = (".aux", ".log", ".out", ".fls", ".fdb_latexmk", ".synctex.gz", ".toc")


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
            completed = subprocess.run(
                [
                    settings.pdflatex_path,
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                    "-file-line-error",
                    f"-output-directory={out_dir}",
                    str(tex_path),
                ],
                capture_output=True,
                text=True,
                # Decode pdflatex's output as UTF-8 explicitly. Without this,
                # `text=True` uses the locale encoding — cp1252 on a typical
                # Windows install — and one non-cp1252 byte in the log (an
                # em-dash in a filename, a package's copyright line) raises
                # UnicodeDecodeError. The build would then fail with a decoding
                # error instead of the LaTeX error that actually happened.
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=False,
            )
        except FileNotFoundError as exc:
            raise DocumentBuildError(
                f"pdflatex not found at {settings.pdflatex_path!r}. "
                "Install MiKTeX (Windows) or set PDFLATEX_PATH."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise DocumentBuildError("pdflatex timed out after 120s") from exc

        last_output = completed.stdout + completed.stderr
        if completed.returncode != 0:
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
        raise DocumentBuildError(f"pdflatex reported success but {pdf_path.name} is missing")

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
    letter_body, letter_version = _template_body(session, campaign, TemplateKind.COVER_LETTER)

    profile_ctx = _profile_context(profile)
    job_ctx = _job_context(job)

    from backend.documents.fabrication import profile_fact_index

    profile_text = profile_fact_index(profile)[:6000]

    slots = find_ai_slots(letter_body) or list(SLOT_SPECS.values())
    ai_values, unresolved = generate_ai_slots(
        slots, profile=profile, job=job, profile_text=profile_text
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
    ai_ctx = {name: RawLatex(escape_latex(text)) for name, text in ai_values.items()}
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

    expectations = {
        DocumentKind.RESUME: ParseExpectations(
            name=profile_ctx["name"],
            email=profile_ctx["email"],
            phone=profile_ctx["phone"],
            employers=employers,
            claimed_keywords=claimed,
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
        report = verify_pdf(path, kind=kind.value, expect=expectations[kind])
        result.reports[kind.value] = report

        document = Document(
            job_id=job_id,
            kind=kind,
            path=str(path),
            sha256=_sha256(path),
            parse_check_passed=report.passed,
            parse_report=report.model_dump(),
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
        log.error("documents_failed_parse_gate", job_id=job_id, detail=result.failure_reason)

    session.add(job)
    return result


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI wiring
    parser = argparse.ArgumentParser(prog="python -m backend.documents.build")
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--force", action="store_true", help="rebuild even if documents exist")
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
