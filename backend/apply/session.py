"""The browser session. Established by a human, reused by the agent.

Claude.md hard rule 8: **never script a login.** There is no function here that
accepts a username or a password, and there must never be one. Automating a
login is what turns "an agent using my browser session" into "credentials in a
config file and a bot signature on my account". The user signs in once, in a
visible window, and the persistent context keeps that session alive.

Headful with ``channel="chrome"`` is also deliberate — headless is a detection
signal. The channel is read from settings so tests and CI can point at plain
Chromium, but the production default stays real Chrome.

The web UI must never serve files from ``browser_profile_dir``: it holds a live
authenticated session (Claude.md, Windows section).
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from backend.config import settings
from backend.logging_setup import configure_logging, get_logger

log = get_logger(__name__)

__all__ = [
    "PLATFORMS",
    "PlatformSelectors",
    "SessionExpired",
    "ensure_logged_in",
    "is_logged_in",
    "launch_context",
    "login",
]


# Set by the integrations layer so a dead session can page the user without
# this module importing Telegram.
on_session_expired: Callable[[str], None] | None = None


class SessionExpired(RuntimeError):
    """The platform session is gone. Halts the pipeline; never auto-recovered.

    Recovery means a human signing in again. Anything else this module could do
    would be scripting a login.
    """

    def __init__(self, platform: str) -> None:
        super().__init__(
            f"{platform} session is not authenticated. "
            f"Run: uv run python -m backend.apply.session login --platform {platform}"
        )
        self.platform = platform


@dataclass(frozen=True)
class PlatformSelectors:
    """Everything session handling needs to know about one platform.

    A registry entry, not code — adding a platform is a data change here plus
    an adapter file, never an edit to the logic below.
    """

    login_url: str
    home_url: str
    #: Selectors that only exist once signed in. Any one matching is enough.
    logged_in: tuple[str, ...]
    #: Selectors for account-restriction interstitials. Any match halts everything.
    restriction_notice: tuple[str, ...] = ()


PLATFORMS: dict[str, PlatformSelectors] = {
    "linkedin": PlatformSelectors(
        login_url="https://www.linkedin.com/login",
        home_url="https://www.linkedin.com/feed/",
        logged_in=(
            "img.global-nav__me-photo",
            "button.global-nav__primary-link-me-menu-trigger",
            "[data-control-name='identity_welcome_message']",
            "div.global-nav__me",
        ),
        restriction_notice=(
            "text=/account has been restricted/i",
            "text=/unusual activity/i",
            "text=/we've restricted your account/i",
            "text=/verify your identity/i",
        ),
    ),
    "seek": PlatformSelectors(
        login_url="https://www.seek.com.au/oauth/login/",
        home_url="https://www.seek.com.au/",
        logged_in=(
            "[data-automation='profile-menu']",
            "[data-automation='signed-in-nav']",
            "button[data-automation='navigation-account']",
        ),
        restriction_notice=("text=/account has been suspended/i",),
    ),
}


def launch_context(playwright: Any, **overrides: Any) -> Any:
    """Open the persistent, headful browser context the apply layer uses.

    ``playwright`` is the started Playwright driver, passed in rather than
    started here so callers control its lifetime and tests can inject a fake.
    """
    profile_dir = settings.browser_profile_dir
    profile_dir.mkdir(parents=True, exist_ok=True)

    kwargs: dict[str, Any] = {
        "user_data_dir": str(profile_dir),
        # Headless is a detection signal — see Claude.md.
        "headless": settings.browser_headless,
        # Real Chrome, not bundled Chromium, in production.
        "channel": settings.browser_channel,
        "locale": "en-AU",
        "timezone_id": settings.timezone,
        "viewport": {"width": 1440, "height": 900},
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    kwargs.update(overrides)

    log.info(
        "launching_browser",
        channel=kwargs["channel"],
        headless=kwargs["headless"],
        profile=str(profile_dir),
    )
    return playwright.chromium.launch_persistent_context(**kwargs)


def _any_visible(page: Any, selectors: tuple[str, ...], timeout_ms: int = 3000) -> bool:
    """Whether any of the candidate selectors is present.

    Several candidates per element on purpose: these platforms reshuffle their
    markup regularly, and a single brittle selector turns a cosmetic UI change
    into "the session looks dead". A selector that simply is not there raises
    in Playwright, which is a normal answer here rather than an error — logged
    at debug so a wholesale markup change is still visible in the logs.
    """
    for selector in selectors:
        try:
            if page.locator(selector).first.is_visible(timeout=timeout_ms):
                return True
        except Exception as exc:  # noqa: BLE001 - absence is the expected case
            log.debug("selector_absent", selector=selector, error=str(exc)[:120])
    return False


def is_logged_in(page: Any, platform: str) -> bool:
    """Whether the session is authenticated, by a known post-login element.

    Checking for a real signed-in element rather than "the URL is not /login":
    both platforms happily serve a logged-out home page at the same URL, and a
    URL check would report a dead session as healthy right up until the apply
    flow filled a form nobody was signed in to.
    """
    selectors = PLATFORMS.get(platform)
    if selectors is None:
        log.warning("unknown_platform_for_login_check", platform=platform)
        return False
    return _any_visible(page, selectors.logged_in)


def has_restriction_notice(page: Any, platform: str) -> bool:
    """Whether the platform is showing an account-restriction interstitial."""
    selectors = PLATFORMS.get(platform)
    if selectors is None or not selectors.restriction_notice:
        return False
    return _any_visible(page, selectors.restriction_notice, timeout_ms=1500)


def ensure_logged_in(page: Any, platform: str) -> None:
    """Raise ``SessionExpired`` if the session is dead. No recovery attempted."""
    if is_logged_in(page, platform):
        return

    log.error("session_expired", platform=platform)
    if on_session_expired is not None:
        try:
            on_session_expired(platform)
        except Exception as exc:
            log.exception("session_expired_hook_failed", error=str(exc))
    raise SessionExpired(platform)


def login(platform: str, *, timeout_seconds: int = 600) -> bool:
    """Open a visible window and WAIT for the human to sign in.

    This function has no credential parameters, by design. It navigates to the
    login page, then polls until a post-login element appears or the timeout
    expires. The user does the typing.
    """
    selectors = PLATFORMS.get(platform)
    if selectors is None:
        raise ValueError(f"unknown platform {platform!r}; known: {sorted(PLATFORMS)}")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        context = launch_context(playwright)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(selectors.home_url, wait_until="domcontentloaded")

            if is_logged_in(page, platform):
                log.info("already_signed_in", platform=platform)
                return True

            page.goto(selectors.login_url, wait_until="domcontentloaded")
            log.warning(
                "waiting_for_manual_login",
                platform=platform,
                timeout_seconds=timeout_seconds,
                instruction="Sign in in the browser window that just opened.",
            )

            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                if is_logged_in(page, platform):
                    log.info("login_confirmed", platform=platform)
                    return True
                time.sleep(3)

            log.error("login_timed_out", platform=platform)
            return False
        finally:
            context.close()


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI wiring
    parser = argparse.ArgumentParser(prog="python -m backend.apply.session")
    sub = parser.add_subparsers(dest="command", required=True)

    login_parser = sub.add_parser("login", help="sign in manually in a visible browser")
    login_parser.add_argument(
        "--platform", default="linkedin", choices=sorted(PLATFORMS), help="platform to sign in to"
    )
    login_parser.add_argument("--timeout", type=int, default=600)

    check = sub.add_parser("check", help="report whether each session is alive")
    check.add_argument("--platform", default=None, choices=sorted(PLATFORMS))

    args = parser.parse_args(argv)
    configure_logging()

    if args.command == "login":
        return 0 if login(args.platform, timeout_seconds=args.timeout) else 1

    from playwright.sync_api import sync_playwright

    platforms = [args.platform] if args.platform else sorted(PLATFORMS)
    ok = True
    with sync_playwright() as playwright:
        context = launch_context(playwright)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            for platform in platforms:
                page.goto(PLATFORMS[platform].home_url, wait_until="domcontentloaded")
                alive = is_logged_in(page, platform)
                ok = ok and alive
                log.info("session_check", platform=platform, authenticated=alive)
        finally:
            context.close()
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
