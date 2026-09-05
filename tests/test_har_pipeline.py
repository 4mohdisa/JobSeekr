"""The capture pipeline: extract, merge, replay.

Every test runs against the synthetic captures in ``tests/fixtures/captures``,
which imitate the markup shapes that matter — Seek's data-automation hooks and
separate questions step, LinkedIn's URN and Ember ids inside a modal.

Synthetic because the real capture has not happened yet. The shapes are what
the pipeline reasons about, and they are pinned here so the real capture lands
on tested code rather than on code that has only ever been read.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from sqlmodel import select

from backend.apply.harextract import (
    Capture,
    extract,
    extract_from_html,
    merge_into,
    push_questions_to_answer_bank,
)
from backend.apply.snapshot import SnapshotPage, page_for_capture
from backend.models import AnswerBank
from backend.siteknowledge import Element, SiteKnowledge, Strategy

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "captures"
SEEK = FIXTURES / "seek_quick_apply.har"  # .steps.json sits beside it
LINKEDIN = FIXTURES / "linkedin_two_step.har"
HAR_ONLY = FIXTURES / "seek_har_only.har"


@pytest.fixture
def session(tmp_path, monkeypatch):
    """A bare in-memory database. No seeded answers — these tests are about
    what a capture puts *into* an empty bank."""
    from sqlmodel import Session, SQLModel, create_engine

    from backend.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def seek_capture() -> Capture:
    return extract(SEEK, platform="seek", variant="quick_apply")


@pytest.fixture
def linkedin_capture() -> Capture:
    return extract(LINKEDIN, platform="linkedin", variant="two_step")


# ------------------------------------------------------------------ extraction


def test_every_step_of_the_capture_is_read(seek_capture):
    assert len(seek_capture.steps) == 3
    assert seek_capture.steps[0], "the listing page has the apply button"
    assert seek_capture.steps[2], "the questions step has the screening fields"


def test_an_element_gets_every_way_of_identifying_it(seek_capture):
    submit = next(
        element
        for element in seek_capture.elements
        if element.identifier == "review-submit-application"
    )
    types = {strategy.type for strategy in submit.strategies}
    assert "testid" in types, "data-automation is the most durable hook Seek offers"
    assert "role" in types
    assert "text" in types


def test_strategies_are_ordered_most_durable_first(seek_capture):
    submit = next(
        element
        for element in seek_capture.elements
        if element.identifier == "review-submit-application"
    )
    priorities = [strategy.priority for strategy in submit.strategies]
    assert priorities == sorted(priorities)
    assert submit.strategies[0].type == "testid"


def test_a_volatile_linkedin_id_becomes_a_pattern(linkedin_capture):
    """A literal captured on one job cannot match the next."""
    upload = next(
        element for element in linkedin_capture.elements if element.kind == "file"
    )
    selectors = [strategy.selector for strategy in upload.strategies]

    assert any("jobs-document-upload" in s and "*=" in s for s in selectors), selectors
    assert not any("ember2311" in s for s in selectors), (
        "the per-render Ember counter must not survive into a strategy"
    )


def test_a_urn_bearing_id_becomes_a_pattern(linkedin_capture):
    email = next(
        element
        for element in linkedin_capture.elements
        if element.identifier == "email"
    )
    selectors = [strategy.selector for strategy in email.strategies]
    assert not any("4012345678" in s for s in selectors), (
        f"the job id must not be baked into a strategy: {selectors}"
    )


def test_a_short_identifier_is_kept_literal_rather_than_patterned():
    """``q1`` -> ``[id*='q']`` would match nearly every element on the page.

    An over-broad strategy is worse than none: resolution finds *something*,
    reports success, and the adapter clicks the wrong control. Below the minimum
    fragment length the literal is kept, which at worst fails to match.
    """
    elements = extract_from_html('<input id="q1" name="q1" type="text">')
    selectors = [s.selector for s in elements[0].strategies]

    assert "[id='q1']" in selectors
    assert "[id*='q']" not in selectors


def test_a_select_does_not_get_a_text_strategy():
    """A dropdown's text is its concatenated options, which identifies nothing."""
    elements = extract_from_html(
        '<select id="x"><option>Yes</option><option>No</option></select>'
    )
    assert not [s for s in elements[0].strategies if s.type == "text"]


