"""LinkedIn Easy Apply. Step logic only — every selector lives in site knowledge.

The most failure-prone surface in the system, and the one with the most
expensive failure mode: a restricted LinkedIn account is not recoverable by
retrying. Three behaviours here exist specifically because of that:

* **Never hardcode a step count.** The modal has anywhere from two to six steps
  depending on the posting. The loop runs until Submit appears or a step
  repeats. The structures actually observed are fingerprinted and accumulated in
  ``flows.json``, so a five-step employer seen twice is recognised the second
  time rather than rediscovered.
* **Filename read-back is mandatory.** LinkedIn silently reuses a previous
  upload when its document library is full or the upload quietly fails. The
  read-back is the only thing between the user and sending last month's resume.
* **Prune the document library.** LinkedIn keeps four documents; past that,
  uploads start silently reusing.

NO SELECTORS IN THIS FILE. Where things are on LinkedIn is data, in
``data/siteknowledge/linkedin/``. That matters more here than anywhere: field
ids embed the job URN and Ember's per-render counter, so the identifiers are
stored as wildcard patterns rather than literals captured from one job. See
``backend.siteknowledge``.

THE STRATEGIES ARE STILL UNVERIFIED — seeded from the previous hardcoded
selectors, which were themselves written without access to the live site.
Confirming them needs the HAR capture.
"""

from __future__ import annotations

from typing import Any

from backend.apply import formdom
from backend.apply.draft import FormField
from backend.apply.session import has_restriction_notice
from backend.logging_setup import get_logger
from backend.models import ApplyType, Document, Job
from backend.siteknowledge import ElementNotFound, SiteKnowledge, load

log = get_logger(__name__)

__all__ = ["MAX_LIBRARY_DOCUMENTS", "LinkedInApplier", "decide_upload_slots"]


# LinkedIn's document library holds this many; beyond it, uploads silently
# reuse an existing file instead of adding a new one.
MAX_LIBRARY_DOCUMENTS = 4


def decide_upload_slots(fields: list[FormField]) -> int:
    """How many upload slots this modal offers.

    A pure function of the enumerated fields so it is unit-testable without a
    browser — which matters, because getting it wrong means either sending a
    resume with no cover letter or sending the combined PDF into a slot the
    recruiter expects to hold only a resume.

    Two or more slots -> resume and cover letter uploaded separately.
    One slot -> combined.pdf, which is precisely why it is built.
    """
    file_fields = [f for f in fields if f.kind == "file"]
    if not file_fields:
        return 0

    labelled_cover = any(
        "cover" in (f.label or f.identifier or "").casefold() for f in file_fields
    )
    if labelled_cover:
        return max(2, len(file_fields))
    return len(file_fields)


