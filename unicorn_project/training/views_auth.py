from django.conf import settings
from django.contrib.auth import authenticate, login, get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.contrib import messages
from django.contrib.auth.views import PasswordChangeView
from django.core import signing
from django.core.mail import send_mail
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.views.decorators.http import require_POST
import hashlib
import json
import pyotp
import base64
import secrets
import time

from unicorn_project.training.models import Personnel
from unicorn_project.training.utils.passwords import send_initial_password_email
from unicorn_project.training.utils.register_links import qr_png_bytes

# Cookie name for trusted devices
_TRUST_COOKIE = "2fa_trusted"
# Number of days a trusted device is remembered
_TRUST_DAYS = 7
# Number of backup codes to generate
_BACKUP_CODE_COUNT = 8
# Email code: expiry in seconds (10 minutes)
_EMAIL_CODE_TTL = 600
# Email code: minimum seconds between resend requests
_EMAIL_CODE_RATE_LIMIT = 60


def _hash_backup_code(code):
    """One-way hash a backup code for safe storage."""
    return hashlib.sha256(code.encode()).hexdigest()


def _generate_backup_codes():
    """Generate a list of readable backup codes like XXXX-XXXX."""
    codes = []
    for _ in range(_BACKUP_CODE_COUNT):
        part1 = secrets.token_hex(2).upper()
        part2 = secrets.token_hex(2).upper()
        codes.append(f"{part1}-{part2}")
    return codes


def _save_backup_codes(personnel, plain_codes):
    """Hash and store backup codes on the personnel record."""
    hashed = [_hash_backup_code(c) for c in plain_codes]
    personnel.totp_backup_codes = json.dumps(hashed)
    personnel.save(update_fields=['totp_backup_codes'])


def _use_backup_code(personnel, code):
    """Attempt to consume a backup code. Returns True if valid, False otherwise."""
    if not personnel.totp_backup_codes:
        return False
    try:
        hashed_list = json.loads(personnel.totp_backup_codes)
    except (json.JSONDecodeError, TypeError):
        return False
    code_hash = _hash_backup_code(code.strip().upper())
    if code_hash in hashed_list:
        hashed_list.remove(code_hash)
        personnel.totp_backup_codes = json.dumps(hashed_list)
        personnel.save(update_fields=['totp_backup_codes'])
        return True
    return False


def _send_email_code(request, user, personnel):
    """
    Generate a 6-digit code, store it (hashed) in session with expiry, and email it.
    Returns (True, None) on success or (False, error_message) on rate-limit/error.
    """
    now = int(time.time())
    last_sent = request.session.get('2fa_email_sent_at', 0)
    if now - last_sent < _EMAIL_CODE_RATE_LIMIT:
        remaining = _EMAIL_CODE_RATE_LIMIT - (now - last_sent)
        return False, f"Please wait {remaining} seconds before requesting another code."

    plain_code = ''.join([str(secrets.randbelow(10)) for _ in range(6)])
    code_hash = hashlib.sha256(plain_code.encode()).hexdigest()

    request.session['2fa_email_code_hash'] = code_hash
    request.session['2fa_email_code_expiry'] = now + _EMAIL_CODE_TTL
    request.session['2fa_email_sent_at'] = now

    # Build recipient (respects DEV_CATCH_ALL_EMAIL like other emails)
    intended_email = personnel.email
    if settings.DEBUG and getattr(settings, 'DEV_CATCH_ALL_EMAIL', None):
        to_email = settings.DEV_CATCH_ALL_EMAIL
        subject_prefix = f"[INTENDED: {intended_email}] "
    else:
        to_email = intended_email
        subject_prefix = ""

    try:
        send_mail(
            subject_prefix + "Your login verification code",
            (
                f"Hello {personnel.name or user.username},\n\n"
                f"Your login verification code is:\n\n"
                f"  {plain_code}\n\n"
                f"This code expires in 10 minutes. Do not share it with anyone.\n\n"
                "If you did not attempt to log in, please contact support immediately.\n\n"
                "Regards,\nUnicorn Admin System"
            ),
            settings.DEFAULT_FROM_EMAIL,
            [to_email],
        )
        return True, None
    except Exception as e:
        return False, f"Failed to send email: {str(e)}"


