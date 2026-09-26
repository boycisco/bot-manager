"""Run language-specific dependency installation inside a bot's folder.

Uses `create_subprocess_exec` (argument list, no shell) for all commands
we control — this avoids Windows/POSIX quoting differences entirely.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
from pathlib import Path

from . import config


class InstallError(Exception):
    """Raised when installation fails; message is user-facing."""


# ------------------------------ runner ------------------------------ #
async def _run_exec(args: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    """Run a program with an explicit argument list (no shell)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise InstallError(f"command not found: {args[0]!r} ({exc})") from exc

    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        _kill_group(proc.pid)
        raise InstallError(f"timed out after {timeout}s: {' '.join(args)}")
    return proc.returncode or 0, out.decode("utf-8", errors="replace")


def _kill_group(pid: int) -> None:
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# ------------------------------ public API ------------------------------ #
async def install_dependencies(bot_path: Path, language: str) -> str:
    """Install deps for the given language. Returns a short human report."""
    if language == "python":
        return await _install_python(bot_path)
    if language == "node":
        return await _install_node(bot_path)
    return "no install step for this language"


# ------------------------------ python ------------------------------ #
def _venv_paths(venv_dir: Path) -> tuple[Path, Path]:
    """Return (python, pip) executables for a venv, cross-platform."""
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe", venv_dir / "Scripts" / "pip.exe"
    return venv_dir / "bin" / "python", venv_dir / "bin" / "pip"


async def _install_python(bot_path: Path) -> str:
    venv_dir = bot_path / ".venv"
    venv_python, venv_pip = _venv_paths(venv_dir)

    if not venv_python.exists():
        rc, out = await _run_exec(
            [sys.executable, "-m", "venv", ".venv"],
            bot_path,
            timeout=300,
        )
        if rc != 0 or not venv_python.exists():
            raise InstallError(f"venv creation failed:\n{_tail(out)}")

    requirements = bot_path / "requirements.txt"
    pyproject = bot_path / "pyproject.toml"

    if requirements.exists():
        rc, out = await _run_exec(
            [
                str(venv_pip),
                "install",
                "-q",
                "--disable-pip-version-check",
                "-r",
                "requirements.txt",
            ],
            bot_path,
            timeout=config.INSTALL_TIMEOUT,
        )
        if rc != 0:
            raise InstallError(f"pip install failed:\n{_tail(out)}")
        return "pip install ok"

    if pyproject.exists():
        rc, out = await _run_exec(
            [str(venv_pip), "install", "-q", "--disable-pip-version-check", "."],
            bot_path,
            timeout=config.INSTALL_TIMEOUT,
        )
        if rc != 0:
            raise InstallError(f"pip install (pyproject) failed:\n{_tail(out)}")
        return "pip install (pyproject) ok"

    return "no requirements.txt / pyproject.toml"


# ------------------------------ node ------------------------------ #
async def _install_node(bot_path: Path) -> str:
    package_json = bot_path / "package.json"
    if not package_json.exists():
        return "no package.json"

    npm = shutil.which("npm")
    if not npm:
        raise InstallError("npm not found on PATH — is Node.js installed?")

    rc, out = await _run_exec(
        [npm, "install", "--no-audit", "--no-fund", "--loglevel=error"],
        bot_path,
        timeout=config.INSTALL_TIMEOUT,
    )
    if rc != 0:
        raise InstallError(f"npm install failed:\n{_tail(out)}")
    return "npm install ok"


# ------------------------------ helper ------------------------------ #
def _tail(text: str, lines: int = 20) -> str:
    parts = text.strip().splitlines()
    return "\n".join(parts[-lines:]) if parts else "(no output)"