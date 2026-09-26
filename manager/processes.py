"""Spawn, monitor, and stop child Telegram bots as subprocesses."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Awaitable, Callable

from . import config
from .models import BotRecord
from .storage import Storage


class ProcessError(Exception):
    """User-facing error when a start/stop operation fails."""


# --------------------------------------------------------------------------- #
# Child environment
# --------------------------------------------------------------------------- #
def _venv_bin_dir(bot_path: Path, language: str) -> Path | None:
    if language == "python":
        if os.name == "nt":
            return bot_path / ".venv" / "Scripts"
        return bot_path / ".venv" / "bin"
    if language == "node":
        return bot_path / "node_modules" / ".bin"
    return None


# Environment variables the child must NEVER see (manager's own secrets).
_SECRET_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "ADMIN_IDS",
    "MANAGER_TOKEN",
    "DATA_DIR",
)


def build_child_env(record: BotRecord) -> dict[str, str]:
    """Assemble the environment the child bot will see.

    We *inherit* the parent process environment so Windows can find
    SystemRoot, SystemDrive, ComSpec, System32 DLLs, and the Winsock
    service provider — without these, asyncio cannot even open a socket
    (WinError 10106). Then we strip the manager's own secrets and layer
    the child's PATH and env vars on top.
    """
    # Start from a full copy of the parent environment.
    env: dict[str, str] = {k: v for k, v in os.environ.items() if k not in _SECRET_KEYS}

    bot_path = record.path

    # Prepend the child's own bin dir (venv/Scripts or node_modules/.bin)
    # and the bot folder itself, then keep the inherited PATH.
    path_parts: list[str] = []
    venv_bin = _venv_bin_dir(bot_path, record.language)
    if venv_bin and venv_bin.exists():
        path_parts.append(str(venv_bin))
    path_parts.append(str(bot_path))
    inherited_path = env.get("PATH", "")
    if inherited_path:
        path_parts.append(inherited_path)
    env["PATH"] = os.pathsep.join(path_parts)

    # Point HOME-ish vars at the bot's own folder, so anything that writes
    # to ~ lands inside data/bots/<name> instead of the manager's home.
    env["HOME"] = str(bot_path)
    env["USERPROFILE"] = str(bot_path)   # Windows
    env["TMPDIR"] = str(config.TMP_DIR)
    env["TEMP"] = str(config.TMP_DIR)
    env["TMP"] = str(config.TMP_DIR)

    # Sane defaults, but don't clobber anything the parent already set.
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("NODE_ENV", "production")
    env.setdefault("LANG", "C.UTF-8")

    # User-supplied vars win over everything.
    env.update({str(k): str(v) for k, v in record.env.items()})
    return env


# --------------------------------------------------------------------------- #
# Cross-platform process-tree kill
# --------------------------------------------------------------------------- #
async def _kill_tree(pid: int, force: bool = False) -> None:
    """Kill a process AND all its children.

    Linux/Mac: use process groups (we always spawn with start_new_session).
    Windows:   shell out to taskkill /T (kills the tree).
    """
    if os.name == "nt":
        args = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            args.append("/F")
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return

    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL if force else signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


# --------------------------------------------------------------------------- #
# ProcessManager
# --------------------------------------------------------------------------- #
ExitCallback = Callable[[str, int], Awaitable[None]]


class ProcessManager:
    """Owns all running child bots. One instance for the whole manager."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self.procs: dict[str, asyncio.subprocess.Process] = {}
        self._watchers: dict[str, asyncio.Task] = {}
        self.on_exit: ExitCallback | None = None

    # ------------------------------ queries ------------------------------ #
    def is_running(self, name: str) -> bool:
        proc = self.procs.get(name)
        return proc is not None and proc.returncode is None

    def running_names(self) -> list[str]:
        return [n for n in self.procs if self.is_running(n)]

    def running_count(self) -> int:
        return len(self.running_names())

    # ------------------------------ start ------------------------------ #
    async def start(self, name: str) -> str:
        record = self.storage.get(name)
        if record is None:
            raise ProcessError(f"no such bot: {name}")
        if self.is_running(name):
            return "already running"
        if self.running_count() >= config.MAX_RUNNING:
            raise ProcessError(
                f"already running {config.MAX_RUNNING} bots — stop one first"
            )

        record.path.mkdir(parents=True, exist_ok=True)
        record.want_running = True
        self.storage.save()

        # Open log in append mode; line-buffered by the child thanks to
        # PYTHONUNBUFFERED=1 / node stdout default.
        log_file = open(record.log_path, "ab", buffering=0)
        try:
            log_file.write(
                f"\n===== start {_now_str()} =====\n".encode("utf-8")
            )
        except Exception:
            pass

        try:
            proc = await asyncio.create_subprocess_shell(
                record.command,
                cwd=str(record.path),
                env=build_child_env(record),
                stdout=log_file,
                stderr=log_file,
                start_new_session=(os.name != "nt"),
                creationflags=(
                    # On Windows, start a new process group so we can kill it.
                    0x00000200  # CREATE_NEW_PROCESS_GROUP
                    if os.name == "nt"
                    else 0
                ),
            )
        except Exception as exc:
            log_file.close()
            record.want_running = False
            self.storage.save()
            raise ProcessError(f"failed to spawn: {exc}") from exc
        finally:
            # Give the child its own handle; we've finished writing the banner.
            try:
                log_file.close()
            except Exception:
                pass

        self.procs[name] = proc
        record.last_exit = None
        self.storage.save()

        # Watch for exit in the background.
        self._watchers[name] = asyncio.create_task(self._watch(name, proc))
        return f"started (pid {proc.pid})"

    # ------------------------------ watch ------------------------------ #
    async def _watch(self, name: str, proc: asyncio.subprocess.Process) -> None:
        code = await proc.wait()

        # Clean up only if this exact process is still the tracked one.
        if self.procs.get(name) is proc:
            self.procs.pop(name, None)

        record = self.storage.get(name)
        if record is not None:
            record.last_exit = f"exit code {code} at {_now_str()}"
            # If it exited on its own (not because we asked), stop retrying
            # on next manager boot.
            record.want_running = False
            self.storage.save()

        if self.on_exit is not None:
            try:
                await self.on_exit(name, code)
            except Exception as exc:  # pragma: no cover
                print(f"[processes] on_exit callback failed: {exc}")

    # ------------------------------ stop ------------------------------ #
    async def stop(self, name: str, keep_want: bool = False) -> str:
        record = self.storage.get(name)
        if record is not None and not keep_want:
            record.want_running = False
            self.storage.save()

        proc = self.procs.get(name)
        if proc is None or proc.returncode is not None:
            return "not running"

        try:
            await _kill_tree(proc.pid, force=False)
        except Exception:
            pass

        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            await _kill_tree(proc.pid, force=True)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

        self.procs.pop(name, None)
        watcher = self._watchers.pop(name, None)
        if watcher is not None:
            watcher.cancel()
        return "stopped"

    # ------------------------------ restart ------------------------------ #
    async def restart(self, name: str) -> str:
        await self.stop(name, keep_want=True)
        # Small delay so the port / token can free up if relevant.
        await asyncio.sleep(1.0)
        return await self.start(name)

    # ------------------------------ logs ------------------------------ #
    @staticmethod
    def tail(path: Path, lines: int = config.LOG_TAIL_LINES) -> str:
        if not path.exists():
            return "(no logs yet)"
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                block = min(size, 64 * 1024)
                fh.seek(size - block)
                data = fh.read()
        except OSError as exc:
            return f"(could not read log: {exc})"
        text = data.decode("utf-8", errors="replace")
        out = text.splitlines()[-lines:]
        return "\n".join(out) if out else "(empty)"

    # ------------------------------ shutdown ------------------------------ #
    async def stop_all(self) -> None:
        for name in list(self.procs):
            try:
                await self.stop(name)
            except Exception as exc:
                print(f"[processes] failed to stop {name}: {exc}")

    # ------------------------------ restore ------------------------------ #
    async def restore_on_boot(self) -> list[str]:
        """Start every bot that was running before the manager restarted."""
        started: list[str] = []
        for record in self.storage.all():
            if not record.want_running:
                continue
            if self.running_count() >= config.MAX_RUNNING:
                # Over quota; don't auto-start more.
                record.want_running = False
                self.storage.save()
                continue
            try:
                await self.start(record.name)
                started.append(record.name)
            except ProcessError as exc:
                print(f"[processes] could not restore {record.name}: {exc}")
        return started


# --------------------------------------------------------------------------- #
def _now_str() -> str:
    import time

    return time.strftime("%Y-%m-%d %H:%M:%S")