"""Resolve a screening question to a verified answer, or abstain.

Claude.md hard rule 2: screening answers come only from the answer bank. If a
question cannot be resolved, abstain, park the job, ask the user via Telegram,
save the answer, retry. Never guess. An ambiguous fuzzy match is an abstention.

**Abstaining is the correct outcome, not a failure.** A parked job costs one
Telegram round trip. A guessed answer puts a false statement about work rights,
licences or salary expectations in front of an employer under the user's name,
and there is no undo. No caller may substitute a default for an ``Abstain``.

Everything here is a pure function with no browser dependency — the module must
never import Playwright. That is what makes the decision logic exhaustively
testable without a page object, and the tests in ``tests/test_answers.py`` are
the most detailed in the project because this is where a wrong answer becomes a
lie told on the user's behalf.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from rapidfuzz import fuzz

from backend.logging_setup import get_logger
from backend.models import AnswerBank, AnswerType, MatchType

log = get_logger(__name__)

__all__ = [
    "AMBIGUITY_MARGIN",
    "FUZZY_THRESHOLD",
    "Abstain",
    "AbstainReason",
    "Answer",
    "coerce_to_choices",
    "load_answers",
    "normalise_question",
    "resolve_all",
    "resolve_answer",
]


FUZZY_THRESHOLD = 88.0
"""Minimum similarity (0-100) for a fuzzy match to be considered at all.

Deliberately high. Screening questions are short and share most of their words
("Do you have a current X licence?"), so the similarity between two genuinely
different questions is already large — 'forklift licence' against 'driver's
licence' scores in the seventies. Anything below this is not a near-miss worth
guessing at, it is a different question.
"""

AMBIGUITY_MARGIN = 6.0
"""How far the best fuzzy match must beat the runner-up to be trusted.

