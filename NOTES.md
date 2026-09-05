# Build notes

Decisions taken where the spec left a choice open, plus anything a later block
needs to know. Factual record, not a changelog.

## Block A — Core

### Config (`backend/config.py`)

- **`browser_profile_dir` is a field, not a property.** The spec lists it both as
  a Browser setting defaulting to `data_dir / "browser_profile"` and as a derived
  path. A field cannot default to the value of another field, so it is declared
  `Optional[Path] = None` and a `model_validator(mode="after")` fills it with
  `data_dir / "browser_profile"` when unset. Net effect: it follows a relocated
  `DATA_DIR`, and `BROWSER_PROFILE_DIR` in `.env` still overrides it. Callers can
  treat it as always populated; the annotation stays `Optional` only because
  pydantic validates before the validator runs.
- **Blank env values mean "unset".** A `_blank_is_unset` before-validator maps
  `KEY=` (empty or whitespace) to `None`. Without it, `APPLY_WARMUP_START_DATE=`
  raises a validation error and, worse, an empty `GMAIL_OAUTH_TOKEN_FILE=` would
  become `Path(".")` — a credential path silently pointing at the working
  directory. A blank *required* key now fails loudly, which is the correct
  outcome for a typo'd `.env`.
- **`stop_file` is not created by `ensure_directories()`.** It is a file whose
  *existence* is the kill switch — creating it would halt the apply loop on first
  run. Its parent is `data_dir`, which is created.
- **`managed_directories`** exists so the directory list has one definition;
  `ensure_directories()` walks it. Anything that needs to enumerate the data tree
  should use it rather than re-listing paths.
- **Relative default paths.** `data_dir="data"` and `sqlite:///data/app.db`
  resolve against the working directory. Intentional: the app is launched from
  the repo root by Task Scheduler, and an absolute default would be wrong on the
  Windows target. Set `DATA_DIR` to an absolute path if that ever changes.
- **Model defaults.** All four generative slots default to `anthropic/claude-opus-5`
  and embeddings to `openai/text-embedding-3-small` (LiteLLM identifiers). Cost is
  the user's call, not a default — `LLM_MODEL_CLASSIFY` is the one to downgrade
  first (e.g. `anthropic/claude-haiku-4-5`) if the $25/month cap bites, since
  classification is the high-volume, low-judgement path. Model names appear here
  and in `.env.example` only.
- **`llm_warn_fraction` is a fraction, not a percentage** (0.8 = warn at 80% of cap).
- **`apply_warmup_start_date` defaults to `None`,** meaning "no ramp configured".
  Guardrails decide what that implies; config does not encode the policy.

### Logging (`backend/logging_setup.py`)

- **Console renderer is chosen by `app_env == "local"`,** as specified. Colours are
  left to structlog's own terminal detection (`ConsoleRenderer` defaults) rather
  than forced, because the Windows target runs both in a terminal and under Task
  Scheduler with no TTY.
- **`get_logger()` calls `configure_logging()` on first use.** Module-level
  `log = get_logger(__name__)` is the normal pattern; lazy configuration means such
  a module can never silently drop events because `main()` had not run yet.
  `configure_logging()` itself is guarded by a module flag and takes `force=True`
  to rebuild (used by tests that need to re-point the log file).
- **Root handlers are cleared before install** so a second `configure_logging(force=True)`
  cannot double-log.
- **File handler rotates** at 10 MB x 5 backups. Unattended overnight runs write a
  lot; an unbounded log file on a single-user Windows box is a real risk.
  `format_exc_info` is in the file chain only — the console renderer formats
  exceptions itself.

### DB (`backend/db.py`)

- **PRAGMA hook is registered on the `Engine` class, not the instance,** so any
  engine created later (tests, Alembic) gets the same SQLite tuning. It no-ops for
  non-`sqlite3` connections so a non-SQLite URL is unaffected.
- **`init_db()` does not create tables.** Alembic owns schema. It calls
  `settings.ensure_directories()` and then ensures the SQLite file's parent exists.
- **`sqlite_path()`** parses `database_url` with `sqlalchemy.make_url` and returns
  `None` for `:memory:` / non-SQLite, so callers (backups, integrity checks) do not
  re-parse the URL string.
- **`get_session()` does not commit**; the route does. `session_scope()` commits on
  success, rolls back and logs on error — never fail silently (Claude.md).

### Dependencies

No missing dependencies found for this block.

### Protocols (`backend/base.py`)

- **`page` / `job` / `documents` / `campaign` on `Applier.apply` are typed `Any`.**
  `backend/base.py` is imported by discovery to get `Source`, and discovery must stay
  HTTP-only and browser-free (Claude.md), so this module must not import Playwright.
  Importing `backend.models` for `Job`/`Campaign` would also create a cycle with the
  layers that define them. Each parameter names its real type in the docstring.
- **`ApplyOutcome` is a `str`-subclassing enum**, not free-form strings: the dashboard,
  guardrails and every applier have to agree on spelling. Values: `submitted`, `dry_run`,
  `blocked`, `abstained`, `failed`. `submitted` is only reachable with `ALLOW_LIVE_SUBMIT` on.
- **`RawJob` is pre-normalisation and lossless.** `raw` keeps the source's untouched
  payload so a board's quirks can be re-parsed later without re-fetching.
- **`LLMClient.complete*` require `model` as a keyword with no default.** A default would
  be somewhere for a hardcoded model id to hide; requiring it forces the caller to pass a
  `settings.llm_model_*` value.

### LLM gateway (`backend/llm/client.py`)

- **Nothing else may `import litellm`.** One door is what makes the cap enforceable.
  Use `from backend.llm.client import llm` (or the module-level `complete`,
  `complete_json`, `embed` shortcuts).
- **`LITELLM_LOCAL_MODEL_COST_MAP` is set (via `os.environ.setdefault`) before
  `import litellm`.** Importing litellm otherwise downloads its pricing table from
  GitHub at import time — every CLI entry point would pay that round-trip on startup
  and stall on an offline machine. Verified: with sockets blocked, importing the module
  now makes no network call at all, and `completion_cost` still prices
  `claude-opus-5` correctly from the bundled map (1000 in / 500 out = $0.0175).
  Export `LITELLM_LOCAL_MODEL_COST_MAP=false` to opt back into live pricing.
- **API keys are passed per call from settings, not from `os.environ`.** LiteLLM only
  looks at the environment, but the keys live in `.env` behind pydantic-settings.
  `_PROVIDER_KEY_FIELDS` maps the `provider/` prefix to the settings field.
- **One `llm_spend` row per provider attempt, not per logical call.** A retried call may
  be billed more than once, and a run of failures is exactly what the user needs to see
  in the spend table. Failed attempts are recorded with `ok=False` and the exception text.
