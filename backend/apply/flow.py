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

from backend import failures, questions, telemetry
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
from backend.ats.formmaps import fingerprint_fields, record_outcome
from backend.ats.generic import last_map_trusted, map_fields
from backend.ats.queueing import decide_queueing
from backend.base import ApplyOutcome, ApplyResult
from backend.config import settings
from backend.logging_setup import get_logger
from backend.models import (
    Application,
    ApplicationOutcome,
    ApplyType,
    CacheName,
    Campaign,
    Document,
    FailureType,
    Job,
    JobStatus,
    Profile,
    QuestionResolution,
    Region,
    Score,
    Stage,
)
from backend.siteknowledge import ElementNotFound, drain_resolutions

log = get_logger(__name__)

__all__ = ["Adapter", "RestrictionDetected", "build_draft", "run_apply"]


# How many form steps to walk before concluding something is wrong. Not a step
# count for any particular platform — a runaway guard.
MAX_STEPS = 12


# Set by the integrations layer, same convention as ``canary.on_drift``.
on_element_unresolvable: Any = None
on_form_approval_needed: Any = None
on_followup_drafted: Any = None


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
    campaign = campaign or (
        session.get(Campaign, job.campaign_id) if job.campaign_id else None
    )
    profile = (
        profile
        or session.exec(
            select(Profile).order_by(Profile.version.desc())  # type: ignore[union-attr]
        ).first()
    )

    documents = list(
        session.exec(select(Document).where(Document.job_id == job.id)).all()
    )
    score_row = session.exec(
        select(Score).where(Score.job_id == job.id).order_by(Score.scored_at.desc())  # type: ignore[union-attr]
    ).first()

    cover_letter_text = ""
    letter_doc = next((d for d in documents if d.kind.value == "cover_letter"), None)
    if letter_doc is not None:
        cover_letter_text = (letter_doc.parse_report or {}).get(
            "cover_letter_text", ""
        ) or ""

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
            draft.answers[field.label or field.identifier] = _synthetic_answer(
                field, value
            )
        else:
            screening.append(field)

    campaign_id = campaign.id if campaign else None
    # From the JOB, not the campaign: a campaign searching NZ can still surface
    # an Australian ad, and work rights are a different question in each
    # country. Same rule the answer bank already applies to region-scoped rows.
    region = getattr(job, "region", None)
    bank = load_answers(session, campaign_id)
    resolved, abstentions = resolve_all(screening, campaign_id, answers=bank)
    draft.answers.update(resolved)
    # Which mechanism answered each question, accumulated as the passes run.
    # Reconstructing it afterwards from draft.answers is not possible:
    # _synthetic_answer stamps EXACT/100.0 on profile, fact and form-map
    # answers alike, so by the time the draft is finished a fact-derived answer
    # is indistinguishable from a bank hit.
    by_mechanism: dict[str, QuestionResolution] = dict.fromkeys(
        resolved, QuestionResolution.BANK
    )

    # Facts before the form map. The form map answers "where does this field's
    # value come from"; facts answer "what is the value". A question the bank
    # cannot answer but a fact can is not a mapping problem, and routing it
    # through the LLM field-mapper would cost a call to rediscover that the
    # answer bank is where screening answers live.
    if abstentions:
        rescued, abstentions = _resolve_via_facts(
            session,
            abstentions,
            screening=screening,
            bank=bank,
            region=region,
            job_id=job.id,
        )
        draft.answers.update(rescued)
        by_mechanism.update(dict.fromkeys(rescued, QuestionResolution.FACT))

    if abstentions and settings.apply_form_mapping_enabled:
        draft.form_fingerprint = fingerprint_fields(screening)
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
        by_mechanism.update(dict.fromkeys(rescued, QuestionResolution.FORM_MAP))
        # An LLM-mapped form is a draft until it has graduated. The guardrail
        # below turns this into a blocked submit and a Telegram approval
        # request; recording it on the draft keeps that decision inspectable in
        # a dry run too.
        draft.form_map_trusted = (
            all(last_map_trusted.values()) if last_map_trusted else True
        )

    draft.abstentions = abstentions
    _record_questions(
        session,
        job=job,
        platform=platform,
        screening=screening,
        answers=draft.answers,
        by_mechanism=by_mechanism,
        abstentions=abstentions,
    )
    return draft


