"""Idempotent seed data: the empty profile, the screening questions, one campaign.

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

**The starter campaign follows the same rule and is seeded inactive.** It exists
so a fresh database is not a silent no-op — discovery reads active campaigns
only, and with none it runs, stores nothing and reports success. Seeding it
paused makes the state visible and editable without letting anything start
applying before the user has looked at it. See ``seed_starter_campaign``.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import col, select

from backend.db import session_scope
from backend.logging_setup import get_logger
from backend.models import (
    AnswerBank,
    AnswerType,
    Campaign,
    FactCategory,
    GrayZoneAction,
    MatchType,
    Profile,
)

log = get_logger(__name__)

STARTER_CAMPAIGN_NAME = "Adelaide starter"

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

    fact_category: FactCategory | None = None
    """Which category of fact can answer this, once the user has written one.

    Routing, not an answer. The pattern above already knows how to recognise the
    question in all its spellings; this says which fact to consult when the row
    itself is blank. Reusing the bank's matcher rather than building a second
    one keeps a single answer to "what is this question asking".
    """


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
        fact_category=FactCategory.WORK_RIGHTS,
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
        fact_category=FactCategory.WORK_RIGHTS,
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
        fact_category=FactCategory.LICENCE,
    ),
    AnswerBankSeed(
        question_pattern="Do you have your own reliable transport?",
        match_type=MatchType.FUZZY,
        answer_type=AnswerType.BOOLEAN,
        notes='Answer "true" or "false".',
        fact_category=FactCategory.TRANSPORT,
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
        fact_category=FactCategory.CHECKS,
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
        fact_category=FactCategory.CHECKS,
    ),
    AnswerBankSeed(
        question_pattern="What is your notice period with your current employer?",
        match_type=MatchType.FUZZY,
        answer_type=AnswerType.TEXT,
        notes=(
            'Short free text expressed as a period, e.g. "4 weeks", "2 weeks", '
            '"Immediate".'
        ),
        fact_category=FactCategory.AVAILABILITY,
    ),
    AnswerBankSeed(
        question_pattern="What is the earliest date you can start?",
        match_type=MatchType.FUZZY,
        answer_type=AnswerType.DATE,
        notes=(
            'ISO date, YYYY-MM-DD, e.g. 2026-09-15. Not "ASAP" — the applier '
            "needs a real date it can type into a date field."
        ),
        fact_category=FactCategory.AVAILABILITY,
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
        fact_category=FactCategory.COMPENSATION,
    ),
    AnswerBankSeed(
        question_pattern=r"(?i)(?=.*\bhour)(?=.*\b(rate|pay|expect|charge))",
        match_type=MatchType.REGEX,
        answer_type=AnswerType.NUMBER,
        notes=("Hourly rate in AUD, digits only, decimals allowed — e.g. 55 or 62.50."),
        fact_category=FactCategory.COMPENSATION,
    ),
    AnswerBankSeed(
        question_pattern="Are you willing to relocate for this role?",
        match_type=MatchType.FUZZY,
        answer_type=AnswerType.BOOLEAN,
        notes='Answer "true" or "false".',
        fact_category=FactCategory.AVAILABILITY,
    ),
    AnswerBankSeed(
        question_pattern="Are you willing to travel for this role?",
        match_type=MatchType.FUZZY,
        answer_type=AnswerType.BOOLEAN,
        notes='Answer "true" or "false".',
        fact_category=FactCategory.AVAILABILITY,
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
        fact_category=FactCategory.EXPERIENCE,
    ),
    AnswerBankSeed(
        question_pattern="What is your highest level of education or qualification?",
        match_type=MatchType.FUZZY,
        answer_type=AnswerType.TEXT,
        notes=(
            "Free text. Use the award's full name as it appears in your profile, "
            'e.g. "Bachelor of Engineering (Honours)".'
        ),
        fact_category=FactCategory.EDUCATION,
    ),
    AnswerBankSeed(
        question_pattern="Are you currently residing in Australia?",
        match_type=MatchType.FUZZY,
        answer_type=AnswerType.BOOLEAN,
        notes='Answer "true" or "false".',
        fact_category=FactCategory.WORK_RIGHTS,
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
        fact_category=FactCategory.WORK_RIGHTS,
    ),
    AnswerBankSeed(
        question_pattern="Are you available for weekend, evening or shift work?",
        match_type=MatchType.FUZZY,
        answer_type=AnswerType.BOOLEAN,
        notes='Answer "true" or "false".',
        fact_category=FactCategory.AVAILABILITY,
    ),
    AnswerBankSeed(
        question_pattern=(
            "Are you willing to undertake a pre-employment medical and drug test?"
        ),
        match_type=MatchType.FUZZY,
        answer_type=AnswerType.BOOLEAN,
        notes='Answer "true" or "false".',
        fact_category=FactCategory.HEALTH,
    ),
    AnswerBankSeed(
        question_pattern=r"(?i)\b(abn|australian\s+business\s+number)\b",
        match_type=MatchType.REGEX,
        answer_type=AnswerType.BOOLEAN,
        notes=(
            'Answer "true" or "false". "true" only if you hold a current ABN and '
            "are willing to contract under it."
        ),
        fact_category=FactCategory.BUSINESS,
    ),
    AnswerBankSeed(
        question_pattern="Can you provide contactable referees?",
        match_type=MatchType.FUZZY,
        answer_type=AnswerType.BOOLEAN,
        notes=(
            'Answer "true" or "false". Referee names and phone numbers are never '
            "auto-filled — a form that demands them gets parked for you."
        ),
        fact_category=FactCategory.REFEREES,
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
        fact_category=FactCategory.HEALTH,
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
    backfilled = 0

    with session_scope() as session:
        existing = set(
            session.exec(
                select(AnswerBank.question_pattern).where(
                    col(AnswerBank.campaign_id).is_(None)
                )
            ).all()
        )

        # Backfill fact_category onto rows seeded before it existed. Without
        # this, an upgraded install keeps all 21 questions and none of them can
        # ever reach a fact — the patterns survive, the routing does not, and the
        # failure is silent because a blank bank row already means "ask".
        by_pattern = {
            row.question_pattern: row
            for row in session.exec(
                select(AnswerBank).where(col(AnswerBank.campaign_id).is_(None))
            ).all()
        }

        for spec in ANSWER_BANK_SEEDS:
            if spec.question_pattern in existing:
                row = by_pattern.get(spec.question_pattern)
                if row is not None and row.fact_category is None and spec.fact_category:
                    row.fact_category = spec.fact_category
                    session.add(row)
                    backfilled += 1
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
                    fact_category=spec.fact_category,
                )
            )
            # Guards against a duplicated pattern inside ANSWER_BANK_SEEDS, which
            # would otherwise insert twice on the very first run.
            existing.add(spec.question_pattern)
            inserted += 1

    log.info(
        "answer_bank_seeded",
        inserted=inserted,
        backfilled=backfilled,
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


FACT_SHELLS: tuple[tuple[str, str, str], ...] = (
    # key, category, the prompt shown above the textarea on the Facts page
    (
        "work_rights",
        "WORK_RIGHTS",
        (
            "Your right to work: citizenship or visa, any conditions, whether you need "
            "sponsorship."
        ),
    ),
    (
        "licence",
        "LICENCE",
        (
            "Driver's or other licences: which state issued it, what class, how long "
            "you have held it, any restrictions."
        ),
    ),
    (
        "checks",
        "CHECKS",
        (
            "Police checks, working-with-children checks, security clearances — which "
            "ones you hold and when they were issued."
        ),
    ),
    (
        "education",
        "EDUCATION",
        "Your highest qualification, where and when, plus anything else relevant.",
    ),
    (
        "experience",
        "EXPERIENCE",
        "Years of experience, and in what. Write it the way you would say it.",
    ),
    (
        "availability",
        "AVAILABILITY",
        (
            "Notice period, earliest start date, and whether you will relocate, "
            "travel, or work weekends and shifts."
        ),
    ),
    (
        "compensation",
        "COMPENSATION",
        "Salary or rate expectations, and whether they are negotiable.",
    ),
    ("transport", "TRANSPORT", "Whether you have your own reliable transport."),
    ("referees", "REFEREES", "Whether you can provide contactable referees."),
    (
        "health",
        "HEALTH",
        (
            "Anything about medicals, drug tests or vaccination status you are willing "
            "to declare."
        ),
    ),
    (
        "business",
        "BUSINESS",
        "ABN, company, or contracting arrangements, if you have any.",
    ),
)
"""The categories the Facts page offers, in the order it shows them.

