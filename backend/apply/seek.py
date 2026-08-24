"""Seek Quick Apply. Selectors and step logic ONLY.

The sequence — enumerate, resolve, abort on abstention, attach, read back,
screenshot, gate, submit, confirm, audit — lives in ``backend.apply.flow`` and
is not repeated here. If this file ever grows a submit decision, that is a bug.

SELECTORS ARE UNVERIFIED. seek.com.au is unreachable from the environment this
was written in (blocked by network policy), so none of the selectors below were
confirmed against the live site. Each carries a confidence note. Verify them
with the HAR workflow in ``backend/apply/har.py`` before enabling live submit —
the procedure is in NOTES.md.
"""

from __future__ import annotations

from typing import Any

from backend.apply.draft import FormField
from backend.logging_setup import get_logger
from backend.models import ApplyType, Document, Job

log = get_logger(__name__)

__all__ = ["SELECTORS", "SeekApplier"]


# Multiple candidates per element, tried in order. A single brittle selector is
# how these adapters rot: Seek reshuffles class names regularly, but its
# data-automation attributes have been comparatively stable, so those lead.
SELECTORS: dict[str, tuple[str, ...]] = {
    # confidence: high — data-automation is Seek's own test hook
    "apply_button": (
        "[data-automation='job-detail-apply']",
        "a[data-automation='job-detail-apply']",
        "button:has-text('Quick apply')",
        "a:has-text('Quick apply')",
    ),
    # confidence: medium
    "resume_upload_radio": (
        "[data-automation='resume-upload-radio']",
        "input[type='radio'][value='upload']",
        "label:has-text('Upload a new resume')",
    ),
    # confidence: medium
    "resume_existing_radio": (
        "[data-automation='resume-select-radio']",
        "input[type='radio'][value='existing']",
    ),
    # confidence: medium
    "resume_file_input": (
        "input[type='file'][data-automation='resume-file-input']",
        "input[type='file'][accept*='pdf']",
        "input[type='file']",
    ),
    # confidence: medium — Seek's cover letter is an editable textarea, which is
    # why the generated text is written rather than uploaded where possible.
    "cover_letter_textarea": (
        "textarea[data-automation='coverLetterTextArea']",
        "textarea[name='coverLetter']",
        "textarea[aria-label*='cover letter' i]",
    ),
    "cover_letter_write_option": (
        "[data-automation='coverLetterWrite']",
        "label:has-text('Write a cover letter')",
    ),
    "cover_letter_file_input": ("input[type='file'][data-automation='coverLetterFileInput']",),
    # confidence: low — step containers change often
    "form_fields": (
        "[data-automation='questionnaire'] .question",
        "form [role='group']",
        "form .question",
    ),
    "continue_button": (
        "[data-automation='continue-button']",
        "button:has-text('Continue')",
        "button:has-text('Next')",
    ),
    "submit_button": (
        "[data-automation='review-submit-application']",
        "button:has-text('Submit application')",
        "button:has-text('Submit')",
    ),
    # confidence: medium
    "confirmation": (
        "[data-automation='application-success']",
        "text=/application (has been )?(sent|submitted)/i",
        "text=/you have applied/i",
    ),
}


def _first_visible(page: Any, key: str, timeout_ms: int = 2500) -> Any | None:
    """The first candidate selector that is actually on the page."""
    for selector in SELECTORS.get(key, ()):
        try:
            locator = page.locator(selector).first
            if locator.is_visible(timeout=timeout_ms):
                return locator
        except Exception as exc:  # noqa: BLE001 - absence is the normal case
            log.debug("selector_absent", key=key, selector=selector, error=str(exc)[:100])
    return None


def _first_present(page: Any, key: str) -> Any | None:
    """Like ``_first_visible`` but for inputs that are attached yet hidden.

    File inputs are routinely visually hidden behind a styled button, so
    ``set_input_files`` has to target them by presence rather than visibility.
    """
    for selector in SELECTORS.get(key, ()):
        try:
            locator = page.locator(selector).first
            if locator.count() > 0:
                return locator
        except Exception as exc:  # noqa: BLE001
            log.debug("selector_absent", key=key, selector=selector, error=str(exc)[:100])
    return None


