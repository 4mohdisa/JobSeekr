"""The contracts every JobSeekr implementation is written against.

Adding a platform means one new file implementing these protocols.

This module deliberately imports neither Playwright nor ``backend.models``.
Discovery imports it to get :class:`Source`, and discovery must stay HTTP-only
and browser-free (Claude.md); importing the browser here would put a Playwright
dependency on that path, and importing the table models would create a cycle
with the layers that define them. The parameters that would otherwise carry
those types are annotated ``Any`` and name their concrete type in the docstring.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

__all__ = [
    "Applier",
    "ApplyOutcome",
    "ApplyResult",
    "LLMClient",
    "RawJob",
    "Source",
]


class RawJob(BaseModel):
    """A job exactly as a source handed it over, before normalisation.

    Not a table. Sources disagree about everything — salary units, location
    spelling, what "posted" means — so this is the honest, lossy-free landing
    zone and the normalise/dedupe step downstream is the only thing allowed to
    make judgement calls. ``raw`` keeps the untouched payload so a source's
    quirks can be re-parsed later without re-fetching.
    """

    source: str
    """Source identifier, matching the implementing ``Source.name``."""

    source_job_id: str
    """The source's own id. Unique per source, not globally."""

    url: str
    title: str
    company: str
    location: str | None = None
    description: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None

    posted_at: datetime | None = None
    """When the ad was posted, UTC. None when the source does not say."""

    apply_type: str | None = None
    """How the ad is applied to (e.g. "easy_apply", "external"), source's wording."""

    ad_contact_email: str | None = None
    """Contact address scraped from the ad body, for the outbound-email path."""

    raw: dict[str, Any] = Field(default_factory=dict)
    """The source's untransformed payload."""


@runtime_checkable
class Source(Protocol):
    """A place jobs are discovered.

    Discovery is HTTP only: a Source must never drive a browser, log in, or
    touch an authenticated session. Only the apply layer is allowed near a
    browser profile, and keeping that line sharp is what stops a discovery run
    from burning the one LinkedIn session the user has.
    """

    name: str
    """Stable identifier written into ``RawJob.source``."""

    def search(
        self,
        *,
        terms: list[str],
        locations: list[str],
        hours_old: int | None = None,
        limit: int | None = None,
    ) -> list[RawJob]:
        """Return ads matching the terms. Best-effort: partial results beat none."""
        ...


class ApplyOutcome(str, Enum):
    """How an application attempt ended.

    A closed set rather than free strings so the dashboard, the guardrails and
    the appliers cannot drift apart on spelling.
    """

    SUBMITTED = "submitted"
    """Really sent. Only reachable with ALLOW_LIVE_SUBMIT on."""

    DRY_RUN = "dry_run"
    """Form filled and verified, submit deliberately not pressed."""

    BLOCKED = "blocked"
    """Guardrails refused: cap, window, kill switch, or already applied."""

    ABSTAINED = "abstained"
    """A screening answer could not be resolved from the answer bank."""

    FAILED = "failed"
    """The attempt broke. Never silent — ``failure_reason`` says why."""


class ApplyResult(BaseModel):
    """The audit record of one application attempt.

    Every field exists because something after the fact needs it: the dashboard
    needs the outcome, the answer bank needs what was answered, the "did it
    attach the right file" post-mortem needs the readback, and Telegram needs
    ``needs_answer`` to know what to ask the user.
    """

    ok: bool
    outcome: ApplyOutcome

    failure_reason: str | None = None
    """Loud, human-readable reason for anything that is not SUBMITTED/DRY_RUN."""

    answers_given: dict[str, Any] = Field(default_factory=dict)
    """Question -> answer actually entered, so a wrong answer is traceable."""

    attachment_readback: str | None = None
    """Filename read back off the form after upload. See ``Applier.apply``."""

    screenshot_pre: Path | None = None
    screenshot_post: Path | None = None

    needs_answer: str | None = None
    """The unanswerable question, when ``outcome`` is ABSTAINED.

    One question, not all of them, even when the form abstained on several. The
    reply channel answers one question per job (``/answer <job_id> <text>``), so
    asking three at once would collect one answer and silently discard two. The
    job is re-queued after each answer and re-parks on the next unresolved
    question, which walks the whole form one verified answer at a time.
    """

    needs_answer_choices: list[str] = Field(default_factory=list)
    """The form's own options for ``needs_answer``, when it was a closed list.

    Sent with the question so the reply matches a value the form will accept —
    a free-text "yes, full working rights" against a strict dropdown resolves to
    nothing and re-parks the job.
    """


@runtime_checkable
class Applier(Protocol):
    """Drives one platform's application form.

    Two rules bind every implementation, and neither has an exception:

    1. Call ``guardrails.check_can_submit()`` immediately before submitting.
       Not at the top of the method — state (caps, the STOP file, the time
       window) can change while a long form is being filled.
    2. Read the attachment filename back off the page after uploading and put
       it in ``ApplyResult.attachment_readback``. LinkedIn silently reuses a
       stale upload, so "we called set_input_files" is not evidence that the
       right resume is attached.
    """

    platform: str
    """Stable platform identifier, e.g. "linkedin"."""

    def can_handle(self, job: Any) -> bool:
        """Whether this applier owns ``job`` (a ``backend.models`` Job row)."""
        ...

    def apply(
        self,
        page: Any,
        job: Any,
        documents: Any,
        campaign: Any,
    ) -> ApplyResult:
        """Fill and finish one application.

        Args:
            page: The live Playwright page, already on an authenticated session.
            job: The Job row being applied to.
            documents: The built, parse-gated documents to attach.
            campaign: The Campaign row supplying answers, templates and caps.
        """
        ...


@runtime_checkable
class LLMClient(Protocol):
    """The only way this codebase talks to a model.

    ``model`` is required on purpose: it forces the caller to pass a
    ``settings.llm_model_*`` value rather than letting a default hide a
    hardcoded model id somewhere in the tree.
    """

    def complete(
        self,
        prompt: str,
        *,
        model: str,
        purpose: str,
        system: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        job_id: int | None = None,
    ) -> str:
        """Free-text completion."""
        ...

    def complete_json(
        self,
        prompt: str,
        *,
        model: str,
        purpose: str,
        schema: dict[str, Any],
        system: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        job_id: int | None = None,
    ) -> dict[str, Any]:
        """Completion constrained to a JSON object satisfying ``schema``."""
        ...

    def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
        purpose: str = "embedding",
    ) -> list[list[float]]:
        """Embed ``texts``, returning one vector per input in the same order."""
        ...
