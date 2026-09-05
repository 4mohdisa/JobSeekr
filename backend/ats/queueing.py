"""Deciding what is worth the user's own time.

A manual-queue job must clear a **higher** bar than an auto-apply job, which
looks backwards until you count what each one costs.

An automated application costs a fraction of a cent and about four minutes of
wall-clock nobody is watching. A manual application costs ninety seconds of the
user's attention — and attention is the genuinely scarce resource in a job
search, the thing that runs out at 9pm after a day of work. Sending a
70-scoring job to the automated path is close to free. Sending the same job to
the manual queue spends something that cannot be replaced, on a job the system
itself is unsure about.

So the manual bar sits above the auto-apply threshold: if it is not good enough
to be worth interrupting someone for, it is not worth queueing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.logging_setup import get_logger

log = get_logger(__name__)

__all__ = [
    "MANUAL_QUEUE_PREMIUM",
    "QueueDecision",
    "decide_queueing",
    "manual_queue_floor",
]


MANUAL_QUEUE_PREMIUM = 8.0
"""How far above the auto-apply threshold a job must score to be queued by hand.

Enough to be a real filter rather than rounding noise, small enough that a
genuinely strong job that merely cannot be automated still reaches the user.
"""


@dataclass(frozen=True)
class QueueDecision:
    action: str  # queue | skip | auto
    reason: str
    threshold: float


def manual_queue_floor(campaign: Any) -> float:
    """The score a job needs before it may take the user's time."""
    auto = float(getattr(campaign, "score_auto_apply", 0) or 0)
    return auto + MANUAL_QUEUE_PREMIUM


def decide_queueing(
    campaign: Any,
    score: float | None,
    *,
    automatable: bool,
) -> QueueDecision:
    """Where a job should go once it is known whether it can be automated.

    ``automatable`` is False for a listing that redirects off-site, needs an
    account per employer, or is showing a CAPTCHA — anything that cannot be
    completed without a person.
    """
    if automatable:
        return QueueDecision("auto", "handled by the apply engine", 0.0)

    floor = manual_queue_floor(campaign)

    if score is None:
        # Unscored and unautomatable: nothing to justify spending attention on.
        return QueueDecision("skip", "not automatable and never scored", floor)

    if score < floor:
        return QueueDecision(
            "skip",
            (
                f"score {score:.0f} is below the manual-queue floor {floor:.0f} "
                f"(auto-apply threshold + {MANUAL_QUEUE_PREMIUM:.0f}); "
                "your attention is the scarce resource"
            ),
            floor,
        )

    return QueueDecision(
        "queue", f"score {score:.0f} clears the manual floor {floor:.0f}", floor
    )
