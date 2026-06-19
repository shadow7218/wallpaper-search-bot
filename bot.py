#!/usr/bin/env python3

import os
import logging
from datetime import datetime, date
from bson import ObjectId

import motor.motor_asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pytz import timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMember
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ===========================================================================
# CONFIG
# ===========================================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BOT_TOKEN           = os.environ["BOT_TOKEN"]
OWNER_ID            = int(os.environ["OWNER_ID"])
MONGODB_URI         = os.environ["MONGODB_URI"]
JOIN_CHANNEL_URL    = os.environ.get("JOIN_CHANNEL_URL", "https://t.me/yourchannel")

# ===========================================================================
# MONGODB
# ===========================================================================

client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URI)
db = client.wallpaper_bot

wallpapers_collection   = db.wallpapers
users_collection        = db.users
requests_collection     = db.requests
categories_collection   = db.categories
admins_collection       = db.admins
channels_collection     = db.channels
banned_collection       = db.banned
settings_collection     = db.settings
usage_collection        = db.usage

# ===========================================================================
# CONVERSATION STATES
# ===========================================================================

UPLOAD_PHOTO, UPLOAD_TAGS = range(2)

# ===========================================================================
# INDEX SETUP
# ===========================================================================

async def setup_indexes():
    await wallpapers_collection.create_index("tags")
    await wallpapers_collection.create_index("upload_date")
    await users_collection.create_index("daily_subscribed")
    await users_collection.create_index("last_active")
    await usage_collection.create_index([("date", 1), ("tag", 1)])
    await usage_collection.create_index([("date", 1), ("category", 1)])
    await banned_collection.create_index("user_id", unique=True)
    await admins_collection.create_index("user_id", unique=True)
    await channels_collection.create_index("username", unique=True)
    await categories_collection.create_index("name", unique=True)

# ===========================================================================
# HELPERS — ROLE CHECKS
# ===========================================================================

def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID

async def is_admin(user_id: int) -> bool:
    if is_owner(user_id):
        return True
    doc = await admins_collection.find_one({"user_id": user_id})
    return doc is not None

async def is_banned(user_id: int) -> bool:
    doc = await banned_collection.find_one({"user_id": user_id})
    return doc is not None

# ===========================================================================
# HELPERS — SETTINGS
# ===========================================================================

async def get_setting(key: str, default=None):
    doc = await settings_collection.find_one({"key": key})
    return doc["value"] if doc else default

async def set_setting(key: str, value):
    await settings_collection.update_one(
        {"key": key}, {"$set": {"value": value}}, upsert=True
    )

# ===========================================================================
# HELPERS — FORCE-JOIN
# ===========================================================================

async def check_force_join(bot, user_id: int) -> list:
    not_joined = []
    async for ch in channels_collection.find({}):
        try:
            member = await bot.get_chat_member(chat_id=ch["username"], user_id=user_id)
            if member.status in (ChatMember.LEFT, ChatMember.BANNED):
                not_joined.append(ch)
        except Exception:
            not_joined.append(ch)
    return not_joined

def join_buttons(not_joined: list) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton(f"📢 Join {ch['username']}", url=ch.get("url", JOIN_CHANNEL_URL))]
        for ch in not_joined
    ]
    keyboard.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_join")])
    return InlineKeyboardMarkup(keyboard)

# ===========================================================================
# HELPERS — GATE CHECK
# ===========================================================================

