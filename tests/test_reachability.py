"""Public code that nothing in production calls.

Three separate features shipped complete, tested, and unreachable: the whole
form-map cache in ``ats/formmaps.py``, ``generic.map_fields``, and
``has_restriction_notice``. Each had unit tests, so the suite was green while
the behaviour they implemented never ran once. A test suite that only checks
"does this function work" cannot see that nothing calls it.

This is the check for that. It is deliberately crude — a name-level reachability
scan, not a call graph — because the bug it catches is crude: a public symbol
that appears nowhere in ``backend/`` except its own definition.

WHEN THIS FAILS, DO NOT JUST ADD THE NAME TO THE ALLOWLIST. The question it is
asking is "did you mean to build something nothing runs?" — the answer is
usually to wire it up or delete it. The allowlist is for the cases where the
answer is genuinely "not yet, and here is why".
"""

from __future__ import annotations

import ast
import pathlib

BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"


# Public symbols that nothing in backend/ calls, and why that is currently
# accepted. Every one of these is a real gap, not a false positive.
KNOWN_UNREACHABLE: dict[str, str] = {
    # -- vestigial ---------------------------------------------------------
    "apply": (
        "backend/base.py Applier protocol. The apply flow defines and uses its "
        "own Adapter protocol instead, so this one is never satisfied or "
        "called. Delete it or make flow.Adapter an alias."
    ),
    "render_template_file": (
        "documents/engine.py. The build renders through render_string with an "
        "explicitly loaded template; nothing loads a template by filename."
    ),
    "rubric_hash": (
        "scoring/rubric.py. Nothing stamps a rubric hash onto a Score, so a "
        "rubric edit is not currently detectable from a stored score."
    ),
    "board": "boards.py lookup helper. Nothing resolves a board by key.",
    "board_keys": "boards.py lookup helper. Nothing enumerates board keys.",
    # -- built, never reached ----------------------------------------------
    "decide_queueing": (
        "ats/queueing.py. The manual-queue decision is never made, so a job "
        "that should be queued for the user to finish by hand is not."
    ),
    "ensure_logged_in": (
        "apply/session.py. Nothing verifies the browser session before a run; "
        "the guardrail checks authentication separately at submit time."
    ),
    # -- awaiting the user -------------------------------------------------
    "draft_for_job": "integrations/outbound.py. Outbound follow-up email is not wired.",
    "preview": "integrations/outbound.py. Outbound follow-up email is not wired.",
    "send_draft": (
        "integrations/outbound.py. Sends mail as the user and requires an "
        "explicit approval token; wiring it is the user's call, not a default."
    ),
    "replay": (
        "apply/har.py. Replay needs recorded HAR captures and an installed "
        "browser; neither exists yet. This is what the rehearsal cannot cover."
    ),
}


def _python_files() -> list[pathlib.Path]:
    return sorted(p for p in BACKEND.rglob("*.py") if "__pycache__" not in p.parts)


def _public_definitions() -> tuple[dict[str, list[str]], set[str]]:
    """Every public function and class, and which of them a decorator registers."""
    definitions: dict[str, list[str]] = {}
    decorated: set[str] = set()
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(BACKEND.parent).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                continue
            if node.name.startswith("_"):
                continue
            definitions.setdefault(node.name, []).append(f"{rel}:{node.lineno}")
            if node.decorator_list:
                # Registered by decoration: FastAPI routes, validators,
                # properties. The decorator is the caller.
                decorated.add(node.name)
    return definitions, decorated


def _referenced_names() -> set[str]:
    """Names used as identifiers or attributes anywhere in backend/.

    String constants are deliberately NOT counted. Including them lets a
    coincidental string — a log event name, a dict key — mark dead code as
    reachable, which is exactly the blindness this test exists to remove.
    """
    names: set[str] = set()
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
    return names


def unreachable() -> dict[str, list[str]]:
    definitions, decorated = _public_definitions()
    referenced = _referenced_names()
    return {
        name: sites
        for name, sites in definitions.items()
        if name not in decorated and name not in referenced
    }


def test_no_new_unreachable_public_code():
    """Nothing public may be added that production never calls."""
    found = unreachable()
    surprises = {name: sites for name, sites in found.items() if name not in KNOWN_UNREACHABLE}

    assert not surprises, (
        "public code nothing in backend/ calls — wire it up or delete it:\n"
        + "\n".join(f"  {name}  ({', '.join(sites)})" for name, sites in sorted(surprises.items()))
        + "\n\nIf it is genuinely not wired yet, add it to KNOWN_UNREACHABLE "
        "with the reason."
    )


def test_the_allowlist_does_not_outlive_its_entries():
    """An entry that became reachable must leave the list, or it hides the next one."""
    found = unreachable()
    stale = sorted(set(KNOWN_UNREACHABLE) - set(found))

    assert not stale, (
        "these are called now and should be removed from KNOWN_UNREACHABLE: "
        f"{stale}"
    )


def test_every_allowlist_entry_says_why():
    thin = [name for name, reason in KNOWN_UNREACHABLE.items() if len(reason) < 40]
    assert not thin, f"allowlist entries needing a real reason: {thin}"
