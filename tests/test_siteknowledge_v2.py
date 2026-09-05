"""Site knowledge that learns from success, and never guesses.

The layer already healed: when the top strategy broke, a lower one took over.
What it could not do was notice that the top one had been broken for a month, or
find an element nobody had written a selector for, or undo a bad edit. Each of
those is a thing here.

The assertion that matters most is the one about proposals. A selector derived
from a page the system could not otherwise read is a GUESS about where the
Submit button is — the one place in this project where a guess would be acted on
rather than abstained from. It must never resolve anything until a person says
yes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine

from backend import failures
from backend.models import FailureType
from backend.siteknowledge import (
    Element,
    ElementNotFound,
    Strategy,
    load,
    rollback,
)
from backend.siteknowledge import health as site_health
from backend.siteknowledge.vocabulary import shared_candidates
from tests.test_siteknowledge import FakePage, knowledge_with


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


# --------------------------------------------------------------------------
# 1 — learning from success, not only from failure
# --------------------------------------------------------------------------


def test_the_strategy_that_worked_is_credited() -> None:
    knowledge = knowledge_with(
        Strategy(type="testid", value="apply", attr="data-automation"),
        Strategy(type="css", value="button.apply"),
    )
    knowledge.resolve(FakePage({"[data-automation='apply']"}), "apply_button")

    [top, _fallback] = knowledge.elements["apply_button"].strategies
    assert (top.success_count, top.fail_count) == (1, 0)


def test_the_strategies_tried_before_the_winner_are_debited() -> None:
    """The half that was missing. Promotion happened; the evidence did not.

    Without this a selector that has been broken for a month carries exactly the
    same weight as one written from a guess, because nothing ever recorded that
    it had been tried and failed.
    """
    knowledge = knowledge_with(
        Strategy(type="testid", value="apply", attr="data-automation"),
        Strategy(type="css", value="button.apply"),
    )
    knowledge.resolve(FakePage({"button.apply"}), "apply_button")

    broken, working = knowledge.elements["apply_button"].strategies
    assert (broken.success_count, broken.fail_count) == (0, 1)
    assert (working.success_count, working.fail_count) == (1, 0)


def test_a_total_failure_debits_every_strategy() -> None:
    knowledge = knowledge_with(
        Strategy(type="testid", value="apply", attr="data-automation"),
        Strategy(type="css", value="button.apply"),
    )
    with pytest.raises(ElementNotFound):
        knowledge.resolve(FakePage(set()), "apply_button")

    assert [s.fail_count for s in knowledge.elements["apply_button"].strategies] == [
        1,
        1,
    ]


def test_a_proven_strategy_outranks_a_more_durable_broken_one() -> None:
    """The reordering, stated as an order rather than as counters.

    A testid is more durable than a CSS class in theory. A testid that has
    failed eleven times is not more durable than a CSS class that has worked
    eleven times, and the ordering has to say so — otherwise every resolution
    pays for the same wrong guess first.
    """
    durable = Strategy(type="testid", value="apply", attr="data-automation")
    durable.fail_count = 11
    scrappy = Strategy(type="css", value="button.apply")
    scrappy.success_count = 11
    element = Element(key="apply_button", strategies=[durable, scrappy])

    assert [s.type for s in element.ordered()] == ["css", "testid"]


def test_an_untried_strategy_sits_between_proven_and_broken() -> None:
    """What the Laplace prior is for. 0.5 is a real position, not a shrug."""
    broken = Strategy(type="css", value="button.old")
    broken.fail_count = 5
    untried = Strategy(type="css", value="button.new")
    proven = Strategy(type="css", value="button.works")
    proven.success_count = 5
    element = Element(key="apply_button", strategies=[broken, untried, proven])

    assert [s.value for s in element.ordered()] == [
        "button.works",
        "button.new",
        "button.old",
    ]


def test_the_last_working_strategy_still_goes_first() -> None:
    """Freshness beats a better lifetime record: a site that changed, changed."""
    stale = Strategy(type="css", value="button.old")
    stale.success_count = 50
    fresh = Strategy(type="css", value="button.new")
    element = Element(
        key="apply_button",
        strategies=[stale, fresh],
        last_working_strategy=fresh.id,
    )

    assert element.ordered()[0].value == "button.new"


# --------------------------------------------------------------------------
# 2 — the counters, finally read
# --------------------------------------------------------------------------


def test_confidence_is_smoothed_so_one_success_is_not_certainty() -> None:
    element = Element(key="apply_button", success_count=1, fail_count=0)
    assert element.confidence == pytest.approx(2 / 3)


def test_an_element_below_the_threshold_is_reported_as_degrading(
    tmp_path, monkeypatch
) -> None:
    """The whole point of computing confidence: say so BEFORE it fails outright."""
    monkeypatch.setattr(site_health.settings, "data_dir", tmp_path)
    _write_platform(
        tmp_path / "siteknowledge" / "acme",
        success=3,
        fail=7,
        strategies=[{"type": "css", "value": "b"}],
    )

    [element] = site_health.degrading()

    assert element.platform == "acme"
    assert element.confidence < site_health.DEGRADED_CONFIDENCE


def test_a_healthy_element_is_not_reported(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(site_health.settings, "data_dir", tmp_path)
    _write_platform(
        tmp_path / "siteknowledge" / "acme",
        success=30,
        fail=1,
        strategies=[{"type": "css", "value": "b"}],
    )

    assert site_health.degrading() == []


def test_too_few_observations_is_not_evidence_of_anything(
    tmp_path, monkeypatch
) -> None:
    """Smoothing puts an untried element at 0.5, which is below the threshold.

    Without a minimum, every element of a fresh install is reported as degrading
    on the first digest — a report that is wrong on day one is a report nobody
    reads on day thirty.
    """
    monkeypatch.setattr(site_health.settings, "data_dir", tmp_path)
    _write_platform(
        tmp_path / "siteknowledge" / "acme",
        success=0,
        fail=1,
        strategies=[{"type": "css", "value": "b"}],
    )

    assert site_health.degrading() == []


# --------------------------------------------------------------------------
# 3 — generating a strategy, and never using it unasked
# --------------------------------------------------------------------------


def test_a_derived_strategy_is_proposed_when_everything_recorded_fails() -> None:
    knowledge = knowledge_with(Strategy(type="css", value="button.gone"))
    # The recorded selector is gone; the page still has a button called Apply.
    page = FakePage({"role=button[name=/.*Apply.*/i]"})

    with pytest.raises(ElementNotFound):
        knowledge.resolve(page, "apply_button")

    [proposal] = knowledge.elements["apply_button"].proposals
    assert proposal.selector == "role=button[name=/.*Apply.*/i]"
    assert proposal.proposed is True


def test_a_proposal_never_resolves_anything() -> None:
    """The safety property. A derived selector is a guess about where a control
    is, and this system does not act on guesses."""
    knowledge = knowledge_with(Strategy(type="css", value="button.gone"))
    page = FakePage({"role=button[name=/.*Apply.*/i]"})

    with pytest.raises(ElementNotFound):
        knowledge.resolve(page, "apply_button")
    # Second pass, same page: the proposal is there and still does not answer.
    with pytest.raises(ElementNotFound):
        knowledge.resolve(page, "apply_button")

    assert len(knowledge.elements["apply_button"].proposals) == 1


def test_accepting_a_proposal_makes_it_a_strategy() -> None:
    knowledge = knowledge_with(Strategy(type="css", value="button.gone"))
    page = FakePage({"role=button[name=/.*Apply.*/i]"})
    with pytest.raises(ElementNotFound):
        knowledge.resolve(page, "apply_button")

    knowledge.accept_proposal("apply_button", "role=button[name=/.*Apply.*/i]")

    assert knowledge.resolve(page, "apply_button") is not None
    assert knowledge.elements["apply_button"].proposals == []


def test_rejecting_a_proposal_deletes_it_so_the_next_failure_derives_again() -> None:
    knowledge = knowledge_with(Strategy(type="css", value="button.gone"))
    page = FakePage({"role=button[name=/.*Apply.*/i]"})
    with pytest.raises(ElementNotFound):
        knowledge.resolve(page, "apply_button")

    assert knowledge.reject_proposal("apply_button", "role=button[name=/.*Apply.*/i]")
    assert knowledge.elements["apply_button"].proposals == []

    with pytest.raises(ElementNotFound):
        knowledge.resolve(page, "apply_button")
    assert len(knowledge.elements["apply_button"].proposals) == 1


def test_the_failure_hook_is_given_the_suggestion(monkeypatch) -> None:
    """So the user gets a yes/no question instead of "go and look at the site"."""
    seen: list[tuple] = []
    monkeypatch.setattr(
        "backend.siteknowledge.on_all_strategies_failed",
        lambda *args: seen.append(args),
    )
    knowledge = knowledge_with(Strategy(type="css", value="button.gone"))

    with pytest.raises(ElementNotFound):
        knowledge.resolve(FakePage({"role=button[name=/.*Apply.*/i]"}), "apply_button")

    assert seen[0][-1] == "role=button[name=/.*Apply.*/i]"


def test_nothing_is_proposed_when_the_element_is_not_there_at_all() -> None:
    knowledge = knowledge_with(Strategy(type="css", value="button.gone"))

    with pytest.raises(ElementNotFound):
        knowledge.resolve(FakePage(set()), "apply_button")

    assert knowledge.elements["apply_button"].proposals == []


# --------------------------------------------------------------------------
# 4 — shared vocabulary
# --------------------------------------------------------------------------


def test_a_platform_inherits_generic_candidates_it_does_not_have(tmp_path) -> None:
    _write_platform(
        tmp_path / "newats",
        success=0,
        fail=0,
        strategies=[{"type": "css", "value": "b"}],
    )
    knowledge = load("newats", directory=tmp_path / "newats")

    selectors = {s.selector for s in knowledge.elements["apply_button"].strategies}
    assert "role=button[name=/.*Apply.*/i]" in selectors


def test_a_platform_strategy_is_tried_before_a_generic_one(tmp_path) -> None:
    """Platform overrides stay on top — among strategies nobody has tried."""
    _write_platform(
        tmp_path / "newats",
        success=0,
        fail=0,
        strategies=[{"type": "css", "value": "button.acme"}],
    )
    knowledge = load("newats", directory=tmp_path / "newats")

    assert knowledge.elements["apply_button"].ordered()[0].selector == "button.acme"


def test_a_generic_candidate_that_works_outranks_a_platform_one_that_does_not(
    tmp_path,
) -> None:
    """Evidence is checked before provenance. It has to be, or the fallback is
    tried last forever and the platform's dead selector is tried first forever."""
    _write_platform(
        tmp_path / "newats",
        success=0,
        fail=0,
        strategies=[{"type": "css", "value": "button.acme", "fail_count": 9}],
    )
    knowledge = load("newats", directory=tmp_path / "newats")
    shared = next(s for s in knowledge.elements["apply_button"].strategies if s.shared)
    shared.success_count = 9

    assert knowledge.elements["apply_button"].ordered()[0].shared is True


