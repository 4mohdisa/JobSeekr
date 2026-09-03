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
from backend.models import AnswerBank, AnswerType, MatchType, Region

log = get_logger(__name__)

__all__ = [
    "AMBIGUITY_MARGIN",
    "FUZZY_THRESHOLD",
    "Abstain",
    "AbstainReason",
    "Answer",
    "coerce_to_choices",
    "load_answers",
    "matching_rows",
    "normalise_question",
    "question_key",
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


MIN_QUESTION_CHARS = 8
MIN_QUESTION_TOKENS = 2
"""Below this, a string is not a screening question.

``fuzz.partial_ratio`` scores a substring at 100, so a stray label of "a"
matched "Do you have full working rights in Australia?" perfectly and answered
Yes. Length is the honest guard: partial_ratio is deliberately kept — a stored
pattern of "notice period" must still match "What is your notice period if you
were to accept an offer?", which shares only 23% of its length.
"""

MAX_QUESTION_CHARS = 400
"""Above this, the text is a page rather than a question.

Matching a fragment of a wall of text is guessing about which of the several
questions inside it is being asked.
"""


class AbstainReason(str, Enum):
    NO_MATCH = "no_match"
    AMBIGUOUS = "ambiguous"
    BLANK_ANSWER = "blank_answer"
    TYPE_MISMATCH = "type_mismatch"
    INVALID_CHOICE = "invalid_choice"
    CROSS_REGION = "cross_region"
    """The only candidate answers belong to a different country.

    Work rights, tax numbers, licences and notice periods are different
    questions in AU and NZ. The trans-Tasman arrangement makes the wrong answer
    *plausible* rather than obviously absurd, which is worse — a plausible wrong
    answer about work rights goes onto a real application and is not caught.
    """


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

    source_row_id: int | None = None
    """The bank row that matched but could not answer (BLANK_ANSWER, INVALID_CHOICE).

    Carried so the escalation can fill *that* row when the user replies. Writing
    the reply under the form's own wording instead would leave the matched row
    blank and add a second row saying something different about the same
    question — and the two then tie in the candidate pool and abstain as
    AMBIGUOUS, so the job re-parks forever on a question it has been told the
    answer to. None when nothing matched at all; then there is no row to fill.
    """


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
# Conflict detection: when a high fuzzy score is a WRONG match, not a near one
# --------------------------------------------------------------------------

# Screening questions are short and share most of their words, so two questions
# with opposite answers routinely score in the nineties: "Are you available for
# part-time work?" against "Are you available for full-time work?" scores 88.9,
# above the threshold, and answered Yes from the wrong row. Raising the
# threshold does not fix that — it is not a weak match, it is a confident wrong
# one — so these two checks disqualify a row outright rather than scoring it.


_NEGATION = re.compile(
    r"\b(?:"
    r"\w+n't"  # can't, don't, doesn't, haven't, isn't, won't
    r"|not|never|none|neither|nor|cannot"
    r"|no"  # "no criminal convictions"; \b keeps "notice" out
    r"|without|lack|lacks|lacking|unable|unwilling|ineligible"
    r"|non-?\w+"
    r")\b"
)


def _negation_count(text: str) -> int:
    """How many negations a question carries.

    Counted, not parity-checked. "Do you not require no visa sponsorship?" is
    grammatically a double negative, but treating it as equivalent to the plain
    form means answering a question nobody can reliably parse. Any difference in
    count is a mismatch.
    """
    return len(_NEGATION.findall(text))


# Families of mutually exclusive qualifiers. Within a family, naming a different
# member is not a near-miss — it is a different question whose answer is often
# the opposite. Word boundaries matter: "month" must not fire on "monthly", and
# "no" must not fire on "notice".
#
# This is a curated list and therefore incomplete by construction. It is a
# second line of defence, not the first: the high fuzzy threshold is what
# catches the families nobody has thought of yet, and abstention is the default
# when neither fires.
_QUALIFIER_FAMILIES: tuple[tuple[str, dict[str, str]], ...] = (
    (
        "work_eligibility",
        {
            "sponsorship": r"sponsorship|sponsor(?:ed|ing)?|requires? a visa|need a visa",
            "working_rights": r"working rights|work rights|right to work|unrestricted work",
        },
    ),
    (
        "employment_basis",
        {
            "full_time": r"full[- ]?time",
            "part_time": r"part[- ]?time",
            "casual": r"casual",
            "contract": r"contract(?:ing|or)?",
            "permanent": r"permanent",
            "temporary": r"temporary|temp|fixed[- ]?term",
        },
    ),
    (
        "time_unit",
        {
            "years": r"years?",
            "months": r"months?",
            "weeks": r"weeks?",
            "days": r"days?",
            "hours": r"hours?",
        },
    ),
    (
        "rate_basis",
        {
            "hourly": r"hourly|per hour|an hour",
            "daily": r"daily|per day|day rate",
            "weekly": r"weekly|per week",
            "fortnightly": r"fortnightly|per fortnight",
            "monthly": r"monthly|per month",
            "annual": r"annual|annually|per annum|per year|yearly",
        },
    ),
    (
        "licence_class",
        {
            "driver": r"driver'?s? licen[cs]e|car licen[cs]e",
            "forklift": r"forklift",
            "heavy_rigid": r"hr licen[cs]e|heavy rigid",
            "heavy_combination": r"hc licen[cs]e|heavy combination",
            "medium_rigid": r"mr licen[cs]e|medium rigid",
            "motorcycle": r"motorcycle|motorbike",
            "white_card": r"white card",
            "rsa": r"rsa|responsible service of alcohol",
        },
    ),
    (
        "shift",
        {
            "day": r"day shift",
            "night": r"night shift|nights",
            "afternoon": r"afternoon shift",
            "weekend": r"weekends?",
        },
    ),
    (
        "distance_unit",
        {
            "metric": r"kilometres?|kilometers?|kms?",
            "imperial": r"miles?",
        },
    ),
    (
        "bound",
        {
            "minimum": r"minimum|at least|no less than",
            "maximum": r"maximum|at most|no more than",
        },
    ),
)

_COMPILED_FAMILIES: tuple[tuple[str, tuple[tuple[str, re.Pattern[str]], ...]], ...] = tuple(
    (
        family,
        tuple(
            (member, re.compile(rf"\b(?:{pattern})\b", re.IGNORECASE))
            for member, pattern in members.items()
        ),
    )
    for family, members in _QUALIFIER_FAMILIES
)


def _qualifier_signature(text: str) -> dict[str, frozenset[str]]:
    """Which member of each qualifier family a question names."""
    signature: dict[str, frozenset[str]] = {}
    for family, members in _COMPILED_FAMILIES:
        hits = frozenset(member for member, pattern in members if pattern.search(text))
        if hits:
            signature[family] = hits
    return signature


# Words that carry no subject matter. Kept small on purpose: "current",
# "australian" and "own" all change what is being asked.
_FUNCTION_WORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "if", "of", "in", "on", "at", "to", "for", "with",
    "by", "from", "as", "is", "are", "was", "were", "be", "been", "being", "do", "does", "did",
    "done", "have", "has", "had", "you", "your", "yours", "my", "me", "i", "it", "its", "this",
    "that", "these", "those", "any", "all", "please", "provide", "confirm", "tell", "us", "we",
    "they", "there", "will", "would", "can", "could", "shall", "should", "may", "might",
    "must", "about", "into", "over", "under", "than", "then", "so", "such", "which", "what",
    "when", "where", "who", "whom", "whose", "how", "why", "not", "no"
})

