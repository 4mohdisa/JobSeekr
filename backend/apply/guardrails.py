"""The one gate every submit path passes through.

Claude.md hard rule 6: every submit path calls ``check_can_submit()``. No
bypass. There is deliberately no parameter that skips a check, no "force"
flag, and no alternate entry point — adding one would defeat the module.

Check 0 is ``ALLOW_LIVE_SUBMIT``. It is false by default and only the user
turns it on. Everything below it can be perfectly satisfied and the answer is
still no. That is the design: the whole engine is built and testable while
being incapable of sending anything until a human flips one environment
variable.

Every check is evaluated and reported, not short-circuited, so a blocked
application says *everything* that was wrong rather than making the user fix
one thing at a time. Checks that cannot be evaluated after a hard block are
reported as ``not_evaluated`` — never as passed.

This module deliberately does not import the browser layer. Session
authentication arrives as an injected predicate so the whole gate stays unit
testable without Playwright.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from backend.boards import BOARDS
from backend.config import settings
from backend.regions import config_for
from backend.discovery.normalize import canonical_company
from backend.logging_setup import get_logger
from backend.models import Application, ApplicationOutcome, Job

log = get_logger(__name__)

__all__ = [
    "CheckResult",
    "GuardrailResult",
    "breaker_status",
    "check_can_submit",
    "record_failure",
    "record_success",
    "trip_global_halt",
]


# Set by the integrations layer so guardrails can notify without importing it.
on_notify: Callable[[str, str], None] | None = None

CIRCUIT_BREAKER_THRESHOLD = 3
CIRCUIT_BREAKER_COOLDOWN_HOURS = 24

# Warm-up ramp: applications permitted per day, by week since the start date.
# Ramping matters because a brand new account submitting thirty applications on
# day one is the pattern platforms act on.
WARMUP_RAMP = (3, 6, 10, 15, 20)
WARMUP_CEILING = 25

# Artifacts that mean a template did not render. Any of these in a cover letter
# is a document that would embarrass the user.
COVER_LETTER_ARTIFACTS = (
    "{{",
    "}}",
    r"\VAR{",
    r"\BLOCK{",
    "TODO",
    "[COMPANY]",
    "[ROLE]",
    "[NAME]",
    "lorem ipsum",
    "XXX",
)


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    evaluated: bool = True


@dataclass
class GuardrailResult:
    allowed: bool
    checks: list[CheckResult] = field(default_factory=list)
    blocked_by: str | None = None

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if c.evaluated and not c.passed]

    def summary(self) -> str:
        if self.allowed:
            return f"allowed ({len(self.checks)} checks passed)"
        return "BLOCKED by " + "; ".join(f"{c.name}: {c.detail}" for c in self.failures)


# --------------------------------------------------------------------------
# Circuit breaker state
# --------------------------------------------------------------------------


def _breaker_path() -> Path:
    return settings.data_dir / "circuit_breaker.json"


def _load_breaker() -> dict[str, Any]:
    path = _breaker_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("breaker_state_unreadable", error=str(exc))
        return {}


def _save_breaker(state: dict[str, Any]) -> None:
    try:
        path = _breaker_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as exc:
        log.error("breaker_state_unwritable", error=str(exc))


def record_success(platform: str) -> None:
    """Reset a platform's consecutive-failure count."""
    state = _load_breaker()
    entry = state.setdefault(platform, {})
    entry["consecutive_failures"] = 0
    entry.pop("disabled_until", None)
    _save_breaker(state)


def record_failure(platform: str, reason: str, *, now: datetime | None = None) -> bool:
    """Record a failure. Returns True if this tripped the breaker."""
    now = now or datetime.now(UTC)
    state = _load_breaker()
    entry = state.setdefault(platform, {})
    entry["consecutive_failures"] = int(entry.get("consecutive_failures", 0)) + 1
    entry["last_reason"] = reason[:200]
    entry["last_failure_at"] = now.isoformat()

    tripped = entry["consecutive_failures"] >= CIRCUIT_BREAKER_THRESHOLD
    if tripped:
        until = now + timedelta(hours=CIRCUIT_BREAKER_COOLDOWN_HOURS)
        entry["disabled_until"] = until.isoformat()
        log.error(
            "circuit_breaker_tripped",
            platform=platform,
            consecutive_failures=entry["consecutive_failures"],
            disabled_until=entry["disabled_until"],
            reason=reason,
        )
        _notify(
            "Circuit breaker tripped",
            f"{platform}: {entry['consecutive_failures']} consecutive failures. "
            f"Disabled until {until:%Y-%m-%d %H:%M} UTC. Last error: {reason[:200]}",
        )
    _save_breaker(state)
    return tripped


