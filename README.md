# JobSeekr

Local job discovery, scoring and auto-application. Single user, Windows, Adelaide AU.
Runs in the user's logged-in desktop session. No cloud, no auth, no deployment.

> **`ALLOW_LIVE_SUBMIT` is `false` and only you turn it on.**
> The entire pipeline — discovery, scoring, document generation, form filling,
> guardrails — runs and reports exactly what it *would* send. Nothing can be
> submitted until you set that one variable in `.env` on the machine itself. It is
> deliberately not a dashboard toggle.

---

## Quickstart

```bash
uv sync
cp .env.example .env          # then fill in your keys
uv run alembic upgrade head
uv run python -m backend.seed # profile row + 21 AU screening questions (blank)

uv run uvicorn backend.main:app --reload      # API on :8000
cd frontend && npm install && npm run dev     # dashboard on :5173
```

Then, in the dashboard:

1. **Profile** — fill it in. These are the *only* facts the system may state about
   you; a generated cover letter that asserts anything else fails the build.
2. **Answer bank** — answer the seeded questions. Blanks are highlighted because a
   blank is what parks an application mid-form.
3. **Campaigns** — create one with your search terms, locations and thresholds.

## Before you enable live submit

Three things are unverified until you run them on your own machine, because the
environment this was built in could not reach the job boards:

```bash
# 1. Confirm Seek's search endpoint and paste the .env lines it prints
uv run python -m backend.discovery.verify_seek --terms "python developer" --where "Adelaide SA"

# 2. Sign in manually — logins are never scripted
uv run python -m backend.apply.session login --platform linkedin
uv run python -m backend.apply.session login --platform seek

# 3. Record a real application flow so the adapters are pinned by tests
uv run python -m backend.apply.har record --platform linkedin --variant two_step
uv run python -m backend.apply.har list        # what is still missing

# 4. Watch a full pass without sending anything
uv run python -m backend.apply.run --dry-run
```

See `NOTES.md` for what each of those is verifying and why.

## Commands

| Command | What it does |
|---|---|
| `uv run uvicorn backend.main:app --reload` | API + dashboard backend |
| `uv run python -m backend.discovery.run` | Find jobs (HTTP only, no browser) |
| `uv run python -m backend.scoring.run` | Filter → embed → rubric-score |
| `uv run python -m backend.scoring.run --estimate 200` | Project the cost before spending |
| `uv run python -m backend.documents.build --job-id N` | Build + parse-gate the PDFs |
| `uv run python -m backend.apply.run --dry-run` | Full apply pass, submits nothing |
| `uv run python -m backend.apply.session login` | Sign in manually in a visible browser |
| `uv run python -m backend.apply.canary` | Check for platform markup drift |
| `uv run python -m backend.integrations.inbound` | Read replies, match, classify |
| `uv run python -m backend.integrations.telegram` | Run the bot |
| `uv run python -m backend.integrations.scheduler` | Run everything on schedule |
| `uv run pytest` | 342 tests |
| `alembic upgrade head` | Migrate |

## How it fits together

```
DISCOVERY   httpx, no browser     jobspy → LinkedIn/Indeed · seek_source → Seek
                ↓ normalize + dedupe → SQLite
SCORING     1. filters + embeddings (all jobs)   2. LLM rubric (top 40)
                ↓ score ≥ threshold
DOCUMENTS   Jinja2 → LaTeX → pdflatex → PARSE GATE
                ↓
APPLY       guardrails → answer bank → attach → read back → submit → audit
                ↓
INBOUND     match reply → classify → response status → analytics
```

## The rules this is built around

These are enforced in code and asserted by tests, not just documented:

- **Nothing is fabricated about you.** Generated narrative is validated against your
  profile; unsupported employers, dates, metrics or credentials fail the build and no
  document is produced.
- **Screening answers come only from the answer bank.** If a question cannot be
  resolved confidently the job is parked and you are asked — never guessed at.
  Ambiguity abstains.
- **No document attaches unless it passed the parse gate** — nine checks including
  ligature survival and two-column detection, because a resume that looks fine to you
  can be unreadable to an ATS.
- **The attachment filename is read back before submitting.** LinkedIn silently
  reuses stale uploads.
- **One application per job, ever.**
- **Every submit path goes through `guardrails.check_can_submit`** — one call site,
  no bypass parameter, sixteen checks.
- **Logins are never scripted.** No function here takes a password.
- **Outbound email is draft-only**, sent only to an address the advertiser published
  in their own ad, and only after you approve it. No harvesting, no follow-ups.

## Layout

```
backend/
  config.py models.py base.py db.py    core: settings, schema, protocols
  llm/client.py                        the one gateway for every model call
  discovery/  scoring/                 find and rank jobs
  documents/                           LaTeX pipeline + the parse gate
  apply/                               session, answers, guardrails, flow, adapters
  ats/                                 external ATS + form map cache
  integrations/                        Telegram, Gmail, outbound, scheduler
  api/                                 the dashboard's REST surface
frontend/                              Vite + React + TS + Tailwind
templates/                            resume, cover letter, email
data/                                  gitignored: db, documents, logs, browser profile
```

`data/browser_profile/` holds a live authenticated session. Nothing in the web UI
serves files from it, and the document endpoint refuses any path outside
`data/documents/`.
