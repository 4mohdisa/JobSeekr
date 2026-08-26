"""Answer resolution, attacked rather than demonstrated.

``tests/test_answers.py`` shows the resolver working. This file tries to make it
lie. The rule throughout: **any case where it answers instead of abstaining is a
bug in the resolver, not in the test.** A parked job costs one Telegram round
trip; a guessed answer puts a false statement about work rights, licences or
salary in front of an employer under the user's name, and there is no undo.

Every case below is one the resolver got wrong before this file existed. The
attack classes, and what they were exploiting:

near-identical questions with opposite answers
    "part-time" against a stored "full-time" scores 88.9 — above the fuzzy
    threshold. Raising the threshold does not help: this is a confident wrong
    match, not a weak one.

negation and double negation
    "Do you NOT require visa sponsorship?" scored 94 against the un-negated
    stored entry and answered with its exact opposite.

two stored answers that disagree
    Any row whose text equalled the question was promoted to an exact match and
    returned immediately, so a second, contradictory entry was never consulted.

unit confusion
    "How many months" against a stored "How many years" scored 89 and returned
    a number in the wrong unit.

degenerate input
    ``fuzz.partial_ratio`` scores any substring at 100, so a form label of "a"
    matched a full question perfectly.

compound questions
    A stored pattern that is literally a substring of a two-part question scores
    100, and answering it fills a field that asked for something else too.

substituted subjects
    "Ruby" for "Python", "Master's" for "Bachelor's" — a one-word swap inside an
    otherwise identical question, scoring 90+.
"""

from __future__ import annotations

import pytest

from backend.apply.answers import (
    MAX_QUESTION_CHARS,
    Abstain,
    AbstainReason,
    Answer,
    resolve_answer,
)
from backend.models import AnswerBank, AnswerType, MatchType
from backend.seed import ANSWER_BANK_SEEDS

_next_id = iter(range(1, 100_000))


def row(
    pattern: str,
    value: str = "Yes",
    *,
    match: MatchType = MatchType.FUZZY,
    campaign_id: int | None = None,
) -> AnswerBank:
    return AnswerBank(
        id=next(_next_id),
        question_pattern=pattern,
        match_type=match,
        answer_value=value,
        answer_type=AnswerType.TEXT,
        campaign_id=campaign_id,
    )


def resolve(question, bank, campaign_id=None, choices=None):
    return resolve_answer(question, campaign_id, answers=bank, choices=choices)


def assert_abstains(question, bank, *, campaign_id=None, choices=None):
    outcome = resolve(question, bank, campaign_id, choices)
    assert isinstance(outcome, Abstain), (
        f"{question!r} was ANSWERED {outcome.value!r} "
        f"({outcome.match_type.value}/{outcome.confidence:.0f}) — it must abstain"
    )
    return outcome


def assert_answers(question, bank, expected, *, campaign_id=None, choices=None):
    outcome = resolve(question, bank, campaign_id, choices)
    assert isinstance(outcome, Answer), f"{question!r} abstained: {outcome.reason.value} {outcome.detail}"
    assert outcome.value == expected
    return outcome


# =========================================================================
# Near-identical questions with opposite answers
# =========================================================================

LICENCES = [
    row("Do you have a current driver's licence?", "Yes"),
    row("Do you have a forklift licence?", "No"),
]
AVAILABILITY = [
    row("Are you available for full-time work?", "Yes"),
    row("Are you available for part-time work?", "No"),
]


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Do you have a forklift licence?", "No"),
        ("Do you have a current driver's licence?", "Yes"),
    ],
)
def test_each_licence_resolves_to_its_own_answer(question, expected):
    assert_answers(question, LICENCES, expected)


@pytest.mark.parametrize(
    ("question", "bank"),
    [
        ("Do you have a forklift licence?", [row("Do you have a current driver's licence?", "Yes")]),
        ("Do you have a current driver's licence?", [row("Do you have a forklift licence?", "No")]),
        ("Do you hold an HR truck licence?", [row("Do you have a current driver's licence?", "Yes")]),
        ("Do you have a white card?", [row("Do you have a forklift licence?", "No")]),
    ],
)
def test_a_licence_is_never_answered_from_a_different_licence(question, bank):
    assert_abstains(question, bank)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Are you available for full-time work?", "Yes"),
        ("Are you available for part-time work?", "No"),
    ],
)
def test_each_availability_resolves_to_its_own_answer(question, expected):
    assert_answers(question, AVAILABILITY, expected)


@pytest.mark.parametrize(
    ("question", "stored"),
    [
        ("Are you available for part-time work?", "Are you available for full-time work?"),
        ("Are you available for casual work?", "Are you available for full-time work?"),
        ("Are you available to work weekends?", "Are you available for full-time work?"),
        ("Are you available for night shift?", "Are you available for day shift?"),
    ],
)
def test_availability_is_never_answered_from_a_different_basis(question, stored):
    """"part-time" against "full-time" scores 88.9 — inside the threshold."""
    assert_abstains(question, [row(stored, "Yes")])


