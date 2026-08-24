"""Fill a form nobody has written an adapter for.

The accessibility tree, not the HTML. ``page.accessibility.snapshot()`` returns
roles, names and values — a few hundred tokens for a form whose raw markup runs
to tens of thousands. It is also *semantically* better input: the accessible
name of a field is the label a human reads, which is exactly what the mapping
needs and exactly what survives a CSS refactor.

The abstain rule is identical to the answer bank's, and for the same reason: a
field this module cannot confidently map is a field it must not fill. Guessing
that "Do you have a current WWCC?" means the driver's licence answer produces a
false statement on an application, and there is no undo.

CAPTCHA is a hard stop. The job is parked and the user notified. No solving
services, no third-party bypass: a CAPTCHA is the site saying it wants a human,
and the correct response is to fetch one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from backend.apply.draft import FormField
from backend.config import settings
from backend.llm.client import LLMBudgetExceeded, llm
from backend.logging_setup import get_logger

log = get_logger(__name__)

__all__ = [
    "CaptchaDetected",
    "detect_captcha",
    "fields_from_accessibility",
    "map_fields",
]


class CaptchaDetected(RuntimeError):
    """The site asked for a human. Park the job and notify — never solve it."""


# Roles the accessibility tree uses for things a user fills in.
_INPUT_ROLES = {
    "textbox": "text",
    "searchbox": "text",
    "combobox": "select",
    "listbox": "select",
    "checkbox": "checkbox",
    "radio": "radio",
    "radiogroup": "radio",
    "spinbutton": "number",
    "slider": "number",
    "button": "button",
}

_CAPTCHA_MARKERS = (
    "recaptcha",
    "hcaptcha",
    "cloudflare turnstile",
    "i'm not a robot",
    "i am not a robot",
    "verify you are human",
    "security check",
    "captcha",
)


def detect_captcha(snapshot: dict[str, Any] | None, html: str | None = None) -> bool:
    """Whether the page is asking for a human."""
    haystacks: list[str] = []
    if snapshot:
        haystacks.append(_flatten_names(snapshot).casefold())
    if html:
        haystacks.append(html[:200_000].casefold())

    return any(marker in text for text in haystacks for marker in _CAPTCHA_MARKERS)


def _flatten_names(node: dict[str, Any], out: list[str] | None = None) -> str:
    collected = out if out is not None else []
    name = node.get("name")
    if name:
        collected.append(str(name))
    for child in node.get("children", []) or []:
        _flatten_names(child, collected)
    return " ".join(collected)


def _walk(node: dict[str, Any], depth: int = 0) -> Iterable[tuple[dict[str, Any], int]]:
    yield node, depth
    for child in node.get("children", []) or []:
        yield from _walk(child, depth + 1)


def fields_from_accessibility(snapshot: dict[str, Any] | None) -> list[FormField]:
    """Turn an accessibility snapshot into the flow's FormField shape.

    Produces the same objects the platform adapters produce, so the shared apply
    flow does not know or care that this form was mapped generically.
    """
    if not snapshot:
        return []

    fields: list[FormField] = []
    seen: set[str] = set()

    for node, _ in _walk(snapshot):
        role = str(node.get("role", "")).lower()
        kind = _INPUT_ROLES.get(role)
        if kind is None or kind == "button":
            continue

        name = str(node.get("name") or "").strip()
        if not name:
            continue

        identifier = re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")[:60]
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)

        choices: list[str] = []
        if kind == "select":
            choices = [
                str(child.get("name", "")).strip()
                for child, _ in _walk(node)
                if str(child.get("role", "")).lower() == "option" and child.get("name")
            ]

        fields.append(
            FormField(
                identifier=identifier,
                label=name,
                kind=kind,
                required=bool(node.get("required")),
                choices=choices,
                current_value=str(node.get("value") or "") or None,
            )
        )

    log.info("accessibility_fields_extracted", count=len(fields))
    return fields


# --------------------------------------------------------------------------
# Mapping
# --------------------------------------------------------------------------

MAPPING_SCHEMA = {
    "type": "object",
    "title": "form_field_mapping",
    "properties": {
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "identifier": {"type": "string"},
                    "source": {
                        "type": "string",
                        "enum": ["profile", "answer_bank", "document", "unknown"],
                    },
                    "profile_path": {
                        "type": "string",
                        "description": "e.g. profile.email. Empty unless source is profile.",
                    },
                    "question": {
                        "type": "string",
                        "description": (
                            "The screening question this field asks, verbatim. "
                            "Empty unless source is answer_bank."
                        ),
                    },
                    "confident": {
                        "type": "boolean",
                        "description": "False if there is any doubt. False is always safe.",
                    },
                },
                "required": ["identifier", "source", "confident"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["fields"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You map the fields of a job application form onto where their values come "
    "from. You NEVER supply values — only where each field's value should be "
    "read from.\n"
    "- 'profile' for identity facts: name, email, phone, address, LinkedIn.\n"
    "- 'answer_bank' for screening questions, with the question written out "
    "verbatim so it can be looked up.\n"
    "- 'document' for resume or cover letter uploads.\n"
    "- 'unknown' whenever you are not certain.\n"
    "Set confident=false if there is ANY doubt. An unmapped field parks the "
    "application and asks the user, which is cheap. A wrongly mapped field puts "
    "a false statement on someone's job application, which cannot be undone."
)

PROFILE_PATHS = (
    "profile.name",
    "profile.first_name",
    "profile.last_name",
    "profile.email",
    "profile.phone",
    "profile.location",
    "profile.linkedin",
    "profile.website",
    "profile.summary",
)


@dataclass
class MappedField:
    identifier: str
    source: str
    profile_path: str = ""
    question: str = ""
    confident: bool = False

    @property
    def usable(self) -> bool:
        return self.confident and self.source != "unknown"


def map_fields(fields: list[FormField], *, platform: str | None = None) -> list[MappedField]:
    """Ask the model where each field's value comes from.

    Only the fields passed in are mapped — on a partial failure the caller
    passes just the unknown ones, so a form is never re-learned wholesale.
    """
    if not fields:
        return []

    described = "\n".join(
        f"- id={f.identifier!r} label={f.label!r} type={f.kind}"
        + (f" choices={f.choices}" if f.choices else "")
        + (" (required)" if f.required else "")
        for f in fields
    )
    prompt = (
        f"Application form{f' on {platform}' if platform else ''}.\n"
        f"Available profile paths: {', '.join(PROFILE_PATHS)}\n\n"
        f"Fields:\n{described}\n\n"
        "Map every field. Use 'unknown' with confident=false when unsure."
    )

    try:
        payload = llm.complete_json(
            prompt,
            model=settings.llm_model_formmap,
            purpose="form_mapping",
            schema=MAPPING_SCHEMA,
            system=_SYSTEM,
        )
    except LLMBudgetExceeded:
        raise
    except Exception as exc:
        log.exception("form_mapping_failed", error=str(exc)[:200])
        return [MappedField(identifier=f.identifier, source="unknown") for f in fields]

    by_id = {f.identifier: f for f in fields}
    mapped: list[MappedField] = []

    for entry in payload.get("fields", []):
        identifier = str(entry.get("identifier", ""))
        if identifier not in by_id:
            continue
        mapped.append(
            MappedField(
                identifier=identifier,
                source=str(entry.get("source", "unknown")),
                profile_path=str(entry.get("profile_path", "") or ""),
                question=str(entry.get("question", "") or ""),
                confident=bool(entry.get("confident", False)),
            )
        )

    # Anything the model skipped is unknown, not absent.
    mapped_ids = {m.identifier for m in mapped}
    for field in fields:
        if field.identifier not in mapped_ids:
            mapped.append(MappedField(identifier=field.identifier, source="unknown"))

    unusable = [m.identifier for m in mapped if not m.usable]
    log.info(
        "form_fields_mapped",
        total=len(mapped),
        confident=len(mapped) - len(unusable),
        unmapped=unusable[:8],
    )
    return mapped