def test_required_fields_and_types_are_identified(seek_capture):
    questions_step = seek_capture.steps[2]
    by_id = {element.identifier: element for element in questions_step}

    assert by_id["q_1"].required and by_id["q_1"].kind == "select"
    assert by_id["q_1"].choices == ["Yes", "No"], "the placeholder option is dropped"
    assert not by_id["start"].required


def test_hidden_inputs_are_not_captured():
    """A hidden CSRF field is not a form field anyone fills."""
    elements = extract_from_html(
        '<input type="hidden" name="csrf" value="x"><input type="text" name="real">'
    )
    assert [element.identifier for element in elements] == ["real"]


def test_the_step_sequence_is_fingerprinted(seek_capture, linkedin_capture):
    assert seek_capture.fingerprint != linkedin_capture.fingerprint
    assert len(seek_capture.fingerprint) == 32


def test_a_har_without_snapshots_still_yields_the_server_rendered_document():
    """Degraded, not broken. Modal steps are missing and that is logged."""
    capture = extract(HAR_ONLY, platform="seek", variant="quick_apply")
    assert len(capture.steps) == 1
    assert any(
        element.identifier == "job-detail-apply" for element in capture.elements
    ), "the largest HTML body should be the document, not a tracking fragment"


def test_a_missing_capture_yields_nothing_rather_than_raising():
    capture = extract(FIXTURES / "nope.har", platform="seek", variant="quick_apply")
    assert capture.steps == []


# ------------------------------------------------------------------- questions


def test_screening_questions_are_recognised(seek_capture):
    questions = {element.label for element in seek_capture.questions}
    assert "Do you have full working rights in Australia?" in questions
    assert "How many years of experience do you have with SQL?" in questions


def test_ordinary_fields_are_not_mistaken_for_questions(seek_capture):
    labels = {element.label for element in seek_capture.questions}
    assert "Preferred start date" not in labels, "a plain detail is not a question"
    assert not any(element.kind == "file" for element in seek_capture.questions)


def test_questions_reach_the_answer_bank_unanswered(session, seek_capture):
    """Hard rule 2: a capture is evidence about the question, never the answer."""
    added = push_questions_to_answer_bank(session, seek_capture)
    session.flush()
    assert added >= 2

    rows = session.exec(select(AnswerBank)).all()
    captured = [row for row in rows if "capture" in (row.notes or "")]
    assert captured

    for row in captured:
        assert row.answer_value == "", f"{row.question_pattern} arrived with an answer"
        assert row.verified_at is None


def test_a_captured_question_carries_its_options(session, seek_capture):
    push_questions_to_answer_bank(session, seek_capture)
    session.flush()
    row = session.exec(
        select(AnswerBank).where(
            AnswerBank.question_pattern
            == "Do you have full working rights in Australia?"
        )
    ).first()
    assert row is not None
    assert row.choices == ["Yes", "No"]


def test_a_blank_captured_row_makes_the_flow_abstain(session, seek_capture):
    """The unanswered row is not inert — it drives the ask-the-user loop."""
    from backend.apply.answers import Abstain, load_answers, resolve_answer

    push_questions_to_answer_bank(session, seek_capture)
    session.flush()

    resolution = resolve_answer(
        "Do you have full working rights in Australia?",
        answers=load_answers(session, campaign_id=None),
        choices=["Yes", "No"],
    )
    assert isinstance(resolution, Abstain), (
        "a blank row must abstain, never resolve to an empty answer"
    )


def test_re_pushing_the_same_questions_adds_nothing(session, seek_capture):
    push_questions_to_answer_bank(session, seek_capture)
    session.flush()
    assert push_questions_to_answer_bank(session, seek_capture) == 0


