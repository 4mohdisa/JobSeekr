"""A dropdown answered with a value the dropdown does not offer.

The failure this covers is quiet. A screening question that is a ``<select>``,
a radio group or a checkbox group accepts exactly the strings it lists; anything
else either fails at submit or submits blank, and neither shows up as an error
anywhere the user looks. So a reply of "two weeks" to a form whose only valid
value is "2 weeks" is not a near miss — it is a broken application.

The rule, from capture to replay:

* the enumeration reads every option's LABEL and its submitted VALUE, off the
  same element, and groups radios and checkboxes into one question
* the abstention and the parked job carry that option set verbatim
* the escalation shows the labels unedited and takes a tap or a number
* the answer bank stores the value, and the option set it was chosen from
* on a different employer's form, a stored answer that is not one of THAT
  form's options abstains rather than being mapped onto the nearest one
* a fact-derived answer is picked FROM the option list, never invented

There are no fuzzy tiers anywhere in this file on purpose. Every "nearly"
belongs to the abstain path.
"""

from __future__ import annotations

import pytest
from sqlmodel import select

from backend.apply.answers import (
    Abstain,
    AbstainReason,
    Answer,
    coerce_to_choices,
    resolve_answer,
)
from backend.apply.draft import Choice, FormField, as_choices
from backend.apply.formdom import (
    MULTI_VALUE_SEPARATOR,
    enumerate_form_fields,
    option_choice,
    split_values,
)
from backend.models import AnswerBank, AnswerType, MatchType

# =========================================================================
# A fake DOM good enough to enumerate
# =========================================================================


class Node:
    """One element: a tag name, attributes, text, and children."""

    def __init__(self, tag: str, text: str = "", **attrs: object) -> None:
        self.tag = tag
        self.text = text
        self.attrs = {
            key.rstrip("_").replace("_", "-"): value for key, value in attrs.items()
        }
        self.children: list[Node] = []

    def add(self, *nodes: Node) -> Node:
        self.children.extend(nodes)
        return self

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


class Loc:
    """The subset of Playwright's Locator the enumeration uses."""

    def __init__(self, nodes: list[Node], page: Page) -> None:
        self.nodes = nodes
        self.page = page

    @property
    def first(self) -> Loc:
        return Loc(self.nodes[:1], self.page)

    def count(self) -> int:
        return len(self.nodes)

    def all(self) -> list[Loc]:
        return [Loc([node], self.page) for node in self.nodes]

    def is_visible(self, timeout: int = 0) -> bool:
        return self.nodes[0].attrs.get("hidden") is None if self.nodes else False

    def get_attribute(self, name: str) -> str | None:
        if not self.nodes:
            return None
        value = self.nodes[0].attrs.get(name)
        return None if value is None else str(value)

    def inner_text(self) -> str:
        return self.nodes[0].text if self.nodes else ""

    def evaluate(self, expression: str) -> object:
        if "tagName" in expression:
            return self.nodes[0].tag if self.nodes else ""
        raise NotImplementedError(expression)

    def locator(self, selector: str) -> Loc:
        found: list[Node] = []
        for node in self.nodes:
            for child in node.walk():
                if child is not node and self.page.matches(child, selector):
                    found.append(child)
        return Loc(found, self.page)


class Page:
    """A tiny selector engine over ``Node`` — tags, #id, [attr] and :has()."""

    def __init__(self, root: Node) -> None:
        self.root = root

    def matches(self, node: Node, selector: str) -> bool:
        for part in (piece.strip() for piece in selector.split(",")):
            if self._matches_one(node, part):
                return True
        return False

    def _matches_one(self, node: Node, selector: str) -> bool:
        if selector.startswith("fieldset:has("):
            return False  # handled by the legend branch below
        if " " in selector:  # "datalist#x option", "fieldset:has(..) legend"
            head, _, tail = selector.rpartition(" ")
            if not self._matches_one(node, tail):
                return False
            return any(
                self._matches_one(ancestor, head) for ancestor in self._ancestors(node)
            )
        if selector.startswith("label[for="):
            wanted = selector.split("'")[1]
            return node.tag == "label" and node.attrs.get("for") == wanted
        if selector.startswith("[") and selector.endswith("]"):
            body = selector[1:-1]
            if "=" in body:
                name, _, raw = body.partition("=")
                return node.attrs.get(name) == raw.strip("'\"")
            return body in node.attrs
        tag, _, rest = selector.partition("#")
        if rest and node.attrs.get("id") != rest:
            return False
        return node.tag == tag if tag else True

    def _ancestors(self, node: Node) -> list[Node]:
        found: list[Node] = []

        def walk(current: Node, trail: list[Node]) -> None:
            if current is node:
                found.extend(trail)
                return
            for child in current.children:
                walk(child, [*trail, current])

        walk(self.root, [])
        return found

    def locator(self, selector: str) -> Loc:
        if selector.startswith("fieldset:has("):
            # "fieldset:has([name='x']) legend" — the group's question.
            name = selector.split("'")[1]
            for node in self.root.walk():
                if node.tag != "fieldset":
                    continue
                if any(child.attrs.get("name") == name for child in node.walk()):
                    return Loc(
                        [child for child in node.walk() if child.tag == "legend"], self
                    )
            return Loc([], self)
        return Loc(
            [node for node in self.root.walk() if self.matches(node, selector)], self
        )


