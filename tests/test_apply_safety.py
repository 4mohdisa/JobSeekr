"""The file a reviewer should read first.

One scenario, fully satisfied: a great score, gate-passed documents, zero
abstentions, an authenticated session, inside the window, under every cap, the
breaker closed. Under the DEFAULT configuration it must still not submit, and
``allow_live_submit`` must be the only reason.

Then the same scenario with that one setting flipped, to prove nothing else was
quietly failing and the switch really is the single gate.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from backend.apply import flow, guardrails
from backend.apply.run import eligible_jobs
from backend.apply.draft import FormField
from backend.base import ApplyOutcome
from backend.config import settings
from backend.models import (
    AnswerBank,
    AnswerType,
    Application,
    ApplicationOutcome,
    Campaign,
    Document,
    DocumentKind,
    GrayZoneAction,
    Job,
    JobStatus,
    MatchType,
    Profile,
    Score,
)
from tests.test_flow import FakeAdapter, FakePage


@pytest.fixture(autouse=True)
def _neutral_clock(monkeypatch):
    """The window is tested in test_guardrails; here it must not gate the clock."""
    monkeypatch.setattr(settings, "apply_window_start", "00:00")
    monkeypatch.setattr(settings, "apply_window_end", "23:59")


@pytest.fixture
def perfect(tmp_path, monkeypatch):
    """Everything an application needs, all of it satisfied."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            Profile(
                version=1,
                identity={
                    "name": "Jordan Fitzgerald",
                    "email": "jordan@example.com",
                    "phone": "+61 412 345 678",
                },
            )
        )
        campaign = Campaign(
            id=1,
            name="live",
            active=True,
            search_terms=["developer"],
            locations=["Adelaide SA"],
            score_floor=60.0,
            score_auto_apply=80.0,
            gray_zone_action=GrayZoneAction.QUEUE,
            daily_caps={"seek": 20},
        )
        session.add(campaign)
        session.flush()

        session.add(
            Job(
                id=1,
                source="seek",
                source_job_id="1",
                url="https://example.com/1",
                title="Senior Developer",
                company="Acme",
                location="Adelaide SA",
                dedupe_hash="h1",
                campaign_id=1,
                status=JobStatus.DOCUMENTS_READY,
            )
        )
        session.flush()

        for kind, name in (
            (DocumentKind.RESUME, "resume.pdf"),
            (DocumentKind.COVER_LETTER, "cover_letter.pdf"),
            (DocumentKind.COMBINED, "combined.pdf"),
        ):
            session.add(
                Document(
                    job_id=1,
                    kind=kind,
                    path=str(tmp_path / f"job_1/{name}"),
                    sha256="abc123",
                    parse_check_passed=True,
                    parse_report={
                        "cover_letter_text": "Dear Hiring Team, I would like to apply."
                    },
                )
            )

        session.add(Score(job_id=1, profile_version=1, rubric_version=1, final=97.0))
        session.add(
            AnswerBank(
                question_pattern="Do you have full working rights in Australia?",
                match_type=MatchType.FUZZY,
                answer_value="Yes",
                answer_type=AnswerType.BOOLEAN,
            )
        )
        session.commit()
        yield session


def steps() -> list[list[FormField]]:
    return [
        [
            FormField(identifier="name", label="Full name"),
            FormField(
                identifier="rights",
                label="Do you have full working rights in Australia?",
                choices=["Yes", "No"],
            ),
            FormField(identifier="resume", label="Resume", kind="file"),
        ]
    ]


def apply_once(session, adapter):
    return flow.run_apply(
        FakePage(),
        session,
        session.get(Job, 1),
        adapter=adapter,
        is_authenticated=lambda platform: True,
    )


# =========================================================================


def test_a_flawless_application_is_still_not_sent_by_default(perfect):
    """The property the whole design rests on."""
    assert settings.allow_live_submit is False

    # Prove first that ONLY the switch stands in the way. This has to happen
    # before the flow runs: an aborted run records an application row, and
    # "one application per job, ever" would then legitimately fail too, hiding
    # the very thing being asserted.
    verdict = guardrails.check_can_submit(
        perfect,
        perfect.get(Job, 1),
        _draft_for(perfect),
        is_authenticated=lambda platform: True,
    )
    assert [c.name for c in verdict.failures] == ["allow_live_submit"], (
        verdict.summary()
    )

    adapter = FakeAdapter(steps=steps())
    result = apply_once(perfect, adapter)

    assert adapter.submitted is False, "an application was submitted by default"
    assert result.outcome is ApplyOutcome.BLOCKED


def test_flipping_only_that_one_setting_lets_the_same_application_through(
    perfect, monkeypatch
):
    monkeypatch.setattr(settings, "allow_live_submit", True)

    adapter = FakeAdapter(steps=steps())
    result = apply_once(perfect, adapter)

    assert adapter.submitted is True, result.failure_reason
    assert result.outcome is ApplyOutcome.SUBMITTED

    application = perfect.exec(select(Application)).one()
    assert application.outcome is ApplicationOutcome.SUBMITTED
    assert application.attachment_readback, "the readback is recorded for the audit"
    assert application.answers_given, "the answers actually entered are recorded"