async def gate_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id

    if await is_banned(user_id):
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return False

    if not is_owner(user_id):
        locked = await get_setting("locked", False)
        if locked:
            keyboard = [[InlineKeyboardButton("📢 Join Channel", url=JOIN_CHANNEL_URL)]]
            await update.message.reply_text(
                "🔒 The bot is currently locked by the owner.\nJoin our channel for updates.",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return False

        not_joined = await check_force_join(context.bot, user_id)
        if not_joined:
            await update.message.reply_text(
                "🔒 To use this bot you must join our channel.",
                reply_markup=join_buttons(not_joined),
            )
            return False

    return True

# ===========================================================================
# HELPERS — SEARCH LIMIT
# ===========================================================================

async def check_search_limit(user_id: int) -> bool:
    if is_owner(user_id):
        return True
    if await is_admin(user_id):
        return True

    limit = await get_setting("search_limit", None)
    if limit is None:
        return True

    today = date.today().isoformat()
    doc = await users_collection.find_one({"_id": user_id})
    searches_today = 0
    if doc and doc.get("search_date") == today:
        searches_today = doc.get("searches_today", 0)

    return searches_today < int(limit)

async def increment_search_count(user_id: int):
    today = date.today().isoformat()
    doc = await users_collection.find_one({"_id": user_id})
    if doc and doc.get("search_date") == today:
        await users_collection.update_one(
            {"_id": user_id},
            {"$inc": {"searches_today": 1}},
        )
    else:
        await users_collection.update_one(
            {"_id": user_id},
            {"$set": {"search_date": today, "searches_today": 1}},
            upsert=True,
        )

# ===========================================================================
# HELPERS — USAGE TRACKING
# ===========================================================================

async def track_search(tag: str, category: str = None):
    today = date.today().isoformat()
    await usage_collection.update_one(
        {"date": today, "tag": tag},
        {"$inc": {"count": 1}},
        upsert=True,
    )
    if category:
        await usage_collection.update_one(
            {"date": today, "category": category},
            {"$inc": {"cat_count": 1}},
            upsert=True,
        )

# ===========================================================================
# HELPERS — REGISTER USER
# ===========================================================================

async def register_user(user):
    today = date.today().isoformat()
    await users_collection.update_one(
        {"_id": user.id},
        {
            "$set": {
                "username":    user.username or user.first_name,
                "first_name":  user.first_name,
                "last_name":   user.last_name,
                "last_active": today,
            },
            "$setOnInsert": {"join_date": today, "searches_today": 0},
        },
        upsert=True,
    )

# ===========================================================================
# PUBLIC COMMANDS
# ===========================================================================

# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await register_user(user)

    if await is_banned(user.id):
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return

    not_joined = await check_force_join(context.bot, user.id)
    if not_joined and not is_owner(user.id):
        await update.message.reply_text(
            "🔒 To use this bot you must join our channel.",
            reply_markup=join_buttons(not_joined),
        )
        return

    await update.message.reply_html(
        f"Hi {user.mention_html()}! Welcome to the Wallpaper Bot. 🖼️\n"
        "I can help you find amazing wallpapers!\n"
        "Type /help to see all available commands."
    )

# ---------------------------------------------------------------------------
# /help  — normal users see only public commands; owner sees everything
# ---------------------------------------------------------------------------

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    text = (
        "🖼️ <b>Wallpaper Bot</b>\n\n"
        "/start — Welcome message\n"
        "/help — Show this menu\n"
        "/search &lt;tag&gt; — Search wallpapers\n"
        "/browse — Browse by category\n"
        "/random — Random wallpaper\n"
        "/trending — Most popular wallpapers\n"
        "/request &lt;description&gt; — Request a wallpaper\n"
        "/daily — Subscribe to daily wallpaper\n"
        "/stopdaily — Unsubscribe from daily\n"
    )

    if is_owner(user_id):
        text += (
            "\n🔧 <b>Owner Commands</b>\n\n"
            "<b>Upload</b>\n"
            "/upload — Upload a wallpaper\n"
            "/stats — Bot statistics\n\n"
            "<b>Broadcast</b>\n"
            "/broadcast &lt;msg&gt; — Send to all users\n\n"
            "<b>Wallpaper Management</b>\n"
            "/list — Latest 20 wallpapers\n"
            "/find &lt;tag&gt; — Find wallpapers by tag\n"
            "/delete &lt;id&gt; — Delete wallpaper\n\n"
            "<b>Category Management</b>\n"
            "/addcategory Name|tag\n"
            "/listcategories\n"
            "/renamecategory Old|New\n"
            "/deletecategory Name\n\n"
            "<b>Admin Management</b>\n"
            "/addadmin &lt;user_id&gt;\n"
            "/removeadmin &lt;user_id&gt;\n"
            "/admins\n\n"
            "<b>Access Control</b>\n"
            "/lock — Lock bot\n"
            "/unlimited — Remove all limits\n"
            "/allow &lt;number&gt; — Set daily search limit\n"
            "/ban &lt;user_id&gt;\n"
            "/unban &lt;user_id&gt;\n"
            "/banned\n\n"
            "<b>Force-Join Channels</b>\n"
            "/addchannel @channel\n"
            "/removechannel @channel\n"
            "/channels\n\n"
            "<b>Reports</b>\n"
            "/users — User statistics\n"
            "/usage — Usage statistics\n"
        )

    await update.message.reply_html(text)

# ---------------------------------------------------------------------------
# /search
# ---------------------------------------------------------------------------

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate_check(update, context):
        return

    user_id = update.effective_user.id
    query = " ".join(context.args).lower().strip()
    if not query:
        await update.message.reply_text("Please provide a search term. Example: /search naruto")
        return

    if not await check_search_limit(user_id):
        limit = await get_setting("search_limit", 0)
        await update.message.reply_text(f"⚠️ Daily search limit reached ({limit} searches/day).")
        return

    wallpapers = await wallpapers_collection.find(
        {"tags": {"$regex": query, "$options": "i"}}
    ).to_list(length=50)

    if wallpapers:
        await update.message.reply_text(f"Found {len(wallpapers)} wallpapers for '{query}':")
        for w in wallpapers:
            try:
                await update.message.reply_document(w["file_id"])
                await wallpapers_collection.update_one({"_id": w["_id"]}, {"$inc": {"view_count": 1}})
            except Exception as e:
                logger.warning(f"Error sending wallpaper {w['_id']}: {e}")
        await increment_search_count(user_id)
        await track_search(query)
    else:
        await update.message.reply_text(f"No wallpapers found for '{query}'.")

# ---------------------------------------------------------------------------
# /browse  — loads categories dynamically from MongoDB
# ---------------------------------------------------------------------------

async def browse_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate_check(update, context):
        return

    categories = await categories_collection.find({}).sort("name", 1).to_list(length=200)
    if not categories:
        await update.message.reply_text("No categories available yet.")
        return

    keyboard = [
        [InlineKeyboardButton(cat["name"], callback_data=f"cattag_{cat['tag']}")]
        for cat in categories
    ]
    await update.message.reply_text("📂 Choose a category:", reply_markup=InlineKeyboardMarkup(keyboard))

# ---------------------------------------------------------------------------
# /random
# ---------------------------------------------------------------------------

async def random_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate_check(update, context):
        return

    query_tag = " ".join(context.args).lower().strip()
    pipeline = []
    if query_tag:
        pipeline.append({"$match": {"tags": {"$regex": query_tag, "$options": "i"}}})
    pipeline.append({"$sample": {"size": 1}})

    result = await wallpapers_collection.aggregate(pipeline).to_list(length=1)
    if result:
        w = result[0]
        await update.message.reply_document(w["file_id"])
        await wallpapers_collection.update_one({"_id": w["_id"]}, {"$inc": {"view_count": 1}})
    else:
        msg = f"No wallpapers found for '{query_tag}'." if query_tag else "No wallpapers available yet."
        await update.message.reply_text(msg)

# ---------------------------------------------------------------------------
# /trending
# ---------------------------------------------------------------------------

async def trending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate_check(update, context):
        return

    wallpapers = await wallpapers_collection.find({}).sort("view_count", -1).limit(10).to_list(length=10)
    if not wallpapers:
        await update.message.reply_text("No wallpapers available yet.")
        return

    await update.message.reply_text("🔥 Top 10 Trending Wallpapers:")
    for w in wallpapers:
        try:
            await update.message.reply_document(w["file_id"])
            await wallpapers_collection.update_one({"_id": w["_id"]}, {"$inc": {"view_count": 1}})
        except Exception as e:
            logger.warning(f"Error sending trending wallpaper {w['_id']}: {e}")

# ---------------------------------------------------------------------------
# /request
# ---------------------------------------------------------------------------

async def request_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate_check(update, context):
        return

    request_text = " ".join(context.args).strip()
    if not request_text:
        await update.message.reply_text("Please describe the wallpaper you want. Example: /request gojo purple")
        return

    user = update.effective_user
    await requests_collection.insert_one({
        "user_id":      user.id,
        "username":     user.username or user.first_name,
        "request_text": request_text,
        "timestamp":    datetime.utcnow(),
    })
    await update.message.reply_text("✅ Request submitted! The owner will review it.")
    try:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=(
                f"📩 New wallpaper request\n"
                f"From: @{user.username or user.first_name} (ID: {user.id})\n\n"
                f"{request_text}"
            ),
        )
    except Exception:
        pass

