# Job Agent

Local job discovery, scoring and auto-application. Single user, Windows, Adelaide AU.
Runs in the user's logged-in desktop session. No cloud, no auth, no deployment.

---

## Hard rules

1. **Never fabricate facts about the user.** Employers, dates, titles, certifications, licences, visa status, salary, metrics — from the profile verbatim or not at all. Narrative phrasing is free; facts are locked.
2. **Screening answers come only from the answer bank.** Can't resolve it → abstain, park the job, ask via Telegram, save the answer, retry. Never guess. Ambiguous fuzzy match → abstain.
3. **No document attaches unless `parse_check_passed = true`.**
4. **Read back the attachment filename before submitting.** LinkedIn silently reuses stale uploads.
5. **One application per job, ever.** `UNIQUE(job_id)` on `applications`.
6. **Every submit path calls `guardrails.check_can_submit()`.** No bypass.
7. **`ALLOW_LIVE_SUBMIT` defaults false.** Only the user turns it on. Never set it yourself.
8. **Never script a login.** Sessions are established manually in a visible browser.
9. **Never fail silently.** Failed or aborted applications are logged loudly.

---

## Code rules

- **Never duplicate logic.** Used twice → extract to a shared helper, hook, or service.
- **Reuse before creating.** Check for an existing implementation to extend first.
- **No speculative abstraction.** Build the interface when there's a second caller, not before.
- **Protocols, not copy-paste.** Every job source implements `Source`; every applier implements `Applier`; every LLM call goes through `llm/client.py`. Adding a platform means one new file, not edits scattered across the codebase.
- Focused files. Delete unused code and dependencies as you go.
- SQLModel for DB, Pydantic for API schemas — separate.
- UTC in the DB, local time in the UI.
- `structlog` only, never `print()`.
- `pathlib` for paths, never string concatenation (Windows).

---

## Git

Branch per major area, not per fix. Multiple agents work in parallel.

```
main
├── feat/core          skeleton, models, config, migrations
├── feat/discovery     sources, dedupe, scoring
├── feat/documents     templates, LaTeX, parse gate
├── feat/apply         session, guardrails, answers, adapters
├── feat/frontend      dashboard
├── feat/integrations  telegram, gmail, outbound
└── feat/ats           external portals, form maps
```

`feat/core` merges first — everything branches from it. Keep to your own directories to avoid conflicts. Commit working increments, don't batch a whole phase into one commit.

---

## Deliberate decisions — do not "improve" these

**pdflatex via MiKTeX. Not tectonic, not xelatex, not lualatex.** XeTeX output breaks ATS text extraction and tectonic has no pdflatex mode.

**A4, single column.** Australian standard; two-column parses catastrophically in ATS.

**100% API. No local models** — no Ollama, no sentence-transformers, no PyTorch. Embeddings via API.

**Headful Playwright, `channel="chrome"`.** Headless is a detection signal.

**Discovery is HTTP only.** Only the apply layer touches an authenticated session.

---

## Architecture

```
DISCOVERY   httpx, no browser     jobspy → LinkedIn/Indeed · seek_client → Seek
                ↓ normalize + dedupe → SQLite
SCORING     1. filters + embeddings API (all jobs)   2. LLM rubric (top 40)
                ↓ score ≥ threshold
DOCUMENTS   Jinja2 → LaTeX → pdflatex → PARSE GATE
                ↓
APPLY       guardrails → answer bank → attach → read back → submit → audit
```

---

## Concepts

**Profile** — one versioned row, all raw material.
**Campaigns** — many; each has own search terms, rubric, templates, thresholds, caps, target goal. Profile is shared; campaigns decide what surfaces and how it's written.
**Answer bank** — verified screening answers, self-populating via Telegram. Global or campaign-scoped.
**Form maps** — cached field mappings keyed by form-structure fingerprint. Platform tier (shared) + company tier (overrides). Store semantic identity, not CSS selectors. Record *where* fields are, never *what* values go in them.
**Trust graduation** — new form map drafts for approval; 3 clean successes → auto.

---

## Stack

Python 3.11+ · uv · FastAPI · SQLModel + Alembic · SQLite (WAL) · APScheduler
Playwright · MiKTeX/pdflatex · pypdf + pdfplumber · LiteLLM · Vite + React + TS + Tailwind

## Commands

```
uv run uvicorn backend.main:app --reload
uv run python -m backend.discovery.run
uv run python -m backend.scoring.run
uv run python -m backend.documents.build --job-id N
uv run python -m backend.apply.run --dry-run
uv run python -m backend.apply.session login
uv run pytest
alembic upgrade head
```

## Windows

Runs in the logged-in session via Task Scheduler at login. Can't be a service — headful Chrome needs a desktop. Machine stays awake and logged in. `data/browser_profile/` holds a live LinkedIn session; the web UI must never serve files from it.