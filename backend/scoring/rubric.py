"""The scoring rubric: what "a good job for this user" means, as data.

Kept as data on the campaign rather than as code because it is the thing the
user tunes most often, and because every score records the ``rubric_version``
it was produced under.

That version field is not bookkeeping. Scores from different rubrics are **not
comparable**: change a weight and every historical score becomes a measurement
of a different question. The analytics page groups by ``rubric_version`` for
exactly this reason, and the weekly rubric-analysis job proposes changes as a
new version rather than editing the old one in place.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from backend.logging_setup import get_logger

log = get_logger(__name__)

__all__ = [
    "DEFAULT_RUBRIC",
    "rubric_for",
    "rubric_hash",
    "rubric_prompt",
    "score_schema",
]


DEFAULT_RUBRIC: dict[str, Any] = {
    "criteria": [
        {
            "key": "skills_match",
            "weight": 30,
            "description": (
                "How much of the required stack the candidate already has "
                "evidence for in their profile. Evidence means a listed skill "
                "or something they demonstrably did, not an adjacent technology."
            ),
        },
        {
            "key": "seniority_fit",
            "weight": 20,
            "description": (
                "Whether the level matches. Score low both when the role is "
                "clearly above the candidate's demonstrated scope and when it "
                "is well below it — an over-qualified application is usually "
                "a rejection, not a safety net."
            ),
        },
        {
            "key": "location_worktype_fit",
            "weight": 15,
            "description": (
                "Match against the campaign's locations and work types. "
                "Remote and hybrid count as a match for any location the "
                "campaign lists unless the ad requires onsite attendance."
            ),
        },
        {
            "key": "salary_fit",
            "weight": 15,
            "description": (
                "Against the campaign salary floor. An ad that states no "
                "salary is NOT penalised — most Australian ads omit it — but "
                "one clearly below the floor is."
            ),
        },
        {
            "key": "domain_relevance",
            "weight": 20,
            "description": (
                "How relevant the industry and problem domain are to the "
                "candidate's background and stated preferences."
            ),
        },
    ],
    "red_flags": [
        "unpaid or 'exposure' work",
        "commission-only remuneration",
        "requires a qualification, licence or clearance the profile does not show",
        "requires citizenship or a visa status the profile does not show",
        "significant unstated travel or relocation requirement",
        "job ad is a recruiter fishing expedition with no named employer or real role",
        "pay-to-apply, training bonds, or asks the candidate for money",
        "multi-level marketing or door-to-door sales dressed as a salaried role",
    ],
    "notes": (
        "Score the fit for THIS candidate, not the quality of the job in the "
        "abstract. A great job the candidate cannot get is a low score."
    ),
}
"""A sensible starting rubric for an Australian job seeker.

Weights sum to 100 so the LLM's per-criterion reasoning maps onto the final
0-100 score without a second normalisation step.
"""


def rubric_for(campaign: Any) -> tuple[dict[str, Any], int]:
    """The rubric and version to score a campaign under.

    Falls back to the default when a campaign has not been given one, so a
    freshly created campaign still scores rather than failing.
    """
    rubric = getattr(campaign, "rubric", None) or {}
    if not rubric.get("criteria"):
        return DEFAULT_RUBRIC, int(getattr(campaign, "rubric_version", 1) or 1)
    return rubric, int(getattr(campaign, "rubric_version", 1) or 1)


def rubric_hash(rubric: dict[str, Any]) -> str:
    """Content hash, for noticing a rubric edited without a version bump."""
    canonical = json.dumps(rubric, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def rubric_prompt(rubric: dict[str, Any]) -> str:
    """Render the rubric into the stage-2 prompt.

    Deliberately compact: this text is sent once per scored job, so every
    unnecessary sentence is multiplied by the number of jobs scored.
    """
    lines = ["Scoring criteria (weights sum to 100):"]
    for criterion in rubric.get("criteria", []):
        lines.append(
            f"- {criterion['key']} (weight {criterion['weight']}): {criterion['description']}"
        )

    red_flags = rubric.get("red_flags") or []
    if red_flags:
        lines.append("")
        lines.append("Red flags — list any that genuinely apply:")
        lines.extend(f"- {flag}" for flag in red_flags)

    if rubric.get("notes"):
        lines.append("")
        lines.append(str(rubric["notes"]))
    return "\n".join(lines)


def score_schema() -> dict[str, Any]:
    """JSON schema stage 2 constrains the model to.

    ``additionalProperties: false`` and the required list are what make the
    output safe to store without defensive re-parsing downstream.
    """
    return {
        "type": "object",
        "title": "job_score",
        "properties": {
            "score": {
                "type": "number",
                "minimum": 0,
                "maximum": 100,
                "description": "Overall weighted fit for this candidate.",
            },
            "reasoning": {
                "type": "string",
                "description": "Two or three sentences. Concrete, not generic.",
            },
            "matched_skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Skills the ad asks for that the profile evidences.",
            },
            "gaps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Requirements the profile does not evidence.",
            },
            "red_flags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Only flags that genuinely apply. Empty is normal.",
            },
            # What the EMPLOYER wants, independent of this candidate. Added to
            # the scoring schema rather than asked for separately because the
            # model is already reading the whole ad here — a second call would
            # pay to read it twice to learn something the first call could have
            # returned. Consumed by the document build, not by scoring.
            "must_haves": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Requirements the ad treats as non-negotiable, in the ad's "
                    "own terms. Not judged against the candidate."
                ),
            },
            "nice_to_haves": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Requirements the ad frames as desirable, not essential.",
            },
            "tone": {
                "type": "string",
                "description": (
                    "How the ad is written, in a few words — e.g. 'formal, "
                    "government', 'informal startup', 'clinical and precise'. "
                    "Used to match the register of the cover letter."
                ),
            },
        },
        "required": [
            "score",
            "reasoning",
            "matched_skills",
            "gaps",
            "red_flags",
            "must_haves",
            "nice_to_haves",
            "tone",
        ],
        "additionalProperties": False,
    }
