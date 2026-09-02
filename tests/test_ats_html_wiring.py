"""The HTML fingerprint path, from a loaded page to a chosen adapter.

``detect_from_html`` and ``detect`` were both correct and both tested before
this file existed. Nothing called them: adapter selection went through
``detect_from_url`` alone, so a white-labelled PageUp on ``careers.acme.com.au``
matched no adapter and went straight to the manual queue — the common
Australian case, failing quietly, on code written precisely to handle it.

These tests are about the wiring rather than the fingerprints: given a page that
only the HTML can identify, does an adapter actually get chosen?
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.apply.run import _applier_from_page, build_appliers
from backend.ats.adapters import GenericAtsApplier
from backend.models import ApplyType, Job

WHITE_LABELLED_URL = "https://careers.acme.com.au/job/1"

PAGEUP_HTML = "<html><body><div id='pageuppeople-app'>Apply now</div></body></html>"
ANONYMOUS_HTML = "<html><body><h1>Work with us</h1><p>Email your CV.</p></body></html>"
IFRAMED_GREENHOUSE = (
    "<html><body><h1>Careers at Acme</h1>"
    "<iframe src='https://boards.greenhouse.io/embed/job_app?token=1'></iframe>"
    "</body></html>"
)


class FakePage:
    """Enough page to be navigated and read."""

    def __init__(self, html: str, *, goto_raises: bool = False) -> None:
        self._html = html
        self._goto_raises = goto_raises
        self.visited: list[str] = []

    def goto(self, url: str, wait_until: str | None = None) -> None:
        if self._goto_raises:
            raise RuntimeError("net::ERR_CONNECTION_REFUSED")
        self.visited.append(url)

    def content(self) -> str:
        return self._html


def a_job(url: str = WHITE_LABELLED_URL) -> Job:
    return Job(
        id=1,
        source="seek",
        source_job_id="1",
        url=url,
        title="Developer",
        company="Acme",
        dedupe_hash="h1",
        apply_type=ApplyType.EXTERNAL,
    )


# =========================================================================
# Selection
# =========================================================================


def test_a_white_labelled_portal_now_selects_an_adapter():
    """The headline case. The URL says nothing; the HTML says PageUp."""
    from backend.ats.detect import detect_from_url

    assert detect_from_url(WHITE_LABELLED_URL).key == "unknown", (
        "the premise of this test is that the URL gives nothing away"
    )

    applier = _applier_from_page(FakePage(PAGEUP_HTML), a_job(), build_appliers())

    assert applier is not None, "a recognisable PageUp page selected no adapter"
    assert applier.platform == "pageup"


def test_the_page_is_actually_loaded_before_being_read():
    page = FakePage(PAGEUP_HTML)
    _applier_from_page(page, a_job(), build_appliers())
    assert page.visited == [WHITE_LABELLED_URL]


def test_an_embedded_form_builder_is_followed_into_the_iframe():
    """The employer's brand wraps someone else's form; the form is what counts."""
    applier = _applier_from_page(
        FakePage(IFRAMED_GREENHOUSE), a_job(), build_appliers()
    )
    assert applier is not None
    assert applier.platform == "greenhouse"


def test_an_unrecognisable_page_selects_nothing_rather_than_guessing():
    """A job nothing can identify belongs in the manual queue, not in a guess."""
    assert _applier_from_page(FakePage(ANONYMOUS_HTML), a_job(), build_appliers()) is None


def test_a_platform_with_no_adapter_is_reported_not_substituted(caplog):
    """Recognised but undrivable must not fall through to some other adapter."""
    only_lever = [GenericAtsApplier("lever")]
    assert _applier_from_page(FakePage(PAGEUP_HTML), a_job(), only_lever) is None
    assert "ats_detected_without_adapter" in caplog.text


def test_a_probe_that_cannot_load_the_page_does_not_end_the_pass(caplog):
    """A dead URL is one job's problem, not the run's."""
    page = FakePage(PAGEUP_HTML, goto_raises=True)
    assert _applier_from_page(page, a_job(), build_appliers()) is None
    assert "ats_html_probe_failed" in caplog.text


# =========================================================================
# Confirmation on the open page
# =========================================================================


def test_the_adapter_says_so_loudly_when_the_open_page_is_another_platform(caplog):
    """Selectors for the wrong platform fill nothing, or fill the wrong things.

    Hard rule 9: it must not be silent.
    """
    adapter = GenericAtsApplier("pageup")
    adapter._confirm_platform(FakePage(IFRAMED_GREENHOUSE), a_job())

    assert "ats_platform_mismatch" in caplog.text
    assert "greenhouse" in caplog.text


def test_no_warning_when_the_page_is_the_platform_expected(caplog):
    adapter = GenericAtsApplier("pageup")
    adapter._confirm_platform(FakePage(PAGEUP_HTML), a_job())
    assert "ats_platform_mismatch" not in caplog.text


def test_a_page_that_cannot_be_read_is_not_treated_as_a_mismatch(caplog):
    class Unreadable:
        def content(self) -> str:
            raise RuntimeError("page closed")

    GenericAtsApplier("pageup")._confirm_platform(Unreadable(), a_job())
    assert "ats_platform_mismatch" not in caplog.text


# =========================================================================
# The pass no longer discards these jobs
# =========================================================================


@pytest.mark.parametrize(
    "apply_type", [ApplyType.EXTERNAL, ApplyType.UNKNOWN]
)
def test_both_externally_applied_types_get_the_html_probe(apply_type: Any):
    """UNKNOWN is the type most white-labelled ads actually carry."""
    job = a_job()
    job.apply_type = apply_type
    applier = _applier_from_page(FakePage(PAGEUP_HTML), job, build_appliers())
    assert applier is not None and applier.platform == "pageup"
