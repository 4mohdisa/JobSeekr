"""Per-site session health: the silent failure, made loud.

Session expiry has no symptom of its own. The adapter lands on a login page,
cannot find the form, parks the job — so what the user sees is a pile of parked
jobs several days later and nothing naming the cause.

These pin the four things that matter:

* a login page means DEAD, and the alert names the site
* not knowing is UNKNOWN, and UNKNOWN does not alert
* the alert fires on the transition, not on every pass
* nothing ever attempts a login
"""

from __future__ import annotations

import inspect

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from backend import sessions
from backend.models import SessionHealth, SessionStatus


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


class FakeLocator:
    def __init__(self, present: bool) -> None:
        self._present = present

    @property
    def first(self) -> FakeLocator:
        return self

    def is_visible(self, timeout: int = 0) -> bool:
        return self._present

    def count(self) -> int:
        return 1 if self._present else 0


class FakePage:
    """A page where exactly the selectors in ``present`` exist."""

    def __init__(self, present: set[str] | None = None, *, goto_raises: bool = False):
        self.present = present or set()
        self.goto_raises = goto_raises
        self.visited: list[str] = []

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(selector in self.present)

    def goto(self, url: str, **kwargs: object) -> None:
        if self.goto_raises:
            raise RuntimeError("net::ERR_CONNECTION_REFUSED")
        self.visited.append(url)


class FakeContext:
    def __init__(self, cookies: list[dict[str, str]] | None = None):
        self._cookies = cookies or []

    def cookies(self) -> list[dict[str, str]]:
        return self._cookies


# =========================================================================
# Detecting a dead session
# =========================================================================


def test_a_password_field_means_signed_out():
    """The signal, and the reason this needs almost no per-site config.

    Every login page has a password input. "A signed-in element is present"
    needs a selector per site and breaks whenever a site reshuffles its header.
    """
    check = sessions.check_site(
        FakePage({"input[type='password']"}), "pageup", "https://pageup.example/"
    )
    assert check.status is SessionStatus.DEAD
    assert "login page" in check.detail


def test_the_generic_marker_works_on_a_site_nobody_configured():
    """The whole point: the user signs into ATS accounts we have no config for."""
    check = sessions.check_site(
        FakePage({"input[type='password']"}),
        "some-employer-careers-site.example",
        "https://some-employer-careers-site.example/",
    )
    assert check.status is SessionStatus.DEAD


def test_a_two_step_login_screen_is_still_recognised():
    """Some sites ask for the email first, so screen one has no password field."""
    check = sessions.check_site(
        FakePage({"form[action*='login' i] input[type='email']"}),
        "jobadder",
        "https://jobadder.example/",
    )
    assert check.status is SessionStatus.DEAD


def test_no_login_page_and_no_marker_is_unknown_not_dead():
    """UNKNOWN is a real outcome, not a failure to decide.

    Reporting it as DEAD would page the user about a healthy session every time
    a site changed its markup — and an alert that cries wolf is an alert that
    gets muted before the real one arrives.
    """
    check = sessions.check_site(FakePage(), "greenhouse", "https://greenhouse.example/")
    assert check.status is SessionStatus.UNKNOWN


def test_an_unreachable_site_says_nothing_about_the_session():
    check = sessions.check_site(
        FakePage(goto_raises=True), "seek", "https://seek.example/"
    )
    assert check.status is SessionStatus.UNREACHABLE
    assert check.status is not SessionStatus.DEAD


def test_a_recognised_signed_in_element_confirms_live(monkeypatch):
    monkeypatch.setattr(sessions, "_logged_in_marker_present", lambda page, site: True)
    check = sessions.check_site(FakePage(), "linkedin", "https://linkedin.example/")
    assert check.status is SessionStatus.LIVE


def test_a_login_page_beats_a_signed_in_element(monkeypatch):
    """If both somehow match, being asked to log in is the safe reading."""
    monkeypatch.setattr(sessions, "_logged_in_marker_present", lambda page, site: True)
    check = sessions.check_site(
        FakePage({"input[type='password']"}), "linkedin", "https://linkedin.example/"
    )
    assert check.status is SessionStatus.DEAD


# =========================================================================
# Recording, and alerting exactly once
# =========================================================================


def test_a_dead_session_alerts_and_names_the_site(session):
    alerts: list[tuple] = []
    sessions.on_session_dead = lambda *args: alerts.append(args)
    try:
        sessions.record(
            session, sessions.SiteCheck("pageup", SessionStatus.DEAD, "login page", 4)
        )
        session.flush()
    finally:
        sessions.on_session_dead = None

    assert len(alerts) == 1
    assert alerts[0][0] == "pageup", "the alert must name the specific site"


