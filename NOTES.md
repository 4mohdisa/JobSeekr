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

## What needs you

1. **Run the whole thing on Windows.** Nothing here proves it works there.
2. **Phase 2 in full** — the `verify_seek` command above. Highest value
   outstanding: discovery is the top of the funnel and is entirely unproven
   against a live site.
3. **Add `GEMINI_API_KEY`** to `.env` if you want the new routing used.
4. **Decide on 2.5 vs 3.1 Flash-Lite** before 2026-10-16.
5. **Dismiss GitGuardian incident 36579249** (dummy placeholder, already
   emptied at the branch tip).
