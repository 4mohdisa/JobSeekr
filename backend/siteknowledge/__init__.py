"""Site knowledge: how to find things on a platform, stored as data.

WHY THIS EXISTS
    Unknown employer forms already had a memory — ``ats/formmaps.py`` learns a
    form's shape once and reuses it everywhere that shape appears. The two
    platforms carrying nearly all the volume, Seek and LinkedIn, had none:
    their selectors were Python literals, so every redesign was a code change
    and nothing the system observed at runtime was ever written down.

    This is the missing half. Same idea as a form map — JSON on disk, editable
    by hand, indexed by what it is rather than where it came from — applied to
    the primary boards.

MULTIPLE STRATEGIES, ORDERED BY DURABILITY
    Every element carries several ways to find it, tried in a fixed order:

        testid  data-automation / data-testid — the site's own test hooks,
                which survive redesigns because their own tests depend on them
        role    ARIA role plus accessible name — what a screen reader sees,
                and what an accessibility audit keeps stable
        label   aria-label substring
        text    visible text
        css     last resort

    CSS class names come last on purpose: they are generated, they carry no
    meaning, and they change on every redesign. A layer whose only strategy is
    CSS is a layer that breaks quarterly.

PATTERNS, NOT LITERALS
    LinkedIn embeds the job URN in field ids and generates Ember ids that differ
    on every page load, so a literal captured from one job is worthless on the
    next. Any strategy value may contain ``*``; it becomes a substring match.
    See ``Strategy.selector``.

SELF-HEALING
    ``resolve`` tries ``last_working_strategy`` first, then walks the rest in
    priority order. Whichever one works is promoted to ``last_working_strategy``
    and the drift is logged, so the file records what the site is doing now
    rather than what it did when the selectors were written.

    If every strategy fails, it raises ``ElementNotFound``. It never falls back
    to a guess: guessing is how the wrong button gets clicked on a page that has
    already changed underneath us.

WHERE, NEVER WHAT
    Same rule as form maps. This records how to find a field. It never records
    the value that goes in one — those live in the profile and the answer bank,
    which is what keeps a stale answer from being replayed months later.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.config import settings
from backend.logging_setup import get_logger

log = get_logger(__name__)

__all__ = [
    "HISTORY_LIMIT",
    "STRATEGY_PRIORITY",
    "Element",
    "ElementNotFound",
    "FlowVariant",
    "SiteKnowledge",
    "Strategy",
    "drain_resolutions",
    "fingerprint_steps",
    "load",
    "on_all_strategies_failed",
    "on_strategy_drift",
    "rollback",
]


DEFAULTS_DIR = Path(__file__).resolve().parent / "defaults"
"""Starting-point knowledge that ships with the code.

