"""Replay a captured flow offline, with no browser and no network.

WHY NOT ``route_from_har``
    ``har.replay`` already serves a capture back through Playwright, and that is
    the right tool for checking the real thing end to end. It needs an installed
    browser, which makes it useless as a routine test: the suite has to run on a
    machine where Chrome may not exist, and it has to run in a second, not a
    minute.

    This is the other half. A captured DOM snapshot is parsed with
    BeautifulSoup and exposed through enough of the Playwright locator protocol
    that a real adapter can be driven against it. No browser, no network, and
    fast enough to run on every commit — which is what turns a capture into a
    permanent fixture rather than a one-off investigation.

WHAT IT DELIBERATELY DOES NOT DO
    It is not a browser. There is no layout, so ``is_visible`` means "present
    and not hidden by an attribute" rather than "actually painted"; no
    JavaScript runs, so a step transition is moving to the next captured
    snapshot rather than something the page decides.

    Those limits are why this supplements ``har.replay`` instead of replacing
    it. What it does prove is the part that actually rots: that the recorded
    strategies still resolve against the markup the site really served.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag

from backend.logging_setup import get_logger

log = get_logger(__name__)

__all__ = ["SnapshotLocator", "SnapshotPage", "load_snapshots", "page_for_capture"]


_ROLE_SELECTOR = re.compile(
    r"^role=(?P<role>[a-z]+)"
    r"(?:\[name=(?:\"(?P<exact>[^\"]*)\"(?:\s+i)?|/(?P<pattern>.*)/i)\])?$"
)
_TEXT_SELECTOR = re.compile(r"^text=/(?P<pattern>.*)/i$")

#: Implicit roles, mirroring ``harextract._aria_role``. The two must agree or a
#: strategy captured by one will not resolve in the other, which would make the
#: harness quietly useless.
_IMPLICIT_ROLES: dict[str, str] = {
    "button": "button",
    "select": "combobox",
    "textarea": "textbox",
}
_INPUT_ROLES: dict[str, str] = {
    "checkbox": "checkbox",
    "radio": "radio",
    "submit": "button",
    "button": "button",
    "file": "button",
}


def _role_of(tag: Tag) -> str:
    explicit = tag.get("role")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    name = tag.name.lower()
    if name == "a":
        return "link" if tag.get("href") else ""
    if name == "input":
        return _INPUT_ROLES.get(str(tag.get("type", "text")).lower(), "textbox")
    return _IMPLICIT_ROLES.get(name, "")


def _accessible_name(tag: Tag, soup: BeautifulSoup) -> str:
    aria = tag.get("aria-label")
    if isinstance(aria, str) and aria.strip():
        return aria.strip()
    identifier = tag.get("id")
    if isinstance(identifier, str) and identifier:
        bound = soup.find("label", attrs={"for": identifier})
        if isinstance(bound, Tag):
            return " ".join(bound.get_text(" ", strip=True).split())
    parent = tag.find_parent("label")
    if isinstance(parent, Tag):
        return " ".join(parent.get_text(" ", strip=True).split())
    return " ".join(tag.get_text(" ", strip=True).split())


class SnapshotLocator:
    """The subset of Playwright's Locator an adapter actually uses."""

    def __init__(self, page: SnapshotPage, tags: list[Tag], selector: str) -> None:
        self._page = page
        self._tags = tags
        self._selector = selector

    @property
    def first(self) -> SnapshotLocator:
        return SnapshotLocator(self._page, self._tags[:1], self._selector)

    @property
    def last(self) -> SnapshotLocator:
        return SnapshotLocator(self._page, self._tags[-1:], self._selector)

    def count(self) -> int:
        return len(self._tags)

    def is_visible(self, timeout: int = 0) -> bool:
        """Present and not hidden by an attribute.

        No layout exists here, so this cannot know about ``display: none`` in a
        stylesheet. It errs towards visible, which is the safe direction for a
        harness: a false positive shows up as an adapter doing something the
        assertion then catches, while a false negative would silently skip the
        element under test.
        """
        if not self._tags:
            return False
        tag = self._tags[0]
        if tag.get("hidden") is not None:
            return False
        if str(tag.get("aria-hidden", "")).lower() == "true":
            return False
        style = str(tag.get("style", "")).replace(" ", "").lower()
        return "display:none" not in style and "visibility:hidden" not in style

    def all(self) -> list[SnapshotLocator]:
        return [SnapshotLocator(self._page, [tag], self._selector) for tag in self._tags]

    def all_inner_texts(self) -> list[str]:
        return [" ".join(tag.get_text(" ", strip=True).split()) for tag in self._tags]

    def inner_text(self) -> str:
        return self.all_inner_texts()[0] if self._tags else ""

    def get_attribute(self, name: str) -> str | None:
        if not self._tags:
            return None
        value = self._tags[0].get(name)
        if value is None:
            return None
        return " ".join(value) if isinstance(value, list) else str(value)

    def evaluate(self, expression: str) -> Any:
        """Only ``el => el.tagName.toLowerCase()`` is supported.

        That is the single expression the adapters use. Anything else raises
        rather than returning a plausible-looking wrong answer — a harness that
        silently invents evaluation results is a harness that green-lights
        broken code.
        """
        if "tagName" in expression:
            return self._tags[0].name.lower() if self._tags else ""
        raise NotImplementedError(
            f"SnapshotPage cannot evaluate {expression!r}; it runs no JavaScript"
        )

    def locator(self, selector: str) -> SnapshotLocator:
        """Scope a query to this element's subtree."""
        matched: list[Tag] = []
        for tag in self._tags:
            matched.extend(self._page.query(selector, root=tag))
        return SnapshotLocator(self._page, matched, selector)

    # -- actions: recorded, not performed -----------------------------------

    def click(self) -> None:
        self._page.clicked.append(self._selector)

    def check(self) -> None:
        self._page.checked.append(self._selector)

    def fill(self, value: str) -> None:
        self._page.filled[self._selector] = value

    def select_option(self, label: str | None = None, **kwargs: Any) -> None:
        self._page.filled[self._selector] = label or str(kwargs)

    def set_input_files(self, path: Any) -> None:
        self._page.uploaded.append(str(path))


