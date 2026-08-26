"""How long to wait between submissions.

A fixed interval is a bot signature: no human applies to a job every 240
seconds on the dot, and a perfectly regular cadence is trivially detectable in
a request log. The interval is therefore drawn from a lognormal distribution —
right-skewed, like real human gaps, with most waits near the median and an
occasional long one — subject to a hard floor.

The floor is not a rate limit dressed up as politeness. It is the last line of
defence against a loop bug submitting fifty applications in a minute.
"""

from __future__ import annotations

import math
import random
import time

from backend.config import settings
from backend.logging_setup import get_logger

log = get_logger(__name__)

__all__ = ["next_interval_seconds", "sleep_between_submits"]

# Above roughly four times the mean the wait stops being "human" and starts
# being a stalled pipeline, so the draw is capped.
_MAX_MULTIPLE_OF_MEAN = 4.0


def next_interval_seconds(rng: random.Random | None = None) -> float:
    """Draw the next inter-submission delay, in seconds.

    Lognormal with a median at the configured mean, floored at
    ``apply_min_interval_floor_seconds`` and capped so a freak draw cannot
    stall the run for an hour. ``rng`` is injectable so tests are deterministic.
    """
    rng = rng or random.Random()

    median = max(float(settings.apply_interval_lognormal_mean_seconds), 1.0)
    floor = float(settings.apply_min_interval_floor_seconds)

    # sigma=0.5 gives a spread where the middle half of draws land roughly
    # between 0.7x and 1.4x the median — varied, without wild outliers.
    drawn = rng.lognormvariate(math.log(median), 0.5)

    return max(floor, min(drawn, median * _MAX_MULTIPLE_OF_MEAN))


def sleep_between_submits(
    rng: random.Random | None = None, *, sleeper=time.sleep
) -> float:
    """Wait the drawn interval. Returns the seconds waited, for the audit log."""
    seconds = next_interval_seconds(rng)
    log.info("pacing_sleep", seconds=round(seconds, 1))
    sleeper(seconds)
    return seconds
