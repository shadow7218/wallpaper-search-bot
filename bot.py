#!/usr/bin/env python3

import os
import logging
import motor.motor_asyncio
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pytz import timezone
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_TELEGRAM_ID = int(os.getenv("OWNER_TELEGRAM_ID"))
MONGODB_CONNECTION_STRING = os.getenv("MONGODB_CONNECTION_STRING")

# MongoDB connection
client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_CONNECTION_STRING)
db = client.wallpaper_bot

wallpapers_collection = db.wallpapers
users_collection = db.users
requests_collection = db.requests

async def is_owner(user_id: int) -> bool:
    return user_id == OWNER_TELEGRAM_ID

async def get_user_stats():
    total_users = await users_collection.count_documents({})
    subscribed_users = await users_collection.count_documents({"daily_subscribed": True})
    return total_users, subscribed_users

async def get_wallpaper_stats():
    total_wallpapers = await wallpapers_collection.count_documents({})
    return total_wallpapers

async def get_most_searched_terms():
    # This will require more sophisticated aggregation later
    return []

async def get_total_views():
    # This will require more sophisticated aggregation later
    return 0

from telegram import Update
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ConversationHandler, CallbackQueryHandler

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Conversation states for upload
UPLOAD_PHOTO, UPLOAD_TAGS = range(2)


async def start(update: Update, context) -> None:
    """Send a message when the command /start is issued and register user."""
    user = update.effective_user
    # Register or update user in database
    await users_collection.update_one(
        {"_id": user.id},
        {"$set": {"username": user.username or user.first_name, "first_name": user.first_name, "last_name": user.last_name}},
        upsert=True
    )
    await update.message.reply_html(
        f"Hi {user.mention_html()}! Welcome to the Wallpaper Bot. "
        "I can help you find and manage wallpapers. "
        "Type /help to see all available commands."
    )


async def help_command(update: Update, context) -> None:
    """Send a message when the command /help is issued."""
    help_text = (
        "Here are the commands you can use:\n\n"
        "/start - Welcome message\n"
        "/help - Show all commands\n"
        "/search <name> - Search wallpapers (e.g., /search naruto)\n"
        "/browse - Show category buttons\n"
        "/random - Get a random wallpaper\n"
        "/random <tag> - Get a random wallpaper with a specific tag (e.g., /random naruto)\n"
        "/trending - Show the most popular wallpapers\n"
        "/request <description> - Request a wallpaper (e.g., /request gojo purple wallpaper)\n"
        "/daily - Subscribe to daily wallpaper\n"
        "/stopdaily - Unsubscribe from daily\n"
    )
    

    if await is_owner(update.effective_user.id):
        help_text += (
            "\nOwner Commands:\n"
            "/upload - Start wallpaper upload mode\n"
            "/stats - Show bot statistics\n"
        )

    await update.message.reply_text(help_text)


async def upload_command(update: Update, context) -> int:
    """Starts the upload conversation and asks for the photo."""
    if not await is_owner(update.effective_user.id):
        await update.message.reply_text("You are not authorized to upload wallpapers.")
        return ConversationHandler.END

    await update.message.reply_text(
        "Please send the wallpaper image you want to upload."
    )
    return UPLOAD_PHOTO


async def upload_photo(update: Update, context) -> int:
    """Stores the photo file_id and asks for tags."""
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document and update.message.document.mime_type.startswith('image'):
        file_id = update.message.document.file_id
    else:
        await update.message.reply_text("That doesn't look like an image. Please send an image file.")
        return UPLOAD_PHOTO

    context.user_data["file_id"] = file_id
    await update.message.reply_text(
        "Image received! Now, please send the tags for this wallpaper, separated by spaces (e.g., naruto sasuke fight)."
    )
    return UPLOAD_TAGS