_TYPO_RATIO = 80.0
"""How close two unmatched words must be to count as spellings of one word.

"licence"/"license" scores 86 and "austrlia"/"australia" 94; "ruby"/"python"
scores 18. The gap is wide, which is why a single threshold works here.
"""


def _content_words(text: str) -> set[str]:
    return {word for word in _loose(text).split() if word not in _FUNCTION_WORDS}


def _substitution(question: str, pattern: str) -> str:
    """Detect a swapped subject: same question, different thing being asked about.

    "How many years of Ruby experience do you have?" scores 91 against a stored
    Python entry, and "Master's degree" scores 90 against "Bachelor's degree".
    No curated list scales to every language, framework and qualification, so
    this works structurally instead.

    An *addition* is fine — "...working rights in Australia?" against a stored
    "...working rights?" is the same question with more words, and the stored
    answer applies. A *substitution* is not: when each side has content the
    other lacks, and those leftovers are not spellings of one another, the two
    questions are about different things.
    """
    left = _content_words(question)
    right = _content_words(pattern)
    left_only = left - right
    right_only = right - left
    if not left_only or not right_only:
        return ""

    smaller, larger = (
        (left_only, right_only) if len(left_only) <= len(right_only) else (right_only, left_only)
    )
    unpaired = [
        word
        for word in smaller
        if max(fuzz.ratio(word, other) for other in larger) < _TYPO_RATIO
    ]
    if unpaired:
        return f"substituted subject ({'/'.join(sorted(unpaired))})"
    return ""


