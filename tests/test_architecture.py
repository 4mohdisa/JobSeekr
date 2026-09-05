"""Rules the codebase must keep, checked by parsing it rather than reading it.

Two of these encode a specific complaint: adding a job board used to require
edits in seven files, and the same list of "domains that belong to a platform
rather than an employer" was maintained by hand in two modules and had already
drifted between them.

Text matching would be wrong here — several of these files discuss board names
in prose, and a docstring explaining why LinkedIn gets the strictest apply
window is not a hardcoded board name. So these parse the AST, and the
domain-list test compares behaviour rather than source.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import UTC, datetime

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"

# Where a board's name is allowed to appear, and why.
BOARD_NAME_EXEMPT = {
    # The registry itself.
    "boards.py",
    # One adapter file per board is the design; each names its own board.
    "discovery/seek_source.py",
    "discovery/jobspy_source.py",
    "discovery/verify_seek.py",
    "apply/seek.py",
    "apply/linkedin.py",
    # Seek's undocumented search endpoint is configuration, not a board name.
    "config.py",
    # "linkedin" is also a PROFILE FIELD — the URL on the user's resume. The
    # documents layer never dispatches on a board, it types one into a PDF.
    "documents/engine.py",
    "documents/build.py",
}


def python_files() -> list[pathlib.Path]:
    return [p for p in BACKEND.rglob("*.py") if ".venv" not in p.parts]


def string_constants(tree: ast.AST) -> list[tuple[int, str]]:
    """Every string literal that is not a docstring."""
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))

    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


# =========================================================================
# One board, one file
# =========================================================================


def test_no_module_outside_the_registry_names_a_job_board():
    """Adding a board must be an entry in backend/boards.py plus its adapter.

    It used to mean editing seven: the discovery registry, the login selectors,
    the applier list, the canary's URLs, the canary's watched selectors, the
    apply-window policy, and two hand-maintained domain lists. Missing one
    failed quietly — a board that discovered jobs and never applied to them, or
    an apply pass running LinkedIn's strictness against Seek.
    """
    from backend.boards import BOARDS

    keys = {entry.key for entry in BOARDS}
    offenders: list[str] = []

    for path in python_files():
        relative = path.relative_to(BACKEND).as_posix()
        if relative in BOARD_NAME_EXEMPT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno, value in string_constants(tree):
            # An exact match only: "linkedin" as a profile field name is not a
            # board reference, and neither is a URL inside an adapter.
            if value in keys:
                offenders.append(f"{relative}:{lineno} {value!r}")

    assert offenders == [], (
        "board names outside backend/boards.py and the adapters — read the board "
        "from the registry instead:\n" + "\n".join(offenders)
    )


def test_every_board_is_reachable_through_the_registry():
    from backend.apply.run import build_appliers
    from backend.boards import BOARDS, applier_boards, source_boards
    from backend.discovery.run import build_sources

    assert {s.name for s in build_sources()} == {e.key for e in source_boards()}

    applier_keys = [a.platform for a in build_appliers()]
    for entry in applier_boards():
        assert entry.key in applier_keys, f"{entry.key} has no applier in the pass"

    # And the pass tries boards before the external ATS adapters.
    assert applier_keys[: len(applier_boards())] == [e.key for e in applier_boards()]
    assert len(BOARDS) >= 3


def test_the_registry_drives_every_board_specific_table():
    from backend.apply.canary import CANARY_PAGES, WATCHED
    from backend.apply.guardrails import _WINDOW_POLICY
    from backend.apply.har import VARIANTS
    from backend.apply.session import PLATFORMS
    from backend.boards import BOARDS, session_boards

    assert set(PLATFORMS) == {e.key for e in session_boards()}
    assert set(CANARY_PAGES) == {e.key for e in BOARDS if e.canary_url}
    assert set(WATCHED) <= set(CANARY_PAGES)
    for entry in BOARDS:
        assert _WINDOW_POLICY[entry.key]["weekdays_only"] is entry.weekdays_only
        for key, _ in entry.har_variants:
            assert any(v.platform == entry.key and v.key == key for v in VARIANTS)


def test_a_canary_only_watches_selectors_its_adapter_actually_defines():
    """Re-listing them is how the canary came to watch renamed selectors."""
    from backend.apply.canary import WATCHED
    from backend.boards import board

    for platform, watched in WATCHED.items():
        entry = board(platform)
        assert entry is not None and entry.selectors is not None
        for key, selectors in watched.items():
            assert entry.selectors()[key] == selectors


def test_discovery_does_not_import_the_apply_layer():
    """Discovery is HTTP only. A browser session must never be in its import graph.

    The board registry holds both a discovery factory and an applier factory
    for the same board, so this is the rule that keeps that from collapsing the
    two layers together.
    """
    offenders: list[str] = []
    for path in (BACKEND / "discovery").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = None
            if isinstance(node, ast.ImportFrom):
                module = node.module
            elif isinstance(node, ast.Import):
                module = node.names[0].name
            if module and module.startswith(
                ("backend.apply", "backend.ats", "playwright")
            ):
                offenders.append(f"{path.relative_to(REPO)}:{node.lineno} {module}")
    assert offenders == [], offenders


def test_importing_the_registry_does_not_pull_in_the_apply_layer():
    """The lazy factories are load-bearing, not a style choice."""
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import backend.boards, sys; "
                "print([m for m in sys.modules if m.startswith('backend.apply')])"
            ),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=REPO,
        check=True,
    )
    assert result.stdout.strip().endswith("[]"), result.stdout


# =========================================================================
# One list of platform domains
# =========================================================================


@pytest.mark.parametrize(
    "domain",
    [
        "seek.com.au",
        "linkedin.com",
        "indeed.com",
        "au.indeed.com",
        "greenhouse.io",
        "boards.greenhouse.io",
        "lever.co",
        "workday.com",
        "myworkdayjobs.com",
        "smartrecruiters.com",
        "bamboohr.com",
        "recruitee.com",
        "workable.com",
        "pageuppeople.com",
        "jobadder.com",
        "taleo.net",
        "icims.com",
        "jobvite.com",
        "glassdoor.com",
        "mail.seek.com.au",
    ],
)
def test_the_contact_scraper_and_the_reply_matcher_agree_on_platform_domains(domain):
    """They kept two copies of this list and the copies drifted.

    The scraper knew about BambooHR and Glassdoor; the matcher did not. So the
    same address was platform plumbing in one module and a real employer in the
    other, which is how a cover letter gets emailed to an ATS robot.
    """
    from backend.boards import is_platform_domain
    from backend.discovery.contacts import _is_usable
    from backend.integrations.matching import InboundEmail

    assert is_platform_domain(domain)
    assert not _is_usable(f"careers@{domain}", source_host=None)

    email = InboundEmail(
        message_id="m",
        subject="s",
        from_address=f"noreply@{domain}",
        body="b",
        received_at=datetime.now(UTC),
    )
    assert email.from_ats


def test_a_real_employer_domain_is_not_treated_as_a_platform():
    from backend.boards import is_platform_domain
    from backend.discovery.contacts import _is_usable

    for domain in ("redgumanalytics.com.au", "acme.com", "seek-consulting.com.au"):
        assert not is_platform_domain(domain), domain
        assert _is_usable(f"careers@{domain}", source_host=None), domain


def test_the_platform_domain_list_covers_every_ats_the_detector_knows():
    """A vendor we can detect but do not recognise in mail is a leak."""
    from backend.ats.detect import ATS_REGISTRY
    from backend.boards import is_platform_domain

    for platform in ATS_REGISTRY:
        for pattern in platform.host_patterns:
            host = pattern.split("/", 1)[0]
            assert is_platform_domain(host), f"{platform.key}: {host}"
