"""External ATS: detection, the form-map cache, generic mapping, queueing.

The form-map tests carry the most weight. A cache that stored values, or that
fingerprinted on the URL, would silently replay one employer's answers at
another — so those properties are asserted directly rather than assumed.
"""

from __future__ import annotations

import json
import re

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from backend.apply.draft import FormField
from backend.ats import generic
from backend.ats.adapters import SELECTORS, GenericAtsApplier, build_ats_appliers
from backend.ats.detect import ATS_REGISTRY, detect, detect_from_html, detect_from_url
from backend.ats.formmaps import (
    TRUST_THRESHOLD,
    FieldMapping,
    FormMapData,
    fingerprint_fields,
    load_map,
    merge_maps,
    record_outcome,
    relearn_targets,
    save_map,
)
from backend.ats.queueing import (
    MANUAL_QUEUE_PREMIUM,
    decide_queueing,
    manual_queue_floor,
)
from backend.config import settings
from backend.models import Campaign, FormMap, GrayZoneAction


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def fields(*specs: tuple[str, str, str]) -> list[FormField]:
    return [
        FormField(identifier=identifier, label=label, kind=kind)
        for identifier, label, kind in specs
    ]


STANDARD = (
    ("first_name", "First name", "text"),
    ("email", "Email address", "text"),
    ("rights", "Do you have full working rights in Australia?", "radio"),
    ("resume", "Resume", "file"),
)


# =========================================================================
# Detection
# =========================================================================


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://acme.jobadder.com/careers/1234", "jobadder"),
        ("https://dc2.pageuppeople.com/apply/123/caw/en/job/5678", "pageup"),
        ("https://jobs.smartrecruiters.com/Acme/74400", "smartrecruiters"),
        ("https://boards.greenhouse.io/acme/jobs/4001", "greenhouse"),
        ("https://jobs.lever.co/acme/abc-123", "lever"),
        ("https://acme.wd3.myworkdayjobs.com/en-US/careers/job/x", "workday"),
        ("https://docs.google.com/forms/d/e/1FAIpQ/viewform", "google_forms"),
        ("https://acme.typeform.com/to/abc", "typeform"),
        ("https://form.jotform.com/240123456789", "jotform"),
    ],
)
def test_url_detection(url, expected):
    assert detect_from_url(url).key == expected


def test_an_unknown_url_is_reported_as_unknown_not_guessed():
    assert detect_from_url("https://careers.acme.com.au/apply/123").key == "unknown"


def test_a_white_labelled_deployment_is_found_in_the_html():
    """The common Australian case: PageUp on the employer's own domain."""
    html = "<html><body><div id='pageuppeople-app'>Apply</div></body></html>"
    result = detect("https://careers.acme.com.au/job/1", html)
    assert result.key == "pageup"
    assert result.confidence == "html"


def test_an_embedded_form_builder_is_the_platform():
    html = '<iframe src="https://docs.google.com/forms/d/e/1FAI/viewform"></iframe>'
    result = detect_from_html(html)
    assert result.key == "google_forms"
    assert result.iframe_src


def test_australian_priority_order():
    """JobAdder and PageUp lead; Workday is last because of per-company accounts."""
    ordered = [p.key for p in sorted(ATS_REGISTRY, key=lambda p: p.priority)]
    assert ordered[0] == "jobadder"
    assert ordered[1] == "pageup"
    assert ordered.index("smartrecruiters") < ordered.index("workday")
    assert ordered.index("greenhouse") < ordered.index("workday")
    assert ordered[-1] == "workday"


def test_workday_is_flagged_as_needing_an_account():
    workday = next(p for p in ATS_REGISTRY if p.key == "workday")
    assert workday.requires_account is True


# =========================================================================
# Fingerprinting
# =========================================================================


def test_the_same_form_fingerprints_identically_regardless_of_field_order():
    forward = fingerprint_fields(fields(*STANDARD))
    backward = fingerprint_fields(fields(*reversed(STANDARD)))
    assert forward == backward