def form(*nodes: Node) -> Page:
    return Page(Node("form").add(*nodes))


def dropdown(name: str, options: list[tuple[str, str | None]], **attrs: object) -> Node:
    node = Node("select", name=name, **attrs)
    for label, value in options:
        node.add(Node("option", label, **({} if value is None else {"value": value})))
    return node


# =========================================================================
# CAPTURE
# =========================================================================


def test_a_dropdowns_options_carry_both_the_label_and_the_submitted_value():
    """The two differ constantly, and only one of them is accepted at submit."""
    page = form(
        Node("label", "Notice period", for_="notice"),
        dropdown(
            "notice",
            [("Immediately", "0"), ("1 - 2 weeks", "2"), ("1 month", "4")],
            id="notice",
            aria_label="Notice period",
        ),
    )

    [field] = enumerate_form_fields(page, step=0)

    assert [(c.label, c.value) for c in field.choices] == [
        ("Immediately", "0"),
        ("1 - 2 weeks", "2"),
        ("1 month", "4"),
    ]


def test_an_option_with_no_value_attribute_submits_its_own_text():
    """What the browser does. Falling back to "" would submit nothing."""
    page = form(dropdown("q", [("Yes", None), ("No", None)], aria_label="Rights?"))

    [field] = enumerate_form_fields(page, step=0)

    assert [(c.label, c.value) for c in field.choices] == [("Yes", "Yes"), ("No", "No")]


def test_a_placeholder_prompt_is_not_an_option():
    page = form(
        dropdown(
            "q",
            [("Select an option", ""), ("Yes", "y")],
            aria_label="Do you have rights?",
        )
    )

    [field] = enumerate_form_fields(page, step=0)

    assert field.choice_labels == ["Yes"]


def test_a_radio_group_is_one_question_not_one_field_per_answer():
    """Per element, a three-way group arrives as three fields each labelled with
    one of the answers, and the question itself is nowhere."""
    page = form(
        Node("fieldset").add(
            Node("legend", "What is your citizenship status?"),
            Node("input", type="radio", name="cit", value="AU", id="c1"),
            Node("label", "Australian citizen", for_="c1"),
            Node("input", type="radio", name="cit", value="PR", id="c2"),
            Node("label", "Permanent resident", for_="c2"),
            Node("input", type="radio", name="cit", value="VISA", id="c3"),
            Node("label", "Visa holder", for_="c3"),
        )
    )

    [field] = enumerate_form_fields(page, step=0)

    assert field.identifier == "cit"
    assert field.label == "What is your citizenship status?"
    assert [(c.label, c.value) for c in field.choices] == [
        ("Australian citizen", "AU"),
        ("Permanent resident", "PR"),
        ("Visa holder", "VISA"),
    ]
    assert not field.multi_select, "a radio group is exclusive by definition"


def test_a_checkbox_group_is_multi_select():
    page = form(
        Node("fieldset").add(
            Node("legend", "Which shifts can you work?"),
            Node("input", type="checkbox", name="shift", value="D", id="s1"),
            Node("label", "Day", for_="s1"),
            Node("input", type="checkbox", name="shift", value="N", id="s2"),
            Node("label", "Night", for_="s2"),
        )
    )

    [field] = enumerate_form_fields(page, step=0)

    assert field.multi_select
    assert field.choice_labels == ["Day", "Night"]


