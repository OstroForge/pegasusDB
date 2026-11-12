import logging

from asgiref.sync import sync_to_async
from django.conf import settings
from django.utils import timezone
from telegram import Update
from telegram.ext import ContextTypes

from ..models import Personnel
from .telegram_bookings import (
    BOOKING_DETAIL_CALLBACK_PREFIX,
    booking_for_personnel_by_id,
    bookings_list_keyboard,
    format_booking_details_message,
    format_bookings_list,
    format_directions_message,
    next_upcoming_booking_for_personnel,
    personnel_for_chat_id,
    upcoming_bookings_for_personnel,
)
from .telegram_course_swaps import (
    COVER_ACCEPT_CALLBACK_PREFIX,
    COVER_DECLINE_CALLBACK_PREFIX,
    COVER_REQUEST_BOOKING_CALLBACK_PREFIX,
    COVER_REQUEST_INSTRUCTOR_CALLBACK_PREFIX,
    COVER_REQUEST_MESSAGE_ADD_CALLBACK,
    COVER_REQUEST_MESSAGE_SKIP_CALLBACK,
    cover_request_keyboard,
    cover_request_message_keyboard,
    cover_swap_for_personnel_by_id,
    format_cover_outcome_message,
    format_cover_request_message,
    format_cover_request_message_prompt,
    format_cover_request_sent_confirmation,
    format_cover_requests_list,
    format_instructors_for_cover_request,
    format_swappable_bookings_for_request,
    incoming_cover_requests_for_personnel,
    instructors_for_cover_request_keyboard,
    pending_cover_requests_sent_by_personnel,
    swappable_bookings_request_keyboard,
)
from .telegram_link import consume_link_token
from .telegram_menu import main_menu_reply_keyboard, register_bot_commands
from .telegram_settings import (
    SETTINGS_CALLBACK_PREFIX,
    TELEGRAM_SETTING_KEYS,
    format_settings_message,
    settings_keyboard,
    toggle_personnel_setting,
)
from .telegram_today import (
    BOOKING_FEEDBACK_CALLBACK_PREFIX,
    BOOKING_DOCS_CALLBACK_PREFIX,
    BOOKING_QR_CALLBACK_PREFIX,
    BOOKING_REGISTER_CALLBACK_PREFIX,
    booking_for_documents,
    document_bookings_for_personnel,
    documents_bookings_keyboard,
    booking_feedback_form_url,
    booking_on_date_for_personnel,
    booking_register_form_url,
    bookings_on_date_for_personnel,
    feedback_qr_photo_and_caption,
    format_today_message,
    list_booking_qr_photos,
    qr_open_keyboard,
    registration_qr_photo_and_caption,
    telegram_qr_input_file,
    today_bookings_keyboard,
)

logger = logging.getLogger(__name__)


@sync_to_async
def _link_personnel(token, chat_id):
    personnel_id = consume_link_token(token)
    if not personnel_id:
        return None, (
            "That link is invalid or has expired. Open your Pegasus profile, "
            "refresh the page, and scan a new QR code."
        )

    try:
        personnel = Personnel.objects.get(pk=personnel_id, is_active=True)
    except Personnel.DoesNotExist:
        return None, "Could not find your Pegasus account for that link."

    existing = Personnel.objects.filter(telegram_chat_id=chat_id).exclude(pk=personnel.pk).first()
    if existing:
        return None, "This Telegram account is already linked to another Pegasus user."

    personnel.telegram_chat_id = str(chat_id)
    personnel.telegram_linked_at = timezone.now()
    personnel.notify_new_bookings_telegram = True
    personnel.notify_booking_changes_telegram = True
    personnel.notify_reminders_telegram = True
    personnel.notify_cover_requests_telegram = True
    personnel.save(
        update_fields=[
            "telegram_chat_id",
            "telegram_linked_at",
            "notify_new_bookings_telegram",
            "notify_booking_changes_telegram",
            "notify_reminders_telegram",
            "notify_cover_requests_telegram",
        ]
    )
    name = personnel.name or personnel.email
    return personnel, f"Linked to Pegasus as {name}. Use the buttons below or /help for commands."


