"""Spending the cheap-model budget on judgement instead of on a prefilter.

Four changes, each with its own hazard:

* unlimited stage-2 fan-out — the cap must still work when set, and the
  monthly projection must still fit
* requirements extracted from the ad — about the EMPLOYER, not the candidate
* several cover-letter variants, judged — a fabricating variant must never win
* a model-read fabrication check in the parse gate — it must not be able to
  fail a document by being unavailable
"""

from __future__ import annotations

import pytest

from backend.config import settings
from backend.scoring.rubric import score_schema
from backend.scoring.run import estimate_cost


# =========================================================================
# Fan-out
# =========================================================================


def test_the_shipped_default_scores_every_job_that_passed_the_filters():
    """The prefilter was the constraint, not the cost."""
    assert settings.scoring_stage2_max == 0, "0 means unlimited"


def test_unlimited_projects_the_real_fan_out_not_forty():
    """An estimate that silently assumed 40 would understate the bill."""
    estimate = estimate_cost(200, top_n=0)
    assert estimate["stage2_jobs"] == 200


def test_the_worst_case_month_fits_the_cap():
    """200 new jobs per run, six runs a day. The backfill case, not steady state."""
    per_run = estimate_cost(200, top_n=0)["total_usd"]
    assert per_run * 6 * 30 < settings.llm_monthly_cap_usd


def test_a_cap_is_still_honoured_when_set():
    capped = estimate_cost(400, top_n=40)
    assert capped["stage2_jobs"] == 40


def test_the_levers_name_the_setting_that_actually_exists():
    """A lever naming a setting nobody can find is not a lever.

    This pointed at SCORING_STAGE1_TOP_N, which no longer truncates the
    fan-out — following it would have changed nothing and looked like the
    advice was wrong.
    """
    # Force a miss by pricing an expensive model.
    estimate = estimate_cost(
        5000, top_n=0, scoring_model="anthropic/claude-opus-5"
    )
    assert not estimate["meets_target"]
    assert any("SCORING_STAGE2_MAX" in lever for lever in estimate["levers"])


# =========================================================================
# Requirement extraction
# =========================================================================


def test_the_ad_requirements_come_back_in_the_scoring_schema():
    """One call, not two. The model is already reading the whole ad."""
    schema = score_schema()
    for field in ("must_haves", "nice_to_haves", "tone"):
        assert field in schema["properties"], field
        assert field in schema["required"], field


def test_the_requirements_survive_into_the_result():
    from backend.scoring.stage2 import _coerce

    result = _coerce(
        {
            "score": 80,
            "reasoning": "fits",
            "matched_skills": ["SQL"],
            "gaps": [],
            "red_flags": [],
            "must_haves": ["5 years SQL", "AU work rights"],
            "nice_to_haves": ["Power BI"],
            "tone": "formal, government",
        },
        job_id=1,
    )

    assert result.must_haves == ["5 years SQL", "AU work rights"]
    assert result.nice_to_haves == ["Power BI"]
    assert result.tone == "formal, government"


def test_a_response_without_requirements_still_scores():
    """An older cached response, or a model that ignored the new fields.

    Losing the score because the extraction is missing would be trading the
    thing that works for the thing that is new.
    """
    from backend.scoring.stage2 import _coerce

    result = _coerce(
        {
            "score": 70,
            "reasoning": "ok",
            "matched_skills": [],
            "gaps": [],
            "red_flags": [],
        },
        job_id=1,
    )
    assert result.ok
    assert result.score == 70
    assert result.must_haves == []


def test_the_requirements_reach_the_writing_prompt():
    from backend.documents.build import _requirements_block

    block = _requirements_block(
        {
            "must_haves": ["5 years SQL"],
            "nice_to_haves": ["Power BI"],
            "tone": "formal, government",
        }
    )
    assert "5 years SQL" in block
    assert "Power BI" in block
    assert "formal, government" in block


