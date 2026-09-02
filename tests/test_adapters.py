"""Adapter contract tests. Fake pages only — no browser, no network.

These verify the parts of the adapters that are real decision logic rather
than selector strings: the one-slot vs two-slot choice, library pruning
arithmetic, and the structural rule that an adapter never decides whether to
submit.
"""

from __future__ import annotations

import inspect
import pathlib

import pytest

from backend.apply import flow, linkedin, seek
from backend.apply.draft import FormField
from backend.apply.linkedin import MAX_LIBRARY_DOCUMENTS, decide_upload_slots
from backend.models import ApplyType, Job


def job(**kwargs) -> Job:
    base = {
        "id": 1,
        "source": "linkedin",
        "source_job_id": "1",
        "url": "https://www.linkedin.com/jobs/view/1",
        "title": "Developer",
        "company": "Acme",
        "dedupe_hash": "h1",
        "apply_type": ApplyType.EASY_APPLY,
    }
    base.update(kwargs)
    return Job(**base)


# ------------------------------------------------------------- can_handle


def test_linkedin_handles_only_easy_apply():
    applier = linkedin.LinkedInApplier()
    assert applier.can_handle(job(apply_type=ApplyType.EASY_APPLY)) is True
    assert applier.can_handle(job(apply_type=ApplyType.EXTERNAL)) is False
    assert applier.can_handle(job(apply_type=ApplyType.UNKNOWN)) is False
    assert applier.can_handle(job(apply_type=ApplyType.MANUAL_ONLY)) is False


def test_seek_does_not_claim_linkedin_jobs():
    applier = seek.SeekApplier()
    assert applier.can_handle(job(source="seek", apply_type=ApplyType.QUICK_APPLY)) is True
    assert applier.can_handle(job(source="linkedin")) is False


# --------------------------------------------------- upload slot decision


def test_one_file_slot_means_the_combined_pdf():
    fields = [
        FormField(identifier="r", label="Resume", kind="file"),
        FormField(identifier="q", label="Years of experience"),
    ]
    assert decide_upload_slots(fields) == 1


def test_a_labelled_cover_letter_slot_means_two_separate_uploads():
    fields = [
        FormField(identifier="r", label="Resume", kind="file"),
        FormField(identifier="c", label="Cover letter", kind="file"),
    ]
    assert decide_upload_slots(fields) == 2


def test_a_cover_letter_slot_is_detected_even_when_it_is_the_only_file_field():
    """Some modals render the cover-letter input only after a toggle."""
    fields = [FormField(identifier="c", label="Upload cover letter", kind="file")]
    assert decide_upload_slots(fields) >= 2


def test_no_file_fields_means_no_slots():
    assert decide_upload_slots([FormField(identifier="q", label="A question")]) == 0


# ------------------------------------------------------- library pruning


class FakeLocator:
    def __init__(self, count: int, *, clickable: bool = True) -> None:
        self._count = count
        self.clicks = 0
        self._clickable = clickable

    def count(self) -> int:
        return self._count

    @property
    def last(self):
        return self

    @property
    def first(self):
        return self

    def locator(self, _selector):
        return FakeButton(self) if self._clickable else FakeLocator(0)

    def click(self):
        self.clicks += 1


class FakeButton:
    def __init__(self, parent: FakeLocator) -> None:
        self.parent = parent

    @property
    def first(self):
        return self

    def count(self) -> int:
        return 1

    def click(self):
        self.parent.clicks += 1
        self.parent._count -= 1


class FakeLibraryPage:
    def __init__(self, documents: int) -> None:
        self.library = FakeLocator(documents)

    def locator(self, _selector):
        return self.library


def test_pruning_removes_only_the_overflow():
    page = FakeLibraryPage(documents=MAX_LIBRARY_DOCUMENTS)
    applier = linkedin.LinkedInApplier()

    deleted = applier.prune_document_library(page)

    # Keeps MAX-1 so the new upload has room, deletes exactly the excess.
    assert deleted == MAX_LIBRARY_DOCUMENTS - (MAX_LIBRARY_DOCUMENTS - 1)
    assert page.library.count() == MAX_LIBRARY_DOCUMENTS - 1


def test_pruning_does_nothing_when_there_is_room():
    page = FakeLibraryPage(documents=1)
    applier = linkedin.LinkedInApplier()
    assert applier.prune_document_library(page) == 0
    assert page.library.count() == 1


