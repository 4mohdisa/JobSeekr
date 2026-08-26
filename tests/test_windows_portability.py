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
    monkeypatch.setattr(build.settings, "latex_passes", 0)

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(build.subprocess, "run", lambda *a, **k: Completed())

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