Seeded EMPTY. A fact with placeholder text would be a fabricated fact about the
user (hard rule 1) sitting in the one place the system treats as verbatim
truth, and the derivation layer would happily reason from it. Empty means the
page shows the prompt and nothing else, and no derivation can happen until the
user writes something real.
"""


def seed_facts() -> int:
    """Create the empty fact shells. Returns how many were added.

    Nothing is invented. Each shell is a key, a category and an empty string;
    the prompts live in FACT_SHELLS and are rendered by the page, never stored
    as the fact text.
    """
    from backend.models import Fact, FactCategory

    added = 0
    with session_scope() as session:
        existing = {row.key for row in session.exec(select(Fact)).all()}
        for key, category, _prompt in FACT_SHELLS:
            if key in existing:
                continue
            session.add(
                Fact(
                    key=key, text="", category=FactCategory[category], jurisdiction=None
                )
            )
            added += 1

    log.info("facts_seeded", added=added, total=len(FACT_SHELLS))
    return added


def seed_starter_campaign() -> bool:
    """Create one **inactive** example campaign if no campaign exists at all.

    Returns True if a row was created.

    Discovery only looks at active campaigns, so a freshly migrated database —
    which has none — runs, logs ``no_active_campaigns``, stores nothing and
    reports itself finished. Every part of that is working as designed and the
    combined effect is a system that appears to run and does nothing, which is
    what happened on the first bring-up of the second machine.

    So this seeds the missing piece: a campaign that is visible in the UI,
    obviously a starting point, and **inactive**, so reviewing and editing it is
    a deliberate step rather than something to undo in a hurry. Activating it is
    the user's decision; nothing here can start applying on its own.

    The search terms are a *search configuration*, not a claim about the user —
    hard rule 1 governs the profile, and this touches none of it. They are still
    only a guess at what to look for, which is the main thing to edit. The
    rubric is deliberately left empty so scoring falls back to ``DEFAULT_RUBRIC``
    rather than pinning a copy of it here that would drift.
    """
    with session_scope() as session:
        if session.exec(select(Campaign.id).limit(1)).first() is not None:
            log.info("starter_campaign_skipped", reason="campaign_already_exists")
            return False

        session.add(
            Campaign(
                name=STARTER_CAMPAIGN_NAME,
                # The whole point. Never seed something that can start applying.
                active=False,
                search_terms=["data analyst", "software engineer"],
                locations=["Adelaide SA"],
                work_types=["full-time"],
                # No salary floor: an invented one silently filters out real ads.
                salary_floor=None,
                exclusions={},
                score_floor=60.0,
                # Above the 80.0 default on purpose — the automatic path should
                # start stricter than the shortlist and be relaxed knowingly.
                score_auto_apply=85.0,
                # Ambiguous score means ask, never guess in either direction.
                gray_zone_action=GrayZoneAction.ASK,
                # A cap must exist: check_can_submit passes outright when no cap
                # is configured, so an uncapped campaign is an unlimited one.
                # "default" applies to every platform that has no entry.
                daily_caps={"default": 5},
                rubric={},
            )
        )

    log.info(
        "starter_campaign_seeded",
        name=STARTER_CAMPAIGN_NAME,
        active=False,
        note="inactive by design — review the search terms, then activate it",
    )
    return True


def seed_all() -> None:
    """Seed everything. Safe to run on every startup and after every migration."""
    profile_created = seed_default_profile()
    answers_inserted = seed_answer_bank()
    facts_created = seed_facts()
    campaign_created = seed_starter_campaign()
    log.info(
        "seed_complete",
        profile_created=profile_created,
        answer_bank_inserted=answers_inserted,
        facts_created=facts_created,
        starter_campaign_created=campaign_created,
    )


if __name__ == "__main__":
    seed_all()
