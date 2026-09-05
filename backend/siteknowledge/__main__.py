"""Inspect and undo changes to site knowledge.

    uv run python -m backend.siteknowledge history linkedin
    uv run python -m backend.siteknowledge rollback linkedin 4
    uv run python -m backend.siteknowledge health

Three things write these files — resolution promoting a strategy, a HAR capture
ingesting one, and a person editing the JSON — and until now none of them left a
record. A capture that overwrote a working selector was permanent, and the only
way back was to remember what it used to say.
"""

from __future__ import annotations

import argparse

from backend.logging_setup import configure_logging, get_logger
from backend.siteknowledge import load, rollback

log = get_logger(__name__)


def _history(platform: str) -> int:
    entries = load(platform).history()
    if not entries:
        print(f"No history for {platform} yet — nothing has been saved over.")
        return 0

    print(f"{platform}: {len(entries)} kept versions, oldest first\n")
    for entry in entries:
        print(
            f"  v{entry['version']:<4} {entry.get('at', '')[:19]}  "
            f"{entry.get('elements', '?'):>3} elements  {entry.get('reason', '')}"
        )
    print(
        f"\nRestore one with: "
        f"uv run python -m backend.siteknowledge rollback {platform} <version>"
    )
    return 0


def _rollback(platform: str, version: int) -> int:
    if not rollback(platform, version):
        print(f"No version {version} kept for {platform}. Try `history {platform}`.")
        return 1
    print(
        f"{platform} rolled back to v{version}. The version it replaced was kept, "
        f"so this is undoable too."
    )
    return 0


def _health() -> int:
    from backend.db import session_scope
    from backend.siteknowledge.health import (
        degrading,
        pending_proposals,
        platform_churn,
    )

    proposals = pending_proposals()
    failing = degrading()
    with session_scope() as session:
        churn = platform_churn(session)

    if proposals:
        print("Suggested fixes awaiting you:")
        for platform, key, selector in proposals:
            print(f"  {platform}/{key}: {selector}")
        print()

    if failing:
        print("Degrading elements (still working, on their way to not):")
        for element in failing:
            print(
                f"  {element.platform}/{element.key}: "
                f"{element.success_count} ok / {element.fail_count} failed "
                f"({element.confidence:.0%})"
            )
        print()

    moving = [row for row in churn if row.events or row.previous_events]
    if moving:
        print("Platform churn, this week against last:")
        for row in moving:
            print(
                f"  {row.platform}: {row.events} (was {row.previous_events})"
                + (f" — quiet since: {', '.join(row.healed)}" if row.healed else "")
            )
        print()

    if not (proposals or failing or moving):
        print("Nothing to report: no proposals, no degrading elements, no churn.")
    return 0


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI wiring
    configure_logging()
    parser = argparse.ArgumentParser(prog="python -m backend.siteknowledge")
    sub = parser.add_subparsers(dest="command", required=True)

    history = sub.add_parser("history", help="kept versions of a platform's elements")
    history.add_argument("platform")

    back = sub.add_parser("rollback", help="restore a kept version")
    back.add_argument("platform")
    back.add_argument("version", type=int)

    sub.add_parser("health", help="proposals, degrading elements and churn")

    args = parser.parse_args(argv)
    if args.command == "history":
        return _history(args.platform)
    if args.command == "rollback":
        return _rollback(args.platform, args.version)
    return _health()


if __name__ == "__main__":  # pragma: no cover - CLI wiring
    raise SystemExit(main())