- **The budget fails closed.** `_check_budget` raises `LLMBudgetExceeded` when
  `spent >= cap`, which also gives a cap of `0` the useful reading ("not spending
  anything this month" halts LLM work outright). Errors reading the spend table are
  deliberately *not* swallowed — a budget that fails open is not a budget. Discovery
  makes no LLM calls, so it keeps running while scoring and writing are stopped.
- **`_record_spend` never raises.** The call has already been paid for, so losing the
  bookkeeping must not also lose the answer; it logs at error level instead.
  Note `llm_spend.job_id` is a real FK to `job.id` with `PRAGMA foreign_keys=ON`, so
  callers must pass a persisted job id or `None`.
- **The budget warning and the "model did not come from settings" warning fire once per
  process**, not per call — a scoring run makes dozens of calls and repeated identical
  warnings bury the real log.
- **`complete_json`** uses LiteLLM's `json_schema` response format where
  `supports_response_schema` says the model has it, and falls back to
  `{"type": "json_object"}` plus the schema in the prompt otherwise (which also satisfies
  OpenAI's "the word json must appear" rule). Parsing strips ``` fences, requires a JSON
  object, and checks `schema["required"]`; one corrective round-trip is made before
  raising. Deliberately not full JSON-Schema validation — the required-key check is what
  stops a `KeyError` three layers downstream.
- **Retries** cover only transient failures (rate limit, timeout, connection, 5xx);
  auth/bad-request/context-length fail the same way twice and retrying them just spends
  money. `stop_after_attempt(1 + llm_max_retries)`, exponential backoff with jitter.
  `LLMBudgetExceeded` is raised before the retry loop and is not a transient error, so an
  exhausted budget can never be retried into more spending.

### Migrations (`alembic.ini`, `alembic/`)

- **Initial revision id: `2c01ae3b59ac`** (`initial_schema`, `down_revision = None`).
  Covers all 11 tables, 7 indexes and every named unique constraint. `alembic check`
  reports "No new upgrade operations detected" against `backend/models.py`, so the
  migration and the models are known to agree — run it after any model change.
- **No `sqlalchemy.url` in `alembic.ini`.** `env.py` reads
  `backend.config.settings.database_url`, so `DATABASE_URL` in `.env` is the single
  source of truth for migrations as well as the app. The key is deleted rather than
  left at the generated `driver://user:pass@...` placeholder, which would otherwise
  be a second, silently-wrong answer to "where is the database".
- **`render_as_batch=True` in both offline and online mode.** SQLite cannot meaningfully
  `ALTER TABLE`, so batch mode (create-copy-drop-rename) is what makes a future column
  drop or type change an ordinary autogenerate. This is also why every constraint in
  `models.py` is named — batch mode cannot rebuild an unnamed constraint.
- **Autogenerate fix 1 — `import sqlmodel` in `script.py.mako`.** SQLModel maps `str` to
  `sqlmodel.sql.sqltypes.AutoString` and autogenerate renders columns by that
  fully-qualified name, so the stock template produces a revision that dies at import
  with `NameError`. The template now emits the import for every revision.
- **Autogenerate fix 2 — ruff post-write hooks.** Alembic's template still emits
  `typing.Union` / `typing.Sequence`, which this project's ruff settings reject
  (UP007/UP035, plus an unsorted import block). Migrations are checked in and have to
  pass the same `ruff check` as hand-written code, so `alembic.ini` now runs
  `ruff check --fix` then `ruff format` on every generated revision. Side benefit: on a
  revision that turns out not to reference sqlmodel, the hook removes the unused import
  again. Generated output is lint- and format-clean with no hand editing.
- **Foreign key enforcement is turned OFF for migration connections only**, via a
  `connect` listener registered on the engine inside `env.py`. Batch mode rebuilds a
  table as create-temp/copy/drop/rename; with FKs enforced SQLite rewrites *other*
  tables' FK clauses to follow that rename (silently repointing them at the temp table)
  and the drop of a referenced parent fails once child rows exist. App connections are
  unaffected — verified `PRAGMA foreign_keys = 1` on a normal `backend.db` connection.
- **The pragma is a `connect` hook, not a statement run against the migration
  connection — this one bit, and cost a debugging round.** Executing anything on the
  migration connection first autobegins a SQLAlchemy transaction; Alembic responds to an
  already-open transaction by making its own per-migration transaction a no-op. pysqlite
  only implicitly BEGINs before DML, so the DDL then ran in autocommit and persisted
  while the `alembic_version` INSERT was rolled back at close. Net result was a fully
  built 11-table schema that `alembic current` still reported as `base` — i.e. the next
  `upgrade head` would have tried to recreate every table. If you ever add setup SQL to
  `env.py`, put it in a `connect` listener for the same reason.
- **`env.py` reuses `backend.db.engine`** rather than building its own, so migrations
  inherit WAL and `busy_timeout=5000`; a migration against a live WAL database otherwise
  fails with "database is locked" instead of waiting. It calls `init_db()` first so a
  fresh clone with no `data/` directory can run `alembic upgrade head` as its first
  command, and `engine.dispose()` before connecting so the FK listener is guaranteed to
  apply to a fresh DBAPI connection.
- Alembic's `fileConfig()` takes over Python logging for the duration of a migration
  run, replacing the structlog handlers installed when `backend.db` was imported. That is
  deliberate — it is what puts the `Running upgrade ...` lines on the console — but it
  means backend log calls made *during* a migration do not reach `data/logs/`.

### Seed data (`backend/seed.py`)

- **Seeded answers are blank on purpose, and this is load-bearing.** `seed_answer_bank()`
  pre-loads 21 Australian screening *questions* with `answer_value=""`, `verified_at=None`,
  `choices=NULL`, `campaign_id=None` (global). Answers are facts about the user — work
  rights, licences, salary, vaccination status — and Claude.md hard rules 1 and 2 forbid
  defaulting them; blank is what makes the applier abstain, park the job and ask via
  Telegram. What seeding actually buys is the *matching metadata*: phrasing, match type,
  answer type, and a `notes` string telling the user the exact format to type
  (`"true"`/`"false"`, ISO date, digits-only annual figure, and so on).
- **11 fuzzy / 10 regex.** Fuzzy is the default; regex is used only where it is clearly
  better — an abbreviation fuzz would miss (WWCC, Blue Card, WWVP, ABN), a spelling that
  varies (licence/license, `driver's`/`drivers`/`drivers'`/`driving`, straight and
  typographic apostrophes), or a pair of questions fuzz would confuse with each other.
  Two such pairs are disambiguated with lookaheads, because a wrong-but-confident match
  is worse than no match: annual salary excludes `hour` so it can never answer an hourly
  question with an annual figure, and visa/citizenship status excludes `sponsor` so it
  cannot answer a sponsorship question, whose correct answer is the opposite polarity.
  The sponsorship row's `notes` spells that polarity out.
- **Idempotency key is `question_pattern` within the global scope**, check-then-insert,
  and no existing row is ever updated — a verified answer outranks anything this file has
  to say. **Consequence for whoever edits a seeded pattern later: you get a second row,
  not an updated one.** Migrate the old row deliberately if you change a pattern.
  The in-loop `existing.add(...)` guards the other direction — a pattern accidentally
  duplicated inside `ANSWER_BANK_SEEDS` would otherwise insert twice on the first run.
- **`seed_default_profile()` checks for *any* profile row, not for version 1.** Once the
  user has edited their profile the current version is 2 or 20, and re-inserting a blank
  version 1 would both collide with `uq_profile_version` and hand the scorer an empty
  profile to score against.
- `seed_all()` is safe to call on every startup and after every migration.

### API shell (`backend/main.py`, `README.md`)

- `create_app()` factory plus module-level `app = create_app()`, so
  `uv run uvicorn backend.main:app --reload` works and tests can still build an isolated
  instance. Startup is an `asynccontextmanager` lifespan, not the deprecated `on_event`.
- **`_register_routers()` distinguishes "not built yet" from "broken".** Feature routers
  live in their own packages (`backend.discovery.routes`, `backend.scoring.routes`,
  `backend.documents.routes`, `backend.apply.routes`, `backend.api.dashboard`) so parallel
  branches never edit one shared file. A `ModuleNotFoundError` whose `.name` *is* the
  module we asked for means that block has not landed — debug line, keep booting. Any
  other `ModuleNotFoundError` came from *inside* a module that does exist and is
  re-raised: a bare `except ImportError: pass` would silently boot an app with a whole
  feature's endpoints missing, which is hard rule 9. A module that exists but exposes no
  `router` raises loudly for the same reason. All four paths were exercised by
  temporarily creating `backend/discovery/routes.py` in each state.
- **The browser-profile guard is enforced, not just commented.**
  `_assert_no_browser_profile_exposure()` runs at the end of `create_app()`, so a bad
  mount fails at import time rather than at request time. It compares **both directions**:
  serving `data/browser_profile/` is the obvious mistake, but the realistic one is
  feat/frontend mounting `data/` to reach built PDFs and dragging the live LinkedIn
  cookies along with it. Serving `data/documents/` stays allowed, and there is a test
  for that too.
- **Gotcha for feat/frontend: FastAPI 0.141 does not flatten `include_router`.** Included
  routers land in `app.routes` as a private `_IncludedRouter` that exposes neither
  `routes` nor `app`; the real `APIRouter` hangs off `original_router`. A first version of
  the guard only walked top-level `Mount`s and would have waved through a `StaticFiles`
  mounted inside an included router. `_static_directories()` now walks `app`, `_base_app`,
  `original_router` and `routes` with an id-based visited set, and the test is
  parametrised over both shapes so a FastAPI rename fails the suite instead of silently
  blinding the check.
- **`/health` really queries the database.** It issues `SELECT 1` and returns 503 with
  `status="degraded"`, `database="unreachable"` when that raises. SQLAlchemy connects
  lazily, so an endpoint that only inspected the engine would happily report 200 over a
  missing file — there is a test that overrides the session dependency with an unopenable
  path to prove the query is what decides the answer.
- **`applications_today` uses the *local* day, converted to UTC.** Adelaide is UTC+9:30
  (+10:30 in DST), so a naive UTC-day cutoff is wrong by up to ten and a half hours —
  measured live at 23:55 Adelaide time, the naive cutoff sat 9h30m later than the correct
  one, i.e. it would have reported zero for almost the entire local day. This matters
  beyond cosmetics the moment feat/apply's guardrails read the same number to enforce a
  daily cap. `_local_day_start_utc()` is currently private to `main.py`; **when guardrails
  needs the same boundary, extract it rather than copying it** — that is the second caller
  the rule is waiting for. An unresolvable `TIMEZONE` logs a warning and falls back to
  UTC instead of taking the endpoint down.
- `/api/meta/status` reuses `llm.budget_status()` verbatim rather than re-querying
  `llm_spend`; the cap lives with the table it reads.
- CORS allows exactly `settings.frontend_origin` with credentials. Never `*` — credentialed
  wildcard CORS is invalid, and this app has no auth to fall back on.
- `backend/__init__.py` now carries `__version__ = "0.1.0"`, used for the FastAPI version
  and `/health`. `importlib.metadata` cannot supply it: `pyproject.toml` has no
  `[build-system]`, so the project is never installed as a distribution. **Keep it in step
  with `[project].version` by hand.**

### Tests (`tests/conftest.py`, `tests/test_core.py`)

- **The environment is rewritten at conftest module scope, before the first backend
  import.** `backend.config` caches settings at import and `backend.db` builds its engine
  from that cached object, so a fixture would be far too late. `DATA_DIR` and
  `DATABASE_URL` point at a `mkdtemp` tree that is deleted on session teardown; the real
  `data/app.db` is never touched by the suite.
- `ALLOW_LIVE_SUBMIT` is **popped** from the environment, never assigned — not even to
  `"false"`. Assigning it would mean the suite tests the env var instead of the default.
  A developer who has genuinely turned live submit on will see these tests go red, which
  is the correct, deliberately loud outcome.
- Schema comes from `SQLModel.metadata.create_all` rather than `alembic upgrade head`:
  these tests assert on `backend/models.py`, and `alembic check` already proves the
  migration agrees with it. Running migrations per session would be slower and test the
  same thing twice.
- **`session_scope()` commits and closes on exit, so anything read from it is expired and
  detached afterwards.** Reading `row.answer_value` after the `with` block raises
  `DetachedInstanceError` — it caught me once. Either assert inside the block or select
  columns instead of ORM instances.
- **Aware in, naive out.** SQLite has no timezone type: an aware UTC value written to a
  `datetime` column reads back naive. Compare against `value.replace(tzinfo=None)`.
- 11 tests: `/health` 200 + `allow_live_submit is False`, `/health` 503 on a dead DB,
  `/api/meta/status`, the `ALLOW_LIVE_SUBMIT` default via `Settings(_env_file=None)`, the
  browser-profile guard (both mount shapes) and its narrower-mount counterpart, all 11
  tables on `SQLModel.metadata`, enum-by-value, seed idempotency **including that a
  verified answer survives a re-seed**, and that seeded answers stay blank and unverified.
  The last two are the hard-rule-1-and-2 regression guards: they fail if anyone ever
  "helpfully" fills the answer bank with plausible defaults.

## Block A — verification

Everything below is real output from `uv run` in `/home/user/JobSeekr`.

| # | Check | Result |
|---|---|---|
| a | `uv run ruff check backend tests` | **All checks passed!** (exit 0) |
| b | `uv run pytest -q` | **11 passed**, 1 warning, 3.0s |
| c | Real uvicorn boot on `:8099` + curl | **HTTP/1.1 200 OK**, JSON body, clean shutdown |
| d | `PRAGMA journal_mode` | **`wal`** |
| e | Hardcoded model names in `backend/` | **only `backend/config.py`** (5 lines) |
| f | `print(` in `backend/` | **zero** |

- **(a)** Two findings on the first run, both fixed rather than silenced: `TRY004` on an
  `isinstance` check in `_register_routers` (dropped the isinstance — `include_router`
  validates its own argument) and `DTZ001` on a naive `datetime()` in a test (now aware
  UTC, with the naive read-back documented). `ruff format --check` also reported one file;
  reformatted, and `ruff format --check backend tests` now reports 20 files already
  formatted. Note this build of ruff enforces more than the E4/E7/E9/F default set even
  with no `[tool.ruff]` config in the repo.
- **(b)** `11 passed, 1 warning in 2.98s`. The warning is upstream and not ours:
  `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated;
  install httpx2 instead` — `httpx` is the pinned dependency, so this is left alone.
- **(c)** `uv run uvicorn backend.main:app --port 8099`, then
  `curl -D - http://127.0.0.1:8099/health`:
  ```
  HTTP/1.1 200 OK
  content-type: application/json

  {"status":"ok","version":"0.1.0","allow_live_submit":false,
   "database":"connected","time":"2026-08-23T14:26:30.520298Z"}
  ```
  `/api/meta/status` answered 200 with
  `{"jobs":0,"applications_today":0,"llm_budget":{"month":"2026-08","spent_usd":0.0,
  "cap_usd":25.0,"remaining_usd":25.0,"fraction":0.0,"warn_fraction":0.8,"warned":false,
  "exceeded":false}}`. Startup logged
  `app_startup allow_live_submit=False app_env=local
  database=/home/user/JobSeekr/data/app.db frontend_origin=http://localhost:5173
  version=0.1.0`, and shutdown logged `app_shutdown`. Port confirmed closed afterwards.
- **(d)** `wal`.
- **(e)** `grep -rnE 'gpt-|claude-|gemini-|text-embedding' backend/` returns five lines,
  all `backend/config.py:54-58`. Nothing outside it, case-insensitively either. No
  violation to fix.
- **(f)** `grep -rnE '(^|[^A-Za-z_.])print\(' backend/` returns nothing.

Also verified, beyond the required list:

- **The live-submit warning fires.** Confirmed by patching `settings.allow_live_submit`
  **in-process only** — never the env var, never `.env`, never `.env.example` — and
  booting the app: two `!`-banner lines around
  `LIVE_SUBMIT_IS_ON detail='ALLOW_LIVE_SUBMIT=true — real applications will be submitted
  to real employers as you...'`. The attribute was restored immediately and a fresh
  process reads `allow_live_submit = False`. Nothing in the repo assigns it true; the only
  grep hit for `true` is that warning's message string.
- **Router registration, all four states**, by temporarily creating
  `backend/discovery/routes.py`: absent → `router_not_present` debug, app boots; valid →
  `router_registered` and `GET /api/discovery/ping` returns `200 {"pong":"discovery"}`;
  broken internal import → `ModuleNotFoundError: No module named
  'totally_missing_package'` propagates; no `router` attribute → `RuntimeError:
  backend.discovery.routes exists but exposes no module-level 'router: APIRouter'`. The
  file was deleted afterwards; `backend/discovery/` holds only `__init__.py`.
- `create_app()` returns an instance distinct from the module-level `app`; CORS resolves
  to `allow_origins=['http://localhost:5173']`, `allow_credentials=True`.
- `pyproject.toml`, `.env.example`, `.gitignore` and `uv.lock` are unmodified — mtimes
  predate this session. No `uv add`, no git commands run.

---

## Block B — discovery & scoring

### The Seek endpoint could not be verified here (blocked host)

The spec says: *find the real endpoint by inspecting network traffic from a seek.com.au
search — do NOT guess it.* **That was not possible in this build environment.**
`www.seek.com.au` is blocked by the network's egress policy; every request returns
`CONNECT tunnel failed, response 403` at the proxy. `linkedin.com` and `au.indeed.com`
are blocked the same way. `github.com` and PyPI are reachable.

So the instruction was honoured in the only way available — by making the guess
*correctable and self-checking* rather than buried:

1. Every request parameter (`SEEK_SEARCH_URL`, site key, source system, locale, page
   size) is a setting. Correcting it is an `.env` edit, not a code change.
2. `uv run python -m backend.discovery.verify_seek --terms "..." --where "..."` runs on
   **your** machine, probes each candidate in turn, reports status codes and the observed
   JSON keys, prints a sample record, and prints the exact `.env` lines to paste.
   **Run this before the first real discovery pass.**
3. `seek_source.py` tries three strategies in order — JSON API → server-rendered page
   state (`SEEK_REDUX_DATA` / `__INITIAL_STATE__` / `__NEXT_DATA__`) → JSON-LD
   `JobPosting` — so one contract change degrades discovery instead of killing it.
4. Field reads go through an alias table and never raise; an unmappable record is logged
   and skipped.

### What inspecting the installed jobspy actually showed

`python-jobspy==1.1.82` (pinned). Verified by reading the package, not assumed:

- Supported sites are `linkedin, indeed, zip_recruiter, glassdoor, google, bayt, naukri,
  bdjobs`. **Seek is not among them** — it genuinely needs its own adapter.
- `scrape_jobs` returns a `pandas.DataFrame`. Real columns include `job_url_direct`,
  `emails`, `interval`/`min_amount`/`max_amount`, `listing_type`. Missing values arrive
  as `NaN`, so every read goes through a NaN-safe accessor.
- `job_url_direct` pointing off Indeed is the off-site-redirect signal → `apply_type='external'`.
- **There is no per-row "is Easy Apply" column.** LinkedIn exposes `easy_apply` only as a
  *search filter*. Claiming `easy_apply` without evidence would send the apply engine into
  a modal that is not there, so it is claimed only when the search itself filtered for it;
  otherwise `unknown`, and the apply layer confirms.

### Salary: two columns added to `job`

`salary_basis` and `salary_is_estimated` (migration `4980eb9af6bf`). The spec's schema had
no place to record what the advertiser actually said, and the normalisation requirement
("never silently claim an annual salary that was stated hourly") cannot be honoured
without it. `salary_min`/`salary_max` are always annualised so one filter works on one
scale; these two columns keep the claim honest.

**Ads that state no salary are KEPT, not dropped**, even under a salary floor — most
Australian ads omit salary, so dropping them would discard the majority of the market to
enforce a floor that was never tested. Opt in to the strict behaviour per campaign with
`exclusions.drop_unstated_salary = true`.

### Cost target: the honest numbers

Target: 200 jobs discovered and scored for **under $0.15**. Discovery is plain HTTP and
free, so this is entirely a scoring budget. Projections from
`backend.scoring.run.estimate_cost` (priced from `LLM_PRICES_PER_M_TOKENS`):

| Configuration | stage 1 | stage 2 | total | meets target |
|---|---|---|---|---|
| **Default — Opus 5, top 40** | $0.0015 | $0.4300 | **$0.4315** | no |
| Opus 5, top 10 | $0.0015 | $0.1075 | $0.1090 | yes |
| Haiku 4.5, top 40 | $0.0015 | $0.0860 | $0.0875 | yes |

**The target is not reachable at the default model while scoring 40 jobs, and no amount of
prefilter tuning fixes that.** Claude Opus 5 is $5/$25 per 1M tokens; the
schema-constrained score object is ~220 output tokens, so stage 2 costs ~$0.0055 per job
on *output alone* — 40 jobs exceed $0.15 before a single prompt token is counted.

The spec says to tune the prefilter rather than the model, and that was done as far as it
goes: descriptions are truncated to 1400 chars for embedding and 2400 for the rubric
prompt, embeddings are batched and cached to disk so re-runs cost nothing, and stage 1 is
~$0.0015 per 200 jobs. Stage 1 is not the problem; the stage-2 fan-out is.

**Choosing the model is left to the user, deliberately.** `LLM_MODEL_SCORING` still
defaults to `anthropic/claude-opus-5` rather than being quietly downgraded to make a
number go green. Instead the code is loud about it: `estimate_cost` returns
`meets_target` plus concrete `levers`, and every scoring run logs
`scoring_cost_over_target` with the projection before spending. Pick a lever:

- keep Opus 5 and set `SCORING_STAGE1_TOP_N=10`, or
- set `LLM_MODEL_SCORING=anthropic/claude-haiku-4-5` and keep 40, or
- raise `SCORING_COST_TARGET_USD`.

Prices live in `LLM_PRICES_PER_M_TOKENS` so a price change is an `.env` edit. They are a
planning aid only — real spend always comes from the `llm_spend` table.

### Other decisions

- **Dedupe fuzzy matching is scoped to one canonical company.** A missed duplicate wastes
  one score; a false duplicate silently deletes a real job the user would have applied to.
  Titles are normalised harder instead (a trailing " - Adelaide" is stripped, a trailing
  " - Backend" is not) so the >0.9 threshold can stay strict.
- **`contacts.py` reads only what the advertiser published in their own ad.** The Spam Act
  boundary is in the module docstring: no harvesting, no address-pattern guessing, no
  address reuse. Platform/ATS senders and no-reply addresses are rejected.
- **`final` score** = stage 2 when it ran, else stage 1 similarity rescaled to 0-100, so a
  threshold means one thing regardless of which stages ran.
- Two bugs the tests caught: `"South Australia"` was being destroyed by the country strip
  in `canonical_suburb`, and `"$120 - $140 per annum"` was read as an hourly rate
  (annualising to $237k). Both fixed, both now regression-tested.

---

## Block C — documents & parse gate

### No existing .tex resume was found

The spec says to locate the user's existing `.tex` resume and audit it for
ATS-killers. There is none: the repository contained only `Claude.md` at the start
of this work, and a search of the filesystem and the full git history found no
`.tex` file. So `templates/resume.tex.j2` was built from scratch, single-column A4,
with every constraint the audit would have enforced applied up front and commented
in the template header so nobody "improves" them later:

- pdflatex only — no `fontspec`, no `unicode-math` (they do not compile under pdflatex)
- `lmodern` + `T1` fontenc — OT1 lets "efficient" extract as "e cient"
- `a4paper` — the LaTeX default is US Letter, wrong for Australia
- one column — two-column resumes extract as interleaved nonsense
- contact details in the **body**, never a header — many parsers skip headers
- no `fontawesome` icons (also not installed here; they extract as garbage)

**If the user has an existing `.tex` resume, drop it in and re-run the parse gate
against it** — the gate is what the audit would have been, and it is automated.

### Template engine

One Jinja2 environment for all three artifact kinds, with the LaTeX-safe delimiters
the spec fixes (`\BLOCK{}`, `\VAR{}`, `\#{}`, `%%` line statements).

`escape_latex` is applied automatically to every substituted value through a
`finalize` hook, so a template author cannot forget it. It uses a sentinel for the
backslash: replacing `\` with `\textbackslash{}` first means the braces that form
introduces get escaped by the later `{`/`}` rules (producing
`\textbackslash\{\}`), and replacing it last means every other rule's backslash
gets mangled. A sentinel containing no special character sidesteps both. This was
a real bug the tests caught.

**Gotcha for anyone editing the shipped templates:** LaTeX comments must use a
single `%`. `%%` is Jinja's `line_statement_prefix` and is parsed as a tag —
a `%%`-prefixed comment block fails with `TemplateSyntaxError: tag name expected`.

### Anti-fabrication is enforced, not requested

`backend/documents/fabrication.py` validates generated narrative against the
profile rather than trusting the prompt. It flags unsupported years, metrics,
organisations and credential words. On violation the slot regenerates **once**
with the specific problems fed back; a second failure fails the build loudly and
produces **no document at all** (verified by a test that feeds in "Certified AWS
Solutions Architect since 2019 … increased revenue 340% … Acme Corporation").

Organisation matching is per significant token rather than whole-phrase: "Python
and SQL" is two real skills that never appear as one contiguous string in the
profile, while "Acme Corporation" and "Stanford University" are still caught
because the distinctive word is simply absent.

### Parse gate

Nine checks (the spec's eight plus two-column detection). Adversarial suite in
`tests/test_parse_gate.py` generates each broken PDF for real with pdflatex and
asserts rejection: two-column, image-only, header-only contact details, 4-page
resume, missing SKILLS section, unmet claimed keywords, truncated byte stream,
missing file, and a cover letter over one page. A clean single-column resume
passes all nine.

**Two-column detection was calibrated against real output, not guessed.** The
first implementation histogrammed word *start* positions and missed the fixture
entirely — wrapping scatters the starts across each column. The working version
looks for a vertical band that no word *overlaps*. Measured: a clean
single-column resume's widest empty band is 3pt (the ragged edge around
right-aligned dates); a `multicol` two-column one is 9pt, because LaTeX's default
`\columnsep` is 10pt. The threshold is 8pt, sitting between them — a 24pt
threshold picked for "looks like a column gap" detected nothing.

### No fallback to an older PDF, ever

A failed gate marks the job failed with the full report attached. There is no
path that reaches for a previously built document: a stale resume that passes the
gate is the wrong document sent confidently, and the downstream filename readback
cannot catch what was never rebuilt.

---

## Block D — apply engine

### Selectors are UNVERIFIED. Read this before enabling live submit.

`seek.com.au` and `linkedin.com` are both unreachable from the environment this
was built in (blocked by network policy — the same block that stopped the Seek
endpoint being confirmed in Block B). **No selector in `backend/apply/seek.py` or
`backend/apply/linkedin.py` was tested against the live site.** Each carries a
confidence note in the source; the `data-automation` (Seek) and `aria-label`
(LinkedIn) hooks are the most durable and lead each candidate list, with CSS
fallbacks behind them.

**Verification procedure — do this before turning `ALLOW_LIVE_SUBMIT` on:**

```
uv run python -m backend.apply.session login --platform linkedin
uv run python -m backend.apply.har record --platform linkedin --variant two_step
uv run python -m backend.apply.har list        # shows what is still missing
uv run python -m backend.apply.run --dry-run   # walks everything, submits nothing
```

`har.py` names the seven variants worth capturing (2-step, 5-step, with and
without a cover-letter slot, an off-site redirect, Seek quick apply, Seek
screening step) because each exercises a different adapter branch. Once
recorded, `replay()` serves them back offline so the adapters are pinned by
tests instead of by hope. `canary.py` then checks daily for markup drift and
**warns without halting** — a renamed CSS class should not stop the pipeline;
a real failure mid-application is what trips the circuit breaker.

### Design decisions

- **Sync Playwright, not async.** A single-user desktop tool that applies to one
  job at a time gains nothing from async, and the sync API composes directly with
  the rest of this synchronous codebase (SQLModel sessions, the flow, the
  guardrails) without colouring every caller.
- **Fuzzy threshold 88, ambiguity margin 6.** Screening questions are short and
  share most of their words, so ordinary similarity between two genuinely
  different questions is already high — "forklift licence" vs "driver's licence"
  scores in the seventies. 88 is strict enough that a near-miss is a different
  question, and the margin means two candidates within 6 points that *disagree*
  abstain rather than letting the top one win.
- **Polarity pairs are handled explicitly.** "Do you require visa sponsorship?"
  and "Do you have full working rights?" share vocabulary but have opposite
  correct answers. Fuzzy matching alone conflates them, and leaking an answer
  across that pair misstates the user's right to work — so a polarity check
  blocks it outright rather than relying on the threshold.
- **Warm-up ramp: 3, 6, 10, 15, 20 per day by week, ceiling 25.** A new account
  submitting thirty applications on day one is the pattern platforms act on.
- **Circuit breaker persists to `data/circuit_breaker.json`.** A file rather
  than a table, because it is operational state rather than user data and it
  must survive a restart without a migration. If it later wants to be queryable
  from the dashboard, it belongs in the DB — noted, not done.
- **Daily caps count the user's LOCAL day**, not UTC. Adelaide is UTC+9:30, so
  a UTC-midnight cap would roll over mid-evening and let a second day's
  allowance out before the user's day ended.
- **`--dry-run` is the default** for `backend.apply.run`. `--live` is the
  explicit opt-in, and even then every guardrail still applies.

### What the safety suite actually proves

`tests/test_apply_safety.py` is the file to read first. It builds one flawless
application — score 97, three gate-passed documents, zero abstentions,
authenticated session, inside the window, under every cap, breaker closed — and
asserts that under default settings the *only* failing check is
`allow_live_submit`. Then it flips that single setting and asserts the same
application goes through. That pairing is what proves the switch is the sole
gate and nothing else was quietly failing underneath it.

It also enforces, repository-wide and by parsing the AST rather than grepping
text (every one of these files legitimately *discusses* the rule in prose):

- `check_can_submit` is called from exactly one place — `backend/apply/flow.py`
- neither adapter imports or calls the guardrails
- `backend/apply/answers.py` imports no Playwright
- nothing anywhere sets `allow_live_submit` true, including `.env.example`
- no hardcoded `time.sleep` in the apply layer outside `pacing.py`

---

## Block E — API and dashboard

### Stack as actually installed

Vite 8, React 19, TypeScript 6, **Tailwind v4.3** and react-router 7. Tailwind v4 is
CSS-first: there is no `tailwind.config.js` and no PostCSS config — the theme lives in
`src/index.css` behind `@theme`, and the build uses the `@tailwindcss/vite` plugin.
Anyone expecting the v3 layout will go looking for files that should not exist.

**Data fetching is a 30-line `useAsync` hook, not a query library.** This dashboard talks
to a FastAPI process on the same machine: there is no network latency to paper over and
no cache-invalidation problem worth a dependency. Everything shared —
date/salary/score formatting, clipboard, the elapsed timer — lives in `src/lib/hooks.ts`
so no page grows its own.

Two TypeScript-6 defaults in the Vite template caught real code:
- `erasableSyntaxOnly` rejects constructor parameter properties, so `ApiError` declares
  and assigns its fields separately.
- React 19 passes `ref` as an ordinary prop, but it still has to be declared on the
  component's props type.

### Shared components, built once

- `DataTable` — sorting, filtering, pagination, expandable rows. Used by Jobs,
  Applications and the Answer bank. Missing values always sort last, in both
  directions: a blank is not a small number, and floating blanks to the top buries
  the real rows.
- `DynamicFieldList` / `StringList` — add, remove and reorder rows of any sub-form.
  The Profile page uses it five times over; Campaigns uses `StringList` for terms,
  locations, work types and both exclusion lists.
- `StatusBadge` / `ScoreBadge` — the single source of truth for status colour. When
  colour lives in each page, "failed" ends up red on one screen and grey on another and
  the operator learns to distrust it.
- `TemplateEditor` — ONE editor for all three kinds, selected by a tab. Three editors
  would mean three autocomplete lists drifting from the one the backend enforces.

Verified: no page calls `fetch()` directly; everything goes through `src/lib/api.ts`.

### The 90-second queue

`Queue.tsx` is designed around the stopwatch rather than around REST:

- **one card at a time** — nothing to scan past
- **one API call per card**, carrying the job link, both document ids, the cover letter
  text and every answer-bank value. A second round trip to fetch the letter is exactly
  the pause that makes manual applying feel like work.
- **every answer is a copy chip with a visible copied-state**. Without the confirmation
  there is no way to tell a successful copy from a missed tap, and re-checking costs
  more time than the copy saved.
- **keyboard first**: `o` opens the ad, `Enter` marks done, `s` skips, `j`/`k` move.
  Done and Skip advance automatically.
- **a live timer against the 90-second target**, which turns amber past it. A target you
  cannot see is not a target.

Unanswered questions are surfaced on the card itself, because a blank answer is why the
job was parked in the first place.

### Analytics greys out what it cannot support

Every bucket carries `sufficient_data` and its `n` from the backend. Below the minimum
(`ANALYTICS_MIN_SAMPLE`, default 8) the row is dimmed, the rate is replaced with
"not enough data", and the raw `n` is shown instead. A 100% interview rate from one
application is not an encouraging number, it is a wrong one, and rendering it invites a
real decision to be made on noise. Both interview rate and any-reply rate are reported,
broken out by campaign, platform, score decile and rubric version — separately, because
scores from different rubrics are not comparable.

### Two security properties, both tested

- `GET /api/documents/{id}/file` resolves the path and refuses anything outside
  `documents_dir` with a 404. The machine running this holds a live authenticated
  browser profile, so a traversal here leaks session cookies, not a resume.
- `ALLOW_LIVE_SUBMIT` is exposed **read-only**. It is absent from `SettingsIn`, so no
  code path can set it, and the Settings page renders it as an indicator with an
  explanation rather than as a toggle.

---

## Block F — Telegram, inbound mail, outbound drafts, scheduler

### Escalation parks the job; it never holds the browser

When an adapter abstains, the flow closes the browser, marks the job
`needs_answer`, and returns. Telegram then asks the question. **Nothing waits on the
reply.** Holding a session pinned on a job board for twenty minutes while the user is
asleep is exactly the pattern that gets an account flagged, and a timeout mid-form
leaves an application half-submitted.

The loop is park → ask → **save to the answer bank** → re-queue. Saving rather than
just using the answer is what makes the bank self-populating: `save_answer` fills the
matching blank row instead of creating a duplicate, so a seeded question is answered
once and never asked again.

### Matching inbound mail is the hard part

**ATS mail does not come from the employer.** A rejection for a university job arrives
from `no-reply@pageuppeople.com`; a JobAdder acknowledgement from `noreply@jobadder.com`.
Domain matching therefore fails on exactly the mail that matters.

So `matching.py` scores several weak signals and requires a threshold (55):

| Signal | Points | Why |
|---|---|---|
| Sender is the contact address published in the ad | 50 | The only *direct* identity link, so it clears alone |
| Source job id or reference in the message | 45 | Explicit, ATS-generated |
| Employer named anywhere | 25 | Strong, but generic on its own |
| Title matches the subject (≥85%) | 25 / 12 | Weak alone — half the market says "Software Engineer" |
| Within 14 / 45 days of applying | 10 / 4 | Replies cluster |
| Sender domain mentions the platform | 8 | Corroborating |

It **returns None rather than a best guess** when the top score is below the threshold
*or* when the top two candidates are within 12 points and cannot be separated. A wrong
match writes "rejected" onto a live application or "interview" onto a dead one, and
every number on the Analytics page is then built on fiction. An unmatched email costs
the user one glance at their inbox.

Classification is an LLM call because keyword rules misread the two cases that matter:
"we were very impressed, however…" is a rejection that reads positive, and "we'd like
to arrange a time" is an interview request containing none of the obvious words.

### Gmail: two auth methods, one interface

- **Personal @gmail.com** → IMAP with an App Password.
- **Google Workspace** → App Passwords were disabled for Workspace accounts in 2025,
  so OAuth via the Gmail API is the only route. **An OAuth app left in *Testing* status
  expires refresh tokens every 7 days** — publishing the app (even privately) is what
  stops weekly re-authorisation. The refresh failure logs that hint explicitly.

Scope is `gmail.readonly`, not `modify`: this module has no business changing the
mailbox. There is no send path in `gmail.py` at all.

### Outbound is draft-only, and the constraint is legal

`outbound.py` carries the Spam Act boundary in its docstring. Three properties are
load-bearing and all three are tested:

1. **The address came from the ad.** `draft_for_job` reads `ad_contact_email` and takes
   **no recipient parameter** — a test asserts the signature has none, because a
   recipient argument is how a draft-only path becomes a mail merge.
2. **A human approves each message.** `send_draft` requires an `approved_by` token and
   refuses without one. There is no auto-send and no scheduled caller.
3. **No follow-ups.** One message per job. A test greps for `send_bulk`,
   `schedule_followup` and `harvest` and fails if any appears.

It also refuses to attach a document that did not pass the parse gate — the same rule
as the apply path.

### Scheduler

APScheduler with a SQLAlchemy job store so the schedule survives the reboots a desktop
actually has. `coalesce=True` and `max_instances=1` so a laptop waking from sleep does
not fire five missed discovery runs at once.

**The two daily apply passes are jittered by up to 45 minutes.** Applications arriving
at exactly 14:00:00 every weekday is a machine signature that no amount of per-submit
pacing hides.

The scheduled apply pass follows the master switch: with `ALLOW_LIVE_SUBMIT` off it runs
end to end as a dry run and reports what it *would* have sent, which is what makes it
safe to leave the scheduler enabled while evaluating the system.

Nightly backup uses `sqlite3.Connection.backup`, not a file copy: the database is in WAL
mode and being written to, and a plain copy can capture a torn state that reads fine and
is missing the last transactions. Fourteen days are retained.

The weekly rubric review **proposes only**. Changing a rubric creates a new version and
makes historical scores incomparable, so it is the user's call.

### Notification hooks, not imports

`guardrails`, `session` and `canary` each expose a plain callable hook that
`notify.register_hooks()` fills in. The safety-critical modules therefore never import
Telegram, stay unit-testable without a bot token, and a notification failure can never
take down the thing it was reporting on.

---

## Block G — external ATS and form maps

### Australian priority, not the US default

`ATS_REGISTRY` is ordered for the Australian market rather than for what US
scraping guides assume:

1. **JobAdder** — the leading AU/NZ-native ATS, and what most small and mid-size
   Australian employers actually run.
2. **PageUp** — an Australian company; government, universities, large enterprise.
3. **SmartRecruiters** — common in the AU mid-market.
4. **Greenhouse / Lever** — over-represented in US guidance, under-represented here
   outside startups.
5. **Workday last**, and flagged `requires_account=True`. Not because it is rare but
   because it demands a separate account per employer, making it the most expensive
   flow to automate and the least worth doing first.

Plus Google Forms, Typeform and JotForm: a great many "bespoke" careers pages are one
of those in an iframe, and `detect_from_html` follows the iframe rather than giving up.

Detection is URL patterns first (free and unambiguous), HTML fingerprint second — the
fallback exists because PageUp and JobAdder are usually white-labelled onto
`careers.employer.com.au` with nothing in the URL to give them away.

### The form map cache

**Fingerprint = sha256 of the sorted set of (name, label, type).** Not the URL and not
the company, so two employers on one ATS with the same form share a map and the second
costs nothing. Sorted, so DOM order changing between renders does not invalidate it.
Labels are whitespace- and case-normalised.

**Semantic identity, never CSS selectors.** A map records the label a human reads;
`selector` exists as a last resort and is normally unset. Class names change with every
ATS release, the question does not. `fill_field` tries `get_by_label`, then
`get_by_role`, then the selector.

**WHERE, never WHAT.** `save_map` raises if a mapping ever carries a value attribute,
and a test greps the serialised JSON for answer-shaped content. This is not tidiness:
a map that accumulated answers would turn a *shared platform-tier file* into one
containing the user's personal data, and would let a stale answer be replayed months
later without passing through the answer bank.

**Two tiers, merged field by field.** `platform/*.json` is shared; `company/{fp}.json`
overrides. Field-by-field so one employer's odd question does not force re-learning the
whole form — and an unresolved override never blanks out a resolved platform field.

**Partial relearn.** `relearn_targets` returns only the fields the cache does not
already know, so a form that gained one question costs one small call rather than a
full re-learn.

**Trust graduation.** Three *consecutive* clean successes. A failure resets the streak
rather than decrementing it — three successes must be consecutive to mean the mapping
generalised — and a trusted map that fails loses its trust immediately.

### Generic filling uses the accessibility tree

`page.accessibility.snapshot()` rather than raw HTML: a few hundred tokens instead of
tens of thousands, and semantically better input, because the accessible name of a
field *is* the label a human reads.

The abstain rule is the answer bank's, for the same reason. The mapping schema has no
value field at all — the model says where a value comes from and never what it is —
and `confident=false` makes a field unusable. A field skipped by the model becomes
`unknown` rather than being silently dropped.

**CAPTCHA is a hard stop.** The job is parked and the user notified. No solving
services, no third-party bypass: a CAPTCHA is the site asking for a human, and the
correct response is to fetch one.

### Manual queue requires a HIGHER score than auto-apply

`queueing.py`, and it looks backwards until you count what each path costs. An
automated application costs a fraction of a cent and four minutes nobody is watching.
A manual one costs ninety seconds of the user's attention — the genuinely scarce
resource in a job search, the thing that runs out at 9pm after a day of work.

So the manual floor is `score_auto_apply + 8`. A job good enough to auto-apply to is
*not* automatically good enough to interrupt someone for; if it is not worth their
attention it is skipped rather than queued.

---

# Windows bring-up attempt — 2026-08-25

## The headline: this did not run on Windows

The session that was asked to do the Windows bring-up **was not on Windows.** It
was the same Linux container the project was originally built in:

```
platform.system() -> Linux        os.name -> posix        os.sep -> '/'
pdflatex          -> /usr/bin/pdflatex   (TeX Live 2023, NOT MiKTeX)
powershell.exe / cmd.exe / miktex-console -> absent
```

There is no path from that container to a Windows desktop, so **every
Windows-specific claim in this file is unverified on Windows.** Phases 1 and 4
were re-scoped to the work that genuinely transfers; Phase 2 was blocked
outright; Phase 3 was fully completed.

Nothing below should be read as "works on Windows". It should be read as
"the Windows-specific defects that could be found without Windows have been
found and fixed".

## Phase 1 — Windows portability (re-scoped: static audit, not a live run)

Could not be done: verifying Python/uv/node/MiKTeX/Playwright on the target
machine, `uv sync` + `npm install` + `alembic upgrade head` on Windows, or
running the suite there.

Done instead: a systematic audit for the defect classes that only bite on
Windows, plus `tests/test_windows_portability.py` (19 tests) that encodes them
as rules failing on Linux too, so a regression is caught here rather than on
the desktop.

**Three real bugs found and fixed:**

1. **`subprocess.run(..., text=True)` on the pdflatex call.** `text=True` alone
   decodes with the locale encoding — cp1252 on a typical Windows install. One
   non-cp1252 byte in a pdflatex log (an em-dash in a path, a package banner)
   raises `UnicodeDecodeError`, and the build then fails with a decoding error
   instead of the LaTeX error that actually happened. Now pinned to
   `encoding="utf-8", errors="replace"`.

2. **Aux cleanup could discard a good build.** `aux.unlink()` was unguarded. On
   Windows an on-access virus scanner holds a just-written file open for a
   moment and `unlink` raises `PermissionError` — losing a PDF that had already
   compiled correctly. Cleanup is now best-effort and logs at debug.

3. **`tzdata` reached Windows only by accident.** Windows ships no tz database,
   so `ZoneInfo("Australia/Adelaide")` raises `ZoneInfoNotFoundError` without
   it — which would crash the guardrails' business-hours check, the daily cap's
   local-day boundary and the scheduler. It was arriving transitively via
   pandas' `sys_platform == 'win32'` marker, i.e. it would have vanished the day
   jobspy was swapped out. Now declared explicitly with a Windows marker.

Audited and found already clean: no `open`/`read_text`/`write_text` without an
explicit encoding, no `/tmp` or other POSIX-absolute paths, no path building by
string concatenation, no `shell=True`, all settings paths are `pathlib.Path`,
no Windows reserved filenames generated.

**Still unverified on Windows and needing you:** MiKTeX's `pdflatex` (this used
TeX Live), `channel="chrome"` launching real Chrome, SQLite WAL behaviour (WAL
does not work on network drives — keep `data/` on a local disk), the 260-char
path limit under a deep user profile, and Task Scheduler at login.

## Phase 2 — BLOCKED. Seek, LinkedIn and Indeed are still unreachable

Re-tested at the start of this session, not assumed:

| Host | Result |
|---|---|
| `www.seek.com.au` | CONNECT fails — proxy 403 |
| `au.indeed.com` | CONNECT fails — proxy 403 |
| `www.linkedin.com` | CONNECT fails — proxy 403 |
| `generativelanguage.googleapis.com` | reachable (404 on `/`, expected) |

`uv run python -m backend.discovery.verify_seek` was run and reported
`NOTHING WORKED`, with `ProxyError: 403 Forbidden` on all three strategies —
the tool behaving exactly as designed, which is itself the one useful result.

**So none of Phase 2 happened:** the endpoint is still unconfirmed, no real jobs
were pulled, jobspy was not exercised against LinkedIn or Indeed, and dedupe is
still tested only against fixtures, never real cross-board duplicates.

**This needs you, on your machine, and it is the highest-value thing outstanding:**

```
uv run python -m backend.discovery.verify_seek --terms "python developer" --where "Adelaide SA"
# paste the .env lines it prints, then:
uv run python -m backend.discovery.run --limit 50
uv run python -m backend.scoring.run --estimate 200
```

## Phase 3 — DONE. Scoring and classification moved to Gemini Flash-Lite

The cost problem flagged in the PR is now solved:

| Scoring model | Projected, 200 jobs | Meets the $0.15 target |
|---|---|---|
| `anthropic/claude-opus-5` (was) | **$0.4315** | no — 2.9x over |
| `gemini/gemini-3.1-flash-lite` (now) | **$0.0252** | **yes — 6x under** |
| `gemini/gemini-2.5-flash-lite` | $0.0092 | yes, but see below |

Routing is split by consequence-of-being-wrong rather than by prestige:

- **scoring, classify -> Gemini Flash-Lite.** Both are constrained
  classification against a fixed schema, both run on every job/email, and both
  are recoverable — a mis-scored job is re-scored, a mis-classified email is one
  status the user corrects.
- **writing -> Claude Opus 5, unchanged.** Cover letters and resume bullets go
  to an employer under the user's name and cannot be recalled. One call per
  application, so the unit cost is irrelevant.
- **formmap -> Claude Opus 5, unchanged.** A mis-mapped field puts a false
  answer on a real application. The abstain rule catches low confidence, but the
  cheap failure here is silent and unrecoverable. Left strong deliberately; move
  it only with evidence.

**Decision you should know about: defaulted to 3.1, not 2.5.** Google retires
`gemini-2.5-flash-lite` on **2026-10-16** — about seven weeks out — replaced by
`gemini-3.1-flash-lite`. Pinning 2.5 would buy $0.016 per 200 jobs and hand you
a forced migration next month. Both are priced in `llm_prices_per_m_tokens` if
you want the cheaper rate short-term.

**No `.env` exists, so no key was used and nothing was called live.** The wiring
is complete and the projection is computed from the configured models. To
actually use it: `GEMINI_API_KEY=...` in `.env`. The Gemini endpoint *is*
reachable from here, so a live smoke test is possible the moment a key exists.

## Phase 4 — DONE. Real templates, real pdflatex, real parse gate

Compiled `templates/resume.tex.j2` and `cover_letter.tex.j2` with the real
`pdflatex` using the **production context builders**, then merged and gated all
three. Confirmed by inspection and by compiling: `a4paper`, single column, no
`fontspec`, no `multicol`, no `fancyhdr`, no fontawesome, T1 + lmodern.

Result: **resume 1 page / 803 chars, cover letter 1 page / 370 chars, combined
2 pages / 1174 chars — all three pass every gate check**, including
pypdf-vs-pdfplumber agreement at 99.9% and no column gutter detected.

**Two real bugs found, both only visible by compiling for real:**

1. **Every resume build failed for any profile created through the UI.** The
   engine runs with `StrictUndefined` — deliberately, because that is what
   catches `job.compnay` before a document reaches an employer — but that also
   means `\BLOCK{if role.location}` *raises* when the key is absent rather than
   evaluating false. The Profile page's experience editor has no location field
   at all, so every profile it produces lacks that key and every build died.
   Fixed by normalising rows in `_profile_context` to carry the full key set.

2. **Double-escaping silently corrupted special characters.** The escaping
   filters (`join_latex`, `latex`, `url`) returned plain `str`, so the finalize
   hook escaped their output a second time and the escape became content. A
   skill of `C#` typeset as the literal text `C\#`.

   This is the worst kind of failure this project can have: the PDF looked
   almost right, **the parse gate passed**, and an ATS searching for "C#" or
   "R&D" found nothing. It survived because the gate's `claimed_keywords` check
   happened to be given only Python/SQL/FastAPI — none containing a special
   character. Fixed by returning `RawLatex` from all three filters, with
   regression tests in `tests/test_engine.py`.

Extracted text from the real compiled PDF, after the fixes:

```
Jordan Fitzgerald
Backend Engineer
jordan.fitzgerald@example.com · +61 412 345 678· Adelaide SA 5000· linkedin.com/in/jordanfitzgerald
SUMMARY
Backend engineer building efficient, financial-grade data pipelines.
SKILLS
Python, SQL, FastAPI, PostgreSQL, C#, R&D tooling
EXPERIENCE
Senior Developer 2022 – Present
Wattle & Finch Pty Ltd
• Rebuilt the financial reconciliation service end to end
...
```

Contact details land in the body and in the first 200 characters; sections
appear as plain words in source order; `efficient`, `financial` and
`certification` all survive extraction intact (the fi/fl ligature check);
`C#`, `R&D`, `100%` and `Wattle & Finch` now all round-trip correctly.

Caveat that matters: this was **TeX Live 2023 on Linux, not MiKTeX on Windows**.
The LaTeX is portable and the packages used are all in a basic MiKTeX install,
but MiKTeX's on-the-fly package installation prompts on first use — run one
build manually before relying on a scheduled one.

## Hardening — Phase 1: merge and tidy. Done, but not the way it was planned.

When I checked at the start of this session, PR #1 was still red: the
GitGuardian check run `97745798725` was `conclusion: failure`, timestamped
`2026-08-25T09:03:02Z` — **before** the incident was dismissed — and it was the
only run on that head. Dismissing an incident resolves the *incident*; it does
not rewrite a check run that already completed. GitHub only learns of a new
conclusion when GitGuardian re-scans and posts one.

The instruction was to report and stop touching git if either PR still failed,
so nothing was merged at that point. Pushing this session's hardening commits
triggered the re-scan, which came back **success**.

Two things had changed under the Phase 1 plan while it was blocked:

* **PR #2 merged itself.** Its base was `claude/job-agent-core-setup-07xswl`, so
  the moment the hardening push landed on that branch, its head `fix/windows-bringup`
  (725e207) became reachable from its base and GitHub marked it merged —
  `merged_at: 2026-08-26T12:45:57Z`, the same second as the push. There was no
  second PR left to merge by hand.
* **PR #1 carried all four phases.** The designated branch for this work is
  `claude/job-agent-core-setup-07xswl`, which *is* PR #1's head, so the hardening
  commits could not land anywhere else. The diff being merged was no longer the
  one that existed when Phase 1 was written.

I put that to you rather than assuming, and you chose to merge. PR #1 was marked
ready and merged into `main` as `70756bc` — 133 files, 19 commits, four phases.

**Correction to what I first told you.** I said PR #2 was "closed, not merged".
That was what the API returned when I queried it, but I queried it *before* the
push — `merged:false` was true at 12:38 and false by 12:45:57. The webhook that
arrived afterwards said `outcome: merged`, I re-checked against the API, and the
API now agrees. So both PRs merged; only the branch deletion below is
outstanding.

**One thing did not get done: `fix/windows-bringup` is still on the remote.**
It is the head of a merged PR, so it is safe to remove.
Deleting a branch over `git push` fails in this container — the proxy drops the
connection on a delete-ref (`send-pack: unexpected disconnect while reading
sideband packet`), and it failed on both attempts. `feat/core` is already gone.
Both remaining branches are fully contained in `main`, verified with
`git merge-base --is-ancestor`, so deleting them loses nothing:

```
fix/windows-bringup                 fully contained in main
claude/job-agent-core-setup-07xswl  fully contained in main
```

Delete them from the GitHub branches page when convenient.

## Hardening — Phase 2: the parse gate now checks facts, not a keyword list

### What was actually wrong

The gate used to be handed `claimed_keywords = profile.skills[:12]` and nothing
else. Its entire coverage was *the first twelve skills*. That is why a resume
shipped with `C#` typeset as the literal `C\#`: the keywords in play contained
no character the escaper could corrupt, so the check passed on a document an ATS
could not read.

Assuming more blind spots existed turned out to be right. A hostile profile —
`C++ C# .NET F# R&D AT&T`, `50% $80k 30% ~5 years`, ampersands in employers,
em/en dashes, curly quotes, `José Müller`, `Ångström`, `data_pipeline_v2`,
`issue #42`, `config{nested}`, a 250-character wrapping bullet — was compiled
through the real template with real pdflatex, extracted with **both** extractors,
and every intended string diffed against the extracted text. Three bugs fell out.

### Bug 1 — ASCII apostrophes were retyped as curly quotes

LaTeX's `'` ligature produces U+2019. Typographically correct; for a resume it
is a corruption, because the profile is the source of truth for facts and an ATS
matching `Dan Murphy's` against `Dan Murphy’s` finds nothing.

### Bug 2 — hyphen runs were retyped as dashes

`--` becomes an en dash, `---` an em dash. Same failure: `2020--2024` extracts
with a character the user never typed.

Both fixed in `backend/documents/latex.py` by pinning ASCII through sentinels
applied **before** `_UNICODE_FIXUPS`, so the deliberate asymmetry survives: a
real U+2019 or U+2014 pasted from Word is still mapped into ligature source and
still round-trips back to itself. Only ASCII the user typed is held to the
letter.

Before and after, same profile, same template, same pdflatex:

```
BEFORE                                       AFTER
------------------------------------------   ------------------------------------------
said "hello" plainly                         said "hello" plainly
it’s a plain apostrophe                 <--  it's a plain apostrophe
a < b and c > d                              a < b and c > d
100% ^ 2 ~ 3                                 100% ^ 2 ~ 3
back\slash literal                           back\slash literal
a_b^c                                        a_b^c
flow off finally                             flow off finally
10 – 20 and 30 — 40                     <--  10 -- 20 and 30 --- 40
```

Two of eight strings were silently different before the fix; zero after.

### Bug 3 — ligature canaries were harvested from LaTeX comments

`no_ligature_corruption` scans the rendered `.tex` for canary words and asserts
they survive extraction. It scanned the whole file, including comments — and the
shipped `resume.tex.j2` explains the ligature rule in a comment containing the
word *efficient*. A comment never typesets, so the gate was asking every
document to contain a word that no correct build could produce.

**Every real resume would have failed this check.** The suite never caught it
because its fixture profile was written around the canary list ("Efficient
financial reporting and certification workflow design") rather than the other
way round — the fixture satisfied the bug instead of exposing it.

Fixed with `_strip_latex_comments` in `verify.py`, applied to `source_text`
before the canary harvest. Escaped `\%` is content and is preserved.

### The main change — the gate checks the actual intended strings

`ParseExpectations` gained `verbatim: list[str]`, checked by a new
`verbatim_facts_present` check, and `expected_verbatim(profile)` in `build.py`
harvests it from the profile rather than from anyone's memory: skills,
employers, role titles, institutions, qualifications, certifications, issuers,
project names, plus any token inside a highlight bullet carrying a character the
escaper can mangle (`&#%$_~^{}+<>'"\`). Wired into the RESUME and COMBINED
expectations.

Whole bullets are deliberately *not* asserted verbatim — LaTeX may hyphenate a
wrapped line, which is formatting, not corruption. Tokens are.

Old gate versus new gate, on one PDF whose underscore escaping is doubled so
`data_pipeline_v2` typesets as the literal `data\_pipeline\_v2`:

```
OLD GATE (claimed_keywords = profile.skills[:12]) -> report passed=True
                                                     "all 8 present"
NEW GATE (24 harvested facts)                     -> report passed=False
   verbatim_facts_present: stated in the profile but not extractable:
   ['data_pipeline_v2']
```

No skill contains an underscore, so the keyword list saw nothing wrong and
signed off on a broken document. That is the failure mode the harvest removes.

### Extracted text, hostile profile, after all three fixes

```
José Müller-Ångström
C++ / C# Engineer
jose.muller@example.com · +61 412 345 678· Adelaide SA 5000· linkedin.com/in/josemuller
SUMMARY
Built .NET and C++ systems; 50% faster, 30% uplift, ~5 years in R&D.
SKILLS
C++, C#, .NET, F#, R&D, AT&T systems, 50% automation, $80k budgets
EXPERIENCE
Senior Engineer — Platform 2022 – Present
Smith & Wesson Pty Ltd , Adelaide SA
• Cut latency 50% and delivered 30% uplift across the C++ core
• Owned data_pipeline_v2 and closed issue #42 with config{nested} overrides
• Negotiated $80k of tooling spend over ~5 years — em—dash, en–dash, curly’quote inside
• A deliberately very long single-line bullet that runs on and on to force LaTeX to wrap it across
multiple lines in the output so that we can confirm the extractor still returns the whole sentence
intact without dropping or reordering any of the words in the middle of the wrapped region
Engineer 2019 – 2022
Ångström Labs
• Shipped F# services for AT&T integrations
PROJECTS
data_pipeline_v2 (C# / .NET)
Handles curly“double”quote and config{nested} shapes at 50% cost.
EDUCATION
BSc Computer Science 2019
José Müller & Co Institute
CERTIFICATIONS
• AWS Certified — Developer — AT&T Training (2023)
WORK RIGHTS
Australian citizen — full working rights.
```

All 20 intended strings present, in both pypdf and pdfplumber. The long bullet
comes back word-for-word once line breaks are collapsed.

One thing in that output is *not* a bug: `2022 – Present` uses an en dash
because the template itself writes `--` between the dates. That is
template-authored typography, not profile data — the profile says `2022` and
`Present`, and both survive as themselves.

### Regression tests

`tests/test_hostile_documents.py`, 44 tests. Both corpora are kept as data, so
adding a newly-suspect string is one list entry:

* escaping in isolation, fast, no pdflatex — apostrophes, hyphen runs, Unicode
  round-trip, and a tripwire asserting that double-escaping stays *visible*
  (if a second pass ever became a no-op, the C# class of bug would stop being
  detectable)
* a parametrised check that no escaped fact leaks a raw `& % $ # _`
* compile-and-diff for both corpora, against both extractors, every string
* the wrapped bullet, checked for reordering and truncation
* the harvester's contents and determinism
* the gate catching the exact `C#` bug that shipped, and the curled-apostrophe
  bug, and the underscore bug the old keyword list waved through
* comment-stripping, including that `\%` is content
* a profile that never uses a canary word still passing the gate — Bug 3's
  regression

Each was confirmed to fail with its fix reverted, so none of them passes
vacuously. Full suite: **410 passed**.

## Hardening — Phase 3: the resolver answered 14 questions it should have refused

Screening answers are the most dangerous output in the system: a wrong one is a
false statement about work rights, licences or salary, made to an employer under
your name, with no undo. So the rule for this phase was that **any case where it
answers instead of abstaining is a bug in the resolver**.

Fifty-three adversarial cases in the first round. Fourteen came back wrong.

| class | asked | stored | answered |
| --- | --- | --- | --- |
| qualifier | "available for **part-time** work?" | "available for **full-time** work?" | Yes (89) |
| negation | "Do you **NOT** require visa sponsorship?" | "Do you require visa sponsorship?" | No (94) |
| negation | "Do you **not** require **no** visa sponsorship?" | same | No (90) |
| negation | "Are you **unable** to work full-time?" | "Are you **able** to work full-time?" | Yes (93) |
| negation | "Do you have **no** criminal convictions?" | "Do you have **any** criminal convictions?" | No (94) |
| negation | "Can you **not** start immediately?" | "Can you start immediately?" | Yes (93) |
| negation | "Do you **lack** full working rights?" | "Do you **have** full working rights?" | Yes (90) |
| unit | "How many **months** of Python experience?" | "How many **years** ...?" | 5 (89) |
| unit | "How many **years** ...?" | "How many **months** ...?" | 60 (89) |
| unit | "How many **days** notice?" | "How many **weeks** notice?" | 4 (89) |
| duplicate | two fuzzy rows, one verbatim | 5 and 7 | 5 (100) |
| duplicate | two EXACT rows, same pattern | 2 weeks and 4 weeks | 2 weeks (100) |
| duplicate | a regex row and a fuzzy row | 120000 and 140000 | 140000 (100) |
| degenerate | a form label of `"a"` | any question | Yes (100) |

A second round found three more: a compound question ("What is your notice
period, **and** what is your salary expectation?") answered from an entry
covering only the first half, and two substituted subjects — "**Ruby**" for
"Python" at 91, "**Master's**" for "Bachelor's" at 90.

### Why raising the threshold would not have worked

Every one of those scored **above** the 88 fuzzy threshold. They are not weak
matches, they are confident wrong ones — screening questions are short and share
most of their words, so the wrong question is already 90% similar to the right
one. Raising the threshold to 95 would have discarded the misspellings the fuzzy
tier exists to absorb and still let "part-time"/"full-time" through at 88.9 only
by accident of that one pair's arithmetic.

So the fix disqualifies a row outright rather than scoring it lower. Four
checks, in `backend/apply/answers.py`:

**Negation count.** Counted, not parity-checked: "not require no sponsorship" is
grammatically a double negative, but treating it as the plain form means
answering a question nobody can reliably parse. Any difference in count
disqualifies the row. Word boundaries matter — "notice" must not read as "no".

**Qualifier families.** Eight sets of mutually exclusive terms (employment basis,
time unit, rate basis, licence class, shift, distance unit, bound, work
eligibility). Naming a different member of the same family is a different
question. This generalises the two hardcoded polarity pairs that were already
there. It is a curated list and therefore incomplete by construction — it is the
second line of defence, not the first.

**Clause count.** How many questions are being asked at once, counted by segment
rather than by opener: "How many years of Python experience **do you have**?"
contains two interrogative openers and is one question, while "... in Australia?
Please attach evidence." is one question followed by an instruction.

**Substitution.** The general case, which no curated list reaches. An *addition*
is fine — "...working rights **in Australia**?" against a stored "...working
rights?" is the same question with more words. A *substitution* is not: when each
side has content words the other lacks and those leftovers are not spellings of
one another, the two questions are about different things. "licence"/"license"
scores 86 and "austrlia"/"australia" 94; "ruby"/"python" scores 18. The gap is
wide enough for one threshold.

### The tiering also had to go

The exact tier used to short-circuit: **any** row whose text happened to equal
the question — including a row the user never declared EXACT — was promoted to
tier 1 and returned immediately, so a second, contradictory entry was never
consulted. Two contradictory entries mean the bank is wrong, and answering from
it puts a stale number on a real application.

Now there is one candidate pool. Regex hits and fuzzy hits compete together, and
if the candidates within `AMBIGUITY_MARGIN` of the winner disagree, it abstains.
The one remaining short-circuit is a row the user explicitly declared
`MatchType.EXACT` — that is a deliberate override, and it wins over a
disagreeing fuzzy row but not over another EXACT row.

Two smaller fixes in the same area: a row scoped to a **different** campaign is
now discarded rather than merely outranked (ranking it equal made two unrelated
campaigns look like a contradiction), and a question shorter than 8 characters or
2 words is rejected before the fuzzy tier, because `partial_ratio` scores any
substring at 100 and a form label of "a" matched a full question perfectly.
`partial_ratio` itself is kept: a stored pattern of "notice period" shares only
23% of its length with "What is your notice period if you were to accept an
offer?" and must still match.

### What the guards cost — measured, not assumed

Thirty-four realistic phrasings of the shipped seed questions, taken from how
Seek and LinkedIn actually word them:

```
guards off  ->  28/34 answered (82%)
guards on   ->  26/34 answered (76%)
```

The two it newly abstains on are synonym swaps: "commence" for "start", "living"
for "residing". Structurally those are identical to "Ruby" for "Python" —
each side has a content word the other lacks and they are not spellings of one
another — so no rule that catches the second can pass the first.

That is the accepted cost, and it is a self-correcting one: an abstention parks
the job, asks you once over Telegram, saves the answer, and the phrasing resolves
exactly from then on. A guess is not recoverable. All 21 seeded questions still
resolve against their own patterns, and the seeded visa/sponsorship pair still
refuses to answer from each other.

### Regression tests

`tests/test_answers_adversarial.py`, 107 tests — every case above, plus the
surface-noise cases that must NOT abstain (casing, whitespace, numbering,
bullets, typos, HTML tags, curly apostrophes) and the degenerate ones that must
(empty, whitespace, `None`, a single character, 5000 characters of noise, French,
Chinese, Arabic, emoji, fullwidth Latin). It is a separate file from
`test_answers.py` on purpose: that one shows the resolver working, this one tries
to make it lie.

Full suite: **517 passed**.

## Hardening — Phase 4: DRY and dead code

### Adding a job board took seven files

That was the specific complaint, and it was accurate. A board's identity was
spread across:

| file | what it held |
| --- | --- |
| `discovery/run.py` | `build_sources()` — which boards are searched |
| `apply/session.py` | `PLATFORMS` — login URLs and signed-in selectors |
| `apply/run.py` | `build_appliers()` — which boards are applied to |
| `apply/canary.py` | `CANARY_PAGES` — the daily drift check's URLs |
| `apply/canary.py` | `WATCHED` — which selectors that check samples |
| `apply/guardrails.py` | `_WINDOW_POLICY` — weekdays-only or not |
| `apply/har.py` | `VARIANTS` — flow shapes worth recording |
| `discovery/contacts.py` | domains that are the board, not the employer |
| `integrations/matching.py` | the same list again |

Nine sites, in seven files. Every one of them fails **quietly** when missed: a
board that discovers jobs and never applies to them, an apply pass running
LinkedIn's strict weekday policy against Seek, a canary watching selectors that
no longer exist.

Now there is `backend/boards.py`, and a board is one entry there plus its
adapter file. Every table above is derived from it. `tests/test_architecture.py`
parses the AST of every backend module and fails if a board key appears outside
the registry and the adapters — parsing rather than grepping, because several of
these files legitimately discuss board names in prose, and `"linkedin"` is also
a **profile field** (the URL on the resume), which is why the documents layer is
exempt by name.

The factories in the registry import lazily, and that is load-bearing rather
than stylistic: discovery is HTTP only and must never pull the apply layer —
which touches a live browser session — into its import graph. Two tests pin it,
one on the discovery package's imports and one that imports `backend.boards` in
a subprocess and asserts no `backend.apply` module was loaded.

### The two domain lists had already drifted

`discovery/contacts.py` and `integrations/matching.py` each kept a hand-written
list of "domains that belong to a platform rather than an employer", plus an
identical `domain == x or domain.endswith("." + x)` check. The lists were not
the same: the contact scraper knew about BambooHR, Glassdoor, ZipRecruiter and
applytojob; the reply matcher did not.

That is not a tidiness problem. The same address was platform plumbing in one
module and a real employer in the other, which is how an outbound cover letter
gets addressed to an ATS robot.

Both now call `boards.is_platform_domain()`, which assembles the list from the
two registries that already hold the data — `BOARDS` and `ATS_REGISTRY` — plus a
short explicit list of vendors we recognise but have no adapter for. A
parametrised test asserts both modules agree on twenty domains, and another
asserts every ATS the detector can identify is also recognised in mail.

### A safety check that was defined and never called

`session.has_restriction_notice()` existed, `BoardSession.restriction_notice`
held selectors for both boards, and nothing ever called either. LinkedIn's
applier had its own `detect_restriction` with its own copy of the selectors —
already drifted on the wording of the identity-verification notice — and Seek's
applier had no restriction check at all.

**A suspended Seek account would have kept receiving applications.**

`flow._restricted()` now falls back to the registry when an adapter defines no
detector, and LinkedIn's detector reads the registry rather than a second copy.
One list of selectors, one implementation, both boards covered.

### Removed

| what | why |
| --- | --- |
| `documents/engine.py::_BLOCK_RE` | compiled, never used |
| `documents/engine.py::undeclared_variables` | no caller in backend, tests or frontend |
| `ats/detect.py::platform_by_key` | no caller |
| `integrations/notify.py::describe_job` | no caller |
| `integrations/gmail.py::parse_rfc822` | a **third** message parser; neither reader used it — IMAP goes through imap-tools, OAuth through the Gmail API payload |
| `frontend/src/lib/hooks.ts::formatScore` | `ScoreBadge` already owns null-handling and formatting |
| `email-validator` | no `EmailStr` anywhere |
| `python-multipart` | no `UploadFile`, `File(...)` or `Form(...)` endpoint |
| `pytest-asyncio` | no async test, no `asyncio_mode` setting |
| `respx` | never imported; the HTTP tests stub at the client |

`tzdata`, `uvicorn` and `ruff` are unimported by design — a Windows-only data
package, a server entry point and a linter — and `lxml` is BeautifulSoup's
parser backend. All kept.

### Wired instead of removed

Two functions were dead because a duplicate of them existed inline:

* `gmail.default_since()` — `inbound.py` restated `timedelta(days=7)` itself
* `har.missing_recordings()` — the `har list` CLI recomputed the same thing

### The blocker: the form-map cache is not connected

The whole of `backend/ats/formmaps.py` — fingerprinting, the platform/company
tiers, `merge_maps`, the trust-after-three-successes rule — is built, tested and
called by **nothing outside its own test file**. `save_map` has no production
caller. `ats/generic.py` asks the LLM to map the form on every single
application instead of consulting the cache, which is the cost saving the tier
system exists to deliver.

`SHAREABLE_PLATFORMS` in `ats/adapters.py` is the tier policy for that unwired
path. It is left in place deliberately: deleting it would erase the decision
about which platforms are safe to share a learned map for, and the code that
should read it does not exist yet.

Connecting it means changing `generic.py` to fingerprint the form, load a
trusted map, fall back to the LLM on a miss, and save what it learns — against
an ATS this container cannot reach. That is a feature, not a cleanup, so it is
reported rather than guessed at. **Your call whether it is worth doing before
the first real run.**

### Also noted, not changed

`npx oxlint src` reports four pre-existing `set-state-in-effect` warnings
(Settings, Campaigns, Profile, Templates). All four are the same shape: a form
draft initialised from fetched data. Not errors, not touched here.

### Verification

```
uv run ruff check backend tests   All checks passed!
uv run pytest -q                  545 passed
npx tsc --noEmit                  clean
npx oxlint src                    0 errors, 5 pre-existing warnings
```

## What needs you

1. **Delete `fix/windows-bringup` and `claude/job-agent-core-setup-07xswl`** from
   the GitHub branches page. Both are fully merged into `main`; branch deletion
   over `git push` is blocked in the build container.
2. **Run the whole thing on Windows.** Nothing here proves it works there.
3. **Verify Seek discovery** — the `verify_seek` command above. Highest value
   outstanding: discovery is the top of the funnel and is entirely unproven
   against a live site.
4. **Decide whether to connect the form-map cache** before the first real run —
   see the Phase 4 blocker above. Every application currently pays for a fresh
   LLM form-mapping call.
5. **Add `GEMINI_API_KEY`** to `.env` if you want the new routing used.
6. **Decide on 2.5 vs 3.1 Flash-Lite** before 2026-10-16.

---

# Windows bring-up — 2026-09-01 (first real machine, first real network)

Everything below either ran on this machine or is marked as not done. Phases 1
and 2 are complete; Phase 3 is complete except for the two inputs I must not
invent (see "What is substituted in Phase 3").

## Platform

```
platform.system() = Windows      Windows 11 Home Single Language 10.0.26200
python  3.14.7 (C:\Python314)    node v24.20.0 / npm 11.19.0
git     2.55.0.windows.5         MiKTeX 25.12 (pdfTeX 4.23)
```

## Phase 1 — Environment: DONE

### Missing on arrival, and what was done

| Tool | State | Action |
|---|---|---|
| `uv` | absent | installed -> 0.12.7 |
| MiKTeX / `pdflatex` | absent | installed (winget, user scope) -> 25.12 |
| Google Chrome | absent | **NOT installed** |
| Playwright browsers | absent | **NOT installed** |

```
py -3 -m pip install --user uv
winget install --id MiKTeX.MiKTeX -e --scope user --silent --disable-interactivity --accept-package-agreements --accept-source-agreements
winget install --id Google.Chrome -e --silent --accept-package-agreements --accept-source-agreements
uv run playwright install chrome
```

Chrome and the Playwright browsers are apply-layer only, and discovery is HTTP,
so Phases 1-3 did not need them. They are the next prerequisite for any apply
work.

**Neither `uv` nor MiKTeX is on PATH:**

```
C:\Users\mohdi\AppData\Roaming\Python\Python314\Scripts
C:\Users\mohdi\AppData\Local\Programs\MiKTeX\miktex\bin\x64
```

That is not cosmetic — it silently disabled the whole document test suite (below).

### MiKTeX had to be made non-interactive before it was safe to run unattended

```
initexmf --set-config-value "[MPM]AutoInstall=1"
```

Without it pdflatex **waits for a GUI confirmation** the first time it needs a
package. I also pre-installed the packages the templates use so no build pays
for an on-demand fetch. Note `mpm --install=A --install=B` aborts at the first
already-installed package; install one at a time.

`uv sync`, `npm install` and `alembic upgrade head` all succeeded first try.

### Test suite: 545 tests, 5 real failures, all fixed

1. **`test_check_can_submit_is_called_from_exactly_one_place`** — `str(PurePath)`
   gives backslashes on Windows, compared against a POSIX literal. This is the
   hard-rule-6 test proving every submit path goes through the guardrail; it
   would have been red on every Windows run for an unrelated reason.
2. **/ 3. `test_app_refuses_to_serve_the_browser_profile`** — conftest redirected
   `DATA_DIR` but not `BROWSER_PROFILE_DIR`. `.env.example` ships that key set,
   so with a real `.env` the two paths stopped overlapping and the guard had
   nothing to catch. I verified the guard itself is correct when driven with
   production-shaped config; the bug was entirely in the test wiring.
4. **/ 5. scoring model tests** — `.env.example` shipped `claude-opus-5` for
   scoring and classification while `config.py` defaults to
   `gemini-3.1-flash-lite`. **Copying `.env.example`, the documented setup path,
   silently reinstated the ~17x cost configuration** that the earlier cost work
   existed to remove. Fixed the example, and made the "shipped default" test
   price `Settings(_env_file=None)` so it tests what ships rather than the local
   file.

### The worse bug those were hiding: the document suite was silently skipping

`test_build_e2e`, `test_parse_gate` and `test_hostile_documents` all guarded on
`shutil.which("pdflatex")`. The application does not use PATH — it runs
`settings.pdflatex_path`. Because MiKTeX's per-user install is not on PATH,
**the entire LaTeX and parse-gate suite skipped while pdflatex was installed and
working.** Green, with the document pipeline completely unexercised. On Linux
this never showed because TeX Live lands in `/usr/bin`.

Replaced with one shared `needs_pdflatex` marker in `conftest.py` that resolves
`settings.pdflatex_path`. `test_parse_gate.py` alone went from 0 to 12 running
tests. `compile_tex` in that module also had to stop calling a bare `"pdflatex"`,
or the tests would run and then die on `FileNotFoundError`.

### `render_pdf` could not be timed out — FIXED

The first full run **hung for over ten minutes inside a call passing
`timeout=120`**. `subprocess.run(capture_output=True, timeout=N)` does fire and
does kill pdflatex — but MiKTeX spawns a package installer, that grandchild
inherits the stdout pipe, and `run` then calls `communicate()` **again with no
timeout** to drain it. The pipe never reaches EOF while the installer holds the
write end, so the build blocks forever. The documented timeout is worthless in
exactly the situation it exists for.

`render_pdf` now redirects to a file instead of a pipe, which removes the
deadlock at the root — no reader threads, no inherited pipe ends, so `wait()`
returns when pdflatex exits whatever its children are doing. `stdin` is
`DEVNULL` so nothing can block on a console read, and the timeout kills the
whole process tree (`taskkill /T` on Windows, `killpg` on POSIX). The per-pass
budget is now `LATEX_TIMEOUT_SECONDS` (default 180) rather than a literal.

Covered by `test_pdflatex_timeout_holds_when_a_child_outlives_the_process`,
which reproduces the exact shape: a process that hands its stdout to a
longer-lived child and then sleeps past the timeout.

### The 10-hour test run — RESOLVED, and it is not a clock bug

The suite self-reported `36807.61s (0:10:13:27)`. I flagged a possible
clock/timezone fault. **There is none.** Measured against network time:

```
python UTC now   2026-09-01T10:15:05Z      network Date hdr 2026-09-01T10:15:05Z
LOCAL CLOCK SKEW +0.1 s
local            2026-09-01T19:45:05+09:30  Australia/Adelaide, ACST, tzdata resolves
```

The real cause is in the Windows event log: **the machine slept twice during the
run.**

```
SleepTime 2026-08-31T19:51:10Z  WakeTime 2026-08-31T22:30:36Z   (2h 39m)
SleepTime 2026-09-01T02:36:01Z  WakeTime 2026-09-01T08:48:27Z   (6h 12m)
```

~8h51m of sleep against a 10h13m wall clock leaves ~1h22m of real work, which
matches the observed per-file timings. pytest measures elapsed wall time, which
keeps advancing across suspend.

**The operational consequence matters more than the diagnosis.** `Claude.md`
says "Machine stays awake and logged in". On this machine that is false — it
slept 8h51m in two days, and it slept again during a 5-second discovery run
later in this session. Assessed against the code:

* `scheduler.py` already handles it correctly: `coalesce=True`,
  `max_instances=1`, `misfire_grace_time=3600`. A job missed during a short
  sleep runs once on resume and does not stack up.
* **But a sleep longer than the 1-hour grace silently skips that run entirely,
  and one of the two sleeps was 6h12m.** Scheduled discovery would simply not
  have happened.
* `pacing.py` uses `time.sleep`, which only ever over-waits across suspend —
  conservative, cannot cause submitting too fast.
* The apply window is enforced at submit time (`guardrails.py`, rule 12
  `inside_window`), not at schedule time, so a late wake cannot submit outside
  09:00-17:00.

Nothing here needs a code change; it needs a decision about the power settings.

### Frontend / backend serving: both confirmed

Started, checked and stopped again. No errors in either log.

```
uvicorn backend.main:app --host 127.0.0.1 --port 8010     ready in 3s
  GET /health -> {"status":"ok","version":"0.1.0","allow_live_submit":false,
                  "database":"connected","time":"2026-09-01T11:20:07Z"}
  GET /docs   -> 200

npm run dev -- --port 5183                                Vite 8.2.2, ready in 3.7s
  GET /       -> 200, <title>JobSeekr</title>, #root present
```

Note `allow_live_submit: false` is served by the live app, not just the file.

Neither was left running. `pkill` does not reach Windows processes from the
bash side — `taskkill /T /F /PID` against the listening PID is what actually
stops them, which matters for anything scripted around these servers later.

## Phase 2 — Seek, LinkedIn, Indeed: DONE

### Reachability (all previously blocked)

```
https://www.seek.com.au/   200
https://www.linkedin.com/  200
https://au.indeed.com/     403   Cloudflare, for plain httpx
```

### Seek had moved, and every configured endpoint was dead

`verify_seek` correctly reported NOTHING WORKED. Causes:

1. **`www.seek.com.au` 308-redirects to `au.seek.com`.**
2. `/api/chalice-search/v5/search` and `/v4/search` both **404** on the new host.
3. The HTML fallback fetched but parsed 0 jobs.

**Confirmed working endpoint:**

```
https://au.seek.com/api/jobsearch/v5/search
  siteKey=AU-Main  sourcesystem=houston  keywords=...  where=...
  page=1  pageSize=20  locale=en-AU
-> 200 application/json
-> keys: data, facets, info, location, searchParams, solMetadata, sortModes, suggestions, totalCount
```

Jobs are in `data[]`. All three strategies now work: JSON API 10 jobs, page
state 10 jobs, JSON-LD 0 (Seek publishes none on the search page).

### Mapping defects found against the real payload, all fixed

* **`location` was `None` for every ad.** Records carry `locations[0].label` — a
  *list* — and no alias matched. `_dig` now indexes lists so a dotted path
  reaches it.
* **Records carry no `url` at all**; it is built from the id, now against the
  configured base host instead of one that 308s on every fetch.
* **The HTML fallback recovered nothing** because the page-state blob keeps jobs
  at `results.results.jobs`, and the scan only looked at top-level keys. Record
  locations are now one list of dotted paths shared by both strategies.
* **Descriptions were the one-sentence teaser.** `bulletPoints` carries the
  actual requirements and was unused; both are kept now, which materially
  improves what scoring sees.
* **The client advertised `br` encoding with no brotli decoder installed.** Seek
  answers gzip so it never bit, but a server honouring `br` would have returned
  a body this client cannot read. Now advertises only what httpx can decode.

### Not a bug, though it looks exactly like one

Non-ASCII survives end to end: an en-dash in a Seek title is `U+2013` in the
`RawJob`. The mojibake I first saw was my console rendering as cp1252
(`sys.stdout.encoding == 'cp1252'`). structlog console output does mangle
non-ASCII the same way, but it substitutes rather than raising, and the database
stores correct UTF-8. Display-only.

### Discovery run: 13 genuine Adelaide jobs in SQLite

Campaign "Adelaide bring-up" (`python developer`, `data engineer`,
`software engineer` / Adelaide SA).

```
seek:     fetched 13  new 13  duplicate 0   error 0
linkedin: fetched  0  new  0  duplicate 0   error 1
indeed:   fetched  0  new  0  duplicate 0   error 1
```

A second run correctly reported `new 0, duplicate 13`.

**Also fixed: the discovery CLI crashed on exit** with
`DetachedInstanceError`. `session_scope` commits on the way out, a commit
expires every tracked instance, and `main`'s first read of `run.ok` then
lazy-loaded against a closed session. The run itself had already succeeded, so
this turned every successful discovery into a non-zero exit and a traceback.

### jobspy: works, but NOT on this machine's Python

Both LinkedIn and Indeed failed with:

```
OverflowError: cannot convert longdouble infinity to integer
  numpy/core/getlimits.py, computing float128 limits at import
```

**`python-jobspy==1.1.82` hard-pins `numpy==1.26.3`, which cannot import on
Python 3.14.** Adding a `numpy>=2.3` floor makes the project unresolvable:

```
Because python-jobspy>=1.1.82 depends on numpy==1.26.3 and your project
depends on numpy>=2.3.0, we can conclude that [they] are incompatible.
```

So this is not fixable by pinning. I reverted the attempt and did **not** fight
it further. To confirm it is purely a Python-version problem I built a
throwaway 3.12 environment and ran both boards for real:

```
python 3.12.14 + numpy 1.26.3 -> jobspy imports OK
linkedin: 15 rows in 8.0s     WORKS
indeed:   15 rows in 1.5s     WORKS  -- gets past the Cloudflare 403
```

**jobspy does get past Indeed's Cloudflare block** (it uses Indeed's own API
rather than the HTML site, so the 403 that plain httpx sees is irrelevant).
Neither board was rate-limited at this volume.

Options, all yours to choose:
1. Run the project on Python 3.12 (`uv python install 3.12`, resync). Everything
   resolves; costs re-verifying this session's work on a different interpreter.
2. Wait for a jobspy release that unpins numpy.
3. Drop jobspy and write direct adapters.

Until one is chosen, **LinkedIn and Indeed discovery do not work on this
machine** and Seek is the only live source.

### Dedupe verified against real cross-board duplicates

105 real LinkedIn + Indeed rows exported from the 3.12 environment and run
through the real `normalize_job` and `find_duplicate` against the 13 real Seek
rows in SQLite.

```
incoming rows considered : 105
flagged as duplicates    :  24
unique after dedupe      :  81
```

Genuine **cross-board** catches (not just same-board repeats from overlapping
search terms):

```
[linkedin] TSPV Software Engineer      @ TechThinking Talent  -> [seek]     TSPV Software Engineer
[indeed]   Software Developers         @ BAE Systems          -> [linkedin] Software Developers
[indeed]   Applications Developer      @ Journey Beyond       -> [linkedin] Applications Developer
```

Cross-checked against ground truth in the raw feed (`software engineer i @
flywire` appears 5 times, `senior developer, fullstack - python @ appnovation`
3 times, and so on). Dedupe behaves correctly on real data.

## Phase 3 — Real documents: DONE (with two substitutions)

### What is substituted in Phase 3, and why

Two inputs were missing and I would not invent either:

1. **There is no Profile in the database.** Hard rule 1 forbids inventing facts
   about you, so the builds use the repo's own synthetic test persona
   ("Jordan Fitzgerald", from `tests/test_build_e2e.py`) written to a
   **throwaway database** — no fabricated profile touches your real data.
2. **There is no LLM API key** (`OPENAI`/`ANTHROPIC`/`GEMINI` all unset), so the
   four AI slots use fixed stand-in prose.

**The job ads are real** — real titles, companies, locations and descriptions
pulled from Seek in Phase 2. What Phase 3 verifies is the LaTeX pipeline and the
parse gate, and those are unaffected by whose name is on the page. The prose
below is not a writing sample.

### Three genuinely different real jobs

| # | Title | Company | Why different |
|---|---|---|---|
| 1 | Artificial Intelligence Specialist | Flinders University | university / research sector |
| 2 | Spot Trader | GreenPoint Energy | energy trading, not a software role |
| 3 | TSPV Software Engineer | TechThinking Talent | defence, security-cleared, via recruiter |

### All nine PDFs built and gated

**Job 1 — Artificial Intelligence Specialist @ Flinders University** (Bedford Park, Adelaide SA)  
`https://au.seek.com/job/94332305`

| artifact | gate | pages | extracted chars | checks |
|---|---|---|---|---|
| resume | PASS | 1 | 510 | 14 |
| cover_letter | PASS | 1 | 470 | 9 |
| combined | PASS | 2 | 981 | 11 |

**Job 2 — Spot Trader @ GreenPoint Energy** (Adelaide SA)  
`https://au.seek.com/job/94333053`

| artifact | gate | pages | extracted chars | checks |
|---|---|---|---|---|
| resume | PASS | 1 | 510 | 14 |
| cover_letter | PASS | 1 | 445 | 9 |
| combined | PASS | 2 | 956 | 11 |

**Job 3 — TSPV Software Engineer @ TechThinking Talent** (Edinburgh, Adelaide SA)  
`https://au.seek.com/job/94335227`

| artifact | gate | pages | extracted chars | checks |
|---|---|---|---|---|
| resume | PASS | 1 | 510 | 14 |
| cover_letter | PASS | 1 | 458 | 9 |
| combined | PASS | 2 | 969 | 11 |

9/9 passed. Check counts are 14 (resume), 9 (cover letter), 11 (combined),
including the new one added below.

## The gate's fourth blind spot, found by reading the extracted text

Every one of the nine PDFs passed, so the gate had nothing to say. Reading the
text is what found it:

```
jordan.fitzgerald@example.com  +61 412 345 678  Adelaide SA 01 September 2026
```

**The letter's date was being absorbed into the contact line.** An ATS reads
that block positionally — the line carrying the email is where it expects the
phone and the location, and it takes the trailing run as the location. So the
location field became `Adelaide SA 01 September 2026`. All eight checks passed,
on all three cover letters, and on the cover-letter page of all three combined
PDFs.

**Root cause was not layout.** The template puts the date in its own paragraph:

```
BLOCK-if profile.location ... profile.location BLOCK-endif
<blank line>
today.long
```

but `backend/documents/engine.py` builds its Jinja `Environment` with
`trim_blocks=True`, which **eats the newline following a block end tag**. The
blank line was consumed, LaTeX saw one paragraph, and the two joined. Generated
`.tex` confirmed it:

```
jordan.fitzgerald@example.com
 $\cdot$ +61 412 345 678 $\cdot$ Adelaide SA
01 September 2026
```

Fixed with an explicit `\par`, which ends the paragraph regardless of what
trimming does to the surrounding newlines. (The first attempt at the fix broke
the build: a LaTeX comment is not a Jinja comment, so naming a block tag in the
explanatory comment was parsed as a real tag.)

### New gate check: `contact_line_uncontaminated`

Asserts no date shares a line with the email. Two things it has to get right,
both learned by watching it fail:

* **It checks every line carrying the email, not the first.** `combined.pdf` has
  two contact blocks and the resume's is clean — checking only the first match
  passed the contaminated cover-letter page. Combined is the artifact attached
  wherever a form has a single upload slot, so that was the worst place to miss.
* **The year is optional after a month name.** The contact line can wrap: a
  slightly longer address pushes `2026` onto the next extracted line, leaving
  `... Adelaide SA 01 September` behind — just as corrupt, and the first version
  of the check missed it. Month names are spelled out rather than matched as
  `[A-Z][a-z]+`, so `12 Regent Street` does not read as a date.

Three regression tests: contaminated rejected, corrected shape accepted, and the
combined-document case where a clean contact line precedes the contaminated one.

### Also observed in the extracted text, deliberately NOT turned into checks

* `Senior Analyst 2021 - 2026` — title and date range share a line via `\hfill`.
  This is conventional resume layout that ATS parsers expect; a check would be
  over-fitting.
* Bullets extract as `U+2022`, though `resume.tex.j2`'s comment claims they are
  plain text lines (`label={}` is not actually set). Harmless — parsers strip
  bullet glyphs — but the comment is misleading.
* Date ranges use an en-dash (`U+2013`). Some ATS date parsers only handle
  hyphens. Real but speculative without a specific ATS to test against.

## Extracted text — all nine PDFs

Verbatim `pdfplumber` output, which is roughly what an ATS sees.

### Job 1 — Artificial Intelligence Specialist @ Flinders University

#### 1.resume

```
Jordan Fitzgerald
Data Analyst
jordan.fitzgerald@example.com · +61 412 345 678 · Adelaide SA
SUMMARY
Efficient financial reporting and certification workflow design.
SKILLS
Python, SQL, financial modelling
EXPERIENCE
Senior Analyst 2021 – 2026
Redgum Analytics, Adelaide
• Identified efficient financial reporting workflow improvements.
• Built qualified candidate certification review tooling.
EDUCATION
BSc Computer Science 2020
University of Adelaide
WORK RIGHTS
Australian citizen with full working rights.
```

#### 1.cover_letter

```
Jordan Fitzgerald
jordan.fitzgerald@example.com · +61 412 345 678 · Adelaide SA
01 September 2026
Hiring Team
Flinders University
Re: Artificial Intelligence Specialist
Dear Hiring Team,
The reporting and analysis work this role describes is the work I do now.
Python and SQL have been the core of my day to day work.
The ad describes a small team owning its own data platform, which is how I like to work.
I would welcome a conversation.
Kind regards,
Jordan Fitzgerald
```

#### 1.combined

```
Jordan Fitzgerald
Data Analyst
jordan.fitzgerald@example.com · +61 412 345 678 · Adelaide SA
SUMMARY
Efficient financial reporting and certification workflow design.
SKILLS
Python, SQL, financial modelling
EXPERIENCE
Senior Analyst 2021 – 2026
Redgum Analytics, Adelaide
• Identified efficient financial reporting workflow improvements.
• Built qualified candidate certification review tooling.
EDUCATION
BSc Computer Science 2020
University of Adelaide
WORK RIGHTS
Australian citizen with full working rights.
Jordan Fitzgerald
jordan.fitzgerald@example.com · +61 412 345 678 · Adelaide SA
01 September 2026
Hiring Team
Flinders University
Re: Artificial Intelligence Specialist
Dear Hiring Team,
The reporting and analysis work this role describes is the work I do now.
Python and SQL have been the core of my day to day work.
The ad describes a small team owning its own data platform, which is how I like to work.
I would welcome a conversation.
Kind regards,
Jordan Fitzgerald
```

### Job 2 — Spot Trader @ GreenPoint Energy

#### 2.resume

```
Jordan Fitzgerald
Data Analyst
jordan.fitzgerald@example.com · +61 412 345 678 · Adelaide SA
SUMMARY
Efficient financial reporting and certification workflow design.
SKILLS
Python, SQL, financial modelling
EXPERIENCE
Senior Analyst 2021 – 2026
Redgum Analytics, Adelaide
• Identified efficient financial reporting workflow improvements.
• Built qualified candidate certification review tooling.
EDUCATION
BSc Computer Science 2020
University of Adelaide
WORK RIGHTS
Australian citizen with full working rights.
```

#### 2.cover_letter

```
Jordan Fitzgerald
jordan.fitzgerald@example.com · +61 412 345 678 · Adelaide SA
01 September 2026
Hiring Team
GreenPoint Energy
Re: Spot Trader
Dear Hiring Team,
The reporting and analysis work this role describes is the work I do now.
Python and SQL have been the core of my day to day work.
The ad describes a small team owning its own data platform, which is how I like to work.
I would welcome a conversation.
Kind regards,
Jordan Fitzgerald
```

#### 2.combined

```
Jordan Fitzgerald
Data Analyst
jordan.fitzgerald@example.com · +61 412 345 678 · Adelaide SA
SUMMARY
Efficient financial reporting and certification workflow design.
SKILLS
Python, SQL, financial modelling
EXPERIENCE
Senior Analyst 2021 – 2026
Redgum Analytics, Adelaide
• Identified efficient financial reporting workflow improvements.
• Built qualified candidate certification review tooling.
EDUCATION
BSc Computer Science 2020
University of Adelaide
WORK RIGHTS
Australian citizen with full working rights.
Jordan Fitzgerald
jordan.fitzgerald@example.com · +61 412 345 678 · Adelaide SA
01 September 2026
Hiring Team
GreenPoint Energy
Re: Spot Trader
Dear Hiring Team,
The reporting and analysis work this role describes is the work I do now.
Python and SQL have been the core of my day to day work.
The ad describes a small team owning its own data platform, which is how I like to work.
I would welcome a conversation.
Kind regards,
Jordan Fitzgerald
```

### Job 3 — TSPV Software Engineer @ TechThinking Talent

#### 3.resume

```
Jordan Fitzgerald
Data Analyst
jordan.fitzgerald@example.com · +61 412 345 678 · Adelaide SA
SUMMARY
Efficient financial reporting and certification workflow design.
SKILLS
Python, SQL, financial modelling
EXPERIENCE
Senior Analyst 2021 – 2026
Redgum Analytics, Adelaide
• Identified efficient financial reporting workflow improvements.
• Built qualified candidate certification review tooling.
EDUCATION
BSc Computer Science 2020
University of Adelaide
WORK RIGHTS
Australian citizen with full working rights.
```

#### 3.cover_letter

```
Jordan Fitzgerald
jordan.fitzgerald@example.com · +61 412 345 678 · Adelaide SA
01 September 2026
Hiring Team
TechThinking Talent
Re: TSPV Software Engineer
Dear Hiring Team,
The reporting and analysis work this role describes is the work I do now.
Python and SQL have been the core of my day to day work.
The ad describes a small team owning its own data platform, which is how I like to work.
I would welcome a conversation.
Kind regards,
Jordan Fitzgerald
```

#### 3.combined

```
Jordan Fitzgerald
Data Analyst
jordan.fitzgerald@example.com · +61 412 345 678 · Adelaide SA
SUMMARY
Efficient financial reporting and certification workflow design.
SKILLS
Python, SQL, financial modelling
EXPERIENCE
Senior Analyst 2021 – 2026
Redgum Analytics, Adelaide
• Identified efficient financial reporting workflow improvements.
• Built qualified candidate certification review tooling.
EDUCATION
BSc Computer Science 2020
University of Adelaide
WORK RIGHTS
Australian citizen with full working rights.
Jordan Fitzgerald
jordan.fitzgerald@example.com · +61 412 345 678 · Adelaide SA
01 September 2026
Hiring Team
TechThinking Talent
Re: TSPV Software Engineer
Dear Hiring Team,
The reporting and analysis work this role describes is the work I do now.
Python and SQL have been the core of my day to day work.
The ad describes a small team owning its own data platform, which is how I like to work.
I would welcome a conversation.
Kind regards,
Jordan Fitzgerald
```

## What needs you

1. **Decide the jobspy question** — Python 3.12, wait for a release, or drop it.
   Until then LinkedIn and Indeed discovery do not run on this machine.
2. **Power settings.** The machine slept 8h51m in two days, and one sleep
   exceeded the scheduler's 1-hour misfire grace, which silently skips a run.
   `Claude.md` assumes it stays awake; it does not.
3. **Add `uv` and MiKTeX to PATH** (paths above).
4. **A real profile and an LLM API key** are the two things blocking genuine
   document generation. Everything else in that pipeline is now proven.
5. **Chrome + `playwright install chrome`** before any apply-layer work.
6. **One clean full-suite run.** Individual suites pass (545 collected; the five
   fixed tests, 20 portability, 72 document tests, 143 in the affected set), but
   the whole suite has not been run end to end since the changes.
7. **`.env`** was created from `.env.example` this session and corrected to the
   Gemini defaults. `ALLOW_LIVE_SUBMIT=false`, untouched.

## Explicitly not done, as instructed

No HAR recorded. No login to any job board. `ALLOW_LIVE_SUBMIT` never touched.

---

# macOS bring-up — 2026-09-02

Second bring-up, following `HANDOFF.md`. Read that first; this records what
actually happened, including where the handoff's expectations were wrong.

## Read this first: it did not run on macOS

**The session ran on Linux, not a Mac.** The agent was given a remote Ubuntu
24.04 container (`x86_64`, kernel 6.18), not the MacBook. Everything below is
real — the commands ran, the output is quoted — but it is Linux output.

What that does and does not cost you:

| | Status |
|---|---|
| POSIX process-tree kill (§4 of HANDOFF) | **Genuinely verified.** Same `os.killpg` code path on macOS and Linux. |
| Test suite, rehearsal, migrations, servers | Verified, on Linux. Nothing in them is macOS-specific. |
| `brew install --cask basictex` | **Never run.** No Homebrew here. |
| `tlmgr install ...` package list | **Never run.** TeX Live came from `apt`. |
| `/Library/TeX/texbin/pdflatex` | **Never exercised.** |
| `zoneinfo` resolving `Australia/Adelaide` from the system tz database | Not a macOS check — `tzdata` is installed on Linux too. |

So: the code is in better shape than it was, and the *Mac* is still unproven.
The one command you still have to run yourself is the BasicTeX install.

## Environment

```
uv 0.8.17 · CPython 3.12.3 · node v22.22.2 · npm 10.9.7
pdfTeX 3.141592653-2.6-1.40.25 (TeX Live 2023/Debian)
```

`uv venv --python 3.12`, `uv sync --all-groups`, `npm install` (53 packages,
0 vulnerabilities) and `alembic upgrade head` all ran clean, first try. The
3.12 pin held; nothing needed relaxing.

LaTeX came from `apt-get install texlive-latex-base texlive-latex-recommended
texlive-latex-extra texlive-fonts-recommended lmodern`. The templates need
exactly `lmodern`, `geometry`, `hyperref`, `fontenc`/`inputenc`, `enumitem` and
`titlesec`; all six resolved via `kpsewhich`. **On the Mac use the `tlmgr` list
in HANDOFF §4 instead** — that list is still the one to run, still untested.

## `PDFLATEX_PATH` — the handoff was half right

The instruction was "fix it in `.env` and `.env.example` — both carry a Windows
MiKTeX path". Neither turned out to be true here:

- **`.env` does not exist in a fresh clone.** It is gitignored, so nothing came
  across with the repo. Created it from `.env.example`.
- **`.env.example` already had `PDFLATEX_PATH=pdflatex`** — portable, correct on
  macOS and Linux both. The Windows path was only ever in the *comment* above it.

The real defect was that the comment named MiKTeX and gave a Windows example
only, which is what makes the next person think the value is wrong. Rewritten to
say the bare name works wherever pdflatex is on `PATH` (macOS BasicTeX/MacTeX,
Linux TeX Live) and to list the absolute paths for both platforms.

`ALLOW_LIVE_SUBMIT=false` in both files. Not touched.

## `.gitattributes` — added, and it is a no-op today

`* text=auto eol=lf`, plus `eol=crlf` for `.bat`/`.cmd`/`.ps1`, binary markers,
and `linguist-generated` on the two lockfiles.

Checked rather than assumed: `git add --renormalize .` produced **no** additional
changes, which confirms every tracked file is already LF. So this costs you
nothing now and prevents the whole-file diffs later.

## The 17-hour hang: the branch works, the test did not cover it

This is the most important result in this session.

**The POSIX branch works.** Under a deliberate reproduction — a process that
spawns a longer-lived grandchild on the inherited stdout, then sleeps past the
timeout — `_run_pdflatex` raised after 5.0s and the grandchild was dead.

**The shipped test could not have told you that.** `tests/test_windows_
portability.py::test_pdflatex_timeout_holds_when_a_child_outlives_the_process`
asserted only `elapsed < 60`. Suppressing `os.killpg` entirely and re-running it:

```
[control] suppressed killpg(pgid=3633, sig=9)
raised DocumentBuildError after 5.0s
grandchild pid=3634 alive_after_kill=True
```

Still under 5 seconds, still passing — with the process tree leaked. The prompt
return comes from the temp-file redirect (no pipe deadlock) plus
`process.kill()` reaping the direct child; the elapsed-time assertion never
touches `_kill_process_tree` at all. HANDOFF §4 said "if that test passes there,
the branch works". That inference does not hold.

Fixed: the stub now records its grandchild's pid, and the test asserts the
process is gone (checking `/proc` state so a zombie is not mistaken for a
survivor). Mutation-checked both ways — the strengthened test **fails** with
`killpg` suppressed and passes with it restored, so it is now load-bearing.

## What ran

| | Result |
|---|---|
| `uv run pytest` | **590 passed**, ~35s (575 before; 15 added) |
| `uv run python -m backend.rehearsal` | **12/12 stages pass**, 6.0s, real pdflatex, 6 PDFs through the parse gate |
| `uv run alembic upgrade head` / `alembic check` | Clean; "No new upgrade operations detected" |
| `uv run ruff check backend tests` | All checks passed |
| `uvicorn backend.main:app` | Serves. `/docs` 200, `/api/campaigns` 200, `/api/jobs` 200. `/` is 404 by design — there is no root route |
| `npm run dev` | Serves on :5173, 200 |
| `npm run build` (`tsc -b && vite build`) | Clean, 40 modules, 294 kB |

Both servers were stopped afterwards and the ports confirmed closed.

`ruff format --check` reports 59 of 90 files would be reformatted. That is
**pre-existing** and was left alone — reformatting the repo would have buried
this session's diff. Worth doing as its own commit.

## Discovery on an empty database

The first-run backfill works exactly as documented:

```
discovery_backfilling  jobs_in_db=0 threshold=25
                       incremental_hours=8 backfill_hours=720
```

Two things the handoff did not mention, both found by running it:

1. **A fresh database has no campaigns, so discovery is a no-op.** The first run
   logged `no_active_campaigns` and fetched nothing. `python -m backend.seed`
   creates the profile shell and the 21 answer-bank rows but **no campaign** —
   campaigns are only created through the API/UI. So on the Mac the order is:
   migrate, seed, *create a campaign*, then discover. A campaign was inserted
   here by hand to get past this.
2. **A total source failure is recorded as a successful run.** With a campaign
   active, all three sources were blocked by this container's egress proxy (403
   at the tunnel — environmental, not a bug). Each failure was logged loudly and
   correctly (`seek_json_transport_failed`, `jobspy_search_failed`), so hard rule
   9 holds at the log. But the `Run` row came back `ok=True` with
   `{'seek': {'fetched': 0, 'error': 0}, ...}` — the per-source `error` counter
   is never incremented from the source exceptions. A silent network outage
   therefore looks identical on the dashboard to a quiet day. Not fixed; it is
   outside this session's scope and wants a small deliberate decision about what
   `ok` should mean.

