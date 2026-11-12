from django.shortcuts import render, redirect, get_object_or_404
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Group
from django.db.models import Q
from django.http import JsonResponse, HttpResponseForbidden, FileResponse, HttpResponse, HttpResponseBadRequest
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.shortcuts import redirect, render, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_GET, require_POST
from django.template.loader import render_to_string
from urllib.parse import urlencode
from .models import AccidentReport, Business, TrainingLocation, CourseType, Personnel, Booking, BookingDay, DelegateRegister, FeedbackResponse, InstructorCourseRequestStatus
from .forms import (
    AccidentReportForm,
    PublicAccidentReportForm,
    BookingForm,
    PersonnelAdminForm,
    PersonnelProfileForm,
    DelegateRegisterForm,
    PublicDelegateRegisterForm,
    FeedbackForm,
    PublicFeedbackForm,
    delivery_personnel_queryset,
)
from .forms_profile import UserProfileForm, PersonnelProfileForm
from .signal_control import disable, enable

from datetime import timedelta, datetime, date
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE as SHAPE
from pptx.enum.text import PP_PARAGRAPH_ALIGNMENT
from pptx.dml.color import RGBColor
from pptx.util import Inches, Pt
from reportlab.lib.pagesizes import A4, portrait
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
import uuid, io

# NEW: a shape enum that works across python-pptx versions
try:
    # newer python-pptx
    from pptx.enum.shapes import MSO_SHAPE as SHAPE
except Exception:
    # older python-pptx
    from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE as SHAPE

# --- helpers --------------------------------------------------

def is_instructor_user(user):
    return user.is_authenticated and (
        user.groups.filter(name__iexact="instructor").exists()
        or hasattr(user, "instructor")
    )

def get_instructor_for_user(user):
    """
    Resolve the Instructor profile for this user.
    If you allow multiple mappings, adjust here; otherwise one-to-one.
    """
    try:
        if hasattr(user, "instructor") and user.instructor:
            return user.instructor
    except Instructor.DoesNotExist:
        pass
    # fallback: name/email match, if you’ve used that pattern in your data
    return Personnel.objects.filter(user=user).first()

from datetime import datetime

def _parse_yyyy_mm_dd(s: str):
    """
    Convert 'YYYY-MM-DD' → date object.
    Returns None if invalid.
    """
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_partial_date_from_post(post, prefix: str):
    day = (post.get(f"{prefix}_day") or "").strip()
    month = (post.get(f"{prefix}_month") or "").strip()
    year = (post.get(f"{prefix}_year") or "").strip()
    if not (day and month and year and len(year) == 4):
        return None
    try:
        return date(int(year), int(month), int(day))
    except (TypeError, ValueError):
        return None


_ACTIVE_BOOKING_STATUSES = ("scheduled", "in_progress", "awaiting_closure", "completed")


def _register_dev_mode() -> bool:
    return getattr(settings, "REGISTER_SHOW_DATE", settings.DEBUG)


def _instructors_for_course_date(course, day_date, *, dev_mode=None):
    """Personnel delivering *course* on *day_date* (booking- or day-level instructor)."""
    if not course or not day_date:
        return Personnel.objects.none()
    if dev_mode is None:
        dev_mode = _register_dev_mode()

    q_day = Q(
        booking_days__booking__course_type=course,
        booking_days__date=day_date,
    )
    # The booking lead is listed only when that day has nobody else assigned.
    q_lead_when_unset = Q(
        bookings__course_type=course,
        bookings__days__date=day_date,
        bookings__days__instructor__isnull=True,
    )
    if not dev_mode:
        q_day &= Q(booking_days__booking__status__in=_ACTIVE_BOOKING_STATUSES)
        q_lead_when_unset &= Q(bookings__status__in=_ACTIVE_BOOKING_STATUSES)

    return (
        Personnel.objects.filter(q_day | q_lead_when_unset)
        .distinct()
        .order_by("name")
    )


def _dev_instructor_register_hint(course, day_date) -> str:
    """Dev-only hint when other course types have sessions on the same date."""
    if not _register_dev_mode() or not course or not day_date:
        return ""
    days = (
        BookingDay.objects.filter(date=day_date)
        .exclude(booking__course_type=course)
        .select_related("booking__course_type", "booking__instructor", "instructor")
    )
    if not days.exists():
        return ""

    parts = []
    seen = set()
    for bd in days.order_by("booking__course_type__code"):
        ct = bd.booking.course_type
        if ct.pk in seen:
            continue
        seen.add(ct.pk)
        inst = bd.instructor or bd.booking.instructor
        inst_name = inst.name if inst else "TBC"
        parts.append(f"{ct.code} ({inst_name})")
    return "Other courses on this date: " + ", ".join(parts)


def _resolve_public_register_instructors(course, day_date):
    """
    Instructors for the public register form.
    Falls back to all active delivery personnel when none are scheduled
    for the course on the given date.
    Returns (instructor_list, used_fallback).
    """
    scheduled = list(_instructors_for_course_date(course, day_date))
    if scheduled:
        return scheduled, False
    return list(delivery_personnel_queryset()), True


def _booking_day_for_public_register(course, reg_date, instructor, *, instructor_fallback=False):
    """Find the BookingDay row to attach a public delegate registration to."""
    from .utils.public_sessions import resolve_booking_day

    return resolve_booking_day(
        course,
        reg_date,
        instructor,
        instructor_fallback=instructor_fallback,
    )


def _public_matching_sessions(course, day_date, single_instructor, request, *, url_day_code=""):
    """Sessions for the picker when one QR covers multiple slots the same day."""
    if url_day_code or not course or not day_date:
        return []
    instructor = single_instructor
    if not instructor:
        inst_id = (request.GET.get("instructor") or "").strip()
        if not inst_id and request.method == "POST":
            inst_id = (request.POST.get("instructor") or "").strip()
        if inst_id:
            instructor = Personnel.objects.filter(pk=inst_id).first()
    if not instructor:
        return []
    from .utils.public_sessions import session_choices

    return session_choices(course, day_date, instructor)

# --- home redirect --------------------------------------------

def _redirect_to_user_dashboard(request):
    from .utils.user_roles import (
        dashboard_url_name_for_role,
        resolve_active_role,
        set_active_role,
    )

    role = resolve_active_role(request.user, request.session)
    if not role:
        return redirect("no_roles")

    set_active_role(request.session, role)
    return redirect(dashboard_url_name_for_role(role))


@login_required
def home(request):
    return _redirect_to_user_dashboard(request)

@require_GET
def public_delegate_instructors_api(request):
    """
    JSON:  /register/instructors?ct=EFAAW&date=2025-10-21
    Returns [{id, name}, ...] for the instructor select.
    """
    code = (request.GET.get("ct") or request.GET.get("code") or request.GET.get("course") or "").strip()
    date_str = (request.GET.get("date") or request.GET.get("d") or "").strip()
    day_date = _parse_yyyy_mm_dd(date_str) or timezone.localdate()

    course = CourseType.objects.filter(code__iexact=code).first()
    if not course:
        return JsonResponse({"instructors": []})

    instructors, instructor_fallback = _resolve_public_register_instructors(course, day_date)
    payload = {
        "instructors": [{"id": i.id, "name": i.name} for i in instructors],
        "fallback": instructor_fallback,
    }
    if not instructor_fallback:
        hint = _dev_instructor_register_hint(course, day_date)
        if hint:
            payload["dev_hint"] = hint
    return JsonResponse(payload)


@require_GET
def public_delegate_sessions_api(request):
    """
    JSON: /register/sessions/?ct=SA&date=2026-07-13&instructor=<uuid>
    Returns session choices when the same course runs more than once on one day.
    """
    if (request.GET.get("day") or "").strip():
        return JsonResponse({"sessions": []})

    code = (request.GET.get("ct") or request.GET.get("code") or request.GET.get("course") or "").strip()
    date_str = (request.GET.get("date") or request.GET.get("d") or "").strip()
    inst_id = (request.GET.get("instructor") or "").strip()
    day_date = _parse_yyyy_mm_dd(date_str) or timezone.localdate()

    course = CourseType.objects.filter(code__iexact=code).first()
    if not course or not inst_id:
        return JsonResponse({"sessions": []})

    instructor = Personnel.objects.filter(pk=inst_id).first()
    if not instructor:
        return JsonResponse({"sessions": []})

    from .utils.public_sessions import session_choices

    sessions = session_choices(course, day_date, instructor)
    return JsonResponse({
        "sessions": [
            {"day_code": s["day_code"], "label": s["label"]}
            for s in sessions
        ],
    })


def _course_type_by_register_code(code: str):
    return CourseType.objects.filter(
        code__iexact=(code or "").strip(),
        is_suspended=False,
    ).first()


