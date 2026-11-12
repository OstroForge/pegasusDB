from django.conf import settings

from ..models import TelegramNotification
from .booking_details import NOTIFICATION_HEADINGS, format_booking_telegram_message
from .booking_email import notify_instructor_via_email
from .notification_prefs import (
    personnel_wants_notification,
    should_notify_booking,
    should_notify_booking_detail_changes,
)
from .telegram_client import send_telegram_message


NOTIFICATION_LABELS = {
    "new_booking": "new booking alert",
    "booking_changes": "booking update",
    "booking_cancellation": "cancellation alert",
    "booking_reminder": "reminder",
    "departure_reminder": "departure reminder",
    "upcoming_booking": "upcoming booking reminder",
    "manual": "manual message",
    "resend": "booking details resend",
}


def notify_instructor_via_telegram(
    booking,
    notification_type: str,
    *,
    intro=None,
    changed_areas=None,
    force=False,
    sent_by=None,
):
    """
    Send a Telegram message to the booking instructor.
    Returns True when a message was sent successfully.
    force=True skips notification preference checks (manual send).
    """
    if not should_notify_booking(booking):
        return False

    if not (settings.TELEGRAM_BOT_TOKEN or "").strip():
        return False

    instructor = booking.instructor
    if not instructor or not (instructor.telegram_chat_id or "").strip():
        return False

    if not force and not personnel_wants_notification(instructor, notification_type, channel="telegram"):
        return False

    text = format_booking_telegram_message(
        booking,
        notification_type=notification_type,
        intro=intro,
        changed_areas=changed_areas,
    )
    message, error = send_telegram_message(instructor.telegram_chat_id, text)
    success = message is not None

    TelegramNotification.objects.create(
        booking=booking,
        personnel=instructor,
        notification_type=notification_type,
        message_id=str(message.message_id) if message else "",
        success=success,
        error_text=error or "",
    )
    return success


def notify_new_booking(booking):
    telegram = notify_instructor_via_telegram(booking, "new_booking")
    email = notify_instructor_via_email(booking, "new_booking")
    return telegram or email


def notify_booking_changes(booking, *, intro=None, changed_areas=None):
    if not changed_areas:
        return False
    if not should_notify_booking_detail_changes(booking):
        return False
    if intro is None:
        intro = "A booking assigned to you has been updated."
    telegram = notify_instructor_via_telegram(
        booking,
        "booking_changes",
        intro=intro,
        changed_areas=changed_areas,
    )
    email = notify_instructor_via_email(
        booking,
        "booking_changes",
        intro=intro,
        changed_areas=changed_areas,
    )
    return telegram or email


def notify_booking_cancellation(booking, *, intro=None):
    from .booking_change_detection import CHANGE_CANCELLATION

    if intro is None:
        intro = "A booking assigned to you has been cancelled."
    telegram = notify_instructor_via_telegram(
        booking,
        "booking_cancellation",
        intro=intro,
        changed_areas=[CHANGE_CANCELLATION],
    )
    # Simple notice only — no full course pack PDF
    email = notify_instructor_via_email(
        booking,
        "booking_cancellation",
        intro=intro,
        changed_areas=[CHANGE_CANCELLATION],
        attach_pdf=False,
    )
    return telegram or email


def notify_manual(booking, *, intro=None):
    if intro is None:
        intro = NOTIFICATION_HEADINGS.get("manual", "Booking details")
    telegram = notify_instructor_via_telegram(booking, "manual", intro=intro, force=True)
    email = notify_instructor_via_email(booking, "manual", intro=intro, force=True)
    return telegram or email


def notify_resend_telegram(booking):
    return notify_instructor_via_telegram(booking, "resend", force=True)


def notify_resend_email(booking, *, recipient=None):
    return notify_instructor_via_email(booking, "resend", force=True, recipient=recipient)


def notify_resend(booking):
    return notify_resend_telegram(booking) or notify_resend_email(booking)


def notify_booking_reminder(booking):
    telegram = notify_instructor_via_telegram(booking, "booking_reminder")
    email = notify_instructor_via_email(booking, "booking_reminder")
    return telegram or email