# =========================================================================
# Negation and double negation
# =========================================================================


@pytest.mark.parametrize(
    ("question", "stored"),
    [
        ("Do you NOT require visa sponsorship?", "Do you require visa sponsorship?"),
        ("Do you not require no visa sponsorship?", "Do you require visa sponsorship?"),
        ("Have you never required visa sponsorship?", "Do you require visa sponsorship?"),
        ("Can you work without sponsorship?", "Do you require visa sponsorship?"),
        ("Are you unable to work full-time?", "Are you able to work full-time?"),
        ("Do you have no criminal convictions?", "Do you have any criminal convictions?"),
        ("Can you not start immediately?", "Can you start immediately?"),
        ("Don't you have full working rights?", "Do you have full working rights?"),
        ("Do you lack full working rights?", "Do you have full working rights?"),
    ],
)
def test_a_negated_question_is_never_answered_from_its_positive_form(question, stored):
    """The answer to the negation is the opposite one — a false declaration."""
    assert_abstains(question, [row(stored, "No")])


def test_negations_are_counted_not_cancelled():
    """A double negative is not treated as equivalent to the plain form.

    "Do you not require no visa sponsorship?" is grammatically a double
    negative. Treating it as the plain question means answering something nobody
    can reliably parse.
    """
    from backend.apply.answers import _negation_count

    assert _negation_count("do you require visa sponsorship") == 0
    assert _negation_count("do you not require visa sponsorship") == 1
    assert _negation_count("do you not require no visa sponsorship") == 2


def test_the_negation_scan_does_not_fire_on_words_that_merely_contain_one():
    """"notice" starts with "no"; "nonetheless" starts with "none"."""
    from backend.apply.answers import _negation_count

    assert _negation_count("what is your notice period") == 0
    assert _negation_count("how many nodes do you manage") == 0
    assert _negation_count("do you know the november release") == 0


# =========================================================================
# Two stored answers that disagree
# =========================================================================


def test_two_fuzzy_rows_that_disagree_abstain_even_when_one_matches_verbatim():
    """Textual identity confers confidence, not immunity.

    Any row whose text equalled the question used to be promoted to the exact
    tier and returned immediately, so the second row was never consulted. Two
    contradictory entries mean the bank is wrong, and answering from it puts a
    stale number on a real application.
    """
    bank = [
        row("How many years of Python experience do you have?", "5"),
        row("How many years of Python experience?", "7"),
    ]
    outcome = assert_abstains("How many years of Python experience do you have?", bank)
    assert outcome.reason == AbstainReason.AMBIGUOUS


def test_two_declared_exact_rows_that_disagree_abstain():
    bank = [
        row("Notice period", "2 weeks", match=MatchType.EXACT),
        row("Notice period", "4 weeks", match=MatchType.EXACT),
    ]
    outcome = assert_abstains("Notice period?", bank)
    assert outcome.reason == AbstainReason.AMBIGUOUS


def test_a_regex_row_and_a_fuzzy_row_that_disagree_abstain():
    bank = [
        row(r"salary expectation", "120000", match=MatchType.REGEX),
        row("What is your salary expectation?", "140000"),
    ]
    assert_abstains("What is your salary expectation?", bank)


def test_a_declared_exact_row_is_the_users_deliberate_override():
    """The one case where precedence still short-circuits.

    Declaring a row EXACT is an explicit act. It is how the user corrects a
    broad entry for one specific question, so it wins over a fuzzy row that
    disagrees — but not over another EXACT row, which is a contradiction.
    """
    bank = [
        row("Notice period", "2 weeks", match=MatchType.EXACT),
        row("What is your notice period?", "4 weeks"),
    ]
    assert_answers("Notice period?", bank, "2 weeks")


def test_a_row_scoped_to_another_campaign_is_discarded_not_outranked():
    """Ranking it equal made two unrelated campaigns look like a contradiction."""
    bank = [row("What is your salary expectation?", "130000", campaign_id=7)]
    assert_abstains("What is your salary expectation?", bank, campaign_id=99)
    assert_answers("What is your salary expectation?", bank, "130000", campaign_id=7)


# =========================================================================
# Unit and value confusion
# =========================================================================


