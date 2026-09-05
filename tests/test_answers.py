"""Answer resolution — the most detailed test suite in this project, deliberately.

A wrong answer here is a false statement about work rights, licences or salary
made to an employer under the user's name. The tests that matter most are the
ones asserting the module REFUSES to answer: "forklift licence" must not be
served from a driver's licence entry, and a sponsorship question must never be
answered from a working-rights entry, because those two have opposite answers.
"""

from __future__ import annotations

import pytest

from backend.apply.answers import (
    AMBIGUITY_MARGIN,
    FUZZY_THRESHOLD,
    Abstain,
    AbstainReason,
    Answer,
    coerce_to_choices,
    normalise_question,
    resolve_all,
    resolve_answer,
)
from backend.models import AnswerBank, AnswerType, MatchType

_next_id = iter(range(1, 10_000))


def row(
    pattern: str,
    value: str = "Yes",
    *,
    match: MatchType = MatchType.FUZZY,
    campaign_id: int | None = None,
    answer_type: AnswerType = AnswerType.TEXT,
    choices: list[str] | None = None,
) -> AnswerBank:
    return AnswerBank(
        id=next(_next_id),
        question_pattern=pattern,
        match_type=match,
        answer_value=value,
        answer_type=answer_type,
        campaign_id=campaign_id,
        choices=choices,
    )


def resolve(question, bank, campaign_id=None, choices=None):
    return resolve_answer(question, campaign_id, answers=bank, choices=choices)


# ------------------------------------------------------------- normalisation


@pytest.mark.parametrize(
    "raw",
    [
        "Do you have a current driver's licence?",
        "  Do you have a current driver's licence?  ",
        "1. Do you have a current driver's licence?",
        "(2) Do you have a current driver's licence",
        "- Do you have a current driver's licence!",
        "DO YOU HAVE A CURRENT DRIVER'S LICENCE?",
    ],
)
def test_question_numbering_case_and_punctuation_are_normalised_away(raw):
    assert normalise_question(raw) == "do you have a current driver's licence"


# ------------------------------------------------------- the dangerous misses


def test_forklift_licence_does_NOT_resolve_from_a_drivers_licence_answer():
    """The classic dangerous fuzzy hit. These are different licences."""
    bank = [row("Do you have a current driver's licence?", "Yes")]
    outcome = resolve("Do you have a forklift licence?", bank)

    assert isinstance(outcome, Abstain), f"expected abstain, got {outcome}"
    assert outcome.reason == AbstainReason.NO_MATCH


def test_the_right_licence_wins_when_both_are_in_the_bank():
    bank = [
        row("Do you have a current driver's licence?", "Yes"),
        row("Do you have a forklift licence?", "No"),
    ]
    driver = resolve("Do you have a current driver's licence?", bank)
    forklift = resolve("Do you have a forklift licence?", bank)

    assert isinstance(driver, Answer) and driver.value == "Yes"
    assert isinstance(forklift, Answer) and forklift.value == "No"


def test_sponsorship_and_working_rights_never_leak_across_each_other():
    """Opposite answers. A leak here misstates the user's right to work."""
    bank = [row("Do you have full working rights in Australia?", "Yes")]
    outcome = resolve("Do you require visa sponsorship?", bank)

    assert isinstance(outcome, Abstain), (
        f"sponsorship answered 'Yes' from working rights: {outcome}"
    )


def test_working_rights_is_not_answered_from_a_sponsorship_entry_either():
    bank = [row("Do you require visa sponsorship to work in Australia?", "No")]
    outcome = resolve("Do you have full working rights in Australia?", bank)
    assert isinstance(outcome, Abstain)


def test_both_polarities_present_resolve_to_their_own_answers():
    bank = [
        row("Do you have full working rights in Australia?", "Yes"),
        row("Do you require visa sponsorship?", "No"),
    ]
    rights = resolve("Do you have full working rights in Australia?", bank)
    sponsor = resolve("Do you require visa sponsorship?", bank)
    assert isinstance(rights, Answer) and rights.value == "Yes"
    assert isinstance(sponsor, Answer) and sponsor.value == "No"


# ------------------------------------------------------------------ ambiguity


def test_two_near_equal_matches_that_disagree_abstain_rather_than_pick_the_best():
    bank = [
        row("How many years of Python experience do you have?", "5"),
        row("How many years of Java experience do you have?", "0"),
    ]
    outcome = resolve("How many years of Ruby experience do you have?", bank)
    assert isinstance(outcome, Abstain)
    assert outcome.reason in {AbstainReason.AMBIGUOUS, AbstainReason.NO_MATCH}


def test_two_near_equal_matches_that_AGREE_are_safe_to_answer():
    """Ambiguity only matters when the candidates disagree."""
    bank = [
        row("Do you have full working rights in Australia?", "Yes"),
        row("Do you have full working rights in Aus?", "Yes"),
    ]
    outcome = resolve("Do you have full working rights in Australia?", bank)
    assert isinstance(outcome, Answer) and outcome.value == "Yes"


def test_duplicate_regex_rows_that_disagree_abstain():
    bank = [
        row(r"police\s+check", "Yes", match=MatchType.REGEX),
        row(r"police", "No", match=MatchType.REGEX),
    ]
    outcome = resolve("Do you have a current police check?", bank)
    assert isinstance(outcome, Abstain)
    assert outcome.reason == AbstainReason.AMBIGUOUS


# ------------------------------------------------------------- misspellings