def _verify_email_code(request, code):
    """Check submitted code against session. Returns True if valid, False otherwise."""
    stored_hash = request.session.get('2fa_email_code_hash')
    expiry = request.session.get('2fa_email_code_expiry', 0)
    if not stored_hash or int(time.time()) > expiry:
        return False
    submitted_hash = hashlib.sha256(code.strip().encode()).hexdigest()
    if secrets.compare_digest(stored_hash, submitted_hash):
        # Consume the code
        request.session.pop('2fa_email_code_hash', None)
        request.session.pop('2fa_email_code_expiry', None)
        return True
    return False


def _is_device_trusted(request, user):
    """Check if the current browser has a valid trust cookie for this user."""
    cookie_val = request.COOKIES.get(_TRUST_COOKIE)
    if not cookie_val:
        return False
    try:
        data = signing.loads(cookie_val, max_age=_TRUST_DAYS * 86400, salt="2fa_trust")
        return data.get("user_id") == user.id
    except signing.BadSignature:
        return False
    except Exception:
        return False


def _set_trust_cookie(response, user):
    """Set a signed trust cookie on the response."""
    value = signing.dumps({"user_id": user.id}, salt="2fa_trust")
    response.set_cookie(
        _TRUST_COOKIE,
        value,
        max_age=_TRUST_DAYS * 86400,
        httponly=True,
        samesite="Lax",
        secure=False,  # Set True in production with HTTPS
    )
    return response


def _queue_two_factor_prompt(request, user):
    """Queue 2FA prompt in session if user does NOT have 2FA enabled."""
    try:
        personnel = user.personnel
        # Show prompt only if user does NOT have 2FA yet
        if not personnel.totp_secret:
            request.session["show_2fa_prompt"] = True
        else:
            request.session["show_2fa_prompt"] = False
    except Personnel.DoesNotExist:
        request.session["show_2fa_prompt"] = True


def _queue_release_notes_prompt(request, user):
    """Queue what's-new modal after login unless the user dismissed this release."""
    from .release_notes import RELEASE_NOTES_ID

    session_key = f"dismissed_release_notes_{RELEASE_NOTES_ID}"
    try:
        personnel = user.personnel
        if personnel.dismissed_release_notes_id == RELEASE_NOTES_ID:
            request.session["show_release_notes"] = False
            return
    except Personnel.DoesNotExist:
        if request.session.get(session_key):
            request.session["show_release_notes"] = False
            return

    request.session["show_release_notes"] = True


def _queue_post_login_prompts(request, user):
    _queue_two_factor_prompt(request, user)
    _queue_release_notes_prompt(request, user)


def custom_login(request):
    """Custom login view with 2FA support."""
    if request.user.is_authenticated:
        return HttpResponseRedirect(reverse("home"))
    
    if request.method == "POST":
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            
            # Check if user has 2FA enabled
            try:
                personnel = user.personnel
                if personnel.totp_secret:
                    # Skip 2FA if this device is trusted
                    if _is_device_trusted(request, user):
                        login(request, user)
                        _queue_post_login_prompts(request, user)
                        return HttpResponseRedirect(reverse("post_login"))
                    # Store user ID and backend in session and redirect to 2FA verification
                    request.session['2fa_user_id'] = user.id
                    request.session['2fa_backend'] = user.backend
                    return HttpResponseRedirect(reverse("two_factor_auth"))
            except Personnel.DoesNotExist:
                pass
            
            # No 2FA, log in normally
            request.session.pop('2fa_user_id', None)
            request.session.pop('2fa_backend', None)
            login(request, user)
            _queue_post_login_prompts(request, user)
            return HttpResponseRedirect(reverse("post_login"))
    else:
        form = AuthenticationForm(request)
    
    return render(request, "registration/login.html", {"form": form})


