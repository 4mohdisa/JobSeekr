"""API behaviour, with the security assertions first.

The document-serving route is the one that matters most: the machine running
this also holds a live authenticated browser profile, so a path-traversal bug
there leaks session cookies rather than a resume.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from backend.config import settings
from backend.db import get_session
from backend.main import app
from backend.models import (
    AnswerBank,
    AnswerType,
    Application,
    ApplicationOutcome,
    Campaign,
    Document,
    DocumentKind,
    GrayZoneAction,
    Job,
    JobStatus,
    MatchType,
    Profile,
    ResponseStatus,
    Score,
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    # StaticPool is required, not cosmetic: an in-memory SQLite database lives
    # inside a single connection, and TestClient runs the app on a different
    # thread from the fixture. Without it the app gets a fresh, empty database
    # and every request fails with "no such table".
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    documents_dir = tmp_path / "documents"
    (documents_dir / "job_1").mkdir(parents=True, exist_ok=True)
    real_pdf = documents_dir / "job_1" / "resume.pdf"
    real_pdf.write_bytes(b"%PDF-1.4 fake but real enough\n%%EOF")

    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF-1.4 should never be served\n%%EOF")

    with Session(engine) as session:
        session.add(Profile(version=1, identity={"name": "Jordan"}, skills=["Python"]))
        campaign = Campaign(
            id=1,
            name="analytics",
            search_terms=["dev"],
            locations=["Adelaide SA"],
            score_floor=60.0,
            score_auto_apply=80.0,
            gray_zone_action=GrayZoneAction.QUEUE,
        )
        session.add(campaign)
        session.flush()

        session.add(
            Job(
                id=1,
                source="seek",
                source_job_id="1",
                url="https://example.com/1",
                title="Developer",
                company="Acme",
                location="Adelaide SA",
                dedupe_hash="h1",
                campaign_id=1,
                status=JobStatus.MANUAL_QUEUE,
            )
        )
        session.flush()

        session.add(
            Document(
                id=1,
                job_id=1,
                kind=DocumentKind.RESUME,
                path=str(real_pdf),
                sha256="abc",
                parse_check_passed=True,
                parse_report={"cover_letter_text": "Dear Hiring Team"},
            )
        )
        session.add(
            Document(
                id=2,
                job_id=1,
                kind=DocumentKind.COVER_LETTER,
                path=str(outside),  # deliberately outside the documents root
                sha256="def",
                parse_check_passed=True,
                parse_report={"cover_letter_text": "Dear Hiring Team"},
            )
        )
        session.add(Score(job_id=1, profile_version=1, rubric_version=1, final=91.0))
        session.add(
            AnswerBank(
                id=1,
                question_pattern="Do you have full working rights in Australia?",
                match_type=MatchType.FUZZY,
                answer_value="Yes",
                answer_type=AnswerType.BOOLEAN,
            )
        )
        session.add(
            AnswerBank(
                id=2,
                question_pattern="What is your notice period?",
                match_type=MatchType.FUZZY,
                answer_value="",
                answer_type=AnswerType.TEXT,
            )
        )
        session.commit()

        app.dependency_overrides[get_session] = lambda: session
        yield TestClient(app)
        app.dependency_overrides.clear()


# =========================================================================
# Security
# =========================================================================


def test_a_document_outside_the_documents_root_is_never_served(client, tmp_path):
    """A traversal here would leak the browser profile, not a resume."""
    monkey_settings_documents = settings.documents_dir
    assert monkey_settings_documents.is_relative_to(tmp_path)

    response = client.get("/api/documents/2/file")
    assert response.status_code == 404, "served a file from outside the documents tree"


def test_a_document_inside_the_root_is_served(client):
    response = client.get("/api/documents/1/file")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"


def test_a_missing_document_id_is_404(client):
    assert client.get("/api/documents/999/file").status_code == 404


def test_allow_live_submit_is_read_only_over_the_api(client):
    """The one control that must require editing .env on the machine itself."""
    before = client.get("/api/settings").json()["allow_live_submit"]
    assert before is False

    response = client.put("/api/settings", json={"allow_live_submit": True})
    assert response.status_code in (200, 422)

    after = client.get("/api/settings").json()["allow_live_submit"]
    assert after is False, "the API changed allow_live_submit"
    assert settings.allow_live_submit is False


def test_the_settings_schema_does_not_expose_the_switch():
    from backend.api.schemas import SettingsIn

    assert "allow_live_submit" not in SettingsIn.model_fields


# =========================================================================
# Control — the emergency brake
# =========================================================================


def test_stop_creates_the_file_the_guardrails_read(client):
    assert client.get("/api/control").json()["stopped"] is False

    response = client.post("/api/control/stop", params={"reason": "testing"})
    assert response.json()["stopped"] is True
    assert settings.stop_file.exists()
    assert "testing" in settings.stop_file.read_text(encoding="utf-8")

    client.post("/api/control/resume")
    assert settings.stop_file.exists() is False


# =========================================================================
# Profile versioning
# =========================================================================


def test_saving_the_profile_creates_a_new_version(client):
    first = client.get("/api/profile").json()
    assert first["version"] == 1

    response = client.put(
        "/api/profile",
        json={"identity": {"name": "Jordan Fitzgerald"}, "skills": ["Python", "SQL"]},
    )
    assert response.status_code == 200
    assert response.json()["version"] == 2

    versions = client.get("/api/profile/versions").json()
    assert [v["version"] for v in versions] == [2, 1], "history is preserved, not overwritten"


# =========================================================================
# Campaigns
# =========================================================================


def test_pause_and_resume_a_campaign(client):
    assert client.post("/api/campaigns/1/pause").json()["active"] is False
    assert client.post("/api/campaigns/1/resume").json()["active"] is True


def test_editing_the_rubric_bumps_its_version(client):
    """Scores from different rubrics are not comparable."""
    campaign = client.get("/api/campaigns/1").json()
    before = campaign["rubric_version"]

    campaign["rubric"] = {"criteria": [{"key": "skills", "weight": 100, "description": "x"}]}
    response = client.put("/api/campaigns/1", json=campaign)

    assert response.json()["rubric_version"] == before + 1


def test_editing_something_else_does_not_bump_the_rubric_version(client):
    campaign = client.get("/api/campaigns/1").json()
    before = campaign["rubric_version"]
    campaign["name"] = "renamed"
    assert client.put("/api/campaigns/1", json=campaign).json()["rubric_version"] == before


# =========================================================================
# Answer bank
# =========================================================================


def test_unanswered_filter_surfaces_the_blanks(client):
    everything = client.get("/api/answers").json()
    blanks = client.get("/api/answers", params={"unanswered_only": True}).json()

    assert len(everything) == 2
    assert len(blanks) == 1
    assert blanks[0]["question_pattern"] == "What is your notice period?"


def test_bulk_answer_update(client):
    response = client.post("/api/answers/bulk", json={"2": "4 weeks"})
    assert response.status_code == 200
    assert client.get("/api/answers", params={"unanswered_only": True}).json() == []


# =========================================================================
# Templates
# =========================================================================


def test_template_preview_reports_a_typo(client):
    response = client.post(
        "/api/templates/preview", params={"body": r"Dear \VAR{job.compnay}"}
    )
    assert response.status_code == 200
    issues = response.json()["issues"]
    assert any("compnay" in issue["placeholder"] for issue in issues)


def test_template_preview_renders_against_a_real_job(client):
    response = client.post(
        "/api/templates/preview", params={"body": r"Role: \VAR{job.title} at \VAR{job.company}"}
    )
    body = response.json()
    assert body["job_id"] == 1
    assert "Developer" in body["rendered"]
    assert "Acme" in body["rendered"]


def test_template_preview_exposes_the_placeholder_vocabulary(client):
    """The editor's autocomplete comes from here, so it cannot drift."""
    body = client.post("/api/templates/preview", params={"body": "x"}).json()
    assert "job" in body["known_placeholders"]
    assert "company" in body["known_placeholders"]["job"]