@require_GET
def public_register_short(request, code):
    """Short URL for delegate registration: /r/<course_code>/ → /register/?ct=…"""
    course = _course_type_by_register_code(code)
    if not course:
        return HttpResponse("Course not found.", status=404)
    target = f"{reverse('public_delegate_register')}?{urlencode({'ct': course.code})}"
    return redirect(target)


@require_GET
def public_register_short_qr(request, code):
    """PNG QR code encoding the short registration URL for a course type."""
    from .utils.register_links import course_register_short_url, qr_png_bytes

    course = _course_type_by_register_code(code)
    if not course:
        return HttpResponse(status=404)
    png = qr_png_bytes(course_register_short_url(request, course.code))
    return HttpResponse(png, content_type="image/png")


def _course_type_by_code(code: str):
    return CourseType.objects.filter(code__iexact=(code or "").strip()).first()


@require_GET
def public_feedback_short(request, code):
    """Short URL for course feedback: /f/<course_code>/ → /feedback/?course=…"""
    course = _course_type_by_code(code)
    if not course:
        return HttpResponse("Course not found.", status=404)
    target = f"{reverse('public_feedback_form')}?{urlencode({'course': course.code})}"
    return redirect(target)


@require_GET
def public_feedback_short_qr(request, code):
    """PNG QR code encoding the short feedback URL for a course type."""
    from .utils.feedback_links import course_feedback_short_url, qr_png_bytes

    course = _course_type_by_code(code)
    if not course:
        return HttpResponse(status=404)
    png = qr_png_bytes(course_feedback_short_url(request, course.code))
    return HttpResponse(png, content_type="image/png")


def _exam_by_code(code: str):
    from .models import Exam

    return Exam.objects.filter(exam_code__iexact=(code or "").strip()).first()


@require_GET
def public_exam_short(request, code):
    """Short URL for delegate exam entry: /e/<exam_code>/ → /exam/?examcode=…"""
    exam = _exam_by_code(code)
    if not exam:
        return HttpResponse("Exam not found.", status=404)
    target = f"{reverse('delegate_exam_start')}?{urlencode({'examcode': exam.exam_code})}"
    return redirect(target)


@require_GET
def public_exam_short_qr(request, code):
    """PNG QR code encoding the short exam URL for an exam."""
    from .utils.exam_links import exam_short_url
    from .utils.register_links import qr_png_bytes

    exam = _exam_by_code(code)
    if not exam:
        return HttpResponse(status=404)
    png = qr_png_bytes(exam_short_url(request, exam.exam_code))
    return HttpResponse(png, content_type="image/png")


def _course_type_with_accident_reports(code: str):
    from .utils.online_exercises import course_types_with_accident_reports

    return course_types_with_accident_reports().filter(code__iexact=(code or "").strip()).first()


@require_GET
def public_accident_report_short(request, code):
    """Short URL for accident reports: /ar/<course_code>/ → form with course pre-selected."""
    course = _course_type_with_accident_reports(code)
    if not course:
        return HttpResponse("Course not found or accident reports are not enabled.", status=404)
    target = f"{reverse('accident_report_public')}?{urlencode({'course': course.code})}"
    return redirect(target)


@require_GET
def public_accident_report_short_qr(request, code):
    """PNG QR code encoding the short accident report URL for a course type."""
    from .utils.accident_report_links import course_accident_report_short_url
    from .utils.register_links import qr_png_bytes

    course = _course_type_with_accident_reports(code)
    if not course:
        return HttpResponse(status=404)
    png = qr_png_bytes(course_accident_report_short_url(request, course.code))
    return HttpResponse(png, content_type="image/png")


def public_delegate_register(request):
    """
    Public delegate register page.
    - Optional ?ct=<COURSE_CODE> to preselect a course
    - Course date is always today (hidden from the delegate)
    - Instructor list is restricted to instructors who have a booking
      for the selected course and today's date.
    """

    # 0) Optional exact day binding (used by dummy/test booking quick links)
    url_day_code = (request.GET.get("day") or "").strip()
    day_code = url_day_code or (request.POST.get("day_code") or "").strip()
    selected_day = None
    session_errors = []
    if day_code:
        selected_day = (
            BookingDay.objects
            .select_related("booking__course_type", "booking__instructor", "instructor")
            .filter(day_code=day_code)
            .first()
        )

    # 1) Resolve course from exact day, ?ct=<code>, POST course_id, or form data
    course = None

    if selected_day and selected_day.booking and selected_day.booking.course_type:
        course = selected_day.booking.course_type
    
    # First try GET ?ct=<code> (not when already bound to a booking day)
    ct_code = (request.GET.get("ct") or "").strip()
    if ct_code and not course:
        course = CourseType.objects.filter(code__iexact=ct_code).first()
        if course and course.is_suspended:
            course = None

    # If not in GET, try POST course_id (from hidden field in form)
    if not course and request.method == "POST":
        course_id = (request.POST.get("course_id") or "").strip()
        if course_id:
            try:
                course = CourseType.objects.get(pk=course_id)
                if course.is_suspended and not selected_day:
                    course = None
            except CourseType.DoesNotExist:
                pass

    from .utils.course_types import bookable_course_types

    course_types = bookable_course_types()
    registration_unavailable = bool(
        ct_code and not selected_day and not course
    )

    # 2) Course date: hidden (always today) in production; editable when REGISTER_SHOW_DATE
    show_register_date = getattr(settings, "REGISTER_SHOW_DATE", settings.DEBUG)
    register_date = timezone.localdate()
    if show_register_date:
        if request.method == "POST":
            parsed = _parse_yyyy_mm_dd((request.POST.get("date") or "").strip())
            if not parsed:
                parsed = _parse_partial_date_from_post(request.POST, "course_date")
            if parsed:
                register_date = parsed
        else:
            if selected_day:
                register_date = selected_day.date
            elif request.GET.get("date"):
                parsed = _parse_yyyy_mm_dd(request.GET.get("date").strip())
                if parsed:
                    register_date = parsed
    initial = {"date": register_date}

    # 3) Build instructors list (depends on course + date)
    instructors = []
    instructor_fallback = False
    if selected_day:
        possible_ids = [selected_day.instructor_id, selected_day.booking.instructor_id]
        instructor_ids = [pid for pid in possible_ids if pid]
        instructors = list(
            Personnel.objects.filter(pk__in=instructor_ids).distinct().order_by("name")
        )
    elif course:
        instructors, instructor_fallback = _resolve_public_register_instructors(course, register_date)

    form = PublicDelegateRegisterForm(
        request.POST or None,
        initial=initial,
        instructors=instructors,
        show_register_date=show_register_date,
    )

    single_instructor = instructors[0] if len(instructors) == 1 else None
    instructor_dev_hint = (
        _dev_instructor_register_hint(course, register_date)
        if course and not instructor_fallback
        else ""
    )
    matching_sessions = _public_matching_sessions(
        course,
        register_date,
        single_instructor,
        request,
        url_day_code=url_day_code,
    )

    if request.method == "POST" and form.is_valid():
        delegate = form.save(commit=False)

        inst = delegate.instructor
        bd = None
        if selected_day:
            allowed_ids = {selected_day.instructor_id, selected_day.booking.instructor_id}
            allowed_ids.discard(None)
            if inst and inst.id in allowed_ids:
                bd = selected_day
            else:
                form.add_error("instructor", "Please select the instructor for this course session.")
        elif course and inst and form.cleaned_data.get("date"):
            reg_date = form.cleaned_data["date"]
            _, submit_fallback = _resolve_public_register_instructors(course, reg_date)
            posted_day_code = (request.POST.get("day_code") or "").strip()
            from .utils.public_sessions import matching_booking_days, resolve_booking_day

            if submit_fallback:
                bd = resolve_booking_day(
                    course, reg_date, inst, instructor_fallback=True
                )
            else:
                days = matching_booking_days(course, reg_date, inst)
                if len(days) > 1 and not posted_day_code:
                    session_errors = ["Please select which session you attended."]
                    form.add_error(None, session_errors[0])
                    bd = None
                else:
                    bd = resolve_booking_day(
                        course,
                        reg_date,
                        inst,
                        day_code=posted_day_code,
                    )
            if not bd and not form.errors:
                form.add_error(
                    "date",
                    "No course session was found for that course and date.",
                )

        if not form.errors and bd:
            delegate.booking_day = bd
            delegate.save()

            # Redirect to a dedicated confirmation screen to avoid accidental duplicate submissions.
            success_params = {}
            if course and getattr(course, "code", None):
                success_params["ct"] = course.code
            if show_register_date and form.cleaned_data.get("date"):
                success_params["date"] = form.cleaned_data["date"].isoformat()
            success_url = reverse("public_delegate_register_success")
            if success_params:
                success_url = f"{success_url}?{urlencode(success_params)}"
            return redirect(success_url)

    return render(
        request,
        "public/delegate_register.html",
        {
            "form": form,
            "course": course,
            "course_types": course_types,
            "instructors": instructors,
            "single_instructor": single_instructor,
            "day_code": day_code,
            "register_date": register_date,
            "show_register_date": show_register_date,
            "instructor_dev_hint": instructor_dev_hint,
            "instructor_fallback": instructor_fallback,
            "registration_unavailable": registration_unavailable,
            "matching_sessions": matching_sessions,
            "posted_day_code": (request.POST.get("day_code") or "").strip(),
            "session_errors": session_errors,
        },
    )