def _record_questions(
    session: Session,
    *,
    job: Job,
    platform: str,
    screening: Sequence[FormField],
    answers: dict[str, Any],
    by_mechanism: dict[str, QuestionResolution],
    abstentions: Sequence[Abstain],
) -> None:
    """File every screening question this step encountered, resolved or not.

    Here rather than at the park, because a question that was answered is
    exactly the half nothing recorded before: no ``Application`` row is written
    when a job parks, and an application that submits had no abstentions by
    construction. Recording only at the point of failure gives a numerator with
    no denominator.

    Only ``screening`` — the fields the profile could not fill. A form asking
    for an email address is not asking the user anything, and counting it would
    push coverage toward 100% by padding the denominator with questions that
    cannot fail.

    Dry runs record too. The questions were genuinely encountered, and a dry run
    that learns nothing about what employers ask is a dry run worth less than it
    costs.
    """
    abstained = {normalise_question(a.question) for a in abstentions}

    for field_ in screening:
        label = question_key(field_)
        key = normalise_question(label)
        if not key:
            continue
        if key in abstained:
            resolution = QuestionResolution.ABSTAINED
            source_row_id = None
        else:
            answer = answers.get(label)
            if answer is None:
                # Neither answered nor abstained: the form-map pass clears
                # abstentions by exclusion, so a field it accounted for without
                # producing an answer would land here. Nothing to file.
                continue
            resolution = by_mechanism.get(label, QuestionResolution.BANK)
            source_row_id = getattr(answer, "source_row_id", None)

        questions.record(
            session,
            question=key,
            question_text=label,
            resolution=resolution,
            platform=platform,
            company=job.company,
            job_id=job.id,
            source_row_id=source_row_id,
        )


def _resolve_via_facts(
    session: Session,
    abstentions: list[Abstain],
    *,
    screening: list[FormField],
    bank: Sequence[Any],
    region: Region | None,
    job_id: int | None,
) -> tuple[dict[str, Any], list[Abstain]]:
    """Try to answer each abstention from a stated fact.

    An answer only comes back when a derivation has already been CONFIRMED by
    the user. A first derivation writes a proposal, asks over Telegram, and
    still abstains — so this pass parks the job exactly as it would have without
    facts, and the next pass has the answer. That is deliberate: the model's
    reading of someone's licence is a proposal until they agree with it.

    The routing comes from the answer-bank row that matched, not from a second
    question matcher. Two matchers disagreeing about what a question is asking
    is how the wrong fact answers it.
    """
    from backend import facts as facts_module

    still: list[Abstain] = []
    rescued: dict[str, Any] = {}

    by_key = {normalise_question(question_key(f)): f for f in screening}

    for abstention in abstentions:
        key = normalise_question(abstention.question)
        field_ = by_key.get(key)
        category = _fact_category_for(abstention.question, bank, region)

        answer = None
        try:
            answer = facts_module.resolve_from_facts(
                session,
                question=(field_.label if field_ else abstention.question),
                question_key=key,
                category=category,
                choices=list(field_.choices) if field_ else None,
                answer_type=_answer_type_for(abstention.question, bank, region),
                region=region,
                job_id=job_id,
            )
        except Exception as exc:  # noqa: BLE001 - a derivation fault is an abstention
            log.warning("fact_resolution_failed", error=str(exc)[:200])

        if answer:
            rescued[field_.label if field_ else abstention.question] = (
                _synthetic_answer(
                    field_ or FormField(identifier=key, label=abstention.question),
                    answer,
                )
            )
            log.info("answered_from_fact", question=key[:80])
        else:
            still.append(abstention)

    return rescued, still


def _fact_category_for(
    question: str, bank: Sequence[Any], region: Region | None = None
) -> Any:
    """Which fact category the matching bank row points at, if any."""
    from backend.apply.answers import matching_rows

    for row in matching_rows(question, bank, region=region):
        if getattr(row, "fact_category", None) is not None:
            return row.fact_category
    return None


