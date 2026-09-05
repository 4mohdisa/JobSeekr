"""The discovery runner itself: window selection, storage, error containment.

The runner had no tests. Its unit pieces did — dedupe, normalise, each source —
but the thing that decides *what window to ask for* and *what to do when a board
dies* was only ever exercised by running it against the live internet.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from backend.base import RawJob, SourceUnavailable
from backend.config import settings
from backend.discovery import run as run_module
from backend.models import Campaign, Job


class RecordingSource:
    """A board that records what it was asked for and returns what it is told."""

    def __init__(self, name: str = "seek", jobs: list[RawJob] | None = None):
        self.name = name
        self.jobs = jobs or []
        self.calls: list[dict] = []

    def search(self, *, terms, locations, hours_old=None, limit=None):
        self.calls.append(
            {
                "terms": terms,
                "locations": locations,
                "hours_old": hours_old,
                "limit": limit,
            }
        )
        return list(self.jobs)


def raw(n: int, *, source: str = "seek") -> RawJob:
    return RawJob(
        source=source,
        source_job_id=str(n),
        url=f"https://au.seek.com/job/{n}",
        title=f"Software Engineer {n}",
        company=f"Company {n}",
        location="Adelaide SA",
        description="Python and SQL.",
        posted_at=None,
        apply_type="unknown",
        raw={},
    )


@pytest.fixture
def factory(tmp_path, monkeypatch):
    """A session factory over a throwaway database, plus one active campaign."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as setup:
        setup.add(
            Campaign(
                name="c",
                active=True,
                search_terms=["python developer"],
                locations=["Adelaide SA"],
                score_floor=60.0,
                score_auto_apply=80.0,
            )
        )
        setup.commit()

    @contextmanager
    def make_session():
        session = Session(engine)
        try:
            yield session
            session.commit()
        finally:
            session.close()

    make_session.engine = engine
    return make_session


def seed_jobs(factory, count: int) -> None:
    with factory() as session:
        for n in range(count):
            session.add(
                Job(
                    source="seek",
                    source_job_id=f"seed-{n}",
                    url=f"https://au.seek.com/job/seed-{n}",
                    title=f"Seeded {n}",
                    company=f"Seeded Co {n}",
                    dedupe_hash=f"seed-hash-{n}",
                )
            )


# ------------------------------------------------------------ the first-run trap


def test_an_empty_database_widens_the_window(factory):
    """The trap this exists to close.

    On a fresh install the incremental window asked three boards for the last
    eight hours, stored almost nothing, and reported success — with nothing in
    the output pointing at the window as the reason.
    """
    source = RecordingSource()
    run_module.run_discovery(sources=[source], session_factory=factory)

    assert source.calls, "the source should have been asked"
    assert source.calls[0]["hours_old"] == settings.discovery_backfill_hours
    assert settings.discovery_backfill_hours > settings.discovery_default_hours_old, (
        "the backfill window must actually be wider than the incremental one"
    )


def test_a_populated_database_stays_incremental(factory):
    """Once there is something to be incremental from, be incremental."""
    seed_jobs(factory, settings.discovery_backfill_threshold)

    source = RecordingSource()
    run_module.run_discovery(sources=[source], session_factory=factory)

    assert source.calls[0]["hours_old"] == settings.discovery_default_hours_old


def test_a_database_just_below_the_threshold_still_backfills(factory):
    seed_jobs(factory, settings.discovery_backfill_threshold - 1)

    source = RecordingSource()
    run_module.run_discovery(sources=[source], session_factory=factory)

    assert source.calls[0]["hours_old"] == settings.discovery_backfill_hours


def test_an_explicit_window_always_wins(factory):
    """--hours-old is the user speaking; nothing may quietly override it."""
    source = RecordingSource()
    run_module.run_discovery(hours_old=3, sources=[source], session_factory=factory)

    assert source.calls[0]["hours_old"] == 3, "an explicit window was overridden"


def test_the_widening_is_announced(factory, caplog):
    """Silently doing something different is how the original trap was set."""
    source = RecordingSource()
    with caplog.at_level("WARNING"):
        run_module.run_discovery(sources=[source], session_factory=factory)

    assert "discovery_backfilling" in caplog.text


# ------------------------------------------------------------------- the runner


def test_discovered_jobs_are_stored_and_counted(factory):
    source = RecordingSource(jobs=[raw(1), raw(2)])
    run = run_module.run_discovery(sources=[source], session_factory=factory)

    assert run.ok
    assert run.counts["sources"]["seek"]["new"] == 2
    with factory() as session:
        assert len(session.exec(select(Job)).all()) == 2


def test_a_second_run_reports_duplicates_not_new_rows(factory):
    source = RecordingSource(jobs=[raw(1), raw(2)])
    run_module.run_discovery(sources=[source], session_factory=factory)
    run = run_module.run_discovery(sources=[source], session_factory=factory)

    assert run.counts["sources"]["seek"]["duplicate"] == 2
    assert run.counts["sources"]["seek"]["new"] == 0


