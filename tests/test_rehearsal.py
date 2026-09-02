"""The offline rehearsal, run as part of the suite.

The rehearsal exists to catch what unit tests cannot: the seams. Running it here
means those seams are checked on every commit rather than only when someone
remembers to run the command.
"""

from __future__ import annotations

from backend import rehearsal
from tests.conftest import needs_pdflatex

pytestmark = needs_pdflatex


def test_the_whole_pipeline_runs_end_to_end(tmp_path):
    report = rehearsal.rehearse(tmp_path / "rehearsal", keep=True)
    failures = [s for s in report.stages if not s.ok]
    assert not failures, "\n".join(s.line() for s in report.stages)


def test_the_rehearsal_never_submits(tmp_path):
    """The one property that must hold even if every other stage breaks."""
    report = rehearsal.rehearse(tmp_path / "rehearsal", keep=True)
    stage = next(s for s in report.stages if s.name == "no live submit")
    assert stage.ok, stage.detail
