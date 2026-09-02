"""External ATS: site knowledge, trust graduation, and map_fields reachability.

Most Seek applications forward into an employer's own ATS, so this path is
where most of the real volume goes — not a side feature.

The last section exists because ``map_fields`` has been dead code twice. An
import and a call site are not proof: both were present the second time. What
proves it is a full ``run_apply`` that reaches it, with a form the answer bank
cannot resolve, which is what these do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlmodel import Session, SQLModel, create_engine

from backend.ats.adapters import build_ats_appliers
from backend.ats.formmaps import TRUST_THRESHOLD
from backend.models import (
    AnswerBank,
    AnswerType,
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

# ==========================================================================
# Site knowledge on every platform
# ==========================================================================


def test_no_selectors_remain_in_the_ats_adapter_source():
    """Same rule as the primary boards: a redesign is a JSON edit."""
    import pathlib
    import re

    source = pathlib.Path("backend/ats/adapters.py").read_text(encoding="utf-8")
    code = "\n".join(
        line.split("#")[0]
        for line in source.splitlines()
        if not line.strip().startswith(("*", '"""', "#"))
    )

    # The generic read-back shapes are deliberately shared and are not platform
    # knowledge; everything else must be gone.
    code = code.split("_READBACK_SELECTORS")[0] + code.split("def _readback_texts")[-1]

    for shape in (r"a\.ja-apply", r"#apply_button", r"a\.postings-btn", r"adventureButton"):
        assert not re.search(shape, code), f"{shape} is still a literal in adapters.py"


def test_every_priority_platform_has_an_adapter():
    """The brief's list, all of it."""
    platforms = {applier.platform for applier in build_ats_appliers()}
    expected = {
        "jobadder",
        "pageup",
        "smartrecruiters",
        "greenhouse",
        "lever",
        "workday",
        "google_forms",
        "typeform",
        "jotform",
    }
    assert expected <= platforms, expected - platforms


def test_each_platform_gets_the_same_multi_strategy_treatment():
    """Phase 2's resolution applies here too, not just to Seek and LinkedIn."""
    for applier in build_ats_appliers():
        for key in ("submit_button", "confirmation"):
            element = applier.knowledge.elements[key]
            assert len(element.strategies) >= 2, f"{applier.platform}/{key}"
            types = {strategy.type for strategy in element.strategies}
            assert types - {"css"}, (
                f"{applier.platform}/{key} is CSS-only and dies whole on a redesign"
            )


def test_google_forms_leans_on_role_because_it_has_no_ids():
    """Google Forms renders generated divs with role attributes and nothing else.

    A CSS-first strategy list there is a list of selectors that never match.
    """
    knowledge = next(
        a.knowledge for a in build_ats_appliers() if a.platform == "google_forms"
    )
    submit = knowledge.elements["submit_button"]
    assert submit.ordered()[0].type in {"role", "testid"}


def test_a_platform_without_knowledge_gets_no_adapter():
    """An applier with no strategies fails on its first resolve, not later."""
    from backend.ats.adapters import _has_knowledge

    assert _has_knowledge("greenhouse")
    assert not _has_knowledge("no_such_ats")


# ==========================================================================
# Trust graduation: draft and ask, then graduate
# ==========================================================================


def test_an_ungraduated_form_map_blocks_the_submit():
    """The half of trust graduation that was missing.

    record_outcome already counted successes and set `trusted`; nothing read it,
    so a form the model mapped thirty seconds ago was submitted exactly like one
    proven three times.
    """
    from backend.apply.guardrails import check_can_submit

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        job = Job(
            id=1,
            source="seek",
            source_job_id="1",
            url="https://boards.greenhouse.io/acme/jobs/1",
            title="Developer",
            company="Acme",
            location="Adelaide SA",
            dedupe_hash="h",
            status=JobStatus.DOCUMENTS_READY,
        )
        session.add(job)
        session.flush()

        draft = _Draft(platform="greenhouse", form_map_trusted=False)
        verdict = check_can_submit(session, job, draft, is_authenticated=lambda p: True)

        names = [c.name for c in verdict.checks if not c.passed]
        assert "form_map_trusted" in names
        assert not verdict.allowed


