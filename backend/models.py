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


class Region(str, Enum):
    """Which Seek market a campaign, job or answer belongs to.

    Seek AU and Seek NZ are the same platform serving different markets. The
    difference is configuration — a site key and a locale — not a second
    adapter, which is why this is an enum on the existing rows rather than a new
    source.

    It is also a safety boundary. Salary is quoted as a bare ``$`` on both
    sites with no currency field anywhere in the payload, so an AU floor
    silently compares against NZD without this. And work rights are a different
    question in each country: an answer verified for one is not evidence about
    the other.
    """

    AU = "AU"
    NZ = "NZ"


class OutboundStatus(str, Enum):
    """Where a follow-up email is in its life. Never auto-advances."""

    DRAFTED = "drafted"
    """Written and waiting. The only state that can become SENT."""

    SENT = "sent"
    SKIPPED = "skipped"
    """The user declined. Terminal — one message per job means a skipped job
    does not get a second draft."""


class SessionStatus(str, Enum):
    """What the last check found for one site's stored session.

    UNKNOWN is a first-class outcome, not a failure to decide. A page that shows
    neither a login form nor a recognised signed-in element tells us nothing,
    and reporting that as DEAD would page the user about a working session every
    time a site reshuffles its header.
    """

    LIVE = "live"
    DEAD = "dead"
    UNKNOWN = "unknown"
    NO_SESSION = "no_session"
    """No cookies stored for this site at all — nothing has ever signed in."""

    UNREACHABLE = "unreachable"
    """The check itself failed. Says nothing about the session."""


class FactCategory(str, Enum):
    """What area of the user's situation a fact describes.

    Coarse and closed. The categories exist so a screening question can be
    routed to the handful of facts that could possibly answer it, not to
    describe the user exhaustively — a taxonomy fine enough to need its own
    documentation would put every fact in a category of one.
    """

    WORK_RIGHTS = "work_rights"
    LICENCE = "licence"
    CHECKS = "checks"
    """Police checks, working-with-children, clearances."""

    EDUCATION = "education"
    EXPERIENCE = "experience"
    AVAILABILITY = "availability"
    """Notice period, start date, shift work, relocation, travel."""

    COMPENSATION = "compensation"
    TRANSPORT = "transport"
    REFEREES = "referees"
    HEALTH = "health"
    """Medicals, drug tests, vaccination status."""

    BUSINESS = "business"
    """ABN, contracting arrangements."""

    OTHER = "other"


class PreferenceScope(str, Enum):
    """How widely a preference applies."""

    GLOBAL = "global"
    CAMPAIGN = "campaign"


class PreferenceSource(str, Enum):
    """Where a preference came from. This is a safety boundary, not metadata.

    ``INFERRED`` is the only one the system may write on its own, and an
    inferred preference is a *proposal* — it changes nothing until the user
    confirms it over Telegram. See ``backend/preferences.py``.
    """

    USER_SET = "user_set"
    """The user stated it directly, in the UI."""

    ASKED = "asked"
    """The system asked and the user answered."""

    INFERRED = "inferred"
    """Derived from observed behaviour. A proposal until confirmed."""


class PreferenceStatus(str, Enum):
    """Where a proposal is in its life.

    Only ``ACTIVE`` preferences affect behaviour. A proposal sits in
    ``PROPOSED`` until the user says yes; ignoring it twice retires it, because
    a question the user has silently declined twice is a question that should
    stop arriving.
    """

    ACTIVE = "active"
    PROPOSED = "proposed"
    REJECTED = "rejected"
    RETIRED = "retired"


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


class QuestionResolution(str, Enum):
    """How a screening question was answered on one encounter, or that it was not.

    The vocabulary is the four mechanisms that can answer a question, plus the
    one honest non-answer. It is deliberately about the MECHANISM and not about
    the answer: the question this enum exists to make answerable is "is the
    system learning to answer these without me", and that is a question about
    where the answer came from.

    Profile-matched identity fields are not in here because they are not
    screening questions — a form asking for an email address is not asking the
    user anything. Counting them would drive coverage toward 100% by adding a
    denominator that never fails.
    """

    BANK = "bank"
    """A verified answer-bank row matched."""

    FACT = "fact"
    """A confirmed derivation from a stated fact answered it."""

    FORM_MAP = "form_map"
    """A cached form map routed the field to a bank row or the profile."""

    ABSTAINED = "abstained"
    """Nothing could answer it. The job was parked and the user asked."""


