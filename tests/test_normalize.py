"""Normalisation is where every source's disagreements get reconciled.

These are the cases that actually bite in Australian ads, not a coverage
exercise: hourly rates that must not be reported as salaries, "3 days ago"
dates that break incremental runs when misread, and obfuscated contact
addresses.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backend.discovery.contacts import extract_contact_email
from backend.discovery.normalize import (
    canonical_company,
    canonical_suburb,
    canonical_title,
    clean_description,
    parse_posted_at,
    parse_salary,
)

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------- names


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Acme Pty Ltd", "acme"),
        ("ACME PTY. LTD.", "acme"),
        ("Acme Australia Pty Ltd", "acme"),
        ("Acme Group Holdings", "acme"),
        ("Smith & Co", "smith and co"),
        ("Nestlé Australia", "nestle"),
        ("  Acme   Limited  ", "acme"),
        (None, ""),
    ],
)
def test_canonical_company_collapses_the_same_employer(raw, expected):
    assert canonical_company(raw) == expected


def test_different_employers_stay_different():
    assert canonical_company("Acme Pty Ltd") != canonical_company("Acne Pty Ltd")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Adelaide SA 5000", "adelaide SA"),
        ("Adelaide, South Australia, AU", "adelaide SA"),
        ("Adelaide, SA", "adelaide SA"),
        ("Melbourne VIC 3000", "melbourne VIC"),
        ("", ""),
    ],
)
def test_canonical_suburb_normalises_australian_spellings(raw, expected):
    assert canonical_suburb(raw) == expected


def test_canonical_title_strips_recruiter_noise():
    assert canonical_title("Senior Python Developer - URGENT") == canonical_title(
        "Senior Python Developer"
    )


# ------------------------------------------------------------------ salaries


def test_annual_range_is_read_as_stated():
    guess = parse_salary("$120,000 - $140,000 + super")
    assert (guess.annual_min, guess.annual_max) == (120000, 140000)
    assert guess.basis == "annual"
    assert guess.estimated is False


def test_k_shorthand_range():
    guess = parse_salary("120k-140k")
    assert (guess.annual_min, guess.annual_max) == (120000, 140000)


def test_bare_range_without_k_is_treated_as_thousands():
    guess = parse_salary("$120 - $140 per annum package")
    assert (guess.annual_min, guess.annual_max) == (120000, 140000)


@pytest.mark.parametrize("text", ["$60 per hour", "$60/hr", "$60 p.h.", "60 an hour"])
def test_hourly_is_annualised_but_never_claimed_as_a_salary(text):
    """The whole point: the figure is comparable, the basis stays honest."""
    guess = parse_salary(text)
    assert guess.basis == "hourly"
    assert guess.estimated is True
    assert guess.raw_min == 60
    assert guess.annual_min == 60 * 38 * 52


def test_up_to_is_a_ceiling_not_a_floor():
    guess = parse_salary("up to $95,000")
    assert guess.annual_min is None
    assert guess.annual_max == 95000


def test_no_salary_mentioned_is_empty_not_zero():
    guess = parse_salary("Great team, competitive remuneration")
    assert guess.annual_min is None and guess.annual_max is None
    assert guess.basis is None


def test_parse_salary_never_raises_on_junk():
    for junk in (None, "", "$$$", "$ - $", "salary: negotiable"):
        parse_salary(junk)


# --------------------------------------------------------------------- dates


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3 days ago", NOW - timedelta(days=3)),
        ("1 day ago", NOW - timedelta(days=1)),
        ("2 hours ago", NOW - timedelta(hours=2)),
        ("30+ days ago", NOW - timedelta(days=30)),
        ("just posted", NOW),
        ("yesterday", NOW - timedelta(days=1)),
    ],
)
def test_relative_dates(text, expected):
    assert parse_posted_at(text, now=NOW) == expected


def test_iso_and_epoch_forms_land_on_utc():
    assert parse_posted_at("2026-08-20T03:00:00Z").isoformat() == "2026-08-20T03:00:00+00:00"
    assert parse_posted_at("2026-08-20").tzinfo is UTC
    assert parse_posted_at(1756000000).tzinfo is UTC
    assert parse_posted_at(1756000000000).tzinfo is UTC  # milliseconds


def test_unreadable_date_is_none_rather_than_a_guess():
    assert parse_posted_at("sometime last spring") is None
    assert parse_posted_at(None) is None


# --------------------------------------------------------------- description


def test_clean_description_strips_markup_but_keeps_paragraphs():
    html = "<div><p>First para</p><p>Second para</p><script>evil()</script></div>"
    cleaned = clean_description(html)
    assert "evil" not in cleaned
    assert "First para" in cleaned and "Second para" in cleaned
    assert "<" not in cleaned


# ------------------------------------------------------------------ contacts


@pytest.mark.parametrize(
    "body",
    [
        "Send your CV to careers@acme.com.au today",
        "Email careers (at) acme.com.au",
        "Email careers [at] acme [dot] com [dot] au",
        "Contact careers at acme.com.au for details",
    ],
)
def test_published_address_is_read_including_light_obfuscation(body):
    assert extract_contact_email(body) == "careers@acme.com.au"


@pytest.mark.parametrize(
    "body",
    [
        "Replies go to no-reply@pageuppeople.com",
        "Do not reply: noreply@acme.com.au",
        "Apply via jobs@seek.com.au",
        "Managed by talent@jobadder.com",
        "logo url: /assets/header.acme.png",
    ],
)
def test_platform_plumbing_and_junk_are_not_treated_as_employer_contacts(body):
    assert extract_contact_email(body) is None


def test_no_address_means_none():
    assert extract_contact_email("Apply through the portal") is None
    assert extract_contact_email(None) is None


def test_board_own_domain_is_rejected_via_source_url():
    assert (
        extract_contact_email(
            "questions to hello@seek.com.au", source_url="https://www.seek.com.au/job/1"
        )
        is None
    )
