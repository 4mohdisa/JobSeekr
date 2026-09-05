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
from backend.llm.client import llm
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
    "FactLeverage",
    "Preview",
    "derive",
    "fact_hash",
    "facts_for",
    "invalidated_by",
    "leverage",
    "on_confirmation_needed",
    "pending_confirmations",
    "preview_all",
    "render_preview",
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
        # A blank fact is not evidence. The filter was in preview_all only, so
        # the dry-run preview said "the fact is blank" while the live path spent
        # a model call per blank row to be told the same thing.
        if row.text.strip()
        and (row.jurisdiction is None or region is None or row.jurisdiction == region)
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


@dataclass
class FactLeverage:
    """One stated fact and how much work it is doing."""

    fact_id: int
    key: str
    category: str
    derived: int
    confirmed: int
    """Derivations the user has agreed with. Only these ever answer anything."""

    stale: int
    """Derivations whose fact has been edited since. They answer nothing until
    they are re-derived and re-confirmed."""


def leverage(session: Session) -> list[FactLeverage]:
    """Every fact and how many derived answers it supports, most first.

    Which fact was worth writing, and — read against the friction ranking in
    ``backend/questions.py`` — which one to write next. Lives here rather than
    with the other question aggregates because ``DerivedAnswer`` deliberately
    has one reader: the hash check that decides whether a cached answer is still
    true must be impossible to bypass, and that is only true while every reader
    is in this file.

    Facts supporting nothing are included. A fact answering no question is as
    much a finding as one answering six — it is either a question nobody asks or
    a fact the derivation step cannot read, and both are worth seeing.
    """
    rows = list(session.exec(select(DerivedAnswer)).all())
    by_fact: dict[int, list[DerivedAnswer]] = {}
    for row in rows:
        if row.fact_id is not None:
            by_fact.setdefault(row.fact_id, []).append(row)

    report = []
    for fact in session.exec(select(Fact)).all():
        if fact.id is None:
            continue
        derived = by_fact.get(fact.id, [])
        current = fact_hash(fact.text)
        report.append(
            FactLeverage(
                fact_id=fact.id,
                key=fact.key,
                category=fact.category.value,
                derived=len(derived),
                confirmed=sum(1 for row in derived if row.confirmed_at is not None),
                stale=sum(1 for row in derived if row.fact_text_hash != current),
            )
        )
    report.sort(key=lambda item: (item.confirmed, item.derived), reverse=True)
    return report


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
    from backend.apply.draft import as_choices

    options = as_choices(choices)
    # The exact strings the form will accept, one per line, in its own order.
    # A repr of a list of Choice objects would put the model's answer inside a
    # dataclass — and "the model picked an option" has to be checkable by string
    # equality against something the form actually submits.
    allowed = "\n".join(f"- {option.label}" for option in options)
    prompt = "\n".join(
        [
            f"FACT (the applicant's own words, verbatim):\n{fact.text}",
            "",
            f"QUESTION ON THE FORM:\n{question}",
            (
                "ALLOWED ANSWERS — reply with ONE of these, copied exactly. You "
                f"may not invent a value outside this list:\n{allowed}"
                if options
                else ""
            ),
            f"EXPECTED ANSWER TYPE: {answer_type.value}",
        ]
    )

    try:
        # Through the module-level `llm`, not an inline import. That object is
        # the seam the rehearsal and the tests replace with a stub; an inline
        # import bypasses it and makes a real call. The same mistake in
        # documents/verify.py took the suite from 27 seconds to 3.5 minutes.
        result = llm.complete_json(
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

    if options:
        # Constrained to the option set, and to it exactly. A fact saying "two
        # weeks notice" against [Immediately, 1-2 weeks, 1 month] must come back
        # as "1-2 weeks" — the model picks from the list, it never writes a new
        # value. Anything else is an abstention: coercing it would be guessing
        # which option it meant, on a form the user has not seen.
        named = [option for option in options if option.matches(answer)]
        if len(named) != 1:
            log.warning(
                "derived_answer_not_in_choices",
                fact=fact.key,
                answer=answer[:80],
                choices=[option.label for option in options],
            )
            return None
        # The submitted value, not the label the model echoed back.
        answer = named[0].value

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
    from backend.apply.draft import as_choices

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
            if choices and not any(
                option.matches(cached.answer_value) for option in as_choices(choices)
            ):
                # Confirmed, and not an option THIS employer offers. The same
                # rule as the answer bank's: a different wording is a different
                # answer, and picking the nearest option would put a value on the
                # form that the user never confirmed. Abstain, which parks the
                # job and asks with this form's own options.
                log.warning(
                    "derived_answer_not_offered_here",
                    question=question_key[:80],
                    answer=cached.answer_value[:60],
                    choices=[c.label for c in as_choices(choices)],
                )
                return None
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
                    row.id,
                    question,
                    derivation.answer,
                    fact.key,
                    fact.text,
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


# --------------------------------------------------------------------------
# Preview — read all 21 at once, before any of them is cached
# --------------------------------------------------------------------------


@dataclass
class Preview:
    """What the derivation would do for one question. Nothing is written."""

    question: str
    category: FactCategory | None
    fact_key: str | None = None
    answer: str | None = None
    reasoning: str = ""
    abstained: bool = True
    reason: str = ""

    @property
    def status(self) -> str:
        return "ANSWER" if not self.abstained else "ABSTAIN"


def preview_all(
    session: Session, *, region: Region | None = None, limit: int | None = None
) -> list[Preview]:
    """Run the real derivation against every seeded question. Writes nothing.

    WHY THIS EXISTS
        A derivation is confirmed once and then cached forever, which is the
        point — the user is asked at most once per question. It also means a
        wrong confirmation is durable: a plausible misreading of a licence
        becomes a legal declaration on every later application, and the moment
        it would be caught is the moment it is least likely to be read
        carefully, one question at a time over Telegram.

        This shows all of them at once, before any is cached, so a bad reading
        is caught by comparing it against its neighbours rather than in
        isolation.

    Deliberately does NOT write a DerivedAnswer row. A entry that left
    proposals behind would be indistinguishable from the real path, and running
    it twice would double them.
    """
    from backend.models import AnswerBank
    from backend.seed import ANSWER_BANK_SEEDS

    # A REGEX row's question_pattern is a regex, not a question. The real flow
    # never has this problem — the question comes from the form field's own
    # label and the bank row is used only for routing — but a entry has no
    # form, and handing a regex to the model produces nonsense delivered with
    # the confidence of an answer. The seeds carry a plain-English rendering
    # for exactly this.
    examples = {
        seed.question_pattern: seed.example_question
        for seed in ANSWER_BANK_SEEDS
        if seed.example_question
    }

    rows = [
        row for row in session.exec(select(AnswerBank)).all() if row.campaign_id is None
    ]
    rows.sort(key=lambda row: row.fact_category.value if row.fact_category else "zz")
    if limit:
        rows = rows[:limit]

    previews: list[Preview] = []
    for row in rows:
        question = examples.get(row.question_pattern) or row.question_pattern
        entry = Preview(question=question, category=row.fact_category)

        if row.fact_category is None:
            entry.reason = "no fact category — nothing to consult"
            previews.append(entry)
            continue

        candidates = facts_for(session, row.fact_category, region=region)
        if not candidates:
            entry.reason = f"the {row.fact_category.value} fact is blank"
            previews.append(entry)
            continue

        for fact in candidates:
            derivation = derive(
                fact,
                question,
                choices=list(row.choices or []) or None,
                answer_type=row.answer_type,
            )
            if derivation is not None:
                entry.fact_key = fact.key
                entry.answer = derivation.answer
                entry.reasoning = derivation.reasoning
                entry.abstained = False
                break
        else:
            entry.fact_key = candidates[0].key
            entry.reason = "no fact supports an answer"

        previews.append(entry)

    log.info(
        "derivation_preview",
        questions=len(previews),
        answered=sum(1 for p in previews if not p.abstained),
        abstained=sum(1 for p in previews if p.abstained),
        note="nothing was written",
    )
    return previews


def render_preview(previews: list[Preview]) -> str:
    """The table. Abstentions are shown with their reason, not omitted.

    An abstention is the more important row: it is a screening question no
    application can answer, and the reason says whether that is a blank fact or
    a fact that genuinely does not cover it.
    """
    lines = [
        "",
        "DERIVATION PREVIEW — nothing written, nothing confirmed",
        "=" * 76,
    ]
    for entry in previews:
        lines.append("")
        lines.append(f"  [{entry.status}] {entry.question[:68]}")
        if entry.category:
            source = f"{entry.category.value}"
            if entry.fact_key:
                source += f" -> {entry.fact_key}"
            lines.append(f"           from: {source}")
        if entry.answer:
            lines.append(f"           answer: {entry.answer}")
        if entry.reasoning:
            lines.append(f"           because: {entry.reasoning[:200]}")
        if entry.reason:
            lines.append(f"           reason: {entry.reason}")

    answered = sum(1 for p in previews if not p.abstained)
    lines.append("")
    lines.append("=" * 76)
    lines.append(
        f"{answered} of {len(previews)} would be answered; "
        f"{len(previews) - answered} would abstain and ask."
    )
    lines.append("Read each answer against the fact it came from before confirming.")
    lines.append("Nothing here has been written or cached.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI wiring
    import argparse

    from backend.db import session_scope
    from backend.logging_setup import configure_logging

    parser = argparse.ArgumentParser(prog="python -m backend.facts")
    sub = parser.add_subparsers(dest="command", required=True)

    preview_parser = sub.add_parser(
        "preview", help="dry-run the derivation against every seeded question"
    )
    preview_parser.add_argument(
        "--region",
        default=None,
        choices=["AU", "NZ"],
        help="jurisdiction to derive for",
    )
    preview_parser.add_argument("--limit", type=int, default=None)

    args = parser.parse_args(argv)
    configure_logging()

    with session_scope() as session:
        previews = preview_all(
            session,
            region=Region(args.region) if args.region else None,
            limit=args.limit,
        )
    print(render_preview(previews))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