def test_editing_a_template_body_bumps_its_version(client):
    created = client.post(
        "/api/templates",
        json={"kind": "resume", "name": "t", "body": "one", "is_default": False},
    ).json()
    assert created["version"] == 1

    updated = client.put(
        f"/api/templates/{created['id']}",
        json={"kind": "resume", "name": "t", "body": "two", "is_default": False},
    ).json()
    assert updated["version"] == 2


# =========================================================================
# Jobs and queue
# =========================================================================


def test_jobs_list_includes_the_score(client):
    page = client.get("/api/jobs").json()
    assert page["total"] == 1
    assert page["items"][0]["score"] == 91.0


def test_job_detail_includes_score_and_documents(client):
    detail = client.get("/api/jobs/1").json()
    assert detail["score_detail"]["final"] == 91.0
    assert len(detail["documents"]) == 2


def test_the_queue_card_carries_everything_needed_to_apply_by_hand(client):
    """One call, because a second round trip is what makes manual feel slow."""
    cards = client.get("/api/queue").json()
    assert len(cards) == 1

    card = cards[0]
    assert card["apply_url"] == "https://example.com/1"
    assert card["cover_letter_text"], "the letter must be on the card, not a second fetch"
    assert card["resume_document_id"] == 1
    assert any(a["question"].startswith("Do you have full") for a in card["answers"])
    assert "What is your notice period?" in card["unanswered_questions"]


