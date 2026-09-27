# Telegram Bot Manager

A Telegram bot that hosts up to three other Telegram bots as subprocesses. Deploy a child bot by sending a ZIP file with its code, choosing its language, and providing its start command and environment variables — all from a Telegram chat.

Built for testing and small-scale hosting of multiple Telegram bots on a single Railway service.

## Features

- **Deploy from chat** — `/deploy` walks you through a 5-step wizard: name → language → start command → env vars → ZIP upload
- **Three languages** — Python (venv + pip install), Node.js (npm install), and "Other" (custom shell command)
- **Per-bot isolation** — each child bot gets its own folder under `data/bots/<name>/`, its own virtualenv or `node_modules`, its own log file, and its own environment
- **Process supervision** — starts, stops, restarts, and auto-restores child bots after a manager restart
- **Crash notifications** — you get a Telegram message when a child bot exits unexpectedly, plus the tail of its logs on demand
- **Safe archives** — ZIP extraction rejects path-traversal ("zip slip"), absolute paths, and symlinks
- **Persistent state** — bot records survive redeploys via a Railway volume
- **Admin-only** — only Telegram IDs listed in `ADMIN_IDS` can use the manager

## Commands

| Command | Purpose |
|---|---|
| `/start` or `/help` | Show help |
| `/deploy` | Walk through deploying a new bot |
| `/list` | Show all deployed bots and their status |
| `/status` | Summary: deployed count, running count, data dir |
| `/logs <name>` | Tail the last 60 lines of a bot's log |
| `/stop <name>` | Stop a running bot |
| `/restart <name>` | Restart a bot |
| `/delete <name>` | Stop and remove a bot (with confirmation) |
| `/cancel` | Abort the current deploy wizard |

## Architecture

```
bot-manager/
├── main.py                  Entry point — loads config, wires modules, runs the bot
├── railway.json             Railway deploy config
├── requirements.txt
└── manager/
    ├── config.py            Env vars, paths, validation
    ├── models.py            BotRecord dataclass
    ├── storage.py           Atomic JSON persistence of state
    ├── archive.py           Safe ZIP extraction
    ├── installer.py         pip / npm install into the bot's folder
    ├── processes.py         Spawn, monitor, stop, restore child processes
    └── bot.py               Telegram commands, deploy wizard, callbacks
```

Each child bot runs as a subprocess with `start_new_session=True` (POSIX) or `CREATE_NEW_PROCESS_GROUP` (Windows), so the manager can cleanly terminate the whole process tree.

## Local development

Requires Python 3.11 or 3.12.

```bash
git clone https://github.com/<you>/bot-manager.git
cd bot-manager
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env with your token and admin ID
python main.py
```

### Environment variables

| Name | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | Manager bot token from [@BotFather](https://t.me/BotFather) |
| `ADMIN_IDS` | yes | Comma-separated numeric Telegram user IDs allowed to control the manager. Get yours from [@userinfobot](https://t.me/userinfobot). |
| `DATA_DIR` | no | Where to store state and child bots (default: `/data` if writable, else `./data`) |
| `MAX_BOTS` | no | Max deployed bots (default `3`) |
| `MAX_RUNNING` | no | Max concurrently running bots (default `3`) |
| `MAX_ZIP_MB` | no | Max upload size in MB (default `20`) |

## Deploy to Railway

1. Fork this repo
2. Create a new Railway project → **Deploy from GitHub repo** → pick your fork
3. In **Variables**, set:
   - `TELEGRAM_BOT_TOKEN`
   - `ADMIN_IDS`
   - `DATA_DIR=/data`
4. In **Settings → Volumes**, add a volume mounted at `/data` (1 GB is plenty). **This is required** — without it, every redeploy wipes deployed bots.
5. Optionally add `NIXPACKS_PKGS=nodejs_20` if you plan to host Node.js bots

Railway detects Python and runs `python main.py`. Deploy logs show the boot sequence and any errors.

## How a child bot is packaged

Send a `.zip` containing either:

- the project files at the top level, or
- a single top-level folder containing the project

Both are handled — the manager flattens a single wrapping folder automatically.

The start command runs from the bot's own folder (`data/bots/<name>/`). For Python, a `.venv` is created automatically and `requirements.txt` is installed into it. For Node, `npm install` runs if `package.json` is present.

The child's environment gets:
- `PATH` prefixed with its own `.venv/bin` (or `node_modules/.bin`)
- `HOME` / `USERPROFILE` pointed at its own folder
- Whatever `KEY=value` pairs you supplied in the wizard
- A copy of the manager's OS environment, **minus** the manager's own secrets (`TELEGRAM_BOT_TOKEN`, `ADMIN_IDS`, `MANAGER_TOKEN`, `DATA_DIR`)

## Security notes

**This tool executes arbitrary code uploaded to it.** Only give access to people you fully trust. The `ADMIN_IDS` allowlist is the only thing standing between your host and anyone who can find the bot.

Practical hardening:

- **Keep `ADMIN_IDS` tight.** One ID, or a small handful.
- **Never commit `.env`.** It's in `.gitignore` for a reason.
- **Use a Railway volume.** Otherwise every redeploy is a fresh start.
- **Rotate the manager token** if it ever leaks (Revoke in @BotFather, then update `TELEGRAM_BOT_TOKEN`).
- **Watch the logs.** The manager prints child output to its own log file, and errors reach the Railway Deploy Logs.
- **Railway is a shared-CPU box.** A misbehaving child bot can starve the manager. The `MAX_RUNNING` cap helps; there's no memory cap yet.

There is **no sandboxing**. Child bots run as the same OS user as the manager, with access to the same filesystem. For untrusted code, run the manager inside its own container/VM you're willing to lose.

## Limits and known issues

- Max 3 deployed bots, max 3 running (configurable via env vars)
- Child bots are stopped cleanly on manager shutdown, but a hard `SIGKILL` of the manager can leave orphans
- Windows child process trees are killed via `taskkill /T`; POSIX uses process groups
- ZIP uploads go through Telegram's API — the practical size ceiling is around 20 MB

## License

## License

MIT — see [LICENSE](LICENSE) for details.