"""The shared apply flow, driven end to end against a fake page and adapter.

No browser, no network. Every assertion here is about the *sequence* being
inviolable: an abstention, a bad attachment or a guardrail refusal must each
stop the run before ``submit`` is ever called, and the fake adapter records
whether it was.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlmodel import Session, SQLModel, create_engine

from backend.apply import flow
from backend.apply.draft import FormField
from backend.base import ApplyOutcome
from backend.config import settings
from backend.models import (
    AnswerBank,
    AnswerType,
    Application,
    Campaign,
    Document,
    DocumentKind,
    GrayZoneAction,
    Job,
    JobStatus,
    MatchType,
    Profile,
    Score,
)

# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakePage:
    """Just enough page for the flow. Records nothing it is not asked to."""

    def __init__(self) -> None:
        self.screenshots: list[str] = []

    def screenshot(self, path: str, full_page: bool = False) -> None:
        self.screenshots.append(path)


@dataclass
class FakeAdapter:
    """A cooperative adapter whose behaviour each test bends slightly."""

    platform: str = "seek"
    steps: list[list[FormField]] = field(default_factory=list)
    upload_slot_count: int = 1
    readback: list[str] | None = None
    confirm: bool = True
    submit_raises: bool = False
    redirect: bool = False
    restriction: bool = False

    submitted: bool = False
    attached: list[Document] = field(default_factory=list)
    filled: dict[str, str] = field(default_factory=dict)
    advanced: int = 0

    def can_handle(self, job: Any) -> bool:
        return True

    def open(self, page: Any, job: Any) -> None:
        return None

    def detect_redirect(self, page: Any) -> bool:
        return self.redirect

    def detect_restriction(self, page: Any) -> bool:
        return self.restriction

    def enumerate_fields(self, page: Any, step: int) -> list[FormField]:
        if step < len(self.steps):
            return self.steps[step]
        return []

    def fill_field(self, page: Any, field_: FormField, value: str) -> None:
        self.filled[field_.identifier] = value

    def upload_slots(self, fields: list[FormField]) -> int:
        return self.upload_slot_count

    def attach(self, page: Any, documents: list[Document]) -> None:
        self.attached = list(documents)

    def read_back_attachments(self, page: Any) -> list[str]:
        if self.readback is not None:
            return self.readback
        # Honest default: report exactly what was attached.
        from pathlib import Path

        return [Path(d.path).name for d in self.attached]

    def is_last_step(self, page: Any, fields: list[FormField]) -> bool:
        return True

    def advance(self, page: Any) -> None:
        self.advanced += 1

    def submit(self, page: Any) -> None:
        if self.submit_raises:
            raise RuntimeError("submit exploded")
        self.submitted = True

    def confirmed(self, page: Any) -> bool:
        return self.confirm


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    with Session(engine) as s:
        s.add(
            Profile(
                version=1,
                identity={
                    "name": "Jordan Fitzgerald",
                    "email": "jordan@example.com",
                    "phone": "+61 412 345 678",
                },
            )
        )
        campaign = Campaign(
            id=1,
            name="test",
            active=True,
            search_terms=["dev"],
            locations=["Adelaide SA"],
            score_floor=60.0,
            score_auto_apply=80.0,
            gray_zone_action=GrayZoneAction.QUEUE,
            daily_caps={"seek": 10},
        )
        s.add(campaign)
        s.flush()

        job = Job(
            id=1,
            source="seek",
            source_job_id="1",
            url="https://example.com/1",
            title="Developer",
            company="Acme",
            location="Adelaide SA",
            dedupe_hash="h1",
            campaign_id=1,
            status=JobStatus.DOCUMENTS_READY,
        )
        s.add(job)
        s.flush()

        for kind, name in (
            (DocumentKind.RESUME, "resume.pdf"),
            (DocumentKind.COVER_LETTER, "cover_letter.pdf"),
            (DocumentKind.COMBINED, "combined.pdf"),
        ):
            s.add(
                Document(
                    job_id=1,
                    kind=kind,
                    path=str(tmp_path / f"job_1/{name}"),
                    sha256="deadbeef",
                    parse_check_passed=True,
                    parse_report={"cover_letter_text": "Dear Hiring Team, please consider me."},
                )
            )

        s.add(Score(job_id=1, profile_version=1, rubric_version=1, final=95.0))
        s.add(
            AnswerBank(
                question_pattern="Do you have full working rights in Australia?",
                match_type=MatchType.FUZZY,
                answer_value="Yes",
                answer_type=AnswerType.BOOLEAN,
            )
        )
        s.commit()
        yield s


@pytest.fixture(autouse=True)
def _neutral_clock(monkeypatch):
    """Take the time-of-day window out of play for this module.

    These tests assert the flow's *sequence*, and they run at whatever hour CI
    happens to start. The window itself — business hours, weekends, the Adelaide
    DST transition — is covered properly in tests/test_guardrails.py; leaving it
    live here would make the whole file pass or fail depending on the clock.
    """
    monkeypatch.setattr(settings, "apply_window_start", "00:00")
    monkeypatch.setattr(settings, "apply_window_end", "23:59")


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(settings, "allow_live_submit", True)


def simple_steps() -> list[list[FormField]]:
    return [
        [
            FormField(identifier="name", label="Full name"),
            FormField(
                identifier="rights",
                label="Do you have full working rights in Australia?",
                choices=["Yes", "No"],
            ),
            FormField(identifier="resume", label="Resume", kind="file"),
        ]
    ]


def run(session, adapter, **kwargs):
    job = session.get(Job, 1)
    kwargs.setdefault("is_authenticated", lambda platform: True)
    return flow.run_apply(FakePage(), session, job, adapter=adapter, **kwargs)


# --------------------------------------------------------------------------
# The safety assertions
# --------------------------------------------------------------------------


def test_default_settings_never_submit_even_on_a_perfect_application(session):
    """The whole engine runs, fills the form, and still does not send."""
    adapter = FakeAdapter(steps=simple_steps())
    result = run(session, adapter)

    assert adapter.submitted is False, "submitted with ALLOW_LIVE_SUBMIT false"
    assert result.ok is False
    assert result.outcome is ApplyOutcome.BLOCKED
    assert "allow_live_submit" in (result.failure_reason or "")


def test_enabling_the_switch_submits_the_same_application(session, live):
    adapter = FakeAdapter(steps=simple_steps())
    result = run(session, adapter)

    assert adapter.submitted is True, result.failure_reason
    assert result.outcome is ApplyOutcome.SUBMITTED
    assert session.get(Job, 1).status == JobStatus.APPLIED


def test_an_abstention_aborts_before_anything_is_submitted(session, live):
    steps = [
        [
            FormField(identifier="name", label="Full name"),
            FormField(identifier="forklift", label="Do you hold a forklift licence?"),
        ]
    ]
    adapter = FakeAdapter(steps=steps)
    result = run(session, adapter)

    assert adapter.submitted is False
    assert result.outcome is ApplyOutcome.ABSTAINED
    assert result.needs_answer is not None
    assert session.get(Job, 1).status == JobStatus.NEEDS_ANSWER


def test_the_browser_is_not_held_open_waiting_for_a_human(session, live):
    """Parking returns immediately; the integrations layer asks over Telegram."""
    adapter = FakeAdapter(
        steps=[[FormField(identifier="q", label="An unanswerable question?")]]
    )
    result = run(session, adapter)
    assert result.outcome is ApplyOutcome.ABSTAINED
    # No advance, no submit — it stopped at the abstention.
    assert adapter.advanced == 0
    assert adapter.submitted is False


def test_an_attachment_readback_mismatch_aborts(session, live):
    """LinkedIn silently reuses stale uploads; this is the only thing that catches it."""
    adapter = FakeAdapter(steps=simple_steps(), readback=["old_resume_2019.pdf"])
    result = run(session, adapter)

    assert adapter.submitted is False
    assert result.outcome is ApplyOutcome.FAILED
    assert "read-back mismatch" in (result.failure_reason or "")


def test_a_form_reporting_no_attachment_at_all_aborts(session, live):
    adapter = FakeAdapter(steps=simple_steps(), readback=[])
    result = run(session, adapter)
    assert adapter.submitted is False
    assert "no attached filename" in (result.failure_reason or "")


def test_an_ungated_document_is_never_attached(session, live):
    for document in session.exec(__import__("sqlmodel").select(Document)).all():
        document.parse_check_passed = False
        session.add(document)
    session.commit()

    adapter = FakeAdapter(steps=simple_steps())
    result = run(session, adapter)

    assert adapter.attached == []
    assert adapter.submitted is False
    assert result.ok is False


def test_one_upload_slot_gets_the_combined_pdf(session, live):
    adapter = FakeAdapter(steps=simple_steps(), upload_slot_count=1)
    run(session, adapter)
    assert [d.kind for d in adapter.attached] == [DocumentKind.COMBINED]


def test_two_upload_slots_get_resume_and_cover_letter_separately(session, live):
    adapter = FakeAdapter(steps=simple_steps(), upload_slot_count=2)
    run(session, adapter)
    assert [d.kind for d in adapter.attached] == [
        DocumentKind.RESUME,
        DocumentKind.COVER_LETTER,
    ]


def test_a_missing_confirmation_state_is_a_failure_not_a_success(session, live):
    """A click that returned is not evidence anything was received."""
    adapter = FakeAdapter(steps=simple_steps(), confirm=False)
    result = run(session, adapter)

    assert adapter.submitted is True  # the click happened
    assert result.ok is False
    assert result.outcome is ApplyOutcome.FAILED
    assert "confirmation" in (result.failure_reason or "")
    assert session.get(Job, 1).status == JobStatus.FAILED


def test_a_submit_that_raises_is_recorded_not_swallowed(session, live):
    adapter = FakeAdapter(steps=simple_steps(), submit_raises=True)
    result = run(session, adapter)
    assert result.ok is False
    assert "submit failed" in (result.failure_reason or "")


def test_a_second_run_on_the_same_job_is_refused(session, live):
    adapter = FakeAdapter(steps=simple_steps())
    first = run(session, adapter)
    assert first.outcome is ApplyOutcome.SUBMITTED

    second_adapter = FakeAdapter(steps=simple_steps())
    second = run(session, second_adapter)

    assert second_adapter.submitted is False
    assert "already has application" in (second.failure_reason or "")
    applications = session.exec(__import__("sqlmodel").select(Application)).all()
    assert len(applications) == 1, "one application per job, ever"


def test_a_repeated_step_aborts_rather_than_looping_forever(session, live):
    """A validation error silently blocking progress must not spin."""
    same = [FormField(identifier="name", label="Full name")]
    adapter = FakeAdapter(steps=[same, same, same])
    adapter.is_last_step = lambda page, fields: False  # type: ignore[method-assign]

    result = run(session, adapter)
    assert adapter.submitted is False
    assert "repeated" in (result.failure_reason or "")


def test_an_offsite_redirect_is_queued_for_manual_application(session, live):
    adapter = FakeAdapter(steps=simple_steps(), redirect=True)
    result = run(session, adapter)

    assert adapter.submitted is False
    job = session.get(Job, 1)
    assert job.status == JobStatus.MANUAL_QUEUE
    assert "manual" in (result.failure_reason or "")


def test_a_restriction_notice_trips_the_global_halt(session, live):
    adapter = FakeAdapter(steps=simple_steps(), restriction=True)
    with pytest.raises(flow.RestrictionDetected):
        run(session, adapter)
    assert settings.stop_file.exists(), "a restriction must stop everything"


def test_an_unauthenticated_session_blocks_submission(session, live):
    adapter = FakeAdapter(steps=simple_steps())
    result = run(session, adapter, is_authenticated=lambda platform: False)
    assert adapter.submitted is False
    assert result.outcome is ApplyOutcome.BLOCKED


# --------------------------------------------------------------------------
# Dry run
# --------------------------------------------------------------------------


def test_dry_run_stops_at_the_gate_and_reports_what_would_be_sent(session, live):
    adapter = FakeAdapter(steps=simple_steps())
    result = run(session, adapter, dry_run=True)

    assert adapter.submitted is False
    assert result.outcome is ApplyOutcome.DRY_RUN
    assert result.answers_given, "a dry run still reports the resolved answers"
    assert result.attachment_readback


def test_dry_run_does_not_mark_the_job_applied(session, live):
    adapter = FakeAdapter(steps=simple_steps())
    run(session, adapter, dry_run=True)
    assert session.get(Job, 1).status != JobStatus.APPLIED


# --------------------------------------------------------------------------
# Structural invariants
# --------------------------------------------------------------------------


def test_the_flow_has_exactly_one_guardrail_call_site():
    """Hard rule 6: every submit path calls check_can_submit. No bypass."""
    import pathlib

    source = pathlib.Path(flow.__file__).read_text(encoding="utf-8")
    assert source.count("check_can_submit(") == 1


def test_no_adapter_may_call_the_guardrails_or_submit_on_its_own(session):
    """Adapters supply selectors and step logic; the flow owns the sequence."""
    import pathlib

    apply_dir = pathlib.Path(flow.__file__).parent
    for module in ("seek.py", "linkedin.py"):
        path = apply_dir / module
        if not path.exists():
            continue
        source = path.read_text(encoding="utf-8")
        assert "check_can_submit" not in source, f"{module} must not gate submits itself"


def test_the_run_apply_signature_has_no_bypass():
    import inspect

    params = set(inspect.signature(flow.run_apply).parameters)
    for forbidden in ("force", "skip_guardrails", "bypass", "no_check"):
        assert forbidden not in params


def test_profile_facts_do_not_come_from_the_answer_bank(session):
    """A name is a fact, not a screening answer."""
    adapter = FakeAdapter(steps=simple_steps())
    run(session, adapter)
    assert adapter.filled["name"] == "Jordan Fitzgerald"


# =========================================================================
# The form-map cache, through the real apply path
# =========================================================================


class CountingMapper:
    """Stands in for the model and counts how often it was actually asked."""

    def __init__(self, answers: dict[str, dict]):
        self.answers = answers
        self.calls = 0
        self.asked: list[list[str]] = []

    def __call__(self, prompt, **kwargs):
        import re

        self.calls += 1
        asked = re.findall(r"id='([^']+)'", prompt)
        self.asked.append(asked)
        return {"fields": [self.answers[i] for i in asked if i in self.answers]}


@pytest.fixture
def mapping_on(monkeypatch):
    monkeypatch.setattr(settings, "apply_form_mapping_enabled", True)


# Labels the deterministic pass cannot place: no PROFILE_FIELD_HINTS substring
# matches "Best way to reach you", and the answer bank holds the working-rights
# question under different wording.
UNPLACEABLE = [
    FormField(identifier="reach", label="Best way to reach you"),
    FormField(identifier="elig", label="Are you legally able to work here?"),
]

MAPPINGS = {
    "reach": {
        "identifier": "reach",
        "source": "profile",
        "profile_path": "profile.email",
        "confident": True,
    },
    "elig": {
        "identifier": "elig",
        "source": "answer_bank",
        "question": "Do you have full working rights in Australia?",
        "confident": True,
    },
}


def test_a_field_the_deterministic_pass_cannot_place_abstains_without_mapping(session):
    """The behaviour being improved on: these fields park the job."""
    job = session.get(Job, 1)
    draft = flow.build_draft(session, job, platform="seek", fields=list(UNPLACEABLE))

    # Abstain.question is the normalised form — casefolded, trailing
    # punctuation dropped — which is what the answer bank compares on.
    assert {a.question for a in draft.abstentions} == {
        "best way to reach you",
        "are you legally able to work here",
    }


def test_form_mapping_rescues_those_fields(session, monkeypatch, mapping_on):
    """A field the system knows the answer to, but could not see that it knew."""
    mapper = CountingMapper(MAPPINGS)
    monkeypatch.setattr(flow.map_fields.__globals__["llm"], "complete_json", mapper)

    job = session.get(Job, 1)
    draft = flow.build_draft(session, job, platform="seek", fields=list(UNPLACEABLE))

    assert draft.abstentions == [], f"still abstaining: {draft.abstentions}"
    assert mapper.calls == 1

    # Mapped, then resolved through the normal sources — not invented.
    assert draft.answers["Best way to reach you"].value == "jordan@example.com"
    assert draft.answers["Are you legally able to work here?"].value == "Yes"


def test_the_second_application_to_the_same_form_makes_no_llm_call(
    session, monkeypatch, mapping_on
):
    """The point of the cache, proven through the real apply path.

    map_fields had no production caller, so the cache was provably correct and
    never saved anything. This is the assertion that it now does.
    """
    mapper = CountingMapper(MAPPINGS)
    monkeypatch.setattr(flow.map_fields.__globals__["llm"], "complete_json", mapper)
    job = session.get(Job, 1)

    first = flow.build_draft(session, job, platform="seek", fields=list(UNPLACEABLE))
    assert mapper.calls == 1
    assert first.abstentions == []

    # A second job, same employer platform, identical form shape.
    session.add(
        Job(
            id=2,
            source="seek",
            source_job_id="2",
            url="https://example.com/2",
            title="Developer II",
            company="Globex",
            location="Adelaide SA",
            dedupe_hash="h2",
            campaign_id=1,
            status=JobStatus.DOCUMENTS_READY,
        )
    )
    session.flush()

    second = flow.build_draft(
        session, session.get(Job, 2), platform="seek", fields=list(UNPLACEABLE)
    )

    assert mapper.calls == 1, (
        f"the second application re-paid for a known form: {mapper.calls} calls"
    )
    assert second.abstentions == []
    assert second.answers["Best way to reach you"].value == "jordan@example.com"


def test_mapping_never_invents_a_screening_answer(session, monkeypatch, mapping_on):
    """Hard rule 2 survives the mapping pass.

    The model may say a field is a screening question. It may not say what the
    answer is — that still has to come from the bank, and a question the bank
    does not hold still abstains and still parks the job.
    """
    mapper = CountingMapper(
        {
            "elig": {
                "identifier": "elig",
                "source": "answer_bank",
                "question": "Do you hold a current forklift licence?",
                "confident": True,
            }
        }
    )
    monkeypatch.setattr(flow.map_fields.__globals__["llm"], "complete_json", mapper)

    job = session.get(Job, 1)
    draft = flow.build_draft(
        session,
        job,
        platform="seek",
        fields=[FormField(identifier="elig", label="Are you legally able to work here?")],
    )

    assert mapper.calls == 1
    assert [a.question for a in draft.abstentions] == ["are you legally able to work here"]
    assert draft.answers == {}


def test_an_unconfident_mapping_still_parks_the_job(session, monkeypatch, mapping_on):
    mapper = CountingMapper(
        {
            "reach": {
                "identifier": "reach",
                "source": "profile",
                "profile_path": "profile.email",
                "confident": False,
            }
        }
    )
    monkeypatch.setattr(flow.map_fields.__globals__["llm"], "complete_json", mapper)

    job = session.get(Job, 1)
    draft = flow.build_draft(
        session, job, platform="seek", fields=[FormField(identifier="reach", label="Best way to reach you")]
    )

    assert draft.abstentions, "an unconfident mapping must not resolve a field"
    assert draft.answers == {}


def test_mapping_is_skipped_entirely_when_disabled(session, monkeypatch):
    """The setting is the off switch for the whole second pass."""
    mapper = CountingMapper(MAPPINGS)
    monkeypatch.setattr(flow.map_fields.__globals__["llm"], "complete_json", mapper)
    monkeypatch.setattr(settings, "apply_form_mapping_enabled", False)

    job = session.get(Job, 1)
    draft = flow.build_draft(session, job, platform="seek", fields=list(UNPLACEABLE))

    assert mapper.calls == 0
    assert len(draft.abstentions) == 2


# --------------------------------------------------------------------------
# Unresolvable elements: park and alert, never guess
# --------------------------------------------------------------------------


def test_an_unresolvable_element_parks_the_job_for_a_human(session):
    """Acceptance: killing every strategy parks the application.

    MANUAL_QUEUE rather than FAILED on purpose. Nothing is wrong with this job;
    retrying it changes nothing until either the site changes back or someone
    updates the strategies, and a person can finish it by hand meanwhile.
    """
    from backend.siteknowledge import ElementNotFound

    adapter = FakeAdapter(steps=simple_steps())

    def explode(page, job):
        raise ElementNotFound("seek", "apply_button", ["[data-automation='x']", ".y"])

    adapter.open = explode

    result = run(session, adapter)

    assert not result.ok
    assert session.get(Job, 1).status is JobStatus.MANUAL_QUEUE
    assert "apply_button" in result.failure_reason
    assert not adapter.submitted


def test_an_unresolvable_element_alerts_with_what_it_tried(session):
    """Acceptance: parks AND alerts. Silence here is the failure mode."""
    from backend.siteknowledge import ElementNotFound

    alerts: list[tuple] = []
    flow.on_element_unresolvable = lambda *args: alerts.append(args)

    adapter = FakeAdapter(steps=simple_steps())

    def explode(page, job):
        raise ElementNotFound("seek", "submit_button", ["a", "b", "c"])

    adapter.open = explode
    try:
        run(session, adapter)
    finally:
        flow.on_element_unresolvable = None

    assert len(alerts) == 1
    platform, key, tried, job_id = alerts[0]
    assert (platform, key, job_id) == ("seek", "submit_button", 1)
    assert tried == ["a", "b", "c"], "the alert must say what was tried"


def test_an_unresolvable_element_mid_flow_still_parks(session):
    """It can surface from any adapter call, not just open()."""
    from backend.siteknowledge import ElementNotFound

    adapter = FakeAdapter(steps=simple_steps())

    def explode(page):
        raise ElementNotFound("seek", "attachment_readback", ["x"])

    adapter.read_back_attachments = explode

    result = run(session, adapter)

    assert not result.ok
    assert session.get(Job, 1).status is JobStatus.MANUAL_QUEUE
    assert not adapter.submitted, "never submit when the read-back could not be read"


def test_a_failing_alert_hook_does_not_mask_the_parking(session):
    """The alert is best-effort; the park is not."""
    from backend.siteknowledge import ElementNotFound

    flow.on_element_unresolvable = lambda *a: 1 / 0

    adapter = FakeAdapter(steps=simple_steps())

    def explode(page, job):
        raise ElementNotFound("seek", "apply_button", [])

    adapter.open = explode
    try:
        result = run(session, adapter)
    finally:
        flow.on_element_unresolvable = None

    assert not result.ok
    assert session.get(Job, 1).status is JobStatus.MANUAL_QUEUE


def test_no_broad_handler_around_an_adapter_call_swallows_element_not_found():
    """Structural guard: this bug was introduced once and is easy to reintroduce.

    Every ``try:`` whose body calls the adapter must re-raise ElementNotFound
    before its broad ``except Exception``. Without that the wrapper never sees
    it, the job is reported as an ordinary failure, and the retry loop keeps
    opening browser sessions against a site that has moved.

    Checked structurally rather than by exercising all eleven adapter methods:
    the next one added would not be covered by a behavioural test nobody
    remembered to extend.
    """
    import ast
    import pathlib

    source = pathlib.Path("backend/apply/flow.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    def calls_adapter(node: ast.AST) -> bool:
        return any(
            isinstance(child, ast.Attribute)
            and isinstance(child.value, ast.Name)
            and child.value.id == "adapter"
            for child in ast.walk(node)
        )

    def reraises_element_not_found(handlers: list[ast.ExceptHandler]) -> bool:
        return any(
            isinstance(h.type, ast.Name) and h.type.id == "ElementNotFound"
            for h in handlers
        )

    # Deliberate exceptions, with the reason. A predicate whose broad handler
    # returns a safe default is not swallowing anything — it is answering.
    exempt_functions = {
        "_restricted": (
            "answers 'is there a restriction notice?'. A detector that cannot "
            "find its element means no notice is showing, which is the normal "
            "case on every healthy page. Raising here would park every job."
        ),
    }
    exempt_lines = {
        node.lineno
        for func in ast.walk(tree)
        if isinstance(func, ast.FunctionDef) and func.name in exempt_functions
        for node in ast.walk(func)
        if isinstance(node, ast.Try)
    }

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        if node.lineno in exempt_lines:
            continue
        if not any(calls_adapter(stmt) for stmt in node.body):
            continue
        broad = any(
            h.type is None or (isinstance(h.type, ast.Name) and h.type.id == "Exception")
            for h in node.handlers
        )
        if broad and not reraises_element_not_found(node.handlers):
            offenders.append(node.lineno)

    assert not offenders, (
        f"flow.py lines {offenders}: a broad handler around an adapter call that "
        "does not re-raise ElementNotFound first"
    )