def test_the_fingerprint_does_not_depend_on_the_company_or_url():
    """Two employers on one ATS share a map, which is the whole point."""
    acme = fingerprint_fields(fields(*STANDARD))
    globex = fingerprint_fields(fields(*STANDARD))
    assert acme == globex


def test_a_different_form_fingerprints_differently():
    other = fingerprint_fields(
        fields(("first_name", "First name", "text"), ("wwcc", "WWCC number", "text"))
    )
    assert other != fingerprint_fields(fields(*STANDARD))


def test_label_whitespace_and_case_do_not_change_the_fingerprint():
    a = fingerprint_fields(fields(("q", "Do you have  full working rights?", "radio")))
    b = fingerprint_fields(fields(("q", "do you have full working rights", "radio")))
    assert a == b


# =========================================================================
# The cache
# =========================================================================


def make_map(fingerprint: str, tier: str = "company", **kwargs) -> FormMapData:
    return FormMapData(
        fingerprint=fingerprint,
        tier=tier,
        platform=kwargs.pop("platform", "greenhouse"),
        fields=kwargs.pop(
            "fields",
            [
                FieldMapping("email", "Email address", "text", source="profile"),
                FieldMapping(
                    "rights",
                    "Do you have full working rights in Australia?",
                    "radio",
                    source="answer_bank",
                    question="Do you have full working rights in Australia?",
                ),
            ],
        ),
    )


def test_a_saved_map_round_trips(session, tmp_path):
    fingerprint = fingerprint_fields(fields(*STANDARD))
    path = save_map(session, make_map(fingerprint))
    session.commit()

    assert path.exists()
    loaded, trusted = load_map(session, fingerprint, platform="greenhouse")
    assert loaded is not None
    assert trusted is False
    assert {f.identifier for f in loaded.fields} == {"email", "rights"}


def test_a_map_is_json_a_human_can_fix(session, tmp_path):
    """Files on disk specifically so a bad mapping can be hand-corrected."""
    fingerprint = "abc123"
    path = save_map(session, make_map(fingerprint))
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["fingerprint"] == fingerprint
    assert isinstance(payload["fields"], list)
    assert payload["fields"][0]["label"] == "Email address"


def test_a_map_records_where_never_what(session):
    """No answer value may ever be persisted into a form map."""
    fingerprint = "wherenotwhat"
    path = save_map(session, make_map(fingerprint))
    payload = json.loads(path.read_text(encoding="utf-8"))

    serialised = json.dumps(payload).casefold()
    for forbidden in ("answer_value", '"value"', "yes", "jordan"):
        assert forbidden not in serialised, f"a form map contained {forbidden!r}"

    for entry in payload["fields"]:
        assert set(entry) <= {
            "identifier",
            "label",
            "kind",
            "source",
            "question",
            # A pointer at a profile field ("profile.email"), not its value —
            # the same class of thing as `source` and `question`, and resolved
            # against the profile at fill time. Without it a cached map knows a
            # field wants some profile value but not which, which is not a
            # usable mapping.
            "profile_path",
            "required",
            "step",
            "selector",
        }


def test_maps_store_semantic_identity_not_css_selectors(session):
    path = save_map(session, make_map("semantic"))
    payload = json.loads(path.read_text(encoding="utf-8"))

    for entry in payload["fields"]:
        assert entry["label"], "every field records the label a human reads"
        # The selector is the LAST resort and is unset unless needed.
        assert entry.get("selector") in (None, "")


def test_a_corrupt_map_file_is_ignored_loudly_not_fatal(session, tmp_path):
    fingerprint = "corrupt"
    path = save_map(session, make_map(fingerprint))
    path.write_text("{ not json", encoding="utf-8")

    loaded, _ = load_map(session, fingerprint, platform="greenhouse")
    assert loaded is None  # re-learned rather than crashing


# =========================================================================
# Two tiers
# =========================================================================