# ---------------------------------------------------------------------------
# /daily  /stopdaily
# ---------------------------------------------------------------------------

async def daily_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate_check(update, context):
        return
    user = update.effective_user
    await users_collection.update_one(
        {"_id": user.id},
        {"$set": {"daily_subscribed": True, "username": user.username or user.first_name}},
        upsert=True,
    )
    await update.message.reply_text("✅ Subscribed to daily wallpapers! You'll get one every day at 12:00 PM IST.")

async def stop_daily_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await users_collection.update_one(
        {"_id": update.effective_user.id}, {"$set": {"daily_subscribed": False}}
    )
    await update.message.reply_text("✅ Unsubscribed from daily wallpapers.")

# ===========================================================================
# OWNER COMMANDS
# ===========================================================================

# ---------------------------------------------------------------------------
# /upload  — conversation; shows wallpaper ID after upload
# ---------------------------------------------------------------------------

async def upload_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner only.")
        return ConversationHandler.END
    await update.message.reply_text("📤 Send the wallpaper image.")
    return UPLOAD_PHOTO

async def upload_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document and update.message.document.mime_type.startswith("image"):
        file_id = update.message.document.file_id
    else:
        await update.message.reply_text("That's not an image. Please send an image.")
        return UPLOAD_PHOTO
    context.user_data["file_id"] = file_id
    await update.message.reply_text("✅ Image received! Now send the tags (space-separated, e.g., naruto sasuke fight).")
    return UPLOAD_TAGS