def notify_departure_reminder(booking, *, on_date, morning_today=False, travel_seconds=None):
    from .telegram_today import format_departure_reminder_message, format_morning_today_reminder_message

    if not should_notify_booking(booking):
        return False

    instructor = booking.instructor
    telegram_ok = False
    email_ok = False

    if (settings.TELEGRAM_BOT_TOKEN or "").strip():
        from .telegram_today import (
            format_qr_followup_message,
            list_booking_qr_photos,
            send_booking_qr_photos,
        )

        qr_photos = list_booking_qr_photos(booking, on_date)
        if instructor and (instructor.telegram_chat_id or "").strip():
            if personnel_wants_notification(instructor, "departure_reminder", channel="telegram"):
                if morning_today:
                    text = format_morning_today_reminder_message(booking, on_date)
                else:
                    buffer_minutes = int(getattr(settings, "DEPARTURE_REMINDER_BUFFER_MINUTES", 30))
                    text = format_departure_reminder_message(
                        booking,
                        on_date,
                        travel_seconds=int(travel_seconds or 0),
                        buffer_minutes=buffer_minutes,
                    )
                if qr_photos:
                    text = (
                        text
                        + "\n\n<b>Delegate QR codes</b> follow this message. "
                        + "Tap <b>Open</b> on a code to open that form."
                    )
                message, error = send_telegram_message(instructor.telegram_chat_id, text)
                telegram_ok = message is not None
                if telegram_ok and qr_photos:
                    send_booking_qr_photos(instructor.telegram_chat_id, booking, on_date)
                TelegramNotification.objects.create(
                    booking=booking,
                    personnel=instructor,
                    notification_type="departure_reminder",
                    message_id=str(message.message_id) if message else "",
                    success=telegram_ok,
                    error_text=error or "",
                )

        day = booking.days.filter(date=on_date).select_related("instructor").first()
        day_instructor = day.instructor if day and day.instructor_id else None
        lead_id = instructor.pk if instructor else None
        if (
            qr_photos
            and day_instructor
            and day_instructor.pk != lead_id
            and (day_instructor.telegram_chat_id or "").strip()
            and personnel_wants_notification(day_instructor, "departure_reminder", channel="telegram")
        ):
            day_message, day_error = send_telegram_message(
                day_instructor.telegram_chat_id,
                format_qr_followup_message(booking),
            )
            day_ok = day_message is not None
            if day_ok:
                send_booking_qr_photos(day_instructor.telegram_chat_id, booking, on_date)
            TelegramNotification.objects.create(
                booking=booking,
                personnel=day_instructor,
                notification_type="departure_reminder",
                message_id=str(day_message.message_id) if day_message else "",
                success=day_ok,
                error_text=day_error or "",
            )
            telegram_ok = telegram_ok or day_ok

    email_ok = notify_instructor_via_email(
        booking,
        "departure_reminder",
        on_date=on_date,
        morning_today=morning_today,
        travel_seconds=travel_seconds,
    )
    return telegram_ok or email_ok


def notify_upcoming_booking_reminder(booking, *, offset_days, first_date):
    from .telegram_today import format_upcoming_booking_reminder_message
    from .upcoming_booking_reminders import upcoming_notification_type

    if not should_notify_booking(booking):
        return False

    instructor = booking.instructor
    notification_type = upcoming_notification_type(offset_days)
    telegram_ok = False

    if (settings.TELEGRAM_BOT_TOKEN or "").strip() and instructor and (instructor.telegram_chat_id or "").strip():
        if personnel_wants_notification(instructor, notification_type, channel="telegram"):
            text = format_upcoming_booking_reminder_message(
                booking,
                offset_days=offset_days,
                first_date=first_date,
            )
            message, error = send_telegram_message(instructor.telegram_chat_id, text)
            telegram_ok = message is not None
            TelegramNotification.objects.create(
                booking=booking,
                personnel=instructor,
                notification_type=notification_type,
                message_id=str(message.message_id) if message else "",
                success=telegram_ok,
                error_text=error or "",
            )

    email_ok = notify_instructor_via_email(
        booking,
        notification_type,
        offset_days=offset_days,
        first_date=first_date,
    )
    return telegram_ok or email_ok
