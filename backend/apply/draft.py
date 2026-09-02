"""What an application looks like before anyone decides to send it.

Assembling the whole application *before* the submit decision is what makes the
guardrails meaningful: they inspect a complete, concrete artifact — these
documents, this cover letter text, these exact answers — rather than a promise
about what the flow is going to do next.

It is also what makes a dry run useful. A draft that was built, gated and
refused still tells the user precisely what would have been sent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.apply.answers import Abstain, Answer
from backend.models import Document, DocumentKind

__all__ = ["ApplicationDraft", "FormField"]


@dataclass
class FormField:
    """One field on an application form, normalised across platforms.

    Adapters produce these; the flow reasons only about these. That is the line
    that keeps platform quirks out of the shared logic.
    """

    identifier: str
    label: str
    kind: str = "text"  # text | textarea | select | radio | checkbox | file | unknown
    required: bool = False
    choices: list[str] = field(default_factory=list)
    step: int = 0
    current_value: str | None = None


@dataclass
class ApplicationDraft:
    """A complete, inspectable application, not yet submitted."""

    job: Any
    campaign: Any = None
    platform: str = "unknown"
    score: float | None = None

    documents: list[Document] = field(default_factory=list)
    cover_letter_text: str = ""

    answers: dict[str, Answer] = field(default_factory=dict)
    abstentions: list[Abstain] = field(default_factory=list)

    fields: list[FormField] = field(default_factory=list)
    attachment_intent: dict[str, str] = field(default_factory=dict)
    """Slot name -> the filename intended for it, for the read-back assertion."""

    attachment_readback: str | None = None
    """What the form actually reported after upload. Compared against intent."""

    screenshot_pre: str | None = None
    screenshot_post: str | None = None

    form_fingerprint: str | None = None

    form_map_trusted: bool = True
    """Whether this form's shape has graduated to being filled unsupervised.

    True by default so a platform with a dedicated adapter — Seek, LinkedIn,
    a known ATS — is unaffected: its fields are not learned by a model and there
    is nothing to graduate. Only a form mapped by ``ats.generic`` sets this
    False, and only until three clean applications on the same shape.
    """
    """The form-map fingerprint this draft's fields hashed to, when one was used.

    Carried so the outcome can be reported back to the cache: a map only earns
    trust by producing clean submissions, and it cannot be credited for one
    without knowing which map was in play.
    """

    @property
    def answers_given(self) -> dict[str, str]:
        """Question -> answer actually entered, for the audit record."""
        return {question: answer.value for question, answer in self.answers.items()}

    def document(self, kind: DocumentKind) -> Document | None:
        return next((d for d in self.documents if d.kind == kind), None)

    def document_by(self, kind: str) -> Document | None:
        """Look a document up by its kind's string value."""
        return next((d for d in self.documents if d.kind.value == kind), None)

    @property
    def all_documents_gated(self) -> bool:
        return bool(self.documents) and all(d.parse_check_passed for d in self.documents)

    def attachment_plan(self, *, slots: int) -> list[Document]:
        """Which documents to attach given how many upload slots the form has.

        One slot gets the combined PDF — that is exactly why it is built. Two
        or more get the resume and cover letter separately, which is what
        recruiters actually prefer when the form allows it.
        """
        if slots <= 1:
            combined = self.document(DocumentKind.COMBINED)
            return [combined] if combined else []

        chosen = [
            self.document(DocumentKind.RESUME),
            self.document(DocumentKind.COVER_LETTER),
        ]
        return [d for d in chosen if d is not None]
