"""Scoring behaviour, entirely offline — the LLM gateway is stubbed throughout.

The cost assertion at the bottom is the one that protects the project's stated
budget: 200 jobs discovered and scored for under $0.15. It fails loudly if
someone widens a truncation budget or the stage-2 fan-out.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine

from backend.config import settings
from backend.llm.client import LLMBudgetExceeded
from backend.models import Campaign, GrayZoneAction, Job
from backend.scoring import stage1, stage2
from backend.scoring.filters import apply_hard_filters
from backend.scoring.rubric import DEFAULT_RUBRIC, rubric_prompt, score_schema
from backend.scoring.run import estimate_cost
from backend.scoring.stage1 import EmbeddingCache, cosine


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def make_campaign(**kwargs) -> Campaign:
    base = {
        "name": "test",
        "search_terms": ["python developer"],
        "locations": ["Adelaide SA"],
        "score_floor": 60.0,
        "score_auto_apply": 80.0,
        "gray_zone_action": GrayZoneAction.QUEUE,
    }
    base.update(kwargs)
    return Campaign(**base)


def make_job(job_id: int = 1, **kwargs) -> Job:
    base = {
        "id": job_id,
        "source": "seek",
        "source_job_id": str(job_id),
        "url": f"https://example.com/{job_id}",
        "title": "Python Developer",
        "company": "Acme",
        "location": "Adelaide SA",
        "description": "We need a Python developer.",
        "dedupe_hash": f"hash{job_id}",
    }
    base.update(kwargs)
    return Job(**base)


# ------------------------------------------------------------- hard filters


def test_excluded_company_is_dropped_with_a_reason():
    campaign = make_campaign(exclusions={"companies": ["Acme Pty Ltd"]})
    outcome = apply_hard_filters([make_job(company="ACME")], campaign)
    assert outcome.kept == []
    assert outcome.rejected[0].reason == "company_excluded"


def test_excluded_title_keyword_is_dropped():
    campaign = make_campaign(exclusions={"title_keywords": ["senior"]})
    outcome = apply_hard_filters([make_job(title="Senior Python Developer")], campaign)
    assert outcome.rejected[0].reason == "title_excluded"


def test_already_applied_job_is_dropped():
    outcome = apply_hard_filters(
        [make_job(job_id=7)], make_campaign(), already_applied_job_ids={7}
    )
    assert outcome.rejected[0].reason == "already_applied"


def test_salary_clearly_below_floor_is_dropped():
    campaign = make_campaign(salary_floor=120000)
    outcome = apply_hard_filters(
        [make_job(salary_min=60000, salary_max=70000)], campaign
    )
    assert outcome.rejected[0].reason == "below_salary_floor"


def test_unstated_salary_is_KEPT_by_default():
    """Most Australian ads omit salary; dropping them would discard the market."""
    campaign = make_campaign(salary_floor=120000)
    outcome = apply_hard_filters([make_job(salary_min=None, salary_max=None)], campaign)
    assert len(outcome.kept) == 1


def test_unstated_salary_can_be_dropped_when_the_user_opts_in():
    campaign = make_campaign(
        salary_floor=120000, exclusions={"drop_unstated_salary": True}
    )
    outcome = apply_hard_filters([make_job(salary_min=None, salary_max=None)], campaign)
    assert outcome.rejected[0].reason == "below_salary_floor"


# ------------------------------------------------------------------ stage 1


def test_cosine_basics():
    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine([], [1, 0]) == 0.0
    assert cosine([0, 0], [1, 0]) == 0.0  # no division by zero


def test_embedding_text_is_truncated_to_the_budget():
    job = make_job(description="x" * 100_000)
    text = stage1.embedding_text(job)
    assert len(text) <= settings.scoring_embedding_char_budget + 300


def test_ranking_puts_the_closest_job_first(monkeypatch):
    jobs = [
        make_job(1, title="Warehouse Storeperson"),
        make_job(2, title="Python Developer"),
    ]

    vectors = {
        "summary": [1.0, 0.0],
        "Warehouse Storeperson": [0.0, 1.0],
        "Python Developer": [0.9, 0.1],
    }

    def fake_embed(texts, **kwargs):
        out = []
        for text in texts:
            match = next((v for k, v in vectors.items() if k in text), [0.5, 0.5])
            out.append(match)
        return out

    monkeypatch.setattr(stage1.llm, "embed", fake_embed)
    ranked = stage1.rank_jobs(
        jobs, summary="summary", cache=EmbeddingCache(Path("/nonexistent/x"))
    )
    assert ranked[0][0].title == "Python Developer"


def test_embedding_cache_prevents_a_second_call(monkeypatch, tmp_path):
    calls: list[int] = []

    def fake_embed(texts, **kwargs):
        calls.append(len(texts))
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(stage1.llm, "embed", fake_embed)
    cache = EmbeddingCache(tmp_path / "emb.jsonl")
    jobs = [make_job(1)]

    stage1.rank_jobs(jobs, summary="s", cache=cache)
    first_round = sum(calls)
    calls.clear()

    # A fresh cache object reading the same file must find everything.
    stage1.rank_jobs(jobs, summary="s", cache=EmbeddingCache(tmp_path / "emb.jsonl"))
    assert first_round > 0
    assert sum(calls) == 0, "second run should be served entirely from cache"


# ------------------------------------------------------------------ stage 2


def test_rubric_prompt_lists_criteria_and_red_flags():
    text = rubric_prompt(DEFAULT_RUBRIC)
    assert "skills_match" in text and "Red flags" in text


def test_score_schema_is_strict():
    schema = score_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        # About the candidate.
        "score",
        "reasoning",
        "matched_skills",
        "gaps",
        "red_flags",
        # About the AD, extracted in the same call because the model is already
        # reading the whole thing. Consumed by the document build.
        "must_haves",
        "nice_to_haves",
        "tone",
    }


def test_the_extracted_requirements_are_about_the_ad_not_the_candidate():
    """The distinction the whole extraction rests on.

    must_haves is what the employer asked for, judged against nobody. gaps is
    what the candidate lacks. Conflating them would tailor the letter to the
    candidate's weaknesses instead of the employer's needs.
    """
    schema = score_schema()
    for field in ("must_haves", "nice_to_haves"):
        description = schema["properties"][field]["description"].lower()
        assert "candidate" not in description or "not judged" in description, field


def test_stage2_maps_a_good_response(monkeypatch):
    monkeypatch.setattr(
        stage2.llm,
        "complete_json",
        lambda *a, **k: {
            "score": 82.5,
            "reasoning": "Strong overlap.",
            "matched_skills": ["python"],
            "gaps": ["kubernetes"],
            "red_flags": [],
        },
    )
    result = stage2.score_job(make_job(), summary="s", rubric=DEFAULT_RUBRIC)
    assert result.ok and result.score == 82.5
    assert result.matched_skills == ["python"]


def test_stage2_clamps_an_out_of_range_score(monkeypatch):
    monkeypatch.setattr(
        stage2.llm,
        "complete_json",
        lambda *a, **k: {
            "score": 140,
            "reasoning": "",
            "matched_skills": [],
            "gaps": [],
            "red_flags": [],
        },
    )
    assert (
        stage2.score_job(make_job(), summary="s", rubric=DEFAULT_RUBRIC).score == 100.0
    )


def test_stage2_records_a_failure_rather_than_crashing_the_pass(monkeypatch):
    def boom(*a, **k):
        raise ValueError("model returned garbage")

    monkeypatch.setattr(stage2.llm, "complete_json", boom)
    result = stage2.score_job(make_job(), summary="s", rubric=DEFAULT_RUBRIC)
    assert result.ok is False and "garbage" in result.error


def test_budget_exhaustion_halts_stage2_cleanly_and_keeps_earlier_results(monkeypatch):
    """Discovery makes no LLM calls, so it keeps running; scoring stops here."""
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        if calls["n"] > 2:
            raise LLMBudgetExceeded(30.0, 25.0)
        return {
            "score": 70,
            "reasoning": "ok",
            "matched_skills": [],
            "gaps": [],
            "red_flags": [],
        }

    monkeypatch.setattr(stage2.llm, "complete_json", fake)
    jobs = [make_job(i) for i in range(1, 6)]
    results, budget_ok = stage2.score_jobs(jobs, summary="s", rubric=DEFAULT_RUBRIC)

    assert budget_ok is False
    assert len(results) == 2, "results collected before the halt are kept"


# --------------------------------------------------------------- cost target


def test_stage1_stays_a_small_fraction_of_the_target():
    """Embedding 200 ads costs about a cent — truncation and batching work.

    It was a tenth of that on OpenAI's small model. Moving embeddings to Gemini
    so the whole pipeline runs on one key costs 7.5x per token; the assertion is
    against the project's own target rather than a round number, because that is
    the thing the spend has to fit inside.
    """
    projection = estimate_cost(200)

    # Both bounds. An upper bound alone is satisfied by zero, which is what a
    # mispriced or unpriced embedding model projects — the failure this is
    # meant to catch reads as the cheapest possible result.
    assert projection["stage1_usd"] > 0, projection
    assert projection["stage1_usd"] < 0.1 * projection["target_usd"], projection
    assert projection["meets_target"], projection


def test_cost_scales_with_volume_when_the_fan_out_is_unlimited():
    """The deliberate trade, and a REVERSAL of what this test used to assert.

    It previously pinned "stage 2 is capped, so doubling discovery must not
    double the bill". That was true and is no longer the default: the prefilter
    had become the constraint rather than the cost, so a good match with unusual
    wording was discarded on an embedding similarity that does not understand
    context.

    With SCORING_STAGE2_MAX=0 the bill is linear in new jobs. That is affordable
    because scoring is incremental — needs_scoring skips anything already
    scored — so the steady-state number is new jobs per run, not the table.
    """
    small = estimate_cost(200, top_n=0)
    large = estimate_cost(400, top_n=0)

    assert large["stage2_jobs"] == 400, "every job that passed the filters"
    assert large["stage2_usd"] > small["stage2_usd"] * 1.9


def test_a_configured_cap_still_stops_the_cost_scaling():
    """Unlimited is the default, not the only option.

    Someone who wants the old behaviour sets SCORING_STAGE2_MAX and gets it
    back exactly — the cap is still enforced, it is simply no longer implicit.
    """
    small = estimate_cost(200, top_n=40)
    large = estimate_cost(400, top_n=40)

    assert large["stage2_jobs"] == small["stage2_jobs"] == 40
    assert large["stage2_usd"] == small["stage2_usd"]


def test_the_unlimited_fan_out_fits_the_monthly_cap():
    """The number that decides whether the default is safe.

    200 new jobs per run x 6 runs a day is the backfill worst case, not the
    steady state. If even that fits the monthly cap, unlimited is a safe
    default; if it stops fitting, this test is where that shows up.
    """
    from backend.config import settings

    per_run = estimate_cost(200, top_n=0)["total_usd"]
    monthly_worst_case = per_run * 6 * 30

    assert monthly_worst_case < settings.llm_monthly_cap_usd, (
        f"unlimited fan-out projects ${monthly_worst_case:.2f}/month against a "
        f"${settings.llm_monthly_cap_usd:.2f} cap — set SCORING_STAGE2_MAX"
    )


def test_the_shipped_default_meets_the_cost_target():
    """The $0.15 / 200-job target, met by the configuration as shipped.

    Stage 2 is constrained classification against a fixed schema — exactly the
    work a small model does well — so it is routed to Gemini Flash-Lite while
    cover letters stay on a strong model. That single change moved the
    projection from ~$0.43 to ~$0.025 for 200 jobs.

    The earlier version of this test pinned the opposite property: at Claude
    Opus 5 the target was unreachable, because ~220 output tokens x $25/1M is
    ~$0.005 per job before a single prompt token, so 40 jobs exceeded $0.15 on
    output alone. That was arithmetic rather than tuning, and the fix was to
    stop paying Opus rates for classification.
    """
    # Priced from the defaults declared in backend/config.py, not from the
    # ambient settings object. Read live, this asserted on whatever the
    # developer's .env happened to say, so it went red on a machine that had
    # simply copied .env.example — testing the local file rather than the
    # shipped configuration it names.
    from backend.config import Settings

    shipped = Settings(_env_file=None)
    default = estimate_cost(
        200,
        scoring_model=shipped.llm_model_scoring,
        embedding_model=shipped.llm_model_embedding,
    )

    assert default["scoring_model"].startswith("gemini/")
    assert default["meets_target"] is True, default
    assert default["total_usd"] < 0.15
    assert not default["levers"], "a projection under target needs no levers"


def test_the_expensive_model_still_reports_itself_as_over_target():
    """The honesty machinery must keep working for whoever changes the model."""
    expensive = estimate_cost(200, scoring_model="anthropic/claude-opus-5")

    assert expensive["meets_target"] is False
    assert expensive["levers"], "an over-target projection must say what to change"
    assert expensive["total_usd"] > 0.15


def test_writing_stays_on_a_strong_model():
    """Cost pressure must not quietly reach the prose sent to an employer.

    A mis-scored job is re-scored; a cover letter is not recalled.
    """
    from backend.config import settings

    assert settings.llm_model_writing.startswith("anthropic/")
    assert settings.llm_model_writing != settings.llm_model_scoring


def test_an_unpriced_model_warns_rather_than_crashing_a_run():
    projection = estimate_cost(10, scoring_model="some/unlisted-model")
    assert projection["stage2_usd"] == 0.0