def test_a_duplicate_generic_candidate_does_not_displace_the_platform_entry(
    tmp_path,
) -> None:
    """A platform that already names the same role keeps its own row — and with
    it, whatever evidence that row has accumulated."""
    _write_platform(
        tmp_path / "newats",
        success=0,
        fail=0,
        strategies=[
            {"type": "role", "role": "button", "name": "*Apply*", "success_count": 4}
        ],
    )
    knowledge = load("newats", directory=tmp_path / "newats")

    matching = [
        s
        for s in knowledge.elements["apply_button"].strategies
        if s.selector == "role=button[name=/.*Apply.*/i]"
    ]
    assert len(matching) == 1
    assert matching[0].success_count == 4
    assert matching[0].shared is False


def test_generic_candidates_are_never_written_into_a_platform_file(tmp_path) -> None:
    """Otherwise a correction to the vocabulary can never reach eleven forks."""
    directory = tmp_path / "newats"
    _write_platform(
        directory, success=0, fail=0, strategies=[{"type": "css", "value": "b"}]
    )
    knowledge = load("newats", directory=directory)
    knowledge.save(force=True, reason="test")

    written = json.loads((directory / "elements.json").read_text())
    strategies = written["elements"]["apply_button"]["strategies"]
    assert [s["value"] for s in strategies] == ["b"]