def test_a_graduated_form_map_does_not_block():
    from backend.apply.guardrails import check_can_submit

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        job = Job(
            id=1,
            source="seek",
            source_job_id="1",
            url="https://boards.greenhouse.io/acme/jobs/1",
            title="Developer",
            company="Acme",
            location="Adelaide SA",
            dedupe_hash="h",
            status=JobStatus.DOCUMENTS_READY,
        )
        session.add(job)
        session.flush()

        verdict = check_can_submit(
            session,
            job,
            _Draft(platform="greenhouse", form_map_trusted=True),
            is_authenticated=lambda p: True,
        )
        assert "form_map_trusted" not in [c.name for c in verdict.checks if not c.passed]


def test_a_known_platform_is_not_gated_on_form_trust():
    """Seek's fields are not learned by a model, so there is nothing to graduate."""
    draft = _Draft(platform="seek")
    assert draft.form_map_trusted is True, "the default must not gate real adapters"


def test_three_clean_applications_graduate_a_shape():
    """The brief's rule, on the fingerprint rather than the company."""
    from backend.ats.formmaps import FormMapData, record_outcome, save_map

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        save_map(session, FormMapData(fingerprint="abc", tier="platform", platform="greenhouse"))
        session.flush()

        for index in range(TRUST_THRESHOLD - 1):
            assert not record_outcome(session, "abc", success=True), index
        assert record_outcome(session, "abc", success=True), "the third graduates it"


def test_the_approval_request_only_fires_when_trust_is_the_only_blocker():
    """Otherwise it asks a question whose answer unblocks nothing."""
    from backend.apply.flow import _blocked_only_on_form_trust

    class Check:
        def __init__(self, name, passed):
            self.name, self.passed = name, passed

    class Verdict:
        def __init__(self, checks):
            self.checks = checks

    only_trust = Verdict([Check("form_map_trusted", False), Check("inside_window", True)])
    also_switch = Verdict(
        [Check("form_map_trusted", False), Check("allow_live_submit", False)]
    )

    assert _blocked_only_on_form_trust(only_trust)
    assert not _blocked_only_on_form_trust(also_switch)


def test_the_approval_message_carries_the_shape_not_the_company():
    """Two employers on the same template share one graduation."""
    from backend.integrations import telegram

    sent: list[str] = []
    telegram.send_message = lambda text, priority=None: sent.append(text) or True  # type: ignore[assignment]

    telegram.request_form_approval(
        7, fingerprint="abcdef123456", platform="greenhouse", answers={"Q": "A"}
    )

    assert sent, "the user must be asked"
    assert "abcdef12" in sent[0]
    assert "3 approvals" in sent[0]


# ==========================================================================
# map_fields, end to end. It has been dead code twice.
# ==========================================================================


@dataclass
class _Draft:
    """Minimal stand-in for ApplicationDraft where only guardrails are exercised."""

    platform: str = "greenhouse"
    campaign: Any = None
    documents: list = field(default_factory=list)
    answers: dict = field(default_factory=dict)
    abstentions: list = field(default_factory=list)
    fields: list = field(default_factory=list)
    form_fingerprint: str | None = None
    form_map_trusted: bool = True
    score: float | None = 95.0
    attachment_readback: str | None = None
    screenshot_pre: str | None = None
    cover_letter_text: str = ""
    attachment_intent: dict = field(default_factory=dict)

    @property
    def answers_given(self):
        return {}


