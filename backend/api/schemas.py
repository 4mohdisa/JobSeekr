"""Pydantic schemas — the wire format, kept separate from the tables.

Claude.md: SQLModel for DB, Pydantic for API schemas. The separation is not
ceremony. Returning table objects directly would leak file paths, let a client
PATCH a primary key, and make every schema change a migration; and the dashboard
wants shapes the tables do not have (a job with its score and documents folded
in, a queue card built for speed).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.models import (
    AnswerType,
    ApplicationOutcome,
    ApplyType,
    DocumentKind,
    GrayZoneAction,
    JobStatus,
    MatchType,
    ResponseStatus,
    TemplateKind,
)

__all__ = [
    "AnalyticsBucket",
    "AnalyticsResponse",
    "AnswerIn",
    "AnswerOut",
    "ApplicationOut",
    "CampaignIn",
    "CampaignOut",
    "ControlState",
    "DerivedAnswerOut",
    "DocumentOut",
    "FactIn",
    "FactOut",
    "FunnelStage",
    "JobDetail",
    "JobOut",
    "Page",
    "PreferenceIn",
    "PreferenceOut",
    "ProfileIn",
    "ProfileOut",
    "QueueCard",
    "ScoreOut",
    "SessionHealthOut",
    "SettingsIn",
    "SettingsOut",
    "TemplateIn",
    "TemplateOut",
    "TemplatePreview",
]


class Page(BaseModel):
    """A slice of a list, with enough to render a pager."""

    items: list[Any]
    total: int
    offset: int = 0
    limit: int = 50


# --------------------------------------------------------------------- profile


class ProfileIn(BaseModel):
    identity: dict[str, Any] = Field(default_factory=dict)
    work_rights: dict[str, Any] = Field(default_factory=dict)
    experience: list[Any] = Field(default_factory=list)
    projects: list[Any] = Field(default_factory=list)
    education: list[Any] = Field(default_factory=list)
    certifications: list[Any] = Field(default_factory=list)
    skills: list[Any] = Field(default_factory=list)
    preferences: dict[str, Any] = Field(default_factory=dict)


class ProfileOut(ProfileIn):
    model_config = ConfigDict(from_attributes=True)

    id: int
    version: int
    created_at: datetime


# -------------------------------------------------------------------- campaign


class CampaignIn(BaseModel):
    name: str
    active: bool = True
    search_terms: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    salary_floor: int | None = None
    work_types: list[str] = Field(default_factory=list)
    exclusions: dict[str, Any] = Field(default_factory=dict)
    score_floor: float = 60.0
    score_auto_apply: float = 80.0
    gray_zone_action: GrayZoneAction = GrayZoneAction.QUEUE
    daily_caps: dict[str, int] = Field(default_factory=dict)
    target_goal_type: str | None = None
    target_goal_count: int | None = None
    template_ids: dict[str, int] = Field(default_factory=dict)
    rubric: dict[str, Any] = Field(default_factory=dict)


class CampaignOut(CampaignIn):
    model_config = ConfigDict(from_attributes=True)

    id: int
    rubric_version: int
    created_at: datetime
    updated_at: datetime

    applied_today: int = 0
    """Counted in the user's local day, matching how the caps are enforced."""


# ----------------------------------------------------------------- answer bank


class AnswerIn(BaseModel):
    question_pattern: str
    match_type: MatchType = MatchType.FUZZY
    answer_value: str = ""
    answer_type: AnswerType = AnswerType.TEXT
    campaign_id: int | None = None
    choices: list[str] | None = None
    notes: str | None = None
    verified: bool = False


class AnswerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    question_pattern: str
    match_type: MatchType
    answer_value: str
    answer_type: AnswerType
    campaign_id: int | None
    choices: list[str] | None
    notes: str | None
    verified_at: datetime | None
    updated_at: datetime

    @property
    def answered(self) -> bool:
        return bool(self.answer_value.strip())


# -------------------------------------------------------------------- template


class TemplateIn(BaseModel):
    kind: TemplateKind
    name: str
    body: str
    campaign_id: int | None = None
    is_default: bool = False


class TemplateOut(TemplateIn):
    model_config = ConfigDict(from_attributes=True)

    id: int
    version: int
    updated_at: datetime


class PlaceholderIssueOut(BaseModel):
    placeholder: str
    kind: str
    detail: str


class TemplatePreview(BaseModel):
    """A rendered template plus everything the editor needs to show problems."""

    job_id: int | None
    rendered: str
    issues: list[PlaceholderIssueOut] = Field(default_factory=list)
    ai_slots: list[str] = Field(default_factory=list)
    known_placeholders: dict[str, list[str]] = Field(default_factory=dict)
    pdf_path: str | None = None
    pdf_document_id: int | None = None
    error: str | None = None


# ------------------------------------------------------------------------ job


class ScoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    stage1: float | None
    stage2: float | None
    final: float | None
    reasoning: str | None
    matched_skills: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)
    rubric_version: int
    profile_version: int
    scored_at: datetime


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: DocumentKind
    parse_check_passed: bool
    built_at: datetime
    template_version: int | None = None
    # The path is deliberately NOT exposed. Files are served through
    # /api/documents/{id}/file, which validates the path first.


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    url: str
    title: str
    company: str
    location: str | None
    salary_min: int | None
    salary_max: int | None
    salary_basis: str | None
    salary_is_estimated: bool
    posted_at: datetime | None
    discovered_at: datetime
    apply_type: ApplyType
    status: JobStatus
    campaign_id: int | None
    ad_contact_email: str | None
    score: float | None = None


