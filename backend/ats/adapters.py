"""ATS adapters. Selectors and step logic; the flow is shared.

Every adapter here satisfies the same contract as ``backend.apply.seek`` and
``backend.apply.linkedin`` — the one defined in ``backend.apply.flow``. None of
them decides whether to submit; that stays in ``guardrails.check_can_submit``,
called once, inside the flow.

``GenericAtsApplier`` is the fallback for a platform with no dedicated adapter.
It enumerates through the accessibility tree, consults the form-map cache, and
asks the model only about fields the cache does not already know.

NO SELECTORS IN THIS FILE. Where things are on each ATS is data, in
``data/siteknowledge/<platform>/``, exactly as for Seek and LinkedIn — the
multi-strategy resolution and self-healing in ``backend.siteknowledge`` apply
here identically, and these platforms drift at least as often as the primary
boards do.

THE STRATEGIES ARE UNVERIFIED against live sites — the same network restriction
that applied to Seek and LinkedIn. Use the HAR workflow in
``backend.apply.har`` to confirm them before enabling live submit.
"""

from __future__ import annotations

from typing import Any

from backend.apply.draft import FormField
from backend.ats.detect import detect, detect_from_url
from backend.ats.generic import (
    CaptchaDetected,
    detect_captcha,
    fields_from_accessibility,
)
from backend.logging_setup import get_logger
from backend.siteknowledge import DEFAULTS_DIR, SiteKnowledge, load
from backend.models import ApplyType, Document, Job

log = get_logger(__name__)

__all__ = ["ATS_APPLIERS", "AtsApplier", "GenericAtsApplier", "build_ats_appliers"]


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


#: Where an uploaded filename is echoed back. Generic on purpose: this is not
#: platform knowledge but the handful of shapes every upload widget uses, and
#: nine near-identical copies in nine JSON files would be nine things to fix.
_READBACK_SELECTORS: tuple[str, ...] = (
    "[class*='file-name']",
    "[class*='fileName']",
    "[data-automation-id*='file']",
    ".attachment-name",
    "input[type='file'] + span",
)


def _readback_texts(page: Any) -> list[str]:
    """Every filename-ish string near an upload control."""
    found: list[str] = []
    for selector in _READBACK_SELECTORS:
        try:
            for text in page.locator(selector).all_inner_texts():
                cleaned = text.strip()
                if cleaned and cleaned not in found:
                    found.append(cleaned)
        except Exception as exc:  # noqa: BLE001
            log.debug("readback_selector_absent", selector=selector, error=str(exc)[:100])
    return found


class GenericAtsApplier:
    """One adapter, configured per platform. Selectors are data.

    A dedicated class per ATS would be six copies of the same twelve methods
    differing only in their selector dict — the duplication Claude.md forbids.
    """

    def __init__(self, platform_key: str, knowledge: SiteKnowledge | None = None) -> None:
        self.platform = platform_key
        #: Same layer the primary boards use. An ATS redesign is a JSON edit in
        #: data/siteknowledge/<platform>/, not a code change, and the
        #: multi-strategy resolution and self-healing apply here identically —
        #: these platforms drift as often as Seek and LinkedIn do.
        self.knowledge = knowledge if knowledge is not None else load(platform_key)
        self._last_snapshot: dict[str, Any] | None = None

    # -- selection ----------------------------------------------------------

    def can_handle(self, job: Job) -> bool:
        """Whether this job's URL belongs to this platform."""
        if job.apply_type not in {ApplyType.EXTERNAL, ApplyType.UNKNOWN}:
            return False
        detection = detect_from_url(job.url)
        return detection.key == self.platform

    def _selectors_for(self, key: str) -> tuple[str, ...]:
        """Every strategy for an element, flattened. For the multi-slot walk."""
        element = self.knowledge.elements.get(key)
        return tuple(s.selector for s in element.ordered()) if element else ()

    # -- navigation ---------------------------------------------------------

    def open(self, page: Any, job: Job) -> None:
        page.goto(job.url, wait_until="domcontentloaded")

        button = self.knowledge.resolve(page, "apply_button")
        if button is not None:
            button.click()
            page.wait_for_load_state("domcontentloaded")

        self._check_captcha(page)
        self._confirm_platform(page, job)

    def _confirm_platform(self, page: Any, job: Job) -> None:
        """Check the page that actually loaded is the platform this adapter drives.

        The URL is not the last word. Clicking through "Apply" often lands on a
        different platform than the ad was served from, and an employer's own
        careers page frequently embeds someone else's form builder in an
        iframe. Either way the selectors held here belong to the wrong platform,
        and filling with them puts values in the wrong fields or in none at all
        — silently, which hard rule 9 does not allow.

        Reports rather than re-points. Swapping the adapter mid-flow, on a page
        that is already open and may already have been clicked through, is a
        bigger change than a mismatch warrants; this makes the mismatch
        impossible to miss in the log and leaves the decision visible.
        """
        try:
            html = page.content()
        except Exception as exc:  # noqa: BLE001 - a check must not break the flow
            log.debug("platform_confirm_skipped", error=str(exc)[:120])
            return

        detection = detect(job.url, html)
        if detection.platform is None or detection.key == self.platform:
            return

        log.warning(
            "ats_platform_mismatch",
            job_id=job.id,
            adapter=self.platform,
            detected=detection.key,
            via=detection.confidence,
            evidence=detection.evidence,
            iframe_src=detection.iframe_src,
        )

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
        target = self.knowledge.resolve(page, "file_input", visible=False)
        if target is None:
            raise RuntimeError(f"no file input on {self.platform}")

        if len(documents) == 1:
            target.set_input_files(documents[0].path)
            return

        inputs = page.locator(self._selectors_for("file_input")[0])
        for index, document in enumerate(documents):
            try:
                inputs.nth(index).set_input_files(document.path)
            except Exception:  # noqa: BLE001 - fall back to the first slot
                target.set_input_files(document.path)

    def read_back_attachments(self, page: Any) -> list[str]:
        """Read the filenames the form displays. Mandatory on every platform."""
        names = [
            name
            for name in _readback_texts(page)
            # A filename has an extension. Without this the read-back collects
            # every label near the upload control and "matches" anything.
            if "." in name
        ]
        if not names:
            log.error("attachment_readback_empty", platform=self.platform)
        return names

    # -- steps --------------------------------------------------------------

    def is_last_step(self, page: Any, fields: list[FormField]) -> bool:
        return self.knowledge.present(page, "submit_button", timeout_ms=1500)

    def advance(self, page: Any) -> None:
        button = _first_visible(
            page, ("button:has-text('Next')", "button:has-text('Continue')")
        )
        if button is None:
            raise RuntimeError(f"no next control on {self.platform}")
        button.click()
        page.wait_for_load_state("domcontentloaded")

    def submit(self, page: Any) -> None:
        self.knowledge.resolve(page, "submit_button").click()

    def confirmed(self, page: Any) -> bool:
        return self.knowledge.present(page, "confirmation", timeout_ms=10000)


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
        if _has_knowledge(platform.key)
    ]


def _has_knowledge(platform_key: str) -> bool:
    """Whether a platform ships site knowledge, and so has a working adapter.

    Replaces the old ``key in SELECTORS`` check. A platform that ``detect`` can
    recognise but has no strategies for is not one this adapter can drive —
    listing it would produce an applier that fails on its first resolve.
    """
    return (DEFAULTS_DIR / platform_key / "elements.json").exists()


ATS_APPLIERS = build_ats_appliers