def _draft_for(session):
    return flow.build_draft(
        session, session.get(Job, 1), platform="seek", fields=steps()[0]
    )


# =========================================================================
# Repository-wide invariants
# =========================================================================

REPO = pathlib.Path(__file__).resolve().parent.parent


def _python_files(*relative: str) -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for rel in relative:
        out.extend(p for p in (REPO / rel).rglob("*.py") if ".venv" not in p.parts)
    return out


def test_nothing_in_the_repository_sets_allow_live_submit_true():
    """Not in code, not in a default, not in a fixture, not in .env.example."""
    offenders: list[str] = []

    for path in _python_files("backend", "tests"):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if "allow_live_submit" not in stripped.lower():
                continue
            if "monkeypatch.setattr" in stripped:
                continue  # a test deliberately enabling it, scoped to that test
            if "=" in stripped and "true" in stripped.lower():
                if stripped.startswith("#") or '"' in stripped or "'" in stripped:
                    continue  # a comment or a log message, not an assignment
                offenders.append(f"{path.relative_to(REPO)}: {stripped}")

    env_example = REPO / ".env.example"
    if env_example.exists():
        for line in env_example.read_text(encoding="utf-8").splitlines():
            if (
                line.strip().lower().startswith("allow_live_submit=")
                and "true" in line.lower()
            ):
                offenders.append(f".env.example: {line.strip()}")

    assert offenders == [], offenders


def test_the_config_default_is_false():
    source = (REPO / "backend/config.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "allow_live_submit"
        ):
            assert isinstance(node.value, ast.Constant)
            assert node.value.value is False
            return
    pytest.fail("allow_live_submit is not declared in backend/config.py")


def _called_names(path: pathlib.Path) -> set[str]:
    """Function names actually invoked in a module.

    Parsed rather than grepped: every one of these files legitimately *talks
    about* the guardrails in its docstring, and a text match would flag the
    documentation that exists to explain the rule.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def _imported_modules(path: pathlib.Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names if node.module)
    return names


def test_check_can_submit_is_called_from_exactly_one_place():
    """Hard rule 6: every submit path goes through the gate, once."""
    callers = [
        # as_posix(), not str(): on Windows str(PurePath) uses backslashes and
        # would never equal the POSIX literal below, so this rule-6 check would
        # fail on every Windows run for a reason that has nothing to do with
        # the guardrail it is protecting.
        path.relative_to(REPO).as_posix()
        for path in _python_files("backend")
        if path.name != "guardrails.py" and "check_can_submit" in _called_names(path)
    ]
    assert callers == ["backend/apply/flow.py"], callers


def test_no_adapter_imports_or_calls_the_guardrails():
    """Adapters supply selectors and step logic. Nothing else."""
    for name in ("seek.py", "linkedin.py"):
        path = REPO / "backend/apply" / name
        if not path.exists():
            continue
        imports = _imported_modules(path)
        assert not any("guardrails" in module for module in imports), (
            f"{name} imports guardrails"
        )
        assert "check_can_submit" not in _called_names(path), f"{name} calls the gate"


def test_answers_module_stays_free_of_playwright():
    tree = ast.parse((REPO / "backend/apply/answers.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "playwright" not in imported


def test_the_apply_layer_has_no_hardcoded_sleep_outside_pacing():
    """A fixed submit cadence is a bot signature; intervals come from pacing.py.

    Scoped to the apply layer deliberately. Discovery's polite delay between
    paged HTTP requests is a different concern with a different purpose — it is
    courtesy to a job board's servers, not the rhythm of applications arriving
    on someone's account.
    """
    offenders: list[str] = []
    for path in _python_files("backend/apply"):
        if path.name in {"pacing.py", "session.py"}:
            continue  # pacing owns intervals; session polls a human signing in
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "time.sleep(" in line and not line.strip().startswith("#"):
                offenders.append(f"{path.relative_to(REPO)}:{number}")
    assert offenders == [], offenders


def test_a_job_that_already_has_an_application_is_never_eligible_again(perfect):
    """Hard rule 5, at the selection step rather than at the submit.

    ``UNIQUE(job_id)`` on ``applications`` is the last line, and ``_run_apply``
    checks for an existing row too — but both of those fire after a browser tab
    has opened on the employer's site. This is the check that stops the job
    being picked up at all, and nothing covered it: emptying the set entirely
    left the whole suite green.

    Deliberately not folded into the status filter. A job whose status was reset
    by hand — from the dashboard, or by a re-queue — is still a job that has
    been applied to once.
    """
    session = perfect
    [(job, _campaign, _score)] = eligible_jobs(session)

    session.add(Application(job_id=job.id, outcome=ApplicationOutcome.SUBMITTED))
    job.status = JobStatus.DOCUMENTS_READY
    session.flush()

    assert eligible_jobs(session) == []
