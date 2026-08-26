"""LinkedIn Easy Apply. Selectors and step logic ONLY.

The most failure-prone surface in the system, and the one with the most
expensive failure mode: a restricted LinkedIn account is not recoverable by
retrying. Three behaviours here exist specifically because of that:

* **Never hardcode a step count.** The modal has anywhere from two to six
  steps depending on the posting. The loop runs until Submit appears or a step
  repeats; a repeated step means a validation error is silently blocking
  progress, which the flow detects by fingerprinting each step's fields.
* **Filename read-back is mandatory.** LinkedIn silently reuses a previous
  upload when its document library is full or the upload quietly fails. The
  read-back is the only thing between the user and sending last month's resume.
* **Prune the document library.** LinkedIn keeps four documents; past that,
  uploads start silently reusing.

SELECTORS ARE UNVERIFIED — linkedin.com is unreachable from the environment
this was written in. Each carries a confidence note; verify with the HAR
workflow before enabling live submit. See NOTES.md.
"""

from __future__ import annotations

from typing import Any

from backend.apply.draft import FormField
from backend.apply.session import has_restriction_notice
from backend.logging_setup import get_logger
from backend.models import ApplyType, Document, Job

log = get_logger(__name__)

__all__ = ["MAX_LIBRARY_DOCUMENTS", "SELECTORS", "LinkedInApplier", "decide_upload_slots"]


# LinkedIn's document library holds this many; beyond it, uploads silently
# reuse an existing file instead of adding a new one.
MAX_LIBRARY_DOCUMENTS = 4


SELECTORS: dict[str, tuple[str, ...]] = {
    # confidence: high — this class has been stable for a long time
    "easy_apply_button": (
        "button.jobs-apply-button",
        "button[aria-label*='Easy Apply' i]",
        "button:has-text('Easy Apply')",
    ),
    # confidence: high
    "modal": (
        "div.jobs-easy-apply-modal",
        "div[role='dialog'][aria-labelledby*='easy-apply' i]",
        "div[data-test-modal]",
    ),
    # confidence: medium
    "next_button": (
        "button[aria-label='Continue to next step']",
        "button[aria-label*='Next' i]",
        "footer button:has-text('Next')",
    ),
    "review_button": ("button[aria-label*='Review' i]", "footer button:has-text('Review')"),
    # confidence: medium
    "submit_button": (
        "button[aria-label='Submit application']",
        "button[aria-label*='Submit' i]",
        "footer button:has-text('Submit application')",
    ),
    # confidence: medium
    "file_input": ("input[type='file']",),
    "resume_file_input": (
        "input[type='file'][name*='resume' i]",
        "input[type='file'][id*='resume' i]",
    ),
    "cover_letter_file_input": (
        "input[type='file'][name*='cover' i]",
        "input[type='file'][id*='cover' i]",
    ),
    # confidence: medium — the uploaded filename LinkedIn echoes back
    "uploaded_filename": (
        "h3.jobs-document-upload__title",
        ".jobs-document-upload-redesign-card__file-name",
        "[class*='document-upload'] [class*='file-name']",
        "[class*='jobs-document-upload'] h3",
    ),
    # confidence: low — the library UI changes often
    "library_document_card": (
        ".jobs-document-upload-redesign-card",
        "[class*='document-upload'] li",
    ),
    "library_delete_button": (
        "button[aria-label*='Delete' i]",
        "button[aria-label*='Remove' i]",
    ),
    # confidence: medium
    "confirmation": (
        "text=/your application was sent/i",
        "text=/application sent/i",
        "h2:has-text('Your application was sent')",
        "[data-test-modal] :text('Done')",
    ),
    # confidence: medium — the most serious thing this file can detect
}


def _first_visible(page: Any, key: str, timeout_ms: int = 2500) -> Any | None:
    for selector in SELECTORS.get(key, ()):
        try:
            locator = page.locator(selector).first
            if locator.is_visible(timeout=timeout_ms):
                return locator
        except Exception as exc:  # noqa: BLE001 - absence is the normal case
            log.debug("selector_absent", key=key, selector=selector, error=str(exc)[:100])
    return None


