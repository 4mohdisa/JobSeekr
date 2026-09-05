"""Site knowledge: resolution, self-healing, and the no-selectors-in-Python rule.

The four behaviours this layer exists for, each tested against a fake page that
can be made to fail selectively:

1. killing the top-priority strategy still resolves, via fallback
2. ...and the strategy that worked is promoted, so the next run tries it first
3. killing every strategy parks the application and alerts — never guesses
4. a hand-edited JSON file is picked up with no code change

Everything here uses a fake page rather than a browser. The fake matches
selectors literally against a set, which is the property that matters: a
strategy either produces a selector that is present or it does not.
"""

from __future__ import annotations

import json

import pytest

from backend.siteknowledge import (
    STRATEGY_PRIORITY,
    Element,
    ElementNotFound,
    SiteKnowledge,
    Strategy,
    fingerprint_steps,
    load,
)


class FakeLocator:
    def __init__(self, present: bool) -> None:
        self._present = present
        self.clicked = False

    @property
    def first(self) -> FakeLocator:
        return self

    def is_visible(self, timeout: int = 0) -> bool:
        return self._present

    def count(self) -> int:
        return 1 if self._present else 0

    def click(self) -> None:
        self.clicked = True

    def all_inner_texts(self) -> list[str]:
        return ["resume-2026-09-03.pdf"] if self._present else []


class FakePage:
    """A page where exactly the selectors in ``present`` exist."""

    def __init__(self, present: set[str]) -> None:
        self.present = present
        self.queried: list[str] = []

    def locator(self, selector: str) -> FakeLocator:
        self.queried.append(selector)
        return FakeLocator(selector in self.present)


def knowledge_with(*strategies: Strategy, required: bool = True) -> SiteKnowledge:
    return SiteKnowledge(
        platform="testplatform",
        elements={
            "apply_button": Element(
                key="apply_button", strategies=list(strategies), required=required
            )
        },
    )


# --------------------------------------------------------------------- selectors


def test_strategy_priority_is_most_durable_first():
    """CSS last is the whole ordering decision; pin it."""
    assert STRATEGY_PRIORITY[0] == "testid"
    assert STRATEGY_PRIORITY[-1] == "css"


def test_an_unknown_strategy_type_is_rejected_at_construction():
    """A typo in a hand-edited file must fail loudly, not resolve to nothing."""
    with pytest.raises(ValueError, match="unknown strategy type"):
        Strategy(type="xpath", value="//button")


@pytest.mark.parametrize(
    ("strategy", "expected"),
    [
        (
            Strategy(type="testid", value="apply", attr="data-automation"),
            "[data-automation='apply']",
        ),
        (Strategy(type="testid", value="ember*", attr="id"), "[id*='ember']"),
        (
            Strategy(type="role", role="button", name="Submit"),
            'role=button[name="Submit" i]',
        ),
        (Strategy(type="label", value="Easy Apply"), "[aria-label*='Easy Apply' i]"),
        (Strategy(type="css", value="button.foo"), "button.foo"),
    ],
)
def test_strategies_compile_to_the_expected_selector(strategy, expected):
    assert strategy.selector == expected


def test_a_space_is_not_backslash_escaped_in_a_text_regex():
    """Playwright parses these as JavaScript regexes.

    ``re.escape`` renders a space as ``\\ ``, which JavaScript treats as an
    identity escape and rejects in unicode mode. This is the kind of thing that
    works in every unit test and fails against a real browser.
    """
    assert Strategy(type="text", value="Quick apply").selector == "text=/Quick apply/i"
    assert "\\ " not in Strategy(type="text", value="a b c").selector


def test_a_wildcard_becomes_a_pattern_not_a_literal():
    """LinkedIn ids embed the job URN and a per-render Ember counter."""
    strategy = Strategy(type="testid", value="*jobPosting*", attr="id")
    assert strategy.selector == "[id*='jobPosting']"
    assert "*jobPosting*" not in strategy.selector


