"""The starter campaign: it must exist, and it must not be able to run.

Discovery reads active campaigns only. A freshly migrated database has none, so
it runs, logs `no_active_campaigns`, stores nothing and reports itself finished
— every part working as designed, adding up to a system that appears to run and
does nothing. Seeding a campaign fixes that; seeding an *active* one would be
worse than the bug, because a campaign nobody has read would start applying.

So the two assertions that matter here are "a campaign exists" and "it is
switched off", and everything else guards the second one.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from backend import seed as seed_module
from backend.models import Campaign, GrayZoneAction
from backend.seed import STARTER_CAMPAIGN_NAME, seed_starter_campaign

# Its own database, not the suite-wide one. These tests need to assert on "no
# campaigns exist", and deleting from the shared database trips the foreign key
# that other modules' jobs hold on campaign — a failure that only appears when
# the whole suite runs, in whichever file happens to go first.


@pytest.fixture(autouse=True)
def session_scope(monkeypatch):
    """Point backend.seed at a throwaway database for the whole module."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)

    @contextmanager
    def make_session():
        session = Session(engine)
        try:
            yield session
            session.commit()
        finally:
            session.close()

    monkeypatch.setattr(seed_module, "session_scope", make_session)
    return make_session


def _starter(session_scope) -> Campaign:
    with session_scope() as session:
        campaign = session.exec(
            select(Campaign).where(Campaign.name == STARTER_CAMPAIGN_NAME)
        ).one()
        session.expunge(campaign)
        return campaign


def test_a_fresh_database_gets_a_campaign(session_scope):
    assert seed_starter_campaign() is True

    with session_scope() as session:
        names = [c.name for c in session.exec(select(Campaign)).all()]
    assert names == [STARTER_CAMPAIGN_NAME]


def test_the_starter_campaign_is_inactive(session_scope):
    """The load-bearing assertion. An active seed applies to jobs unreviewed."""
    seed_starter_campaign()
    assert _starter(session_scope).active is False, (
        "the seeded campaign is live; it would start discovering and applying "
        "before the user has read it"
    )


def test_discovery_does_not_pick_it_up_while_it_is_inactive(session_scope):
    """Proved through the query discovery actually uses, not by reading a flag."""
    seed_starter_campaign()

    with session_scope() as session:
        # Written exactly as discovery writes it, so this breaks if that changes.
        active = session.exec(select(Campaign).where(Campaign.active == True)).all()

    assert active == [], "an inactive campaign was still selected for discovery"


def test_it_is_not_seeded_when_the_user_already_has_a_campaign(session_scope):
    """Never add a second campaign to a database someone is already using."""
    with session_scope() as session:
        session.add(
            Campaign(
                name="mine",
                active=True,
                search_terms=["python"],
                locations=["Adelaide SA"],
                score_floor=60.0,
                score_auto_apply=80.0,
            )
        )

    assert seed_starter_campaign() is False

    with session_scope() as session:
        names = sorted(c.name for c in session.exec(select(Campaign)).all())
    assert names == ["mine"]


def test_seeding_twice_creates_one_campaign(session_scope):
    assert seed_starter_campaign() is True
    assert seed_starter_campaign() is False

    with session_scope() as session:
        rows = session.exec(
            select(Campaign).where(Campaign.name == STARTER_CAMPAIGN_NAME)
        ).all()
    assert len(rows) == 1


def test_re_seeding_does_not_reactivate_a_campaign_the_user_paused(session_scope):
    """Pausing must stick. Re-seeding after an upgrade must not undo it."""
    seed_starter_campaign()
    with session_scope() as session:
        campaign = session.exec(
            select(Campaign).where(Campaign.name == STARTER_CAMPAIGN_NAME)
        ).one()
        campaign.active = True
        campaign.search_terms = ["the user's own edit"]
        session.add(campaign)

    seed_starter_campaign()

    edited = _starter(session_scope)
    assert edited.active is True, "re-seeding switched off a campaign the user ran"
    assert edited.search_terms == ["the user's own edit"], "re-seeding overwrote edits"


def test_it_carries_a_daily_cap(session_scope):
    """check_can_submit passes outright when no cap is set, so absence is unlimited."""
    seed_starter_campaign()
    caps = _starter(session_scope).daily_caps

    assert caps, "a campaign with no daily cap is an uncapped campaign"
    assert caps.get("default", 0) > 0


def test_it_asks_rather_than_guessing_in_the_gray_zone(session_scope):
    seed_starter_campaign()
    assert _starter(session_scope).gray_zone_action == GrayZoneAction.ASK


def test_it_invents_no_salary_floor(session_scope):
    """A made-up floor silently filters out ads the user would have wanted."""
    seed_starter_campaign()
    assert _starter(session_scope).salary_floor is None


def test_it_leaves_the_rubric_empty_so_the_default_is_used(session_scope):
    """Pinning a copy of DEFAULT_RUBRIC here is how the two drift apart."""
    seed_starter_campaign()
    assert _starter(session_scope).rubric == {}

    from backend.scoring.rubric import rubric_for

    rubric, version = rubric_for(_starter(session_scope))
    assert rubric, "an empty rubric must fall back to the default, not score on nothing"
    assert version >= 1


def test_the_auto_apply_bar_is_above_the_shortlist_bar(session_scope):
    """Automatic submission should start stricter than the shortlist."""
    seed_starter_campaign()
    campaign = _starter(session_scope)
    assert campaign.score_auto_apply > campaign.score_floor


def test_seed_all_includes_the_campaign(session_scope):
    from backend.seed import seed_all

    seed_all()
    with session_scope() as session:
        assert (
            session.exec(
                select(Campaign).where(Campaign.name == STARTER_CAMPAIGN_NAME)
            ).first()
            is not None
        )