def public_delegate_register_success(request):
    """Confirmation page shown after a successful public delegate registration."""
    params = {}
    ct = (request.GET.get("ct") or "").strip()
    date = (request.GET.get("date") or "").strip()
    if ct:
        params["ct"] = ct
    if date:
        params["date"] = date

    register_another_url = reverse("public_delegate_register")
    if params:
        register_another_url = f"{register_another_url}?{urlencode(params)}"

    return render(
        request,
        "public/delegate_register_success.html",
        {"register_another_url": register_another_url},
    )

@login_required
def switch_role(request, role):
    from .utils.user_roles import set_active_role, user_has_role

    role = role.lower()

    if user_has_role(request.user, role):
        set_active_role(request.session, role)
        messages.success(request, f"Switched to {role.capitalize()}.")
        return redirect("home")

    messages.error(request, "You don't have that role.")
    return redirect("home")

# Admin app
@login_required
def app_admin_dashboard(request):
    if not (request.user.is_superuser or request.user.groups.filter(name='admin').exists()):
        return HttpResponseForbidden()
    bookings = Booking.objects.select_related('business','training_location','course_type','instructor').order_by('-course_date')[:50]
    return render(request,'admin_app/dashboard.html',{'bookings':bookings})

@login_required
def app_admin_booking_new(request):
    if not (request.user.is_superuser or request.user.groups.filter(name='admin').exists()):
        return HttpResponseForbidden()

    form = BookingForm(request.POST or None)

    if request.method == 'POST' and form.is_valid():
        b = form.save()

        from datetime import timedelta

        # How many days?  Use course_type.duration_days (Decimal) and round UP
        duration = float(b.course_type.duration_days or 1.0)
        n_days = int(duration + 0.999)   # 0.5 -> 1, 1.0 -> 1, 1.5 -> 2, etc.

        for i in range(n_days):
            BookingDay.objects.create(
                booking=b,
                date=b.course_date + timedelta(days=i),   # day 0,1,2,...
                start_time=b.start_time,
                instructor=b.instructor,
            )

        messages.success(request, "Booking created")
        return redirect('app_admin_booking_detail', pk=b.id)

    return render(request, 'admin_app/booking_form.html', {'form': form})


@login_required
def app_admin_booking_detail(request, pk):
    if not (request.user.is_superuser or request.user.groups.filter(name='admin').exists()):
        return HttpResponseForbidden()
    b = get_object_or_404(Booking, pk=pk)
    days = b.days.order_by('date')   # was 'day_no'
    return render(request, 'admin_app/booking_detail.html', {'booking': b, 'days': days})

# helper for human label + bootstrap class
_STATUS_MAP = {
    "scheduled":        ("Scheduled", "badge bg-info text-dark"),
    "in_progress":      ("In progress", "badge bg-dark"),
    "awaiting_closure": ("Awaiting instructor closure", "badge", "background-color:#6f42c1;color:#fff"),
    "completed":        ("Completed", "badge bg-success"),
    "cancelled":        ("Cancelled", "badge bg-warning text-dark"),
}

@login_required
def user_profile(request):

    sidebar_theme_options = [
        {"value": "default", "label": "Default", "hint": "Classic unicorn burgundy"},
        {"value": "ocean", "label": "Ocean", "hint": "Deep navy blue"},
        {"value": "forest", "label": "Forest", "hint": "Dark green"},
        {"value": "plum", "label": "Plum", "hint": "Rich aubergine"},
        {"value": "charcoal", "label": "Charcoal", "hint": "Slate grey"},
    ]

    avatar_icon_options = [
        {"value": "initials", "label": "Initials", "icon_class": ""},
        {"value": "person", "label": "Person", "icon_class": "bi-person-fill"},
        {"value": "person_badge", "label": "Person Badge", "icon_class": "bi-person-badge-fill"},
        {"value": "teacher", "label": "Teacher", "icon_class": "bi-easel2-fill"},
        {"value": "star", "label": "Star", "icon_class": "bi-star-fill"},
        {"value": "graduation", "label": "Graduation", "icon_class": "bi-mortarboard-fill"},
        {"value": "tools", "label": "Tools", "icon_class": "bi-tools"},
        {"value": "unicorn", "label": "Sparkles", "icon_class": "bi-stars"},
        {"value": "briefcase", "label": "Briefcase", "icon_class": "bi-briefcase-fill"},
        {"value": "books", "label": "Books", "icon_class": "bi-journal-bookmark-fill"},
        {"value": "note", "label": "Note", "icon_class": "bi-journal-text"},
        {"value": "cog", "label": "Cog", "icon_class": "bi-gear-fill"},
        {"value": "heart", "label": "Heart", "icon_class": "bi-heart-fill"},
        {"value": "palette", "label": "Palette", "icon_class": "bi-palette-fill"},
        {"value": "lightning", "label": "Lightning", "icon_class": "bi-lightning-charge-fill"},
        {"value": "music", "label": "Music", "icon_class": "bi-music-note-beamed"},
        {"value": "camera", "label": "Camera", "icon_class": "bi-camera-fill"},
        {"value": "bug", "label": "Bug", "icon_class": "bi-bug-fill"},
        {"value": "rocket", "label": "Rocket", "icon_class": "bi-rocket-takeoff-fill"},
        {"value": "ambulance", "label": "Ambulance", "icon_class": "bi-truck-front-fill"},
        {"value": "fire_engine", "label": "Fire Engine", "icon_class": "bi-fire"},
        {"value": "first_aid", "label": "First Aid Kit", "icon_class": "bi-heart-pulse-fill"},
        {"value": "extinguisher", "label": "Fire Extinguisher", "icon_class": "bi-shield-fill-plus"},
        {"value": "firefighter", "label": "Firefighter", "icon_class": "bi-person-badge-fill"},
        {"value": "paramedic", "label": "Ambulance Crew", "icon_class": "bi-person-vcard-fill"},
        {"value": "hospital", "label": "Hospital", "icon_class": "bi-hospital-fill"},
        {"value": "lifebuoy", "label": "Lifebuoy", "icon_class": "bi-life-preserver"},
        {"value": "shield_medical", "label": "Medical Shield", "icon_class": "bi-shield-plus"},
    ]

    from django.contrib.auth import update_session_auth_hash

    try:
        personnel = request.user.personnel
    except Personnel.DoesNotExist:
        messages.error(request, "No personnel profile linked to this account.")
        return redirect("no_roles")

    if request.method == "POST":
        uform = UserProfileForm(request.POST, instance=request.user)
        pform = PersonnelProfileForm(request.POST, instance=personnel, user=request.user)

        if uform.is_valid() and pform.is_valid():
            user = uform.save(commit=False)
            profile = pform.save(commit=False)
            new_email = uform.cleaned_data["email"].strip().lower()
            user.username = new_email
            user.email = new_email
            user.save()

            first = uform.cleaned_data.get("first_name", "").strip()
            last  = uform.cleaned_data.get("last_name", "").strip()
            full_name = f"{first} {last}".strip()

            profile.email = new_email
            if full_name:
                profile.name = full_name

            profile.save()

            preferred_role = (profile.default_dashboard_role or "").strip()
            if preferred_role:
                request.session["active_role"] = preferred_role
            else:
                request.session.pop("active_role", None)

            update_session_auth_hash(request, user)
            messages.success(request, "Your profile has been updated.")
            return redirect("user_profile")

    else:
        uform = UserProfileForm(instance=request.user)
        pform = PersonnelProfileForm(instance=personnel, user=request.user)

    current_sidebar_theme = pform["sidebar_theme"].value() or personnel.sidebar_theme
    current_sidebar_custom_color = pform["sidebar_custom_color"].value() or personnel.sidebar_custom_color or "#8b0000"
    current_avatar_icon = pform["avatar_icon"].value() or personnel.avatar_icon
    current_night_mode = bool(pform["night_mode"].value() if "night_mode" in pform.fields else personnel.night_mode)

    return render(request, "profile.html", {
        "uform": uform,
        "pform": pform,
        "sidebar_theme_options": sidebar_theme_options,
        "avatar_icon_options": avatar_icon_options,
        "current_sidebar_theme": current_sidebar_theme,
        "current_sidebar_custom_color": current_sidebar_custom_color,
        "current_avatar_icon": current_avatar_icon,
        "current_avatar_initial": personnel.avatar_initial,
        "current_night_mode": current_night_mode,
        "has_2fa": bool(personnel.totp_secret),
        "telegram_linked": bool((personnel.telegram_chat_id or "").strip()),
        "telegram_linked_at": personnel.telegram_linked_at,
        "telegram_bot_configured": bool(settings.TELEGRAM_BOT_USERNAME),
        "is_instructor_profile": is_instructor_user(request.user),
        "deliverable_courses": (
            personnel.deliverable_course_types.filter(is_suspended=False).order_by("name")
            if is_instructor_user(request.user)
            else []
        ),
        "requestable_courses": _requestable_courses_for_instructor(personnel)
        if is_instructor_user(request.user)
        else [],
        "pending_course_requests": (
            personnel.course_delivery_requests.filter(
                status=InstructorCourseRequestStatus.PENDING,
            )
            .select_related("course_type")
            .order_by("course_type__name")
            if is_instructor_user(request.user)
            else []
        ),
    })


