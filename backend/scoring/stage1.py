"""Stage 1: rank everything cheaply, so stage 2 only sees the top of the pile.

The cost target for the whole pipeline is 200 jobs discovered and scored for
under $0.15. Discovery is free (plain HTTP) and stage 2 is capped at 40 jobs,
so stage 1 is the only part whose cost scales with *everything* found. Three
decisions keep it small, and all three matter more than the model choice:

* **Truncate.** Only the first ``scoring_embedding_char_budget`` characters of
  an ad are embedded. The tail of a job ad is EEO boilerplate and "about us" —
  paying to embed it buys nothing.
* **Batch.** One request per batch of jobs, not one per job. Per-request
  overhead dominates otherwise.
* **Cache.** Embeddings are keyed by a hash of the exact text embedded, so a
  re-run, a re-score under a new rubric, or a second campaign touching the same
  ad all cost nothing.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from backend.config import settings
from backend.llm.client import llm
from backend.logging_setup import get_logger
from backend.models import Job

log = get_logger(__name__)

__all__ = [
    "EmbeddingCache",
    "campaign_profile_summary",
    "cosine",
    "embedding_text",
    "rank_jobs",
]


def embedding_text(job: Job) -> str:
    """The text actually embedded for a job.

    Title and company lead because they carry the most signal per character;
    the description is truncated behind them.
    """
    head = f"{job.title or ''} at {job.company or ''} ({job.location or 'location unstated'})"
    body = (job.description or "")[: settings.scoring_embedding_char_budget]
    return f"{head}\n{body}".strip()


def campaign_profile_summary(profile: Any, campaign: Any) -> str:
    """A compact statement of what this campaign is looking for.

    Built once per (campaign, profile version) and embedded once — not per
    job. Uses the profile's own words; nothing here is generated.
    """
    identity = getattr(profile, "identity", None) or {}
    skills = getattr(profile, "skills", None) or []
    experience = getattr(profile, "experience", None) or []
    preferences = getattr(profile, "preferences", None) or {}

    def _titles(entries: Sequence[Any], limit: int = 6) -> list[str]:
        out: list[str] = []
        for entry in entries[:limit]:
            if isinstance(entry, dict):
                title = entry.get("title") or entry.get("role") or entry.get("position")
                company = entry.get("company") or entry.get("employer")
                if title:
                    out.append(f"{title}{f' at {company}' if company else ''}")
            elif entry:
                out.append(str(entry))
        return out

    def _skills(entries: Sequence[Any], limit: int = 40) -> list[str]:
        out: list[str] = []
        for entry in entries[:limit]:
            if isinstance(entry, dict):
                name = entry.get("name") or entry.get("skill")
                if name:
                    out.append(str(name))
            elif entry:
                out.append(str(entry))
        return out

    parts = [
        f"Target roles: {', '.join(str(t) for t in (campaign.search_terms or []))}",
        f"Locations: {', '.join(str(loc) for loc in (campaign.locations or []))}",
    ]
    if identity.get("headline"):
        parts.append(f"Headline: {identity['headline']}")
    if experience:
        parts.append(f"Experience: {'; '.join(_titles(experience))}")
    if skills:
        parts.append(f"Skills: {', '.join(_skills(skills))}")
    if preferences:
        wanted = preferences.get("wants") or preferences.get("summary")
        if wanted:
            parts.append(f"Preferences: {wanted}")
    return "\n".join(parts)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity. Returns 0.0 for a zero vector rather than dividing by it."""
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class EmbeddingCache:
    """Disk-backed embedding cache keyed by a hash of the embedded text.

    Keyed by content rather than by job id on purpose: an ad whose description
    is later enriched gets a new key and is re-embedded, while the same ad seen
    from two campaigns is embedded once.

    Plain JSON lines, so a user can inspect or delete it without tooling.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (settings.data_dir / "embeddings.jsonl")
        self._cache: dict[str, list[float]] = {}
        self._loaded = False

    @staticmethod
    def key(text: str, model: str) -> str:
        digest = hashlib.sha256(f"{model}\x1f{text}".encode()).hexdigest()
        return digest[:32]

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        self._cache[record["k"]] = record["v"]
                    except (json.JSONDecodeError, KeyError, TypeError):
                        continue  # a torn line costs one re-embed, not a crash
        except OSError as exc:
            log.warning("embedding_cache_unreadable", path=str(self.path), error=str(exc))

    def get(self, key: str) -> list[float] | None:
        self._load()
        return self._cache.get(key)

    def put(self, key: str, vector: list[float]) -> None:
        self._load()
        self._cache[key] = vector
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"k": key, "v": vector}) + "\n")
        except OSError as exc:
            log.warning("embedding_cache_unwritable", error=str(exc))


def _embed_with_cache(
    texts: list[str], *, cache: EmbeddingCache, purpose: str
) -> list[list[float]]:
    """Embed only what is not cached, in batches, preserving input order."""
    model = settings.llm_model_embedding
    keys = [EmbeddingCache.key(text, model) for text in texts]
    vectors: list[list[float] | None] = [cache.get(key) for key in keys]

    pending = [i for i, vector in enumerate(vectors) if vector is None]
    hits = len(texts) - len(pending)

    batch_size = max(1, settings.scoring_embedding_batch_size)
    for start in range(0, len(pending), batch_size):
        chunk = pending[start : start + batch_size]
        fresh = llm.embed([texts[i] for i in chunk], purpose=purpose)
        for index, vector in zip(chunk, fresh, strict=False):
            vectors[index] = vector
            cache.put(keys[index], vector)

    log.info(
        "embeddings_resolved",
        total=len(texts),
        cache_hits=hits,
        embedded=len(pending),
        batches=math.ceil(len(pending) / batch_size) if pending else 0,
    )
    return [vector or [] for vector in vectors]


def rank_jobs(
    jobs: Iterable[Job],
    *,
    summary: str,
    top_n: int | None = None,
    cache: EmbeddingCache | None = None,
) -> list[tuple[Job, float]]:
    """Rank jobs against the campaign summary, best first.

    Returns every job with its similarity — the caller stores stage1 for all of
    them so the dashboard can show the funnel — but only the first ``top_n``
    are worth passing to stage 2.
    """
    jobs = list(jobs)
    if not jobs:
        return []

    cache = cache or EmbeddingCache()
    top_n = top_n or settings.scoring_stage1_top_n

    texts = [embedding_text(job) for job in jobs]
    query_vector = _embed_with_cache([summary], cache=cache, purpose="stage1_summary")[0]
    job_vectors = _embed_with_cache(texts, cache=cache, purpose="stage1_jobs")

    ranked = [
        (job, cosine(query_vector, vector))
        for job, vector in zip(jobs, job_vectors, strict=False)
    ]
    ranked.sort(key=lambda pair: pair[1], reverse=True)
    return ranked