async def _reply_with_menu(update, text, *, parse_mode=None, disable_web_page_preview=False, reply_markup=None):
    kwargs = {"reply_markup": reply_markup or main_menu_reply_keyboard()}
    if parse_mode:
        kwargs["parse_mode"] = parse_mode
    if disable_web_page_preview:
        kwargs["disable_web_page_preview"] = True
    await update.effective_message.reply_text(text, **kwargs)


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not chat_id:
        return

    linked = await sync_to_async(personnel_for_chat_id)(str(chat_id))
    if not linked:
        await update.message.reply_text("Account not linked. Use the QR code on your Pegasus profile.")
        return

    await update.message.reply_text(
        "Tap a button below for quick access to commands.",
        reply_markup=main_menu_reply_keyboard(),
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not chat_id:
        return

    args = context.args or []
    if args:
        personnel, message = await _link_personnel(args[0], chat_id)
        if personnel:
            await context.bot.send_message(
                chat_id=chat_id,
                text=message,
                reply_markup=main_menu_reply_keyboard(),
            )
        else:
            await update.message.reply_text(message)
        return

    linked = await sync_to_async(personnel_for_chat_id)(str(chat_id))
    if linked:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"Already linked as {linked.name or linked.email}.\n"
                "Use the buttons below or /help for all commands."
            ),
            reply_markup=main_menu_reply_keyboard(),
        )
        return

    bot = settings.TELEGRAM_BOT_USERNAME or "your_bot"
    await update.message.reply_text(
        f"Welcome to Unicorn Pegasus.\n\n"
        f"To link your account, open Profile in Pegasus and scan the Telegram QR code, "
        f"or open t.me/{bot} from the link shown there."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reply_with_menu(
        update,
        "Commands:\n"
        "/start — link your Pegasus account (via profile QR)\n"
        "/menu — show command buttons\n"
        "/today — today's course summary\n"
        "/nextcourse — full details for your next upcoming course\n"
        "/bookings — list upcoming bookings (tap one or reply with its number)\n"
        "/covers — incoming course cover requests (accept or decline)\n"
        "/requestcover — ask another instructor to cover one of your courses\n"
        "  (during request: tap buttons or /cancel to stop)\n"
        "/directions — address and map link for your next booking\n"
        "/email — email the course documents (map, contacts, fees and QR codes) again\n"
        "/qrcodes — all QR codes for today's course (registration, feedback, accident reports, exams)\n"
        "/registration — registration QR and link for today's course\n"
        "/feedback — feedback QR and link for today's course\n"
        "/settings — turn booking alerts on or off\n"
        "/help — this message",
    )


def _store_bookings_pick_list(context, bookings):
    context.user_data["booking_ids"] = [b.pk for b in bookings]
    context.user_data["telegram_pick_list"] = "bookings"


async def _reply_booking_details(context, chat_id, booking):
    text = await sync_to_async(format_booking_details_message)(booking)
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def bookings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    personnel = await sync_to_async(personnel_for_chat_id)(chat_id)
    if not personnel:
        await update.message.reply_text("Account not linked. Use the QR code on your Pegasus profile.")
        return

    bookings = await sync_to_async(list)(upcoming_bookings_for_personnel(personnel))
    _store_bookings_pick_list(context, bookings)
    text = await sync_to_async(format_bookings_list)(bookings)
    keyboard = bookings_list_keyboard(bookings) if bookings else None
    await update.message.reply_text(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )


async def booking_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    if not chat_id:
        return

    data = query.data
    if not data.startswith(BOOKING_DETAIL_CALLBACK_PREFIX):
        await query.answer()
        return

    booking_id = data[len(BOOKING_DETAIL_CALLBACK_PREFIX):]
    if not booking_id:
        await query.answer("Could not read that booking.", show_alert=True)
        return

    await query.answer()

    try:
        personnel = await sync_to_async(personnel_for_chat_id)(str(chat_id))
        if not personnel:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Account not linked. Use the QR code on your Pegasus profile.",
            )
            return

        booking = await sync_to_async(booking_for_personnel_by_id)(personnel, booking_id)
        if not booking:
            await context.bot.send_message(
                chat_id=chat_id,
                text="That booking was not found or is no longer available.",
            )
            return

        await _reply_booking_details(context, chat_id, booking)
    except Exception:
        logger.exception("Telegram booking detail callback failed for %s", booking_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text="Sorry, something went wrong loading that booking. Please try again.",
        )


def _store_cover_pick_list(context, swaps):
    context.user_data["cover_swap_ids"] = [s.pk for s in swaps]
    context.user_data["telegram_pick_list"] = "covers"


@sync_to_async
def _covers_data_for_personnel(personnel):
    incoming = incoming_cover_requests_for_personnel(personnel)
    outgoing = pending_cover_requests_sent_by_personnel(personnel)
    return incoming, outgoing


