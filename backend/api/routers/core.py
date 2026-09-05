"""Profile, campaigns, answer bank, templates, settings and the stop control.

Grouped in one module because they are all small CRUD surfaces over the same
session dependency; splitting them into six files would be structure without
substance. The larger, shaped endpoints (jobs, queue, applications, analytics,
documents) live in their own modules.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from backend import facts, preferences
from backend.api.schemas import (
    AnswerIn,
    AnswerOut,
    CampaignIn,
    CampaignOut,
    ControlState,
    DerivedAnswerOut,
    FactIn,
    FactOut,
    PlaceholderIssueOut,
    PreferenceIn,
    PreferenceOut,
    ProfileIn,
    ProfileOut,
    SessionHealthOut,
    SettingsIn,
    SettingsOut,
    TemplateIn,
    TemplateOut,
    TemplatePreview,
)
from backend.config import settings
from backend.db import get_session
from backend.logging_setup import get_logger
from backend.models import (
    AnswerBank,
    Application,
    ApplicationOutcome,
    Campaign,
    DerivedAnswer,
    Fact,
    Job,
    JobStatus,
    Preference,
    PreferenceSource,
    Profile,
    Region,
    SessionHealth,
    SessionStatus,
    Template,
)

log = get_logger(__name__)

profile_router = APIRouter(prefix="/profile", tags=["profile"])
campaigns_router = APIRouter(prefix="/campaigns", tags=["campaigns"])
answers_router = APIRouter(prefix="/answers", tags=["answers"])
templates_router = APIRouter(prefix="/templates", tags=["templates"])
settings_router = APIRouter(prefix="/settings", tags=["settings"])
control_router = APIRouter(prefix="/control", tags=["control"])
preferences_router = APIRouter(prefix="/preferences", tags=["preferences"])
facts_router = APIRouter(prefix="/facts", tags=["facts"])
sessions_router = APIRouter(prefix="/sessions", tags=["sessions"])


# ==========================================================================
# Profile — versioned, never mutated in place
# ==========================================================================


def _current_profile(session: Session) -> Profile | None:
    return session.exec(select(Profile).order_by(Profile.version.desc())).first()  # type: ignore[union-attr]


@profile_router.get("", response_model=ProfileOut)
def get_profile(session: Session = Depends(get_session)) -> Profile:
    profile = _current_profile(session)
    if profile is None:
        raise HTTPException(404, "no profile yet; run `uv run python -m backend.seed`")
    return profile


@profile_router.put("", response_model=ProfileOut)
def update_profile(
    payload: ProfileIn, session: Session = Depends(get_session)
) -> Profile:
    """Save a NEW profile version rather than editing the current one.

    Scores record the ``profile_version`` they were computed against. Editing
    history in place would silently invalidate that attribution and make old
    scores unexplainable.
    """
    current = _current_profile(session)
    version = (current.version + 1) if current else 1

    profile = Profile(version=version, **payload.model_dump())
    session.add(profile)
    session.commit()
    session.refresh(profile)
    log.info("profile_version_created", version=version)
    return profile


@profile_router.get("/versions", response_model=list[ProfileOut])
def list_profile_versions(session: Session = Depends(get_session)) -> list[Profile]:
    return list(session.exec(select(Profile).order_by(Profile.version.desc())).all())  # type: ignore[union-attr]


# ==========================================================================
# Campaigns
# ==========================================================================


def _applied_today(session: Session, campaign_id: int) -> int:
    """Count in the user's local day, matching how the caps are enforced."""
    tz = ZoneInfo(settings.timezone)
    local_midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    start = local_midnight.astimezone(UTC)

    rows = session.exec(
        select(Application, Job)
        .join(Job, Application.job_id == Job.id)  # type: ignore[arg-type]
        .where(
            Application.applied_at >= start,
            Application.outcome == ApplicationOutcome.SUBMITTED,
            Job.campaign_id == campaign_id,
        )
    ).all()
    return len(list(rows))


