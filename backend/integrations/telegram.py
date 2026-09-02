"""The Telegram bot: how a parked job gets answered and how the user says stop.

The escalation rule matters more than the transport. When an adapter cannot
resolve a screening question, the job is **parked** — the browser closes, the
job is marked ``needs_answer``, and the question is asked here. The browser is
never held open waiting for a human: a session pinned for twenty minutes on a
job board while someone is asleep is exactly the pattern that gets an account
flagged, and a timeout mid-form leaves an application half-submitted.

So the loop is: park → ask → save the answer to the answer bank → re-queue.
The answer is saved, not just used, which is what makes the bank
self-populating: the same question never has to be asked twice.

Single user, single chat. There is no authorisation model beyond
``TELEGRAM_CHAT_ID`` because there is exactly one legitimate recipient.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import select

from backend.config import settings
from backend import failures, preferences
from backend.db import session_scope
from backend.integrations.notify import Priority, set_sender
from backend.logging_setup import configure_logging, get_logger
from backend.models import (
    AnswerBank,
    AnswerType,
    Application,
    ApplicationOutcome,
    Campaign,
    Job,
    JobStatus,
    MatchType,
)

log = get_logger(__name__)

__all__ = ["build_application", "escalate_question", "send_digest", "send_message"]


def _configured() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


def send_message(text: str, priority: Priority = Priority.NORMAL) -> bool:
    """Send one message. Returns False rather than raising when unconfigured."""
    if not _configured():
        log.warning("telegram_unconfigured", priority=priority.value, text=text[:200])
        return False

    import httpx

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        response = httpx.post(
            url,
            json={
                "chat_id": settings.telegram_chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if response.status_code != 200:
            log.error("telegram_send_failed", status=response.status_code, body=response.text[:200])
            return False
        return True
    except Exception as exc:
        log.exception("telegram_send_error", error=str(exc)[:200])
        return False


# ==========================================================================
# Escalation
# ==========================================================================


def escalate_question(job_id: int, question: str, *, choices: list[str] | None = None) -> bool:
    """Ask the user one screening question about a parked job.

    Called after the job is already parked and the browser is already closed.
    Nothing is waiting on the reply.
    """
    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None:
            return False
        title, company, url = job.title, job.company, job.url

    hint = (
        f"\nOptions: {' / '.join(choices)}" if choices else "\nReply with the answer as text."
    )
    return send_message(
        f"*Question needed*\n"
        f"{title} at {company}\n"
        f"[open the ad]({url})\n\n"
        f"_{question}_{hint}\n\n"
        f"The answer is saved to the answer bank, so this is asked once.\n"
        f"Reply: `/answer {job_id} <your answer>`",
        Priority.IMMEDIATE,
    )


def save_answer(
    question: str,
    answer: str,
    *,
    campaign_id: int | None = None,
    row_id: int | None = None,
) -> int:
    """Store an answer and return its row id.

    Updates a blank row for the same question rather than adding a duplicate —
    the seeded questions exist precisely to be filled in.

    ``row_id`` is the row the abstention actually matched, and it is the whole
    reason the loop terminates. The escalated question is the *form's* wording;
    a seeded row's ``question_pattern`` is a regex. Matching those two by string
    equality never succeeds, so without this the reply is filed as a new row
    while the matched row stays blank, and on the retry the two tie in the
    candidate pool and resolve to AMBIGUOUS — the job re-parks on a question it
    has just been told the answer to, forever.
    """
    with session_scope() as session:
        existing = None
        if row_id is not None:
            existing = session.get(AnswerBank, row_id)
        if existing is None:
            existing = session.exec(
                select(AnswerBank).where(AnswerBank.question_pattern == question)
            ).first()

        if existing is not None:
            existing.answer_value = answer
            existing.verified_at = datetime.now(UTC)
            existing.updated_at = datetime.now(UTC)
            session.add(existing)
            session.flush()
            log.info("answer_saved", row_id=existing.id, question=question[:60])
            return existing.id

        row = AnswerBank(
            question_pattern=question,
            match_type=MatchType.FUZZY,
            answer_value=answer,
            answer_type=AnswerType.TEXT,
            campaign_id=campaign_id,
            verified_at=datetime.now(UTC),
            notes="answered over Telegram",
        )
        session.add(row)
        session.flush()
        log.info("answer_created", row_id=row.id, question=question[:60])
        return row.id


def requeue_job(job_id: int) -> bool:
    """Put a parked job back in line now that its question is answered."""
    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None or job.status != JobStatus.NEEDS_ANSWER:
            return False
        job.status = JobStatus.DOCUMENTS_READY
        # Cleared with the status it belongs to. A stale question left on a
        # re-queued job would be answered a second time by the next /answer,
        # overwriting a good answer-bank row with a reply meant for a different
        # question.
        job.needs_answer_question = None
        session.add(job)
        log.info("job_requeued", job_id=job_id)
        return True


# ==========================================================================
# Digest
# ==========================================================================


def build_digest(*, hours: int = 24) -> str:
    """The evening summary: what was sent, what is stuck, what it cost."""
    since = datetime.now(UTC) - timedelta(hours=hours)

    with session_scope() as session:
        applications = list(
            session.exec(select(Application).where(Application.applied_at >= since)).all()
        )
        jobs = {job.id: job for job in session.exec(select(Job)).all()}
        campaigns = {c.id: c for c in session.exec(select(Campaign)).all()}

        parked = list(
            session.exec(select(Job).where(Job.status == JobStatus.NEEDS_ANSWER)).all()
        )
        queued = list(
            session.exec(select(Job).where(Job.status == JobStatus.MANUAL_QUEUE)).all()
        )
        failed = list(session.exec(select(Job).where(Job.status == JobStatus.FAILED)).all())

    submitted = [a for a in applications if a.outcome == ApplicationOutcome.SUBMITTED]

    by_campaign: dict[str, list[Application]] = {}
    for application in submitted:
        job = jobs.get(application.job_id)
        campaign = campaigns.get(job.campaign_id) if job and job.campaign_id else None
        by_campaign.setdefault(campaign.name if campaign else "unassigned", []).append(
            application
        )

    lines = [f"*Daily digest* — {len(submitted)} applications in the last {hours}h"]

    for name, rows in sorted(by_campaign.items()):
        lines.append(f"\n*{name}* ({len(rows)})")
        for application in rows[:10]:
            job = jobs.get(application.job_id)
            if job is None:
                continue
            documents = ""
            if application.resume_doc_id:
                documents = f" · [resume](/api/documents/{application.resume_doc_id}/file)"
            if application.cover_letter_doc_id:
                documents += f" · [letter](/api/documents/{application.cover_letter_doc_id}/file)"
            lines.append(f"· {job.title} — {job.company}{documents}")

    if parked:
        lines.append(f"\n*Waiting on you* — {len(parked)} parked for a screening answer")
        for job in parked[:5]:
            lines.append(f"· {job.title} at {job.company} (`/job {job.id}`)")

    if queued:
        lines.append(f"\n*Manual queue* — {len(queued)} waiting")

    if failed:
        lines.append(f"\n*Failed* — {len(failed)} (parse gate or apply errors; see the dashboard)")

    # Trends, not events: a failure worth an immediate alert already got one
    # from the layer that detected it. What the digest adds is repetition.
    with session_scope() as session:
        lines.extend(failures.digest_lines(session))

        # Proposals batch here rather than interrupting when they are inferred:
        # an inference is never urgent, and a message the moment a fifth skip
        # lands is how this channel becomes one the user mutes. sweep_ignored
        # first, so a proposal nobody answered ages out instead of reappearing
        # forever.
        preferences.sweep_ignored(session)
        lines.extend(preferences.digest_lines(session))

    from backend.llm.client import budget_status

    spend = budget_status()
    lines.append(
        f"\n_Spend this month: ${spend.get('spent_usd', 0):.2f} of "
        f"${spend.get('cap_usd', settings.llm_monthly_cap_usd):.2f}_"
    )

    if not settings.allow_live_submit:
        lines.append("\n_ALLOW_LIVE_SUBMIT is off — nothing was actually submitted._")

    return "\n".join(lines)


def send_digest(*, hours: int = 24) -> bool:
    return send_message(build_digest(hours=hours), Priority.DIGEST)


# ==========================================================================
# Commands
# ==========================================================================


def _cmd_stop(argument: str) -> str:
    """/stop [campaign] — immediate, mid-action. Nothing further is submitted."""
    if argument.strip():
        with session_scope() as session:
            campaign = session.exec(
                select(Campaign).where(Campaign.name == argument.strip())
            ).first()
            if campaign is None:
                return f"No campaign called '{argument.strip()}'."
            campaign.active = False
            session.add(campaign)
        return f"Campaign '{argument.strip()}' paused. Other campaigns keep running."

    settings.stop_file.parent.mkdir(parents=True, exist_ok=True)
    settings.stop_file.write_text(
        f"stopped {datetime.now(UTC).isoformat()}\nvia Telegram /stop\n", encoding="utf-8"
    )
    # Any job mid-application will fail its guardrail check on the next submit
    # attempt and return to the queue rather than being sent.
    return "*STOPPED.* Nothing will be submitted until you /resume."


def _cmd_resume(argument: str) -> str:
    if argument.strip():
        with session_scope() as session:
            campaign = session.exec(
                select(Campaign).where(Campaign.name == argument.strip())
            ).first()
            if campaign is None:
                return f"No campaign called '{argument.strip()}'."
            campaign.active = True
            session.add(campaign)
        return f"Campaign '{argument.strip()}' resumed."

    settings.stop_file.unlink(missing_ok=True)
    return "Resumed. Applications will run at the next scheduled pass."


def _cmd_status(_: str) -> str:
    from backend.apply.guardrails import breaker_status
    from backend.llm.client import budget_status

    stopped = settings.stop_file.exists()
    since = datetime.now(UTC) - timedelta(hours=24)

    with session_scope() as session:
        today = len(
            list(session.exec(select(Application).where(Application.applied_at >= since)).all())
        )
        parked = len(
            list(session.exec(select(Job).where(Job.status == JobStatus.NEEDS_ANSWER)).all())
        )
        queued = len(
            list(session.exec(select(Job).where(Job.status == JobStatus.MANUAL_QUEUE)).all())
        )

    spend = budget_status()
    breakers = {k: v for k, v in breaker_status().items() if v.get("disabled")}

    return (
        f"*Status*\n"
        f"Running: {'NO — stopped' if stopped else 'yes'}\n"
        f"Live submit: {'ON' if settings.allow_live_submit else 'off (dry run)'}\n"
        f"Applications (24h): {today}\n"
        f"Parked for answers: {parked}\n"
        f"Manual queue: {queued}\n"
        f"Spend: ${spend.get('spent_usd', 0):.2f} / ${spend.get('cap_usd', 0):.2f}\n"
        + (f"Disabled platforms: {', '.join(breakers)}\n" if breakers else "")
    )


def _matched_row_id(session: Any, question: str, campaign_id: int | None) -> int | None:
    """The answer-bank row ``question`` matched but could not be answered from.

    None when nothing matched — a question the bank has never seen — in which
    case the answer becomes a new row.
    """
    from backend.apply.answers import Abstain, load_answers, resolve_answer

    outcome = resolve_answer(
        question, campaign_id, answers=load_answers(session, campaign_id)
    )
    return outcome.source_row_id if isinstance(outcome, Abstain) else None


def _cmd_answer(argument: str) -> str:
    """/answer <job_id> <answer> — save it and re-queue the job."""
    parts = argument.strip().split(maxsplit=1)
    if len(parts) < 2:
        return "Usage: /answer <job_id> <your answer>"

    try:
        job_id = int(parts[0])
    except ValueError:
        return "Usage: /answer <job_id> <your answer>"

    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None:
            return f"No job {job_id}."

        # Which question was this? The one the escalation recorded on the job.
        # This used to guess — the oldest blank answer-bank row — which is only
        # ever right when exactly one job is parked and its question already had
        # a row. With two jobs parked, or a question the bank has never seen, it
        # filed the answer against someone else's question: the real question
        # stayed unresolved, the job re-parked on the next pass, and a verified
        # answer elsewhere in the bank was overwritten. Fall back to the old
        # guess only for a job parked before this field existed.
        question = job.needs_answer_question
        if not question:
            blank = session.exec(
                select(AnswerBank)
                .where(AnswerBank.answer_value == "")
                .order_by(AnswerBank.updated_at)  # type: ignore[arg-type]
            ).first()
            question = blank.question_pattern if blank else f"question for job {job_id}"
            row_id = blank.id if blank else None
        else:
            # Ask the resolver which row this question matched, rather than
            # re-implementing the match here. It abstained on this exact
            # question during the apply pass; if it matched a blank row, that is
            # the row the answer belongs in.
            row_id = _matched_row_id(session, question, job.campaign_id)

    # Deliberately global (campaign_id stays None). A screening answer over
    # Telegram is a fact about the user, not about one campaign, and scoping it
    # would make every other campaign ask the same question again.
    save_answer(question, parts[1], row_id=row_id)
    requeued = requeue_job(job_id)
    return (
        f"Saved: _{question}_ → *{parts[1]}*\n"
        + ("Job re-queued." if requeued else "Job was not parked; answer still saved.")
    )


def _cmd_digest(_: str) -> str:
    return build_digest()


def _cmd_yes(argument: str) -> str:
    """Confirm a proposed preference. Only this makes an inference take effect."""
    return _decide_preference(argument, confirm=True)


def _cmd_no(argument: str) -> str:
    """Reject a proposed preference, and stop it being proposed again."""
    return _decide_preference(argument, confirm=False)


def _decide_preference(argument: str, *, confirm: bool) -> str:
    identifier = argument.strip().split()[0] if argument.strip() else ""
    if not identifier.isdigit():
        return "Usage: /yes <id> or /no <id> — the id is in the digest line."

    with session_scope() as session:
        row = (
            preferences.confirm(session, int(identifier))
            if confirm
            else preferences.reject(session, int(identifier))
        )
        if row is None:
            return f"No preference {identifier}."
        verb = "Active" if confirm else "Rejected"
        return f"{verb}: {row.key} = {row.value}"


COMMANDS = {
    "/stop": _cmd_stop,
    "/resume": _cmd_resume,
    "/status": _cmd_status,
    "/answer": _cmd_answer,
    "/digest": _cmd_digest,
    "/yes": _cmd_yes,
    "/no": _cmd_no,
}


def handle_command(text: str) -> str:
    """Dispatch a command. Pure enough to unit test without a bot."""
    text = text.strip()
    command, _, argument = text.partition(" ")
    handler = COMMANDS.get(command.split("@")[0].lower())
    if handler is None:
        return (
            "Commands: /stop [campaign] · /resume [campaign] · /status · "
            "/answer · /digest · /yes <id> · /no <id>"
        )
    try:
        return handler(argument)
    except Exception as exc:
        log.exception("telegram_command_failed", command=command, error=str(exc)[:200])
        return f"That failed: {type(exc).__name__}: {exc}"


def build_application() -> Any:
    """The long-polling bot, for `python -m backend.integrations.telegram`."""
    from telegram import Update
    from telegram.ext import Application as TelegramApplication
    from telegram.ext import ContextTypes, MessageHandler, filters

    if not _configured():
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")

    application = TelegramApplication.builder().token(settings.telegram_bot_token).build()

    async def on_message(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None or update.message is None:
            return
        # One user, one chat: anything else is not for us.
        if str(update.effective_chat.id) != str(settings.telegram_chat_id):
            log.warning("telegram_foreign_chat", chat_id=update.effective_chat.id)
            return
        reply = handle_command(update.message.text or "")
        await update.message.reply_text(reply, parse_mode="Markdown")

    application.add_handler(MessageHandler(filters.TEXT, on_message))
    return application


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI wiring
    parser = argparse.ArgumentParser(prog="python -m backend.integrations.telegram")
    parser.add_argument("--digest", action="store_true", help="send the digest and exit")
    args = parser.parse_args(argv)

    configure_logging()
    set_sender(send_message)

    if args.digest:
        return 0 if send_digest() else 1

    build_application().run_polling()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
