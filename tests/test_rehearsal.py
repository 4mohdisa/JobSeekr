"""The offline rehearsal, run as part of the suite.

The rehearsal exists to catch what unit tests cannot: the seams. Running it here
means those seams are checked on every commit rather than only when someone
remembers to run the command.
"""

from __future__ import annotations

import pytest

from backend import rehearsal
from tests.conftest import needs_pdflatex

pytestmark = needs_pdflatex


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    """One rehearsal, read by both tests.

    It was run twice, once per test, and the second run computed nothing the
    first did not — 8 extra pdflatex subprocesses and about 1.5s, on a 32s
    suite that runs on every commit. Module-scoped because the report is a
    read-only value object; neither test mutates it.
    """
    return rehearsal.rehearse(tmp_path_factory.mktemp("rehearsal"), keep=True)


def test_the_whole_pipeline_runs_end_to_end(report):
    failures = [s for s in report.stages if not s.ok]
    assert not failures, "\n".join(s.line() for s in report.stages)


def test_the_rehearsal_never_submits(report):
    """The one property that must hold even if every other stage breaks."""
    stage = next(s for s in report.stages if s.name == "no live submit")
    assert stage.ok, stage.detail