def _complete_2fa_login(request, user, trust_device=False):
    """Finish login after successful 2FA, optionally setting the trust cookie."""
    backend = request.session.pop('2fa_backend', 'unicorn_project.training.auth_backends.EmailBackend')
    user.backend = backend
    login(request, user)
    request.session.pop('2fa_user_id', None)
    _queue_post_login_prompts(request, user)
    response = HttpResponseRedirect(reverse("post_login"))
    if trust_device:
        _set_trust_cookie(response, user)
    return response


def two_factor_auth(request):
    """2FA verification page - accept TOTP code or a backup code."""
    user_id = request.session.get('2fa_user_id')
    if not user_id:
        return HttpResponseRedirect(reverse("login"))
    
    User = get_user_model()
    try:
        user = User.objects.get(id=user_id)
        personnel = user.personnel
    except (User.DoesNotExist, Personnel.DoesNotExist):
        request.session.pop('2fa_user_id', None)
        messages.error(request, "User not found. Please log in again.")
        return HttpResponseRedirect(reverse("login"))
    
    # Check that user still has 2FA enabled
    if not personnel.totp_secret:
        request.session.pop('2fa_user_id', None)
        messages.warning(request, "2FA is not enabled for this account.")
        return HttpResponseRedirect(reverse("login"))
    
    has_backup_codes = bool(personnel.totp_backup_codes and json.loads(personnel.totp_backup_codes or '[]'))
    email_pending = bool(request.session.get('2fa_email_code_hash'))
    ctx = {
        "username": user.username,
        "has_backup_codes": has_backup_codes,
        "user_email_masked": _mask_email(personnel.email),
        "email_pending": email_pending,
    }

    if request.method == "POST":
        code = (request.POST.get("code") or "").strip()
        trust_device = request.POST.get("trust_device") == "on"
        mode = request.POST.get("mode", "totp")  # totp | backup | email

        if not code:
            messages.error(request, "Please enter a code.")
            return render(request, "two_factor/auth.html", {**ctx, "mode": mode})

        if mode == "backup":
            if _use_backup_code(personnel, code):
                remaining = len(json.loads(personnel.totp_backup_codes or '[]'))
                messages.success(request, f"Backup code accepted. {remaining} backup code(s) remaining.")
                return _complete_2fa_login(request, user, trust_device)
            else:
                messages.error(request, "Invalid backup code. Please try again.")
                return render(request, "two_factor/auth.html", {**ctx, "mode": "backup"})

        elif mode == "email":
            if _verify_email_code(request, code):
                return _complete_2fa_login(request, user, trust_device)
            else:
                if not request.session.get('2fa_email_code_hash'):
                    messages.error(request, "That code has expired. Request a new one.")
                else:
                    messages.error(request, "Invalid code. Please try again.")
                return render(request, "two_factor/auth.html", {**ctx, "mode": "email"})

        else:
            # Standard TOTP code
            if len(code) != 6 or not code.isdigit():
                messages.error(request, "Code must be 6 digits.")
                return render(request, "two_factor/auth.html", ctx)
            totp = pyotp.TOTP(personnel.totp_secret)
            try:
                if totp.verify(code, valid_window=1):
                    return _complete_2fa_login(request, user, trust_device)
                else:
                    messages.error(request, "Invalid code. Please check your authenticator app and try again.")
                    return render(request, "two_factor/auth.html", ctx)
            except Exception as e:
                messages.error(request, f"Authentication error: {str(e)}")
                return render(request, "two_factor/auth.html", ctx)

    mode = request.GET.get("mode", "totp")
    return render(request, "two_factor/auth.html", {**ctx, "mode": mode})


def _mask_email(email):
    """Return a masked version of an email, e.g. jo**@example.com"""
    try:
        local, domain = email.split("@", 1)
        visible = local[:2] if len(local) >= 2 else local[:1]
        return f"{visible}{'*' * (len(local) - len(visible))}@{domain}"
    except Exception:
        return "your registered email"


