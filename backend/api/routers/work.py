"""Jobs, the manual queue, applications, analytics and document serving.

These are the shaped endpoints — the ones whose payloads are designed around
what the screen has to do rather than around the table layout.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from sqlmodel import Session, col, select

from backend.api.schemas import (
    AnalyticsBucket,
    AnalyticsResponse,
    ApplicationOut,
    ApplicationPatch,
    CopyableAnswer,
    DocumentOut,
    FunnelStage,
    JobDetail,
    JobOut,
    Page,
    QueueCard,
    ScoreOut,
)
from backend.config import settings
from backend.db import get_session
from backend.logging_setup import get_logger
from backend.models import (
    AnswerBank,
    Application,
    ApplicationOutcome,
    Document,
    DocumentKind,
    Job,
    JobStatus,
    ResponseStatus,
    Score,
)

log = get_logger(__name__)

jobs_router = APIRouter(prefix="/jobs", tags=["jobs"])
queue_router = APIRouter(prefix="/queue", tags=["queue"])
applications_router = APIRouter(prefix="/applications", tags=["applications"])
analytics_router = APIRouter(prefix="/analytics", tags=["analytics"])
documents_router = APIRouter(prefix="/documents", tags=["documents"])


def _latest_score(session: Session, job_id: int) -> Score | None:
    return session.exec(
        select(Score).where(Score.job_id == job_id).order_by(Score.scored_at.desc())  # type: ignore[union-attr]
    ).first()


def _job_out(session: Session, job: Job) -> JobOut:
    out = JobOut.model_validate(job, from_attributes=True)
    score = _latest_score(session, job.id)
    out.score = score.final if score else None
    return out


# ==========================================================================
# Jobs
# ==========================================================================


@jobs_router.get("", response_model=Page)
def list_jobs(
    campaign_id: int | None = None,
    status: JobStatus | None = None,
    source: str | None = None,
    company: str | None = None,
    q: str | None = Query(default=None, description="free text over title and company"),
    min_score: float | None = None,
    max_score: float | None = None,
    sort: str = Query(default="score", pattern="^(score|discovered_at|title|company)$"),
    offset: int = 0,
    limit: int = Query(default=50, le=500),
    session: Session = Depends(get_session),
) -> Page:
    query = select(Job)
    if campaign_id is not None:
        query = query.where(Job.campaign_id == campaign_id)
    if status is not None:
        query = query.where(Job.status == status)
    if source is not None:
        query = query.where(Job.source == source)
    if company is not None:
        query = query.where(col(Job.company).ilike(f"%{company}%"))
    if q:
        query = query.where(
            col(Job.title).ilike(f"%{q}%") | col(Job.company).ilike(f"%{q}%")
        )

    rows = [_job_out(session, job) for job in session.exec(query).all()]

    if min_score is not None:
        rows = [r for r in rows if (r.score or 0) >= min_score]
    if max_score is not None:
        rows = [r for r in rows if (r.score or 0) <= max_score]

    reverse = sort in {"score", "discovered_at"}
    rows.sort(key=lambda r: getattr(r, sort) or (0 if sort == "score" else ""), reverse=reverse)

    return Page(items=rows[offset : offset + limit], total=len(rows), offset=offset, limit=limit)


@jobs_router.get("/{job_id}", response_model=JobDetail)
def get_job(job_id: int, session: Session = Depends(get_session)) -> JobDetail:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "no such job")

    detail = JobDetail.model_validate(job, from_attributes=True)
    score = _latest_score(session, job_id)
    detail.score = score.final if score else None
    detail.score_detail = (
        ScoreOut.model_validate(score, from_attributes=True) if score else None
    )
    detail.documents = [
        DocumentOut.model_validate(d, from_attributes=True)
        for d in session.exec(select(Document).where(Document.job_id == job_id)).all()
    ]
    return detail


@jobs_router.post("/{job_id}/status", response_model=JobOut)
def set_job_status(
    job_id: int, status: JobStatus, session: Session = Depends(get_session)
) -> JobOut:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "no such job")
    job.status = status
    session.add(job)
    session.commit()
    session.refresh(job)
    return _job_out(session, job)


# ==========================================================================
# Queue — built for a 90-second manual application
# ==========================================================================

# Questions worth pre-loading onto a queue card. Most Australian application
# forms ask some subset of these, and having them already on screen is most of
# what makes a manual application fast.
QUEUE_ANSWER_LIMIT = 12


@queue_router.get("", response_model=list[QueueCard])
def get_queue(
    campaign_id: int | None = None,
    limit: int = Query(default=25, le=100),
    session: Session = Depends(get_session),
) -> list[QueueCard]:
    """Everything needed to apply by hand, in ONE call per card.

    Deliberately not REST-pure. The target is 90 seconds per application, and a
    second round trip to fetch the cover letter or the answers is exactly the
    kind of pause that makes manual applying feel like work.
    """
    query = select(Job).where(
        col(Job.status).in_([JobStatus.MANUAL_QUEUE, JobStatus.NEEDS_ANSWER, JobStatus.QUEUED])
    )
    if campaign_id is not None:
        query = query.where(Job.campaign_id == campaign_id)

    jobs = list(session.exec(query).all())

    scored = [(job, _latest_score(session, job.id)) for job in jobs]
    scored.sort(key=lambda pair: (pair[1].final if pair[1] else 0) or 0, reverse=True)

    answers = list(
        session.exec(
            select(AnswerBank).where(
                (AnswerBank.campaign_id == campaign_id) | (AnswerBank.campaign_id.is_(None))  # type: ignore[union-attr]
            )
        ).all()
    )

    cards: list[QueueCard] = []
    for job, score in scored[:limit]:
        documents = list(session.exec(select(Document).where(Document.job_id == job.id)).all())

        def document_id(kind: DocumentKind) -> int | None:
            match = next(
                (d for d in documents if d.kind == kind and d.parse_check_passed), None
            )
            return match.id if match else None

        letter = next(
            (d for d in documents if d.kind == DocumentKind.COVER_LETTER), None
        )
        cover_text = (letter.parse_report or {}).get("cover_letter_text", "") if letter else ""

        usable = [a for a in answers if (a.answer_value or "").strip()][:QUEUE_ANSWER_LIMIT]
        blank = [a.question_pattern for a in answers if not (a.answer_value or "").strip()]

        cards.append(
            QueueCard(
                job=_job_out(session, job),
                score=score.final if score else None,
                reasoning=score.reasoning if score else None,
                apply_url=job.url,
                resume_document_id=document_id(DocumentKind.RESUME),
                cover_letter_document_id=document_id(DocumentKind.COVER_LETTER),
                combined_document_id=document_id(DocumentKind.COMBINED),
                cover_letter_text=cover_text,
                answers=[
                    CopyableAnswer(
                        question=a.question_pattern, value=a.answer_value, answered=True
                    )
                    for a in usable
                ],
                unanswered_questions=blank[:QUEUE_ANSWER_LIMIT],
            )
        )
    return cards


@queue_router.post("/{job_id}/done", response_model=JobOut)
def mark_queue_done(job_id: int, session: Session = Depends(get_session)) -> JobOut:
    """Record a manual application. Honours one-application-per-job."""
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "no such job")

    existing = session.exec(select(Application).where(Application.job_id == job_id)).first()
    if existing is None:
        session.add(
            Application(
                job_id=job_id,
                applied_at=datetime.now(UTC),
                outcome=ApplicationOutcome.SUBMITTED,
                platform=job.source,
                user_notes="applied manually from the queue",
            )
        )
    job.status = JobStatus.APPLIED
    session.add(job)
    session.commit()
    session.refresh(job)
    return _job_out(session, job)


@queue_router.post("/{job_id}/skip", response_model=JobOut)
def mark_queue_skipped(job_id: int, session: Session = Depends(get_session)) -> JobOut:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "no such job")
    job.status = JobStatus.SKIPPED
    session.add(job)
    session.commit()
    session.refresh(job)
    return _job_out(session, job)


# ==========================================================================
# Applications
# ==========================================================================


def _application_out(session: Session, application: Application) -> ApplicationOut:
    out = ApplicationOut.model_validate(application, from_attributes=True)
    job = session.get(Job, application.job_id)
    if job is not None:
        out.job_title = job.title
        out.job_company = job.company
        out.job_url = job.url
    return out


@applications_router.get("", response_model=Page)
def list_applications(
    campaign_id: int | None = None,
    platform: str | None = None,
    response_status: ResponseStatus | None = None,
    outcome: ApplicationOutcome | None = None,
    offset: int = 0,
    limit: int = Query(default=50, le=500),
    session: Session = Depends(get_session),
) -> Page:
    query = select(Application).order_by(Application.applied_at.desc())  # type: ignore[union-attr]
    if platform is not None:
        query = query.where(Application.platform == platform)
    if response_status is not None:
        query = query.where(Application.response_status == response_status)
    if outcome is not None:
        query = query.where(Application.outcome == outcome)

    rows = [_application_out(session, a) for a in session.exec(query).all()]
    if campaign_id is not None:
        job_ids = {
            job.id for job in session.exec(select(Job).where(Job.campaign_id == campaign_id)).all()
        }
        rows = [r for r in rows if r.job_id in job_ids]

    return Page(items=rows[offset : offset + limit], total=len(rows), offset=offset, limit=limit)


@applications_router.patch("/{application_id}", response_model=ApplicationOut)
def patch_application(
    application_id: int, payload: ApplicationPatch, session: Session = Depends(get_session)
) -> ApplicationOut:
    row = session.get(Application, application_id)
    if row is None:
        raise HTTPException(404, "no such application")

    for key, value in payload.model_dump(exclude_none=True).items():
        setattr(row, key, value)
    if payload.response_status is not None and row.response_at is None:
        row.response_at = datetime.now(UTC)
    session.add(row)
    session.commit()
    session.refresh(row)
    return _application_out(session, row)


@applications_router.get("/export.csv")
def export_applications(session: Session = Depends(get_session)) -> StreamingResponse:
    """Stream the history as CSV, for the user's own records."""
    rows = session.exec(
        select(Application).order_by(Application.applied_at.desc())  # type: ignore[union-attr]
    ).all()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "applied_at",
            "company",
            "title",
            "platform",
            "outcome",
            "response_status",
            "response_at",
            "failure_reason",
            "attachment_readback",
            "job_url",
            "notes",
        ]
    )
    for application in rows:
        job = session.get(Job, application.job_id)
        writer.writerow(
            [
                application.applied_at.isoformat(),
                job.company if job else "",
                job.title if job else "",
                application.platform or "",
                application.outcome.value,
                application.response_status.value,
                application.response_at.isoformat() if application.response_at else "",
                application.failure_reason or "",
                application.attachment_readback or "",
                job.url if job else "",
                application.user_notes or "",
            ]
        )

    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=applications.csv"},
    )