@pytest.mark.parametrize(
    ("question", "stored", "value"),
    [
        (
            "How many months of Python experience do you have?",
            "How many years of Python experience do you have?",
            "5",
        ),
        (
            "How many years of Python experience do you have?",
            "How many months of Python experience do you have?",
            "60",
        ),
        ("What is your expected hourly rate?", "What is your expected annual salary?", "140000"),
        ("What is your expected annual salary?", "What is your expected hourly rate?", "75"),
        ("How many days notice do you require?", "How many weeks notice do you require?", "4"),
        ("What is your daily rate?", "What is your hourly rate?", "75"),
        ("How many kilometres can you commute?", "How many miles can you commute?", "20"),
        (
            "What is your maximum salary expectation?",
            "What is your minimum salary expectation?",
            "120000",
        ),
    ],
)
def test_a_number_is_never_returned_in_the_wrong_unit(question, stored, value):
    """"months" against "years" scores 89 and returned 5 — off by a factor of 12."""
    assert_abstains(question, [row(stored, value)])


def test_a_unit_word_does_not_fire_on_a_longer_word_containing_it():
    """"month" must not match inside "monthly", or every rate question conflicts."""
    from backend.apply.answers import _qualifier_signature

    assert "time_unit" not in _qualifier_signature("what is your monthly rate")
    assert _qualifier_signature("how many months")["time_unit"] == frozenset({"months"})


# =========================================================================
# Substituted subjects
# =========================================================================


@pytest.mark.parametrize(
    ("question", "stored"),
    [
        (
            "How many years of Ruby experience do you have?",
            "How many years of Python experience do you have?",
        ),
        ("Are you based in Western Australia?", "Are you based in South Australia?"),
        (
            "Do you have a Master's degree in Computer Science?",
            "Do you have a Bachelor's degree in Computer Science?",
        ),
    ],
)
def test_a_swapped_subject_is_a_different_question(question, stored):
    """No curated list scales to every language, framework and qualification.

    Detected structurally instead: each side has content the other lacks, and
    those leftovers are not spellings of one another.
    """
    assert_abstains(question, [row(stored, "5")])


def test_extra_words_are_not_a_substitution():
    """An addition is the same question with more words; the answer still applies."""
    bank = [row("Do you have full working rights?", "Yes")]
    assert_answers("Do you have full working rights in Australia?", bank, "Yes")


@pytest.mark.parametrize(
    "asked",
    [
        "Do you have a current drivers licence?",
        "Do you have a current driver license?",
        "Do you have a current Drivers Licence?",
        "Do you have a current driver’s licence?",
        "Do you have a current driver's licence",
    ],
)
def test_spelling_variants_are_not_mistaken_for_substitutions(asked):
    """"licence"/"license" scores 86; "ruby"/"python" scores 18."""
    assert_answers(asked, [row("Do you have a current driver's licence?", "Yes")], "Yes")


# =========================================================================
# Compound questions
# =========================================================================


def test_a_two_part_question_is_not_answered_from_an_entry_covering_one_part():
    """The stored pattern is a literal substring, so partial_ratio scores 100."""
    assert_abstains(
        "What is your notice period, and what is your salary expectation?",
        [row("What is your notice period?", "4 weeks")],
    )


def test_a_question_followed_by_its_opposite_abstains():
    assert_abstains(
        "Do you have full working rights in Australia? If not, do you require sponsorship?",
        [row("Do you have full working rights in Australia?", "Yes")],
    )


def test_a_trailing_instruction_is_not_a_second_question():
    """"Please attach evidence" asks nothing, so the entry still applies."""
    assert_answers(
        "Do you have full working rights in Australia? Please attach evidence.",
        [row("Do you have full working rights in Australia?", "Yes")],
        "Yes",
    )


def test_a_wh_question_ending_in_do_you_have_is_still_one_question():
    """Counting openers rather than clauses broke every "How many ... do you have?"."""
    from backend.apply.answers import _clause_count

    assert _clause_count("how many years of python experience do you have") == 1
    assert _clause_count("how many years of python experience") == 1
    assert _clause_count("what is your notice period, and what is your salary expectation") == 2


# =========================================================================
# Degenerate input
# =========================================================================


@pytest.mark.parametrize(
    "question",
    [
        "",
        "     ",
        None,
        "?",
        "a",
        "ok",
        "z" * 5000,
        "Do you have full working rights in Australia? " + "x" * 20_000,
        "Avez-vous le droit de travailler en Australie ?",
        "您在澳大利亚有完全的工作权吗？",
        "هل لديك حقوق عمل كاملة في أستراليا؟",
        "🚗 licence?",
        "Ｄｏ ｙｏｕ ｈａｖｅ ｆｕｌｌ ｗｏｒｋｉｎｇ ｒｉｇｈｔｓ？",
    ],
)
def test_degenerate_input_never_produces_an_answer(question):
    """A one-character label scored 100 against a full question: partial_ratio
    treats any substring as a perfect match."""
    assert_abstains(question, [row("Do you have full working rights in Australia?", "Yes")])


def test_the_length_guard_does_not_break_short_stored_patterns():
    """partial_ratio is kept deliberately — "notice period" shares 23% of its
    length with the question below and must still match."""
    assert_answers(
        "What is your notice period if you were to accept an offer?",
        [row("Notice period", "4 weeks")],
        "4 weeks",
    )


