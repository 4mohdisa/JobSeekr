# JobSeekr

Local job discovery, scoring and auto-application for a single user, on one Windows
machine, in Adelaide. It finds ads, scores them against your profile, writes a tailored
resume and cover letter, and — when you let it — fills the application in.

No cloud, no accounts, no deployment. It runs in your own logged-in desktop session
because the apply step drives a real, visible Chrome window against sessions you signed
into by hand.

```
DISCOVERY   httpx, no browser     jobspy → LinkedIn/Indeed · seek_client → Seek
                ↓ normalise + dedupe → SQLite
SCORING     1. filters + embeddings API (all jobs)   2. LLM rubric (top 40)
                ↓ score ≥ threshold
DOCUMENTS   Jinja2 → LaTeX → pdflatex → PARSE GATE
                ↓
APPLY       guardrails → answer bank → attach → read back → submit → audit
```

---

## ALLOW_LIVE_SUBMIT is false, and only you may change that

`ALLOW_LIVE_SUBMIT=false` is the master switch, and it ships off. With it off, every
apply run does the whole job — opens the form, fills it, attaches the documents, reads
the attachment back — and then **stops at the final button** and records a dry run.

Turning it on means the agent presses Submit on real applications to real employers,
as you. Only the human user flips it, by hand, in their own `.env`. No script, agent,
test, fixture or default may set it, and nothing in this repository does.

The kill switch is independent: create a file called `STOP` in `data/` and the apply
loop halts between jobs.

---

## Quickstart

Python 3.11+ and [uv](https://docs.astral.sh/uv/). MiKTeX (for `pdflatex`) and Chrome
are needed for documents and applying, not for the API.

```bash
uv sync                                    # install the pinned dependency set
cp .env.example .env                       # then edit .env: API keys, profile paths
alembic upgrade head                       # create the SQLite schema
uv run python -m backend.seed              # empty profile + AU screening questions
uv run uvicorn backend.main:app --reload   # http://127.0.0.1:8000
```

Check it came up:

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok","version":"0.1.0","allow_live_submit":false,
#  "database":"connected","time":"..."}
```

Interactive API docs are at `/docs`. `GET /api/meta/status` gives the headline
counters, including this month's LLM spend against the cap.

Seeding is idempotent — re-run it whenever you like. It loads the *questions* an
Australian application form asks, never the answers: those are facts about you, and
the agent asks over Telegram and stores what you tell it rather than guessing.

---

## Commands

| Command | What it does |
|---|---|
| `uv run uvicorn backend.main:app --reload` | API + dashboard backend |
| `uv run python -m backend.discovery.run` | Fetch new ads from every source |
| `uv run python -m backend.scoring.run` | Score the backlog (embeddings, then rubric) |
| `uv run python -m backend.documents.build --job-id N` | Build and parse-gate documents for one job |
| `uv run python -m backend.apply.run --dry-run` | Walk the apply queue without submitting |
| `uv run python -m backend.apply.session login` | Open a visible browser to sign in by hand |
| `uv run pytest` | Test suite |
| `alembic upgrade head` | Apply migrations |

Commands beyond the API and the test suite arrive with their own blocks; the API boots
without them.

---

## Layout

```
backend/
  main.py          FastAPI app: lifespan, CORS, /health, router mount point
  config.py        every runtime knob, and the ONLY place a model name may appear
  models.py        SQLModel tables + the status enums (11 tables)
  base.py          the Source / Applier / LLMClient protocols
  db.py            engine, sessions, SQLite WAL tuning
  llm/client.py    the one door every LLM call goes through, and the $/month cap
  seed.py          idempotent seed data
  discovery/ scoring/ documents/ apply/ integrations/ ats/ api/
alembic/           migrations — Alembic owns the schema, nothing else creates tables
data/              gitignored: app.db, logs, documents, screenshots, browser profile
tests/
frontend/          Vite + React + TS + Tailwind dashboard
```

House rules worth knowing before you edit: `structlog` only (never `print`), `pathlib`
for paths (never string concatenation — this targets Windows), UTC in the database and
local time in the UI, SQLModel for tables and Pydantic for API schemas, and every job
source, applier and LLM call goes through the protocol in `base.py` rather than being
copy-pasted. `Claude.md` is the full spec.

---

## Security

`data/browser_profile/` holds a live, authenticated LinkedIn session. **The web UI must
never serve files from it** — a static mount over `data/` would hand those cookies to
any page that asks. `backend/main.py` refuses to build the app if a route would expose
that directory, in either direction, and there is a test for it.

`.env` and `data/` are gitignored. Keep them that way.
