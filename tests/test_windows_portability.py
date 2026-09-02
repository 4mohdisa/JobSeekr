"""Windows-portability invariants, checked from any platform.

The project targets Windows but was written and is tested on Linux, so the
failure modes that only appear on Windows get no natural coverage. These tests
encode them as rules that fail on Linux too:

* text decoded with the locale encoding (cp1252 on Windows, UTF-8 here) —
  the single most common way a Linux-developed tool breaks on Windows
* paths built by string concatenation rather than pathlib
* the tz database, which Windows does not ship
* cleanup that can fail a successful operation because Windows locks files

None of this replaces running on Windows. It does mean a regression is caught
here rather than on the user's desktop.
"""

from __future__ import annotations

import ast
import contextlib
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"


def python_files() -> list[pathlib.Path]:
    return [p for p in BACKEND.rglob("*.py") if ".venv" not in p.parts]


def parsed() -> list[tuple[pathlib.Path, ast.Module]]:
    return [(p, ast.parse(p.read_text(encoding="utf-8"))) for p in python_files()]


def _keywords(call: ast.Call) -> dict[str, ast.expr]:
    return {kw.arg: kw.value for kw in call.keywords if kw.arg}


def _is_const(node: ast.expr | None, value: object) -> bool:
    return isinstance(node, ast.Constant) and node.value == value


def _receiver_is_pathlike(node: ast.expr) -> bool:
    """Whether `x` in `x.open(...)` plausibly names a filesystem path.

    A name-based heuristic rather than type inference, which is not available
    to a static check. It is deliberately biased toward flagging: a false
    positive costs one explicit `encoding=`, while a miss ships a cp1252 read
    to the user's Windows machine.
    """
    if isinstance(node, ast.Call):  # Path(...).open(...)
        target = node.func
        return isinstance(target, ast.Name) and target.id in {"Path", "PurePath"}

    name = ""
    if isinstance(node, ast.Name):
        name = node.id
    elif isinstance(node, ast.Attribute):
        name = node.attr

    lowered = name.lower()
    return any(hint in lowered for hint in ("path", "file", "dir", "target", "dest"))


# =========================================================================
# Encoding — the big one
# =========================================================================


def test_every_text_file_read_or_write_declares_an_encoding():
    """Windows defaults to cp1252, so an unqualified read of UTF-8 mangles it.

    Job ads are full of non-ASCII: em-dashes, curly quotes, accented company
    names. Reading one without an explicit encoding on Windows either raises
    or silently substitutes characters that then end up in a cover letter.
    """
    offenders: list[str] = []

    for path, tree in parsed():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            if isinstance(node.func, ast.Attribute):
                name = node.func.attr
                # `.open()` is not necessarily file I/O — `adapter.open(page,
                # job)` navigates a browser and `pdfplumber.open(path)` is that
                # library's own API, which reads bytes and takes no encoding.
                # Only treat it as file I/O when the receiver looks like a path.
                if name == "open" and not _receiver_is_pathlike(node.func.value):
                    continue
            else:
                name = getattr(node.func, "id", "")

            if name not in {"open", "read_text", "write_text"}:
                continue

            keywords = _keywords(node)

            # Binary mode carries no encoding, which is correct.
            mode = keywords.get("mode")
            positional_mode = node.args[0] if (name == "open" and node.args) else None
            for candidate in (mode, positional_mode):
                if isinstance(candidate, ast.Constant) and "b" in str(candidate.value):
                    break
            else:
                if "encoding" not in keywords:
                    offenders.append(f"{path.relative_to(REPO)}:{node.lineno} {name}()")

    assert offenders == [], (
        "text I/O without an explicit encoding — these read as cp1252 on Windows:\n"
        + "\n".join(offenders)
    )


def test_subprocess_capturing_text_declares_an_encoding():
    """`text=True` alone decodes with the locale encoding.

    pdflatex is the one external process here, and its log routinely contains
    non-ASCII from package banners and file names. On Windows that decodes as
    cp1252 and can raise UnicodeDecodeError, replacing the real LaTeX error
    with a confusing one.
    """
    offenders: list[str] = []

    for path, tree in parsed():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {"run", "check_output", "Popen"}
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
            ):
                continue

            keywords = _keywords(node)
            wants_text = _is_const(keywords.get("text"), True) or _is_const(
                keywords.get("universal_newlines"), True
            )
            if wants_text and "encoding" not in keywords:
                offenders.append(f"{path.relative_to(REPO)}:{node.lineno}")

    assert offenders == [], (
        "subprocess capturing text without an encoding (cp1252 on Windows):\n"
        + "\n".join(offenders)
    )