**No ads were fetched, so no live source is verified from here.** Seek,
LinkedIn and Indeed are all still only proven on the Windows machine.

## Gap 1 — `escalate_question` is wired, and the loop actually closes

`Claude.md` hard rule 2: "abstain, park the job, ask via Telegram, save the
answer, retry". The asking half is now called.

Wiring, end to end:

- `apply/flow.py::_park` records the parked question on the job.
- `apply/run.py` collects parked jobs during the pass and calls
  `escalate_question` in `_escalate_parked` **after the session commits**. Not
  inside it: the pass holds one session open for the whole queue (minutes, with
  pacing), so asking mid-transaction races the reply — a prompt `/answer` would
  read a job whose `NEEDS_ANSWER` status was not committed yet and `requeue_job`
  would refuse it without saying why.
- A send failure or exception is logged and the pass continues; the job stays
  parked.

**Two real bugs were in the way, and the loop could not have worked without
fixing them.** Both were found by trying to make the round trip pass.

1. **The answer was filed against the wrong question.** `_cmd_answer` picked *the
   oldest blank row in the whole bank*, regardless of which job was being
   answered. With two jobs parked, answering one wrote into the other's row:
   the real question stayed unresolved and a verified answer elsewhere was
   overwritten. Now the job carries its own question (new nullable column
   `job.needs_answer_question`, migration `11c09d52282c`) and `/answer` uses it.

