"""The one apply flow. Every platform runs through this, unchanged.

Platform adapters supply selectors and step logic. The *sequence* — enumerate,
resolve, abort on abstention, attach, read back, screenshot, gate, submit,
confirm, audit — is written once, here. If a platform file starts reordering or
re-implementing those steps, the safety properties stop being properties of the
system and become properties of whichever adapter you happen to be using.

THE ADAPTER CONTRACT
--------------------
An adapter is an object with:

    platform: str
    can_handle(job) -> bool
    open(page, job) -> None
    enumerate_fields(page, step: int) -> list[FormField]
    fill_field(page, field: FormField, value: str) -> None
    upload_slots(fields) -> int
    attach(page, documents) -> None
    read_back_attachments(page) -> list[str]
    is_last_step(page, fields) -> bool
    advance(page) -> None
    submit(page) -> None
    confirmed(page) -> bool

Optional: ``detect_redirect(page) -> bool`` and ``detect_restriction(page) -> bool``.

Nothing in that list decides *whether* to submit. That decision has exactly one
home: ``guardrails.check_can_submit``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from sqlmodel import Session, select

from backend.apply import guardrails
from backend.apply.answers import (
    Abstain,
    Answer,
    load_answers,
    normalise_question,
    question_key,
    resolve_all,
    resolve_answer,
)
from backend.apply.draft import ApplicationDraft, FormField
from backend.ats.generic import map_fields
from backend.base import ApplyOutcome, ApplyResult
from backend.config import settings
from backend.logging_setup import get_logger
from backend.models import (
    Application,
    ApplicationOutcome,
    ApplyType,
    Campaign,
    Document,
    Job,
    JobStatus,
    Profile,
    Score,
)

log = get_logger(__name__)

__all__ = ["Adapter", "RestrictionDetected", "build_draft", "run_apply"]


# How many form steps to walk before concluding something is wrong. Not a step
# count for any particular platform — a runaway guard.
MAX_STEPS = 12


class RestrictionDetected(RuntimeError):
    """The platform is showing an account restriction. Halts everything."""


@runtime_checkable
class Adapter(Protocol):
    """See the module docstring. Selectors and step logic only."""

    platform: str

    def can_handle(self, job: Any) -> bool: ...
    def open(self, page: Any, job: Any) -> None: ...
    def enumerate_fields(self, page: Any, step: int) -> list[FormField]: ...
    def fill_field(self, page: Any, field: FormField, value: str) -> None: ...
    def upload_slots(self, fields: list[FormField]) -> int: ...
    def attach(self, page: Any, documents: list[Document]) -> None: ...
    def read_back_attachments(self, page: Any) -> list[str]: ...
    def is_last_step(self, page: Any, fields: list[FormField]) -> bool: ...
    def advance(self, page: Any) -> None: ...
    def submit(self, page: Any) -> None: ...
    def confirmed(self, page: Any) -> bool: ...


# Profile-derived fields never come from the answer bank: the user's name is a
# fact, not a screening answer. One mapping, used by every platform.
PROFILE_FIELD_HINTS: dict[str, tuple[str, ...]] = {
    "name": ("full name", "your name", "name"),
    "first_name": ("first name", "given name"),
    "last_name": ("last name", "surname", "family name"),
    "email": ("email", "e-mail"),
    "phone": ("phone", "mobile", "contact number", "telephone"),
    "location": ("location", "suburb", "city", "address"),
}


def _profile_value(profile: Profile | None, key: str) -> str | None:
    if profile is None:
        return None
    identity = profile.identity or {}
    if key in identity:
        return str(identity[key])
    if key in {"first_name", "last_name"} and identity.get("name"):
        parts = str(identity["name"]).split()
        if not parts:
            return None
        return parts[0] if key == "first_name" else " ".join(parts[1:]) or parts[0]
    return None


def _match_profile_field(field: FormField, profile: Profile | None) -> str | None:
    label = (field.label or field.identifier or "").casefold()
    for key, hints in PROFILE_FIELD_HINTS.items():
        if any(hint in label for hint in hints):
            value = _profile_value(profile, key)
            if value:
                return value
    return None


def build_draft(
    session: Session,
    job: Job,
    *,
    platform: str,
    fields: list[FormField],
    profile: Profile | None = None,
    campaign: Campaign | None = None,
) -> ApplicationDraft:
    """Resolve every enumerated field into a complete, inspectable draft."""
    campaign = campaign or (session.get(Campaign, job.campaign_id) if job.campaign_id else None)
    profile = profile or session.exec(
        select(Profile).order_by(Profile.version.desc())  # type: ignore[union-attr]
    ).first()

    documents = list(session.exec(select(Document).where(Document.job_id == job.id)).all())
    score_row = session.exec(
        select(Score).where(Score.job_id == job.id).order_by(Score.scored_at.desc())  # type: ignore[union-attr]
    ).first()

    cover_letter_text = ""
    letter_doc = next((d for d in documents if d.kind.value == "cover_letter"), None)
    if letter_doc is not None:
        cover_letter_text = (letter_doc.parse_report or {}).get("cover_letter_text", "") or ""

    draft = ApplicationDraft(
        job=job,
        campaign=campaign,
        platform=platform,
        score=score_row.final if score_row else None,
        documents=documents,
        cover_letter_text=cover_letter_text,
        fields=fields,
    )

    # Split the form: identity facts come from the profile, everything else has
    # to be answerable from the answer bank or the job is parked.
    screening: list[FormField] = []
    for field in fields:
        if field.kind == "file":
            continue
        value = _match_profile_field(field, profile)
        if value is not None:
            draft.answers[field.label or field.identifier] = _synthetic_answer(field, value)
        else:
            screening.append(field)

    campaign_id = campaign.id if campaign else None
    bank = load_answers(session, campaign_id)
    resolved, abstentions = resolve_all(screening, campaign_id, answers=bank)
    draft.answers.update(resolved)

    if abstentions and settings.apply_form_mapping_enabled:
        rescued, abstentions = _resolve_via_form_map(
            session,
            [f for f in screening if question_key(f) not in resolved],
            abstentions,
            platform=platform,
            profile=profile,
            bank=bank,
            campaign_id=campaign_id,
        )
        draft.answers.update(rescued)

    draft.abstentions = abstentions
    return draft


def _resolve_via_form_map(
    session: Session,
    unresolved: list[FormField],
    abstentions: list[Abstain],
    *,
    platform: str,
    profile: Profile | None,
    bank: Sequence[Any],
    campaign_id: int | None,
) -> tuple[dict[str, Any], list[Abstain]]:
    """Second pass over the fields nothing deterministic could place.

    The first pass matches labels against ``PROFILE_FIELD_HINTS`` and the
    answer bank literally. That handles "Email address" and fails on "Best
    contact e-mail for you" — a field the system knows the answer to and cannot
    see it knows. This pass asks the model what such a field is *for*, and the
    answer is cached by form shape, so a given form costs one mapping call ever
    rather than one per application.

    It maps, it does not answer. A field the model says is a screening question
    still goes back to the answer bank for its value, so hard rule 2 holds:
    the bank remains the only source of screening answers, and a field that
    maps to nothing the bank knows still abstains and still parks the job.
    """
    if not unresolved:
        return {}, abstentions

    mapped = {m.identifier: m for m in map_fields(unresolved, platform=platform, session=session)}

    rescued: dict[str, Any] = {}
    cleared: set[str] = set()

    for field in unresolved:
        mapping = mapped.get(field.identifier)
        if mapping is None or not mapping.usable:
            continue

        answer = None
        if mapping.source == "profile" and mapping.profile_path:
            value = _profile_value(profile, mapping.profile_path.split(".")[-1])
            if value:
                answer = _synthetic_answer(field, value)
        elif mapping.source == "answer_bank" and mapping.question:
            outcome = resolve_answer(
                mapping.question,
                campaign_id,
                answers=bank,
                choices=field.choices or None,
            )
            if isinstance(outcome, Answer):
                answer = outcome

        if answer is None:
            continue

        log.info(
            "field_resolved_via_form_map",
            field=field.identifier,
            source=mapping.source,
            platform=platform,
        )
        rescued[field.label or field.identifier] = answer
        cleared.add(normalise_question(question_key(field)))

    # Drop only the abstentions actually resolved, and keep everything else.
    # Written as an exclusion rather than by rebuilding the list from matched
    # fields on purpose: Abstain.question is normalised, so a lookup that
    # missed silently dropped the abstention instead of parking the job — the
    # field vanished from the draft rather than stopping it. Anything this
    # cannot positively account for stays an abstention.
    still = [a for a in abstentions if normalise_question(a.question) not in cleared]
    return rescued, still


def _synthetic_answer(field: FormField, value: str):
    from backend.apply.answers import Answer
    from backend.models import MatchType

    return Answer(
        value=value,
        source_row_id=None,
        match_type=MatchType.EXACT,
        confidence=100.0,
        question=field.label or field.identifier,
    )


def _screenshot(page: Any, job_id: int, suffix: str) -> str | None:
    try:
        settings.screenshots_dir.mkdir(parents=True, exist_ok=True)
        path = settings.screenshots_dir / f"job_{job_id}_{suffix}.png"
        page.screenshot(path=str(path), full_page=True)
        return str(path)
    except Exception as exc:  # noqa: BLE001 - a missing screenshot must not abort
        log.warning("screenshot_failed", job_id=job_id, suffix=suffix, error=str(exc))
        return None


def _record(
    session: Session,
    job: Job,
    draft: ApplicationDraft,
    *,
    outcome: ApplicationOutcome,
    failure_reason: str | None,
    readback: str | None,
    status: JobStatus,
) -> None:
    """Write the audit row and update the job. Never fails silently."""
    existing = session.exec(select(Application).where(Application.job_id == job.id)).first()
    if existing is None:
        session.add(
            Application(
                job_id=job.id,
                applied_at=datetime.now(UTC),
                resume_doc_id=(d.id if (d := draft.document_by("resume")) else None),
                cover_letter_doc_id=(
                    d.id if (d := draft.document_by("cover_letter")) else None
                ),
                attachment_readback=readback,
                answers_given=draft.answers_given,
                screenshot_pre=draft.screenshot_pre,
                screenshot_post=draft.screenshot_post,
                outcome=outcome,
                failure_reason=failure_reason,
                platform=draft.platform,
            )
        )
    job.status = status
    session.add(job)


def run_apply(
    page: Any,
    session: Session,
    job: Job,
    *,
    adapter: Adapter,
    is_authenticated: Callable[[str], bool] | None = None,
    dry_run: bool = False,
) -> ApplyResult:
    """Fill and (maybe) submit one application. The only path to a submit.

    Returns an ``ApplyResult`` on every path, including every abort — a silent
    return would violate "never fail silently" and leave the dashboard unable
    to explain what happened.
    """
    assert job.id is not None

    # 1 — preconditions. Re-asserted here as well as in the guardrails: defence
    # in depth, because attaching an ungated document is unrecoverable.
    already = session.exec(select(Application).where(Application.job_id == job.id)).first()
    if already is not None:
        return _abort(
            session,
            job,
            None,
            ApplyOutcome.BLOCKED,
            f"job {job.id} already has application {already.id}",
        )

    try:
        adapter.open(page, job)
    except Exception as exc:
        log.exception("adapter_open_failed", job_id=job.id, platform=adapter.platform)
        guardrails.record_failure(adapter.platform, f"open failed: {exc}")
        return _abort(session, job, None, ApplyOutcome.FAILED, f"could not open form: {exc}")

    if _restricted(adapter, page):
        guardrails.trip_global_halt(
            f"{adapter.platform} restriction notice detected while applying to job {job.id}"
        )
        raise RestrictionDetected(adapter.platform)

    if getattr(adapter, "detect_redirect", None) and adapter.detect_redirect(page):
        job.apply_type = ApplyType.MANUAL_ONLY
        job.status = JobStatus.MANUAL_QUEUE
        session.add(job)
        log.warning("apply_redirects_offsite", job_id=job.id, platform=adapter.platform)
        return ApplyResult(
            ok=False,
            outcome=ApplyOutcome.BLOCKED,
            failure_reason="listing redirects off-site; queued for manual application",
        )

    # 2-6 — walk the steps, resolving as we go.
    draft: ApplicationDraft | None = None
    seen_steps: set[frozenset[str]] = set()

    for step in range(MAX_STEPS):
        fields = adapter.enumerate_fields(page, step)

        # A repeated step means a validation error is silently blocking
        # progress. Never hardcode a step count; detect the loop instead.
        fingerprint = frozenset(f.identifier for f in fields)
        if fingerprint and fingerprint in seen_steps:
            return _abort(
                session,
                job,
                draft,
                ApplyOutcome.FAILED,
                f"form step repeated at step {step}; a validation error is blocking progress",
            )
        seen_steps.add(fingerprint)

        step_draft = build_draft(session, job, platform=adapter.platform, fields=fields)
        if draft is None:
            draft = step_draft
        else:
            draft.fields.extend(step_draft.fields)
            draft.answers.update(step_draft.answers)
            draft.abstentions.extend(step_draft.abstentions)

        # 5 — ANY abstention aborts. The browser closes; the job is parked and
        # the user is asked. Never hold the session open waiting for a human.
        if draft.abstentions:
            return _park(session, job, draft)

        for field in fields:
            if field.kind == "file":
                continue
            answer = draft.answers.get(field.label or field.identifier)
            if answer is None:
                continue
            try:
                adapter.fill_field(page, field, answer.value)
            except Exception as exc:
                log.exception("fill_failed", job_id=job.id, field=field.identifier)
                return _abort(
                    session, job, draft, ApplyOutcome.FAILED, f"could not fill {field.label}: {exc}"
                )

        # 7 — attach, then PROVE the right file is attached.
        upload_fields = [f for f in fields if f.kind == "file"]
        if upload_fields:
            slots = adapter.upload_slots(fields)
            planned = draft.attachment_plan(slots=slots)
            if not planned:
                return _abort(
                    session, job, draft, ApplyOutcome.FAILED, "no gated document to attach"
                )
            if not all(d.parse_check_passed for d in planned):
                return _abort(
                    session,
                    job,
                    draft,
                    ApplyOutcome.BLOCKED,
                    "refusing to attach a document that failed the parse gate",
                )

            adapter.attach(page, planned)
            draft.attachment_intent = {
                d.kind.value: Path(d.path).name for d in planned
            }

            readback = adapter.read_back_attachments(page)
            mismatch = _readback_mismatch(draft.attachment_intent, readback)
            if mismatch:
                # LinkedIn silently reuses a stale upload. This is the only
                # thing standing between the user and sending last week's
                # resume, so it is a hard abort — never a warning.
                guardrails.record_failure(adapter.platform, "attachment readback mismatch")
                return _abort(
                    session,
                    job,
                    draft,
                    ApplyOutcome.FAILED,
                    f"attachment read-back mismatch: {mismatch}",
                    readback=", ".join(readback),
                )
            draft.attachment_readback = ", ".join(readback)

        if adapter.is_last_step(page, fields):
            break
        adapter.advance(page)
    else:
        return _abort(
            session, job, draft, ApplyOutcome.FAILED, f"form exceeded {MAX_STEPS} steps"
        )

    assert draft is not None

    # 8 — evidence of what was about to be sent.
    draft.screenshot_pre = _screenshot(page, job.id, "pre")

    # 9 — THE gate. The only place a submit decision is made.
    verdict = guardrails.check_can_submit(
        session, job, draft, is_authenticated=is_authenticated
    )

    if dry_run:
        log.info(
            "dry_run_complete",
            job_id=job.id,
            would_submit=verdict.allowed,
            guardrails=verdict.summary(),
            answers=len(draft.answers),
        )
        _record(
            session,
            job,
            draft,
            outcome=ApplicationOutcome.ABORTED,
            failure_reason=f"dry run: {verdict.summary()}",
            readback=draft.attachment_readback,
            status=JobStatus.DOCUMENTS_READY,
        )
        return ApplyResult(
            ok=True,
            outcome=ApplyOutcome.DRY_RUN,
            failure_reason=None if verdict.allowed else verdict.summary(),
            answers_given=draft.answers_given,
            attachment_readback=draft.attachment_readback,
            screenshot_pre=draft.screenshot_pre,
        )

    if not verdict.allowed:
        return _abort(
            session, job, draft, ApplyOutcome.BLOCKED, verdict.summary(), status=JobStatus.QUEUED
        )

    # 10 — submit.
    try:
        adapter.submit(page)
    except Exception as exc:  # noqa: BLE001
        guardrails.record_failure(adapter.platform, f"submit raised: {exc}")
        return _abort(session, job, draft, ApplyOutcome.FAILED, f"submit failed: {exc}")

    # 11 — confirm by DETECTING the confirmation state. A click that returned
    # is not evidence that anything was received.
    draft.screenshot_post = _screenshot(page, job.id, "post")
    if not adapter.confirmed(page):
        guardrails.record_failure(adapter.platform, "no confirmation state after submit")
        return _abort(
            session,
            job,
            draft,
            ApplyOutcome.FAILED,
            "submitted but no confirmation state appeared",
        )

    # 12 — audit.
    guardrails.record_success(adapter.platform)
    _record(
        session,
        job,
        draft,
        outcome=ApplicationOutcome.SUBMITTED,
        failure_reason=None,
        readback=draft.attachment_readback,
        status=JobStatus.APPLIED,
    )
    log.info(
        "application_submitted",
        job_id=job.id,
        platform=adapter.platform,
        company=job.company,
        title=job.title,
    )
    return ApplyResult(
        ok=True,
        outcome=ApplyOutcome.SUBMITTED,
        answers_given=draft.answers_given,
        attachment_readback=draft.attachment_readback,
        screenshot_pre=draft.screenshot_pre,
        screenshot_post=draft.screenshot_post,
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _restricted(adapter: Adapter, page: Any) -> bool:
    """Whether the platform is showing an account-restriction interstitial.

    An adapter may override the check; when it does not, the board registry's
    selectors are used. Before that fallback existed, Seek had restriction
    selectors defined and nothing that ever looked at them, so a suspended Seek
    account would have kept receiving applications.
    """
    from backend.apply.session import has_restriction_notice

    detector = getattr(adapter, "detect_restriction", None)
    try:
        if detector is None:
            return has_restriction_notice(page, adapter.platform)
        return bool(detector(page))
    except Exception:  # noqa: BLE001
        return False


def _readback_mismatch(intended: dict[str, str], readback: list[str]) -> str | None:
    """Return a description of the mismatch, or None when every file matches."""
    if not intended:
        return None
    if not readback:
        return "the form reported no attached filename"

    seen = [name.strip().casefold() for name in readback if name]
    for kind, filename in intended.items():
        target = filename.strip().casefold()
        if not any(target in name or name in target for name in seen):
            return f"expected {filename} for {kind}, form shows {readback}"
    return None


def _park(session: Session, job: Job, draft: ApplicationDraft) -> ApplyResult:
    """Mark the job as needing an answer and close cleanly.

    The browser is NOT held open waiting for a human. The integrations layer
    asks the question over Telegram, saves the answer, and the job is re-queued.
    """
    job.status = JobStatus.NEEDS_ANSWER
    session.add(job)
    questions = [a.question for a in draft.abstentions]
    log.warning(
        "application_parked_needs_answer",
        job_id=job.id,
        platform=draft.platform,
        questions=questions,
    )
    return ApplyResult(
        ok=False,
        outcome=ApplyOutcome.ABSTAINED,
        failure_reason=f"{len(questions)} unanswered screening questions",
        answers_given=draft.answers_given,
        needs_answer=questions[0] if questions else None,
    )


def _abort(
    session: Session,
    job: Job,
    draft: ApplicationDraft | None,
    outcome: ApplyOutcome,
    reason: str,
    *,
    readback: str | None = None,
    status: JobStatus = JobStatus.FAILED,
) -> ApplyResult:
    """Every failure path lands here: logged loudly, audited, never silent."""
    log.error("application_aborted", job_id=job.id, outcome=outcome.value, reason=reason)

    if draft is not None:
        _record(
            session,
            job,
            draft,
            outcome=(
                ApplicationOutcome.ABORTED
                if outcome is ApplyOutcome.BLOCKED
                else ApplicationOutcome.FAILED
            ),
            failure_reason=reason,
            readback=readback,
            status=status,
        )
    else:
        job.status = status
        session.add(job)

    return ApplyResult(
        ok=False,
        outcome=outcome,
        failure_reason=reason,
        answers_given=draft.answers_given if draft else {},
        attachment_readback=readback,
        screenshot_pre=draft.screenshot_pre if draft else None,
    )