def request_email_2fa_code(request):
    """Send a one-time email code for 2FA. Must have an active 2FA session."""
    user_id = request.session.get('2fa_user_id')
    if not user_id:
        return JsonResponse({"ok": False, "error": "No active 2FA session."}, status=403)

    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required."}, status=405)

    User = get_user_model()
    try:
        user = User.objects.get(id=user_id)
        personnel = user.personnel
    except (User.DoesNotExist, Personnel.DoesNotExist):
        return JsonResponse({"ok": False, "error": "User not found."}, status=404)

    ok, error = _send_email_code(request, user, personnel)
    if ok:
        return JsonResponse({"ok": True, "masked_email": _mask_email(personnel.email)})
    else:
        return JsonResponse({"ok": False, "error": error}, status=429)


class ForcePasswordChangeView(PasswordChangeView):
    """
    Runs the normal password change, then clears staff_profile.must_change_password
    so users can proceed past the gate.
    """
    success_url = reverse_lazy('password_change_done')

    def form_valid(self, form):
        response = super().form_valid(form)
        prof = getattr(self.request.user, 'personnel', None)
        if prof and getattr(prof, 'must_change_password', False):
            prof.must_change_password = False
            prof.save(update_fields=['must_change_password'])
        messages.success(self.request, "Password changed successfully.")
        return response


def forgot_password(request):
    """Generate and email a new temporary password.

    Response is always generic so we don't leak whether an email exists.
    """
    if request.method == "POST":
        email = (request.POST.get("email") or "").strip()

        if email:
            prof = Personnel.objects.filter(email__iexact=email).select_related("user").first()
            if prof and prof.user:
                temp_password = get_random_string(12)

                prof.user.set_password(temp_password)
                prof.user.is_active = True
                prof.user.save(update_fields=["password", "is_active"])

                prof.must_change_password = True
                prof.save(update_fields=["must_change_password"])

                send_initial_password_email(prof, temp_password)

        messages.success(
            request,
            "If an account exists for that email address, a new temporary password has been sent."
        )
        return HttpResponseRedirect(reverse("login"))

    return render(request, "registration/forgot_password.html")


def _totp_setup_context(email, secret):
    uri = pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name="Pegasus Training")
    png = qr_png_bytes(uri)
    return {
        "provisioning_uri": uri,
        "secret": secret,
        "qr_data_url": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
    }


def two_factor_setup(request):
    """Setup 2FA for the logged-in user."""
    if not request.user.is_authenticated:
        return HttpResponseRedirect(reverse("login"))
    
    try:
        personnel = request.user.personnel
    except Personnel.DoesNotExist:
        messages.error(request, "Personnel record not found.")
        return HttpResponseRedirect(reverse("user_profile"))
    
    # Check if user already has 2FA enabled
    if personnel.totp_secret:
        messages.warning(request, "2FA is already enabled for your account. Disable it first if you want to set up a new code.")
        return HttpResponseRedirect(reverse("user_profile"))
    
    # Clear the 2FA prompt since user is setting up 2FA
    request.session.pop('show_2fa_prompt', None)
    
    if request.method == "POST":
        # User is verifying the code
        confirm_code = (request.POST.get("code") or "").strip()
        temp_secret = request.session.get('2fa_temp_secret')
        
        if not temp_secret:
            messages.error(request, "Session expired. Please try again.")
            return HttpResponseRedirect(reverse("two_factor_setup"))
        
        if not confirm_code or len(confirm_code) != 6 or not confirm_code.isdigit():
            messages.error(request, "Please enter a valid 6-digit code.")
            # Regenerate QR for display
            return render(request, "two_factor/setup.html", _totp_setup_context(request.user.email, temp_secret))
        
        # Verify the code with temp secret
        totp = pyotp.TOTP(temp_secret)
        try:
            if totp.verify(confirm_code, valid_window=1):
                # Code is valid - save the secret and generate backup codes
                plain_codes = _generate_backup_codes()
                personnel.totp_secret = temp_secret
                personnel.totp_backup_codes = json.dumps([_hash_backup_code(c) for c in plain_codes])
                personnel.save(update_fields=['totp_secret', 'totp_backup_codes'])
                request.session.pop('2fa_temp_secret', None)
                # Show backup codes to user once
                return render(request, "two_factor/backup_codes.html", {
                    'backup_codes': plain_codes,
                    'just_enabled': True,
                })
            else:
                messages.error(request, "Invalid code. Please try again with the code displayed in your authenticator app.")
                return render(request, "two_factor/setup.html", _totp_setup_context(request.user.email, temp_secret))
        except Exception as e:
            messages.error(request, f"Error verifying code: {str(e)}")
            return render(request, "two_factor/setup.html", _totp_setup_context(request.user.email, temp_secret))
    
    # GET request - generate a new secret if not already in session
    temp_secret = request.session.get('2fa_temp_secret')
    if not temp_secret:
        temp_secret = pyotp.random_base32()
        request.session['2fa_temp_secret'] = temp_secret
    
    return render(request, "two_factor/setup.html", _totp_setup_context(request.user.email, temp_secret))


