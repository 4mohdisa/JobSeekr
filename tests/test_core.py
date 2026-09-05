"""Block A only: the app boots, the schema is whole, and seeding cannot clobber.

Every test here is hermetic — no network, no real ``data/`` directory, no LLM
provider. Anything that needs a live browser, a real form or a paid API call
belongs to the block that introduces it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, col, create_engine, func, select
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from backend.config import Settings, settings
from backend.db import get_session, session_scope
from backend.main import _assert_no_browser_profile_exposure, app
from backend.models import (
    AnswerBank,
    Application,
    ApplyType,
    CacheEvent,
    Campaign,
    DerivedAnswer,
    Document,
    Fact,
    FailureEvent,
    FormMap,
    Job,
    JobStatus,
    LLMSpend,
    OutboundMessage,
    Preference,
    Profile,
    QuestionEvent,
    Run,
    Score,
    SessionHealth,
    StageTiming,
    Template,
)
from backend.seed import ANSWER_BANK_SEEDS, seed_answer_bank

# The full schema, spelled out rather than derived from the metadata it is
# checking: a model deleted by accident should fail this test, not redefine it.
EXPECTED_TABLES = {
    "answer_bank",
    "application",
    "cache_event",
    "campaign",
    "derived_answer",
    "document",
    "fact",
    "failure_event",
    "form_map",
    "job",
    "llm_spend",
    "outbound_message",
    "preference",
    "profile",
    "question_event",
    "run",
    "score",
    "session_health",
    "stage_timing",
    "template",
}

MODEL_CLASSES = (
    AnswerBank,
    Application,
    CacheEvent,
    Campaign,
    DerivedAnswer,
    Document,
    Fact,
    FailureEvent,
    FormMap,
    Job,
    LLMSpend,
    OutboundMessage,
    Preference,
    Profile,
    QuestionEvent,
    Run,
    Score,
    SessionHealth,
    StageTiming,
    Template,
)


# ------------------------------------------------------------------- the shell


def test_health_is_ok_and_live_submit_is_off(client: TestClient) -> None:
    """/health answers 200, proves the DB round-trip, and reports the safety switch."""
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "connected"
    assert body["allow_live_submit"] is False
    # Must parse, and must carry an explicit UTC offset — the API speaks UTC.
    assert datetime.fromisoformat(body["time"]).utcoffset() is not None


def test_health_is_503_when_the_database_is_unreachable(client: TestClient) -> None:
    """A dead database must fail the health check, not be reported as healthy.

    The engine connects lazily, so an endpoint that only inspected objects would
    happily answer 200 over a missing file. This points the session dependency at
    an unopenable path to prove the ``SELECT 1`` is what decides the answer.
    """
    unopenable = create_engine("sqlite:////nonexistent-dir/jobseekr-missing.db")

    def _broken_session() -> Iterator[Session]:
        with Session(unopenable) as session:
            yield session

    app.dependency_overrides[get_session] = _broken_session
    try:
        response = client.get("/health")
    finally:
        app.dependency_overrides.clear()
        unopenable.dispose()

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["database"] == "unreachable"


def test_meta_status_returns_counters(client: TestClient) -> None:
    """/api/meta/status reads the real tables and reuses the LLM budget helper."""
    response = client.get("/api/meta/status")

    assert response.status_code == 200
    body = response.json()
    assert body["jobs"] >= 0
    assert body["applications_today"] >= 0
    assert body["llm_budget"]["cap_usd"] == settings.llm_monthly_cap_usd
    assert {"spent_usd", "remaining_usd", "exceeded"} <= set(body["llm_budget"])


def test_allow_live_submit_defaults_false_without_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The master switch is off when nothing turns it on. Hard rule 7."""
    monkeypatch.delenv("ALLOW_LIVE_SUBMIT", raising=False)

    # _env_file=None so a developer's own .env cannot influence the default under
    # test; this asserts the value baked into the class, nothing else.
    assert Settings(_env_file=None).allow_live_submit is False


def _app_mounting(directory: Path) -> FastAPI:
    """The obvious shape: a static mount straight onto the app."""
    exposed = FastAPI()
    exposed.mount("/files", StaticFiles(directory=directory), name="files")
    return exposed


def _app_including_router_that_mounts(directory: Path) -> FastAPI:
    """The sneaky shape: the mount arrives inside an included feature router."""
    router = APIRouter(
        routes=[Mount("/files", StaticFiles(directory=directory), name="files")]
    )
    exposed = FastAPI()
    exposed.include_router(router)
    return exposed