class LinkedInApplier:
    """Implements the adapter contract in ``backend.apply.flow`` for LinkedIn."""

    platform = "linkedin"

    def __init__(self, knowledge: SiteKnowledge | None = None) -> None:
        self.knowledge = knowledge if knowledge is not None else load(self.platform)
        self._steps_seen: list[list[FormField]] = []

    def can_handle(self, job: Job) -> bool:
        """Easy Apply only. Anything else belongs to a different path."""
        return job.source == "linkedin" and job.apply_type is ApplyType.EASY_APPLY

    # -- navigation ---------------------------------------------------------

    def open(self, page: Any, job: Job) -> None:
        page.goto(job.url, wait_until="domcontentloaded")
        self._steps_seen = []

        if self.detect_restriction(page):
            raise RuntimeError("LinkedIn restriction notice on the job page")

        self.knowledge.resolve(page, "easy_apply_button").click()

        if not self.knowledge.present(page, "modal", timeout_ms=8000):
            raise RuntimeError("Easy Apply modal did not open")

    def detect_restriction(self, page: Any) -> bool:
        """An account restriction. The most serious state this file detects.

        Reported to the flow, which trips the global halt through guardrails —
        never handled locally, because the correct response is stopping
        everything, not skipping one job.

        The selectors live in the board registry, not here: session checking
        and applying both need them, and the two copies had already drifted
        apart on the wording of the identity-verification notice.
        """
        return has_restriction_notice(page, self.platform)

    def detect_redirect(self, page: Any) -> bool:
        """A listing that claims Easy Apply and then sends you off-site.

        The flow marks these ``manual_only`` rather than fighting them.
        """
        from backend.boards import board

        entry = board(self.platform)
        domains = entry.domains if entry else ()
        try:
            url = page.url or ""
        except Exception:  # noqa: BLE001
            return False
        if not any(domain in url for domain in domains):
            return True
        # The modal never opened but the page navigated away from the job view.
        return not self.knowledge.present(page, "modal", timeout_ms=1000) and (
            "/jobs/" not in url
        )

    # -- fields -------------------------------------------------------------

    def enumerate_fields(self, page: Any, step: int) -> list[FormField]:
        """Describe the current modal step.

        Scoped to the modal where one is present: LinkedIn's job page behind the
        overlay is full of its own inputs — search boxes, message composers —
        and enumerating those would feed the answer bank questions no employer
        asked.
        """
        try:
            scope = self.knowledge.resolve(page, "modal", timeout_ms=5000) or page
        except ElementNotFound:
            scope = page

        fields = formdom.enumerate_form_fields(
            page if scope is page else scope,
            step,
            # `id` first: LinkedIn's ids carry the URN and are what its own
            # labels point at, while `name` is frequently absent.
            identifier_attributes=("id", "name", "aria-describedby"),
            visibility_timeout_ms=400,
        )
        self._steps_seen.append(fields)

        # Recognised on repeat, walked cautiously when new. An unknown shape is
        # not an error — a five-step Easy Apply is a variant we had not seen, not
        # a failure — but it is worth saying so in the log, because "the modal
        # got longer than we expected" and "a validation error is silently
        # blocking progress" look identical until someone reads the trail.
        if self.knowledge.known_variant(self._steps_seen) is None:
            log.info(
                "easy_apply_structure_unrecognised",
                platform=self.platform,
                steps_so_far=len(self._steps_seen),
                note="walking cautiously; recorded on completion",
            )
        return fields

    def fill_field(self, page: Any, field: FormField, value: str) -> None:
        formdom.fill(page, field, value)

    # -- attachments --------------------------------------------------------

    def upload_slots(self, fields: list[FormField]) -> int:
        return decide_upload_slots(fields)

    def attach(self, page: Any, documents: list[Document]) -> None:
        self.prune_document_library(page)

        if len(documents) == 1:
            target = self.knowledge.resolve(
                page, "resume_file_input", visible=False
            ) or self.knowledge.resolve(page, "file_input", visible=False)
            target.set_input_files(documents[0].path)
            return

        for document in documents:
            key = (
                "cover_letter_file_input"
                if document.kind.value == "cover_letter"
                else "resume_file_input"
            )
            target = self.knowledge.resolve(
                page, key, visible=False
            ) or self.knowledge.resolve(page, "file_input", visible=False)
            target.set_input_files(document.path)

    def read_back_attachments(self, page: Any) -> list[str]:
        """Read the filenames LinkedIn displays. MANDATORY — see the docstring."""
        try:
            return self.knowledge.read_all_text(page, "uploaded_filename")
        except ElementNotFound:
            log.error("attachment_readback_empty", platform=self.platform)
            raise

    def prune_document_library(self, page: Any, *, keep: int = MAX_LIBRARY_DOCUMENTS - 1) -> int:
        """Delete the oldest library entries so a fresh upload is not reused.

        Guarded so it can never remove more than the overflow: ``keep`` is a
        floor, deletions are counted, and each one is logged. Silently deleting
        a user's documents would be worse than a failed upload.
        """
        # The cap is a learned fact about LinkedIn, so it is read from the
        # quirks file: if LinkedIn changes it, that is a JSON edit like every
        # other piece of site knowledge. MAX_LIBRARY_DOCUMENTS stays as the
        # fallback for a knowledge file that predates the quirk.
        quirk = self.knowledge.quirk("library_holds_four")
        capacity = int((quirk or {}).get("capacity", MAX_LIBRARY_DOCUMENTS))
        keep = max(0, min(keep, capacity))
        card = self.knowledge.elements.get("library_document_card")
        delete = self.knowledge.elements.get("library_delete_button")
        if card is None or delete is None:  # pragma: no cover - structural
            return 0

        card_selector = card.ordered()[0].selector
        delete_selector = delete.ordered()[0].selector

        deleted = 0
        try:
            total = page.locator(card_selector).count()
        except Exception as exc:  # noqa: BLE001 - no library UI on this modal
            log.debug("document_library_absent", error=str(exc)[:120])
            return 0

        overflow = max(0, total - keep)
        for _ in range(overflow):
            try:
                button = page.locator(card_selector).last.locator(delete_selector).first
                if button.count() == 0:
                    break
                button.click()
                deleted += 1
                log.info("document_library_pruned", deleted=deleted, kept=keep)
            except Exception as exc:  # noqa: BLE001
                log.warning("document_library_prune_failed", error=str(exc)[:150])
                break
        return deleted

    # -- steps --------------------------------------------------------------

    def is_last_step(self, page: Any, fields: list[FormField]) -> bool:
        """Submit is visible. Never a step count — the modal length varies."""
        return self.knowledge.present(page, "submit_button", timeout_ms=1500)

    def advance(self, page: Any) -> None:
        for key in ("next_button", "review_button"):
            try:
                self.knowledge.resolve(page, key).click()
                return
            except ElementNotFound:
                continue
        raise ElementNotFound(self.platform, "next_button|review_button", [])

    def submit(self, page: Any) -> None:
        self.knowledge.resolve(page, "submit_button").click()

    def confirmed(self, page: Any) -> bool:
        confirmed = self.knowledge.present(page, "confirmation", timeout_ms=10000)
        if confirmed:
            # Only record a variant the whole way through. A structure captured
            # from a run that failed halfway is not a flow LinkedIn serves, it
            # is a flow we failed to walk, and storing it would teach the wrong
            # shape.
            variant = self.knowledge.observe_flow(self._steps_seen)
            log.info(
                "easy_apply_variant",
                fingerprint=variant.fingerprint,
                steps=variant.step_count,
                seen_before=variant.observed_count - 1,
            )
        self.knowledge.save()
        return confirmed
