"""Page lifecycle for the apply pass. One page per application, always closed.

The distinction this module exists to keep is between the **context** and its
**pages**.

The context IS the session. It is the persistent, headful Chrome profile in
``data/browser_profile`` that the user signed into by hand, and it must outlive
every application in the pass — closing it means the next pass has no session
and there is nothing the agent may do about that, because Claude.md hard rule 8
forbids scripting a login. Nothing here ever closes a context.

Pages are disposable, and until now nothing closed them either. A pass walking
sixty jobs overnight left sixty tabs open, each holding its DOM, its JavaScript
heap and its image cache, in a browser that is expected to stay up for days.

The rules, in order of how much they matter:

1. **Every application gets its own page, and that page is closed** — submitted,
   abstained, blocked, failed or exploded. ``application_page`` is a context
   manager precisely so an exception cannot skip the close.
2. **One page open between applications.** The anchor page (the one the
   persistent context starts with, used for session checks) and nothing else.
   Anything more means a page leaked, and it is logged loudly rather than
   quietly tidied.
3. **A hard cap.** Some pages are not ours to predict: an ATS that opens the
   real form in a popup adds a page the flow never asked for. Past the cap the
   orphans are closed and the fact is a warning, not a debug line.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from backend.logging_setup import get_logger

log = get_logger(__name__)

__all__ = [
    "MAX_OPEN_PAGES",
    "application_page",
    "close_orphan_pages",
    "open_pages",
    "warn_unless_single_page",
]


MAX_OPEN_PAGES = 4
"""How many pages may be open before orphans are force-closed.

Not one. A single application legitimately holds two for a moment — the anchor
page plus its own — and an ATS that opens its form in a popup makes three. Four
leaves room for that without letting a leak run all night.
"""


def open_pages(context: Any) -> list[Any]:
    """The context's live pages, or an empty list if it cannot say.

    A test's fake page has no context at all, and reading ``.pages`` off a
    closed context raises. Neither is a reason for an apply pass to end, so both
    answer "no pages" here and every caller degrades to doing nothing.
    """
    if context is None:
        return []
    try:
        return list(context.pages)
    except Exception as exc:  # noqa: BLE001 - a dead context is not our failure
        log.debug("open_pages_unavailable", error=str(exc)[:120])
        return []


def _close(page: Any, *, reason: str) -> bool:
    """Close one page. Never raises — a failed close must not end the pass."""
    try:
        page.close()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("page_close_failed", reason=reason, error=str(exc)[:150])
        return False


@contextmanager
def application_page(context: Any, *, job_id: int | None = None) -> Iterator[Any]:
    """Open a page for one application and close it whatever happens.

    The close is in a ``finally``, so it runs on ``SessionExpired``, on
    ``RestrictionDetected``, on the ``break`` those cause in the caller's loop,
    and on any adapter blowing up mid-form. That is the whole reason this is a
    context manager rather than a pair of calls: the outcomes that skip a
    cleanup line are exactly the ones that leave the browser in a bad state.

    The context is never closed here. Only the page.
    """
    page = context.new_page()
    log.debug(
        "application_page_opened", job_id=job_id, open_pages=len(open_pages(context))
    )
    try:
        yield page
    finally:
        # Anything the application itself opened — a popup form, a preview tab —
        # is a page the flow never asked for and nobody else will close. Take
        # them with us, before the page that spawned them.
        _close_pages_opened_after(context, page, job_id=job_id)
        _close(page, reason="application_finished")
        log.debug(
            "application_page_closed",
            job_id=job_id,
            open_pages=len(open_pages(context)),
        )


def _close_pages_opened_after(context: Any, page: Any, *, job_id: int | None) -> None:
    """Close every page that appeared after ``page`` did.

    Positional rather than by identity because Playwright appends new pages to
    ``context.pages``: anything after this application's own page arrived during
    this application. The anchor page sits before it and is never touched.
    """
    pages = open_pages(context)
    try:
        index = pages.index(page)
    except ValueError:
        return
    for extra in pages[index + 1 :]:
        log.warning(
            "closing_page_opened_by_application",
            job_id=job_id,
            detail="the application opened a page of its own — a popup form or a preview",
        )
        _close(extra, reason="opened_by_application")


def warn_unless_single_page(context: Any, *, when: str) -> bool:
    """Assert the anchor page is the only one open. Returns True when it is.

    Loud on purpose. A leaked page is not an error the pass can recover from and
    not a reason to stop, but it is the difference between a browser that
    survives an overnight run and one that does not — and it will never be found
    unless somebody says so at the moment it happens.
    """
    pages = open_pages(context)
    if context is None or len(pages) == 1:
        return True

    log.error(
        "page_leak_detected",
        when=when,
        open_pages=len(pages),
        expected=1,
        urls=[_url(page) for page in pages][:8],
    )
    return False


def _url(page: Any) -> str:
    try:
        return str(page.url)[:120]
    except Exception:  # noqa: BLE001
        return "<unreadable>"


def close_orphan_pages(
    context: Any, *, keep: Any = None, cap: int = MAX_OPEN_PAGES
) -> int:
    """Close pages beyond the cap, oldest kept first. Returns how many closed.

    ``keep`` is the anchor page, which is never closed however many pages are
    open — the pass still needs somewhere to check the session.

    Under the cap this does nothing at all, so it is safe to call every
    iteration. Over it, closing is the right response rather than merely
    reporting: the alternative is a browser that grows until the machine swaps,
    on an unattended run nobody is watching.
    """
    pages = open_pages(context)
    excess = len(pages) - cap
    if excess <= 0:
        return 0

    closable = [page for page in pages if page is not keep]
    # Oldest first: the newest pages are the ones the current work is using.
    doomed = closable[:excess]
    closed = sum(1 for page in doomed if _close(page, reason="page_cap_exceeded"))
    log.warning(
        "page_cap_exceeded",
        open_pages=len(pages),
        cap=cap,
        closed=closed,
        remaining=len(open_pages(context)),
    )
    return closed