class Stage(str, Enum):
    """A timed step of one application.

    PACING IS NOT WORK
        The randomised wait between submissions is a safety property protecting
        the user's account, not latency to optimise. It is measured because an
        unmeasured wait is indistinguishable from a hang — but it is a separate
        member so that no total can quietly include it, and
        :data:`WORK_STAGES` is what any duration sums over.
    """

    PAGE_LOAD = "page_load"
    FIELD_ENUMERATION = "field_enumeration"
    ANSWER_RESOLUTION = "answer_resolution"
    DOCUMENT_BUILD = "document_build"
    UPLOAD = "upload"
    SUBMIT = "submit"

    PACING = "pacing"
    """Deliberate delay. Never counted as work. See the class docstring."""


WORK_STAGES = frozenset(Stage) - {Stage.PACING}
"""Every stage that is actually doing something.

Defined by subtraction so a new stage is work unless someone deliberately
excludes it — the safe default, since the failure mode being prevented is a
wait being counted as work rather than the reverse.
"""


class CacheName(str, Enum):
    """A cache whose hit rate is recorded at the lookup itself.

    The answer bank and the facts layer are absent on purpose: their lookups
    ARE screening questions, and ``question_event`` already records the
    outcome of every one. Recording them again here would be the same fact in
    two tables, free to drift. ``backend/telemetry.py`` reads their rates from
    the question ledger and reports all five together.
    """

    FORM_MAP = "form_map"
    """One lookup per form shape. The second application to a known shape
    should cost no model call at all."""

    SITE_KNOWLEDGE = "site_knowledge"
    """One lookup per element. A hit is the FIRST strategy working; a lower
    one working is a heal, which is a miss for this purpose — the element was
    found, but the file's idea of how to find it was wrong."""

    EMBEDDING = "embedding"
    """One lookup per text embedded."""


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
    region: Region = Field(sa_column=_enum_column(Region), default=Region.AU)
    """Which Seek market this campaign searches. Drives the site key, the
    locale, the currency a salary floor is read in, and the timezone the apply
    window is measured in."""

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

    fact_category: FactCategory | None = Field(
        sa_column=Column(
            SAEnum(FactCategory, values_callable=_enum_values), nullable=True
        ),
        default=None,
    )
    """Which category of fact can answer this question, when the row is blank.

    Routing, not an answer. The bank's pattern matching already knows how to
    recognise "do you hold a driver's licence?" in all its spellings; this says
    which fact to consult once it has. Without it the derivation layer would
    need a second question matcher, and two matchers disagreeing about what a
    question is asking is how the wrong fact answers it.
    """

    region: Region | None = Field(
        sa_column=Column(SAEnum(Region, values_callable=_enum_values), nullable=True),
        default=None,
    )
    """Which country this answer is true for. NULL means it holds everywhere.

    Work rights, tax numbers, licences and notice periods are different
    questions in AU and NZ, and an answer verified for one is not evidence about
    the other. A row scoped to a region only ever matches that region;
    resolution abstains rather than reaching across.
    """

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
    region: Region = Field(sa_column=_enum_column(Region), default=Region.AU)
    """Derived from the ad's own countryCode, never from the campaign.

    A campaign searching NZ can still surface an Australian ad, and the ad is
    the authority on where it is. This is what ``salary_currency`` is read from.
    """

    salary_currency: str | None = None
    """ISO code for salary_min/salary_max. NULL when no salary was stated.

    Explicit because Seek gives none: both markets print "$75,000 – $90,000"
    with no currency anywhere in the payload, so without this column an NZD
    figure and an AUD figure are the same number.
    """

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

    rubric_hash: str | None = None
    """Content hash of the rubric this score was computed against.

    rubric_version only changes when someone remembers to bump it, so an edited
    rubric produced scores indistinguishable from ones computed before the edit
    — a silently stale shortlist. The hash changes whenever the text does.
    """

    stage1: float | None = None
    stage2: float | None = None
    final: float | None = None
    reasoning: str | None = None
    requirements: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )
    """What the EMPLOYER asked for: must_haves, nice_to_haves, tone.

    Extracted in the same stage-2 call that scores the job, because the model is
    already reading the whole ad there. Stored on the Score rather than the Job
    because it is model output about the ad, and re-scoring against a changed
    rubric can legitimately re-derive it.

    Read by the document build. Scoring itself does not use it — the point is to
    tailor against the ad, not only to rank against it.
    """

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
            Integer,
            ForeignKey("job.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )
    detail: str | None = None

    occurred_at: datetime = Field(default_factory=utcnow, index=True)
    resolved_at: datetime | None = None
    resolution: str | None = None


class QuestionEvent(SQLModel, table=True):
    """One screening question, one encounter, and what answered it.

    WHY THIS EXISTS SEPARATELY FROM EVERYTHING ELSE
        Before this table nothing recorded a question that was answered
        successfully. ``FailureEvent`` records abstentions, and no
        ``Application`` row is written when a job parks — so a submitted
        application had zero abstentions by construction and a parked one had no
        application row. The two tables never describe the same pass, and any
        coverage ratio built from them is a lower bound on one side or the
        other, never a real fraction.

        This is the denominator. Every screening question the flow encounters
        gets a row, resolved or not, so "what share of questions resolve without
        asking me" is a division of two numbers from the same population.

    THE OVERLAP WITH FailureEvent IS DELIBERATE
        An abstention is written to both. The failure ledger answers "what is
        going wrong" and drives the circuit-breaker trends; this ledger answers
        "what am I being asked". Folding one into the other would mean either
        filing successes in a failure table or making the trend report depend on
        a table that mostly holds successes.

    ONE ROW PER ENCOUNTER, NOT PER QUESTION
        A job that parks and is retried after the answer arrives writes the
        question twice — once ABSTAINED, once BANK. That is the learning loop
        being visible, and it is why frequency is reported over DISTINCT
        employers rather than raw row counts.
    """

    __tablename__ = "question_event"

    id: int | None = Field(default=None, primary_key=True)

    question: str = Field(index=True)
    """The normalised question, from ``answers.normalise_question``.

    Normalised so that two forms differing only in casing, numbering or trailing
    punctuation are one question. Near-identical *phrasings* are a separate
    problem and are clustered at read time — see ``backend/questions.py``.
    """

    question_text: str
    """The raw label as the form worded it, for display. Never grouped on."""

    resolution: QuestionResolution = Field(sa_column=_enum_column(QuestionResolution))

    source_row_id: int | None = None
    """The AnswerBank row that answered it, when one did.

    Not a foreign key: the row may later be deleted or merged, and losing the
    history of what answered a question is worse than holding a stale id. It is
    provenance, not a join.
    """

    platform: str = Field(index=True)
    company: str | None = Field(default=None, index=True)
    """Which employer asked. "Across how many employers" is the frequency
    number that matters — one employer asking eleven times is not eleven
    employers asking."""

    job_id: int | None = Field(
        default=None,
        # SET NULL for the same reason FailureEvent uses it: the ledger outlives
        # the job it describes, and the aggregate dimensions (question,
        # platform, company, resolution) all survive without the pointer.
        sa_column=Column(
            Integer,
            ForeignKey("job.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )

    occurred_at: datetime = Field(default_factory=utcnow, index=True)


class StageTiming(SQLModel, table=True):
    """How long one stage of one application took.

    Nothing in this system measured its own speed, so "is it getting faster"
    was unanswerable. ``Run`` records a start and an end for a whole pass, which
    is browser startup plus every application plus every pacing wait as one
    undifferentiated number — a regression in field enumeration and a slow
    network are the same figure.

    WHY A ROW PER STAGE PER APPLICATION
        The question worth answering is which stage regressed, not whether the
        pass got slower. Per-application rather than per-run because the mean is
        the number that moves when something is wrong and the mean needs the
        individual observations.

    PACING IS RECORDED HERE AND EXCLUDED EVERYWHERE
        The wait between submissions is stored as ``Stage.PACING`` so that an
        unexpectedly long wait is visible, and it is not in ``WORK_STAGES``, so
        no work total can include it. It protects the user's LinkedIn account;
        a chart that let it read as latency would invite someone to shorten it.
    """

    __tablename__ = "stage_timing"

    id: int | None = Field(default=None, primary_key=True)
    stage: Stage = Field(sa_column=_enum_column(Stage))
    duration_ms: int
    platform: str | None = Field(default=None, index=True)

    job_id: int | None = Field(
        default=None,
        # SET NULL, as the other ledgers use: the measurement outlives the job.
        sa_column=Column(
            Integer,
            ForeignKey("job.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )

    occurred_at: datetime = Field(default_factory=utcnow, index=True)


class CacheEvent(SQLModel, table=True):
    """One cache lookup, and whether it hit.

    The caches are the whole cost story: a form shape seen twice should cost one
    model call, an ad seen twice should be embedded once, and an element found
    by its first strategy costs one DOM query instead of five. Each rate should
    climb as the system learns, and nothing recorded them — the hits were log
    lines, which cannot be trended.

    See :class:`CacheName` for why the answer bank and the facts layer are not
    in here.
    """

    __tablename__ = "cache_event"

    id: int | None = Field(default=None, primary_key=True)
    cache: CacheName = Field(sa_column=_enum_column(CacheName))
    hit: bool
    platform: str | None = Field(default=None, index=True)

    job_id: int | None = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey("job.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )

    occurred_at: datetime = Field(default_factory=utcnow, index=True)


class Preference(SQLModel, table=True):
    """What the user prefers, learned or told. Key-value, never dynamic columns.

    KEY-VALUE, DELIBERATELY
        A system that adds a column whenever it learns something cannot be
        migrated and cannot be reasoned about: the schema becomes a function of
        the user's history, every deployment has a different one, and no
        migration can be written ahead of time. Rows are boring and boring is
        the point.

        ``value_type`` carries what the string means, so a reader coerces
        rather than guesses.

    SOURCE IS A SAFETY BOUNDARY
        ``source`` is not provenance trivia. Facts about the user — work
        rights, licences, certifications, dates — may only ever be ``USER_SET``
        or ``ASKED``, and never ``INFERRED``: hard rule 1 says facts come from
        the profile verbatim or not at all, and an inferred fact is a fabricated
        one however good the evidence looked. ``preferences.set`` enforces it.

    A PROPOSAL IS NOT A PREFERENCE
        An inferred row is written with ``status=PROPOSED`` and does not affect
        behaviour. Only the user's confirmation makes it ``ACTIVE``.
    """

    __tablename__ = "preference"
    __table_args__ = (
        # One row per key per scope. Without this the same preference could be
        # learned twice with different values and reads would depend on order.
        UniqueConstraint("key", "campaign_id", name="uq_preference_key_scope"),
    )

    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(index=True)
    value: str
    value_type: AnswerType = Field(sa_column=_enum_column(AnswerType))
    scope: PreferenceScope = Field(sa_column=_enum_column(PreferenceScope))
    campaign_id: int | None = Field(default=None, foreign_key="campaign.id", index=True)

    source: PreferenceSource = Field(sa_column=_enum_column(PreferenceSource))
    status: PreferenceStatus = Field(sa_column=_enum_column(PreferenceStatus))

    confidence: float = 0.0
    times_confirmed: int = 0
    times_ignored: int = 0
    """Proposals the user did not answer. Two and it retires itself."""

    evidence: str | None = None
    """Why this was proposed, in the user's terms — shown when asking."""

    learned_at: datetime = Field(default_factory=utcnow)
    last_asked_at: datetime | None = None
    confirmed_at: datetime | None = None


class Fact(SQLModel, table=True):
    """Layer 1: something true about the user, in their own words. VERBATIM.

    THE TEXT IS NEVER ALTERED
        Not normalised, not summarised, not "cleaned up". This is the same rule
        as hard rule 1 applied to storage: the user wrote "Full SA driver's
        licence, class C, held since 2019, no restrictions" and that exact
        sentence is what every derived answer is checked against. A paraphrase
        would quietly become the source of truth for a legal declaration.

    WHY FREE TEXT AND NOT STRUCTURED FIELDS
        A licence is not a boolean. It has a state, a class, an issue date and
        possibly conditions, and the useful shape differs per person and per
        category. Structured fields would force a schema decision for every
        category up front, and the first form asking something the schema did
        not anticipate would have nowhere to put the answer. Prose holds
        everything; the derivation layer is what turns it into a Yes.

    JURISDICTION
        NULL means the fact holds everywhere. Set, it means the fact is only
        evidence about that country — an SA driver's licence answers an
        Australian question and says nothing about a New Zealand one.
    """

    __tablename__ = "fact"
    __table_args__ = (UniqueConstraint("key", name="uq_fact_key"),)

    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(index=True)
    """Stable identifier, e.g. "licence" or "work_rights". One fact per key."""

    text: str
    """The user's own words. Stored verbatim; never rewritten."""

    category: FactCategory = Field(sa_column=_enum_column(FactCategory))
    jurisdiction: Region | None = Field(
        sa_column=Column(SAEnum(Region, values_callable=_enum_values), nullable=True),
        default=None,
    )

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class DerivedAnswer(SQLModel, table=True):
    """Layer 2: an answer worked out from a fact, then confirmed once.

    The point is that the user is asked at most once per question. "Do you hold
    a current driver's licence? Yes/No" is derivable from the licence fact, but
    deriving it is a judgement, so the first derivation goes to Telegram for
    confirmation. After that it is cached and never asked again.

    STALENESS IS BY CONTENT, NOT TIME
        ``fact_text_hash`` is the hash of the fact text this was derived from.
        Editing the fact changes the hash, which invalidates every derivation
        that came from it — because "class C" becoming "class MR" changes the
        answer to questions nobody thought to revisit. A timestamp would not
        catch that; only the content can.
    """

    __tablename__ = "derived_answer"
    __table_args__ = (
        UniqueConstraint(
            "question_key", "region", name="uq_derived_answer_question_region"
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    question_key: str = Field(index=True)
    """The normalised question this answers. Same normalisation as the bank."""

    question_text: str
    """The question as the form asked it, for the confirmation message."""

    answer_value: str
    answer_type: AnswerType = Field(sa_column=_enum_column(AnswerType))

    fact_id: int | None = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey("fact.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
    )
    """Which fact this came from. CASCADE: a deleted fact takes its derivations
    with it, because an answer whose evidence no longer exists is not an answer."""

    fact_text_hash: str
    """Content hash of the fact text at derivation time. See the docstring."""

    region: Region | None = Field(
        sa_column=Column(SAEnum(Region, values_callable=_enum_values), nullable=True),
        default=None,
    )

    reasoning: str | None = None
    """Why the fact supports this answer, shown when asking for confirmation."""

    confirmed_at: datetime | None = None
    """NULL until the user says yes. An unconfirmed derivation never answers."""

    created_at: datetime = Field(default_factory=utcnow)


class SessionHealth(SQLModel, table=True):
    """The last thing we learned about one site's signed-in state.

    WHY THIS IS A TABLE
        Session expiry is the silent failure. The adapter lands on a login page,
        cannot find the form, and parks the job — so the symptom is a pile of
        parked jobs days later, with nothing naming the cause. This makes the
        cause checkable before any application is attempted, and nameable in an
        alert.

    TWO TIMESTAMPS, DELIBERATELY
        ``last_checked_at`` moves on every check. ``last_verified_at`` moves
        only when the session was actually found LIVE. The gap between them is
        the interesting number: "checked a minute ago, last known good four days
        ago" is a session that has been dead for four days, and one timestamp
        cannot say that.
    """

    __tablename__ = "session_health"
    __table_args__ = (UniqueConstraint("site", name="uq_session_health_site"),)

    id: int | None = Field(default=None, primary_key=True)
    site: str = Field(index=True)
    """The site key — a platform key where one exists, else the cookie domain."""

    status: SessionStatus = Field(sa_column=_enum_column(SessionStatus))
    detail: str | None = None
    """What the check saw, in the user's terms. Shown in the alert."""

    cookie_count: int = 0
    """How many cookies are stored for this site. Zero means never signed in."""

    last_checked_at: datetime | None = None
    last_verified_at: datetime | None = None
    """Last time the session was confirmed LIVE. See the docstring."""

    consecutive_failures: int = 0
    """Resets on a LIVE check. Used to alert once rather than on every pass."""


class OutboundMessage(SQLModel, table=True):
    """One follow-up email per job, ever. The record that makes that true.

    The module docstring in integrations/outbound.py has always said "one
    message per job, ever" as one of the three properties that make this
    defensible under the Spam Act. It was documented and not enforced: nothing
    recorded what had been sent, so nothing could refuse a second.

    UNIQUE(job_id) is that enforcement, and it deliberately mirrors
    UNIQUE(job_id) on applications — the same rule, for the same reason, spelled
    the same way.

    A SKIPPED row occupies the slot exactly as a SENT one does. Declining to
    write to an employer is a decision, and offering the same draft again next
    week would quietly overturn it.
    """

    __tablename__ = "outbound_message"
    __table_args__ = (UniqueConstraint("job_id", name="uq_outbound_message_job"),)

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id", index=True)

    to_address: str
    """From the ad, never from a parameter. See the outbound module docstring."""

    subject: str
    body: str
    attachments: list[Any] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    """Filenames only. The files themselves live in data/documents/."""

    status: OutboundStatus = Field(sa_column=_enum_column(OutboundStatus))

    approved_by: str | None = None
    """Who approved it, recorded on the row that was actually sent. NULL on a
    draft, because a draft has not been approved by anyone."""

    created_at: datetime = Field(default_factory=utcnow)
    sent_at: datetime | None = None


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