class SeekApplier:
    """Implements the adapter contract in ``backend.apply.flow`` for Seek."""

    platform = "seek"

    def can_handle(self, job: Job) -> bool:
        return job.source == "seek" and job.apply_type in {
            ApplyType.QUICK_APPLY,
            ApplyType.UNKNOWN,
        }

    # -- navigation ---------------------------------------------------------

    def open(self, page: Any, job: Job) -> None:
        page.goto(job.url, wait_until="domcontentloaded")
        button = _first_visible(page, "apply_button")
        if button is None:
            raise RuntimeError("no Quick Apply control on this listing")
        button.click()
        page.wait_for_load_state("domcontentloaded")

    def detect_redirect(self, page: Any) -> bool:
        """Whether the apply flow left Seek for the employer's own system."""
        try:
            return "seek.com.au" not in (page.url or "")
        except Exception:  # noqa: BLE001
            return False

    # -- fields -------------------------------------------------------------

    def enumerate_fields(self, page: Any, step: int) -> list[FormField]:
        """Report the fields on the current step.

        Seek puts screening questions on their own step, so the flow's step
        loop is what walks them — this method only describes what is visible
        right now.
        """
        fields: list[FormField] = []

        for handle in page.locator("input, textarea, select").all():
            try:
                if not handle.is_visible(timeout=500):
                    continue
                input_type = (handle.get_attribute("type") or "text").lower()
                identifier = (
                    handle.get_attribute("name")
                    or handle.get_attribute("id")
                    or handle.get_attribute("data-automation")
                    or ""
                )
                if not identifier:
                    continue

                label = (
                    handle.get_attribute("aria-label")
                    or handle.get_attribute("placeholder")
                    or self._label_for(page, identifier)
                    or identifier
                )
                tag = handle.evaluate("el => el.tagName.toLowerCase()")

                kind = {
                    "file": "file",
                    "radio": "radio",
                    "checkbox": "checkbox",
                }.get(input_type, "textarea" if tag == "textarea" else "text")
                if tag == "select":
                    kind = "select"

                choices: list[str] = []
                if kind == "select":
                    choices = [
                        option.strip()
                        for option in handle.locator("option").all_inner_texts()
                        if option.strip()
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
            except Exception as exc:  # noqa: BLE001 - one odd control, not the form
                log.debug("field_enumeration_skipped", error=str(exc)[:120])

        return fields

    @staticmethod
    def _label_for(page: Any, identifier: str) -> str | None:
        try:
            label = page.locator(f"label[for='{identifier}']").first
            if label.count() > 0:
                return label.inner_text()
        except Exception:  # noqa: BLE001
            return None
        return None

    def fill_field(self, page: Any, field: FormField, value: str) -> None:
        locator = page.locator(f"[name='{field.identifier}'], #{field.identifier}").first
        if field.kind == "select":
            locator.select_option(label=value)
        elif field.kind in {"radio", "checkbox"}:
            page.locator(
                f"[name='{field.identifier}'][value='{value}'], "
                f"label:has-text('{value}') input[name='{field.identifier}']"
            ).first.check()
        else:
            locator.fill(value)

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
        upload_option = _first_visible(page, "resume_upload_radio")
        if upload_option is not None:
            upload_option.click()

        file_input = _first_present(page, "resume_file_input")
        if file_input is None:
            raise RuntimeError("no resume file input found")

        resume = next((d for d in documents if d.kind.value in {"resume", "combined"}), None)
        if resume is None:
            raise RuntimeError("no resume or combined document to attach")
        file_input.set_input_files(resume.path)

        letter = next((d for d in documents if d.kind.value == "cover_letter"), None)
        if letter is None:
            return

        # Prefer the textarea: a pasted letter renders in Seek's own UI, which
        # is what the recruiter reads first.
        textarea = _first_visible(page, "cover_letter_textarea")
        if textarea is not None:
            text = (letter.parse_report or {}).get("cover_letter_text", "")
            if text:
                textarea.fill(text)
                return

        letter_input = _first_present(page, "cover_letter_file_input")
        if letter_input is not None:
            letter_input.set_input_files(letter.path)

    def read_back_attachments(self, page: Any) -> list[str]:
        """Read the filenames the form says are attached. Mandatory."""
        names: list[str] = []
        for selector in (
            "[data-automation='resume-file-name']",
            "[data-automation='uploaded-file-name']",
            ".file-name",
            "[class*='fileName']",
        ):
            try:
                for text in page.locator(selector).all_inner_texts():
                    cleaned = text.strip()
                    if cleaned:
                        names.append(cleaned)
            except Exception as exc:  # noqa: BLE001
                log.debug("readback_selector_absent", selector=selector, error=str(exc)[:100])
        return names

    # -- steps --------------------------------------------------------------

    def is_last_step(self, page: Any, fields: list[FormField]) -> bool:
        return _first_visible(page, "submit_button") is not None

    def advance(self, page: Any) -> None:
        button = _first_visible(page, "continue_button")
        if button is None:
            raise RuntimeError("no continue control on this step")
        button.click()
        page.wait_for_load_state("domcontentloaded")

    def submit(self, page: Any) -> None:
        button = _first_visible(page, "submit_button")
        if button is None:
            raise RuntimeError("no submit control")
        button.click()

    def confirmed(self, page: Any) -> bool:
        """Detect the confirmation state. A returned click is not evidence."""
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception as exc:  # noqa: BLE001
            log.debug("networkidle_timeout", error=str(exc)[:100])
        return _first_visible(page, "confirmation", timeout_ms=8000) is not None
