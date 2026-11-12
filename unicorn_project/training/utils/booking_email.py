"""Send instructor booking notification emails with course pack PDF."""

from __future__ import annotations

import logging

from django.conf import settings

from ..models import EmailNotification
from .booking_email_content import (
    booking_email_subject,
    format_booking_email_html,
    format_booking_email_plain,
)
from .booking_notification_pdf import render_booking_notification_pdf
from .emailing import send_pdf_to_booking_contacts

logger = logging.getLogger(__name__)


def instructor_email_address(personnel) -> str | None:
    if not personnel:
        return None
    direct = (personnel.email or "").strip()
    if direct:
        return direct
    user = getattr(personnel, "user", None)
    if user:
        return (user.email or "").strip() or None
    return None


def notify_instructor_via_email(
    booking,
    notification_type: str,
    *,
    intro=None,
    changed_areas=None,
    force=False,
    on_date=None,
    offset_days=None,
    first_date=None,
    travel_seconds=None,
    buffer_minutes=None,
    morning_today=False,
    attach_pdf=True,
    recipient=None,
) -> bool:
    """
    Email the booking instructor with HTML body and optional course pack PDF.
    Returns True when the message was sent successfully.
    """
    from .notification_prefs import personnel_wants_notification, should_notify_booking

    if not should_notify_booking(booking):
        return False

    instructor = recipient or booking.instructor
    to_addr = instructor_email_address(instructor)
    if not to_addr:
        return False

    if not force and not personnel_wants_notification(instructor, notification_type, channel="email"):
        return False

    subject = booking_email_subject(notification_type, booking)
    plain = format_booking_email_plain(
        booking,
        notification_type,
        intro=intro,
        changed_areas=changed_areas,
        on_date=on_date,
        offset_days=offset_days,
        first_date=first_date,
        travel_seconds=travel_seconds,
        buffer_minutes=buffer_minutes,
        morning_today=morning_today,
    )
    html = format_booking_email_html(
        booking,
        notification_type,
        intro=intro,
        changed_areas=changed_areas,
        on_date=on_date,
        offset_days=offset_days,
        first_date=first_date,
        travel_seconds=travel_seconds,
        buffer_minutes=buffer_minutes,
        morning_today=morning_today,
    )

    attachments = []
    if attach_pdf:
        try:
            pdf_bytes, filename, mime = render_booking_notification_pdf(
                booking,
                on_date=on_date or first_date,
            )
            attachments.append((filename, pdf_bytes, mime))
        except Exception as exc:
            logger.exception("Course pack PDF failed for booking %s", booking.pk)
            plain += f"\n\n(PDF attachment could not be generated: {exc})"

    provider_message_id = ""
    success = False
    error_text = ""

    try:
        send_pdf_to_booking_contacts(
            subject=subject,
            body=html,
            to=[to_addr],
            attachments=attachments,
            html=True,
        )
        success = True
    except Exception as exc:
        error_text = str(exc)
        logger.exception("Booking email failed for %s (%s)", booking.pk, notification_type)

    EmailNotification.objects.create(
        booking=booking,
        personnel=instructor,
        notification_type=notification_type,
        provider_message_id=provider_message_id,
        success=success,
        error_text=error_text,
    )
    return success