def test_a_lone_checkbox_is_a_consent_tick_not_a_one_option_list():
    """ "I agree to the terms" is not a question with one answer."""
    page = form(
        Node("input", type="checkbox", name="agree", id="a1"),
        Node("label", "I agree to the terms", for_="a1"),
    )

    [field] = enumerate_form_fields(page, step=0)

    assert field.choices == []
    assert not field.multi_select
    assert field.label == "I agree to the terms"


def test_a_multiple_select_is_multi_select():
    page = form(
        dropdown(
            "langs",
            [("Python", "py"), ("SQL", "sql")],
            multiple="",
            aria_label="Languages",
        )
    )

    [field] = enumerate_form_fields(page, step=0)

    assert field.multi_select


def test_a_datalist_is_captured_as_the_sites_expected_values():
    page = form(
        Node("input", type="text", name="city", list="cities", aria_label="City"),
        Node("datalist", id="cities").add(
            Node("option", "Adelaide", value="ADL"),
            Node("option", "Melbourne", value="MEL"),
        ),
    )

    [field] = enumerate_form_fields(page, step=0)

    assert [(c.label, c.value) for c in field.choices] == [
        ("Adelaide", "ADL"),
        ("Melbourne", "MEL"),
    ]
    assert not field.multi_select, "a datalist still accepts free text"


@pytest.mark.parametrize(
    "label", ["Other", "Other (please specify)", "Something else (specify)"]
)
def test_an_other_option_is_marked_as_needing_typed_detail(label):
    """It is an option, but choosing it leaves a text box nothing here can fill."""
    choice = option_choice(label, "OTHER")
    assert choice is not None and choice.is_free_text


def test_an_ordinary_option_is_not_marked_free_text():
    assert not option_choice("Motherhood leave", "ML").is_free_text, (
        "'other' inside a word is not an Other option"
    )


def test_an_option_label_is_never_tidied():
    """ "1 - 2 weeks" has to arrive as "1 - 2 weeks"; the reply is matched on it."""
    assert option_choice("  1 - 2  weeks  ", "2").label == "1 - 2  weeks"


# =========================================================================
# STORE and REPLAY
# =========================================================================


def choices(*pairs: tuple[str, str]) -> list[Choice]:
    return [Choice(label=label, value=value) for label, value in pairs]


def bank_row(question: str, answer: str, options: list[Choice] | None = None):
    return AnswerBank(
        id=1,
        question_pattern=question,
        match_type=MatchType.FUZZY,
        answer_value=answer,
        answer_type=AnswerType.CHOICE if options else AnswerType.TEXT,
        choices=[{"label": c.label, "value": c.value} for c in options]
        if options
        else None,
    )


def test_a_chosen_option_resolves_to_the_value_the_form_submits():
    """Not the label. The label is what the user reads; the value is sent."""
    options = choices(("Immediately", "0"), ("1 - 2 weeks", "2"))
    outcome = resolve_answer(
        "What is your notice period?",
        answers=[bank_row("What is your notice period?", "2", options)],
        choices=options,
    )

    assert isinstance(outcome, Answer)
    assert outcome.value == "2"


def test_a_stored_answer_the_form_does_not_offer_abstains_rather_than_guessing():
    """The whole point. A previous employer offered "1-2 weeks"; this one does
    not, and the nearest option is a notice period the user never gave."""
    outcome = resolve_answer(
        "What is your notice period?",
        answers=[bank_row("What is your notice period?", "1-2 weeks")],
        choices=choices(("Immediately", "0"), ("2 weeks", "2"), ("1 month", "4")),
    )

    assert isinstance(outcome, Abstain)
    assert outcome.reason is AbstainReason.INVALID_CHOICE


def test_a_substring_is_not_a_match():
    """ "2 weeks" is inside "1 - 2 weeks" and inside nothing else on this form.

    The old mapper had a substring tier and would have picked it — confidently,
    and wrongly, since a one-to-two-week range is not two weeks.
    """
    assert (
        coerce_to_choices("2 weeks", choices(("1 - 2 weeks", "2"), ("1 month", "4")))
        is None
    )


def test_a_yes_no_synonym_still_maps_onto_a_yes_no_list():
    """Not a nearest-match guess: exactly one option means yes, and one means no."""
    assert coerce_to_choices("y", choices(("Yes", "TRUE"), ("No", "FALSE"))) == "TRUE"