def test_the_alert_fires_on_the_transition_not_every_pass(session):
    """A session stays dead until the user signs in.

    Paging them on every pass about a thing they already know is how the channel
    stops being read before the next real alert arrives.
    """
    alerts: list[tuple] = []
    sessions.on_session_dead = lambda *args: alerts.append(args)
    try:
        for _ in range(4):
            sessions.record(
                session,
                sessions.SiteCheck("pageup", SessionStatus.DEAD, "login page", 4),
            )
            session.flush()
    finally:
        sessions.on_session_dead = None

    assert len(alerts) == 1, f"alerted {len(alerts)} times for one dead session"
    assert session.exec(select(SessionHealth)).one().consecutive_failures == 4


def test_recovering_then_dying_again_alerts_again(session):
    """The suppression is per episode, not forever."""
    alerts: list[tuple] = []
    sessions.on_session_dead = lambda *args: alerts.append(args)
    try:
        sessions.record(session, sessions.SiteCheck("pageup", SessionStatus.DEAD))
        session.flush()
        sessions.record(session, sessions.SiteCheck("pageup", SessionStatus.LIVE))
        session.flush()
        sessions.record(session, sessions.SiteCheck("pageup", SessionStatus.DEAD))
        session.flush()
    finally:
        sessions.on_session_dead = None

    assert len(alerts) == 2


def test_unknown_does_not_alert(session):
    alerts: list[tuple] = []
    sessions.on_session_dead = lambda *args: alerts.append(args)
    try:
        for status in (
            SessionStatus.UNKNOWN,
            SessionStatus.UNREACHABLE,
            SessionStatus.NO_SESSION,
            SessionStatus.LIVE,
        ):
            sessions.record(session, sessions.SiteCheck("x", status))
            session.flush()
    finally:
        sessions.on_session_dead = None

    assert alerts == []


def test_only_a_live_check_moves_last_verified_at(session):
    """The two timestamps answer different questions.

    "Checked a minute ago, last confirmed good four days ago" is a session that
    has been dead for four days, and one timestamp cannot say that.
    """
    sessions.record(session, sessions.SiteCheck("seek", SessionStatus.LIVE))
    session.flush()
    row = session.exec(select(SessionHealth)).one()
    verified = row.last_verified_at
    assert verified is not None

    sessions.record(session, sessions.SiteCheck("seek", SessionStatus.DEAD))
    session.flush()
    row = session.exec(select(SessionHealth)).one()

    assert row.last_checked_at is not None
    assert row.last_verified_at == verified, "a dead check must not refresh 'last good'"


def test_a_live_check_resets_the_failure_count(session):
    for _ in range(3):
        sessions.record(session, sessions.SiteCheck("seek", SessionStatus.DEAD))
        session.flush()
    sessions.record(session, sessions.SiteCheck("seek", SessionStatus.LIVE))
    session.flush()

    assert session.exec(select(SessionHealth)).one().consecutive_failures == 0


# =========================================================================
# Which sites get checked
# =========================================================================


def test_cookie_hosts_are_kept_whole():
    """The full host, not a registrable domain.

    Collapsing careers.acme.com.au to acme.com.au would send the check to the
    employer's marketing site, which has no login page — reporting a dead
    careers portal as healthy.
    """
    counts = sessions.sites_with_cookies(
        FakeContext(
            [
                {"domain": ".careers.acme.com.au"},
                {"domain": "careers.acme.com.au"},
                {"domain": ".linkedin.com"},
            ]
        )
    )
    assert counts.get("careers.acme.com.au") == 2, counts
    assert counts.get("linkedin.com") == 1


def test_the_several_hosts_of_one_platform_merge_to_one_site():
    """Merging is by platform key, not by guessing at suffix rules.

    au.seek.com matters specifically: boards.py lists only seek.com.au, while
    the host Seek actually serves today was verified into regions.py by the
    2026-09-03 probe. Reading one list found the old host and missed the live
    one.
    """
    for host in ("www.seek.com.au", "au.seek.com", "nz.seek.com", "www.seek.co.nz"):
        assert sessions._site_for_domain(host) == "seek", host
    assert sessions._site_for_domain("www.linkedin.com") == "linkedin"


def test_an_ats_cookie_domain_is_named_by_the_existing_detector():
    """Same logic that names a job URL, not a second matcher.

    The first version read `platform.domains`, which is spelled `host_patterns`
    — so it silently matched nothing and every ATS looked like an unknown site.
    A cookie domain is now resolved through detect_from_url.
    """
    assert sessions._site_for_domain("boards.greenhouse.io") == "greenhouse"
    assert sessions._site_for_domain("jobs.lever.co") == "lever"
    assert sessions._site_for_domain("myapplications.pageup.com.au") == "pageup"


def test_an_unknown_host_keeps_its_own_name():
    """Which is also the URL worth checking it at."""
    assert sessions._site_for_domain("careers.acme.com.au") == "careers.acme.com.au"


def test_a_site_with_no_cookies_is_reported_not_omitted(session):
    """ "LinkedIn is missing from this list" is not something anyone notices."""
    results = sessions.check_all(session, FakeContext([]), FakePage())
    by_site = {check.site: check for check in results}

    assert "linkedin" in by_site
    assert by_site["linkedin"].status is SessionStatus.NO_SESSION