def _to_campaign_out(session: Session, campaign: Campaign) -> CampaignOut:
    data = CampaignOut.model_validate(campaign, from_attributes=True)
    data.applied_today = _applied_today(session, campaign.id) if campaign.id else 0
    return data


@campaigns_router.get("", response_model=list[CampaignOut])
def list_campaigns(session: Session = Depends(get_session)) -> list[CampaignOut]:
    return [
        _to_campaign_out(session, campaign)
        for campaign in session.exec(select(Campaign).order_by(Campaign.name)).all()  # type: ignore[arg-type]
    ]


@campaigns_router.post("", response_model=CampaignOut, status_code=201)
def create_campaign(
    payload: CampaignIn, session: Session = Depends(get_session)
) -> CampaignOut:
    campaign = Campaign(**payload.model_dump())
    session.add(campaign)
    session.commit()
    session.refresh(campaign)
    return _to_campaign_out(session, campaign)


@campaigns_router.get("/{campaign_id}", response_model=CampaignOut)
def get_campaign(
    campaign_id: int, session: Session = Depends(get_session)
) -> CampaignOut:
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(404, "no such campaign")
    return _to_campaign_out(session, campaign)


@campaigns_router.put("/{campaign_id}", response_model=CampaignOut)
def update_campaign(
    campaign_id: int, payload: CampaignIn, session: Session = Depends(get_session)
) -> CampaignOut:
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(404, "no such campaign")

    previous_rubric = campaign.rubric or {}
    for key, value in payload.model_dump().items():
        setattr(campaign, key, value)

    # A changed rubric means a new version: scores from different rubrics are
    # not comparable, and the analytics page groups by rubric_version.
    if (payload.rubric or {}) != previous_rubric:
        campaign.rubric_version += 1
        log.info(
            "rubric_version_bumped",
            campaign=campaign.name,
            version=campaign.rubric_version,
        )

    campaign.updated_at = datetime.now(UTC)
    session.add(campaign)
    session.commit()
    session.refresh(campaign)
    return _to_campaign_out(session, campaign)


@campaigns_router.delete("/{campaign_id}", status_code=204)
def delete_campaign(campaign_id: int, session: Session = Depends(get_session)) -> None:
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(404, "no such campaign")
    session.delete(campaign)
    session.commit()


@campaigns_router.post("/{campaign_id}/pause", response_model=CampaignOut)
def pause_campaign(
    campaign_id: int, session: Session = Depends(get_session)
) -> CampaignOut:
    return _set_active(session, campaign_id, active=False)


@campaigns_router.post("/{campaign_id}/resume", response_model=CampaignOut)
def resume_campaign(
    campaign_id: int, session: Session = Depends(get_session)
) -> CampaignOut:
    return _set_active(session, campaign_id, active=True)


def _set_active(session: Session, campaign_id: int, *, active: bool) -> CampaignOut:
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(404, "no such campaign")
    campaign.active = active
    campaign.updated_at = datetime.now(UTC)
    session.add(campaign)
    session.commit()
    session.refresh(campaign)
    log.info("campaign_active_changed", campaign=campaign.name, active=active)
    return _to_campaign_out(session, campaign)


# ==========================================================================
# Global stop — the emergency brake
# ==========================================================================


@control_router.get("", response_model=ControlState)
def control_state() -> ControlState:
    stopped = settings.stop_file.exists()
    reason = None
    if stopped:
        try:
            reason = settings.stop_file.read_text(encoding="utf-8")[:500]
        except OSError:
            reason = "stop file present but unreadable"
    return ControlState(
        stopped=stopped, stop_file=str(settings.stop_file), reason=reason
    )