def test_company_overrides_win_field_by_field():
    platform_map = make_map(
        "fp",
        tier="platform",
        fields=[
            FieldMapping("email", "Email", "text", source="profile"),
            FieldMapping("q1", "Question one", "text", source="answer_bank", question="one"),
        ],
    )
    company_map = make_map(
        "fp",
        fields=[FieldMapping("q1", "Question one", "text", source="answer_bank", question="ONE")],
    )

    merged = merge_maps(platform_map, company_map)
    by_id = merged.by_identifier()

    assert by_id["q1"].question == "ONE", "company override wins"
    assert by_id["email"].source == "profile", "untouched platform fields survive"


def test_an_unresolved_override_does_not_blank_a_resolved_platform_field():
    platform_map = make_map(
        "fp", tier="platform", fields=[FieldMapping("q", "Q", "text", source="profile")]
    )
    company_map = make_map("fp", fields=[FieldMapping("q", "Q", "text", source="unknown")])

    merged = merge_maps(platform_map, company_map)
    assert merged.by_identifier()["q"].source == "profile"


def test_merging_with_nothing_returns_the_other_side():
    only = make_map("fp")
    assert merge_maps(None, only) is only
    assert merge_maps(only, None) is only


# =========================================================================
# Partial relearn
# =========================================================================


def test_only_unknown_fields_are_relearned():
    """Re-learning a whole form because one field appeared costs a full call."""
    existing = make_map(
        "fp",
        fields=[
            FieldMapping("email", "Email address", "text", source="profile"),
            FieldMapping("rights", "Working rights?", "radio", source="answer_bank"),
        ],
    )
    incoming = fields(
        ("email", "Email address", "text"),
        ("rights", "Working rights?", "radio"),
        ("wwcc", "WWCC number", "text"),
    )

    targets = relearn_targets(existing, incoming)
    assert [f.identifier for f in targets] == ["wwcc"]


def test_with_no_existing_map_everything_is_relearned():
    incoming = fields(*STANDARD)
    assert len(relearn_targets(None, incoming)) == len(incoming)


# =========================================================================
# Trust graduation
# =========================================================================


def test_a_map_becomes_trusted_after_three_clean_successes(session):
    save_map(session, make_map("trust"))
    session.commit()

    assert record_outcome(session, "trust", success=True) is False
    assert record_outcome(session, "trust", success=True) is False
    assert record_outcome(session, "trust", success=True) is True

    row = session.exec(select(FormMap).where(FormMap.fingerprint == "trust")).one()
    assert row.trusted is True
    assert row.success_count == TRUST_THRESHOLD


def test_a_failure_resets_the_streak_rather_than_decrementing(session):
    """Three successes must be CONSECUTIVE to mean the mapping generalised."""
    save_map(session, make_map("streak"))
    session.commit()

    record_outcome(session, "streak", success=True)
    record_outcome(session, "streak", success=True)
    record_outcome(session, "streak", success=False)

    assert record_outcome(session, "streak", success=True) is False
    row = session.exec(select(FormMap).where(FormMap.fingerprint == "streak")).one()
    assert row.trusted is False
    assert row.success_count == 1


def test_a_trusted_map_that_fails_loses_its_trust(session):
    save_map(session, make_map("revoke"))
    session.commit()
    for _ in range(TRUST_THRESHOLD):
        record_outcome(session, "revoke", success=True)

    record_outcome(session, "revoke", success=False)

    row = session.exec(select(FormMap).where(FormMap.fingerprint == "revoke")).one()
    assert row.trusted is False


def test_a_new_map_is_not_trusted(session):
    save_map(session, make_map("fresh"))
    session.commit()
    _, trusted = load_map(session, "fresh", platform="greenhouse")
    assert trusted is False


# =========================================================================
# Generic filling
# =========================================================================


SNAPSHOT = {
    "role": "WebArea",
    "name": "Apply",
    "children": [
        {"role": "textbox", "name": "First name", "required": True},
        {"role": "textbox", "name": "Email address"},
        {
            "role": "combobox",
            "name": "Do you have full working rights in Australia?",
            "children": [
                {"role": "option", "name": "Yes"},
                {"role": "option", "name": "No"},
            ],
        },
        {"role": "button", "name": "Submit"},
    ],
}


