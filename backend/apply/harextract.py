"""Turn a capture session into site knowledge.

THE POINT
    A HAR capture that only informs a code change is a capture you have to redo
    by hand every time the site moves. This writes ``data/siteknowledge/``
    directly, so walking one application by hand is what teaches the system
    where things are — and the next capture updates that knowledge rather than
    replacing it.

WHAT IT READS
    * the HAR itself, for HTML responses
    * ``{variant}.steps.json`` beside it — the DOM at each step of the flow,
      captured by ``har.record``. The HAR alone cannot see a modal that was
      built by JavaScript after the document loaded, which is every step of
      LinkedIn Easy Apply, so the snapshots are where the useful markup is.

WHAT IT DERIVES
    For every interactive element: each way of identifying it, ordered by
    durability, in the same shape ``backend.siteknowledge`` resolves. For the
    flow: the step sequence, fingerprinted. For the form: which fields are
    required, what type they are, and which of them are screening questions.

SCREENING QUESTIONS GO TO THE ANSWER BANK UNANSWERED
    A question found during a capture is a question that will be asked again
    during a real application. It is written as an unanswered row so the user is
    asked once, at leisure, rather than at 2am while a job is parked.

    Unanswered — never guessed. Hard rule 2 says answers come only from the
    answer bank; a capture is evidence about the *question*, and none at all
    about the answer.

MERGE, NEVER OVERWRITE
    ``merge_into`` keeps success counts, failure counts, promotions and
    verification timestamps. A re-capture adds newly seen strategies and
    refreshes what the page looks like now; it does not erase the evidence of
    what has actually been working in production.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag

from backend.logging_setup import get_logger
from backend.siteknowledge import Element, SiteKnowledge, Strategy, fingerprint_steps

log = get_logger(__name__)

__all__ = [
    "Capture",
    "CapturedElement",
    "MergeReport",
    "extract",
    "extract_from_html",
    "merge_into",
    "push_questions_to_answer_bank",
]


# Attributes that carry a site's own test hooks, in the order to prefer them.
# Seek uses data-automation; LinkedIn uses several; data-testid is the common
# convention everywhere else.
TESTID_ATTRIBUTES: tuple[str, ...] = (
    "data-testid",
    "data-test-id",
    "data-automation",
    "data-test",
    "data-tracking-control-name",
)

INTERACTIVE_TAGS: tuple[str, ...] = ("button", "input", "select", "textarea", "a")

MIN_PATTERN_LENGTH = 4
"""Shortest stable fragment worth turning into a wildcard.