@control_router.post("/stop", response_model=ControlState)
def stop_everything(reason: str = "stopped from the dashboard") -> ControlState:
    """Create the STOP file.

    This is the emergency brake. ``guardrails.check_can_submit`` reads this file
    on every single submit decision, so creating it stops applications
    immediately — including one already mid-form, which will fail its gate.
    """
    settings.stop_file.parent.mkdir(parents=True, exist_ok=True)
    settings.stop_file.write_text(
        f"stopped {datetime.now(UTC).isoformat()}\n{reason}\n", encoding="utf-8"
    )
    log.warning("global_stop_engaged", reason=reason)
    return control_state()


@control_router.post("/resume", response_model=ControlState)
def resume_everything() -> ControlState:
    settings.stop_file.unlink(missing_ok=True)
    log.warning("global_stop_released")
    return control_state()


# ==========================================================================
# Answer bank
# ==========================================================================


@answers_router.get("", response_model=list[AnswerOut])
def list_answers(
    campaign_id: int | None = None,
    unanswered_only: bool = False,
    session: Session = Depends(get_session),
) -> list[AnswerBank]:
    rows = list(
        session.exec(select(AnswerBank).order_by(AnswerBank.question_pattern)).all()
    )  # type: ignore[arg-type]
    if campaign_id is not None:
        rows = [r for r in rows if r.campaign_id in (campaign_id, None)]
    if unanswered_only:
        rows = [r for r in rows if not (r.answer_value or "").strip()]
    return rows


