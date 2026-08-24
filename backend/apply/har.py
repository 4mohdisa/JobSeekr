"""Record real application flows once, replay them offline forever.

The adapters' selectors could not be verified where this was written — Seek and
LinkedIn are both unreachable from that environment. This module is how the
user closes that gap on their own machine, and how the selectors stay honest
afterwards:

1. ``record`` opens a real browser with ``record_har_path`` set. The user walks
   through an application by hand; every request and response is captured.
2. ``replay`` serves that capture back to a test with ``route_from_har``, so the
   adapter can be exercised against real markup with no network at all.

Recording is a one-time cost that turns "these selectors looked right" into
"these selectors are pinned by a test".

    uv run python -m backend.apply.har record --platform linkedin --variant two_step
    uv run pytest tests/test_har_replay.py
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.config import settings
from backend.logging_setup import configure_logging, get_logger

log = get_logger(__name__)

__all__ = ["VARIANTS", "Variant", "har_path", "list_recordings", "record", "replay"]


@dataclass(frozen=True)
class Variant:
    """One flow shape worth capturing, and why it differs."""

    key: str
    platform: str
    description: str


# The shapes that actually change adapter behaviour. Recording all of these
# covers the branches that would otherwise only be exercised in production.
VARIANTS: tuple[Variant, ...] = (
    Variant(
        "two_step",
        "linkedin",
        "Short Easy Apply: contact details then submit. The common case.",
    ),
    Variant(
        "five_step",
        "linkedin",
        "Long Easy Apply with screening questions across several steps — "
        "proves the step loop terminates on Submit rather than a step count.",
    ),
    Variant(
        "with_cover_letter",
        "linkedin",
        "Two upload slots: resume and cover letter uploaded separately.",
    ),
    Variant(
        "without_cover_letter",
        "linkedin",
        "One upload slot: combined.pdf is used instead.",
    ),
    Variant(
        "offsite_redirect",
        "linkedin",
        "Claims Easy Apply then redirects off-site — must be marked manual_only.",
    ),
    Variant(
        "quick_apply",
        "seek",
        "Seek Quick Apply: resume upload plus the editable cover-letter textarea.",
    ),
    Variant(
        "screening_step",
        "seek",
        "Seek with screening questions on their own separate step.",
    ),
)


def har_path(platform: str, variant: str) -> Path:
    """Where a recording lives. One file per platform and variant."""
    return settings.har_dir / platform / f"{variant}.har"


def list_recordings() -> dict[str, list[str]]:
    """Which variants have actually been recorded, per platform."""
    found: dict[str, list[str]] = {}
    if not settings.har_dir.exists():
        return found
    for platform_dir in sorted(settings.har_dir.iterdir()):
        if platform_dir.is_dir():
            found[platform_dir.name] = sorted(
                path.stem for path in platform_dir.glob("*.har")
            )
    return found


def missing_recordings() -> list[Variant]:
    """Variants nobody has captured yet — the gaps in offline coverage."""
    recorded = list_recordings()
    return [
        variant
        for variant in VARIANTS
        if variant.key not in recorded.get(variant.platform, [])
    ]


def record(platform: str, variant: str, *, url: str | None = None) -> Path:
    """Open a real browser and capture the session to a HAR file.

    The user drives. Nothing is automated here — this records what a human
    does, which is the only way to capture a genuine application flow.
    """
    from playwright.sync_api import sync_playwright

    from backend.apply.session import PLATFORMS, launch_context

    target = har_path(platform, variant)
    target.parent.mkdir(parents=True, exist_ok=True)

    selectors = PLATFORMS.get(platform)
    start_url = url or (selectors.home_url if selectors else "about:blank")

    log.warning(
        "har_recording_started",
        platform=platform,
        variant=variant,
        path=str(target),
        instruction=(
            "Walk through ONE application by hand, then close the browser window. "
            "Use a job you are willing to actually apply to, or stop before the "
            "final submit — the capture is still useful."
        ),
    )

    with sync_playwright() as playwright:
        context = launch_context(
            playwright, record_har_path=str(target), record_har_content="embed"
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(start_url, wait_until="domcontentloaded")
            # Block until the human closes the window.
            page.wait_for_event("close", timeout=0)
        except Exception as exc:  # noqa: BLE001 - closing the window is normal
            log.debug("har_recording_ended", detail=str(exc)[:150])
        finally:
            context.close()

    log.info("har_recording_saved", path=str(target), exists=target.exists())
    return target


def replay(context: Any, platform: str, variant: str, *, url_glob: str = "**/*") -> bool:
    """Serve a recording back to a browser context. Returns False if absent.

    Tests call this instead of touching the network, so an adapter can be run
    against real captured markup.
    """
    path = har_path(platform, variant)
    if not path.exists():
        log.warning("har_recording_missing", platform=platform, variant=variant, path=str(path))
        return False
    context.route_from_har(str(path), url=url_glob, not_found="abort")
    return True


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI wiring
    parser = argparse.ArgumentParser(prog="python -m backend.apply.har")
    sub = parser.add_subparsers(dest="command", required=True)

    record_parser = sub.add_parser("record", help="capture a real application flow")
    record_parser.add_argument("--platform", required=True)
    record_parser.add_argument("--variant", required=True, choices=[v.key for v in VARIANTS])
    record_parser.add_argument("--url", default=None, help="start at a specific job URL")

    sub.add_parser("list", help="show which variants have been recorded")

    args = parser.parse_args(argv)
    configure_logging()

    if args.command == "record":
        record(args.platform, args.variant, url=args.url)
        return 0

    recorded = list_recordings()
    for variant in VARIANTS:
        have = variant.key in recorded.get(variant.platform, [])
        log.info(
            "har_variant",
            platform=variant.platform,
            variant=variant.key,
            recorded=have,
            description=variant.description,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