# A question containing more question-clauses than the stored pattern does is
# asking more than one thing, and answering it from an entry that covers only
# the first half puts an answer to an unasked question into the field:
# "What is your notice period, and what is your salary expectation?" matched a
# stored notice-period entry at 100 and filled in "4 weeks".
_INTERROGATIVE = (
    r"what|how|when|where|why|which|who|"
    r"do you|are you|can you|have you|will you|would you|did you|is your|was your"
)
_OPENS_A_QUESTION = re.compile(rf"\b(?:{_INTERROGATIVE})\b")
# " ... , and what ...", " ... or do you ..." — a second question joined on.
_JOINED_QUESTION = re.compile(rf",?\s+(?:and|or)\s+(?:{_INTERROGATIVE})\b")


def _clause_count(text: str) -> int:
    """How many questions are being asked at once.

    Counted by segment rather than by opener, because "How many years of Python
    experience do you have?" contains two openers and is one question. A segment
    that follows a question mark without asking anything — "... in Australia?
    Please attach evidence." — is not a second question either.
    """
    segments = sum(1 for part in text.split("?") if _OPENS_A_QUESTION.search(part))
    return max(segments, 1) + len(_JOINED_QUESTION.findall(text))


def _conflicts_with_pattern_source(question: str, pattern: str) -> str:
    """The checks that survive being run against a regex.

    A regex row's ``question_pattern`` is a fragment by design — "police\\s+check"
    is meant to match a longer question — so the clause and substitution checks
    below cannot be applied to it. Negation and qualifier families still can:
    the author wrote the pattern against one framing, and it must not answer the
    negation of it or a different member of the same family.
    """
    asked_negations = _negation_count(question)
    stored_negations = _negation_count(pattern)
    if asked_negations != stored_negations:
        return f"negation mismatch ({asked_negations} vs {stored_negations})"

    left = _qualifier_signature(question)
    right = _qualifier_signature(pattern)
    for family in left.keys() & right.keys():
        if left[family] != right[family]:
            return (
                f"{family} mismatch "
                f"({'/'.join(sorted(left[family]))} vs {'/'.join(sorted(right[family]))})"
            )
    return ""


def _conflicts(question: str, pattern: str) -> str:
    """Why this row must not answer this question, or "" if it may.

    Returned as a reason string rather than a bool so the abstention detail and
    the debug log can say which check fired.
    """
    reason = _conflicts_with_pattern_source(question, pattern)
    if reason:
        return reason

    asked_clauses = _clause_count(question)
    stored_clauses = _clause_count(pattern)
    if asked_clauses != stored_clauses:
        return f"clause count mismatch ({asked_clauses} vs {stored_clauses})"

    return _substitution(question, pattern)


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
            source_row_id=row.id,
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
                source_row_id=row.id,
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


