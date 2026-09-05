"""What runs when.

APScheduler with a SQLAlchemy job store, so the schedule survives a restart —
this runs on a desktop that reboots, and a schedule held only in memory would
quietly stop existing after a Windows update.

Apply passes carry deliberate jitter. Two applications arriving at exactly
14:00:00 every weekday is a machine signature no amount of per-submit pacing
hides, so the pass itself starts at a random offset within the hour.

    uv run python -m backend.integrations.scheduler
"""

from __future__ import annotations

import argparse
import random
from typing import Any

from backend.config import settings
from backend.logging_setup import configure_logging, get_logger

log = get_logger(__name__)

__all__ = ["build_scheduler", "describe_schedule"]


def _discovery_job() -> None:
    from backend.discovery.run import run_discovery

    run_discovery()


def _scoring_job() -> None:
    from backend.scoring.run import run_scoring

    run_scoring()


def _apply_job() -> None:
    from backend.apply.run import run_apply_pass

    # dry_run follows the master switch: with ALLOW_LIVE_SUBMIT off the pass
    # still runs end to end and reports what it would have sent, which is what
    # makes the scheduled path safe to leave enabled while evaluating.
    run_apply_pass(dry_run=not settings.allow_live_submit)


def _inbound_job() -> None:
    from backend.integrations.inbound import run_inbound_sweep

    run_inbound_sweep()


def _ghosting_job() -> None:
    from backend.integrations.inbound import sweep_ghosted

    sweep_ghosted(days=30)


def _digest_job() -> None:
    from backend.integrations.telegram import send_digest

    send_digest()


def _weekly_digest_job() -> None:
    from backend.integrations.telegram import send_weekly_digest

    send_weekly_digest()


def _canary_job() -> None:
    from backend.apply.canary import run_canary

    run_canary()


def _session_health_job() -> None:
    """Check every stored session once a day, before the morning apply pass.

    Separate from the apply pass's own check so a dead session is named in the
    morning rather than discovered at 10:00 when there is work queued behind it.
    """
    from playwright.sync_api import sync_playwright

    from backend.apply.session import launch_context
    from backend.db import session_scope
    from backend.sessions import check_all

    with sync_playwright() as playwright:
        context = launch_context(playwright)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            with session_scope() as session:
                check_all(session, context, page)
        finally:
            context.close()


def _backup_job() -> None:
    """Copy the SQLite file using the online backup API.

    ``sqlite3.Connection.backup`` is used rather than a file copy because the
    database is in WAL mode and being written to; copying the file alone can
    capture a torn state that reads fine and is missing the last transactions.
    """
    import sqlite3
    from datetime import UTC, datetime

    source_path = settings.database_url.replace("sqlite:///", "")
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    target = settings.backups_dir / f"app-{datetime.now(UTC):%Y%m%d-%H%M}.db"

    try:
        with (
            sqlite3.connect(source_path) as source,
            sqlite3.connect(target) as destination,
        ):
            source.backup(destination)
    except Exception as exc:
        log.exception("backup_failed", error=str(exc)[:200])
        return

    # Keep a fortnight; a desktop's disk is not a backup service.
    backups = sorted(settings.backups_dir.glob("app-*.db"))
    for stale in backups[:-14]:
        stale.unlink(missing_ok=True)

    log.info("backup_written", path=str(target), retained=min(len(backups), 14))


def _rubric_review_job() -> None:
    """Weekly: look at what actually got replies and propose rubric changes.

    Proposes only. A rubric change creates a new version and makes historical
    scores incomparable, so it is the user's call — the job sends the proposal
    to Telegram and stops there.
    """
    from backend.integrations.notify import Priority, notify

    try:
        from sqlmodel import select

        from backend.db import session_scope
        from backend.models import Application, ResponseStatus

        with session_scope() as session:
            applications = list(session.exec(select(Application)).all())

        replied = [
            a
            for a in applications
            if a.response_status
            in {ResponseStatus.ACKNOWLEDGED, ResponseStatus.INTERVIEW_REQUEST}
        ]
        if len(applications) < settings.analytics_min_sample:
            log.info(
                "rubric_review_skipped", reason="not enough data", n=len(applications)
            )
            return

        notify(
            "Weekly rubric review",
            f"{len(replied)} of {len(applications)} applications drew a reply.\n"
            "Open Analytics to see the breakdown by score decile — if the top decile is "
            "not out-performing the rest, the rubric is not discriminating and is worth "
            "re-weighting.\n\n"
            "Changing it creates a new rubric version; old scores stay attributed to the "
            "version they were computed under.",
            Priority.DIGEST,
        )
    except Exception as exc:
        log.exception("rubric_review_failed", error=str(exc)[:200])


