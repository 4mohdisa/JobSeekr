"""What an element IS, independent of which platform is serving it.

Nine ATS platforms have their own knowledge files and the same four elements in
each: a resume file input, a submit button, an apply button, a confirmation.
Their strategies are all platform-specific — a testid here, a CSS class there —
so a tenth platform starts with nothing, and every one of the nine that loses a
selector falls through to no candidate at all.

The patterns repeat because the *roles* repeat. A submit button is a button
whose accessible name says submit, on every site ever built. That is a fact
about application forms, not about Greenhouse, and it belongs somewhere a new
platform inherits it from.

WHAT THIS IS NOT
    Not a replacement for platform knowledge. These are last-resort candidates:
    broad by construction, so they match more than they should and are ordered
    accordingly. ``Strategy.shared`` marks them, and a platform's own strategy
    wins every tie. They exist so that "no strategy resolved this element" — a
    parked job and an alert — becomes "the durable one broke and the generic one
    caught it", which is a warning instead of a failure.

WHY ROLE AND ACCESSIBLE NAME
    They are the two things a form cannot change without changing what it is.
    A redesign renames classes and regenerates test ids; it does not stop the
    submit button being a button called Submit, because a form that did would be
    unusable with a screen reader. Every candidate here is `role` or `text` for
    that reason — never CSS, which is the layer that churns.
"""

from __future__ import annotations

from typing import Any

__all__ = ["SHARED_ELEMENTS", "shared_candidates"]

_NOTE = "shared vocabulary — generic candidate, not platform knowledge"


def _role(role: str, name: str) -> dict[str, Any]:
    return {"type": "role", "role": role, "name": name, "note": _NOTE}


def _css(value: str, note: str) -> dict[str, Any]:
    return {"type": "css", "value": value, "note": note}


def _text(value: str) -> dict[str, Any]:
    return {"type": "text", "value": value, "note": _NOTE}


# Plain dicts, in the same shape the platform JSON files use, so the shared
# vocabulary is DATA loaded the same way platform knowledge is rather than a
# second construction path that could drift from it. It also keeps this module
# free of any import from the package it lives in, which would be a cycle.
SHARED_ELEMENTS: dict[str, list[dict[str, Any]]] = {
    "apply_button": [
        _role("button", "*Apply*"),
        _role("link", "*Apply*"),
        _role("button", "*Apply now*"),
    ],
    "submit_button": [
        _role("button", "*Submit*"),
        _role("button", "*Submit application*"),
        _role("button", "*Send application*"),
    ],
    "file_input": [
        # An upload control is routinely a hidden <input type=file> behind a
        # styled button, so this is matched by presence rather than visibility —
        # which is why resolve(visible=False) exists, and why a role candidate
        # naming the visible button would find the wrong node.
        _css("input[type='file']", "shared vocabulary — every upload control is one"),
    ],
    "resume_file_input": [
        _css("input[type='file']", "shared vocabulary — every upload control is one"),
    ],
    "confirmation": [
        _text("application submitted"),
        _text("thank you for applying"),
        _text("we have received your application"),
    ],
    "next_button": [
        _role("button", "*Next*"),
        _role("button", "*Continue*"),
    ],
    "required_marker": [
        # Not resolved by any adapter today. Present because it is the third
        # pattern every one of these forms has and the one a new platform needs
        # first — a required field the flow left empty is a submit that bounces.
        _css("[aria-required='true']", _NOTE),
    ],
}
"""Generic candidates by element key.

Keyed by the SAME element keys the platform files use, so merging is a lookup
rather than a mapping table that could disagree with itself.
"""


def shared_candidates(key: str) -> list[Any]:
    """Generic ``Strategy`` candidates for one element key, or nothing.

    Fresh objects every call. Strategies carry their own success and failure
    counters once resolution starts learning, and handing every platform the
    same instances would pool nine platforms' evidence into one strategy that
    belongs to none of them.

    The import is function-local because this module is imported BY the package
    that defines ``Strategy``.
    """
    from backend.siteknowledge import Strategy

    return [
        Strategy(shared=True, **candidate) for candidate in SHARED_ELEMENTS.get(key, [])
    ]
