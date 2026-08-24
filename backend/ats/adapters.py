"""ATS adapters. Selectors and step logic; the flow is shared.

Every adapter here satisfies the same contract as ``backend.apply.seek`` and
``backend.apply.linkedin`` — the one defined in ``backend.apply.flow``. None of
them decides whether to submit; that stays in ``guardrails.check_can_submit``,
called once, inside the flow.

``GenericAtsApplier`` is the fallback for a platform with no dedicated adapter.
It enumerates through the accessibility tree, consults the form-map cache, and
asks the model only about fields the cache does not already know.

SELECTORS ARE UNVERIFIED against live sites — the same network restriction that
applied to Seek and LinkedIn. Use the HAR workflow in ``backend.apply.har`` to
confirm them before enabling live submit.
"""

from __future__ import annotations

from typing import Any

from backend.apply.draft import FormField
from backend.ats.detect import detect_from_url
from backend.ats.generic import (
    CaptchaDetected,
    detect_captcha,
    fields_from_accessibility,
)
from backend.logging_setup import get_logger
from backend.models import ApplyType, Document, Job

log = get_logger(__name__)

__all__ = ["ATS_APPLIERS", "AtsApplier", "GenericAtsApplier", "build_ats_appliers"]


# Per-platform selector sets. Multiple candidates each, most durable first —
# these platforms all expose stable data attributes, which outlive class names.
SELECTORS: dict[str, dict[str, tuple[str, ...]]] = {
    "jobadder": {
        "apply_button": ("a.ja-apply", "a:has-text('Apply')", "button:has-text('Apply')"),
        "submit_button": ("button[type='submit']", "button:has-text('Submit application')"),
        "file_input": ("input[type='file']",),
        "confirmation": (
            "text=/thank you for (your )?applic/i",
            "text=/application (received|submitted)/i",
        ),
    },
    "pageup": {
        "apply_button": (
            "a#apply-button",
            "a:has-text('Apply now')",
            "button:has-text('Apply')",
        ),
        "submit_button": ("input[type='submit']", "button:has-text('Submit')"),
        "file_input": ("input[type='file']",),
        "confirmation": (
            "text=/thank you for (your )?applic/i",
            "text=/successfully submitted/i",
        ),
    },
    "smartrecruiters": {
        "apply_button": ("button:has-text('I'm interested')", "a:has-text('Apply')"),
        "submit_button": ("button[type='submit']", "button:has-text('Submit')"),
        "file_input": ("input[type='file']",),
        "confirmation": ("text=/thank you/i", "text=/application (sent|received)/i"),
    },
    "greenhouse": {
        "apply_button": ("a#apply_button", "button:has-text('Apply')"),
        "submit_button": ("input#submit_app", "button:has-text('Submit Application')"),
        "file_input": ("input[type='file']",),
        "confirmation": ("text=/thank you for applying/i", "#application_confirmation"),
    },
    "lever": {
        "apply_button": ("a.postings-btn", "a:has-text('Apply')"),
        "submit_button": ("button:has-text('Submit application')", "button[type='submit']"),
        "file_input": ("input[type='file'][name='resume']", "input[type='file']"),
        "confirmation": ("text=/thank you for applying/i", ".application-confirmation"),
    },
    "workday": {
        "apply_button": (
            "a[data-automation-id='adventureButton']",
            "button:has-text('Apply')",
        ),
        "submit_button": (
            "button[data-automation-id='bottom-navigation-next-button']",
            "button:has-text('Submit')",
        ),
        "file_input": ("input[type='file']",),
        "confirmation": (
            "text=/submitted/i",
            "[data-automation-id='successMessage']",
        ),
    },
}

# Platforms whose forms are the same everywhere, so a learned map is safe to
# share at the platform tier rather than per company.
SHAREABLE_PLATFORMS = frozenset({"greenhouse", "lever", "smartrecruiters", "workable"})


def _first_visible(page: Any, selectors: tuple[str, ...], timeout_ms: int = 2500) -> Any | None:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.is_visible(timeout=timeout_ms):
                return locator
        except Exception as exc:  # noqa: BLE001 - absence is the normal answer
            log.debug("selector_absent", selector=selector, error=str(exc)[:100])
    return None


def _first_present(page: Any, selectors: tuple[str, ...]) -> Any | None:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0:
                return locator
        except Exception as exc:  # noqa: BLE001
            log.debug("selector_absent", selector=selector, error=str(exc)[:100])
    return None