def _requestable_courses_for_instructor(personnel):
    from .models import CourseType, InstructorCourseRequestStatus

    approved_ids = personnel.deliverable_course_types.values_list("pk", flat=True)
    pending_ids = personnel.course_delivery_requests.filter(
        status=InstructorCourseRequestStatus.PENDING,
    ).values_list("course_type_id", flat=True)
    return CourseType.objects.filter(is_suspended=False).exclude(
        pk__in=approved_ids,
    ).exclude(pk__in=pending_ids).order_by("name")


@login_required
@require_POST
def instructor_request_course_delivery(request):
    from .models import CourseType
    from .services.instructor_course_requests import (
        CourseDeliveryRequestError,
        create_course_delivery_request,
    )

    if not is_instructor_user(request.user):
        messages.error(request, "Only instructors can request course delivery approval.")
        return redirect("user_profile")

    try:
        personnel = request.user.personnel
    except Personnel.DoesNotExist:
        messages.error(request, "No personnel profile linked to this account.")
        return redirect("no_roles")

    course_type_id = (request.POST.get("course_type_id") or "").strip()
    message = (request.POST.get("message") or "").strip()
    course_type = get_object_or_404(CourseType, pk=course_type_id, is_suspended=False)

    try:
        create_course_delivery_request(personnel, course_type, message=message)
        messages.success(
            request,
            f"Request sent to admin for approval to deliver {course_type.name}.",
        )
    except CourseDeliveryRequestError as exc:
        messages.error(request, str(exc))

    return redirect("user_profile")


@login_required
@require_POST
def toggle_night_mode(request):
    try:
        personnel = request.user.personnel
    except Personnel.DoesNotExist:
        messages.error(request, "No personnel profile linked to this account.")
        return redirect("no_roles")

    personnel.night_mode = request.POST.get("night_mode") == "on"
    personnel.save(update_fields=["night_mode"])

    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER") or reverse("user_profile")
    return redirect(next_url)

# Public attendance
def public_attendance(request, booking_day_id):
    bday = get_object_or_404(BookingDay, pk=booking_day_id)
    if request.method=='POST':
        form = AttendanceForm(request.POST)
        if form.is_valid():
            att = form.save(commit=False)
            att.booking_day = bday
            att.save()
            return render(request,'public/attendance_success.html',{'booking_day':bday})
    else:
        form = AttendanceForm()
    return render(request,'public/attendance.html',{'form':form,'booking_day':bday})

# API
@login_required
def api_locations_by_business(request):
    bid = request.GET.get('business')
    data=[]
    if bid:
        for x in TrainingLocation.objects.filter(business_id=bid).order_by('name'):
            data.append({'id': str(x.id), 'name': f"{x.name} — {x.address_line or ''} {x.postcode or ''}"})
    return JsonResponse({'data':data})

# Instructor my bookings view
@login_required
def instructor_bookings(request):
    # Require the user to be linked to an Instructor record
    try:
        instructor = Personnel.objects.select_related('user').get(user=request.user)
    except Instructor.DoesNotExist:
        messages.error(request, "Your user account isn’t linked to an Instructor profile yet.")
        return redirect('instructor_dashboard')  # or 'home'

    bookings = (
        Booking.objects
        .select_related('business', 'training_location', 'course_type')
        .filter(instructor=instructor)
        .order_by('-course_date', 'start_time')
    )
    return render(request, 'instructor/bookings.html', {
        'bookings': bookings,
        'instructor': instructor,
    })

@login_required
def instructor_booking_detail(request, pk):
    """
    Detail page for a single booking, visible only to its assigned instructor.
    """
    iinst = get_instructor_for_user(request.user)
    if not inst:
        return HttpResponseForbidden("Not an instructor account.")

    booking = get_object_or_404(
        Booking.objects.select_related("business", "training_location", "course_type", "instructor"),
        pk=pk,
    )
    if booking.instructor_id != inst.id:
        return HttpResponseForbidden("This booking is not assigned to you.")

    # Convenient status label
    status_labels = dict(getattr(Booking, "STATUS_CHOICES", []))
    status_label = status_labels.get(booking.status, booking.status)

    # Include the course days
    days = booking.days.all().order_by("date")

    return render(request, "instructor/booking_detail.html", {
        "title": "Booking details",
        "booking": booking,
        "status_label": status_label,
        "day_rows": days,
        "today": timezone.localdate(),
    })

def _user_is_instructor(user):
    return hasattr(user, "personnel") and user.personnel.is_active and user.personnel.can_login


#@login_required
#def post_login_router(request):
#    user = request.user
#
#    if user.is_superuser or user.is_staff:
#        return redirect("app_admin_dashboard")
#
#    if _user_is_instructor(user):
#        return redirect("instructor_dashboard")
#
#    return redirect("home")
#
#
#
    # Fallback
#    return redirect("app_admin_dashboard")


def _resolve_course_type(value):
    if not value:
        return None
    # Try UUID first
    try:
        return CourseType.objects.filter(id=uuid.UUID(str(value))).first()
    except Exception:
        pass
    # Fall back to course code (case-insensitive)
    return CourseType.objects.filter(code__iexact=str(value)).first()

def _parse_date_flexible(s: str):
    if not s:
        return None
    d = parse_date(s)  # expects YYYY-MM-DD
    if d:
        return d
    # Try dd/mm/yyyy
    if "/" in s:
        try:
            dd, mm, yyyy = s.split("/")
            return parse_date(f"{yyyy}-{int(mm):02d}-{int(dd):02d}")
        except Exception:
            return None
    return None

def public_feedback_instructors_api(request):
    """
    Returns instructors who are delivering the given course on the given date.
    Query params:
      - course: course code (e.g. FAAW) or course UUID
      - date:   YYYY-MM-DD (required for filtering)
    If either parameter is missing, returns an empty list (no noisy fallback).
    """
    course_param = (request.GET.get("course") or "").strip()
    date_str     = (request.GET.get("date") or "").strip()

    # Resolve course by code OR UUID
    ct = None
    if course_param:
        ct = CourseType.objects.filter(code__iexact=course_param).first()
        if not ct:
            try:
                uuid.UUID(course_param)
                ct = CourseType.objects.filter(pk=course_param).first()
            except Exception:
                ct = None

    # Parse date flexibly: YYYY-MM-DD or dd/mm/yyyy
    the_date = None
    if date_str:
        the_date = parse_date(date_str)
        if not the_date and "/" in date_str:
            try:
                dd, mm, yyyy = date_str.split("/")
                the_date = parse_date(f"{int(yyyy):04d}-{int(mm):02d}-{int(dd):02d}")
            except Exception:
                the_date = None

    # Strict behaviour: only return matches when both pieces are present
    qs = Personnel.objects.none()
    if ct and the_date:
        qs = (
            Personnel.objects
            .filter(
                bookings__course_type=ct,
                bookings__days__date=the_date,
            )
            .distinct()
            .order_by("name")
        )

    return JsonResponse({
        "options": [{"id": str(i.id), "name": i.name} for i in qs]
    })


def _resolve_course_type(q):
    if not q:
        return None
    q = q.strip()
    ct = CourseType.objects.filter(code__iexact=q).first()
    if ct:
        return ct
    try:
        uuid.UUID(q)
        return CourseType.objects.filter(pk=q).first()
    except Exception:
        return None

def _parse_flexible_date(s):
    if not s:
        return None
    d = parse_date(s)
    if d:
        return d
    if "/" in s:
        try:
            dd, mm, yyyy = s.split("/")
            return parse_date(f"{int(yyyy):04d}-{int(mm):02d}-{int(dd):02d}")
        except Exception:
            return None
    return None

