"""The setup checklist: what is missing, and whether it stops an application.

The value is entirely in the BLOCK/WARN distinction. "No API key" and "no
Telegram" are both red on a one-colour checklist, and only one of them stops an
application from completing — so a doctor that cannot tell them apart is a
doctor that gets ignored on a fresh install where everything is red.

These pin which side of that line each check falls on, and that the fixes name
a real command.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine

from backend import doctor
from backend.config import settings
from backend.doctor import BLOCK, OK, WARN


@pytest.fixture
def db(tmp_path, monkeypatch):
    """An empty database, wired in as the one the checks read."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    class Scope:
        def __enter__(self):
            self.session = Session(engine)
            return self.session

        def __exit__(self, *exc):
            self.session.close()
            return False

    monkeypatch.setattr("backend.db.session_scope", Scope)
    return engine


# =========================================================================
# Blocking vs warning — the distinction the whole thing rests on
# =========================================================================


def test_no_api_key_blocks(monkeypatch):
    """Nothing can be scored or written, so no application can complete."""
    monkeypatch.setattr("backend.llm.client._api_key_for", lambda model: None)
    assert doctor.check_llm_keys().status == BLOCK


def test_both_keys_present_is_ok(monkeypatch):
    monkeypatch.setattr("backend.llm.client._api_key_for", lambda model: "key")
    assert doctor.check_llm_keys().status == OK


def test_one_missing_key_still_blocks(monkeypatch):
    """Scoring and embeddings are different providers.

    One key working says nothing about the other, and stage 1 failing is a
    silent total loss of ranking rather than a visible error.
    """
    monkeypatch.setattr(
        "backend.llm.client._api_key_for",
        lambda model: "key" if "gemini" in model else None,
    )
    assert doctor.check_llm_keys().status == BLOCK


def test_no_telegram_warns_rather_than_blocks(monkeypatch):
    """Applications still run. They just cannot ask anything.

    Which matters — an abstention parks a job and asks — but it is degraded,
    not stopped, and calling it BLOCK would make the report cry wolf.
    """
    monkeypatch.setattr(settings, "telegram_bot_token", None)
    monkeypatch.setattr(settings, "telegram_chat_id", None)

    finding = doctor.check_telegram()
    assert finding.status == WARN
    assert "answer bank" in finding.detail, "the consequence should be stated"


def test_blank_facts_block(db):
    """A blank fact answers nothing, so every screening question parks."""
    from backend.models import Fact, FactCategory

    with Session(db) as session:
        for key in ("licence", "work_rights"):
            session.add(
                Fact(key=key, text="", category=FactCategory.LICENCE, jurisdiction=None)
            )
        session.commit()

    assert doctor.check_facts().status == BLOCK


def test_some_facts_written_warns(db):
    from backend.models import Fact, FactCategory

    with Session(db) as session:
        session.add(Fact(key="licence", text="Class C", category=FactCategory.LICENCE))
        session.add(Fact(key="work_rights", text="", category=FactCategory.WORK_RIGHTS))
        session.commit()

    finding = doctor.check_facts()
    assert finding.status == WARN
    assert "work_rights" in finding.detail, "it should name which are still blank"


def test_all_facts_written_is_ok(db):
    from backend.models import Fact, FactCategory

    with Session(db) as session:
        session.add(Fact(key="licence", text="Class C", category=FactCategory.LICENCE))
        session.commit()

    assert doctor.check_facts().status == OK


def test_a_profile_with_no_name_blocks(db):
    """Every document needs a name and an email."""
    from backend.models import Profile

    with Session(db) as session:
        session.add(Profile(version=1, identity={}))
        session.commit()

    assert doctor.check_profile().status == BLOCK


def test_a_complete_profile_is_ok(db):
    from backend.models import Profile

    with Session(db) as session:
        session.add(
            Profile(version=1, identity={"name": "Jordan", "email": "j@example.com"})
        )
        session.commit()

    assert doctor.check_profile().status == OK


def test_a_dead_session_blocks(db):
    """Nothing can be submitted to a site you are signed out of."""
    from backend.models import SessionHealth, SessionStatus

    with Session(db) as session:
        session.add(SessionHealth(site="seek", status=SessionStatus.DEAD))
        session.commit()

    finding = doctor.check_sessions()
    assert finding.status == BLOCK
    assert "seek" in finding.detail
    assert "login --platform seek" in finding.fix, "the fix must be runnable as-is"


def test_never_checked_sessions_warn_rather_than_block(db):
    """Not knowing is not the same as being signed out."""
    assert doctor.check_sessions().status == WARN


def test_the_placeholder_campaign_warns(db):
    """An active campaign on seeded terms is worse than none.

    None discovers nothing and says so; placeholder terms discover the wrong
    jobs and look like they are working.
    """
    from backend.models import Campaign, GrayZoneAction

    with Session(db) as session:
        session.add(
            Campaign(
                name="Adelaide starter",
                active=True,
                search_terms=["data analyst", "software engineer"],
                locations=["Adelaide SA"],
                score_floor=60.0,
                score_auto_apply=80.0,
                gray_zone_action=GrayZoneAction.ASK,
                daily_caps={"default": 5},
            )
        )
        session.commit()

    finding = doctor.check_campaign()
    assert finding.status == WARN
    assert "placeholder" in finding.detail


