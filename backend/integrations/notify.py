"""One place that knows how to reach the user.

The apply layer must be able to page someone — a dead session, a tripped
circuit breaker, a platform restriction — without importing Telegram. So
``guardrails``, ``session`` and ``canary`` each expose a plain callable hook,
and this module fills them in. That keeps the safety-critical code free of a
network dependency and keeps it unit-testable without a bot token.

Nothing here raises. A notification that fails must never take down the thing
it was trying to report.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum

from backend.logging_setup import get_logger

log = get_logger(__name__)

__all__ = ["Priority", "notify", "register_hooks", "set_sender"]


class Priority(str, Enum):
    """How loudly something needs to arrive."""

    IMMEDIATE = "immediate"
    """Interview requests, restrictions, dead sessions. Send now."""

    NORMAL = "normal"
    DIGEST = "digest"
    """Roll up into the evening summary rather than interrupting."""


# Set by the Telegram layer at startup. Left None in tests and in a checkout
# with no bot configured, where notifications become log lines.
_sender: Callable[[str, Priority], None] | None = None


def set_sender(sender: Callable[[str, Priority], None] | None) -> None:
    global _sender
    _sender = sender
    log.info("notify_sender_registered", configured=sender is not None)


def notify(title: str, body: str = "", priority: Priority = Priority.NORMAL) -> None:
    """Send a message to the user. Never raises."""
    message = f"*{title}*\n{body}".strip()

    if _sender is None:
        # Not an error: a user who has not configured Telegram still gets
        # everything in the log, which is where the digest reads from anyway.
        log.warning("notify_unsent", title=title, body=body[:400], priority=priority.value)
        return

    try:
        _sender(message, priority)
    except Exception as exc:
        log.exception("notify_failed", title=title, error=str(exc)[:200])


def register_hooks() -> None:
    """Wire the safety layers' notification hooks to this module.

    Called once at startup. Each hook is a plain callable on the target module
    so that module never imports an integration.
    """
    from backend import siteknowledge
    from backend.apply import canary, flow, guardrails, session

    guardrails.on_notify = lambda title, body: notify(title, body, Priority.IMMEDIATE)

    session.on_session_expired = lambda platform: notify(
        "Session expired",
        f"The {platform} session is no longer signed in. Applications are halted.\n\n"
        f"Run: uv run python -m backend.apply.session login --platform {platform}",
        Priority.IMMEDIATE,
    )

    flow.on_element_unresolvable = lambda platform, key, tried, job_id: notify(
        "Element not found",
        f"{platform}: nothing resolved `{key}` on job {job_id}, so it has gone to "
        f"the manual queue rather than being guessed at.\n"
        f"Tried {len(tried)} strategies.\n\n"
        f"Fix: edit data/siteknowledge/{platform}/elements.json, or re-record a HAR.",
        Priority.IMMEDIATE,
    )

    siteknowledge.on_all_strategies_failed = lambda platform, key, tried: log.error(
        "all_strategies_failed", platform=platform, key=key, tried=tried
    )

    siteknowledge.on_strategy_drift = lambda platform, key, was, now: notify(
        "Strategy drift",
        f"{platform}: `{key}` stopped resolving via `{was}` and now resolves via "
        f"`{now}`. Applications continue — the working strategy has been promoted "
        f"in data/siteknowledge/{platform}/elements.json.",
        Priority.DIGEST,
    )

    canary.on_drift = lambda platform, missing: notify(
        "Selector drift",
        f"{platform} is missing expected elements: {', '.join(missing)}.\n"
        "Applications still run; re-record a HAR and check the adapter when convenient.",
        Priority.NORMAL,
    )

    log.info("notify_hooks_registered")


