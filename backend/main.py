"""The FastAPI application shell: lifespan, CORS, liveness, and a router hook.

This module owns process startup and nothing else. Feature endpoints belong to
their own branch's package and are mounted here through :func:`_register_routers`,
which imports them defensively so the app still boots on a checkout where those
blocks do not exist yet.

**Security — the browser profile is never web-reachable.** ``data/browser_profile/``
holds a live, authenticated LinkedIn session (Claude.md, Windows section). Anything
that could hand those cookies to a page — a ``StaticFiles`` mount, a download route,
a "show me the file" helper — is a session-theft vector on a machine that also runs
a browser. No route may serve a path inside ``settings.browser_profile_dir``, and
:func:`_assert_no_browser_profile_exposure` refuses to build the app if one does.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, func, select
from starlette.staticfiles import StaticFiles

from backend import __version__
from backend.config import settings
from backend.db import get_session, init_db, sqlite_path
from backend.llm.client import budget_status
from backend.logging_setup import configure_logging, get_logger
from backend.models import Application, Job, utcnow

__all__ = ["app", "create_app"]

log = get_logger(__name__)

SessionDep = Annotated[Session, Depends(get_session)]

# Where each later branch mounts its endpoints. The module must expose a module
# level ``router: APIRouter``; the prefix and tags are the router's own business.
# Keeping one router per package is what stops parallel branches from all editing
# the same file (Claude.md, Git section).
_FEATURE_ROUTERS: tuple[tuple[str, str], ...] = (
    ("backend.discovery.routes", "feat/discovery"),
    ("backend.scoring.routes", "feat/discovery"),
    ("backend.documents.routes", "feat/documents"),
    ("backend.apply.routes", "feat/apply"),
    ("backend.api", "feat/frontend"),
)


# --------------------------------------------------------------------- schemas
# Pydantic here, SQLModel in models.py — an API response is a contract with the
# dashboard, not a row (Claude.md).


class HealthResponse(BaseModel):
    """Liveness payload. ``database`` reflects a real query, never an assumption."""

    status: str
    version: str
    allow_live_submit: bool
    database: str
    time: datetime


class MetaStatus(BaseModel):
    """The dashboard's at-a-glance numbers. Later blocks extend this; keep it small."""

    jobs: int
    applications_today: int
    llm_budget: dict[str, Any] = Field(
        description="This UTC month's spend picture, from llm.budget_status()."
    )


# --------------------------------------------------------------------- helpers


def _local_day_start_utc() -> datetime:
    """Midnight of the user's *local* day, expressed in UTC for a DB comparison.

    Adelaide runs UTC+9:30/+10:30, so a plain UTC-day cutoff would count up to
    ten and a half hours of yesterday's applications as today's. That is a
    cosmetic error on a dashboard and a correctness error the moment the apply
    guardrails read the same number to enforce a daily cap.
    """
    try:
        zone = ZoneInfo(settings.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        # A typo in TIMEZONE must not take the whole status endpoint down, but it
        # must not pass unnoticed either.
        log.warning("timezone_unresolved", timezone=settings.timezone, fallback="UTC")
        zone = UTC
    local_midnight = datetime.now(zone).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return local_midnight.astimezone(UTC)


def _static_directories(
    nodes: Iterable[Any], seen: set[int] | None = None
) -> list[Path]:
    """Every filesystem directory the app is configured to serve, however nested.

    Route objects hide their children under several different names — a ``Mount``
    holds the sub-application under ``app`` (or ``_base_app`` once middleware
    wraps it), FastAPI's ``include_router`` stores the original ``APIRouter``
    behind a wrapper object, and routers expose ``routes``. A ``StaticFiles``
    reachable through *any* of them is served, so all of them are walked: a check
    that only looked at top-level mounts would pass an app that is wide open.
    """
    seen = set() if seen is None else seen
    directories: list[Path] = []

    for node in nodes:
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))

        if isinstance(node, StaticFiles):
            directories.extend(Path(d) for d in node.all_directories)
            continue

        for attr in ("app", "_base_app", "original_router", "routes"):
            child = getattr(node, attr, None)
            if child is None:
                continue
            branch = child if isinstance(child, list | tuple) else [child]
            directories.extend(_static_directories(branch, seen))

    return directories


def _assert_no_browser_profile_exposure(app: FastAPI) -> None:
    """Refuse to build an app that could serve the authenticated browser profile.

    Checked in both directions on purpose. Serving the profile directory itself
    is the obvious mistake; the likely one is feat/frontend mounting ``data/`` to
    get at built documents and dragging ``data/browser_profile/`` along with it.
    """
    assert settings.browser_profile_dir is not None  # set by the config validator
    forbidden = settings.browser_profile_dir.resolve()
    for directory in _static_directories(app.routes):
        served = directory.resolve()
        if served.is_relative_to(forbidden) or forbidden.is_relative_to(served):
            log.error(
                "browser_profile_would_be_served",
                served=str(served),
                browser_profile_dir=str(forbidden),
            )
            raise RuntimeError(
                f"Route serves {served}, which exposes the authenticated browser "
                f"session in {forbidden}. Serve a narrower directory."
            )