def public_feedback_form(request):
    """
    Public course feedback form. Supports optional prefill via:
      ?course=<code or UUID>&date=YYYY-MM-DD&instructor=<UUID>
    and automatically links the feedback to the matching Booking
    (by course_type + course_date + instructor) where possible.
    """
    course_q = request.GET.get("course") or request.GET.get("ct") or ""
    prefilled_course = _resolve_course_type(course_q)

    url_day_code = (request.GET.get("day") or "").strip()
    day_code = url_day_code or (request.POST.get("day_code") or "").strip()
    selected_day = None
    session_errors = []
    if day_code:
        selected_day = (
            BookingDay.objects
            .select_related("booking__course_type", "booking__instructor", "instructor", "booking")
            .filter(day_code=day_code)
            .first()
        )
    if selected_day and selected_day.booking and selected_day.booking.course_type:
        prefilled_course = selected_day.booking.course_type

    show_feedback_date = getattr(settings, "REGISTER_SHOW_DATE", settings.DEBUG)
    feedback_date = timezone.localdate()
    if show_feedback_date:
        if request.method == "POST":
            parsed = _parse_flexible_date((request.POST.get("date") or "").strip())
            if parsed:
                feedback_date = parsed
        elif request.GET.get("date"):
            parsed = _parse_flexible_date(request.GET.get("date").strip())
            if parsed:
                feedback_date = parsed

    init = {"date": feedback_date}
    inst_q = (request.GET.get("instructor") or "").strip()
    if selected_day:
        feedback_date = selected_day.date
        init["date"] = feedback_date
    if inst_q and not selected_day:
        try:
            init["instructor"] = Personnel.objects.get(pk=inst_q)
        except Personnel.DoesNotExist:
            pass
    elif selected_day:
        inst = selected_day.instructor or selected_day.booking.instructor
        if inst:
            init["instructor"] = inst

    course = prefilled_course
    if not course and request.method == "POST":
        posted_course = (request.POST.get("course_type") or "").strip()
        if posted_course:
            try:
                course = CourseType.objects.get(pk=posted_course)
            except (CourseType.DoesNotExist, ValueError):
                course = _resolve_course_type(posted_course)

    instructors = []
    instructor_fallback = False
    if selected_day:
        possible_ids = [selected_day.instructor_id, selected_day.booking.instructor_id]
        instructor_ids = [pid for pid in possible_ids if pid]
        instructors = list(
            Personnel.objects.filter(pk__in=instructor_ids).distinct().order_by("name")
        )
    elif course:
        instructors, instructor_fallback = _resolve_public_register_instructors(course, feedback_date)

    form = PublicFeedbackForm(
        request.POST or None,
        initial=init,
        instructors=instructors,
        show_feedback_date=show_feedback_date,
    )

    form.fields["course_type"].required = not bool(prefilled_course)
    if prefilled_course:
        form.fields["course_type"].initial = prefilled_course.id

    single_instructor = instructors[0] if len(instructors) == 1 else None
    instructor_dev_hint = (
        _dev_instructor_register_hint(course, feedback_date)
        if course and not instructor_fallback and not instructors
        else ""
    )
    matching_sessions = _public_matching_sessions(
        course,
        feedback_date,
        single_instructor,
        request,
        url_day_code=url_day_code,
    )

    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data

        course = prefilled_course or cd.get("course_type")
        the_date = cd.get("date")
        inst = cd.get("instructor")

        booking = None
        if selected_day:
            booking = selected_day.booking
        elif course and the_date and inst:
            _, submit_fallback = _resolve_public_register_instructors(course, the_date)
            posted_day_code = (request.POST.get("day_code") or "").strip()
            from .utils.public_sessions import matching_booking_days, resolve_booking_day

            booking_day = None
            if submit_fallback:
                booking_day = resolve_booking_day(
                    course, the_date, inst, instructor_fallback=True
                )
            else:
                days = matching_booking_days(course, the_date, inst)
                if len(days) > 1 and not posted_day_code:
                    session_errors = ["Please select which session you attended."]
                    form.add_error(None, session_errors[0])
                else:
                    booking_day = resolve_booking_day(
                        course,
                        the_date,
                        inst,
                        day_code=posted_day_code,
                    )
            if booking_day:
                booking = booking_day.booking

        if not form.errors:
            FeedbackResponse.objects.create(
                booking     = booking,
                course_type = course,
                date        = the_date,
                instructor  = inst,

                overall_rating         = cd.get("overall_rating"),
                prior_knowledge        = cd.get("prior_knowledge"),
                post_knowledge         = cd.get("post_knowledge"),
                q_purpose_clear        = cd.get("q_purpose_clear"),
                q_personal_needs       = cd.get("q_personal_needs"),
                q_exercises_useful     = cd.get("q_exercises_useful"),
                q_structure            = cd.get("q_structure"),
                q_pace                 = cd.get("q_pace"),
                q_content_clear        = cd.get("q_content_clear"),
                q_instructor_knowledge = cd.get("q_instructor_knowledge"),
                q_materials_quality    = cd.get("q_materials_quality"),
                q_books_quality        = cd.get("q_books_quality"),
                q_venue_suitable       = cd.get("q_venue_suitable"),
                q_benefit_at_work      = cd.get("q_benefit_at_work"),
                q_benefit_outside      = cd.get("q_benefit_outside"),

                comments       = cd.get("comments") or "",
                wants_callback = cd.get("wants_callback") or False,
                contact_name   = cd.get("contact_name") or "",
                contact_email  = cd.get("contact_email") or "",
                contact_phone  = cd.get("contact_phone") or "",
            )

            messages.success(request, "Thanks for your feedback!")
            success_params = {}
            if course and getattr(course, "code", None):
                success_params["course"] = course.code
            if the_date:
                success_params["date"] = the_date.isoformat()
            if inst:
                success_params["instructor"] = str(inst.pk)
            day_for_redirect = (
                (request.POST.get("day_code") or "").strip()
                or day_code
                or ""
            )
            if day_for_redirect:
                success_params["day"] = day_for_redirect
            success_url = reverse("public_feedback_thanks")
            if success_params:
                success_url = f"{success_url}?{urlencode(success_params)}"
            return redirect(success_url)

    from .utils.course_types import bookable_course_types

    return render(
        request,
        "public/feedback_form.html",
        {
            "form": form,
            "course_type": prefilled_course,
            "course_types": bookable_course_types(),
            "instructors": instructors,
            "single_instructor": single_instructor,
            "show_feedback_date": show_feedback_date,
            "instructor_fallback": instructor_fallback,
            "instructor_dev_hint": instructor_dev_hint,
            "matching_sessions": matching_sessions,
            "posted_day_code": (request.POST.get("day_code") or "").strip(),
            "session_errors": session_errors,
            "day_code": day_code,
            "feedback_date": feedback_date,
        },
    )


def public_feedback_pdf(request, pk):
    """Minimal single-response PDF so the route works."""
    fb = get_object_or_404(
        FeedbackResponse.objects.select_related("course_type", "instructor"),
        pk=pk
    )

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=portrait(A4))
    W, H = portrait(A4)
    left, top = 15*mm, H - 15*mm

    y = top
    c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(W/2, y, f"{fb.course_type.code} — Course Feedback")
    y -= 8*mm
    c.setFont("Helvetica", 10)
    c.drawString(left, y, f"Date: {fb.date.strftime('%d %b %Y')}")
    y -= 5*mm
    c.drawString(left, y, f"Instructor: {fb.instructor.name if fb.instructor else '—'}")
    y -= 10*mm

    c.setFont("Helvetica", 9)
    c.drawString(left, y, f"Prior knowledge: {fb.prior_knowledge or '—'} / 5")
    y -= 5*mm
    c.drawString(left, y, f"Post knowledge:  {fb.post_knowledge  or '—'} / 5")
    y -= 8*mm
    c.drawString(left, y, f"Comments: {(fb.comments or '')[:300]}")
    y -= 12*mm

    c.setFont("Helvetica", 8)
    c.drawString(left, 10*mm, "Unicorn Fire & Safety Solutions, Unicorn House, 6 Salendine, Shrewsbury, SY1 3XJ: info@unicornsafety.co.uk: 01743 360211")

    c.save()
    buf.seek(0)
    return FileResponse(buf, as_attachment=True, filename=f"feedback_{fb.course_type.code}_{fb.date:%Y%m%d}.pdf")

def public_feedback_thanks(request):
    """Thank-you page after feedback; offer another submission on the same device."""
    params = {}
    for key in ("course", "ct", "date", "day", "instructor"):
        val = (request.GET.get(key) or "").strip()
        if val:
            params[key] = val
    feedback_another_url = reverse("public_feedback_form")
    if params:
        feedback_another_url = f"{feedback_another_url}?{urlencode(params)}"
    return render(request, "public/feedback_thanks.html", {
        "feedback_another_url": feedback_another_url,
    })

def no_roles_assigned(request):
    return render(request, "no_roles.html")


@login_required
def accident_report_list(request):
    messages.info(
        request,
        "Accident reports are now shown on each relevant course booking (Accident reports tab).",
    )
    return redirect("instructor_bookings")


def _accident_report_booking_redirect(booking, *, portal: str):
    if portal == "admin":
        return redirect(f"{reverse('admin_booking_edit', args=[booking.pk])}?tab=accident-reports")
    return redirect(f"{reverse('instructor_booking_detail', args=[booking.pk])}?tab=accident-reports")


