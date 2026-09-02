"""The apply pass: eligible jobs in, audited applications out.

    uv run python -m backend.apply.run --dry-run

Start with ``--dry-run``. It walks every step including the guardrail
evaluation and stops before submitting, so the user can read exactly what
would have been sent and why it would (or would not) have gone.

Between submits the runner sleeps a randomised interval — see
:mod:`backend.apply.pacing`. Nothing here decides whether an application may be
sent; that lives in ``guardrails.check_can_submit``, called once, inside the
flow.
"""

from __future__ import annotations

import argparse
import random
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, select

from backend import preferences
from backend.apply import guardrails
from backend.apply.flow import RestrictionDetected, run_apply
from backend.apply.pacing import sleep_between_submits
from backend.apply.session import (
    SessionExpired,
    ensure_logged_in,
    is_logged_in,
    launch_context,
)
from backend.base import ApplyOutcome
from backend.boards import applier_boards
from backend.config import settings
from backend.db import persist_detached, session_scope
from backend.logging_setup import configure_logging, get_logger
from backend.models import (
    Application,
    ApplyType,
    Campaign,
    Document,
    GrayZoneAction,
    Job,
    JobStatus,
    Run,
    RunPhase,
    Score,
)

log = get_logger(__name__)

__all__ = ["build_appliers", "eligible_jobs", "run_apply_pass"]


def build_appliers() -> list[Any]:
    """The platforms that can be applied to, in the order they are tried.

    Job boards first — they own the listing and their quick-apply flows are the
    cheapest path. External ATS adapters follow in Australian priority order
    (JobAdder and PageUp lead; Workday is last because it wants an account per
    employer). The flow itself never learns about any of them.
    """
    from backend.ats.adapters import build_ats_appliers

    return [entry.make_applier() for entry in applier_boards()] + build_ats_appliers()


def _applier_from_page(page: Any, job: Job, appliers: list[Any]) -> Any | None:
    """Identify the ATS from the loaded page when the URL did not name it.

    ``detect`` tries the URL first and falls back to the HTML fingerprint, which
    is what recognises a white-labelled portal — and an ``iframe`` to an
    embedded form builder, where the wrapper page is the employer's own brand
    and the form belongs to someone else entirely.

    Returns None rather than guessing: a job nothing can identify belongs in the
    manual queue, not in an adapter chosen on a hunch.
    """
    from backend.ats.detect import detect

    try:
        page.goto(job.url, wait_until="domcontentloaded")
        html = page.content()
    except Exception as exc:  # noqa: BLE001 - a probe must never end the pass
        log.warning("ats_html_probe_failed", job_id=job.id, error=str(exc)[:150])
        return None

    detection = detect(job.url, html)
    if detection.platform is None:
        return None

    applier = next(
        (a for a in appliers if getattr(a, "platform", None) == detection.key), None
    )
    if applier is None:
        # Recognised, but nothing can drive it. Loud, because this is the case
        # where adding one selector set would unlock a whole platform.
        log.warning(
            "ats_detected_without_adapter",
            job_id=job.id,
            platform=detection.key,
            evidence=detection.evidence,
        )
        return None

    log.info(
        "ats_detected_from_html",
        job_id=job.id,
        platform=detection.key,
        evidence=detection.evidence,
        iframe_src=detection.iframe_src,
    )
    return applier


def _latest_score(session: Session, job_id: int) -> float | None:
    row = session.exec(
        select(Score).where(Score.job_id == job_id).order_by(Score.scored_at.desc())  # type: ignore[union-attr]
    ).first()
    return row.final if row else None


def eligible_jobs(
    session: Session,
    *,
    campaign_id: int | None = None,
    platform: str | None = None,
    limit: int | None = None,
) -> list[tuple[Job, Campaign | None, float | None]]:
    """Jobs whose documents are ready and which have never been applied to.

    Ordered best-score-first: if the day's cap or the warm-up ramp cuts the run
    short, it should cut off the worst jobs, not an arbitrary slice.
    """
    query = select(Job).where(
        Job.status.in_([JobStatus.DOCUMENTS_READY, JobStatus.QUEUED])  # type: ignore[union-attr]
    )
    if campaign_id is not None:
        query = query.where(Job.campaign_id == campaign_id)
    if platform is not None:
        query = query.where(Job.source == platform)

    applied = {row.job_id for row in session.exec(select(Application)).all()}

    out: list[tuple[Job, Campaign | None, float | None]] = []
    for job in session.exec(query).all():
        if job.id in applied:
            continue
        documents = list(session.exec(select(Document).where(Document.job_id == job.id)).all())
        if not documents or not all(d.parse_check_passed for d in documents):
            continue
        campaign = session.get(Campaign, job.campaign_id) if job.campaign_id else None
        out.append((job, campaign, _latest_score(session, job.id)))

    out.sort(key=lambda item: item[2] or 0.0, reverse=True)
    return out[:limit] if limit else out