async def upload_tags(update: Update, context) -> int:
    """Stores the wallpaper with its tags in the database and ends the conversation."""
    tags = [tag.strip().lower() for tag in update.message.text.split() if tag.strip()]
    file_id = context.user_data["file_id"]

    if not tags:
        await update.message.reply_text("No tags provided. Please try again with some tags.")
        return UPLOAD_TAGS

    wallpaper = {
        "file_id": file_id,
        "tags": tags,
        "view_count": 0,
        "upload_date": datetime.utcnow()
    }
    await wallpapers_collection.insert_one(wallpaper)

    await update.message.reply_text(
        f"Wallpaper uploaded successfully with tags: {', '.join(tags)}!"
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_upload(update: Update, context) -> int:
    """Cancels the upload conversation."""
    await update.message.reply_text("Upload cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


async def search_command(update: Update, context) -> None:
    """Searches for wallpapers based on provided tags."""
    query = " ".join(context.args).lower()
    if not query:
        await update.message.reply_text("Please provide a search term. Example: /search naruto")
        return

    # Find wallpapers with tags matching the query (case-insensitive, partial match)
    # Using $regex for partial and case-insensitive matching
    matching_wallpapers = wallpapers_collection.find({"tags": {"$regex": query, "$options": "i"}})
    
    found_wallpapers = []
    async for wallpaper in matching_wallpapers:
        found_wallpapers.append(wallpaper)

    if found_wallpapers:
        await update.message.reply_text(f"Found {len(found_wallpapers)} wallpapers for '{query}':")
        for wallpaper in found_wallpapers:
            await update.message.reply_document(wallpaper["file_id"])
            # Increment view count
            await wallpapers_collection.update_one(
                {"_id": wallpaper["_id"]},
                {"$inc": {"view_count": 1}}
            )
    else:
        await update.message.reply_text(f"No wallpapers found for '{query}'.")


CATEGORIES = [
    "Naruto", "One Piece", "Jujutsu Kaisen", "Dragon Ball",
    "Demon Slayer", "Attack on Titan", "My Hero Academia"
]

async def browse_command(update: Update, context) -> None:
    """Shows an inline keyboard with wallpaper categories."""
    keyboard = [
        [InlineKeyboardButton(category, callback_data=f"category_{category.lower().replace(' ', '_')}")]
        for category in CATEGORIES
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Please choose a category:", reply_markup=reply_markup)


async def button_callback_handler(update: Update, context) -> None:
    """Handles button presses from the inline keyboard."""
    query = update.callback_query
    await query.answer()

    data = query.data
    if data.startswith("category_"):
        category = data.replace("category_", "").replace('_', ' ')
        await query.edit_message_text(f"Showing wallpapers for category: {category.title()}")
        
        matching_wallpapers = wallpapers_collection.find({"tags": {"$regex": category, "$options": "i"}})
        found_wallpapers = []
        async for wallpaper in matching_wallpapers:
            found_wallpapers.append(wallpaper)

        if found_wallpapers:
            for wallpaper in found_wallpapers:
                await context.bot.send_document(chat_id=query.message.chat_id, document=wallpaper["file_id"])
                await wallpapers_collection.update_one(
                    {"_id": wallpaper["_id"]},
                    {"$inc": {"view_count": 1}}
                )
        else:
            await context.bot.send_message(chat_id=query.message.chat_id, text=f"No wallpapers found for category: {category.title()}.")


async def random_command(update: Update, context) -> None:
    """Sends a random wallpaper, optionally filtered by tag."""
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
            {"_id": wallpaper["_id"]},
            {"$inc": {"view_count": 1}}
        )
    else:
        if query_tag:
            await update.message.reply_text(f"No random wallpapers found for tag: {query_tag}.")
        else:
            await update.message.reply_text("No wallpapers available yet.")


async def trending_command(update: Update, context) -> None:
    """Shows the most popular wallpapers based on view count."""
    trending_wallpapers = wallpapers_collection.find({}).sort("view_count", -1).limit(10)
    
    found_wallpapers = []
    async for wallpaper in trending_wallpapers:
        found_wallpapers.append(wallpaper)

    if found_wallpapers:
        await update.message.reply_text("Here are the top 10 trending wallpapers:")
        for wallpaper in found_wallpapers:
            await update.message.reply_document(wallpaper["file_id"])
            # Increment view count for trending view
            await wallpapers_collection.update_one(
                {"_id": wallpaper["_id"]},
                {"$inc": {"view_count": 1}}
            )
    else:
        await update.message.reply_text("No wallpapers available to determine trending ones yet.")


async def request_command(update: Update, context) -> None:
    """Allows users to request a wallpaper and notifies the owner."""
    request_text = " ".join(context.args)
    if not request_text:
        await update.message.reply_text("Please provide a description of the wallpaper you want to request. Example: /request gojo purple wallpaper")
        return

    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name

    request_doc = {
        "user_id": user_id,
        "username": username,
        "request_text": request_text,
        "timestamp": datetime.utcnow()
    }
    await requests_collection.insert_one(request_doc)

    await update.message.reply_text("Your request has been submitted! The owner will be notified.")

    # Notify owner
    owner_message = (
        f"New wallpaper request from @{username} (ID: {user_id}):\n"
        f"'{request_text}'"
    )
    await context.bot.send_message(chat_id=OWNER_TELEGRAM_ID, text=owner_message)


async def daily_command(update: Update, context) -> None:
    """Subscribes the user to daily wallpapers."""
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name

    await users_collection.update_one(
        {"_id": user_id},
        {"$set": {"daily_subscribed": True, "username": username}},
        upsert=True
    )
    await update.message.reply_text("You have subscribed to daily wallpapers! You will receive a random wallpaper every day at 12:00 PM IST.")


async def stop_daily_command(update: Update, context) -> None:
    """Unsubscribes the user from daily wallpapers."""
    user_id = update.effective_user.id

    await users_collection.update_one(
        {"_id": user_id},
        {"$set": {"daily_subscribed": False}}
    )
    await update.message.reply_text("You have unsubscribed from daily wallpapers. You will no longer receive daily updates.")


async def stats_command(update: Update, context) -> None:
    """Shows bot statistics (owner only)."""
    if not await is_owner(update.effective_user.id):
        await update.message.reply_text("You are not authorized to view statistics.")
        return

    total_users, subscribed_users = await get_user_stats()
    total_wallpapers = await get_wallpaper_stats()
    # most_searched_terms = await get_most_searched_terms() # To be implemented more robustly
    # total_views = await get_total_views() # To be implemented more robustly

    stats_text = (
        f"📊 Bot Statistics 📊\n\n"
        f"Total Users: {total_users}\n"
        f"Subscribed to Daily: {subscribed_users}\n"
        f"Total Wallpapers: {total_wallpapers}\n"
        # f"Most Searched Terms: {', '.join(most_searched_terms)}\n"
        # f"Total Wallpaper Views: {total_views}\n"
    )
    await update.message.reply_text(stats_text)


async def send_daily_wallpaper(context) -> None:
    """Sends a random wallpaper to all subscribed users."""
    subscribed_users = users_collection.find({"daily_subscribed": True})
    
    pipeline = [{"$sample": {"size": 1}}]
    random_wallpaper = await wallpapers_collection.aggregate(pipeline).to_list(length=1)

    if random_wallpaper:
        wallpaper = random_wallpaper[0]
        async for user in subscribed_users:
            try:
                await context.bot.send_document(chat_id=user["_id"], document=wallpaper["file_id"])
                await wallpapers_collection.update_one(
                    {"_id": wallpaper["_id"]},
                    {"$inc": {"view_count": 1}}
                )
            except Exception as e:
                logger.error(f"Could not send daily wallpaper to user {user['_id']}: {e}")
    else:
        logger.info("No wallpapers available to send daily.")


async def echo(update: Update, context) -> None:
    """If the message is not a command, treat it as a search query."""
    query = update.message.text.lower()
    if not query:
        return

    # Find wallpapers with tags matching the query (case-insensitive, partial match)
    matching_wallpapers = wallpapers_collection.find({"tags": {"$regex": query, "$options": "i"}})
    
    found_wallpapers = []
    async for wallpaper in matching_wallpapers:
        found_wallpapers.append(wallpaper)

    if found_wallpapers:
        await update.message.reply_text(f"Found {len(found_wallpapers)} wallpapers for \'{query}\':")
        for wallpaper in found_wallpapers:
            await update.message.reply_document(wallpaper["file_id"])
            # Increment view count
            await wallpapers_collection.update_one(
                {"_id": wallpaper["_id"]},
                {"$inc": {"view_count": 1}}
            )
    else:
        await update.message.reply_text(f"No wallpapers found for \'{query}\'.")


def main() -> None:
    """Start the bot."""
    # Create the Application and pass it your bot's token.
    application = Application.builder().token(BOT_TOKEN).build()

    # on different commands - answer in Telegram
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
    application.add_handler(CallbackQueryHandler(button_callback_handler))

    # Add conversation handler for upload
    upload_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("upload", upload_command)],
        states={
            UPLOAD_PHOTO: [MessageHandler(filters.PHOTO | filters.Document.IMAGE, upload_photo)],
            UPLOAD_TAGS: [MessageHandler(filters.TEXT & ~filters.COMMAND, upload_tags)],
        },
        fallbacks=[CommandHandler("cancel", cancel_upload)],
    )
    application.add_handler(upload_conv_handler)

    # on non command i.e message - echo the message on Telegram
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    # Run the bot until the user presses Ctrl-C
    # Setup APScheduler for daily wallpapers
    scheduler = AsyncIOScheduler(timezone=timezone('Asia/Kolkata')) # IST is Asia/Kolkata
    scheduler.add_job(send_daily_wallpaper, 'cron', hour=12, minute=0, args=(application,), id='daily_wallpaper_job')
    scheduler.start()

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