# ==========================================================================
# Analytics
# ==========================================================================

REPLIED_STATUSES = {
    ResponseStatus.ACKNOWLEDGED,
    ResponseStatus.REJECTED,
    ResponseStatus.INTERVIEW_REQUEST,
    ResponseStatus.RECRUITER_OUTREACH,
}


def _bucket(key: str, applications: list[Application], minimum: int) -> AnalyticsBucket:
    """Summarise one breakdown group, refusing to report a rate off noise."""
    applied = len(applications)
    acknowledged = sum(1 for a in applications if a.response_status == ResponseStatus.ACKNOWLEDGED)
    replied = sum(1 for a in applications if a.response_status in REPLIED_STATUSES)
    interviews = sum(
        1 for a in applications if a.response_status == ResponseStatus.INTERVIEW_REQUEST
    )

    sufficient = applied >= minimum
    return AnalyticsBucket(
        key=key,
        applied=applied,
        acknowledged=acknowledged,
        replied=replied,
        interviews=interviews,
        sufficient_data=sufficient,
        # Rates are None below the threshold ON PURPOSE. A 100% interview rate
        # from one application is not a small sample, it is a wrong number, and
        # rendering it invites a real decision to be made on noise.
        interview_rate=round(interviews / applied, 4) if sufficient and applied else None,
        any_reply_rate=round(replied / applied, 4) if sufficient and applied else None,
    )