2. **Answering a seeded question created a rival row and deadlocked the loop.**
   `Abstain.question` is the *form's* wording; a seeded row's `question_pattern`
   is a regex. `save_answer` matched them by string equality, which never
   succeeds, so the reply was stored as a *new* fuzzy row while the matched row
   stayed blank. On the retry both rows match, they tie inside
   `AMBIGUITY_MARGIN`, they disagree ("" vs "Yes") — and `resolve_answer`
   abstains as `AMBIGUOUS`. The job re-parks on a question you have just
   answered, forever, and it would have done so for all 21 seeded questions.
   Fixed by carrying `Abstain.source_row_id` through to `save_answer`, which now
   fills the row that actually matched.

Proof, in `tests/test_escalation_loop.py` — the full circuit against the real
database, only the Telegram HTTP call faked: abstain → park → escalate →
`/answer` → stored in the matched row → re-queued → **the retry resolves and
fills the field**. Plus: two jobs parked at once do not steal each other's
answers; a failed send leaves the job parked and says so; an escalation that
raises does not end the pass.

Mutation-checked: reverting the `row_id` fix makes the loop test fail with two
rows in the bank and the seeded one still blank — the exact deadlock above.

## Gap 2 — `detect`'s HTML fingerprint is wired

The handoff recommended wiring `detect` into the adapter's `open()`. **That
alone would not have fixed anything**, and it is worth being clear why:
adapter selection runs `can_handle` → `detect_from_url`. A white-labelled PageUp
on `careers.acme.com.au` matches no adapter, so `no_applier_for_job` fires and
the job goes to `MANUAL_QUEUE` — `open()` is never reached on exactly the jobs
the HTML fingerprint exists for.

So it is wired in two places:

- **Selection** (`apply/run.py::_applier_from_page`) — when no adapter claims the
  job by URL and it is `EXTERNAL`/`UNKNOWN`, the page is loaded and `detect(url,
  html)` runs. This is the path that fixes the Australian case. Returns None
  rather than guessing: unidentifiable still means manual queue.
- **Confirmation** (`ats/adapters.py::_confirm_platform`, in `open()`) — the page
  that actually loaded is checked against the adapter driving it. Clicking
  "Apply" often lands on a different platform, and employers embed other
  vendors' form builders in iframes; either way the selectors are wrong.
  It **reports** rather than re-points — swapping adapters on an already-open,
  already-clicked page is a bigger change than a mismatch justifies, and hard
  rule 9 means it must not be silent.

`tests/test_ats_html_wiring.py` covers both: a white-labelled PageUp page now
selects the PageUp adapter, an iframe to Greenhouse is followed into,
an unrecognisable page selects nothing, a recognised platform with no adapter is
reported rather than substituted, a probe that cannot load does not end the
pass, and the mismatch warning fires (and does not false-fire).

`tests/test_reachability.py` had both functions pinned as known-unreachable;
both entries are removed, so the check now enforces that they stay wired.
`decide_queueing`, `ensure_logged_in`, the outbound-email trio, `replay` and the
vestigial five are untouched and still pinned.

## Still unverified

Everything HANDOFF §1 listed as unverified, still is, plus:

- **macOS itself.** Nothing here ran on a Mac. BasicTeX, `tlmgr`,
  `/Library/TeX/texbin`, and the Mac's own tz database are all still untested.
- **Live discovery.** Egress blocked; 0 ads from all three sources.
- **Real scoring and document content.** No API key, no profile. Every LLM call
  in the rehearsal is still a deterministic stub.
- **Telegram, for real.** The loop is proven against a fake sender. No token was
  configured, so no message has ever left the machine. The transport in
  `send_message` is unchanged and still unexercised.
- **Any browser.** No Chrome, no Playwright browsers, no HAR. The ATS detection
  work is tested against fake pages — the fingerprints and the selection logic
  are proven, driving a real portal is not.
- **`ALLOW_LIVE_SUBMIT`.** Still false everywhere. Never touched.

## What needs you

1. **Run the BasicTeX install on the Mac** — HANDOFF §4, `brew install --cask
   basictex` then the `tlmgr install` list. It is the one setup step this
   session could not do for you. Then `uv run python -m backend.rehearsal`; if
   it passes, the Mac is set up.
2. **Create a campaign before running discovery**, or discovery silently does
   nothing on the fresh database. Migrate → `python -m backend.seed` → create a
   campaign in the UI → discover.
3. **A Telegram bot token and chat id.** The answer-bank loop is wired and
   tested but cannot ask you anything without them, and until it can, every
   novel screening question still parks the job. This is now the single change
   that unlocks the most.
4. **Your LaTeX resume and an API key** — unchanged from the last handoff, still
   the two things blocking real document generation.
5. **Decide what a discovery `Run`'s `ok` should mean** when every source failed
   (see above). Currently `ok=True`, `error=0`.
6. **`ruff format`** as its own commit, if you want the repo format-clean.
7. **Branch name.** This work is on `claude/macos-bringup-yd8i0q`, not the
   `fix/macos-bringup` that was asked for — the session harness pins the branch
   it is allowed to push. Rename or merge as you prefer; the commits are the
   same.

## Explicitly not done

`ALLOW_LIVE_SUBMIT` untouched. No login to any job board, no HAR recorded, no
browser installed, no message sent to Telegram, no scheduled check-ins.

---

# Discovery honesty and a starter campaign — 2026-09-02 (later)

Still on Linux, not the Mac. Steps 1 and 4 of the follow-up (BasicTeX via
`tlmgr`, and real discovery against live boards) are deliberately **not** done
here — both need macOS or working egress. What follows is the two pure-Python
items.

## 1. A run where every board failed is no longer `ok`

The bug, from the previous section: all three boards blocked by a proxy, every
failure logged, and the `Run` row still `ok=True` with `error: 0` on every
source. A total outage was indistinguishable from a quiet Sunday.

**The root cause was one level down from where it looked.** `run_discovery` set
`ok = not errors`, and `errors` was genuinely empty — because the sources never
raised. Each one catches its own transport failures and returns `[]`:

- `seek_source` — `_fetch_json` returns None and `_fetch_html` returned `[]` on
  a 403, and `[]` also means "the page had no ads". The two were the same value.
- `jobspy_source` — catches per-query and `continue`s.
- Worse, **jobspy's LinkedIn scraper swallows its errors inside the library**,
  logs them and returns an empty frame. Indeed re-raises; LinkedIn does not.

So "best effort: partial results beat an exception" had quietly become "an
outage returns success".

The fix keeps that rule and carves out the one case it must not cover — nothing
fetched *and* nothing that was tried worked:

- New `SourceUnavailable` in `backend/base.py`. Sources raise it only when every
  request failed and nothing came back. Anything that returned rows, even one
  page, is still a success and still does not raise.
- `seek_source._fetch_html` now returns `None` for "could not fetch" and `[]`
  for "fetched, nothing there". Collapsing those was the actual defect.
- `jobspy_source` counts attempts and failures, and — for LinkedIn — reads
  jobspy's own ERROR records around the call. Zero rows *plus* a logged error is
  a failure; rows plus a logged error is a partial success.
- `discover` increments the per-source `error` bucket when a source fails, and
  returns the set of sources that answered.
- `ok = bool(succeeded) and not errors`. Both halves are needed: `succeeded`
  catches the silent outage, `not errors` still catches a loud failure or an ad
  that would not store. `counts["sources_succeeded"]` records which boards
  answered.

Reading another library's log is not lovely. It is scoped to the duration of the
call and to jobspy's own ERROR records, and it hooks the **log record factory**
rather than adding a handler — jobspy's loggers set `propagate = False`, so a
root handler never sees them, and attaching by name only works if the logger
already exists. That is true after `import jobspy`, but it is an ordering
dependency that would have failed silently the day it stopped holding and
restored the exact blind spot being closed. There is a test for a logger created
late for precisely this reason.

Verified against the real blocked network, which is the same outage that
produced the original false positive:

```
sources={'seek':     {'fetched': 0, 'error': 1},
         'linkedin': {'fetched': 0, 'error': 1},
         'indeed':   {'fetched': 0, 'error': 1}},
sources_succeeded=[]   ok=False   exit code 1
```

Before the change this same run reported `ok=True` and `error: 0` on all three.

`discovery_no_source_succeeded` also names *which* problem it is —
`no_active_campaign` versus `every_source_failed` — because they need different
fixes and were previously one vague line.

**A genuinely quiet day is still `ok=True`.** There are tests pinning that in
both directions; if they ever go red, the fix has overcorrected and an empty
Adelaide is being reported as an outage.

## 2. A starter campaign, seeded inactive

Discovery reads active campaigns only, and a freshly migrated database has none,
so it ran, stored nothing and reported itself finished. Every part working as
designed, adding up to a system that appears to run and does nothing.

`seed_starter_campaign()` (in `backend/seed.py`, called by `seed_all`) creates
one campaign **only when no campaign exists at all**:

| Field | Value | Why |
|---|---|---|
| `name` | `Adelaide starter` | |
| `active` | **`False`** | The point. Nothing can apply before it is read. |
| `search_terms` | `["data analyst", "software engineer"]` | A guess at what to look for — **the main thing to edit.** |
| `locations` | `["Adelaide SA"]` | |
| `work_types` | `["full-time"]` | |
| `salary_floor` | `None` | An invented floor silently filters out real ads. |
| `score_floor` | `60.0` | Matches the API default. |
| `score_auto_apply` | `85.0` | Above the 80.0 default — the automatic path should start stricter and be relaxed knowingly. |
| `gray_zone_action` | `ASK` | An ambiguous score asks; it never guesses either way. |
| `daily_caps` | `{"default": 5}` | **Load-bearing:** `check_can_submit` passes outright when no cap is configured, so a campaign with no caps is an uncapped one. |
| `rubric` | `{}` | Falls back to `DEFAULT_RUBRIC`; pinning a copy here is how the two drift. |

The search terms are search *configuration*, not a claim about the user — hard
rule 1 governs the profile and nothing here touches it. They are still a guess,
which is why the campaign ships switched off.

Re-seeding never overwrites it, and specifically never re-activates a campaign
the user paused or reverts an edit — same rule as the answer bank, with a test.

Verified on a genuinely fresh database: `alembic upgrade head` →
`python -m backend.seed` → profile, 21 answer rows and one inactive campaign; a
second `seed` run inserts nothing.

## Suite

**622 passed** (590 before; 32 added), ruff clean, rehearsal 12/12, `alembic
check` reports no new operations. The suite was run twice to check the new
files are not order-dependent — the first version of the campaign tests was, by
deleting from the shared database across a foreign key, so they now use their
own.

## Still for the Mac

Unchanged: BasicTeX via `tlmgr`, `PDFLATEX_PATH`, and real discovery against
live boards. Nothing in this section has been exercised against a real job
board — the sources' *failure* path is now well covered, their success path
still only by the fixtures and by the previous Windows run.

---

# Overnight session — 2026-09-03 (macOS)

Nine phases, eight branches, eight PRs. Everything below was run on this
machine unless it says otherwise. **Nothing was submitted; `ALLOW_LIVE_SUBMIT`
was never touched and is still false.**

## The PR stack

Phases 2–8 genuinely build on each other, so they are a **linear stack** rather
than eight branches off `main` as the brief specified. Merge in order and each
retargets cleanly.

```
main
├── #5  chore/docs              (independent)
└── #6  feat/siteknowledge
    └── #7  feat/har-pipeline
        └── #8  feat/failure-memory
            └── #9  feat/preferences
                └── #10 feat/seek-nz
                    └── #11 feat/ats-adapters
                        └── #12 chore/reachability
```

Stacking was a judgement call, not an oversight. Phase 3's extractor emits
`Strategy` objects, Phase 4 keys failures on `element_id` and `flow_variant`,
Phase 7 rewrites the ATS adapters onto the same layer — branching those off
`main` would have meant re-implementing Phase 2 three times or resolving the
same conflicts three times at merge.

## What was built

| Phase | Branch | PR | Substance |
|---|---|---|---|
| 1 | `chore/docs` | #5 | tlmgr correction, platform-aware pdflatex hint |
| 2 | `feat/siteknowledge` | #6 | Site knowledge layer, both primary boards |
| 3 | `feat/har-pipeline` | #7 | Capture → knowledge, offline replay harness |
| 4 | `feat/failure-memory` | #8 | `FailureEvent` ledger, digest trends |
| 5 | `feat/preferences` | #9 | Preference memory, proposals, Preferences page |
| 6 | `feat/seek-nz` | #10 | NZ as configuration, currency + work-rights safety |
| 7 | `feat/ats-adapters` | #11 | Nine ATS platforms on site knowledge, trust graduation |
| 8 | `chore/reachability` | #12 | 11 unreachable → 5, all five deliberate |

**804 tests pass** (622 at session start; 182 added). Rehearsal 12/12 on every
branch. Frontend typechecks.

## Decisions taken without asking

The brief said to pick a sensible default and note it. These are those.

1. **Stacked branches** rather than eight off `main`. Reasoning above.
2. **Site knowledge ships in `backend/siteknowledge/defaults/` and seeds into
   `data/siteknowledge/` on first load.** The brief specified the `data/` path,
   but `data/` is gitignored, so defaults living only there would not survive a
   clone. The live copy under `data/` is never overwritten after seeding — it
   is what the user edits and what promotion writes into.
3. **Generic HTML stayed in Python.** "No Seek or LinkedIn selector remains in
   Python" is enforced by a test, but `input, textarea, select` and
   `label[for=...]` live in `backend/apply/formdom.py`, shared. They are the
   HTML standard, identical on every site; filing them under "facts about Seek"
   would be filing a fact about HTML in nine places.
4. **Captured element keys are namespaced `captured_*`.** A capture cannot
   silently rewire a curated adapter key — mapping "a button labelled Submit"
   onto `submit_button` would be a guess, and a wrong guess repoints the adapter
   at a different control. Promotion is a human reading the merge report.
5. **`FailureEvent.job_id` is `ON DELETE SET NULL`.** RESTRICT would let the
   ledger pin every job it ever touched; CASCADE would erase the record that
   anything failed. SET NULL keeps every dimension anything aggregates on.
6. **Cross-currency salary comparison keeps the job.** Dropping an ad because
   its currency is unknown hides real work; keeping it costs one manual look.
   Only the unconverted comparison is ruled out.
7. **The outbound email trio stays unwired.** See "Needs you" below.

## What broke, and what found it

Eight real bugs. Every one was caught by a test or the rehearsal, not by
reading.

- **Three broad `except Exception` handlers swallowed `ElementNotFound`**
  before `run_apply`'s wrapper could see it, so a moved site reported "could
  not open form" and retried forever. An AST test now fails if a fourth
  appears. (Phase 2)
- **The capture extractor skipped `<a>` with no `href`** — which is exactly
  Seek's Quick Apply control, the single most important element on the page.
  (Phase 3)
- **Short identifiers were patterned into near-universal selectors.** `q1`
  became `[id*='q']`, which matches nearly every element on the page. An
  over-broad strategy is worse than none: resolution finds *something*, reports
  success, and the adapter clicks the wrong control. (Phase 3)
- **Two naive-vs-aware datetime crashes**, in the failure digest and in
  `propose_from_skips`. The second would have raised inside the apply pass.
  (Phases 4, 5)
- **Two fact-detection patterns were wrong**: the work-rights pattern did not
  match "full working rights in Australia", and the salary pattern matched
  "salary expectations" but not "current_salary". Both would have let the
  system infer a fact about the user. (Phase 5)
- **The first cross-currency rule silently disabled the salary floor** for
  every job discovered before the currency column existed. (Phase 6)
- **Wiring `ensure_logged_in` halted the pass on every external application.**
  An employer ATS has no login, so `is_logged_in` returns False for Greenhouse.
  The rehearsal caught it within a minute. (Phase 8)

## Tests that could not fail

Your standing note was right to insist on this. **Every new assertion was
mutation-checked** — 36 mutations across the eight phases. Three tests passed
against a mutation that should have broken them:

1. **Phase 4** — an assertion ending in `or "resume_file_input" in body`, where
   the right operand was always true. Split into two tests that each fail on
   their own mutation.
2. **Phase 6** — the headline test, "an AU answer must not answer an NZ
   question", passed with the region filter *deleted*. The two questions score
   below the fuzzy threshold, so it abstained via `NO_MATCH` regardless. Added a
   regex-row case where `working rights` matches both phrasings outright and the
   Australian "Yes" really would be used.
3. **Phase 7** — nothing covered the form-map trust verdict travelling from disk
   to the draft, which is precisely where it was being discarded. Added both
   directions.

A fourth artefact was not a code bug but worth recording: **`.pyc` staleness
made a restored file look mutated.** `AU-Main` and `NZ-Main` are the same byte
length, so size+mtime cache invalidation missed the change. Mutation runs now
clear `__pycache__` between iterations.

## Live findings

**Seek NZ, probed 2026-09-03** — the brief said not to assume it mirrors AU, and
the important finding is one an assumption would have missed:

