"""Dedupe correctness, especially the case that must NOT match.

A missed duplicate wastes one score. A false duplicate silently deletes a real
job the user would have applied to, and nothing downstream can recover it — so
the negative tests here matter more than the positive ones.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine

from backend.discovery.dedupe import dedupe_hash, find_duplicate
from backend.models import Job


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def make_job(**kwargs) -> Job:
    base = {
        "source": "seek",
        "source_job_id": "1",
        "url": "https://example.com/1",
        "title": "Senior Python Developer",
        "company": "Acme Pty Ltd",
        "location": "Adelaide SA 5000",
    }
    base.update(kwargs)
    base["dedupe_hash"] = dedupe_hash(base["company"], base["title"], base["location"])
    return Job(**base)


# ---------------------------------------------------------------- the hash


def test_hash_is_stable_across_spelling_differences():
    a = dedupe_hash("Acme Pty Ltd", "Senior Python Developer", "Adelaide SA 5000")
    b = dedupe_hash("ACME", "senior python developer", "Adelaide, South Australia, AU")
    assert a == b


def test_hash_separates_genuinely_different_roles():
    a = dedupe_hash("Acme", "Senior Python Developer", "Adelaide SA")
    b = dedupe_hash("Acme", "Junior Python Developer", "Adelaide SA")
    assert a != b


def test_hash_is_deterministic():
    args = ("Acme", "Developer", "Adelaide SA")
    assert dedupe_hash(*args) == dedupe_hash(*args)


# ------------------------------------------------------------ exact matching


def test_cross_posted_ad_is_found_by_hash(session):
    session.add(make_job(source="seek", source_job_id="1"))
    session.commit()

    incoming = make_job(source="linkedin", source_job_id="99", company="ACME")
    assert find_duplicate(session, incoming) is not None


# ------------------------------------------------------------ fuzzy matching


def test_near_identical_title_at_same_company_is_a_duplicate(session):
    session.add(make_job(title="Senior Python Developer"))
    session.commit()

    incoming = make_job(
        source="linkedin",
        source_job_id="99",
        title="Senior Python Developer - Adelaide",
    )
    assert find_duplicate(session, incoming) is not None


def test_same_title_at_a_different_company_is_NOT_a_duplicate(session):
    """The negative case the company scope exists for.

    Half the market advertises "Software Engineer". Matching those across
    employers would collapse unrelated jobs and cost real applications.
    """
    session.add(make_job(company="Acme Pty Ltd", title="Software Engineer"))
    session.commit()

    incoming = make_job(
        source="linkedin",
        source_job_id="99",
        company="Globex Pty Ltd",
        title="Software Engineer",
    )
    assert find_duplicate(session, incoming) is None


def test_unrelated_title_at_same_company_is_not_a_duplicate(session):
    session.add(make_job(title="Senior Python Developer"))
    session.commit()

    incoming = make_job(
        source="linkedin", source_job_id="99", title="Warehouse Storeperson"
    )
    assert find_duplicate(session, incoming) is None


def test_a_job_is_not_its_own_duplicate(session):
    job = make_job()
    session.add(job)
    session.commit()
    assert find_duplicate(session, job) is None


def test_location_suffix_is_stripped_but_a_role_qualifier_is_not(session):
    """The line between "same ad, location appended" and "different role"."""
    session.add(make_job(title="Software Engineer - Backend"))
    session.commit()

    # A different specialisation at the same employer must survive.
    incoming = make_job(
        source="linkedin", source_job_id="99", title="Software Engineer - Frontend"
    )
    assert find_duplicate(session, incoming) is None
