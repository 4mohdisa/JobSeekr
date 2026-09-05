"""Two layers: what is true about the user, and what that implies for a form.

LAYER 1 — FACTS
    Free text, the user's words, stored verbatim. "Full SA driver's licence,
    class C, held since 2019, no restrictions" is one fact. Nothing here
    normalises, summarises or tidies it: a paraphrase would quietly become the
    source of truth for what is often a legal declaration.

    Free text rather than structured fields because a licence is not a boolean.
    It has a state, a class, a date and possibly conditions, and the useful
    shape differs per category and per person. Any schema chosen up front is a
    schema the next form asks a question outside of.

LAYER 2 — DERIVED ANSWERS
    A form asks "Do you hold a current driver's licence? Yes/No". The fact
    supports Yes. Deriving that is a judgement, so the FIRST derivation of a
    question goes to Telegram for confirmation; after that it is cached and the
    user is never asked again.

    Every derivation records which fact it came from and a hash of that fact's
    text. Editing the fact invalidates its derivations — "class C" becoming
    "class MR" changes answers nobody would think to revisit, and only the
    content can catch that. A timestamp cannot.

ABSTAIN, DO NOT GUESS
    Hard rule 2 is unchanged: an answer the bank cannot support is an
    abstention, and an abstention parks the job and asks. This layer widens what
    the bank can support; it does not weaken the rule. A question about an MR
    licence against a fact saying class C is a confident No — the fact says
    exactly what class it is. A question about a forklift licence against the
    same fact is an ABSTAIN, because the fact is silent, and silence is not a No
    when a wrong No costs an interview.

JURISDICTION
    A fact with a jurisdiction is evidence about that country only. An SA
    driver's licence answers an Australian question and says nothing about a New
    Zealand one. Reuses ``Region`` and the same rule ``answers.py`` already
    applies to region-scoped bank rows, rather than inventing a second notion of
    where an answer holds.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, select

from backend.config import settings
from backend.logging_setup import get_logger
from backend.models import (
    AnswerType,
    DerivedAnswer,
    Fact,
    FactCategory,
    Region,
)

log = get_logger(__name__)

__all__ = [
    "Derivation",
    "derive",
    "fact_hash",
    "facts_for",
    "invalidated_by",
    "on_confirmation_needed",
    "pending_confirmations",
    "resolve_from_facts",
    "set_fact",
    "stale_derivations",
]


# Set by the integrations layer, same convention as canary.on_drift.
on_confirmation_needed: Any = None


def fact_hash(text: str) -> str:
    """Content hash of a fact's text. Changes whenever the wording does.

    Deliberately hashes the raw string, whitespace and all. A user who reworded
    a fact meant something by it, and deciding that a whitespace-only edit is
    "the same fact" is exactly the judgement that lets a real edit slip through
    as cosmetic.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# Layer 1 — facts
# --------------------------------------------------------------------------


def set_fact(
    session: Session,
    *,
    key: str,
    text: str,
    category: FactCategory,
    jurisdiction: Region | None = None,
) -> Fact:
    """Store a fact verbatim. Editing one invalidates what was derived from it.

    Returns the row. The text is written exactly as given — the only
    transformation is stripping trailing whitespace off the whole string, which
    is an artifact of the textarea rather than something the user typed.
    """
    text = text.rstrip()
    row = session.exec(select(Fact).where(Fact.key == key)).first()

    if row is None:
        row = Fact(key=key, text=text, category=category, jurisdiction=jurisdiction)
        session.add(row)
        log.info("fact_created", key=key, category=category.value)
        return row

    changed = row.text != text
    row.text = text
    row.category = category
    row.jurisdiction = jurisdiction
    row.updated_at = datetime.now(UTC)
    session.add(row)

    if changed:
        # Not a cascade delete: the derivations are kept so the user can see
        # what stopped being valid and why. They simply stop answering, because
        # `resolve_from_facts` compares hashes before trusting one.
        stale = invalidated_by(session, row)
        log.warning(
            "fact_edited",
            key=key,
            invalidated=len(stale),
            note="derivations from this fact must be re-derived and re-confirmed",
        )
    return row


def facts_for(
    session: Session,
    category: FactCategory,
    *,
    region: Region | None = None,
) -> list[Fact]:
    """Facts that could answer a question in this category and region.

    A fact with no jurisdiction holds everywhere. A fact WITH one is evidence
    about that country only — the same rule region-scoped answer-bank rows
    already follow, so there is one story about where an answer holds rather
    than two.
    """
    rows = session.exec(select(Fact).where(Fact.category == category)).all()
    return [
        row
        for row in rows
        if row.jurisdiction is None or region is None or row.jurisdiction == region
    ]