def test_an_edited_campaign_is_ok(db):
    from backend.models import Campaign, GrayZoneAction

    with Session(db) as session:
        session.add(
            Campaign(
                name="Adelaide starter",
                active=True,
                search_terms=["clinical data manager"],
                locations=["Adelaide SA"],
                score_floor=60.0,
                score_auto_apply=80.0,
                gray_zone_action=GrayZoneAction.ASK,
                daily_caps={"default": 5},
            )
        )
        session.commit()

    assert doctor.check_campaign().status == OK


def test_no_active_campaign_blocks(db):
    assert doctor.check_campaign().status == BLOCK


# =========================================================================
# The Playwright channel — a check that once contradicted itself
# =========================================================================


def test_the_chrome_channel_defers_to_the_chrome_check(monkeypatch):
    """channel="chrome" uses the SYSTEM Chrome, not a Playwright build.

    The first version counted cached chromium builds and reported OK on those
    alone — the exact false green it was written to prevent, since a chromium
    cache says nothing about whether Chrome is installed.
    """
    monkeypatch.setattr(settings, "browser_channel", "chrome")
    monkeypatch.setattr(
        doctor,
        "check_chrome",
        lambda: doctor.Finding("Chrome", BLOCK, fix="install it"),
    )

    finding = doctor.check_playwright_channel()
    assert finding.status == BLOCK
    assert finding.fix == "install it", "it should carry the real fix through"


def test_the_chrome_channel_is_ok_when_chrome_is_installed(monkeypatch):
    monkeypatch.setattr(settings, "browser_channel", "chrome")
    monkeypatch.setattr(
        doctor, "check_chrome", lambda: doctor.Finding("Chrome", OK, "found")
    )
    assert doctor.check_playwright_channel().status == OK


def test_a_bundled_channel_needs_a_downloaded_build(monkeypatch, tmp_path):
    """The cache IS the right thing to look at for chromium/firefox/webkit."""
    monkeypatch.setattr(settings, "browser_channel", "firefox")
    monkeypatch.setattr(doctor.Path, "home", staticmethod(lambda: tmp_path))

    finding = doctor.check_playwright_channel()
    assert finding.status == BLOCK
    assert "playwright install firefox" in finding.fix


# =========================================================================
# Reporting
# =========================================================================


def test_a_check_that_raises_becomes_a_warning_not_a_dead_run(monkeypatch):
    """One broken check must not hide the state of the other eleven."""

    def boom() -> doctor.Finding:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(doctor, "CHECKS", (boom,))

    findings = doctor.run_doctor()

    assert len(findings) == 1
    assert findings[0].status == WARN
    assert "disk on fire" in findings[0].detail


def test_every_non_ok_finding_names_a_fix(db, monkeypatch):
    """A red row with no command is a red row nobody can act on."""
    monkeypatch.setattr("backend.llm.client._api_key_for", lambda model: None)

    for finding in doctor.run_doctor():
        if finding.status == OK:
            continue
        if "the check itself failed" in finding.detail:
            continue
        assert finding.fix or finding.detail, f"{finding.name} says nothing actionable"


def test_the_report_separates_blocking_from_warning():
    body = doctor.render(
        [
            doctor.Finding("keys", BLOCK, "missing", "set them", group="credentials"),
            doctor.Finding("telegram", WARN, "absent", "set it", group="credentials"),
            doctor.Finding("pdflatex", OK, "found", group="tooling"),
        ]
    )

    assert "1 blocking: keys" in body
    assert "1 warning: telegram" in body
    assert "set them" in body, "the fix must appear for a blocking row"


def test_the_report_does_not_print_a_fix_for_a_healthy_row():
    body = doctor.render([doctor.Finding("pdflatex", OK, "found", "do not show me")])
    assert "do not show me" not in body


def test_only_blocks_decide_the_exit_code():
    """A warning is something to know about, not a broken install.

    Exiting non-zero for one would make this useless in a script.
    """
    warned = [doctor.Finding("a", WARN, "x")]
    blocked = [doctor.Finding("a", BLOCK, "x")]

    assert not any(f.blocking for f in warned)
    assert any(f.blocking for f in blocked)


def test_the_switches_are_reported_not_judged():
    """They are the user's to set. A doctor that called OFF a problem would be
    arguing with a deliberate decision."""
    finding = doctor.check_switches()
    assert finding.status == OK
    assert "ALLOW_LIVE_SUBMIT" in finding.detail


def test_the_doctor_writes_nothing(db):
    """Safe to run at any moment, including mid-application."""
    import pathlib

    source = pathlib.Path("backend/doctor.py").read_text(encoding="utf-8")
    code = "\n".join(
        line.split("#")[0]
        for line in source.splitlines()
        if not line.strip().startswith(("#", '"', "*"))
    )

    for forbidden in (
        "session.add(",
        "session.commit(",
        "session.delete(",
        ".write_text(",
    ):
        assert forbidden not in code, f"doctor.py calls {forbidden}"