def test_yes_does_not_map_onto_a_visa_status_list():
    assert (
        coerce_to_choices(
            "Yes", choices(("Australian Citizen", "AU"), ("Visa holder", "V"))
        )
        is None
    )


def test_an_answer_may_name_its_option_by_label_or_by_value():
    options = choices(("1 - 2 weeks", "2"))
    assert coerce_to_choices("1 - 2 weeks", options) == "2"
    assert coerce_to_choices("2", options) == "2"


def test_a_multi_select_answer_is_rejected_whole_if_any_part_is_unknown():
    """Half a multi-select is not a partial answer, it is a different one."""
    options = choices(("Day", "D"), ("Night", "N"))
    both = MULTI_VALUE_SEPARATOR.join(["D", "N"])
    assert coerce_to_choices(both, options) == both
    assert coerce_to_choices(MULTI_VALUE_SEPARATOR.join(["D", "X"]), options) is None


def test_the_multi_value_separator_cannot_appear_in_an_option_label():
    """A comma or a pipe could. A control character cannot survive rendering."""
    assert MULTI_VALUE_SEPARATOR == "\x1f"
    assert not MULTI_VALUE_SEPARATOR.isprintable()
    assert split_values("a\x1fb") == ["a", "b"]


# =========================================================================
# The option set travels with the abstention
# =========================================================================


def test_an_abstention_on_a_dropdown_carries_the_forms_options():
    """Including on NO_MATCH — the most common way a choice question is first
    seen, and the branch that used to carry nothing at all."""
    outcome = resolve_answer(
        "Which state are you based in?",
        answers=[],
        choices=choices(("South Australia", "SA"), ("Victoria", "VIC")),
    )

    assert isinstance(outcome, Abstain)
    assert outcome.reason is AbstainReason.NO_MATCH
    assert [(c.label, c.value) for c in outcome.choices] == [
        ("South Australia", "SA"),
        ("Victoria", "VIC"),
    ]


def test_the_abstention_records_whether_several_options_may_be_chosen():
    outcome = resolve_answer(
        "Which shifts can you work?",
        answers=[],
        choices=choices(("Day", "D")),
        multi_select=True,
    )

    assert isinstance(outcome, Abstain)
    assert outcome.multi_select


# =========================================================================
# Reading whatever shape the options were stored in
# =========================================================================


def test_a_legacy_row_of_bare_strings_still_reads():
    """Rows written before options carried values hold plain labels."""
    assert as_choices(["Yes", "No"]) == [
        Choice(label="Yes", value="Yes"),
        Choice(label="No", value="No"),
    ]


def test_a_stored_dict_reads_back_as_the_option_it_was():
    assert as_choices([{"label": "1 - 2 weeks", "value": "2"}]) == [
        Choice(label="1 - 2 weeks", value="2")
    ]


def test_a_form_field_reports_its_labels_in_the_forms_own_order():
    field = FormField(
        identifier="q",
        label="Notice?",
        choices=choices(("Immediately", "0"), ("1 - 2 weeks", "2")),
    )
    assert field.choice_labels == ["Immediately", "1 - 2 weeks"]


# =========================================================================
# ASK — what reaches the phone
# =========================================================================


@pytest.fixture(autouse=True)
def _clean_bank():
    """The escalation writes to the real (throwaway) database, as it does live.

    An in-memory engine would not do: escalate_question and handle_callback open
    their OWN sessions through session_scope, which is the whole point — the bot
    is a different process from the pass that parked the job.
    """
    from backend.db import session_scope
    from backend.models import AnswerBank, Job

    yield
    with session_scope() as session:
        for row in session.exec(select(AnswerBank)).all():
            session.delete(row)
        for job in session.exec(select(Job)).all():
            session.delete(job)


@pytest.fixture
def outbox(monkeypatch):
    """Capture what escalate_question would send, without a bot."""
    from backend.integrations import telegram

    sent: list[dict] = []

    def fake_send(text, priority=None, *, keyboard=None, markdown=True):
        sent.append({"text": text, "keyboard": keyboard, "markdown": markdown})
        return True

    monkeypatch.setattr(telegram, "send_message", fake_send)
    return sent