Copied into ``data/siteknowledge/`` the first time a platform is loaded, and
never read again after that. The copy under ``data/`` is the live one: it is
what the user edits and what resolution promotes into, and it must not be
silently overwritten by a package upgrade.
"""


STRATEGY_PRIORITY: tuple[str, ...] = ("testid", "role", "label", "text", "css")
"""Resolution order, most durable first. See the module docstring."""


# Set by the integrations layer, same convention as ``canary.on_drift``. Left
# None in tests and in a checkout with no bot configured.
on_strategy_drift: Any = None
on_all_strategies_failed: Any = None


# How element resolution went since the last drain. A "first" is the top
# strategy working, which is what this file's ordering claims should happen.
# Anything else — a lower strategy healing it, or nothing working at all — is a
# miss: the element may still have been found, but the recorded idea of how to
# find it was wrong, and that is the number worth trending.
#
# Counted here and drained by the caller rather than written to the database,
# because this layer has no session and must not acquire one: it is loaded by
# the canary, by the session check and by every adapter, none of which should
# open a transaction to look up a button.
_resolutions: dict[str, int] = {"first": 0, "later": 0}


def drain_resolutions() -> tuple[int, int]:
    """(first-strategy hits, resolutions that needed a lower strategy or failed).

    Resets the counters. The caller records them against whatever unit of work
    it was doing; the apply flow drains once per application.
    """
    first, later = _resolutions["first"], _resolutions["later"]
    _resolutions["first"] = 0
    _resolutions["later"] = 0
    return first, later


class ElementNotFound(RuntimeError):
    """Every known way of finding an element failed.

    Deliberately its own type rather than a bare RuntimeError: the flow routes
    this to the manual queue and alerts, because a platform that no longer
    matches any recorded strategy is a platform we should stop guessing at.
    """

    def __init__(self, platform: str, key: str, tried: list[str]) -> None:
        self.platform = platform
        self.key = key
        self.tried = tried
        super().__init__(
            f"{platform}: no strategy resolved {key!r} (tried {len(tried)}: {tried})"
        )


# --------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------


def _escape_regex(value: str) -> str:
    """Escape for a JavaScript regex, which is what Playwright parses.

    ``re.escape`` escapes a space as ``\\ ``. Python accepts that; JavaScript
    treats it as an identity escape, which is a SyntaxError in unicode mode.
    Since a space is not special in either dialect, unescaping it is both safe
    and necessary.
    """
    return re.escape(value).replace("\\ ", " ")


def _wildcard_to_regex(value: str) -> str:
    """``ember*`` -> ``ember.*``, with everything else escaped."""
    return ".*".join(_escape_regex(part) for part in value.split("*"))


@dataclass
class Strategy:
    """One way of finding an element.

    ``value`` may contain ``*`` as a wildcard. That is not cosmetic: LinkedIn
    field ids embed the job URN (``...jobPosting:4012345678...``) and Ember
    generates ``ember1234`` ids that change per page load, so a literal
    captured during one capture session cannot match the next job.
    """

    type: str
    value: str = ""
    #: testid only — which attribute carries it. Seek uses data-automation,
    #: most other sites use data-testid, so it cannot be assumed.
    attr: str = "data-testid"
    #: role only — the ARIA role and the accessible name to match.
    role: str = ""
    name: str = ""
    #: Human note about where this came from, e.g. "HAR capture 2026-09-03".
    note: str = ""

    #: True for a candidate inherited from the shared vocabulary rather than
    #: written for this platform. Only breaks ties — evidence still decides the
    #: order, because a generic strategy that keeps working IS better than a
    #: platform one that keeps failing.
    shared: bool = False

    #: True for a strategy the system derived from the live page after every
    #: recorded one failed. NEVER tried by resolution — see Element.ordered.
    #: It is a suggestion to the user, and a suggestion that could quietly
    #: resolve an element is a suggestion that has already been accepted.
    proposed: bool = False

    #: Times this exact strategy found the element, and times it was tried and
    #: did not. Written by resolution, and what `confidence` is computed from.
    #: Per strategy rather than per element: "this element is unreliable" is not
    #: actionable, "this SELECTOR stopped working" is.
    success_count: int = 0
    fail_count: int = 0

    @property
    def confidence(self) -> float:
        """Observed reliability, Laplace-smoothed, in 0..1.

        ``(hits + 1) / (tries + 2)``. The smoothing is what makes the number
        usable for ordering rather than just for reporting: a strategy nobody
        has tried scores exactly 0.5, so it sorts BELOW anything with a record
        of working and ABOVE anything with a record of failing. Without it an
        untried strategy is 0/0 and every tie-break becomes a special case.

        One success out of one gives 0.67, not 1.0 — a single observation should
        not outrank a strategy that has worked forty times.
        """
        return (self.success_count + 1) / (self.success_count + self.fail_count + 2)

    def __post_init__(self) -> None:
        if self.type not in STRATEGY_PRIORITY:
            raise ValueError(
                f"unknown strategy type {self.type!r}; expected one of {STRATEGY_PRIORITY}"
            )

    @property
    def priority(self) -> int:
        return STRATEGY_PRIORITY.index(self.type)

    @property
    def selector(self) -> str:
        """A Playwright selector string.

        Everything becomes a selector rather than a different locator call per
        type, so resolution stays a single ``page.locator(...)`` loop and a
        hand-written JSON file cannot reach a code path nothing else uses.
        """
        wild = "*" in self.value
        if self.type == "testid":
            operator = "*=" if wild else "="
            literal = self.value.replace("*", "")
            return f"[{self.attr}{operator}'{literal}']"

        if self.type == "role":
            if not self.name:
                return f"role={self.role}"
            if "*" in self.name:
                return f"role={self.role}[name=/{_wildcard_to_regex(self.name)}/i]"
            return f'role={self.role}[name="{self.name}" i]'

        if self.type == "label":
            literal = self.value.replace("*", "")
            return f"[aria-label*='{literal}' i]"

        if self.type == "text":
            if wild:
                return f"text=/{_wildcard_to_regex(self.value)}/i"
            return f"text=/{_escape_regex(self.value)}/i"

        # css: passed through untouched. The escape hatch, and the reason the
        # priority order puts it last rather than forbidding it.
        return self.value

    @property
    def id(self) -> str:
        """Stable identity, used to record which strategy last worked.

        The selector string itself: deterministic, unique among an element's
        strategies in practice, and readable in the JSON file — which matters
        because a human is expected to open these.
        """
        return f"{self.type}:{self.selector}"


# --------------------------------------------------------------------------
# Elements
# --------------------------------------------------------------------------


@dataclass
class Element:
    """One thing we need to find, and everything learned about finding it."""

    key: str
    strategies: list[Strategy] = field(default_factory=list)
    #: Whether the flow can proceed without it. A missing optional element is a
    #: normal outcome (Seek's cover-letter textarea is absent on some forms);
    #: a missing required one is a failure.
    required: bool = True
    last_working_strategy: str | None = None
    last_verified_at: str | None = None
    success_count: int = 0
    fail_count: int = 0
    notes: str = ""

    #: Strategies derived from a live page after every recorded one failed,
    #: waiting for the user to accept or reject them. Never tried.
    proposals: list[Strategy] = field(default_factory=list)

    @property
    def confidence(self) -> float:
        """How reliably this element gets found at all, Laplace-smoothed.

        Element-level rather than strategy-level: it answers "is this element
        still findable", which is the question the digest asks before it fails
        outright. ``success_count`` and ``fail_count`` have existed since the
        layer was written and nothing ever read them.
        """
        return (self.success_count + 1) / (self.success_count + self.fail_count + 2)

    @property
    def observations(self) -> int:
        return self.success_count + self.fail_count

    def ordered(self) -> list[Strategy]:
        """Strategies in the order to try them, best evidence first.

        Three keys, in this order:

        1. ``last_working_strategy`` — the freshest evidence about what the site
           looks like right now, and it beats a better lifetime record because
           a site that changed yesterday changed for everyone.
        2. Observed reliability. This is the change: the order used to be the
           original guess at which selector TYPE is most durable, and a testid
           that had failed eleven times still went first because test ids are
           durable in theory. Evidence outranks the guess.
        3. Platform knowledge before the shared vocabulary, then durability.
           Among strategies nobody has tried yet, a platform's own selector is
           better evidence than a generic candidate — the vocabulary is a
           fallback, not a replacement. Note the level this sits at: a SHARED
           candidate that keeps working still outranks a platform one that keeps
           failing, because confidence is checked first. Only the unproven are
           ordered by where they came from.

        Proposals are excluded. A suggestion that could quietly resolve an
        element is a suggestion that has already been accepted.
        """
        candidates = [s for s in self.strategies if not s.proposed]
        return sorted(
            candidates,
            key=lambda s: (
                s.id != self.last_working_strategy,
                -s.confidence,
                s.shared,
                s.priority,
            ),
        )


# --------------------------------------------------------------------------
# Flow variants
# --------------------------------------------------------------------------


def fingerprint_steps(steps: list[list[Any]]) -> str:
    """Identify a multi-step flow by its shape.

    LinkedIn's Easy Apply is two steps for some employers and five for others,
    so "how many steps and what is on each" is the thing worth recognising.

    Built by reusing ``formmaps.fingerprint_fields`` per step and hashing the
    sequence — the per-step hash is order-insensitive within a step (the DOM
    order of fields is not meaningful) while the sequence of steps is ordered
    (step 3 asking for a cover letter is a different flow from step 1 asking).
    """
    import hashlib

    from backend.ats.formmaps import fingerprint_fields

    per_step = [fingerprint_fields(fields) for fields in steps]
    joined = ">".join(per_step)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


@dataclass
class FlowVariant:
    """A step structure this platform has actually been observed to use."""

    fingerprint: str
    step_count: int
    #: Per step: the field kinds seen, for a human reading the file.
    steps: list[dict[str, Any]] = field(default_factory=list)
    label: str = ""
    observed_count: int = 0
    first_seen_at: str = ""
    last_seen_at: str = ""


# --------------------------------------------------------------------------
# The knowledge file
# --------------------------------------------------------------------------


@dataclass
class SiteKnowledge:
    """Everything known about one platform. Backed by JSON under ``data/``."""

    platform: str
    elements: dict[str, Element] = field(default_factory=dict)
    flow_variants: dict[str, FlowVariant] = field(default_factory=dict)
    quirks: list[dict[str, Any]] = field(default_factory=list)
    directory: Path | None = None
    #: Set when resolution promotes a strategy or a variant is observed, so
    #: ``save`` can be called unconditionally without rewriting clean files.
    dirty: bool = False

    # -- resolution -------------------------------------------------------

    def resolve(
        self,
        page: Any,
        key: str,
        *,
        visible: bool = True,
        timeout_ms: int = 2500,
    ) -> Any | None:
        """Find an element, healing and promoting as it goes.

        Returns the locator, or None when the element is optional and genuinely
        absent. Raises ``ElementNotFound`` when a *required* element cannot be
        found by any recorded strategy.

        ``visible=False`` matches attached-but-hidden nodes. File inputs are
        routinely hidden behind a styled button, so an upload target has to be
        found by presence; using visibility for those was a real bug class.
        """
        element = self.elements.get(key)
        if element is None:
            raise ElementNotFound(self.platform, key, [])

        tried: list[str] = []
        attempted: list[Strategy] = []
        for position, strategy in enumerate(element.ordered()):
            selector = strategy.selector
            tried.append(selector)
            attempted.append(strategy)
            try:
                locator = page.locator(selector).first
                found = (
                    locator.is_visible(timeout=timeout_ms)
                    if visible
                    else locator.count() > 0
                )
            except Exception as exc:  # noqa: BLE001 - absence is the normal case
                log.debug(
                    "strategy_absent",
                    platform=self.platform,
                    key=key,
                    selector=selector,
                    error=str(exc)[:120],
                )
                continue

            if found:
                _resolutions["first" if position == 0 else "later"] += 1
                # Every strategy tried BEFORE the winner was tried and did not
                # work. Recording only the winner is how the ordering never
                # learns: a broken selector that is asked first every time
                # accumulates no evidence that it is broken.
                self._record_attempts(attempted[:-1], strategy)
                self._record_success(element, strategy)
                return locator

        _resolutions["later"] += 1
        self._record_attempts(attempted, None)
        self._record_failure(element, tried)
        if element.required:
            # Re-derive BEFORE giving up. Every recorded way of finding this
            # element has failed, which is exactly the moment the shared
            # vocabulary's generic candidates are worth testing against the live
            # page — and if one of them finds it, the user gets a suggested fix
            # instead of a parked job and a mystery.
            proposal = self._propose_strategy(page, element, visible=visible)
            if on_all_strategies_failed is not None:
                try:
                    on_all_strategies_failed(
                        self.platform, key, tried, proposal.selector if proposal else ""
                    )
                except TypeError:
                    # An older hook that takes three arguments. Alerting on the
                    # failure matters more than including the suggestion.
                    try:
                        on_all_strategies_failed(self.platform, key, tried)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("strategy_alert_hook_failed", error=str(exc)[:150])
                except Exception as exc:  # noqa: BLE001 - alerting must not mask the fault
                    log.warning("strategy_alert_hook_failed", error=str(exc)[:150])
            raise ElementNotFound(self.platform, key, tried)

        log.debug("optional_element_absent", platform=self.platform, key=key)
        return None

    def _propose_strategy(
        self, page: Any, element: Element, *, visible: bool = True
    ) -> Strategy | None:
        """Derive a new way of finding this element from the live page.

        Generation rather than selection, which is the gap this closes: the
        layer could only ever pick from strategies someone had already written,
        so when all of them broke it parked the job and waited for a human to
        open the site.

        The candidates come from the shared vocabulary — accessible role and
        name, the two things a redesign cannot change without changing what the
        form IS — and each is tried against the page in front of us. The first
        that resolves is recorded as a PROPOSAL: stored on the element, sent to
        the user, and never used to resolve anything until they accept it. A
        derived selector that silently started answering would be the system
        guessing at where the Submit button is, which is the one place guessing
        is unacceptable.

        Returns the proposal, or None when nothing generic matched either.
        """
        from backend.siteknowledge.vocabulary import shared_candidates

        known = {strategy.id for strategy in element.strategies}
        known.update(strategy.id for strategy in element.proposals)

        for candidate in shared_candidates(element.key):
            if candidate.id in known:
                continue
            try:
                locator = page.locator(candidate.selector).first
                found = (
                    locator.is_visible(timeout=1000) if visible else locator.count() > 0
                )
            except Exception as exc:  # noqa: BLE001 - absence is normal here
                log.debug(
                    "derived_candidate_absent",
                    platform=self.platform,
                    key=element.key,
                    selector=candidate.selector,
                    error=str(exc)[:120],
                )
                continue
            if not found:
                continue

            candidate.proposed = True
            candidate.note = (
                f"derived {datetime.now(UTC).date().isoformat()} after every "
                f"recorded strategy failed; not in use until accepted"
            )
            element.proposals.append(candidate)
            self.dirty = True
            log.warning(
                "strategy_proposed",
                platform=self.platform,
                key=element.key,
                selector=candidate.selector,
                note="found on the live page; awaiting confirmation",
            )
            return candidate

        log.error(
            "no_strategy_could_be_derived",
            platform=self.platform,
            key=element.key,
            note="the element is not findable by role or name either",
        )
        return None

    def accept_proposal(self, key: str, selector: str) -> Strategy | None:
        """Promote a proposal to a real strategy. The user said yes.

        Appended rather than replacing anything: the old strategies stay, with
        their record of having failed, which is what keeps them ordered last
        instead of being silently forgotten and re-added by a later capture.
        """
        element = self.elements.get(key)
        if element is None:
            return None
        match = next((s for s in element.proposals if s.selector == selector), None)
        if match is None:
            return None

        element.proposals.remove(match)
        match.proposed = False
        # A platform strategy now, not a shared candidate: the user confirmed it
        # for THIS site, and it must survive the save that drops shared ones.
        match.shared = False
        element.strategies.append(match)
        self.dirty = True
        log.info(
            "strategy_accepted", platform=self.platform, key=key, selector=selector
        )
        return match

    def reject_proposal(self, key: str, selector: str) -> bool:
        """The user said no. Deleted, so the next failure derives again."""
        element = self.elements.get(key)
        if element is None:
            return False
        match = next((s for s in element.proposals if s.selector == selector), None)
        if match is None:
            return False
        element.proposals.remove(match)
        self.dirty = True
        log.info(
            "strategy_rejected", platform=self.platform, key=key, selector=selector
        )
        return True

    def _record_attempts(self, failed: list[Strategy], winner: Strategy | None) -> None:
        """Credit the strategy that worked, debit the ones that did not.

        Called on every resolution, not only on failure. Before this the layer
        promoted a strategy only when the one above it failed — it learned from
        failure and nothing else, so a strategy that had worked forty times
        carried exactly as much weight as one written from a guess.
        """
        for strategy in failed:
            strategy.fail_count += 1
            self.dirty = True
        if winner is not None:
            winner.success_count += 1
            self.dirty = True

    def _record_success(self, element: Element, strategy: Strategy) -> None:
        drifted = (
            element.last_working_strategy is not None
            and element.last_working_strategy != strategy.id
        )
        if drifted:
            log.warning(
                "strategy_drift",
                platform=self.platform,
                key=element.key,
                was=element.last_working_strategy,
                now=strategy.id,
            )
            if on_strategy_drift is not None:
                try:
                    on_strategy_drift(
                        self.platform,
                        element.key,
                        element.last_working_strategy or "",
                        strategy.id,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("strategy_drift_hook_failed", error=str(exc)[:150])

        if element.last_working_strategy != strategy.id:
            element.last_working_strategy = strategy.id
            self.dirty = True

        element.success_count += 1
        element.last_verified_at = datetime.now(UTC).isoformat()
        self.dirty = True

    def _record_failure(self, element: Element, tried: list[str]) -> None:
        element.fail_count += 1
        self.dirty = True
        log.error(
            "element_unresolved",
            platform=self.platform,
            key=element.key,
            required=element.required,
            tried=tried,
        )

    def present(self, page: Any, key: str, *, timeout_ms: int = 2500) -> bool:
        """Whether an element is there, as a boolean rather than an exception.

        "Is Submit visible yet?" has a legitimate answer of no. Letting
        ``ElementNotFound`` escape from that question would park a job for the
        crime of being on step two of three, so the required-element alarm is
        deliberately suppressed here and nowhere else.
        """
        try:
            return self.resolve(page, key, timeout_ms=timeout_ms) is not None
        except ElementNotFound:
            return False

    def read_all_text(self, page: Any, key: str) -> list[str]:
        """Every matching node's text, across every strategy.

        Used for attachment read-back, which deliberately collects from all
        matches rather than the first: a modal showing both the old and the new
        filename *is* the stale-upload case, and taking only the first match
        would hide exactly the failure this read-back exists to catch.
        """
        element = self.elements.get(key)
        if element is None:
            raise ElementNotFound(self.platform, key, [])

        names: list[str] = []
        tried: list[str] = []
        for strategy in element.ordered():
            tried.append(strategy.selector)
            try:
                for text in page.locator(strategy.selector).all_inner_texts():
                    cleaned = text.strip()
                    if cleaned and cleaned not in names:
                        names.append(cleaned)
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "readback_strategy_absent",
                    platform=self.platform,
                    key=key,
                    error=str(exc)[:120],
                )

        if names:
            return names

        self._record_failure(element, tried)
        if element.required:
            raise ElementNotFound(self.platform, key, tried)
        return []

    # -- flow variants ----------------------------------------------------

    def observe_flow(self, steps: list[list[Any]], *, label: str = "") -> FlowVariant:
        """Record the step structure just walked. Returns the variant.

        An unrecognised structure is recorded and returned rather than raising:
        a five-step Easy Apply is not an error, it is a variant we had not seen.
        The caller decides to walk it cautiously; this only remembers it.
        """
        fingerprint = fingerprint_steps(steps)
        now = datetime.now(UTC).isoformat()
        variant = self.flow_variants.get(fingerprint)

        if variant is None:
            variant = FlowVariant(
                fingerprint=fingerprint,
                step_count=len(steps),
                steps=[
                    {
                        "index": index,
                        "field_count": len(fields),
                        "kinds": sorted(
                            {str(getattr(f, "kind", "unknown")) for f in fields}
                        ),
                        "required": sorted(
                            str(getattr(f, "identifier", ""))
                            for f in fields
                            if getattr(f, "required", False)
                        ),
                    }
                    for index, fields in enumerate(steps)
                ],
                label=label or f"{len(steps)}-step",
                first_seen_at=now,
            )
            self.flow_variants[fingerprint] = variant
            log.info(
                "flow_variant_new",
                platform=self.platform,
                fingerprint=fingerprint,
                steps=len(steps),
            )
        else:
            log.debug(
                "flow_variant_known",
                platform=self.platform,
                fingerprint=fingerprint,
                seen=variant.observed_count,
            )

        variant.observed_count += 1
        variant.last_seen_at = now
        self.dirty = True
        return variant

    def known_variant(self, steps: list[list[Any]]) -> FlowVariant | None:
        """Whether this exact step structure has been seen before."""
        return self.flow_variants.get(fingerprint_steps(steps))

    # -- quirks -----------------------------------------------------------

    def quirk(self, key: str) -> dict[str, Any] | None:
        return next((q for q in self.quirks if q.get("key") == key), None)

    # -- persistence ------------------------------------------------------

    def save(self, *, force: bool = False, reason: str = "resolution") -> None:
        """Write promotions and counters back, keeping the version replaced.

        A no-op when nothing changed. ``reason`` says WHY this version exists —
        resolution promoting a strategy, a capture ingest, an accepted proposal,
        a hand edit — and it is the difference between a history that can be
        rolled back and a pile of timestamps. Three different things write these
        files and none of them left a record of having done so; a bad ingest was
        permanent.
        """
        if not (self.dirty or force):
            return
        if self.directory is None:  # pragma: no cover - only in synthetic tests
            return

        self.directory.mkdir(parents=True, exist_ok=True)
        self._archive(reason)
        _write_json(
            self.directory / "elements.json",
            {
                "platform": self.platform,
                "elements": {
                    key: _element_payload(element)
                    for key, element in sorted(self.elements.items())
                },
            },
        )
        _write_json(
            self.directory / "flows.json",
            {
                "platform": self.platform,
                "variants": {
                    fp: asdict(variant)
                    for fp, variant in sorted(self.flow_variants.items())
                },
            },
        )
        self.dirty = False
        log.debug("site_knowledge_saved", platform=self.platform, reason=reason)

    # -- versions ---------------------------------------------------------

    def _archive(self, reason: str) -> None:
        """Copy the file about to be overwritten into history, and index it.

        Before the write, not after: the point is to keep what is being
        replaced. A failure here must not stop the save — losing a history entry
        is recoverable, refusing to record what resolution learned is not — so
        everything is caught and logged.
        """
        if self.directory is None:  # pragma: no cover - synthetic knowledge
            return
        current = self.directory / "elements.json"
        if not current.exists():
            return

        try:
            history_dir = self.directory / "history"
            history_dir.mkdir(parents=True, exist_ok=True)
            entries = self.history()
            version = (entries[-1]["version"] + 1) if entries else 1

            shutil.copy2(current, history_dir / f"{version:04d}-elements.json")
            entries.append(
                {
                    "version": version,
                    "at": datetime.now(UTC).isoformat(),
                    "reason": reason,
                    "elements": len(self.elements),
                }
            )
            _write_json(
                history_dir / "index.json",
                {"platform": self.platform, "versions": entries[-HISTORY_LIMIT:]},
            )
            for stale in entries[:-HISTORY_LIMIT]:
                (history_dir / f"{stale['version']:04d}-elements.json").unlink(
                    missing_ok=True
                )
        except OSError as exc:
            log.warning(
                "site_knowledge_history_failed",
                platform=self.platform,
                error=str(exc)[:200],
            )

    def history(self) -> list[dict[str, Any]]:
        """Every kept version, oldest first: what changed, when, and why."""
        if self.directory is None:  # pragma: no cover - synthetic knowledge
            return []
        index = _read_json(self.directory / "history" / "index.json")
        return list(index.get("versions") or [])


HISTORY_LIMIT = 20
"""How many previous versions of a platform's elements file to keep.