def test_no_requirements_means_the_prompt_is_unchanged():
    """Scoring may not have run. The ad's own text is still in the prompt."""
    from backend.documents.build import _requirements_block

    assert _requirements_block(None) == ""
    assert _requirements_block({}) == ""


def test_the_prompt_forbids_gesturing_at_an_unmet_requirement():
    """Naming must-haves invites writing to them whether or not they are true.

    Which is the fabrication risk the extraction creates, so the instruction
    that closes it is load-bearing rather than decoration.
    """
    from backend.documents.build import _requirements_block

    block = _requirements_block({"must_haves": ["10 years Kubernetes"]})
    lowered = block.lower()
    assert "only where the candidate facts above support it" in lowered
    assert "do not gesture" in lowered


# =========================================================================
# Variants
# =========================================================================


class FakeJob:
    id = 1
    title = "Data Analyst"
    company = "Acme"
    location = "Adelaide SA"
    description = "We need SQL."
    salary_min = None
    salary_max = None
    salary_is_estimated = False


def slot():
    from backend.documents.engine import AISlot

    return AISlot(
        name="opening_hook",
        instruction="Open the letter.",
        tone="direct",
        max_words=60,
    )


def test_the_judge_picks_the_variant_it_names(monkeypatch):
    from backend.documents import build as build_module

    monkeypatch.setattr(build_module.llm, "complete", lambda *a, **k: "2")

    chosen = build_module._pick_best(
        ["first", "second", "third"],
        slot=slot(),
        job=FakeJob(),
        requirements={"must_haves": ["SQL"]},
    )
    assert chosen == "second"


def test_a_single_variant_is_not_judged_at_all(monkeypatch):
    """No comparison to make, so no call to pay for."""
    from backend.documents import build as build_module

    called: list[int] = []
    monkeypatch.setattr(
        build_module.llm, "complete", lambda *a, **k: called.append(1) or "1"
    )

    assert build_module._pick_best(["only"], slot=slot(), job=FakeJob(), requirements={}) == "only"
    assert called == []


@pytest.mark.parametrize("answer", ["9", "0", "banana", ""])
def test_an_unusable_judgement_falls_back_to_the_first_variant(monkeypatch, answer):
    """A judge that cannot answer must not be able to block a build.

    Every candidate has already passed the fabrication check, so the fallback
    is a correct document rather than a missing one.
    """
    from backend.documents import build as build_module

    monkeypatch.setattr(build_module.llm, "complete", lambda *a, **k: answer)

    chosen = build_module._pick_best(
        ["first", "second"], slot=slot(), job=FakeJob(), requirements={}
    )
    assert chosen == "first"


def test_a_judge_that_raises_falls_back_to_the_first_variant(monkeypatch):
    from backend.documents import build as build_module

    def boom(*a, **k):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(build_module.llm, "complete", boom)
    assert (
        build_module._pick_best(["first", "second"], slot=slot(), job=FakeJob(), requirements={})
        == "first"
    )


def test_a_fabricating_variant_never_reaches_the_judge(monkeypatch):
    """Fabrication first, quality second.

    A better-written variant that invented something is not a candidate at all.
    Filtering before judging means the judge never gets the chance to prefer it
    — which it would, because invented specifics read as stronger writing.
    """
    from backend.documents import build as build_module
    from backend.documents.fabrication import Violation

    honest = "Worked with SQL at Acme."
    liar = "Led a team of forty at Google for a decade."

    texts = iter([liar, honest, honest])
    monkeypatch.setattr(build_module.llm, "complete", lambda *a, **k: next(texts))
    monkeypatch.setattr(
        build_module,
        "validate_no_fabrication",
        lambda text, profile, job: (
            [Violation(kind="employer", value="Google", detail="not in profile")]
            if text == liar
            else []
        ),
    )

    judged: list[list[str]] = []

    def spy(candidates, **kwargs):
        judged.append(list(candidates))
        return candidates[0]

    monkeypatch.setattr(build_module, "_pick_best", spy)
    monkeypatch.setattr(settings, "document_variants", 3)

    generated, unresolved = build_module.generate_ai_slots(
        [slot()], profile=object(), job=FakeJob(), profile_text="SQL at Acme"
    )

    assert unresolved == []
    assert judged, "the judge should have been called"
    assert liar not in judged[0], "a fabricating variant was offered to the judge"