def invalidated_by(session: Session, fact: Fact) -> list[DerivedAnswer]:
    """Derivations whose fact text no longer matches the fact."""
    current = fact_hash(fact.text)
    return [
        row
        for row in session.exec(
            select(DerivedAnswer).where(DerivedAnswer.fact_id == fact.id)
        ).all()
        if row.fact_text_hash != current
    ]


def stale_derivations(session: Session) -> list[DerivedAnswer]:
    """Every derivation whose source fact has since been edited or removed."""
    facts = {row.id: fact_hash(row.text) for row in session.exec(select(Fact)).all()}
    return [
        row
        for row in session.exec(select(DerivedAnswer)).all()
        if row.fact_id not in facts or facts[row.fact_id] != row.fact_text_hash
    ]


# --------------------------------------------------------------------------
# Layer 2 — derivation
# --------------------------------------------------------------------------


@dataclass
class Derivation:
    """One answer worked out from one fact, before anyone confirms it."""

    answer: str
    answer_type: AnswerType
    fact: Fact
    reasoning: str
    #: What the model was not certain about, if anything. Non-empty means the
    #: caller must abstain rather than use ``answer``.
    uncertainty: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.answer) and not self.uncertainty


_DERIVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "supported": {
            "type": "boolean",
            "description": (
                "True ONLY if the fact states enough to answer the question "
                "with certainty. False for anything the fact does not address."
            ),
        },
        "answer": {"type": "string"},
        "reasoning": {
            "type": "string",
            "description": "Which words in the fact support the answer.",
        },
        "uncertainty": {
            "type": "string",
            "description": (
                "Empty when certain. Otherwise what is missing or ambiguous."
            ),
        },
    },
    "required": ["supported", "answer", "reasoning", "uncertainty"],
}

_DERIVE_SYSTEM = """\
You convert a stated fact about a job applicant into an answer to one screening
question on an application form.

RULES, IN ORDER OF IMPORTANCE

1. Use ONLY the fact text. You have no other knowledge of this person. If the
   fact does not address the question, set supported=false. Never infer from
   what is typical, likely, or implied by the applicant's situation.

2. A fact that is SILENT on something is not a No. "Full SA driver's licence,
   class C" says nothing about a forklift licence, so a forklift question is
   supported=false — not "No". Silence and denial are different, and a wrong No
   costs an interview.

3. A fact that is SPECIFIC is a No for things it excludes. The same fact, asked
   about an MR (medium rigid) licence, IS a confident No: it states the class,
   and class C is not MR. Answer No, supported=true.

4. When the question offers choices, answer with one of them exactly.

5. Never soften, upgrade or round a fact. These are legal declarations.

If you are weighing whether something counts, that weighing is itself the
uncertainty — set supported=false and say what is missing.\
"""


def derive(
    fact: Fact,
    question: str,
    *,
    choices: list[str] | None = None,
    answer_type: AnswerType = AnswerType.TEXT,
    job_id: int | None = None,
) -> Derivation | None:
    """Work out an answer from one fact. Returns None when it does not follow.

    None is the common and correct outcome. The caller abstains on it, which
    parks the job and asks — the existing loop, unchanged.
    """
    from backend.llm.client import complete_json

    prompt = "\n".join(
        [
            f"FACT (the applicant's own words, verbatim):\n{fact.text}",
            "",
            f"QUESTION ON THE FORM:\n{question}",
            f"ALLOWED ANSWERS: {choices}" if choices else "",
            f"EXPECTED ANSWER TYPE: {answer_type.value}",
        ]
    )

    try:
        result = complete_json(
            prompt,
            model=settings.llm_model_classify,
            purpose="fact_derivation",
            schema=_DERIVE_SCHEMA,
            system=_DERIVE_SYSTEM,
            temperature=0.0,
            job_id=job_id,
        )
    except Exception as exc:  # noqa: BLE001 - a model failure is an abstention
        log.warning("fact_derivation_failed", fact=fact.key, error=str(exc)[:200])
        return None

    if not result.get("supported"):
        log.info(
            "fact_does_not_support_answer",
            fact=fact.key,
            question=question[:100],
            reason=str(result.get("uncertainty") or result.get("reasoning"))[:200],
        )
        return None

    answer = str(result.get("answer") or "").strip()
    uncertainty = str(result.get("uncertainty") or "").strip()
    if not answer:
        return None

    if choices and answer not in choices:
        # The model was told to pick one of the choices and did not. Coercing it
        # would be guessing which option it meant, on a form the user has not
        # seen. Abstain instead.
        log.warning(
            "derived_answer_not_in_choices",
            fact=fact.key,
            answer=answer[:80],
            choices=choices,
        )
        return None

    derivation = Derivation(
        answer=answer,
        answer_type=answer_type,
        fact=fact,
        reasoning=str(result.get("reasoning") or "").strip(),
        uncertainty=uncertainty,
    )
    if not derivation.usable:
        log.info(
            "derivation_uncertain",
            fact=fact.key,
            uncertainty=uncertainty[:200],
            note="abstaining rather than answering",
        )
        return None
    return derivation