def _accident_report_poll_response(request, booking):
    from .utils.accident_reports import accident_reports_for_booking

    reports = accident_reports_for_booking(booking)
    ref = getattr(booking, "course_reference", None) or booking.pk
    # flush=True so Windows/runserver shows it immediately alongside request lines
    print(
        f"*** [AccidentReports] poll — {ref} — {len(reports)} report(s) ***",
        flush=True,
    )
    rows_html = render_to_string(
        "instructor/_accident_report_rows.html",
        {"reports": reports, "booking": booking},
        request=request,
    )
    return JsonResponse({"html": rows_html})


def _accident_report_booking_guard(request, booking_id, *, portal: str):
    from .utils.accident_reports import user_may_view_booking_accident_reports
    from .utils.online_exercises import booking_accident_reports_enabled

    booking = get_object_or_404(
        Booking.objects.select_related("course_type"),
        pk=booking_id,
    )
    if not user_may_view_booking_accident_reports(request.user, booking):
        return None, HttpResponseForbidden("You do not have access to this booking.")
    if not booking_accident_reports_enabled(booking):
        return None, HttpResponseForbidden("Accident reports are not enabled for this course.")
    return booking, None


@login_required
def instructor_booking_accident_reports_poll(request, booking_id):
    booking, denied = _accident_report_booking_guard(request, booking_id, portal="instructor")
    if denied:
        return denied
    return _accident_report_poll_response(request, booking)


@login_required
def admin_booking_accident_reports_poll(request, booking_id):
    booking, denied = _accident_report_booking_guard(request, booking_id, portal="admin")
    if denied:
        return denied
    return _accident_report_poll_response(request, booking)


@login_required
def accident_report_create(request):
    if request.method == "POST":
        form = AccidentReportForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Accident report submitted successfully.")
            return redirect("accident_report_list")
    else:
        form = AccidentReportForm()

    return render(request, "public/accident_report_form.html", {"form": form})

from django.utils import timezone

def _accident_report_show_date() -> bool:
    return getattr(settings, "REGISTER_SHOW_DATE", settings.DEBUG)


def accident_report_public(request):
    """
    Public accident report form.

    Links to a booking via:
      - ?ref=<course_reference>  (booking link from the Accident reports tab), or
      - course + incident date + reported-to instructor (resolved on save).
    """
    from .utils.accident_reports import (
        accident_report_day_options,
        booking_incident_dates,
        default_incident_date_for_booking,
        default_incident_date_for_course,
        incident_date_on_booking,
        instructors_for_accident_report_course_date,
        instructors_for_booking_report,
        resolve_accident_report_booking,
    )
    from .utils.online_exercises import booking_accident_reports_enabled, course_types_with_accident_reports

    booking = None
    ref = (request.GET.get("ref") or request.POST.get("ref") or "").strip()
    if ref:
        booking = (
            Booking.objects.filter(course_reference__iexact=ref)
            .select_related("course_type", "instructor")
            .prefetch_related("course_type__online_exercises", "days")
            .first()
        )
        if booking and not booking_accident_reports_enabled(booking):
            booking = None

    course_code = (request.GET.get("course") or request.POST.get("course") or "").strip()
    prefilled_course = None
    if booking:
        prefilled_course = booking.course_type
    elif course_code:
        prefilled_course = course_types_with_accident_reports().filter(code__iexact=course_code).first()

    show_accident_date = _accident_report_show_date()
    if booking:
        incident_date = default_incident_date_for_booking(booking)
    elif prefilled_course:
        incident_date = default_incident_date_for_course(prefilled_course)
    else:
        incident_date = timezone.localdate()

    if request.method == "POST":
        parsed = _parse_flexible_date((request.POST.get("date") or "").strip())
        if parsed:
            incident_date = parsed
    elif show_accident_date and request.GET.get("date"):
        parsed = _parse_flexible_date(request.GET.get("date").strip())
        if parsed:
            incident_date = parsed

    booking_link_active = bool(booking and incident_date_on_booking(booking, incident_date))
    booking_day_dates = sorted(booking_incident_dates(booking)) if booking else []

    # Open form (no booking/course QR): resolve what's running on the incident date.
    day_options = None
    if not booking and not prefilled_course and request.method != "POST":
        day_options = accident_report_day_options(incident_date)

    init = {"date": incident_date}
    if day_options and day_options["mode"] == "single_course" and day_options["courses"]:
        init["course_type"] = day_options["courses"][0]["id"]

    inst_q = (request.GET.get("instructor") or request.POST.get("reported_to") or "").strip()
    if booking and booking.instructor_id and booking_link_active:
        init["reported_to"] = booking.instructor_id
    elif inst_q:
        try:
            init["reported_to"] = Personnel.objects.get(pk=inst_q)
        except Personnel.DoesNotExist:
            pass
    elif day_options and len(day_options["instructors"]) == 1:
        init["reported_to"] = day_options["instructors"][0]["id"]
        if day_options["mode"] == "multi_course":
            init["course_type"] = day_options["instructors"][0]["course_id"]

    course = prefilled_course
    if not course and request.method == "POST":
        posted_course = (request.POST.get("course_type") or "").strip()
        if posted_course:
            try:
                course = course_types_with_accident_reports().get(pk=posted_course)
            except (CourseType.DoesNotExist, ValueError):
                course = _resolve_course_type(posted_course)
    elif not course and day_options and day_options["mode"] == "single_course" and day_options["courses"]:
        course = course_types_with_accident_reports().filter(
            pk=day_options["courses"][0]["id"]
        ).first()

    instructors = []
    if booking_link_active:
        instructors = instructors_for_booking_report(booking, incident_date)
    elif course:
        instructors = instructors_for_accident_report_course_date(course, incident_date)
    elif day_options and day_options["instructors"]:
        instructor_ids = [row["id"] for row in day_options["instructors"]]
        instructors = list(Personnel.objects.filter(pk__in=instructor_ids).order_by("name"))

    instructors_unavailable = bool(
        (course or (day_options and day_options["mode"] != "none"))
        and not booking_link_active
        and not instructors
    )
    form = PublicAccidentReportForm(
        request.POST or None,
        initial=init,
        instructors=instructors,
        show_accident_date=show_accident_date,
        prefilled_course=prefilled_course,
        lock_course=bool(booking or prefilled_course),
    )

    if prefilled_course:
        form.fields["course_type"].initial = prefilled_course.id

    matching_sessions = []
    session_errors = []
    if booking_link_active:
        single = instructors[0] if len(instructors) == 1 else None
        if not single and request.method == "POST":
            rid = (request.POST.get("reported_to") or "").strip()
            if rid:
                single = Personnel.objects.filter(pk=rid).first()
        matching_sessions = _public_matching_sessions(
            booking.course_type, incident_date, single, request
        )
    elif course:
        single = instructors[0] if len(instructors) == 1 else None
        if not single and request.method == "POST":
            rid = (request.POST.get("reported_to") or "").strip()
            if rid:
                single = Personnel.objects.filter(pk=rid).first()
        matching_sessions = _public_matching_sessions(course, incident_date, single, request)

    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        course = prefilled_course or cd.get("course_type")
        the_date = cd.get("date")
        reported_to = cd.get("reported_to")
        posted_day_code = (request.POST.get("day_code") or "").strip()

        if matching_sessions and len(matching_sessions) > 1 and not posted_day_code:
            session_errors = ["Please select which session this incident relates to."]
            form.add_error(None, session_errors[0])
        else:
            submit_link_active = bool(booking and incident_date_on_booking(booking, the_date))
            resolved_booking = resolve_accident_report_booking(
                booking_ref=ref if submit_link_active else "",
                course=course,
                incident_date=the_date,
                reported_to=reported_to,
                day_code=posted_day_code,
            )
            report = form.save(commit=False)
            report.reported_to = reported_to
            if resolved_booking:
                report.booking = resolved_booking
            report.save()
            messages.success(request, "Accident report submitted successfully.")
            return redirect("accident_report_thanks")

    return render(
        request,
        "public/accident_report_form.html",
        {
            "form": form,
            "booking": booking,
            "course_type": prefilled_course,
            "show_accident_date": show_accident_date,
            "booking_link_active": booking_link_active,
            "booking_day_dates": booking_day_dates,
            "incident_date": incident_date,
            "instructors_unavailable": instructors_unavailable,
            "matching_sessions": matching_sessions,
            "session_errors": session_errors,
            "GOOGLE_MAPS_API_KEY": settings.GOOGLE_MAPS_API_KEY,
        },
    )


