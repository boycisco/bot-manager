"""Telegram-facing layer: commands, deploy wizard, callbacks."""

from __future__ import annotations

import functools
import shutil
import logging
import time
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from . import config
from .archive import (
    UnsafeArchiveError,
    cleanup_extras,
    flatten_single_root,
    safe_extract,
)
from .installer import InstallError, install_dependencies
from .models import BotRecord
from .processes import ProcessError, ProcessManager
from .storage import Storage

# --------------------------------------------------------------------------- #
# Conversation states
# --------------------------------------------------------------------------- #
NAME, LANGUAGE, COMMAND, ENVVARS, UPLOAD = range(5)

STORAGE_KEY = "storage"
PROCS_KEY = "processes"

HELP_TEXT = (
    "*Bot manager*\n\n"
    "`/deploy` — deploy a new bot\n"
    "`/list` — show deployed bots\n"
    "`/status` — running summary\n"
    "`/logs <name>` — last log lines\n"
    "`/stop <name>` — stop a running bot\n"
    "`/restart <name>` — restart a bot\n"
    "`/delete <name>` — remove a bot and its files\n"
    "`/cancel` — abort the current wizard"
)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def _is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user) and user.id in config.ADMIN_IDS


def admin_only(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update):
            if update.effective_message:
                await update.effective_message.reply_text("⛔ Not authorized.")
            return ConversationHandler.END
        return await func(update, context)

    return wrapper


# --------------------------------------------------------------------------- #
# bot_data accessors
# --------------------------------------------------------------------------- #
def _storage(context: ContextTypes.DEFAULT_TYPE) -> Storage:
    return context.bot_data[STORAGE_KEY]


def _procs(context: ContextTypes.DEFAULT_TYPE) -> ProcessManager:
    return context.bot_data[PROCS_KEY]


# --------------------------------------------------------------------------- #
# /start and /help
# --------------------------------------------------------------------------- #
@admin_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


# --------------------------------------------------------------------------- #
# /deploy wizard
# --------------------------------------------------------------------------- #
@admin_only
async def deploy_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(_storage(context).records) >= config.MAX_BOTS:
        await update.message.reply_text(
            f"❌ You already have {config.MAX_BOTS} bots deployed.\n"
            f"Use `/delete <name>` to remove one first.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    context.user_data["draft"] = {}
    await update.message.reply_text(
        f"🚀 *New bot* — step 1/5\n\n"
        f"Send a short name (letters, digits, `-`, `_`; max 32).\n\n"
        f"/cancel anytime to abort.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return NAME


@admin_only
async def got_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.message.text or "").strip()
    if not config.NAME_RE.fullmatch(name):
        await update.message.reply_text(
            "❌ Invalid name. Use only letters, digits, `-`, `_` (max 32)."
        )
        return NAME
    if _storage(context).get(name):
        await update.message.reply_text("❌ Name already taken. Pick another:")
        return NAME

    context.user_data["draft"]["name"] = name
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🐍 Python", callback_data="lang:python"),
            InlineKeyboardButton("🟩 Node.js", callback_data="lang:node"),
            InlineKeyboardButton("⚙️ Other", callback_data="lang:other"),
        ]]
    )
    await update.message.reply_text(
        "Step 2/5 — What language is it written in?", reply_markup=kb
    )
    return LANGUAGE