async def covers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    personnel, chat_id = await _linked_personnel_or_reply(update)
    if not personnel:
        return

    incoming, outgoing = await _covers_data_for_personnel(personnel)
    _store_cover_pick_list(context, incoming)

    summary = await sync_to_async(format_cover_requests_list)(incoming, outgoing)
    await context.bot.send_message(
        chat_id=chat_id,
        text=summary,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    for swap in incoming:
        text = await sync_to_async(format_cover_request_message)(
            swap,
            intro=None,
        )
        keyboard = cover_request_keyboard(swap)
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )


@sync_to_async
def _telegram_respond_to_cover(swap_id, personnel, *, accept):
    from ..services.course_swaps import CourseSwapError, accept_swap, decline_swap

    try:
        if accept:
            swap = accept_swap(swap_id=swap_id, recipient=personnel)
        else:
            swap = decline_swap(swap_id=swap_id, recipient=personnel)
        return swap, None
    except CourseSwapError as exc:
        return None, str(exc)


async def _cover_action_callback(update, context, *, accept):
    query = update.callback_query
    if not query or not query.data:
        return

    prefix = COVER_ACCEPT_CALLBACK_PREFIX if accept else COVER_DECLINE_CALLBACK_PREFIX
    data = query.data
    if not data.startswith(prefix):
        await query.answer()
        return

    swap_id = data[len(prefix):]
    if not swap_id:
        await query.answer("Could not read that request.", show_alert=True)
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    if not chat_id:
        return

    personnel = await sync_to_async(personnel_for_chat_id)(str(chat_id))
    if not personnel:
        await query.answer("Account not linked.", show_alert=True)
        return

    swap, error = await _telegram_respond_to_cover(swap_id, personnel, accept=accept)
    if error:
        await query.answer(error, show_alert=True)
        return

    await query.answer("Accepted." if accept else "Declined.")

    try:
        text = await sync_to_async(format_cover_outcome_message)(swap, accepted=accept)
        await query.edit_message_text(text, parse_mode="HTML")
    except Exception:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")


async def cover_accept_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _cover_action_callback(update, context, accept=True)


async def cover_decline_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _cover_action_callback(update, context, accept=False)


async def cover_pick_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    swap_ids = context.user_data.get("cover_swap_ids")
    if not swap_ids:
        return

    text = (update.message.text or "").strip()
    if not text.isdigit():
        return

    pick = int(text)
    if pick < 1 or pick > len(swap_ids):
        await update.message.reply_text(
            f"Please enter a number between 1 and {len(swap_ids)}, or run /covers again."
        )
        return

    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    personnel = await sync_to_async(personnel_for_chat_id)(chat_id)
    if not personnel:
        await update.message.reply_text("Account not linked. Use the QR code on your Pegasus profile.")
        return

    swap = await sync_to_async(cover_swap_for_personnel_by_id)(personnel, swap_ids[pick - 1])
    if not swap:
        await update.message.reply_text("That cover request was not found or is no longer pending.")
        return

    body = await sync_to_async(format_cover_request_message)(swap)
    keyboard = cover_request_keyboard(swap)
    await update.message.reply_text(
        body,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )


def _clear_cover_request_flow(context):
    for key in (
        "cover_request_booking_ids",
        "cover_request_booking_id",
        "cover_request_instructor_ids",
        "cover_request_instructor_id",
    ):
        context.user_data.pop(key, None)
    if str(context.user_data.get("telegram_pick_list", "")).startswith("cover_request"):
        context.user_data.pop("telegram_pick_list", None)


@sync_to_async
def _cover_request_start_data(personnel):
    from ..services.course_swaps import eligible_target_instructors, swappable_bookings_for_instructor

    bookings = list(swappable_bookings_for_instructor(personnel))
    instructors = list(eligible_target_instructors(exclude_personnel=personnel))
    return bookings, instructors


@sync_to_async
def _cover_request_booking_for_personnel(personnel, booking_id):
    from ..models import Booking
    from ..services.course_swaps import SWAPPABLE_BOOKING_STATUSES

    return (
        Booking.objects.filter(
            pk=booking_id,
            instructor=personnel,
            status__in=SWAPPABLE_BOOKING_STATUSES,
        )
        .exclude(business__is_dummy=True)
        .select_related("course_type", "business", "training_location")
        .prefetch_related("days")
        .first()
    )