class GenericAtsApplier:
    """One adapter, configured per platform. Selectors are data.

    A dedicated class per ATS would be six copies of the same twelve methods
    differing only in their selector dict — the duplication Claude.md forbids.
    """

    def __init__(self, platform_key: str) -> None:
        self.platform = platform_key
        self._selectors = SELECTORS.get(platform_key, {})
        self._last_snapshot: dict[str, Any] | None = None

    # -- selection ----------------------------------------------------------

    def can_handle(self, job: Job) -> bool:
        """Whether this job's URL belongs to this platform."""
        if job.apply_type not in {ApplyType.EXTERNAL, ApplyType.UNKNOWN}:
            return False
        detection = detect_from_url(job.url)
        return detection.key == self.platform

    def _select(self, key: str) -> tuple[str, ...]:
        return self._selectors.get(key, ())

    # -- navigation ---------------------------------------------------------

    def open(self, page: Any, job: Job) -> None:
        page.goto(job.url, wait_until="domcontentloaded")

        button = _first_visible(page, self._select("apply_button"))
        if button is not None:
            button.click()
            page.wait_for_load_state("domcontentloaded")

        self._check_captcha(page)

    def _check_captcha(self, page: Any) -> None:
        """A CAPTCHA is a hard stop. No solving services, ever."""
        try:
            snapshot = page.accessibility.snapshot()
        except Exception:  # noqa: BLE001
            snapshot = None
        try:
            html = page.content()
        except Exception:  # noqa: BLE001
            html = None

        if detect_captcha(snapshot, html):
            log.error("captcha_detected", platform=self.platform)
            raise CaptchaDetected(
                f"{self.platform} is showing a CAPTCHA. The job is parked for you to "
                "complete by hand — this system does not use CAPTCHA-solving services."
            )

    def detect_restriction(self, page: Any) -> bool:
        return False

    # -- fields -------------------------------------------------------------

    def enumerate_fields(self, page: Any, step: int) -> list[FormField]:
        """Read the form from the accessibility tree.

        Far cheaper in tokens than raw HTML, and semantically better: the
        accessible name of a field is the label a human reads.
        """
        self._check_captcha(page)
        try:
            snapshot = page.accessibility.snapshot()
        except Exception as exc:  # noqa: BLE001
            log.warning("accessibility_snapshot_failed", error=str(exc)[:150])
            snapshot = None

        self._last_snapshot = snapshot
        fields = fields_from_accessibility(snapshot)
        for field in fields:
            field.step = step
        return fields

    def fill_field(self, page: Any, field: FormField, value: str) -> None:
        """Fill by accessible name first, falling back to a selector.

        Semantic resolution first is the whole point of storing labels rather
        than CSS: an ATS release renames classes far more often than it renames
        the question a human reads.
        """
        label = field.label
        attempts = (
            lambda: page.get_by_label(label, exact=False),
            lambda: page.get_by_role(
                "textbox" if field.kind in {"text", "textarea"} else field.kind, name=label
            ),
            lambda: page.locator(f"[name='{field.identifier}'], #{field.identifier}"),
        )

        for attempt in attempts:
            try:
                locator = attempt().first
                if locator.count() == 0:
                    continue
                if field.kind == "select":
                    locator.select_option(label=value)
                elif field.kind in {"radio", "checkbox"}:
                    locator.check()
                else:
                    locator.fill(value)
                return
            except Exception as exc:  # noqa: BLE001 - try the next strategy
                log.debug("fill_attempt_failed", field=field.identifier, error=str(exc)[:100])

        raise RuntimeError(f"could not fill {field.label!r} on {self.platform}")

    # -- attachments --------------------------------------------------------

    def upload_slots(self, fields: list[FormField]) -> int:
        return sum(1 for f in fields if f.kind == "file") or 1

    def attach(self, page: Any, documents: list[Document]) -> None:
        target = _first_present(page, self._select("file_input"))
        if target is None:
            raise RuntimeError(f"no file input on {self.platform}")

        if len(documents) == 1:
            target.set_input_files(documents[0].path)
            return

        inputs = page.locator(self._select("file_input")[0])
        for index, document in enumerate(documents):
            try:
                inputs.nth(index).set_input_files(document.path)
            except Exception:  # noqa: BLE001 - fall back to the first slot
                target.set_input_files(document.path)

    def read_back_attachments(self, page: Any) -> list[str]:
        """Read the filenames the form displays. Mandatory on every platform."""
        names: list[str] = []
        for selector in (
            "[class*='file-name']",
            "[class*='fileName']",
            "[data-automation-id*='file']",
            ".attachment-name",
            "input[type='file'] + span",
        ):
            try:
                for text in page.locator(selector).all_inner_texts():
                    cleaned = text.strip()
                    if cleaned and "." in cleaned:
                        names.append(cleaned)
            except Exception as exc:  # noqa: BLE001
                log.debug("readback_selector_absent", selector=selector, error=str(exc)[:100])

        if not names:
            log.error("attachment_readback_empty", platform=self.platform)
        return names

    # -- steps --------------------------------------------------------------

    def is_last_step(self, page: Any, fields: list[FormField]) -> bool:
        return _first_visible(page, self._select("submit_button"), timeout_ms=1500) is not None

    def advance(self, page: Any) -> None:
        button = _first_visible(
            page, ("button:has-text('Next')", "button:has-text('Continue')")
        )
        if button is None:
            raise RuntimeError(f"no next control on {self.platform}")
        button.click()
        page.wait_for_load_state("domcontentloaded")

    def submit(self, page: Any) -> None:
        button = _first_visible(page, self._select("submit_button"))
        if button is None:
            raise RuntimeError(f"no submit control on {self.platform}")
        button.click()

    def confirmed(self, page: Any) -> bool:
        return _first_visible(page, self._select("confirmation"), timeout_ms=10000) is not None


# Alias so the name reads properly at call sites.
AtsApplier = GenericAtsApplier


def build_ats_appliers() -> list[GenericAtsApplier]:
    """Every ATS adapter, in Australian priority order.

    JobAdder and PageUp lead because they are what Australian employers
    actually use; Workday is last because it needs an account per company.
    """
    from backend.ats.detect import ATS_REGISTRY

    return [
        GenericAtsApplier(platform.key)
        for platform in sorted(ATS_REGISTRY, key=lambda p: p.priority)
        if platform.key in SELECTORS
    ]


ATS_APPLIERS = build_ats_appliers
