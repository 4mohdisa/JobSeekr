"""Test wiring: redirect the whole data tree at a throwaway directory.

``backend.config`` reads the environment once at import and caches it, and
``backend.db`` builds its engine from that cached object at import. So the
environment has to be rewritten *before the first backend import*, which is why
this happens at module scope in conftest rather than in a fixture — pytest
imports conftest before it imports any test module.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

# Safe above the environment rewrite below: fastapi imports nothing from backend.
from fastapi.testclient import TestClient

_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="jobseekr-tests-"))

os.environ["DATA_DIR"] = str(_TEST_DATA_DIR)
os.environ["DATABASE_URL"] = f"sqlite:///{(_TEST_DATA_DIR / 'test.db').as_posix()}"
# Redirected with DATA_DIR, not left to the validator's default. The default
# only anchors the profile under data_dir when BROWSER_PROFILE_DIR is unset —
# and .env.example ships it set, so a developer with a real .env got a profile
# directory pointing at the repo while data_dir pointed at this temp tree. The
# two then no longer overlap, and the test that proves the app refuses to serve
# the authenticated browser session passed only because there was nothing left
# to catch. Pinning it here keeps the production relationship (profile lives
# inside data_dir) that the guard actually exists to police.
os.environ["BROWSER_PROFILE_DIR"] = str(_TEST_DATA_DIR / "browser_profile")
# Removed, never assigned: the suite must exercise the real default. A developer
# who has genuinely turned live submit on in their shell or .env will see these
# tests go red, which is the correct and deliberately loud outcome.
os.environ.pop("ALLOW_LIVE_SUBMIT", None)


@pytest.fixture(scope="session", autouse=True)
def _test_database() -> Iterator[None]:
    """Create the schema in the throwaway database and tear the whole tree down.

    ``create_all`` rather than ``alembic upgrade head`` on purpose: these tests
    assert on ``backend.models``, and Alembic already has its own check that the
    migration and the models agree (``alembic check``). Running migrations per
    session would only make the suite slower and test the same thing twice.
    """
    from sqlmodel import SQLModel

    import backend.models  # noqa: F401  — importing registers every table
    from backend.db import engine

    SQLModel.metadata.create_all(engine)
    try:
        yield
    finally:
        engine.dispose()
        shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)


@pytest.fixture(scope="session")
def client(_test_database: None) -> Iterator[TestClient]:
    """A TestClient inside the app's lifespan, so startup really runs."""
    from backend.main import app

    with TestClient(app) as test_client:
        yield test_client


def resolved_pdflatex() -> str | None:
    """The pdflatex binary the application would actually run, or None.

    Deliberately not ``shutil.which("pdflatex")``. The build shells out to
    ``settings.pdflatex_path``, and on Windows that is an absolute path into a
    per-user MiKTeX install which does not put itself on PATH. Guarding the
    document tests on a bare PATH lookup meant that on the first real Windows
    machine every LaTeX test skipped while pdflatex was installed, configured
    and working — the suite reported green with the whole document pipeline
    unexercised, which is worse than reporting red.
    """
    from backend.config import settings

    configured = settings.pdflatex_path
    if os.path.isabs(configured):
        return configured if os.path.exists(configured) else None
    return shutil.which(configured)


needs_pdflatex = pytest.mark.skipif(
    resolved_pdflatex() is None,
    reason="pdflatex not found via PDFLATEX_PATH or PATH",
)
