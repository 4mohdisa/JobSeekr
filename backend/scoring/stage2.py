"""Stage 2: the LLM rubric pass, run only on stage 1's survivors.

One call per job, constrained to a JSON schema. Everything expensive about a
job ad — the boilerplate, the company blurb — is truncated out before the
prompt is built, because this is the call that is paid for per job.

Budget exhaustion is handled as a first-class outcome rather than an error:
``LLMBudgetExceeded`` stops stage 2 cleanly and the caller records a not-ok
run. Discovery makes no LLM calls, so it keeps working — the system degrades
to "still finding jobs, not scoring them" instead of stopping dead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.config import settings
from backend.llm.client import LLMBudgetExceeded, llm
from backend.logging_setup import get_logger
from backend.models import Job
from backend.scoring.rubric import rubric_prompt, score_schema

log = get_logger(__name__)

__all__ = ["StageTwoResult", "score_job", "score_jobs"]


_SYSTEM = (
    "You score how well a job ad fits ONE specific candidate. You are strict "
    "and specific. You never invent facts about the candidate: if the profile "
    "does not evidence something, it is a gap, not a match."
)


@dataclass
class StageTwoResult:
    job_id: int
    ok: bool
    score: float | None = None
    reasoning: str | None = None
    matched_skills: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)

    #: What the EMPLOYER asked for, independent of this candidate. Read by the
    #: document build to tailor against the ad rather than only score against it.
    must_haves: list[str] = field(default_factory=list)
    nice_to_haves: list[str] = field(default_factory=list)
    tone: str | None = None

    error: str | None = None


def build_prompt(job: Job, *, summary: str, rubric: dict[str, Any]) -> str:
    """The stage-2 prompt. Deliberately lean — this is the per-job cost."""
    description = (job.description or "")[: settings.scoring_prompt_char_budget]
    salary = "not stated"
    if job.salary_min or job.salary_max:
        salary = f"{job.salary_min or '?'}-{job.salary_max or '?'} AUD"
        if job.salary_is_estimated:
            salary += f" (estimated from a {job.salary_basis} rate)"

    return (
        f"CANDIDATE\n{summary}\n\n"
        f"JOB\n"
        f"Title: {job.title}\n"
        f"Company: {job.company}\n"
        f"Location: {job.location or 'not stated'}\n"
        f"Salary: {salary}\n"
        f"Description:\n{description}\n\n"
        f"{rubric_prompt(rubric)}\n\n"
        "Also extract, from the ad alone and WITHOUT reference to the "
        "candidate: what the employer treats as non-negotiable, what it frames "
        "as desirable, and the register the ad is written in.\n\n"
        "Return the JSON object only."
    )


def _coerce(payload: dict[str, Any], job_id: int) -> StageTwoResult:
    """Turn a validated payload into a result, tolerating loose list types."""

    def _strings(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    try:
        score = float(payload["score"])
    except (KeyError, TypeError, ValueError):
        return StageTwoResult(
            job_id=job_id, ok=False, error="score missing or non-numeric"
        )

    return StageTwoResult(
        job_id=job_id,
        ok=True,
        score=max(0.0, min(100.0, score)),
        reasoning=str(payload.get("reasoning") or "").strip() or None,
        matched_skills=_strings(payload.get("matched_skills")),
        gaps=_strings(payload.get("gaps")),
        red_flags=_strings(payload.get("red_flags")),
        must_haves=_strings(payload.get("must_haves")),
        nice_to_haves=_strings(payload.get("nice_to_haves")),
        tone=str(payload.get("tone") or "").strip() or None,
    )


def score_job(job: Job, *, summary: str, rubric: dict[str, Any]) -> StageTwoResult:
    """Score one job. Raises only ``LLMBudgetExceeded``; everything else is a
    failed result, because one unscoreable ad must not end the pass."""
    assert job.id is not None
    try:
        payload = llm.complete_json(
            build_prompt(job, summary=summary, rubric=rubric),
            model=settings.llm_model_scoring,
            purpose="scoring",
            schema=score_schema(),
            system=_SYSTEM,
            job_id=job.id,
        )
    except LLMBudgetExceeded:
        raise
    except Exception as exc:
        log.exception("stage2_call_failed", job_id=job.id, error=str(exc)[:300])
        return StageTwoResult(
            job_id=job.id, ok=False, error=f"{type(exc).__name__}: {exc}"[:300]
        )

    return _coerce(payload, job.id)


def score_jobs(
    jobs: list[Job], *, summary: str, rubric: dict[str, Any]
) -> tuple[list[StageTwoResult], bool]:
    """Score each job. Returns (results, budget_ok).

    ``budget_ok`` False means the monthly cap halted the pass partway; the
    results collected before that point are still returned and still valid.
    """
    results: list[StageTwoResult] = []
    for job in jobs:
        try:
            results.append(score_job(job, summary=summary, rubric=rubric))
        except LLMBudgetExceeded as exc:
            log.error(
                "stage2_halted_on_budget",
                scored_before_halt=len(results),
                remaining=len(jobs) - len(results),
                detail=str(exc),
            )
            return results, False
    return results, True