```
www.seek.co.nz                        308 -> nz.seek.com
nz.seek.com/api/jobsearch/v5/search   200, envelope identical to AU
siteKey=NZ-Main is the market selector, NOT the host
```

`au.seek.com` with `siteKey=NZ-Main` returns NZ jobs. The host is cosmetic. So
sending the wrong site key returns the wrong country's listings from the right
host, and nothing in the response says so.

**Neither market returns a currency field.** AU prints `$75,000 – $90,000 per
year`, NZ prints `$81,083 - $110,618`. Identical notation, ~0.9 NZD/AUD.
`locations[].countryCode` is the only reliable discriminator.

**jobspy supports New Zealand** — `Country.NEWZEALAND`, Indeed domain `nz`,
confirmed against the installed 1.1.82 rather than its docs. `country_indeed`
was hardcoded to `"Australia"`; it is now per region. LinkedIn needs no country
parameter, its location string drives the market.

## Unverified

Unchanged from the last handoff, and this session did not move any of it:

- **Nothing has ever been submitted.** `ALLOW_LIVE_SUBMIT` has never been true.
- **No browser has ever been driven.** Chrome and the Playwright browsers are
  still not installed. Every apply-path test uses a fake page, a snapshot page,
  or a fake adapter.
- **Every strategy value is a guess.** Phase 2 and Phase 7 changed the
  *structure* — multi-strategy, self-healing, stored as data. The values were
  migrated from the previous hardcoded selectors, which were themselves written
  without access to the live sites. The HAR capture is what makes them real.
- **The capture pipeline has never seen a real HAR.** It is tested against
  synthetic fixtures shaped like both platforms' real markup.
- **No real scoring or document content.** No API key, no profile, so every LLM
  call in the rehearsal is a deterministic stub.
- **Telegram, Gmail inbound, outbound email** — no credentials, never run. The
  new `/yes`, `/no` and form-approval paths are tested but have never sent a
  message.

## Needs you

1. **Run the HAR capture.** Everything around it is built. `uv run python -m
   backend.apply.har record --platform seek --variant quick_apply`, press
   Shift+Enter at each step, then `... har ingest --platform seek --variant
   quick_apply --dry-run` to see what it would learn before writing anything.
   This is the single highest-value thing left — it turns every strategy from a
   guess into something verified.

2. **Decide on outbound email.** `send_draft` sends mail *as you*, from your
   address, to a real recruiter. It is the one action here whose blast radius is
   someone else's inbox, and unlike an application it cannot be undone by a
   switch after the fact. Wiring is one call in `apply/run.py` plus the approval
   token it already requires. Left off deliberately — say the word.

3. **Review the starter campaign's search terms.** Still the placeholder guess
   (`data analyst`, `software engineer`, Adelaide SA). It is active from the
   bring-up, so discovery is running against terms nobody chose.

4. **Set a region on any NZ campaign.** Existing campaigns are AU by migration
   default, which is what they actually are.

5. **Merge the stack in order**, #5 and #6 first.

---

# Session — 2026-09-03 (afternoon, macOS)

Five phases. The eight-PR stack from the overnight session merged to main;
three new phases built on top. **`ALLOW_LIVE_SUBMIT` untouched and still false.
`send_draft` still unwired** — both were explicit constraints.

## Phase 1 — the stack merged

All eight merged in order with **no conflicts**, so no judgement calls on
content. One process incident worth recording:

**`gh pr merge --delete-branch` closed #7 instead of retargeting it.** Merging
#6 deleted `feat/siteknowledge`, and GitHub closes any PR whose base branch is
deleted. #7 could not be reopened either — GitHub refuses once the base is
gone — so it was recreated as **#13** with the same branch, commits and body,
plus a note explaining the replacement.

The fix for the rest of the stack: **retarget every remaining PR to `main`
before deleting any more branches.** #8–#12 were retargeted first and merged
cleanly. Worth remembering for the next stack — the safe order is retarget the
dependent, then merge-and-delete the parent, not the reverse.

`main` afterwards: 809 tests (804 from the stack + 5 from `chore/docs`, which
branched independently), rehearsal green, all branches deleted.

## What was built

| Phase | Branch | PR | Substance |
|---|---|---|---|
| 1 | — | #5–#13 merged | The overnight stack, on main |
| 2 | `feat/answer-bank-facts` | #14 | Facts verbatim + derived answers, confirmed once |
| 3 | `feat/session-health` | #15 | Per-site session checking, Sessions page |
| 4 | `feat/deeper-scoring` | #16 | Unlimited fan-out, ad requirements, variants, self-check |

**894 tests** (809 at merge; 85 added). Rehearsal green on every branch.
Frontend typechecks.

## The cost delta, as asked

Priced from the configured models (`gemini/gemini-3.1-flash-lite` for scoring):

| stage-2 fan-out | per run | per month @ 6 runs/day |
|---|---|---|
| 40 (previous) | $0.0252 | $4.54 |
| 100 | $0.0608 | $10.94 |
| **all 200 (new default)** | **$0.1200** | **$21.60** |

Against a $25/month cap, so unlimited fits — but only just, and that is the
**backfill worst case rather than the steady state**. Scoring is incremental
(`needs_scoring` skips anything already scored), so the real figure is *new*
jobs per run: 20–40 at an 8-hour window, about **$4.32/month**. The $21.60 line
only happens on a 720-hour backfill.

`test_the_worst_case_month_fits_the_cap` pins it, so the day it stops fitting is
a failing test rather than a surprise. `SCORING_STAGE2_MAX` restores a cap
exactly if wanted.

Phase 4's other calls are per *application*, not per job, so they do not move
this much: variants are 3 writing calls instead of 1 per slot, and the judge
plus the self-check are cheap-model calls.

## Decisions taken without asking

1. **Unlimited fan-out as the default** (`SCORING_STAGE2_MAX=0`). The brief said
   to allow it; making it the default follows from the arithmetic above.
2. **Requirements extracted inside the existing stage-2 call** rather than a
   separate extraction pass. The model is already reading the whole ad there; a
   second call would pay to read it twice.
3. **Requirements stored on `Score`, not `Job`.** It is model output about the
   ad, and re-scoring against a changed rubric can legitimately re-derive it.
4. **Three variants, not more.** The judge reads every variant, so cost is
   linear in the count and the gain is not — best-of-three beats best-of-one by
   far more than best-of-six beats best-of-three.
5. **The self-check passes when it cannot run.** Every other parse-gate check is
   deterministic; a model outage failing a document they all accepted would make
   the one reliable thing unreliable.
6. **Login pages detected by a password field**, not per-site signed-in
   selectors. Works on every site with zero configuration, which matters
   because you create the ATS accounts yourself and there is no list to hold.
7. **`fact_category` on `AnswerBank`** rather than a second question matcher.
   Two matchers that can disagree about what a question is asking is how a
   licence fact answers a police-check question.
8. **Fact shells seeded empty.** Placeholder text would be a fabricated fact
   about you in the one store treated as verbatim truth.

## What broke, and what found it

Nine real bugs. Every one caught by a test, the suite, or the rehearsal.

- **The self-check bypassed the LLM stub seam** — an inline
  `from backend.llm.client import complete_json` instead of the module-level
  `llm` the rehearsal replaces. Every document build attempted a real API call
  and retried: **the suite went from 27s to 3m27s.** Caught by watching the
  runtime, then pinned by a test that fails if the inline import returns.
- **Cookie domains collapsed to a registrable domain**, turning
  `careers.acme.com.au` into `acme.com.au` — checking the employer's marketing
  site, finding no login page, and reporting a dead careers portal as *healthy*.
- **The ATS session lookup read `platform.domains`**, which is spelled
  `host_patterns`. It silently matched nothing, so every ATS looked like an
  unknown site. Now goes through `detect_from_url` — the same logic that names
  a job URL.
- **`au.seek.com` did not map to `seek`.** `boards.py` listed only
  `seek.com.au`; the host Seek actually serves was verified into `regions.py` by
  the Phase 6 probe. Fixing it in `sessions.py` required naming `"seek"` outside
  the registry, which `test_no_module_outside_the_registry_names_a_job_board`
  caught. **Fixed at the registry instead** — `boards.py` now derives Seek's
  domains from the region configs, so there is one live-verified list.
- **Two fact-routing gaps**: `Score` was used in `documents/build.py` without
  being imported, and the 21 seeded `AnswerBank` rows pre-dated
  `fact_category`, so an upgraded install kept every question and none could
  reach a fact — silently, because a blank row already means "ask". Backfilled.
- **`ensure_logged_in` halted the pass on every external application** (carried
  over from Phase 8 and re-verified here): an employer ATS has no login, so
  `is_logged_in` returns False for Greenhouse. Scoped to platforms that have a
  session.
- **`DerivationRefused` was defined and never raised**, and three facts
  functions were unreachable. The reachability audit caught all four: the
  exception was deleted, the rest wired.

## Tests that could not fail

**Two of 24 mutations passed on the first attempt.** Both were on the most
important behaviour in Phase 2, and both were the same shape as the failures
from last session — an assertion surviving because a *different* guard caught
the mutation:

1. **The silent-fact test passed with the `supported` check deleted.** Its stub
   also returned an empty answer, so the empty-answer guard caught it and the
   real gate was never exercised. Rewritten with `supported=false`, a valid
   answer, and no stated doubt — so the flag is the only thing standing between
   it and "No" on a real form. Then it *still* passed, because the
   `uncertainty` guard caught it; tightened again to leave uncertainty empty.
   Three attempts to get one assertion to actually test its subject.
2. **The re-ask test only counted messages**, so it passed even when the cached
   branch started returning *unconfirmed* answers onto real applications. Added
   one that checks the return value on the path that reads the cache.

Also worth recording: a test that encoded the *old* design rather than being
wrong. `test_cost_scales_with_the_stage2_cap_not_the_discovery_volume`
asserted "doubling discovery must not double the bill", which was true and is
deliberately no longer the default. Replaced with three tests pinning what is
true now, rather than adjusted to pass.

## Unverified

Unchanged from the overnight session — none of this moved:

- **Nothing has ever been submitted.** `ALLOW_LIVE_SUBMIT` has never been true.
- **No browser has ever been driven.** Chrome and the Playwright browsers are
  still not installed. Every apply-path test uses a fake page, a snapshot page,
  or a fake adapter — including all of Phase 3, whose `FakePage` is a set of
  selectors rather than a browser.
- **Every site-knowledge strategy value is still a guess.** The HAR capture is
  what makes them real.
- **No real LLM call has been made in any of this.** Every derivation, variant
  judgement and self-check is tested against a stub. The *plumbing* is proven;
  the model's judgement is not, and the derivation prompt in particular is the
  one place where a plausible-but-wrong reading becomes a legal declaration.
- **Telegram has never sent a message.** `/yes d<id>`, `/no d<id>`, the
  derivation confirmation and the session-dead alert are all tested and none
  has ever left the machine.
- **Session checking has never seen a real cookie jar.** It reads
  `context.cookies()`, which has only ever been a fake.

## Needs you

1. **Write your facts.** `/facts` — eleven empty textareas. Nothing in Phase 2
   can do anything until they have your words in them, and the licence and
   work-rights ones are the two that unlock the most screening questions.
2. **Run the HAR capture.** Still the highest-value item outstanding, and still
   what turns every strategy value from a guess into something verified.
3. **Sign in to the ATS accounts**, then let one session check run. Phase 3 has
   never seen a real cookie jar, and the first run is what tells us whether the
   password-field heuristic holds on real sites.
4. **Review the first few derivations before trusting the cache.** Each is
   confirmed once and then never asked again, which is the point — but it also
   means a wrong confirmation is durable. Check the reasoning line on the first
   handful.
5. **Merge #14, #15, #16 in order.** Retarget the next one to `main` *before*
   deleting the merged branch, or the same auto-close that hit #7 will happen
   again.
6. **`send_draft` is still off**, as instructed. Unchanged and unwired.

---

# Session — 2026-09-05 (macOS)

Six phases. The three-PR stack merged, the tree formatted, and four new
commands built. **`ALLOW_LIVE_SUBMIT` untouched and still false.
`OUTBOUND_ENABLED` added, defaulting false — `send_draft` is wired and off.**

## Phase 1 — merged, and formatted

#14, #15, #16 merged in order with no conflicts. **Retargeting each dependent
to `main` before deleting the merged branch worked** — no auto-closes this
time, which is the fix for what killed #7 two sessions ago. That ordering is
now the rule: retarget the dependent, *then* merge-and-delete the parent.

`ruff format` as its own commit: 79 of 126 files. The lint pass underneath it
found four things worth fixing rather than suppressing, one of them real —
`flow.py` used `Region` in three annotations without importing it. `from
__future__ import annotations` makes annotations strings so it never raised,
but it was still an undefined name, and `get_type_hints` on those signatures
would have failed. Introduced with the facts layer last session.

## What was built

| Phase | Branch | PR | Substance |
|---|---|---|---|
| 1 | — | #14–#16 merged | The stack, on main, plus `ruff format` |
| 2 | `chore/smoke-tests` | #17 | `backend.smoke` — every real transport, once |
| 3 | `feat/derivation-preview` | #18 | `backend.facts preview` — dry-run all 21 |
| 4 | `feat/outbound-wire` | #19 | `send_draft` wired, every guard intact, off |
| 5 | `feat/setup-doctor` | #20 | `backend.doctor` — traffic-light checklist |

**953 tests** (894 at merge; 59 added). Rehearsal green on every branch.
Frontend typechecks. Ruff clean.

## Two corrections to this file

**Chrome and Playwright work.** NOTES.md has carried "no browser has ever been
driven" for three sessions. `backend.smoke` launched real headful Chrome, loaded
a page and closed it. Chrome is installed at `/Applications/Google Chrome.app`
and `channel="chrome"` drives it. That claim was true and is not any more.

**`channel="chrome"` needs no Playwright browser download.** It uses the system
Chrome. The first version of the doctor's Playwright check counted cached
browser builds and would have reported OK on a chromium-only cache — the exact
false green it was written to prevent.

## Decisions taken without asking

1. **`OUTBOUND_ENABLED` is separate from `ALLOW_LIVE_SUBMIT`,** not folded into
   it. They authorise different things: one puts a document on a form the
   employer asked to be filled in, the other puts a message in a stranger's
   inbox. Someone may reasonably want the first only.
2. **The switch is checked inside `send_draft`,** not at the call site. One
   place that can send, one place that can be off — an approval given before
   the feature was enabled must not become a send afterwards.
3. **`OutboundMessage` with `UNIQUE(job_id)`.** "One message per job, ever" was
   documented and *not enforced*: nothing recorded what had been sent, so
   nothing could refuse a second. A SKIPPED row holds the slot as SENT does,
   because declining is a decision.
4. **A failed send stays DRAFTED.** A transport error is not a decision, and
   marking it SENT would consume the job's one slot with nothing having arrived.
5. **The Telegram follow-up notification is not actionable.** Send/Skip/Edit is
   in the UI: a message that could send an email with one tap is a message one
   mistap sends an email from.
6. **Regex seeds carry an `example_question`.** Ten of the 21 match by regex, so
   their `question_pattern` is a regex — fine for matching, nonsense to hand a
   model as "the question on the form". The real flow never hits this because
   the question comes from the form field's label.
7. **Doctor exits non-zero on BLOCK only.** A warning is not a broken install.
8. **Smoke skips are not failures** either, for the same reason: the command
   has to be runnable mid-setup, which is the only time it is useful.

## What broke, and what found it

Six real bugs, plus two self-inflicted ones caught immediately.

- **`Region` used but never imported** in `flow.py` (above). Found by ruff.
- **`derive()` bypassed the LLM stub seam** with an inline
  `from backend.llm.client import complete_json` — the same anti-pattern fixed
  in `verify.py` last session. It would have made a real paid call from a
  rehearsal. That was the last one of its kind in the tree.
- **The preview fed raw regexes to the model** for ten of 21 questions.
- **Two regex seeds shared one example question** — the salary-expectation
  regex contains `hour` in its own negative lookahead, so the token match gave
  it the hourly-rate question. Found by *reading the preview output*, which is
  what the preview is for.
- **The doctor's Playwright check contradicted its own docstring** (above).
- **"One message per job" was unenforced** (above).
- Two name collisions made wired functions look unwired in the name-based
  reachability audit: a local variable `preview`, and `skip` aliased at its
  only call site. Renamed rather than allowlisted, so the audit keeps telling
  the truth.

## Tests that could not fail

**Three of 39 mutations passed on the first attempt.** All three the same shape
as previous sessions — a *different* condition satisfying the assertion instead
of the guard under test:

1. **The blank-fact test had no blank fact**, only a *missing* one. `facts_for`
   returned nothing either way, so the guard was never exercised. The fixture
   now contains a real blank fact.
2. **The parse-gate test hit the profile check first.** `draft_for_job` checks
   for a profile before it checks the gate, so the test passed on "no profile
   to write from" and never reached the guard it is named for.
3. **One mutation had simply not applied** — ruff had stripped the trailing
   comment my match string included, so the edit silently no-opped. Worth
   recording separately: a mutation that does not apply looks identical to a
   test that caught it, and the only way to tell is to assert the target was
   found. The mutation harness now does.

## Unverified

- **Nothing has ever been submitted.** `ALLOW_LIVE_SUBMIT` has never been true.
- **No email has ever been sent.** `OUTBOUND_ENABLED` has never been true, and
  `send_draft` has never run against a real SMTP server.
- **No real LLM call has been made anywhere in this project.** Every
  derivation, variant judgement and self-check is tested against a stub.
  `backend.smoke --only gemini` is the thing that changes that.
- **Every site-knowledge strategy value is still a guess.** No HAR capture has
  been recorded.
- **Telegram has never sent a message.**
- **Session checking has never seen a real cookie jar** — the profile holds no
  cookies, confirmed by `backend.smoke`.
- **The derivation prompt has never been run against a real fact**, because the
  facts are blank. `backend.facts preview` is built and reports 0 of 21
  answerable.

## Needs you

Run `uv run python -m backend.doctor` — it now tells you this itself. As of
this session it reports **3 blocking, 4 warnings**:

1. **API keys** (BLOCK) — `GEMINI_API_KEY` and `OPENAI_API_KEY`. This unblocks
   the most: scoring, cover letters, fact derivation, the variant judge and the
   fabrication self-check are all stubbed and unexercised. Then
   `uv run python -m backend.smoke` to confirm they work and see the real cost.
2. **Profile** (BLOCK) — no name or email. Every document needs both.
3. **Facts** (BLOCK) — all 11 blank. Then `uv run python -m backend.facts
   preview` and read all 21 derivations before confirming any: each is
   confirmed once and cached forever.
4. **Campaign** (WARN) — still active on the seeded placeholder terms.
5. **Sessions** (WARN) — sign in to the boards and the ATS accounts.
6. **Site knowledge** (WARN) — run the HAR capture.

Then merge #17 → #18 → #19 → #20, retargeting each next PR to `main` before
deleting the merged branch.

---

# Session — 2026-09-05 (macOS, unattended)

Two phases: import the real templates, then browser tab lifecycle.
`ALLOW_LIVE_SUBMIT` and `OUTBOUND_ENABLED` were not touched and are still false.

## Phase 1 — real templates  (`feat/real-templates`)

### The audit: what your resume source does, and what it does to an ATS

Your `main.tex` compiles and looks good. Extracted, it is much worse than it
looks. This is `pdfplumber` on **your existing PDF**, which is roughly what an
ATS reads:

```
M I
OHAMMED SA
(cid:131)+61450106807 #mohdisa233@gmail.com (cid:239)linkedin.com/in/4mohdisa §github.com/4mohdisa (cid:128)isaxcode.com
TECHNICAL SKILLS
Languages: TypeScript,JavaScript,Python,Go,Rust,Swift,Dart, Cloud&Data: AWSEC2,CloudflareR2/D1,GCP,Supabase,
SQL PostgreSQL,Vercel
...
MagainRealEstate Feb2026toPresent
PropertyManager Adelaide,SA
```

Six separate failures in eleven lines. Every change below is one of them.

| # | What I changed | Why — measured, not assumed |
|---|---|---|
| 1 | `charter` → `lmodern` | **The single worst defect.** charter's interword space at 10pt is **2.77pt**; `pdfplumber`'s default `x_tolerance` is **3pt**, and it only starts a new word when the gap *exceeds* the tolerance. So every space in your resume is discarded: `MagainRealEstate`, `PropertyManager`, `Adelaide,SA`. An ATS searching for "Real Estate" finds nothing. lmodern at 10pt measures **3.33pt** and clears it. Measured for charter/lmodern at 10 and 11pt — charter only survives from 11pt (3.03pt), which is one hundredth of a point of margin. |
| 2 | Removed `\usepackage{fontawesome5}` and `marvosym`, dropped all icons | Your contact icons extract as `(cid:131)`, `#`, `(cid:239)`, `§`, `(cid:128)`. Your **email extracts as `#mohdisa233@gmail.com`** — an ATS email field that no employer can reply to. `marvosym` is also absent from a stock TeX Live/MiKTeX install, so it is a build dependency you do not need. |
| 3 | Removed `\scshape` from the name | `\scshape` over mixed case split your name into two interleaved lines: `M I` / `OHAMMED SA`. Section headings keep the look because they are typed uppercase, where small caps changes nothing. |
| 4 | `\begin{multicols}{2}` skills block → single column | This is the two-column failure `Claude.md` names, and it is visible above: `Languages: TypeScript,...,Dart, Cloud&Data: AWSEC2,...` on one line. The parse gate's `single_column_layout` check would reject it. |
| 5 | `letterpaper` → `a4paper` | Australian standard. Also removed the five `\addtolength` margin hacks and `\usepackage[empty]{fullpage}` in favour of one `geometry` call — `fullpage.sty` lives in the `preprint` bundle and is **not** in TeX Live basic or a minimal MiKTeX, so on a fresh Windows box it is an on-the-fly package download mid-build (or a hard failure offline). |
| 6 | Contact details already in the body; kept there, split over two lines | See "blind spot 8" below — one line of five fields overflows the measure and strands a separator. |
| 7 | Removed `\usepackage{tikz}`, `svg.path`, `xcolor`, `latexsym`, `verbatim`, `babel`, `tabularx`, `graphicx` | Unused, or used only for the icons and colours that had to go. Fewer packages, fewer things absent on Windows. |
| 8 | Kept `\input{glyphtounicode}` and `\pdfgentounicode=1` | The one part of your preamble that was actively helping: it makes pdftex write a correct ToUnicode CMap. |
| 9 | Employer / institution / project names: `\textbf` → `\large` | Found late, and it is not obvious. See blind spot 7. |

### What I did NOT preserve, and why

- **The two-column skills block with its six category labels** (`Languages:`,
  `Frontend:`, `Backend:`, `Cloud & Data:`, `AI & ML:`, `Tools:`). The columns
  had to go regardless — that is failure #4. The *labels* went with them
  because `Profile.skills` is a flat `list[str]`, which is what the scoring
  embeddings match on, what `expected_verbatim` harvests one-by-one for the
  gate, and what the dashboard's profile editor edits. Keeping the labels means
  either storing the same skills twice (they would drift) or restructuring
  `skills` into groups, which is a frontend change well outside "import my
  templates". All 43 skills are in the resume, verbatim, comma-separated.
  Say the word if you want the labels back and I will do the editor properly.
- **Underlined contact links.** `\underline` makes a link unbreakable, so a
  slightly longer handle overflows the line instead of wrapping. The links are
  still links (`hyperref`, `hidelinks`).
- **`\resumeSubheading`'s `tabular*`.** Replaced with `\hfill`, which is
  visually identical here and is what the gate has already been calibrated
  against.

### Your content, in the database, verbatim

`Profile` version **2**. Every string is copied character-for-character out of
`main.tex` — nothing paraphrased, nothing summarised, nothing inferred.

- identity (name, email, phone, location, linkedin, **github**, website)
- 43 skills, 5 roles with 13 bullets, 12 projects, 3 education entries
- `references`: "Professional references are available upon request."

**Left empty rather than invented:** `headline`, `summary`, `work_rights`,
`certifications`. Your resume states none of them, and hard rule 1 says facts
come from you or not at all. `work_rights` being blank will keep showing up in
`backend.doctor` — that is correct, it needs your answer, not my guess.

Two schema notes:
- `identity` gained `github` and `references`, which the old template had no
  concept of. Added to `KNOWN_FIELDS`, `_profile_context` and the editor
  vocabulary so they are real fields, not smuggled ones.
- **`source='imported'`** is on `identity`, `work_rights`, and every row of
  `experience`, `projects`, `education` and `certifications`. `skills` is a
  flat list of strings and cannot carry a per-item key without breaking the
  template vocabulary and the profile editor, so its provenance is recorded in
  `preferences.field_sources`, along with every other field's.

The import script is in the session scratchpad, not the repo — your contact
details and employment history are not something I will commit to git without
you asking.

### The fact / narrative split, made explicit

The rule you asked for is now enforced by the template's own vocabulary:

| | Source | What it covers |
|---|---|---|
| `profile.*`, `job.*`, `today.*` | Deterministic substitution | Every employer, date, title, institution, qualification, project name, stack, link, and every contact detail. A model never touches these. |
| `ai.*` | Generated per job, word-capped, validated against the profile | The cover letter's four paragraphs, and the resume's **experience bullet points**. |

`ai.bullets` is new. Each role's bullets are rewritten toward the specific ad
**from your own bullets** — one model call per role, told it may reorder and
rephrase but may not add or drop a fact. The result is checked by
`validate_no_fabrication`, and the bullet count must match going in and coming
out (a model that merges two bullets into one has dropped a fact without
inventing a word). **Any failure falls back to your verbatim highlights** and
logs why. That is deliberately different from the cover letter, where an
unsupported claim fails the build: a letter paragraph has no truthful version
to fall back to, and a resume bullet always does — the one you wrote.

Project descriptions are deliberately **not** an AI slot. They are dense with
checkable facts ("317 tests", "the published ACSM metabolic equations",
"50+ languages", "Apple TestFlight") and belong on the substitution side.

### Nine artifacts, three real jobs, all gated

Built against the three real Seek ads already in SQLite. **The four cover-letter
paragraphs are stand-in text, not a writing sample** — there is still no API
key, so nothing was generated. The resume's `ai.bullets` was deliberately *not*
stubbed: it was left to fail the real way, so the production fallback path ran
and the bullets in these PDFs are your own words.

### Job 1 — Data Analyst @ Energy Logistix  (Largs North, Adelaide SA)

`https://au.seek.com/job/94360955`

| artifact | gate | pages | extracted chars | checks |
|---|---|---|---|---|
| resume | PASS | 2 | 5828 | 19 |
| cover_letter | PASS | 1 | 947 | 12 |
| combined | PASS | 3 | 6776 | 16 |

### Job 4 — Business Analyst @ ACH Group  (Adelaide SA)

`https://au.seek.com/job/94266944`

| artifact | gate | pages | extracted chars | checks |
|---|---|---|---|---|
| resume | PASS | 2 | 5828 | 19 |
| cover_letter | PASS | 1 | 936 | 12 |
| combined | PASS | 3 | 6765 | 16 |

### Job 17 — Senior Manager Data, AI and Analytics (Chief Data and AI Officer) @ SA Water  (Adelaide SA)

`https://au.seek.com/job/94321820`

| artifact | gate | pages | extracted chars | checks |
|---|---|---|---|---|
| resume | PASS | 2 | 5828 | 19 |
| cover_letter | PASS | 1 | 1033 | 12 |
| combined | PASS | 3 | 6862 | 16 |
9/9 passed. Check counts are **19 / 12 / 16** — up from 14 / 9 / 11 last session,
because of the four new checks below. The resume is 2 pages, same as yours.

### The gate had four more blind spots. All four are now checks.

You said it had four; I found four more. Every one of them passed **all**
existing checks on documents I was about to call finished. They are numbered
5–8 continuing the list in this file.

#### 5. `word_spacing_survives_extraction` — a squeezed line loses every space on it

The nine PDFs passed 15/15. Reading the text found this:

```
LLM-powered resume engine that rewrites a resume against a specific job ad, scores it the way an ATS would, and drafts the
coverletter. Promptpipelinetunedforfactualgroundingsothemodelreshapesrealexperienceinsteadofinventingit. Builtsolo
```

and

```
nutrition-labelpathscovertherest. DailyenergybalancecomputedfromthepublishedACSMmetabolicequationsratherthana
```

**The mechanism is arithmetic.** `pdfplumber` starts a new word when the gap
between two characters *exceeds* `x_tolerance`, default 3pt. lmodern's
interword space is 0.333em: 3.33pt at 10pt, but **3.00pt under `\small`** —
which does not exceed 3.0 — and justification can shrink it further. Every
space on a squeezed line is discarded.

It was worse than "some lines": the **same paragraph extracted differently in
different artifacts**. `cover_letter.pdf` kept its spaces and `combined.pdf`,
built from it by `PdfWriter.append`, did not. Combined is the artifact attached
wherever a form has a single upload slot, so the broken one is the one that
gets sent.

Two fixes, both root-cause:
- `\raggedright` on both templates. Justification is what lets TeX shrink the
  glue; ragged right removes the shrink entirely. Measured on a fixed
  paragraph: justified loses 11% of its spaces, ragged loses none.
- No `\small` anywhere that carries a keyword — which turned out to be
  everywhere, since dates, locations and technology stacks are all keywords.

The check itself extracts a **second time at `x_tolerance=1.2`** — below any
kerning gap, above nothing — and reports every token the default pass merged
that the tight pass separates. A token counts only when the tight pass splits
it into two or more pieces of two or more characters, which is what keeps a URL
(no internal gap for either pass) from ever being reported.

#### 6. `record_headings_start_a_line` — the next employer glued to the previous entry

Also found by reading text that had just passed everything:

```
EDUCATION
Performance Education Jan 2026 to Dec 2026
Professional Year Program, ICT Adelaide, SA Torrens University Australia May 2023 to May 2025
Bachelor of Information Technology Adelaide, SA 42 Adelaide Piscine Jan 2023
```

`\vspace` does **not** end a paragraph. The four-point gap I put between
entries was added inside the paragraph, so LaTeX flowed the next institution
onto the previous entry's last line — visually as well as in the text. An ATS
segments education and work history by line, so "Professional Year Program,
ICT" would be filed against Torrens University with Torrens' dates. The same
thing happened between every pair of projects.

Fixed with `\par\vspace{4pt}`. The check asserts every employer and institution
the profile states begins an extracted line, driven by a new
`ParseExpectations.line_starts` that `build_documents` fills. Supplied for the
resume and the combined PDF only — a cover letter names employers inside
sentences, where mid-line is exactly right.

#### 7. `facts_survive_both_extractors` — the gate was grading the friendlier extraction

This is the one I would have shipped. `verify_pdf` runs **two** extractors and
then does this:

```python
text = plumber_text if len(plumber_text) >= len(pypdf_text) else pypdf_text
```

pdfplumber always wins, because it rebuilds words from glyph positions. So
**every content check in the gate has only ever read pdfplumber**, and the
pypdf text was thrown away before any of them saw it.

pypdf reads the content stream instead, and inserts a space wherever pdfTeX
emitted a tightening kern. In lmodern **bold**:

```
W emark Real Estate    Mar 2024 to F eb 2026
V ericent              Sep 2023 to F eb 2024
T orrens University Australia
```

Two of your five employers and one of your three institutions were unfindable
by an entire class of parser, on a document reporting "passed all 15 checks".
Isolated and confirmed: bold splits, italic and upright do not, at every size.

Fixing it needed a real choice, so here is the measurement. Every alternative
font that survives pypdf in bold fails the word-spacing test at 10pt:

| | pypdf: facts lost | pdfplumber: facts lost | merged words | pages |
|---|---|---|---|---|
| lmodern 10pt, bold facts (what I had) | **3** | 0 | 0 | 2 |
| helvet 11pt, bold facts | 3 | 0 | 12 | 2 |
| charter 11pt, bold facts | 2 | 0 | 9 | — |
| times 11/12pt, bold facts | 2 | 6 | 82 | — |
| **lmodern 10pt, facts not bold** | **0** | **0** | **0** | **2** |

So: employer, institution and project names are set in `\large` rather than
`\textbf`. They are still the largest thing on their line and still read as
headings; they are simply not bold. Section headings **stay bold** — they
survive both extractors, and I checked.