@sync_to_async
def _cover_request_instructor_for_personnel(personnel, instructor_id):
    from ..services.course_swaps import eligible_target_instructors

    return eligible_target_instructors(exclude_personnel=personnel).filter(pk=instructor_id).first()


def _create_cover_request_sync(personnel, booking_id, instructor_id, message=""):
    from ..models import Booking
    from ..services.course_swaps import (
        CourseSwapError,
        SWAPPABLE_BOOKING_STATUSES,
        create_swap_request,
        eligible_target_instructors,
    )

    booking = (
        Booking.objects.filter(
            pk=booking_id,
            instructor=personnel,
            status__in=SWAPPABLE_BOOKING_STATUSES,
        )
        .exclude(business__is_dummy=True)
        .select_related("course_type", "business", "training_location")
        .prefetch_related("days")
        .first()
    )
    to_instructor = eligible_target_instructors(exclude_personnel=personnel).filter(pk=instructor_id).first()
    if not booking or not to_instructor:
        return None, "That course or instructor is no longer available."

    try:
        swap = create_swap_request(
            booking=booking,
            from_instructor=personnel,
            to_instructor=to_instructor,
            message=message or "",
        )
        return swap, None
    except CourseSwapError as exc:
        return None, str(exc)


@sync_to_async
def _telegram_create_cover_request(personnel, booking_id, instructor_id, message=""):
    return _create_cover_request_sync(personnel, booking_id, instructor_id, message=message)


def _eligible_instructors_for_cover(personnel):
    from ..services.course_swaps import eligible_target_instructors

    return list(eligible_target_instructors(exclude_personnel=personnel))


async def _begin_cover_request_booking_step(update, context, personnel, chat_id, bookings):
    _clear_cover_request_flow(context)
    context.user_data["cover_request_booking_ids"] = [str(b.pk) for b in bookings]
    context.user_data["telegram_pick_list"] = "cover_request_booking"

    text = await sync_to_async(format_swappable_bookings_for_request)(bookings)
    keyboard = await sync_to_async(swappable_bookings_request_keyboard)(bookings)
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )


async def requestcover_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    personnel, chat_id = await _linked_personnel_or_reply(update)
    if not personnel:
        return

    bookings, instructors = await _cover_request_start_data(personnel)
    if not bookings:
        await context.bot.send_message(
            chat_id=chat_id,
            text="You have no scheduled or in-progress courses available for a cover request "
            "(or each already has a pending request).",
        )
        return
    if not instructors:
        await context.bot.send_message(
            chat_id=chat_id,
            text="No other instructors are available to request cover from.",
        )
        return

    await _begin_cover_request_booking_step(update, context, personnel, chat_id, bookings)


async def _cover_request_select_booking(update, context, personnel, chat_id, booking_id):
    booking = await _cover_request_booking_for_personnel(personnel, booking_id)
    if not booking:
        await context.bot.send_message(
            chat_id=chat_id,
            text="That course is not available for a cover request.",
        )
        return

    instructors = await sync_to_async(_eligible_instructors_for_cover)(personnel)
    if not instructors:
        _clear_cover_request_flow(context)
        await context.bot.send_message(
            chat_id=chat_id,
            text="No other instructors are available to request cover from.",
        )
        return

    context.user_data["cover_request_booking_id"] = str(booking.pk)
    context.user_data["cover_request_instructor_ids"] = [str(p.pk) for p in instructors]
    context.user_data["telegram_pick_list"] = "cover_request_instructor"

    text = await sync_to_async(format_instructors_for_cover_request)(booking, instructors)
    keyboard = await sync_to_async(instructors_for_cover_request_keyboard)(instructors)
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )


async def _cover_request_goto_message_step(context, personnel, chat_id, booking_id, instructor_id):
    booking = await _cover_request_booking_for_personnel(personnel, booking_id)
    instructor = await _cover_request_instructor_for_personnel(personnel, instructor_id)
    if not booking or not instructor:
        _clear_cover_request_flow(context)
        await context.bot.send_message(
            chat_id=chat_id,
            text="That course or instructor is no longer available.",
        )
        return

    context.user_data["cover_request_instructor_id"] = str(instructor_id)
    context.user_data["telegram_pick_list"] = "cover_request_message"

    prompt = await sync_to_async(format_cover_request_message_prompt)(booking, instructor)
    keyboard = await sync_to_async(cover_request_message_keyboard)()
    await context.bot.send_message(
        chat_id=chat_id,
        text=prompt,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )


