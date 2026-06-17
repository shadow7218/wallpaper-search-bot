#!/usr/bin/env python3

import os
import logging
from datetime import datetime
from bson import ObjectId

import motor.motor_asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pytz import timezone

# Load environment variables from Railway dashboard (or any host env)
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_TELEGRAM_ID = int(os.environ["OWNER_TELEGRAM_ID"])
MONGODB_CONNECTION_STRING = os.environ["MONGODB_CONNECTION_STRING"]

# MongoDB connection
client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_CONNECTION_STRING)
db = client.wallpaper_bot

wallpapers_collection = db.wallpapers
users_collection = db.users
requests_collection = db.requests
categories_collection = db.categories  # NEW: dynamic categories

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    CallbackQueryHandler,
)

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Conversation states for upload
UPLOAD_PHOTO, UPLOAD_TAGS = range(2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def is_owner(user_id: int) -> bool:
    return user_id == OWNER_TELEGRAM_ID


async def get_user_stats():
    total_users = await users_collection.count_documents({})
    subscribed_users = await users_collection.count_documents({"daily_subscribed": True})
    return total_users, subscribed_users


async def get_wallpaper_stats():
    return await wallpapers_collection.count_documents({})


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def start(update: Update, context) -> None:
    user = update.effective_user
    await users_collection.update_one(
        {"_id": user.id},
        {
            "$set": {
                "username": user.username or user.first_name,
                "first_name": user.first_name,
                "last_name": user.last_name,
            }
        },
        upsert=True,
    )
    await update.message.reply_html(
        f"Hi {user.mention_html()}! Welcome to the Wallpaper Bot. "
        "I can help you find and manage wallpapers. "
        "Type /help to see all available commands."
    )


# ---------------------------------------------------------------------------
# /help  — owner commands hidden from regular users
# ---------------------------------------------------------------------------

async def help_command(update: Update, context) -> None:
    help_text = (
        "Here are the commands you can use:\n\n"
        "/start - Welcome message\n"
        "/help - Show all commands\n"
        "/search <name> - Search wallpapers (e.g., /search naruto)\n"
        "/browse - Show category buttons\n"
        "/random - Get a random wallpaper\n"
        "/random <tag> - Get a random wallpaper with a specific tag\n"
        "/trending - Show the most popular wallpapers\n"
        "/request <description> - Request a wallpaper\n"
        "/daily - Subscribe to daily wallpaper\n"
        "/stopdaily - Unsubscribe from daily\n"
    )

    if await is_owner(update.effective_user.id):
        help_text += (
            "\n🔧 Owner Commands:\n"
            "/upload - Start wallpaper upload mode\n"
            "/stats - Show bot statistics\n"
            "/addcategory Name|tag - Add a new category\n"
            "/listcategories - List all categories\n"
            "/renamecategory Old Name|New Name - Rename a category\n"
            "/deletecategory Name - Delete a category\n"
            "/broadcast <message> - Send a message to all users\n"
            "/list - Show 20 most recent wallpapers\n"
            "/delete <id> - Delete a wallpaper by ID\n"
        )

    await update.message.reply_text(help_text)


# ---------------------------------------------------------------------------
# /upload (owner only) — unchanged logic, kept intact
# ---------------------------------------------------------------------------

async def upload_command(update: Update, context) -> int:
    if not await is_owner(update.effective_user.id):
        await update.message.reply_text("You are not authorized to upload wallpapers.")
        return ConversationHandler.END

    await update.message.reply_text("Please send the wallpaper image you want to upload.")
    return UPLOAD_PHOTO


async def upload_photo(update: Update, context) -> int:
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document and update.message.document.mime_type.startswith("image"):
        file_id = update.message.document.file_id
    else:
        await update.message.reply_text("That doesn't look like an image. Please send an image file.")
        return UPLOAD_PHOTO

    context.user_data["file_id"] = file_id
    await update.message.reply_text(
        "Image received! Now, please send the tags for this wallpaper, "
        "separated by spaces (e.g., naruto sasuke fight)."
    )
    return UPLOAD_TAGS


async def upload_tags(update: Update, context) -> int:
    tags = [tag.strip().lower() for tag in update.message.text.split() if tag.strip()]
    file_id = context.user_data["file_id"]

    if not tags:
        await update.message.reply_text("No tags provided. Please try again with some tags.")
        return UPLOAD_TAGS

    wallpaper = {
        "file_id": file_id,
        "tags": tags,
        "view_count": 0,
        "upload_date": datetime.utcnow(),
    }
    await wallpapers_collection.insert_one(wallpaper)
    await update.message.reply_text(f"Wallpaper uploaded successfully with tags: {', '.join(tags)}!")
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_upload(update: Update, context) -> int:
    await update.message.reply_text("Upload cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /search
# ---------------------------------------------------------------------------

async def search_command(update: Update, context) -> None:
    query = " ".join(context.args).lower()
    if not query:
        await update.message.reply_text("Please provide a search term. Example: /search naruto")
        return

    matching_wallpapers = wallpapers_collection.find({"tags": {"$regex": query, "$options": "i"}})
    found_wallpapers = []
    async for wallpaper in matching_wallpapers:
        found_wallpapers.append(wallpaper)

    if found_wallpapers:
        await update.message.reply_text(f"Found {len(found_wallpapers)} wallpapers for '{query}':")
        for wallpaper in found_wallpapers:
            await update.message.reply_document(wallpaper["file_id"])
            await wallpapers_collection.update_one(
                {"_id": wallpaper["_id"]}, {"$inc": {"view_count": 1}}
            )
    else:
        await update.message.reply_text(f"No wallpapers found for '{query}'.")


# ---------------------------------------------------------------------------
# /browse — dynamic categories from MongoDB
# ---------------------------------------------------------------------------

async def browse_command(update: Update, context) -> None:
    categories = []
    async for cat in categories_collection.find({}).sort("name", 1):
        categories.append(cat)

    if not categories:
        await update.message.reply_text(
            "No categories available yet. The owner can add some with /addcategory."
        )
        return

    # Button shows the display name; callback carries the tag
    keyboard = [
        [InlineKeyboardButton(cat["name"], callback_data=f"cattag_{cat['tag']}")]
        for cat in categories
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Please choose a category:", reply_markup=reply_markup)


# ---------------------------------------------------------------------------
# Callback handler — handles category tag buttons
# ---------------------------------------------------------------------------

async def button_callback_handler(update: Update, context) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data

    # NEW format: cattag_<tag>
    if data.startswith("cattag_"):
        tag = data[len("cattag_"):]
        await query.edit_message_text(f"Showing wallpapers for: {tag}")

        matching_wallpapers = wallpapers_collection.find({"tags": {"$regex": tag, "$options": "i"}})
        found_wallpapers = []
        async for wallpaper in matching_wallpapers:
            found_wallpapers.append(wallpaper)

        if found_wallpapers:
            for wallpaper in found_wallpapers:
                await context.bot.send_document(
                    chat_id=query.message.chat_id, document=wallpaper["file_id"]
                )
                await wallpapers_collection.update_one(
                    {"_id": wallpaper["_id"]}, {"$inc": {"view_count": 1}}
                )
        else:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"No wallpapers found for tag: {tag}.",
            )

    # Legacy format fallback (category_<name>) — kept for backward compatibility
    elif data.startswith("category_"):
        category = data.replace("category_", "").replace("_", " ")
        await query.edit_message_text(f"Showing wallpapers for category: {category.title()}")

        matching_wallpapers = wallpapers_collection.find({"tags": {"$regex": category, "$options": "i"}})
        found_wallpapers = []
        async for wallpaper in matching_wallpapers:
            found_wallpapers.append(wallpaper)

        if found_wallpapers:
            for wallpaper in found_wallpapers:
                await context.bot.send_document(
                    chat_id=query.message.chat_id, document=wallpaper["file_id"]
                )
                await wallpapers_collection.update_one(
                    {"_id": wallpaper["_id"]}, {"$inc": {"view_count": 1}}
                )
        else:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"No wallpapers found for category: {category.title()}.",
            )


# ---------------------------------------------------------------------------
# /random
# ---------------------------------------------------------------------------

async def random_command(update: Update, context) -> None:
    query_tag = " ".join(context.args).lower()

    pipeline = []
    if query_tag:
        pipeline.append({"$match": {"tags": {"$regex": query_tag, "$options": "i"}}})
    pipeline.append({"$sample": {"size": 1}})

    random_wallpaper = await wallpapers_collection.aggregate(pipeline).to_list(length=1)

    if random_wallpaper:
        wallpaper = random_wallpaper[0]
        await update.message.reply_document(wallpaper["file_id"])
        await wallpapers_collection.update_one(
            {"_id": wallpaper["_id"]}, {"$inc": {"view_count": 1}}
        )
    else:
        if query_tag:
            await update.message.reply_text(f"No random wallpapers found for tag: {query_tag}.")
        else:
            await update.message.reply_text("No wallpapers available yet.")


# ---------------------------------------------------------------------------
# /trending
# ---------------------------------------------------------------------------

async def trending_command(update: Update, context) -> None:
    trending_wallpapers = wallpapers_collection.find({}).sort("view_count", -1).limit(10)

    found_wallpapers = []
    async for wallpaper in trending_wallpapers:
        found_wallpapers.append(wallpaper)

    if found_wallpapers:
        await update.message.reply_text("Here are the top 10 trending wallpapers:")
        for wallpaper in found_wallpapers:
            await update.message.reply_document(wallpaper["file_id"])
            await wallpapers_collection.update_one(
                {"_id": wallpaper["_id"]}, {"$inc": {"view_count": 1}}
            )
    else:
        await update.message.reply_text("No wallpapers available to determine trending ones yet.")


# ---------------------------------------------------------------------------
# /request
# ---------------------------------------------------------------------------

async def request_command(update: Update, context) -> None:
    request_text = " ".join(context.args)
    if not request_text:
        await update.message.reply_text(
            "Please provide a description. Example: /request gojo purple wallpaper"
        )
        return

    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name

    request_doc = {
        "user_id": user_id,
        "username": username,
        "request_text": request_text,
        "timestamp": datetime.utcnow(),
    }
    await requests_collection.insert_one(request_doc)
    await update.message.reply_text("Your request has been submitted! The owner will be notified.")

    owner_message = (
        f"New wallpaper request from @{username} (ID: {user_id}):\n'{request_text}'"
    )
    await context.bot.send_message(chat_id=OWNER_TELEGRAM_ID, text=owner_message)


# ---------------------------------------------------------------------------
# /daily  /stopdaily
# ---------------------------------------------------------------------------

async def daily_command(update: Update, context) -> None:
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name

    await users_collection.update_one(
        {"_id": user_id},
        {"$set": {"daily_subscribed": True, "username": username}},
        upsert=True,
    )
    await update.message.reply_text(
        "You have subscribed to daily wallpapers! "
        "You will receive a random wallpaper every day at 12:00 PM IST."
    )


async def stop_daily_command(update: Update, context) -> None:
    user_id = update.effective_user.id
    await users_collection.update_one(
        {"_id": user_id}, {"$set": {"daily_subscribed": False}}
    )
    await update.message.reply_text(
        "You have unsubscribed from daily wallpapers. You will no longer receive daily updates."
    )


# ---------------------------------------------------------------------------
# /stats (owner only)
# ---------------------------------------------------------------------------

async def stats_command(update: Update, context) -> None:
    if not await is_owner(update.effective_user.id):
        await update.message.reply_text("You are not authorized to view statistics.")
        return

    total_users, subscribed_users = await get_user_stats()
    total_wallpapers = await get_wallpaper_stats()

    stats_text = (
        f"📊 Bot Statistics 📊\n\n"
        f"Total Users: {total_users}\n"
        f"Subscribed to Daily: {subscribed_users}\n"
        f"Total Wallpapers: {total_wallpapers}\n"
    )
    await update.message.reply_text(stats_text)


# ---------------------------------------------------------------------------
# /addcategory (owner only)  — /addcategory Name|tag
# ---------------------------------------------------------------------------

async def add_category_command(update: Update, context) -> None:
    if not await is_owner(update.effective_user.id):
        await update.message.reply_text("You are not authorized to use this command.")
        return

    raw = " ".join(context.args)
    if "|" not in raw:
        await update.message.reply_text(
            "Invalid format. Use:\n/addcategory Category Name|tag\n\nExample:\n/addcategory One Punch Man|onepunchman"
        )
        return

    name, tag = raw.split("|", 1)
    name = name.strip()
    tag = tag.strip().lower()

    if not name or not tag:
        await update.message.reply_text("Both name and tag are required.")
        return

    existing = await categories_collection.find_one({"name": name})
    if existing:
        await update.message.reply_text(f"A category named '{name}' already exists.")
        return

    await categories_collection.insert_one({"name": name, "tag": tag})
    await update.message.reply_text(f"✅ Category added:\nName: {name}\nTag: {tag}")


# ---------------------------------------------------------------------------
# /listcategories (owner only)
# ---------------------------------------------------------------------------

async def list_categories_command(update: Update, context) -> None:
    if not await is_owner(update.effective_user.id):
        await update.message.reply_text("You are not authorized to use this command.")
        return

    categories = []
    async for cat in categories_collection.find({}).sort("name", 1):
        categories.append(cat)

    if not categories:
        await update.message.reply_text("No categories found.")
        return

    lines = [cat["name"] for cat in categories]
    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# /renamecategory (owner only)  — /renamecategory Old Name|New Name
# ---------------------------------------------------------------------------

async def rename_category_command(update: Update, context) -> None:
    if not await is_owner(update.effective_user.id):
        await update.message.reply_text("You are not authorized to use this command.")
        return

    raw = " ".join(context.args)
    if "|" not in raw:
        await update.message.reply_text(
            "Invalid format. Use:\n/renamecategory Old Name|New Name\n\nExample:\n/renamecategory One Punch Man|One Punch"
        )
        return

    old_name, new_name = raw.split("|", 1)
    old_name = old_name.strip()
    new_name = new_name.strip()

    if not old_name or not new_name:
        await update.message.reply_text("Both old name and new name are required.")
        return

    existing = await categories_collection.find_one({"name": old_name})
    if not existing:
        await update.message.reply_text(f"No category found with name: '{old_name}'")
        return

    await categories_collection.update_one(
        {"name": old_name}, {"$set": {"name": new_name}}
    )
    await update.message.reply_text(
        f"✅ Category renamed:\nOld: {old_name}\nNew: {new_name}\nTag unchanged: {existing['tag']}"
    )


# ---------------------------------------------------------------------------
# /deletecategory (owner only)  — /deletecategory Category Name
# ---------------------------------------------------------------------------

async def delete_category_command(update: Update, context) -> None:
    if not await is_owner(update.effective_user.id):
        await update.message.reply_text("You are not authorized to use this command.")
        return

    name = " ".join(context.args).strip()
    if not name:
        await update.message.reply_text(
            "Please provide the category name. Example:\n/deletecategory One Punch"
        )
        return

    result = await categories_collection.delete_one({"name": name})
    if result.deleted_count:
        await update.message.reply_text(f"✅ Category '{name}' deleted. Wallpapers are not affected.")
    else:
        await update.message.reply_text(f"No category found with name: '{name}'")


# ---------------------------------------------------------------------------
# /broadcast (owner only)  — /broadcast message
# ---------------------------------------------------------------------------

async def broadcast_command(update: Update, context) -> None:
    if not await is_owner(update.effective_user.id):
        await update.message.reply_text("You are not authorized to use this command.")
        return

    message_text = " ".join(context.args).strip()
    if not message_text:
        await update.message.reply_text(
            "Please provide a message to broadcast. Example:\n/broadcast New wallpapers uploaded 🔥"
        )
        return

    sent = 0
    failed = 0
    async for user in users_collection.find({}):
        try:
            await context.bot.send_message(chat_id=user["_id"], text=message_text)
            sent += 1
        except Exception as e:
            logger.warning(f"Broadcast failed for user {user['_id']}: {e}")
            failed += 1

    await update.message.reply_text(
        f"📢 Broadcast complete.\n✅ Sent: {sent}\n❌ Failed: {failed}"
    )


# ---------------------------------------------------------------------------
# /list (owner only) — 20 most recent wallpapers
# ---------------------------------------------------------------------------

async def list_wallpapers_command(update: Update, context) -> None:
    if not await is_owner(update.effective_user.id):
        await update.message.reply_text("You are not authorized to use this command.")
        return

    recent = wallpapers_collection.find({}).sort("upload_date", -1).limit(20)
    wallpapers = []
    async for w in recent:
        wallpapers.append(w)

    if not wallpapers:
        await update.message.reply_text("No wallpapers found in the database.")
        return

    lines = []
    for w in wallpapers:
        tags_str = " ".join(w.get("tags", []))
        lines.append(f"ID: {w['_id']}\nTags: {tags_str}\n")

    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# /delete <id> (owner only)
# ---------------------------------------------------------------------------

async def delete_wallpaper_command(update: Update, context) -> None:
    if not await is_owner(update.effective_user.id):
        await update.message.reply_text("You are not authorized to use this command.")
        return

    if not context.args:
        await update.message.reply_text(
            "Please provide a wallpaper ID. Example:\n/delete 6875a8c..."
        )
        return

    wallpaper_id_str = context.args[0].strip()
    try:
        wallpaper_oid = ObjectId(wallpaper_id_str)
    except Exception:
        await update.message.reply_text("Invalid wallpaper ID format.")
        return

    result = await wallpapers_collection.delete_one({"_id": wallpaper_oid})
    if result.deleted_count:
        await update.message.reply_text(f"✅ Wallpaper {wallpaper_id_str} deleted successfully.")
    else:
        await update.message.reply_text(f"No wallpaper found with ID: {wallpaper_id_str}")


# ---------------------------------------------------------------------------
# Daily wallpaper scheduler job
# ---------------------------------------------------------------------------

async def send_daily_wallpaper(context) -> None:
    subscribed_users = users_collection.find({"daily_subscribed": True})

    pipeline = [{"$sample": {"size": 1}}]
    random_wallpaper = await wallpapers_collection.aggregate(pipeline).to_list(length=1)

    if random_wallpaper:
        wallpaper = random_wallpaper[0]
        async for user in subscribed_users:
            try:
                await context.bot.send_document(
                    chat_id=user["_id"], document=wallpaper["file_id"]
                )
                await wallpapers_collection.update_one(
                    {"_id": wallpaper["_id"]}, {"$inc": {"view_count": 1}}
                )
            except Exception as e:
                logger.error(f"Could not send daily wallpaper to user {user['_id']}: {e}")
    else:
        logger.info("No wallpapers available to send daily.")


# ---------------------------------------------------------------------------
# Echo handler — non-command text treated as search
# ---------------------------------------------------------------------------

async def echo(update: Update, context) -> None:
    query = update.message.text.lower()
    if not query:
        return

    matching_wallpapers = wallpapers_collection.find({"tags": {"$regex": query, "$options": "i"}})
    found_wallpapers = []
    async for wallpaper in matching_wallpapers:
        found_wallpapers.append(wallpaper)

    if found_wallpapers:
        await update.message.reply_text(f"Found {len(found_wallpapers)} wallpapers for '{query}':")
        for wallpaper in found_wallpapers:
            await update.message.reply_document(wallpaper["file_id"])
            await wallpapers_collection.update_one(
                {"_id": wallpaper["_id"]}, {"$inc": {"view_count": 1}}
            )
    else:
        await update.message.reply_text(f"No wallpapers found for '{query}'.")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()

    # --- Standard commands ---
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("browse", browse_command))
    application.add_handler(CommandHandler("random", random_command))
    application.add_handler(CommandHandler("trending", trending_command))
    application.add_handler(CommandHandler("request", request_command))
    application.add_handler(CommandHandler("daily", daily_command))
    application.add_handler(CommandHandler("stopdaily", stop_daily_command))
    application.add_handler(CommandHandler("stats", stats_command))

    # --- Owner commands ---
    application.add_handler(CommandHandler("addcategory", add_category_command))
    application.add_handler(CommandHandler("listcategories", list_categories_command))
    application.add_handler(CommandHandler("renamecategory", rename_category_command))
    application.add_handler(CommandHandler("deletecategory", delete_category_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("list", list_wallpapers_command))
    application.add_handler(CommandHandler("delete", delete_wallpaper_command))

    # --- Inline keyboard callbacks ---
    application.add_handler(CallbackQueryHandler(button_callback_handler))

    # --- Upload conversation ---
    upload_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("upload", upload_command)],
        states={
            UPLOAD_PHOTO: [MessageHandler(filters.PHOTO | filters.Document.IMAGE, upload_photo)],
            UPLOAD_TAGS: [MessageHandler(filters.TEXT & ~filters.COMMAND, upload_tags)],
        },
        fallbacks=[CommandHandler("cancel", cancel_upload)],
    )
    application.add_handler(upload_conv_handler)

    # --- Echo (non-command text) ---
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    # --- APScheduler: daily wallpaper at 12:00 PM IST ---
    scheduler = AsyncIOScheduler(timezone=timezone("Asia/Kolkata"))
    scheduler.add_job(
        send_daily_wallpaper,
        "cron",
        hour=12,
        minute=0,
        args=(application,),
        id="daily_wallpaper_job",
    )
    scheduler.start()

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