def _candidates(
    question_text: str, question: str, answers: Sequence[AnswerBank]
) -> list[tuple[float, MatchType, AnswerBank]]:
    """Every bank row that plausibly matches this question, with its score.

    Extracted so the facts layer can ask "which row matched?" without a second
    matcher. Two matchers disagreeing about what a question is asking is how a
    licence fact ends up answering a police-check question.
    """
    candidates: list[tuple[float, MatchType, AnswerBank]] = []
    loose_question = _loose(question)

    for row in answers:
        pattern = normalise_question(row.question_pattern)

        if row.match_type == MatchType.REGEX:
            try:
                compiled = re.compile(row.question_pattern, re.IGNORECASE)
            except re.error as exc:
                # A bad pattern is a data problem in one row; it must not stop
                # the other rows from resolving this question.
                log.warning(
                    "answer_bank_invalid_regex",
                    row_id=row.id,
                    pattern=row.question_pattern,
                    error=str(exc),
                )
                continue
            if not (compiled.search(question_text) or compiled.search(question)):
                continue
            # A regex is a deliberate pattern, but it was written against one
            # framing of the question. It must not answer the negation of it.
            reason = _conflicts_with_pattern_source(question, pattern)
            if reason:
                log.debug("candidate_rejected", pattern=pattern, reason=reason)
                continue
            candidates.append((100.0, MatchType.REGEX, row))
            continue

        reason = _conflicts(question, pattern)
        if reason:
            # Not a weak match — a confident wrong one.
            log.debug("candidate_rejected", pattern=pattern, reason=reason)
            continue
        score = max(
            fuzz.token_sort_ratio(loose_question, _loose(pattern)),
            fuzz.partial_ratio(loose_question, _loose(pattern)),
        )
        if score >= FUZZY_THRESHOLD:
            candidates.append((float(score), MatchType.FUZZY, row))

    return candidates


def matching_rows(
    question_text: str, answers: Sequence[AnswerBank], *, region: Region | None = None
) -> list[AnswerBank]:
    """Bank rows matching this question, best first. Same matcher as resolution.

    Used by the facts layer to find which row a question belongs to, and so
    which category of fact could answer it. Region-scoped rows for a different
    country are excluded here for the same reason resolution excludes them: they
    are answers to a different question.
    """
    question = normalise_question(question_text)
    if not question:
        return []
    in_region = [
        row
        for row in answers
        if region is None or row.region is None or row.region == region
    ]
    ranked = sorted(
        _candidates(question_text, question, in_region), key=lambda c: c[0], reverse=True
    )
    return [row for _, _, row in ranked]


