"""Generic HTML form reading, shared by every adapter.

WHAT BELONGS HERE AND WHAT DOES NOT
    This module knows about ``<input>``, ``<select>``, ``<textarea>`` and
    ``<label for=...>``. That is the HTML standard, identical on Seek, on
    LinkedIn and on every ATS, so it is written once here.

    It knows nothing about any particular site. Anything that distinguishes one
    platform from another — which button submits, which attribute carries the
    test id, where the uploaded filename is echoed back — lives in
    ``backend/siteknowledge`` as data, never as a literal in Python.

WHY IT EXISTS
    ``apply/seek.py`` and ``apply/linkedin.py`` each carried their own copy of
    this enumeration, roughly fifty lines apiece, differing only in which
    attributes they tried for an identifier and whether they scoped to a modal.
    Two copies of the same traversal is how they drift: the LinkedIn copy had
    already grown a file-input visibility exemption the Seek copy lacked, and
    that difference was a bug on Seek rather than a deliberate distinction.
"""

from __future__ import annotations

from typing import Any

from backend.apply.draft import FormField
from backend.logging_setup import get_logger

log = get_logger(__name__)

__all__ = ["FORM_CONTROLS", "enumerate_form_fields", "fill", "label_for"]


FORM_CONTROLS = "input, textarea, select"
"""Every interactive control the HTML standard defines for a form.

Not site knowledge: this is the same on every page on the internet, which is
why it is a constant here rather than a strategy in a per-platform JSON file.
"""

_PLACEHOLDER_OPTIONS = {"select an option", "choose an option", "please select", ""}
"""Dropdown prompts that are not real choices.

Left in, they become candidate answers, and the answer bank would then be asked
to match a question against the word "Select an option".
"""


def label_for(scope: Any, identifier: str) -> str | None:
    """The text of the ``<label for=...>`` bound to this control."""
    try:
        label = scope.locator(f"label[for='{identifier}']").first
        if label.count() > 0:
            return label.inner_text()
    except Exception:  # noqa: BLE001 - an unlabelled control is normal
        return None
    return None


def _kind_of(handle: Any, input_type: str) -> str:
    tag = handle.evaluate("el => el.tagName.toLowerCase()")
    if tag == "select":
        return "select"
    if input_type in {"file", "radio", "checkbox"}:
        return input_type
    return "textarea" if tag == "textarea" else "text"


def enumerate_form_fields(
    scope: Any,
    step: int,
    *,
    identifier_attributes: tuple[str, ...] = ("name", "id"),
    visibility_timeout_ms: int = 500,
) -> list[FormField]:
    """Describe every control currently in ``scope``.

    ``scope`` is a page or a locator — LinkedIn scopes to the Easy Apply modal,
    Seek uses the whole page. Both satisfy the same locator protocol, so this
    does not need to know which it was handed.

    ``identifier_attributes`` is ordered: the first attribute present wins.
    Platforms differ in which one is meaningful (Seek's ``name`` is stable,
    LinkedIn's ``id`` carries the URN), so the caller states its preference
    rather than this guessing.

    File inputs are never skipped for invisibility. They are routinely hidden
    behind a styled button, and treating "not visible" as "not there" is what
    made an upload slot invisible to the enumeration that has to find it.
    """
    fields: list[FormField] = []

    for handle in scope.locator(FORM_CONTROLS).all():
        try:
            input_type = (handle.get_attribute("type") or "text").lower()
            is_file = input_type == "file"

            if not is_file and not handle.is_visible(timeout=visibility_timeout_ms):
                continue

            identifier = ""
            for attribute in identifier_attributes:
                identifier = handle.get_attribute(attribute) or ""
                if identifier:
                    break
            if not identifier:
                # Nothing to key an answer or a form map against. Skipping is
                # correct: a field we cannot name is a field we cannot fill,
                # and inventing an index would break the moment the DOM order
                # changed.
                continue

            kind = _kind_of(handle, input_type)
            label = (
                handle.get_attribute("aria-label")
                or handle.get_attribute("placeholder")
                or label_for(scope, identifier)
                or identifier
            )

            choices: list[str] = []
            if kind == "select":
                choices = [
                    option.strip()
                    for option in handle.locator("option").all_inner_texts()
                    if option.strip()
                    and option.strip().casefold() not in _PLACEHOLDER_OPTIONS
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


def fill(page: Any, field: FormField, value: str) -> None:
    """Put ``value`` into ``field``, by whichever mechanism its kind needs.

    The locator is built from the identifier the enumeration already read off
    this page, so it is a lookup of a known control rather than site knowledge.
    """
    target = f"[name='{field.identifier}'], [id='{field.identifier}']"

    if field.kind == "select":
        page.locator(target).first.select_option(label=value)
        return

    if field.kind in {"radio", "checkbox"}:
        # Radios share a name across the group, so the value is what picks the
        # member. The label fallback covers groups whose inputs are visually
        # replaced and only clickable through their label.
        page.locator(
            f"[name='{field.identifier}'][value='{value}'], "
            f"[id='{field.identifier}'][value='{value}']"
        ).first.check()
        return

    page.locator(target).first.fill(value)
