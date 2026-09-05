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

import re
from typing import Any

from backend.apply.draft import Choice, FormField
from backend.logging_setup import get_logger

log = get_logger(__name__)

__all__ = [
    "FORM_CONTROLS",
    "MULTI_VALUE_SEPARATOR",
    "enumerate_form_fields",
    "fill",
    "label_for",
    "option_choice",
    "read_choices",
    "split_values",
]


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

_FREE_TEXT_OPTION = re.compile(
    r"^\s*other\b|please\s+specify|\(specify\)", re.IGNORECASE
)
"""An option that is a prompt for more text rather than an answer.

Marked rather than dropped: it is a legitimate option and the user may want it,
but choosing it leaves a text box that nothing here can fill from the answer
bank, so the escalation has to say so instead of treating it as an answer.
"""

_GROUPED_KINDS = ("radio", "checkbox")
"""Kinds whose members share a ``name`` and are ONE question, not several.

A three-way radio group enumerated per element is three fields, each labelled
with one option and none of them carrying the question. That is how a closed
list reaches the answer bank as three unanswerable text questions.
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


def option_choice(label: str, value: str) -> Choice | None:
    """One ``<option>`` or group member, or None when it is only a prompt.

    The label is stripped of surrounding whitespace and NOT otherwise touched.
    "1 - 2 weeks" must reach the user as "1 - 2 weeks": tidying it is how a
    reply stops matching the option it was chosen from.
    """
    label = (label or "").strip()
    value = value if value is not None else ""
    if not label and not value:
        return None
    if label.casefold() in _PLACEHOLDER_OPTIONS:
        return None
    return Choice(
        label=label or value,
        # An <option> with no value attribute submits its own text. Falling back
        # to the label rather than to "" is what the browser itself does.
        value=value if value != "" else label,
        is_free_text=bool(_FREE_TEXT_OPTION.search(label)),
    )


def _options_of(container: Any) -> list[Choice]:
    """Every ``<option>`` under ``container``, label and value together.

    One locator per option, and both attributes read off THAT locator — the
    label and the value have to come from the same element or an answer is
    submitted under the wrong option's value.

    Locators rather than a single ``evaluate``: the offline snapshot harness
    that replays captured HAR markup runs no JavaScript, and an enumeration that
    only works in a live browser cannot be tested against a real form.
    """
    found: list[Choice] = []
    for option in container.locator("option").all():
        choice = option_choice(option.inner_text(), option.get_attribute("value") or "")
        if choice is not None:
            found.append(choice)
    return found


def read_choices(
    handle: Any, kind: str, scope: Any = None
) -> tuple[list[Choice], bool]:
    """The options one control offers, and whether several may be picked.

    ``select`` reads its own ``<option>`` children. Anything else may carry a
    ``list=`` pointing at a ``<datalist>`` elsewhere in the document, which is
    why the surrounding scope is needed to find it.
    """
    try:
        if kind == "select":
            return _options_of(handle), handle.get_attribute("multiple") is not None

        list_id = handle.get_attribute("list")
        if not list_id or scope is None:
            return [], False
        # A datalist is a suggestion list, not a closed one: the browser still
        # accepts free text. It is captured because the site is telling us what
        # it expects, and an answer from that set is the one most likely to be
        # understood — but multi_select stays False and the field keeps its own
        # kind, so nothing treats it as a dropdown.
        return _options_of(scope.locator(f"datalist#{list_id}")), False
    except Exception as exc:  # noqa: BLE001 - a control with no options is normal
        log.debug("choice_read_failed", kind=kind, error=str(exc)[:120])
        return [], False


def _member_label(scope: Any, handle: Any) -> str:
    """What one radio or checkbox in a group is labelled, on the page."""
    for getter in ("aria-label", "value"):
        text = handle.get_attribute(getter)
        if text:
            if getter == "value":
                break
            return text
    element_id = handle.get_attribute("id") or ""
    return (
        (element_id and label_for(scope, element_id))
        or handle.get_attribute("value")
        or ""
    )


def _group_question(scope: Any, name: str, members: list[Any]) -> str:
    """The question a radio/checkbox group is asking, not one of its answers.

    A fieldset legend is the standard answer and the one real forms use; an
    aria-label on the group container is the other. Failing both, the group's
    name is all there is — which is honest: a group with no question text on the
    page cannot be turned into one by guessing from its options.
    """
    for selector in (
        f"fieldset:has([name='{name}']) legend",
        "[role='radiogroup'][aria-label], [role='group'][aria-label]",
    ):
        try:
            locator = scope.locator(selector).first
            if locator.count() > 0:
                text = (
                    locator.get_attribute("aria-label")
                    if "aria-label" in selector
                    else locator.inner_text()
                )
                if text and text.strip():
                    return text.strip()
        except Exception as exc:  # noqa: BLE001 - no legend is the common case
            log.debug("group_question_lookup_failed", name=name, error=str(exc)[:120])
    return name


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

    Radios and checkboxes sharing a ``name`` are collapsed into ONE field whose
    choices are the group's members. Enumerated per element they arrive as three
    fields each labelled with one of the answers, and the question itself is
    nowhere — which is how a closed list reaches the answer bank as several
    unanswerable free-text questions.
    """
    fields: list[FormField] = []
    groups: dict[str, list[Any]] = {}
    group_order: list[str] = []

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

            if kind in _GROUPED_KINDS:
                # Grouped by the name that actually submits, not by whichever
                # identifier attribute the caller preferred: a radio group's
                # members share `name` and have DIFFERENT ids, so grouping by id
                # would leave every member its own field again.
                name = handle.get_attribute("name") or identifier
                if name not in groups:
                    groups[name] = []
                    group_order.append(name)
                groups[name].append(handle)
                continue

            label = (
                handle.get_attribute("aria-label")
                or handle.get_attribute("placeholder")
                or label_for(scope, identifier)
                or identifier
            )

            choices, multiple = read_choices(
                handle, "select" if kind == "select" else "datalist", scope
            )

            fields.append(
                FormField(
                    identifier=identifier,
                    label=label.strip(),
                    kind=kind,
                    required=handle.get_attribute("required") is not None,
                    choices=choices,
                    multi_select=multiple,
                    step=step,
                )
            )
        except Exception as exc:  # noqa: BLE001 - one odd control, not the form
            log.debug("field_enumeration_skipped", error=str(exc)[:120])

    fields.extend(_grouped_fields(scope, groups, group_order, step))
    return fields


