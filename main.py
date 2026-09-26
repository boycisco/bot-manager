"""Entry point: load config, wire modules, run the manager bot."""

from __future__ import annotations

import logging

from manager import config
from manager.bot import build_application
from manager.processes import ProcessManager
from manager.storage import Storage


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )

    config.ensure_dirs()
    config.validate()
    print(config.summary())

    storage = Storage()
    storage.load()
    print(f"[boot] loaded {len(storage.records)} bot record(s)")

    processes = ProcessManager(storage)
    app = build_application(storage, processes)

    print("[boot] starting polling - Ctrl+C to stop")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