class SnapshotPage:
    """A page backed by captured HTML rather than a browser."""

    def __init__(self, html: str, url: str = "https://example.invalid/") -> None:
        self.soup = BeautifulSoup(html, "html.parser")
        self.url = url
        self.clicked: list[str] = []
        self.checked: list[str] = []
        self.filled: dict[str, str] = {}
        self.uploaded: list[str] = []
        self.queried: list[str] = []

    # -- selector engine ----------------------------------------------------

    def query(self, selector: str, root: Tag | None = None) -> list[Tag]:
        """Resolve a Playwright selector against the snapshot.

        Handles the three engines ``Strategy.selector`` emits — ``role=``,
        ``text=/../i`` and plain CSS. An unsupported engine raises rather than
        returning nothing, because "no match" and "I did not understand the
        selector" must not look the same to a test.
        """
        scope = root if root is not None else self.soup

        role_match = _ROLE_SELECTOR.match(selector)
        if role_match:
            role = role_match.group("role")
            exact = role_match.group("exact")
            pattern = role_match.group("pattern")
            found = []
            for tag in scope.find_all(True):
                if _role_of(tag) != role:
                    continue
                if exact is None and pattern is None:
                    found.append(tag)
                    continue
                name = _accessible_name(tag, self.soup)
                if exact is not None and name.casefold() == exact.casefold():
                    found.append(tag)
                elif pattern is not None and re.search(pattern, name, re.IGNORECASE):
                    found.append(tag)
            return found

        text_match = _TEXT_SELECTOR.match(selector)
        if text_match:
            pattern = text_match.group("pattern")
            return [
                tag
                for tag in scope.find_all(True)
                if re.search(
                    pattern,
                    " ".join(tag.get_text(" ", strip=True).split()),
                    re.IGNORECASE,
                )
            ]

        if selector.startswith(("role=", "text=", "xpath=")):
            raise NotImplementedError(f"unsupported selector engine: {selector!r}")

        # Playwright's :has-text() is not CSS; nothing this project generates
        # uses it any more, but a hand-edited knowledge file could.
        if ":has-text(" in selector:
            raise NotImplementedError(
                f"SnapshotPage does not implement :has-text(): {selector!r}"
            )

        return list(scope.select(selector))

    def locator(self, selector: str) -> SnapshotLocator:
        self.queried.append(selector)
        return SnapshotLocator(self, self.query(selector), selector)

    # -- the rest of the page protocol --------------------------------------

    def goto(self, url: str, **kwargs: Any) -> None:
        self.url = url

    def wait_for_load_state(self, *args: Any, **kwargs: Any) -> None:
        return None

    def screenshot(self, **kwargs: Any) -> None:
        return None

    def content(self) -> str:
        return str(self.soup)


def load_snapshots(path: Path) -> list[dict[str, Any]]:
    """Read a ``{variant}.steps.json`` written during a capture."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("steps") or [])


def page_for_capture(path: Path, step: int = 0) -> SnapshotPage:
    """A page backed by one step of a capture."""
    snapshots = load_snapshots(path)
    if not snapshots:
        raise ValueError(f"{path} contains no steps")
    snapshot = snapshots[min(step, len(snapshots) - 1)]
    return SnapshotPage(snapshot.get("html", ""), snapshot.get("url", ""))