If two stored answers score within this of each other and disagree about the
answer, the honest state is "I do not know which of these you meant", and the
module abstains rather than taking the top one. Picking the best of two
plausible matches is exactly the failure mode this margin exists to prevent.
"""


class AbstainReason(str, Enum):
    NO_MATCH = "no_match"
    AMBIGUOUS = "ambiguous"
    BLANK_ANSWER = "blank_answer"
    TYPE_MISMATCH = "type_mismatch"
    INVALID_CHOICE = "invalid_choice"


@dataclass(frozen=True)
class Answer:
    """A resolved answer, with enough provenance to audit it afterwards."""

    value: str
    source_row_id: int | None
    match_type: MatchType
    confidence: float
    question: str
    answer_type: AnswerType = AnswerType.TEXT


@dataclass(frozen=True)
class Abstain:
    """The safe outcome. Callers must park the job, never substitute a default."""

    question: str
    reason: AbstainReason
    detail: str = ""
    candidates: list[str] = field(default_factory=list)


Resolution = Answer | Abstain


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------

_LEADING_NUMBER = re.compile(r"^\s*(?:\(?\d{1,2}[.):]|[-*•])\s*")
_WHITESPACE = re.compile(r"\s+")
_TRAILING_PUNCT = re.compile(r"[\s?:.!*]+$")
_PUNCT = re.compile(r"[^\w\s']")


def normalise_question(text: str | None) -> str:
    """Reduce a question to a comparable form.

    Strips the numbering forms real forms use ("1.", "(2)", "-"), collapses
    whitespace, drops trailing punctuation, and casefolds. Apostrophes are kept
    so "driver's" and "drivers" stay distinguishable to the exact tier while
    still matching under fuzz.
    """
    if not text:
        return ""
    cleaned = _LEADING_NUMBER.sub("", str(text))
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    cleaned = _TRAILING_PUNCT.sub("", cleaned)
    return cleaned.casefold()


def _loose(text: str) -> str:
    """Aggressive normalisation for fuzzy comparison only."""
    return _WHITESPACE.sub(" ", _PUNCT.sub(" ", text.replace("'", ""))).strip()


# --------------------------------------------------------------------------
# Negation
# --------------------------------------------------------------------------

# Question pairs whose correct answers are OPPOSITE. Fuzzy matching alone
# conflates them: "Do you require visa sponsorship?" and "Do you have full
# working rights?" share most of their vocabulary but a "yes" to one is a "no"
# to the other. Leaking an answer across such a pair is not a near-miss, it is
# a false statement about the user's right to work.
_POLARITY_MARKERS: tuple[tuple[str, ...], ...] = (
    ("require sponsorship", "need sponsorship", "require visa", "need visa", "require a visa"),
    ("full working rights", "unrestricted work rights", "right to work", "work rights"),
)


def _polarity_signature(text: str) -> frozenset[int]:
    """Which polarity-sensitive concepts a question touches."""
    return frozenset(
        index
        for index, markers in enumerate(_POLARITY_MARKERS)
        if any(marker in text for marker in markers)
    )


def _polarity_conflict(question: str, pattern: str) -> bool:
    """True when the two texts sit on opposite sides of a polarity pair."""
    left = _polarity_signature(question)
    right = _polarity_signature(pattern)
    if not left or not right:
        return False
    return left != right


# --------------------------------------------------------------------------
# Choice coercion
# --------------------------------------------------------------------------

_YES = {"yes", "y", "true", "1"}
_NO = {"no", "n", "false", "0"}


def coerce_to_choices(value: str, choices: Sequence[str] | None) -> str | None:
    """Map an answer onto a form's offered options, or None if unclear.

    Only an unambiguous mapping is accepted. "Yes" onto ["Yes", "No"] is
    obvious; "Yes" onto ["Australian Citizen", "Permanent Resident", "Visa
    holder"] is not an answer to that question at all, and returning a
    best-effort pick would silently assert a visa status.
    """
    if not choices:
        return value
    folded = value.strip().casefold()

    exact = [choice for choice in choices if choice.strip().casefold() == folded]
    if len(exact) == 1:
        return exact[0]

    if folded in _YES or folded in _NO:
        wanted = _YES if folded in _YES else _NO
        hits = [choice for choice in choices if choice.strip().casefold() in wanted]
        if len(hits) == 1:
            return hits[0]
        return None

    contains = [choice for choice in choices if folded and folded in choice.strip().casefold()]
    if len(contains) == 1:
        return contains[0]

    return None


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def load_answers(session: Any, campaign_id: int | None) -> list[AnswerBank]:
    """Load the answer-bank rows in scope: this campaign's, plus the globals."""
    from sqlmodel import or_, select

    return list(
        session.exec(
            select(AnswerBank).where(
                or_(AnswerBank.campaign_id == campaign_id, AnswerBank.campaign_id.is_(None))
            )
        ).all()
    )


def _scope_rank(row: AnswerBank, campaign_id: int | None) -> int:
    """Campaign-scoped rows beat global ones. Higher wins."""
    return 1 if (campaign_id is not None and row.campaign_id == campaign_id) else 0


def _blank(row: AnswerBank) -> bool:
    return not (row.answer_value or "").strip()


def _make_answer(
    row: AnswerBank, question: str, match_type: MatchType, confidence: float
) -> Answer:
    return Answer(
        value=row.answer_value.strip(),
        source_row_id=row.id,
        match_type=match_type,
        confidence=confidence,
        question=question,
        answer_type=row.answer_type,
    )


def _finalise(
    row: AnswerBank,
    *,
    question: str,
    match_type: MatchType,
    confidence: float,
    choices: Sequence[str] | None,
) -> Resolution:
    """Apply the blank and choice checks that every tier shares."""
    if _blank(row):
        return Abstain(
            question=question,
            reason=AbstainReason.BLANK_ANSWER,
            detail=(
                f"answer bank row {row.id} matches but has no answer yet "
                f"({row.question_pattern!r})"
            ),
        )

    answer = _make_answer(row, question, match_type, confidence)
    if choices:
        mapped = coerce_to_choices(answer.value, choices)
        if mapped is None:
            return Abstain(
                question=question,
                reason=AbstainReason.INVALID_CHOICE,
                detail=f"stored answer {answer.value!r} does not map onto {list(choices)}",
                candidates=list(choices),
            )
        answer = Answer(
            value=mapped,
            source_row_id=answer.source_row_id,
            match_type=answer.match_type,
            confidence=answer.confidence,
            question=answer.question,
            answer_type=answer.answer_type,
        )
    return answer


def resolve_answer(
    question_text: str,
    campaign_id: int | None = None,
    *,
    answers: Sequence[AnswerBank],
    choices: Sequence[str] | None = None,
) -> Resolution:
    """Resolve one screening question. Never returns None; never guesses.

    Tiers, in order: exact, regex, fuzzy. Campaign-scoped rows beat global ones
    within every tier.
    """
    question = normalise_question(question_text)
    if not question:
        return Abstain(question=question_text or "", reason=AbstainReason.NO_MATCH)

    # --- tier 1: exact ---------------------------------------------------
    exact = [
        row
        for row in answers
        if row.match_type == MatchType.EXACT
        and normalise_question(row.question_pattern) == question
    ]
    # An exact-text equality also satisfies a fuzzy row; treat it as exact.
    exact += [
        row
        for row in answers
        if row.match_type != MatchType.EXACT
        and normalise_question(row.question_pattern) == question
    ]
    if exact:
        exact.sort(key=lambda row: _scope_rank(row, campaign_id), reverse=True)
        return _finalise(
            exact[0],
            question=question,
            match_type=MatchType.EXACT,
            confidence=100.0,
            choices=choices,
        )

    # --- tier 2: regex ---------------------------------------------------
    regex_hits: list[AnswerBank] = []
    for row in answers:
        if row.match_type != MatchType.REGEX:
            continue
        try:
            pattern = re.compile(row.question_pattern, re.IGNORECASE)
        except re.error as exc:
            # A bad pattern is a data problem in one row; it must not stop the
            # other rows from resolving this question.
            log.warning(
                "answer_bank_invalid_regex",
                row_id=row.id,
                pattern=row.question_pattern,
                error=str(exc),
            )
            continue
        if pattern.search(question_text) or pattern.search(question):
            regex_hits.append(row)

    if regex_hits:
        regex_hits.sort(key=lambda row: _scope_rank(row, campaign_id), reverse=True)
        top_scope = _scope_rank(regex_hits[0], campaign_id)
        same_scope = [r for r in regex_hits if _scope_rank(r, campaign_id) == top_scope]
        distinct = {(r.answer_value or "").strip().casefold() for r in same_scope}
        if len(distinct) > 1:
            return Abstain(
                question=question,
                reason=AbstainReason.AMBIGUOUS,
                detail=f"{len(same_scope)} regex rows match and disagree",
                candidates=[r.question_pattern for r in same_scope],
            )
        return _finalise(
            same_scope[0],
            question=question,
            match_type=MatchType.REGEX,
            confidence=100.0,
            choices=choices,
        )

    # --- tier 3: fuzzy ---------------------------------------------------
    scored: list[tuple[float, AnswerBank]] = []
    loose_question = _loose(question)
    for row in answers:
        if row.match_type == MatchType.REGEX:
            continue
        pattern = normalise_question(row.question_pattern)
        if _polarity_conflict(question, pattern):
            # Opposite-polarity question: not a weak match, a wrong one.
            log.debug(
                "polarity_conflict_skipped", question=question, pattern=pattern
            )
            continue
        score = max(
            fuzz.token_sort_ratio(loose_question, _loose(pattern)),
            fuzz.partial_ratio(loose_question, _loose(pattern)),
        )
        if score >= FUZZY_THRESHOLD:
            scored.append((float(score), row))

    if not scored:
        return Abstain(
            question=question,
            reason=AbstainReason.NO_MATCH,
            detail=f"no answer bank entry above {FUZZY_THRESHOLD}",
        )

    scored.sort(key=lambda pair: (pair[0], _scope_rank(pair[1], campaign_id)), reverse=True)
    best_score, best_row = scored[0]

    contenders = [
        (score, row)
        for score, row in scored
        if best_score - score <= AMBIGUITY_MARGIN
        and _scope_rank(row, campaign_id) == _scope_rank(best_row, campaign_id)
    ]
    distinct_answers = {(row.answer_value or "").strip().casefold() for _, row in contenders}
    if len(distinct_answers) > 1:
        return Abstain(
            question=question,
            reason=AbstainReason.AMBIGUOUS,
            detail=(
                f"{len(contenders)} entries within {AMBIGUITY_MARGIN} points "
                f"of each other disagree (top score {best_score:.1f})"
            ),
            candidates=[row.question_pattern for _, row in contenders],
        )

    return _finalise(
        best_row,
        question=question,
        match_type=MatchType.FUZZY,
        confidence=best_score,
        choices=choices,
    )


def resolve_all(
    questions: Sequence[Any],
    campaign_id: int | None = None,
    *,
    answers: Sequence[AnswerBank],
) -> tuple[dict[str, Answer], list[Abstain]]:
    """Resolve every question, returning ALL abstentions rather than the first.

    The apply flow needs all-or-nothing semantics: one abstention aborts the
    application, and the user should be asked every outstanding question in one
    Telegram round trip rather than discovering them one job at a time.

    ``questions`` may be plain strings or objects with ``label``/``text`` and
    optional ``choices``.
    """
    resolved: dict[str, Answer] = {}
    abstentions: list[Abstain] = []

    for item in questions:
        if isinstance(item, str):
            label, choices = item, None
        else:
            label = getattr(item, "label", None) or getattr(item, "text", "") or str(item)
            choices = getattr(item, "choices", None)

        outcome = resolve_answer(label, campaign_id, answers=answers, choices=choices)
        if isinstance(outcome, Abstain):
            abstentions.append(outcome)
        else:
            resolved[label] = outcome

    if abstentions:
        log.warning(
            "answers_abstained",
            count=len(abstentions),
            questions=[a.question for a in abstentions][:8],
        )
    return resolved, abstentions
