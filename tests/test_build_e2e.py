"""Build three real PDFs end to end, and prove fabrication is actually blocked.

The LLM is stubbed; pdflatex is real. The negative test at the bottom is the
one that matters most: a model that returns invented employers, dates and
metrics must fail the build, not produce a confident-looking lie about the user.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from backend.documents import build as build_module
from backend.documents.build import build_documents
from backend.documents.engine import (
    find_ai_slots,
    render_string,
    template_root,
    validate_placeholders,
)
from backend.documents.fabrication import validate_no_fabrication
from backend.documents.latex import escape_latex
from backend.models import (
    Campaign,
    DocumentKind,
    GrayZoneAction,
    Job,
    JobStatus,
    Profile,
)
from tests.conftest import needs_pdflatex

pytestmark = needs_pdflatex


CLEAN_TEXT = {
    "opening_hook": "Building reporting pipelines at Redgum Analytics is the work this role describes.",
    "skills_bridge": "Python and SQL have been the core of my day to day work.",
    "why_company": "The ad describes a small team owning its own data platform, which is how I like to work.",
    "closing": "I would welcome a conversation.",
}

FABRICATED_TEXT = {
    "opening_hook": "As a Certified AWS Solutions Architect since 2019, I increased revenue 340%.",
    "skills_bridge": "At Acme Corporation I led a team of 40 engineers.",
    "why_company": "My Master of Data Science from Stanford University prepared me for this.",
    "closing": "Available immediately.",
}


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(build_module.settings, "data_dir", tmp_path)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(
            Profile(
                version=1,
                identity={
                    "name": "Jordan Fitzgerald",
                    "email": "jordan.fitzgerald@example.com",
                    "phone": "+61 412 345 678",
                    "location": "Adelaide SA",
                    "headline": "Data Analyst",
                    "summary": "Efficient financial reporting and certification workflow design.",
                },
                work_rights={
                    "statement": "Australian citizen with full working rights."
                },
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
        )
        campaign = Campaign(
            name="analytics",
            search_terms=["data analyst"],
            locations=["Adelaide SA"],
            score_floor=60.0,
            score_auto_apply=80.0,
            gray_zone_action=GrayZoneAction.QUEUE,
        )
        s.add(campaign)
        s.flush()
        s.add(
            Job(
                id=1,
                source="seek",
                source_job_id="1",
                url="https://example.com/1",
                title="Data Analyst",
                company="Wattle Group",
                location="Adelaide SA",
                description="We need Python and SQL for financial reporting.",
                dedupe_hash="h1",
                campaign_id=campaign.id,
            )
        )
        s.commit()
        yield s


def stub_llm(monkeypatch, values: dict[str, str]):
    def fake_complete(prompt, *, purpose, **kwargs):
        slot = purpose.removeprefix("document_")
        return values.get(slot, "")

    monkeypatch.setattr(build_module.llm, "complete", fake_complete)

    # The parse gate's fabrication self-check is an LLM call too. Left
    # unstubbed it attempts a real one per document and the file's runtime goes
    # from two seconds to twenty-six on LiteLLM's retry backoff. Returning "no
    # unsupported claims" keeps these tests about the deterministic gate, which
    # is what they are for — the self-check has its own tests.
    from backend.documents import verify as verify_module

    monkeypatch.setattr(
        verify_module.llm, "complete_json", lambda *a, **k: {"unsupported": []}
    )


# ------------------------------------------------------------------ escaping


def test_latex_escaping_survives_hostile_user_data():
    assert escape_latex("Smith & Co") == r"Smith \& Co"
    assert escape_latex("100% remote") == r"100\% remote"
    assert escape_latex("C#") == r"C\#"
    assert escape_latex("R&D_team") == r"R\&D\_team"
    assert escape_latex("a\\b") == r"a\textbackslash{}b"
    assert escape_latex("~x^2") == r"\textasciitilde{}x\textasciicircum{}2"
    assert escape_latex(None) == ""
    # The backslash must be escaped FIRST, or later replacements double-escape.
    assert "textbackslash" in escape_latex("\\&")


def test_placeholder_validation_catches_the_typo_it_exists_for():
    issues = validate_placeholders(r"Dear \VAR{job.compnay}")
    assert any(i.kind == "unknown_field" and "compnay" in i.placeholder for i in issues)


def test_wrong_delimiters_are_reported():
    issues = validate_placeholders("Dear {{job.company}}")
    assert any(i.kind == "wrong_delimiters" for i in issues)


def test_a_correct_template_has_no_issues():
    body = r"Dear \VAR{job.company}, I am \VAR{profile.name}. \VAR{ai.opening_hook}"
    assert validate_placeholders(body) == []


def test_find_ai_slots_returns_only_the_slots_used():
    body = r"\VAR{ai.opening_hook} and \VAR{ai.closing}"
    assert [slot.name for slot in find_ai_slots(body)] == ["opening_hook", "closing"]


def test_rendering_escapes_substituted_values_automatically():
    out = render_string(
        r"Company: \VAR{job.company}", {"job": {"company": "Smith & Co"}}
    )
    assert r"Smith \& Co" in out


# -------------------------------------------------------------- anti-fabrication


def test_fabricated_claims_are_detected(session):
    profile = session.get(Profile, 1)
    job = session.get(Job, 1)

    violations = validate_no_fabrication(
        "As a Certified AWS Solutions Architect since 2019, I increased revenue 340% "
        "at Acme Corporation.",
        profile,
        job,
    )
    kinds = {v.kind for v in violations}
    assert "year" in kinds, violations
    assert "metric" in kinds, violations
    assert "credential" in kinds, violations


def test_truthful_narrative_is_allowed(session):
    profile = session.get(Profile, 1)
    job = session.get(Job, 1)
    violations = validate_no_fabrication(
        "At Redgum Analytics I built efficient financial reporting workflows using "
        "Python and SQL.",
        profile,
        job,
    )
    assert violations == [], violations


def test_the_target_company_may_be_named(session):
    """Naming the employer being applied to is not fabrication."""
    profile = session.get(Profile, 1)
    job = session.get(Job, 1)
    assert (
        validate_no_fabrication(
            "Wattle Group is hiring for work I have done.", profile, job
        )
        == []
    )


# ------------------------------------------------------------------- full build


def test_full_build_produces_three_gated_pdfs(session, monkeypatch):
    stub_llm(monkeypatch, CLEAN_TEXT)
    result = build_module.build_documents(session, 1)

    assert result.ok, result.failure_reason
    assert set(result.documents) == {"resume", "cover_letter", "combined"}

    for kind in (DocumentKind.RESUME, DocumentKind.COVER_LETTER, DocumentKind.COMBINED):
        document = result.documents[kind.value]
        assert document.parse_check_passed, result.reports[kind.value].summary()
        assert document.sha256, "every document records a content hash"

    assert result.reports["resume"].pages <= 2
    assert result.reports["cover_letter"].pages == 1
    assert session.get(Job, 1).status == JobStatus.DOCUMENTS_READY


def test_sha256_matches_the_file_on_disk(session, monkeypatch):
    from pathlib import Path

    stub_llm(monkeypatch, CLEAN_TEXT)
    result = build_module.build_documents(session, 1)
    document = result.documents["resume"]
    assert build_module._sha256(Path(document.path)) == document.sha256


def test_a_fabricating_model_fails_the_build_loudly(session, monkeypatch):
    """The negative case that matters: no document is produced at all."""
    stub_llm(monkeypatch, FABRICATED_TEXT)
    result = build_module.build_documents(session, 1)

    assert result.ok is False
    assert result.violations, "the specific fabrications must be reported"
    assert "unsupported facts" in (result.failure_reason or "")
    assert result.documents == {}, "a fabricating build must produce NO document"
    assert session.get(Job, 1).status == JobStatus.FAILED


def test_aux_files_are_cleaned_up(session, monkeypatch):
    from pathlib import Path

    stub_llm(monkeypatch, CLEAN_TEXT)
    result = build_module.build_documents(session, 1)
    out_dir = Path(result.documents["resume"].path).parent
    leftovers = [
        p.name for p in out_dir.iterdir() if p.suffix in {".aux", ".log", ".out"}
    ]
    assert leftovers == [], leftovers


# ------------------------------------------------- the fact / narrative split
#
# The resume's only generated content is its experience bullet points, and they
# are generated FROM the user's own bullets. Everything else on the page is
# substitution. These tests pin that boundary in both directions: the generated
# half must be generated, and it must never be allowed to invent.


def test_the_resume_template_declares_its_generated_slot():
    """ai.bullets is read from a block expression, not a substitution.

    find_ai_slots originally scanned \\VAR{...} only, so the resume reported
    itself as using no slots at all — and a slot that is used but not generated
    is a StrictUndefined failure at render time, on every build.
    """
    body = (template_root() / "resume.tex.j2").read_text(encoding="utf-8")
    assert r"\VAR{ai." not in body, (
        "the resume reads ai.bullets from a block, not a var"
    )
    assert [slot.name for slot in find_ai_slots(body)] == ["bullets"]


def test_the_shipped_templates_have_no_placeholder_issues():
    """What the dashboard's template editor will show the user for each one.

    Loop variables are the reason this is worth asserting: `role`, `project`
    and `entry` are bound by the template itself, and reporting them as unknown
    namespaces filled the editor with fourteen errors that were not errors.
    """
    for filename in ("resume.tex.j2", "cover_letter.tex.j2", "email.txt.j2"):
        body = (template_root() / filename).read_text(encoding="utf-8")
        assert validate_placeholders(body) == [], filename


def test_the_resume_expectations_carry_every_record_heading(session, monkeypatch):
    """Employers and institutions reach the gate as line_starts.

    Without them the record-boundary check has nothing to check, and a resume
    whose education entries have run together passes silently.
    """
    stub_llm(monkeypatch, CLEAN_TEXT)
    result = build_documents(session, 1)
    assert result.ok, result.failure_reason

    document = result.documents["resume"]
    checks = {c["name"] for c in document.parse_report["checks"]}
    assert "record_headings_start_a_line" in checks, sorted(checks)

    detail = next(
        c["detail"]
        for c in document.parse_report["checks"]
        if c["name"] == "record_headings_start_a_line"
    )
    # Two headings: the one employer and the one institution in the fixture.
    assert "2 record headings" in detail, detail


# --------------------------------------------------------------- role bullets


def _bullets_for(session, monkeypatch, answer):
    """Generate the first role's bullets with the model returning ``answer``."""
    from backend.documents.build import generate_role_bullets

    profile = session.exec(select(Profile).order_by(Profile.version.desc())).first()
    job = session.get(Job, 1)

    def fake_complete(prompt, *, purpose, **kwargs):
        if purpose == "document_role_bullets":
            if isinstance(answer, Exception):
                raise answer
            return answer
        return ""

    monkeypatch.setattr(build_module.llm, "complete", fake_complete)
    return generate_role_bullets(profile=profile, job=job), profile


