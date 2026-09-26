"""Central configuration, loaded from environment variables (or a local .env)."""

from __future__ import annotations

import os
import re
from pathlib import Path

# Load .env during local development. On Railway, real env vars are used
# and this is a no-op.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"Env var {name} must be an integer, got {raw!r}") from exc


def _admin_ids() -> set[int]:
    raw = os.getenv("ADMIN_IDS", "")
    ids: set[int] = set()
    for chunk in re.split(r"[,\s]+", raw):
        if chunk.strip().isdigit():
            ids.add(int(chunk))
    return ids


# ----------------------------- core settings ----------------------------- #
BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_IDS: set[int] = _admin_ids()

MAX_BOTS: int = _int_env("MAX_BOTS", 3)
MAX_RUNNING: int = _int_env("MAX_RUNNING", 3)
MAX_ZIP_MB: int = _int_env("MAX_ZIP_MB", 20)

# ----------------------------- filesystem ----------------------------- #
def _default_data_dir() -> Path:
    """Prefer /data on Railway (persistent volume), else ./data locally."""
    if Path("/data").is_dir() and os.access("/data", os.W_OK):
        return Path("/data")
    return Path("./data")


DATA_DIR: Path = Path(os.getenv("DATA_DIR") or _default_data_dir()).resolve()
BOTS_DIR: Path = DATA_DIR / "bots"
TMP_DIR: Path = DATA_DIR / "tmp"
STATE_FILE: Path = DATA_DIR / "state.json"

# ----------------------------- runtime tuning ----------------------------- #
LOG_TAIL_LINES: int = 60
INSTALL_TIMEOUT: int = 900
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
ENV_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)


def ensure_dirs() -> None:
    """Create data directories if they don't exist yet."""
    for path in (DATA_DIR, BOTS_DIR, TMP_DIR):
        path.mkdir(parents=True, exist_ok=True)


def validate() -> None:
    """Fail fast with a friendly message if something critical is missing."""
    problems: list[str] = []
    if not BOT_TOKEN:
        problems.append(
            "TELEGRAM_BOT_TOKEN is not set. Get one from @BotFather on Telegram."
        )
    if not ADMIN_IDS:
        problems.append(
            "ADMIN_IDS is empty. Get your numeric id from @userinfobot on Telegram."
        )
    if problems:
        raise SystemExit("\n".join("✗ " + p for p in problems))


def summary() -> str:
    """Human-readable summary, useful for debugging at startup."""
    lines = [
        "Configuration",
        f"  DATA_DIR      = {DATA_DIR}",
        f"  BOTS_DIR      = {BOTS_DIR}",
        f"  MAX_BOTS      = {MAX_BOTS}",
        f"  MAX_RUNNING   = {MAX_RUNNING}",
        f"  MAX_ZIP_MB    = {MAX_ZIP_MB}",
        f"  ADMIN_IDS     = {sorted(ADMIN_IDS)}",
        f"  TOKEN set     = {'yes' if BOT_TOKEN else 'no'}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    # Running `python -m manager.config` prints the config for a quick check.
    ensure_dirs()
    validate()
    print(summary())