The same change fixed something I had not diagnosed yet. `\hfill` emits no
space glyph, and pypdf only infers one across it when the font does not change:
so `\large` employer + normal-size date welded into `Wemark Real EstateMar
2024`, and italic stack + upright status welded into `AWS EC2Live, paid`,
`RAGOpen source`, `extensionmacOS`. Putting the same font on both sides of
every `\hfill` fixed all of them.

The check compares the profile's own facts against **both** extractions and
fails on any that only one can find. Deliberately scoped to facts rather than
whole text: the two extractors are allowed to disagree about layout — that is
what `extractor_agreement`'s 90% tolerance is for — but not about whether your
employer appears in your resume.

#### 8. `contact_line_not_truncated` — a field wrapped off the end of the contact strip

Your contact strip is one centred line. Five fields do not fit the measure:

```
+61 450 106 807 | mohdisa233@gmail.com | Adelaide, SA | linkedin.com/in/4mohdisa | github.com/4mohdisa |
isaxcode.com
```

The line ends on a **dangling separator with nothing after it**, and
`isaxcode.com` is orphaned onto its own line — directly between the contact
block and the first section heading, which is the slot a parser reads as a
headline. Splitting that line on `|` yields a trailing empty field, and the
gate's own model of an ATS (in the `contact_line_uncontaminated` comment) is
that it "takes the trailing run of that line as the location". The trailing run
is empty and the field before it is a GitHub URL.

It is also width-fragile: one more character in the location and `Adelaide, SA`
itself falls off, with nothing noticing.

Both templates now use a deliberate two-line contact block. The check looks for
a **stranded separator** — leading, trailing, or doubled — on any line carrying
the email. Keying on the stranded separator rather than on a list of expected
fields is what keeps a two-line contact block and the cover letter's signature
line legal; an earlier draft that asserted "the email line must contain every
URL field" false-positived on both, and on its own control.

**It found a second instance immediately.** I fixed the resume, rebuilt, and
the check failed the *cover letter*, whose strip overflows at 11pt for the same
reason. I had not noticed.

### Also observed in the extracted text, deliberately NOT turned into checks

- **Overlapping employment.** Neutral Base LLC (Jul–Dec 2025) sits inside the
  Wemark Real Estate range (Mar 2024 – Feb 2026). That is your history, not a
  document defect, and a gate that second-guessed it would be wrong.
- **In `combined.pdf` the cover letter falls after the resume's REFERENCES
  heading**, so a section-following parser attributes the letter to that
  section, and the letter's recipient block (`Talent Acquisition Team` /
  title / company / location) reads like a dated employment record at the
  company being applied to. Real, but inherent to concatenating two documents,
  and the fix is a design decision about `combined.pdf` rather than a check.
  Flagged for you.
- **`Lumo` links to `github.com/4mohdisa/VeltAI`.** That is what your `main.tex`
  says, so that is what was imported. Change it in the profile if it is stale.
- Date ranges read "Feb 2026 to Present" with the word "to", as yours do. Kept
  — an en-dash is what the previous session flagged as an ATS date-parser risk.

### Two fixes to things that were already there

- **`validate_placeholders` reported every loop variable as an unknown
  namespace.** `\VAR{role.title}` inside `\BLOCK{for role in ...}` produced
  "'role' is not a known namespace" — fourteen of them for the shipped resume
  alone, so the dashboard's template editor showed a wall of errors that were
  not errors and a real typo had nowhere to stand out. It now collects the
  names a template binds for itself.
- **`find_ai_slots` scanned `\VAR{...}` only.** The resume reads `ai.bullets`
  from a block expression, so the resume reported itself as using no AI slots —
  and a slot that is used but not generated is a `StrictUndefined` failure at
  render time, on every build. It now scans block expressions too.
- **`preview_ai_context` is new and shared.** Three places were building the
  `ai` context by hand (the template editor's preview endpoint, the
  hostile-documents fixture, the builder), and `ai.bullets` is shaped
  differently from the prose slots. A hand-built one is free to be a shape the
  shipped template never sees, which is exactly what happened.

### Tests that could not fail

**17 mutations, 17 killed, 0 survivors.** Every new check and every new guard
was mutated and the naming test confirmed to fail. The harness records whether
the target string was *found* before editing, because a mutation that does not
apply looks identical to a test that caught it — which has fooled this project
before.

The mutations that mattered most, because each one is a plausible way to write
the check slightly wrong:

- glued-word probe run at `x_tolerance=3.0` (the same as the default, so it can
  never disagree with it) → killed
- glued-word probe keyed on the x range only, ignoring which line a piece is on
  → killed by the two **acceptance** tests, not the rejection test: it makes
  every word on the page look glued
- `record_headings_start_a_line` relaxed from `line.startswith(heading)` to
  `heading in line` → killed
- `facts_survive_both_extractors` comparing pdfplumber against itself → killed
- `contact_line_not_truncated` no longer anchored at end-of-line → killed
- `generate_role_bullets` iterating `profile.experience` instead of the same
  `_normalise_rows(...)` the template loops over → killed

That last one was a **real bug in my own new code**, found by an adversarial
pass rather than by me. `ai.bullets` is indexed by loop position; the template
iterates normalised rows, which drop anything that is not a dict. One junk row
in `experience` and every later role silently inherits the previous employer's
bullet points — a fabrication containing no invented words, which nothing
downstream could catch. Both lists now come from the same function.

Each rejection test also asserts the failure is the **only** one, and asserts
its own premise (that the fixture really is broken in the way it claims), so a
fixture that quietly stops reproducing the defect fails rather than passes.


### Extracted text — all nine PDFs

Verbatim `pdfplumber` output, which is roughly what an ATS sees.

#### Job 1 (Energy Logistix) — resume.pdf

```
Mohammed Isa
+61 450 106 807 | mohdisa233@gmail.com | Adelaide, SA
linkedin.com/in/4mohdisa | github.com/4mohdisa | isaxcode.com
TECHNICAL SKILLS
TypeScript, JavaScript, Python, Go, Rust, Swift, Dart, SQL, React, Next.js, React Native, Expo, Flutter, Tailwind
CSS, Node.js, FastAPI, Convex, REST APIs, Clerk, Stripe, AWS EC2, Cloudflare R2/D1, GCP, Supabase,
PostgreSQL, Vercel, LLM integration, OpenAI & Gemini APIs, GPT-4o Vision, Whisper, RAG pipelines, prompt
engineering, multi-model orchestration, agentic workflows, MCP, computer vision, OCR, Git, Docker, GitHub Actions,
Playwright, Testing, Documentation
EXPERIENCE
Magain Real Estate Feb 2026 to Present
Property Manager Adelaide, SA
• Manage digital property workflows, documentation, records, and system updates.
• Use PropertyMe and related platforms for operations and reporting.
• Build internal AI tooling to automate document drafting and record reconciliation.
Neutral Base LLC Jul 2025 to Dec 2025
Junior Software Engineer, Contract Remote
• Architected universal S3 storage component for Convex providers.
• Integrated Cloudflare R2, GCP uploads, CORS, and Clerk workflows.
• Contributed macOS app features and Cloudflare database workflows.
Wemark Real Estate Mar 2024 to Feb 2026
Property Manager and Systems Support Adelaide, SA
• Maintained property systems, digital records, documents, and workflow updates.
• Supported internal IT issues, platform usage, and process improvements.
• Managed high-volume communications, reporting, invoices, and operational data.
Vericent Sep 2023 to Feb 2024
Junior Software Engineer, Contract Remote
• Built fraud detection software for enterprise real estate operations.
• Developed SQL pipelines, Python detection logic, and Go services.
StepSharp Digital May 2023 to Sep 2023
Web Developer and Project Coordinator Adelaide, SA
• Built responsive websites with frontend and server-side integrations.
• Managed deployments, testing, security checks, and client delivery tasks.
PROJECTS
Applyable | Next.js, FastAPI, Gemini API, LaTeX, Stripe, AWS EC2 Live, paid
LLM-powered resume engine that rewrites a resume against a specific job ad, scores it the way an ATS would, and
drafts the cover letter. Prompt pipeline tuned for factual grounding so the model reshapes real experience instead of
inventing it. Built solo end to end with subscription billing in production.
Datavisual Studio | Next.js, FastAPI, PostgreSQL, multi-model LLM, Caddy, EC2 Live
Upload a messy CSV and get a working dashboard plus a research write-up synthesised from several models at once.
Multi-model orchestration layer fans a prompt out across providers and reconciles the answers; aggregation stays
deterministic so the numbers never come from a model.
Lumo | Expo React Native, Go, Supabase, Clerk, vision models, RevenueCat iOS
Calorie and gym tracking for iOS. Log a meal by photo and a vision model identifies the food and portion; barcode and
nutrition-label paths cover the rest. Daily energy balance computed from the published ACSM metabolic equations
rather than a flat multiplier. Go API, Expo app, 317 tests.
Motionaire | Tauri v2, Rust, wgpu, React, FFmpeg, LLM agent Open source
Desktop video editor with a natural-language timeline: describe the cut you want and an LLM agent translates it into
timeline operations. Rust and wgpu handle compositing, React drives the timeline UI, FFmpeg does the export.
Mindbase | Next.js, MCP, Tauri v2, PostgreSQL, retrieval Live
Shared memory layer so AI agents stop re-asking things the team already answered. Ships a Model Context Protocol
server that any MCP-capable agent can query, a human review console for curating what gets remembered, and a
macOS companion app.
VisionExtract | Next.js, TypeScript, GPT-4o Vision, OCR Live
Computer-vision text extraction from any document or photo across 50+ languages, using GPT-4o Vision where
classical OCR breaks down on handwriting and poor scans. Privacy-first: pages are processed and discarded rather
than stored.
Crawl2AI | Python, FastAPI, Playwright, Next.js, RAG Open source
RAG ingestion crawler that walks documentation sidebars and pagination links and returns clean, chunk-ready
Markdown for feeding to an LLM. Playwright handles JavaScript-rendered docs that plain scrapers miss.
accrual-audit | TypeScript, deterministic engine Open source
Rent ledger audit engine: coverage-based payment allocation, gap detection, late payment events, and period
reconciliation. Deliberately deterministic rather than model-driven, so every arrears figure is reproducible and
defensible in a tribunal.
Renlio | Next.js, TypeScript, Clerk, Tailwind, shadcn/ui In progress
Rental management tool built from the property manager side of the desk: listings, tenant applications, and the
paperwork trail in one place instead of four inboxes.
X-Finder | Flutter, Dart, Python TestFlight
Cross-platform mobile app that resolves a single username into public profiles across many online platforms in real
time. Flutter client, Python aggregation backend, shipped to Apple TestFlight.
NeutralDrive | Swift, Xcode, File Provider extension macOS
Native macOS client that surfaces remote object storage in Finder through a file provider extension, so files sync on
demand rather than downloading a whole bucket.
Crime Management System | JavaScript, Node.js, SQL Open source
Web platform for police case reporting and investigation workflows, with role-based access control and full audit
tracking on every record change.
EDUCATION
Performance Education Jan 2026 to Dec 2026
Professional Year Program, ICT Adelaide, SA
Torrens University Australia May 2023 to May 2025
Bachelor of Information Technology Adelaide, SA
42 Adelaide Piscine Jan 2023
Intensive Programming Bootcamp Adelaide, SA
REFERENCES
Professional references are available upon request.
```

#### Job 1 (Energy Logistix) — cover_letter.pdf

```
Mohammed Isa
Adelaide, SA
+61 450 106 807 | mohdisa233@gmail.com
linkedin.com/in/4mohdisa | github.com/4mohdisa | isaxcode.com
05 September 2026
Talent Acquisition Team
Data Analyst
Energy Logistix
Largs North, Adelaide SA
Re: Data Analyst
Dear Hiring Team,
My most recent engineering work has been building and shipping data-facing web applications end
to end, which is the substance of what this role asks for.
The advertisement describes a team that owns its own reporting and tooling rather than
outsourcing it, which is the kind of work I have chosen deliberately in every role so far.
My day to day has been Python, TypeScript and SQL against real production data: pipelines at
Vericent, reporting and operational data at Wemark Real Estate, and storage and upload
infrastructure at Neutral Base LLC.
I would welcome a conversation about how this fits what the team needs.
Yours sincerely,
Mohammed Isa
mohdisa233@gmail.com | +61 450 106 807
```

#### Job 1 (Energy Logistix) — combined.pdf

```
Mohammed Isa
+61 450 106 807 | mohdisa233@gmail.com | Adelaide, SA
linkedin.com/in/4mohdisa | github.com/4mohdisa | isaxcode.com
TECHNICAL SKILLS
TypeScript, JavaScript, Python, Go, Rust, Swift, Dart, SQL, React, Next.js, React Native, Expo, Flutter, Tailwind
CSS, Node.js, FastAPI, Convex, REST APIs, Clerk, Stripe, AWS EC2, Cloudflare R2/D1, GCP, Supabase,
PostgreSQL, Vercel, LLM integration, OpenAI & Gemini APIs, GPT-4o Vision, Whisper, RAG pipelines, prompt
engineering, multi-model orchestration, agentic workflows, MCP, computer vision, OCR, Git, Docker, GitHub Actions,
Playwright, Testing, Documentation
EXPERIENCE
Magain Real Estate Feb 2026 to Present
Property Manager Adelaide, SA
• Manage digital property workflows, documentation, records, and system updates.
• Use PropertyMe and related platforms for operations and reporting.
• Build internal AI tooling to automate document drafting and record reconciliation.
Neutral Base LLC Jul 2025 to Dec 2025
Junior Software Engineer, Contract Remote
• Architected universal S3 storage component for Convex providers.
• Integrated Cloudflare R2, GCP uploads, CORS, and Clerk workflows.
• Contributed macOS app features and Cloudflare database workflows.
Wemark Real Estate Mar 2024 to Feb 2026
Property Manager and Systems Support Adelaide, SA
• Maintained property systems, digital records, documents, and workflow updates.
• Supported internal IT issues, platform usage, and process improvements.
• Managed high-volume communications, reporting, invoices, and operational data.
Vericent Sep 2023 to Feb 2024
Junior Software Engineer, Contract Remote
• Built fraud detection software for enterprise real estate operations.
• Developed SQL pipelines, Python detection logic, and Go services.
StepSharp Digital May 2023 to Sep 2023
Web Developer and Project Coordinator Adelaide, SA
• Built responsive websites with frontend and server-side integrations.
• Managed deployments, testing, security checks, and client delivery tasks.
PROJECTS
Applyable | Next.js, FastAPI, Gemini API, LaTeX, Stripe, AWS EC2 Live, paid
LLM-powered resume engine that rewrites a resume against a specific job ad, scores it the way an ATS would, and
drafts the cover letter. Prompt pipeline tuned for factual grounding so the model reshapes real experience instead of
inventing it. Built solo end to end with subscription billing in production.
Datavisual Studio | Next.js, FastAPI, PostgreSQL, multi-model LLM, Caddy, EC2 Live
Upload a messy CSV and get a working dashboard plus a research write-up synthesised from several models at once.
Multi-model orchestration layer fans a prompt out across providers and reconciles the answers; aggregation stays
deterministic so the numbers never come from a model.
Lumo | Expo React Native, Go, Supabase, Clerk, vision models, RevenueCat iOS
Calorie and gym tracking for iOS. Log a meal by photo and a vision model identifies the food and portion; barcode and
nutrition-label paths cover the rest. Daily energy balance computed from the published ACSM metabolic equations
rather than a flat multiplier. Go API, Expo app, 317 tests.
Motionaire | Tauri v2, Rust, wgpu, React, FFmpeg, LLM agent Open source
Desktop video editor with a natural-language timeline: describe the cut you want and an LLM agent translates it into
timeline operations. Rust and wgpu handle compositing, React drives the timeline UI, FFmpeg does the export.
Mindbase | Next.js, MCP, Tauri v2, PostgreSQL, retrieval Live
Shared memory layer so AI agents stop re-asking things the team already answered. Ships a Model Context Protocol
server that any MCP-capable agent can query, a human review console for curating what gets remembered, and a
macOS companion app.
VisionExtract | Next.js, TypeScript, GPT-4o Vision, OCR Live
Computer-vision text extraction from any document or photo across 50+ languages, using GPT-4o Vision where
classical OCR breaks down on handwriting and poor scans. Privacy-first: pages are processed and discarded rather
than stored.
Crawl2AI | Python, FastAPI, Playwright, Next.js, RAG Open source
RAG ingestion crawler that walks documentation sidebars and pagination links and returns clean, chunk-ready
Markdown for feeding to an LLM. Playwright handles JavaScript-rendered docs that plain scrapers miss.
accrual-audit | TypeScript, deterministic engine Open source
Rent ledger audit engine: coverage-based payment allocation, gap detection, late payment events, and period
reconciliation. Deliberately deterministic rather than model-driven, so every arrears figure is reproducible and
defensible in a tribunal.
Renlio | Next.js, TypeScript, Clerk, Tailwind, shadcn/ui In progress
Rental management tool built from the property manager side of the desk: listings, tenant applications, and the
paperwork trail in one place instead of four inboxes.
X-Finder | Flutter, Dart, Python TestFlight
Cross-platform mobile app that resolves a single username into public profiles across many online platforms in real
time. Flutter client, Python aggregation backend, shipped to Apple TestFlight.
NeutralDrive | Swift, Xcode, File Provider extension macOS
Native macOS client that surfaces remote object storage in Finder through a file provider extension, so files sync on
demand rather than downloading a whole bucket.
Crime Management System | JavaScript, Node.js, SQL Open source
Web platform for police case reporting and investigation workflows, with role-based access control and full audit
tracking on every record change.
EDUCATION
Performance Education Jan 2026 to Dec 2026
Professional Year Program, ICT Adelaide, SA
Torrens University Australia May 2023 to May 2025
Bachelor of Information Technology Adelaide, SA
42 Adelaide Piscine Jan 2023
Intensive Programming Bootcamp Adelaide, SA
REFERENCES
Professional references are available upon request.
Mohammed Isa
Adelaide, SA
+61 450 106 807 | mohdisa233@gmail.com
linkedin.com/in/4mohdisa | github.com/4mohdisa | isaxcode.com
05 September 2026
Talent Acquisition Team
Data Analyst
Energy Logistix
Largs North, Adelaide SA
Re: Data Analyst
Dear Hiring Team,
My most recent engineering work has been building and shipping data-facing web applications end
to end, which is the substance of what this role asks for.
The advertisement describes a team that owns its own reporting and tooling rather than
outsourcing it, which is the kind of work I have chosen deliberately in every role so far.
My day to day has been Python, TypeScript and SQL against real production data: pipelines at
Vericent, reporting and operational data at Wemark Real Estate, and storage and upload
infrastructure at Neutral Base LLC.
I would welcome a conversation about how this fits what the team needs.
Yours sincerely,
Mohammed Isa
mohdisa233@gmail.com | +61 450 106 807
```

#### Job 4 (ACH Group) — resume.pdf

```
Mohammed Isa
+61 450 106 807 | mohdisa233@gmail.com | Adelaide, SA
linkedin.com/in/4mohdisa | github.com/4mohdisa | isaxcode.com
TECHNICAL SKILLS
TypeScript, JavaScript, Python, Go, Rust, Swift, Dart, SQL, React, Next.js, React Native, Expo, Flutter, Tailwind
CSS, Node.js, FastAPI, Convex, REST APIs, Clerk, Stripe, AWS EC2, Cloudflare R2/D1, GCP, Supabase,
PostgreSQL, Vercel, LLM integration, OpenAI & Gemini APIs, GPT-4o Vision, Whisper, RAG pipelines, prompt
engineering, multi-model orchestration, agentic workflows, MCP, computer vision, OCR, Git, Docker, GitHub Actions,
Playwright, Testing, Documentation
EXPERIENCE
Magain Real Estate Feb 2026 to Present
Property Manager Adelaide, SA
• Manage digital property workflows, documentation, records, and system updates.
• Use PropertyMe and related platforms for operations and reporting.
• Build internal AI tooling to automate document drafting and record reconciliation.
Neutral Base LLC Jul 2025 to Dec 2025
Junior Software Engineer, Contract Remote
• Architected universal S3 storage component for Convex providers.
• Integrated Cloudflare R2, GCP uploads, CORS, and Clerk workflows.
• Contributed macOS app features and Cloudflare database workflows.
Wemark Real Estate Mar 2024 to Feb 2026
Property Manager and Systems Support Adelaide, SA
• Maintained property systems, digital records, documents, and workflow updates.
• Supported internal IT issues, platform usage, and process improvements.
• Managed high-volume communications, reporting, invoices, and operational data.
Vericent Sep 2023 to Feb 2024
Junior Software Engineer, Contract Remote
• Built fraud detection software for enterprise real estate operations.
• Developed SQL pipelines, Python detection logic, and Go services.
StepSharp Digital May 2023 to Sep 2023
Web Developer and Project Coordinator Adelaide, SA
• Built responsive websites with frontend and server-side integrations.
• Managed deployments, testing, security checks, and client delivery tasks.
PROJECTS
Applyable | Next.js, FastAPI, Gemini API, LaTeX, Stripe, AWS EC2 Live, paid
LLM-powered resume engine that rewrites a resume against a specific job ad, scores it the way an ATS would, and
drafts the cover letter. Prompt pipeline tuned for factual grounding so the model reshapes real experience instead of
inventing it. Built solo end to end with subscription billing in production.
Datavisual Studio | Next.js, FastAPI, PostgreSQL, multi-model LLM, Caddy, EC2 Live
Upload a messy CSV and get a working dashboard plus a research write-up synthesised from several models at once.
Multi-model orchestration layer fans a prompt out across providers and reconciles the answers; aggregation stays
deterministic so the numbers never come from a model.
Lumo | Expo React Native, Go, Supabase, Clerk, vision models, RevenueCat iOS
Calorie and gym tracking for iOS. Log a meal by photo and a vision model identifies the food and portion; barcode and
nutrition-label paths cover the rest. Daily energy balance computed from the published ACSM metabolic equations
rather than a flat multiplier. Go API, Expo app, 317 tests.
Motionaire | Tauri v2, Rust, wgpu, React, FFmpeg, LLM agent Open source
Desktop video editor with a natural-language timeline: describe the cut you want and an LLM agent translates it into
timeline operations. Rust and wgpu handle compositing, React drives the timeline UI, FFmpeg does the export.
Mindbase | Next.js, MCP, Tauri v2, PostgreSQL, retrieval Live
Shared memory layer so AI agents stop re-asking things the team already answered. Ships a Model Context Protocol
server that any MCP-capable agent can query, a human review console for curating what gets remembered, and a
macOS companion app.
VisionExtract | Next.js, TypeScript, GPT-4o Vision, OCR Live
Computer-vision text extraction from any document or photo across 50+ languages, using GPT-4o Vision where
classical OCR breaks down on handwriting and poor scans. Privacy-first: pages are processed and discarded rather
than stored.
Crawl2AI | Python, FastAPI, Playwright, Next.js, RAG Open source
RAG ingestion crawler that walks documentation sidebars and pagination links and returns clean, chunk-ready
Markdown for feeding to an LLM. Playwright handles JavaScript-rendered docs that plain scrapers miss.
accrual-audit | TypeScript, deterministic engine Open source
Rent ledger audit engine: coverage-based payment allocation, gap detection, late payment events, and period
reconciliation. Deliberately deterministic rather than model-driven, so every arrears figure is reproducible and
defensible in a tribunal.
Renlio | Next.js, TypeScript, Clerk, Tailwind, shadcn/ui In progress
Rental management tool built from the property manager side of the desk: listings, tenant applications, and the
paperwork trail in one place instead of four inboxes.
X-Finder | Flutter, Dart, Python TestFlight
Cross-platform mobile app that resolves a single username into public profiles across many online platforms in real
time. Flutter client, Python aggregation backend, shipped to Apple TestFlight.
NeutralDrive | Swift, Xcode, File Provider extension macOS
Native macOS client that surfaces remote object storage in Finder through a file provider extension, so files sync on
demand rather than downloading a whole bucket.
Crime Management System | JavaScript, Node.js, SQL Open source
Web platform for police case reporting and investigation workflows, with role-based access control and full audit
tracking on every record change.
EDUCATION
Performance Education Jan 2026 to Dec 2026
Professional Year Program, ICT Adelaide, SA
Torrens University Australia May 2023 to May 2025
Bachelor of Information Technology Adelaide, SA
42 Adelaide Piscine Jan 2023
Intensive Programming Bootcamp Adelaide, SA
REFERENCES
Professional references are available upon request.
```

#### Job 4 (ACH Group) — cover_letter.pdf

```
Mohammed Isa
Adelaide, SA
+61 450 106 807 | mohdisa233@gmail.com
linkedin.com/in/4mohdisa | github.com/4mohdisa | isaxcode.com
05 September 2026
Talent Acquisition Team
Business Analyst
ACH Group
Adelaide SA
Re: Business Analyst
Dear Hiring Team,
My most recent engineering work has been building and shipping data-facing web applications end
to end, which is the substance of what this role asks for.
The advertisement describes a team that owns its own reporting and tooling rather than
outsourcing it, which is the kind of work I have chosen deliberately in every role so far.
My day to day has been Python, TypeScript and SQL against real production data: pipelines at
Vericent, reporting and operational data at Wemark Real Estate, and storage and upload
infrastructure at Neutral Base LLC.
I would welcome a conversation about how this fits what the team needs.
Yours sincerely,
Mohammed Isa
mohdisa233@gmail.com | +61 450 106 807
```

#### Job 4 (ACH Group) — combined.pdf

```
Mohammed Isa
+61 450 106 807 | mohdisa233@gmail.com | Adelaide, SA
linkedin.com/in/4mohdisa | github.com/4mohdisa | isaxcode.com
TECHNICAL SKILLS
TypeScript, JavaScript, Python, Go, Rust, Swift, Dart, SQL, React, Next.js, React Native, Expo, Flutter, Tailwind
CSS, Node.js, FastAPI, Convex, REST APIs, Clerk, Stripe, AWS EC2, Cloudflare R2/D1, GCP, Supabase,
PostgreSQL, Vercel, LLM integration, OpenAI & Gemini APIs, GPT-4o Vision, Whisper, RAG pipelines, prompt
engineering, multi-model orchestration, agentic workflows, MCP, computer vision, OCR, Git, Docker, GitHub Actions,
Playwright, Testing, Documentation
EXPERIENCE
Magain Real Estate Feb 2026 to Present
Property Manager Adelaide, SA
• Manage digital property workflows, documentation, records, and system updates.
• Use PropertyMe and related platforms for operations and reporting.
• Build internal AI tooling to automate document drafting and record reconciliation.
Neutral Base LLC Jul 2025 to Dec 2025
Junior Software Engineer, Contract Remote
• Architected universal S3 storage component for Convex providers.
• Integrated Cloudflare R2, GCP uploads, CORS, and Clerk workflows.
• Contributed macOS app features and Cloudflare database workflows.
Wemark Real Estate Mar 2024 to Feb 2026
Property Manager and Systems Support Adelaide, SA
• Maintained property systems, digital records, documents, and workflow updates.
• Supported internal IT issues, platform usage, and process improvements.
• Managed high-volume communications, reporting, invoices, and operational data.
Vericent Sep 2023 to Feb 2024
Junior Software Engineer, Contract Remote
• Built fraud detection software for enterprise real estate operations.
• Developed SQL pipelines, Python detection logic, and Go services.
StepSharp Digital May 2023 to Sep 2023
Web Developer and Project Coordinator Adelaide, SA
• Built responsive websites with frontend and server-side integrations.
• Managed deployments, testing, security checks, and client delivery tasks.
PROJECTS
Applyable | Next.js, FastAPI, Gemini API, LaTeX, Stripe, AWS EC2 Live, paid
LLM-powered resume engine that rewrites a resume against a specific job ad, scores it the way an ATS would, and
drafts the cover letter. Prompt pipeline tuned for factual grounding so the model reshapes real experience instead of
inventing it. Built solo end to end with subscription billing in production.
Datavisual Studio | Next.js, FastAPI, PostgreSQL, multi-model LLM, Caddy, EC2 Live
Upload a messy CSV and get a working dashboard plus a research write-up synthesised from several models at once.
Multi-model orchestration layer fans a prompt out across providers and reconciles the answers; aggregation stays
deterministic so the numbers never come from a model.
Lumo | Expo React Native, Go, Supabase, Clerk, vision models, RevenueCat iOS
Calorie and gym tracking for iOS. Log a meal by photo and a vision model identifies the food and portion; barcode and
nutrition-label paths cover the rest. Daily energy balance computed from the published ACSM metabolic equations
rather than a flat multiplier. Go API, Expo app, 317 tests.
Motionaire | Tauri v2, Rust, wgpu, React, FFmpeg, LLM agent Open source
Desktop video editor with a natural-language timeline: describe the cut you want and an LLM agent translates it into
timeline operations. Rust and wgpu handle compositing, React drives the timeline UI, FFmpeg does the export.
Mindbase | Next.js, MCP, Tauri v2, PostgreSQL, retrieval Live
Shared memory layer so AI agents stop re-asking things the team already answered. Ships a Model Context Protocol
server that any MCP-capable agent can query, a human review console for curating what gets remembered, and a
macOS companion app.
VisionExtract | Next.js, TypeScript, GPT-4o Vision, OCR Live
Computer-vision text extraction from any document or photo across 50+ languages, using GPT-4o Vision where
classical OCR breaks down on handwriting and poor scans. Privacy-first: pages are processed and discarded rather
than stored.
Crawl2AI | Python, FastAPI, Playwright, Next.js, RAG Open source
RAG ingestion crawler that walks documentation sidebars and pagination links and returns clean, chunk-ready
Markdown for feeding to an LLM. Playwright handles JavaScript-rendered docs that plain scrapers miss.
accrual-audit | TypeScript, deterministic engine Open source
Rent ledger audit engine: coverage-based payment allocation, gap detection, late payment events, and period
reconciliation. Deliberately deterministic rather than model-driven, so every arrears figure is reproducible and
defensible in a tribunal.
Renlio | Next.js, TypeScript, Clerk, Tailwind, shadcn/ui In progress
Rental management tool built from the property manager side of the desk: listings, tenant applications, and the
paperwork trail in one place instead of four inboxes.
X-Finder | Flutter, Dart, Python TestFlight
Cross-platform mobile app that resolves a single username into public profiles across many online platforms in real
time. Flutter client, Python aggregation backend, shipped to Apple TestFlight.
NeutralDrive | Swift, Xcode, File Provider extension macOS
Native macOS client that surfaces remote object storage in Finder through a file provider extension, so files sync on
demand rather than downloading a whole bucket.
Crime Management System | JavaScript, Node.js, SQL Open source
Web platform for police case reporting and investigation workflows, with role-based access control and full audit
tracking on every record change.
EDUCATION
Performance Education Jan 2026 to Dec 2026
Professional Year Program, ICT Adelaide, SA
Torrens University Australia May 2023 to May 2025
Bachelor of Information Technology Adelaide, SA
42 Adelaide Piscine Jan 2023
Intensive Programming Bootcamp Adelaide, SA
REFERENCES
Professional references are available upon request.
Mohammed Isa
Adelaide, SA
+61 450 106 807 | mohdisa233@gmail.com
linkedin.com/in/4mohdisa | github.com/4mohdisa | isaxcode.com
05 September 2026
Talent Acquisition Team
Business Analyst
ACH Group
Adelaide SA
Re: Business Analyst
Dear Hiring Team,
My most recent engineering work has been building and shipping data-facing web applications end
to end, which is the substance of what this role asks for.
The advertisement describes a team that owns its own reporting and tooling rather than
outsourcing it, which is the kind of work I have chosen deliberately in every role so far.
My day to day has been Python, TypeScript and SQL against real production data: pipelines at
Vericent, reporting and operational data at Wemark Real Estate, and storage and upload
infrastructure at Neutral Base LLC.
I would welcome a conversation about how this fits what the team needs.
Yours sincerely,
Mohammed Isa
mohdisa233@gmail.com | +61 450 106 807
```

#### Job 17 (SA Water) — resume.pdf