def test_a_dead_board_does_not_kill_the_run(factory):
    """A LinkedIn outage must not silently stop Seek discovery."""

    class DeadSource:
        name = "linkedin"

        def search(self, **kwargs):
            raise RuntimeError("board down")

    alive = RecordingSource(jobs=[raw(1)])
    run = run_module.run_discovery(
        sources=[DeadSource(), alive], session_factory=factory
    )

    assert run.ok is False, "a failed board must be reported, not swallowed"
    assert run.counts["sources"]["seek"]["new"] == 1, "the healthy board still ran"
    assert any(e["source"] == "linkedin" for e in run.errors)


def test_a_dry_run_stores_nothing(factory):
    source = RecordingSource(jobs=[raw(1), raw(2)])
    run_module.run_discovery(sources=[source], dry_run=True, session_factory=factory)

    with factory() as session:
        assert session.exec(select(Job)).all() == []


def test_the_returned_run_is_readable_after_the_session_closes(factory):
    """session_scope commits on exit, which expires every tracked instance.

    Reading run.ok afterwards used to raise DetachedInstanceError, turning every
    successful discovery into a traceback and a non-zero exit.
    """
    run = run_module.run_discovery(sources=[RecordingSource()], session_factory=factory)
    assert run.ok in (True, False)
    assert isinstance(run.counts, dict)


# --------------------------------------------------- a total outage is not a quiet day


class DeadSource:
    """A board that raises, the way a source signals it could not be reached."""

    def __init__(self, name: str = "seek"):
        self.name = name

    def search(self, **kwargs):
        raise SourceUnavailable(f"{self.name}: every endpoint unreachable")


class SilentlyEmptySource:
    """A board that swallows its own failure and returns nothing.

    This is the shape every real source had: transport errors were caught
    inside ``search`` and turned into ``[]``, so the runner never saw them.
    """

    def __init__(self, name: str = "seek"):
        self.name = name

    def search(self, **kwargs):
        return []


def test_a_run_where_every_source_failed_is_not_ok(factory):
    """The bug this replaces: total outage recorded as a clean, quiet run.

    Observed on a real machine — all three boards blocked by a proxy, every
    failure logged loudly, and the Run row still read ok=True with error=0 on
    every source. Nothing downstream could tell that from a Sunday afternoon.
    """
    run = run_module.run_discovery(
        sources=[DeadSource("seek"), DeadSource("linkedin"), DeadSource("indeed")],
        session_factory=factory,
    )

    assert run.ok is False, "every board failed and the run still reported ok"
    for name in ("seek", "linkedin", "indeed"):
        assert run.counts["sources"][name]["error"] == 1, (
            f"{name} failed but its bucket reported no error"
        )
    assert run.counts["sources_succeeded"] == []


def test_one_surviving_source_still_counts_the_dead_ones(factory):
    """Partial outage: ok is False, but the healthy board's work is kept."""
    run = run_module.run_discovery(
        sources=[DeadSource("linkedin"), RecordingSource("seek", jobs=[raw(1)])],
        session_factory=factory,
    )

    assert run.counts["sources"]["linkedin"]["error"] == 1
    assert run.counts["sources"]["seek"]["new"] == 1, "the healthy board still stored"
    assert run.counts["sources_succeeded"] == ["seek"]
    assert run.ok is False, "a dead board is still an error worth reporting"


def test_a_genuinely_quiet_day_is_still_ok(factory):
    """Zero ads with every board answering is success, and must stay success.

    The counterpart to the test above: if this ever goes red, the fix has
    overcorrected and an empty market now looks like an outage.
    """
    run = run_module.run_discovery(
        sources=[RecordingSource("seek"), RecordingSource("linkedin")],
        session_factory=factory,
    )

    assert run.ok is True, "an empty market must not be reported as a failure"
    assert run.counts["sources"]["seek"]["fetched"] == 0
    assert sorted(run.counts["sources_succeeded"]) == ["linkedin", "seek"]


def test_a_source_that_returns_nothing_without_failing_counts_as_succeeded(factory):
    """`ok` keys off the source answering, not off it finding something."""
    run = run_module.run_discovery(
        sources=[SilentlyEmptySource("seek")], session_factory=factory
    )

    assert run.ok is True
    assert run.counts["sources_succeeded"] == ["seek"]


def test_no_active_campaigns_is_not_a_successful_run(factory, caplog):
    """A fresh database ran no sources at all — that is not success either."""
    with factory() as session:
        for campaign in session.exec(select(Campaign)).all():
            campaign.active = False
            session.add(campaign)

    with caplog.at_level("ERROR"):
        run = run_module.run_discovery(
            sources=[RecordingSource()], session_factory=factory
        )

    assert run.ok is False, "a run that asked nobody anything reported success"
    assert "discovery_no_source_succeeded" in caplog.text
    assert "no_active_campaign" in caplog.text, (
        "an unconfigured system and a total outage need different fixes and "
        "must not be reported identically"
    )


def test_the_failure_is_reported_loudly(factory, caplog):
    """Hard rule 9. A silent outage is the thing being fixed."""
    with caplog.at_level("ERROR"):
        run_module.run_discovery(sources=[DeadSource()], session_factory=factory)

    assert "discovery_no_source_succeeded" in caplog.text
    assert "every_source_failed" in caplog.text
