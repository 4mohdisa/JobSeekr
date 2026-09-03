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
    "STRATEGY_PRIORITY",
    "Element",
    "ElementNotFound",
    "FlowVariant",
    "SiteKnowledge",
    "Strategy",
    "fingerprint_steps",
    "load",
    "on_all_strategies_failed",
    "on_strategy_drift",
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

    def ordered(self) -> list[Strategy]:
        """Strategies in the order to try them.

        ``last_working_strategy`` first — it is the freshest evidence about
        what the site looks like right now — then everything else by durability.
        A strategy that worked yesterday beats one that was durable in theory.
        """
        by_priority = sorted(self.strategies, key=lambda s: s.priority)
        if not self.last_working_strategy:
            return by_priority

        promoted = [s for s in by_priority if s.id == self.last_working_strategy]
        return promoted + [s for s in by_priority if s.id != self.last_working_strategy]


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
        for strategy in element.ordered():
            selector = strategy.selector
            tried.append(selector)
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
                self._record_success(element, strategy)
                return locator

        self._record_failure(element, tried)
        if element.required:
            if on_all_strategies_failed is not None:
                try:
                    on_all_strategies_failed(self.platform, key, tried)
                except Exception as exc:  # noqa: BLE001 - alerting must not mask the fault
                    log.warning("strategy_alert_hook_failed", error=str(exc)[:150])
            raise ElementNotFound(self.platform, key, tried)

        log.debug("optional_element_absent", platform=self.platform, key=key)
        return None

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

    def save(self, *, force: bool = False) -> None:
        """Write promotions and counters back. A no-op when nothing changed."""
        if not (self.dirty or force):
            return
        if self.directory is None:  # pragma: no cover - only in synthetic tests
            return

        self.directory.mkdir(parents=True, exist_ok=True)
        _write_json(
            self.directory / "elements.json",
            {
                "platform": self.platform,
                "elements": {
                    key: asdict(element) for key, element in sorted(self.elements.items())
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
        log.debug("site_knowledge_saved", platform=self.platform)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write atomically. A half-written knowledge file breaks every later run."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
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
            strategies=strategies,
            required=bool(raw.get("required", True)),
            last_working_strategy=raw.get("last_working_strategy"),
            last_verified_at=raw.get("last_verified_at"),
            success_count=int(raw.get("success_count", 0)),
            fail_count=int(raw.get("fail_count", 0)),
            notes=raw.get("notes", ""),
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