@require_GET
def accident_report_day_options_api(request):
    """
    Courses + instructors for accident reports on a date (no booking ref).
    Query: ?date=YYYY-MM-DD
    """
    from .utils.accident_reports import accident_report_day_options

    date_str = (request.GET.get("date") or "").strip()
    day_date = _parse_yyyy_mm_dd(date_str) or _parse_flexible_date(date_str) or timezone.localdate()
    options = accident_report_day_options(day_date)
    payload = {
        "date": day_date.isoformat(),
        "mode": options["mode"],
        "courses": options["courses"],
        "instructors": options["instructors"],
    }
    if options["mode"] == "none":
        payload["message"] = (
            f"No courses with accident reports are scheduled on "
            f"{day_date.strftime('%d %b %Y')}."
        )
    return JsonResponse(payload)


@require_GET
def accident_report_instructors_api(request):
    """
    Instructors for the Reported to dropdown.
    Query: ?course=<code>&date=YYYY-MM-DD  or  ?ref=<booking_ref>&date=YYYY-MM-DD
    """
    from .utils.accident_reports import (
        incident_date_on_booking,
        instructors_for_accident_report_course_date,
        instructors_for_booking_report,
    )
    from .utils.online_exercises import booking_accident_reports_enabled, course_types_with_accident_reports

    date_str = (request.GET.get("date") or "").strip()
    day_date = _parse_yyyy_mm_dd(date_str) or _parse_flexible_date(date_str) or timezone.localdate()

    ref = (request.GET.get("ref") or "").strip()
    if ref:
        booking = (
            Booking.objects.filter(course_reference__iexact=ref)
            .select_related("course_type", "instructor")
            .prefetch_related("days")
            .first()
        )
        if not booking or not booking_accident_reports_enabled(booking):
            return JsonResponse({"instructors": [], "booking_link_active": False})
        active = incident_date_on_booking(booking, day_date)
        instructors = (
            instructors_for_booking_report(booking, day_date) if active else []
        )
        payload = {
            "instructors": [{"id": str(i.id), "name": i.name} for i in instructors],
            "booking_link_active": active,
        }
        if not active:
            payload["message"] = (
                f"No course day on {day_date.strftime('%d %b %Y')} "
                f"for booking {booking.course_reference}."
            )
        return JsonResponse(payload)

    code = (request.GET.get("course") or request.GET.get("ct") or "").strip()
    course = course_types_with_accident_reports().filter(code__iexact=code).first()
    if not course:
        return JsonResponse({"instructors": []})

    instructors = instructors_for_accident_report_course_date(course, day_date)
    payload = {
        "instructors": [{"id": str(i.id), "name": i.name} for i in instructors],
        "booking_link_active": False,
    }
    if not instructors:
        payload["message"] = (
            f"No instructors are scheduled for {course.name} on "
            f"{day_date.strftime('%d %b %Y')}. Try another incident date."
        )
        hint = _dev_instructor_register_hint(course, day_date)
        if hint:
            payload["dev_hint"] = hint
    return JsonResponse(payload)

def accident_report_thanks(request):
    return render(request, "public/accident_report_thanks.html")

# --- Accident reports: DETAIL -------------------------------------------------

@login_required
def accident_report_detail(request, pk):
    from .utils.accident_reports import user_may_view_accident_report

    report = get_object_or_404(AccidentReport.objects.select_related("booking", "reported_to"), pk=pk)
    if not user_may_view_accident_report(request.user, report):
        return HttpResponseForbidden("You do not have access to this report.")

    back_url = None
    booking_id = (request.GET.get("booking") or "").strip()
    if booking_id:
        booking, denied = _accident_report_booking_guard(request, booking_id, portal="instructor")
        if booking and not denied:
            back_url = f"{reverse('instructor_booking_detail', args=[booking.pk])}?tab=accident-reports"
        else:
            booking, denied = _accident_report_booking_guard(request, booking_id, portal="admin")
            if booking and not denied:
                back_url = f"{reverse('admin_booking_edit', args=[booking.pk])}?tab=accident-reports"
    elif report.booking_id:
        from .services.dummy_bookings import admin_may_view_booking

        if admin_may_view_booking(request.user, report.booking):
            back_url = f"{reverse('admin_booking_edit', args=[report.booking_id])}?tab=accident-reports"
        else:
            back_url = f"{reverse('instructor_booking_detail', args=[report.booking_id])}?tab=accident-reports"

    return render(
        request,
        "instructor/accident_report_detail.html",
        {"report": report, "back_url": back_url},
    )

# --- Accident reports: EXPORT PPTX (selected IDs) -----------------------------

@login_required
def accident_report_export_pptx(request):
    """
    POST with ids[]=<uuid>… -> returns a styled .pptx
    One 4:3 slide per report with subtle first-aid watermark + corner L accent.
    """
    if request.method != "POST":
        return HttpResponseBadRequest("POST only")

    ids = request.POST.getlist("ids[]") or request.POST.getlist("ids")
    if not ids:
        return HttpResponseBadRequest("No IDs supplied")

    reports_qs = AccidentReport.objects.filter(pk__in=ids)
    booking = getattr(request, "_accident_report_booking", None)
    if booking is not None:
        reports_qs = reports_qs.filter(booking=booking)

    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_PARAGRAPH_ALIGNMENT

    reports = list(
        reports_qs.order_by("date", "time")
    )
    if not reports:
        return HttpResponseBadRequest("No matching reports")

    # ----- Theme colours
    BRAND = {
        "primary": RGBColor(220, 53, 69),    # red title bar / accents
        "ink":     RGBColor(33, 37, 41),     # near-black text
        "chipbg":  RGBColor(243, 244, 246),  # pill bg
        "chipfg":  RGBColor(33, 37, 41),
        "rule":    RGBColor(222, 226, 230),  # rules
        "panelbg": RGBColor(248, 249, 250),  # main card
        "panelbd": RGBColor(230, 232, 235),  # panel border
    }

    prs = Presentation()
    # Force 4:3 (10" x 7.5")
    prs.slide_width = Inches(10.0)
    prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]

    # ---------- helpers

    def add_title_bar(slide, title_text):
        bar = slide.shapes.add_shape(
            SHAPE.RECTANGLE, Inches(0), Inches(0), Inches(10.0), Inches(0.9)
        )
        bar.fill.solid(); bar.fill.fore_color.rgb = BRAND["primary"]
        bar.line.fill.background()
        tf = bar.text_frame; tf.clear()
        p = tf.paragraphs[0]
        p.text = title_text
        p.font.size = Pt(28); p.font.bold = True
        p.font.color.rgb = RGBColor(255, 255, 255)
        p.alignment = PP_PARAGRAPH_ALIGNMENT.LEFT
        tf.margin_left = Inches(0.4)

    def meta_pill(slide, text):
        pill = slide.shapes.add_shape(
            SHAPE.ROUNDED_RECTANGLE, Inches(0.4), Inches(1.0), Inches(9.2), Inches(0.65)
        )
        pill.fill.solid(); pill.fill.fore_color.rgb = BRAND["chipbg"]
        pill.line.fill.background()
        tf = pill.text_frame; tf.clear(); tf.word_wrap = True
        p = tf.paragraphs[0]
        p.text = text
        p.font.size = Pt(16); p.font.color.rgb = BRAND["chipfg"]
        p.alignment = PP_PARAGRAPH_ALIGNMENT.CENTER
        return pill

    def section(slide, left, top, title, body, width, height):
        box = slide.shapes.add_textbox(left, top, width, height)
        tf = box.text_frame; tf.clear(); tf.word_wrap = True
        tf.margin_left = Inches(0.06); tf.margin_right = Inches(0.06)

        h = tf.paragraphs[0]
        h.text = title
        h.font.size = Pt(12); h.font.bold = True; h.font.color.rgb = BRAND["ink"]

        txt = (body or "—").strip()
        L = len(txt)
        base = 16 if L < 300 else (14 if L < 600 else 12)

        p = tf.add_paragraph(); p.text = txt
        p.font.size = Pt(base); p.font.color.rgb = BRAND["ink"]
        return box

    def vline(slide, x, y, h):
        ln = slide.shapes.add_shape(SHAPE.RECTANGLE, x, y, Inches(0.02), h)
        ln.fill.solid(); ln.fill.fore_color.rgb = BRAND["rule"]
        ln.line.fill.background()

    def watermark_plus(slide):
        """
        Very faint first-aid '+' watermark in the canvas centre.
        """
        cx, cy = Inches(5.0), Inches(4.1)
        w, t = Inches(2.8), Inches(0.45)   # arm length / thickness

        # horizontal
        hbar = slide.shapes.add_shape(SHAPE.ROUNDED_RECTANGLE, cx - w/2, cy - t/2, w, t)
        # vertical
        vbar = slide.shapes.add_shape(SHAPE.ROUNDED_RECTANGLE, cx - t/2, cy - w/2, t, w)
        for shp in (hbar, vbar):
            shp.fill.solid()
            shp.fill.fore_color.rgb = BRAND["primary"]
            shp.fill.fore_color.brightness = 0.7   # push toward white
            shp.line.fill.background()

    def corner_L_accent(slide):
        """
        Soft brand L-shape in the top-left of the inner panel.
        """
        # short horizontal
        h = slide.shapes.add_shape(SHAPE.RECTANGLE, Inches(0.25), Inches(0.95), Inches(0.9), Inches(0.07))
        # long vertical
        v = slide.shapes.add_shape(SHAPE.RECTANGLE, Inches(0.25), Inches(0.95), Inches(0.07), Inches(1.3))
        for shp in (h, v):
            shp.fill.solid()
            shp.fill.fore_color.rgb = BRAND["primary"]
            shp.fill.fore_color.brightness = 0.5   # softer than title
            shp.line.fill.background()

    # ---------- build slides

    for ar in reports:
        slide = prs.slides.add_slide(blank)

        # Background card
        panel = slide.shapes.add_shape(
            SHAPE.RECTANGLE, Inches(0.2), Inches(0.9), Inches(9.6), Inches(6.3)
        )
        panel.fill.solid(); panel.fill.fore_color.rgb = BRAND["panelbg"]
        panel.line.color.rgb = BRAND["panelbd"]

        # Motifs first (behind text visually)
        watermark_plus(slide)
        corner_L_accent(slide)

        # Header
        add_title_bar(slide, "Accident / Incident Report")

        # Meta pill
        meta_text = " • ".join([
            f"Date: {ar.date:%d %b %Y}" if ar.date else "Date: —",
            f"Time: {ar.time.strftime('%H:%M')}" if ar.time else "Time: —",
            f"Location: {ar.location or '—'}",
        ])
        meta_pill(slide, meta_text)

        # Columns
        left_x  = Inches(0.45)
        right_x = Inches(5.1)
        top_y   = Inches(1.9)

        # Left: people + address
        section(
            slide, left_x, top_y,
            "People involved",
            f"Injured: {ar.injured_name or '—'}\n"
            f"First aider: {ar.first_aider_name or '—'}\n"
            f"Reporter: {ar.reporter_name or '—'}",
            width=Inches(4.4), height=Inches(1.45),
        )
        section(
            slide, left_x, top_y + Inches(1.6),
            "Injured address",
            ar.injured_address or "—",
            width=Inches(4.4), height=Inches(1.1),
        )

        # Divider
        vline(slide, Inches(4.95), Inches(1.85), Inches(4.45))

        # Right: narratives
        ry = top_y
        section(slide, right_x, ry,                   "What happened",               ar.what_happened,             width=Inches(4.35), height=Inches(1.35))
        section(slide, right_x, ry + Inches(1.5),     "Injuries sustained",         ar.injuries_sustained,        width=Inches(4.35), height=Inches(1.05))
        section(slide, right_x, ry + Inches(2.7),     "Actions carried out",        ar.actions_carried_out,       width=Inches(4.35), height=Inches(1.05))
        section(slide, right_x, ry + Inches(3.9),     "Actions to prevent recurrence", ar.actions_prevent_recurrence, width=Inches(4.35), height=Inches(1.05))

        # Footer tag
        footer = slide.shapes.add_textbox(Inches(0.3), Inches(7.25), Inches(9.4), Inches(0.3))
        ft = footer.text_frame; ft.clear()
        fp = ft.paragraphs[0]
        fp.text = "Confidential — Internal Safety Record"
        fp.font.size = Pt(10); fp.font.color.rgb = RGBColor(160, 165, 170)

    # ---------- return file
    from io import BytesIO
    buf = BytesIO(); prs.save(buf); buf.seek(0)
    resp = HttpResponse(
        buf.read(),
        content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )
    resp["Content-Disposition"] = 'attachment; filename="Accident reports.pptx"'
    return resp