async def cover_request_booking_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    booking_ids = context.user_data.get("cover_request_booking_ids") or []
    text = (update.message.text or "").strip()
    if not text.isdigit():
        return

    pick = int(text)
    if pick < 1 or pick > len(booking_ids):
        await update.message.reply_text(
            f"Please enter a number between 1 and {len(booking_ids)}, or send /cancel."
        )
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    personnel = await sync_to_async(personnel_for_chat_id)(str(chat_id))
    if not personnel:
        await update.message.reply_text("Account not linked. Use the QR code on your Pegasus profile.")
        return

    await _cover_request_select_booking(update, context, personnel, chat_id, booking_ids[pick - 1])


async def cover_request_booking_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return

    if not query.data.startswith(COVER_REQUEST_BOOKING_CALLBACK_PREFIX):
        await query.answer()
        return

    booking_id = query.data[len(COVER_REQUEST_BOOKING_CALLBACK_PREFIX):]
    if not booking_id:
        await query.answer("Could not read that course.", show_alert=True)
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    if not chat_id:
        return

    await query.answer()

    personnel = await sync_to_async(personnel_for_chat_id)(str(chat_id))
    if not personnel:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Account not linked. Use the QR code on your Pegasus profile.",
        )
        return

    await _cover_request_select_booking(update, context, personnel, chat_id, booking_id)


async def cover_request_instructor_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    instructor_ids = context.user_data.get("cover_request_instructor_ids") or []
    booking_id = context.user_data.get("cover_request_booking_id")
    text = (update.message.text or "").strip()
    if not booking_id or not text.isdigit():
        return

    pick = int(text)
    if pick < 1 or pick > len(instructor_ids):
        await update.message.reply_text(
            f"Please enter a number between 1 and {len(instructor_ids)}, or send /cancel."
        )
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    personnel = await sync_to_async(personnel_for_chat_id)(str(chat_id))
    if not personnel:
        await update.message.reply_text("Account not linked. Use the QR code on your Pegasus profile.")
        return

    instructor_id = instructor_ids[pick - 1]
    await _cover_request_goto_message_step(context, personnel, chat_id, booking_id, instructor_id)


async def cover_request_instructor_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return

    if not query.data.startswith(COVER_REQUEST_INSTRUCTOR_CALLBACK_PREFIX):
        await query.answer()
        return

    instructor_id = query.data[len(COVER_REQUEST_INSTRUCTOR_CALLBACK_PREFIX):]
    booking_id = context.user_data.get("cover_request_booking_id")
    if not instructor_id or not booking_id:
        await query.answer("This request has expired. Send /requestcover to start again.", show_alert=True)
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    if not chat_id:
        return

    await query.answer()

    personnel = await sync_to_async(personnel_for_chat_id)(str(chat_id))
    if not personnel:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Account not linked. Use the QR code on your Pegasus profile.",
        )
        return

    await _cover_request_goto_message_step(context, personnel, chat_id, booking_id, instructor_id)


async def cover_request_message_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return

    data = query.data
    if data not in (COVER_REQUEST_MESSAGE_SKIP_CALLBACK, COVER_REQUEST_MESSAGE_ADD_CALLBACK):
        await query.answer()
        return

    if context.user_data.get("telegram_pick_list") != "cover_request_message":
        await query.answer("This request has expired. Send /requestcover to start again.", show_alert=True)
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    if not chat_id:
        return

    if data == COVER_REQUEST_MESSAGE_SKIP_CALLBACK:
        await query.answer()
        submitted = await _submit_cover_request(update, context, message="")
        if submitted:
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
        return

    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await context.bot.send_message(
        chat_id=chat_id,
        text="Type your message below (max 500 characters), or send /cancel to stop.",
    )


async def _submit_cover_request(update, context, *, message):
    booking_id = context.user_data.get("cover_request_booking_id")
    instructor_id = context.user_data.get("cover_request_instructor_id")
    if not booking_id or not instructor_id:
        return False

    chat_id = update.effective_chat.id if update.effective_chat else None
    personnel = await sync_to_async(personnel_for_chat_id)(str(chat_id))
    if not personnel:
        await update.effective_message.reply_text(
            "Account not linked. Use the QR code on your Pegasus profile."
        )
        return True

    swap, error = await _telegram_create_cover_request(
        personnel,
        booking_id,
        instructor_id,
        message=(message or "").strip()[:500],
    )
    _clear_cover_request_flow(context)

    if error:
        await update.effective_message.reply_text(error)
        return True

    confirmation = await sync_to_async(format_cover_request_sent_confirmation)(swap)
    await update.effective_message.reply_text(
        confirmation,
        parse_mode="HTML",
        reply_markup=main_menu_reply_keyboard(),
    )
    return True


