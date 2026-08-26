"""Learn a form's shape once, reuse it everywhere that shape appears.

FINGERPRINTING
    The key is a hash of the *sorted set of field identities* — name attribute,
    label text, input type. Deliberately not the URL and not the company: two
    employers on the same ATS with the same application form produce the same
    fingerprint, so the map learned at the first is reused at the second for
    free. Sorted, so field order changing between renders does not invalidate it.

SEMANTIC IDENTITY, NOT CSS SELECTORS
    A map records ``{"label": "Do you have full working rights?", "name":
    "q_12", "type": "radio"}`` — never ``#app > div:nth-child(3) > input``.
    Class names and DOM structure change every time an ATS ships a release;
    the label a human reads is far more stable. Resolution tries the semantic
    identity first and falls back to a selector only when it must.

WHERE, NEVER WHAT
    A map records where the fields are. It never records the values that go in
    them. Values live in the profile and the answer bank, which is what keeps
    a cached map from silently answering a question with last year's answer —
    and what keeps a shared platform-tier map from containing personal data.

TWO TIERS
    ``data/formmaps/platform/*.json`` is shared across every employer on that
    ATS. ``data/formmaps/company/{fingerprint}.json`` overrides it for one
    employer's quirks. Company wins field by field, so a single odd question
    does not require re-learning the whole form.

FILES ON DISK, INDEXED IN SQLITE
    JSON so the user can open a bad mapping in an editor and fix it. The index
    exists for querying trust and success counts, not as the source of truth.

TRUST GRADUATION
    A newly learned map is a draft: it goes to Telegram for approval. After
    three clean successes it is trusted and used without asking.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from backend.config import settings
from backend.logging_setup import get_logger
from backend.models import FormMap, FormMapTier

log = get_logger(__name__)

__all__ = [
    "TRUST_THRESHOLD",
    "FieldMapping",
    "FormMapData",
    "fingerprint_fields",
    "load_map",
    "merge_maps",
    "record_outcome",
    "save_map",
]


TRUST_THRESHOLD = 3
"""Clean successes before a map is used without asking.