def _answer_type_for(question: str, bank: Sequence[Any], region: Region | None = None):
    """The answer type the matching bank row expects."""
    from backend.apply.answers import matching_rows
    from backend.models import AnswerType

    for row in matching_rows(question, bank, region=region):
        return row.answer_type
    return AnswerType.TEXT


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

    mapped = {
        m.identifier: m
        for m in map_fields(unresolved, platform=platform, session=session)
    }

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
    existing = session.exec(
        select(Application).where(Application.job_id == job.id)
    ).first()
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

    # Report back to the form-map cache. Trust graduation is the half of
    # formmaps that stayed unwired after load/save were connected: without
    # this, record_outcome is never called, no map ever reaches TRUST_THRESHOLD
    # and every learned map stays a draft forever. A map is credited only for
    # an application that actually went in; anything else resets its streak,
    # because a map that produced a failure has not been shown to work.
    if draft.form_fingerprint:
        became_trusted = record_outcome(
            session,
            draft.form_fingerprint,
            success=outcome is ApplicationOutcome.SUBMITTED,
        )
        if became_trusted:
            log.info(
                "form_map_trusted",
                fingerprint=draft.form_fingerprint,
                platform=draft.platform,
            )


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

    This wrapper exists for one reason: ``ElementNotFound`` can surface from any
    adapter call — open, attach, read back, advance, submit — and the response is
    the same wherever it came from. A platform that no longer matches any
    recorded strategy is a platform we must stop guessing at, so the job goes to
    the manual queue and the user is told which element went missing. Catching it
    per call site would be eleven copies of one decision.
    """
    try:
        return _run_apply(
            page,
            session,
            job,
            adapter=adapter,
            is_authenticated=is_authenticated,
            dry_run=dry_run,
        )
    except ElementNotFound as exc:
        return _park_unresolvable(session, job, adapter.platform, exc)
    finally:
        # Drain the site-knowledge tally once per application, however it ended.
        # The adapters resolve elements; the knowledge layer has no session and
        # must not open one, so it counts and this records. In the finally
        # because a failed application is exactly when the first strategy stops
        # working — dropping those would make the hit rate a survivor's average.
        _record_element_lookups(session, job.id, adapter.platform)
        _persist_knowledge(adapter)


def _persist_knowledge(adapter: Adapter) -> None:
    """Write back whatever this application taught the site-knowledge layer.

    Once per application, here, rather than in each adapter. The nine external
    ATS adapters share one class that never called save at all, so every
    promotion and every counter those platforms learned was discarded when the
    process exited — the layer learned, and then forgot, on two thirds of the
    platforms it supports. LinkedIn and Seek each called it from their own
    ``confirmed()``, which is the wrong moment twice over: not reached on the
    failure path, and reached on every poll of the confirmation state.

    ``save`` is a no-op when nothing changed, so this costs nothing on a pass
    that learned nothing.
    """
    knowledge = getattr(adapter, "knowledge", None)
    if knowledge is None:
        return
    try:
        knowledge.save(reason="resolution")
    except Exception as exc:  # noqa: BLE001 - bookkeeping must not fail the application
        log.warning(
            "site_knowledge_save_failed",
            platform=getattr(adapter, "platform", "unknown"),
            error=str(exc)[:200],
        )


def _record_element_lookups(
    session: Session, job_id: int | None, platform: str
) -> None:
    """File how the site-knowledge lookups went for one application.

    A hit is the FIRST strategy working. A lower strategy healing the element
    counts as a miss even though the element was found: the element is fine, the
    file's idea of how to find it is not, and the whole point of the number is
    to see that before it becomes a failure.
    """
    first, later = drain_resolutions()
    telemetry.record_cache(
        session,
        CacheName.SITE_KNOWLEDGE,
        hit=True,
        platform=platform,
        job_id=job_id,
        count=first,
    )
    telemetry.record_cache(
        session,
        CacheName.SITE_KNOWLEDGE,
        hit=False,
        platform=platform,
        job_id=job_id,
        count=later,
    )


def _blocked_only_on_form_trust(verdict: Any) -> bool:
    """Whether the sole reason this was blocked is an ungraduated form map.

    Only then is asking the user useful. If the switch is also off, or the
    window is closed, approving the form changes nothing and the message would
    be a question whose answer does not unblock anything.
    """
    failures = [
        check.name for check in getattr(verdict, "checks", []) if not check.passed
    ]
    return failures == ["form_map_trusted"]


def _park_unresolvable(
    session: Session, job: Job, platform: str, exc: ElementNotFound
) -> ApplyResult:
    """Hand a job to the human because the page no longer matches what we know.

    Not FAILED: nothing about this job is wrong, and retrying it changes
    nothing until either the site changes back or someone updates the strategies.
    MANUAL_QUEUE says exactly that — a person can finish this one by hand while
    the knowledge file is corrected.

    The failure is also recorded against the circuit breaker. One missing element
    is drift; the same element missing on every job is a platform that has moved,
    and continuing to open browser sessions into it is how an account gets
    flagged.
    """
    job.status = JobStatus.MANUAL_QUEUE
    session.add(job)
    guardrails.record_failure(platform, f"unresolvable element: {exc.key}")
    failures.record(
        session,
        platform=platform,
        failure_type=FailureType.ELEMENT_UNRESOLVED,
        element_id=exc.key,
        company=job.company,
        job_id=job.id,
        detail=f"tried {len(exc.tried)} strategies",
    )

    log.error(
        "element_unresolvable_parked",
        job_id=job.id,
        platform=platform,
        element=exc.key,
        tried=exc.tried,
    )
    if on_element_unresolvable is not None:
        try:
            on_element_unresolvable(platform, exc.key, exc.tried, job.id)
        except Exception as hook_exc:  # noqa: BLE001 - alerting must not mask it
            log.warning("unresolvable_hook_failed", error=str(hook_exc)[:150])

    return ApplyResult(
        ok=False,
        outcome=ApplyOutcome.FAILED,
        failure_reason=(
            f"no strategy resolved {exc.key!r} on {platform}; "
            "queued for manual completion and the site knowledge needs updating"
        ),
    )


def _run_apply(
    page: Any,
    session: Session,
    job: Job,
    *,
    adapter: Adapter,
    is_authenticated: Callable[[str], bool] | None = None,
    dry_run: bool = False,
) -> ApplyResult:
    assert job.id is not None

    # 1 — preconditions. Re-asserted here as well as in the guardrails: defence
    # in depth, because attaching an ungated document is unrecoverable.
    already = session.exec(
        select(Application).where(Application.job_id == job.id)
    ).first()
    if already is not None:
        return _abort(
            session,
            job,
            None,
            ApplyOutcome.BLOCKED,
            f"job {job.id} already has application {already.id}",
        )

    try:
        with telemetry.time_stage(
            session, Stage.PAGE_LOAD, job_id=job.id, platform=adapter.platform
        ):
            adapter.open(page, job)
    except ElementNotFound:
        # Not an ordinary failure: the page no longer matches anything we know,
        # and run_apply's wrapper turns that into a manual-queue park with an
        # alert. Swallowing it here would report "could not open form" and
        # retry forever against a site that has moved.
        raise
    except Exception as exc:
        log.exception("adapter_open_failed", job_id=job.id, platform=adapter.platform)
        guardrails.record_failure(adapter.platform, f"open failed: {exc}")
        return _abort(
            session, job, None, ApplyOutcome.FAILED, f"could not open form: {exc}"
        )

    if _restricted(adapter, page):
        guardrails.trip_global_halt(
            f"{adapter.platform} restriction notice detected while applying to job {job.id}"
        )
        raise RestrictionDetected(adapter.platform)

    if getattr(adapter, "detect_redirect", None) and adapter.detect_redirect(page):
        job.apply_type = ApplyType.MANUAL_ONLY

        # Not every off-site listing is worth the user's time. This path used to
        # queue all of them, which is how the manual queue fills with jobs the
        # system itself scored as mediocre — and a manual application costs about
        # ninety seconds of attention, the one resource that actually runs out.
        #
        # decide_queueing holds that judgement (a manual job must clear the
        # auto-apply threshold plus a premium). It was written for exactly this
        # moment and nothing had ever called it.
        campaign = session.get(Campaign, job.campaign_id) if job.campaign_id else None
        score_row = session.exec(
            select(Score).where(Score.job_id == job.id).order_by(Score.id.desc())  # type: ignore[union-attr]
        ).first()
        decision = decide_queueing(
            campaign, score_row.final if score_row else None, automatable=False
        )

        job.status = (
            JobStatus.MANUAL_QUEUE if decision.action == "queue" else JobStatus.SKIPPED
        )
        session.add(job)
        log.warning(
            "apply_redirects_offsite",
            job_id=job.id,
            platform=adapter.platform,
            decision=decision.action,
            reason=decision.reason,
        )
        return ApplyResult(
            ok=False,
            outcome=ApplyOutcome.BLOCKED,
            failure_reason=f"listing redirects off-site; {decision.reason}",
        )

    # 2-6 — walk the steps, resolving as we go.
    draft: ApplicationDraft | None = None
    seen_steps: set[frozenset[str]] = set()

    for step in range(MAX_STEPS):
        with telemetry.time_stage(
            session,
            Stage.FIELD_ENUMERATION,
            job_id=job.id,
            platform=adapter.platform,
        ):
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

        with telemetry.time_stage(
            session,
            Stage.ANSWER_RESOLUTION,
            job_id=job.id,
            platform=adapter.platform,
        ):
            step_draft = build_draft(
                session, job, platform=adapter.platform, fields=fields
            )
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
            except ElementNotFound:
                raise
            except Exception as exc:
                log.exception("fill_failed", job_id=job.id, field=field.identifier)
                return _abort(
                    session,
                    job,
                    draft,
                    ApplyOutcome.FAILED,
                    f"could not fill {field.label}: {exc}",
                )

        # 7 — attach, then PROVE the right file is attached.
        upload_fields = [f for f in fields if f.kind == "file"]
        if upload_fields:
            slots = adapter.upload_slots(fields)
            planned = draft.attachment_plan(slots=slots)
            if not planned:
                return _abort(
                    session,
                    job,
                    draft,
                    ApplyOutcome.FAILED,
                    "no gated document to attach",
                )
            if not all(d.parse_check_passed for d in planned):
                return _abort(
                    session,
                    job,
                    draft,
                    ApplyOutcome.BLOCKED,
                    "refusing to attach a document that failed the parse gate",
                )

            with telemetry.time_stage(
                session, Stage.UPLOAD, job_id=job.id, platform=adapter.platform
            ):
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
                guardrails.record_failure(
                    adapter.platform, "attachment readback mismatch"
                )
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
        # An ungraduated form map is not an ordinary block. Everything else
        # blocked here is environmental — the switch is off, the window closed,
        # a cap is reached — and needs no decision from the user. This one is a
        # question only they can answer, and the drafted application plus a
        # screenshot is what makes it answerable.
        if _blocked_only_on_form_trust(verdict) and on_form_approval_needed is not None:
            try:
                on_form_approval_needed(
                    job.id,
                    draft.form_fingerprint or "",
                    draft.platform,
                    draft.screenshot_pre,
                    {a.question: a.value for a in draft.answers.values()},
                )
            except Exception as exc:  # noqa: BLE001 - asking must not abort
                log.warning("form_approval_request_failed", error=str(exc)[:150])

        return _abort(
            session,
            job,
            draft,
            ApplyOutcome.BLOCKED,
            verdict.summary(),
            status=JobStatus.QUEUED,
        )

    # 10-11 — submit, then confirm by DETECTING the confirmation state. A click
    # that returned is not evidence that anything was received.
    #
    # Both inside one timer, and the timer records in a finally, so every way
    # out of this block — the raise, the two aborts, the success — is measured.
    # The confirmation wait is where most of a submit's time actually goes, so
    # timing only the click would report the fast half.
    with telemetry.time_stage(
        session, Stage.SUBMIT, job_id=job.id, platform=adapter.platform
    ):
        try:
            adapter.submit(page)
        except ElementNotFound:
            # Not an ordinary failure: the page no longer matches anything we
            # know, and run_apply's wrapper turns that into a manual-queue park
            # with an alert. Swallowing it here would report "could not open
            # form" and retry forever against a site that has moved.
            raise
        except Exception as exc:  # noqa: BLE001
            guardrails.record_failure(adapter.platform, f"submit raised: {exc}")
            return _abort(
                session, job, draft, ApplyOutcome.FAILED, f"submit failed: {exc}"
            )

        draft.screenshot_post = _screenshot(page, job.id, "post")
        if not adapter.confirmed(page):
            guardrails.record_failure(
                adapter.platform, "no confirmation state after submit"
            )
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
    _remember_observed_fields(session, draft)
    _draft_followup(session, job)
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


def _draft_followup(session: Session, job: Job) -> None:
    """Write a follow-up draft for the user to approve, if the ad published an
    address.

    Only after a CONFIRMED submission: a follow-up to an application that never
    landed is a cold email, which is the thing this project is not allowed to
    send. Drafting only — nothing here can send, and OUTBOUND_ENABLED gates the
    send path separately.

    Silent when the ad published no address. That is the common case and it is
    not a failure: addresses are never guessed or looked up.
    """
    from backend.integrations.outbound import (
        OutboundRefused,
        draft_for_job,
        record_draft,
    )

    if not getattr(job, "ad_contact_email", None):
        return

    try:
        row = record_draft(session, draft_for_job(session, job.id))
    except OutboundRefused as exc:
        log.info("followup_not_drafted", job_id=job.id, reason=str(exc)[:200])
        return
    except Exception as exc:  # noqa: BLE001 - never fail a sent application
        log.warning("followup_draft_failed", job_id=job.id, error=str(exc)[:200])
        return

    session.flush()
    if on_followup_drafted is not None and row.id is not None:
        try:
            on_followup_drafted(
                row.id,
                job.id,
                row.to_address,
                row.subject,
                row.body,
                list(row.attachments),
            )
        except Exception as exc:  # noqa: BLE001 - telling the user is best effort
            log.warning("followup_notify_failed", error=str(exc)[:150])


def _remember_observed_fields(session: Session, draft: ApplicationDraft) -> None:
    """Propose preferences from the plain fields on an accepted application.

    Only after a confirmed submit: a value the employer never received is not
    evidence of anything. Only non-question fields — a screening question's
    answer belongs to the answer bank, which already owns that loop, and
    duplicating it here would give one answer two homes that could disagree.

    Everything written is a PROPOSAL. `preferences.observed_field` additionally
    refuses fact-shaped keys outright, so a start date or a licence seen on a
    form never becomes an inferred claim about the user.
    """
    from backend import preferences

    for field_ in draft.fields:
        if field_.choices or (field_.label or "").strip().endswith("?"):
            continue  # a screening question; the answer bank owns it
        answer = draft.answers.get(field_.identifier)
        value = getattr(answer, "value", None)
        if not value:
            continue
        try:
            preferences.observed_field(
                session, key=field_.label or field_.identifier, value=value
            )
        except Exception as exc:  # noqa: BLE001 - never fail a sent application
            log.debug("observed_field_skipped", error=str(exc)[:120])


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

    The browser is NOT held open waiting for a human. The caller asks the
    question over Telegram once this session has committed, the reply is saved
    to the answer bank, and the job is re-queued.

    The question is recorded on the job, not just returned, because the half
    that asks and the half that answers are different processes: ``/answer``
    arrives at the bot long after this pass has ended, and it has nothing but
    the job id to work from.
    """
    job.status = JobStatus.NEEDS_ANSWER
    questions = [a.question for a in draft.abstentions]
    job.needs_answer_question = questions[0] if questions else None
    session.add(job)

    # One row per question, not one per park: the trend worth seeing is "this
    # question keeps arriving", and a company that abstains on three different
    # questions is a different signal from one that abstains on the same one
    # three times.
    for abstention in draft.abstentions:
        failures.record(
            session,
            platform=draft.platform,
            failure_type=FailureType.ANSWER_ABSTAINED,
            company=job.company,
            question=abstention.question,
            job_id=job.id,
            detail=getattr(abstention, "reason", None)
            and str(getattr(abstention.reason, "value", abstention.reason)),
        )
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
        needs_answer_choices=_choices_for(draft, questions[0]) if questions else [],
    )


def _choices_for(draft: ApplicationDraft, question: str) -> list[str]:
    """The form's options for ``question``, if it was a closed list.

    Matched on the normalised question because ``Abstain.question`` is already
    normalised while ``FormField.label`` is raw.
    """
    target = normalise_question(question)
    for field_ in draft.fields:
        if normalise_question(question_key(field_)) == target:
            return list(field_.choices)
    return []


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
    log.error(
        "application_aborted", job_id=job.id, outcome=outcome.value, reason=reason
    )

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