def test_pushing_never_disturbs_an_existing_answer(session, seek_capture):
    from backend.models import AnswerType, MatchType

    session.add(
        AnswerBank(
            question_pattern="Do you have full working rights in Australia?",
            match_type=MatchType.FUZZY,
            answer_value="Yes",
            answer_type=AnswerType.BOOLEAN,
        )
    )
    session.flush()

    push_questions_to_answer_bank(session, seek_capture)
    session.flush()

    rows = session.exec(
        select(AnswerBank).where(
            AnswerBank.question_pattern
            == "Do you have full working rights in Australia?"
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].answer_value == "Yes", "a capture must never overwrite an answer"


# ----------------------------------------------------------------------- merge


def test_a_first_capture_creates_elements(seek_capture):
    knowledge = SiteKnowledge(platform="seek")
    report = merge_into(knowledge, seek_capture)

    assert report.new_elements
    assert knowledge.elements
    assert knowledge.dirty


def test_a_recapture_preserves_success_counts_and_promotions(seek_capture):
    """The heart of "merge, never overwrite".

    Counts and promotions are production evidence about what has actually been
    working. A capture cannot know any of it, so it must not reset it.
    """
    knowledge = SiteKnowledge(platform="seek")
    merge_into(knowledge, seek_capture)

    key, element = next(iter(knowledge.elements.items()))
    element.success_count = 17
    element.fail_count = 2
    element.last_working_strategy = element.strategies[0].id
    original_strategy_count = len(element.strategies)

    report = merge_into(knowledge, seek_capture)

    after = knowledge.elements[key]
    assert after.success_count == 17
    assert after.fail_count == 2
    assert after.last_working_strategy == element.strategies[0].id
    assert len(after.strategies) == original_strategy_count, (
        "an identical re-capture must not duplicate strategies"
    )
    assert report.preserved_counts[key] == (17, 2)


def test_a_recapture_adds_newly_seen_strategies(seek_capture):
    knowledge = SiteKnowledge(platform="seek")
    merge_into(knowledge, seek_capture)

    key = next(iter(knowledge.elements))
    knowledge.elements[key].strategies = [
        Strategy(type="css", value="#only-the-old-one")
    ]

    report = merge_into(knowledge, seek_capture)

    assert report.new_strategies[key], "the freshly captured strategies should arrive"
    assert any(
        strategy.value == "#only-the-old-one"
        for strategy in knowledge.elements[key].strategies
    ), "and the pre-existing one should survive"


def test_merging_records_the_flow_variant(seek_capture):
    knowledge = SiteKnowledge(platform="seek")
    report = merge_into(knowledge, seek_capture)

    assert report.new_variant
    assert knowledge.known_variant(seek_capture.steps) is not None

    second = merge_into(knowledge, seek_capture)
    assert not second.new_variant, "the same shape is recognised, not re-added"
    assert len(knowledge.flow_variants) == 1


def test_merging_does_not_invent_adapter_element_keys(seek_capture):
    """Captured keys are namespaced, so a capture cannot silently rewire an adapter.

    Mapping "a button labelled Submit" onto the adapter's ``submit_button`` would
    be a guess, and a wrong guess repoints the adapter at a different control.
    A human promotes a captured key to a curated one after reading the merge.
    """
    knowledge = SiteKnowledge(
        platform="seek",
        elements={
            "submit_button": Element(
                key="submit_button",
                strategies=[Strategy(type="css", value="#curated")],
                success_count=9,
            )
        },
    )
    merge_into(knowledge, seek_capture)

    assert knowledge.elements["submit_button"].success_count == 9
    assert knowledge.elements["submit_button"].strategies[0].value == "#curated"
    assert all(
        key.startswith("captured_")
        for key in knowledge.elements
        if key != "submit_button"
    )


# ------------------------------------------------------------- replay harness


def test_the_harness_resolves_a_testid_strategy():
    page = page_for_capture(SEEK.with_suffix(".steps.json"), step=0)
    strategy = Strategy(type="testid", value="job-detail-apply", attr="data-automation")
    assert page.locator(strategy.selector).count() == 1


def test_the_harness_resolves_a_role_and_name_strategy():
    page = page_for_capture(SEEK.with_suffix(".steps.json"), step=2)
    strategy = Strategy(type="role", role="button", name="Submit application")
    assert page.locator(strategy.selector).count() == 1


def test_the_harness_resolves_a_wildcard_pattern():
    page = page_for_capture(LINKEDIN.with_suffix(".steps.json"), step=1)
    strategy = Strategy(type="testid", value="jobs-document-upload*", attr="id")
    assert page.locator(strategy.selector).count() == 1


def test_the_harness_resolves_a_text_regex():
    page = page_for_capture(SEEK.with_suffix(".steps.json"), step=0)
    assert (
        page.locator(Strategy(type="text", value="Quick apply").selector).count() >= 1
    )


def test_the_harness_refuses_a_selector_it_cannot_honour():
    """ "No match" and "I did not understand that" must not look the same."""
    page = SnapshotPage("<button>x</button>")
    with pytest.raises(NotImplementedError):
        page.locator("button:has-text('x')").count()
    with pytest.raises(NotImplementedError):
        page.locator("xpath=//button").count()


def test_the_harness_refuses_to_evaluate_javascript():
    page = SnapshotPage("<input id='a'>")
    with pytest.raises(NotImplementedError):
        page.locator("#a").evaluate("el => el.offsetWidth")


def test_the_harness_reports_attribute_hidden_elements_as_invisible():
    page = SnapshotPage('<button hidden>a</button><button id="b">b</button>')
    assert not page.locator("button").first.is_visible()
    assert page.locator("#b").is_visible()


# ------------------------------- the point: a real adapter, real captured markup


def test_the_seek_adapter_resolves_its_elements_against_captured_markup():
    """A capture becomes a permanent fixture: the adapter runs against it.

    This is what makes the pipeline worth building. Not "the extractor produced
    plausible JSON" but "the adapter, unmodified, finds what it needs in the
    markup Seek actually served".
    """
    from backend.apply.seek import SeekApplier

    knowledge = SiteKnowledge(
        platform="seek",
        elements={
            "apply_button": Element(
                key="apply_button",
                strategies=[
                    Strategy(
                        type="testid", value="job-detail-apply", attr="data-automation"
                    )
                ],
            ),
            "submit_button": Element(
                key="submit_button",
                strategies=[
                    Strategy(type="role", role="button", name="Submit application")
                ],
            ),
        },
    )
    applier = SeekApplier(knowledge=knowledge)
    steps = SEEK.with_suffix(".steps.json")

    listing = page_for_capture(steps, step=0)
    knowledge.resolve(listing, "apply_button").click()
    assert listing.clicked, "the adapter found and clicked the real apply control"

    questions = page_for_capture(steps, step=2)
    assert applier.is_last_step(questions, [])
    assert not applier.is_last_step(page_for_capture(steps, step=1), [])


def test_the_seek_adapter_enumerates_the_real_questions_step():
    """The adapter's own enumeration, over captured markup, with no browser."""
    from backend.apply.seek import SeekApplier

    applier = SeekApplier(knowledge=SiteKnowledge(platform="seek"))
    page = page_for_capture(SEEK.with_suffix(".steps.json"), step=2)

    fields = applier.enumerate_fields(page, step=0)
    by_id = {field.identifier: field for field in fields}

    assert by_id["q_1"].label == "Do you have full working rights in Australia?"
    assert by_id["q_1"].choices == ["Yes", "No"]
    assert by_id["q_1"].required


def test_a_captured_strategy_resolves_through_the_real_resolution_path(seek_capture):
    """Extract -> merge -> resolve, with nothing hand-written in between.

    If the extractor emits a strategy the resolver cannot honour, this is where
    it shows up — and that mismatch would make the whole pipeline silently
    useless.
    """
    knowledge = SiteKnowledge(platform="seek")
    merge_into(knowledge, seek_capture)

    page = page_for_capture(SEEK.with_suffix(".steps.json"), step=0)
    key = next(
        key
        for key, element in knowledge.elements.items()
        if any("job-detail-apply" in s.selector for s in element.strategies)
    )
    assert knowledge.resolve(page, key) is not None


def test_every_captured_strategy_is_resolvable_by_the_harness(seek_capture):
    """No strategy the extractor emits may be one the resolver cannot parse."""
    for element in seek_capture.elements:
        for strategy in element.strategies:
            page = SnapshotPage("<html><body></body></html>")
            page.locator(strategy.selector).count()  # must not raise


# ---------------------------------------------------------------- persistence


def test_an_ingested_capture_survives_a_save_and_reload(tmp_path, seek_capture):
    from backend.siteknowledge import load

    knowledge = SiteKnowledge(platform="seek", directory=tmp_path)
    merge_into(knowledge, seek_capture)
    knowledge.save()

    reloaded = load("seek", directory=tmp_path)
    assert reloaded.elements.keys() == knowledge.elements.keys()
    assert reloaded.flow_variants.keys() == knowledge.flow_variants.keys()


def test_the_snapshot_fixtures_are_valid_json():
    """A malformed fixture would make every test above vacuous."""
    for path in FIXTURES.glob("*.steps.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["steps"], f"{path.name} has no steps"
        for step in payload["steps"]:
            assert step["html"].strip(), f"{path.name} has an empty step"
