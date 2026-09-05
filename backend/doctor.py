"""One command that says what is left before you flip the switch.

WHY THIS AND NOT THE SMOKE TEST
    ``backend.smoke`` answers "does this credential work" and costs a real round
    trip per integration. This answers "what is missing" and touches nothing:
    it reads configuration, the database and the filesystem, and it is safe to
    run at any moment including mid-application.

    Between them: doctor tells you what to set up, smoke tells you whether what
    you set up works.

TRAFFIC LIGHTS, AND WHAT THEY MEAN
    OK      nothing to do
    WARN    the system runs without it, but something is degraded or unproven
    BLOCK   an application cannot complete until this is fixed

    The distinction is the whole value. "No API key" and "no facts written" are
    both red on a checklist that has one colour, and only one of them stops an
    application from being submitted.

    Nothing here is a failure of the doctor. A fresh install is expected to be
    mostly BLOCK, and the exit code reflects blocks only so it can be used in a
    script without treating a warning as broken.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from backend.config import settings
from backend.logging_setup import configure_logging, get_logger

log = get_logger(__name__)

__all__ = ["CHECKS", "Finding", "run_doctor"]

OK = "ok"
WARN = "warn"
BLOCK = "block"


@dataclass
class Finding:
    """One prerequisite, its state, and the exact command to fix it."""

    name: str
    status: str
    detail: str = ""
    fix: str = ""
    #: Grouping for the printed report, so related things read together.
    group: str = "general"

    @property
    def blocking(self) -> bool:
        return self.status == BLOCK


def _finding(name: str, group: str) -> Callable[..., Finding]:
    def build(status: str, detail: str = "", fix: str = "") -> Finding:
        return Finding(name=name, status=status, detail=detail, fix=fix, group=group)

    return build


# --------------------------------------------------------------------------
# Tooling
# --------------------------------------------------------------------------


def check_pdflatex() -> Finding:
    make = _finding("pdflatex", "tooling")
    path = settings.pdflatex_path
    resolved = shutil.which(path) or (path if Path(path).exists() else None)
    if not resolved:
        return make(
            BLOCK,
            f"not found at {path!r}",
            "brew install --cask basictex  # then set PDFLATEX_PATH",
        )
    return make(OK, resolved)


def check_chrome() -> Finding:
    """Real Chrome, not bundled Chromium. Headless is a detection signal."""
    make = _finding("Chrome", "tooling")
    for candidate in (
        "/Applications/Google Chrome.app",
        "C:/Program Files/Google/Chrome/Application/chrome.exe",
    ):
        if Path(candidate).exists():
            return make(OK, candidate)
    if shutil.which("google-chrome") or shutil.which("google-chrome-stable"):
        return make(OK, "on PATH")
    return make(
        BLOCK,
        "Google Chrome is not installed",
        "brew install --cask google-chrome",
    )


def check_playwright_channel() -> Finding:
    """Whether Playwright can drive the configured channel.

    ``channel="chrome"`` uses the SYSTEM Chrome install, not a Playwright-managed
    browser build — so a cache full of chromium says nothing about it. The first
    version of this check counted cached builds and reported OK on chromium
    alone, which is exactly the false green it was written to prevent.

    What actually matters: the playwright package, and Chrome itself. The
    latter has its own check, so this defers to it rather than duplicating the
    lookup.
    """
    make = _finding("Playwright", "tooling")
    try:
        import playwright  # noqa: F401
    except ImportError:
        return make(
            BLOCK, "the playwright package is not installed", "uv sync --all-groups"
        )

    channel = settings.browser_channel
    if channel in {"chrome", "chrome-beta", "msedge"}:
        chrome = check_chrome()
        if chrome.status is not OK:
            return make(
                BLOCK,
                f"channel={channel!r} needs a system install of it",
                chrome.fix,
            )
        return make(OK, f"installed; channel={channel!r} uses the system browser")

    # A bundled channel (chromium, firefox, webkit) does need a downloaded
    # build, and then the cache is the right thing to look at.
    for cache in (
        Path.home() / "Library/Caches/ms-playwright",
        Path.home() / ".cache/ms-playwright",
    ):
        if cache.exists() and any(
            entry.is_dir() and entry.name.startswith(channel)
            for entry in cache.iterdir()
        ):
            return make(OK, f"{channel} build present")

    return make(
        BLOCK,
        f"no {channel} build downloaded",
        f"uv run playwright install {channel}",
    )


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


#: Every model an application cannot complete without, and what it does.
#: Checked per MODEL rather than per provider: each is a setting, and any one of
#: them can be pointed at a different provider without touching this.
_REQUIRED_MODELS: tuple[tuple[str, str], ...] = (
    ("llm_model_scoring", "scoring"),
    ("llm_model_embedding", "stage-1 ranking"),
    ("llm_model_writing", "resume and cover letter prose"),
    ("llm_model_formmap", "reading unknown application forms"),
)


def check_llm_keys() -> Finding:
    """Every configured model needs a key, whoever provides it.

    All four, not the two it used to check. Scoring and embeddings are on one
    Gemini key by default now, so those two passed while ``llm_model_writing``
    pointed at Anthropic with no ANTHROPIC_API_KEY set — and the report said
    "API keys OK" for a machine that cannot build a single document. A setup
    check that misses the thing blocking you is worse than no setup check.
    """
    make = _finding("API keys", "credentials")
    from backend.llm.client import _api_key_for

    missing = [
        (getattr(settings, field), purpose)
        for field, purpose in _REQUIRED_MODELS
        if not _api_key_for(getattr(settings, field))
    ]
    if not missing:
        return make(OK, f"all {len(_REQUIRED_MODELS)} configured models have keys")

    settings_to_fill = " and ".join(sorted({_key_setting(m) for m, _ in missing}))
    named = ", ".join(f"{purpose} ({model})" for model, purpose in missing)
    return make(
        BLOCK,
        f"no key for {named}",
        f"set {settings_to_fill} in .env",
    )


def _key_setting(model: str) -> str:
    """The .env name of the key a model needs, read off the gateway's own map.

    Named rather than left as "the matching key": naming two fixed providers was
    wrong the moment the embedding model moved to Gemini, and a second copy of
    the provider table is a second thing to forget to update.
    """
    from backend.llm.client import _PROVIDER_KEY_FIELDS

    provider = model.split("/", 1)[0].lower()
    field = _PROVIDER_KEY_FIELDS.get(provider)
    return field.upper() if field else f"a key for {provider}"


def check_telegram() -> Finding:
    """WARN, not BLOCK: applications still run, they just cannot ask anything.

    Which matters — an abstention parks a job and asks, so without Telegram the
    answer bank never self-populates and parked jobs stay parked. Degraded
    rather than stopped.
    """
    make = _finding("Telegram", "credentials")
    if settings.telegram_bot_token and settings.telegram_chat_id:
        return make(OK, "configured")
    return make(
        WARN,
        "not configured — parked jobs cannot ask you anything, so the answer "
        "bank will not self-populate",
        "set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env",
    )


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------


def check_migrations() -> Finding:
    make = _finding("Migrations", "data")
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        from sqlalchemy import inspect as sa_inspect

        from backend.db import engine

        config = Config("alembic.ini")
        head = ScriptDirectory.from_config(config).get_current_head()

        with engine.connect() as connection:
            if "alembic_version" not in sa_inspect(connection).get_table_names():
                return make(
                    BLOCK, "the database has no schema", "uv run alembic upgrade head"
                )
            current = connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar()
    except Exception as exc:  # noqa: BLE001
        return make(
            WARN, f"could not check: {str(exc)[:120]}", "uv run alembic upgrade head"
        )

    if current != head:
        return make(
            BLOCK,
            f"at {current}, head is {head}",
            "uv run alembic upgrade head",
        )
    return make(OK, f"at head ({head})")


def check_profile() -> Finding:
    make = _finding("Profile", "data")
    from sqlmodel import select

    from backend.db import session_scope
    from backend.models import Profile

    with session_scope() as session:
        profile = session.exec(select(Profile).order_by(Profile.version.desc())).first()  # type: ignore[union-attr]
        if profile is None:
            return make(BLOCK, "no profile row", "uv run python -m backend.seed")
        identity = profile.identity or {}
        if not identity.get("name") or not identity.get("email"):
            return make(
                BLOCK,
                "the profile has no name or email — every document needs both",
                "fill it in on the Profile page",
            )
        return make(OK, f"v{profile.version}, {identity.get('name')}")


def check_facts() -> Finding:
    """BLOCK: a blank fact answers nothing, and screening questions park jobs."""
    make = _finding("Facts", "data")
    from sqlmodel import select

    from backend.db import session_scope
    from backend.models import Fact

    with session_scope() as session:
        rows = list(session.exec(select(Fact)).all())
        if not rows:
            return make(BLOCK, "no fact rows", "uv run python -m backend.seed")

        written = [row for row in rows if row.text.strip()]
        if not written:
            return make(
                BLOCK,
                f"all {len(rows)} are blank — every screening question will park",
                "write them on the Facts page, then `backend.facts preview`",
            )
        if len(written) < len(rows):
            blank = sorted(row.key for row in rows if not row.text.strip())
            return make(
                WARN,
                f"{len(written)} of {len(rows)} written; blank: {', '.join(blank[:6])}",
                "fill the rest on the Facts page",
            )
        return make(OK, f"all {len(rows)} written")


def check_campaign() -> Finding:
    """An active campaign on placeholder terms is worse than none.

    None discovers nothing and says so; placeholder terms discover the wrong
    jobs and look like they are working.
    """
    make = _finding("Campaign", "data")
    from sqlmodel import select

    from backend.db import session_scope
    from backend.models import Campaign
    from backend.seed import STARTER_CAMPAIGN_NAME

    with session_scope() as session:
        campaigns = list(session.exec(select(Campaign)).all())
        if not campaigns:
            return make(BLOCK, "no campaigns", "uv run python -m backend.seed")

        active = [c for c in campaigns if c.active]
        if not active:
            return make(
                BLOCK,
                "no active campaign — discovery will find nothing",
                "activate one on the Campaigns page",
            )

        placeholder = [
            c
            for c in active
            if c.name == STARTER_CAMPAIGN_NAME
            and set(c.search_terms or []) == {"data analyst", "software engineer"}
        ]
        if placeholder:
            return make(
                WARN,
                f"{placeholder[0].name!r} is active on the seeded placeholder terms",
                "edit its search terms on the Campaigns page",
            )
        return make(OK, f"{len(active)} active")


def check_sessions() -> Finding:
    make = _finding("Sessions", "data")
    from sqlmodel import select

    from backend.db import session_scope
    from backend.models import SessionHealth, SessionStatus

    with session_scope() as session:
        rows = list(session.exec(select(SessionHealth)).all())
        if not rows:
            return make(
                WARN,
                "never checked — run an apply pass or wait for the 09:00 check",
                "uv run python -m backend.smoke --only cookies",
            )
        dead = [row.site for row in rows if row.status is SessionStatus.DEAD]
        if dead:
            return make(
                BLOCK,
                f"signed out of {', '.join(dead)}",
                f"uv run python -m backend.apply.session login --platform {dead[0]}",
            )
        live = [row.site for row in rows if row.status is SessionStatus.LIVE]
        if not live:
            return make(WARN, "no site is confirmed signed in", "sign in to the boards")
        return make(OK, f"{len(live)} live: {', '.join(sorted(live))}")


def check_site_knowledge() -> Finding:
    """Whether the strategies are captured or still the shipped guesses.

    WARN rather than BLOCK: the defaults may well work. But they were written
    without access to the live sites, so "it has never been verified" is worth
    saying out loud before someone turns on live submit.
    """
    make = _finding("Site knowledge", "data")
    import json

    root = settings.siteknowledge_dir
    if not root.exists():
        return make(WARN, "not seeded yet — it is written on first use", "")

    seeded = 0
    captured = 0
    for elements in sorted(root.glob("*/elements.json")):
        try:
            payload = json.loads(elements.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        notes = [
            strategy.get("note", "")
            for element in (payload.get("elements") or {}).values()
            for strategy in element.get("strategies", [])
        ]
        if any("captured" in note for note in notes):
            captured += 1
        elif notes:
            seeded += 1

    if captured:
        return make(OK, f"{captured} platform(s) have captured strategies")
    return make(
        WARN,
        f"{seeded} platform(s) still on shipped defaults — never verified against "
        "a live site",
        "uv run python -m backend.apply.har record --platform seek --variant quick_apply",
    )


def check_switches() -> Finding:
    """Reports rather than judges. These are the user's to set."""
    make = _finding("Switches", "switches")
    states = [
        f"ALLOW_LIVE_SUBMIT={'on' if settings.allow_live_submit else 'off'}",
        f"OUTBOUND_ENABLED={'on' if settings.outbound_enabled else 'off'}",
    ]
    return make(OK, ", ".join(states))