def two_factor_verify(request):
    """Verify and enable 2FA (alternate entry point - primarily for setup flow)."""
    if not request.user.is_authenticated:
        return HttpResponseRedirect(reverse("login"))
    
    # Just redirect to setup since verify is part of the setup process
    return HttpResponseRedirect(reverse("two_factor_setup"))


def dismiss_2fa_prompt(request):
    """Clear the 2FA prompt from session so it only shows once per login."""
    if request.method == "POST":
        request.session.pop("show_2fa_prompt", None)
        return JsonResponse({"ok": True})
    return JsonResponse({"ok": False}, status=405)


@require_POST
def dismiss_release_notes(request):
    """Dismiss the what's-new modal for this login or permanently."""
    from .release_notes import RELEASE_NOTES_ID

    if not request.user.is_authenticated:
        return JsonResponse({"ok": False}, status=403)

    permanent = (request.POST.get("permanent") or "").strip() == "1"
    if permanent:
        session_key = f"dismissed_release_notes_{RELEASE_NOTES_ID}"
        try:
            personnel = request.user.personnel
            personnel.dismissed_release_notes_id = RELEASE_NOTES_ID
            personnel.save(update_fields=["dismissed_release_notes_id"])
        except Personnel.DoesNotExist:
            request.session[session_key] = True

    request.session.pop("show_release_notes", None)
    return JsonResponse({"ok": True})


def regenerate_backup_codes(request):
    """Generate a new set of backup codes, replacing the old ones."""
    if not request.user.is_authenticated:
        return HttpResponseRedirect(reverse("login"))
    
    try:
        personnel = request.user.personnel
    except Personnel.DoesNotExist:
        messages.error(request, "Personnel record not found.")
        return HttpResponseRedirect(reverse("user_profile"))
    
    if not personnel.totp_secret:
        messages.warning(request, "2FA is not enabled for your account.")
        return HttpResponseRedirect(reverse("user_profile"))
    
    if request.method == "POST":
        plain_codes = _generate_backup_codes()
        _save_backup_codes(personnel, plain_codes)
        return render(request, "two_factor/backup_codes.html", {
            'backup_codes': plain_codes,
            'just_enabled': False,
        })
    
    return render(request, "two_factor/backup_codes_confirm.html")


def two_factor_disable(request):
    """Disable 2FA for the logged-in user."""
    if not request.user.is_authenticated:
        return HttpResponseRedirect(reverse("login"))
    
    try:
        personnel = request.user.personnel
    except Personnel.DoesNotExist:
        messages.error(request, "Personnel record not found.")
        return HttpResponseRedirect(reverse("user_profile"))
    
    if not personnel.totp_secret:
        messages.warning(request, "2FA is not enabled for your account.")
        return HttpResponseRedirect(reverse("user_profile"))
    
    if request.method == "POST":
        # Confirm disable
        confirm = request.POST.get("confirm")
        if confirm == "yes":
            personnel.totp_secret = None
            personnel.totp_backup_codes = None
            personnel.save(update_fields=['totp_secret', 'totp_backup_codes'])
            messages.success(request, "2FA has been disabled for your account.")
            return HttpResponseRedirect(reverse("user_profile"))
        else:
            return HttpResponseRedirect(reverse("user_profile"))
    
    # Show confirmation page
    return render(request, "two_factor/disable.html")