```
Mohammed Isa
+61 450 106 807 | mohdisa233@gmail.com | Adelaide, SA
linkedin.com/in/4mohdisa | github.com/4mohdisa | isaxcode.com
TECHNICAL SKILLS
TypeScript, JavaScript, Python, Go, Rust, Swift, Dart, SQL, React, Next.js, React Native, Expo, Flutter, Tailwind
CSS, Node.js, FastAPI, Convex, REST APIs, Clerk, Stripe, AWS EC2, Cloudflare R2/D1, GCP, Supabase,
PostgreSQL, Vercel, LLM integration, OpenAI & Gemini APIs, GPT-4o Vision, Whisper, RAG pipelines, prompt
engineering, multi-model orchestration, agentic workflows, MCP, computer vision, OCR, Git, Docker, GitHub Actions,
Playwright, Testing, Documentation
EXPERIENCE
Magain Real Estate Feb 2026 to Present
Property Manager Adelaide, SA
• Manage digital property workflows, documentation, records, and system updates.
• Use PropertyMe and related platforms for operations and reporting.
• Build internal AI tooling to automate document drafting and record reconciliation.
Neutral Base LLC Jul 2025 to Dec 2025
Junior Software Engineer, Contract Remote
• Architected universal S3 storage component for Convex providers.
• Integrated Cloudflare R2, GCP uploads, CORS, and Clerk workflows.
• Contributed macOS app features and Cloudflare database workflows.
Wemark Real Estate Mar 2024 to Feb 2026
Property Manager and Systems Support Adelaide, SA
• Maintained property systems, digital records, documents, and workflow updates.
• Supported internal IT issues, platform usage, and process improvements.
• Managed high-volume communications, reporting, invoices, and operational data.
Vericent Sep 2023 to Feb 2024
Junior Software Engineer, Contract Remote
• Built fraud detection software for enterprise real estate operations.
• Developed SQL pipelines, Python detection logic, and Go services.
StepSharp Digital May 2023 to Sep 2023
Web Developer and Project Coordinator Adelaide, SA
• Built responsive websites with frontend and server-side integrations.
• Managed deployments, testing, security checks, and client delivery tasks.
PROJECTS
Applyable | Next.js, FastAPI, Gemini API, LaTeX, Stripe, AWS EC2 Live, paid
LLM-powered resume engine that rewrites a resume against a specific job ad, scores it the way an ATS would, and
drafts the cover letter. Prompt pipeline tuned for factual grounding so the model reshapes real experience instead of
inventing it. Built solo end to end with subscription billing in production.
Datavisual Studio | Next.js, FastAPI, PostgreSQL, multi-model LLM, Caddy, EC2 Live
Upload a messy CSV and get a working dashboard plus a research write-up synthesised from several models at once.
Multi-model orchestration layer fans a prompt out across providers and reconciles the answers; aggregation stays
deterministic so the numbers never come from a model.
Lumo | Expo React Native, Go, Supabase, Clerk, vision models, RevenueCat iOS
Calorie and gym tracking for iOS. Log a meal by photo and a vision model identifies the food and portion; barcode and
nutrition-label paths cover the rest. Daily energy balance computed from the published ACSM metabolic equations
rather than a flat multiplier. Go API, Expo app, 317 tests.
Motionaire | Tauri v2, Rust, wgpu, React, FFmpeg, LLM agent Open source
Desktop video editor with a natural-language timeline: describe the cut you want and an LLM agent translates it into
timeline operations. Rust and wgpu handle compositing, React drives the timeline UI, FFmpeg does the export.
Mindbase | Next.js, MCP, Tauri v2, PostgreSQL, retrieval Live
Shared memory layer so AI agents stop re-asking things the team already answered. Ships a Model Context Protocol
server that any MCP-capable agent can query, a human review console for curating what gets remembered, and a
macOS companion app.
VisionExtract | Next.js, TypeScript, GPT-4o Vision, OCR Live
Computer-vision text extraction from any document or photo across 50+ languages, using GPT-4o Vision where
classical OCR breaks down on handwriting and poor scans. Privacy-first: pages are processed and discarded rather
than stored.
Crawl2AI | Python, FastAPI, Playwright, Next.js, RAG Open source
RAG ingestion crawler that walks documentation sidebars and pagination links and returns clean, chunk-ready
Markdown for feeding to an LLM. Playwright handles JavaScript-rendered docs that plain scrapers miss.
accrual-audit | TypeScript, deterministic engine Open source
Rent ledger audit engine: coverage-based payment allocation, gap detection, late payment events, and period
reconciliation. Deliberately deterministic rather than model-driven, so every arrears figure is reproducible and
defensible in a tribunal.
Renlio | Next.js, TypeScript, Clerk, Tailwind, shadcn/ui In progress
Rental management tool built from the property manager side of the desk: listings, tenant applications, and the
paperwork trail in one place instead of four inboxes.
X-Finder | Flutter, Dart, Python TestFlight
Cross-platform mobile app that resolves a single username into public profiles across many online platforms in real
time. Flutter client, Python aggregation backend, shipped to Apple TestFlight.
NeutralDrive | Swift, Xcode, File Provider extension macOS
Native macOS client that surfaces remote object storage in Finder through a file provider extension, so files sync on
demand rather than downloading a whole bucket.
Crime Management System | JavaScript, Node.js, SQL Open source
Web platform for police case reporting and investigation workflows, with role-based access control and full audit
tracking on every record change.
EDUCATION
Performance Education Jan 2026 to Dec 2026
Professional Year Program, ICT Adelaide, SA
Torrens University Australia May 2023 to May 2025
Bachelor of Information Technology Adelaide, SA
42 Adelaide Piscine Jan 2023
Intensive Programming Bootcamp Adelaide, SA
REFERENCES
Professional references are available upon request.
```

#### Job 17 (SA Water) — cover_letter.pdf

```
Mohammed Isa
Adelaide, SA
+61 450 106 807 | mohdisa233@gmail.com
linkedin.com/in/4mohdisa | github.com/4mohdisa | isaxcode.com
05 September 2026
Talent Acquisition Team
Senior Manager Data, AI and Analytics (Chief Data and AI Officer)
SA Water
Adelaide SA
Re: Senior Manager Data, AI and Analytics (Chief Data and AI Officer)
Dear Hiring Team,
My most recent engineering work has been building and shipping data-facing web applications end
to end, which is the substance of what this role asks for.
The advertisement describes a team that owns its own reporting and tooling rather than
outsourcing it, which is the kind of work I have chosen deliberately in every role so far.
My day to day has been Python, TypeScript and SQL against real production data: pipelines at
Vericent, reporting and operational data at Wemark Real Estate, and storage and upload
infrastructure at Neutral Base LLC.
I would welcome a conversation about how this fits what the team needs.
Yours sincerely,
Mohammed Isa
mohdisa233@gmail.com | +61 450 106 807
```

#### Job 17 (SA Water) — combined.pdf

```
Mohammed Isa
+61 450 106 807 | mohdisa233@gmail.com | Adelaide, SA
linkedin.com/in/4mohdisa | github.com/4mohdisa | isaxcode.com
TECHNICAL SKILLS
TypeScript, JavaScript, Python, Go, Rust, Swift, Dart, SQL, React, Next.js, React Native, Expo, Flutter, Tailwind
CSS, Node.js, FastAPI, Convex, REST APIs, Clerk, Stripe, AWS EC2, Cloudflare R2/D1, GCP, Supabase,
PostgreSQL, Vercel, LLM integration, OpenAI & Gemini APIs, GPT-4o Vision, Whisper, RAG pipelines, prompt
engineering, multi-model orchestration, agentic workflows, MCP, computer vision, OCR, Git, Docker, GitHub Actions,
Playwright, Testing, Documentation
EXPERIENCE
Magain Real Estate Feb 2026 to Present
Property Manager Adelaide, SA
• Manage digital property workflows, documentation, records, and system updates.
• Use PropertyMe and related platforms for operations and reporting.
• Build internal AI tooling to automate document drafting and record reconciliation.
Neutral Base LLC Jul 2025 to Dec 2025
Junior Software Engineer, Contract Remote
• Architected universal S3 storage component for Convex providers.
• Integrated Cloudflare R2, GCP uploads, CORS, and Clerk workflows.
• Contributed macOS app features and Cloudflare database workflows.
Wemark Real Estate Mar 2024 to Feb 2026
Property Manager and Systems Support Adelaide, SA
• Maintained property systems, digital records, documents, and workflow updates.
• Supported internal IT issues, platform usage, and process improvements.
• Managed high-volume communications, reporting, invoices, and operational data.
Vericent Sep 2023 to Feb 2024
Junior Software Engineer, Contract Remote
• Built fraud detection software for enterprise real estate operations.
• Developed SQL pipelines, Python detection logic, and Go services.
StepSharp Digital May 2023 to Sep 2023
Web Developer and Project Coordinator Adelaide, SA
• Built responsive websites with frontend and server-side integrations.
• Managed deployments, testing, security checks, and client delivery tasks.
PROJECTS
Applyable | Next.js, FastAPI, Gemini API, LaTeX, Stripe, AWS EC2 Live, paid
LLM-powered resume engine that rewrites a resume against a specific job ad, scores it the way an ATS would, and
drafts the cover letter. Prompt pipeline tuned for factual grounding so the model reshapes real experience instead of
inventing it. Built solo end to end with subscription billing in production.
Datavisual Studio | Next.js, FastAPI, PostgreSQL, multi-model LLM, Caddy, EC2 Live
Upload a messy CSV and get a working dashboard plus a research write-up synthesised from several models at once.
Multi-model orchestration layer fans a prompt out across providers and reconciles the answers; aggregation stays
deterministic so the numbers never come from a model.
Lumo | Expo React Native, Go, Supabase, Clerk, vision models, RevenueCat iOS
Calorie and gym tracking for iOS. Log a meal by photo and a vision model identifies the food and portion; barcode and
nutrition-label paths cover the rest. Daily energy balance computed from the published ACSM metabolic equations
rather than a flat multiplier. Go API, Expo app, 317 tests.
Motionaire | Tauri v2, Rust, wgpu, React, FFmpeg, LLM agent Open source
Desktop video editor with a natural-language timeline: describe the cut you want and an LLM agent translates it into
timeline operations. Rust and wgpu handle compositing, React drives the timeline UI, FFmpeg does the export.
Mindbase | Next.js, MCP, Tauri v2, PostgreSQL, retrieval Live
Shared memory layer so AI agents stop re-asking things the team already answered. Ships a Model Context Protocol
server that any MCP-capable agent can query, a human review console for curating what gets remembered, and a
macOS companion app.
VisionExtract | Next.js, TypeScript, GPT-4o Vision, OCR Live
Computer-vision text extraction from any document or photo across 50+ languages, using GPT-4o Vision where
classical OCR breaks down on handwriting and poor scans. Privacy-first: pages are processed and discarded rather
than stored.
Crawl2AI | Python, FastAPI, Playwright, Next.js, RAG Open source
RAG ingestion crawler that walks documentation sidebars and pagination links and returns clean, chunk-ready
Markdown for feeding to an LLM. Playwright handles JavaScript-rendered docs that plain scrapers miss.
accrual-audit | TypeScript, deterministic engine Open source
Rent ledger audit engine: coverage-based payment allocation, gap detection, late payment events, and period
reconciliation. Deliberately deterministic rather than model-driven, so every arrears figure is reproducible and
defensible in a tribunal.
Renlio | Next.js, TypeScript, Clerk, Tailwind, shadcn/ui In progress
Rental management tool built from the property manager side of the desk: listings, tenant applications, and the
paperwork trail in one place instead of four inboxes.
X-Finder | Flutter, Dart, Python TestFlight
Cross-platform mobile app that resolves a single username into public profiles across many online platforms in real
time. Flutter client, Python aggregation backend, shipped to Apple TestFlight.
NeutralDrive | Swift, Xcode, File Provider extension macOS
Native macOS client that surfaces remote object storage in Finder through a file provider extension, so files sync on
demand rather than downloading a whole bucket.
Crime Management System | JavaScript, Node.js, SQL Open source
Web platform for police case reporting and investigation workflows, with role-based access control and full audit
tracking on every record change.
EDUCATION
Performance Education Jan 2026 to Dec 2026
Professional Year Program, ICT Adelaide, SA
Torrens University Australia May 2023 to May 2025
Bachelor of Information Technology Adelaide, SA
42 Adelaide Piscine Jan 2023
Intensive Programming Bootcamp Adelaide, SA
REFERENCES
Professional references are available upon request.
Mohammed Isa
Adelaide, SA
+61 450 106 807 | mohdisa233@gmail.com
linkedin.com/in/4mohdisa | github.com/4mohdisa | isaxcode.com
05 September 2026
Talent Acquisition Team
Senior Manager Data, AI and Analytics (Chief Data and AI Officer)
SA Water
Adelaide SA
Re: Senior Manager Data, AI and Analytics (Chief Data and AI Officer)
Dear Hiring Team,
My most recent engineering work has been building and shipping data-facing web applications end
to end, which is the substance of what this role asks for.
The advertisement describes a team that owns its own reporting and tooling rather than
outsourcing it, which is the kind of work I have chosen deliberately in every role so far.
My day to day has been Python, TypeScript and SQL against real production data: pipelines at
Vericent, reporting and operational data at Wemark Real Estate, and storage and upload
infrastructure at Neutral Base LLC.
I would welcome a conversation about how this fits what the team needs.
Yours sincerely,
Mohammed Isa
mohdisa233@gmail.com | +61 450 106 807
```

## Phase 2 — browser tab lifecycle  (`feat/tab-management`)

### What it did before

`run_apply_pass` opened **one** page on first use and handed the same object to
every application in the pass. Nothing closed it, and nothing closed anything an
application opened for itself. The only close in the pass was
`context.close()` in the `finally` — which closes the *session*, at the end.

So a pass was not leaking a tab per application in the sense of a bug; it simply
had no page lifecycle at all. Nothing accumulated across a *short* run because
there was only ever one page. But nothing could clean up after an ATS that
opened its form in a popup either, and a long unattended run had no upper bound
on what a single reused page carried.

### What it does now

**`backend/apply/pages.py`** — one small module, four functions, and a single
idea: the **context** is the session and must outlive the pass; **pages** are
disposable.

| | |
|---|---|
| `application_page(context, job_id=...)` | Context manager. Opens a page, closes it in a `finally` — submitted, abstained, blocked, failed or exploded. Also closes anything the application opened *after* it (a popup form, a preview tab), before closing itself. Never touches the context. |
| `warn_unless_single_page(context, when=...)` | Returns True when the anchor page is the only one open. Otherwise `log.error("page_leak_detected", ...)` with the URLs. Loud, and does not stop the pass. |
| `close_orphan_pages(context, keep=anchor, cap=MAX_OPEN_PAGES)` | Over the cap, closes the oldest orphans and warns. The anchor is never closed however many pages are open — the pass still needs somewhere to check the session. |
| `open_pages(context)` | Safe accessor. A closed context raises on `.pages`, and a test's bare page has no context; both answer "no pages" so every caller degrades to doing nothing. |

`MAX_OPEN_PAGES = 4`, not 1: one application legitimately holds two for a
moment (anchor + its own), and an ATS popup makes three. Four leaves room for
that without letting a leak run all night.

**In `run_apply_pass`:**

- The single shared page became an **anchor page** — one per pass, used only for
  the session health check, `ensure_logged_in`, and nothing else. It stays open
  deliberately: it is the page the context is guaranteed to still have when an
  application's own page has been closed.
- Each job runs inside `with application_scope(job.id) as page:`.
- The white-labelled-ATS HTML probe (`_applier_from_page`) now runs on the
  **application's own page**, not the anchor. It navigates, and the anchor is
  what the session checks run on — leaving it parked on the last job's ad was
  quietly wrong.
- **Pacing moved to before the page is opened.** The wait between submits is
  minutes long, and holding a tab open across it is precisely the accumulation
  this is meant to prevent.
- `warn_unless_single_page` + `close_orphan_pages` run at the **top** of each
  iteration rather than the bottom. The body can leave by `continue`, by `break`
  or by raising, and only the top of the next iteration sees all three.
- One final `warn_unless_single_page` in the `finally`, before the context
  closes — a leak on the last application would otherwise be hidden by the
  close.
- `context.close()` is still the only close of a context, still at the very end.

There is also a new `context_factory` parameter, purely so the lifecycle is
testable without Playwright. A Playwright context is a page factory with a list
on it, so the fake is thin and `run_apply_pass` runs its real code path in the
tests rather than a test-only branch.

### Verified against real headful Chrome

`channel="chrome"`, headful, a throwaway `user-data-dir` — deliberately **not**
`data/browser_profile`, because the point is to prove Chrome behaves, not to
poke your live LinkedIn session. Ten applications in sequence, each opening a
page, navigating to a document with a real DOM, and closing it.

```
chrome pid 33173, baseline RSS 877 MB, pages open 1
round  during  after   RSS MB  single?
    1       2      1      912  True
    2       2      1      917  True
    3       2      1      923  True
    ...
   10       2      1      945  True

cap check
  opened 7 pages, closed 3, left 4 (cap 4)
  anchor still usable: True

RSS first 5 rounds 921 MB, last 5 936 MB, drift +15 MB
all checks passed
```

Two pages open during each application, one after, every time. The cap sweep
took the three orphans and kept the anchor, and the anchor still navigated and
returned content afterwards — the context survived.

**The control, same script with nothing closing the pages** — which is what the
pass did before:

```
CONTROL — nothing closes the pages. baseline RSS 864 MB
round  open   RSS MB
    1     2     1019
    5     6     1601
   10    11     2288

pages left open: 11
RSS first 5 1315 MB, last 5 2021 MB, drift +706 MB
```

**864 MB to 2288 MB across ten pages — about 142 MB per application.** At sixty
jobs in an overnight run that is roughly 8.5 GB of resident memory in a browser
that is expected to stay up for days. With the fix the same ten rounds drift
15 MB, which is noise.

### Tests that could not fail

`tests/test_tab_lifecycle.py`, 20 tests. **11 mutations, 11 killed** — but only
after three of them survived the first attempt, and all three were the test's
fault, not the mutation's:

1. **"the close is not in a `finally`" survived.** No test made an exception
   propagate *through* the context manager. `run_apply_pass` catches
   `RestrictionDetected`, `SessionExpired` and `Exception` itself, so by the
   time the manager resumes there is nothing in flight and a close written after
   the `yield` with no `finally` runs perfectly well. Every pass-level test
   stayed green while the guard they were named for was gone. Fixed with a
   direct unit test that raises inside the `with` — which is the only place the
   `finally` is actually load-bearing.

2. **"the cap fires under the threshold too" survived.** The "under the cap"
   test opened exactly `MAX_OPEN_PAGES` pages — *on* the cap, not under it —
   where the slice that picks victims comes out empty however the threshold is
   written. Fixed to sit strictly under the cap with more than one closable
   page, and the implementation now names the quantity (`excess = len(pages) -
   cap`) instead of relying on a negative slice being empty by accident.

3. **"the HTML probe goes back to navigating the anchor page" survived.** The
   test only asserted that every application page was closed, which stays true
   when the probe navigates the anchor instead. Fixed to assert the anchor is
   still on `about:blank` and the application's own page is the one carrying the
   job URL.

The rest were killed first time: the page never closed, the context closed along
with the page, a failing close propagating, popups left behind, the leak
assertion always returning True, the cap closing nothing, the cap closing the
anchor, and the pass going back to one shared page.

### Not done, deliberately

- **`backend/apply/canary.py`, `har.py`, `smoke.py` and
  `integrations/scheduler.py` all open a page and close only the context.** They
  are one-shot tools that exit immediately afterwards, so there is nothing to
  accumulate; converting them would be churn. The pass is where a run lasts all
  night.
- **No cap on total pages opened per pass** — only on pages open at once. A pass
  that opens and closes 500 pages is a pass with 500 jobs, which is a scheduling
  question, not a leak.

---

# Session — 2026-09-05 (macOS, unattended, four phases)

Merge to main, then question intelligence, performance telemetry and site
knowledge v2. `ALLOW_LIVE_SUBMIT` and `OUTBOUND_ENABLED` were not touched and
are still false. Nothing was submitted and no email was sent.

**Main is at `c9ba16a`: 1012 tests, rehearsal green.** The three feature
branches are stacked PRs on top of it — see "The PR stack" below, and read the
merge order before deleting anything.

## Phase 1 — merged

`feat/real-templates` fast-forwarded; `feat/tab-management` merged with the one
expected NOTES.md conflict (both appended at the end; both sections kept).
Full suite and rehearsal green after each: **992 tests** after the first,
**1012** after the second. Both branches deleted locally and on the remote.

There were no open PRs to retarget — the four stale remote branches the local
checkout still listed (`chore/smoke-tests`, `feat/derivation-preview`,
`feat/outbound-wire`, `feat/setup-doctor`) had already been deleted on GitHub;
`git fetch -p` pruned them. PRs #21 and #22 were opened retroactively so the
two merges have a record, and closed as merged by the push.

## The PR stack

