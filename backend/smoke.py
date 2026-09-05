"""Exercise every real transport, once, on demand.

WHY THIS EXISTS
    Everything in this project is proven against fakes. The rehearsal runs the
    whole pipeline with a stub LLM, a fake page and a fake cookie jar — which
    proves the wiring and nothing about whether the credentials work. So the
    first real Telegram message, the first real model call and the first real
    browser launch would otherwise all happen in the middle of an application,
    where a failure costs a job rather than a minute.

    This is the thing to run after adding each credential.

WHAT A CHECK MAY DO
    Exactly one round trip, with the smallest possible payload. A smoke test
    that costs real money or sends several messages is one nobody runs.

SKIPPING IS A RESULT
    A missing credential is reported as SKIP with the reason and the setting to
    fill in — never as a failure. The whole point is to be runnable at any
    stage of setup and to say plainly what is not wired yet.

    Nothing here writes to the database, and nothing submits an application.

    uv run python -m backend.smoke
    uv run python -m backend.smoke --only telegram,gemini
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from backend.config import settings
from backend.logging_setup import configure_logging, get_logger

log = get_logger(__name__)

__all__ = ["CHECKS", "Result", "run_smoke"]


@dataclass
class Result:
    """What one check found."""

    name: str
    status: str  # pass | fail | skip
    detail: str = ""
    #: Anything worth printing that is not pass/fail — a token count, a cost.
    facts: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "pass"


def _skip(name: str, reason: str) -> Result:
    return Result(name, "skip", reason)


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


def check_telegram() -> Result:
    """Send one real message and confirm the API accepted it.

    Confirms delivery rather than "did not raise": send_message returns False
    when unconfigured and swallows transport errors by design, so a check that
    only looked for an absence of exceptions would pass with no bot at all.
    """
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return _skip(
            "telegram",
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in .env",
        )

    from backend.integrations.notify import Priority
    from backend.integrations.telegram import send_message

    sent = send_message(
        "*JobSeekr smoke test*\nIf you can read this, Telegram is wired up.",
        Priority.NORMAL,
    )
    if not sent:
        return Result(
            "telegram",
            "fail",
            "the API rejected the message — check the token and the chat id",
        )
    return Result("telegram", "pass", "message delivered; check your phone")


def check_llm_completion() -> Result:
    """One real completion, priced from what the provider actually charged.

    The cost comes from the llm_spend row this writes, not from the projection
    in scoring/run.py — the point of a smoke test is to find out what really
    happens, and the projected price is exactly the thing worth checking.
    """
    model = settings.llm_model_scoring
    if not _key_for(model):
        return _skip("llm completion", f"no API key for {model}")

    from backend.llm.client import llm

    reply = llm.complete(
        "Reply with the single word: ready",
        model=model,
        purpose="smoke_completion",
        temperature=0.0,
        max_tokens=16,
    )
    cost = _last_spend("smoke_completion")
    return Result(
        "llm completion",
        "pass",
        f"{model} replied {reply.strip()[:40]!r}",
        facts={
            "model": model,
            "input_tokens": cost.get("input_tokens", 0),
            "output_tokens": cost.get("output_tokens", 0),
            "cost_usd": cost.get("cost_usd", 0.0),
        },
    )


def check_llm_embedding() -> Result:
    """One real embedding. Separate check because it is a separate endpoint.

    Both default models are Gemini now, so one key covers both — but a key that
    works for completions can still be refused for embeddings (a different API
    enabled, a different quota), and stage 1 failing is a silent, total loss of
    ranking rather than a visible error.
    """
    model = settings.llm_model_embedding
    if not _key_for(model):
        return _skip("llm embedding", f"no API key for {model}")

    from backend.llm.client import llm

    vectors = llm.embed(
        ["data analyst, Adelaide"], model=model, purpose="smoke_embedding"
    )
    if not vectors or not vectors[0]:
        return Result("llm embedding", "fail", "the provider returned no vector")

    cost = _last_spend("smoke_embedding")
    return Result(
        "llm embedding",
        "pass",
        f"{len(vectors[0])} dimensions",
        facts={
            "model": model,
            "dimensions": len(vectors[0]),
            "cost_usd": cost.get("cost_usd", 0.0),
        },
    )


def check_pdflatex() -> Result:
    """Build one real PDF through the real binary and the real parse gate.

    Through render_pdf rather than a bare subprocess call, so this exercises the
    timeout handling, the process-tree kill and the aux-file cleanup as well —
    the parts that only ever run on a real build.
    """
    import tempfile
    from pathlib import Path

    from backend.documents.build import DocumentBuildError, render_pdf

    source = (
        r"\documentclass[11pt,a4paper]{article}"
        r"\usepackage[T1]{fontenc}\usepackage{lmodern}"
        r"\begin{document}Smoke test: efficient office affluent.\end{document}"
    )
    with tempfile.TemporaryDirectory() as directory:
        try:
            path = render_pdf(source, Path(directory), "smoke")
        except DocumentBuildError as exc:
            return Result("pdflatex", "fail", str(exc)[:200])

        size = path.stat().st_size
        # render_pdf keeps the .tex next to the PDF on purpose — for a real
        # build it is how you see what was typeset. A smoke test has no such
        # reader, so it clears its own source and the leftovers list below then
        # means what it says: aux files render_pdf failed to clean up.
        path.with_suffix(".tex").unlink(missing_ok=True)
        leftovers = sorted(p.name for p in Path(directory).glob("smoke.*") if p != path)

    return Result(
        "pdflatex",
        "pass",
        f"built a {size:,}-byte PDF at {settings.pdflatex_path}",
        facts={"bytes": size, "aux_files_left": leftovers},
    )


def check_browser() -> Result:
    """Launch the real headful Chrome, load a page, close it.

    about:blank rather than a real site: this is checking that Playwright can
    drive the configured channel, not that the internet works, and loading a job
    board from a smoke test would put an unexplained hit in someone's logs.

    Uses launch_context, so it exercises the real persistent profile — the same
    one the apply layer uses, and the thing that actually differs from a default
    Playwright launch.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return _skip("browser", "playwright is not installed — uv sync --all-groups")

    from backend.apply.session import launch_context

    context = None
    playwright = None
    try:
        playwright = sync_playwright().start()
        context = launch_context(playwright)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("about:blank", wait_until="domcontentloaded")
        title = page.title()
    except Exception as exc:  # noqa: BLE001 - the message is the whole result
        text = str(exc)
        if "executable doesn" in text.lower() or "channel" in text.lower():
            return _skip(
                "browser",
                f"the {settings.browser_channel!r} channel is not installed — "
                f"uv run playwright install {settings.browser_channel}",
            )
        return Result("browser", "fail", text[:200])
    finally:
        if context is not None:
            try:
                context.close()
            except Exception as exc:  # noqa: BLE001 - a dead context is normal
                log.debug("smoke_context_close_failed", error=str(exc)[:120])
        if playwright is not None:
            try:
                playwright.stop()
            except Exception as exc:  # noqa: BLE001
                log.debug("smoke_playwright_stop_failed", error=str(exc)[:120])

    return Result(
        "browser",
        "pass",
        f"launched {settings.browser_channel}, headless={settings.browser_headless}",
        facts={"title": title, "profile": str(settings.browser_profile_dir)},
    )