def _accident_report_export_for_booking(request, booking, *, portal: str):
    request._accident_report_booking = booking
    return accident_report_export_pptx(request)


@require_POST
@login_required
def instructor_booking_accident_reports_export(request, booking_id):
    booking, denied = _accident_report_booking_guard(request, booking_id, portal="instructor")
    if denied:
        return denied
    return _accident_report_export_for_booking(request, booking, portal="instructor")


@require_POST
@login_required
def admin_booking_accident_reports_export(request, booking_id):
    booking, denied = _accident_report_booking_guard(request, booking_id, portal="admin")
    if denied:
        return denied
    return _accident_report_export_for_booking(request, booking, portal="admin")


@require_POST
@login_required
def accident_report_delete(request):
    """
    Bulk-delete accident reports by ids[]=...
    """
    booking = getattr(request, "_accident_report_booking", None)
    portal = getattr(request, "_accident_report_portal", "instructor")
    ids = request.POST.getlist("ids[]") or request.POST.getlist("ids")
    if not ids:
        messages.warning(request, "No reports selected.")
        if booking:
            return _accident_report_booking_redirect(booking, portal=portal)
        return redirect("instructor_bookings")

    qs = AccidentReport.objects.filter(pk__in=ids)
    if booking is not None:
        qs = qs.filter(booking=booking)
    count = qs.count()
    if count == 0:
        messages.warning(request, "No matching reports found.")
        if booking:
            return _accident_report_booking_redirect(booking, portal=portal)
        return redirect("instructor_bookings")

    qs.delete()
    messages.success(request, f"Deleted {count} report{'s' if count != 1 else ''}.")
    if booking:
        return _accident_report_booking_redirect(booking, portal=portal)
    return redirect("instructor_bookings")


def _accident_report_delete_for_booking(request, booking, *, portal: str):
    request._accident_report_booking = booking
    request._accident_report_portal = portal
    return accident_report_delete(request)


@require_POST
@login_required
def instructor_booking_accident_reports_delete(request, booking_id):
    booking, denied = _accident_report_booking_guard(request, booking_id, portal="instructor")
    if denied:
        return denied
    return _accident_report_delete_for_booking(request, booking, portal="instructor")


@require_POST
@login_required
def admin_booking_accident_reports_delete(request, booking_id):
    booking, denied = _accident_report_booking_guard(request, booking_id, portal="admin")
    if denied:
        return denied
    return _accident_report_delete_for_booking(request, booking, portal="admin")


@login_required
def accident_report_poll(request):
    """Legacy global poll — redirects empty; booking tabs use scoped poll URLs."""
    return JsonResponse({"html": ""})


@require_POST
@login_required
def accident_report_anonymise(request):
    booking = getattr(request, "_accident_report_booking", None)
    portal = getattr(request, "_accident_report_portal", "instructor")
    ids = request.POST.getlist("ids")
    if not ids:
        messages.warning(request, "Select at least one report to anonymise.")
        if booking:
            return _accident_report_booking_redirect(booking, portal=portal)
        return redirect("instructor_bookings")

    qs = AccidentReport.objects.filter(id__in=ids, anonymized_at__isnull=True)
    if booking is not None:
        qs = qs.filter(booking=booking)
    now = timezone.now()
    updated = qs.update(
        injured_name="Anonymised",
        injured_address="Anonymised",
        first_aider_name="Anonymised",
        reporter_name="Anonymised",
        anonymized_at=now,
    )

    messages.success(request, f"Anonymised {updated} report{'s' if updated != 1 else ''}.")
    if booking:
        return _accident_report_booking_redirect(booking, portal=portal)
    return redirect("instructor_bookings")


def _accident_report_anonymise_for_booking(request, booking, *, portal: str):
    request._accident_report_booking = booking
    request._accident_report_portal = portal
    return accident_report_anonymise(request)


@require_POST
@login_required
def instructor_booking_accident_reports_anonymise(request, booking_id):
    booking, denied = _accident_report_booking_guard(request, booking_id, portal="instructor")
    if denied:
        return denied
    return _accident_report_anonymise_for_booking(request, booking, portal="instructor")


@require_POST
@login_required
def admin_booking_accident_reports_anonymise(request, booking_id):
    booking, denied = _accident_report_booking_guard(request, booking_id, portal="admin")
    if denied:
        return denied
    return _accident_report_anonymise_for_booking(request, booking, portal="admin")


@require_GET
def health_check(request):
    """Lightweight probe for Render / ops — checks DB and pending migrations."""
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    status = {"database": "ok", "pending_migrations": 0}

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception as exc:
        status["database"] = str(exc)
        return JsonResponse(status, status=503)

    try:
        executor = MigrationExecutor(connection)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
        status["pending_migrations"] = len(plan)
        if plan:
            status["next_migrations"] = [
                migration.name for migration, _backwards in plan[:8]
            ]
    except Exception as exc:
        status["migration_check"] = str(exc)
        return JsonResponse(status, status=503)

    if status["pending_migrations"]:
        return JsonResponse(status, status=503)
    return JsonResponse(status)


def app_entry(request):
    """Common bookmark target — send users to the post-login router."""
    if request.user.is_authenticated:
        return redirect("post_login")
    return redirect(f"{reverse('login')}?next=/post-login/")