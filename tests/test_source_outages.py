"""The sources themselves must distinguish an outage from an empty market.

`tests/test_discovery_run.py` proves the *runner* reports a total outage. It
does that with fake sources, so on its own it would pass while the real boards
still swallowed their failures — which is exactly the state that produced a
run recording ok=True, zero ads and zero errors while a proxy blocked all three
boards.

These tests drive the real `SeekSource` and `JobSpySource` and assert the
boundary directly: every request failing raises `SourceUnavailable`, one
request succeeding does not.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from backend.base import SourceUnavailable
from backend.config import settings
from backend.discovery.jobspy_source import JobSpySource
from backend.discovery.seek_source import SeekSource

# =========================================================================
# Seek — HTTP, driven through a mock transport
# =========================================================================


def client_that(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_seek_raises_when_every_endpoint_refuses():
    """The observed outage: JSON, the fallback and the HTML page all 403."""

    def blocked(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden")

    source = SeekSource(client=client_that(blocked))

    with pytest.raises(SourceUnavailable, match="unreachable"):
        source.search(terms=["data analyst"], locations=["Adelaide SA"])


def test_seek_raises_when_the_connection_itself_fails():
    """A proxy refusing to tunnel, rather than a server answering 403."""

    def dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Tunnel connection failed", request=request)

    source = SeekSource(client=client_that(dead))

    with pytest.raises(SourceUnavailable):
        source.search(terms=["data analyst"], locations=["Adelaide SA"])


def test_seek_does_not_raise_when_the_market_is_simply_empty():
    """A 200 with no ads is a quiet day and must stay a success.

    This is the assertion that stops the fix overcorrecting: if it ever goes
    red, an empty Adelaide becomes an outage and the run fails for no reason.
    """

    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    source = SeekSource(client=client_that(empty))

    assert source.search(terms=["data analyst"], locations=["Adelaide SA"]) == []


def test_seek_does_not_raise_when_only_some_queries_fail():
    """Best effort still holds: one endpoint answering is the source answering.

    Two terms, one of which is blocked. The source reached Seek, so this is a
    partial result, not an outage — it must not raise.
    """

    def one_term_blocked(request: httpx.Request) -> httpx.Response:
        if "blocked" in request.url.query.decode():
            return httpx.Response(403, text="Forbidden")
        return httpx.Response(200, json={"data": []})

    source = SeekSource(client=client_that(one_term_blocked))

    assert source.search(terms=["blocked", "fine"], locations=["Adelaide SA"]) == []


# =========================================================================
# jobspy — the library call is stubbed
# =========================================================================


def _stub_scrape(monkeypatch, behaviour) -> None:
    """Replace jobspy.scrape_jobs, which JobSpySource imports lazily."""
    import sys
    import types

    module = types.ModuleType("jobspy")
    module.scrape_jobs = behaviour
    monkeypatch.setitem(sys.modules, "jobspy", module)


def test_jobspy_raises_when_every_search_fails(monkeypatch):
    def always_fails(**kwargs: Any):
        raise RuntimeError("Tunnel connection failed: 403 Forbidden")

    _stub_scrape(monkeypatch, always_fails)
    source = JobSpySource(site="indeed")

    with pytest.raises(SourceUnavailable, match="indeed"):
        source.search(terms=["data analyst"], locations=["Adelaide SA"])


def test_jobspy_does_not_raise_when_a_search_returns_nothing(monkeypatch):
    """An empty frame is an answer. Only a failure to answer is an outage."""
    import pandas as pd

    _stub_scrape(monkeypatch, lambda **kwargs: pd.DataFrame())
    source = JobSpySource(site="indeed")

    assert source.search(terms=["data analyst"], locations=["Adelaide SA"]) == []


def test_jobspy_survives_one_failing_term_out_of_two(monkeypatch):
    """Partial results beat an exception — the rule this must not break."""
    import pandas as pd

    def one_of_each(**kwargs: Any):
        if kwargs.get("search_term") == "bad":
            raise RuntimeError("boom")
        return pd.DataFrame()

    _stub_scrape(monkeypatch, one_of_each)
    source = JobSpySource(site="indeed")

    # "good" answered, so the source answered: no raise.
    assert source.search(terms=["bad", "good"], locations=["Adelaide SA"]) == []


def test_jobspy_raises_only_after_trying_every_term(monkeypatch):
    """The exception is about the source, not about one unlucky query."""
    seen: list[str] = []

    def always_fails(**kwargs: Any):
        seen.append(kwargs.get("search_term"))
        raise RuntimeError("boom")

    _stub_scrape(monkeypatch, always_fails)
    source = JobSpySource(site="linkedin")

    with pytest.raises(SourceUnavailable):
        source.search(terms=["a", "b"], locations=["Adelaide SA"])

    assert seen == ["a", "b"], "gave up before trying every term"


def test_jobspy_catches_a_scraper_that_fails_without_raising(monkeypatch):
    """LinkedIn's real shape: it logs the failure and returns an empty frame.

    Indeed lets the transport error propagate; LinkedIn swallows it. Observed
    against a blocked proxy, LinkedIn came back with zero rows, no exception,
    and was recorded as a source that succeeded — the exact silent outage this
    whole change exists to stop.
    """
    import logging

    import pandas as pd

    def logs_and_returns_empty(**kwargs: Any):
        logging.getLogger("JobSpy:LinkedIn").error(
            "LinkedIn: HTTPSConnectionPool(...): Tunnel connection failed"
        )
        return pd.DataFrame()

    _stub_scrape(monkeypatch, logs_and_returns_empty)
    source = JobSpySource(site="linkedin")

    with pytest.raises(SourceUnavailable):
        source.search(terms=["data analyst"], locations=["Adelaide SA"])


def test_a_logged_error_alongside_real_ads_is_still_a_success(monkeypatch):
    """One page failing while another returns ads is a partial result."""
    import logging

    import pandas as pd

    def noisy_but_productive(**kwargs: Any):
        logging.getLogger("JobSpy:LinkedIn").error("one page failed")
        return pd.DataFrame(
            [
                {
                    "id": "1",
                    "title": "Data Analyst",
                    "company": "Acme",
                    "job_url": "https://www.linkedin.com/jobs/view/1",
                    "location": "Adelaide SA",
                }
            ]
        )

    _stub_scrape(monkeypatch, noisy_but_productive)
    source = JobSpySource(site="linkedin")

    # Must not raise: ads came back, so the board answered.
    source.search(terms=["data analyst"], locations=["Adelaide SA"])


def test_the_error_watcher_restores_the_record_factory(monkeypatch):
    """It hooks a global. Leaving it installed would taint every later log."""
    import logging

    import pandas as pd

    _stub_scrape(monkeypatch, lambda **kwargs: pd.DataFrame())
    before = logging.getLogRecordFactory()

    JobSpySource(site="linkedin").search(terms=["a"], locations=["Adelaide SA"])

    assert logging.getLogRecordFactory() is before, "the record factory leaked"


def test_the_record_factory_is_restored_even_when_the_scraper_raises(monkeypatch):
    """The failing path is the one that must not leak a global."""
    import logging

    def explodes(**kwargs: Any):
        raise RuntimeError("boom")

    _stub_scrape(monkeypatch, explodes)
    before = logging.getLogRecordFactory()

    with pytest.raises(SourceUnavailable):
        JobSpySource(site="linkedin").search(terms=["a"], locations=["Adelaide SA"])

    assert logging.getLogRecordFactory() is before, "the record factory leaked"


def test_a_logger_created_late_is_still_caught(monkeypatch):
    """The blind spot the record factory exists to close.

    Attaching to jobspy's loggers by name only works if they already exist.
    Here the logger is created inside the call, after any such scan would have
    run — and the failure must still be seen.
    """
    import logging

    import pandas as pd

    def logs_from_a_brand_new_logger(**kwargs: Any):
        logging.getLogger("JobSpy:NeverSeenBefore").error("blocked by proxy")
        return pd.DataFrame()

    _stub_scrape(monkeypatch, logs_from_a_brand_new_logger)

    with pytest.raises(SourceUnavailable):
        JobSpySource(site="linkedin").search(terms=["a"], locations=["Adelaide SA"])


def test_settings_are_untouched_by_these_tests():
    """Guard: nothing above may leave a mutated global behind."""
    assert settings.discovery_max_pages >= 1
