"""Tabs are closed after every application; the signed-in context is not.

Nothing here drives a real browser — that verification is a separate, manual
headful run recorded in NOTES.md. What these tests pin is the *lifecycle*: that
every path out of an application closes its page, including the paths that skip
a cleanup line, and that no path closes the context.

The fakes are deliberately thin. A real Playwright context is a page factory
with a list on it, and that is exactly what is modelled, so ``run_apply_pass``
runs its real code here rather than a test-only branch.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlmodel import Session, SQLModel, create_engine

from backend.apply import run as run_module
from backend.apply.flow import RestrictionDetected
from backend.apply.pages import (
    MAX_OPEN_PAGES,
    application_page,
    close_orphan_pages,
    warn_unless_single_page,
)
from backend.apply.session import SessionExpired
from backend.base import ApplyOutcome, ApplyResult
from backend.models import (
    ApplyType,
    Document,
    DocumentKind,
    Job,
    JobStatus,
)

# =========================================================================
# Fakes
# =========================================================================


class FakePage:
    """A page that can be navigated, read and closed, and knows if it was."""

    def __init__(self, context: FakeContext) -> None:
        self._context = context
        self.closed = False
        self.url = "about:blank"
        self.close_error: Exception | None = None

    def goto(self, url: str, wait_until: str | None = None) -> None:
        self.url = url

    def content(self) -> str:
        return "<html><body>nothing identifiable</body></html>"

    def close(self) -> None:
        if self.close_error is not None:
            raise self.close_error
        self.closed = True
        if self in self._context.pages:
            self._context.pages.remove(self)


class FakeContext:
    """A page factory with a list on it, which is what a context is here."""

    def __init__(self, *, start_with_a_page: bool = True) -> None:
        self.pages: list[FakePage] = []
        self.closed = False
        self.opened: list[FakePage] = []
        if start_with_a_page:
            self.new_page()

    def new_page(self) -> FakePage:
        page = FakePage(self)
        self.pages.append(page)
        self.opened.append(page)
        return page

    def cookies(self) -> list[dict[str, str]]:
        return []

    def close(self) -> None:
        self.closed = True


class FakeApplier:
    """Handles everything, and never actually applies — run_apply is stubbed."""

    platform = "greenhouse"

    def can_handle(self, job: Job) -> bool:
        return True


# =========================================================================
# Fixtures
# =========================================================================


def a_job(job_id: int) -> Job:
    return Job(
        id=job_id,
        source="seek",
        source_job_id=str(job_id),
        url=f"https://example.com/{job_id}",
        title="Data Analyst",
        company="Wattle Group",
        dedupe_hash=f"h{job_id}",
        status=JobStatus.DOCUMENTS_READY,
        apply_type=ApplyType.QUICK_APPLY,
    )


@pytest.fixture
def session_factory(monkeypatch):
    """Three eligible jobs in an in-memory database, and a quiet pass."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as setup:
        for job_id in (1, 2, 3):
            setup.add(a_job(job_id))
        setup.flush()  # the documents below have a foreign key to these
        for job_id in (1, 2, 3):
            setup.add(
                Document(
                    job_id=job_id,
                    kind=DocumentKind.RESUME,
                    path=f"/tmp/{job_id}.pdf",
                    sha256="x" * 64,
                    parse_check_passed=True,
                )
            )
        setup.commit()

    # Everything the pass reaches for that is not the browser.
    monkeypatch.setattr(run_module, "build_appliers", lambda: [FakeApplier()])
    monkeypatch.setattr(run_module.preferences, "propose_from_skips", lambda s: None)

    import backend.sessions as sessions_module

    monkeypatch.setattr(sessions_module, "check_all", lambda *a, **k: None)

    class _Scope:
        def __enter__(self) -> Session:
            self.session = Session(engine)
            return self.session

        def __exit__(self, *exc: object) -> None:
            self.session.commit()
            self.session.close()

    return _Scope


def a_pass(session_factory, context: FakeContext, **kwargs) -> Any:
    return run_module.run_apply_pass(
        session_factory=session_factory,
        context_factory=lambda: context,
        dry_run=True,
        **kwargs,
    )


# =========================================================================
# Every application closes its page
# =========================================================================