def test_a_site_with_no_cookies_is_not_navigated(session):
    """Loading a page to confirm nobody ever signed in is wasted work."""
    page = FakePage()
    sessions.check_all(session, FakeContext([]), page)
    assert page.visited == []


def test_a_site_found_only_in_cookies_is_still_checked(session):
    """The ATS accounts the user signed into themselves are the whole point."""
    page = FakePage({"input[type='password']"})
    results = sessions.check_all(
        session, FakeContext([{"domain": "careers.acme.com.au"}]), page
    )

    sites = {check.site for check in results}
    assert "careers.acme.com.au" in sites
    assert any(
        check.status is SessionStatus.DEAD and check.site == "careers.acme.com.au"
        for check in results
    )


def test_check_all_records_every_result(session):
    sessions.check_all(
        session, FakeContext([{"domain": "careers.acme.com.au"}]), FakePage()
    )
    stored = {row.site for row in session.exec(select(SessionHealth)).all()}
    assert "careers.acme.com.au" in stored
    assert "seek" in stored


# =========================================================================
# Never re-login. Hard rule 8.
# =========================================================================


def test_nothing_in_this_module_takes_a_credential():
    """Hard rule 8: never script a login."""
    for name, function in inspect.getmembers(sessions, inspect.isfunction):
        params = set(inspect.signature(function).parameters)
        for forbidden in ("password", "username", "email", "credentials", "secret"):
            assert forbidden not in params, f"{name} takes a {forbidden}"


def test_the_module_never_submits_a_login_form():
    """It reports. The user signs in themselves, in a visible browser."""
    import pathlib

    source = pathlib.Path("backend/sessions.py").read_text(encoding="utf-8")
    code = "\n".join(
        line.split("#")[0]
        for line in source.splitlines()
        if not line.strip().startswith(("*", '"""', "#"))
    )
    for forbidden in (".fill(", ".click(", ".press(", "set_input_files"):
        assert forbidden not in code, f"sessions.py calls {forbidden}"


# =========================================================================
# Reporting
# =========================================================================


def test_the_digest_is_silent_when_everything_is_signed_in(session):
    """A line that appears every evening saying nothing stops being read."""
    sessions.record(session, sessions.SiteCheck("seek", SessionStatus.LIVE))
    sessions.record(session, sessions.SiteCheck("x", SessionStatus.NO_SESSION))
    session.flush()

    assert sessions.digest_lines(session) == []


def test_the_digest_names_the_dead_site_and_the_command(session):
    sessions.record(
        session, sessions.SiteCheck("pageup", SessionStatus.DEAD, "login page")
    )
    session.flush()

    body = "\n".join(sessions.digest_lines(session))
    assert "pageup" in body
    assert "login --platform pageup" in body


def test_a_long_unverified_session_is_reported_even_if_not_dead(session):
    """A session nothing has confirmed in days is worth surfacing."""
    from datetime import UTC, datetime, timedelta

    sessions.record(session, sessions.SiteCheck("seek", SessionStatus.UNKNOWN))
    session.flush()
    row = session.exec(select(SessionHealth)).one()
    row.last_verified_at = datetime.now(UTC) - timedelta(days=5)
    session.flush()

    assert sessions.stale_sites(session, hours=48)
    assert "seek" in "\n".join(sessions.digest_lines(session))


def test_a_recently_verified_session_is_not_reported(session):
    sessions.record(session, sessions.SiteCheck("seek", SessionStatus.LIVE))
    session.flush()
    assert sessions.stale_sites(session, hours=48) == []


def test_a_naive_timestamp_does_not_crash_the_digest(session):
    """Rows read back from SQLite are naive; the schema stores UTC."""
    from datetime import UTC, datetime

    sessions.record(session, sessions.SiteCheck("seek", SessionStatus.LIVE))
    session.flush()
    row = session.exec(select(SessionHealth)).one()
    row.last_verified_at = datetime.now(UTC).replace(tzinfo=None)
    row.status = SessionStatus.UNKNOWN
    session.flush()

    sessions.stale_sites(session)  # must not raise
    sessions.digest_lines(session)


# =========================================================================
# Wiring
# =========================================================================


def test_the_check_runs_daily_before_the_morning_apply_pass():
    """Named in the morning, not discovered at 10:00 with work queued behind it."""
    from backend.integrations.scheduler import SCHEDULE

    by_id = {job["id"]: job for job in SCHEDULE}
    assert "session_health" in by_id

    health = by_id["session_health"]
    apply_morning = by_id["apply_morning"]
    assert health["trigger"] == "cron"
    assert health["hour"] < apply_morning["hour"], (
        "the check must run before the first apply pass, not after it"
    )


def test_the_apply_pass_checks_sessions_before_applying():
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("backend/apply/run.py").read_text(encoding="utf-8"))
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "check_all" in calls
