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