# ------------------------------------------------------------------- resolution


def test_the_top_priority_strategy_is_used_when_it_works():
    knowledge = knowledge_with(
        Strategy(type="testid", value="apply", attr="data-automation"),
        Strategy(type="css", value="button.apply"),
    )
    page = FakePage({"[data-automation='apply']", "button.apply"})

    assert knowledge.resolve(page, "apply_button") is not None
    assert page.queried == ["[data-automation='apply']"], "should stop at the first hit"


def test_killing_the_top_strategy_still_resolves_via_fallback():
    """Acceptance: the layer's reason for existing."""
    knowledge = knowledge_with(
        Strategy(type="testid", value="apply", attr="data-automation"),
        Strategy(type="role", role="button", name="Quick apply"),
        Strategy(type="css", value="button.apply"),
    )
    # The test id is gone, as it would be after a redesign.
    page = FakePage({'role=button[name="Quick apply" i]', "button.apply"})

    assert knowledge.resolve(page, "apply_button") is not None
    assert page.queried[0] == "[data-automation='apply']", "tried the durable one first"


def test_the_working_strategy_is_promoted_and_tried_first_next_time():
    """Acceptance: promotion, not just fallback."""
    knowledge = knowledge_with(
        Strategy(type="testid", value="apply", attr="data-automation"),
        Strategy(type="role", role="button", name="Quick apply"),
    )
    working = 'role=button[name="Quick apply" i]'
    page = FakePage({working})

    knowledge.resolve(page, "apply_button")
    element = knowledge.elements["apply_button"]
    assert element.last_working_strategy == f"role:{working}"
    assert element.success_count == 1
    assert knowledge.dirty, "a promotion has to be persistable"

    # Second run: the promoted strategy is tried before the higher-priority one.
    second = FakePage({working})
    knowledge.resolve(second, "apply_button")
    assert second.queried[0] == working


def test_drift_is_reported_when_a_promotion_replaces_a_previous_one():
    seen: list[tuple[str, str, str, str]] = []
    from backend import siteknowledge

    knowledge = knowledge_with(
        Strategy(type="testid", value="apply", attr="data-automation"),
        Strategy(type="role", role="button", name="Quick apply"),
    )
    knowledge.elements[
        "apply_button"
    ].last_working_strategy = "testid:[data-automation='apply']"

    siteknowledge.on_strategy_drift = lambda *args: seen.append(args)
    try:
        knowledge.resolve(
            FakePage({'role=button[name="Quick apply" i]'}), "apply_button"
        )
    finally:
        siteknowledge.on_strategy_drift = None

    assert len(seen) == 1, "drift from one working strategy to another must be reported"
    assert seen[0][1] == "apply_button"


def test_a_first_resolution_is_not_reported_as_drift():
    """Nothing drifted the first time an element is ever resolved."""
    seen: list[tuple] = []
    from backend import siteknowledge

    knowledge = knowledge_with(Strategy(type="css", value="button.apply"))
    siteknowledge.on_strategy_drift = lambda *args: seen.append(args)
    try:
        knowledge.resolve(FakePage({"button.apply"}), "apply_button")
    finally:
        siteknowledge.on_strategy_drift = None

    assert seen == []


def test_killing_every_strategy_raises_rather_than_guessing():
    """Acceptance: never fall through to a guess."""
    knowledge = knowledge_with(
        Strategy(type="testid", value="apply", attr="data-automation"),
        Strategy(type="role", role="button", name="Quick apply"),
        Strategy(type="css", value="button.apply"),
    )
    page = FakePage(set())

    with pytest.raises(ElementNotFound) as excinfo:
        knowledge.resolve(page, "apply_button")

    assert excinfo.value.key == "apply_button"
    assert len(excinfo.value.tried) == 3, "every strategy is reported, for the alert"
    assert knowledge.elements["apply_button"].fail_count == 1


