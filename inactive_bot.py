#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Inactive Tracker Bot
- Tracks member "interactions": messages, poll votes (bot-created polls), and reactions (if your PTB+Bot API expose them).
- Commands:
  /inactive <days>  — list users with no interactions in the last N days (default 30)
  /lastseen @user   — show last recorded interaction time for a member
  /stats            — tracked users/groups and storage size
  /export           — CSV of user_id,last_interaction_iso (global)
  /help             — help
Notes:
- For reliable tracking, disable privacy mode in @BotFather or make the bot an admin so it can read group messages.
- Persistence file defaults to /data/inactive_tracker.pkl (works great with a Railway volume mounted at /data).
"""

import os
import io
import csv
import time
import logging
from datetime import datetime, timezone
from typing import Dict, Set

from dotenv import load_dotenv

from telegram import (
    Update,
    ChatMember,
    ChatMemberUpdated,
    Message,
)
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    PollAnswerHandler,
    ChatMemberHandler,
    CallbackContext,
    filters,
    PicklePersistence,
)

# -------------- Configuration --------------

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Prefer Railway volume if available; overrideable via env.
PERSIST_FILE = os.getenv("PERSIST_FILE", "/data/inactive_tracker.pkl")

# Ensure parent dir exists (handles local runs too).
try:
    os.makedirs(os.path.dirname(PERSIST_FILE), exist_ok=True)
except Exception:
    # dirname may be '' if set to local filename; ignore
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("inactive-bot")

# bot_data keys
KEY_LAST_SEEN = "last_seen"   # dict[int user_id] -> float (unix ts)
KEY_GROUPS    = "groups"      # set[int chat_id] of groups we track


# -------------- Helpers --------------

def now_ts() -> float:
    return time.time()

def ensure_storage(context: CallbackContext) -> None:
    bd = context.application.bot_data
    if KEY_LAST_SEEN not in bd:
        bd[KEY_LAST_SEEN] = {}  # type: ignore[assignment]
    if KEY_GROUPS not in bd:
        bd[KEY_GROUPS] = set()  # type: ignore[assignment]

def touch_user(context: CallbackContext, user_id: int) -> None:
    ensure_storage(context)
    last_seen: Dict[int, float] = context.application.bot_data[KEY_LAST_SEEN]  # type: ignore[assignment]
    last_seen[user_id] = now_ts()

def fmt_user(user) -> str:
    name = (user.full_name or "").strip()
    if user.username:
        tag = f"@{user.username}"
        return f"{name} ({tag})" if name else tag
    return name or f"ID:{user.id}"

def human_dt(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


# -------------- Command Handlers --------------

async def cmd_start(update: Update, context: CallbackContext) -> None:
    ensure_storage(context)
    await update.message.reply_text(
        "👋 I’m tracking member activity here.\n\n"
        "I mark interactions when someone sends a message, votes in a poll I create, "
        "and (if supported) reacts to a message.\n\n"
        "Commands:\n"
        "• /inactive <days>\n"
        "• /lastseen @user\n"
        "• /stats\n"
        "• /export\n"
        "• /help"
    )

async def cmd_help(update: Update, context: CallbackContext) -> None:
    await update.message.reply_text(
        "🛠 **Inactive Tracker Bot**\n\n"
        "• /inactive <days> — List users with no interactions in the last N days (default 30)\n"
        "• /lastseen @user — Show last recorded interaction time\n"
        "• /stats — Show tracked users/groups + storage size\n"
        "• /export — Export CSV of last-seen\n\n"
        "Tips:\n"
        "• Disable privacy mode or make me an admin so I can see messages.\n"
        "• I only receive poll votes for polls **I** create.\n"
        "• Reactions require library/Bot API support.\n",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_stats(update: Update, context: CallbackContext) -> None:
    ensure_storage(context)
    last_seen: Dict[int, float] = context.application.bot_data[KEY_LAST_SEEN]  # type: ignore[assignment]
    groups: Set[int] = context.application.bot_data[KEY_GROUPS]  # type: ignore[assignment]
    size_bytes = 0
    try:
        size_bytes = os.path.getsize(PERSIST_FILE)
    except Exception:
        pass
    await update.message.reply_text(
        f"📊 Tracked users: {len(last_seen)}\n"
        f"🗂 Tracked groups: {len(groups)}\n"
        f"💾 Storage size: {size_bytes} bytes"
    )

async def cmd_export(update: Update, context: CallbackContext) -> None:
    ensure_storage(context)
    last_seen: Dict[int, float] = context.application.bot_data[KEY_LAST_SEEN]  # type: ignore[assignment]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["user_id", "last_interaction_iso"])
    for uid, ts in last_seen.items():
        writer.writerow([uid, datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()])
    buf.seek(0)
    await update.message.reply_document(
        document=io.BytesIO(buf.getvalue().encode("utf-8")),
        filename="inactive_last_seen.csv",
        caption="CSV export of last-interaction timestamps."
    )

async def cmd_inactive(update: Update, context: CallbackContext) -> None:
    ensure_storage(context)
    args = context.args or []
    try:
        days = int(args[0]) if args else 30
        if days <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Usage: /inactive <days>, e.g. /inactive 30")
        return

    chat = update.effective_chat
    if not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text("Run this in a group/supergroup.")
        return

    cutoff = now_ts() - days * 86400
    bd = context.application.bot_data
    last_seen: Dict[int, float] = bd[KEY_LAST_SEEN]  # type: ignore[assignment]

    # Track this group
    groups: Set[int] = bd[KEY_GROUPS]  # type: ignore[assignment]
    groups.add(chat.id)

    # We can’t enumerate all members via Bot API; we report among tracked users + admins.
    try:
        admins = await chat.get_administrators()
        admin_ids = {adm.user.id for adm in admins if not adm.user.is_bot}
    except Exception:
        admin_ids = set()

    candidate_ids = set(last_seen.keys()) | admin_ids

    inactive_lines = []
    for uid in sorted(candidate_ids):
        ts = last_seen.get(uid, 0.0)
        if ts < cutoff:
            try:
                cm = await chat.get_member(uid)
                label = fmt_user(cm.user)
            except Exception:
                label = f"ID:{uid}"
            seen_str = "never" if ts == 0 else human_dt(ts)
            inactive_lines.append(f"• {label} — last seen: {seen_str}")

    if not inactive_lines:
        await update.message.reply_text(f"✅ No tracked members inactive for ≥ {days} days.")
        return

    header = f"🚫 Inactive for ≥ {days} days (tracked users):"
    chunk = [header]
    total_len = len(header)
    for line in inactive_lines:
        if total_len + len(line) + 1 > 3500:
            await update.message.reply_text("\n".join(chunk))
            chunk = ["(cont’d)"]
            total_len = len("(cont’d)")
        chunk.append(line)
        total_len += len(line) + 1
    if chunk:
        await update.message.reply_text("\n".join(chunk))

async def cmd_lastseen(update: Update, context: CallbackContext) -> None:
    ensure_storage(context)
    if not context.args:
        await update.message.reply_text("Usage: /lastseen @username or numeric user ID")
        return

    target = context.args[0].lstrip("@")
    chat = update.effective_chat
    user_id = None

    if target.isdigit():
        user_id = int(target)
    else:
        # Try to resolve from tracked users by username in this chat
        last_seen: Dict[int, float] = context.application.bot_data[KEY_LAST_SEEN]  # type: ignore[assignment]
        for uid in last_seen.keys():
            try:
                cm = await chat.get_member(uid)
                if cm.user.username and cm.user.username.lower() == target.lower():
                    user_id = uid
                    break
            except Exception:
                continue

    if not user_id:
        await update.message.reply_text("Couldn’t resolve that user in this chat (or not tracked yet).")
        return

    ts = context.application.bot_data[KEY_LAST_SEEN].get(user_id)  # type: ignore[index]
    if not ts:
        await update.message.reply_text("No activity recorded for that user yet.")
        return

    await update.message.reply_text(f"Last seen: {human_dt(ts)}")


# -------------- Update Handlers --------------

async def on_message(update: Update, context: CallbackContext) -> None:
    # Track any user message in groups/supergroups
    chat = update.effective_chat
    if not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    ensure_storage(context)
    groups: Set[int] = context.application.bot_data[KEY_GROUPS]  # type: ignore[assignment]
    groups.add(chat.id)

    msg: Message = update.effective_message
    if msg and msg.from_user and not msg.from_user.is_bot:
        touch_user(context, msg.from_user.id)

async def on_poll_answer(update: Update, context: CallbackContext) -> None:
    # Fired when a user votes in a poll created by this bot
    pa = update.poll_answer
    if pa and pa.user and not pa.user.is_bot:
        touch_user(context, pa.user.id)

async def on_chat_member(update: Update, context: CallbackContext) -> None:
    # When members join/leave/promote/demote
    cmu: ChatMemberUpdated = update.chat_member
    if not cmu:
        return
    ensure_storage(context)
    groups: Set[int] = context.application.bot_data[KEY_GROUPS]  # type: ignore[assignment]
    groups.add(cmu.chat.id)

    after: ChatMember = cmu.new_chat_member
    user = after.user
    if user and not user.is_bot:
        # Count (re)joining as an interaction (optional; comment out to disable)
        if after.status in ("member", "administrator"):
            touch_user(context, user.id)

async def on_reaction(update: Update, context: CallbackContext) -> None:
    """
    Best-effort reaction handler.
    Some PTB versions expose Update.message_reaction. If present, try to read user field.
    If your PTB build doesn’t support reactions yet, this handler quietly does nothing.
    """
    mr = getattr(update, "message_reaction", None)
    if not mr:
        return
    user = getattr(mr, "user", None)
    if user and not user.is_bot:
        touch_user(context, user.id)


# -------------- Main --------------

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing. Set it in your Railway Variables or a local .env")

    persistence = PicklePersistence(filepath=PERSIST_FILE)
    app = ApplicationBuilder().token(BOT_TOKEN).persistence(persistence).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("inactive", cmd_inactive))
    app.add_handler(CommandHandler("lastseen", cmd_lastseen))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("export", cmd_export))

    # Track messages in groups
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL, on_message))

    # Poll votes (bot-created)
    app.add_handler(PollAnswerHandler(on_poll_answer))

    # Member status updates
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))

    # Reactions: only meaningful if Update has the attribute in this PTB version
    if hasattr(Update, "message_reaction"):
        app.add_handler(MessageHandler(filters.ALL, on_reaction))

    log.info("Starting bot…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
