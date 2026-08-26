"""Idempotent seed data: the empty profile and the Australian screening questions.

Run it as often as you like — ``uv run python -m backend.seed``. Every insert is
guarded by a check, and nothing here ever updates a row that already exists, so
re-seeding after an upgrade can never overwrite an answer the user has verified.

**The seeded answers are deliberately blank.** This module pre-loads the
*questions* an Australian application form asks, never the answers to them.
Answers are facts about the user — work rights, licences, salary, vaccination
status — and Claude.md hard rules 1 and 2 forbid inventing or defaulting them:
the bank must abstain and ask rather than guess. A blank ``answer_value`` with
``verified_at`` NULL is the correct, load-bearing state; it makes the applier
park the job and ask via Telegram, and the answer it gets back is then stored
and verified once. Filling these in with plausible-looking defaults would
silently put fabricated claims onto real job applications.

What seeding buys is the matching metadata: the question phrasings, whether they
match by regex or fuzz, what shape the answer has to be, and a note telling the
user the exact format to type. All rows are global (``campaign_id`` NULL); a
campaign-scoped entry added later wins over them.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import col, select

from backend.db import session_scope
from backend.logging_setup import get_logger
from backend.models import AnswerBank, AnswerType, MatchType, Profile

log = get_logger(__name__)

# The profile is versioned and never edited in place; version 1 is the empty
# shell the user fills in, and every Score records the version it scored against.
DEFAULT_PROFILE_VERSION = 1


@dataclass(frozen=True)
class AnswerBankSeed:
    """One screening question the bank should recognise, minus the answer.

    Frozen because these are constants, not a table row under construction —
    the ``AnswerBank`` model is the mutable thing.
    """

    question_pattern: str
    match_type: MatchType
    answer_type: AnswerType
    notes: str


# Fuzzy is the default: most screening questions are a full sentence and a
# fuzzy ratio against a canonical phrasing handles the variation. Regex is used
# only where it is clearly better — an abbreviation the fuzz would miss (WWCC,
# ABN), a spelling that varies (licence/license), or a pair of questions fuzz
# would confuse with each other (annual salary vs hourly rate, visa status vs
# sponsorship), where a wrong-but-confident match is worse than no match.
ANSWER_BANK_SEEDS: tuple[AnswerBankSeed, ...] = (
    AnswerBankSeed(
        question_pattern="Do you have full working rights in Australia?",
        match_type=MatchType.FUZZY,
        answer_type=AnswerType.BOOLEAN,
        notes=(
            'Answer "true" or "false". Must match the work rights recorded in '
            "your profile exactly — this is a legal declaration, never soften it."
        ),
    ),
    AnswerBankSeed(
        # Must not swallow sponsorship questions, which need the opposite answer.
        question_pattern=(
            r"(?i)^(?!.*sponsor).*\b(visa|citizenship|permanent resident|residency)\b"
        ),
        match_type=MatchType.REGEX,
        answer_type=AnswerType.TEXT,
        notes=(
            "Short free text. Use the wording on your grant notice or passport, "
            'e.g. "Australian citizen", "Permanent resident", '
            '"Subclass 500 student visa".'
        ),
    ),
    AnswerBankSeed(
        # Every apostrophe placement and both spellings: "driver's licence",
        # "drivers licence", "drivers' license", "driving licence", straight or
        # typographic apostrophe.
        question_pattern=r"(?i)\bdriv(er|ing)['’]?s?['’]?\s*licen[cs]e\b",
        match_type=MatchType.REGEX,
        answer_type=AnswerType.BOOLEAN,
        notes=(
            'Answer "true" or "false". "true" only if the licence is current and '
            "Australian — an overseas or expired licence is a false here."
        ),
    ),
    AnswerBankSeed(
        question_pattern="Do you have your own reliable transport?",
        match_type=MatchType.FUZZY,
        answer_type=AnswerType.BOOLEAN,
        notes='Answer "true" or "false".',
    ),
    AnswerBankSeed(
        question_pattern=(
            r"(?i)\b(police\s+(check|clearance|certificate)"
            r"|national\s+police|criminal\s+(history|record)\s+check)\b"
        ),
        match_type=MatchType.REGEX,
        answer_type=AnswerType.BOOLEAN,
        notes=(
            'Answer "true" or "false". "true" only if you hold a National Police '
            "Certificate issued inside the window the ad asks for, usually 12 months."
        ),
    ),
    AnswerBankSeed(
        question_pattern=(
            r"(?i)\b(working\s+with\s+children|wwcc|blue\s+card"
            r"|working\s+with\s+vulnerable\s+people|wwvp)\b"
        ),
        match_type=MatchType.REGEX,
        answer_type=AnswerType.BOOLEAN,
        notes=(
            'Answer "true" or "false". The check is state-issued — WWCC in SA, NSW '
            'and VIC, Blue Card in QLD, WWVP in the ACT. "true" only if current.'
        ),
    ),
    AnswerBankSeed(
        question_pattern="What is your notice period with your current employer?",
        match_type=MatchType.FUZZY,
        answer_type=AnswerType.TEXT,
        notes=(
            'Short free text expressed as a period, e.g. "4 weeks", "2 weeks", '
            '"Immediate".'
        ),
    ),
    AnswerBankSeed(
        question_pattern="What is the earliest date you can start?",
        match_type=MatchType.FUZZY,
        answer_type=AnswerType.DATE,
        notes=(
            'ISO date, YYYY-MM-DD, e.g. 2026-09-15. Not "ASAP" — the applier '
            "needs a real date it can type into a date field."
        ),
    ),
    AnswerBankSeed(
        # Two lookaheads so either word order matches; "hour" excluded so this
        # never answers an hourly-rate question with an annual figure.
        question_pattern=(
            r"(?i)^(?!.*hour)(?=.*\b(salary|remuneration|package|income)\b)"
            r"(?=.*\b(expect|desir|requir|seek|minimum))"
        ),
        match_type=MatchType.REGEX,
        answer_type=AnswerType.NUMBER,
        notes=(
            "Annual gross in AUD, digits only — no dollar sign, no commas, no "
            '"k". e.g. 95000.'
        ),
    ),
    AnswerBankSeed(
        question_pattern=r"(?i)(?=.*\bhour)(?=.*\b(rate|pay|expect|charge))",
        match_type=MatchType.REGEX,
        answer_type=AnswerType.NUMBER,
        notes=("Hourly rate in AUD, digits only, decimals allowed — e.g. 55 or 62.50."),
    ),
    AnswerBankSeed(
        question_pattern="Are you willing to relocate for this role?",
        match_type=MatchType.FUZZY,
        answer_type=AnswerType.BOOLEAN,
        notes='Answer "true" or "false".',
    ),
    AnswerBankSeed(
        question_pattern="Are you willing to travel for this role?",
        match_type=MatchType.FUZZY,
        answer_type=AnswerType.BOOLEAN,
        notes='Answer "true" or "false".',
    ),
    AnswerBankSeed(
        # The field named varies per ad, so there is no canonical sentence to
        # fuzz against — only the "how many years" frame is stable.
        question_pattern=(
            r"(?i)(\bhow\s+many\s+years\b|\byears?\s+of\s+(\w+\s+)?experience\b"
            r"|\byears['’]?\s+experience\b)"
        ),
        match_type=MatchType.REGEX,
        answer_type=AnswerType.NUMBER,
        notes=(
            "Whole number of years, digits only, e.g. 5. This pattern is "
            "field-agnostic: if an ad asks about one specific technology and your "
            "real figure differs, leave it blank so the applier asks instead."
        ),
    ),
    AnswerBankSeed(
        question_pattern="What is your highest level of education or qualification?",
        match_type=MatchType.FUZZY,
        answer_type=AnswerType.TEXT,
        notes=(
            "Free text. Use the award's full name as it appears in your profile, "
            'e.g. "Bachelor of Engineering (Honours)".'
        ),
    ),
    AnswerBankSeed(
        question_pattern="Are you currently residing in Australia?",
        match_type=MatchType.FUZZY,
        answer_type=AnswerType.BOOLEAN,
        notes='Answer "true" or "false".',
    ),
    AnswerBankSeed(
        question_pattern=(
            r"(?i)\b(sponsor\w*|subclass\s*482|employer\s+nomination|work\s+permit)\b"
        ),
        match_type=MatchType.REGEX,
        answer_type=AnswerType.BOOLEAN,
        notes=(
            'Answer "true" or "false". Watch the polarity: "true" means you DO '
            "require sponsorship, which is the opposite of the working-rights answer."
        ),
    ),
    AnswerBankSeed(
        question_pattern="Are you available for weekend, evening or shift work?",
        match_type=MatchType.FUZZY,
        answer_type=AnswerType.BOOLEAN,
        notes='Answer "true" or "false".',
    ),
    AnswerBankSeed(
        question_pattern=(
            "Are you willing to undertake a pre-employment medical and drug test?"
        ),
        match_type=MatchType.FUZZY,
        answer_type=AnswerType.BOOLEAN,
        notes='Answer "true" or "false".',
    ),
    AnswerBankSeed(
        question_pattern=r"(?i)\b(abn|australian\s+business\s+number)\b",
        match_type=MatchType.REGEX,
        answer_type=AnswerType.BOOLEAN,
        notes=(
            'Answer "true" or "false". "true" only if you hold a current ABN and '
            "are willing to contract under it."
        ),
    ),
    AnswerBankSeed(
        question_pattern="Can you provide contactable referees?",
        match_type=MatchType.FUZZY,
        answer_type=AnswerType.BOOLEAN,
        notes=(
            'Answer "true" or "false". Referee names and phone numbers are never '
            "auto-filled — a form that demands them gets parked for you."
        ),
    ),
    AnswerBankSeed(
        question_pattern=(
            r"(?i)\b(vaccinat\w*|vaccine|immunis\w*|immuniz\w*"
            r"|covid[\s-]?19|influenza|flu\s+(shot|jab))\b"
        ),
        match_type=MatchType.REGEX,
        answer_type=AnswerType.TEXT,
        notes=(
            'Short free text, e.g. "Fully vaccinated (COVID-19 and influenza)". '
            "Leaving it blank makes the applier ask you rather than disclose a "
            "health fact you have not approved."
        ),
    ),
)


def seed_answer_bank() -> int:
    """Insert any global screening question the bank is missing.

    Returns the number of rows inserted. ``question_pattern`` is the identity
    here: an existing global row with the same pattern is left completely
    untouched, answer and all, because the user's verified answer outranks
    anything this file has to say about the question.
    """
    inserted = 0

    with session_scope() as session:
        existing = set(
            session.exec(
                select(AnswerBank.question_pattern).where(
                    col(AnswerBank.campaign_id).is_(None)
                )
            ).all()
        )

        for spec in ANSWER_BANK_SEEDS:
            if spec.question_pattern in existing:
                continue
            session.add(
                AnswerBank(
                    question_pattern=spec.question_pattern,
                    match_type=spec.match_type,
                    # Blank on purpose — see the module docstring. The applier
                    # treats this as "ask the user", not as "answer with nothing".
                    answer_value="",
                    answer_type=spec.answer_type,
                    campaign_id=None,
                    choices=None,
                    verified_at=None,
                    notes=spec.notes,
                )
            )
            # Guards against a duplicated pattern inside ANSWER_BANK_SEEDS, which
            # would otherwise insert twice on the very first run.
            existing.add(spec.question_pattern)
            inserted += 1

    log.info(
        "answer_bank_seeded",
        inserted=inserted,
        skipped=len(ANSWER_BANK_SEEDS) - inserted,
        defined=len(ANSWER_BANK_SEEDS),
    )
    return inserted


def seed_default_profile() -> bool:
    """Create the empty version-1 profile if no profile exists at all.

    Returns True if a row was created. The check is "any profile row", not
    "version 1": once the user has edited their profile the current version is
    2 or 20, and re-inserting a blank version 1 would both collide with
    ``uq_profile_version`` and offer the scorer an empty profile to score
    against.
    """
    with session_scope() as session:
        if session.exec(select(Profile.id).limit(1)).first() is not None:
            log.info("profile_seed_skipped", reason="profile_already_exists")
            return False

        # Every JSON field defaults to an empty dict or list, so this is the
        # empty shell the user fills in via the UI.
        session.add(Profile(version=DEFAULT_PROFILE_VERSION))

    log.info("profile_seeded", version=DEFAULT_PROFILE_VERSION)
    return True


def seed_all() -> None:
    """Seed everything. Safe to run on every startup and after every migration."""
    profile_created = seed_default_profile()
    answers_inserted = seed_answer_bank()
    log.info(
        "seed_complete",
        profile_created=profile_created,
        answer_bank_inserted=answers_inserted,
    )


if __name__ == "__main__":
    seed_all()