def park(*, question: str, options: list[Choice], multi: bool = False) -> int:
    """A job parked on ``question``, with its options persisted."""
    from dataclasses import asdict

    from backend.db import session_scope
    from backend.models import Job, JobStatus

    with session_scope() as session:
        job = Job(
            source="seek",
            source_job_id=f"choice-{question[:24]}",
            dedupe_hash=f"hash-{question[:32]}",
            title="Data Analyst",
            company="Acme",
            url="https://example.invalid/job",
            status=JobStatus.NEEDS_ANSWER,
            needs_answer_question=question,
            needs_answer_choices=[asdict(option) for option in options],
            needs_answer_multi=multi,
        )
        session.add(job)
        session.flush()
        return job.id


def stored_answer(question: str):
    from backend.db import session_scope

    with session_scope() as session:
        row = session.exec(
            select(AnswerBank).where(AnswerBank.question_pattern == question)
        ).first()
        if row is None:
            return None
        session.expunge(row)
        return row


def job_status(job_id: int):
    from backend.db import session_scope
    from backend.models import Job

    with session_scope() as session:
        return session.get(Job, job_id).status


def test_the_options_arrive_as_buttons_labelled_exactly_as_the_site_wrote_them(outbox):
    """No tidying. "1 - 2 weeks" has to be tappable as "1 - 2 weeks"."""
    from backend.integrations import telegram

    job_id = park(
        question="What is your notice period?",
        options=choices(("Immediately", "0"), ("1 - 2 weeks", "2"), ("1 month", "4")),
    )

    assert telegram.escalate_question(job_id, "What is your notice period?")

    labels = [button["text"] for row in outbox[0]["keyboard"] for button in row]
    assert labels == ["Immediately", "1 - 2 weeks", "1 month"]


def test_the_message_is_not_sent_as_markdown():
    """An option reading "3+ years_of_experience" would be mangled into
    "3+ yearsofexperience" — and then match nothing when replied with."""
    import inspect

    from backend.integrations import telegram

    source = inspect.getsource(telegram.escalate_question)
    assert "markdown=False" in source
    assert source.count("markdown=False") == source.count("send_message(")


def test_a_long_option_list_is_numbered_rather_than_turned_into_buttons(outbox):
    """Telegram renders twenty buttons; a phone does not render them readably."""
    from backend.integrations import telegram

    many = choices(*[(f"Option {n}", str(n)) for n in range(1, 13)])
    job_id = park(question="Pick your state?", options=many)

    assert telegram.escalate_question(job_id, "Pick your state?")

    assert outbox[0]["keyboard"] is None
    assert "1. Option 1" in outbox[0]["text"]
    assert "12. Option 12" in outbox[0]["text"]
    assert f"/answer {job_id} <number>" in outbox[0]["text"]


def test_the_threshold_between_buttons_and_a_numbered_list_is_pinned(outbox):
    from backend.integrations import telegram

    exactly = choices(
        *[(f"Option {n}", str(n)) for n in range(1, telegram.MAX_KEYBOARD_OPTIONS + 1)]
    )
    job_id = park(question="Pick one of these?", options=exactly)
    telegram.escalate_question(job_id, "Pick one of these?")

    assert outbox[0]["keyboard"] is not None, "at the limit, still buttons"


def test_a_multi_select_gets_a_done_button(outbox):
    from backend.integrations import telegram

    job_id = park(
        question="Which shifts can you work?",
        options=choices(("Day", "D"), ("Night", "N")),
        multi=True,
    )

    telegram.escalate_question(job_id, "Which shifts can you work?")

    labels = [button["text"] for row in outbox[0]["keyboard"] for button in row]
    assert labels == ["Day", "Night", "Done"]


def test_a_single_select_has_no_done_button(outbox):
    from backend.integrations import telegram

    job_id = park(
        question="Do you have full working rights?", options=choices(("Yes", "Y"))
    )
    telegram.escalate_question(job_id, "Do you have full working rights?")

    labels = [button["text"] for row in outbox[0]["keyboard"] for button in row]
    assert "Done" not in labels


def test_an_other_option_is_flagged_as_needing_typed_detail(outbox):
    """Choosing it leaves a text box the answer bank cannot fill."""
    from backend.integrations import telegram

    job_id = park(
        question="How did you hear about us?",
        options=[
            Choice(label="Seek", value="SEEK"),
            Choice(label="Other (please specify)", value="OTHER", is_free_text=True),
        ],
    )
    telegram.escalate_question(job_id, "How did you hear about us?")

    assert "Other (please specify)" in outbox[0]["text"]
    assert "by hand" in outbox[0]["text"]