def test_queue_done_records_an_application_once(client):
    assert client.post("/api/queue/1/done").json()["status"] == "applied"
    # A second call must not create a second application.
    client.post("/api/queue/1/done")
    assert client.get("/api/applications").json()["total"] == 1


# =========================================================================
# Analytics — the honesty requirement
# =========================================================================


def _add_applications(client, count: int, status: ResponseStatus) -> None:
    session = app.dependency_overrides[get_session]()
    for index in range(count):
        job_id = 100 + index
        session.add(
            Job(
                id=job_id,
                source="seek",
                source_job_id=f"s{job_id}",
                url=f"https://example.com/{job_id}",
                title="Developer",
                company="Acme",
                dedupe_hash=f"h{job_id}",
                campaign_id=1,
            )
        )
        session.flush()
        session.add(
            Application(
                job_id=job_id,
                outcome=ApplicationOutcome.SUBMITTED,
                platform="seek",
                response_status=status,
            )
        )
    session.commit()


def test_a_tiny_sample_reports_no_rate_at_all(client):
    """A 100% interview rate off one application is a wrong number."""
    _add_applications(client, 1, ResponseStatus.INTERVIEW_REQUEST)

    analytics = client.get("/api/analytics").json()
    platform = next(b for b in analytics["by_platform"] if b["key"] == "seek")

    assert platform["applied"] == 1
    assert platform["sufficient_data"] is False
    assert platform["interview_rate"] is None
    assert platform["any_reply_rate"] is None


def test_a_sufficient_sample_reports_both_rates(client):
    minimum = client.get("/api/analytics").json()["minimum_sample"]
    _add_applications(client, minimum, ResponseStatus.INTERVIEW_REQUEST)

    analytics = client.get("/api/analytics").json()
    platform = next(b for b in analytics["by_platform"] if b["key"] == "seek")

    assert platform["sufficient_data"] is True
    assert platform["interview_rate"] == 1.0
    assert platform["any_reply_rate"] == 1.0


def test_the_funnel_is_reported(client):
    _add_applications(client, 3, ResponseStatus.ACKNOWLEDGED)
    stages = {s["stage"]: s["count"] for s in client.get("/api/analytics").json()["funnel"]}
    assert stages["applied"] == 3
    assert stages["acknowledged"] == 3
    assert stages["interview"] == 0


# =========================================================================
# Applications
# =========================================================================


def test_csv_export_streams_the_history(client):
    _add_applications(client, 2, ResponseStatus.NONE)
    response = client.get("/api/applications/export.csv")

    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    lines = response.text.strip().splitlines()
    assert lines[0].startswith("applied_at,company,title")
    assert len(lines) == 3  # header plus two rows


def test_patching_notes_and_response_status(client):
    _add_applications(client, 1, ResponseStatus.NONE)
    application = client.get("/api/applications").json()["items"][0]

    response = client.patch(
        f"/api/applications/{application['id']}",
        json={"user_notes": "phone screen booked", "response_status": "interview_request"},
    )
    body = response.json()
    assert body["user_notes"] == "phone screen booked"
    assert body["response_status"] == "interview_request"
    assert body["response_at"] is not None