Twenty because the thing being recovered from is a bad edit or a bad ingest,
which is noticed within days — and these files are tens of kilobytes, so the
cost of keeping them is a rounding error against the cost of not being able to
undo a capture that overwrote a working selector.
"""


def rollback(platform: str, version: int, *, directory: Path | None = None) -> bool:
    """Restore a platform's elements file to a kept version.

    Returns False when that version is not in the history rather than raising:
    the caller is a person typing a number, and the honest answer to a number
    that is not there is "no", not a traceback.

    The rollback is itself archived, so rolling back to the wrong version is
    also undoable. That matters more than it sounds: the reason to roll back at
    all is that something overwrote a working file, and a one-way undo just
    moves which write is unrecoverable.
    """
    target = directory if directory is not None else _platform_dir(platform)
    source = target / "history" / f"{version:04d}-elements.json"
    if not source.exists():
        log.error(
            "site_knowledge_rollback_missing",
            platform=platform,
            version=version,
            path=str(source),
        )
        return False

    knowledge = load(platform, directory=target)
    knowledge._archive(f"superseded by rollback to v{version}")
    shutil.copy2(source, target / "elements.json")
    log.warning(
        "site_knowledge_rolled_back",
        platform=platform,
        version=version,
        note="elements.json replaced; flows and quirks untouched",
    )
    return True


def _element_payload(element: Element) -> dict[str, Any]:
    """One element as it should be written back.

    Shared-vocabulary candidates are dropped: they are merged in at load from
    ``vocabulary.py``, and writing them into a platform file would fork eleven
    private copies that a correction to the vocabulary could never reach. The
    evidence they accumulated is lost with them, which is the deliberate cost —
    a generic candidate's record belongs to the generic candidate, and pooling
    nine platforms' evidence into it would be worse than starting fresh.
    """
    payload = asdict(element)
    payload["strategies"] = [
        asdict(strategy) for strategy in element.strategies if not strategy.shared
    ]
    payload["proposals"] = [asdict(strategy) for strategy in element.proposals]
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write atomically. A half-written knowledge file breaks every later run."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # A hand-edited file with a typo must be loud, not silently ignored:
        # falling back to defaults would quietly discard the user's edit.
        log.error("site_knowledge_unreadable", path=str(path), error=str(exc)[:200])
        raise


def _platform_dir(platform: str) -> Path:
    return settings.siteknowledge_dir / platform


def _seed_from_defaults(platform: str, target: Path) -> None:
    """Copy the packaged starting point into ``data/`` on first use.

    Only when the target does not exist. The live copy is the user's — a
    package upgrade must never overwrite selectors they corrected by hand or
    strategies that resolution promoted.
    """
    source = DEFAULTS_DIR / platform
    if not source.is_dir() or target.exists():
        return
    target.mkdir(parents=True, exist_ok=True)
    for item in source.glob("*.json"):
        shutil.copy2(item, target / item.name)
    log.info("site_knowledge_seeded", platform=platform, path=str(target))


def _with_shared(key: str, strategies: list[Strategy]) -> list[Strategy]:
    """Platform strategies, plus the generic candidates it does not already have.

    Merged at LOAD rather than written into the files, so the vocabulary can be
    corrected in one place instead of eleven, and a platform file stays a record
    of what is known about that platform. Duplicates are dropped by selector
    identity: a platform that already names the same role and name keeps its own
    entry, with whatever evidence that entry has accumulated.

    The shared ones go on the end. That is only the starting order — once
    anything has been tried, ``Element.ordered`` sorts by evidence and the
    ``shared`` flag is just the tie-break.
    """
    from backend.siteknowledge.vocabulary import shared_candidates

    known = {strategy.id for strategy in strategies}
    return strategies + [
        candidate for candidate in shared_candidates(key) if candidate.id not in known
    ]


def load(platform: str, *, directory: Path | None = None) -> SiteKnowledge:
    """Load a platform's knowledge, seeding from the packaged defaults if new."""
    target = directory if directory is not None else _platform_dir(platform)
    if directory is None:
        _seed_from_defaults(platform, target)

    element_payload = _read_json(target / "elements.json")
    flow_payload = _read_json(target / "flows.json")
    quirk_payload = _read_json(target / "quirks.json")

    elements: dict[str, Element] = {}
    for key, raw in (element_payload.get("elements") or {}).items():
        strategies = [Strategy(**s) for s in raw.get("strategies", [])]
        elements[key] = Element(
            key=raw.get("key", key),
            strategies=_with_shared(raw.get("key", key), strategies),
            required=bool(raw.get("required", True)),
            last_working_strategy=raw.get("last_working_strategy"),
            last_verified_at=raw.get("last_verified_at"),
            success_count=int(raw.get("success_count", 0)),
            fail_count=int(raw.get("fail_count", 0)),
            notes=raw.get("notes", ""),
            proposals=[Strategy(**s) for s in raw.get("proposals", [])],
        )

    variants = {
        fingerprint: FlowVariant(**raw)
        for fingerprint, raw in (flow_payload.get("variants") or {}).items()
    }

    knowledge = SiteKnowledge(
        platform=platform,
        elements=elements,
        flow_variants=variants,
        quirks=list(quirk_payload.get("quirks") or []),
        directory=target,
    )
    log.debug(
        "site_knowledge_loaded",
        platform=platform,
        elements=len(elements),
        variants=len(variants),
        quirks=len(knowledge.quirks),
    )
    return knowledge