def test_tapping_an_option_stores_its_value_and_requeues_the_job(outbox):
    """The value, not the label — the label is what the user read."""
    from backend.integrations import telegram
    from backend.models import JobStatus

    options = choices(("Immediately", "0"), ("1 - 2 weeks", "2"))
    job_id = park(question="What is your notice period?", options=options)

    reply, keyboard = telegram.handle_callback(f"c:{job_id}:1:")

    assert keyboard is None, "the question is answered; the buttons go"
    assert "1 - 2 weeks" in reply

    stored = stored_answer("What is your notice period?")
    assert stored is not None
    assert stored.answer_value == "2", "the submitted value, not the label"
    assert stored.choices == [
        {"label": "Immediately", "value": "0", "is_free_text": False},
        {"label": "1 - 2 weeks", "value": "2", "is_free_text": False},
    ], "the whole option set the answer was chosen from"
    assert job_status(job_id) is JobStatus.DOCUMENTS_READY


def test_a_multi_select_accumulates_taps_until_done(outbox):
    from backend.integrations import telegram

    options = choices(("Day", "D"), ("Night", "N"), ("Weekend", "W"))
    job_id = park(question="Which shifts can you work?", options=options, multi=True)

    _, keyboard = telegram.handle_callback(f"c:{job_id}:0:")
    assert keyboard is not None, "a multi-select is not finished by one tap"
    assert keyboard[0][0]["text"].startswith("\u2713"), "the chosen option is ticked"

    # The selection travels in the callback data, so the second tap carries it.
    _, keyboard = telegram.handle_callback(f"c:{job_id}:2:0")
    done = keyboard[-1][0]
    assert done["text"] == "Done"

    _, keyboard = telegram.handle_callback(done["callback_data"])
    assert keyboard is None

    stored = stored_answer("Which shifts can you work?")
    assert stored.answer_value == MULTI_VALUE_SEPARATOR.join(["D", "W"])


def test_tapping_the_same_option_twice_deselects_it(outbox):
    from backend.integrations import telegram

    job_id = park(
        question="Which shifts suit you?",
        options=choices(("Day", "D"), ("Night", "N")),
        multi=True,
    )

    _, keyboard = telegram.handle_callback(f"c:{job_id}:0:0")

    assert not keyboard[0][0]["text"].startswith("\u2713")


def test_done_with_nothing_selected_does_not_store_an_empty_answer(outbox):
    from backend.integrations import telegram
    from backend.models import JobStatus

    job_id = park(
        question="Which shifts are possible?",
        options=choices(("Day", "D")),
        multi=True,
    )

    reply, _ = telegram.handle_callback(f"d:{job_id}::")

    assert "at least one" in reply
    assert job_status(job_id) is JobStatus.NEEDS_ANSWER


def test_a_typed_reply_that_names_no_option_is_refused_with_the_options():
    """The failure the whole phase is about, refused at the one place it can be."""
    from backend.integrations import telegram
    from backend.models import JobStatus

    job_id = park(
        question="What notice do you need to give?",
        options=choices(("Immediately", "0"), ("1 - 2 weeks", "2")),
    )

    reply = telegram.handle_command(f"/answer {job_id} two weeks")

    assert "not one of the options" in reply
    assert "1 - 2 weeks" in reply
    assert job_status(job_id) is JobStatus.NEEDS_ANSWER, (
        "an unusable reply must not re-queue the job"
    )


def test_a_typed_number_picks_the_option_at_that_position():
    """How a list too long for buttons is answered."""
    from backend.integrations import telegram

    job_id = park(
        question="Which state do you work in?",
        options=choices(("SA", "S"), ("VIC", "V"), ("NSW", "N")),
    )

    reply = telegram.handle_command(f"/answer {job_id} 2")

    assert "VIC" in reply
    assert stored_answer("Which state do you work in?").answer_value == "V"


def test_a_typed_exact_label_is_accepted():
    from backend.integrations import telegram

    job_id = park(
        question="Which region do you work in?",
        options=choices(("South Australia", "SA"), ("Victoria", "VIC")),
    )

    telegram.handle_command(f"/answer {job_id} South Australia")

    assert stored_answer("Which region do you work in?").answer_value == "SA"


def test_a_free_text_question_is_still_answered_as_free_text():
    """Nothing above may break the ordinary path."""
    from backend.integrations import telegram

    job_id = park(question="What are your salary expectations?", options=[])

    reply = telegram.handle_command(f"/answer {job_id} 95000")

    assert "Job re-queued." in reply