class JobDetail(JobOut):
    description: str | None = None
    score_detail: ScoreOut | None = None
    documents: list[DocumentOut] = Field(default_factory=list)


# ---------------------------------------------------------------------- queue


class CopyableAnswer(BaseModel):
    """One answer-bank value, shaped for one-tap copying."""

    question: str
    value: str
    answered: bool


class QueueCard(BaseModel):
    """Everything needed to apply by hand in 90 seconds, in one payload.

    Shaped for the stopwatch rather than for REST purity: the UI must not have
    to make a second call to show the cover letter or the answers, because the
    round trip is the thing that makes manual applying feel slow.
    """

    job: JobOut
    score: float | None
    reasoning: str | None
    apply_url: str
    resume_document_id: int | None
    cover_letter_document_id: int | None
    combined_document_id: int | None
    cover_letter_text: str
    answers: list[CopyableAnswer]
    unanswered_questions: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- application


class ApplicationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: int
    applied_at: datetime
    outcome: ApplicationOutcome
    response_status: ResponseStatus
    response_at: datetime | None
    failure_reason: str | None
    platform: str | None
    user_notes: str | None
    attachment_readback: str | None
    resume_doc_id: int | None
    cover_letter_doc_id: int | None
    answers_given: dict[str, Any] = Field(default_factory=dict)

    job_title: str | None = None
    job_company: str | None = None
    job_url: str | None = None


class ApplicationPatch(BaseModel):
    user_notes: str | None = None
    response_status: ResponseStatus | None = None
    outcome: ApplicationOutcome | None = None


# ------------------------------------------------------------------ analytics


class AnalyticsBucket(BaseModel):
    """One row of a breakdown, honest about whether it means anything."""

    key: str
    applied: int
    acknowledged: int
    replied: int
    interviews: int

    sufficient_data: bool
    """False when n is below the reporting minimum.

    The UI must grey the row out rather than render a rate. A 100% interview
    rate from one application is worse than showing nothing: it invites a real
    decision to be made on noise.
    """

    interview_rate: float | None = None
    any_reply_rate: float | None = None


class FunnelStage(BaseModel):
    stage: str
    count: int


class AnalyticsResponse(BaseModel):
    minimum_sample: int
    total_applied: int
    funnel: list[FunnelStage]
    by_campaign: list[AnalyticsBucket]
    by_platform: list[AnalyticsBucket]
    by_score_decile: list[AnalyticsBucket]
    by_rubric_version: list[AnalyticsBucket]


# ------------------------------------------------------------------- settings


class SettingsIn(BaseModel):
    """The user-tunable subset. Note what is absent: allow_live_submit."""

    llm_monthly_cap_usd: float | None = None
    apply_window_start: str | None = None
    apply_window_end: str | None = None
    apply_min_interval_floor_seconds: int | None = None
    scoring_stage1_top_n: int | None = None
    scoring_cost_target_usd: float | None = None
    discovery_default_hours_old: int | None = None


class SettingsOut(BaseModel):
    llm_monthly_cap_usd: float
    apply_window_start: str
    apply_window_end: str
    apply_min_interval_floor_seconds: int
    scoring_stage1_top_n: int
    scoring_cost_target_usd: float
    discovery_default_hours_old: int
    timezone: str

    allow_live_submit: bool
    """READ ONLY. Env-only switch; the API refuses to set it. See the router."""

    spend: dict[str, Any] = Field(default_factory=dict)
    circuit_breakers: dict[str, Any] = Field(default_factory=dict)


class ControlState(BaseModel):
    stopped: bool
    stop_file: str
    reason: str | None = None


class PreferenceOut(BaseModel):
    """One learned or stated preference, with where it came from.

    ``source`` and ``status`` are shown in the UI, not hidden implementation
    detail: the point of the page is that the user can see what the system
    decided for itself and undo it.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    key: str
    value: str
    value_type: str
    scope: str
    campaign_id: int | None
    source: str
    status: str
    confidence: float
    times_confirmed: int
    times_ignored: int
    evidence: str | None
    learned_at: datetime
    confirmed_at: datetime | None


class PreferenceIn(BaseModel):
    """A preference the user sets by hand. Always user_set, never inferred."""

    key: str = Field(min_length=1, max_length=200)
    value: str = Field(max_length=2000)
    value_type: AnswerType = AnswerType.TEXT
    campaign_id: int | None = None


class FactOut(BaseModel):
    """One stated fact, plus what has been derived from it."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    key: str
    text: str
    category: str
    jurisdiction: str | None
    updated_at: datetime


class FactIn(BaseModel):
    """A fact as typed. Stored verbatim — nothing here normalises the text."""

    text: str = Field(max_length=8000)
    jurisdiction: str | None = None


class DerivedAnswerOut(BaseModel):
    """An answer worked out from a fact, and whether it has been confirmed."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    question_key: str
    question_text: str
    answer_value: str
    answer_type: str
    fact_id: int | None
    region: str | None
    reasoning: str | None
    confirmed_at: datetime | None
    stale: bool = False
    """True when the source fact has been edited since. A stale derivation does
    not answer anything — it is shown so the change is visible rather than
    silent."""


class SessionHealthOut(BaseModel):
    """One site's signed-in state, as the Sessions page shows it."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    site: str
    status: str
    detail: str | None
    cookie_count: int
    last_checked_at: datetime | None
    last_verified_at: datetime | None
    consecutive_failures: int