def breaker_status(now: datetime | None = None) -> dict[str, Any]:
    """Current breaker state per platform, for the dashboard."""
    now = now or datetime.now(UTC)
    state = _load_breaker()
    out: dict[str, Any] = {}
    for platform, entry in state.items():
        disabled_until = entry.get("disabled_until")
        active = False
        if disabled_until:
            try:
                active = datetime.fromisoformat(disabled_until) > now
            except ValueError:
                active = False
        out[platform] = {
            "consecutive_failures": entry.get("consecutive_failures", 0),
            "disabled": active,
            "disabled_until": disabled_until if active else None,
            "last_reason": entry.get("last_reason"),
        }
    return out


def _notify(title: str, body: str) -> None:
    if on_notify is None:
        log.warning("notify_hook_unset", title=title, body=body[:200])
        return
    try:
        on_notify(title, body)
    except Exception as exc:
        log.exception("notify_hook_failed", error=str(exc))


def trip_global_halt(reason: str) -> None:
    """Stop everything, now.

    Creates the STOP file, which check 1 reads and every runner honours. Used
    for the most serious failure mode in the system: a platform restriction
    notice, where continuing risks the user's account rather than one
    application.
    """
    try:
        settings.stop_file.parent.mkdir(parents=True, exist_ok=True)
        settings.stop_file.write_text(
            f"halted {datetime.now(UTC).isoformat()}\n{reason}\n", encoding="utf-8"
        )
    except OSError as exc:
        log.error("stop_file_unwritable", error=str(exc))

    log.error("global_halt", reason=reason)
    _notify("GLOBAL HALT", f"All applications stopped.\n\n{reason}\n\nDelete data/STOP to resume.")


# --------------------------------------------------------------------------
# Individual checks
# --------------------------------------------------------------------------


def _local_day_bounds(now: datetime) -> tuple[datetime, datetime]:
    """The user's local calendar day, as UTC instants.

    Daily caps are a *human* day cap: "no more than 10 applications today"
    means today where the user lives, not UTC midnight. Adelaide is UTC+9:30
    (+10:30 in DST), so counting on UTC boundaries would roll the cap over
    mid-evening.
    """
    tz = ZoneInfo(settings.timezone)
    local = now.astimezone(tz)
    start_local = datetime.combine(local.date(), time.min, tzinfo=tz)
    return start_local.astimezone(UTC), (start_local + timedelta(days=1)).astimezone(UTC)


def _applications_today(session: Session, now: datetime, platform: str | None) -> int:
    start, end = _local_day_bounds(now)
    query = select(Application).where(
        Application.applied_at >= start,
        Application.applied_at < end,
        Application.outcome == ApplicationOutcome.SUBMITTED,
    )
    if platform:
        query = query.where(Application.platform == platform)
    return len(list(session.exec(query).all()))


def _parse_hhmm(value: str, fallback: time) -> time:
    try:
        hour, _, minute = value.partition(":")
        return time(int(hour), int(minute or 0))
    except (TypeError, ValueError):
        return fallback


def _within_window(
    now: datetime, platform: str, *, timezone: str | None = None
) -> tuple[bool, str]:
    """Whether submitting is allowed at this instant, in the JOB's local time.

    LinkedIn is restricted to weekday business hours; applications arriving at
    3am Sunday are a pattern nobody wants attached to their account. Other
    platforms use the configured window. The policy is data, not branches, so
    adding a platform does not mean editing this function.

    ``timezone`` is the market's, not the machine's. An application to an
    Auckland employer should land inside Auckland business hours — NZ runs two
    to two and a half hours ahead of South Australia, and the gap is not fixed
    because the two observe DST on different schedules. Using the machine's
    timezone would put an NZ application at 7am local for half the year, which
    is exactly the "applied at an odd hour" pattern the window exists to avoid.
    """
    zone_name = timezone or settings.timezone
    tz = ZoneInfo(zone_name)
    local = now.astimezone(tz)

    policy = _WINDOW_POLICY.get(platform, _WINDOW_POLICY["_default"])
    if policy["weekdays_only"] and local.weekday() >= 5:
        return False, f"{local:%A} is a weekend in {zone_name}"

    start = _parse_hhmm(settings.apply_window_start, time(9, 0))
    end = _parse_hhmm(settings.apply_window_end, time(17, 0))
    if not (start <= local.time() < end):
        return False, (
            f"{local:%H:%M} {zone_name} is outside {start:%H:%M}-{end:%H:%M}"
        )
    return True, f"{local:%a %H:%M} {zone_name}"