def test_pruning_never_deletes_below_the_keep_floor():
    page = FakeLibraryPage(documents=10)
    applier = linkedin.LinkedInApplier()
    applier.prune_document_library(page, keep=2)
    assert page.library.count() == 2


def test_a_missing_library_ui_is_not_an_error():
    class NoLibrary:
        def locator(self, _selector):
            raise RuntimeError("no such element")

    assert linkedin.LinkedInApplier().prune_document_library(NoLibrary()) == 0


# ------------------------------------------------------------ read-back


class FakeReadbackPage:
    def __init__(self, names: list[str]) -> None:
        self.names = names

    def locator(self, _selector):
        page = self

        class _L:
            def all_inner_texts(self_inner):
                return page.names

        return _L()


def test_readback_returns_the_displayed_filenames():
    applier = linkedin.LinkedInApplier()
    names = applier.read_back_attachments(FakeReadbackPage(["tailored_resume.pdf"]))
    assert "tailored_resume.pdf" in names


def test_an_empty_readback_is_visible_to_the_caller():
    """The flow turns this into a hard abort; the adapter must not hide it.

    Under site knowledge an empty read-back on a required element raises rather
    than returning [] — louder, and it carries which element and what was tried.
    Silently returning nothing here is what would let a stale upload through.
    """
    from backend.siteknowledge import ElementNotFound

    applier = linkedin.LinkedInApplier()
    with pytest.raises(ElementNotFound):
        applier.read_back_attachments(FakeReadbackPage([]))


# --------------------------------------------------- structural invariants


@pytest.mark.parametrize("module", [seek, linkedin])
def test_an_adapter_never_decides_whether_to_submit(module):
    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
    assert "check_can_submit" not in source
    assert "allow_live_submit" not in source


@pytest.mark.parametrize(
    ("module", "cls"),
    [(seek, "SeekApplier"), (linkedin, "LinkedInApplier")],
)
def test_adapters_implement_the_whole_flow_contract(module, cls):
    """A missing method would fail at the worst possible moment — mid-application."""
    applier = getattr(module, cls)()
    required = [
        name
        for name in dir(flow.Adapter)
        if not name.startswith("_") and callable(getattr(flow.Adapter, name, None))
    ]
    for name in required:
        assert hasattr(applier, name), f"{cls} is missing {name}"
        assert callable(getattr(applier, name))


@pytest.mark.parametrize("platform", ["seek", "linkedin"])
def test_every_critical_element_has_more_than_one_strategy(platform):
    """Single brittle selectors are how these adapters rot."""
    from backend.siteknowledge import load

    knowledge = load(platform)
    critical = [
        key for key in knowledge.elements if "submit" in key or "confirmation" in key
    ]
    assert critical, "expected submit/confirmation elements"
    for key in critical:
        assert len(knowledge.elements[key].strategies) >= 2, (
            f"{platform}/{key} has only one strategy"
        )


@pytest.mark.parametrize("platform", ["seek", "linkedin"])
def test_required_elements_do_not_rely_on_class_names_alone(platform):
    """The point of the layer, not just a count.

    Four CSS selectors would satisfy "more than one strategy" and still die on
    the next redesign *together*, because generated class names all change at
    once. So the rule is about class names specifically, not about CSS: an
    attribute selector on a standard HTML type — ``input[type='file']`` — is as
    durable as the HTML spec and is a legitimate sole strategy. A class or a
    ``[class*=]`` match is not.
    """
    from backend.siteknowledge import load

    knowledge = load(platform)
    for key, element in knowledge.elements.items():
        if not element.required:
            continue
        durable = [
            strategy
            for strategy in element.strategies
            if strategy.type != "css"
            or ("." not in strategy.value and "class" not in strategy.value)
        ]
        assert durable, (
            f"{platform}/{key} depends entirely on class names, so one redesign "
            "takes every strategy at once"
        )


def test_no_credential_parameters_exist_anywhere_in_the_apply_layer():
    """Claude.md hard rule 8: never script a login."""
    from backend.apply import session as session_module

    for name, func in inspect.getmembers(session_module, inspect.isfunction):
        params = set(inspect.signature(func).parameters)
        for forbidden in ("password", "username", "email", "credentials", "secret"):
            assert forbidden not in params, f"{name} takes a {forbidden} parameter"
