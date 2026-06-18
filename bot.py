```python
#!/usr/bin/env python3

import os
import logging
import motor.motor_asyncio
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pytz import timezone
from dotenv import load_dotenv
from bson import ObjectId
from bson.errors import InvalidId

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, 
    ConversationHandler, CallbackQueryHandler, ContextTypes
)
from telegram.error import TelegramError

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_TELEGRAM_ID = int(os.getenv("OWNER_TELEGRAM_ID", 0))
MONGODB_CONNECTION_STRING = os.getenv("MONGODB_CONNECTION_STRING")

# MongoDB connection
client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_CONNECTION_STRING)
db = client.wallpaper_bot

wallpapers_collection = db.wallpapers
users_collection = db.users
requests_collection = db.requests
admins_collection = db.admins
categories_collection = db.categories
channels_collection = db.channels
banned_collection = db.banned
settings_collection = db.settings
usage_collection = db.usage

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Conversation states for upload
UPLOAD_PHOTO, UPLOAD_TAGS = range(2)

# --- Helper Functions ---
async def is_owner(user_id: int) -> bool:
    return user_id == OWNER_TELEGRAM_ID

async def is_admin(user_id: int) -> bool:
    if await is_owner(user_id):
        return True
    admin = await admins_collection.find_one({"_id": user_id})
    return bool(admin)

async def setup_db():
    # Ensure basic settings document exists
    if not await settings_collection.find_one({"_id": "bot_settings"}):
        await settings_collection.insert_one({"_id": "bot_settings", "mode": "unlimited", "limit": 0})

async def log_search(user_id: int, query: str, search_type: str = "search"):
    await usage_collection.insert_one({
        "user_id": user_id,
        "query": query.lower(),
        "type": search_type,
        "timestamp": datetime.utcnow()
    })

async def verify_access(update: Update, context: ContextTypes.DEFAULT_TYPE, check_limit: bool = False) -> bool:
    user_id = update.effective_user.id
    message = update.message if update.message else update.callback_query.message

    if await is_admin(user_id):
        return True

    # 1. Ban Check
    if await banned_collection.find_one({"_id": user_id}):
        await message.reply_text("🚫 You are banned from using this bot.")
        return False

    # 2. Force Join Check
    channels = await channels_collection.find().to_list(length=None)
    unjoined_channels = []
    for channel in channels:
        try:
            member = await context.bot.get_chat_member(chat_id=channel["_id"], user_id=user_id)
            if member.status in ['left', 'kicked']:
                unjoined_channels.append(channel["_id"])
        except TelegramError:
            unjoined_channels.append(channel["_id"])

    if unjoined_channels:
        keyboard = []
        for ch in unjoined_channels:
            url = f"https://t.me/{ch.replace('@', '')}"
            keyboard.append([InlineKeyboardButton("📢 Join Channel", url=url)])
        keyboard.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_join")])
        
        await message.reply_text(
            "🔒 To use this bot you must join our channel.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return False

    # 3. Subscription/Limit Check
    if check_limit:
        settings = await settings_collection.find_one({"_id": "bot_settings"}) or {"mode": "unlimited"}
        mode = settings.get("mode", "unlimited")
        
        if mode == "locked":
            kb = []
            if channels:
                kb.append([InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{channels[0]['_id'].replace('@', '')}")])
            await message.reply_text(
                "🔒 The bot is currently locked by the owner.\nJoin our channel for updates.",
                reply_markup=InlineKeyboardMarkup(kb) if kb else None
            )
            return False
        
        elif mode == "allow":
            limit = settings.get("limit", 0)
            today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            usage_count = await usage_collection.count_documents({
                "user_id": user_id,
                "timestamp": {"$gte": today}
            })
            if usage_count >= limit:
                kb = []
                if channels:
                    kb.append([InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{channels[0]['_id'].replace('@', '')}")])
                await message.reply_text(
                    "⚠️ Daily search limit reached.",
                    reply_markup=InlineKeyboardMarkup(kb) if kb else None
                )
                return False

    return True

# --- Commands ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not await verify_access(update, context, check_limit=False):
        return

    now = datetime.utcnow()
    await users_collection.update_one(
        {"_id": user.id},
        {
            "$set": {
                "username": user.username or user.first_name, 
                "first_name": user.first_name, 
                "last_name": user.last_name,
                "last_active": now
            },
            "$setOnInsert": {"join_date": now, "daily_subscribed": False}
        },
        upsert=True
    )
    await update.message.reply_html(
        f"Hi {user.mention_html()}! Welcome to the Wallpaper Bot.\n"
        "I can help you find and manage wallpapers.\n"
        "Type /help to see all available commands."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not await verify_access(update, context, check_limit=False):
        return

    help_text = (
        "USER COMMANDS:\n"
        "/start - Welcome message\n"
        "/help - Show all commands\n"
        "/search <name> - Search wallpapers (e.g., /search naruto)\n"
        "/browse - Show category buttons\n"
        "/random - Get a random wallpaper\n"
        "/random <tag> - Get random wallpaper with a tag (e.g., /random naruto)\n"
        "/trending - Show the most popular wallpapers\n"
        "/request <desc> - Request a wallpaper\n"
        "/daily - Subscribe to daily wallpaper\n"
        "/stopdaily - Unsubscribe from daily\n"
    )

    if await is_admin(user_id):
        help_text += (
            "\nADMIN COMMANDS:\n"
            "/upload - Upload wallpaper\n"
            "/broadcast <msg> - Send to all users\n"
            "/list - Latest 20 wallpapers\n"
            "/find <tag> - Find by tag\n"
            "/delete <id> - Delete wallpaper\n"
            "/addcategory <Name>|<tag>\n"
            "/listcategories - List all\n"
            "/renamecategory <Old>|<New>\n"
            "/deletecategory <Name>\n"
            "/users - User statistics\n"
            "/usage - Bot usage stats\n"
        )
    
    if await is_owner(user_id):
        help_text += (
            "\nOWNER COMMANDS:\n"
            "/stats - Show bot statistics\n"
            "/subscription - View current settings\n"
            "/lock - Lock bot\n"
            "/unlimited - Remove limits\n"
            "/allow <number> - Set daily limit\n"
            "/addchannel @channel - Add force join\n"
            "/removechannel @channel - Remove force join\n"
            "/channels - List required channels\n"
            "/addadmin <id> - Add admin\n"
            "/removeadmin <id> - Remove admin\n"
            "/admins - List admins\n"
            "/ban <id> - Ban user\n"
            "/unban <id> - Unban user\n"
            "/banned - List banned users\n"
        )

    await update.message.reply_text(help_text)

# --- Normal User Features ---

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await verify_access(update, context, check_limit=True):
        return

    query = " ".join(context.args).lower()
    if not query:
        await update.message.reply_text("Please provide a search term. Example: /search naruto")
        return

    await log_search(update.effective_user.id, query, "search")

    matching = wallpapers_collection.find({"tags": {"$regex": query, "$options": "i"}})
    found = await matching.to_list(length=10) # Limiting to 10 to avoid flood

    if found:
        await update.message.reply_text(f"Found {len(found)} wallpapers for '{query}':")
        for wp in found:
            try:
                await update.message.reply_document(wp["file_id"])
                await wallpapers_collection.update_one({"_id": wp["_id"]}, {"$inc": {"view_count": 1}})
            except TelegramError:
                pass
    else:
        await update.message.reply_text(f"No wallpapers found for '{query}'.")

async def browse_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await verify_access(update, context, check_limit=True):
        return

    cats = await categories_collection.find().to_list(length=None)
    if not cats:
        await update.message.reply_text("No categories available.")
        return

    keyboard = []
    for cat in cats:
        # Pass the category ObjectId string in callback
        cat_id = str(cat["_id"])
        keyboard.append([InlineKeyboardButton(cat["name"], callback_data=f"cat_{cat_id}")])
    
    await update.message.reply_text("Please choose a category:", reply_markup=InlineKeyboardMarkup(keyboard))

async def random_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await verify_access(update, context, check_limit=True):
        return

    query_tag = " ".join(context.args).lower()
    pipeline = []
    if query_tag:
        pipeline.append({"$match": {"tags": {"$regex": query_tag, "$options": "i"}}})
        await log_search(update.effective_user.id, query_tag, "search")
    
    pipeline.append({"$sample": {"size": 1}})
    random_wp = await wallpapers_collection.aggregate(pipeline).to_list(length=1)

    if random_wp:
        wp = random_wp[0]
        try:
            await update.message.reply_document(wp["file_id"])
            await wallpapers_collection.update_one({"_id": wp["_id"]}, {"$inc": {"view_count": 1}})
        except TelegramError:
            await update.message.reply_text("Error sending document.")
    else:
        if query_tag:
            await update.message.reply_text(f"No random wallpapers found for tag: {query_tag}.")
        else:
            await update.message.reply_text("No wallpapers available yet.")

async def trending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await verify_access(update, context, check_limit=True):
        return

    trending = wallpapers_collection.find({}).sort("view_count", -1).limit(10)
    found = await trending.to_list(length=10)

    if found:
        await update.message.reply_text("Here are the top trending wallpapers:")
        for wp in found:
            try:
                await update.message.reply_document(wp["file_id"])
                await wallpapers_collection.update_one({"_id": wp["_id"]}, {"$inc": {"view_count": 1}})
            except TelegramError:
                pass
    else:
        await update.message.reply_text("No wallpapers available yet.")

async def request_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await verify_access(update, context, check_limit=False):
        return

    request_text = " ".join(context.args)
    if not request_text:
        await update.message.reply_text("Provide a description. Example: /request gojo purple wallpaper")
        return

    user = update.effective_user
    await requests_collection.insert_one({
        "user_id": user.id,
        "username": user.username or user.first_name,
        "request_text": request_text,
        "timestamp": datetime.utcnow()
    })
    await update.message.reply_text("Your request has been submitted!")
    owner_msg = f"New wallpaper request from @{user.username or user.first_name} (ID: {user.id}):\n'{request_text}'"
    try:
        await context.bot.send_message(chat_id=OWNER_TELEGRAM_ID, text=owner_msg)
    except TelegramError:
        pass

async def daily_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await verify_access(update, context, check_limit=False):
        return
    await users_collection.update_one(
        {"_id": update.effective_user.id},
        {"$set": {"daily_subscribed": True}},
        upsert=True
    )
    await update.message.reply_text("Subscribed to daily wallpapers!")

async def stop_daily_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await verify_access(update, context, check_limit=False):
        return
    await users_collection.update_one(
        {"_id": update.effective_user.id},
        {"$set": {"daily_subscribed": False}}
    )
    await update.message.reply_text("Unsubscribed from daily wallpapers.")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text: return
    if not await verify_access(update, context, check_limit=True):
        return

    query = update.message.text.lower()
    await log_search(update.effective_user.id, query, "search")

    matching = wallpapers_collection.find({"tags": {"$regex": query, "$options": "i"}})
    found = await matching.to_list(length=10)

    if found:
        await update.message.reply_text(f"Found {len(found)} wallpapers for '{query}':")
        for wp in found:
            try:
                await update.message.reply_document(wp["file_id"])
                await wallpapers_collection.update_one({"_id": wp["_id"]}, {"$inc": {"view_count": 1}})
            except TelegramError:
                pass
    else:
        await update.message.reply_text(f"No wallpapers found for '{query}'.")

# --- Admin & Owner Commands ---

async def upload_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("You are not authorized.")
        return ConversationHandler.END
    await update.message.reply_text("Please send the wallpaper image you want to upload.")
    return UPLOAD_PHOTO

async def upload_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document and update.message.document.mime_type.startswith('image'):
        file_id = update.message.document.file_id
    else:
        await update.message.reply_text("Please send an image file.")
        return UPLOAD_PHOTO

    context.user_data["file_id"] = file_id
    await update.message.reply_text("Image received! Send tags separated by spaces (e.g., naruto sasuke).")
    return UPLOAD_TAGS

async def upload_tags(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tags = [t.strip().lower() for t in update.message.text.split() if t.strip()]
    file_id = context.user_data.get("file_id")

    if not tags:
        await update.message.reply_text("No tags provided. Try again.")
        return UPLOAD_TAGS

    wp = {
        "file_id": file_id,
        "tags": tags,
        "view_count": 0,
        "upload_date": datetime.utcnow()
    }
    result = await wallpapers_collection.insert_one(wp)
    
    await update.message.reply_text(
        f"✅ Wallpaper uploaded\n\nWallpaper ID:\n`{str(result.inserted_id)}`\n\nTags:\n{' '.join(tags)}",
        parse_mode="Markdown"
    )
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Upload cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update.effective_user.id):
        return
    
    msg = " ".join(context.args)
    if not msg:
        await update.message.reply_text("Usage: /broadcast <message>")
        return

    status_msg = await update.message.reply_text("Broadcasting... Please wait.")
    users = await users_collection.find({}).to_list(length=None)
    success = 0
    failed = 0

    for u in users:
        try:
            await context.bot.send_message(chat_id=u["_id"], text=msg)
            success += 1
        except TelegramError:
            failed += 1

    await status_msg.edit_text(f"Broadcast complete!\nSuccess: {success}\nFailed: {failed}")

async def list_wallpapers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update.effective_user.id): return
    wps = await wallpapers_collection.find().sort("upload_date", -1).limit(20).to_list(length=20)
    
    if not wps:
        await update.message.reply_text("No wallpapers found.")
        return

    text = "Latest 20 Wallpapers:\n\n"
    for w in wps:
        text += f"ID: `{str(w['_id'])}`\nTags: {' '.join(w['tags'])}\n\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def find_wallpaper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update.effective_user.id): return
    tag = " ".join(context.args).lower()
    if not tag:
        await update.message.reply_text("Usage: /find <tag>")
        return

    wps = await wallpapers_collection.find({"tags": {"$regex": tag, "$options": "i"}}).to_list(length=10)
    if not wps:
        await update.message.reply_text("No results found.")
        return
    
    for w in wps:
        await update.message.reply_document(
            w["file_id"], 
            caption=f"Wallpaper ID: `{str(w['_id'])}`\nTags: {' '.join(w['tags'])}", 
            parse_mode="Markdown"
        )

async def delete_wallpaper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Usage: /delete <ID>")
        return
    
    wid = context.args[0]
    try:
        obj_id = ObjectId(wid)
    except InvalidId:
        await update.message.reply_text("Invalid Wallpaper ID format.")
        return

    wp = await wallpapers_collection.find_one({"_id": obj_id})
    if not wp:
        await update.message.reply_text("Wallpaper not found.")
        return

    kb = [
        [
            InlineKeyboardButton("✅ Confirm", callback_data=f"del_{wid}"),
            InlineKeyboardButton("❌ Cancel", callback_data="delcancel")
        ]
    ]
    await update.message.reply_document(
        wp["file_id"],
        caption=f"Are you sure you want to delete this wallpaper?\nTags: {' '.join(wp['tags'])}",
        reply_markup=InlineKeyboardMarkup(kb)
    )

# --- Category Management ---

async def add_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update.effective_user.id): return
    text = " ".join(context.args)
    if "|" not in text:
        await update.message.reply_text("Usage: /addcategory Name|tag")
        return
    name, tag = text.split("|", 1)
    name, tag = name.strip(), tag.strip().lower()

    if await categories_collection.find_one({"name": {"$regex": f"^{name}$", "$options": "i"}}):
        await update.message.reply_text("Category already exists.")
        return

    await categories_collection.insert_one({"name": name, "tag": tag})
    await update.message.reply_text(f"Added category: {name} -> {tag}")

a