def _gray_zone_decision(
    job: Job, campaign: Campaign | None, score: float | None
) -> str:
    """What to do with a job scoring between the floor and the auto-apply mark.

    Returns "apply", "skip", "ask" or "queue". A job above the auto-apply
    threshold always returns "apply"; the gray zone is only the band between.
    """
    if campaign is None or score is None:
        return "apply"
    if score >= (campaign.score_auto_apply or 0):
        return "apply"
    if score < (campaign.score_floor or 0):
        return "skip"
    return (campaign.gray_zone_action or GrayZoneAction.QUEUE).value


def _handle_gray_zone(session: Session, job: Job, action: str) -> bool:
    """Apply the campaign's gray-zone policy. Returns True to keep applying."""
    if action == "apply":
        return True

    status = {
        "skip": JobStatus.SKIPPED,
        # "ask" and "queue" both land in the manual queue; the difference is
        # that "ask" also pages the user, which the integrations layer does off
        # the back of this status.
        "ask": JobStatus.MANUAL_QUEUE,
        "queue": JobStatus.MANUAL_QUEUE,
    }.get(action, JobStatus.MANUAL_QUEUE)

    job.status = status
    session.add(job)
    log.info("gray_zone_handled", job_id=job.id, action=action, status=status.value)
    return False


def run_apply_pass(
    *,
    campaign_id: int | None = None,
    platform: str | None = None,
    limit: int | None = None,
    dry_run: bool = True,
    session_factory: Callable[[], Any] = session_scope,
    page_factory: Callable[[], Any] | None = None,
    rng: random.Random | None = None,
) -> Run:
    """Run one apply pass and record the ``Run`` row.

    ``dry_run`` defaults True: the safe thing has to be the thing you get when
    you do not think about it.
    """
    started = datetime.now(UTC)
    errors: list[dict[str, Any]] = []
    counts = {"considered": 0, "submitted": 0, "blocked": 0, "parked": 0, "failed": 0}
    pending_escalations: list[tuple[int, str, list[str]]] = []
    appliers = build_appliers()
    rng = rng or random.Random()

    playwright = None
    context = None
    page = None

    with session_factory() as session:
        jobs = eligible_jobs(
            session, campaign_id=campaign_id, platform=platform, limit=limit
        )
        log.info("apply_pass_starting", eligible=len(jobs), dry_run=dry_run)

        def start_page() -> Any:
            """The browser is only started once there is real work for it."""
            nonlocal page, playwright, context
            if page is None:
                if page_factory is not None:
                    page = page_factory()
                else:
                    from playwright.sync_api import sync_playwright

                    playwright = sync_playwright().start()
                    context = launch_context(playwright)
                    page = context.pages[0] if context.pages else context.new_page()
            return page

        # Verify the session ONCE, before any work, rather than discovering it
        # is dead after building documents for a job that cannot be submitted.
        # The guardrail checks authentication again at submit time — this is the
        # nicer failure mode, not a replacement for it.
        checked_sessions: set[str] = set()

        def verify_session(platform_name: str) -> None:
            from backend.apply.session import PLATFORMS

            # Only platforms that HAVE a session. An employer ATS has no login
            # at all — Greenhouse serves its form to anyone — so "not signed in"
            # there is the normal state, and checking it would halt the pass on
            # every external application.
            if platform_name in checked_sessions or platform_name not in PLATFORMS:
                return
            checked_sessions.add(platform_name)
            ensure_logged_in(start_page(), platform_name)

        try:
            for index, (job, campaign, score) in enumerate(jobs):
                counts["considered"] += 1

                action = _gray_zone_decision(job, campaign, score)
                if not _handle_gray_zone(session, job, action):
                    continue

                applier = next((a for a in appliers if a.can_handle(job)), None)

                if applier is not None:
                    # Raises SessionExpired, which the pass already handles by
                    # halting — a dead session means every remaining job would
                    # fail the same way.
                    verify_session(applier.platform)

                if applier is None and job.apply_type in {
                    ApplyType.EXTERNAL,
                    ApplyType.UNKNOWN,
                }:
                    # The URL gave nothing away. In Australia that is the common
                    # case rather than an edge one: PageUp and JobAdder are
                    # routinely white-labelled onto careers.employer.com.au,
                    # where neither the host nor the path names the platform.
                    # Load the page and fingerprint the HTML before giving up —
                    # this is the only path that reaches detect_from_html, and
                    # without it every such job goes to the manual queue.
                    applier = _applier_from_page(start_page(), job, appliers)

                if applier is None:
                    log.warning(
                        "no_applier_for_job",
                        job_id=job.id,
                        source=job.source,
                        apply_type=job.apply_type.value,
                    )
                    job.status = JobStatus.MANUAL_QUEUE
                    session.add(job)
                    continue

                page = start_page()

                if index > 0 and not dry_run:
                    sleep_between_submits(rng)

                try:
                    result = run_apply(
                        page,
                        session,
                        job,
                        adapter=applier,
                        # Bind the page by value: a bare closure over the loop
                        # variable would re-read it when the guardrails call
                        # the predicate, which is the sort of thing that
                        # silently checks the wrong page's login state.
                        is_authenticated=lambda platform_name, _page=page: is_logged_in(
                            _page, platform_name
                        ),
                        dry_run=dry_run,
                    )
                except RestrictionDetected as exc:
                    # Everything stops. Not this job, everything.
                    errors.append({"job_id": job.id, "error": f"restriction: {exc}"})
                    log.error("apply_pass_halted_restriction", job_id=job.id)
                    break
                except SessionExpired as exc:
                    errors.append({"job_id": job.id, "error": str(exc)})
                    log.error("apply_pass_halted_session", platform=exc.platform)
                    break
                except Exception as exc:
                    counts["failed"] += 1
                    errors.append({"job_id": job.id, "error": f"{type(exc).__name__}: {exc}"})
                    log.exception("apply_failed", job_id=job.id)
                    guardrails.record_failure(applier.platform, str(exc))
                    continue

                if result.outcome is ApplyOutcome.SUBMITTED:
                    counts["submitted"] += 1
                elif result.outcome is ApplyOutcome.ABSTAINED:
                    counts["parked"] += 1
                    if result.needs_answer:
                        # Queued, not sent here. Asking inside this transaction
                        # races the reply: the pass holds the session open for
                        # the rest of the queue (minutes, with pacing), so a
                        # prompt /answer would look up a job whose NEEDS_ANSWER
                        # status has not been committed yet, and re-queueing
                        # would silently refuse.
                        pending_escalations.append(
                            (job.id, result.needs_answer, result.needs_answer_choices)
                        )
                elif result.outcome in {ApplyOutcome.BLOCKED, ApplyOutcome.DRY_RUN}:
                    counts["blocked"] += 1
                else:
                    counts["failed"] += 1
        finally:
            if context is not None:
                context.close()
            if playwright is not None:
                playwright.stop()

        run = Run(
            started_at=started,
            ended_at=datetime.now(UTC),
            phase=RunPhase.APPLY,
            counts={**counts, "dry_run": dry_run},
            errors=errors,
            ok=not errors,
        )
        persist_detached(session, run)

    # Outside the session: the parks are committed, so a reply that arrives
    # immediately finds the job in NEEDS_ANSWER and re-queues it.
    _escalate_parked(pending_escalations)

    # Look for patterns in what the user skipped. Nothing here is applied —
    # every result is a PROPOSED row that reaches the user in the evening
    # digest and changes no behaviour until they confirm it.
    try:
        with session_scope() as session:
            preferences.propose_from_skips(session)
    except Exception as exc:  # noqa: BLE001 - inference must not fail a pass
        log.warning("skip_inference_failed", error=str(exc)[:150])

    log.info("apply_pass_complete", **counts, dry_run=dry_run, errors=len(errors))
    return run