def test_every_shared_call_returns_its_own_objects() -> None:
    """Nine platforms sharing one object would pool their evidence into it."""
    first = shared_candidates("apply_button")
    first[0].success_count = 5

    assert shared_candidates("apply_button")[0].success_count == 0


# --------------------------------------------------------------------------
# 5 — drift as a trend
# --------------------------------------------------------------------------


def test_churn_compares_this_window_against_the_last(session: Session) -> None:
    _drift(session, "linkedin", "submit_button", hours_ago=10)
    _drift(session, "linkedin", "next_button", hours_ago=20)
    _drift(session, "linkedin", "submit_button", hours_ago=200)

    [churn] = site_health.platform_churn(session, hours=168)

    assert (churn.events, churn.previous_events) == (2, 1)
    assert churn.accelerating is True


def test_the_fastest_moving_platform_is_first(session: Session) -> None:
    _drift(session, "seek", "submit_button", hours_ago=1)
    for element in ("submit_button", "next_button", "file_input"):
        _drift(session, "workday", element, hours_ago=1)

    assert [row.platform for row in site_health.platform_churn(session)] == [
        "workday",
        "seek",
    ]


def test_an_element_quiet_since_last_week_is_named(session: Session) -> None:
    """Whether last week's fix held. Named as "quiet", not "fixed" — nothing here
    knows the difference between a repair and an unvisited site."""
    _drift(session, "linkedin", "submit_button", hours_ago=200)
    _drift(session, "linkedin", "next_button", hours_ago=10)

    [churn] = site_health.platform_churn(session, hours=168)

    assert churn.healed == ["submit_button"]