def check_session_cookies() -> Result:
    """Read the real cookie jar and name which sites have a session.

    Reports rather than checks: whether those sessions are LIVE needs the daily
    check, which loads a page per site. This answers the prior question — has
    anything ever signed in — which is what "0 session checks" cannot tell you
    apart from "the check has not run".
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return _skip("session cookies", "playwright is not installed")

    from backend.apply.session import launch_context
    from backend.sessions import _site_for_domain, sites_with_cookies

    context = None
    playwright = None
    try:
        playwright = sync_playwright().start()
        context = launch_context(playwright)
        counts = sites_with_cookies(context)
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        if "executable doesn" in text.lower() or "channel" in text.lower():
            return _skip(
                "session cookies",
                f"the {settings.browser_channel!r} channel is not installed",
            )
        return Result("session cookies", "fail", text[:200])
    finally:
        if context is not None:
            try:
                context.close()
            except Exception as exc:  # noqa: BLE001 - a dead context is normal
                log.debug("smoke_context_close_failed", error=str(exc)[:120])
        if playwright is not None:
            try:
                playwright.stop()
            except Exception as exc:  # noqa: BLE001
                log.debug("smoke_playwright_stop_failed", error=str(exc)[:120])

    if not counts:
        return _skip(
            "session cookies",
            "the browser profile holds no cookies — sign in with "
            "`uv run python -m backend.apply.session login --platform seek`",
        )

    by_site: dict[str, int] = {}
    for domain, count in counts.items():
        site = _site_for_domain(domain)
        by_site[site] = by_site.get(site, 0) + count

    named = ", ".join(f"{site} ({count})" for site, count in sorted(by_site.items()))
    return Result(
        "session cookies",
        "pass",
        f"{len(by_site)} sites with cookies: {named}",
        facts={"sites": by_site},
    )


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def _key_for(model: str) -> str | None:
    from backend.llm.client import _api_key_for

    return _api_key_for(model)


def _last_spend(purpose: str) -> dict[str, Any]:
    """What the provider actually charged for the call just made.

    Returns an empty dict rather than raising when nothing was recorded: a
    provider that reports no usage is a smaller problem than a smoke test that
    crashes after a successful call.
    """
    from sqlmodel import select

    from backend.db import session_scope
    from backend.models import LLMSpend

    try:
        with session_scope() as session:
            row = session.exec(
                select(LLMSpend)
                .where(LLMSpend.purpose == purpose)
                .order_by(LLMSpend.id.desc())  # type: ignore[union-attr]
            ).first()
            if row is None:
                return {}
            return {
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "cost_usd": row.cost_usd,
            }
    except Exception as exc:  # noqa: BLE001
        log.debug("spend_lookup_failed", purpose=purpose, error=str(exc)[:120])
        return {}


CHECKS: dict[str, Callable[[], Result]] = {
    "telegram": check_telegram,
    "gemini": check_llm_completion,
    "embedding": check_llm_embedding,
    "pdflatex": check_pdflatex,
    "browser": check_browser,
    "cookies": check_session_cookies,
}
"""Every check, by the name ``--only`` accepts.