async def cover_request_message_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("telegram_pick_list") != "cover_request_message":
        return

    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Please send a message, tap a button above, or send /cancel.")
        return

    await _submit_cover_request(update, context, message=text)


async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("telegram_pick_list") != "cover_request_message":
        await update.message.reply_text("Nothing to skip right now.")
        return
    await _submit_cover_request(update, context, message="")


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not str(context.user_data.get("telegram_pick_list", "")).startswith("cover_request"):
        await update.message.reply_text("Nothing to cancel right now.")
        return
    _clear_cover_request_flow(context)
    await update.message.reply_text("Cover request cancelled.")


async def booking_pick_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pick_list = context.user_data.get("telegram_pick_list")

    if pick_list == "cover_request_message":
        await cover_request_message_step(update, context)
        return

    text = (update.message.text or "").strip()
    if not text.isdigit():
        return

    if pick_list == "cover_request_booking":
        await cover_request_booking_pick(update, context)
        return

    if pick_list == "cover_request_instructor":
        await cover_request_instructor_pick(update, context)
        return

    if pick_list == "covers" and context.user_data.get("cover_swap_ids"):
        await cover_pick_message(update, context)
        return

    booking_ids = context.user_data.get("booking_ids")
    if not booking_ids:
        return

    pick = int(text)
    if pick < 1 or pick > len(booking_ids):
        await update.message.reply_text(
            f"Please enter a number between 1 and {len(booking_ids)}, or run /bookings again."
        )
        return

    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    personnel = await sync_to_async(personnel_for_chat_id)(chat_id)
    if not personnel:
        await update.message.reply_text("Account not linked. Use the QR code on your Pegasus profile.")
        return

    booking = await sync_to_async(booking_for_personnel_by_id)(personnel, booking_ids[pick - 1])
    if not booking:
        await update.message.reply_text("That booking was not found or is no longer available.")
        return

    await _reply_booking_details(context, update.effective_chat.id, booking)


async def directions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    personnel = await sync_to_async(personnel_for_chat_id)(chat_id)
    if not personnel:
        await update.message.reply_text("Account not linked. Use the QR code on your Pegasus profile.")
        return

    booking = await sync_to_async(next_upcoming_booking_for_personnel)(personnel)
    text = await sync_to_async(format_directions_message)(booking)
    await _reply_with_menu(update, text, parse_mode="HTML", disable_web_page_preview=True)


async def nextcourse_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    personnel, chat_id = await _linked_personnel_or_reply(update)
    if not personnel:
        return

    booking = await sync_to_async(next_upcoming_booking_for_personnel)(personnel)
    if not booking:
        await context.bot.send_message(
            chat_id=chat_id,
            text="You have no upcoming bookings.",
            reply_markup=main_menu_reply_keyboard(),
        )
        return

    text = await sync_to_async(format_booking_details_message)(booking)
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=main_menu_reply_keyboard(),
    )


async def _linked_personnel_or_reply(update):
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not chat_id:
        return None, None
    personnel = await sync_to_async(personnel_for_chat_id)(str(chat_id))
    if not personnel:
        await update.effective_message.reply_text(
            "Account not linked. Use the QR code on your Pegasus profile."
        )
        return None, chat_id
    return personnel, chat_id


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    personnel, chat_id = await _linked_personnel_or_reply(update)
    if not personnel:
        return

    on_date = timezone.localdate()
    bookings = await sync_to_async(list)(bookings_on_date_for_personnel(personnel, on_date))
    text = await sync_to_async(format_today_message)(bookings, on_date)
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    personnel, chat_id = await _linked_personnel_or_reply(update)
    if not personnel:
        return

    text = format_settings_message(personnel)
    keyboard = settings_keyboard(personnel)
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def settings_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return

    data = query.data
    if not data.startswith(SETTINGS_CALLBACK_PREFIX):
        await query.answer()
        return

    setting_key = data[len(SETTINGS_CALLBACK_PREFIX):]
    if setting_key not in TELEGRAM_SETTING_KEYS:
        await query.answer()
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    if not chat_id:
        return

    await query.answer()

    try:
        personnel = await sync_to_async(personnel_for_chat_id)(str(chat_id))
        if not personnel:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Account not linked. Use the QR code on your Pegasus profile.",
            )
            return

        personnel = await sync_to_async(toggle_personnel_setting)(personnel, setting_key)
        text = format_settings_message(personnel)
        keyboard = settings_keyboard(personnel)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception:
        logger.exception("Telegram settings toggle failed for key %s", setting_key)
        await context.bot.send_message(
            chat_id=chat_id,
            text="Sorry, something went wrong updating your settings. Please try again.",
        )


