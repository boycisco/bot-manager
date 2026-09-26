"""Persist the list of deployed bots to a JSON file on disk."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import config
from .models import BotRecord


class Storage:
    """Thin wrapper around state.json with atomic writes."""

    def __init__(self) -> None:
        self.records: dict[str, BotRecord] = {}

    # ------------------------------ load ------------------------------ #
    def load(self) -> None:
        config.ensure_dirs()
        if not config.STATE_FILE.exists():
            self.records = {}
            return

        try:
            raw = json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # Corrupt state file — back it up and start fresh rather than crash.
            backup = config.STATE_FILE.with_suffix(".json.bad")
            shutil.move(str(config.STATE_FILE), str(backup))
            print(f"[storage] state.json was unreadable ({exc}); moved to {backup}")
            self.records = {}
            return

        self.records = {}
        for name, data in raw.items():
            try:
                self.records[name] = BotRecord.from_dict(data)
            except TypeError as exc:
                print(f"[storage] skipping malformed record {name!r}: {exc}")

    # ------------------------------ save ------------------------------ #
    def save(self) -> None:
        config.ensure_dirs()
        payload = {name: rec.to_dict() for name, rec in self.records.items()}
        tmp = config.STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(config.STATE_FILE)  # atomic swap

    # ------------------------------ helpers ------------------------------ #
    def get(self, name: str) -> BotRecord | None:
        return self.records.get(name)

    def add(self, record: BotRecord) -> None:
        self.records[record.name] = record
        self.save()

    def remove(self, name: str) -> BotRecord | None:
        record = self.records.pop(name, None)
        if record is not None:
            self.save()
        return record

    def all(self) -> list[BotRecord]:
        return sorted(self.records.values(), key=lambda r: r.name)