@pytest.mark.parametrize(
    "build_app", [_app_mounting, _app_including_router_that_mounts]
)
def test_app_refuses_to_serve_the_browser_profile(
    build_app: Callable[[Path], FastAPI],
) -> None:
    """A route covering the authenticated session must stop the app from building.

    Mounting ``data/`` is the realistic mistake — it looks like "serve the built
    PDFs" and quietly includes ``data/browser_profile/``, which holds live
    LinkedIn cookies.

    Both shapes are checked because FastAPI wraps an included router in a private
    object that does not expose ``routes``. If that internal name ever changes,
    this test fails loudly rather than the guard quietly going blind.
    """
    assert settings.browser_profile_dir is not None
    settings.ensure_directories()

    exposed = build_app(settings.data_dir)

    with pytest.raises(RuntimeError, match="browser session"):
        _assert_no_browser_profile_exposure(exposed)


def test_a_narrower_static_mount_is_allowed() -> None:
    """The guard must not block serving documents — only the browser profile."""
    settings.ensure_directories()

    allowed = FastAPI()
    allowed.mount(
        "/documents", StaticFiles(directory=settings.documents_dir), name="documents"
    )

    _assert_no_browser_profile_exposure(allowed)


# ------------------------------------------------------------------ the schema


def test_every_model_imports_and_registers_its_table() -> None:
    """All 20 tables are on SQLModel.metadata, and nothing extra is."""
    assert len(EXPECTED_TABLES) == 20
    assert {model.__tablename__ for model in MODEL_CLASSES} == EXPECTED_TABLES
    assert set(SQLModel.metadata.tables) == EXPECTED_TABLES


def test_enums_are_stored_by_value() -> None:
    """Status columns persist the lowercase value the rest of the stack uses."""
    assert JobStatus.DISCOVERED.value == "discovered"
    assert ApplyType.EASY_APPLY.value == "easy_apply"


# ------------------------------------------------------------------- the seeds


def _answer_bank_count() -> int:
    with session_scope() as session:
        return int(session.exec(select(func.count()).select_from(AnswerBank)).one())


def test_seed_answer_bank_is_idempotent() -> None:
    """Re-seeding inserts nothing and, crucially, rewrites nothing.

    The second half is the part that matters: ``question_pattern`` is the
    identity, and a verified answer the user gave over Telegram must survive
    every future re-seed. Hard rule 2 — a silently reset answer would put a
    wrong claim on a real application.
    """
    before = _answer_bank_count()
    inserted = seed_answer_bank()
    after_first = _answer_bank_count()

    assert after_first == before + inserted
    assert after_first == len(ANSWER_BANK_SEEDS)

    # Simulate the user answering one question over Telegram.
    verified_at = datetime(2026, 1, 1, tzinfo=UTC)
    with session_scope() as session:
        row = session.exec(select(AnswerBank).limit(1)).one()
        pattern = row.question_pattern
        row.answer_value = "true"
        row.verified_at = verified_at
        session.add(row)

    assert seed_answer_bank() == 0
    assert _answer_bank_count() == after_first

    with session_scope() as session:
        row = session.exec(
            select(AnswerBank).where(AnswerBank.question_pattern == pattern)
        ).one()
        assert row.answer_value == "true"
        # SQLite has no timezone type, so an aware UTC value written by the app
        # reads back as naive UTC wall-clock (models.py). Compare like for like.
        assert row.verified_at == verified_at.replace(tzinfo=None)


def test_seeded_answers_are_blank_and_unverified() -> None:
    """Seeding loads questions, never answers.

    A blank, unverified row is what makes the applier abstain and ask. Filling
    these in with plausible defaults would fabricate facts about the user
    (hard rule 1), so this test exists to fail if someone ever "helpfully" does.
    """
    seed_answer_bank()

    # Columns, not ORM instances: session_scope commits and closes on exit, which
    # expires and detaches anything still attached to it.
    with session_scope() as session:
        unverified = session.exec(
            select(AnswerBank.answer_value, AnswerBank.campaign_id).where(
                col(AnswerBank.verified_at).is_(None)
            )
        ).all()

    assert unverified, "expected the seeded rows to still be unverified"
    for answer_value, campaign_id in unverified:
        assert answer_value == ""
        assert campaign_id is None
