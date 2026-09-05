"""The scoring pass: hard filters, then stage 1, then stage 2 on the survivors.

    uv run python -m backend.scoring.run

A score is valid for exactly one ``(job_id, profile_version, rubric_version)``
triple. Re-running is therefore free for anything already scored under the
current profile and rubric, and a rubric bump re-scores everything — which is
correct, because scores from different rubrics are not comparable.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, select

from backend.config import settings
from backend.db import persist_detached, session_scope
from backend.llm.client import LLMBudgetExceeded
from backend.logging_setup import configure_logging, get_logger
from backend.models import (
    Application,
    Campaign,
    Job,
    JobStatus,
    Profile,
    Run,
    RunPhase,
    Score,
)
from backend.scoring.filters import apply_hard_filters
from backend.scoring.rubric import rubric_for, rubric_hash
from backend.scoring.stage1 import EmbeddingCache, campaign_profile_summary, rank_jobs
from backend.scoring.stage2 import score_jobs

log = get_logger(__name__)

__all__ = ["estimate_cost", "needs_scoring", "run_scoring", "score_campaign"]


_CHARS_PER_TOKEN = 4.0

# Fixed overhead per stage-2 prompt: the rendered rubric plus the candidate
# summary, which ride along with every scored job.
_STAGE2_PROMPT_OVERHEAD_CHARS = 1800

# A schema-constrained score object (score, reasoning, three short arrays).
_STAGE2_OUTPUT_TOKENS = 220


def _price(model: str, direction: str) -> float:
    """Published USD per 1M tokens for a model, or 0.0 if we have no figure.

    An unknown model projects as free rather than crashing a run. The warning
    below makes that visible instead of quietly reporting an impossibly cheap
    estimate.
    """
    prices = settings.llm_prices_per_m_tokens.get(model)
    if prices is None:
        log.warning("no_price_for_model", model=model, hint="add it to LLM_PRICES_PER_M_TOKENS")
        return 0.0
    return float(prices.get(direction, 0.0))


def estimate_cost(
    n_jobs: int,
    *,
    top_n: int | None = None,
    scoring_model: str | None = None,
    embedding_model: str | None = None,
) -> dict[str, Any]:
    """Project the spend to discover and score ``n_jobs`` under current settings.

    Discovery is plain HTTP and costs nothing, so this is the whole bill.

    The projection is priced from the *configured* models rather than from
    hardcoded numbers, because which model is configured dominates everything
    else: at Claude Opus 5 rates the schema-constrained output alone
    (~220 tokens x $25/1M) costs more per job than the entire prompt, so the
    stage-2 fan-out — not the truncation budget — is the lever that matters.
    ``meets_target`` says plainly whether the current configuration fits the
    project's stated target, and ``levers`` says what to change if not.
    """
    # 0 (unlimited) projects the full fan-out, which is the number worth
    # reporting — an estimate that silently assumed 40 would understate the bill.
    top_n = top_n or settings.scoring_stage2_max or n_jobs
    scoring_model = scoring_model or settings.llm_model_scoring
    embedding_model = embedding_model or settings.llm_model_embedding

    embed_tokens = n_jobs * (settings.scoring_embedding_char_budget + 120) / _CHARS_PER_TOKEN
    stage1_usd = embed_tokens / 1_000_000 * _price(embedding_model, "input")

    scored = min(n_jobs, top_n)
    input_tokens = (
        scored
        * (settings.scoring_prompt_char_budget + _STAGE2_PROMPT_OVERHEAD_CHARS)
        / _CHARS_PER_TOKEN
    )
    output_tokens = scored * _STAGE2_OUTPUT_TOKENS
    stage2_usd = (
        input_tokens / 1_000_000 * _price(scoring_model, "input")
        + output_tokens / 1_000_000 * _price(scoring_model, "output")
    )

    total = stage1_usd + stage2_usd
    target = settings.scoring_cost_target_usd
    meets = total <= target

    levers: list[str] = []
    if not meets:
        per_job = stage2_usd / scored if scored else 0.0
        affordable = int((target - stage1_usd) / per_job) if per_job else 0
        levers = [
            f"set SCORING_STAGE2_MAX to about {max(1, affordable)} "
            f"(currently {settings.scoring_stage2_max or 'unlimited'})",
            f"lower SCORING_PROMPT_CHAR_BUDGET from {settings.scoring_prompt_char_budget}",
            f"configure a cheaper LLM_MODEL_SCORING than {scoring_model} (your call)",
            f"raise SCORING_COST_TARGET_USD above {target}",
        ]

    return {
        "jobs": n_jobs,
        "stage2_jobs": scored,
        "scoring_model": scoring_model,
        "embedding_model": embedding_model,
        "stage1_usd": round(stage1_usd, 6),
        "stage2_usd": round(stage2_usd, 6),
        "total_usd": round(total, 6),
        "target_usd": target,
        "meets_target": meets,
        "levers": levers,
    }


def needs_scoring(
    session: Session,
    job: Job,
    *,
    profile_version: int,
    rubric_version: int,
    rubric_digest: str | None = None,
) -> bool:
    """True when no current score exists for this job.

    ``rubric_digest`` is what makes an edited rubric detectable. rubric_version
    only changes when a human remembers to bump it, so editing the criteria
    without bumping produced scores indistinguishable from ones computed before
    the edit — the shortlist silently reflecting a rubric that no longer exists.
    A stored score whose hash does not match the current rubric is stale and
    the job is re-scored.
    """
    existing = session.exec(
        select(Score).where(
            Score.job_id == job.id,
            Score.profile_version == profile_version,
            Score.rubric_version == rubric_version,
        )
    ).first()
    if existing is None:
        return True

    if rubric_digest and existing.rubric_hash and existing.rubric_hash != rubric_digest:
        log.info(
            "rescoring_after_rubric_edit",
            job_id=job.id,
            stored=existing.rubric_hash,
            current=rubric_digest,
            note="the rubric text changed without a version bump",
        )
        return True
    return False


def _current_profile(session: Session) -> Profile | None:
    return session.exec(select(Profile).order_by(Profile.version.desc())).first()  # type: ignore[union-attr]


def _applied_job_ids(session: Session) -> set[int]:
    return {row.job_id for row in session.exec(select(Application)).all()}


def _final_score(stage1: float | None, stage2: float | None) -> float | None:
    """The score everything downstream compares against a threshold.

    Stage 2 wins whenever it ran: it read the actual rubric. Stage 1 is a
    similarity in 0-1 and is only a fallback for jobs that never reached the
    LLM, rescaled to the same 0-100 axis so a threshold means one thing.
    """
    if stage2 is not None:
        return stage2
    if stage1 is not None:
        return round(max(0.0, min(1.0, stage1)) * 100, 2)
    return None


def score_campaign(
    session: Session,
    campaign: Campaign,
    *,
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    cache: EmbeddingCache | None = None,
) -> dict[str, Any]:
    """Score one campaign's unscored jobs. Returns counts for the run record."""
    profile = _current_profile(session)
    if profile is None:
        log.error("no_profile_row", campaign=campaign.name)
        return {"error": "no profile"}

    rubric, rubric_version = rubric_for(campaign)
    rubric_digest = rubric_hash(rubric)
    profile_version = profile.version

    candidates = list(
        session.exec(
            select(Job).where(
                Job.campaign_id == campaign.id,
                Job.status.in_(  # type: ignore[union-attr]
                    [JobStatus.DISCOVERED, JobStatus.SCORED, JobStatus.REJECTED]
                ),
            )
        ).all()
    )
    if not force:
        candidates = [
            job
            for job in candidates
            if needs_scoring(
                session,
                job,
                profile_version=profile_version,
                rubric_version=rubric_version,
                rubric_digest=rubric_digest,
            )
        ]
    if limit:
        candidates = candidates[:limit]

    counts: dict[str, Any] = {
        "campaign": campaign.name,
        "candidates": len(candidates),
        "rubric_version": rubric_version,
        "profile_version": profile_version,
    }
    if not candidates:
        return counts

    filtered = apply_hard_filters(
        candidates, campaign, already_applied_job_ids=_applied_job_ids(session)
    )
    counts["filtered_out"] = len(filtered.rejected)
    counts["filter_reasons"] = filtered.summary

    for rejection in filtered.rejected:
        job = next((j for j in candidates if j.id == rejection.job_id), None)
        if job is not None and not dry_run:
            job.status = JobStatus.REJECTED
            session.add(job)

    if not filtered.kept:
        return counts

    projection = estimate_cost(len(filtered.kept))
    counts["projected_usd"] = projection["total_usd"]
    if not projection["meets_target"]:
        # Loud, not silent: the user should learn this before the invoice.
        log.warning(
            "scoring_cost_over_target",
            projected_usd=projection["total_usd"],
            target_usd=projection["target_usd"],
            scoring_model=projection["scoring_model"],
            levers=projection["levers"],
        )

    summary = campaign_profile_summary(profile, campaign)
    ranked = rank_jobs(
        filtered.kept,
        summary=summary,
        # Rank everything when the fan-out is unlimited: truncating here would
        # discard jobs before stage 2 could see them, which is the prefilter
        # problem this setting exists to remove.
        top_n=settings.scoring_stage2_max or len(filtered.kept) or None,
        cache=cache,
    )
    counts["ranked"] = len(ranked)

    # 0 means every job that passed the hard filters. The ranking still
    # decides the ORDER, so if the budget cap stops the run partway the best
    # jobs have already been scored.
    fan_out = settings.scoring_stage2_max or len(ranked)
    survivors = [job for job, _ in ranked[:fan_out]]
    stage1_by_id = {job.id: similarity for job, similarity in ranked}

    if dry_run:
        counts["would_score"] = len(survivors)
        return counts

    results, budget_ok = score_jobs(survivors, summary=summary, rubric=rubric)
    counts["stage2_scored"] = sum(1 for r in results if r.ok)
    counts["stage2_failed"] = sum(1 for r in results if not r.ok)
    counts["budget_ok"] = budget_ok

    stage2_by_id = {result.job_id: result for result in results}

    finals: dict[int, float | None] = {}

    for job in filtered.kept:
        stage1 = stage1_by_id.get(job.id)
        result = stage2_by_id.get(job.id)
        stage2 = result.score if result and result.ok else None
        final = _final_score(stage1, stage2)
        finals[job.id] = final

        session.add(
            Score(
                job_id=job.id,
                profile_version=profile_version,
                rubric_version=rubric_version,
                rubric_hash=rubric_digest,
                stage1=stage1,
                stage2=stage2,
                final=final,
                reasoning=result.reasoning if result else None,
                requirements=(
                    {
                        "must_haves": result.must_haves,
                        "nice_to_haves": result.nice_to_haves,
                        "tone": result.tone,
                    }
                    if result
                    else {}
                ),
                matched_skills=result.matched_skills if result else [],
                gaps=result.gaps if result else [],
                red_flags=result.red_flags if result else [],
            )
        )

        if final is None:
            continue
        job.status = (
            JobStatus.SCORED if final >= (campaign.score_floor or 0) else JobStatus.REJECTED
        )
        session.add(job)

    floor = campaign.score_floor or 0
    counts["above_floor"] = sum(1 for final in finals.values() if (final or 0) >= floor)
    return counts