@answers_router.post("", response_model=AnswerOut, status_code=201)
def create_answer(
    payload: AnswerIn, session: Session = Depends(get_session)
) -> AnswerBank:
    row = AnswerBank(
        **payload.model_dump(exclude={"verified"}),
        verified_at=datetime.now(UTC) if payload.verified else None,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@answers_router.put("/{answer_id}", response_model=AnswerOut)
def update_answer(
    answer_id: int, payload: AnswerIn, session: Session = Depends(get_session)
) -> AnswerBank:
    row = session.get(AnswerBank, answer_id)
    if row is None:
        raise HTTPException(404, "no such answer")

    for key, value in payload.model_dump(exclude={"verified"}).items():
        setattr(row, key, value)
    row.verified_at = datetime.now(UTC) if payload.verified else None
    row.updated_at = datetime.now(UTC)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@answers_router.post("/bulk", response_model=list[AnswerOut])
def bulk_update_answers(
    payload: dict[int, str], session: Session = Depends(get_session)
) -> list[AnswerBank]:
    """Set several answers at once — the answer-bank table edits in place."""
    updated: list[AnswerBank] = []
    for answer_id, value in payload.items():
        row = session.get(AnswerBank, int(answer_id))
        if row is None:
            continue
        row.answer_value = value
        row.updated_at = datetime.now(UTC)
        session.add(row)
        updated.append(row)
    session.commit()
    for row in updated:
        session.refresh(row)
    return updated


@answers_router.delete("/{answer_id}", status_code=204)
def delete_answer(answer_id: int, session: Session = Depends(get_session)) -> None:
    row = session.get(AnswerBank, answer_id)
    if row is None:
        raise HTTPException(404, "no such answer")
    session.delete(row)
    session.commit()


# ==========================================================================
# Templates
# ==========================================================================


@templates_router.get("", response_model=list[TemplateOut])
def list_templates(session: Session = Depends(get_session)) -> list[Template]:
    return list(
        session.exec(select(Template).order_by(Template.kind, Template.name)).all()
    )  # type: ignore[arg-type]


@templates_router.post("", response_model=TemplateOut, status_code=201)
def create_template(
    payload: TemplateIn, session: Session = Depends(get_session)
) -> Template:
    row = Template(**payload.model_dump())
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@templates_router.put("/{template_id}", response_model=TemplateOut)
def update_template(
    template_id: int, payload: TemplateIn, session: Session = Depends(get_session)
) -> Template:
    row = session.get(Template, template_id)
    if row is None:
        raise HTTPException(404, "no such template")

    body_changed = row.body != payload.body
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    if body_changed:
        # Every built document records the template version it used; a changed
        # body that kept its version would make that record a lie.
        row.version += 1
    row.updated_at = datetime.now(UTC)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@templates_router.delete("/{template_id}", status_code=204)
def delete_template(template_id: int, session: Session = Depends(get_session)) -> None:
    row = session.get(Template, template_id)
    if row is None:
        raise HTTPException(404, "no such template")
    session.delete(row)
    session.commit()


@templates_router.post("/preview", response_model=TemplatePreview)
def preview_template(
    body: str,
    job_id: int | None = Query(default=None),
    session: Session = Depends(get_session),
) -> TemplatePreview:
    """Render a template body against a REAL job, and report placeholder problems.

    Reuses the document engine rather than reimplementing rendering, so what
    the editor shows is what the builder will produce.
    """
    from backend.documents.engine import (
        KNOWN_FIELDS,
        find_ai_slots,
        render_string,
        validate_placeholders,
    )

    issues = [
        PlaceholderIssueOut(placeholder=i.placeholder, kind=i.kind, detail=i.detail)
        for i in validate_placeholders(body)
    ]
    slots = [slot.name for slot in find_ai_slots(body)]

    job = session.get(Job, job_id) if job_id else None
    if job is None:
        job = session.exec(
            select(Job)
            .where(Job.status != JobStatus.DISCOVERED)
            .order_by(Job.discovered_at.desc())  # type: ignore[union-attr]
        ).first()

    rendered = ""
    error = None
    if job is not None:
        from backend.documents.build import (
            _job_context,
            _profile_context,
            _today_context,
        )

        profile = _current_profile(session)
        context = {
            "profile": _profile_context(profile) if profile else {},
            "job": _job_context(job),
            "campaign": {"name": ""},
            "today": _today_context(),
            # Preview shows the slot names rather than paying an LLM call.
            "ai": {
                name: f"[{name} — generated per job]" for name in KNOWN_FIELDS["ai"]
            },
        }
        try:
            rendered = render_string(body, context)
        except Exception as exc:  # noqa: BLE001 - a broken template is the point
            error = f"{type(exc).__name__}: {exc}"
    else:
        error = "no job in the database to preview against yet"

    return TemplatePreview(
        job_id=job.id if job else None,
        rendered=rendered,
        issues=issues,
        ai_slots=slots,
        known_placeholders={
            root: list(fields) for root, fields in KNOWN_FIELDS.items()
        },
        error=error,
    )


# ==========================================================================
# Settings
# ==========================================================================

# Settings the API is allowed to change. allow_live_submit is deliberately
# absent: it is the one control that must require a human editing .env on the
# machine itself, so a compromised or mis-clicked dashboard cannot start
# sending applications.
MUTABLE_SETTINGS = frozenset(SettingsIn.model_fields)


@settings_router.get("", response_model=SettingsOut)
def get_settings() -> SettingsOut:
    from backend.apply.guardrails import breaker_status
    from backend.llm.client import budget_status

    return SettingsOut(
        llm_monthly_cap_usd=settings.llm_monthly_cap_usd,
        apply_window_start=settings.apply_window_start,
        apply_window_end=settings.apply_window_end,
        apply_min_interval_floor_seconds=settings.apply_min_interval_floor_seconds,
        scoring_stage1_top_n=settings.scoring_stage1_top_n,
        scoring_cost_target_usd=settings.scoring_cost_target_usd,
        discovery_default_hours_old=settings.discovery_default_hours_old,
        timezone=settings.timezone,
        allow_live_submit=settings.allow_live_submit,
        spend=budget_status(),
        circuit_breakers=breaker_status(),
    )


@settings_router.put("", response_model=SettingsOut)
def update_settings(payload: SettingsIn) -> SettingsOut:
    """Update the tunable settings for this process.

    ``allow_live_submit`` cannot be set here and there is no code path that
    would let it be: the field is not on ``SettingsIn``, and this loop only
    walks that model's fields. It stays an environment variable the user edits
    on the machine, deliberately.
    """
    changed: dict[str, Any] = {}
    for key, value in payload.model_dump(exclude_none=True).items():
        if key not in MUTABLE_SETTINGS:  # pragma: no cover - defence in depth
            continue
        setattr(settings, key, value)
        changed[key] = value

    if changed:
        log.info("settings_updated", **changed)
    return get_settings()


@settings_router.get("/spend")
def get_spend() -> dict[str, Any]:
    from backend.llm.client import budget_status

    return budget_status()


@settings_router.get("/runs")
def recent_runs(
    limit: int = 20, session: Session = Depends(get_session)
) -> list[dict[str, Any]]:
    from backend.models import Run

    rows = session.exec(
        select(Run).order_by(Run.started_at.desc()).limit(limit)  # type: ignore[union-attr]
    ).all()
    return [
        {
            "id": r.id,
            "phase": r.phase.value,
            "started_at": r.started_at,
            "ended_at": r.ended_at,
            "ok": r.ok,
            "counts": r.counts,
            "errors": r.errors,
        }
        for r in rows
    ]


@settings_router.get("/health-summary")
def health_summary(session: Session = Depends(get_session)) -> dict[str, Any]:
    """A compact status block for the dashboard header."""
    from backend.apply.guardrails import breaker_status
    from backend.llm.client import budget_status

    since = datetime.now(UTC) - timedelta(days=1)
    recent = session.exec(
        select(Application).where(Application.applied_at >= since)
    ).all()

    return {
        "stopped": settings.stop_file.exists(),
        "allow_live_submit": settings.allow_live_submit,
        "applications_last_24h": len(list(recent)),
        "spend": budget_status(),
        "circuit_breakers": breaker_status(),
    }


# ==========================================================================
# Preferences — what the system learned, and the user's veto over it
# ==========================================================================


@preferences_router.get("", response_model=list[PreferenceOut])
def list_preferences(
    status: str | None = None,
    session: Session = Depends(get_session),
) -> list[Preference]:
    """Everything learned or stated, newest first.

    Proposals are included by default and carry ``status="proposed"``. The page
    exists so the user can see what the system decided for itself, which means
    it has to show the things that have not taken effect yet.
    """
    rows = list(
        session.exec(select(Preference).order_by(Preference.learned_at.desc())).all()  # type: ignore[union-attr]
    )
    if status:
        rows = [row for row in rows if row.status.value == status]
    return rows


@preferences_router.post("", response_model=PreferenceOut, status_code=201)
def create_preference(
    payload: PreferenceIn, session: Session = Depends(get_session)
) -> Preference:
    """Set a preference by hand. Always ``user_set`` — the user is the source.

    This is also the only route by which a fact-shaped key gets a value, which
    is the whole point: ``preferences.set`` refuses to infer one.
    """
    row = preferences.set(
        session,
        key=payload.key,
        value=payload.value,
        source=PreferenceSource.USER_SET,
        value_type=payload.value_type,
        campaign_id=payload.campaign_id,
    )
    session.commit()
    session.refresh(row)
    return row


@preferences_router.post("/{preference_id}/confirm", response_model=PreferenceOut)
def confirm_preference(
    preference_id: int, session: Session = Depends(get_session)
) -> Preference:
    row = preferences.confirm(session, preference_id)
    if row is None:
        raise HTTPException(404, "no such preference")
    session.commit()
    session.refresh(row)
    return row


@preferences_router.post("/{preference_id}/reject", response_model=PreferenceOut)
def reject_preference(
    preference_id: int, session: Session = Depends(get_session)
) -> Preference:
    row = preferences.reject(session, preference_id)
    if row is None:
        raise HTTPException(404, "no such preference")
    session.commit()
    session.refresh(row)
    return row


@preferences_router.delete("/{preference_id}", status_code=204)
def delete_preference(
    preference_id: int, session: Session = Depends(get_session)
) -> None:
    row = session.get(Preference, preference_id)
    if row is None:
        raise HTTPException(404, "no such preference")
    session.delete(row)
    session.commit()


# ==========================================================================
# Facts — layer 1, the user's own words
# ==========================================================================


@facts_router.get("", response_model=list[FactOut])
def list_facts(session: Session = Depends(get_session)) -> list[Fact]:
    """Every fact, in the order the Facts page shows them.

    Ordered by the seed list rather than alphabetically or by id: the page is a
    form the user fills top to bottom, and reordering it between visits makes
    it harder to see what is still blank.
    """
    from backend.seed import FACT_SHELLS

    order = {key: index for index, (key, _c, _p) in enumerate(FACT_SHELLS)}
    rows = list(session.exec(select(Fact)).all())
    return sorted(rows, key=lambda row: (order.get(row.key, 999), row.key))


@facts_router.put("/{key}", response_model=FactOut)
def update_fact(
    key: str, payload: FactIn, session: Session = Depends(get_session)
) -> Fact:
    """Write a fact verbatim. Editing one invalidates its derived answers."""
    row = session.exec(select(Fact).where(Fact.key == key)).first()
    if row is None:
        raise HTTPException(
            404, f"no fact {key!r}; run `uv run python -m backend.seed`"
        )

    facts.set_fact(
        session,
        key=key,
        text=payload.text,
        category=row.category,
        jurisdiction=Region(payload.jurisdiction) if payload.jurisdiction else None,
    )
    session.commit()
    session.refresh(row)
    return row


@facts_router.get("/derived", response_model=list[DerivedAnswerOut])
def list_derived(
    fact_id: int | None = None, session: Session = Depends(get_session)
) -> list[Any]:
    """Derived answers, optionally just the ones from one fact.

    Each carries ``stale``, so the page can show that an edited fact has
    invalidated an answer rather than leaving the user to infer it.
    """
    stale_ids = {row.id for row in facts.stale_derivations(session)}
    rows = list(session.exec(select(DerivedAnswer)).all())
    if fact_id is not None:
        rows = [row for row in rows if row.fact_id == fact_id]
    return [
        DerivedAnswerOut(
            **{
                field: getattr(row, field)
                for field in DerivedAnswerOut.model_fields
                if field != "stale"
            },
            stale=row.id in stale_ids,
        )
        for row in rows
    ]


@facts_router.post("/derived/{derivation_id}/confirm", response_model=DerivedAnswerOut)
def confirm_derived(derivation_id: int, session: Session = Depends(get_session)) -> Any:
    row = facts.confirm(session, derivation_id)
    if row is None:
        raise HTTPException(404, "no such derivation")
    session.commit()
    session.refresh(row)
    return DerivedAnswerOut.model_validate(row)


@facts_router.delete("/derived/{derivation_id}", status_code=204)
def reject_derived(derivation_id: int, session: Session = Depends(get_session)) -> None:
    """The user disagreed. Deleted so the next pass re-derives."""
    if not facts.reject(session, derivation_id):
        raise HTTPException(404, "no such derivation")
    session.commit()


# ==========================================================================
# Sessions — which sites are signed in, and when that was last confirmed
# ==========================================================================


@sessions_router.get("", response_model=list[SessionHealthOut])
def list_sessions(session: Session = Depends(get_session)) -> list[SessionHealth]:
    """Every site's session state, worst first.

    Ordered by trouble rather than alphabetically: the page exists to answer
    "is anything signed out", and a dead session at the bottom of an
    alphabetical list is a dead session nobody sees.
    """
    rank = {
        SessionStatus.DEAD: 0,
        SessionStatus.UNREACHABLE: 1,
        SessionStatus.UNKNOWN: 2,
        SessionStatus.NO_SESSION: 3,
        SessionStatus.LIVE: 4,
    }
    rows = list(session.exec(select(SessionHealth)).all())
    return sorted(rows, key=lambda row: (rank.get(row.status, 9), row.site))
