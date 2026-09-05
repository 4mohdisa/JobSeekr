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
    # -- built, never reached ----------------------------------------------
    # -- YOUR DECISION, not an oversight -----------------------------------
    #
    # The outbound follow-up path is complete and deliberately unwired. Every
    # other entry in this file is something to fix; these three are a question.
    #
    # send_draft sends email AS the user, from their address, to a real
    # recruiter. That is the one action in this system whose blast radius is
    # someone else's inbox, and unlike an application it cannot be gated by
    # ALLOW_LIVE_SUBMIT after the fact — a badly judged follow-up is simply
    # sent. Turning it on is a policy decision about how this system represents
    # the user, and defaulting it on would be making that decision for them.
    #
    # Wiring it is one call in apply/run.py after a submitted application, plus
    # the approval token send_draft already requires. Reviewed 2026-09-03 and
    # left off on purpose.
    "preview": (
        "integrations/outbound.py. OutboundDraft.preview renders a draft as "
        "text. The dashboard shows the subject and body as separate fields "
        "instead, so nothing calls it — kept because it is the readable form "
        "for a log line or a console check."
    ),
    "replay": (
        "apply/har.py. Replay needs recorded HAR captures and an installed "
        "browser; neither exists yet. This is what the rehearsal cannot cover."
    ),
    # -- test harness, by design -------------------------------------------
    "page_for_capture": (
        "apply/snapshot.py. The offline replay harness's entry point: it exists "
        "so tests can drive a real adapter against captured markup with no "
        "browser. Production has a real page and never needs one of these. "
        "Same category as `replay` above — tooling, not an unwired feature."
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
            if not isinstance(
                node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
            ):
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
    surprises = {
        name: sites for name, sites in found.items() if name not in KNOWN_UNREACHABLE
    }

    assert not surprises, (
        "public code nothing in backend/ calls — wire it up or delete it:\n"
        + "\n".join(
            f"  {name}  ({', '.join(sites)})"
            for name, sites in sorted(surprises.items())
        )
        + "\n\nIf it is genuinely not wired yet, add it to KNOWN_UNREACHABLE "
        "with the reason."
    )


def test_the_allowlist_does_not_outlive_its_entries():
    """An entry that became reachable must leave the list, or it hides the next one."""
    found = unreachable()
    stale = sorted(set(KNOWN_UNREACHABLE) - set(found))

    assert not stale, (
        f"these are called now and should be removed from KNOWN_UNREACHABLE: {stale}"
    )


def test_every_allowlist_entry_says_why():
    thin = [name for name, reason in KNOWN_UNREACHABLE.items() if len(reason) < 40]
    assert not thin, f"allowlist entries needing a real reason: {thin}"


# =========================================================================
# What the sweep wired, and what it deleted
# =========================================================================
#
# The audit found these three built-and-never-called before. Wiring them is
# only half the job: without a test the next refactor silently unwires them
# again, which is exactly how formmaps, map_fields and escalate_question each
# became dead twice.


def test_decide_queueing_is_reached_from_the_redirect_path():
    """An off-site listing must be judged, not queued unconditionally.

    The manual queue costs the user about ninety seconds of attention per job.
    Filling it with listings the system itself scored as mediocre is how it
    stops being read.
    """
    import ast
    import pathlib

    source = pathlib.Path("backend/apply/flow.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "decide_queueing" in calls


def test_a_low_scoring_offsite_job_is_skipped_rather_than_queued():
    """The behaviour, not just the call site."""
    from backend.ats.queueing import decide_queueing
    from backend.models import Campaign, GrayZoneAction

    campaign = Campaign(
        id=1,
        name="c",
        search_terms=["dev"],
        locations=["Adelaide SA"],
        score_floor=60.0,
        score_auto_apply=80.0,
        gray_zone_action=GrayZoneAction.QUEUE,
        daily_caps={"default": 5},
    )

    assert decide_queueing(campaign, 70.0, automatable=False).action == "skip"
    assert decide_queueing(campaign, 95.0, automatable=False).action == "queue"


def test_ensure_logged_in_runs_before_any_application():
    """A dead session should fail immediately, not after building documents."""
    import ast
    import pathlib

    source = pathlib.Path("backend/apply/run.py").read_text(encoding="utf-8")
    assert "ensure_logged_in" in source

    tree = ast.parse(source)
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "ensure_logged_in" in calls


def test_the_session_check_skips_platforms_that_have_no_session():
    """An employer ATS has no login; "not signed in" there is the normal state.

    Checking it would halt the pass on every external application — which it
    did, and the rehearsal caught it.
    """
    from backend.apply.session import PLATFORMS

    assert "greenhouse" not in PLATFORMS
    assert "linkedin" in PLATFORMS


def test_an_edited_rubric_invalidates_a_stored_score():
    """rubric_version only moves when someone remembers to bump it.

    Without the hash, editing the criteria produced scores indistinguishable
    from ones computed before the edit — a silently stale shortlist.
    """
    from sqlmodel import Session, SQLModel, create_engine

    from backend.models import Job, JobStatus, Score
    from backend.scoring.run import needs_scoring

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        job = Job(
            id=1,
            source="seek",
            source_job_id="1",
            url="https://example.com/1",
            title="Dev",
            company="Acme",
            location="Adelaide SA",
            dedupe_hash="h",
            status=JobStatus.DISCOVERED,
        )
        session.add(job)
        session.flush()
        session.add(
            Score(
                job_id=1,
                profile_version=1,
                rubric_version=1,
                rubric_hash="oldhash",
                final=80.0,
            )
        )
        session.flush()

        assert not needs_scoring(
            session, job, profile_version=1, rubric_version=1, rubric_digest="oldhash"
        )
        assert needs_scoring(
            session, job, profile_version=1, rubric_version=1, rubric_digest="newhash"
        ), "an edited rubric must invalidate the stored score"


def test_the_deleted_vestigials_are_actually_gone():
    """Deleted, not merely unreferenced — a dormant duplicate protocol drifts."""
    import backend.base
    import backend.boards
    import backend.documents.engine

    assert not hasattr(backend.base, "Applier"), (
        "two protocols describing the same role is how they drift"
    )
    assert not hasattr(backend.documents.engine, "render_template_file")
    assert not hasattr(backend.boards, "board_keys")
