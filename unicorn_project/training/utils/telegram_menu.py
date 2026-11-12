from telegram import BotCommand, KeyboardButton, ReplyKeyboardMarkup

BOT_COMMANDS = [
    BotCommand("today", "Today's courses"),
    BotCommand("nextcourse", "Your next upcoming course"),
    BotCommand("bookings", "Upcoming bookings"),
    BotCommand("directions", "Directions to next course"),
    BotCommand("covers", "Course cover requests"),
    BotCommand("requestcover", "Request course cover"),
    BotCommand("email", "Email course documents again"),
    BotCommand("qrcodes", "QR codes for today's course"),
    BotCommand("registration", "Registration QR for today"),
    BotCommand("feedback", "Feedback QR for today"),
    BotCommand("settings", "Notification settings"),
    BotCommand("menu", "Show command buttons"),
    BotCommand("help", "Help"),
]


def main_menu_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("/today"), KeyboardButton("/nextcourse")],
            [KeyboardButton("/bookings"), KeyboardButton("/directions")],
            [KeyboardButton("/covers"), KeyboardButton("/requestcover")],
            [KeyboardButton("/email"), KeyboardButton("/qrcodes")],
            [KeyboardButton("/registration"), KeyboardButton("/feedback")],
            [KeyboardButton("/settings"), KeyboardButton("/help")],
        ],
        resize_keyboard=True,
    )


async def register_bot_commands(bot) -> None:
    await bot.set_my_commands(BOT_COMMANDS)