``q1`` is "volatile" by the digit test, and its stable fragment is ``q`` —
``[id*='q']`` matches almost every element on the page. An over-broad strategy
is worse than no strategy: resolution would find *something*, report success,
and click the wrong control. Below this length the literal is kept instead,
which at worst fails to match, and failing to match is safe.
"""

#: Substrings that mark an id as generated per render rather than stable. Any id
#: containing one is stored as a wildcard pattern around the meaningful part —
#: a literal ``ember1234`` matches exactly one page load.
VOLATILE_ID_MARKERS: tuple[str, ...] = (
    "ember",
    "urn:li:",
    "jobposting",
    "react-select",
    ":r",  # React 18 useId
)


@dataclass
class CapturedElement:
    """One interactive element, with every way it could be identified."""

    tag: str
    kind: str
    identifier: str = ""
    label: str = ""
    required: bool = False
    choices: list[str] = field(default_factory=list)
    strategies: list[Strategy] = field(default_factory=list)
    step: int = 0

    @property
    def is_question(self) -> bool:
        """Whether this reads like a screening question rather than a detail.

        A question mark is the strongest signal and the cheapest. Beyond that,
        a closed choice with a label long enough to be a sentence is almost
        always a screening question — "Do you have full working rights" often
        arrives without punctuation.
        """
        label = (self.label or "").strip()
        if not label or self.kind == "file":
            return False
        if label.endswith("?"):
            return True
        return bool(self.choices) and len(label.split()) >= 4


@dataclass
class Capture:
    """Everything one capture session learned."""

    platform: str
    variant: str
    steps: list[list[CapturedElement]] = field(default_factory=list)
    captured_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def elements(self) -> list[CapturedElement]:
        return [element for step in self.steps for element in step]

    @property
    def questions(self) -> list[CapturedElement]:
        return [element for element in self.elements if element.is_question]

    @property
    def fingerprint(self) -> str:
        return fingerprint_steps(self.steps)


@dataclass
class MergeReport:
    """What a merge changed, so a re-capture is inspectable rather than trusted."""

    new_elements: list[str] = field(default_factory=list)
    new_strategies: dict[str, list[str]] = field(default_factory=dict)
    preserved_counts: dict[str, tuple[int, int]] = field(default_factory=dict)
    new_variant: bool = False


# --------------------------------------------------------------------------
# Deriving strategies from markup
# --------------------------------------------------------------------------


def _text_of(tag: Tag) -> str:
    return " ".join(tag.get_text(" ", strip=True).split())[:80]


def _volatile(value: str) -> bool:
    lowered = value.casefold()
    return any(marker in lowered for marker in VOLATILE_ID_MARKERS) or any(
        character.isdigit() for character in value[-6:]
    )


def _stable_fragment(value: str) -> str:
    """The part of a volatile identifier worth matching on.

    ``jobs-document-upload-ember1234`` -> ``jobs-document-upload``. Splitting on
    the first volatile marker or trailing digits keeps the semantic prefix and
    drops the per-render tail.
    """
    lowered = value.casefold()
    for marker in VOLATILE_ID_MARKERS:
        index = lowered.find(marker)
        if index > 0:
            return value[:index].rstrip("-_ :")
    return value.rstrip("0123456789").rstrip("-_ :") or value


def _aria_role(tag: Tag) -> str:
    """The element's ARIA role, explicit or implicit.

    Only the implicit roles that matter for an application form are mapped. A
    fuller table would be dead weight: nothing here needs to know that ``<th>``
    is a columnheader.
    """
    explicit = tag.get("role")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()

    name = tag.name.lower()
    if name == "button":
        return "button"
    if name == "a":
        return "link" if tag.get("href") else ""
    if name == "select":
        return "combobox"
    if name == "textarea":
        return "textbox"
    if name == "input":
        return {
            "checkbox": "checkbox",
            "radio": "radio",
            "submit": "button",
            "button": "button",
            "file": "button",
        }.get(str(tag.get("type", "text")).lower(), "textbox")
    return ""


def _accessible_name(tag: Tag, soup: BeautifulSoup) -> str:
    """Roughly what a screen reader announces.

    aria-label, then the bound <label>, then the element's own text. Not a full
    accname implementation — that is a specification of its own — but it covers
    the shapes application forms actually use.
    """
    aria = tag.get("aria-label")
    if isinstance(aria, str) and aria.strip():
        return aria.strip()

    identifier = tag.get("id")
    if isinstance(identifier, str) and identifier:
        bound = soup.find("label", attrs={"for": identifier})
        if isinstance(bound, Tag):
            return _text_of(bound)

    parent = tag.find_parent("label")
    if isinstance(parent, Tag):
        return _text_of(parent)

    return _text_of(tag)


def _strategies_for(tag: Tag, soup: BeautifulSoup) -> list[Strategy]:
    """Every way of finding this element, ordered by durability.

    Order comes from ``STRATEGY_PRIORITY``, not from the order they are appended
    — ``Element.ordered`` sorts. Appending in priority order anyway keeps a
    hand-read JSON file sensible.
    """
    strategies: list[Strategy] = []

    for attribute in TESTID_ATTRIBUTES:
        value = tag.get(attribute)
        if isinstance(value, str) and value.strip():
            strategies.append(
                Strategy(
                    type="testid",
                    value=value.strip(),
                    attr=attribute,
                    note="captured",
                )
            )

    role = _aria_role(tag)
    name = _accessible_name(tag, soup)
    if role and name:
        strategies.append(Strategy(type="role", role=role, name=name, note="captured"))
    elif role:
        strategies.append(Strategy(type="role", role=role, note="captured"))

    aria = tag.get("aria-label")
    if isinstance(aria, str) and aria.strip():
        strategies.append(Strategy(type="label", value=aria.strip(), note="captured"))

    # Text only identifies elements whose visible text *is* their label —
    # buttons and links. A <select>'s text is its concatenated options
    # ("Select an option Yes No"), which identifies nothing and would match any
    # dropdown sharing those words.
    if tag.name in {"button", "a"} or _aria_role(tag) == "button":
        text = _text_of(tag)
        if text and len(text) <= 60:
            strategies.append(Strategy(type="text", value=text, note="captured"))

    # An id or name, as a CSS attribute selector. Volatile ones become patterns:
    # a literal captured on one job cannot match the next.
    for attribute in ("id", "name"):
        value = tag.get(attribute)
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        fragment = _stable_fragment(value) if _volatile(value) else ""
        if fragment and fragment != value and len(fragment) >= MIN_PATTERN_LENGTH:
            strategies.append(
                Strategy(
                    type="testid",
                    value=f"{fragment}*",
                    attr=attribute,
                    note="captured; volatile identifier stored as a pattern",
                )
            )
        else:
            # Either genuinely stable, or too short to pattern safely. A literal
            # that stops matching is a recoverable failure; a wildcard that
            # matches everything is a wrong click.
            strategies.append(
                Strategy(
                    type="css",
                    value=f"[{attribute}='{value}']",
                    note="captured",
                )
            )

    return strategies


def _kind_of(tag: Tag) -> str:
    name = tag.name.lower()
    if name == "select":
        return "select"
    if name == "textarea":
        return "textarea"
    if name in {"button", "a"}:
        return "button"
    input_type = str(tag.get("type", "text")).lower()
    if input_type in {"file", "radio", "checkbox"}:
        return input_type
    if input_type in {"submit", "button"}:
        return "button"
    return "text"


def extract_from_html(html: str, *, step: int = 0) -> list[CapturedElement]:
    """Derive every interactive element from one DOM snapshot."""
    soup = BeautifulSoup(html, "html.parser")
    captured: list[CapturedElement] = []

    for tag in soup.find_all(INTERACTIVE_TAGS):
        if not isinstance(tag, Tag):  # pragma: no cover - defensive
            continue
        if tag.name == "input" and str(tag.get("type", "")).lower() == "hidden":
            continue
        if tag.name == "a" and not any(
            tag.get(attribute)
            for attribute in ("role", "href", "aria-label", *TESTID_ATTRIBUTES)
        ):
            # A bare <a> with no destination and no hook is a text span. One
            # carrying a test hook is a control: Seek's Quick Apply is exactly
            # that shape, and skipping it dropped the single most important
            # element on the page.
            continue

        kind = _kind_of(tag)
        identifier = ""
        for attribute in ("name", "id", *TESTID_ATTRIBUTES):
            value = tag.get(attribute)
            if isinstance(value, str) and value.strip():
                identifier = value.strip()
                break

        choices: list[str] = []
        if kind == "select":
            choices = [
                _text_of(option)
                for option in tag.find_all("option")
                if _text_of(option)
                and _text_of(option).casefold()
                not in {"select an option", "choose an option", "please select"}
            ]

        captured.append(
            CapturedElement(
                tag=tag.name,
                kind=kind,
                identifier=identifier,
                label=_accessible_name(tag, soup),
                required=tag.get("required") is not None
                or str(tag.get("aria-required", "")).lower() == "true",
                choices=choices,
                strategies=_strategies_for(tag, soup),
                step=step,
            )
        )

    return captured


# --------------------------------------------------------------------------
# Reading a capture off disk
# --------------------------------------------------------------------------


def _html_from_har(har: dict[str, Any]) -> list[str]:
    """Every HTML response body in the capture, largest first.

    Largest first because the document is the interesting one and a HAR is full
    of small HTML fragments — ad frames, tracking pixels with an HTML body,
    error pages.
    """
    bodies: list[str] = []
    entries = (har.get("log") or {}).get("entries") or []
    for entry in entries:
        content = ((entry.get("response") or {}).get("content")) or {}
        if "html" not in str(content.get("mimeType", "")).lower():
            continue
        text = content.get("text")
        if isinstance(text, str) and text.strip():
            bodies.append(text)
    return sorted(bodies, key=len, reverse=True)


def extract(har_file: Path, *, platform: str, variant: str) -> Capture:
    """Read a capture and everything beside it.

    Prefers ``{variant}.steps.json`` — the per-step DOM snapshots — because a
    HAR cannot see a modal that JavaScript built after the document loaded, and
    that is every step of Easy Apply. Falls back to the HAR's own HTML bodies
    when no snapshots were taken, which at least covers server-rendered forms.
    """
    steps: list[list[CapturedElement]] = []

    snapshot_file = har_file.with_suffix(".steps.json")
    if snapshot_file.exists():
        payload = json.loads(snapshot_file.read_text(encoding="utf-8"))
        for index, snapshot in enumerate(payload.get("steps") or []):
            html = snapshot.get("html") or ""
            if html.strip():
                steps.append(extract_from_html(html, step=index))
        log.info(
            "capture_read_snapshots",
            platform=platform,
            variant=variant,
            steps=len(steps),
        )
    elif har_file.exists():
        har = json.loads(har_file.read_text(encoding="utf-8"))
        bodies = _html_from_har(har)
        if bodies:
            steps.append(extract_from_html(bodies[0], step=0))
        log.warning(
            "capture_read_har_only",
            platform=platform,
            variant=variant,
            note=(
                "no .steps.json beside the HAR, so only the server-rendered "
                "document was read; modal steps will be missing"
            ),
        )
    else:
        log.error("capture_absent", path=str(har_file))

    capture = Capture(platform=platform, variant=variant, steps=steps)
    log.info(
        "capture_extracted",
        platform=platform,
        variant=variant,
        steps=len(capture.steps),
        elements=len(capture.elements),
        questions=len(capture.questions),
        fingerprint=capture.fingerprint,
    )
    return capture


# --------------------------------------------------------------------------
# Merging into knowledge
# --------------------------------------------------------------------------


def _element_key(element: CapturedElement) -> str:
    """A stable knowledge key for a captured element.

    The identifier where there is one, normalised. Captures name elements by
    what they are on the page; the curated keys the adapters use
    (``apply_button``, ``submit_button``) are assigned by a human reviewing the
    merge. Inventing a mapping from "a button labelled Submit" to the adapter's
    ``submit_button`` would be a guess, and a wrong guess would rewire the
    adapter to click something else.
    """
    base = element.identifier or element.label or f"{element.tag}-{element.kind}"
    slug = "".join(
        character if character.isalnum() else "_" for character in base.casefold()
    ).strip("_")
    return f"captured_{slug}"[:80] or "captured_unnamed"


def merge_into(knowledge: SiteKnowledge, capture: Capture) -> MergeReport:
    """Fold a capture into existing knowledge without losing history.

    Success counts, failure counts, promotions and verification timestamps are
    production evidence: they record what has actually been working, which a
    capture cannot know. A re-capture adds strategies and refreshes the picture
    of the page; it never resets that evidence.
    """
    report = MergeReport()

    for element in capture.elements:
        key = _element_key(element)
        existing = knowledge.elements.get(key)

        if existing is None:
            knowledge.elements[key] = Element(
                key=key,
                strategies=list(element.strategies),
                required=element.required,
                notes=f"captured from {capture.platform}/{capture.variant}",
            )
            report.new_elements.append(key)
            knowledge.dirty = True
            continue

        # Preserve everything learned in production.
        report.preserved_counts[key] = (existing.success_count, existing.fail_count)

        known = {strategy.id for strategy in existing.strategies}
        added = [s for s in element.strategies if s.id not in known]
        if added:
            existing.strategies.extend(added)
            report.new_strategies[key] = [s.id for s in added]
            knowledge.dirty = True

    if capture.steps:
        if knowledge.known_variant(capture.steps) is None:
            report.new_variant = True
        knowledge.observe_flow(capture.steps, label=capture.variant)

    log.info(
        "capture_merged",
        platform=capture.platform,
        variant=capture.variant,
        new_elements=len(report.new_elements),
        elements_gaining_strategies=len(report.new_strategies),
        new_variant=report.new_variant,
    )
    return report


# --------------------------------------------------------------------------
# Screening questions -> answer bank
# --------------------------------------------------------------------------


def push_questions_to_answer_bank(session: Any, capture: Capture) -> int:
    """Record every screening question seen, unanswered. Returns rows added.

    Unanswered is the entire point. Hard rule 2: answers come only from the
    answer bank, and a capture is evidence about which questions get asked,
    not about how to answer them. Writing a guessed answer here would launder
    a guess into a "verified" row and every later application would trust it.

    Existing rows are left exactly as they are — including answered ones, whose
    answers must never be disturbed by a capture.
    """
    from sqlmodel import select

    from backend.apply.answers import normalise_question
    from backend.models import AnswerBank, AnswerType, MatchType

    existing = {
        normalise_question(row.question_pattern)
        for row in session.exec(select(AnswerBank)).all()
    }

    added = 0
    for element in capture.questions:
        question = element.label.strip()
        if normalise_question(question) in existing:
            continue

        session.add(
            AnswerBank(
                question_pattern=question,
                match_type=MatchType.FUZZY,
                # No answer. The user supplies it; nothing here may.
                #
                # A blank row is not inert: answers.resolve abstains on it
                # (AbstainReason.BLANK_ANSWER), which parks the job and asks
                # over Telegram. That is the answer-bank loop working as
                # designed, primed ahead of time instead of discovered mid-run.
                answer_value="",
                answer_type=(AnswerType.CHOICE if element.choices else AnswerType.TEXT),
                choices=list(element.choices) or None,
                # verified_at stays NULL: unconfirmed until the user says so.
                verified_at=None,
                notes=(
                    f"seen during the {capture.platform}/{capture.variant} capture "
                    f"on {capture.captured_at[:10]}"
                    + (
                        f"; options: {', '.join(element.choices)}"
                        if element.choices
                        else ""
                    )
                ),
            )
        )
        existing.add(normalise_question(question))
        added += 1

    log.info(
        "capture_questions_recorded",
        platform=capture.platform,
        variant=capture.variant,
        seen=len(capture.questions),
        added=added,
        note="all unanswered — the user answers them, never the capture",
    )
    return added