def resolve_answer(
    question_text: str,
    campaign_id: int | None = None,
    *,
    answers: Sequence[AnswerBank],
    choices: Sequence[str] | None = None,
    region: Region | None = None,
) -> Resolution:
    """Resolve one screening question. Never returns None; never guesses.

    A row declared ``MatchType.EXACT`` whose text equals the question is the
    user's deliberate override and wins outright. Everything else — regex rows
    and fuzzy rows alike — competes in one pool, and if the candidates within
    ``AMBIGUITY_MARGIN`` of the winner disagree about the answer, the module
    abstains.

    That single pool is deliberate. Tiering used to short-circuit: any row whose
    text happened to equal the question was promoted to the exact tier and
    returned immediately, so a second stored row saying something different was
    never consulted. Two contradictory entries in the bank mean the bank is
    wrong, and answering from it puts a stale number on a real application.

    Campaign-scoped rows beat global ones; rows belonging to a DIFFERENT
    campaign are discarded rather than merely outranked.
    """
    question = normalise_question(question_text)
    if not question:
        return Abstain(question=question_text or "", reason=AbstainReason.NO_MATCH)

    if len(question) < MIN_QUESTION_CHARS or len(question.split()) < MIN_QUESTION_TOKENS:
        return Abstain(
            question=question,
            reason=AbstainReason.NO_MATCH,
            detail=(
                f"too short to be a question "
                f"(<{MIN_QUESTION_CHARS} chars or <{MIN_QUESTION_TOKENS} words)"
            ),
        )
    if len(question) > MAX_QUESTION_CHARS:
        return Abstain(
            question=question[:200],
            reason=AbstainReason.NO_MATCH,
            detail=f"{len(question)} characters is a page, not a question",
        )

    # A row scoped to another campaign is not evidence about this one. load_answers
    # already filters, but resolve_answer takes whatever list it is handed.
    answers = [
        row for row in answers if row.campaign_id is None or row.campaign_id == campaign_id
    ]

    # Region is a harder boundary than campaign. A row scoped to NZ is not a
    # weaker answer for an AU application, it is an answer to a different
    # question, so it is removed from the pool entirely rather than outranked.
    #
    # Dropping them can empty the pool, and that is the correct outcome: the
    # abstention below reports CROSS_REGION so the user is asked the question
    # for the region that actually needs it, instead of the system reusing the
    # other country's answer because it was the only one available.
    region_conflicted = []
    if region is not None:
        region_conflicted = [
            row for row in answers if row.region is not None and row.region != region
        ]
        answers = [
            row for row in answers if row.region is None or row.region == region
        ]

    # --- the deliberate override: a declared EXACT row, matched verbatim -----
    declared_exact = [
        row
        for row in answers
        if row.match_type == MatchType.EXACT
        and normalise_question(row.question_pattern) == question
    ]
    if declared_exact:
        top = max(_scope_rank(row, campaign_id) for row in declared_exact)
        same_scope = [r for r in declared_exact if _scope_rank(r, campaign_id) == top]
        if len({(r.answer_value or "").strip().casefold() for r in same_scope}) > 1:
            return Abstain(
                question=question,
                reason=AbstainReason.AMBIGUOUS,
                detail=f"{len(same_scope)} exact rows match this question and disagree",
                candidates=[r.question_pattern for r in same_scope],
            )
        return _finalise(
            same_scope[0],
            question=question,
            match_type=MatchType.EXACT,
            confidence=100.0,
            choices=choices,
        )

    # --- one candidate pool: regex hits and fuzzy hits together -------------
    candidates = _candidates(question_text, question, answers)

    if not candidates:
        # Distinguish "nothing in the bank" from "the only thing in the bank was
        # the other country's answer". They call for different fixes: the first
        # needs any answer, the second needs this region's answer specifically,
        # and reporting them identically is how someone ends up "fixing" it by
        # widening the existing row to cover both countries.
        if region_conflicted:
            other = sorted({row.region.value for row in region_conflicted if row.region})
            log.warning(
                "answer_cross_region_abstain",
                question=question[:120],
                asked_for=region.value if region else None,
                available_for=other,
            )
            return Abstain(
                question=question,
                reason=AbstainReason.CROSS_REGION,
                detail=(
                    f"the answer bank only has this for {', '.join(other)}, and the "
                    f"application is {region.value if region else 'unknown'}; "
                    "work rights and licences differ by country"
                ),
                candidates=[row.question_pattern for row in region_conflicted],
            )
        return Abstain(
            question=question,
            reason=AbstainReason.NO_MATCH,
            detail=f"no answer bank entry above {FUZZY_THRESHOLD}",
        )

    # Scope first, score second: a campaign row outranks a better-scoring global
    # one, and only rows at the winning scope are weighed against each other.
    top_scope = max(_scope_rank(row, campaign_id) for _, _, row in candidates)
    in_scope = [c for c in candidates if _scope_rank(c[2], campaign_id) == top_scope]
    in_scope.sort(key=lambda c: c[0], reverse=True)
    best_score, best_match_type, best_row = in_scope[0]

    contenders = [c for c in in_scope if best_score - c[0] <= AMBIGUITY_MARGIN]
    if len({(row.answer_value or "").strip().casefold() for _, _, row in contenders}) > 1:
        return Abstain(
            question=question,
            reason=AbstainReason.AMBIGUOUS,
            detail=(
                f"{len(contenders)} entries within {AMBIGUITY_MARGIN} points "
                f"of each other disagree (top score {best_score:.1f})"
            ),
            candidates=[row.question_pattern for _, _, row in contenders],
        )

    return _finalise(
        best_row,
        question=question,
        match_type=best_match_type,
        confidence=best_score,
        choices=choices,
    )


def question_key(item: Any) -> str:
    """The string a question is filed under, for both resolution and abstention.

    Callers need to match an :class:`Abstain` back to the field that produced
    it, which only works if they derive the key the same way this module does.
    Exported so that logic lives in one place rather than being reimplemented
    slightly differently at the call site.
    """
    if isinstance(item, str):
        return item
    return getattr(item, "label", None) or getattr(item, "text", "") or str(item)


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
        label = question_key(item)
        choices = None if isinstance(item, str) else getattr(item, "choices", None)

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