OUTCOMES = [
    ApplyOutcome.SUBMITTED,
    ApplyOutcome.ABSTAINED,
    ApplyOutcome.BLOCKED,
    ApplyOutcome.DRY_RUN,
    ApplyOutcome.FAILED,
]


@pytest.mark.parametrize("outcome", OUTCOMES, ids=lambda o: o.value)
def test_the_page_is_closed_whatever_the_outcome(session_factory, monkeypatch, outcome):
    context = FakeContext()
    monkeypatch.setattr(
        run_module,
        "run_apply",
        lambda *a, **k: ApplyResult(ok=True, outcome=outcome),
    )

    a_pass(session_factory, context)

    application_pages = context.opened[1:]  # the first is the anchor
    assert len(application_pages) == 3, "one page per application"
    assert all(page.closed for page in application_pages), (
        f"a page survived a {outcome.value} application"
    )


def test_an_exception_mid_application_still_closes_the_page(
    session_factory, monkeypatch
):
    """The reason this is a context manager and not two statements."""
    context = FakeContext()

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the adapter fell over mid-form")

    monkeypatch.setattr(run_module, "run_apply", explode)

    run = a_pass(session_factory, context)

    application_pages = context.opened[1:]
    assert len(application_pages) == 3, "the pass continued after each failure"
    assert all(page.closed for page in application_pages)
    assert len(run.errors) == 3