Three because one success can be luck and two can be the same lucky form seen
twice; three across different jobs means the mapping generalised.
"""


@dataclass
class FieldMapping:
    """Where one field is and what it is for. Never what to put in it."""

    identifier: str
    label: str
    kind: str = "text"
    #: What this field wants: a profile path ("profile.email"), an answer-bank
    #: question, or "unknown" when the LLM could not tell.
    source: str = "unknown"
    #: The exact answer-bank question to resolve, when source is "answer_bank".
    question: str | None = None
    required: bool = False
    step: int = 0
    #: Last-resort locator. Tried only after semantic resolution fails.
    selector: str | None = None

    @property
    def resolved(self) -> bool:
        return self.source not in {"unknown", ""}


@dataclass
class FormMapData:
    fingerprint: str
    tier: str
    platform: str | None
    fields: list[FieldMapping] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    notes: str = ""

    @property
    def unresolved(self) -> list[FieldMapping]:
        return [f for f in self.fields if not f.resolved]

    @property
    def complete(self) -> bool:
        return bool(self.fields) and not self.unresolved

    def by_identifier(self) -> dict[str, FieldMapping]:
        return {f.identifier: f for f in self.fields}


# --------------------------------------------------------------------------
# Fingerprinting
# --------------------------------------------------------------------------


def _normalise_label(text: str | None) -> str:
    import re

    if not text:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip().casefold().rstrip("?:*")


def fingerprint_fields(fields: Iterable[Any]) -> str:
    """Stable hash of a form's shape.

    Built from the SORTED SET of (name, label, type) triples, so the same form
    fingerprints identically regardless of the order the DOM happened to yield
    the fields in, and regardless of which company is serving it.
    """
    identities = sorted(
        {
            "|".join(
                (
                    str(getattr(f, "identifier", "") or ""),
                    _normalise_label(getattr(f, "label", "")),
                    str(getattr(f, "kind", "text") or "text"),
                )
            )
            for f in fields
        }
    )
    digest = hashlib.sha256("\n".join(identities).encode("utf-8")).hexdigest()
    return digest[:32]


# --------------------------------------------------------------------------
# Disk
# --------------------------------------------------------------------------


def _path_for(fingerprint: str, tier: str, platform: str | None) -> Path:
    if tier == FormMapTier.PLATFORM.value:
        directory = settings.formmaps_platform_dir
        name = f"{platform or 'unknown'}-{fingerprint}.json"
    else:
        directory = settings.formmaps_company_dir
        name = f"{fingerprint}.json"
    return directory / name


def _read(path: Path) -> FormMapData | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # A hand-edited map with a typo should not take the run down; it should
        # be loudly ignored so the form is re-learned.
        log.error("form_map_unreadable", path=str(path), error=str(exc)[:200])
        return None

    return FormMapData(
        fingerprint=payload.get("fingerprint", ""),
        tier=payload.get("tier", FormMapTier.COMPANY.value),
        platform=payload.get("platform"),
        fields=[FieldMapping(**f) for f in payload.get("fields", [])],
        created_at=payload.get("created_at", ""),
        updated_at=payload.get("updated_at", ""),
        notes=payload.get("notes", ""),
    )


def save_map(
    session: Session | None,
    data: FormMapData,
    *,
    trusted: bool = False,
) -> Path:
    """Write the map to disk and index it. Refuses to persist any value.

    The value check is not defensive coding for its own sake: a map that
    accumulated answers would turn a shared platform-tier file into a file
    containing the user's personal data, and would let a stale answer be
    replayed months later without going through the answer bank.
    """
    for mapping in data.fields:
        for attribute in ("value", "answer", "answer_value"):
            if hasattr(mapping, attribute):  # pragma: no cover - structural guard
                raise ValueError(
                    "form maps record WHERE fields are, never WHAT goes in them"
                )

    data.updated_at = datetime.now(UTC).isoformat()
    path = _path_for(data.fingerprint, data.tier, data.platform)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(data), indent=2), encoding="utf-8")

    if session is not None:
        row = session.exec(
            select(FormMap).where(FormMap.fingerprint == data.fingerprint)
        ).first()
        if row is None:
            row = FormMap(
                fingerprint=data.fingerprint,
                tier=FormMapTier(data.tier),
                platform=data.platform,
                path=str(path),
                trusted=trusted,
            )
        else:
            row.path = str(path)
            row.tier = FormMapTier(data.tier)
            row.platform = data.platform
        session.add(row)

    log.info(
        "form_map_saved",
        fingerprint=data.fingerprint,
        tier=data.tier,
        platform=data.platform,
        fields=len(data.fields),
        unresolved=len(data.unresolved),
        path=str(path),
    )
    return path


def merge_maps(base: FormMapData | None, override: FormMapData | None) -> FormMapData | None:
    """Company tier over platform tier, field by field.

    Field-by-field rather than whole-file so one employer's odd question does
    not require re-learning every other field on the form.
    """
    if base is None:
        return override
    if override is None:
        return base

    merged = {f.identifier: f for f in base.fields}
    for mapping in override.fields:
        # An unresolved override must not blank out a resolved base field.
        if mapping.resolved or mapping.identifier not in merged:
            merged[mapping.identifier] = mapping

    return FormMapData(
        fingerprint=override.fingerprint or base.fingerprint,
        tier=FormMapTier.COMPANY.value,
        platform=override.platform or base.platform,
        fields=list(merged.values()),
        created_at=base.created_at,
        notes="merged company overrides over the platform map",
    )


def load_map(
    session: Session | None,
    fingerprint: str,
    *,
    platform: str | None = None,
) -> tuple[FormMapData | None, bool]:
    """Load the effective map for a fingerprint. Returns (map, trusted)."""
    platform_map = _read(_path_for(fingerprint, FormMapTier.PLATFORM.value, platform))
    company_map = _read(_path_for(fingerprint, FormMapTier.COMPANY.value, platform))
    merged = merge_maps(platform_map, company_map)

    trusted = False
    if session is not None:
        row = session.exec(select(FormMap).where(FormMap.fingerprint == fingerprint)).first()
        trusted = bool(row and row.trusted)

    if merged is not None:
        log.debug(
            "form_map_loaded",
            fingerprint=fingerprint,
            trusted=trusted,
            fields=len(merged.fields),
            unresolved=len(merged.unresolved),
        )
    return merged, trusted


# --------------------------------------------------------------------------
# Trust graduation
# --------------------------------------------------------------------------


def record_outcome(session: Session, fingerprint: str, *, success: bool) -> bool:
    """Record how a map performed. Returns True when it becomes trusted.

    A failure resets the streak rather than decrementing it: three successes
    must be *consecutive* to mean the mapping generalised, and a map that
    alternates is not one to trust.
    """
    row = session.exec(select(FormMap).where(FormMap.fingerprint == fingerprint)).first()
    if row is None:
        log.warning("form_map_outcome_unknown_fingerprint", fingerprint=fingerprint)
        return False

    if success:
        row.success_count += 1
        row.last_verified_at = datetime.now(UTC)
    else:
        row.fail_count += 1
        row.success_count = 0
        if row.trusted:
            # A trusted map that failed has stopped matching reality.
            row.trusted = False
            log.warning("form_map_trust_revoked", fingerprint=fingerprint)

    graduated = False
    if success and not row.trusted and row.success_count >= TRUST_THRESHOLD:
        row.trusted = True
        graduated = True
        log.info(
            "form_map_trusted",
            fingerprint=fingerprint,
            platform=row.platform,
            successes=row.success_count,
        )

    session.add(row)
    return graduated


def relearn_targets(
    existing: FormMapData | None, fields: Sequence[Any]
) -> list[Any]:
    """Which fields still need the LLM.

    On a partial failure only the unknown fields are re-learned and merged back.
    Re-learning a whole form because one field was added costs a full LLM call
    for information already known.
    """
    if existing is None:
        return list(fields)

    known = {
        mapping.identifier
        for mapping in existing.fields
        if mapping.resolved
    }
    return [f for f in fields if getattr(f, "identifier", "") not in known]
