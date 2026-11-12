import asyncio
import logging
from io import BytesIO

from django.conf import settings
from telegram import Bot, InputFile

logger = logging.getLogger(__name__)


def _run_async(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return asyncio.run(coro)
    except RuntimeError:
        pass
    return asyncio.run(coro)


def send_telegram_message(chat_id, text, *, parse_mode="HTML", reply_markup=None, disable_web_page_preview=True):
    token = (settings.TELEGRAM_BOT_TOKEN or "").strip()
    chat_id = (chat_id or "").strip()
    if not token or not chat_id:
        return None, "Telegram bot token or chat id missing."

    async def _send():
        bot = Bot(token=token)
        return await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
        )

    try:
        message = _run_async(_send())
        return message, ""
    except Exception as exc:
        logger.exception("Telegram send failed for chat_id=%s", chat_id)
        return None, str(exc)


def send_telegram_photo(chat_id, png_bytes, *, caption="", filename="qr.png", reply_markup=None):
    """Send a PNG. Caption may be HTML. reply_markup can include a URL button."""
    token = (settings.TELEGRAM_BOT_TOKEN or "").strip()
    chat_id = (str(chat_id or "")).strip()
    if not token or not chat_id or not png_bytes:
        return None, "Telegram bot token, chat id, or image missing."

    async def _send():
        bot = Bot(token=token)
        return await bot.send_photo(
            chat_id=chat_id,
            photo=InputFile(BytesIO(png_bytes), filename=filename),
            caption=caption or None,
            parse_mode="HTML" if caption else None,
            reply_markup=reply_markup,
        )

    try:
        message = _run_async(_send())
        return message, ""
    except Exception as exc:
        logger.exception("Telegram photo send failed for chat_id=%s", chat_id)
        return None, str(exc)
