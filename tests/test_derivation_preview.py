"""The derivation preview, and the invalidation it exists to protect.

A derivation is confirmed once and cached forever. That is the point — the user
is asked at most once per question — and it is also why a wrong confirmation is
durable: a plausible misreading of a licence becomes a legal declaration on
every later application.

Two defences, both tested here:

* the preview, which shows all 21 at once BEFORE any is cached, so a bad
  reading is caught by comparison rather than in isolation over Telegram
* invalidation, so correcting a fact retracts what was derived from it
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from backend import facts
from backend.models import (
    AnswerBank,
    AnswerType,
    DerivedAnswer,
    FactCategory,
    MatchType,
    Region,
)

LICENCE = "Full SA driver's licence, class C, held since 2019, no restrictions"


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def seeded(session):
    """One licence fact and the two bank rows that route to it."""
    facts.set_fact(
        session,
        key="licence",
        text=LICENCE,
        category=FactCategory.LICENCE,
        jurisdiction=Region.AU,
    )
    session.add(
        AnswerBank(
            question_pattern=r"(?i)\bdriv(er|ing)['’]?s?['’]?\s*licen[cs]e\b",
            match_type=MatchType.REGEX,
            answer_value="",
            answer_type=AnswerType.BOOLEAN,
            fact_category=FactCategory.LICENCE,
        )
    )
    session.add(
        AnswerBank(
            question_pattern="Do you have your own reliable transport?",
            match_type=MatchType.FUZZY,
            answer_value="",
            answer_type=AnswerType.BOOLEAN,
            fact_category=FactCategory.TRANSPORT,
        )
    )
    session.flush()
    return session


def answers(monkeypatch, value="Yes", supported=True, uncertainty=""):
    monkeypatch.setattr(
        facts.llm,
        "complete_json",
        lambda *a, **k: {
            "supported": supported,
            "answer": value,
            "reasoning": "the fact states a full class C licence",
            "uncertainty": uncertainty,
        },
    )


# =========================================================================
# The preview writes nothing
# =========================================================================


def test_the_preview_writes_no_derivation(seeded, monkeypatch):
    """The whole guarantee. A preview that left proposals behind would be
    indistinguishable from the real path, and running it twice would double
    them."""
    answers(monkeypatch)

    previews = facts.preview_all(seeded)
    seeded.flush()

    assert previews, "it should have previewed something"
    assert seeded.exec(select(DerivedAnswer)).all() == [], "nothing may be written"


def test_the_preview_confirms_nothing(seeded, monkeypatch):
    answers(monkeypatch)
    facts.preview_all(seeded)
    seeded.flush()
    assert facts.pending_confirmations(seeded) == []


def test_the_preview_does_not_ask_over_telegram(seeded, monkeypatch):
    """Twenty-one messages would be the opposite of what this is for."""
    asked: list[tuple] = []
    answers(monkeypatch)
    monkeypatch.setattr(facts, "on_confirmation_needed", lambda *a: asked.append(a))
    try:
        facts.preview_all(seeded)
    finally:
        facts.on_confirmation_needed = None

    assert asked == []


# =========================================================================
# What it reports
# =========================================================================


def test_a_supported_question_shows_the_answer_and_the_reasoning(seeded, monkeypatch):
    answers(monkeypatch, value="Yes")

    licence = next(
        p for p in facts.preview_all(seeded) if p.category is FactCategory.LICENCE
    )

    assert not licence.abstained
    assert licence.answer == "Yes"
    assert licence.reasoning, "the reasoning is what makes a bad reading catchable"
    assert licence.fact_key == "licence"


def test_an_abstention_says_why(seeded, monkeypatch):
    """An abstention is the more important row.

    It is a screening question no application can answer, and the reason says
    whether that is a blank fact or a fact that genuinely does not cover it.
    """
    answers(monkeypatch, supported=False, value="")

    previews = facts.preview_all(seeded)
    licence = next(p for p in previews if p.category is FactCategory.LICENCE)
    transport = next(p for p in previews if p.category is FactCategory.TRANSPORT)

    assert licence.abstained and "no fact supports" in licence.reason
    assert transport.abstained and "blank" in transport.reason, (
        "a blank fact and an unsupportive one need different fixes"
    )


def test_a_blank_fact_is_not_sent_to_the_model(seeded, monkeypatch):
    """Deriving from an empty string wastes a call to learn nothing.

    The blank fact has to EXIST for this to test the guard. Without it the
    transport category is merely empty, `facts_for` returns nothing, and the
    count stays right whether or not the blank check is there — the test would
    pass against its own mutation, which is how it first read.
    """
    facts.set_fact(
        seeded,
        key="transport",
        text="",
        category=FactCategory.TRANSPORT,
        jurisdiction=None,
    )
    seeded.flush()

    calls: list[int] = []
    monkeypatch.setattr(
        facts.llm,
        "complete_json",
        lambda *a, **k: (
            calls.append(1)
            or {"supported": False, "answer": "", "reasoning": "", "uncertainty": ""}
        ),
    )

    facts.preview_all(seeded)

    # Only the licence fact has text; transport is blank and must be skipped.
    assert len(calls) == 1


def test_every_previewed_question_is_readable_english(session, monkeypatch):
    """A regex handed to the model produces nonsense with the confidence of an
    answer.

    The real flow never has this problem — the question comes from the form
    field's own label — but a preview has no form, so the seeds carry a
    plain-English rendering and the preview must use it.
    """
    from backend.seed import seed_answer_bank

    monkeypatch.setattr("backend.seed.session_scope", lambda: _Scope(session))
    seed_answer_bank()
    session.flush()

    for preview in facts.preview_all(session):
        assert not preview.question.startswith("(?i)"), preview.question
        assert "\\b" not in preview.question, preview.question


def test_the_regex_seeds_all_carry_a_distinct_example():
    """Two seeds sharing an example means one of them is previewing the wrong
    question — which is how a salary answer ends up under an hourly-rate
    heading."""
    from backend.seed import ANSWER_BANK_SEEDS

    regex_seeds = [s for s in ANSWER_BANK_SEEDS if s.match_type == MatchType.REGEX]
    assert regex_seeds

    examples = [s.example_question for s in regex_seeds]
    assert all(examples), "every regex seed needs a human question"
    assert len(set(examples)) == len(examples), "two seeds share an example question"


class _Scope:
    """Minimal stand-in for session_scope, which the seed helpers use."""

    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, *exc):
        return False


def test_the_rendered_table_marks_answers_and_abstentions():
    body = facts.render_preview(
        [
            facts.Preview(
                question="Do you hold a licence?",
                category=FactCategory.LICENCE,
                fact_key="licence",
                answer="Yes",
                reasoning="states class C",
                abstained=False,
            ),
            facts.Preview(
                question="Do you have an ABN?",
                category=FactCategory.BUSINESS,
                abstained=True,
                reason="the business fact is blank",
            ),
        ]
    )

    assert "ANSWER" in body and "ABSTAIN" in body
    assert "states class C" in body, "the reasoning must be visible to be checked"
    assert "the business fact is blank" in body
    assert "1 of 2 would be answered" in body


# =========================================================================
# Invalidation — the durable-wrong-confirmation case
# =========================================================================


def confirmed(session, monkeypatch, answer="Yes"):
    answers(monkeypatch, value=answer)
    monkeypatch.setattr(facts, "on_confirmation_needed", lambda *a: None)
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


def test_correcting_a_fact_retracts_what_was_derived_from_it(seeded, monkeypatch):
    """THE case. Class C becoming class MR changes an answer nobody would think
    to revisit, and the confirmation that made it durable was given weeks ago.
    """
    confirmed(seeded, monkeypatch)

    assert (
        facts.resolve_from_facts(
            seeded,
            question="Do you hold a current driver's licence?",
            question_key="do you hold a current driver's licence",
            category=FactCategory.LICENCE,
            choices=["Yes", "No"],
            region=Region.AU,
        )
        == "Yes"
    ), "the confirmed answer should be in use before the edit"

    facts.set_fact(
        seeded,
        key="licence",
        text="Learner's permit only, no full licence",
        category=FactCategory.LICENCE,
        jurisdiction=Region.AU,
    )
    seeded.flush()

    assert (
        facts.resolve_from_facts(
            seeded,
            question="Do you hold a current driver's licence?",
            question_key="do you hold a current driver's licence",
            category=FactCategory.LICENCE,
            choices=["Yes", "No"],
            region=Region.AU,
        )
        is None
    ), "a confirmed answer derived from a corrected fact must stop answering"


def test_the_retraction_needs_a_new_confirmation_not_just_a_new_answer(
    seeded, monkeypatch
):
    """Re-deriving must not silently re-confirm.

    Otherwise correcting a fact would swap one unreviewed answer for another,
    which is the same failure with an extra step.
    """
    confirmed(seeded, monkeypatch)
    facts.set_fact(
        seeded,
        key="licence",
        text="Learner's permit only",
        category=FactCategory.LICENCE,
        jurisdiction=Region.AU,
    )
    seeded.flush()

    facts.resolve_from_facts(
        seeded,
        question="Do you hold a current driver's licence?",
        question_key="do you hold a current driver's licence",
        category=FactCategory.LICENCE,
        choices=["Yes", "No"],
        region=Region.AU,
    )
    seeded.flush()

    rows = seeded.exec(select(DerivedAnswer)).all()
    assert rows, "it should have re-derived"
    assert all(row.confirmed_at is None for row in rows), (
        "the re-derived answer must be unconfirmed"
    )


def test_a_cosmetic_reread_of_the_same_fact_keeps_the_confirmation(seeded, monkeypatch):
    """Invalidation must not fire on every save.

    If it did, the Facts page would retract every answer each time the user
    opened it, and confirming would become a chore rather than a one-off.
    """
    confirmed(seeded, monkeypatch)

    facts.set_fact(
        seeded,
        key="licence",
        text=LICENCE,
        category=FactCategory.LICENCE,
        jurisdiction=Region.AU,
    )
    seeded.flush()

    assert (
        facts.resolve_from_facts(
            seeded,
            question="Do you hold a current driver's licence?",
            question_key="do you hold a current driver's licence",
            category=FactCategory.LICENCE,
            choices=["Yes", "No"],
            region=Region.AU,
        )
        == "Yes"
    )


#: Modules allowed to touch DerivedAnswer, and why. Everything else must go
#: through facts.resolve_from_facts, which compares the fact hash before
#: trusting a cached answer — a second reader that skipped that check would
#: answer from evidence the user has since corrected.
DERIVED_ANSWER_READERS = {
    "backend/facts.py": "owns the hash check",
    "backend/models.py": "declares the table",
    # Display only. The Facts page shows what was derived and whether it has
    # gone stale; it never fills a form with one.
    "backend/api/schemas.py": "the wire shape for the Facts page",
    "backend/api/routers/core.py": "lists derivations for the Facts page",
}


def test_only_the_hash_checking_path_reads_a_derivation():
    """The hash check lives in one place, so it can only be bypassed in one.

    Scoped by an allowlist rather than a blanket ban: the API legitimately
    reads these to SHOW them, and the thing that must never appear is a new
    reader in the apply path that fills a form from a cached answer without
    asking whether its fact still says that.
    """
    import pathlib

    offenders = []
    for path in sorted(pathlib.Path("backend").rglob("*.py")):
        key = path.as_posix()
        if key in DERIVED_ANSWER_READERS:
            continue
        if "DerivedAnswer" in path.read_text(encoding="utf-8"):
            offenders.append(key)

    assert not offenders, (
        f"{offenders} read DerivedAnswer directly; answering must go through "
        "facts.resolve_from_facts, which checks the fact hash"
    )


def test_the_allowlist_does_not_outlive_its_entries():
    """An entry that stopped reading it hides the next one that starts."""
    import pathlib

    stale = [
        key
        for key in DERIVED_ANSWER_READERS
        if "DerivedAnswer" not in pathlib.Path(key).read_text(encoding="utf-8")
    ]
    assert not stale, f"remove from DERIVED_ANSWER_READERS: {stale}"


def test_the_derivation_goes_through_the_stubbable_seam():
    """An inline import bypasses the stub and makes a real call.

    The same mistake in documents/verify.py took the suite from 27 seconds to
    three and a half minutes, and here it would mean the rehearsal quietly
    calling a paid API.
    """
    import pathlib

    source = pathlib.Path("backend/facts.py").read_text(encoding="utf-8")
    code = "\n".join(
        line.split("#")[0]
        for line in source.splitlines()
        if not line.strip().startswith("#")
    )

    assert "llm.complete_json(" in code
    assert "import complete_json" not in code
