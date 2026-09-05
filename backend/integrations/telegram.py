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
from pathlib import Path
from typing import Any

from sqlmodel import select

from backend import facts, failures, preferences, questions, sessions, telemetry
from backend.config import settings
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

__all__ = [
    "build_application",
    "escalate_question",
    "notify_followup_draft",
    "request_derivation_confirmation",
    "request_form_approval",
    "send_digest",
    "send_message",
    "send_photo",
    "send_weekly_digest",
]


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
            log.error(
                "telegram_send_failed",
                status=response.status_code,
                body=response.text[:200],
            )
            return False
        return True
    except Exception as exc:
        log.exception("telegram_send_error", error=str(exc)[:200])
        return False


# ==========================================================================
# Escalation
# ==========================================================================


def send_photo(path: str, caption: str = "") -> bool:
    """Send one image. Returns False rather than raising when unconfigured.

    Used for form approvals: a screenshot is the only way the user can judge
    whether a model put the right values in the right boxes without opening the
    site themselves. A text description of a form is not reviewable.
    """
    if not _configured():
        log.warning("telegram_unconfigured", caption=caption[:200])
        return False

    import httpx

    file_path = Path(path)
    if not file_path.exists():
        log.warning("telegram_photo_missing", path=path)
        return False

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendPhoto"
    try:
        with file_path.open("rb") as handle:
            response = httpx.post(
                url,
                data={
                    "chat_id": settings.telegram_chat_id,
                    "caption": caption[:1024],
                    "parse_mode": "Markdown",
                },
                files={"photo": handle},
                timeout=30,
            )
        if response.status_code != 200:
            log.warning("telegram_photo_failed", status=response.status_code)
            return False
        return True
    except Exception as exc:  # noqa: BLE001 - notification must never abort a run
        log.warning("telegram_photo_error", error=str(exc)[:200])
        return False


def request_derivation_confirmation(
    derivation_id: int,
    question: str,
    answer: str,
    fact_key: str,
    fact_text: str,
    reasoning: str,
) -> bool:
    """Ask once whether an answer derived from a fact is right.

    Once. After confirmation it is cached and this question never comes back —
    which is the whole reason facts exist rather than one stored value per
    question. The fact text is quoted in full so the user is checking the
    model's reading against their own words, not against a summary of them.
    """
    body = "\n".join(
        [
            "*Derived an answer — confirm once*",
            "",
            f"*Question:* {question[:200]}",
            f"*Answer:*  {answer[:120]}",
            "",
            f"*From your `{fact_key}` fact:*",
            f"_{fact_text[:400]}_",
            "",
            f"*Reasoning:* {reasoning[:300]}" if reasoning else "",
            "",
            f"`/yes d{derivation_id}` to confirm · `/no d{derivation_id}` if wrong",
            "_Confirmed once, then never asked again._",
        ]
    )
    return send_message(body, Priority.NORMAL)


def notify_followup_draft(
    message_id: int,
    job_id: int,
    to_address: str,
    subject: str,
    body: str,
    attachments: list[str],
) -> bool:
    """Show a drafted follow-up. The decision happens in the UI, not here.

    Deliberately not actionable over Telegram. Send / Skip / Edit needs the
    whole draft in front of you and an edit box, and a message that could send
    an email with one tap is a message one mistap sends an email from.

    Names both PDFs rather than attaching them: the point is to see what would
    go out, and the files are already on disk where the dashboard can show them.
    """
    lines = [
        "*Follow-up drafted* — nothing sent",
        "",
        f"*To:* {to_address}",
        f"*Subject:* {subject[:150]}",
        "",
        body.strip()[:700] + ("…" if len(body.strip()) > 700 else ""),
        "",
        f"*Attachments:* {', '.join(attachments) if attachments else 'none'}",
        "",
        (
            f"Review it on the Outbound page (job {job_id}, draft {message_id}): "
            "Send, Skip or Edit there."
        ),
    ]
    if not settings.outbound_enabled:
        lines.append("_OUTBOUND_ENABLED is off — nothing can be sent yet._")
    return send_message("\n".join(lines), Priority.NORMAL)