async def _send_qr_photo(context, chat_id, *, png, caption, filename, title, url):
    if not png:
        await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML")
        return

    await context.bot.send_photo(
        chat_id=chat_id,
        photo=telegram_qr_input_file(png, filename),
        caption=caption,
        parse_mode="HTML",
        reply_markup=qr_open_keyboard(title, url) if url else None,
    )


async def _send_today_form_qr(context, chat_id, booking, *, form_kind, on_date):
    if form_kind == "register":
        png, caption = await sync_to_async(registration_qr_photo_and_caption)(booking, on_date)
        url = await sync_to_async(booking_register_form_url)(booking, on_date)
        filename = "register-qr.png"
        title = "Delegate registration"
    else:
        png, caption = await sync_to_async(feedback_qr_photo_and_caption)(booking, on_date)
        url = await sync_to_async(booking_feedback_form_url)(booking, on_date)
        filename = "feedback-qr.png"
        title = "Course feedback"

    await _send_qr_photo(
        context,
        chat_id,
        png=png,
        caption=caption,
        filename=filename,
        title=title,
        url=url,
    )


async def _send_booking_qr_set(context, chat_id, booking, on_date):
    photos = await sync_to_async(list_booking_qr_photos)(booking, on_date)
    if not photos:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Could not build QR codes for this course.",
        )
        return

    for photo in photos:
        await _send_qr_photo(
            context,
            chat_id,
            png=photo["png"],
            caption=photo["caption"],
            filename=photo["filename"],
            title=photo["title"],
            url=photo["url"],
        )


async def _today_form_command(update, context, *, form_kind):
    personnel, chat_id = await _linked_personnel_or_reply(update)
    if not personnel:
        return

    on_date = timezone.localdate()
    bookings = await sync_to_async(list)(bookings_on_date_for_personnel(personnel, on_date))
    if not bookings:
        label = "registration" if form_kind == "register" else "feedback"
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"You have no courses scheduled for today, so there is no {label} form to share.",
        )
        return

    if len(bookings) == 1:
        await _send_today_form_qr(context, chat_id, bookings[0], form_kind=form_kind, on_date=on_date)
        return

    prefix = (
        BOOKING_REGISTER_CALLBACK_PREFIX
        if form_kind == "register"
        else BOOKING_FEEDBACK_CALLBACK_PREFIX
    )
    prompt = (
        "You have more than one course today. Select which registration form to share:"
        if form_kind == "register"
        else "You have more than one course today. Select which feedback form to share:"
    )
    keyboard = today_bookings_keyboard(bookings, on_date=on_date, callback_prefix=prefix)
    await context.bot.send_message(
        chat_id=chat_id,
        text=prompt,
        reply_markup=keyboard,
    )


async def qrcodes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    personnel, chat_id = await _linked_personnel_or_reply(update)
    if not personnel:
        return

    on_date = timezone.localdate()
    bookings = await sync_to_async(list)(bookings_on_date_for_personnel(personnel, on_date))
    if not bookings:
        await context.bot.send_message(
            chat_id=chat_id,
            text="You have no courses scheduled for today, so there are no QR codes to share.",
        )
        return

    if len(bookings) == 1:
        await _send_booking_qr_set(context, chat_id, bookings[0], on_date)
        return

    keyboard = today_bookings_keyboard(
        bookings,
        on_date=on_date,
        callback_prefix=BOOKING_QR_CALLBACK_PREFIX,
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text="You have more than one course today. Select which course's QR codes to send:",
        reply_markup=keyboard,
    )


def _email_course_documents(personnel, booking):
    from .booking_email import instructor_email_address
    from .booking_notifications import notify_resend_email

    address = instructor_email_address(personnel)
    if not address:
        return False, "No email address is saved on your profile, so the documents could not be sent."
    sent = notify_resend_email(booking, recipient=personnel)
    if not sent:
        return False, "The course documents email could not be sent. Please try again."
    course = getattr(booking.course_type, "name", "") or "Course"
    ref = booking.course_reference or ""
    label = f"{course} ({ref})" if ref else course
    return True, f"Course documents for {label} have been emailed to {address}."