class _Locator:
    def __init__(self, present=True):
        self._present = present

    @property
    def first(self):
        return self

    def is_visible(self, timeout=0):
        return self._present

    def count(self):
        return 1 if self._present else 0

    def click(self):
        return None

    def fill(self, value):
        return None

    def check(self):
        return None

    def select_option(self, **kwargs):
        return None

    def set_input_files(self, path):
        return None

    def all_inner_texts(self):
        return ["combined.pdf"]


class _Page:
    url = "https://boards.greenhouse.io/acme/jobs/1"

    def locator(self, selector):
        return _Locator()

    def goto(self, url, **kwargs):
        return None

    def wait_for_load_state(self, *a, **k):
        return None

    def screenshot(self, **kwargs):
        return None


@pytest.fixture
def loaded(tmp_path, monkeypatch):
    from backend.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "apply_form_mapping_enabled", True)

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Profile(version=1, identity={"name": "A", "email": "a@example.com"}))
        session.add(
            Campaign(
                id=1,
                name="c",
                active=True,
                search_terms=["dev"],
                locations=["Adelaide SA"],
                score_floor=60.0,
                score_auto_apply=80.0,
                gray_zone_action=GrayZoneAction.QUEUE,
                daily_caps={"default": 10},
            )
        )
        job = Job(
            id=1,
            source="seek",
            source_job_id="1",
            url="https://boards.greenhouse.io/acme/jobs/1",
            title="Developer",
            company="Acme",
            location="Adelaide SA",
            dedupe_hash="h",
            campaign_id=1,
            status=JobStatus.DOCUMENTS_READY,
        )
        session.add(job)
        session.flush()  # the Document and Score below carry a FK to job.id
        session.add(
            Document(
                job_id=1,
                kind=DocumentKind.COMBINED,
                path=str(tmp_path / "combined.pdf"),
                sha256="d",
                parse_check_passed=True,
                parse_report={"cover_letter_text": "Dear team"},
            )
        )
        session.add(Score(job_id=1, profile_version=1, rubric_version=1, final=95.0))
        # Deliberately NOT an answer for the referral question below: an answer
        # bank that already knows everything never reaches the form-map path.
        session.add(
            AnswerBank(
                question_pattern="Do you have full working rights in Australia?",
                match_type=MatchType.FUZZY,
                answer_value="Yes",
                answer_type=AnswerType.BOOLEAN,
            )
        )
        session.commit()
        yield session


def test_map_fields_is_reached_by_a_real_application_run(loaded, monkeypatch):
    """The one that matters. An import and a call site are not proof.

    Both were present the second time this was dead. What proves it is a full
    run_apply that arrives at map_fields with a field the answer bank cannot
    resolve — which is the only condition under which it is supposed to run.
    """
    from backend.apply import flow
    from backend.apply.draft import FormField
    from backend.ats import generic

    called: list[list[str]] = []
    real = generic.map_fields

    def spy(fields, **kwargs):
        called.append([f.identifier for f in fields])
        return real(fields, **kwargs)

    monkeypatch.setattr(flow, "map_fields", spy)
    # No API key in tests, so the model call underneath returns nothing usable.
    # That is fine: this asserts the path is reached, not what the model says.
    monkeypatch.setattr(generic, "_map_via_llm", lambda fields, platform=None: [])

    adapter = _Adapter(
        steps=[
            [
                FormField(
                    identifier="referral",
                    label="How did you hear about this role?",
                    choices=["Seek", "LinkedIn", "Referral"],
                )
            ]
        ]
    )

    flow.run_apply(
        _Page(), loaded, loaded.get(Job, 1), adapter=adapter, is_authenticated=lambda p: True
    )

    assert called, "map_fields was never reached by a real run — it is dead again"
    assert "referral" in called[0]