def test_total_failure_fires_the_alert_hook():
    """Acceptance: parks AND alerts. This is the alert half."""
    alerts: list[tuple] = []
    from backend import siteknowledge

    knowledge = knowledge_with(Strategy(type="css", value="button.apply"))
    siteknowledge.on_all_strategies_failed = lambda *args: alerts.append(args)
    try:
        with pytest.raises(ElementNotFound):
            knowledge.resolve(FakePage(set()), "apply_button")
    finally:
        siteknowledge.on_all_strategies_failed = None

    assert len(alerts) == 1
    assert alerts[0][1] == "apply_button"


def test_an_optional_element_returns_none_instead_of_raising():
    """Seek's cover-letter textarea is genuinely absent on some forms."""
    knowledge = knowledge_with(
        Strategy(type="css", value="textarea.cl"), required=False
    )
    assert knowledge.resolve(FakePage(set()), "apply_button") is None


def test_an_unknown_element_key_raises():
    """A typo'd key must not silently behave like an absent optional element."""
    knowledge = knowledge_with(Strategy(type="css", value="x"))
    with pytest.raises(ElementNotFound):
        knowledge.resolve(FakePage(set()), "no_such_element")


def test_present_answers_no_instead_of_raising():
    """`is_last_step` asks a question whose answer is legitimately no."""
    knowledge = knowledge_with(Strategy(type="css", value="button.submit"))
    assert knowledge.present(FakePage(set()), "apply_button") is False
    assert knowledge.present(FakePage({"button.submit"}), "apply_button") is True


# ----------------------------------------------------------------- flow variants


class _Field:
    def __init__(self, identifier, kind="text", required=False):
        self.identifier = identifier
        self.label = identifier
        self.kind = kind
        self.required = required


def test_a_two_step_and_a_five_step_flow_fingerprint_differently():
    two = [[_Field("a")], [_Field("b")]]
    five = [[_Field("a")], [_Field("b")], [_Field("c")], [_Field("d")], [_Field("e")]]
    assert fingerprint_steps(two) != fingerprint_steps(five)


def test_step_order_matters_but_field_order_within_a_step_does_not():
    """A cover letter on step 1 is a different flow from one on step 3.

    Field order inside a step is just DOM order and carries no meaning.
    """
    a, b = _Field("a"), _Field("b")
    assert fingerprint_steps([[a, b]]) == fingerprint_steps([[b, a]])
    assert fingerprint_steps([[a], [b]]) != fingerprint_steps([[b], [a]])


def test_an_unknown_variant_is_recorded_rather_than_treated_as_an_error():
    knowledge = SiteKnowledge(platform="linkedin")
    steps = [[_Field("q1")], [_Field("q2")]]

    assert knowledge.known_variant(steps) is None
    variant = knowledge.observe_flow(steps)

    assert variant.step_count == 2
    assert variant.observed_count == 1
    assert knowledge.known_variant(steps) is variant


def test_seeing_the_same_variant_again_increments_rather_than_duplicating():
    knowledge = SiteKnowledge(platform="linkedin")
    steps = [[_Field("q1")], [_Field("q2")]]

    knowledge.observe_flow(steps)
    knowledge.observe_flow(steps)

    assert len(knowledge.flow_variants) == 1
    assert next(iter(knowledge.flow_variants.values())).observed_count == 2


# -------------------------------------------------------------------- persistence


