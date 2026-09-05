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

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    "CacheRateOut",
    "CampaignFunnel",
    "CampaignIn",
    "CampaignOut",
    "ControlState",
    "CostPointOut",
    "CoveragePointOut",
    "DerivedAnswerOut",
    "DocumentOut",
    "FactIn",
    "FactLeverageOut",
    "FactOut",
    "FunnelStage",
    "JobDetail",
    "JobOut",
    "OutboundEditIn",
    "OutboundMessageOut",
    "OutboundSendIn",
    "Page",
    "PerformanceTelemetry",
    "PreferenceIn",
    "PreferenceOut",
    "ProfileIn",
    "ProfileOut",
    "QuestionClusterOut",
    "QuestionIntelligence",
    "QueueCard",
    "RunProfileOut",
    "ScoreOut",
    "SessionHealthOut",
    "SettingsIn",
    "SettingsOut",
    "StageProfileOut",
    "StageStatOut",
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


class ChoiceOut(BaseModel):
    """One option on a closed-list question: what is read, what is submitted."""

    label: str
    value: str
    is_free_text: bool = False


def _normalise_choices(value: Any) -> Any:
    """Accept every shape a stored option set has ever had.

    Rows seeded before options carried values hold bare strings, and the
    dashboard hands back whatever it was given. Normalising here rather than
    migrating means a legacy row still renders instead of 500ing the page — and
    a bare string is not lossy: a form with no ``value`` attribute submits its
    own text, which is exactly what this expands to.
    """
    if not value:
        return None
    from dataclasses import asdict

    from backend.apply.draft import as_choices

    return [asdict(choice) for choice in as_choices(value)] or None


class AnswerIn(BaseModel):
    question_pattern: str
    match_type: MatchType = MatchType.FUZZY
    answer_value: str = ""
    answer_type: AnswerType = AnswerType.TEXT
    campaign_id: int | None = None
    choices: list[ChoiceOut] | None = None
    notes: str | None = None
    verified: bool = False

    _fix_choices = field_validator("choices", mode="before")(
        staticmethod(_normalise_choices)
    )


class AnswerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    question_pattern: str
    match_type: MatchType
    answer_value: str
    answer_type: AnswerType
    campaign_id: int | None
    choices: list[ChoiceOut] | None
    notes: str | None
    verified_at: datetime | None
    updated_at: datetime

    _fix_choices = field_validator("choices", mode="before")(
        staticmethod(_normalise_choices)
    )

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


class CampaignFunnel(BaseModel):
    """One campaign's whole pipeline, not just what came out of the end.

    ``discovered`` and ``scored`` are counts of jobs; every later stage counts
    applications. That is the join the funnel is for — a campaign that discovers
    four hundred ads and applies to three is failing somewhere the reply rate
    cannot see.
    """

    campaign_id: int | None
    name: str
    discovered: int
    scored: int
    applied: int
    acknowledged: int
    replied: int
    interviews: int

    sufficient_data: bool
    interview_rate: float | None = None
    """Interviews per application, or None below the reporting minimum.

    Only the rates are suppressed. The counts above are facts about what
    happened and are always shown — it is the comparison that needs a sample.
    """


class QuestionClusterOut(BaseModel):
    """One screening question, however many ways employers worded it."""

    question: str
    variants: int
    asked: int
    employers: int
    platforms: int
    resolved: int
    abstained: int
    jobs_parked: int
    last_seen: datetime | None = None


class CoveragePointOut(BaseModel):
    """One week of the question ledger."""

    week: str
    asked: int
    resolved: int
    sufficient_data: bool
    rate: float | None = None


class FactLeverageOut(BaseModel):
    """One stated fact and how many derived answers it supports."""

    fact_id: int
    key: str
    category: str
    derived: int
    confirmed: int
    stale: int


class QuestionIntelligence(BaseModel):
    """What is being asked, what it costs, and whether the loop is learning."""

    frequency: list[QuestionClusterOut]
    friction: list[QuestionClusterOut]
    coverage: list[CoveragePointOut]
    fact_leverage: list[FactLeverageOut]


class StageStatOut(BaseModel):
    """One timed stage, summarised."""

    stage: str
    observations: int
    total_ms: int
    mean_ms: int
    median_ms: int
    slowest_ms: int


class StageProfileOut(BaseModel):
    """Where the time went.

    ``pacing`` is a separate field, never an entry in ``work``. The wait between
    submissions is a safety property protecting the user's account, and a chart
    that let it read as latency would invite someone to shorten it.
    """

    work: list[StageStatOut]
    pacing: StageStatOut | None = None
    slowest_stage: str | None = None
    work_total_ms: int = 0


class RunProfileOut(BaseModel):
    """One apply pass and the stage that cost it the most."""

    run_id: int
    started_at: datetime
    ended_at: datetime | None
    applications: int
    work_ms: int
    pacing_ms: int
    slowest_stage: str | None
    slowest_stage_ms: int


class CacheRateOut(BaseModel):
    cache: str
    unit: str
    """What one lookup counts. The caches are consulted at different
    granularities, and some in sequence, so the denominators are not the same
    population — saying so is the difference between a chart and a trap."""

    week: str
    lookups: int
    hits: int
    rate: float


class CostPointOut(BaseModel):
    week: str
    applications: int
    total_usd: float
    per_application_usd: float


class PerformanceTelemetry(BaseModel):
    """Speed, cache hit rates and cost — the three things that should improve."""

    stages: StageProfileOut
    runs: list[RunProfileOut]
    caches: list[CacheRateOut]
    cost: list[CostPointOut]


class AnalyticsResponse(BaseModel):
    minimum_sample: int
    total_applied: int
    funnel: list[FunnelStage]
    by_campaign: list[AnalyticsBucket]
    by_platform: list[AnalyticsBucket]
    by_score_decile: list[AnalyticsBucket]
    by_rubric_version: list[AnalyticsBucket]
    campaign_funnels: list[CampaignFunnel]
    questions: QuestionIntelligence
    performance: PerformanceTelemetry


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


class OutboundMessageOut(BaseModel):
    """A drafted or sent follow-up, as the Outbound page shows it."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: int
    to_address: str
    subject: str
    body: str
    attachments: list[Any]
    status: str
    approved_by: str | None
    created_at: datetime
    sent_at: datetime | None


class OutboundEditIn(BaseModel):
    """An edited subject and body. The recipient is deliberately absent.

    Not an oversight: the address comes from the ad and from nowhere else, and
    accepting one here would make this endpoint the recipient parameter the
    module refuses to have.
    """

    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=20000)


class OutboundSendIn(BaseModel):
    """Who approved it. There is no auto-send path."""

    approved_by: str = Field(min_length=1, max_length=120)