def _escalate_parked(escalations: list[tuple[int, str, list[str]]]) -> None:
    """Ask the user about each parked job. Never lets a send failure end the pass.

    This is the half of the answer-bank loop that turns a parked job into a
    question the user can actually answer. Without it a job parks and stops
    there forever, and the bank never self-populates.
    """
    if not escalations:
        return

    from backend.integrations.telegram import escalate_question

    sent = 0
    for job_id, question, choices in escalations:
        try:
            if escalate_question(job_id, question, choices=choices or None):
                sent += 1
            else:
                # send_message already logged why. Loud, not silent: the job
                # stays parked and nobody has been asked about it.
                log.warning("escalation_not_delivered", job_id=job_id)
        except Exception as exc:
            log.exception("escalation_failed", job_id=job_id, error=str(exc)[:200])

    log.info("escalations_sent", sent=sent, parked=len(escalations))


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI wiring
    parser = argparse.ArgumentParser(prog="python -m backend.apply.run")
    parser.add_argument("--campaign", type=int, default=None)
    parser.add_argument("--platform", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="fill and evaluate everything, submit nothing (the default)",
    )
    parser.add_argument(
        "--live",
        dest="dry_run",
        action="store_false",
        help="actually submit — still subject to ALLOW_LIVE_SUBMIT and every guardrail",
    )
    args = parser.parse_args(argv)

    configure_logging()
    if not args.dry_run and not settings.allow_live_submit:
        log.warning(
            "live_requested_but_switch_is_off",
            detail="ALLOW_LIVE_SUBMIT is false; the guardrails will block every submit",
        )

    run = run_apply_pass(
        campaign_id=args.campaign,
        platform=args.platform,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    return 0 if run.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