def test_an_abstention_is_not_drift(session: Session) -> None:
    """The ledger holds every kind of failure; churn is about selectors."""
    failures.record(
        session,
        platform="seek",
        failure_type=FailureType.ANSWER_ABSTAINED,
        question="what is your notice period",
    )

    assert site_health.platform_churn(session) == []


# --------------------------------------------------------------------------
# 6 — versions and rollback
# --------------------------------------------------------------------------


def test_saving_keeps_the_version_it_replaced(tmp_path) -> None:
    directory = tmp_path / "acme"
    _write_platform(
        directory, success=0, fail=0, strategies=[{"type": "css", "value": "original"}]
    )
    knowledge = load("acme", directory=directory)
    knowledge.elements["apply_button"].strategies = [
        Strategy(type="css", value="replacement")
    ]
    knowledge.save(force=True, reason="capture ingest")

    [entry] = knowledge.history()
    assert entry["reason"] == "capture ingest"
    kept = json.loads((directory / "history" / "0001-elements.json").read_text())
    assert kept["elements"]["apply_button"]["strategies"][0]["value"] == "original"


def test_a_bad_ingest_can_be_undone(tmp_path) -> None:
    directory = tmp_path / "acme"
    _write_platform(
        directory, success=0, fail=0, strategies=[{"type": "css", "value": "original"}]
    )
    knowledge = load("acme", directory=directory)
    knowledge.elements["apply_button"].strategies = [
        Strategy(type="css", value="ruined-by-a-bad-capture")
    ]
    knowledge.save(force=True, reason="capture ingest")

    assert rollback("acme", 1, directory=directory)

    restored = load("acme", directory=directory)
    values = [
        s.value for s in restored.elements["apply_button"].strategies if not s.shared
    ]
    assert values == ["original"]


def test_the_rollback_itself_is_undoable(tmp_path) -> None:
    """Rolling back to the wrong version must not be the unrecoverable write."""
    directory = tmp_path / "acme"
    _write_platform(
        directory, success=0, fail=0, strategies=[{"type": "css", "value": "original"}]
    )
    knowledge = load("acme", directory=directory)
    knowledge.elements["apply_button"].strategies = [Strategy(type="css", value="v2")]
    knowledge.save(force=True, reason="capture ingest")

    rollback("acme", 1, directory=directory)

    reasons = [entry["reason"] for entry in load("acme", directory=directory).history()]
    assert reasons == ["capture ingest", "superseded by rollback to v1"]


def test_rolling_back_to_a_version_that_was_never_kept_says_no(tmp_path) -> None:
    directory = tmp_path / "acme"
    _write_platform(
        directory, success=0, fail=0, strategies=[{"type": "css", "value": "original"}]
    )

    assert rollback("acme", 99, directory=directory) is False


def test_a_save_that_changed_nothing_writes_no_version(tmp_path) -> None:
    """Otherwise the history fills with identical entries and buys nothing."""
    directory = tmp_path / "acme"
    _write_platform(
        directory, success=0, fail=0, strategies=[{"type": "css", "value": "original"}]
    )
    knowledge = load("acme", directory=directory)
    knowledge.save()

    assert knowledge.history() == []


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _write_platform(
    directory, *, success: int, fail: int, strategies: list[dict]
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "elements.json").write_text(
        json.dumps(
            {
                "platform": directory.name,
                "elements": {
                    "apply_button": {
                        "key": "apply_button",
                        "required": True,
                        "strategies": strategies,
                        "success_count": success,
                        "fail_count": fail,
                    }
                },
            },
            indent=2,
        )
    )


def _drift(session: Session, platform: str, element: str, *, hours_ago: float) -> None:
    event = failures.record(
        session,
        platform=platform,
        failure_type=FailureType.SELECTOR_DRIFT,
        element_id=element,
    )
    event.occurred_at = datetime.now(UTC) - timedelta(hours=hours_ago)
    session.flush()