def _register_routers(app: FastAPI) -> None:
    """Mount the feature routers that exist in this checkout.

    Absent modules are expected — blocks land on their own branches — so a
    missing router is a debug line, not a failure. An ImportError raised *inside*
    a module that does exist is a real bug and is re-raised: swallowing it would
    silently boot an app with endpoints missing (Claude.md hard rule 9).
    """
    for module_path, branch in _FEATURE_ROUTERS:
        try:
            module = import_module(module_path)
        except ModuleNotFoundError as exc:
            if exc.name == module_path:
                log.debug("router_not_present", module=module_path, branch=branch)
                continue
            raise
        router = getattr(module, "router", None)
        if router is None:
            raise RuntimeError(
                f"{module_path} exists but exposes no module-level "
                f"`router: APIRouter`, so its endpoints would never be served."
            )
        app.include_router(router)
        log.info("router_registered", module=module_path, branch=branch)


# ---------------------------------------------------------------------- routes

meta_router = APIRouter(tags=["meta"])


@meta_router.get("/health", response_model=HealthResponse)
def health(session: SessionDep) -> HealthResponse | JSONResponse:
    """Liveness plus a real round-trip to the database.

    The point of this endpoint is to fail when the DB is gone, so it issues an
    actual ``SELECT 1`` rather than reporting on the engine object's existence —
    SQLAlchemy connects lazily and a healthy-looking engine proves nothing.
    """
    payload = HealthResponse(
        status="ok",
        version=__version__,
        allow_live_submit=settings.allow_live_submit,
        database="connected",
        time=utcnow(),
    )
    try:
        session.execute(text("SELECT 1")).scalar_one()
    except SQLAlchemyError:
        log.exception("health_database_unreachable", database_url=settings.database_url)
        payload.status = "degraded"
        payload.database = "unreachable"
        return JSONResponse(status_code=503, content=payload.model_dump(mode="json"))
    return payload


@meta_router.get("/api/meta/status", response_model=MetaStatus)
def meta_status(session: SessionDep) -> MetaStatus:
    """Headline counters for the dashboard."""
    try:
        jobs = session.exec(select(func.count()).select_from(Job)).one()
        applications_today = session.exec(
            select(func.count())
            .select_from(Application)
            .where(Application.applied_at >= _local_day_start_utc())
        ).one()
        # Reused, never recomputed: the cap lives in llm/client.py with the
        # spend table it reads.
        budget = budget_status()
    except SQLAlchemyError as exc:
        log.exception("meta_status_failed")
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return MetaStatus(
        jobs=int(jobs),
        applications_today=int(applications_today),
        llm_budget=budget,
    )


# ------------------------------------------------------------------- lifecycle


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown. Replaces the deprecated ``on_event`` hooks.

    Underscored parameter because it is unused and because naming it ``app``
    would shadow the module-level instance inside this function.
    """
    # Idempotent: importing any backend module has already configured the
    # pipeline. Called anyway so the API is not relying on that side effect.
    configure_logging()
    settings.ensure_directories()
    init_db()

    database_path = sqlite_path()
    log.info(
        "app_startup",
        version=__version__,
        app_env=settings.app_env,
        allow_live_submit=settings.allow_live_submit,
        database=str(database_path.resolve())
        if database_path is not None
        else settings.database_url,
        frontend_origin=settings.frontend_origin,
    )

    if settings.allow_live_submit:
        # The one switch that turns this from a simulator into something that
        # acts as the user. It has to be impossible to miss in the log.
        banner = "!" * 78
        log.warning("live_submit_enabled", banner=banner)
        log.warning(
            "LIVE_SUBMIT_IS_ON",
            detail=(
                "ALLOW_LIVE_SUBMIT=true — real applications will be submitted to "
                "real employers as you. Set it back to false to run dry."
            ),
        )
        log.warning("live_submit_enabled", banner=banner)

    yield

    log.info("app_shutdown")


def create_app() -> FastAPI:
    """Build the application. A factory so tests can hold their own instance."""
    app = FastAPI(
        title="JobSeekr",
        version=__version__,
        summary="Local job discovery, scoring and auto-application.",
        lifespan=_lifespan,
    )

    # The dashboard is a Vite dev server on another origin, and it sends cookies.
    # A single explicit origin, never "*": credentialed wildcard CORS is invalid
    # and this app has no auth to fall back on.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(meta_router)
    _register_routers(app)

    # Last, so it sees every mount any block added above. Fails at import time
    # rather than at request time — see the module docstring.
    _assert_no_browser_profile_exposure(app)
    return app


app = create_app()
