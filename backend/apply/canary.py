"""Daily canary: notice a platform changed its markup before an application does.

Navigates one job page per platform and asserts the elements the adapters
depend on are still there. Drift WARNS — it never halts. A missing selector on
a canary page usually means a cosmetic change, and stopping the whole pipeline
for that would cost more applications than it saves. A real failure during an
application is what trips the circuit breaker; this is the early warning.

    uv run python -m backend.apply.canary
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from backend.apply.session import PLATFORMS, launch_context
from backend.boards import BOARDS
from backend.logging_setup import configure_logging, get_logger

log = get_logger(__name__)

__all__ = ["CANARY_PAGES", "CanaryResult", "run_canary"]


# Set by the integrations layer so drift can reach the user.
on_drift: Callable[[str, list[str]], None] | None = None


# Both tables come from the board registry: a canary page is data, and the
# selectors it watches are the adapter's own, sampled rather than re-listed.
# Re-listing them was how the canary came to watch selectors an adapter had
# already renamed.
CANARY_PAGES: dict[str, str] = {
    entry.key: entry.canary_url for entry in BOARDS if entry.canary_url
}

WATCHED: dict[str, dict[str, tuple[str, ...]]] = {
    entry.key: {
        key: entry.selectors()[key]
        for key in entry.canary_selectors
        if key in entry.selectors()
    }
    for entry in BOARDS
    if entry.canary_url and entry.selectors
}


@dataclass
class CanaryResult:
    platform: str
    reachable: bool
    drifted: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.reachable and not self.drifted


def _selector_present(page: Any, selectors: tuple[str, ...]) -> bool:
    for selector in selectors:
        try:
            if page.locator(selector).first.count() > 0:
                return True
        except Exception as exc:  # noqa: BLE001
            log.debug("canary_selector_error", selector=selector, error=str(exc)[:100])
    return False


def check_platform(page: Any, platform: str) -> CanaryResult:
    """Load the canary page and report which watched selectors are missing."""
    url = CANARY_PAGES.get(platform)
    if url is None:
        return CanaryResult(
            platform=platform, reachable=False, error="no canary URL configured"
        )

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as exc:  # noqa: BLE001 - unreachable is a report, not a crash
        # A blocked or offline host is not drift. Saying so plainly matters:
        # this environment cannot reach either platform at all.
        return CanaryResult(
            platform=platform,
            reachable=False,
            error=f"{type(exc).__name__}: {exc}"[:200],
        )

    drifted = [
        name
        for name, selectors in WATCHED.get(platform, {}).items()
        if not _selector_present(page, selectors)
    ]
    return CanaryResult(platform=platform, reachable=True, drifted=drifted)


def run_canary(
    platforms: list[str] | None = None, *, page: Any = None
) -> dict[str, Any]:
    """Check each platform. Warns on drift; never halts the pipeline."""
    platforms = platforms or sorted(CANARY_PAGES)
    results: list[CanaryResult] = []

    if page is not None:
        results = [check_platform(page, platform) for platform in platforms]
    else:
        from playwright.sync_api import sync_playwright

        playwright = sync_playwright().start()
        context = None
        try:
            context = launch_context(playwright)
            live_page = context.pages[0] if context.pages else context.new_page()
            results = [check_platform(live_page, platform) for platform in platforms]
        finally:
            if context is not None:
                context.close()
            playwright.stop()

    for result in results:
        if not result.reachable:
            log.warning(
                "canary_unreachable", platform=result.platform, error=result.error
            )
        elif result.drifted:
            log.warning(
                "canary_drift_detected",
                platform=result.platform,
                missing=result.drifted,
                detail="selectors are missing; the adapter may fail on the next application",
            )
            if on_drift is not None:
                try:
                    on_drift(result.platform, result.drifted)
                except Exception as exc:
                    log.exception("canary_notify_failed", error=str(exc))
        else:
            log.info("canary_ok", platform=result.platform)

    return {
        "checked": [r.platform for r in results],
        "ok": [r.platform for r in results if r.ok],
        "drifted": {r.platform: r.drifted for r in results if r.drifted},
        "unreachable": {r.platform: r.error for r in results if not r.reachable},
    }


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI wiring
    parser = argparse.ArgumentParser(prog="python -m backend.apply.canary")
    parser.add_argument("--platform", default=None, choices=sorted(PLATFORMS))
    args = parser.parse_args(argv)

    configure_logging()
    summary = run_canary([args.platform] if args.platform else None)
    log.info("canary_complete", **summary)
    # Drift is a warning, not a failure: exit 0 so a scheduled run does not
    # page anyone at 3am over a renamed CSS class.
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
