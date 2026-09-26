"""Data shape for one deployed bot."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import config


@dataclass
class BotRecord:
    """Everything we need to know about a deployed child bot."""

    name: str
    language: str                       # "python" | "node" | "other"
    command: str                        # shell command, e.g. "python main.py"
    env: dict[str, str] = field(default_factory=dict)
    owner_chat_id: int | None = None    # Telegram chat to notify on crashes
    created_at: float = field(default_factory=time.time)
    want_running: bool = False          # should it be running right now?
    last_exit: str | None = None        # e.g. "exit code 1 at 14:32:05"

    # ------------------------------ paths ------------------------------ #
    @property
    def path(self) -> Path:
        return config.BOTS_DIR / self.name

    @property
    def log_path(self) -> Path:
        return self.path / "bot.log"

    # ------------------------- (de)serialization ----------------------- #
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BotRecord":
        # Tolerate unknown keys from older versions of the file.
        known = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in known}
        return cls(**clean)