@pytest.mark.parametrize(
    "error",
    [RestrictionDetected("account restricted"), SessionExpired("linkedin")],
    ids=["restriction", "session_expired"],
)
def test_the_page_is_closed_on_the_errors_that_halt_the_pass(
    session_factory, monkeypatch, error
):
    """These leave by `break`, which is the path a cleanup line would miss."""
    context = FakeContext()

    def halt(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(run_module, "run_apply", halt)

    a_pass(session_factory, context)

    application_pages = context.opened[1:]
    assert len(application_pages) == 1, "the pass halted on the first job"
    assert application_pages[0].closed, "breaking out of the loop leaked a tab"


def test_a_job_with_no_applier_still_closes_its_page(session_factory, monkeypatch):
    """This path leaves by `continue` before any application happens."""
    context = FakeContext()

    class HandlesNothing:
        platform = "greenhouse"

        def can_handle(self, job: Job) -> bool:
            return False

    monkeypatch.setattr(run_module, "build_appliers", lambda: [HandlesNothing()])
    monkeypatch.setattr(
        run_module,
        "run_apply",
        lambda *a, **k: pytest.fail("nothing should have been applied to"),
    )

    with session_factory() as setup:
        for job in setup.exec(__import__("sqlmodel").select(Job)).all():
            job.apply_type = ApplyType.EXTERNAL
            setup.add(job)

    anchor = context.pages[0]
    a_pass(session_factory, context)

    application_pages = context.opened[1:]
    assert application_pages, "the HTML probe needs a page of its own"
    assert all(page.closed for page in application_pages)

    # The point of giving the probe its own page: it NAVIGATES, and the anchor
    # is what the session checks run on. Probing from the anchor leaves it
    # parked on the last job's ad, and every page still gets closed — so
    # without this the test passed either way.
    assert anchor.url == "about:blank", (
        f"the probe navigated the anchor page to {anchor.url}"
    )
    assert application_pages[-1].url.startswith("https://example.com/"), (
        "the probe did not navigate the application's own page"
    )


# =========================================================================
# The context survives; only pages die
# =========================================================================


def test_the_context_outlives_every_application(session_factory, monkeypatch):
    """Closing the context would end the signed-in session. Never here."""
    context = FakeContext()
    seen_open_contexts: list[bool] = []

    def record(*args: Any, **kwargs: Any) -> ApplyResult:
        seen_open_contexts.append(not context.closed)
        return ApplyResult(ok=True, outcome=ApplyOutcome.DRY_RUN)

    monkeypatch.setattr(run_module, "run_apply", record)

    a_pass(session_factory, context)

    assert seen_open_contexts == [True, True, True]
    # Closed exactly once, at the end of the pass, by the pass itself.
    assert context.closed


def test_the_page_is_closed_when_the_body_raises():
    """The `finally`, proven directly.

    Nothing in ``run_apply_pass`` reaches this: it catches RestrictionDetected,
    SessionExpired and Exception itself, so by the time the context manager
    resumes there is no exception in flight and even a close written after the
    `yield` with no `finally` would run. Replacing the try/finally with a plain
    `if` left every pass-level test green. This is the test that fails.
    """
    context = FakeContext()

    with pytest.raises(RuntimeError, match="boom"), application_page(context):
        raise RuntimeError("boom")

    page = context.opened[-1]
    assert page.closed, "an exception escaping the body leaked the tab"
    assert not context.closed


def test_application_page_never_closes_the_context():
    """The unit, isolated from the pass."""
    context = FakeContext()
    with application_page(context) as page:
        assert page in context.pages
    assert page.closed
    assert not context.closed


def test_a_close_that_fails_does_not_end_the_pass():
    """A page that refuses to close is a warning, not an exception."""
    context = FakeContext()
    with application_page(context) as page:
        page.close_error = RuntimeError("target page, context or browser closed")
    assert not page.closed
    assert not context.closed


# =========================================================================
# One page between applications
# =========================================================================


def test_exactly_one_page_is_open_between_applications(session_factory, monkeypatch):
    context = FakeContext()
    open_counts: list[int] = []

    def record(*args: Any, **kwargs: Any) -> ApplyResult:
        # Two: the anchor, and this application's own page.
        open_counts.append(len(context.pages))
        return ApplyResult(ok=True, outcome=ApplyOutcome.DRY_RUN)

    monkeypatch.setattr(run_module, "run_apply", record)

    a_pass(session_factory, context)

    assert open_counts == [2, 2, 2], (
        f"pages accumulated across applications: {open_counts}"
    )


def test_a_leak_between_applications_is_logged_loudly():
    context = FakeContext()
    assert warn_unless_single_page(context, when="test") is True

    context.new_page()
    assert warn_unless_single_page(context, when="test") is False


def test_the_single_page_assertion_is_quiet_without_a_context():
    """A test injecting a bare page has no context, and that is not a leak."""
    assert warn_unless_single_page(None, when="test") is True


def test_pages_an_application_opened_itself_are_closed_with_it():
    """An ATS that opens its real form in a popup. Nobody else closes that."""
    context = FakeContext()
    anchor = context.pages[0]

    with application_page(context) as page:
        popup = context.new_page()

    assert page.closed
    assert popup.closed, "a popup the application opened outlived it"
    assert context.pages == [anchor], "the anchor is the only page left"


# =========================================================================
# The cap
# =========================================================================


def test_under_the_cap_nothing_is_closed():
    """Genuinely under it.

    The first version of this test opened exactly MAX_OPEN_PAGES pages, where
    the slice choosing what to close is empty however the threshold is written —
    so a guard mutated to fire under the cap as well as over it still passed.
    """
    assert MAX_OPEN_PAGES >= 4, "this test needs a cap of at least 4 to mean anything"
    context = FakeContext()
    for _ in range(MAX_OPEN_PAGES - 2):
        context.new_page()

    # Strictly under the cap, AND with more than one page that a sweep could
    # take. One closable page is not enough: the slice that picks victims is
    # sized by the excess, and with a single candidate it comes out empty
    # whether the threshold is right or not — which is how the first version of
    # this test passed a guard mutated to fire under the cap.
    assert 2 < len(context.pages) < MAX_OPEN_PAGES, len(context.pages)

    assert close_orphan_pages(context, keep=context.pages[0]) == 0
    assert all(not page.closed for page in context.pages)


def test_over_the_cap_the_oldest_orphans_are_closed():
    context = FakeContext()
    anchor = context.pages[0]
    for _ in range(MAX_OPEN_PAGES + 2):
        context.new_page()
    assert len(context.pages) == MAX_OPEN_PAGES + 3

    closed = close_orphan_pages(context, keep=anchor)

    assert closed == 3
    assert len(context.pages) == MAX_OPEN_PAGES
    assert not anchor.closed, "the anchor page was closed — the pass loses its session"
    assert anchor in context.pages


def test_the_anchor_is_kept_even_when_it_is_the_oldest_page():
    """The anchor is page zero, so an oldest-first sweep would take it first."""
    context = FakeContext()
    anchor = context.pages[0]
    for _ in range(MAX_OPEN_PAGES + 1):
        context.new_page()

    close_orphan_pages(context, keep=anchor)

    assert anchor in context.pages
    assert not anchor.closed