# --------------------------------------------------------------------------
# Resolution — the door the flow uses
# --------------------------------------------------------------------------


def resolve_from_facts(
    session: Session,
    *,
    question: str,
    question_key: str,
    category: FactCategory | None,
    choices: list[str] | None = None,
    answer_type: AnswerType = AnswerType.TEXT,
    region: Region | None = None,
    job_id: int | None = None,
) -> str | None:
    """The confirmed answer for this question, deriving and asking if needed.

    Returns the answer string, or None — and None means abstain, which parks the
    job and asks. Three outcomes:

    * a confirmed derivation whose fact is unchanged  -> the answer
    * a confirmed derivation whose fact HAS changed   -> None, and re-derive
    * no derivation, or one still awaiting the user   -> None

    An unconfirmed derivation never answers. That is the whole point of the
    confirmation step: the model's reading of a fact is a proposal until the
    person whose fact it is agrees with it.
    """
    cached = session.exec(
        select(DerivedAnswer).where(
            DerivedAnswer.question_key == question_key,
            DerivedAnswer.region == region,
        )
    ).first()

    if cached is not None:
        fact = session.get(Fact, cached.fact_id) if cached.fact_id else None
        if fact is None:
            log.warning("derivation_orphaned", question=question_key[:80])
        elif cached.fact_text_hash != fact_hash(fact.text):
            log.warning(
                "derivation_stale",
                question=question_key[:80],
                fact=fact.key,
                note="the fact was edited; re-deriving",
            )
            session.delete(cached)
            cached = None
        elif cached.confirmed_at is not None:
            return cached.answer_value
        else:
            # Asked and not yet answered. Asking again on every pass is how the
            # channel becomes one the user mutes.
            log.info("derivation_awaiting_confirmation", question=question_key[:80])
            return None

    if category is None:
        return None

    candidates = facts_for(session, category, region=region)
    if not candidates:
        log.info(
            "no_fact_for_category",
            category=category.value,
            region=region.value if region else None,
        )
        return None

    for fact in candidates:
        derivation = derive(
            fact,
            question,
            choices=choices,
            answer_type=answer_type,
            job_id=job_id,
        )
        if derivation is None:
            continue

        row = DerivedAnswer(
            question_key=question_key,
            question_text=question,
            answer_value=derivation.answer,
            answer_type=answer_type,
            fact_id=fact.id,
            fact_text_hash=fact_hash(fact.text),
            region=region,
            reasoning=derivation.reasoning,
            confirmed_at=None,
        )
        session.add(row)
        session.flush()

        log.info(
            "derivation_proposed",
            question=question_key[:80],
            fact=fact.key,
            answer=derivation.answer[:60],
        )
        if on_confirmation_needed is not None:
            try:
                on_confirmation_needed(
                    row.id, question, derivation.answer, fact.key, fact.text,
                    derivation.reasoning,
                )
            except Exception as exc:  # noqa: BLE001 - asking must not abort
                log.warning("derivation_ask_failed", error=str(exc)[:150])

        # Not returned. It is a proposal until confirmed, so this pass abstains
        # and the job is parked exactly as it would be with no answer at all.
        return None

    log.info(
        "no_fact_supports_question",
        question=question_key[:80],
        category=category.value,
        considered=len(candidates),
    )
    return None


def confirm(session: Session, derivation_id: int) -> DerivedAnswer | None:
    """The user agreed. From here the answer is used without asking again."""
    row = session.get(DerivedAnswer, derivation_id)
    if row is None:
        return None
    row.confirmed_at = datetime.now(UTC)
    session.add(row)
    log.info("derivation_confirmed", question=row.question_key[:80], id=derivation_id)
    return row


def reject(session: Session, derivation_id: int) -> bool:
    """The user disagreed. The derivation is deleted, not marked wrong.

    Deleted so the next pass re-derives rather than treating a rejected reading
    as settled — the fact may since have been corrected, which is the usual
    reason a derivation was wrong in the first place.
    """
    row = session.get(DerivedAnswer, derivation_id)
    if row is None:
        return False
    session.delete(row)
    log.info("derivation_rejected", id=derivation_id)
    return True


def pending_confirmations(session: Session) -> list[DerivedAnswer]:
    """Derivations waiting on the user."""
    return list(
        session.exec(
            select(DerivedAnswer).where(DerivedAnswer.confirmed_at.is_(None))  # type: ignore[union-attr]
        ).all()
    )
