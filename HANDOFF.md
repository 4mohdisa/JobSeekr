# Handoff

Written 2026-09-01 at the end of the first Windows bring-up, for picking the
project up on a MacBook. `NOTES.md` is the full factual record; this is the
short version plus everything macOS-specific.

**State of main: 575 tests pass, the offline rehearsal passes end to end.**
Everything below was verified on Windows 11 / Python 3.12.14 / MiKTeX 25.12
unless it says otherwise.

---

## 1. Where the project actually is

### Works, verified by running it

| Area | Evidence |
|---|---|
| Test suite | 575 passed in ~70s on main |
| End-to-end pipeline | `python -m backend.rehearsal` — all 12 stages pass |
| Seek discovery | Live. 150 ads in one run against the real API |
| LinkedIn + Indeed | Live via jobspy. 28 and 59 ads in one run |
| Dedupe | Real cross-board duplicates rejected (43 in one run) |
| Document build | Real pdflatex, 6 PDFs, all through the parse gate |
| Apply flow | Full sequence to the point of submit, then blocked and audited |
| Guardrails | Correctly blocked; only environmental checks fail |
| Form-map cache | Second application to a known form shape costs 0 LLM calls |

211 real Adelaide jobs are in the local SQLite database **on the Windows
machine**. `data/` is gitignored, so none of that comes with the repo — re-run
discovery on the Mac to repopulate.

### Not verified anywhere

- **Nothing has ever been submitted.** `ALLOW_LIVE_SUBMIT` has never been true.
- **No browser has ever been driven.** Chrome and the Playwright browsers are
  not installed. Every apply-path test uses a fake page and adapter.
- **HAR record and replay** — never run. Needs a browser and a human at the
  keyboard.
- **No real scoring or document content.** There is no API key and no profile,
  so every LLM call in the rehearsal is a deterministic stub. The pipeline is
  proven; the *output quality* is not.
- **Telegram, Gmail inbound, outbound email** — no credentials, never run.
- **The POSIX branch of `render_pdf`'s process-tree kill has never executed.**
  Only the Windows `taskkill` branch has. See §4.

---

## 2. The 13 unreachable functions

Public code that nothing in `backend/` calls. `tests/test_reachability.py`
pins this list and fails if a new one appears, so it cannot grow silently.
Three others in this class — the whole form-map cache, `map_fields`, and
`has_restriction_notice` — were found and wired during this bring-up.

Ordered by what I would do first.

### Wire these — they are missing halves of features that exist

**`escalate_question`** — `integrations/telegram.py:84`
`Claude.md`'s answer-bank loop is "abstain, park the job, ask via Telegram,
save the answer, retry". The asking half is never called, so an abstention
parks the job and stops there forever. This is the single biggest functional
gap: without it the answer bank never self-populates and every novel screening
question is a dead end. **Recommend: wire it into the abstention path in
`apply/flow.py`.** Needs a Telegram token first (§5).

**`detect`** — `ats/detect.py:237`
Full detection: URL first, then HTML. Adapters only call `detect_from_url`, so
a white-labelled ATS on an employer's own domain is never recognised — and
that is the common Australian case (PageUp on `careers.acme.com.au`).
`detect_from_html` exists precisely for it and there is already a passing test
for the behaviour. It needs a loaded page, so it belongs in the adapter's
`open()`, after navigation. **Recommend: wire it.**

**`decide_queueing`** — `ats/queueing.py:51`
The manual-queue decision. Nothing calls it, so a job that should be handed to
you to finish by hand never is — it just fails or parks. **Recommend: wire it
into `apply/run.py`'s gray-zone handling.**

### Decide, then wire — these act on your behalf

**`send_draft`, `draft_for_job`, `preview`** — `integrations/outbound.py`
The outbound follow-up email path. `send_draft` sends mail as you and already
refuses without an explicit approval token. Wiring it is a policy decision, not
a default — **it is your call whether this system ever sends email.**

**`replay`** — `apply/har.py:137`
Replays a recorded HAR so an application flow can be tested offline forever.
Blocked on having recordings at all (§5). This is the one seam the rehearsal
cannot cover. **Recommend: wire it as soon as you have a capture.**