def test_fields_come_from_the_accessibility_tree():
    extracted = generic.fields_from_accessibility(SNAPSHOT)
    labels = [f.label for f in extracted]

    assert "First name" in labels
    assert "Do you have full working rights in Australia?" in labels
    assert "Submit" not in labels, "a button is not a field to fill"


def test_choices_are_read_from_the_options():
    extracted = generic.fields_from_accessibility(SNAPSHOT)
    combo = next(f for f in extracted if f.kind == "select")
    assert combo.choices == ["Yes", "No"]


def test_an_empty_snapshot_yields_no_fields():
    assert generic.fields_from_accessibility(None) == []


@pytest.mark.parametrize(
    "marker",
    ["reCAPTCHA", "I'm not a robot", "hCaptcha", "Verify you are human"],
)
def test_captcha_is_detected(marker):
    snapshot = {"role": "WebArea", "name": marker, "children": []}
    assert generic.detect_captcha(snapshot) is True


def test_a_normal_form_is_not_mistaken_for_a_captcha():
    assert generic.detect_captcha(SNAPSHOT) is False


def test_an_unconfident_mapping_is_not_usable(monkeypatch):
    """Same abstain rule as the answer bank, for the same reason."""
    monkeypatch.setattr(
        generic.llm,
        "complete_json",
        lambda *a, **k: {
            "fields": [
                {"identifier": "first_name", "source": "profile", "confident": True},
                {"identifier": "wwcc", "source": "answer_bank", "confident": False},
            ]
        },
    )
    mapped = generic.map_fields(
        fields(("first_name", "First name", "text"), ("wwcc", "WWCC number", "text"))
    )
    by_id = {m.identifier: m for m in mapped}

    assert by_id["first_name"].usable is True
    assert by_id["wwcc"].usable is False


def test_a_field_the_model_skipped_becomes_unknown(monkeypatch):
    monkeypatch.setattr(generic.llm, "complete_json", lambda *a, **k: {"fields": []})
    mapped = generic.map_fields(fields(("x", "Something", "text")))
    assert mapped[0].source == "unknown"
    assert mapped[0].usable is False


