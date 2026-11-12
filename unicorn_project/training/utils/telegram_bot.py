import logging
import sys

from django.conf import settings
from telegram import Update
from telegram.error import Conflict
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from .telegram_handlers import (
    booking_detail_callback,
    booking_documents_callback,
    booking_feedback_callback,
    booking_pick_message,
    booking_qr_callback,
    booking_register_callback,
    bookings_command,
    cancel_command,
    cover_accept_callback,
    cover_decline_callback,
    cover_request_booking_callback,
    cover_request_instructor_callback,
    cover_request_message_callback,
    covers_command,
    directions_command,
    email_command,
    feedback_command,
    help_command,
    menu_command,
    nextcourse_command,
    qrcodes_command,
    registration_command,
    requestcover_command,
    settings_command,
    settings_toggle_callback,
    skip_command,
    start_command,
    today_command,
)
from .telegram_bookings import BOOKING_DETAIL_CALLBACK_PREFIX
from .telegram_course_swaps import (
    COVER_ACCEPT_CALLBACK_PREFIX,
    COVER_DECLINE_CALLBACK_PREFIX,
    COVER_REQUEST_BOOKING_CALLBACK_PREFIX,
    COVER_REQUEST_INSTRUCTOR_CALLBACK_PREFIX,
    COVER_REQUEST_MESSAGE_CALLBACK_PREFIX,
)
from .telegram_menu import register_bot_commands
from .telegram_settings import SETTINGS_CALLBACK_PREFIX
from .telegram_today import (
    BOOKING_DOCS_CALLBACK_PREFIX,
    BOOKING_FEEDBACK_CALLBACK_PREFIX,
    BOOKING_QR_CALLBACK_PREFIX,
    BOOKING_REGISTER_CALLBACK_PREFIX,
)

logger = logging.getLogger(__name__)


async def _on_startup(application: Application, *, for_polling: bool = False) -> None:
    if for_polling:
        await application.bot.delete_webhook(drop_pending_updates=False)
        logger.info("Cleared webhook before polling (local dev).")
    me = await application.bot.get_me()
    configured = (settings.TELEGRAM_BOT_USERNAME or "").strip().lstrip("@")
    await register_bot_commands(application.bot)
    logger.info("Telegram bot ready: @%s (id %s)", me.username, me.id)
    if configured and configured.lower() != (me.username or "").lower():
        logger.error(
            "TELEGRAM_BOT_USERNAME=%r does not match token bot @%s — QR deep links will open the wrong chat.",
            configured,
            me.username,
        )


async def _on_error(update, context) -> None:
    if isinstance(context.error, Conflict):
        logger.warning(
            "Another process is polling this bot token (HTTP 409). "
            "Stop duplicate telegram_poll terminals and try again."
        )
        return
    logger.exception("Telegram handler error: %s", context.error)