def request_form_approval(
    job_id: int,
    *,
    fingerprint: str,
    platform: str,
    screenshot: str | None = None,
    answers: dict[str, str] | None = None,
) -> bool:
    """Show the user a drafted application on an unknown form and ask.

    Sent instead of a submission, not after one. The application is fully built
    — documents through the parse gate, answers resolved, guardrails run — and
    then stopped, because the field mapping came from a model and has not been
    proven on this form shape yet.

    Three approvals on the same fingerprint graduate it to automatic
    (``formmaps.TRUST_THRESHOLD``). The fingerprint is the form's SHAPE, not the
    company, so two employers using the same template share the graduation and
    the user is asked once rather than once per employer.
    """
    lines = [
        f"*Form approval needed* — job {job_id} on {platform}",
        "",
        (
            "This form's field mapping came from the model and has not been proven "
            "on this shape yet, so nothing was submitted."
        ),
    ]
    if answers:
        lines.append("")
        lines.append("*What it would send:*")
        for question, value in list(answers.items())[:10]:
            lines.append(f"· {question[:70]}: {value[:60]}")
    lines.append("")
    lines.append(f"`/approve {job_id}` to send it · `/skip {job_id}` to drop it")
    lines.append(f"_shape {fingerprint[:12]} — 3 approvals graduate it to automatic_")

    body = "\n".join(lines)

    if screenshot:
        # Caption first: if the photo fails the user still gets the question,
        # rather than silently getting nothing.
        if send_photo(screenshot, caption=body[:1024]):
            return True
        log.warning("form_approval_photo_failed_falling_back_to_text", job_id=job_id)

    return send_message(body, Priority.IMMEDIATE)


def escalate_question(
    job_id: int, question: str, *, choices: list[str] | None = None
) -> bool:
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
        f"\nOptions: {' / '.join(choices)}"
        if choices
        else "\nReply with the answer as text."
    )
    return send_message(
        (
            f"*Question needed*\n"
            f"{title} at {company}\n"
            f"[open the ad]({url})\n\n"
            f"_{question}_{hint}\n\n"
            f"The answer is saved to the answer bank, so this is asked once.\n"
            f"Reply: `/answer {job_id} <your answer>`"
        ),
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
            session.exec(
                select(Application).where(Application.applied_at >= since)
            ).all()
        )
        jobs = {job.id: job for job in session.exec(select(Job)).all()}
        campaigns = {c.id: c for c in session.exec(select(Campaign)).all()}

        parked = list(
            session.exec(select(Job).where(Job.status == JobStatus.NEEDS_ANSWER)).all()
        )
        queued = list(
            session.exec(select(Job).where(Job.status == JobStatus.MANUAL_QUEUE)).all()
        )
        failed = list(
            session.exec(select(Job).where(Job.status == JobStatus.FAILED)).all()
        )

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
                documents = (
                    f" · [resume](/api/documents/{application.resume_doc_id}/file)"
                )
            if application.cover_letter_doc_id:
                documents += f" · [letter](/api/documents/{application.cover_letter_doc_id}/file)"
            lines.append(f"· {job.title} — {job.company}{documents}")

    if parked:
        lines.append(
            f"\n*Waiting on you* — {len(parked)} parked for a screening answer"
        )
        for job in parked[:5]:
            lines.append(f"· {job.title} at {job.company} (`/job {job.id}`)")

    if queued:
        lines.append(f"\n*Manual queue* — {len(queued)} waiting")

    if failed:
        lines.append(
            f"\n*Failed* — {len(failed)} (parse gate or apply errors; see the dashboard)"
        )

    # Trends, not events: a failure worth an immediate alert already got one
    # from the layer that detected it. What the digest adds is repetition.
    with session_scope() as session:
        lines.extend(failures.digest_lines(session))

        # Proposals batch here rather than interrupting when they are inferred:
        # an inference is never urgent, and a message the moment a fifth skip
        # lands is how this channel becomes one the user mutes. sweep_ignored
        # first, so a proposal nobody answered ages out instead of reappearing
        # forever.
        # Sessions first: a dead session is the reason a run did nothing, and
        # burying it under the application list is how it gets missed.
        lines.extend(sessions.digest_lines(session))

        preferences.sweep_ignored(session)
        lines.extend(preferences.digest_lines(session))

        # A reminder, not a second ask. resolve_from_facts already messaged when
        # it derived each of these; what the digest adds is that they are still
        # outstanding, and each one is a screening question no application can
        # answer until it is confirmed.
        waiting = facts.pending_confirmations(session)
        if waiting:
            lines.append(f"\n*Derived answers awaiting you* — {len(waiting)}")
            for row in waiting[:5]:
                lines.append(
                    f"· {row.question_text[:60]} -> {row.answer_value[:30]} "
                    f"(`/yes d{row.id}` · `/no d{row.id}`)"
                )

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


