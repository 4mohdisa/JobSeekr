"""Drive the whole pipeline end to end, offline, in one command.

    uv run python -m backend.rehearsal

Every other test in this project is a unit. They prove each block behaves, and
they cannot prove the blocks *fit*: a field renamed on one side of a seam, an
enum compared against a string, a status the next stage does not recognise — all
pass a unit suite and all break the pipeline. This runs the real functions in
the real order and asserts the seams.

What is real: the database and its migrations-equivalent schema, scoring's
selection and persistence, the Jinja/LaTeX build, pdflatex, the parse gate, the
apply flow's whole sequence, the guardrails, and the audit record.

What is substituted, and why:

* **The LLM.** Deterministic canned responses. The point is the wiring, not the
  model, and a rehearsal that needed an API key would not be run.
* **The browser.** A fake page and adapter. Playwright needs a browser binary
  and a real form; the flow's *sequence* is what matters here and it is
  identical either way. HAR replay is the one seam this cannot exercise — it
  needs recorded captures and an installed browser, neither of which exists on
  a fresh machine.
* **Nothing about safety.** ``ALLOW_LIVE_SUBMIT`` is never touched and the pass
  runs with ``dry_run=True``, so the run stops at the point of submission and
  records a dry run, which is exactly what it does in production until the user
  turns live submit on themselves.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session, SQLModel, create_engine, select

from backend.logging_setup import configure_logging, get_logger

# A guardrail name in a recorded failure_reason: an identifier immediately
# followed by a colon, after a separator. Anchored this way so the "dry run:"
# and "BLOCKED by " prefixes the message carries cannot be mistaken for one.
_GUARDRAIL_NAME = re.compile(r"(?:^|;|by )\s*([a-z_][a-z0-9_]*)\s*:")

log = get_logger(__name__)

__all__ = ["RehearsalReport", "Stage", "rehearse"]


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


@dataclass
class Stage:
    name: str
    ok: bool
    detail: str = ""

    def line(self) -> str:
        return f"  [{'PASS' if self.ok else 'FAIL'}] {self.name:24s} {self.detail}"


@dataclass
class RehearsalReport:
    stages: list[Stage] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.stages)

    def add(self, name: str, ok: bool, detail: str = "") -> Stage:
        stage = Stage(name=name, ok=ok, detail=detail)
        self.stages.append(stage)
        log.info("rehearsal_stage", stage=name, ok=ok, detail=detail[:200])
        return stage

    def render(self) -> str:
        body = "\n".join(s.line() for s in self.stages)
        verdict = "REHEARSAL PASSED" if self.ok else "REHEARSAL FAILED"
        return f"{body}\n\n{verdict}"


# --------------------------------------------------------------------------
# Substitutes
# --------------------------------------------------------------------------


AI_SLOTS = {
    "opening_hook": "The reporting and analysis work this role describes is the work I do now.",
    "skills_bridge": "Python and SQL have been the core of my day to day work.",
    "why_company": "The ad describes a small team owning its own data platform.",
    "closing": "I would welcome a conversation.",
}


class StubLLM:
    """Deterministic stand-in for the gateway, counting what it was asked."""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    def _count(self, purpose: str) -> None:
        self.calls[purpose] = self.calls.get(purpose, 0) + 1

    def complete(self, prompt: str, *, purpose: str = "", **kwargs: Any) -> str:
        self._count(purpose)
        return AI_SLOTS.get(purpose.removeprefix("document_"), "")

    def complete_json(self, prompt: str, *, purpose: str = "", **kwargs: Any) -> dict:
        self._count(purpose)
        if purpose == "form_mapping":
            return {"fields": []}
        # Stage-two rubric scoring.
        return {
            "overall": 82,
            "verdict": "strong",
            "dimensions": {},
            "reasons": ["Matches the stated stack."],
            "concerns": [],
        }

    def embed(
        self, texts: list[str], *, purpose: str = "", **kwargs: Any
    ) -> list[list[float]]:
        self._count(purpose or "embedding")
        # Deterministic, non-degenerate vectors: cosine must not be NaN.
        out = []
        for text in texts:
            seed = sum(ord(c) for c in text[:200]) or 1
            out.append([((seed * (i + 3)) % 97) / 97 + 0.01 for i in range(16)])
        return out


class FakePage:
    """Enough page for the flow. Records what it was asked to do."""

    def __init__(self) -> None:
        self.screenshots: list[str] = []
        self.url = "https://example.test/apply/1"

    def screenshot(self, path: str, full_page: bool = False) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")
        self.screenshots.append(path)

    def close(self) -> None:
        return None


@dataclass
class FakeAdapter:
    """A cooperative external-ATS adapter over a form with one screening question."""

    platform: str = "greenhouse"
    submitted: bool = False
    attached: list[Any] = field(default_factory=list)
    filled: dict[str, str] = field(default_factory=dict)

    def can_handle(self, job: Any) -> bool:
        return True

    def open(self, page: Any, job: Any) -> None:
        return None

    def detect_redirect(self, page: Any) -> bool:
        return False

    def detect_restriction(self, page: Any) -> bool:
        return False

    def enumerate_fields(self, page: Any, step: int) -> list[Any]:
        from backend.apply.draft import FormField

        if step > 0:
            return []
        return [
            FormField(identifier="name", label="Full name"),
            FormField(identifier="email", label="Email address"),
            FormField(
                identifier="rights",
                label="Do you have full working rights in Australia?",
                kind="radio",
                choices=["Yes", "No"],
            ),
            FormField(identifier="resume", label="Resume", kind="file"),
        ]

    def fill_field(self, page: Any, field_: Any, value: str) -> None:
        self.filled[field_.identifier] = value

    def upload_slots(self, fields: list[Any]) -> int:
        return 1

    def attach(self, page: Any, documents: list[Any]) -> None:
        self.attached = list(documents)

    def read_back_attachments(self, page: Any) -> list[str]:
        return [Path(d.path).name for d in self.attached]

    def is_last_step(self, page: Any, fields: list[Any]) -> bool:
        return True

    def advance(self, page: Any) -> None:
        return None

    def submit(self, page: Any) -> None:
        self.submitted = True

    def confirmed(self, page: Any) -> bool:
        return True


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def seed(session: Session) -> tuple[Any, Any]:
    """One profile, one campaign, two jobs. Obviously fictional, on purpose."""
    from backend.models import (
        AnswerBank,
        AnswerType,
        Campaign,
        GrayZoneAction,
        Job,
        JobStatus,
        MatchType,
        Profile,
    )

    profile = Profile(
        version=1,
        identity={
            "name": "Rehearsal Fixture",
            "email": "rehearsal@example.invalid",
            "phone": "+61 400 000 000",
            "location": "Adelaide SA",
            "headline": "Data Analyst",
            "summary": "Efficient financial reporting and certification workflow design.",
        },
        work_rights={"statement": "Australian citizen with full working rights."},
        experience=[
            {
                "title": "Senior Analyst",
                "company": "Redgum Analytics",
                "location": "Adelaide",
                "start": "2021",
                "end": "2026",
                "highlights": [
                    "Identified efficient financial reporting workflow improvements.",
                    "Built qualified candidate certification review tooling.",
                ],
            }
        ],
        education=[
            {
                "qualification": "BSc Computer Science",
                "institution": "University of Adelaide",
                "year": "2020",
            }
        ],
        skills=["Python", "SQL", "financial modelling"],
        certifications=[],
        projects=[],
        preferences={},
    )
    session.add(profile)

    campaign = Campaign(
        name="rehearsal",
        active=True,
        search_terms=["data analyst"],
        locations=["Adelaide SA"],
        score_floor=50.0,
        score_auto_apply=70.0,
        gray_zone_action=GrayZoneAction.QUEUE,
        daily_caps={"greenhouse": 10},
    )
    session.add(campaign)
    session.flush()

    session.add(
        AnswerBank(
            question_pattern="Do you have full working rights in Australia?",
            match_type=MatchType.FUZZY,
            answer_value="Yes",
            answer_type=AnswerType.BOOLEAN,
        )
    )

    # The board name comes from the registry, never a literal: adding a board
    # is meant to be one entry in backend/boards.py, and a hardcoded "seek"
    # here would be one more place to miss.
    from backend.boards import source_boards

    board_key = source_boards()[0].key
    jobs = []
    for n, (title, company) in enumerate(
        [("Data Analyst", "Wattle Group"), ("Reporting Analyst", "Redgum Corp")],
        start=1,
    ):
        job = Job(
            source=board_key,
            source_job_id=str(n),
            url=f"https://boards.greenhouse.io/example/jobs/{n}",
            title=title,
            company=company,
            location="Adelaide SA",
            description=(
                "We need Python and SQL for financial reporting. "
                "You will build efficient reporting workflows for a small team."
            ),
            dedupe_hash=f"rehearsal-{n}",
            campaign_id=campaign.id,
            status=JobStatus.DISCOVERED,
        )
        session.add(job)
        jobs.append(job)
    session.flush()
    return campaign, jobs


# --------------------------------------------------------------------------
# The rehearsal
# --------------------------------------------------------------------------


def rehearse(root: Path, *, keep: bool = False) -> RehearsalReport:
    """Run every stage in order against a throwaway database under ``root``."""
    from backend.apply import run as apply_run
    from backend.ats import generic as generic_module
    from backend.config import settings
    from backend.documents import build as build_module
    from backend.documents import verify as verify_module
    from backend.models import (
        Application,
        ApplicationOutcome,
        Document,
        Job,
        JobStatus,
        QuestionEvent,
        Score,
        Stage,
        StageTiming,
    )
    from backend.scoring import run as scoring_run
    from backend.scoring import stage1, stage2

    report = RehearsalReport()
    root.mkdir(parents=True, exist_ok=True)
    settings.data_dir = root

    stub = StubLLM()
    # verify_module included: the parse gate's fabrication self-check is an
    # LLM call too, and a module missing from this list makes a real one.
    for module in (build_module, verify_module, stage1, stage2, generic_module):
        if hasattr(module, "llm"):
            module.llm = stub

    engine = create_engine(
        f"sqlite:///{(root / 'rehearsal.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)

    @contextmanager
    def factory():
        session = Session(engine)
        try:
            yield session
            session.commit()
        finally:
            session.close()

    # -- 1. schema + fixtures ------------------------------------------------
    with factory() as session:
        campaign, jobs = seed(session)
        campaign_id = campaign.id
        job_ids = [j.id for j in jobs]
    report.add("seed", bool(job_ids), f"{len(job_ids)} jobs, 1 profile, 1 campaign")

    # -- 2. scoring ----------------------------------------------------------
    try:
        run = scoring_run.run_scoring(campaign_id=campaign_id, session_factory=factory)
        with factory() as session:
            scores = session.exec(select(Score)).all()
            top = max((s.final for s in scores), default=None)
        report.add(
            "scoring",
            bool(scores) and run.ok,
            f"{len(scores)} scores, best={top}, run.ok={run.ok}",
        )
    except Exception as exc:  # noqa: BLE001 - the report is the product
        report.add("scoring", False, f"{type(exc).__name__}: {exc}")
        scores = []

    # -- 3. documents + parse gate ------------------------------------------
    built_for: list[int] = []
    for job_id in job_ids:
        try:
            result = build_module.build_documents_for(job_id, session_factory=factory)
        except AttributeError:
            with factory() as session:
                result = build_module.build_documents(session, job_id)
        except Exception as exc:  # noqa: BLE001
            report.add("documents", False, f"job {job_id}: {type(exc).__name__}: {exc}")
            break
        if result.ok:
            built_for.append(job_id)
        else:
            report.add("documents", False, f"job {job_id}: {result.failure_reason}")
            break
    else:
        with factory() as session:
            docs = session.exec(select(Document)).all()
            gated = [d for d in docs if d.parse_check_passed]
        report.add(
            "documents+gate",
            len(built_for) == len(job_ids) and len(gated) == len(docs) and bool(docs),
            f"{len(docs)} documents, {len(gated)} passed the parse gate",
        )

    # -- 4. status handoff ---------------------------------------------------
    with factory() as session:
        statuses = {j.id: j.status for j in session.exec(select(Job)).all()}
    ready = [jid for jid, st in statuses.items() if st == JobStatus.DOCUMENTS_READY]
    report.add(
        "status handoff",
        bool(ready),
        f"documents_ready={len(ready)} of {len(job_ids)} ({ {str(s) for s in statuses.values()} })",
    )

    adapter = FakeAdapter()

    # -- 5. apply (dry run) --------------------------------------------------
    original_appliers = apply_run.build_appliers
    apply_run.build_appliers = lambda: [adapter]
    try:
        run = apply_run.run_apply_pass(
            campaign_id=campaign_id,
            dry_run=True,
            session_factory=factory,
            page_factory=FakePage,
        )
        report.add(
            "apply pass",
            run is not None,
            f"counts={run.counts if run else None}",
        )
    except Exception as exc:  # noqa: BLE001
        report.add("apply pass", False, f"{type(exc).__name__}: {exc}")
    finally:
        apply_run.build_appliers = original_appliers

    # -- 5b. guardrails, by name ---------------------------------------------
    #
    # Which guardrails failed, not merely that the run was blocked. Everything
    # is blocked while ALLOW_LIVE_SUBMIT is false, so "blocked" alone says
    # nothing and hides the interesting case: a guardrail failing for a reason
    # that is a bug rather than a setting. cover_letter_clean failed on every
    # application because nothing ever wrote parse_report["cover_letter_text"],
    # and the only evidence was a line in a log nobody reads.
    #
    # Read back out of the recorded failure_reason rather than by calling
    # check_can_submit here: hard rule 6 gives that function exactly one call
    # site, and a second one — even a read-only probe — is the beginning of a
    # second opinion about whether it is safe to submit.
    ENVIRONMENTAL = {
        "allow_live_submit",  # the master switch, deliberately off
        "inside_window",  # rehearsals run at whatever hour they run
        "session_authenticated",  # no browser session on a fresh machine
        "warmup_cap",
        "daily_cap",
    }
    with factory() as session:
        reasons = [
            a.failure_reason or "" for a in session.exec(select(Application)).all()
        ]
    failed: set[str] = set()
    for reason in reasons:
        failed.update(_GUARDRAIL_NAME.findall(reason))

    # The parse has to prove itself. An earlier version split on "; " and took
    # the text before the first colon, which silently swallowed the very first
    # guardrail because the message is prefixed "dry run: BLOCKED by ..." — so
    # the stage passed while under-reporting, which is worse than not checking.
    # allow_live_submit is false for the whole rehearsal and must always appear.
    parsed_ok = "allow_live_submit" in failed
    unexpected = failed - ENVIRONMENTAL
    report.add(
        "guardrails",
        parsed_ok and not unexpected,
        f"failing={sorted(failed)}"
        + (
            f"  UNEXPECTED={sorted(unexpected)}"
            if unexpected
            else (
                " (all environmental)"
                if parsed_ok
                else "  PARSE FAILED: no allow_live_submit"
            )
        ),
    )

    # -- 6. audit trail ------------------------------------------------------
    with factory() as session:
        applications = session.exec(select(Application)).all()
        outcomes = [a.outcome for a in applications]
        with_answers = [a for a in applications if a.answers_given]
        with_readback = [a for a in applications if a.attachment_readback]
    report.add(
        "audit trail",
        bool(applications),
        f"{len(applications)} application rows, outcomes={[str(o) for o in outcomes]}",
    )
    report.add(
        "audit completeness",
        bool(applications) and len(with_answers) == len(applications),
        f"answers recorded on {len(with_answers)}/{len(applications)}, "
        f"readback on {len(with_readback)}/{len(applications)}",
    )

    # -- 7. never submitted --------------------------------------------------
    report.add(
        "no live submit",
        adapter.submitted is False and settings.allow_live_submit is False,
        f"adapter.submit called={adapter.submitted}, "
        f"allow_live_submit={settings.allow_live_submit}",
    )
    report.add(
        "outcome recorded",
        bool(outcomes) and all(o is not None for o in outcomes),
        f"every application row carries an outcome ({[str(o) for o in outcomes]})",
    )
    if outcomes:
        report.add(
            "dry run recorded",
            all(o is not ApplicationOutcome.SUBMITTED for o in outcomes),
            "a dry run must never record a submission",
        )

    # -- 8. the telemetry is actually wired ----------------------------------
    #
    # Not "does the module work" — the unit tests answer that. This asks whether
    # a full pipeline run leaves any measurement behind at all. Four features in
    # this project shipped complete, tested, and never called; a stage timer
    # that nothing invokes is exactly that shape, and it would look identical to
    # a working one in every unit test.
    #
    # PACING and SUBMIT are legitimately absent from a dry run: nothing is
    # submitted, and nothing waits between submissions. The stages asserted here
    # are the ones a dry run genuinely reaches.
    timings = session.exec(select(StageTiming)).all()
    stages_seen = {row.stage for row in timings}
    expected_stages = {
        Stage.DOCUMENT_BUILD,
        Stage.PAGE_LOAD,
        Stage.FIELD_ENUMERATION,
        Stage.ANSWER_RESOLUTION,
    }
    report.add(
        "telemetry wired",
        expected_stages <= stages_seen,
        f"{len(timings)} stage timings, "
        f"{sorted(s.value for s in stages_seen)}"
        + (
            ""
            if expected_stages <= stages_seen
            else f" — missing {sorted(s.value for s in expected_stages - stages_seen)}"
        ),
    )

    questions_filed = session.exec(select(QuestionEvent)).all()
    report.add(
        "question ledger wired",
        bool(questions_filed),
        f"{len(questions_filed)} screening questions filed",
    )

    report.add("llm calls", True, f"{stub.calls}")

    if not keep:
        shutil.rmtree(root, ignore_errors=True)
    return report


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI wiring
    parser = argparse.ArgumentParser(prog="python -m backend.rehearsal")
    parser.add_argument(
        "--keep", action="store_true", help="leave the rehearsal directory in place"
    )
    parser.add_argument(
        "--root", default=None, help="where to build (default: a temp dir)"
    )
    args = parser.parse_args(argv)

    configure_logging()
    root = (
        Path(args.root)
        if args.root
        else Path(tempfile.mkdtemp(prefix="jobseekr-rehearsal-"))
    )

    started = datetime.now(UTC)
    report = rehearse(root, keep=args.keep or args.root is not None)
    elapsed = (datetime.now(UTC) - started).total_seconds()

    sys.stdout.write("\nOFFLINE END-TO-END REHEARSAL\n")
    sys.stdout.write("=" * 72 + "\n")
    sys.stdout.write(report.render() + f"\n({elapsed:.1f}s)\n")
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