async def upload_tags(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tags = [t.strip().lower() for t in update.message.text.split() if t.strip()]
    if not tags:
        await update.message.reply_text("No tags provided. Try again.")
        return UPLOAD_TAGS

    file_id = context.user_data["file_id"]
    result = await wallpapers_collection.insert_one({
        "file_id":     file_id,
        "tags":        tags,
        "view_count":  0,
        "upload_date": datetime.utcnow(),
    })
    wall_id = str(result.inserted_id)
    await update.message.reply_html(
        f"✅ Wallpaper uploaded!\n\n"
        f"Wallpaper ID:\n<code>{wall_id}</code>\n\n"
        f"Tags:\n{' '.join(tags)}"
    )
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Upload cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# ---------------------------------------------------------------------------
# /stats
# ---------------------------------------------------------------------------

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner only.")
        return

    total_users       = await users_collection.count_documents({})
    subscribed        = await users_collection.count_documents({"daily_subscribed": True})
    total_wallpapers  = await wallpapers_collection.count_documents({})
    total_categories  = await categories_collection.count_documents({})

    await update.message.reply_html(
        f"📊 <b>Bot Statistics</b>\n\n"
        f"👥 Total Users: {total_users}\n"
        f"📅 Daily Subscribers: {subscribed}\n"
        f"🖼️ Total Wallpapers: {total_wallpapers}\n"
        f"📂 Total Categories: {total_categories}"
    )

# ---------------------------------------------------------------------------
# /broadcast  — sends to all users, reports success/fail
# ---------------------------------------------------------------------------

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner only.")
        return

    message_text = " ".join(context.args).strip()
    if not message_text:
        await update.message.reply_text("Usage: /broadcast Your message here")
        return

    sent = failed = 0
    async for user in users_collection.find({}):
        try:
            await context.bot.send_message(chat_id=user["_id"], text=message_text)
            sent += 1
        except Exception as e:
            logger.warning(f"Broadcast failed for {user['_id']}: {e}")
            failed += 1

    await update.message.reply_text(f"📢 Broadcast done.\n✅ Sent: {sent}\n❌ Failed: {failed}")

# ---------------------------------------------------------------------------
# /list  — latest 20 wallpapers (ID + tags)
# ---------------------------------------------------------------------------

async def list_wallpapers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner only.")
        return

    wallpapers = await wallpapers_collection.find({}).sort("upload_date", -1).limit(20).to_list(length=20)
    if not wallpapers:
        await update.message.reply_text("No wallpapers found.")
        return

    lines = []
    for w in wallpapers:
        tags_str = " ".join(w.get("tags", []))
        lines.append(f"Wallpaper ID:\n<code>{w['_id']}</code>\n\nTags:\n{tags_str}")

    # Telegram message limit: split if needed
    chunk = []
    char_count = 0
    for block in lines:
        if char_count + len(block) > 3500:
            await update.message.reply_html("\n\n---\n\n".join(chunk))
            chunk = []
            char_count = 0
        chunk.append(block)
        char_count += len(block)
    if chunk:
        await update.message.reply_html("\n\n---\n\n".join(chunk))

# ---------------------------------------------------------------------------
# /find TAG  — search by tag, show ID + tags + image
# ---------------------------------------------------------------------------

async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner only.")
        return

    tag = " ".join(context.args).strip()
    if not tag:
        await update.message.reply_text("Usage: /find sasuke")
        return

    wallpapers = await wallpapers_collection.find(
        {"tags": {"$regex": tag, "$options": "i"}}
    ).to_list(length=50)

    if not wallpapers:
        await update.message.reply_text(f"No wallpapers found for tag: {tag}")
        return

    await update.message.reply_text(f"Found {len(wallpapers)} wallpapers for '{tag}':")
    for w in wallpapers:
        tags_str = " ".join(w.get("tags", []))
        caption = f"Wallpaper ID:\n<code>{w['_id']}</code>\n\nTags:\n{tags_str}"
        try:
            await update.message.reply_document(w["file_id"], caption=caption, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Could not send wallpaper {w['_id']}: {e}")
            await update.message.reply_html(caption)

# ---------------------------------------------------------------------------
# /delete WALLPAPER_ID  — inline confirmation
# ---------------------------------------------------------------------------

async def delete_wallpaper_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner only.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /delete WALLPAPER_ID")
        return

    wall_id_str = context.args[0].strip()
    try:
        oid = ObjectId(wall_id_str)
    except Exception:
        await update.message.reply_text("❌ Invalid wallpaper ID format.")
        return

    existing = await wallpapers_collection.find_one({"_id": oid})
    if not existing:
        await update.message.reply_text(f"No wallpaper found with ID: {wall_id_str}")
        return

    tags_str = " ".join(existing.get("tags", []))
    keyboard = [[
        InlineKeyboardButton("✅ Confirm", callback_data=f"delconfirm_{wall_id_str}"),
        InlineKeyboardButton("❌ Cancel",  callback_data="delcancel"),
    ]]
    await update.message.reply_html(
        f"⚠️ <b>Confirm deletion</b>\n\n"
        f"Wallpaper ID:\n<code>{wall_id_str}</code>\n\n"
        f"Tags:\n{tags_str}",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

# ---------------------------------------------------------------------------
# CATEGORY MANAGEMENT
# ---------------------------------------------------------------------------

async def add_category_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner only.")
        return

    raw = " ".join(context.args).strip()
    if "|" not in raw:
        await update.message.reply_text("Usage: /addcategory Category Name|tag\nExample: /addcategory Naruto|shadow")
        return

    name, tag = raw.split("|", 1)
    name, tag = name.strip(), tag.strip().lower()
    if not name or not tag:
        await update.message.reply_text("Both name and tag are required.")
        return

    if await categories_collection.find_one({"name": name}):
        await update.message.reply_text(f"❌ Category '{name}' already exists.")
        return

    await categories_collection.insert_one({"name": name, "tag": tag})
    await update.message.reply_text(f"✅ Category added:\nName: {name}\nTag: {tag}")

async def list_categories_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner only.")
        return

    categories = await categories_collection.find({}).sort("name", 1).to_list(length=200)
    if not categories:
        await update.message.reply_text("No categories found.")
        return

    lines = [f"{cat['name']} -> {cat['tag']}" for cat in categories]
    await update.message.reply_text("📂 Categories:\n\n" + "\n".join(lines))

async def rename_category_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner only.")
        return

    raw = " ".join(context.args).strip()
    if "|" not in raw:
        await update.message.reply_text("Usage: /renamecategory Old Name|New Name")
        return

    old_name, new_name = raw.split("|", 1)
    old_name, new_name = old_name.strip(), new_name.strip()
    if not old_name or not new_name:
        await update.message.reply_text("Both old name and new name are required.")
        return

    existing = await categories_collection.find_one({"name": old_name})
    if not existing:
        await update.message.reply_text(f"❌ No category found: '{old_name}'")
        return

    await categories_collection.update_one({"name": old_name}, {"$set": {"name": new_name}})
    await update.message.reply_text(
        f"✅ Category renamed:\n"
        f"Old: {old_name}\n"
        f"New: {new_name}\n"
        f"Tag unchanged: {existing['tag']}"
    )

async def delete_category_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner only.")
        return

    name = " ".join(context.args).strip()
    if not name:
        await update.message.reply_text("Usage: /deletecategory Category Name")
        return

    result = await categories_collection.delete_one({"name": name})
    if result.deleted_count:
        await update.message.reply_text(f"✅ Category '{name}' deleted. Wallpapers are not affected.")
    else:
        await update.message.reply_text(f"❌ No category found: '{name}'")

# ---------------------------------------------------------------------------
# ADMIN MANAGEMENT (owner only)
# ---------------------------------------------------------------------------

async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /addadmin USER_ID")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    if target_id == OWNER_ID:
        await update.message.reply_text("Owner is already the highest privilege.")
        return
    try:
        await admins_collection.insert_one({"user_id": target_id, "added_at": datetime.utcnow()})
        await update.message.reply_text(f"✅ User {target_id} added as admin.")
    except Exception:
        await update.message.reply_text(f"User {target_id} is already an admin.")

async def remove_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /removeadmin USER_ID")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    result = await admins_collection.delete_one({"user_id": target_id})
    if result.deleted_count:
        await update.message.reply_text(f"✅ User {target_id} removed from admins.")
    else:
        await update.message.reply_text(f"User {target_id} is not an admin.")

async def list_admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner only.")
        return
    admins = await admins_collection.find({}).to_list(length=200)
    if not admins:
        await update.message.reply_text("No admins added yet.")
        return
    lines = [f"• {a['user_id']}" for a in admins]
    await update.message.reply_text("👮 Admins:\n" + "\n".join(lines))

# ---------------------------------------------------------------------------
# ACCESS CONTROL (owner only)
# ---------------------------------------------------------------------------

async def lock_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner only.")
        return
    locked = await get_setting("locked", False)
    if locked:
        await set_setting("locked", False)
        await update.message.reply_text("🔓 Bot unlocked.")
    else:
        await set_setting("locked", True)
        await update.message.reply_text("🔒 Bot locked for all users.")

async def unlimited_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner only.")
        return
    await set_setting("locked", False)
    await set_setting("search_limit", None)
    await update.message.reply_text("✅ Bot unlocked. All limits removed.")

async def allow_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /allow NUMBER\nExample: /allow 10")
        return
    try:
        limit = int(context.args[0])
        if limit < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid positive number.")
        return
    await set_setting("search_limit", limit)
    await set_setting("locked", False)
    await update.message.reply_text(f"✅ Daily search limit set to {limit} searches per user.")

# ---------------------------------------------------------------------------
# BAN SYSTEM (owner only)
# ---------------------------------------------------------------------------

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /ban USER_ID")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    if target_id == OWNER_ID:
        await update.message.reply_text("You cannot ban yourself.")
        return
    try:
        await banned_collection.insert_one({"user_id": target_id, "banned_at": datetime.utcnow()})
        await update.message.reply_text(f"✅ User {target_id} banned.")
    except Exception:
        await update.message.reply_text(f"User {target_id} is already banned.")

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /unban USER_ID")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    result = await banned_collection.delete_one({"user_id": target_id})
    if result.deleted_count:
        await update.message.reply_text(f"✅ User {target_id} unbanned.")
    else:
        await update.message.reply_text(f"User {target_id} is not banned.")

async def list_banned_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner only.")
        return
    banned = await banned_collection.find({}).to_list(length=200)
    if not banned:
        await update.message.reply_text("No banned users.")
        return
    lines = [f"• {b['user_id']}" for b in banned]
    await update.message.reply_text("🚫 Banned Users:\n" + "\n".join(lines))

# ---------------------------------------------------------------------------
# CHANNEL MANAGEMENT (owner only)
# ---------------------------------------------------------------------------

async def add_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /addchannel @ChannelName")
        return
    username = context.args[0].strip()
    if not username.startswith("@"):
        username = "@" + username
    url = f"https://t.me/{username.lstrip('@')}"
    try:
        await channels_collection.insert_one({"username": username, "url": url})
        await update.message.reply_text(f"✅ Channel {username} added to required channels.")
    except Exception:
        await update.message.reply_text(f"Channel {username} is already in the list.")

async def remove_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /removechannel @ChannelName")
        return
    username = context.args[0].strip()
    if not username.startswith("@"):
        username = "@" + username
    result = await channels_collection.delete_one({"username": username})
    if result.deleted_count:
        await update.message.reply_text(f"✅ Channel {username} removed.")
    else:
        await update.message.reply_text(f"Channel {username} not found.")

async def list_channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner only.")
        return
    channels = await channels_collection.find({}).to_list(length=100)
    if not channels:
        await update.message.reply_text("No required channels set.")
        return
    lines = [ch["username"] for ch in channels]
    await update.message.reply_text("📢 Required Channels:\n" + "\n".join(lines))

# ---------------------------------------------------------------------------
# REPORTS (owner only)
# ---------------------------------------------------------------------------

async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner only.")
        return
    today = date.today().isoformat()
    total        = await users_collection.count_documents({})
    active_today = await users_collection.count_documents({"last_active": today})
    new_today    = await users_collection.count_documents({"join_date": today})
    await update.message.reply_html(
        f"👥 <b>User Statistics</b>\n\n"
        f"Total Users: {total}\n"
        f"Active Today: {active_today}\n"
        f"New Today: {new_today}"
    )

async def usage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner only.")
        return
    today = date.today().isoformat()

    total_searches   = 0
    most_searched    = "N/A"
    top_count        = 0

    async for doc in usage_collection.find({"date": today, "tag": {"$exists": True}}):
        total_searches += doc.get("count", 0)
        if doc.get("count", 0) > top_count:
            top_count     = doc["count"]
            most_searched = doc["tag"]

    top_category  = "N/A"
    top_cat_count = 0
    async for doc in usage_collection.find({"date": today, "category": {"$exists": True}}):
        if doc.get("cat_count", 0) > top_cat_count:
            top_cat_count = doc["cat_count"]
            top_category  = doc["category"]

    await update.message.reply_html(
        f"📈 <b>Usage Statistics (Today)</b>\n\n"
        f"Total Searches: {total_searches}\n"
        f"Most Searched Tag: {most_searched} ({top_count})\n"
        f"Most Used Category: {top_category} ({top_cat_count})"
    )

# ===========================================================================
# CALLBACK QUERY HANDLER
# ===========================================================================

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data    = query.data
    user_id = query.from_user.id

    # --- Force-join verification ---
    if data == "check_join":
        not_joined = await check_force_join(context.bot, user_id)
        if not_joined:
            await query.edit_message_text(
                "🔒 You still haven't joined all required channels.",
                reply_markup=join_buttons(not_joined),
            )
        else:
            await query.edit_message_text("✅ You're verified! You can now use the bot.")
        return

    # --- Category browse ---
    if data.startswith("cattag_"):
        tag = data[len("cattag_"):]

        if await is_banned(user_id):
            await query.edit_message_text("🚫 You are banned.")
            return

        if not is_owner(user_id):
            locked = await get_setting("locked", False)
            if locked:
                await query.edit_message_text("🔒 The bot is currently locked by the owner.")
                return

            not_joined = await check_force_join(context.bot, user_id)
            if not_joined:
                await query.edit_message_text(
                    "🔒 Please join our channel first.",
                    reply_markup=join_buttons(not_joined),
                )
                return

        await query.edit_message_text(f"🔍 Searching: {tag}...")

        wallpapers = await wallpapers_collection.find(
            {"tags": {"$regex": tag, "$options": "i"}}
        ).to_list(length=50)

        if wallpapers:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"Found {len(wallpapers)} wallpapers:",
            )
            for w in wallpapers:
                try:
                    await context.bot.send_document(
                        chat_id=query.message.chat_id, document=w["file_id"]
                    )
                    await wallpapers_collection.update_one(
                        {"_id": w["_id"]}, {"$inc": {"view_count": 1}}
                    )
                except Exception as e:
                    logger.warning(f"Error sending wallpaper in browse: {e}")
            await track_search(tag, category=tag)
        else:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"No wallpapers found for: {tag}",
            )
        return

    # --- Delete confirmation ---
    if data.startswith("delconfirm_"):
        if not is_owner(user_id):
            await query.answer("⛔ Owner only.", show_alert=True)
            return
        wall_id_str = data[len("delconfirm_"):]
        try:
            result = await wallpapers_collection.delete_one({"_id": ObjectId(wall_id_str)})
            if result.deleted_count:
                await query.edit_message_text(
                    f"✅ Wallpaper deleted.\n\nID: <code>{wall_id_str}</code>",
                    parse_mode="HTML",
                )
            else:
                await query.edit_message_text("No wallpaper found with that ID.")
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
        return

    if data == "delcancel":
        await query.edit_message_text("❌ Deletion cancelled.")
        return

# ===========================================================================
# ECHO — plain text treated as search
# ===========================================================================

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate_check(update, context):
        return

    user_id = update.effective_user.id
    query   = update.message.text.lower().strip()
    if not query:
        return

    if not await check_search_limit(user_id):
        limit = await get_setting("search_limit", 0)
        await update.message.reply_text(f"⚠️ Daily search limit reached ({limit} searches/day).")
        return

    wallpapers = await wallpapers_collection.find(
        {"tags": {"$regex": query, "$options": "i"}}
    ).to_list(length=50)

    if wallpapers:
        await update.message.reply_text(f"Found {len(wallpapers)} wallpapers for '{query}':")
        for w in wallpapers:
            try:
                await update.message.reply_document(w["file_id"])
                await wallpapers_collection.update_one({"_id": w["_id"]}, {"$inc": {"view_count": 1}})
            except Exception as e:
                logger.warning(f"Echo search send error: {e}")
        await increment_search_count(user_id)
        await track_search(query)
    else:
        await update.message.reply_text(f"No wallpapers found for '{query}'.")

# ===========================================================================
# SCHEDULER — daily wallpaper
# ===========================================================================

async def send_daily_wallpaper(bot) -> None:
    result = await wallpapers_collection.aggregate([{"$sample": {"size": 1}}]).to_list(length=1)
    if not result:
        logger.info("No wallpapers available for daily send.")
        return

    wallpaper = result[0]
    async for user in users_collection.find({"daily_subscribed": True}):
        try:
            await bot.send_document(chat_id=user["_id"], document=wallpaper["file_id"])
            await wallpapers_collection.update_one(
                {"_id": wallpaper["_id"]}, {"$inc": {"view_count": 1}}
            )
        except Exception as e:
            logger.warning(f"Daily send failed for {user['_id']}: {e}")

# ===========================================================================
# MAIN
# ===========================================================================

def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()

    # Public commands
    application.add_handler(CommandHandler("start",    start))
    application.add_handler(CommandHandler("help",     help_command))
    application.add_handler(CommandHandler("search",   search_command))
    application.add_handler(CommandHandler("browse",   browse_command))
    application.add_handler(CommandHandler("random",   random_command))
    application.add_handler(CommandHandler("trending", trending_command))
    application.add_handler(CommandHandler("request",  request_command))
    application.add_handler(CommandHandler("daily",    daily_command))
    application.add_handler(CommandHandler("stopdaily",stop_daily_command))

    # Owner commands
    application.add_handler(CommandHandler("stats",          stats_command))
    application.add_handler(CommandHandler("broadcast",      broadcast_command))
    application.add_handler(CommandHandler("list",           list_wallpapers_command))
    application.add_handler(CommandHandler("find",           find_command))
    application.add_handler(CommandHandler("delete",         delete_wallpaper_command))
    application.add_handler(CommandHandler("addcategory",    add_category_command))
    application.add_handler(CommandHandler("listcategories", list_categories_command))
    application.add_handler(CommandHandler("renamecategory", rename_category_command))
    application.add_handler(CommandHandler("deletecategory", delete_category_command))
    application.add_handler(CommandHandler("addadmin",       add_admin_command))
    application.add_handler(CommandHandler("removeadmin",    remove_admin_command))
    application.add_handler(CommandHandler("admins",         list_admins_command))
    application.add_handler(CommandHandler("lock",           lock_command))
    application.add_handler(CommandHandler("unlimited",      unlimited_command))
    application.add_handler(CommandHandler("allow",          allow_command))
    application.add_handler(CommandHandler("ban",            ban_command))
    application.add_handler(CommandHandler("unban",          unban_command))
    application.add_handler(CommandHandler("banned",         list_banned_command))
    application.add_handler(CommandHandler("addchannel",     add_channel_command))
    application.add_handler(CommandHandler("removechannel",  remove_channel_command))
    application.add_handler(CommandHandler("channels",       list_channels_command))
    application.add_handler(CommandHandler("users",          users_command))
    application.add_handler(CommandHandler("usage",          usage_command))

    # Upload conversation
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("upload", upload_command)],
        states={
            UPLOAD_PHOTO: [MessageHandler(filters.PHOTO | filters.Document.IMAGE, upload_photo)],
            UPLOAD_TAGS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, upload_tags)],
        },
        fallbacks=[CommandHandler("cancel", cancel_upload)],
    ))

    # Inline keyboard callbacks
    application.add_handler(CallbackQueryHandler(button_callback_handler))

    # Echo (plain-text search)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    # APScheduler — daily wallpaper at 12:00 PM IST
    scheduler = AsyncIOScheduler(timezone=timezone("Asia/Kolkata"))
    scheduler.add_job(
        lambda: application.create_task(send_daily_wallpaper(application.bot)),
        "cron", hour=12, minute=0, id="daily_wallpaper",
    )

    async def post_init(app: Application) -> None:
        await setup_indexes()
        scheduler.start()
        logger.info("Bot started. Indexes created. Scheduler running.")

    application.post_init = post_init
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