# Schedule as data. Adding a job is a row, not a code change.
SCHEDULE: tuple[dict[str, Any], ...] = (
    {"id": "discovery", "func": _discovery_job, "trigger": "interval", "hours": 4},
    {
        "id": "scoring",
        "func": _scoring_job,
        "trigger": "interval",
        "hours": 4,
        "minutes": 20,
    },
    {
        "id": "apply_morning",
        "func": _apply_job,
        "trigger": "cron",
        "hour": 10,
        "minute": 0,
    },
    {
        "id": "apply_afternoon",
        "func": _apply_job,
        "trigger": "cron",
        "hour": 14,
        "minute": 30,
    },
    {"id": "inbound", "func": _inbound_job, "trigger": "interval", "hours": 2},
    {
        "id": "ghosting",
        "func": _ghosting_job,
        "trigger": "cron",
        "hour": 3,
        "minute": 0,
    },
    {"id": "backup", "func": _backup_job, "trigger": "cron", "hour": 2, "minute": 0},
    {"id": "digest", "func": _digest_job, "trigger": "cron", "hour": 19, "minute": 0},
    {"id": "canary", "func": _canary_job, "trigger": "cron", "hour": 8, "minute": 0},
    # Sunday, after the rubric review below and half an hour clear of it, so the
    # two weekly messages arrive as two readable things rather than one wall.
    {
        "id": "weekly_digest",
        "func": _weekly_digest_job,
        "trigger": "cron",
        "day_of_week": "sun",
        "hour": 18,
        "minute": 30,
    },
    # 09:00, an hour before the morning apply pass, so a dead session is named
    # while there is still time to sign in before anything is attempted.
    {
        "id": "session_health",
        "func": _session_health_job,
        "trigger": "cron",
        "hour": 9,
        "minute": 0,
    },
    {
        "id": "rubric_review",
        "func": _rubric_review_job,
        "trigger": "cron",
        "day_of_week": "sun",
        "hour": 18,
    },
)

# Jobs whose exact start time should not be predictable.
_JITTERED = {"apply_morning", "apply_afternoon"}
_JITTER_SECONDS = 45 * 60


def build_scheduler(*, rng: random.Random | None = None) -> Any:
    """Build the configured scheduler. Does not start it."""
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    from apscheduler.schedulers.background import BackgroundScheduler

    rng = rng or random.Random()

    scheduler = BackgroundScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=settings.database_url)},
        timezone=settings.timezone,
        job_defaults={
            # A missed run (laptop asleep) should not fire five times on wake.
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 3600,
        },
    )

    for entry in SCHEDULE:
        spec = dict(entry)
        job_id = spec.pop("id")
        func = spec.pop("func")
        trigger = spec.pop("trigger")

        if job_id in _JITTERED:
            spec["jitter"] = _JITTER_SECONDS

        scheduler.add_job(
            func, trigger=trigger, id=job_id, replace_existing=True, **spec
        )

    log.info(
        "scheduler_built",
        jobs=[entry["id"] for entry in SCHEDULE],
        timezone=settings.timezone,
        live_submit=settings.allow_live_submit,
    )
    return scheduler


def describe_schedule() -> list[dict[str, Any]]:
    """The schedule, for the dashboard and for `--list`."""
    out = []
    for entry in SCHEDULE:
        spec = {k: v for k, v in entry.items() if k not in {"func"}}
        spec["jittered"] = entry["id"] in _JITTERED
        out.append(spec)
    return out


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI wiring
    parser = argparse.ArgumentParser(prog="python -m backend.integrations.scheduler")
    parser.add_argument(
        "--list", action="store_true", help="print the schedule and exit"
    )
    args = parser.parse_args(argv)

    configure_logging()

    if args.list:
        for entry in describe_schedule():
            log.info("scheduled_job", **entry)
        return 0

    from backend.integrations.notify import register_hooks, set_sender
    from backend.integrations.telegram import send_message

    set_sender(send_message)
    register_hooks()

    scheduler = build_scheduler()
    scheduler.start()
    log.info("scheduler_started", note="Ctrl-C to stop")

    try:
        import time

        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        log.info("scheduler_stopped")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