@pytest.mark.parametrize(
    "asked",
    [
        "Do you have a current drivers licence?",
        "Do you have a current driver license?",
        "Do you have a current Drivers Licence?",
        "do you have a current driver's licence",
    ],
)
def test_misspellings_and_variants_resolve_to_the_same_entry(asked):
    bank = [row("Do you have a current driver's licence?", "Yes")]
    outcome = resolve(asked, bank)
    assert isinstance(outcome, Answer), f"{asked!r} did not resolve: {outcome}"
    assert outcome.value == "Yes"


# ------------------------------------------------------------------- scoping


def test_campaign_scoped_row_beats_a_global_row():
    bank = [
        row("What is your salary expectation?", "90000", campaign_id=None),
        row("What is your salary expectation?", "130000", campaign_id=7),
    ]
    outcome = resolve("What is your salary expectation?", bank, campaign_id=7)
    assert isinstance(outcome, Answer)
    assert outcome.value == "130000"


def test_a_different_campaigns_row_does_not_win():
    bank = [
        row("What is your salary expectation?", "90000", campaign_id=None),
        row("What is your salary expectation?", "130000", campaign_id=7),
    ]
    outcome = resolve("What is your salary expectation?", bank, campaign_id=99)
    assert isinstance(outcome, Answer)
    assert outcome.value == "90000"


# --------------------------------------------------------------- blank rows


def test_a_seeded_but_unanswered_row_abstains_with_its_own_reason():
    """The 21 seeded questions start blank. Blank must never mean empty string."""
    bank = [row("What is your notice period?", "")]
    outcome = resolve("What is your notice period?", bank)

    assert isinstance(outcome, Abstain)
    assert outcome.reason == AbstainReason.BLANK_ANSWER
    assert "notice period" in outcome.detail


def test_a_whitespace_only_answer_is_also_blank():
    bank = [row("What is your notice period?", "   ")]
    outcome = resolve("What is your notice period?", bank)
    assert isinstance(outcome, Abstain) and outcome.reason == AbstainReason.BLANK_ANSWER


# ------------------------------------------------------------ invalid regex


def test_an_invalid_regex_row_does_not_break_resolution_of_others():
    bank = [
        row("[unclosed", "Broken", match=MatchType.REGEX),
        row("What is your notice period?", "4 weeks"),
    ]
    outcome = resolve("What is your notice period?", bank)
    assert isinstance(outcome, Answer)
    assert outcome.value == "4 weeks"


# ---------------------------------------------------------- choice coercion


def test_yes_maps_onto_a_yes_no_choice_list():
    assert coerce_to_choices("Yes", ["Yes", "No"]) == "Yes"
    assert coerce_to_choices("no", ["Yes", "No"]) == "No"


def test_yes_does_NOT_map_onto_visa_status_options():
    """Answering 'Yes' to a citizenship dropdown would assert a visa status."""
    assert (
        coerce_to_choices(
            "Yes", ["Australian Citizen", "Permanent Resident", "Visa holder"]
        )
        is None
    )


def test_an_answer_that_cannot_be_mapped_abstains_with_invalid_choice():
    bank = [row("What is your work rights status?", "Yes")]
    outcome = resolve(
        "What is your work rights status?",
        bank,
        choices=["Australian Citizen", "Permanent Resident", "Visa holder"],
    )
    assert isinstance(outcome, Abstain)
    assert outcome.reason == AbstainReason.INVALID_CHOICE
    assert outcome.candidates == [
        "Australian Citizen",
        "Permanent Resident",
        "Visa holder",
    ]


def test_an_exact_choice_match_is_used_verbatim():
    bank = [row("Citizenship status?", "Australian Citizen")]
    outcome = resolve(
        "Citizenship status?",
        bank,
        choices=["Australian Citizen", "Permanent Resident"],
    )
    assert isinstance(outcome, Answer) and outcome.value == "Australian Citizen"


# ------------------------------------------------------------------- no match


def test_a_completely_unknown_question_abstains():
    bank = [row("Do you have a current driver's licence?", "Yes")]
    outcome = resolve("What is your favourite colour?", bank)
    assert isinstance(outcome, Abstain) and outcome.reason == AbstainReason.NO_MATCH


def test_an_empty_bank_abstains():
    outcome = resolve("Anything at all?", [])
    assert isinstance(outcome, Abstain)


def test_an_empty_question_abstains():
    outcome = resolve("", [row("x", "y")])
    assert isinstance(outcome, Abstain)


# -------------------------------------------------------------- resolve_all


def test_resolve_all_returns_every_abstention_not_just_the_first():
    bank = [row("Do you have a current driver's licence?", "Yes")]
    resolved, abstentions = resolve_all(
        [
            "Do you have a current driver's licence?",
            "What is your notice period?",
            "Do you hold a forklift licence?",
        ],
        answers=bank,
    )
    assert len(resolved) == 1
    assert len(abstentions) == 2, [a.question for a in abstentions]


def test_resolve_all_accepts_field_objects_with_choices():
    class Field:
        def __init__(self, label, choices=None):
            self.label = label
            self.choices = choices

    bank = [row("Do you have full working rights in Australia?", "Yes")]
    resolved, abstentions = resolve_all(
        [Field("Do you have full working rights in Australia?", ["Yes", "No"])],
        answers=bank,
    )
    assert not abstentions
    assert next(iter(resolved.values())).value == "Yes"


# --------------------------------------------------------------- invariants


def test_module_has_no_browser_dependency():
    """Purity is what makes this logic testable; a Playwright import breaks it.

    Checked against the import graph rather than the file text — the module
    docstring legitimately mentions Playwright to explain the rule.
    """
    import ast
    import pathlib

    import backend.apply.answers as module

    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "playwright" not in imported, imported


def test_thresholds_are_documented_constants():
    assert FUZZY_THRESHOLD >= 80, "a loose threshold is how wrong answers get sent"
    assert AMBIGUITY_MARGIN > 0