def build_weekly_digest(*, hours: int = 168) -> str:
    """The Sunday review: what the week taught the system, not what it did today.

    Separate from the nightly digest rather than folded into it. The numbers
    here — which questions cost the most applications, whether coverage is
    climbing — move on a scale of weeks, and ``failures.digest_lines`` already
    names the cost of a section that appears every evening saying nothing: it
    stops being read, on exactly the evening it has something to say.

    Empty sections are omitted for the same reason, so a quiet week produces a
    short message rather than a page of zeroes.
    """
    lines = [f"*Weekly review* — last {max(1, hours // 24)}d"]

    with session_scope() as session:
        lines.extend(questions.digest_lines(session, hours=hours))
        lines.extend(telemetry.digest_lines(session, hours=hours))

        # One call, two readings. It was two calls with no write between them.
        all_leverage = facts.leverage(session)
        leverage = [row for row in all_leverage if row.confirmed]
        if leverage:
            lines.append("\n*Facts doing the work*")
            for row in leverage[:5]:
                stale = f" ({row.stale} stale)" if row.stale else ""
                lines.append(
                    f"· `{row.key}` answers {row.confirmed} "
                    f"question{'s' if row.confirmed != 1 else ''}{stale}"
                )

        idle = [row for row in all_leverage if not row.derived]
        if idle:
            lines.append(
                f"\n_{len(idle)} fact{'s' if len(idle) != 1 else ''} answering "
                f"nothing: {', '.join(row.key for row in idle[:5])}_"
            )

    if len(lines) == 1:
        lines.append("\n_Nothing asked and nothing derived this week._")
    return "\n".join(lines)


def send_weekly_digest(*, hours: int = 168) -> bool:
    return send_message(build_weekly_digest(hours=hours), Priority.DIGEST)


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
        f"stopped {datetime.now(UTC).isoformat()}\nvia Telegram /stop\n",
        encoding="utf-8",
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
            list(
                session.exec(
                    select(Application).where(Application.applied_at >= since)
                ).all()
            )
        )
        parked = len(
            list(
                session.exec(
                    select(Job).where(Job.status == JobStatus.NEEDS_ANSWER)
                ).all()
            )
        )
        queued = len(
            list(
                session.exec(
                    select(Job).where(Job.status == JobStatus.MANUAL_QUEUE)
                ).all()
            )
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
    return f"Saved: _{question}_ → *{parts[1]}*\n" + (
        "Job re-queued." if requeued else "Job was not parked; answer still saved."
    )


def _cmd_digest(_: str) -> str:
    return build_digest()


def _cmd_weekly(_: str) -> str:
    return build_weekly_digest()


def _decide_derivation(derivation_id: int, *, confirm: bool) -> str:
    """Confirm or reject an answer derived from a fact."""
    from backend import facts

    with session_scope() as session:
        if confirm:
            row = facts.confirm(session, derivation_id)
            if row is None:
                return f"No derivation {derivation_id}."
            return f"Confirmed: {row.question_text[:80]} -> {row.answer_value[:60]}"

        if not facts.reject(session, derivation_id):
            return f"No derivation {derivation_id}."
        return (
            "Rejected. It will be re-derived next time — if the fact itself is "
            "wrong, fix it on the Facts page first."
        )


def _cmd_yes(argument: str) -> str:
    """Confirm a proposed preference. Only this makes an inference take effect."""
    return _decide_preference(argument, confirm=True)


def _cmd_no(argument: str) -> str:
    """Reject a proposed preference, and stop it being proposed again."""
    return _decide_preference(argument, confirm=False)


def _decide_preference(argument: str, *, confirm: bool) -> str:
    identifier = argument.strip().split()[0] if argument.strip() else ""

    # A "d" prefix means a derived answer rather than a preference. One command
    # pair for both because the user should not have to remember which kind of
    # thing they are confirming — the id in the message says which.
    if identifier.startswith("d") and identifier[1:].isdigit():
        return _decide_derivation(int(identifier[1:]), confirm=confirm)

    if not identifier.isdigit():
        return "Usage: /yes <id> or /no <id> — the id is in the message."

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
    "/weekly": _cmd_weekly,
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
            "/answer · /digest · /weekly · /yes <id> · /no <id>"
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

    application = (
        TelegramApplication.builder().token(settings.telegram_bot_token).build()
    )

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
    parser.add_argument(
        "--digest", action="store_true", help="send the digest and exit"
    )
    args = parser.parse_args(argv)

    configure_logging()
    set_sender(send_message)

    if args.digest:
        return 0 if send_digest() else 1

    build_application().run_polling()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