def run_scoring(
    *,
    campaign_id: int | None = None,
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    session_factory: Callable[[], Any] = session_scope,
    campaigns: Iterable[Campaign] | None = None,
) -> Run:
    """Run the scoring pass and record the ``Run`` row."""
    started = datetime.now(UTC)
    errors: list[dict[str, Any]] = []
    per_campaign: list[dict[str, Any]] = []
    cache = EmbeddingCache()

    with session_factory() as session:
        if campaigns is None:
            query = select(Campaign).where(Campaign.active == True)
            if campaign_id is not None:
                query = select(Campaign).where(Campaign.id == campaign_id)
            selected = list(session.exec(query).all())
        else:
            selected = list(campaigns)

        for campaign in selected:
            try:
                per_campaign.append(
                    score_campaign(
                        session,
                        campaign,
                        limit=limit,
                        force=force,
                        dry_run=dry_run,
                        cache=cache,
                    )
                )
            except LLMBudgetExceeded as exc:
                log.error("scoring_halted_on_budget", campaign=campaign.name, detail=str(exc))
                errors.append({"campaign": campaign.name, "error": str(exc)})
                break
            except Exception as exc:
                log.exception("campaign_scoring_failed", campaign=campaign.name)
                errors.append(
                    {"campaign": campaign.name, "error": f"{type(exc).__name__}: {exc}"[:300]}
                )

        run = Run(
            started_at=started,
            ended_at=datetime.now(UTC),
            phase=RunPhase.SCORING,
            counts={"campaigns": per_campaign, "dry_run": dry_run},
            errors=errors,
            ok=not errors,
        )
        persist_detached(session, run)

    log.info("scoring_complete", campaigns=len(per_campaign), errors=len(errors))
    return run


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI wiring
    parser = argparse.ArgumentParser(prog="python -m backend.scoring.run")
    parser.add_argument("--campaign", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--force", action="store_true", help="re-score even if a score already exists"
    )
    parser.add_argument("--dry-run", action="store_true", help="filter and rank, no LLM calls")
    parser.add_argument(
        "--estimate",
        type=int,
        metavar="N",
        default=None,
        help="print the projected cost for N jobs and exit",
    )
    args = parser.parse_args(argv)

    configure_logging()
    if args.estimate:
        projection = estimate_cost(args.estimate)
        log.info("cost_projection", **projection)
        return 0

    run = run_scoring(
        campaign_id=args.campaign, limit=args.limit, force=args.force, dry_run=args.dry_run
    )
    return 0 if run.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