@admin_only
async def got_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang = query.data.split(":", 1)[1]
    context.user_data["draft"]["language"] = lang

    hint = {"python": "python main.py", "node": "node index.js", "other": "./run.sh"}[lang]
    await query.edit_message_text(
        f"Step 3/5 — Send the command to start it.\n\n"
        f"This runs from the bot's own folder, e.g. `{hint}`.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return COMMAND


@admin_only
async def got_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd = (update.message.text or "").strip()
    if not cmd:
        await update.message.reply_text("❌ Send a non-empty command:")
        return COMMAND

    context.user_data["draft"]["command"] = cmd
    await update.message.reply_text(
        "Step 4/5 — Send environment variables, one `KEY=value` per line.\n\n"
        "This is where the child bot's own token goes, e.g.:\n"
        "`BOT_TOKEN=123456:ABC...`\n\n"
        "Send /skip if it needs none.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ENVVARS


def _parse_env(text: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = config.ENV_LINE_RE.match(line)
        if not m:
            raise ValueError(f"`{line}` is not in KEY=value form")
        env[m.group(1)] = m.group(2)
    return env


async def _ask_for_zip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        f"Step 5/5 — Send the project as a `.zip` (max {config.MAX_ZIP_MB} MB).\n\n"
        f"Tip: zip the folder's contents or a single top-level folder — both work.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return UPLOAD


@admin_only
async def got_env(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        env = _parse_env(update.message.text or "")
    except ValueError as exc:
        await update.message.reply_text(f"❌ {exc}\n\nFix and resend, or /skip.")
        return ENVVARS
    context.user_data["draft"]["env"] = env
    return await _ask_for_zip(update, context)


@admin_only
async def skip_env(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["draft"]["env"] = {}
    return await _ask_for_zip(update, context)


@admin_only
async def got_zip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    doc = message.document

    if doc is None:
        await message.reply_text("❌ Please send the project as a `.zip` file.")
        return UPLOAD
    if not (doc.file_name or "").lower().endswith(".zip"):
        await message.reply_text("❌ That's not a `.zip`. Try again or /cancel.")
        return UPLOAD
    if doc.file_size and doc.file_size > config.MAX_ZIP_MB * 1024 * 1024:
        await message.reply_text(f"❌ Too big (max {config.MAX_ZIP_MB} MB).")
        return UPLOAD

    draft = context.user_data.get("draft") or {}
    name = draft.get("name")
    if not name:
        await message.reply_text("❌ Session lost. Please /deploy again.")
        return ConversationHandler.END

    status = await message.reply_text("⬇️ Downloading archive…")

    config.ensure_dirs()
    zip_path = config.TMP_DIR / f"{name}-{int(time.time())}.zip"
    try:
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(custom_path=str(zip_path))
    except Exception as exc:
        await status.edit_text(f"❌ Download failed: {exc}")
        return ConversationHandler.END

    record = BotRecord(
        name=name,
        language=draft["language"],
        command=draft["command"],
        env=draft.get("env", {}),
        owner_chat_id=update.effective_chat.id,
    )

    try:
        if record.path.exists():
            shutil.rmtree(record.path)
        safe_extract(zip_path, record.path)
        flatten_single_root(record.path)
        cleanup_extras(record.path)
    except UnsafeArchiveError as exc:
        shutil.rmtree(record.path, ignore_errors=True)
        await status.edit_text(f"❌ Rejected archive: {exc}")
        return ConversationHandler.END
    except Exception as exc:
        shutil.rmtree(record.path, ignore_errors=True)
        await status.edit_text(f"❌ Could not unpack archive: {exc}")
        return ConversationHandler.END
    finally:
        zip_path.unlink(missing_ok=True)

    _storage(context).add(record)

    await status.edit_text("📦 Installing dependencies…")
    try:
        report = await install_dependencies(record.path, record.language)
    except InstallError as exc:
        await status.edit_text(
            f"❌ Install failed:\n<pre>{escape(str(exc)[:3000])}</pre>",
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    await status.edit_text("⚙️ Starting…")
    try:
        start_msg = await _procs(context).start(name)
    except ProcessError as exc:
        await status.edit_text(f"⚠️ Deployed but couldn't start: {exc}")
        return ConversationHandler.END

    files = sorted(
        p.name
        for p in record.path.iterdir()
        if p.name not in {".venv", "node_modules", "__pycache__"}
    )[:15]
    env_keys = ", ".join(record.env.keys()) or "none"
    files_str = ", ".join(files) or "—"

    def h(value: object) -> str:
        return escape(str(value))

    await status.edit_text(
        f"✅ <b>{h(name)}</b> deployed\n\n"
        f"<b>Language:</b> {h(record.language)}\n"
        f"<b>Command:</b> <code>{h(record.command)}</code>\n"
        f"<b>Env keys:</b> {h(env_keys)}\n"
        f"<b>Files:</b> {h(files_str)}\n\n"
        f"<i>{h(report)}</i>\n"
        f"<i>{h(start_msg)}</i>\n\n"
        f"Watch it with <code>/logs {h(name)}</code>.",
        parse_mode=ParseMode.HTML,
    )
    context.user_data.pop("draft", None)
    return ConversationHandler.END


@admin_only
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("draft", None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
# Management commands
# --------------------------------------------------------------------------- #
@admin_only
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    storage = _storage(context)
    procs = _procs(context)
    if not storage.records:
        await update.message.reply_text("No bots deployed yet. Use /deploy.")
        return

    lines = [f"*Deployed bots* ({len(storage.records)}/{config.MAX_BOTS})", ""]
    for rec in storage.all():
        alive = procs.is_running(rec.name)
        dot = "🟢" if alive else "🔴"
        lines.append(f"{dot} `{rec.name}` — {rec.language} — `{rec.command}`")
        if not alive and rec.last_exit:
            lines.append(f"     _{rec.last_exit}_")
    lines.append("")
    lines.append(f"Running: {procs.running_count()}/{config.MAX_RUNNING}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


def _arg(update: Update) -> str | None:
    parts = (update.message.text or "").split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else None


@admin_only
async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = _arg(update)
    storage = _storage(context)
    if not name or name not in storage.records:
        await update.message.reply_text(
            "Usage: `/logs <name>`", parse_mode=ParseMode.MARKDOWN
        )
        return

    rec = storage.records[name]
    text = _procs(context).tail(rec.log_path, config.LOG_TAIL_LINES)
    if len(text) > 3500:
        text = "…" + text[-3500:]

    # HTML + <pre> keeps log content safe (backticks, asterisks, etc.).
    await update.message.reply_text(
        f"📜 <b>{escape(name)}</b>\n<pre>{escape(text)}</pre>",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = _arg(update)
    if not name or name not in _storage(context).records:
        await update.message.reply_text(
            "Usage: `/stop <name>`", parse_mode=ParseMode.MARKDOWN
        )
        return
    msg = await _procs(context).stop(name)
    await update.message.reply_text(f"⏹ `{name}`: {msg}", parse_mode=ParseMode.MARKDOWN)


@admin_only
async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = _arg(update)
    if not name or name not in _storage(context).records:
        await update.message.reply_text(
            "Usage: `/restart <name>`", parse_mode=ParseMode.MARKDOWN
        )
        return
    try:
        msg = await _procs(context).restart(name)
    except ProcessError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return
    await update.message.reply_text(
        f"🔄 `{name}`: {msg}", parse_mode=ParseMode.MARKDOWN
    )


@admin_only
async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = _arg(update)
    if not name or name not in _storage(context).records:
        await update.message.reply_text(
            "Usage: `/delete <name>`", parse_mode=ParseMode.MARKDOWN
        )
        return
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🗑 Delete", callback_data=f"del:{name}"),
            InlineKeyboardButton("Cancel", callback_data="del:__cancel__"),
        ]]
    )
    await update.message.reply_text(
        f"Delete `{name}` and all of its files?",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )


@admin_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    storage = _storage(context)
    procs = _procs(context)
    await update.message.reply_text(
        f"*Status*\n"
        f"Deployed: {len(storage.records)}/{config.MAX_BOTS}\n"
        f"Running:  {procs.running_count()}/{config.MAX_RUNNING}\n"
        f"Data dir: `{config.DATA_DIR}`",
        parse_mode=ParseMode.MARKDOWN,
    )


# --------------------------------------------------------------------------- #
# Delete confirmation callback
# --------------------------------------------------------------------------- #
async def on_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _is_admin(update):
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()

    name = query.data.split(":", 1)[1]
    if name == "__cancel__":
        await query.edit_message_text("Cancelled.")
        return

    storage = _storage(context)
    procs = _procs(context)
    if name not in storage.records:
        await query.edit_message_text("Already gone.")
        return

    await procs.stop(name)
    record = storage.remove(name)
    if record is not None:
        shutil.rmtree(record.path, ignore_errors=True)

    await query.edit_message_text(
        f"🗑 Deleted `{name}`.", parse_mode=ParseMode.MARKDOWN
    )


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
async def _post_init(app: Application) -> None:
    procs: ProcessManager = app.bot_data[PROCS_KEY]
    started = await procs.restore_on_boot()
    if started:
        print(f"[boot] restored: {', '.join(started)}")
    else:
        print("[boot] no bots to restore")


async def _post_shutdown(app: Application) -> None:
    procs: ProcessManager = app.bot_data[PROCS_KEY]
    print("[shutdown] stopping child bots…")
    await procs.stop_all()


def build_application(storage: Storage, processes: ProcessManager) -> Application:
    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    app.bot_data[STORAGE_KEY] = storage
    app.bot_data[PROCS_KEY] = processes

    # Notify the owner whenever a child bot exits unexpectedly.
    async def notify_exit(name: str, code: int) -> None:
        record = storage.get(name)
        if record is None or record.owner_chat_id is None:
            return
        try:
            await app.bot.send_message(
                record.owner_chat_id,
                f"⚠️ Bot `{name}` exited with code {code}.\n"
                f"Check with `/logs {name}`.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as exc:
            print(f"[notify_exit] failed for {name}: {exc}")

    processes.on_exit = notify_exit

    deploy_conv = ConversationHandler(
        entry_points=[
            CommandHandler("deploy", deploy_entry),
            CommandHandler("new", deploy_entry),
        ],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_name)],
            LANGUAGE: [CallbackQueryHandler(got_language, pattern=r"^lang:")],
            COMMAND: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_command)],
            ENVVARS: [
                CommandHandler("skip", skip_env),
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_env),
            ],
            UPLOAD: [MessageHandler(filters.Document.ALL, got_zip)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
        allow_reentry=True,
        per_message=False,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(deploy_conv)
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CallbackQueryHandler(on_delete_callback, pattern=r"^del:"))

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Log the full traceback for us, and try to tell the user something went wrong.
        logging.getLogger(__name__).exception(
            "handler error", exc_info=context.error
        )
        if isinstance(update, Update) and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "❌ Something went wrong handling that. Check the logs."
                )
            except Exception:
                pass

    app.add_error_handler(on_error)

    return app