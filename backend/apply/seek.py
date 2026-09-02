"""Seek Quick Apply. Step logic only — every selector lives in site knowledge.

The sequence — enumerate, resolve, abort on abstention, attach, read back,
screenshot, gate, submit, confirm, audit — lives in ``backend.apply.flow`` and
is not repeated here. If this file ever grows a submit decision, that is a bug.

NO SELECTORS IN THIS FILE. Where things are on Seek is data, in
``data/siteknowledge/seek/``, seeded from ``backend/siteknowledge/defaults/``.
Each element carries several ordered strategies and heals itself when one stops
working; see the ``backend.siteknowledge`` docstring. A Seek redesign is a JSON
edit, not a code change, and the flow variants Seek actually serves accumulate
in ``flows.json`` as they are observed.

THE STRATEGIES ARE STILL UNVERIFIED. They were seeded from the previous
hardcoded selectors, which were themselves written without access to the live
site. The structure is what changed here; confirming the values needs the HAR
capture in ``backend/apply/har.py``.
"""

from __future__ import annotations

from typing import Any

from backend.apply import formdom
from backend.apply.draft import FormField
from backend.logging_setup import get_logger
from backend.models import ApplyType, Document, Job
from backend.siteknowledge import SiteKnowledge, load

log = get_logger(__name__)

__all__ = ["SeekApplier"]


class SeekApplier:
    """Implements the adapter contract in ``backend.apply.flow`` for Seek."""

    platform = "seek"

    def __init__(self, knowledge: SiteKnowledge | None = None) -> None:
        #: Injectable so tests can supply a knowledge file without touching
        #: the real one, and so a future region (Seek NZ) can share this class
        #: with a different set of strategies.
        self.knowledge = knowledge if knowledge is not None else load(self.platform)
        self._steps_seen: list[list[FormField]] = []

    def can_handle(self, job: Job) -> bool:
        return job.source == "seek" and job.apply_type in {
            ApplyType.QUICK_APPLY,
            ApplyType.UNKNOWN,
        }

    # -- navigation ---------------------------------------------------------

    def open(self, page: Any, job: Job) -> None:
        page.goto(job.url, wait_until="domcontentloaded")
        self._steps_seen = []
        self.knowledge.resolve(page, "apply_button").click()
        page.wait_for_load_state("domcontentloaded")

    def detect_redirect(self, page: Any) -> bool:
        """Whether the apply flow left Seek for the employer's own system.

        The domains come from the board registry rather than a literal here:
        session checking, mail attribution and this all need the same list, and
        a fourth copy is a fourth thing to update when Seek adds a host.
        """
        from backend.boards import board

        entry = board(self.platform)
        domains = entry.domains if entry else ()
        try:
            url = page.url or ""
        except Exception:  # noqa: BLE001
            return False
        return not any(domain in url for domain in domains)

    # -- fields -------------------------------------------------------------

    def enumerate_fields(self, page: Any, step: int) -> list[FormField]:
        """Report the fields on the current step.

        Seek puts screening questions on their own step, so the flow's step
        loop is what walks them — this only describes what is visible now. Each
        step is retained so ``observe_flow`` can fingerprint the whole sequence
        once the flow finishes.
        """
        fields = formdom.enumerate_form_fields(
            page,
            step,
            # Seek's `name` is the meaningful one; `data-automation` is the
            # fallback because some controls carry only that.
            identifier_attributes=("name", "id", "data-automation"),
        )
        self._steps_seen.append(fields)
        return fields

    def fill_field(self, page: Any, field: FormField, value: str) -> None:
        formdom.fill(page, field, value)

    # -- attachments --------------------------------------------------------

    def upload_slots(self, fields: list[FormField]) -> int:
        """How many upload slots the form offers.

        Seek's cover letter is usually a textarea rather than a second upload,
        so a single file slot is the common case — which is exactly what
        combined.pdf exists for.
        """
        return sum(1 for field in fields if field.kind == "file") or 1

    def attach(self, page: Any, documents: list[Document]) -> None:
        """Upload the freshly built documents.

        Seek offers "use an existing resume" as well as upload. Upload is
        chosen deliberately: the whole document pipeline exists to tailor a
        resume to this job and prove it parses, and selecting a stored file
        would silently discard all of that and send whatever was there before.
        """
        upload_option = self.knowledge.resolve(page, "resume_upload_radio")
        if upload_option is not None:
            upload_option.click()

        # visible=False: file inputs are hidden behind a styled button.
        file_input = self.knowledge.resolve(page, "resume_file_input", visible=False)
        resume = next((d for d in documents if d.kind.value in {"resume", "combined"}), None)
        if resume is None:
            raise RuntimeError("no resume or combined document to attach")
        file_input.set_input_files(resume.path)

        letter = next((d for d in documents if d.kind.value == "cover_letter"), None)
        if letter is None:
            return

        # Prefer the textarea: a pasted letter renders in Seek's own UI, which
        # is what the recruiter reads first.
        textarea = self.knowledge.resolve(page, "cover_letter_textarea")
        if textarea is not None:
            text = (letter.parse_report or {}).get("cover_letter_text", "")
            if text:
                textarea.fill(text)
                return

        letter_input = self.knowledge.resolve(
            page, "cover_letter_file_input", visible=False
        )
        if letter_input is not None:
            letter_input.set_input_files(letter.path)

    def read_back_attachments(self, page: Any) -> list[str]:
        """Read the filenames the form says are attached. Mandatory."""
        return self.knowledge.read_all_text(page, "attachment_readback")

    # -- steps --------------------------------------------------------------

    def is_last_step(self, page: Any, fields: list[FormField]) -> bool:
        return self.knowledge.present(page, "submit_button")

    def advance(self, page: Any) -> None:
        self.knowledge.resolve(page, "continue_button").click()
        page.wait_for_load_state("domcontentloaded")

    def submit(self, page: Any) -> None:
        self.knowledge.resolve(page, "submit_button").click()

    def confirmed(self, page: Any) -> bool:
        """Detect the confirmation state. A returned click is not evidence."""
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception as exc:  # noqa: BLE001
            log.debug("networkidle_timeout", error=str(exc)[:100])

        confirmed = self.knowledge.present(page, "confirmation", timeout_ms=8000)
        if confirmed:
            self.knowledge.observe_flow(self._steps_seen)
        self.knowledge.save()
        return confirmed