def _first_present(page: Any, key: str) -> Any | None:
    for selector in SELECTORS.get(key, ()):
        try:
            locator = page.locator(selector).first
            if locator.count() > 0:
                return locator
        except Exception as exc:  # noqa: BLE001
            log.debug("selector_absent", key=key, selector=selector, error=str(exc)[:100])
    return None


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

    def can_handle(self, job: Job) -> bool:
        """Easy Apply only. Anything else belongs to a different path."""
        return job.source == "linkedin" and job.apply_type is ApplyType.EASY_APPLY

    # -- navigation ---------------------------------------------------------

    def open(self, page: Any, job: Job) -> None:
        page.goto(job.url, wait_until="domcontentloaded")

        if self.detect_restriction(page):
            raise RuntimeError("LinkedIn restriction notice on the job page")

        button = _first_visible(page, "easy_apply_button")
        if button is None:
            raise RuntimeError("no Easy Apply button — this listing is not Easy Apply")
        button.click()

        if _first_visible(page, "modal", timeout_ms=8000) is None:
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
        try:
            url = page.url or ""
        except Exception:  # noqa: BLE001
            return False
        if "linkedin.com" not in url:
            return True
        # The modal never opened but the page navigated away from the job view.
        return _first_visible(page, "modal", timeout_ms=1000) is None and "/jobs/" not in url

    # -- fields -------------------------------------------------------------

    def enumerate_fields(self, page: Any, step: int) -> list[FormField]:
        modal = _first_visible(page, "modal", timeout_ms=5000)
        scope = modal if modal is not None else page

        fields: list[FormField] = []
        for handle in scope.locator("input, textarea, select").all():
            try:
                input_type = (handle.get_attribute("type") or "text").lower()
                is_file = input_type == "file"
                if not is_file and not handle.is_visible(timeout=400):
                    continue

                identifier = (
                    handle.get_attribute("id")
                    or handle.get_attribute("name")
                    or handle.get_attribute("aria-describedby")
                    or ""
                )
                if not identifier:
                    continue

                label = (
                    handle.get_attribute("aria-label")
                    or self._label_text(scope, identifier)
                    or identifier
                )
                tag = handle.evaluate("el => el.tagName.toLowerCase()")
                kind = (
                    "file"
                    if is_file
                    else {
                        "radio": "radio",
                        "checkbox": "checkbox",
                    }.get(input_type, "textarea" if tag == "textarea" else "text")
                )
                if tag == "select":
                    kind = "select"

                choices: list[str] = []
                if kind == "select":
                    choices = [
                        option.strip()
                        for option in handle.locator("option").all_inner_texts()
                        if option.strip() and option.strip().lower() != "select an option"
                    ]

                fields.append(
                    FormField(
                        identifier=identifier,
                        label=label.strip(),
                        kind=kind,
                        required=handle.get_attribute("required") is not None,
                        choices=choices,
                        step=step,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("field_enumeration_skipped", error=str(exc)[:120])
        return fields

    @staticmethod
    def _label_text(scope: Any, identifier: str) -> str | None:
        try:
            label = scope.locator(f"label[for='{identifier}']").first
            if label.count() > 0:
                return label.inner_text()
        except Exception:  # noqa: BLE001
            return None
        return None

    def fill_field(self, page: Any, field: FormField, value: str) -> None:
        locator = page.locator(f"#{field.identifier}, [name='{field.identifier}']").first
        if field.kind == "select":
            locator.select_option(label=value)
        elif field.kind in {"radio", "checkbox"}:
            page.locator(
                f"[name='{field.identifier}'][value='{value}'], "
                f"label:has-text('{value}')"
            ).first.check()
        else:
            locator.fill(value)

    # -- attachments --------------------------------------------------------

    def upload_slots(self, fields: list[FormField]) -> int:
        return decide_upload_slots(fields)

    def attach(self, page: Any, documents: list[Document]) -> None:
        self.prune_document_library(page)

        if len(documents) == 1:
            target = _first_present(page, "resume_file_input") or _first_present(
                page, "file_input"
            )
            if target is None:
                raise RuntimeError("no file input in the Easy Apply modal")
            target.set_input_files(documents[0].path)
            return

        for document in documents:
            key = (
                "cover_letter_file_input"
                if document.kind.value == "cover_letter"
                else "resume_file_input"
            )
            target = _first_present(page, key) or _first_present(page, "file_input")
            if target is None:
                raise RuntimeError(f"no file input for {document.kind.value}")
            target.set_input_files(document.path)

    def read_back_attachments(self, page: Any) -> list[str]:
        """Read the filenames LinkedIn displays. MANDATORY — see module docstring."""
        names: list[str] = []
        for selector in SELECTORS["uploaded_filename"]:
            try:
                for text in page.locator(selector).all_inner_texts():
                    cleaned = text.strip()
                    if cleaned:
                        names.append(cleaned)
            except Exception as exc:  # noqa: BLE001
                log.debug("readback_selector_absent", selector=selector, error=str(exc)[:100])

        if not names:
            log.error("attachment_readback_empty", platform=self.platform)
        return names

    def prune_document_library(self, page: Any, *, keep: int = MAX_LIBRARY_DOCUMENTS - 1) -> int:
        """Delete the oldest library entries so a fresh upload is not reused.

        Guarded so it can never remove more than the overflow: ``keep`` is a
        floor, deletions are counted, and each one is logged. Silently deleting
        a user's documents would be worse than a failed upload.
        """
        keep = max(0, min(keep, MAX_LIBRARY_DOCUMENTS))
        deleted = 0
        try:
            cards = page.locator(SELECTORS["library_document_card"][0])
            total = cards.count()
        except Exception as exc:  # noqa: BLE001 - no library UI on this modal
            log.debug("document_library_absent", error=str(exc)[:120])
            return 0

        overflow = max(0, total - keep)
        for _ in range(overflow):
            try:
                card = page.locator(SELECTORS["library_document_card"][0]).last
                button = card.locator(SELECTORS["library_delete_button"][0]).first
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
        return _first_visible(page, "submit_button", timeout_ms=1500) is not None

    def advance(self, page: Any) -> None:
        button = _first_visible(page, "next_button") or _first_visible(page, "review_button")
        if button is None:
            raise RuntimeError("no Next or Review control in the modal")
        button.click()

    def submit(self, page: Any) -> None:
        button = _first_visible(page, "submit_button")
        if button is None:
            raise RuntimeError("no submit control")
        button.click()

    def confirmed(self, page: Any) -> bool:
        return _first_visible(page, "confirmation", timeout_ms=10000) is not None