| PR | Branch | Base |
|---|---|---|
| [#23](https://github.com/4mohdisa/JobSeekr/pull/23) | `feat/question-intelligence` | `main` |
| [#24](https://github.com/4mohdisa/JobSeekr/pull/24) | `feat/performance-telemetry` | `feat/question-intelligence` |
| [#25](https://github.com/4mohdisa/JobSeekr/pull/25) | `feat/siteknowledge-v2` | `feat/performance-telemetry` |

Stacked because they genuinely depend on each other: telemetry reads the
question ledger #23 adds, and site knowledge v2 uses the weekly digest and the
cache recording from both. **Merge #23 → #24 → #25, retargeting each next PR to
its new base BEFORE deleting the merged branch.** Deleting a base auto-closes
the PRs targeting it and GitHub will not reopen them.

This whole section lands with #25, so #23 and #24 carry no NOTES entry of their
own. That is the cost of stacking; the alternative was a NOTES.md conflict on
every merge.

## Phase 2 — question intelligence (#23)

### The denominator had to be built first

Nothing recorded a question that was *answered*. `FailureEvent` holds
abstentions; no `Application` row is written when a job parks. So a submitted
application had zero abstentions **by construction** (`flow.py` returns
`_park` at the first one) and a parked one had no application row at all. The
two tables never describe the same pass, and every coverage ratio computable
from them was a lower bound on one side or the other.

New `question_event` table: one row per screening question per encounter,
carrying which mechanism answered it — bank, fact, form map, or abstained.
Written from `build_draft`, because reconstructing provenance afterwards is
impossible: `_synthetic_answer` stamps `EXACT`/confidence 100.0 on profile,
fact and form-map answers alike, so by the time a draft is finished a
fact-derived answer is indistinguishable from a bank hit.

Profile-filled identity fields are deliberately excluded. A form asking for an
email address is not asking the user anything, and counting it would push
coverage toward 100% by padding the denominator with questions that cannot
fail.

### Clustering reuses the resolver's disqualifiers

`answers.same_question` is the resolution matcher with the answer bank taken
out: the same `_loose` forms, the same rapidfuzz pair, the same
`FUZZY_THRESHOLD`, and the same four disqualifiers. Without the last part,
"Are you available for part-time work?" and "…full-time work?" score **88.9** —
over the threshold — and would be reported as one question. That would tell the
user they had already answered something they had answered the opposite of.

### Two existing numbers were wrong

1. **The funnel's `acknowledged` and `replied` stages were identical by
   construction.** `work.py` inlined a literal copy of the four statuses
   `REPLIED_STATUSES` holds, so the two bars could never disagree for any
   dataset. `replied` now means a person replied, as against an automated
   acknowledgement, derived by subtraction from `REPLIED_STATUSES` so a fifth
   status can only ever be added in one place.
2. **The per-campaign funnel counts submitted applications only.** An aborted
   attempt never reached an employer; counting it depresses every rate below it.
   The existing global funnel counted every `Application` row.

The honest definitions are written into `_campaign_funnels`' docstring:
`discovered` is every job row carrying the campaign id with status ignored (it
is a single mutable field, so filtering it makes the top of the funnel shrink as
the pipeline succeeds); `scored` is DISTINCT job ids with a non-null `final`
(one job legitimately has a Score row per profile/rubric pair, and a stage-2
failure still writes a row with no number in it).

### Fact leverage lives in facts.py, not with the other aggregates

`tests/test_derivation_preview.py` enforces that `DerivedAnswer` has exactly one
reader, so the fact-hash staleness check cannot be bypassed. The lazy move was
to add `backend/questions.py` to that allowlist. The right one was to put
`leverage()` in `facts.py`. Widening a safety guard to accommodate new code is
how the guard stops meaning anything.

### The weekly digest is new

There was no weekly digest — only the nightly one, which already carries a
168-hour failure-trends section. Rather than add a fourth thing to the evening
message, `build_weekly_digest()` runs Sunday 18:30 (`/weekly` on demand), half
an hour after the existing rubric review. Question intelligence, performance and
site-knowledge health all report into it. `failures.digest_lines` already names
why: a section that appears every evening saying nothing is a section that stops
being read, on exactly the evening it has something to say.

### The dashboard did not build

Seven pre-existing TypeScript errors — five `Button variant="secondary"`, which
is not one of the four variants, and two `DataTable` calls missing the required
`rowKey`. `npm run build` failed on `main` too; confirmed identical before and
after. Fixed, because "surface it on the Analytics page" is not true of a page
that cannot be built.

### Tests that could not fail

**23 mutations, 23 killed** — but one survived the first attempt and one did not
apply, both worth writing down.

- *"friction ranks by how often asked, not by jobs parked"* **survived.** The
  test set up one question that parked jobs and one that did not, so the
  never-parked *filter* satisfied the assertion and the *ranking* was never
  exercised. Exactly the shape this project keeps hitting: a different guard
  answering for the one under test. Fixed with a second test where both
  questions park jobs and the less-frequent one is more expensive.
- *"the flow records profile-filled identity fields too"* reported **NOT
  APPLIED** — the target string `screening=screening,` appears twice in
  `flow.py`. The harness checks the target was found exactly once before
  editing, because a mutation that does not apply is indistinguishable from a
  test that caught it.

## Phase 3 — performance telemetry (#24)

### What was measured, and what was already there

Nothing. `Run` records a start and an end for a whole pass — browser startup
plus every application plus every pacing wait as one undifferentiated number, in
which a field-enumeration regression and a slow morning are the same figure.
`Application.applied_at` is the only other time record and it is an instant, not
a duration, stamped at the end.

`stage_timing` records six stages per application: page load, field enumeration,
answer resolution, document build, upload, submit. The document build is not on
the apply path at all — a job is only eligible once its documents exist and have
passed the gate — so it is timed in `documents/build.py`.

`cache_event` records the three caches whose lookups are not questions: form
maps per **form shape**, site knowledge per **element**, embeddings per **text**.

### The answer bank and the facts layer are not recorded twice

Their lookups *are* screening questions, and `question_event` already records
the outcome of every one. Writing them to a second table would be the same fact
in two places, free to drift.

They are consulted in **sequence** — facts only see what the bank could not
answer — so their denominators are different populations on purpose. A facts hit
rate computed over every question would fall every time the answer bank
improved, which is the opposite of the truth. Every rate on the chart is
labelled with what one lookup counts, for the same reason.

### Pacing

Recorded as `Stage.PACING`, and structurally excluded from work:

- `WORK_STAGES = frozenset(Stage) - {Stage.PACING}` — defined by subtraction, so
  a stage added later is work unless someone deliberately excludes it.
- `StageProfile` returns `work` and `pacing` as **separate fields**, not one
  list a caller could sum.
- The wire schema keeps them separate, and the UI labels it "deliberate, not
  work".

It is measured rather than ignored because an unmeasured wait is
indistinguishable from a hang. Nothing about pacing itself was changed.

### The rehearsal now asserts the instrumentation is reached

Two new stages: `telemetry wired` and `question ledger wired`. Not "does the
module work" — the unit tests answer that. Whether a full pipeline run leaves
any measurement behind at all. Four features in this project shipped complete,
tested and never called; a stage timer nothing invokes looks identical to a
working one in every unit test. A dry run reaches four of the six stages
(`SUBMIT` and `PACING` are legitimately absent), and those four are what the
stage asserts.

Live proof from one rehearsal: 10 stage timings across `document_build`,
`page_load`, `field_enumeration`, `answer_resolution` and `upload`; 3 embedding
cache events; 2 question events.

### Waste: twelve claims, each verified with real numbers

Every claim was checked against the code by an independent pass whose default
was to refute it. Nine are real and **not worth fixing** — the measurements are
here so nobody re-derives them:

| Claim | Verdict | Measured cost |
|---|---|---|
| ATS probe loads the job URL, then `adapter.open` loads it again | CONFIRMED | 1–4s per probed job, ~1% of pass wall clock against a 90–240s pacing sleep. The obvious `if page.url != job.url` guard is **unreliable** — white-labelled portals redirect, so `page.url` is the final URL and the guard skips the reload on exactly the jobs it was written for |
| `build_draft` re-reads profile, documents, score and the whole answer bank per form step | CONFIRMED | ~12 extra SQLite statements per application, under 5 ms. Identity-map hits, no API cost |
| `eligible_jobs` N+1 | CONFIRMED | 9 queries and 3.1 ms on the real 235-job database |
| `get_analytics` calls `_latest_score` per application | CONFIRMED | Zero today (no application rows). 25–75 ms at 500 applications |
| Combined PDF runs a third fabrication self-check | CONFIRMED | ~$0.001 per job, ~33% of the document self-check spend. **Not fixed — see below** |
| `EmbeddingCache.put` opens/closes the file per vector | CONFIRMED | 7 ms per 200-job cold run, 0.3% of a run that spends seconds on network |
| Month-wide `SUM` over unindexed `called_at` before every LLM call | CONFIRMED | 0.047 ms at 1k rows, 3.24 ms at 100k. 0.02% of the call it guards |
| `needs_scoring` N+1 | CONFIRMED | 12.6 ms at N=200, 558 ms at N=10,000 |
| LinkedIn saves its knowledge file on every `confirmed()` | PARTLY | 0.3 ms and 12.6 KB per attempt. Superseded by phase 4, which moved saving out of the adapters entirely |

Three were worth fixing and are in #24:

1. **The rehearsal ran twice per suite**, once per test, and the second run
   computed nothing the first did not: 8 extra pdflatex subprocesses, 1.45–1.53s
   of a 31.9s suite that runs on every commit (~4.7%, and worse on
   Windows/MiKTeX). One module-scoped fixture.
2. **`eligible_jobs` hydrated every `Application` row** — JSON columns, enums and
   all — to read one integer off each. Now selects the column: 20.1 ms → 1.7 ms
   at a year of applications, identical set.
3. **A blank fact was offered to the derivation step**, costing one model call
   per blank row to be told it says nothing. The filter already existed in
   `preview_all`, so the dry-run preview said "the fact is blank" while the live
   path paid to rediscover it. Moved into `facts_for`, where both callers get it.

Two more were fixed in my own new code before they shipped: the question ledger
was clustered twice per analytics request, and `build_weekly_digest` called
`facts.leverage` twice with no write between.

### `eligible_jobs` had no test coverage at all

Found while mutating fix 2. Emptying the applied set entirely — so a job could
be applied to **twice**, against hard rule 5 — left all 1072 tests green.
`UNIQUE(job_id)` and `_run_apply`'s own check would still have caught it, but
only after a browser tab had opened on the employer's site. There is a test now,
in `tests/test_apply_safety.py` where a reviewer will find it.

### Tests that could not fail

**22 mutations, 22 killed**, plus 2 on the waste fixes. Three survived first
time and all three were the tests' fault:

- *"cost attributes job-less spend to every application"* survived because the
  guard it mutated is defensive rather than behaviour-bearing — a `None` key in
  a defaultdict that nothing reads. Replaced with the plausible wrong version
  (pooling every job's spend), and a test that a second job's spend does not
  land on this application.
- *"site knowledge counts a healed element as a first-strategy hit"* and
  *"drain_resolutions does not reset"* both survived because nothing exercised
  the new tally through a real `resolve` call. Four tests added.

## Phase 4 — site knowledge v2 (#25)

### The bug underneath all six items

`GenericAtsApplier` — one class shared by **all nine** external ATS adapters —
never called `knowledge.save()` at all. Every promotion, every counter, every
flow variant those platforms learned was discarded when the process exited. The
layer learned and then forgot, on two thirds of the platforms it supports.
LinkedIn and Seek each called it from their own `confirmed()`, which is the
wrong moment twice over: not reached on the failure path, and reached on every
poll of the confirmation state.

Saving is now one call in the flow, once per application, however it ended.
`save` is a no-op when nothing changed.

### 1. Learn from success

Every strategy tried *before* the winner is debited and the winner credited, on
every resolution — `Strategy.success_count` / `fail_count`. Ordering became:

1. `last_working_strategy` (freshest evidence — a site that changed, changed)
2. observed reliability
3. platform-before-shared, then durability, as tie-breaks among the unproven

The change is #2. The order used to be the original guess at which selector
*type* is most durable, so a testid that had failed eleven times still went
first because test ids are durable in theory.

### 2. Use the counts

`success_count` and `fail_count` existed since this layer was written and
nothing read them. Confidence is Laplace-smoothed, `(hits + 1) / (tries + 2)`,
which is what makes it usable for ordering rather than only for reporting: an
untried strategy scores exactly 0.5, below anything with a record of working and
above anything with a record of failing. One success out of one gives 0.67, not
1.0 — a single observation should not outrank a strategy that has worked forty
times.

`MIN_OBSERVATIONS = 4` before an element is called degrading. Without it, every
element of a fresh install is reported as degrading on the first digest, and a
report that is wrong on day one is a report nobody reads on day thirty.

### 3. Generate strategies, don't only select them

When every recorded strategy fails, `_propose_strategy` tries the shared
vocabulary's candidates against the live page and stores the first that
resolves as a **proposal**: written to the file, sent to Telegram with the
suggested fix, and **never used to resolve anything** until `/usefix`.

A derived selector is a guess about where the Submit button is. This is the one
place in the project where a guess would be acted on rather than abstained from,
so it goes through the same shape as the answer bank: propose, ask, confirm,
then use. Rejecting deletes it rather than remembering it as wrong, so the next
failure derives again from a fresh look at the page.

### 4. Cross-platform vocabulary

Nine platforms, the same four element keys (`apply_button`, `confirmation`,
`file_input`, `submit_button`), and no shared candidates — so a tenth platform
starts with nothing and any of the nine that loses a selector falls through to
no candidate at all.

`vocabulary.py` holds role-and-accessible-name candidates, merged in at **load**
and never written into a platform file: a correction to the vocabulary must not
have to reach eleven private forks. They are plain dicts in the same shape the
JSON files use, so the vocabulary is data loaded the same way platform knowledge
is, and the module has no import from the package it lives in.

Platform strategies win every tie. Note the level that sits at: a *shared*
candidate that keeps working still outranks a platform one that keeps failing,
because confidence is checked first. Only the unproven are ordered by provenance.

### 5. Drift as a trend

Drift already reached the failure ledger (`notify._record_drift`). What was
missing was the reading of it: `platform_churn` compares this window against the
one immediately before, because the question is not "how many failures" but
"more or fewer than last time".

Elements that drifted last week and not this week are reported as **"quiet
since"**, not "fixed". Nothing here knows the difference between a repair and a
site that was not visited, and the digest says so in the line itself.

### 6. Version and roll back

Every `save` archives the file it is about to replace into
`history/NNNN-elements.json` and indexes it with a reason — `resolution`,
`capture ingest`, `accepted derived strategy`, `superseded by rollback`. Three
things write these files and none of them left a record; a bad ingest was
permanent.

    uv run python -m backend.siteknowledge history linkedin
    uv run python -m backend.siteknowledge rollback linkedin 4
    uv run python -m backend.siteknowledge health

The rollback is itself archived, so rolling back to the wrong version is
undoable too — the reason to roll back at all is that something overwrote a
working file, and a one-way undo just moves which write is unrecoverable.
`HISTORY_LIMIT = 20`.

### Tests that could not fail

**29 mutations, 29 killed.** One survived first time and one did not apply:

- *"the apply flow no longer saves what an application taught"* **survived** —
  nothing asserted that the flow saves at all, which is precisely the gap that
  let nine adapters discard everything they learned in the first place. A test
  now drives a whole application through `run_apply` against a knowledge object
  with a real directory and asserts the counter reached the file.
- *"a duplicate shared candidate is added alongside the platform one"* reported
  NOT APPLIED because `ruff format` had joined the target onto one line.

## Decisions taken without asking

- **Stacked the three PRs** rather than branching all three from `main`. They
  genuinely depend on each other and would otherwise conflict in `models.py`,
  `work.py`, `schemas.py`, `flow.py` and `Analytics.tsx`. The merge order and
  the retarget-before-delete rule are at the top of this section.
- **A new weekly digest** rather than a fourth section in the nightly one.
  Three phases wanted a weekly-cadence report; that is a second caller, not
  speculation.
- **Funnel `acknowledged` = any contact, `replied` = a person replied.**
  `response_status` holds only the latest state, so the stages nest rather than
  partition — an interview counts at all three. That is what makes the funnel
  monotonic and readable.
- **`analytics_min_sample` gates rates, not counts.** Counts are facts about
  what happened; the greying rule is about comparisons. Coverage, interview rate
  and the per-campaign rate are gated; frequency, friction and fact leverage are
  not.
- **Run attribution for timings is by time window**, not a run id, because the
  `Run` row is written at the *end* of a pass. Exact on a single-user machine
  that runs one pass at a time; wrong the moment two passes overlap. The
  alternative — writing the Run row up front — changes what a crashed pass
  leaves behind, which is a bigger change than the problem.
- **Fixed the seven pre-existing TypeScript errors.** Out of scope strictly, but
  the phase-2 deliverable is a dashboard page and the dashboard did not build.

## What broke, and what found it

- **A wrong number, caught by mutation, not by review.** The friction ranking
  test passed against a mutation that reversed the ranking, because the
  never-parked filter satisfied it instead. Fourth session running that this
  exact shape has appeared.
- **`eligible_jobs` had no coverage.** Hard rule 5's first line of defence, and
  emptying it left 1072 tests green.
- **Nine adapters never persisted anything.** Found by an adversarial read, not
  by a test — no test asserted that saving happens.
- **The frontend had not built for some time.** Nothing in the suite runs
  `tsc`, so the backend tests stayed green through it.

## Still unverified

Everything the previous sessions listed, unchanged, plus:

- **No stage timing has ever been recorded from a real browser.** The rehearsal
  proves the timers are reached; every duration in the database so far is a fake
  page returning instantly. The numbers are structurally right and
  quantitatively meaningless until an application runs against a real site.
- **No cache event has been recorded for form maps or site knowledge.** The
  rehearsal disables form mapping and its fake adapter does not use site
  knowledge, so only the embedding cache has produced rows.
- **`cost_per_application` has never had a submitted application to divide by.**
  Zero application rows exist.
- **No strategy has ever been proposed against a real page.** `_propose_strategy`
  is tested against the fake page, which matches selectors literally against a
  set. Whether Playwright's `role=` selector finds a real Greenhouse Apply
  button is a question only the HAR capture can answer.
- **No knowledge file has ever been rolled back in anger.** The versioning is
  tested; it has never recovered a real bad ingest.
- **The weekly digest has never been sent.** No Telegram credentials.
- **`same_question` clustering has never seen a real screening question.** Its
  disqualifiers are the resolver's, which have been exercised against realistic
  phrasings in `tests/test_answers.py`, but the clustering itself has only ever
  run on fixtures.

## Needs you

1. **`scoring_stage1_top_n` does nothing.** It is read in exactly one place
   (`stage1.py:235`), assigned to a local, and never used again — while being
   exposed as a user-editable setting in the API and the dashboard. Setting it
   to 10 to cut spend changes nothing. `scoring_stage2_max` is the live knob
   (PR #16 replaced the prefilter with it and left this behind). **Delete the
   setting, or wire it?** Deleting a control from your dashboard is your call,
   so I left it. The code change either way is three lines.

2. **The combined PDF's fabrication self-check is a third model call over text
   that was just checked twice** — ~$0.001 per job, about a third of the
   document self-check spend. `verify_pdf(profile=None)` is a documented,
   supported mode, so the fix looks like one expression. **It is not safe as
   written**, and this is the trap: if the resume fails the model check but the
   combined PDF only runs the deterministic ones, the resume is ungated while
   the combined PDF passes — and a one-slot form attaches the combined PDF. The
   correct fix propagates the resume and letter verdicts into the combined
   report. It touches hard rule 3, so I did not do it unattended.

3. **The HAR capture, still.** Every strategy value in every knowledge file is
   an unverified guess, including the new shared vocabulary. Phase 4 makes the
   layer learn *better* once real values exist; it cannot supply them.

4. **Everything the previous sessions listed** — API keys, the answer bank,
   `ALLOW_LIVE_SUBMIT`, `work_rights` — is unchanged.

## Explicitly not done

- Pacing was not touched, in any sense: not shortened, not reconfigured, not
  optimised. It is measured, and the measurement is structurally prevented from
  entering a work total.
- `ALLOW_LIVE_SUBMIT` and `OUTBOUND_ENABLED` were not touched.
- No recurring check-in, timer or poll was scheduled. The only new scheduled job
  is the Sunday weekly digest, which is a report, not a poll.
- The nine "not worth fixing" performance findings were left alone rather than
  optimised on speculation. The measurements are in the table above so the
  question does not have to be reopened from scratch.

# Session — 2026-09-06 (macOS, unattended, four phases)

Gemini configuration, exact dropdown options in escalation, a full verification
sweep, and this record. `ALLOW_LIVE_SUBMIT` and `OUTBOUND_ENABLED` were not
touched and are still false. Nothing was submitted and no email was sent.

**Main is at `a1cc30e`: 1106 tests.** Three stacked PRs sit on top of it —
[#26](https://github.com/4mohdisa/JobSeekr/pull/26),
[#27](https://github.com/4mohdisa/JobSeekr/pull/27),
[#28](https://github.com/4mohdisa/JobSeekr/pull/28) — ending at **1187 tests**.

## The PR stack

| PR | Branch | Base |
|---|---|---|
| [#26](https://github.com/4mohdisa/JobSeekr/pull/26) | `fix/gemini-config` | `main` |
| [#27](https://github.com/4mohdisa/JobSeekr/pull/27) | `feat/choice-questions` | `fix/gemini-config` |
| [#28](https://github.com/4mohdisa/JobSeekr/pull/28) | `chore/full-verification` | `feat/choice-questions` |

Stacked because #27 changes what #26 changed (the same call sites carry both a
pinned temperature and an option set) and #28 adds tests for code #27
introduced. **Merge #26 → #27 → #28, retargeting each next PR to its new base
BEFORE deleting the merged branch.** Deleting a base auto-closes the PRs
targeting it and GitHub will not reopen them.

## Phase 1 — Gemini configuration (#26)

### Temperature is fixed in the one door, not at nine call sites

LiteLLM warns that a temperature below 1.0 on a Gemini 3 model causes infinite
loops and degraded reasoning, and that temperature, top_p and top_k are
deprecated from Gemini 3 on. Nine call sites pass a temperature — scoring,
writing, classify, formmap, derivation, the variant judge, the fabrication
self-check, the role-bullet rewrite and smoke — and **all nine route through
`LLMGateway._completion`**, so that is where the rule lives. Nine copies of a
provider rule is nine places for it to go stale.

Matched on the major version (`^gemini/gemini-(\d+)` ≥ 3), not a list of model
ids: `gemini-3.1-flash-lite` and a future `gemini-4-*` are both covered and
`gemini-2.5-flash-lite` is correctly left alone. Anthropic is untouched.

### The pin throws the caller's intent away, so the intent moved channel

Every explicit temperature in this codebase is *below* the 0.2 default and
means "be deterministic". Pinning to 1.0 discards that silently. So when the
pin overrides a caller, a determinism sentence is appended to the system
message — appended, never replacing, or every rule the call site set would go
with it. A caller already at 1.0 gets nothing added, because nothing was taken.

The one place temperature meant something else was the document **variant
ladder** (`0.4 + 0.2 * index`), which existed so three drafts were not three
phrasings of one sentence. Under the pin that ladder collapses into three
samples at one setting — silently, because a variant cannot report that it had
nothing to differ from. It is asked for in the prompt now ("this is draft 2 of
3, open on a different part of the fit"), which survives the pin and works on
providers that never had a ladder. The old expression is gone from the source,
not merely unused: left in place it reads as the live mechanism to the next
person, who would then tune a number Gemini discards.

### Embeddings moved to Gemini, and it costs 7.5x per token

`openai/text-embedding-3-small` → `gemini/gemini-embedding-001`, so the whole
pipeline runs on one key. The reason is the failure mode, not the tidiness:
**stage 1 does not fail loudly without its key — it silently stops ranking.**
Every job scores, none is prefiltered, and nothing says so.

The honest cost, since it is a real regression on one axis:

| | OpenAI small | Gemini embedding-001 |
|---|---|---|
| per 1M input tokens | $0.02 | $0.15 |
| stage 1, 200 jobs | $0.0015 | $0.0114 |
| **total, 200 jobs** | **$0.1200** | **$0.1299** |
| headroom under the $0.15 target | 20% | 13% |

Verified live: 3072 dimensions returned, $0.000001 for one vector. The
embedding cache is keyed on `(model, text)`, so the switch re-embeds rather
than mixing 1536- and 3072-dimension vectors — no migration needed.

`gemini-embedding-001` caps input at 2048 tokens; `scoring_embedding_char_budget`
is 1400 characters (~350 tokens), so there is no truncation risk. Batch size 96
is under Gemini's 100-per-batch limit.

### smoke.tex

`render_pdf` keeps the `.tex` beside the PDF on purpose — for a real build that
is how you see what was typeset. The smoke check has no reader for it, so it
now clears its own source, and `aux_files_left` means "render_pdf failed to
clean up" instead of listing the source on every run. It reports `[]` now.

### A test that could not fail

`test_one_missing_key_still_blocks` made a model keyless by matching `"gemini"`
in its id, on the premise that scoring and embeddings were different providers.
The moment both moved to Gemini it made **none** of them keyless — and passed.
Parametrised over *which model* is keyless instead. Fifth session running that
this exact shape has appeared: a different condition satisfying the assertion
instead of the one under test.

The `doctor` remedy it guards was wrong too ("set GEMINI_API_KEY and
OPENAI_API_KEY"), and now derives the setting name from the gateway's own
provider map.

**16 mutations, 16 killed.** Two survived first time, both the tests' fault:
- *"embeddings go back to a second provider"* survived because the test asserted
  on `settings.llm_model_embedding`, which reads this machine's `.env` — so it
  was testing the developer's machine, not the shipped default. Now asserts on
  `Settings.model_fields[...].default`.
- *"stage 1 is priced at zero"* survived because the cost assertion was an upper
  bound only, and zero satisfies it — which is exactly what a mispriced model
  projects. Both bounds now.

## Phase 2 — exact dropdown options in escalation (#27)

### What was actually broken

A closed-list field accepts exactly the strings it lists. Escalation sent the
question as free text, so a reply of "two weeks" to a form offering "2 weeks"
was stored, replayed, and then either failed at submit or submitted blank —
with nothing anywhere saying so.

### CAPTURE

`formdom` reads every option's **label** and its **submitted value**, off the
same element. They differ constantly; an option with no `value` attribute falls
back to its own text, which is what the browser does.

The bigger find was underneath: **radios and checkboxes were enumerated one
field per element.** A three-way radio group arrived as three fields, each
labelled with one of the *answers*, and the question itself was nowhere. That is
how a closed list reached the answer bank as several unanswerable free-text
questions. They are grouped by `name` now — by `name` specifically, not by
whichever identifier attribute the adapter preferred, because a group's members
share `name` and have different `id`s. A lone checkbox stays a consent tick, not
a one-option list.

Datalists are read (as the site's expected values, not as a closed list —
a datalist still accepts free text). Single vs multi is recorded. An
"Other (please specify)" option is marked as needing typed detail the answer
bank cannot supply.

Read through **locators, not one `evaluate()`**. The first version used a single
JS expression per control so label and value came off the same element — and it
broke the offline snapshot harness that replays captured HAR markup, which runs
no JavaScript. That harness is the only way any of this is testable against real
markup, and it caught the regression immediately. Per-option locators read both
attributes off the same locator anyway.

### CARRY, across a process boundary

The abstention carries the option set **whatever the reason it abstained** —
including `NO_MATCH`, which is the most common way a choice question is first
seen and the branch that carried nothing at all. `resolve_answer` attaches them
once, in a wrapper, rather than at six `Abstain(...)` construction sites.

The parked job persists them (`job.needs_answer_choices`, `needs_answer_multi`,
migration `69618fa3f706`). The pass that asks and the process that receives the
reply are different ones; an in-memory list does not survive that gap, and
without the options the bot cannot tell a valid reply from an invalid one.
`run.py` deliberately no longer passes them alongside — `escalate_question`
reads them back off the job, so the message and the reply validation look at the
same list rather than two copies that can disagree.

### ASK

Inline keyboard, one button per option. Above 8 options, a numbered list that
accepts the number. Multi-select accumulates taps and finishes on Done, with the
selection travelling **in the callback data** (`c:<job>:<index>:<selection>`)
rather than in a new table — the bot restarts, and a half-made selection that
survives a restart is not worth a schema change.

The message is sent **without Markdown**. This is not cosmetic: an option
reading `3+ years_of_experience` renders as `3+ yearsofexperience` under
Markdown, and the user would then reply with a string that matches no option.

### STORE

The answer bank's existing `choices` column — NULL on every seeded row since it
was added — holds the full option set as `{label, value, is_free_text}`. The
**value** goes in `answer_value`, so the value, the label and the option set are
all on the row and cannot drift apart. Multi-select values join with `\x1f`
(ASCII unit separator): a control character cannot appear in rendered option
text, so it can never collide with a real value the way a comma or a pipe would.

### REPLAY, and how often it will re-ask

A stored answer that is not one of *this* form's options abstains and asks
again. **The substring tier is gone.** It mapped an answer contained in exactly
one option — so a stored "2 weeks" would have won against "1 - 2 weeks", and a
one-to-two-week range is not two weeks. Yes/no synonyms still map, because
exactly one option meaning yes is a normalisation, not a nearest-match guess.

**How often this re-asks, as asked for.** There is no live data to measure
against — 21 answer-bank rows, all blank, none with a recorded option set, and
zero applications ever submitted. So this is reasoning, not measurement:

- **Yes/no questions — near zero extra re-asks.** They are the bulk of
  Australian screening questions, the option labels are near-universally
  "Yes"/"No", and the yes/no tier covers case and synonym differences.
- **Bounded ranges (notice period, years of experience, salary bands) — expect
  a re-ask on most new employers.** These are exactly where wording varies
  ("1-2 weeks" / "1 - 2 weeks" / "Two weeks or less" / "2 weeks"), and they are
  also where a wrong answer is a false statement rather than a formatting slip.
  Rough guess: **one re-ask per distinct wording**, so a handful over the first
  weeks and then a long tail as new ATS vendors appear.
- **Enumerations (state, citizenship status, how did you hear about us) — one
  re-ask per employer family.** PageUp, Workday and Greenhouse each phrase these
  differently.

The cost of a re-ask is one Telegram tap. The cost of the alternative is a
false statement about notice period or work rights on a real application, which
is unrecoverable. That trade is why the substring tier is gone rather than
tuned. If the re-ask rate turns out to be annoying in practice, the honest
lever is a **per-question alias list on the bank row** — "these option sets are
the same question" confirmed once by the user — not a similarity threshold.

### DERIVE

Already partly built: `facts.derive` took `choices` and abstained on an
out-of-set answer. Two gaps closed.

1. The model now sees the exact option strings one per line and is told it may
   not invent a value outside the list, and the returned answer is resolved to
   the option's **submitted value** rather than the label it echoed back.
2. **A CONFIRMED derivation was not rechecked.** `DerivedAnswer` is cached by
   question key, and different employers offer different lists, so a confirmed
   "Yes" was being replayed verbatim onto a form offering
   [Full, Provisional, None]. It abstains now — the same rule as the answer
   bank's, in the one place it was missing.

### Tests that could not fail

**36 mutations, 36 killed.** Three reported NOT APPLIED first time (a target
string I had written from memory rather than from the file, an em-dash, and a
prompt line that had been reflowed). Each was re-targeted and re-run rather than
assumed caught — a mutation that does not apply is indistinguishable from a
test that killed it, which is the trap this project keeps hitting from the other
direction.

**`enumerate_form_fields` had no direct test of any kind** before this. It is
the function that decides what the answer bank is asked about. It has 12 now,
against a small fake DOM.

## Phase 3 — full verification sweep (#28)

| Check | Result |
|---|---|
| `uv run pytest` | **1187 passed**, 0 failed, **0 skipped** |
| `uv run python -m backend.rehearsal` | **14/14 stages pass** (the doc says 12; telemetry and the question ledger added two last session) |
| `uv run ruff check backend tests` | clean |
| `uv run ruff format --check` | 128 files already formatted |
| `uv run alembic check` | no new upgrade operations |
| `alembic upgrade head` on a fresh database | 14 revisions applied, then `check` clean |
| `npx tsc --noEmit` | clean |
| `npm run build` | built, 322 kB / 95.8 kB gzipped |
| `npx oxlint src` | exit 0; 8 warnings, all pre-existing (`set-state-in-effect` ×6, `purity` ×1) and none in a file this session touched |
| `uv run python -m backend.smoke` | **5 passed, 0 failed, 1 skipped** |
| `uv run python -m backend.doctor` | **2 blocking, 3 warnings** — see below |

### Skips, and why

**Zero skipped tests.** There is exactly one conditional skip marker in the
suite — `needs_pdflatex` in `conftest.py`, on 16 tests — and pdflatex resolves
here, so all 16 ran. That marker resolves `settings.pdflatex_path` rather than
`shutil.which("pdflatex")`, which is the fix for the Windows session where the
entire document suite skipped silently and the suite reported green.

The one smoke skip is `session cookies`: **the browser profile holds no
cookies** — nothing has ever signed in. Stated reason, correct behaviour.

### What `doctor` says still blocks you

```
[BLOCK] API keys        no key for resume and cover letter prose
                        (anthropic/claude-opus-5), reading unknown application
                        forms (anthropic/claude-opus-5)
[BLOCK] Facts           all 11 are blank — every screening question will park
[WARN ] Campaign        'Adelaide starter' is active on the seeded placeholder terms
[WARN ] Sessions        never checked — run an apply pass or wait for the 09:00 check
[WARN ] Site knowledge  2 platform(s) still on shipped defaults
```

Two things improved since the last handoff without being mentioned anywhere:
**a Profile now exists** (v2, Mohammed Isa) and **Chrome and the Playwright
browsers are installed** — the smoke browser check launches real headful Chrome
and loads a page. Both were listed as blockers in `HANDOFF.md` §5.

Database as it stands: 235 jobs, 2 profile versions, 1 campaign, 21 answer-bank
rows (**all blank**), 11 facts (**all blank**), 0 applications, 0 stage timings,
0 question events, 10 LLM spend rows.

### Safety-critical paths with no coverage — three found, three fixed

The instruction was to assume there are more after `eligible_jobs`. There were.

1. **`formdom.fill` had no test of any kind.** It is the last few inches — the
   one place a resolved answer becomes something an employer receives. A wrong
   string there is invisible everywhere upstream: the answer bank is right, the
   escalation was right, and the form still gets the label where it wanted the
   value. Six tests now, covering select-by-value, the label fallback,
   multi-select, radio groups, checkbox groups and plain text.
2. **`guardrails` campaign target goal had no test.** A campaign configured to
   stop at N kept submitting past N, and the only sign would have been the
   dashboard count going up. Two tests, including the off-by-one.
3. **`guardrails` auth-check exception path had no test.** A predicate that
   throws — expired context, closed browser — must read as "not signed in".
   Untested, and the failure direction is the whole point: treating an exception
   as anything else submits into a dead session, which on LinkedIn means a
   silently discarded application.

### One thing fixed rather than reported: `doctor` said "API keys OK"

`check_llm_keys` inspected `llm_model_scoring` and `llm_model_embedding` only.
Both are on the one Gemini key now, so both passed — while `llm_model_writing`
and `llm_model_formmap` point at `anthropic/claude-opus-5` with no
`ANTHROPIC_API_KEY` set. The setup report said **`[OK] API keys`** for a machine
that cannot build a single document.

A setup check that misses the thing blocking you is worse than no setup check,
so this is a fix rather than a finding. All four configured models are checked
now, each named with what it does, and the report has gone from
"1 blocking: Facts" to "2 blocking: API keys, Facts" — which is the truth.

**15 further mutations on those four, 15 killed** (after replacing one
equivalent mutant — `values[0] if values else value` on a text field is
behaviourally identical to `value` for every input a text field can carry, so
its survival was not evidence of a missing test; recorded here rather than
papered over with a contrived assertion).

Coverage of the two modules: `formdom` 78% → 88%, `guardrails` 92% → 94%.
Whole-backend line coverage is **81%**. The genuinely thin modules are all
browser- or credential-bound and cannot be covered without a live session:
`apply/har.py` 26%, `integrations/gmail.py` 28%, `integrations/scheduler.py`
32%, `ats/adapters.py` 35%, `apply/canary.py` 36%, `apply/seek.py` 40%,
`apply/session.py` 42%. `discovery/verify_seek.py` and `siteknowledge/__main__.py`
are 0% and are both CLI entry points.

### Frontend dead surface

**No unreachable pages.** All 13 files in `pages/` have a route in `App.tsx`
and a matching nav entry; the two lists are exact mirrors.

**No unrendered components.** All 18 exported components across the five files
in `components/` are rendered somewhere.

**Nine API client methods are defined and never called**, and two backend
routes have no client method at all. Verified by grepping each name across
`frontend/src` — `api` is imported as a namespace object everywhere and never
destructured, so the grep is exhaustive.

| `api.ts` method | backend route |
|---|---|
| `profileVersions` | `GET /profile/versions` |
| `getCampaign` | `GET /campaigns/{id}` |
| `deleteCampaign` | `DELETE /campaigns/{id}` |
| `createAnswer` | `POST /answers` |
| `deleteAnswer` | `DELETE /answers/{id}` |
| `deleteTemplate` | `DELETE /templates/{id}` |
| `setJobStatus` | `POST /jobs/{id}/status` |
| `healthSummary` | `GET /settings/health-summary` |
| `jobDocuments` | `GET /documents/job/{id}` |

Backend routes with no client method: `GET /settings/spend` (redundant — the
same data is nested in `GET /settings`, which the Settings page does read) and
`GET /documents/{id}` (the JSON metadata one; only `/file` is used).

The pattern worth noticing: **the UI can create and edit campaigns, answers and
templates but cannot delete any of them**, and cannot change a job's status.
Those four look like affordances that were dropped, not endpoints nobody
needed. Reported rather than built — adding nine UI features unasked is not a
verification sweep.

## Decisions taken without asking

- **Enforced the Gemini temperature rule in `llm/client.py` rather than at the
  nine call sites named in the brief.** Every one of them routes through that
  function; nine copies of a provider rule is nine places for it to go stale.
  The call sites that needed changing are the ones where temperature carried
  meaning the pin destroys — which was exactly one, the variant ladder.
- **`gemini/gemini-embedding-001`, not `gemini-embedding-2`.** The `-2` model is
  $0.20 vs $0.15 per 1M and takes 8192 input tokens instead of 2048; neither
  matters at a 1400-character budget, and `-001` is the GA id.
- **Introduced a `Choice(label, value, is_free_text)` type** rather than a
  parallel `choice_values` dict alongside the existing `choices: list[str]`.
  Two lists for one concept is how they drift, and the value is the half that
  reaches the employer. `as_choices()` reads all three shapes that exist in the
  wild — `Choice` objects, stored dicts, and the bare strings every row written
  before this holds — so no data migration was needed for the answer bank.
- **Multi-select values join with `\x1f`.** A comma or a pipe can appear in an
  option label; a control character cannot survive rendering. Marked with a
  `ponytail:` comment naming the ceiling (store as JSON if a form ever submits
  control characters).
- **Eight options is the button/numbered-list threshold.** Telegram renders
  twenty buttons; a phone does not render them readably, and an option the user
  cannot read is one they cannot pick correctly.
- **`AnswerOut` normalises stored option sets in a validator** rather than
  migrating the column. A legacy row of bare strings still renders instead of
  500ing the answer bank page, and a bare string is not lossy — a form with no
  `value` attribute submits its own text, which is exactly what it expands to.
- **The answer bank page edits a choice row as a dropdown of its own options.**
  Out of scope strictly, but leaving a free-text box there is the same failure
  the whole phase is about, one layer up.
- **Reported the nine unused API client methods rather than wiring them.**

## What broke, and what found it

- **The offline snapshot harness caught the `evaluate()` regression instantly.**
  The first version of the option reader used one JS expression per control;
  `SnapshotPage` runs no JavaScript and refuses to fake a result, so the HAR
  replay test failed rather than silently returning no options. That refusal is
  why the bug was found in the same minute it was written.
- **A test asserting on `settings.*` was testing this machine's `.env`.** It
  passed against a mutation that reverted the shipped default, because the local
  `.env` had already been updated. Same shape as the four previous sessions.
- **An upper-bound-only cost assertion was satisfied by zero** — the exact value
  an unpriced model projects.
- **Nine unused API client methods and four missing delete affordances**, found
  by an exhaustive grep rather than by any test. Nothing in the suite runs the
  frontend.
- **`doctor` reported "API keys OK" for a machine that cannot write a
  document.** Found by writing down a claim about the code in this file and then
  going back to check it was true before shipping it.
- **A mutation survived because two settings ship pointing at the same model
  id.** Dropping `llm_model_writing` from the doctor's check left the test green,
  because `llm_model_formmap` names the same `claude-opus-5` and satisfied the
  assertion instead. The test now pins four distinct ids first. Sixth instance
  of this shape.

## Still unverified

Everything the previous sessions listed, unchanged, plus:

- **No real dropdown has ever been enumerated.** Every option set in this
  session's tests is a fixture or the captured Seek markup. Whether a live
  Workday radio group exposes its legend where `_group_question` looks for it is
  a question only a browser can answer.
- **No inline keyboard has ever been rendered by Telegram.** `escalate_question`
  builds the payload and the tests assert on it; `send_message` has never been
  called with a `reply_markup` against the real API. The smoke test sends a
  plain message, which does not exercise it.
- **`handle_callback` has never been reached from a real button tap.**
  `build_application` now registers a `CallbackQueryHandler`, and the bot has
  never been run.
- **The `\x1f` multi-select round trip has never touched a real form.**
- **The determinism instruction has never been measured.** Whether saying "be
  deterministic" to a Gemini 3 model at temperature 1.0 actually produces
  stable output is an empirical question and this only makes the ask.
- **The variant diversity prompt has never produced real variants.**
  `llm_model_writing` is `anthropic/claude-opus-5` and there is no Anthropic key,
  so every document variant in the rehearsal is a deterministic stub.

## Needs you

1. **Your facts.** All 11 fact rows are blank,
   so every screening question parks — and now that escalation asks properly,
   the loop will work the moment there is anything to derive from. The Facts
   page, then `uv run python -m backend.facts preview`.

2. **`LLM_MODEL_WRITING` and `LLM_MODEL_FORMMAP` point at Anthropic and there is
   no Anthropic key.** Scoring, classification, derivation and embeddings all
   run on the one Gemini key now; document writing and form mapping do not, and
   would have failed on the first real attempt. **`doctor` reports this now** —
   it did not before, and that is the one thing in this list I fixed rather than
   reported (below). Either add `ANTHROPIC_API_KEY` or move those two settings
   to Gemini; the second is a two-line `.env` edit, and the cost note in
   `config.py` explains why writing is on the strong model.

3. **`scoring_stage1_top_n` still does nothing.** Read once at `stage1.py:235`,
   assigned to a local, never used again — while exposed as an editable setting
   in the API and the dashboard. Unchanged from the last session because it is
   your call whether a control disappears from your own dashboard. Either:
   *delete* — drop it from `config.py`, `schemas.py` (two places) and
   `core.py:519`; or *wire* — `return ranked[:top_n] if top_n else ranked` at
   `stage1.py:249`. Three lines each way, one word from you.

4. **The combined PDF's third fabrication self-check**, unchanged from last
   session. Still ~$0.001 per job, still touches hard rule 3, still not safe as
   the one-expression fix it looks like.

5. **The HAR capture, still.** It now unblocks more than it did: every option
   value, every group legend, and every derived selector in every knowledge file
   is an unverified guess until a real page has been recorded.

6. **Four things the dashboard cannot do**: delete a campaign, delete an answer,
   delete a template, change a job's status. The endpoints exist and the client
   methods exist; nothing calls them. Say the word and they are small.

## Explicitly not done

- `ALLOW_LIVE_SUBMIT` and `OUTBOUND_ENABLED` were not touched.
- No recurring check-in, timer or poll was scheduled.
- Pacing was not touched.
- The nine unused API client methods were reported, not wired.
- `scoring_stage1_top_n` was left exactly as it was, for the second session
  running, because deleting a control from the dashboard is not mine to decide.