# Which platforms are restricted to business days. Read from the board
# registry rather than restated here, so a new board cannot silently inherit
# the permissive default because somebody forgot this table.
_WINDOW_POLICY: dict[str, dict[str, Any]] = {
    **{entry.key: {"weekdays_only": entry.weekdays_only} for entry in BOARDS},
    "_default": {"weekdays_only": False},
}


def _warmup_allowance(now: datetime) -> tuple[int | None, str]:
    """How many applications the ramp permits today, or None when unset."""
    start: date | None = settings.apply_warmup_start_date
    if start is None:
        return None, "no warm-up start date configured"

    tz = ZoneInfo(settings.timezone)
    days = (now.astimezone(tz).date() - start).days
    if days < 0:
        return 0, f"warm-up starts {start.isoformat()}"

    week = days // 7
    if week < len(WARMUP_RAMP):
        return WARMUP_RAMP[week], f"week {week + 1} of warm-up"
    return WARMUP_CEILING, "warm-up complete"


def _last_submit_at(session: Session, platform: str | None) -> datetime | None:
    query = select(Application).where(Application.outcome == ApplicationOutcome.SUBMITTED)
    if platform:
        query = query.where(Application.platform == platform)
    rows = sorted(
        session.exec(query).all(), key=lambda r: r.applied_at, reverse=True
    )
    if not rows:
        return None
    stamp = rows[0].applied_at
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _cover_letter_artifacts(text: str) -> list[str]:
    lowered = text.casefold()
    return [token for token in COVER_LETTER_ARTIFACTS if token.casefold() in lowered]


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def check_can_submit(
    session: Session,
    job: Job,
    draft: Any,
    *,
    now: datetime | None = None,
    is_authenticated: Callable[[str], bool] | None = None,
    min_interval_seconds: float | None = None,
) -> GuardrailResult:
    """Decide whether this application may be submitted. The only submit gate.

    ``draft`` is an ``ApplicationDraft`` (see ``backend.apply.draft``); it is
    typed loosely so this module stays importable without the flow layer.
    """
    now = now or datetime.now(UTC)
    checks: list[CheckResult] = []
    platform = getattr(draft, "platform", None) or job.source
    campaign = getattr(draft, "campaign", None)

    def add(name: str, passed: bool, detail: str = "") -> None:
        checks.append(CheckResult(name=name, passed=passed, detail=detail))

    # 0 — the master switch.
    live = bool(settings.allow_live_submit)
    add(
        "allow_live_submit",
        live,
        "ALLOW_LIVE_SUBMIT is false — the system cannot submit until you turn it on"
        if not live
        else "enabled",
    )

    # 1 — the emergency brake.
    stop_exists = settings.stop_file.exists()
    add(
        "stop_file_absent",
        not stop_exists,
        f"{settings.stop_file} exists — delete it to resume" if stop_exists else "",
    )

    # 2 — campaign active.
    campaign_active = bool(getattr(campaign, "active", True)) if campaign else True
    add(
        "campaign_active",
        campaign_active,
        "campaign is paused" if not campaign_active else "",
    )

    # 3 — platform daily cap.
    caps = (getattr(campaign, "daily_caps", None) or {}) if campaign else {}
    cap = caps.get(platform, caps.get("default"))
    if cap is None:
        add("platform_daily_cap", True, "no cap configured")
    else:
        used = _applications_today(session, now, platform)
        add(
            "platform_daily_cap",
            used < int(cap),
            f"{used}/{cap} submitted today on {platform} (local day)",
        )

    # 4 — campaign target goal.
    goal = getattr(campaign, "target_goal_count", None) if campaign else None
    if goal:
        total = len(
            list(
                session.exec(
                    select(Application).where(
                        Application.outcome == ApplicationOutcome.SUBMITTED
                    )
                ).all()
            )
        )
        add("campaign_target_goal", total < int(goal), f"{total}/{goal} toward goal")
    else:
        add("campaign_target_goal", True, "no goal configured")

    # 5 — one application per job, ever.
    already = session.exec(select(Application).where(Application.job_id == job.id)).first()
    add(
        "not_already_applied",
        already is None,
        f"application {already.id} already exists for job {job.id}" if already else "",
    )

    # 6 — score threshold.
    threshold = getattr(campaign, "score_auto_apply", None) if campaign else None
    score = getattr(draft, "score", None)
    if threshold is None:
        add("score_threshold", True, "no auto-apply threshold configured")
    elif score is None:
        add("score_threshold", False, "job has no score")
    else:
        add(
            "score_threshold",
            score >= threshold,
            f"score {score} vs auto-apply threshold {threshold}",
        )

    # 7 — company not excluded.
    exclusions = (getattr(campaign, "exclusions", None) or {}) if campaign else {}
    excluded = {
        canonical_company(str(name)) for name in (exclusions.get("companies") or [])
    }
    is_excluded = canonical_company(job.company) in excluded
    add("company_not_excluded", not is_excluded, job.company if is_excluded else "")

    # 8 — every attached document passed the parse gate.
    documents: Sequence[Any] = getattr(draft, "documents", None) or []
    ungated = [
        getattr(d, "kind", "?") for d in documents if not getattr(d, "parse_check_passed", False)
    ]
    add(
        "documents_parse_checked",
        bool(documents) and not ungated,
        f"documents failing the parse gate: {ungated}"
        if ungated
        else ("no documents attached" if not documents else ""),
    )

    # 9 — the cover letter is real.
    letter = (getattr(draft, "cover_letter_text", None) or "").strip()
    artifacts = _cover_letter_artifacts(letter)
    add(
        "cover_letter_clean",
        bool(letter) and not artifacts,
        "cover letter is empty"
        if not letter
        else (f"unrendered template artifacts: {artifacts}" if artifacts else ""),
    )

    # 10 — no abstentions. Hard rule 2.
    abstentions = getattr(draft, "abstentions", None) or []
    add(
        "no_abstentions",
        not abstentions,
        f"{len(abstentions)} unanswered screening questions: "
        + ", ".join(getattr(a, "question", "?") for a in abstentions[:3])
        if abstentions
        else "",
    )

    # 11 — session authenticated.
    if is_authenticated is None:
        checks.append(
            CheckResult(
                name="session_authenticated",
                passed=False,
                detail="no authentication predicate supplied",
                evaluated=False,
            )
        )
    else:
        try:
            authed = bool(is_authenticated(platform))
        except Exception as exc:
            authed = False
            log.exception("auth_check_failed", platform=platform, error=str(exc))
        add("session_authenticated", authed, f"{platform} session")

    # 11b — an LLM-mapped form that has not graduated yet.
    #
    # A form whose fields were mapped by a model is a hypothesis about where the
    # values go. Three clean applications on the same fingerprint is what turns
    # it into knowledge (formmaps.TRUST_THRESHOLD). Until then the application is
    # built in full, shown to the user with a screenshot, and NOT submitted.
    #
    # This is the half of trust graduation that was missing: record_outcome
    # already counted successes and set `trusted`, and nothing ever read it, so
    # a form the model guessed at thirty seconds ago was submitted exactly like
    # one proven three times.
    map_trusted = bool(getattr(draft, "form_map_trusted", True))
    add(
        "form_map_trusted",
        map_trusted,
        ""
        if map_trusted
        else (
            "this form's shape has not graduated yet — the application is drafted "
            "for your approval instead of submitted"
        ),
    )

    # 12 — inside the allowed window, measured where the JOB is.
    job_timezone = config_for(getattr(job, "region", None)).timezone
    in_window, window_detail = _within_window(now, platform, timezone=job_timezone)
    add("inside_window", in_window, window_detail)

    # 13 — randomised minimum interval elapsed.
    required = (
        min_interval_seconds
        if min_interval_seconds is not None
        else float(settings.apply_min_interval_floor_seconds)
    )
    last = _last_submit_at(session, platform)
    if last is None:
        add("min_interval_elapsed", True, "no previous submission on this platform")
    else:
        elapsed = (now - last).total_seconds()
        add(
            "min_interval_elapsed",
            elapsed >= required,
            f"{elapsed:.0f}s since last {platform} submit (need {required:.0f}s)",
        )

    # 14 — warm-up ramp.
    allowance, ramp_detail = _warmup_allowance(now)
    if allowance is None:
        add("warmup_ramp", True, ramp_detail)
    else:
        used_today = _applications_today(session, now, None)
        add(
            "warmup_ramp",
            used_today < allowance,
            f"{used_today}/{allowance} today ({ramp_detail})",
        )

    # 15 — circuit breaker not open for this platform.
    status = breaker_status(now).get(platform, {})
    disabled = bool(status.get("disabled"))
    add(
        "circuit_breaker_closed",
        not disabled,
        f"disabled until {status.get('disabled_until')} after "
        f"{status.get('consecutive_failures')} failures"
        if disabled
        else "",
    )

    failures = [c for c in checks if c.evaluated and not c.passed]
    result = GuardrailResult(
        allowed=not failures and all(c.evaluated for c in checks),
        checks=checks,
        blocked_by=failures[0].name if failures else None,
    )

    if result.allowed:
        log.info("guardrails_allowed", job_id=job.id, platform=platform)
    else:
        log.warning(
            "guardrails_blocked",
            job_id=job.id,
            platform=platform,
            blocked_by=result.blocked_by,
            failures=[c.name for c in result.failures],
            detail=result.summary(),
        )
    return result
