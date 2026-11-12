import asyncio
import base64
import json
import logging
import secrets

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import Personnel
from .utils.register_links import qr_png_bytes
from .utils.telegram_bot import process_webhook_update
from .utils.telegram_link import create_link_token

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def telegram_webhook(request, secret: str):
    """
    Telegram Bot API webhook (production).
    Registered via `python manage.py telegram_set_webhook` on deploy.
    """
    expected = (settings.TELEGRAM_WEBHOOK_SECRET or "").strip()
    if not expected or not secrets.compare_digest(secret, expected):
        return HttpResponseForbidden("invalid secret")

    header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not header or not secrets.compare_digest(header, expected):
        return HttpResponseForbidden("invalid secret token")

    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponse("bad json", status=400)

    try:
        asyncio.run(process_webhook_update(payload))
    except Exception:
        logger.exception("Telegram webhook processing failed")
        return HttpResponse("error", status=500)

    return HttpResponse("ok")


@login_required
def telegram_link_token(request):
    try:
        personnel = request.user.personnel
    except Personnel.DoesNotExist:
        return JsonResponse({"error": "No personnel profile."}, status=400)

    bot_username = (settings.TELEGRAM_BOT_USERNAME or "").strip().lstrip("@")
    if not bot_username:
        return JsonResponse({"error": "TELEGRAM_BOT_USERNAME is not configured."}, status=503)

    token = create_link_token(personnel.pk)
    deep_link = f"https://t.me/{bot_username}?start={token}"

    qr_data_url = "data:image/png;base64," + base64.b64encode(qr_png_bytes(deep_link)).decode("ascii")

    return JsonResponse(
        {
            "deep_link": deep_link,
            "qr_data_url": qr_data_url,
            "linked": bool((personnel.telegram_chat_id or "").strip()),
            "linked_at": personnel.telegram_linked_at.isoformat() if personnel.telegram_linked_at else None,
        }
    )


@login_required
@require_POST
def telegram_unlink(request):
    try:
        personnel = request.user.personnel
    except Personnel.DoesNotExist:
        return redirect("user_profile")

    personnel.telegram_chat_id = ""
    personnel.telegram_linked_at = None
    personnel.save(update_fields=["telegram_chat_id", "telegram_linked_at"])
    messages.success(request, "Telegram account unlinked.")
    return redirect("user_profile")