**`ensure_logged_in`** — `apply/session.py:139`
Nothing verifies the browser session before a run. The guardrail does check
authentication at submit time, so this is a nicer-failure-mode improvement
rather than a hole. **Recommend: call it at the start of an apply pass** so a
dead session fails immediately instead of after building documents.

### Probably delete

**`apply`** — `backend/base.py:167`
A method on the `Applier` protocol. The apply flow defines and uses its own
`Adapter` protocol instead, so `Applier` is never satisfied or called by
anything. Two protocols describing the same role is how they drift.
**Recommend: delete `Applier`, or make it an alias of `flow.Adapter`.**

**`render_template_file`** — `documents/engine.py:231`
The build renders through `render_string` with an explicitly loaded template.
Nothing loads a template by filename. **Recommend: delete.**

**`board` / `board_keys`** — `boards.py:249,253`
Registry lookup helpers nothing looks up. Harmless, three lines each.
**Recommend: delete, or leave — low stakes either way.**

### Worth a second look

**`rubric_hash`** — `scoring/rubric.py:115`
Nothing stamps a rubric hash onto a `Score`. `Score` already carries
`rubric_version`, but a version integer only changes when someone remembers to
bump it — a hash changes whenever the rubric text does. Right now an edited
rubric produces scores indistinguishable from ones computed before the edit.
**Recommend: store it on `Score` and compare on read.** Small change, prevents
a silently stale shortlist.

---

## 3. Running things

All commands from the repo root. `uv` reads `.python-version` and will fetch
CPython 3.12 itself.

```bash
uv run pytest                                    # 575 tests, ~70s
uv run python -m backend.rehearsal               # whole pipeline, offline, ~11s
uv run python -m backend.discovery.run           # real network
uv run python -m backend.scoring.run             # needs an API key
uv run python -m backend.documents.build --job-id N
uv run python -m backend.apply.run --dry-run
uv run uvicorn backend.main:app --reload         # API on :8000
cd frontend && npm run dev                       # UI on :5173
```

**The rehearsal is the fastest way to know the machine is set up correctly.**
It needs no network, no API key and no browser — only Python and a working
pdflatex. If it passes, the pipeline is wired end to end on that machine.

Discovery notes:

- The default window is 8 hours, which is right for the 4-hourly schedule and
  useless on an empty database. `run_discovery` now detects a near-empty jobs
  table and widens to 720h automatically, logging `discovery_backfilling`.
  You should not have to think about this, but that is why the first run pulls
  far more than later ones.
- `--hours-old N` always overrides.
- LinkedIn and Indeed volumes vary a lot between identical calls. Not an error.

---

## 4. Fresh machine setup, and what differs on macOS

### Common

```bash
git clone https://github.com/4mohdisa/JobSeekr.git
cd JobSeekr
uv sync --all-groups
cp .env.example .env          # then edit — see below
uv run alembic upgrade head
cd frontend && npm install && cd ..
```

**Python is pinned to 3.12.x** (`requires-python = ">=3.12,<3.13"`). This is
not cosmetic: `python-jobspy` 1.1.82 hard-pins `numpy==1.26.3`, whose last
supported interpreter is 3.12. On 3.14 numpy dies at import and LinkedIn and
Indeed discovery both go down while Seek keeps working. Do not relax the bound
without checking jobspy first.

### LaTeX — the main macOS difference

Windows used MiKTeX. On macOS use BasicTeX (small) or MacTeX (large):

```bash
brew install --cask basictex
```

BasicTeX ships a minimal package set, so install what the templates need:

```bash
sudo tlmgr update --self
sudo tlmgr install enumitem titlesec geometry hyperref oberdiek url \
                   cm-super ec psnfss iftex kvoptions letltxmacro refcount etoolbox
```

Then in `.env`:

```
PDFLATEX_PATH=/Library/TeX/texbin/pdflatex
```

The current `.env.example` and the Windows `.env` both carry a Windows path —
**this is the setting most likely to be wrong after the move.**

Two MiKTeX-specific things that do *not* apply on macOS:

- MiKTeX needed `initexmf --set-config-value "[MPM]AutoInstall=1"`, or
  pdflatex stops and waits for a GUI confirmation the first time it needs a
  package — an unattended run hanging forever. TeX Live does not prompt; it
  just fails if a package is missing, which is why the `tlmgr install` list
  above matters.