def test_all_variants_fabricating_still_fails_the_build(monkeypatch):
    """The existing guarantee, unchanged by adding variants."""
    from backend.documents import build as build_module
    from backend.documents.fabrication import Violation

    monkeypatch.setattr(build_module.llm, "complete", lambda *a, **k: "I led Google.")
    monkeypatch.setattr(
        build_module,
        "validate_no_fabrication",
        lambda text, profile, job: [
            Violation(kind="employer", value="Google", detail="not in profile")
        ],
    )
    monkeypatch.setattr(settings, "document_variants", 3)

    _generated, unresolved = build_module.generate_ai_slots(
        [slot()], profile=object(), job=FakeJob(), profile_text="SQL at Acme"
    )
    assert unresolved, "an all-fabricating slot must still be reported"


# =========================================================================
# The fabrication self-check in the parse gate
# =========================================================================


def test_an_unsupported_claim_fails_the_check(monkeypatch):
    from backend.documents import verify as verify_module

    monkeypatch.setattr(
        verify_module.llm,
        "complete_json",
        lambda *a, **k: {
            "unsupported": ["extensive experience leading cross-functional teams"]
        },
    )

    passed, detail = verify_module._fabrication_self_check(
        "Extensive experience leading cross-functional teams.", object(), "resume"
    )
    assert not passed
    assert "cross-functional" in detail


def test_a_clean_document_passes(monkeypatch):
    from backend.documents import verify as verify_module

    monkeypatch.setattr(
        verify_module.llm, "complete_json", lambda *a, **k: {"unsupported": []}
    )
    passed, detail = verify_module._fabrication_self_check("SQL at Acme.", object(), "resume")
    assert passed
    assert detail == ""


def test_an_unavailable_check_passes_rather_than_failing_the_document(monkeypatch):
    """The gate must not depend on a third party being up.

    Every other check here is deterministic. A model outage failing a document
    they all accepted would make the one thing you can rely on unreliable.
    """
    from backend.documents import verify as verify_module

    def boom(*a, **k):
        raise RuntimeError("no API key")

    monkeypatch.setattr(verify_module.llm, "complete_json", boom)

    passed, detail = verify_module._fabrication_self_check("anything", object(), "resume")
    assert passed
    assert "skipped" in detail


def test_the_check_is_skippable_by_setting(monkeypatch):
    """No API key at all is a supported configuration."""
    from backend.documents.verify import verify_pdf

    monkeypatch.setattr(settings, "document_fabrication_check", False)
    report = verify_pdf("/nonexistent.pdf", kind="resume", profile=object())
    assert "no_unsupported_claims" not in [check.name for check in report.checks]


def test_the_check_goes_through_the_stubbable_seam():
    """An inline import bypasses the stub and makes a real call.

    That is not a style point: it took the suite from 27 seconds to 3.5 minutes
    on LiteLLM's retry backoff, and it would have made every document build
    depend on a live API key.
    """
    import pathlib

    source = pathlib.Path("backend/documents/verify.py").read_text(encoding="utf-8")
    # Comment lines are stripped first: the comment at the call site names the
    # anti-pattern in order to warn against it, and matching that would make
    # this test fail on its own documentation.
    code = "\n".join(
        line.split("#")[0] for line in source.splitlines() if not line.strip().startswith("#")
    )

    assert "llm.complete_json(" in code
    assert "import complete_json" not in code
