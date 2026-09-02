"""The database schema, and the status vocabularies that go with it.

Single source of truth. Every other module imports its tables *and* its enums
from here — `JobStatus.APPLIED` is defined once so a typo in a string literal
can never quietly create an eleventh job state that nothing else understands.

Three conventions worth knowing before adding a table:

* **Enums are stored by value, not by name.** SQLModel's default mapping writes
  the member *name* (``DISCOVERED``), which disagrees with the lowercase string
  every other layer — API payloads, logs, the dashboard — actually uses. Every
  enum column therefore goes through :func:`_enum_column`, which pins storage to
  the member value. It emits a plain ``VARCHAR`` with no ``CHECK`` constraint:
  values are still validated in Python on write, but adding a status later stays
  a code change instead of a SQLite table rebuild.
* **Timestamps are UTC, written by :func:`utcnow`.** SQLite has no timezone type,
  so a value read back is a *naive* datetime holding UTC wall-clock. Attach
  ``datetime.UTC`` before doing local-time arithmetic; never assume it is local.
* **JSON columns are replaced, not mutated.** SQLAlchemy does not track in-place
  edits, so ``job.exclusions["x"] = 1`` will not be persisted. Assign a new
  object: ``campaign.exclusions = {**campaign.exclusions, "x": 1}``.

Importing this module registers the tables on ``SQLModel.metadata``; it does not
touch the database. Alembic owns schema creation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from sqlalchemy import JSON, Column, ForeignKey, Integer
from sqlalchemy import Enum as SAEnum
from sqlmodel import Field, Index, SQLModel, UniqueConstraint

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def utcnow() -> datetime:
    """Timezone-aware now. The only clock this schema uses — UTC in the DB."""
    return datetime.now(UTC)


def _enum_values(enum_type: type[Enum]) -> list[str]:
    """Tell SQLAlchemy to persist member values rather than member names."""
    return [str(member.value) for member in enum_type]


def _enum_column(enum_type: type[Enum]) -> Column:
    """Build the column for an enum field.

    Defined once so no table can drift into storing names while its neighbours
    store values. A fresh ``Column`` per call — SQLAlchemy columns cannot be
    shared between tables.
    """
    return Column(
        SAEnum(
            enum_type,
            native_enum=False,
            values_callable=_enum_values,
            # SQLAlchemy otherwise lets an unrecognised *string* through to the
            # database unchecked, which is exactly the typo this module exists to
            # prevent. Valid values still work as plain strings.
            validate_strings=True,
        ),
        nullable=False,
    )


# --------------------------------------------------------------------------- #
# Enums — the status vocabularies. Import these, never the raw strings.
# --------------------------------------------------------------------------- #


class GrayZoneAction(str, Enum):
    """What a campaign does with a job scoring between floor and auto-apply."""

    APPLY = "apply"
    SKIP = "skip"
    ASK = "ask"
    QUEUE = "queue"


class MatchType(str, Enum):
    """How an answer-bank entry is matched against a screening question."""

    EXACT = "exact"
    REGEX = "regex"
    FUZZY = "fuzzy"


class AnswerType(str, Enum):
    """Shape of a stored screening answer, so the applier can coerce it."""

    TEXT = "text"
    BOOLEAN = "boolean"
    CHOICE = "choice"
    NUMBER = "number"
    DATE = "date"


class TemplateKind(str, Enum):
    """What a template renders."""

    RESUME = "resume"
    COVER_LETTER = "cover_letter"
    EMAIL = "email"


class ApplyType(str, Enum):
    """How a job can be applied to, which decides the applier and the guardrails."""

    QUICK_APPLY = "quick_apply"
    EASY_APPLY = "easy_apply"
    EXTERNAL = "external"
    UNKNOWN = "unknown"
    MANUAL_ONLY = "manual_only"


class JobStatus(str, Enum):
    """Lifecycle of a discovered job."""

    DISCOVERED = "discovered"
    SCORED = "scored"
    REJECTED = "rejected"
    QUEUED = "queued"
    DOCUMENTS_READY = "documents_ready"
    NEEDS_ANSWER = "needs_answer"
    APPLYING = "applying"
    APPLIED = "applied"
    FAILED = "failed"
    MANUAL_QUEUE = "manual_queue"
    SKIPPED = "skipped"
    GHOSTED = "ghosted"


class DocumentKind(str, Enum):
    """What a built PDF contains."""

    RESUME = "resume"
    COVER_LETTER = "cover_letter"
    COMBINED = "combined"


class ApplicationOutcome(str, Enum):
    """Terminal result of one submit attempt."""

    SUBMITTED = "submitted"
    FAILED = "failed"
    ABORTED = "aborted"


class ResponseStatus(str, Enum):
    """What the employer did after the application went in."""

    NONE = "none"
    ACKNOWLEDGED = "acknowledged"
    REJECTED = "rejected"
    INTERVIEW_REQUEST = "interview_request"
    RECRUITER_OUTREACH = "recruiter_outreach"
    GHOSTED = "ghosted"


class RunPhase(str, Enum):
    """Which stage of the pipeline a run covers."""

    DISCOVERY = "discovery"
    SCORING = "scoring"
    DOCUMENTS = "documents"
    APPLY = "apply"
    EMAIL = "email"
    MAINTENANCE = "maintenance"


class FailureType(str, Enum):
    """What kind of failure a FailureEvent records.

    Coarse on purpose. The question the ledger answers is "which selectors drift
    most, which employers keep abstaining, which questions keep arriving" — and
    a taxonomy fine enough to need its own documentation would produce buckets
    with one member each, which trends nothing.
    """

    SELECTOR_DRIFT = "selector_drift"
    """A strategy stopped working and a lower-priority one took over."""

    ELEMENT_UNRESOLVED = "element_unresolved"
    """No strategy resolved an element. The job went to the manual queue."""

    ANSWER_ABSTAINED = "answer_abstained"
    """A screening question could not be answered from the bank."""

    PARSE_GATE = "parse_gate"
    """A built document failed the parse gate and was not attached."""

    READBACK_MISMATCH = "readback_mismatch"
    """The form reported a different filename than the one uploaded."""

    SUBMIT_FAILED = "submit_failed"
    """The submit itself raised, or no confirmation state appeared."""

    SOURCE_UNAVAILABLE = "source_unavailable"
    """A discovery source returned nothing because every request failed."""


class FormMapTier(str, Enum):
    """Form-map scope. Company maps override platform maps."""

    PLATFORM = "platform"
    COMPANY = "company"


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #


class Profile(SQLModel, table=True):
    """The user's raw material, versioned.

    Never edited in place: a change writes a new row with a new ``version`` so
    every :class:`Score` stays attributable to the profile it was computed from.
    ``version`` is unique for exactly that reason.
    """

    __tablename__ = "profile"
    __table_args__ = (UniqueConstraint("version", name="uq_profile_version"),)

    id: int | None = Field(default=None, primary_key=True)
    version: int
    identity: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )
    work_rights: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )
    experience: list[Any] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    projects: list[Any] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    education: list[Any] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    certifications: list[Any] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    skills: list[Any] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    preferences: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )
    created_at: datetime = Field(default_factory=utcnow)


class Campaign(SQLModel, table=True):
    """A search: what to look for, how to judge it, how hard to push.

    The profile is shared across campaigns; everything that varies per job hunt
    — terms, rubric, thresholds, caps, templates — lives here.
    """

    __tablename__ = "campaign"

    id: int | None = Field(default=None, primary_key=True)
    name: str
    active: bool = True
    search_terms: list[Any] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    locations: list[Any] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    salary_floor: int | None = None
    work_types: list[Any] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    exclusions: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )
    score_floor: float
    score_auto_apply: float
    # Defaults to ASK: when the score is ambiguous the house rule is to ask the
    # user, never to guess in either direction.
    gray_zone_action: GrayZoneAction = Field(
        default=GrayZoneAction.ASK, sa_column=_enum_column(GrayZoneAction)
    )
    daily_caps: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )
    target_goal_type: str | None = None
    target_goal_count: int | None = None
    template_ids: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )
    rubric: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )
    rubric_version: int = 1
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class AnswerBank(SQLModel, table=True):
    """Verified screening answers. Nothing else may answer a screening question.

    ``campaign_id`` NULL means the entry is global; a campaign-scoped entry wins
    over a global one. ``verified_at`` NULL means the answer has not been
    confirmed by the user yet.
    """

    __tablename__ = "answer_bank"

    id: int | None = Field(default=None, primary_key=True)
    question_pattern: str
    match_type: MatchType = Field(sa_column=_enum_column(MatchType))
    answer_value: str
    answer_type: AnswerType = Field(sa_column=_enum_column(AnswerType))
    campaign_id: int | None = Field(default=None, foreign_key="campaign.id", index=True)
    # none_as_null so "no choices" is SQL NULL rather than the JSON text 'null'.
    choices: list[Any] | None = Field(
        default=None, sa_column=Column(JSON(none_as_null=True), nullable=True)
    )
    verified_at: datetime | None = None
    notes: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Template(SQLModel, table=True):
    """A Jinja2 source document. ``body`` is the template text, not the output."""

    __tablename__ = "template"

    id: int | None = Field(default=None, primary_key=True)
    kind: TemplateKind = Field(sa_column=_enum_column(TemplateKind))
    name: str
    body: str
    version: int = 1
    campaign_id: int | None = Field(default=None, foreign_key="campaign.id", index=True)
    is_default: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Job(SQLModel, table=True):
    """One job ad, from any source.

    ``(source, source_job_id)`` is the source's own identity and is unique — the
    same ad seen twice on the same board is one row. ``dedupe_hash`` catches the
    harder case: the same role cross-posted to different boards under different
    ids. Everything a board may legitimately omit (location, description,
    salary, posting date) is nullable, because discovery must not crash on a
    thin listing.
    """

    __tablename__ = "job"
    __table_args__ = (
        UniqueConstraint("source", "source_job_id", name="uq_job_source_source_job_id"),
        Index("ix_job_dedupe_hash", "dedupe_hash"),
        Index("ix_job_status", "status"),
    )

    id: int | None = Field(default=None, primary_key=True)
    source: str
    source_job_id: str
    url: str
    title: str
    company: str
    location: str | None = None
    description: str | None = None
    salary_min: int | None = None
    salary_max: int | None = None

    salary_basis: str | None = None
    """What the advertiser actually stated: annual, hourly, daily, monthly.

    ``salary_min``/``salary_max`` are always annualised so a campaign salary
    floor can filter on one scale. This field, with ``salary_is_estimated``,
    keeps the system honest about the difference — an ad quoting "$60/hr" must
    never be reported as though the employer stated an annual figure.
    """

    salary_is_estimated: bool = False
    """True when the annualised figures were derived rather than stated."""

    posted_at: datetime | None = None
    discovered_at: datetime = Field(default_factory=utcnow)
    dedupe_hash: str
    apply_type: ApplyType = Field(
        default=ApplyType.UNKNOWN, sa_column=_enum_column(ApplyType)
    )
    ad_contact_email: str | None = None
    campaign_id: int | None = Field(default=None, foreign_key="campaign.id", index=True)
    status: JobStatus = Field(
        default=JobStatus.DISCOVERED, sa_column=_enum_column(JobStatus)
    )

    needs_answer_question: str | None = None
    """The screening question this job is parked on, verbatim.

    Set when the status becomes ``NEEDS_ANSWER``, cleared on re-queue. It has to
    be persisted rather than held in memory because the two halves of the loop
    are different processes: the apply pass parks the job and asks, the Telegram
    bot receives the reply minutes or hours later. Without it ``/answer`` cannot
    tell which question it is answering, and with several jobs parked at once it
    would file the answer against the wrong question — which then resolves
    nothing and re-parks the job on the next pass.
    """


class Document(SQLModel, table=True):
    """A built PDF on disk.

    ``parse_check_passed`` is the attach gate: false means the PDF failed text
    extraction and must never reach an employer. ``sha256`` is what the applier
    reads back after upload to prove the right file went up.
    """

    __tablename__ = "document"

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id", index=True)
    kind: DocumentKind = Field(sa_column=_enum_column(DocumentKind))
    path: str
    sha256: str
    parse_check_passed: bool = False
    parse_report: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )
    template_version: int | None = None
    built_at: datetime = Field(default_factory=utcnow)


class Score(SQLModel, table=True):
    """One scoring of one job against one profile version and one rubric version.

    The unique constraint makes re-scoring idempotent while keeping the history:
    bump the profile or the rubric and you get a new row, not a lost one.
    ``stage1`` is the cheap embedding pass, ``stage2`` the LLM rubric pass, and
    both are NULL until their stage runs.
    """

    __tablename__ = "score"
    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "profile_version",
            "rubric_version",
            name="uq_score_job_profile_rubric",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id")
    profile_version: int
    rubric_version: int
    stage1: float | None = None
    stage2: float | None = None
    final: float | None = None
    reasoning: str | None = None
    matched_skills: list[Any] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    gaps: list[Any] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    red_flags: list[Any] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    scored_at: datetime = Field(default_factory=utcnow)


class Application(SQLModel, table=True):
    """The audit record of a submit attempt — successful or not.

    ``job_id`` is UNIQUE: one application per job, ever. That constraint is the
    enforcement of the house rule, not a convenience index, so it lives on the
    column rather than in application code. Rows are written for failed and
    aborted attempts too; ``outcome`` says which.
    """

    __tablename__ = "application"
    __table_args__ = (UniqueConstraint("job_id", name="uq_application_job_id"),)

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id")
    applied_at: datetime = Field(default_factory=utcnow)
    resume_doc_id: int | None = Field(default=None, foreign_key="document.id")
    cover_letter_doc_id: int | None = Field(default=None, foreign_key="document.id")
    attachment_readback: str | None = None
    answers_given: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )
    # Screenshot paths are nullable: an attempt aborted before submit has no
    # "after" shot, and the evidence trail should record that honestly.
    screenshot_pre: str | None = None
    screenshot_post: str | None = None
    outcome: ApplicationOutcome = Field(sa_column=_enum_column(ApplicationOutcome))
    failure_reason: str | None = None
    response_status: ResponseStatus = Field(
        default=ResponseStatus.NONE, sa_column=_enum_column(ResponseStatus)
    )
    response_at: datetime | None = None
    user_notes: str | None = None
    platform: str | None = None


class Run(SQLModel, table=True):
    """One execution of one pipeline phase, for the dashboard and for triage.

    ``ended_at`` NULL means still running or killed mid-flight; ``ok`` false
    means the phase reported failures in ``errors``.
    """

    __tablename__ = "run"

    id: int | None = Field(default=None, primary_key=True)
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None
    phase: RunPhase = Field(sa_column=_enum_column(RunPhase))
    counts: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )
    errors: list[Any] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    ok: bool = True


class FormMap(SQLModel, table=True):
    """A cached field mapping for an application form, keyed by form fingerprint.

    Records *where* fields are, never what values go in them. ``trusted`` is the
    graduation flag: a draft is used only with approval until enough clean
    successes flip it, which is why the success/fail counters are stored.
    """

    __tablename__ = "form_map"
    __table_args__ = (UniqueConstraint("fingerprint", name="uq_form_map_fingerprint"),)

    id: int | None = Field(default=None, primary_key=True)
    fingerprint: str
    tier: FormMapTier = Field(sa_column=_enum_column(FormMapTier))
    platform: str | None = None
    path: str
    success_count: int = 0
    fail_count: int = 0
    trusted: bool = False
    last_verified_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


class FailureEvent(SQLModel, table=True):
    """One failure, remembered.

    WHY A TABLE AND NOT JUST A LOG LINE
        The circuit breaker already stops repeated failure, and then forgets it
        happened. That makes every failure look like the first one. This is the
        memory: with it the system can answer which selectors drift most, which
        employers consistently abstain, which questions keep arriving
        unanswered, and whether a parse-gate failure is new or recurring.

    IDENTITY, NOT NARRATIVE
        The columns are the dimensions worth grouping by. ``detail`` carries the
        human-readable remainder and nothing aggregates on it — a trend built by
        grouping free text is a trend built on phrasing.

    RESOLUTION IS PART OF THE RECORD
        ``resolved_at`` and ``resolution`` are what separate "this keeps
        happening" from "this happened and was fixed". Without them the ledger
        grows monotonically and every old failure keeps voting in the trends.
    """

    __tablename__ = "failure_event"

    id: int | None = Field(default=None, primary_key=True)
    platform: str = Field(index=True)
    failure_type: FailureType = Field(sa_column=_enum_column(FailureType))

    element_id: str | None = Field(default=None, index=True)
    """The site-knowledge element key, when the failure was about finding one."""

    flow_variant: str | None = Field(default=None, index=True)
    """The flow fingerprint, so a failure specific to one variant is visible."""

    company: str | None = Field(default=None, index=True)
    """Which employer, so "this company always abstains" is answerable."""

    question: str | None = None
    """The normalised screening question, when that is what failed."""

    job_id: int | None = Field(
        default=None,
        # ON DELETE SET NULL, not the default RESTRICT and not CASCADE.
        #
        # The ledger exists to outlive the thing that failed. RESTRICT makes a
        # failure row block the deletion of its job — the ledger would quietly
        # pin every job it ever touched. CASCADE goes the other way and erases
        # the record that anything failed, which loses exactly the history this
        # table was added to keep.
        #
        # SET NULL keeps the trend and drops the pointer: the platform, element,
        # company and question all survive, and those are what anything
        # aggregates on. Only the link back to one deleted job is lost.
        sa_column=Column(
            Integer, ForeignKey("job.id", ondelete="SET NULL"), nullable=True, index=True
        ),
    )
    detail: str | None = None

    occurred_at: datetime = Field(default_factory=utcnow, index=True)
    resolved_at: datetime | None = None
    resolution: str | None = None


class LLMSpend(SQLModel, table=True):
    """One LLM call's cost. Written by ``llm/client.py`` and by nothing else.

    Failed calls are recorded too (``ok`` false): tokens are billed whether or
    not the response was usable, so the monthly cap has to see them.
    """

    __tablename__ = "llm_spend"

    id: int | None = Field(default=None, primary_key=True)
    called_at: datetime = Field(default_factory=utcnow)
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    purpose: str
    job_id: int | None = Field(default=None, foreign_key="job.id", index=True)
    ok: bool = True
    error: str | None = None


__all__ = [
    "AnswerBank",
    "AnswerType",
    "Application",
    "ApplicationOutcome",
    "ApplyType",
    "Campaign",
    "Document",
    "DocumentKind",
    "FormMap",
    "FormMapTier",
    "GrayZoneAction",
    "Job",
    "JobStatus",
    "LLMSpend",
    "MatchType",
    "Profile",
    "ResponseStatus",
    "Run",
    "RunPhase",
    "Score",
    "Template",
    "TemplateKind",
    "utcnow",
]
