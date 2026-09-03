"""Seek NZ: configuration, not a second adapter — and the traps that come with it.

The endpoint facts here were established by probing the live site on
2026-09-03, not assumed from the AU shape. What the probe found:

    www.seek.co.nz              308 -> nz.seek.com
    nz.seek.com/api/jobsearch/v5/search   200, envelope identical to AU
    siteKey=NZ-Main is the market selector, NOT the host

The two dangerous parts have nothing to do with the endpoint:

* neither market returns a currency field, and both print a bare "$"
* work rights are a different question in each country, and the trans-Tasman
  arrangement makes a wrong answer plausible rather than obviously absurd
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine

from backend.apply.answers import AbstainReason, Abstain, resolve_answer
from backend.discovery.seek_source import SeekSource, parse_json_payload
from backend.models import AnswerBank, AnswerType, MatchType, Region
from backend.regions import REGIONS, config_for, currency_for, region_of_job
from backend.scoring.filters import _salary_below_floor


# ---------------------------------------------------------------- configuration


def test_both_markets_are_configured():
    assert set(REGIONS) == {Region.AU, Region.NZ}


def test_the_nz_endpoint_matches_what_the_live_probe_found():
    """Pinned from a real 2026-09-03 probe, not inferred from the AU shape."""
    nz = config_for(Region.NZ)
    assert nz.base_url == "https://nz.seek.com"
    assert nz.search_url == "https://nz.seek.com/api/jobsearch/v5/search"
    assert nz.site_key == "NZ-Main"
    assert nz.locale == "en-NZ"


def test_the_site_key_differs_between_markets():
    """The host alone does not select the market — the probe proved this.

    au.seek.com with siteKey=NZ-Main returns NZ jobs. So sending the wrong site
    key silently returns the wrong country's listings from the right host, and
    nothing about the response says so.
    """
    assert config_for(Region.AU).site_key != config_for(Region.NZ).site_key


def test_seek_is_one_source_parameterised_by_region():
    """Not a second adapter. If this ever becomes SeekNZSource, it has drifted."""
    au = SeekSource(region=Region.AU)
    nz = SeekSource(region=Region.NZ)

    assert type(au) is type(nz)
    assert au.config.site_key == "AU-Main"
    assert nz.config.site_key == "NZ-Main"

    params = nz._json_params(term="analyst", where="Auckland", page=1, hours_old=24)
    assert params["siteKey"] == "NZ-Main"
    assert params["locale"] == "en-NZ"


def test_a_region_string_is_accepted_as_well_as_the_enum():
    assert SeekSource(region="NZ").region is Region.NZ
    assert config_for("nz").site_key == "NZ-Main"


# -------------------------------------------------------------------- currency


def test_the_currency_comes_from_the_ad_not_the_campaign():
    """A campaign searching NZ can still surface an Australian listing."""
    record = {"locations": [{"label": "Sydney", "countryCode": "AU"}]}
    assert region_of_job(record) is Region.AU


def test_an_ad_without_a_country_code_has_no_region():
    """None, never a default. Guessing the country is guessing the currency."""
    assert region_of_job({"locations": [{"label": "Remote"}]}) is None
    assert region_of_job({}) is None
    assert currency_for(None) is None


def test_a_parsed_nz_record_carries_nzd():
    payload = {
        "data": [
            {
                "id": "1",
                "title": "Analyst",
                "advertiser": {"description": "Acme"},
                "locations": [{"label": "Auckland", "countryCode": "NZ"}],
            }
        ]
    }
    job = parse_json_payload(payload)[0]
    assert job.region == "NZ"
    assert job.salary_currency == "NZD"


class FakeJob:
    def __init__(self, *, salary_min=None, salary_max=None, currency=None, region=None):
        self.id = 1
        self.salary_min = salary_min
        self.salary_max = salary_max
        self.salary_currency = currency
        self.region = region


def test_a_floor_never_compares_across_currencies():
    """The trap the whole region concept exists for.

    Both markets print "$81,083" with no currency anywhere in the payload. At
    roughly 0.9 NZD to the AUD, comparing them silently keeps NZ jobs that fall
    below an AUD floor — an error in the direction of applying to worse work.
    """
    nz_job = FakeJob(salary_min=80_000, salary_max=90_000, currency="NZD")

    dropped = _salary_below_floor(
        nz_job, 100_000, keep_unstated=True, floor_currency="AUD"
    )
    assert dropped is False, "must refuse the comparison, not perform it"


def test_a_floor_still_filters_within_one_currency():
    """Refusing to compare across currencies must not disable the filter."""
    au_job = FakeJob(salary_min=50_000, salary_max=60_000, currency="AUD")
    assert _salary_below_floor(au_job, 100_000, keep_unstated=True, floor_currency="AUD")


def test_a_job_with_no_explicit_currency_uses_its_region():
    """job.region is NOT NULL, so a currency is always derivable.

    Without this the floor would quietly stop filtering every row that predates
    the currency column — a silent regression, not a visible one.
    """
    job = FakeJob(salary_min=50_000, salary_max=60_000, region=Region.AU)
    assert _salary_below_floor(job, 100_000, keep_unstated=True, floor_currency="AUD")


def test_an_uncomparable_job_is_kept_rather_than_dropped():
    """Cannot-compare keeps the job.

    Dropping an ad because its currency is unknown hides real work; keeping it
    costs one manual look. Only the unconverted comparison is ruled out.
    """
    job = FakeJob(salary_min=1, salary_max=2, currency="NZD")
    assert not _salary_below_floor(job, 999_999, keep_unstated=True, floor_currency="AUD")


# ---------------------------------------------------------------- work rights


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def bank(**kwargs) -> AnswerBank:
    kwargs.setdefault("match_type", MatchType.FUZZY)
    kwargs.setdefault("answer_type", AnswerType.BOOLEAN)
    return AnswerBank(**kwargs)


AU_RIGHTS = "Do you have full working rights in Australia?"
NZ_RIGHTS = "Do you have full working rights in New Zealand?"


def test_an_australian_answer_does_not_answer_a_new_zealand_question():
    """THE critical one.

    There is a reciprocal visa arrangement, which makes the wrong answer look
    reasonable rather than absurd — the worst kind of wrong. It goes onto a real
    application and nothing catches it.
    """
    rows = [bank(question_pattern=AU_RIGHTS, answer_value="Yes", region=Region.AU)]

    result = resolve_answer(NZ_RIGHTS, answers=rows, region=Region.NZ)

    assert isinstance(result, Abstain)
    assert result.reason is AbstainReason.CROSS_REGION


def test_a_broad_au_row_does_not_answer_the_nz_question(session):
    """The case that actually bites, and the one the previous test cannot catch.

    A fuzzy AU row scores below threshold against the NZ wording, so that test
    would abstain even with region filtering removed — it passes for the wrong
    reason. A *regex* row is different: `working rights` matches both countries'
    phrasings outright, so without the region filter this resolves to the
    Australian "Yes" and puts it on a New Zealand application.

    Verified by mutation: deleting the region filter makes this fail.
    """
    rows = [
        bank(
            question_pattern=r"working rights",
            match_type=MatchType.REGEX,
            answer_value="Yes",
            region=Region.AU,
        )
    ]

    result = resolve_answer(NZ_RIGHTS, answers=rows, region=Region.NZ)

    assert isinstance(result, Abstain), (
        f"an AU-scoped row answered a NZ question with {getattr(result, 'value', None)!r}"
    )
    assert result.reason is AbstainReason.CROSS_REGION


def test_the_same_broad_row_still_answers_its_own_region(session):
    """The filter must not simply break regex rows."""
    rows = [
        bank(
            question_pattern=r"working rights",
            match_type=MatchType.REGEX,
            answer_value="Yes",
            region=Region.AU,
        )
    ]
    result = resolve_answer(AU_RIGHTS, answers=rows, region=Region.AU)
    assert not isinstance(result, Abstain)
    assert result.value == "Yes"


def test_the_abstention_says_which_region_it_had_instead():
    """"Nothing in the bank" and "the other country's answer" need different fixes.

    Reporting them identically is how someone "fixes" it by widening the
    existing row to cover both countries — which is the bug, not the fix.
    """
    rows = [bank(question_pattern=AU_RIGHTS, answer_value="Yes", region=Region.AU)]
    result = resolve_answer(NZ_RIGHTS, answers=rows, region=Region.NZ)

    assert "AU" in result.detail
    assert result.reason is not AbstainReason.NO_MATCH


def test_a_region_scoped_answer_resolves_for_its_own_region():
    rows = [bank(question_pattern=NZ_RIGHTS, answer_value="Yes", region=Region.NZ)]
    result = resolve_answer(NZ_RIGHTS, answers=rows, region=Region.NZ)
    assert not isinstance(result, Abstain)
    assert result.value == "Yes"


def test_an_unscoped_answer_still_holds_everywhere():
    """Most questions are not country-specific and must not need scoping."""
    rows = [
        bank(
            question_pattern="How many years of Python experience do you have?",
            answer_value="5",
            answer_type=AnswerType.NUMBER,
            region=None,
        )
    ]
    result = resolve_answer(
        "How many years of Python experience do you have?",
        answers=rows,
        region=Region.NZ,
    )
    assert not isinstance(result, Abstain)


def test_the_right_region_wins_when_both_are_present():
    rows = [
        bank(question_pattern=AU_RIGHTS, answer_value="Yes", region=Region.AU),
        bank(question_pattern=NZ_RIGHTS, answer_value="No", region=Region.NZ),
    ]
    result = resolve_answer(NZ_RIGHTS, answers=rows, region=Region.NZ)
    assert not isinstance(result, Abstain)
    assert result.value == "No"


def test_without_a_region_the_old_behaviour_is_unchanged():
    """Callers that do not know the region must not start abstaining."""
    rows = [bank(question_pattern=AU_RIGHTS, answer_value="Yes", region=Region.AU)]
    result = resolve_answer(AU_RIGHTS, answers=rows)
    assert not isinstance(result, Abstain)


# ---------------------------------------------------------------- timezone


def test_nz_uses_its_own_timezone():
    assert config_for(Region.NZ).timezone == "Pacific/Auckland"
    assert config_for(Region.AU).timezone == "Australia/Adelaide"


def test_the_apply_window_is_measured_where_the_job_is():
    """NZ business hours, not Adelaide's.

    The gap is 2h or 2h30 depending on the month — the two observe DST on
    different schedules — so a fixed offset would be wrong for part of the year.
    Measuring in Adelaide would put an NZ application at 7am local for months.
    """
    from datetime import UTC, datetime

    from backend.apply.guardrails import _within_window

    # 20:00 UTC. That is 06:30 in Adelaide (outside 09:00-17:00) and 09:00 in
    # Auckland during NZDT (inside it).
    moment = datetime(2026, 1, 14, 20, 0, tzinfo=UTC)

    au_ok, au_detail = _within_window(moment, "seek", timezone="Australia/Adelaide")
    nz_ok, nz_detail = _within_window(moment, "seek", timezone="Pacific/Auckland")

    assert not au_ok, au_detail
    assert nz_ok, nz_detail


# ---------------------------------------------------------------- jobspy


def test_jobspy_supports_new_zealand():
    """Checked against the installed jobspy, not assumed from its docs."""
    from jobspy.model import Country

    nz = [country for country in Country if "ZEALAND" in country.name]
    assert nz, "jobspy has no New Zealand country"
    assert nz[0].indeed_domain_value[0] == "nz"


def test_the_jobspy_country_is_per_region():
    assert config_for(Region.AU).jobspy_country == "Australia"
    assert config_for(Region.NZ).jobspy_country == "New Zealand"


def test_the_jobspy_source_passes_its_region_country():
    """Hardcoding Australia returns the wrong market's jobs for an NZ campaign."""
    from backend.discovery.jobspy_source import JobSpySource

    assert JobSpySource("indeed", region=Region.NZ).config.jobspy_country == "New Zealand"
    assert JobSpySource("indeed").config.jobspy_country == "Australia"
