"""The inbound sweep: read mail, match it, classify it, record the outcome.

Classification is an LLM call because the alternative — keyword rules over
recruitment boilerplate — misreads the two cases that matter. "We were very
impressed by your application, however…" is a rejection that reads positive,
and "we would like to arrange a time to speak" is an interview request that
contains none of the obvious words.

An interview request notifies immediately. Everything else waits for the digest.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlmodel import select

from backend.config import settings
from backend.db import session_scope
from backend.integrations.gmail import default_since
from backend.integrations.matching import InboundEmail, match_email
from backend.integrations.notify import Priority, notify
from backend.llm.client import LLMBudgetExceeded, llm
from backend.logging_setup import configure_logging, get_logger
from backend.models import (
    Application,
    ApplicationOutcome,
    Job,
    JobStatus,
    ResponseStatus,
    Run,
    RunPhase,
)

log = get_logger(__name__)

__all__ = ["classify_email", "run_inbound_sweep", "sweep_ghosted"]


CLASSIFICATION_SCHEMA = {
    "type": "object",
    "title": "email_classification",
    "properties": {
        "category": {
            "type": "string",
            "enum": [
                "acknowledgment",
                "rejection",
                "interview_request",
                "recruiter_outreach",
                "irrelevant",
            ],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "summary": {"type": "string", "description": "One sentence, plain."},
    },
    "required": ["category", "confidence", "summary"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You classify replies to job applications. Australian recruitment mail is "
    "polite: a rejection often opens with praise, and an interview request "
    "often just proposes a time. Judge by what the sender is DOING, not by "
    "tone. 'irrelevant' covers newsletters, job alerts and anything that is "
    "not a reply to an application."
)

CATEGORY_TO_STATUS = {
    "acknowledgment": ResponseStatus.ACKNOWLEDGED,
    "rejection": ResponseStatus.REJECTED,
    "interview_request": ResponseStatus.INTERVIEW_REQUEST,
    "recruiter_outreach": ResponseStatus.RECRUITER_OUTREACH,
}


def classify_email(email: InboundEmail) -> dict[str, Any]:
    """Classify one message. Returns 'irrelevant' rather than raising."""
    prompt = (
        f"From: {email.from_address}\n"
        f"Subject: {email.subject}\n\n"
        f"{email.body[:2500]}"
    )
    try:
        return llm.complete_json(
            prompt,
            model=settings.llm_model_classify,
            purpose="email_classification",
            schema=CLASSIFICATION_SCHEMA,
            system=_SYSTEM,
        )
    except LLMBudgetExceeded:
        raise
    except Exception as exc:
        log.exception("classification_failed", subject=email.subject[:80], error=str(exc)[:200])
        return {"category": "irrelevant", "confidence": 0.0, "summary": "classification failed"}


def _seen_path() -> Path:
    return settings.data_dir / "seen_emails.json"


def _load_seen() -> set[str]:
    path = _seen_path()
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return set()


def _save_seen(seen: set[str]) -> None:
    try:
        path = _seen_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Keep the file bounded; message ids older than the sweep window can
        # never come back.
        path.write_text(json.dumps(sorted(seen)[-5000:]), encoding="utf-8")
    except OSError as exc:
        log.warning("seen_emails_unwritable", error=str(exc))


def run_inbound_sweep(
    *,
    since: datetime | None = None,
    reader: Any = None,
    session_factory: Callable[[], Any] = session_scope,
    limit: int = 200,
) -> Run:
    """Read recent mail, attach what matches, and record the run."""
    started = datetime.now(UTC)
    since = since or default_since()
    counts = {"fetched": 0, "matched": 0, "unmatched": 0, "irrelevant": 0, "interviews": 0}
    errors: list[dict[str, Any]] = []

    if reader is None:
        from backend.integrations.gmail import build_reader

        reader = build_reader()

    try:
        emails = reader.fetch_recent(since=since, limit=limit)
    except Exception as exc:
        log.exception("inbound_fetch_failed", error=str(exc)[:200])
        emails = []
        errors.append({"stage": "fetch", "error": str(exc)[:300]})

    counts["fetched"] = len(emails)
    seen = _load_seen()

    with session_factory() as session:
        applications = list(
            session.exec(
                select(Application).where(Application.outcome == ApplicationOutcome.SUBMITTED)
            ).all()
        )
        jobs = {job.id: job for job in session.exec(select(Job)).all()}

        for email in emails:
            if email.message_id and email.message_id in seen:
                continue

            candidate = match_email(email, applications, jobs)
            if candidate is None:
                counts["unmatched"] += 1
                if email.message_id:
                    seen.add(email.message_id)
                continue

            try:
                classification = classify_email(email)
            except LLMBudgetExceeded as exc:
                errors.append({"stage": "classify", "error": str(exc)})
                log.error("inbound_halted_on_budget", detail=str(exc))
                break

            category = classification.get("category", "irrelevant")
            if category == "irrelevant":
                counts["irrelevant"] += 1
                if email.message_id:
                    seen.add(email.message_id)
                continue

            application = session.get(Application, candidate.application_id)
            if application is None:
                continue

            status = CATEGORY_TO_STATUS.get(category)
            if status is not None:
                application.response_status = status
                application.response_at = email.received_at
                session.add(application)
                counts["matched"] += 1

            job = jobs.get(application.job_id)
            if status is ResponseStatus.INTERVIEW_REQUEST:
                counts["interviews"] += 1
                # The one thing worth interrupting someone's evening for.
                notify(
                    "Interview request",
                    f"{job.title if job else 'A job'} at {job.company if job else '?'}\n\n"
                    f"{classification.get('summary', '')}\n\n"
                    f"From: {email.from_address}\nSubject: {email.subject}",
                    Priority.IMMEDIATE,
                )

            if email.message_id:
                seen.add(email.message_id)

        run = Run(
            started_at=started,
            ended_at=datetime.now(UTC),
            phase=RunPhase.EMAIL,
            counts=counts,
            errors=errors,
            ok=not errors,
        )
        session.add(run)
        session.flush()
        session.refresh(run)

    _save_seen(seen)
    log.info("inbound_sweep_complete", **counts)
    return run


def sweep_ghosted(
    *, days: int = 30, session_factory: Callable[[], Any] = session_scope
) -> int:
    """Mark applications with no contact after ``days`` as ghosted.

    Not a judgement about the employer — it is what makes the funnel honest.
    An application sitting at "no reply" forever inflates the pending count and
    hides the real reply rate.
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)
    changed = 0

    with session_factory() as session:
        rows = session.exec(
            select(Application).where(
                Application.response_status == ResponseStatus.NONE,
                Application.outcome == ApplicationOutcome.SUBMITTED,
            )
        ).all()

        for application in rows:
            applied_at = application.applied_at
            if applied_at.tzinfo is None:
                applied_at = applied_at.replace(tzinfo=UTC)
            if applied_at > cutoff:
                continue

            application.response_status = ResponseStatus.GHOSTED
            session.add(application)

            job = session.get(Job, application.job_id)
            if job is not None and job.status == JobStatus.APPLIED:
                job.status = JobStatus.GHOSTED
                session.add(job)
            changed += 1

    log.info("ghosting_sweep_complete", marked=changed, days=days)
    return changed


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI wiring
    parser = argparse.ArgumentParser(prog="python -m backend.integrations.inbound")
    parser.add_argument("--days", type=int, default=7, help="how far back to read")
    parser.add_argument("--ghost-after", type=int, default=None, help="run the ghosting sweep")
    args = parser.parse_args(argv)

    configure_logging()
    if args.ghost_after:
        sweep_ghosted(days=args.ghost_after)
        return 0

    run = run_inbound_sweep(since=datetime.now(UTC) - timedelta(days=args.days))
    return 0 if run.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