def build_application(*, for_polling: bool = False) -> Application:
    token = (settings.TELEGRAM_BOT_TOKEN or "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in .env")

    async def on_startup(application: Application) -> None:
        await _on_startup(application, for_polling=for_polling)

    app = Application.builder().token(token).post_init(on_startup).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("today", today_command))
    app.add_handler(CommandHandler("nextcourse", nextcourse_command))
    app.add_handler(CommandHandler("bookings", bookings_command))
    app.add_handler(CommandHandler("covers", covers_command))
    app.add_handler(CommandHandler("requestcover", requestcover_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("skip", skip_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("directions", directions_command))
    app.add_handler(CommandHandler("email", email_command))
    app.add_handler(CommandHandler("documents", email_command))
    app.add_handler(CommandHandler("qrcodes", qrcodes_command))
    app.add_handler(CommandHandler("registration", registration_command))
    app.add_handler(CommandHandler("feedback", feedback_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CallbackQueryHandler(booking_detail_callback, pattern=f"^{BOOKING_DETAIL_CALLBACK_PREFIX}"))
    app.add_handler(CallbackQueryHandler(cover_accept_callback, pattern=f"^{COVER_ACCEPT_CALLBACK_PREFIX}"))
    app.add_handler(CallbackQueryHandler(cover_decline_callback, pattern=f"^{COVER_DECLINE_CALLBACK_PREFIX}"))
    app.add_handler(
        CallbackQueryHandler(
            cover_request_booking_callback,
            pattern=f"^{COVER_REQUEST_BOOKING_CALLBACK_PREFIX}",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            cover_request_instructor_callback,
            pattern=f"^{COVER_REQUEST_INSTRUCTOR_CALLBACK_PREFIX}",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            cover_request_message_callback,
            pattern=f"^{COVER_REQUEST_MESSAGE_CALLBACK_PREFIX}",
        )
    )
    app.add_handler(CallbackQueryHandler(booking_register_callback, pattern=f"^{BOOKING_REGISTER_CALLBACK_PREFIX}"))
    app.add_handler(CallbackQueryHandler(booking_qr_callback, pattern=f"^{BOOKING_QR_CALLBACK_PREFIX}"))
    app.add_handler(CallbackQueryHandler(booking_documents_callback, pattern=f"^{BOOKING_DOCS_CALLBACK_PREFIX}"))
    app.add_handler(CallbackQueryHandler(booking_feedback_callback, pattern=f"^{BOOKING_FEEDBACK_CALLBACK_PREFIX}"))
    app.add_handler(CallbackQueryHandler(settings_toggle_callback, pattern=f"^{SETTINGS_CALLBACK_PREFIX}"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, booking_pick_message))
    app.add_error_handler(_on_error)
    return app


def webhook_url() -> str:
    secret = (settings.TELEGRAM_WEBHOOK_SECRET or "").strip()
    site = (getattr(settings, "SITE_URL", "") or "").rstrip("/")
    if not secret or not site:
        return ""
    return f"{site}/telegram/webhook/{secret}/"


async def process_webhook_update(update_data: dict) -> None:
    """Handle one inbound Telegram update posted to our webhook."""
    application = build_application()
    async with application:
        update = Update.de_json(update_data, application.bot)
        await application.process_update(update)


async def set_production_webhook() -> str:
    """Register HTTPS webhook with Telegram (production only)."""
    secret = (settings.TELEGRAM_WEBHOOK_SECRET or "").strip()
    site = (getattr(settings, "SITE_URL", "") or "").rstrip("/")
    if not secret:
        raise RuntimeError("TELEGRAM_WEBHOOK_SECRET is not set")
    if not site.startswith("https://"):
        raise RuntimeError(f"SITE_URL must be https for Telegram webhook (got {site!r})")

    url = webhook_url()
    application = build_application()
    async with application:
        await application.bot.set_webhook(
            url=url,
            secret_token=secret,
            drop_pending_updates=True,
        )
        me = await application.bot.get_me()
        logger.info("Telegram webhook registered for @%s -> %s", me.username, url)
    return url


async def clear_webhook() -> None:
    application = build_application()
    async with application:
        await application.bot.delete_webhook(drop_pending_updates=False)


def run_polling():
    configured = (settings.TELEGRAM_BOT_USERNAME or "").strip().lstrip("@")
    logger.info("Starting Telegram bot polling (configured as @%s)…", configured or "?")
    print(
        f"Telegram polling starting for @{configured or '?'} — leave this terminal open. "
        "Only one poller may use each bot token.",
        flush=True,
    )
    try:
        build_application(for_polling=True).run_polling(drop_pending_updates=True)
    except Exception as exc:
        msg = str(exc)
        if "Conflict" in msg or "getUpdates" in msg:
            print(
                "\nERROR: Another process is already polling this bot token (HTTP 409).\n"
                "  • Stop telegram_poll.bat elsewhere, or\n"
                "  • Run `python manage.py telegram_set_webhook --clear` if the bot has a production webhook, or\n"
                "  • Use a separate dev bot token in .env (not the live production token).\n",
                file=sys.stderr,
                flush=True,
            )
        raise