@analytics_router.get("", response_model=AnalyticsResponse)
def get_analytics(session: Session = Depends(get_session)) -> AnalyticsResponse:
    minimum = settings.analytics_min_sample
    applications = list(session.exec(select(Application)).all())
    jobs = {job.id: job for job in session.exec(select(Job)).all()}

    by_campaign: dict[str, list[Application]] = {}
    by_platform: dict[str, list[Application]] = {}
    by_decile: dict[str, list[Application]] = {}
    by_rubric: dict[str, list[Application]] = {}

    for application in applications:
        job = jobs.get(application.job_id)

        campaign_key = str(job.campaign_id) if job and job.campaign_id else "none"
        by_campaign.setdefault(campaign_key, []).append(application)

        by_platform.setdefault(application.platform or "unknown", []).append(application)

        score = _latest_score(session, application.job_id)
        if score and score.final is not None:
            decile = f"{int(score.final // 10) * 10}-{int(score.final // 10) * 10 + 9}"
            by_decile.setdefault(decile, []).append(application)
            by_rubric.setdefault(f"v{score.rubric_version}", []).append(application)

    funnel = [
        FunnelStage(stage="applied", count=len(applications)),
        FunnelStage(
            stage="acknowledged",
            count=sum(
                1
                for a in applications
                if a.response_status
                in {
                    ResponseStatus.ACKNOWLEDGED,
                    ResponseStatus.REJECTED,
                    ResponseStatus.INTERVIEW_REQUEST,
                    ResponseStatus.RECRUITER_OUTREACH,
                }
            ),
        ),
        FunnelStage(
            stage="replied",
            count=sum(1 for a in applications if a.response_status in REPLIED_STATUSES),
        ),
        FunnelStage(
            stage="interview",
            count=sum(
                1 for a in applications if a.response_status == ResponseStatus.INTERVIEW_REQUEST
            ),
        ),
    ]

    def buckets(grouped: dict[str, list[Application]]) -> list[AnalyticsBucket]:
        return sorted(
            (_bucket(key, rows, minimum) for key, rows in grouped.items()),
            key=lambda b: b.key,
        )

    return AnalyticsResponse(
        minimum_sample=minimum,
        total_applied=len(applications),
        funnel=funnel,
        by_campaign=buckets(by_campaign),
        by_platform=buckets(by_platform),
        by_score_decile=buckets(by_decile),
        by_rubric_version=buckets(by_rubric),
    )


