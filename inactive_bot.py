import os
import csv
import io
import time
import logging
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

from telegram import (
    Update,
    ChatMember,
    ChatMemberUpdated,
    Poll,
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

# ----- Config -----
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Where we persist last-seen data
PERSIST_FILE = "inactive_tracker.pkl"

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("inactive-bot")

# Keys inside bot_data
KEY_LAST_SEEN = "last_seen"   # dict[int user_id] -> float (unix ts)
KEY_GROUPS    = "groups"      # set[int chat_id] of groups we track


def now_ts() -> float:
    return time.time()


def ensure_storage(context: CallbackContext) -> None:
    bd = context.application.bot_data
    if KEY_LAST_SEEN not in bd:
        bd[KEY_LAST_SEEN] = {}
    if KEY_GROUPS not in bd:
        bd[KEY_GROUPS] = set()


def touch_user(context: CallbackContext, user_id: int) -> None:
    ensure_storage(context)
    context.application.bot_data[KEY_LAST_SEEN][user_id] = now_ts()


def fmt_user(user) -> str:
    name = (user.full_name or "").strip()
    if user.username:
        tag = f"@{user.username}"
        return f"{name} ({tag})" if name else tag
    return name or f"ID:{user.id}"


def human_dt(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    # Display in local time (server’s local). If you want a fixed TZ, adjust here.
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


# --- Handlers ---

async def on_start(update: Update, context: CallbackContext) -> None:
    ensure_storage(context)
    await update.message.reply_text(
        "👋 I’m tracking member activity here.\n\n"
        "• I record messages, poll votes (for polls I create), and reactions (if supported).\n"
        "• Use /inactive <days> to list inactive members.\n"
        "• Try /help for more."
    )


async def on_help(update: Update, context: CallbackContext) -> None:
    text = (
        "🛠 **Inactive Tracker Bot**\n\n"
        "Commands:\n"
        "• /inactive <days> — List users with no interactions in the last N days (default 30)\n"
        "• /lastseen @user — Show last recorded interaction time for a member\n"
        "• /stats — Show basic stats\n"
        "• /export — Export CSV of last seen times\n\n"
        "Notes:\n"
        "• For reliable results, disable privacy mode or make me an admin so I can see messages.\n"
        "• Poll votes are tracked only for polls I post.\n"
        "• Reactions are recorded if the Bot API & library expose reaction updates on this setup."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def on_message(update: Update, context: CallbackContext) -> None:
    # Track any message in groups/supergroups
    if not update.effective_chat or update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    ensure_storage(context)
    context.application.bot_data[KEY_GROUPS].add(update.effective_chat.id)

    msg: Message = update.effective_message
    if msg and msg.from_user and not msg.from_user.is_bot:
        touch_user(context, msg.from_user.id)


async def on_poll_answer(update: Update, context: CallbackContext) -> None:
    # User voted in a poll (that the bot created)
    pa = update.poll_answer
    if not pa:
        return
    user = pa.user
    if user and not user.is_bot:
        touch_user(context, user.id)


async def on_chat_member(update: Update, context: CallbackContext) -> None:
    # When members join/leave; joining could be counted as an interaction if desired
    cmu: ChatMemberUpdated = update.chat_member
    if not cmu:
        return
    ensure_storage(context)
    context.application.bot_data[KEY_GROUPS].add(cmu.chat.id)

    before: ChatMember = cmu.old_chat_member
    after: ChatMember = cmu.new_chat_member
    user = after.user
    if user and not user.is_bot:
        # Example: mark join as interaction (optional). Comment out if you don’t want this.
        if after.status in ("member", "administrator"):
            touch_user(context, user.id)


# --- Reactions support (best-effort) ---
# python-telegram-bot v21+ may include Update.message_reaction / message_reaction_count
# We handle them if present; otherwise the bot still works fine without reactions.

async def on_reaction(update: Update, context: CallbackContext) -> None:
    # Some PTB versions expose update.message_reaction with fields like user, emoji, etc.
    # This handler will be attached only if attribute exists at runtime.
    mr = getattr(update, "message_reaction", None)
    if not mr:
        return
    user = getattr(mr, "user", None)
    if user and not user.is_bot:
        touch_user(context, user.id)


# --- Utility commands ---

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

    cutoff = now_ts() - days * 86400
    bd = context.application.bot_data
    last_seen: dict[int, float] = bd[KEY_LAST_SEEN]

    # Build a set of current chat members we know about
    chat = update.effective_chat
    if not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text("Run this in a group/supergroup.")
        return

    # Try to fetch members; bots can’t enumerate *all* members via Bot API directly,
    # so we approximate using last_seen keys intersected with members we can see,
    # plus recent senders. For a thorough audit, promote the bot and ensure privacy off.
    # We will use chat.get_administrators() and the users we’ve seen so far.
    seen_user_ids = set(last_seen.keys())

    try:
        admins = await chat.get_administrators()
        admin_ids = {adm.user.id for adm in admins if not adm.user.is_bot}
    except Exception:
        admin_ids = set()

    # We can’t list every member via Bot API, so we’ll report among *tracked* users + admins.
    candidate_ids = seen_user_ids | admin_ids

    inactive_lines = []
    for uid in sorted(candidate_ids):
        ts = last_seen.get(uid, 0.0)
        if ts < cutoff:
            # Try to resolve user mention from recent message cache (best-effort)
            # We ask Telegram for ChatMember info to format name, if allowed
            try:
                cm = await chat.get_member(uid)
                label = fmt_user(cm.user)
            except Exception:
                label = f"ID:{uid}"
            seen_str = "never" if ts == 0 else human_dt(ts)
            inactive_lines.append(f"• {label} — last seen: {seen_str}")

    if not inactive_lines:
        await update.message.reply_text(f"✅ No tracked members inactive for ≥ {days} days.")
    else:
        header = f"🚫 Inactive for ≥ {days} days (tracked users):"
        # Telegram messages have length limits—chunk if very long
        chunk = [header]
        total = 0
        for line in inactive_lines:
            if sum(len(x) for x in chunk) + len(line) + 1 > 3500:
                await update.message.reply_text("\n".join(chunk))
                chunk = ["(cont’d)"]
            chunk.append(line)
        if chunk:
            await update.message.reply_text("\n".join(chunk))


async def cmd_lastseen(update: Update, context: CallbackContext) -> None:
    ensure_storage(context)
    if not context.args:
        await update.message.reply_text("Usage: /lastseen @username or numeric user ID")
        return

    target = context.args[0].lstrip("@")
    user_id = None

    # Try to resolve by username via chat member lookup
    chat = update.effective_chat
    if chat and target.isdigit():
        user_id = int(target)
    else:
        # Try best-effort: search recent tracked users to find one with that username
        bd = context.application.bot_data
        last_seen: dict[int, float] = bd[KEY_LAST_SEEN]
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

    ts = context.application.bot_data[KEY_LAST_SEEN].get(user_id)
    if not ts:
        await update.message.reply_text("No activity recorded for that user yet.")
        return

    await update.message.reply_text(f"Last seen: {human_dt(ts)}")


async def cmd_stats(update: Update, context: CallbackContext) -> None:
    ensure_storage(context)
    last_seen = context.application.bot_data[KEY_LAST_SEEN]
    groups = context.application.bot_data[KEY_GROUPS]
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
    last_seen = context.application.bot_data[KEY_LAST_SEEN]
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


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing. Put it in .env")

    persistence = PicklePersistence(filepath=PERSIST_FILE)
    app = ApplicationBuilder().token(BOT_TOKEN).persistence(persistence).build()

    # Commands
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("help", on_help))
    app.add_handler(CommandHandler("inactive", cmd_inactive))
    app.add_handler(CommandHandler("lastseen", cmd_lastseen))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("export", cmd_export))

    # Messages in groups (any content)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL, on_message))

    # Poll votes
    app.add_handler(PollAnswerHandler(on_poll_answer))

    # Member join/leave/promote/demote (optional interaction)
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))

    # Reactions (best-effort): only attach if Update has the attribute in this PTB version
    if hasattr(Update, "message_reaction"):
        # PTB doesn’t ship a dedicated ReactionHandler yet; we can catch all updates
        # and check inside. Here, reuse MessageHandler with a dummy filter that never matches messages.
        # We'll rely on Update.message_reaction being present for non-message updates.
        app.add_handler(MessageHandler(filters.ALL, on_reaction))

    log.info("Starting bot…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