def test_the_page_length_guard_is_generous_enough_for_a_real_question():
    long_but_real = (
        "Please confirm that you have read and understood the position description "
        "and that you meet the requirements listed in it. Do you have full working "
        "rights in Australia?"
    )
    assert len(long_but_real) < MAX_QUESTION_CHARS


# =========================================================================
# Surface noise must NOT cause an abstention
# =========================================================================


@pytest.mark.parametrize(
    "asked",
    [
        "DO YOU HAVE FULL WORKING RIGHTS IN AUSTRALIA?",
        "  Do   you  have full working   rights in Australia ?  ",
        "Do you have full working rights in Australia",
        "3. Do you have full working rights in Australia?",
        "• Do you have full working rights in Australia?",
        "Do you have full workign rights in Australia?",
        "Do you have ful working rights in Austrlia?",
        "Do you have full working rights in Australia?\t",
        "Do you have full working rights in Australia? *",
        "Do you have full working\nrights in Australia?",
        "Do you have full working rights in Australia?",
        "<p>Do you have full working rights in Australia?</p>",
    ],
)
def test_formatting_noise_still_resolves(asked):
    """Abstaining is safe, but abstaining on everything makes the agent useless."""
    assert_answers(asked, [row("Do you have full working rights in Australia?", "Yes")], "Yes")


# =========================================================================
# The shipped answer bank must keep working
# =========================================================================


def seeded_bank() -> list[AnswerBank]:
    return [
        AnswerBank(
            id=next(_next_id),
            question_pattern=seed.question_pattern,
            match_type=seed.match_type,
            answer_value="stored",
            answer_type=AnswerType.TEXT,
            campaign_id=None,
        )
        for seed in ANSWER_BANK_SEEDS
    ]


@pytest.mark.parametrize(
    "seed",
    [s for s in ANSWER_BANK_SEEDS if s.match_type != MatchType.REGEX],
    ids=lambda s: s.question_pattern[:40],
)
def test_every_seeded_question_still_resolves_against_its_own_pattern(seed):
    """The guards must not make the shipped bank unable to answer itself."""
    assert_answers(seed.question_pattern, seeded_bank(), "stored")


@pytest.mark.parametrize(
    "asked",
    [
        "Do you have full working rights in Australia?",
        "Do you have a current Australian driver's licence?",
        "Do you have a current drivers license?",
        "Do you hold a valid driver licence?",
        "Do you have your own reliable transport?",
        "What is your notice period?",
        "What is the earliest date you can start?",
        "Are you willing to relocate?",
        "What is your highest level of education?",
        "Are you currently residing in Australia?",
        "Can you provide two contactable referees?",
        "Do you have a current police check?",
        "Do you have a National Police Certificate?",
        "Do you have a WWCC?",
        "What is your visa status?",
        "Are you an Australian citizen or permanent resident?",
        "What are your salary expectations?",
        "Do you have an ABN?",
    ],
)
def test_real_form_phrasings_still_resolve_against_the_shipped_bank(asked):
    """Measured, not assumed: 26 of 34 realistic phrasings resolve.

    The two the guards newly abstain on are synonym swaps — "commence" for
    "start", "living" for "residing" — which are structurally identical to
    "Ruby" for "Python". That is the accepted cost: each one is a single
    Telegram round trip, after which the answer is saved and the phrasing
    resolves exactly from then on. Guessing is not recoverable.
    """
    assert_answers(asked, seeded_bank(), "stored")


def test_the_seeded_visa_and_sponsorship_rows_do_not_leak_into_each_other():
    """Their answers are opposites, so a leak is a false legal declaration.

    The seeds were written to avoid this — the visa-status regex carries a
    negative lookahead for "sponsor", and sponsorship has its own row. This
    pins the property rather than the wording, so rewriting either pattern
    cannot quietly reintroduce the leak.
    """
    bank = seeded_bank()
    for entry in bank:
        pattern = entry.question_pattern
        # The visa-status pattern MENTIONS "sponsor" inside its negative
        # lookahead, so match on the lookahead first.
        if "(?!.*sponsor)" in pattern:
            entry.answer_value = "VISA STATUS"
        elif "sponsor" in pattern:
            entry.answer_value = "SPONSORSHIP"
        elif "working rights" in pattern.casefold():
            entry.answer_value = "WORKING RIGHTS"

    assert_answers("Do you require visa sponsorship?", bank, "SPONSORSHIP")
    assert_answers("What is your visa status?", bank, "VISA STATUS")
    assert_answers("Do you have full working rights in Australia?", bank, "WORKING RIGHTS")
    # And the negated form of each is nobody's question to answer.
    assert_abstains("Do you NOT require visa sponsorship?", bank)