# ==========================================================================
# Documents
# ==========================================================================


@documents_router.get("/{document_id}", response_model=DocumentOut)
def get_document(document_id: int, session: Session = Depends(get_session)) -> Document:
    document = session.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "no such document")
    return document


@documents_router.get("/{document_id}/file")
def get_document_file(
    document_id: int, session: Session = Depends(get_session)
) -> FileResponse:
    """Serve a built PDF, and nothing else.

    SECURITY. The path is resolved and checked to be inside the documents
    directory before anything is read. The browser profile directory holds a
    live authenticated session (Claude.md), so a traversal bug here would not
    leak a document — it would leak the user's LinkedIn cookies. Anything
    outside the documents tree is a 404, never a 500 and never a file.
    """
    document = session.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "no such document")

    try:
        resolved = Path(document.path).resolve()
        root = settings.documents_dir.resolve()
        resolved.relative_to(root)
    except (ValueError, OSError):
        log.error(
            "document_path_outside_root",
            document_id=document_id,
            path=document.path,
            root=str(settings.documents_dir),
        )
        raise HTTPException(404, "no such document") from None

    if not resolved.is_file():
        raise HTTPException(404, "document file is missing on disk")

    return FileResponse(
        path=str(resolved), media_type="application/pdf", filename=resolved.name
    )


@documents_router.get("/job/{job_id}", response_model=list[DocumentOut])
def list_job_documents(job_id: int, session: Session = Depends(get_session)) -> list[Document]:
    return list(session.exec(select(Document).where(Document.job_id == job_id)).all())


@documents_router.post("/job/{job_id}/build")
def build_job_documents(
    job_id: int, force: bool = False, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Build (or rebuild) the documents for one job, gate included."""
    from backend.documents.build import build_documents

    result = build_documents(session, job_id, force=force)
    session.commit()
    return {
        "ok": result.ok,
        "failure_reason": result.failure_reason,
        "documents": {kind: doc.id for kind, doc in result.documents.items()},
        "reports": {kind: report.model_dump() for kind, report in result.reports.items()},
        "violations": [str(v) for v in result.violations],
    }
