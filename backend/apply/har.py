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
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.boards import BOARDS
from backend.config import settings
from backend.logging_setup import configure_logging, get_logger

log = get_logger(__name__)

__all__ = [
    "VARIANTS",
    "Variant",
    "har_path",
    "list_recordings",
    "missing_recordings",
    "ingest",
    "record",
    "replay",
    "snapshots_path",
]


@dataclass(frozen=True)
class Variant:
    """One flow shape worth capturing, and why it differs."""

    key: str
    platform: str
    description: str


# The shapes that actually change adapter behaviour, from the board registry.
# Recording all of them covers the branches that would otherwise only be
# exercised in production.
VARIANTS: tuple[Variant, ...] = tuple(
    Variant(key, entry.key, description)
    for entry in BOARDS
    for key, description in entry.har_variants
)


def har_path(platform: str, variant: str) -> Path:
    """Where a recording lives. One file per platform and variant."""
    return settings.har_dir / platform / f"{variant}.har"


def snapshots_path(platform: str, variant: str) -> Path:
    """Where the per-step DOM snapshots live, beside the HAR.

    The HAR captures the network. It cannot capture a modal that JavaScript
    built after the document loaded — which is every step of Easy Apply — so
    the markup that matters is only in these.
    """
    return har_path(platform, variant).with_suffix(".steps.json")


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
            "Walk through ONE application by hand, pressing Shift+Enter at each step "
            "to snapshot the DOM, then close the browser window. "
            "Use a job you are willing to actually apply to, or stop before the "
            "final submit — the capture is still useful."
        ),
    )

    steps: list[dict[str, str]] = []

    with sync_playwright() as playwright:
        context = launch_context(
            playwright, record_har_path=str(target), record_har_content="embed"
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()

            def snapshot(label: str) -> None:
                """Freeze the DOM as it is right now.

                Bound to every navigation and, more importantly, to Enter — the
                user presses it after each step of the modal. Without a manual
                trigger there is nothing to hook: an Easy Apply step transition
                fires no navigation event at all.
                """
                try:
                    steps.append(
                        {"label": label, "url": page.url, "html": page.content()}
                    )
                    log.info("har_step_captured", index=len(steps) - 1, label=label)
                except Exception as exc:  # noqa: BLE001 - a closed page is normal
                    log.debug("har_step_capture_failed", error=str(exc)[:120])

            page.expose_binding(
                "jobseekrCaptureStep", lambda _source: snapshot("manual")
            )
            page.add_init_script(
                # Shift+Enter, so it cannot collide with submitting a field.
                "window.addEventListener('keydown', e => {"
                "  if (e.key === 'Enter' && e.shiftKey) window.jobseekrCaptureStep();"
                "});"
            )
            page.on("load", lambda _page: snapshot("load"))

            page.goto(start_url, wait_until="domcontentloaded")
            # Block until the human closes the window.
            page.wait_for_event("close", timeout=0)
        except Exception as exc:  # noqa: BLE001 - closing the window is normal
            log.debug("har_recording_ended", detail=str(exc)[:150])
        finally:
            context.close()

    if steps:
        snapshot_target = snapshots_path(platform, variant)
        snapshot_target.write_text(
            json.dumps(
                {"platform": platform, "variant": variant, "steps": steps}, indent=2
            ),
            encoding="utf-8",
        )
        log.info(
            "har_snapshots_saved", path=str(snapshot_target), steps=len(steps)
        )
    else:
        log.warning(
            "har_no_snapshots",
            note=(
                "no DOM snapshots were taken, so only the server-rendered "
                "document can be extracted. Press Shift+Enter at each step next time."
            ),
        )

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


def ingest(platform: str, variant: str, *, dry_run: bool = False) -> int:
    """Fold a recording into site knowledge and the answer bank.

    This is the step that makes a capture session worth doing: it writes what
    was seen into ``data/siteknowledge/`` rather than leaving it as evidence for
    a human to hand-transcribe.

    Merges. Success counts, failure counts and promotions are production
    evidence about what has actually been working, which a capture cannot know,
    so a re-capture adds strategies and never resets them.
    """
    from backend.apply.harextract import extract, merge_into, push_questions_to_answer_bank
    from backend.db import session_scope
    from backend.siteknowledge import load

    capture = extract(har_path(platform, variant), platform=platform, variant=variant)
    if not capture.steps:
        log.error(
            "capture_empty",
            platform=platform,
            variant=variant,
            note="nothing to ingest — record the flow first",
        )
        return 1

    knowledge = load(platform)
    report = merge_into(knowledge, capture)

    if dry_run:
        log.warning(
            "ingest_dry_run",
            new_elements=report.new_elements,
            elements_gaining_strategies=sorted(report.new_strategies),
            new_variant=report.new_variant,
            questions=[element.label for element in capture.questions],
            note="nothing written",
        )
        return 0

    knowledge.save()
    with session_scope() as session:
        added = push_questions_to_answer_bank(session, capture)

    log.info(
        "ingest_complete",
        platform=platform,
        variant=variant,
        new_elements=len(report.new_elements),
        elements_gaining_strategies=len(report.new_strategies),
        preserved=len(report.preserved_counts),
        new_variant=report.new_variant,
        questions_added=added,
    )
    return 0


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI wiring
    parser = argparse.ArgumentParser(prog="python -m backend.apply.har")
    sub = parser.add_subparsers(dest="command", required=True)

    record_parser = sub.add_parser("record", help="capture a real application flow")
    record_parser.add_argument("--platform", required=True)
    record_parser.add_argument("--variant", required=True, choices=[v.key for v in VARIANTS])
    record_parser.add_argument("--url", default=None, help="start at a specific job URL")

    sub.add_parser("list", help="show which variants have been recorded")

    ingest_parser = sub.add_parser(
        "ingest", help="turn a recording into site knowledge (merges, never overwrites)"
    )
    ingest_parser.add_argument("--platform", required=True)
    ingest_parser.add_argument("--variant", required=True, choices=[v.key for v in VARIANTS])
    ingest_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing anything",
    )

    args = parser.parse_args(argv)
    configure_logging()

    if args.command == "record":
        record(args.platform, args.variant, url=args.url)
        return 0

    if args.command == "ingest":
        return ingest(args.platform, args.variant, dry_run=args.dry_run)

    missing = {(v.platform, v.key) for v in missing_recordings()}
    for variant in VARIANTS:
        log.info(
            "har_variant",
            platform=variant.platform,
            variant=variant.key,
            recorded=(variant.platform, variant.key) not in missing,
            description=variant.description,
        )
    log.info("har_coverage", recorded=len(VARIANTS) - len(missing), total=len(VARIANTS))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