CHECKS: tuple[Callable[[], Finding], ...] = (
    check_pdflatex,
    check_chrome,
    check_playwright_channel,
    check_llm_keys,
    check_telegram,
    check_migrations,
    check_profile,
    check_facts,
    check_campaign,
    check_sessions,
    check_site_knowledge,
    check_switches,
)


def run_doctor() -> list[Finding]:
    """Run every check. A check that raises becomes a WARN, not a dead run."""
    findings: list[Finding] = []
    for check in CHECKS:
        try:
            findings.append(check())
        except Exception as exc:
            log.exception("doctor_check_raised", check=check.__name__)
            findings.append(
                Finding(
                    name=check.__name__.removeprefix("check_"),
                    status=WARN,
                    detail=f"the check itself failed: {str(exc)[:150]}",
                    group="general",
                )
            )
    return findings


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

_LIGHT = {OK: "OK   ", WARN: "WARN ", BLOCK: "BLOCK"}
_GROUP_TITLES = {
    "tooling": "Tooling",
    "credentials": "Credentials",
    "data": "Your data",
    "switches": "Switches",
    "general": "Other",
}


def render(findings: list[Finding]) -> str:
    width = max((len(f.name) for f in findings), default=12)
    lines = ["", "SETUP CHECK", "=" * 74]

    for group, title in _GROUP_TITLES.items():
        rows = [f for f in findings if f.group == group]
        if not rows:
            continue
        lines.append("")
        lines.append(f"{title}")
        for finding in rows:
            lines.append(
                f"  [{_LIGHT[finding.status]}] {finding.name:<{width}}  {finding.detail}"
            )
            if finding.fix and finding.status != OK:
                lines.append(f"           {'':<{width}}  -> {finding.fix}")

    blocks = [f for f in findings if f.status == BLOCK]
    warns = [f for f in findings if f.status == WARN]

    lines.append("")
    lines.append("=" * 74)
    if blocks:
        lines.append(f"{len(blocks)} blocking: {', '.join(f.name for f in blocks)}")
        lines.append("An application cannot complete until those are fixed.")
    else:
        lines.append("Nothing blocking.")
    if warns:
        lines.append(f"{len(warns)} warning: {', '.join(f.name for f in warns)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI wiring
    import argparse

    parser = argparse.ArgumentParser(prog="python -m backend.doctor")
    parser.parse_args(argv)
    configure_logging()

    findings = run_doctor()
    print(render(findings))

    # Blocks only. A warning is something to know about, not a broken install,
    # and exiting non-zero for one would make this useless in a script.
    return 1 if any(f.blocking for f in findings) else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