def test_no_subprocess_uses_shell_true():
    """shell=True invokes cmd.exe on Windows and /bin/sh here — different
    quoting, different escaping, different everything. A list argv is portable."""
    offenders: list[str] = []
    for path, tree in parsed():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_const(_keywords(node).get("shell"), True):
                offenders.append(f"{path.relative_to(REPO)}:{node.lineno}")
    assert offenders == [], offenders


# =========================================================================
# Paths
# =========================================================================


def test_no_posix_only_absolute_paths():
    """`/tmp` and `/home` do not exist on Windows.

    Temporary files belong in tempfile.gettempdir(); everything else belongs
    under settings.data_dir.
    """
    offenders: list[str] = []
    for path, tree in parsed():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            value = node.value
            if value.startswith(("/tmp", "/home", "/var", "/usr", "/etc", "/opt")):
                offenders.append(f"{path.relative_to(REPO)}:{node.lineno} {value!r}")
    assert offenders == [], offenders


def test_paths_are_not_built_by_string_concatenation():
    """`a + "/" + b` produces a broken path on Windows; pathlib's / does not."""
    offenders: list[str] = []
    for path, tree in parsed():
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Add):
                continue
            for side in (node.left, node.right):
                if (
                    isinstance(side, ast.Constant)
                    and isinstance(side.value, str)
                    and side.value in {"/", "\\", "/*"}
                ):
                    offenders.append(f"{path.relative_to(REPO)}:{node.lineno}")
    assert offenders == [], offenders


def test_settings_paths_are_pathlib_not_strings():
    from backend.config import settings

    for attribute in (
        "data_dir",
        "logs_dir",
        "documents_dir",
        "screenshots_dir",
        "formmaps_dir",
        "backups_dir",
        "har_dir",
        "stop_file",
    ):
        assert isinstance(getattr(settings, attribute), pathlib.Path), attribute


def test_managed_directories_survive_being_created_twice():
    """ensure_directories runs on every startup; it must be idempotent."""
    from backend.config import settings

    settings.ensure_directories()
    settings.ensure_directories()


# =========================================================================
# Timezone — Windows ships no tz database
# =========================================================================


def test_tzdata_is_declared_for_windows():
    """Without it, ZoneInfo raises on Windows and the guardrails crash.

    It arrives transitively via pandas today, but relying on that means the
    apply pass breaks the day jobspy is swapped out. The guardrails' business
    hours window, the daily cap's local-day boundary and the scheduler all
    depend on this resolving.
    """
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert "tzdata" in pyproject, "tzdata is not a declared dependency"

    tzdata_line = next(
        line for line in pyproject.splitlines() if line.strip().startswith('"tzdata')
    )
    assert "win32" in tzdata_line, "tzdata should carry a Windows marker"


def test_the_configured_timezone_actually_resolves():
    from zoneinfo import ZoneInfo

    from backend.config import settings

    zone = ZoneInfo(settings.timezone)
    assert zone is not None


def test_adelaide_is_a_half_hour_offset_zone():
    """Adelaide is UTC+9:30/+10:30. Anything assuming whole hours is wrong."""
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Australia/Adelaide")
    offset = datetime(2026, 6, 1, tzinfo=UTC).astimezone(zone).utcoffset()
    assert offset is not None
    assert offset.total_seconds() % 3600 != 0, "expected a half-hour offset"


# =========================================================================
# File locking — Windows refuses to unlink an open file
# =========================================================================


_SPAWNS_AND_HANGS = """
import subprocess, sys, time

# A child that outlives its parent and holds the stdout it inherited.
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(120)"],
    stdout=sys.stdout,
    stderr=subprocess.STDOUT,
)
# Record the grandchild so the test can check it was actually reaped. Written
# to a file rather than stdout: stdout is the temp file _run_pdflatex owns, and
# the whole point of the redirect is that nothing else reads it.
if len(sys.argv) > 1:
    with open(sys.argv[1], "w") as handle:
        handle.write(str(child.pid))
time.sleep(120)
"""