def _grouped_fields(
    scope: Any, groups: dict[str, list[Any]], order: list[str], step: int
) -> list[FormField]:
    """One field per radio/checkbox name, carrying its members as choices."""
    fields: list[FormField] = []

    for name in order:
        members = groups[name]
        try:
            kind = (members[0].get_attribute("type") or "").lower()
            # A lone checkbox is a consent tick — "I agree to the terms" — not a
            # one-option list. It keeps its own label and no choices, because
            # offering the user a single button to press is not a question.
            if kind == "checkbox" and len(members) == 1:
                identifier = members[0].get_attribute("name") or name
                label = (
                    members[0].get_attribute("aria-label")
                    or label_for(scope, members[0].get_attribute("id") or "")
                    or identifier
                )
                fields.append(
                    FormField(
                        identifier=identifier,
                        label=label.strip(),
                        kind="checkbox",
                        required=members[0].get_attribute("required") is not None,
                        step=step,
                    )
                )
                continue

            choices = [
                choice
                for choice in (
                    option_choice(
                        _member_label(scope, member),
                        member.get_attribute("value") or "",
                    )
                    for member in members
                )
                if choice is not None
            ]
            fields.append(
                FormField(
                    identifier=name,
                    label=_group_question(scope, name, members).strip(),
                    kind=kind or "radio",
                    required=any(
                        member.get_attribute("required") is not None
                        for member in members
                    ),
                    choices=choices,
                    # Radios are exclusive by definition; checkboxes are not.
                    multi_select=kind == "checkbox",
                    step=step,
                )
            )
        except Exception as exc:  # noqa: BLE001 - one odd group, not the form
            log.debug("field_group_skipped", name=name, error=str(exc)[:120])

    return fields


# ``\x1f`` (ASCII unit separator) joins the values of a multi-select answer.
# A control character cannot appear in rendered option text, so it can never
# collide with a real value the way a comma or a pipe would.
# ponytail: one separator, no escaping. If a form ever submits control
# characters in an option value, store the selection as JSON instead.
MULTI_VALUE_SEPARATOR = "\x1f"


def split_values(value: str) -> list[str]:
    """The individual values in a stored answer, single or multi."""
    return [part for part in value.split(MULTI_VALUE_SEPARATOR) if part]


def fill(page: Any, field: FormField, value: str) -> None:
    """Put ``value`` into ``field``, by whichever mechanism its kind needs.

    The locator is built from the identifier the enumeration already read off
    this page, so it is a lookup of a known control rather than site knowledge.

    ``value`` is the form's own submitted value wherever the field is a closed
    list — that is what the enumeration captured and what the answer bank
    stored. Selecting by label was the old behaviour and is kept only as a
    fallback, for a stored answer that predates the option values being read.
    """
    target = f"[name='{field.identifier}'], [id='{field.identifier}']"
    values = split_values(value)

    if field.kind == "select":
        locator = page.locator(target).first
        try:
            locator.select_option(value=values if field.multi_select else values[0])
        except Exception as exc:  # noqa: BLE001 - fall back to the visible text
            log.debug(
                "select_by_value_failed", field=field.identifier, error=str(exc)[:120]
            )
            locator.select_option(label=values if field.multi_select else values[0])
        return

    if field.kind in _GROUPED_KINDS:
        # Radios share a name across the group, so the value is what picks the
        # member. The label fallback covers groups whose inputs are visually
        # replaced and only clickable through their label.
        for one in values:
            page.locator(
                f"[name='{field.identifier}'][value='{one}'], "
                f"[id='{field.identifier}'][value='{one}']"
            ).first.check()
        return

    page.locator(target).first.fill(value)