"gemini" rather than "llm completion" because that is what the user types, and
the completion model is the one they set GEMINI_API_KEY for.
"""


def run_smoke(only: list[str] | None = None) -> list[Result]:
    """Run the checks and return their results. Never raises.

    A check that raises unexpectedly becomes a failed result rather than taking
    the run down — the whole value here is seeing every transport at once, and
    one broken credential must not hide the state of the other five.
    """
    selected = [name for name in CHECKS if not only or name in only]
    results: list[Result] = []

    for name in selected:
        started = time.monotonic()
        try:
            result = CHECKS[name]()
        except Exception as exc:
            log.exception("smoke_check_raised", check=name)
            result = Result(name, "fail", f"{type(exc).__name__}: {exc}"[:200])
        result.seconds = time.monotonic() - started
        results.append(result)

        log.info(
            "smoke_check",
            check=result.name,
            status=result.status,
            detail=result.detail,
            seconds=round(result.seconds, 2),
            **result.facts,
        )

    return results


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

_MARK = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}


def render(results: list[Result]) -> str:
    """The table. Skips are listed with their reason, not hidden."""
    width = max((len(r.name) for r in results), default=10)
    lines = ["", "SMOKE TEST", "=" * 72]

    for result in results:
        lines.append(
            f"  [{_MARK[result.status]}] {result.name:<{width}}  {result.detail}"
        )
        for key, value in result.facts.items():
            if key == "cost_usd":
                lines.append(f"         {'':<{width}}  {key}: ${value:.6f}")
            else:
                lines.append(f"         {'':<{width}}  {key}: {value}")

    passed = sum(1 for r in results if r.status == "pass")
    failed = sum(1 for r in results if r.status == "fail")
    skipped = sum(1 for r in results if r.status == "skip")
    total_cost = sum(float(r.facts.get("cost_usd", 0.0)) for r in results)

    lines.append("")
    lines.append(f"{passed} passed, {failed} failed, {skipped} skipped")
    if total_cost:
        lines.append(f"spent ${total_cost:.6f} on this run")
    if skipped:
        lines.append("Skips are missing credentials, not failures — see each reason.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI wiring
    parser = argparse.ArgumentParser(prog="python -m backend.smoke")
    parser.add_argument(
        "--only",
        default="",
        help=f"comma-separated subset of: {', '.join(CHECKS)}",
    )
    args = parser.parse_args(argv)
    configure_logging()

    only = [name.strip() for name in args.only.split(",") if name.strip()]
    unknown = [name for name in only if name not in CHECKS]
    if unknown:
        parser.error(f"unknown check(s): {unknown}; known: {sorted(CHECKS)}")

    results = run_smoke(only or None)
    print(render(results))

    # Skips are not failures: the command must be runnable at any stage of
    # setup, and exiting non-zero for an unconfigured integration would make it
    # useless in exactly the situation it was built for.
    return 1 if any(r.status == "fail" for r in results) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