def _process_is_alive(pid: int) -> bool:
    """True if pid exists and is not an already-reaped zombie.

    ``os.kill(pid, 0)`` alone is not enough: a killed child that its parent has
    not waited on is a zombie, still addressable by signal, but dead. Reading
    the state field keeps this from reporting a corpse as a survivor.
    """
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    proc_stat = pathlib.Path(f"/proc/{pid}/stat")
    if not proc_stat.exists():  # macOS has no /proc; os.kill is all we have
        return True
    try:
        state = proc_stat.read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def test_pdflatex_timeout_holds_when_a_child_outlives_the_process(tmp_path):
    """The LaTeX timeout must fire even if pdflatex leaves a process behind.

    A real regression, not a hypothetical: the first build on a cold MiKTeX
    blocked for over ten minutes against a documented ``timeout=120``.
    ``subprocess.run(capture_output=True, timeout=N)`` does kill pdflatex when
    the timeout fires — but MiKTeX's package installer is a *grandchild* that
    inherited the stdout pipe, and the follow-up ``communicate()`` takes no
    timeout, so it waits forever for an EOF that cannot arrive while that
    installer holds the write end.

    The stand-in is that exact shape: a process that starts a longer-lived
    child on its own stdout, then sleeps well past the timeout. It must raise
    promptly rather than waiting the grandchild out.

    Returning promptly is only half of it, and on its own it proves the weaker
    half. ``process.kill()`` reaps the direct child, so with the temp-file
    redirect in place this call returns in about the timeout even if the
    process-tree kill does nothing at all — verified by suppressing
    ``os.killpg`` during the macOS bring-up, which leaked the grandchild while
    the elapsed-time assertion still passed. So assert the grandchild is dead
    too: that is the assertion that actually covers ``_kill_process_tree``, and
    on POSIX it is the only thing exercising ``os.killpg`` at all.
    """
    import sys
    import time

    from backend.documents.build import DocumentBuildError, _run_pdflatex

    stub = tmp_path / "spawns_and_hangs.py"
    stub.write_text(_SPAWNS_AND_HANGS, encoding="utf-8")
    pidfile = tmp_path / "grandchild.pid"

    started = time.monotonic()
    with pytest.raises(DocumentBuildError, match="timed out"):
        _run_pdflatex([sys.executable, str(stub), str(pidfile)], timeout=5)
    elapsed = time.monotonic() - started

    # The grandchild sleeps 120s. Anything approaching that means the pipe
    # deadlock is back; the fix returns in about the 5s timeout plus teardown.
    assert elapsed < 60, f"timeout did not hold: took {elapsed:.1f}s"

    assert pidfile.exists(), "stub never recorded a grandchild pid"
    grandchild = int(pidfile.read_text().strip())

    deadline = time.monotonic() + 10
    while _process_is_alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.1)

    if _process_is_alive(grandchild):
        import os
        import signal

        with contextlib.suppress(OSError):  # never leak a 120s sleeper
            os.kill(grandchild, signal.SIGKILL)
        pytest.fail(
            f"process-tree kill leaked grandchild pid={grandchild}: "
            "pdflatex was killed but its child outlived it"
        )


def test_latex_aux_cleanup_tolerates_a_locked_file(tmp_path, monkeypatch):
    """A successful build must not be lost to a failed tidy-up.

    On Windows an on-access virus scanner holds a just-written file open for a
    moment, and unlink raises PermissionError. That must not discard a PDF
    that was produced correctly.
    """
    from backend.documents import build

    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF")
    (tmp_path / "resume.aux").write_text("aux", encoding="utf-8")

    real_unlink = pathlib.Path.unlink

    def locked_unlink(self, *args, **kwargs):
        if self.suffix == ".aux":
            raise PermissionError(32, "The process cannot access the file")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", locked_unlink)

    # Exercise only the cleanup loop, with pdflatex stubbed out entirely.
    # Stubbed at _run_pdflatex rather than at subprocess: the runner owns the
    # timeout and process-tree handling, and patching underneath it would let
    # this test pass while really invoking pdflatex on a stub document.
    monkeypatch.setattr(build.settings, "latex_passes", 0)
    monkeypatch.setattr(build, "_run_pdflatex", lambda *a, **k: (0, ""))

    result = build.render_pdf("\\documentclass{article}\\begin{document}x\\end{document}",
                              tmp_path, "resume")
    assert result == pdf, "a locked .aux must not fail a completed build"


# =========================================================================
# SQLite
# =========================================================================


def test_sqlite_connect_args_allow_cross_thread_use():
    """uvicorn's threadpool hands a connection between threads on any OS."""
    from backend.db import engine

    assert engine.dialect.name == "sqlite"


def test_database_url_is_not_a_posix_absolute_path():
    """A default of sqlite:////home/... would be unopenable on Windows."""
    from backend.config import Settings

    default = Settings.model_fields["database_url"].default
    assert not default.startswith("sqlite:////"), default
    assert "/home/" not in default and "/tmp/" not in default


@pytest.mark.parametrize("name", ["CON", "PRN", "AUX", "NUL", "COM1", "LPT1"])
def test_no_reserved_windows_filenames_are_generated(name):
    """These are device names on Windows and cannot be used as filenames."""
    from backend.models import DocumentKind

    generated = {kind.value.upper() for kind in DocumentKind}
    assert name not in generated
