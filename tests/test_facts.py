"""The two-layer answer bank: facts verbatim, answers derived and confirmed once.

The dangerous parts, in order:

* a fact that is SILENT on something must abstain, not answer No
* a fact that is SPECIFIC may answer No for what it excludes
* an unconfirmed derivation answers nothing
* editing a fact invalidates what was derived from it
* jurisdiction is a hard boundary, same as the answer bank's region

Every derivation test stubs the model. The model's job is judgement and it is
not what these are checking — they check that a refusal is honoured, that an
answer cannot be used before confirmation, and that stale evidence stops
counting.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from backend import facts
from backend.models import (
    AnswerBank,
    AnswerType,
    DerivedAnswer,
    Fact,
    FactCategory,
    MatchType,
    Region,
)

LICENCE = "Full SA driver's licence, class C, held since 2019, no restrictions"
RIGHTS = "Australian permanent resident, unrestricted, no sponsorship required"


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def licence(session) -> Fact:
    row = facts.set_fact(
        session,
        key="licence",
        text=LICENCE,
        category=FactCategory.LICENCE,
        jurisdiction=Region.AU,
    )
    session.flush()
    return row


# =========================================================================
# Layer 1 — verbatim
# =========================================================================


def test_a_fact_is_stored_exactly_as_typed(session):
    """A paraphrase would become the source of truth for a legal declaration."""
    messy = "  Full SA driver's licence,\n  class C — held since 2019.  "
    row = facts.set_fact(
        session, key="licence", text=messy, category=FactCategory.LICENCE
    )
    session.flush()

    # Only the trailing whitespace goes, which is a textarea artifact.
    assert row.text == messy.rstrip()
    assert "class C — held since 2019." in row.text
    assert row.text.startswith("  Full SA")


def test_setting_the_same_key_twice_updates_rather_than_duplicating(session):
    facts.set_fact(session, key="licence", text="first", category=FactCategory.LICENCE)
    session.flush()
    facts.set_fact(session, key="licence", text="second", category=FactCategory.LICENCE)
    session.flush()

    rows = session.exec(select(Fact).where(Fact.key == "licence")).all()
    assert len(rows) == 1
    assert rows[0].text == "second"


def test_the_hash_changes_when_the_wording_does():
    """Staleness is by content. A timestamp cannot catch class C becoming MR."""
    assert facts.fact_hash("class C") != facts.fact_hash("class MR")
    assert facts.fact_hash("class C") == facts.fact_hash("class C")


def test_whitespace_counts_as_a_change():
    """A user who reworded a fact meant something by it.

    Deciding a whitespace edit is "the same fact" is the judgement that lets a
    real edit slip through as cosmetic.
    """
    assert facts.fact_hash("class C, no restrictions") != facts.fact_hash(
        "class C,no restrictions"
    )


# =========================================================================
# Layer 2 — derivation, and the abstain rule
# =========================================================================


def test_a_specific_fact_can_answer_no_for_what_it_excludes(
    session, licence, monkeypatch
):
    """Class C is not MR. The fact states the class, so No is supported."""
    monkeypatch.setattr(
        facts.llm,
        "complete_json",
        lambda *a, **k: {
            "supported": True,
            "answer": "No",
            "reasoning": "the fact states class C, which is not MR",
            "uncertainty": "",
        },
    )

    result = facts.derive(
        licence, "Do you hold an MR (medium rigid) licence?", choices=["Yes", "No"]
    )
    assert result is not None
    assert result.answer == "No"
    assert result.usable


def test_a_silent_fact_abstains_rather_than_answering_no(session, licence, monkeypatch):
    """THE rule. Silence is not denial, and a wrong No costs an interview."""
    monkeypatch.setattr(
        facts.llm,
        "complete_json",
        lambda *a, **k: {
            "supported": False,
            "answer": "",
            "reasoning": "the fact says nothing about forklifts",
            "uncertainty": "no forklift licence mentioned",
        },
    )

    assert (
        facts.derive(
            licence, "Do you hold a current forklift licence?", choices=["Yes", "No"]
        )
        is None
    )


def test_an_unsupported_result_is_ignored_even_when_it_carries_an_answer(
    session, licence, monkeypatch
):
    """The `supported` flag is the gate, not the emptiness of `answer`.

    The previous test passes even with the supported check deleted, because its
    stub also returns an empty answer — so the empty-answer guard catches it and
    the real gate is never exercised. A model that says supported=false AND
    volunteers "No" is the actual failure mode: silence rendered as denial.

    Verified by mutation: removing the supported check makes this fail.
    """
    # supported=false, a valid answer, and NO stated doubt. Every other guard
    # in derive() lets this through — the empty-answer check, the choices check
    # and the uncertainty check all pass — so the `supported` flag is the only
    # thing standing between this and "No" on a real application.
    monkeypatch.setattr(
        facts.llm,
        "complete_json",
        lambda *a, **k: {
            "supported": False,
            "answer": "No",
            "reasoning": "the fact says nothing about forklifts",
            "uncertainty": "",
        },
    )

    assert (
        facts.derive(
            licence, "Do you hold a current forklift licence?", choices=["Yes", "No"]
        )
        is None
    ), "an unsupported derivation must not answer, whatever it volunteered"


def test_any_stated_uncertainty_abstains(session, licence, monkeypatch):
    """ "supported" plus a doubt is still a doubt.

    If the model was weighing whether something counts, that weighing IS the
    uncertainty — using the answer anyway would be guessing with extra steps.
    """
    monkeypatch.setattr(
        facts.llm,
        "complete_json",
        lambda *a, **k: {
            "supported": True,
            "answer": "Yes",
            "reasoning": "probably covers it",
            "uncertainty": "the fact does not say whether it is still current",
        },
    )

    assert (
        facts.derive(licence, "Is your licence current?", choices=["Yes", "No"]) is None
    )


def test_an_answer_outside_the_offered_choices_abstains(session, licence, monkeypatch):
    """Coercing it would be guessing which option the model meant."""
    monkeypatch.setattr(
        facts.llm,
        "complete_json",
        lambda *a, **k: {
            "supported": True,
            "answer": "Class C",
            "reasoning": "",
            "uncertainty": "",
        },
    )

    assert (
        facts.derive(licence, "Do you hold a licence?", choices=["Yes", "No"]) is None
    )


def test_a_model_failure_is_an_abstention_not_a_crash(session, licence, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(facts.llm, "complete_json", boom)
    assert facts.derive(licence, "Do you hold a licence?") is None


# =========================================================================
# Confirmation — asked once, then never again
# =========================================================================


def confirmable(monkeypatch, answer="Yes"):
    monkeypatch.setattr(
        facts.llm,
        "complete_json",
        lambda *a, **k: {
            "supported": True,
            "answer": answer,
            "reasoning": "the fact states a full class C licence",
            "uncertainty": "",
        },
    )


def test_a_first_derivation_asks_and_still_abstains(session, licence, monkeypatch):
    """The model's reading of someone's licence is a proposal until they agree."""
    asked: list[tuple] = []
    confirmable(monkeypatch)
    monkeypatch.setattr(
        facts, "on_confirmation_needed", lambda *args: asked.append(args)
    )

    answer = facts.resolve_from_facts(
        session,
        question="Do you hold a current driver's licence?",
        question_key="do you hold a current driver's licence",
        category=FactCategory.LICENCE,
        choices=["Yes", "No"],
        region=Region.AU,
    )

    assert answer is None, "an unconfirmed derivation must not answer"
    assert len(asked) == 1, "and the user must be asked"
    assert session.exec(select(DerivedAnswer)).one().confirmed_at is None


def test_a_confirmed_derivation_answers_without_asking_again(
    session, licence, monkeypatch
):
    asked: list[tuple] = []
    confirmable(monkeypatch)
    monkeypatch.setattr(
        facts, "on_confirmation_needed", lambda *args: asked.append(args)
    )

    key = "do you hold a current driver's licence"
    facts.resolve_from_facts(
        session,
        question="Do you hold a current driver's licence?",
        question_key=key,
        category=FactCategory.LICENCE,
        choices=["Yes", "No"],
        region=Region.AU,
    )
    session.flush()
    facts.confirm(session, session.exec(select(DerivedAnswer)).one().id)
    session.flush()

    asked.clear()
    answer = facts.resolve_from_facts(
        session,
        question="Do you hold a current driver's licence?",
        question_key=key,
        category=FactCategory.LICENCE,
        choices=["Yes", "No"],
        region=Region.AU,
    )

    assert answer == "Yes"
    assert asked == [], "confirmed once means never asked again"


def test_an_unanswered_proposal_is_not_re_asked_on_every_pass(
    session, licence, monkeypatch
):
    """Asking again each pass is how the channel becomes one the user mutes."""
    asked: list[tuple] = []
    confirmable(monkeypatch)
    monkeypatch.setattr(
        facts, "on_confirmation_needed", lambda *args: asked.append(args)
    )

    key = "do you hold a current driver's licence"
    for _ in range(3):
        facts.resolve_from_facts(
            session,
            question="Do you hold a current driver's licence?",
            question_key=key,
            category=FactCategory.LICENCE,
            choices=["Yes", "No"],
            region=Region.AU,
        )
        session.flush()

    assert len(asked) == 1


def test_a_second_pass_on_an_unconfirmed_derivation_still_abstains(
    session, licence, monkeypatch
):
    """The cached path must honour confirmation, not just avoid re-asking.

    The re-ask test above only counts messages, so it passes even if the cached
    branch starts returning the answer — which would mean an unconfirmed reading
    of the user's licence going onto a real application. This checks the return
    value on the path that actually reads the cache.

    Verified by mutation: making the cached branch ignore confirmed_at fails it.
    """
    confirmable(monkeypatch)
    monkeypatch.setattr(facts, "on_confirmation_needed", lambda *args: None)

    key = "do you hold a current driver's licence"
    kwargs = {
        "question": "Do you hold a current driver's licence?",
        "question_key": key,
        "category": FactCategory.LICENCE,
        "choices": ["Yes", "No"],
        "region": Region.AU,
    }

    assert facts.resolve_from_facts(session, **kwargs) is None
    session.flush()
    assert session.exec(select(DerivedAnswer)).one().confirmed_at is None

    # Second pass: the row is cached and unconfirmed. Still no answer.
    assert facts.resolve_from_facts(session, **kwargs) is None, (
        "an unconfirmed derivation must never answer, cached or not"
    )


def test_rejecting_deletes_so_the_next_pass_re_derives(session, licence, monkeypatch):
    confirmable(monkeypatch)
    monkeypatch.setattr(facts, "on_confirmation_needed", lambda *args: None)

    facts.resolve_from_facts(
        session,
        question="Do you hold a licence?",
        question_key="do you hold a licence",
        category=FactCategory.LICENCE,
        choices=["Yes", "No"],
        region=Region.AU,
    )
    session.flush()
    row_id = session.exec(select(DerivedAnswer)).one().id

    assert facts.reject(session, row_id)
    session.flush()
    assert session.exec(select(DerivedAnswer)).all() == []


# =========================================================================
# Editing a fact invalidates its derivations
# =========================================================================


def confirmed_derivation(session, licence, monkeypatch, answer="Yes") -> DerivedAnswer:
    confirmable(monkeypatch, answer=answer)
    monkeypatch.setattr(facts, "on_confirmation_needed", lambda *args: None)
    facts.resolve_from_facts(
        session,
        question="Do you hold a current driver's licence?",
        question_key="do you hold a current driver's licence",
        category=FactCategory.LICENCE,
        choices=["Yes", "No"],
        region=Region.AU,
    )
    session.flush()
    row = session.exec(select(DerivedAnswer)).one()
    facts.confirm(session, row.id)
    session.flush()
    return row


def test_editing_a_fact_stops_its_derivations_answering(session, licence, monkeypatch):
    """Class C becoming class MR changes answers nobody would revisit."""
    confirmed_derivation(session, licence, monkeypatch)

    facts.set_fact(
        session,
        key="licence",
        text="Full SA driver's licence, class MR, held since 2019",
        category=FactCategory.LICENCE,
        jurisdiction=Region.AU,
    )
    session.flush()

    # Re-deriving is what happens next; the point is the old answer is gone.
    monkeypatch.setattr(facts, "on_confirmation_needed", lambda *args: None)
    answer = facts.resolve_from_facts(
        session,
        question="Do you hold a current driver's licence?",
        question_key="do you hold a current driver's licence",
        category=FactCategory.LICENCE,
        choices=["Yes", "No"],
        region=Region.AU,
    )
    assert answer is None, "a derivation from edited evidence must not answer"


def test_an_unedited_fact_keeps_its_derivations(session, licence, monkeypatch):
    """The mirror: invalidation must not fire on every save."""
    confirmed_derivation(session, licence, monkeypatch)

    facts.set_fact(
        session,
        key="licence",
        text=LICENCE,
        category=FactCategory.LICENCE,
        jurisdiction=Region.AU,
    )
    session.flush()

    answer = facts.resolve_from_facts(
        session,
        question="Do you hold a current driver's licence?",
        question_key="do you hold a current driver's licence",
        category=FactCategory.LICENCE,
        choices=["Yes", "No"],
        region=Region.AU,
    )
    assert answer == "Yes"


def test_stale_derivations_are_reportable(session, licence, monkeypatch):
    confirmed_derivation(session, licence, monkeypatch)
    assert facts.stale_derivations(session) == []

    facts.set_fact(
        session,
        key="licence",
        text=LICENCE + " (renewed 2026)",
        category=FactCategory.LICENCE,
        jurisdiction=Region.AU,
    )
    session.flush()
    assert len(facts.stale_derivations(session)) == 1


def test_deleting_a_fact_takes_its_derivations_with_it(session, licence, monkeypatch):
    """An answer whose evidence no longer exists is not an answer."""
    confirmed_derivation(session, licence, monkeypatch)

    session.delete(licence)
    session.commit()

    assert session.exec(select(DerivedAnswer)).all() == []


# =========================================================================
# Jurisdiction
# =========================================================================


def test_an_australian_fact_does_not_answer_a_new_zealand_question(session, licence):
    """An SA licence says nothing about a New Zealand one.

    Same rule the answer bank already applies to region-scoped rows, reused
    rather than reimplemented.
    """
    assert facts.facts_for(session, FactCategory.LICENCE, region=Region.AU) == [licence]
    assert facts.facts_for(session, FactCategory.LICENCE, region=Region.NZ) == []


def test_a_fact_with_no_jurisdiction_holds_everywhere(session):
    row = facts.set_fact(
        session,
        key="experience",
        text="Eight years as a data analyst",
        category=FactCategory.EXPERIENCE,
        jurisdiction=None,
    )
    session.flush()

    for region in (Region.AU, Region.NZ, None):
        assert row in facts.facts_for(session, FactCategory.EXPERIENCE, region=region)


def test_no_fact_in_the_category_abstains(session):
    assert (
        facts.resolve_from_facts(
            session,
            question="Do you hold a licence?",
            question_key="do you hold a licence",
            category=FactCategory.LICENCE,
            region=Region.AU,
        )
        is None
    )


def test_a_question_with_no_category_abstains(session, licence):
    """No routing means no fact to consult, which is an abstention not a guess."""
    assert (
        facts.resolve_from_facts(
            session,
            question="Something nobody mapped",
            question_key="something nobody mapped",
            category=None,
            region=Region.AU,
        )
        is None
    )


# =========================================================================
# Routing reuses the answer bank's matcher
# =========================================================================


def test_the_seeded_questions_all_route_to_a_fact_category():
    """All 21 migrated, none lost."""
    from backend.seed import ANSWER_BANK_SEEDS

    assert len(ANSWER_BANK_SEEDS) == 21
    missing = [s.question_pattern for s in ANSWER_BANK_SEEDS if s.fact_category is None]
    assert not missing, missing


def test_the_matcher_finds_the_row_that_owns_a_question():
    """One matcher, not two.

    Two matchers disagreeing about what a question is asking is how a licence
    fact ends up answering a police-check question.
    """
    from backend.apply.answers import matching_rows

    bank = [
        AnswerBank(
            question_pattern=r"(?i)\bdriv(er|ing)['’]?s?['’]?\s*licen[cs]e\b",
            match_type=MatchType.REGEX,
            answer_value="",
            answer_type=AnswerType.BOOLEAN,
            fact_category=FactCategory.LICENCE,
        ),
        AnswerBank(
            question_pattern=r"(?i)\b(police\s+(check|clearance))\b",
            match_type=MatchType.REGEX,
            answer_value="",
            answer_type=AnswerType.BOOLEAN,
            fact_category=FactCategory.CHECKS,
        ),
    ]

    rows = matching_rows("Do you hold a current driver's licence?", bank)
    assert rows and rows[0].fact_category is FactCategory.LICENCE

    rows = matching_rows("Do you have a current police check?", bank)
    assert rows and rows[0].fact_category is FactCategory.CHECKS


def test_the_matcher_excludes_the_other_regions_rows():
    from backend.apply.answers import matching_rows

    bank = [
        AnswerBank(
            question_pattern="Do you have full working rights in Australia?",
            match_type=MatchType.FUZZY,
            answer_value="",
            answer_type=AnswerType.BOOLEAN,
            fact_category=FactCategory.WORK_RIGHTS,
            region=Region.AU,
        )
    ]
    assert (
        matching_rows(
            "Do you have full working rights in Australia?", bank, region=Region.NZ
        )
        == []
    )


def test_the_seeded_facts_are_empty(session):
    """A placeholder would be a fabricated fact in the one verbatim store.

    The derivation layer would reason from it happily.
    """
    from backend.seed import FACT_SHELLS

    for _key, _category, prompt in FACT_SHELLS:
        assert prompt, "the prompt must exist for the page to render"
    # And nothing in FACT_SHELLS supplies text at all.
    assert all(len(shell) == 3 for shell in FACT_SHELLS)
