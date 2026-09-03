"""Preference memory, and the rules that keep it from inventing things.

Two of these matter far more than the rest:

* an inferred preference changes nothing until the user confirms it
* a fact about the user can never be inferred at all

Everything else is spam control.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from backend import preferences
from backend.models import (
    Job,
    JobStatus,
    Preference,
    PreferenceSource,
    PreferenceStatus,
)


@pytest.fixture
def session():
    from backend.models import Campaign, GrayZoneAction

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        # Campaign 7 exists because campaign-scoped preferences carry a real
        # foreign key — the scope is enforced by the schema, not by convention.
        s.add(
            Campaign(
                id=7,
                name="scoped",
                active=True,
                search_terms=["dev"],
                locations=["Adelaide SA"],
                score_floor=60.0,
                score_auto_apply=80.0,
                gray_zone_action=GrayZoneAction.ASK,
                daily_caps={"default": 5},
            )
        )
        s.flush()
        yield s


def skip_jobs(session, *, company: str = "Globex", title: str = "Data Analyst", count: int = 5):
    for index in range(count):
        session.add(
            Job(
                source="seek",
                source_job_id=f"{company}-{index}",
                url=f"https://example.com/{company}/{index}",
                title=title,
                company=company,
                location="Adelaide SA",
                dedupe_hash=f"{company}-{index}",
                status=JobStatus.SKIPPED,
                discovered_at=datetime.now(UTC),
            )
        )
    session.flush()


# =========================================================================
# The hard rule: facts are never inferred
# =========================================================================


@pytest.mark.parametrize(
    "key",
    [
        "work_rights",
        "full working rights in Australia",
        "visa_status",
        "drivers_licence",
        "forklift license",
        "police_clearance",
        "highest_qualification",
        "date_of_birth",
        "notice_period",
        "preferred start date",
        "current_salary",
        "referee_details",
    ],
)
def test_a_fact_about_the_user_can_never_be_inferred(session, key):
    """Hard rule 1. An inferred fact is a fabricated one, however good the evidence."""
    with pytest.raises(preferences.FactInferenceRefused):
        preferences.propose(
            session, key=key, value="Yes", evidence="seen on four applications"
        )

    assert session.exec(select(Preference)).all() == [], "nothing may be written"


@pytest.mark.parametrize(
    "key", ["work_rights", "drivers_licence", "preferred start date"]
)
def test_the_same_fact_is_fine_when_the_user_sets_it(session, key):
    """The rule is about the *source*, not the subject."""
    row = preferences.set(
        session, key=key, value="Yes", source=PreferenceSource.USER_SET
    )
    assert row.status is PreferenceStatus.ACTIVE


def test_refusal_is_not_a_downgrade_to_a_proposal(session):
    """A fabricated fact awaiting approval is still fabricated.

    Presenting it for confirmation invites the user to wave through something
    the system had no business deriving, so it raises rather than proposing.
    """
    with pytest.raises(preferences.FactInferenceRefused):
        preferences.propose(session, key="visa_status", value="Citizen", evidence="x")

    assert not session.exec(
        select(Preference).where(Preference.status == PreferenceStatus.PROPOSED)
    ).all()


def test_an_ordinary_preference_is_not_mistaken_for_a_fact(session):
    for key in ("exclude_company:Globex", "referral_source", "preferred_industry"):
        assert not preferences.is_fact_key(key), key


def test_an_observed_form_field_that_is_fact_shaped_is_declined(session):
    """Start date and licences show up on forms constantly. Still facts."""
    assert (
        preferences.observed_field(session, key="preferred start date", value="2 weeks")
        is None
    )
    assert preferences.observed_field(session, key="referral_source", value="Seek") is not None


# =========================================================================
# A proposal changes nothing
# =========================================================================


def test_an_inferred_preference_does_not_take_effect(session):
    preferences.propose(
        session, key="exclude_company:Globex", value="Globex", evidence="you skipped 5"
    )
    session.flush()

    assert preferences.active(session) == {}, "a proposal must not affect behaviour"
    assert preferences.get(session, "exclude_company:Globex") is None


def test_confirming_makes_it_take_effect(session):
    row = preferences.propose(
        session, key="exclude_company:Globex", value="Globex", evidence="you skipped 5"
    )
    session.flush()

    preferences.confirm(session, row.id)
    session.flush()

    assert preferences.get(session, "exclude_company:Globex") == "Globex"


def test_confirming_records_it_as_asked_not_user_set(session):
    """The system raised it and the user agreed. Weaker than them stating it."""
    row = preferences.propose(session, key="k", value="v", evidence="e")
    session.flush()
    preferences.confirm(session, row.id)
    assert row.source is PreferenceSource.ASKED
    assert row.times_confirmed == 1


def test_rejecting_keeps_the_row_so_it_is_not_proposed_again(session):
    row = preferences.propose(session, key="k", value="v", evidence="e")
    session.flush()
    preferences.reject(session, row.id)
    session.flush()

    assert row.status is PreferenceStatus.REJECTED
    assert preferences.active(session) == {}
    assert session.exec(select(Preference)).all(), "the row survives as a record"


def test_a_user_set_preference_is_not_overwritten_by_an_inference(session):
    preferences.set(
        session, key="preferred_industry", value="health", source=PreferenceSource.USER_SET
    )
    session.flush()

    preferences.propose(
        session, key="preferred_industry", value="mining", evidence="you applied to 5"
    )
    session.flush()

    assert preferences.get(session, "preferred_industry") == "health"


# =========================================================================
# Learning from skips
# =========================================================================


def test_five_skips_at_one_company_proposes_an_exclusion(session):
    skip_jobs(session, company="Globex", count=preferences.SKIPS_BEFORE_PROPOSAL)

    written = preferences.propose_from_skips(session)
    session.flush()

    keys = [row.key for row in written]
    assert "exclude_company:Globex" in keys
    assert all(row.status is PreferenceStatus.PROPOSED for row in written)


def test_four_skips_is_not_enough(session):
    skip_jobs(session, company="Globex", count=preferences.SKIPS_BEFORE_PROPOSAL - 1)
    assert not [
        row
        for row in preferences.propose_from_skips(session)
        if row.key == "exclude_company:Globex"
    ]


def test_a_repeated_title_keyword_is_proposed(session):
    skip_jobs(session, company="A", title="Senior Salesforce Consultant", count=2)
    skip_jobs(session, company="B", title="Salesforce Developer", count=3)

    keys = [row.key for row in preferences.propose_from_skips(session)]
    assert "exclude_keyword:salesforce" in keys


def test_common_title_words_are_not_proposed(session):
    """Without stopwords the user is asked to exclude every senior role."""
    skip_jobs(session, company="A", title="Senior Data Analyst", count=6)

    keys = [row.key for row in preferences.propose_from_skips(session)]
    assert "exclude_keyword:senior" not in keys
    assert "exclude_keyword:adelaide" not in keys


def test_a_rejected_proposal_is_not_raised_again(session):
    skip_jobs(session, company="Globex", count=6)

    first = preferences.propose_from_skips(session)
    session.flush()
    for row in first:
        preferences.reject(session, row.id)
    session.flush()

    assert preferences.propose_from_skips(session) == [], (
        "re-proposing something the user declined is exactly the spam this guards"
    )


def test_skips_outside_the_window_do_not_count(session):
    skip_jobs(session, company="Globex", count=6)
    for job in session.exec(select(Job)).all():
        job.discovered_at = datetime.now(UTC) - timedelta(days=90)
    session.flush()

    assert preferences.propose_from_skips(session, hours=720) == []


# =========================================================================
# Not spam
# =========================================================================


def test_no_more_than_three_proposals_a_day(session):
    for index in range(8):
        preferences.propose(session, key=f"k{index}", value="v", evidence=f"e{index}")
    session.flush()

    assert len(preferences.pending(session)) == preferences.DAILY_PROPOSAL_CAP


def test_the_most_confident_proposals_go_first(session):
    preferences.propose(session, key="low", value="v", evidence="e", confidence=0.1)
    preferences.propose(session, key="high", value="v", evidence="e", confidence=0.9)
    session.flush()

    assert preferences.pending(session, limit=1)[0].key == "high"


def test_a_proposal_already_asked_today_is_not_asked_again(session):
    preferences.propose(session, key="k", value="v", evidence="e")
    session.flush()

    assert preferences.digest_lines(session), "asked once"
    session.flush()
    assert preferences.digest_lines(session) == [], "not asked twice the same day"


def test_two_ignores_retire_a_proposal(session):
    row = preferences.propose(session, key="k", value="v", evidence="e")
    session.flush()

    for _ in range(preferences.IGNORES_BEFORE_RETIREMENT):
        preferences.mark_ignored(session, row.id)
    session.flush()

    assert row.status is PreferenceStatus.RETIRED
    assert preferences.pending(session) == []


def test_the_sweep_ages_out_unanswered_proposals(session):
    row = preferences.propose(session, key="k", value="v", evidence="e")
    session.flush()
    preferences.digest_lines(session)
    session.flush()

    row.last_asked_at = datetime.now(UTC) - timedelta(days=5)
    session.flush()

    assert preferences.sweep_ignored(session) == 1
    assert row.times_ignored == 1


def test_the_digest_is_silent_when_there_is_nothing_to_propose(session):
    assert preferences.digest_lines(session) == []


def test_the_digest_explains_the_evidence_and_offers_both_answers(session):
    preferences.propose(
        session,
        key="exclude_company:Globex",
        value="Globex",
        evidence="you skipped 5 jobs at Globex",
    )
    session.flush()

    body = "\n".join(preferences.digest_lines(session))
    assert "you skipped 5 jobs at Globex" in body
    assert "/yes" in body and "/no" in body, "declining must be as easy as accepting"


def test_a_naive_last_asked_timestamp_does_not_crash(session):
    """SQLite hands back naive datetimes; comparing them to aware ones raises."""
    row = preferences.propose(session, key="k", value="v", evidence="e")
    row.last_asked_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=3)
    session.flush()

    assert preferences.pending(session), "must compare, not raise"
    assert preferences.sweep_ignored(session) == 1


# =========================================================================
# Scope
# =========================================================================


def test_a_campaign_preference_beats_a_global_one(session):
    preferences.set(
        session, key="tone", value="formal", source=PreferenceSource.USER_SET
    )
    preferences.set(
        session,
        key="tone",
        value="direct",
        source=PreferenceSource.USER_SET,
        campaign_id=7,
    )
    session.flush()

    assert preferences.get(session, "tone") == "formal"
    assert preferences.get(session, "tone", campaign_id=7) == "direct"


def test_another_campaigns_preference_is_not_visible(session):
    preferences.set(
        session,
        key="tone",
        value="direct",
        source=PreferenceSource.USER_SET,
        campaign_id=7,
    )
    session.flush()
    assert preferences.get(session, "tone", campaign_id=9) is None