- `pdflatex` is on `PATH` by default with BasicTeX, so the parse-gate and
  document tests will not silently skip the way they did on Windows. (They
  resolve `settings.pdflatex_path` now, so they would not skip anyway.)

### `render_pdf` process-tree kill — untested on POSIX

`backend/documents/build.py` kills the whole process tree when pdflatex times
out. Two branches:

- Windows: `taskkill /T /F /PID` — **exercised, this is the one that ran here.**
- POSIX: `start_new_session=True` plus `os.killpg(..., SIGKILL)` — **never
  executed anywhere.**

`tests/test_windows_portability.py::test_pdflatex_timeout_holds_when_a_child_outlives_the_process`
does cover it and will exercise the POSIX branch the first time you run the
suite on the Mac. If that test passes there, the branch works. Worth watching
for on the first run — it is the fix for a hang that cost 17 hours.

### Timezone data

`tzdata` is installed only on Windows (`; sys_platform == 'win32'` in
`pyproject.toml`) because Windows ships no tz database. macOS has one, so
`zoneinfo` uses the system copy and `Australia/Adelaide` resolves without the
package. Nothing to do — but if you ever see a `ZoneInfoNotFoundError` on the
Mac, that marker is why.

### Line endings

There is no `.gitattributes`, and the Windows machine has `core.autocrlf=true`.
Files in the repo are LF; the Windows working copy is CRLF. This has caused no
problems so far, but a shared repo across both platforms without a
`.gitattributes` is how spurious whole-file diffs start. **Consider adding one
early** — `* text=auto eol=lf` is the usual answer.

### Not installed anywhere yet

Chrome and the Playwright browsers. Needed only for the apply layer; discovery
is plain HTTP and the rehearsal uses a fake page.

```bash
brew install --cask google-chrome
uv run playwright install chrome
```

`Claude.md` deliberately specifies headful Chrome (`channel="chrome"`) —
headless is a detection signal. Do not switch it.

---

## 5. Blocked on you

Nothing here can be worked around; each needs something only you can supply.

**1. Your LaTeX resume.** I searched both drives and the whole git history and
there is no `.tex` resume anywhere on the Windows machine — only the project's
own template. The `Profile` table is **empty**, and hard rule 1 forbids
inventing anything about you, so the profile import could not be built against
a guessed format. Paste the `.tex` (or point at it) and the importer is a short
job: the JSON columns can carry `source: "imported"` per field without a
migration, so you will be able to see exactly what was extracted verbatim and
what needs checking.

**Until there is a profile row, real document generation cannot run at all** —
the rehearsal only works because it seeds a fixture profile.

**2. API keys.** `.env` has none. Without them scoring and document writing
cannot run for real. `Claude.md` routes scoring and classification to
`gemini-3.1-flash-lite` to hit the ~$0.15/200-job target — note the
`.env.example` used to ship `claude-opus-5` for those and silently reinstated a
~17x configuration; that is fixed, but check your `.env` matches.

**3. The answer bank is empty.** Zero rows. Every screening question will
abstain and park the job. It is designed to self-populate via Telegram — but
`escalate_question` is one of the unreachable functions above, so right now
nothing asks. Either seed a few answers by hand or wire the Telegram loop.

**4. HAR capture.** Needs you at the keyboard in a logged-in browser. This is
the only way to test a real application flow offline, and it unblocks `replay`.
Explicitly deferred from this session.

**5. `ALLOW_LIVE_SUBMIT`.** Still `false` in both `.env` and `.env.example`,
never touched. Only you may set it, by hand. Do not turn it on until at least
one full dry run against a real form has been inspected.

---

## 6. Repo state

- `main` is at the merge of `fix/windows-bringup`, pushed, 575 tests green.
- `fix/windows-bringup` and `claude/job-agent-core-setup-07xswl` are deleted
  locally and on the remote. Both were fully merged first.
- **There is no CI.** No `.github/workflows` exists, so nothing runs on push —
  the test suite is only ever run by hand. Worth adding; the suite is fast
  (~70s) and needs no secrets if the LaTeX packages are installed in the job.
- This merge was done locally rather than through a pull request: `gh` is not
  installed on the Windows machine and there was no CI to wait for.