VERBATIM_HIGHLIGHTS = [
    "Identified efficient financial reporting workflow improvements.",
    "Built qualified candidate certification review tooling.",
]


def test_role_bullets_fall_back_to_the_users_own_words(session, monkeypatch):
    """No API key is not a failed build — it is the user's own resume.

    Unlike the cover letter's paragraphs, a bullet already has a correct
    version: the one the user wrote. Falling back to it is the safe answer, and
    returning nothing would silently delete their work history.
    """
    bullets, _ = _bullets_for(session, monkeypatch, RuntimeError("no API key"))
    assert bullets == [VERBATIM_HIGHLIGHTS]


def test_role_bullets_reject_a_changed_bullet_count(session, monkeypatch):
    """Two bullets in, one bullet out means a fact was dropped or merged."""
    bullets, _ = _bullets_for(
        session, monkeypatch, "Merged both bullets into one line."
    )
    assert bullets == [VERBATIM_HIGHLIGHTS], (
        "a model that returned the wrong number of bullets was accepted"
    )


def test_role_bullets_reject_a_fabricated_rewrite(session, monkeypatch):
    """The count is right and the content is invented. Still refused."""
    fabricated = (
        "Led a team of 40 engineers at Acme Corporation since 2019.\n"
        "Increased revenue 340% as a Certified AWS Solutions Architect."
    )
    bullets, profile = _bullets_for(session, monkeypatch, fabricated)
    assert bullets == [VERBATIM_HIGHLIGHTS], "fabricated bullets reached the document"

    # The premise: those really are fabrications by the project's own validator,
    # so this test fails for the reason it claims to.
    assert validate_no_fabrication(fabricated, profile, session.get(Job, 1))