def test_an_untrusted_map_on_disk_reaches_the_draft(loaded, monkeypatch):
    """The verdict has to travel, not just be computed.

    load_map already returned `trusted` and generic.py threw it away, which is
    precisely how trust graduation ended up counting successes nobody read.
    This asserts the value that comes back from disk is the value the guardrail
    later sees.
    """
    from backend.apply import flow
    from backend.apply.draft import FormField
    from backend.ats import generic
    from backend.ats.formmaps import FieldMapping, FormMapData, save_map

    fields = [
        FormField(
            identifier="referral",
            label="How did you hear about this role?",
            choices=["Seek", "LinkedIn"],
        )
    ]

    # A map that exists and resolves the field, but has never graduated.
    from backend.ats.formmaps import fingerprint_fields

    save_map(
        loaded,
        FormMapData(
            fingerprint=fingerprint_fields(fields),
            tier="platform",
            platform="greenhouse",
            fields=[
                FieldMapping(
                    identifier="referral",
                    label="How did you hear about this role?",
                    source="answer_bank",
                    question="How did you hear about this role?",
                )
            ],
        ),
        trusted=False,
    )
    loaded.flush()
    monkeypatch.setattr(generic, "_map_via_llm", lambda fields, platform=None: [])

    draft = flow.build_draft(
        loaded, loaded.get(Job, 1), platform="greenhouse", fields=fields
    )

    assert draft.form_map_trusted is False, (
        "an ungraduated map must arrive at the guardrail as untrusted"
    )


def test_a_trusted_map_on_disk_reaches_the_draft_as_trusted(loaded, monkeypatch):
    """The mirror, so the previous test cannot pass by always returning False."""
    from backend.apply import flow
    from backend.apply.draft import FormField
    from backend.ats import generic
    from backend.ats.formmaps import (
        FieldMapping,
        FormMapData,
        fingerprint_fields,
        save_map,
    )

    fields = [
        FormField(
            identifier="referral",
            label="How did you hear about this role?",
            choices=["Seek", "LinkedIn"],
        )
    ]
    save_map(
        loaded,
        FormMapData(
            fingerprint=fingerprint_fields(fields),
            tier="platform",
            platform="greenhouse",
            fields=[
                FieldMapping(
                    identifier="referral",
                    label="How did you hear about this role?",
                    source="answer_bank",
                    question="How did you hear about this role?",
                )
            ],
        ),
        trusted=True,
    )
    loaded.flush()
    monkeypatch.setattr(generic, "_map_via_llm", lambda fields, platform=None: [])

    draft = flow.build_draft(
        loaded, loaded.get(Job, 1), platform="greenhouse", fields=fields
    )
    assert draft.form_map_trusted is True


def test_map_fields_is_not_called_when_the_bank_already_knows(loaded, monkeypatch):
    """The cache exists to avoid the model, so a resolved form must not reach it."""
    from backend.apply import flow
    from backend.apply.draft import FormField

    called: list[Any] = []
    monkeypatch.setattr(flow, "map_fields", lambda fields, **kw: called.append(fields) or [])

    adapter = _Adapter(
        steps=[
            [
                FormField(
                    identifier="rights",
                    label="Do you have full working rights in Australia?",
                    choices=["Yes", "No"],
                )
            ]
        ]
    )

    flow.run_apply(
        _Page(), loaded, loaded.get(Job, 1), adapter=adapter, is_authenticated=lambda p: True
    )

    assert not called, "an answerable form must not cost an LLM call"


@dataclass
class _Adapter:
    steps: list = field(default_factory=list)
    platform: str = "greenhouse"
    submitted: bool = False

    def can_handle(self, job):
        return True

    def open(self, page, job):
        return None

    def detect_redirect(self, page):
        return False

    def detect_restriction(self, page):
        return False

    def enumerate_fields(self, page, step):
        return self.steps[step] if step < len(self.steps) else []

    def fill_field(self, page, field_, value):
        return None

    def upload_slots(self, fields):
        return 1

    def attach(self, page, documents):
        return None

    def read_back_attachments(self, page):
        return ["combined.pdf"]

    def is_last_step(self, page, fields):
        return True

    def advance(self, page):
        return None

    def submit(self, page):
        self.submitted = True

    def confirmed(self, page):
        return True