def test_a_failed_mapping_call_marks_everything_unknown(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr(generic.llm, "complete_json", boom)
    mapped = generic.map_fields(fields(*[(i, l, k) for i, l, k in STANDARD]))
    assert all(not m.usable for m in mapped)


def test_the_mapping_schema_cannot_return_values():
    """The model maps WHERE a value comes from; it never supplies one."""
    properties = generic.MAPPING_SCHEMA["properties"]["fields"]["items"]["properties"]
    for forbidden in ("value", "answer", "answer_value", "text"):
        assert forbidden not in properties


# =========================================================================
# Adapters
# =========================================================================


def test_every_adapter_implements_the_flow_contract():
    from backend.apply.flow import Adapter

    required = [
        name
        for name in dir(Adapter)
        if not name.startswith("_") and callable(getattr(Adapter, name, None))
    ]
    for applier in build_ats_appliers():
        for name in required:
            assert hasattr(applier, name), f"{applier.platform} is missing {name}"


def test_no_ats_adapter_decides_whether_to_submit():
    """Parsed, not grepped: the module docstring explains the rule in prose."""
    import ast
    import pathlib

    from backend.ats import adapters

    tree = ast.parse(pathlib.Path(adapters.__file__).read_text(encoding="utf-8"))

    called: set[str] = set()
    read: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
        elif isinstance(node, ast.Attribute):
            read.add(node.attr)

    assert "check_can_submit" not in called
    assert "allow_live_submit" not in read


def test_adapters_are_built_in_australian_priority_order():
    keys = [applier.platform for applier in build_ats_appliers()]
    assert keys[0] == "jobadder"
    assert keys[1] == "pageup"
    assert keys[-1] == "workday"


def test_an_adapter_only_claims_its_own_platform():
    from backend.models import ApplyType, Job

    job = Job(
        id=1,
        source="seek",
        source_job_id="1",
        url="https://boards.greenhouse.io/acme/jobs/1",
        title="Dev",
        company="Acme",
        dedupe_hash="h",
        apply_type=ApplyType.EXTERNAL,
    )
    greenhouse = GenericAtsApplier("greenhouse")
    lever = GenericAtsApplier("lever")

    assert greenhouse.can_handle(job) is True
    assert lever.can_handle(job) is False


def test_confirmation_selectors_have_more_than_one_candidate():
    for platform, selectors in SELECTORS.items():
        assert len(selectors["confirmation"]) >= 2, f"{platform} has one brittle selector"


# =========================================================================
# Queueing — the user's attention is the scarce resource
# =========================================================================


@pytest.fixture
def campaign():
    return Campaign(
        id=1,
        name="c",
        search_terms=["dev"],
        locations=["Adelaide SA"],
        score_floor=60.0,
        score_auto_apply=80.0,
        gray_zone_action=GrayZoneAction.QUEUE,
    )


def test_the_manual_floor_sits_above_the_auto_apply_threshold(campaign):
    """Backwards-looking but correct: manual costs attention, auto costs cents."""
    assert manual_queue_floor(campaign) == campaign.score_auto_apply + MANUAL_QUEUE_PREMIUM
    assert manual_queue_floor(campaign) > campaign.score_auto_apply


def test_a_job_good_enough_to_auto_apply_is_not_good_enough_to_queue(campaign):
    decision = decide_queueing(campaign, campaign.score_auto_apply, automatable=False)
    assert decision.action == "skip"
    assert "scarce resource" in decision.reason


def test_a_strong_unautomatable_job_reaches_the_queue(campaign):
    decision = decide_queueing(campaign, 95.0, automatable=False)
    assert decision.action == "queue"


def test_an_automatable_job_never_reaches_the_manual_queue(campaign):
    decision = decide_queueing(campaign, 85.0, automatable=True)
    assert decision.action == "auto"


def test_an_unscored_unautomatable_job_is_skipped(campaign):
    assert decide_queueing(campaign, None, automatable=False).action == "skip"


# ---------------------------------------------------------- the cache is used


@pytest.fixture(autouse=True)
def _isolated_formmaps(tmp_path, monkeypatch):
    """Every test in this module gets its own form-map cache.

    map_fields persists what it learns, and conftest redirects DATA_DIR once
    per *session* rather than per test. Without this, one test's saved map
    would be a silent cache hit in the next, and a test asserting an LLM call
    happened would pass while making none.
    """
    monkeypatch.setattr(settings, "data_dir", tmp_path)


class CountingMapper:
    """Stands in for the model and counts how often it was actually asked."""

    def __init__(self, answers: dict[str, dict]):
        self.answers = answers
        self.calls = 0
        self.asked: list[list[str]] = []

    def __call__(self, prompt, **kwargs):
        self.calls += 1
        # The prompt names each field it wants mapped as id='...'.
        asked = re.findall(r"id='([^']+)'", prompt)
        self.asked.append(asked)
        return {"fields": [self.answers[i] for i in asked if i in self.answers]}


RESOLVED = {
    "first_name": {
        "identifier": "first_name",
        "source": "profile",
        "profile_path": "profile.first_name",
        "confident": True,
    },
    "email": {
        "identifier": "email",
        "source": "profile",
        "profile_path": "profile.email",
        "confident": True,
    },
    "rights": {
        "identifier": "rights",
        "source": "answer_bank",
        "question": "Do you have full working rights in Australia?",
        "confident": True,
    },
}


def test_the_same_form_shape_twice_makes_one_llm_call(monkeypatch, session):
    """The whole point of the cache: pay for a form shape once, not per job.

    Before this was wired, formmaps.py had no production caller and every
    application re-paid for a mapping the system had already learned.
    """
    mapper = CountingMapper(RESOLVED)
    monkeypatch.setattr(generic.llm, "complete_json", mapper)

    shape = fields(
        ("first_name", "First name", "text"),
        ("email", "Email address", "text"),
        ("rights", "Do you have full working rights in Australia?", "radio"),
    )

    first = generic.map_fields(shape, platform="greenhouse", session=session)
    assert mapper.calls == 1, "the first form of a new shape must be learned"
    assert all(m.usable for m in first)

    second = generic.map_fields(shape, platform="greenhouse", session=session)
    assert mapper.calls == 1, (
        f"a cached form shape must cost zero LLM calls, made {mapper.calls - 1} extra"
    )

    # Served from cache, but identical to what was learned — a cache that
    # returned something weaker would be worse than no cache.
    assert [m.identifier for m in second] == [m.identifier for m in first]
    assert all(m.usable for m in second)
    by_id = {m.identifier: m for m in second}
    assert by_id["email"].profile_path == "profile.email"
    assert by_id["rights"].question == "Do you have full working rights in Australia?"
    assert by_id["rights"].source == "answer_bank"


def test_the_cache_survives_without_a_session(monkeypatch):
    """The maps live on disk; the session only adds the index row."""
    mapper = CountingMapper(RESOLVED)
    monkeypatch.setattr(generic.llm, "complete_json", mapper)
    shape = fields(("email", "Email address", "text"))

    generic.map_fields(shape, platform="greenhouse")
    generic.map_fields(shape, platform="greenhouse")

    assert mapper.calls == 1


def test_a_form_that_gains_a_field_is_a_new_form(monkeypatch, session):
    """A changed shape is a cache miss, by design — and must stay one.

    The fingerprint covers the whole form, so adding a question produces a
    different fingerprint and the form is learned afresh. That is deliberate:
    reusing a mapping across shapes would mean applying one form's field
    positions to another, which is how a value lands in the wrong box. The cost
    is that a site varying its questions per job re-learns per variant.

    (Partial re-learning does exist, but within a single fingerprint — see
    test_an_unconfident_mapping_is_never_served_from_cache.)
    """
    mapper = CountingMapper(RESOLVED)
    monkeypatch.setattr(generic.llm, "complete_json", mapper)

    two = fields(
        ("first_name", "First name", "text"), ("email", "Email address", "text")
    )
    three = fields(
        ("first_name", "First name", "text"),
        ("email", "Email address", "text"),
        ("rights", "Do you have full working rights in Australia?", "radio"),
    )
    assert fingerprint_fields(two) != fingerprint_fields(three)

    generic.map_fields(two, platform="greenhouse", session=session)
    assert mapper.calls == 1

    generic.map_fields(three, platform="greenhouse", session=session)
    assert mapper.calls == 2, "a new shape must be learned, not guessed from the old one"

    # ...and each shape is independently cached from then on.
    generic.map_fields(two, platform="greenhouse", session=session)
    generic.map_fields(three, platform="greenhouse", session=session)
    assert mapper.calls == 2


def test_an_unconfident_mapping_is_never_served_from_cache(monkeypatch, session):
    """The abstain rule has to survive the round trip through the cache.

    A guess stored as resolved would be replayed on every later application
    without anyone being asked again — exactly what the answer bank's abstain
    rule exists to prevent.
    """
    mapper = CountingMapper(
        {
            "email": RESOLVED["email"],
            "wwcc": {"identifier": "wwcc", "source": "answer_bank", "confident": False},
        }
    )
    monkeypatch.setattr(generic.llm, "complete_json", mapper)
    shape = fields(("email", "Email address", "text"), ("wwcc", "WWCC number", "text"))

    generic.map_fields(shape, platform="greenhouse", session=session)
    second = generic.map_fields(shape, platform="greenhouse", session=session)

    assert mapper.calls == 2, "an unresolved field must be asked again, not replayed"
    assert mapper.asked[1] == ["wwcc"], "only the unresolved field is re-asked"
    by_id = {m.identifier: m for m in second}
    assert by_id["wwcc"].usable is False
    assert by_id["email"].usable is True


def test_a_different_form_shape_is_a_different_cache_entry(monkeypatch, session):
    """Fingerprint is the form's shape — two forms must not share a mapping."""
    mapper = CountingMapper(RESOLVED)
    monkeypatch.setattr(generic.llm, "complete_json", mapper)

    generic.map_fields(fields(("email", "Email address", "text")), session=session)
    generic.map_fields(
        fields(("first_name", "First name", "text")), session=session
    )
    assert mapper.calls == 2