def test_role_bullets_accept_an_honest_rewrite(session, monkeypatch):
    """The control. A truthful rewrite of the same facts is used."""
    honest = (
        "Improved efficient financial reporting workflows.\n"
        "Built certification review tooling for qualified candidates."
    )
    bullets, _ = _bullets_for(session, monkeypatch, honest)
    assert bullets == [honest.splitlines()]


def test_bullets_stay_aligned_with_the_rows_the_template_loops_over(session):
    """ai.bullets is indexed by loop position, so the two lists must agree.

    The template iterates ``_normalise_rows(profile.experience, ...)``, which
    DROPS anything that is not a dict. Generating bullets straight from
    ``profile.experience`` kept those rows, and one junk row shifted every
    later role onto the previous employer's bullet points — a fabrication
    containing no invented words, which nothing downstream could catch.
    """
    from backend.documents.build import (
        _ROW_DEFAULTS,
        _normalise_rows,
        generate_role_bullets,
    )

    profile = session.exec(select(Profile).order_by(Profile.version.desc())).first()
    profile.experience = [
        {"company": "Redgum Analytics", "highlights": ["First employer's bullet."]},
        "a row that is not a dict at all",
        {"company": "Wattle Group", "highlights": ["Second employer's bullet."]},
    ]

    rows = _normalise_rows(profile.experience, _ROW_DEFAULTS["experience"])
    bullets = generate_role_bullets(profile=profile, job=session.get(Job, 1))

    assert len(bullets) == len(rows), (
        f"{len(rows)} rows rendered but {len(bullets)} bullet lists generated — "
        "every role after the junk row gets the wrong employer's bullets"
    )
    assert bullets[
        rows.index(next(r for r in rows if r["company"] == "Wattle Group"))
    ] == ["Second employer's bullet."]