def test_a_hand_edited_file_is_picked_up_with_no_code_change(tmp_path):
    """Acceptance: the file is the source of truth, not a cache of the code."""
    (tmp_path / "elements.json").write_text(
        json.dumps(
            {
                "platform": "seek",
                "elements": {
                    "apply_button": {
                        "key": "apply_button",
                        "required": True,
                        "strategies": [
                            {"type": "css", "value": "#hand-edited-by-the-user"}
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    knowledge = load("seek", directory=tmp_path)
    page = FakePage({"#hand-edited-by-the-user"})

    assert knowledge.resolve(page, "apply_button") is not None
    assert page.queried == ["#hand-edited-by-the-user"]


def test_a_malformed_file_raises_rather_than_silently_falling_back(tmp_path):
    """Silently reverting to defaults would discard the user's edit invisibly."""
    (tmp_path / "elements.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        load("seek", directory=tmp_path)


def test_promotions_survive_a_save_and_reload(tmp_path):
    knowledge = SiteKnowledge(
        platform="seek",
        elements={
            "apply_button": Element(
                key="apply_button",
                strategies=[
                    Strategy(type="testid", value="apply", attr="data-automation"),
                    Strategy(type="css", value="button.apply"),
                ],
            )
        },
        directory=tmp_path,
    )
    knowledge.resolve(FakePage({"button.apply"}), "apply_button")
    knowledge.save()

    reloaded = load("seek", directory=tmp_path)
    element = reloaded.elements["apply_button"]
    assert element.last_working_strategy == "css:button.apply"
    assert element.success_count == 1
    assert element.ordered()[0].selector == "button.apply", "promotion survived"


def test_save_is_a_no_op_when_nothing_changed(tmp_path):
    """Rewriting a clean file on every run would churn the user's edits."""
    knowledge = SiteKnowledge(platform="seek", directory=tmp_path)
    knowledge.save()
    assert not (tmp_path / "elements.json").exists()


def test_the_defaults_ship_for_both_primary_boards():
    """The seeded starting point has to exist, or the adapters resolve nothing."""
    for platform in ("seek", "linkedin"):
        knowledge = load(platform)
        assert knowledge.elements, f"{platform} ships no elements"
        assert knowledge.quirks, f"{platform} ships no quirks"


def test_quirks_carry_the_facts_the_adapters_depend_on():
    """These are load-bearing, not documentation — the adapters read them."""
    linkedin = load("linkedin")
    assert linkedin.quirk("library_holds_four")["capacity"] == 4
    assert linkedin.quirk("stale_uploads") is not None
    assert load("seek").quirk("inline_cover_letter") is not None


# ------------------------------------------------- no selectors in Python source


SELECTOR_SHAPES = (
    # A CSS attribute selector, a class/id selector on a specific element, or a
    # Playwright text/has-text engine call — all the ways a selector shows up.
    r"\[data-(automation|testid|test-id|test-modal)",
    r"\[aria-label",
    r":has-text\(",
    r"\btext=/",
    r"\brole=(button|dialog|link|radio|heading|textbox)\b",
    r"\.jobs-[a-z-]+",
    r"\bjobs-easy-apply\b",
    r"global-nav__",
)


@pytest.mark.parametrize(
    "module_path", ["backend/apply/seek.py", "backend/apply/linkedin.py"]
)
def test_no_platform_selector_remains_in_python_source(module_path):
    """Acceptance: a redesign is a JSON edit, not a code change.

    Scoped to the two primary adapters. Generic HTML — ``input, textarea,
    select`` in ``formdom`` — is deliberately not covered: that is the HTML
    standard, identical on every site, and pushing it into per-platform data
    would be filing a fact about HTML under "facts about Seek".
    """
    import pathlib
    import re

    source = pathlib.Path(module_path).read_text(encoding="utf-8")
    # Strip docstrings and comments: those legitimately discuss selectors.
    code = "\n".join(
        line.split("#")[0]
        for line in source.splitlines()
        if not line.strip().startswith(("*", '"""', "#"))
    )

    offenders = [shape for shape in SELECTOR_SHAPES if re.search(shape, code)]
    assert not offenders, (
        f"{module_path} still contains platform selectors matching {offenders}; "
        "they belong in data/siteknowledge/"
    )


@pytest.mark.parametrize(
    "module_path", ["backend/apply/seek.py", "backend/apply/linkedin.py"]
)
def test_the_adapters_no_longer_export_a_selectors_table(module_path):
    """The old module-level SELECTORS dict must be gone, not merely unused."""
    import importlib

    module = importlib.import_module(module_path.replace("/", ".").removesuffix(".py"))
    assert not hasattr(module, "SELECTORS")