async def email_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    personnel, chat_id = await _linked_personnel_or_reply(update)
    if not personnel:
        return

    bookings = await sync_to_async(document_bookings_for_personnel)(personnel)
    if not bookings:
        await context.bot.send_message(
            chat_id=chat_id,
            text="You have no upcoming courses to email documents for.",
        )
        return

    if len(bookings) == 1:
        ok, message = await sync_to_async(_email_course_documents)(personnel, bookings[0])
        await context.bot.send_message(chat_id=chat_id, text=message)
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text="Which course should I email the documents for?",
        reply_markup=documents_bookings_keyboard(bookings),
    )


async def registration_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _today_form_command(update, context, form_kind="register")


async def feedback_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _today_form_command(update, context, form_kind="feedback")


async def _today_form_callback(update, context, *, form_kind, callback_prefix):
    query = update.callback_query
    if not query or not query.data:
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    if not chat_id:
        return

    data = query.data
    if not data.startswith(callback_prefix):
        await query.answer()
        return

    booking_id = data[len(callback_prefix):]
    if not booking_id:
        await query.answer("Could not read that course.", show_alert=True)
        return

    await query.answer()

    try:
        personnel = await sync_to_async(personnel_for_chat_id)(str(chat_id))
        if not personnel:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Account not linked. Use the QR code on your Pegasus profile.",
            )
            return

        booking = await sync_to_async(booking_for_personnel_by_id)(personnel, booking_id)
        if not booking:
            await context.bot.send_message(
                chat_id=chat_id,
                text="That course was not found or is no longer available.",
            )
            return

        on_date = timezone.localdate()
        await _send_today_form_qr(context, chat_id, booking, form_kind=form_kind, on_date=on_date)
    except Exception:
        logger.exception("Telegram %s callback failed for %s", form_kind, booking_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text="Sorry, something went wrong. Please try again.",
        )


async def booking_register_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _today_form_callback(
        update,
        context,
        form_kind="register",
        callback_prefix=BOOKING_REGISTER_CALLBACK_PREFIX,
    )


async def booking_feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _today_form_callback(
        update,
        context,
        form_kind="feedback",
        callback_prefix=BOOKING_FEEDBACK_CALLBACK_PREFIX,
    )


async def booking_documents_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    if not chat_id:
        return

    data = query.data
    if not data.startswith(BOOKING_DOCS_CALLBACK_PREFIX):
        await query.answer()
        return

    booking_id = data[len(BOOKING_DOCS_CALLBACK_PREFIX):]
    if not booking_id:
        await query.answer("Could not read that course.", show_alert=True)
        return

    await query.answer()

    try:
        personnel = await sync_to_async(personnel_for_chat_id)(str(chat_id))
        if not personnel:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Account not linked. Use the QR code on your Pegasus profile.",
            )
            return

        booking = await sync_to_async(booking_for_documents)(personnel, booking_id)
        if not booking:
            await context.bot.send_message(
                chat_id=chat_id,
                text="That course was not found or is no longer available.",
            )
            return

        ok, message = await sync_to_async(_email_course_documents)(personnel, booking)
        await context.bot.send_message(chat_id=chat_id, text=message)
    except Exception:
        logger.exception("Telegram documents callback failed for %s", booking_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text="Sorry, something went wrong. Please try again.",
        )


async def booking_qr_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    if not chat_id:
        return

    data = query.data
    if not data.startswith(BOOKING_QR_CALLBACK_PREFIX):
        await query.answer()
        return

    booking_id = data[len(BOOKING_QR_CALLBACK_PREFIX):]
    if not booking_id:
        await query.answer("Could not read that course.", show_alert=True)
        return

    await query.answer()

    try:
        personnel = await sync_to_async(personnel_for_chat_id)(str(chat_id))
        if not personnel:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Account not linked. Use the QR code on your Pegasus profile.",
            )
            return

        on_date = timezone.localdate()
        booking = await sync_to_async(booking_on_date_for_personnel)(personnel, booking_id, on_date)
        if not booking:
            await context.bot.send_message(
                chat_id=chat_id,
                text="That course was not found or is no longer available.",
            )
            return

        await _send_booking_qr_set(context, chat_id, booking, on_date)
    except Exception:
        logger.exception("Telegram QR codes callback failed for %s", booking_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text="Sorry, something went wrong. Please try again.",
        )
