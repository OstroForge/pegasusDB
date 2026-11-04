import json
import math
from datetime import timedelta, datetime, time, time as dtime
from collections import defaultdict
from decimal import Decimal

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import user_passes_test, login_required
from django.contrib.auth.models import User, Group
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Q, Count, Min, Max, Avg, OuterRef, Subquery
from django.db.models.deletion import ProtectedError
from django.forms import modelformset_factory, inlineformset_factory
from django.http import Http404, HttpResponseForbidden, HttpResponse, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse, NoReverseMatch
from django.utils import timezone
from django.utils.crypto import get_random_string

from django.utils.formats import date_format
from django.views.decorators.http import require_http_methods
from math import ceil, floor
from unicorn_project.training.utils.passwords import send_initial_password_email

from .services.booking_status import auto_update_booking_statuses

from .models import (
    Business, CourseType, Personnel, Booking, TrainingLocation,
    BookingDay, DelegateRegister, CourseCompetency,
    Exam, ExamQuestion, ExamAttempt, ExamAttemptAnswer, CompetencyAssessment,
    FeedbackResponse, CertificateNameChange, Invoice, InvoiceItem, MetaSetting,
    LogoOverride,
)

from .forms import (
    Attendance, BusinessForm, CourseTypeForm, TrainingLocationForm,
    PersonnelForm, BookingForm, DelegateRegisterAdminForm,CourseCompetencyForm,
    CourseTypeForm, QuestionFormSet, AnswerFormSet, ExamForm, PersonnelProfileForm,
    delivery_personnel_queryset,
    MetaSettingForm,
    LogoOverrideForm,
)

from .views_instructor import (
    _assessment_selection_context,
    _feedback_queryset_for_booking,
    _unique_delegates_for_booking as _unique_assessment_delegates_for_booking,
    list_course_receipts_drive,
    render_invoice_pdf_via_preview,
)
from .utils.certificates import (
    build_certificates_pdf_for_booking,
    _unique_delegates_for_booking as _unique_pass_delegates_for_booking,
)
from .google_oauth import get_drive_service
from .services.dummy_bookings import (
    admin_may_view_booking,
    delete_dummy_booking_tree,
    filter_bookings_visible_to_admin,
    filter_delegate_registers_visible_to_admin,
    get_admin_booking_day_or_404,
    get_admin_booking_or_404,
)
from .utils.instructor_access import lead_invoice_status_map



# =========================
# Helpers / guards
# =========================

def get_meta(key, default=None):
    try:
        return MetaSetting.objects.get(key=key).value
    except MetaSetting.DoesNotExist:
        return default

def _parse_time_or_none(s):
    if not s:
        return None
    try:
        hh, mm = s.split(':', 1)
        return time(int(hh), int(mm))
    except Exception:
        return None

def _add_hours_to_time(t: time, hours_float: float) -> time:
    """Return a time = t + hours_float (e.g., 7.0 or 3.5 hours)."""
    base = datetime(2000, 1, 1, t.hour or 0, t.minute or 0)
    end  = base + timedelta(seconds=round(hours_float * 3600))
    return time(end.hour, end.minute)

def _hours_for_day_index(i: int, total_days: float, rows: int) -> float:
    """
    i      = 1-based day index
    rows   = ceil(total_days)
    rules  = 7h for whole days; 3.5h for the final fractional day
    """
    if rows == 1:
        return 3.5 if (0 < total_days < 1) else 7.0
    whole = floor(total_days)
    frac  = total_days - whole
    if i <= whole:
        return 7.0
    if frac > 0 and i == whole + 1:
        return 3.5
    return 7.0

def is_admin(user):
    return user.is_authenticated and (
        user.is_superuser
        or user.groups.filter(name__iexact="admin").exists()
    )

def admin_required(view_func):
    @login_required
    def _wrapped(request, *args, **kwargs):
        if is_admin(request.user):
            return view_func(request, *args, **kwargs)
        raise PermissionDenied
    return _wrapped

def _ensure_core_groups():
    for gname in ("admin", "instructor"):
        Group.objects.get_or_create(name=gname)


IMPERSONATOR_SESSION_KEY = "impersonator_user_id"
IMPERSONATED_SESSION_KEY = "impersonated_user_id"


def _login_backend_for(user):
    return getattr(user, "backend", None) or settings.AUTHENTICATION_BACKENDS[0]


@login_required
@require_http_methods(["POST"])
def admin_impersonate_user(request, pk: int):
    if not request.user.is_superuser:
        raise PermissionDenied

    target = get_object_or_404(User, pk=pk, is_active=True)

    if target.pk == request.user.pk:
        messages.info(request, "You are already signed in as this user.")
        return redirect(request.POST.get("next") or "admin_personnel_list")

    if target.is_superuser:
        messages.error(request, "For safety, superuser-to-superuser impersonation is disabled.")
        return redirect(request.POST.get("next") or "admin_personnel_list")

    original_superuser_id = request.session.get(IMPERSONATOR_SESSION_KEY) or request.user.pk

    login(request, target, backend=_login_backend_for(request.user))
    request.session[IMPERSONATOR_SESSION_KEY] = original_superuser_id
    request.session[IMPERSONATED_SESSION_KEY] = target.pk

    messages.warning(request, f"Impersonation mode enabled: viewing as {target.username}.")
    return redirect(request.POST.get("next") or "home")


@login_required
@require_http_methods(["POST"])
def admin_stop_impersonation(request):
    original_superuser_id = request.session.get(IMPERSONATOR_SESSION_KEY)
    if not original_superuser_id:
        messages.info(request, "Impersonation mode is not active.")
        return redirect(request.POST.get("next") or "home")

    original = User.objects.filter(pk=original_superuser_id, is_active=True, is_superuser=True).first()
    if not original:
        request.session.pop(IMPERSONATOR_SESSION_KEY, None)
        request.session.pop(IMPERSONATED_SESSION_KEY, None)
        messages.error(request, "Could not restore the original superuser session.")
        return redirect(request.POST.get("next") or "home")

    login(request, original, backend=_login_backend_for(request.user))
    request.session.pop(IMPERSONATOR_SESSION_KEY, None)
    request.session.pop(IMPERSONATED_SESSION_KEY, None)

    messages.success(request, "Returned to your superuser account.")
    return redirect(request.POST.get("next") or "app_admin_dashboard")

def _make_local_aware(local_date, local_time):
    """
    Combine date + time into an aware datetime in the current TIME_ZONE,
    handling DST transitions (ambiguous/non-existent times) gracefully.
    """
    tz = timezone.get_current_timezone()
    t = local_time or dtime(0, 0)
    naive = datetime.combine(local_date, t)

    try:
        return timezone.make_aware(naive, tz)
    except Exception:
        # Try resolving ambiguity (autumn clock change)
        try:
            return timezone.make_aware(naive.replace(fold=1), tz)
        except Exception:
            # Non-existent local times (spring forward): nudge +1h
            return timezone.make_aware(naive + timedelta(hours=1), tz)

# =========================
# Businesses
# =========================
@admin_required
def business_list(request):
    q = (request.GET.get("q") or "").strip()

    qs = Business.objects.all()

    if q:
        qs = qs.filter(
            Q(name__icontains=q) |
            Q(town__icontains=q) |
            Q(postcode__icontains=q)
            # If your model has these, uncomment:
            # | Q(primary_contact_name__icontains=q)
            # | Q(primary_contact_email__icontains=q)
            # Or search related contacts (adjust relation name):
            # | Q(contacts__name__icontains=q)
            # | Q(contacts__email__icontains=q)
        ).distinct()

    rows = []
    for b in qs.order_by("name"):
        rows.append({
            "cells": [f"{b.name} (Dummy)" if b.is_dummy else b.name, b.town or "", b.postcode or ""],
            "edit_url": reverse("admin_business_edit", args=[b.id]),
        })

    ctx = {
        "title": "Businesses",
        "headers": ["Name", "Town", "Postcode"],
        "rows": rows,
        "create_url": reverse("admin_business_new"),
        "q": q,                   # <- keeps the search box filled
        "list_id": "businesses",  # <- tells the template to show the search UI
    }
    return render(request, "admin/list.html", ctx)



@admin_required
def business_form(request, pk=None):
    obj = get_object_or_404(Business, pk=pk) if pk else None

    # --- Save flow ---------------------------------------------------------
    if request.method == "POST":
        form = BusinessForm(
            request.POST,
            instance=obj,
            allow_dummy_configuration=request.user.is_superuser,
        )
        if form.is_valid():
            obj = form.save()
            messages.success(request, "Changes saved.")
            if "save_return" in request.POST:
                return redirect("admin_business_list")
            return redirect("admin_business_edit", pk=obj.id)
        else:
            messages.error(request, "Please fix the errors below.")
    else:
        form = BusinessForm(instance=obj, allow_dummy_configuration=request.user.is_superuser)

    # --- Locations (left card) --------------------------------------------
    locations = []
    location_map = {}
    if obj:
        locations = TrainingLocation.objects.filter(business=obj).order_by("-is_active", "name")
        location_map = {
            str(loc.id): {
                "id": str(loc.id),
                "name": loc.name or "",
                "property_name": loc.property_name or "",
                "business_id": str(obj.id),
                "contact_name": loc.contact_name or "",
                "telephone": loc.telephone or "",
                "email": loc.email or "",
                "address_line": loc.address_line or "",
                "town": loc.town or "",
                "postcode": loc.postcode or "",
                "is_active": loc.is_active,
            }
            for loc in locations
        }

    # --- Bookings (right card) + filters ----------------------------------
    bookings = []
    instructors = []
    statuses = []

    # GET filters (won't interfere with the POST save form)
    f_status = request.GET.get("b_status", "all")
    f_instr  = request.GET.get("b_instr") or ""
    f_from   = request.GET.get("b_from") or ""
    f_to     = request.GET.get("b_to") or ""

    if obj:
        base = filter_bookings_visible_to_admin(
            request.user,
            Booking.objects
            .filter(business=obj)
            .select_related("course_type", "instructor", "training_location")
            .annotate(first_day=Min("days__date")),
        )

        # Distinct statuses present for this business (for tabs)
        statuses = (
            base.order_by()
                .values_list("status", flat=True)
                .distinct()
        )

        # Instructors who have bookings with this business (for dropdown)
        instructors = (
            Personnel.objects
            .filter(bookings__business=obj)   # related_name 'bookings' on Booking.instructor
            .distinct()
            .order_by("name")
        )

        qs = base

        # Status filter
        if f_status and f_status != "all":
            qs = qs.filter(status=f_status)

        # Instructor filter
        if f_instr:
            qs = qs.filter(instructor_id=f_instr)

        # Date range (use course_date if set, otherwise annotated first_day)
        if f_from:
            qs = qs.filter(Q(course_date__gte=f_from) | Q(first_day__gte=f_from))
        if f_to:
            qs = qs.filter(Q(course_date__lte=f_to) | Q(first_day__lte=f_to))

        bookings = (
            qs.order_by("-first_day", "-course_date", "-created_at")
              .distinct()[:100]
        )

    # --- Render ------------------------------------------------------------
    return render(request, "admin/form_business.html", {
        "title": ("Edit Business" if obj else "New Business"),
        "form": form,
        "business": obj,

        # locations card
        "locations": locations,
        "location_map_json": json.dumps(location_map, cls=DjangoJSONEncoder),

        # bookings card + filter data
        "bookings": bookings,
        "booking_statuses": statuses,
        "booking_instructors": instructors,
        "b_status": f_status,
        "b_instr": f_instr,
        "b_from": f_from,
        "b_to": f_to,

        # misc
        "back_url": reverse("admin_business_list"),
        "delete_url": reverse("admin_business_delete", args=[obj.id]) if obj else None,
    })


# =========================
# Training Locations
# =========================
@admin_required
def location_new(request, business_id):
    biz = get_object_or_404(Business, pk=business_id)
    other_locations = TrainingLocation.objects.filter(business=biz, is_active=True).order_by("name")

    if request.method == "POST":
        form = TrainingLocationForm(request.POST)
        if form.is_valid():
            loc = form.save(commit=False)
            loc.business = biz
            loc.save()  # duplicates allowed
            messages.success(request, "Location saved.")
            if "save_return" in request.POST:
                return redirect("admin_business_edit", pk=biz.id)
            return redirect("admin_location_edit", pk=loc.id)
        else:
            messages.error(request, "Please fix the errors below.")
    else:
        form = TrainingLocationForm()

    return render(request, "admin/form_location.html", {
        "title": f"Add Location – {biz.name}",
        "form": form,
        "business": biz,
        "other_locations": other_locations,
        "back_url": reverse("admin_business_edit", args=[biz.id]),
        "GOOGLE_MAPS_API_KEY": settings.GOOGLE_MAPS_API_KEY,
    })


@admin_required
@require_http_methods(["POST"])
def admin_location_create_ajax(request):
    """Create a training location from the booking form modal."""
    business_id = (request.POST.get("business_id") or "").strip()
    if not business_id:
        return JsonResponse({"ok": False, "errors": {"business_id": ["Business is required."]}}, status=400)

    biz = get_object_or_404(Business, pk=business_id)
    form = TrainingLocationForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"ok": False, "errors": form.errors}, status=400)

    loc = form.save(commit=False)
    loc.business = biz
    loc.save()

    return JsonResponse({
        "ok": True,
        "location": {
            "id": str(loc.id),
            "name": loc.name or "",
            "property_name": loc.property_name or "",
            "business_id": str(biz.id),
            "contact_name": loc.contact_name or "",
            "telephone": loc.telephone or "",
            "email": loc.email or "",
            "address_line": loc.address_line or "",
            "town": loc.town or "",
            "postcode": loc.postcode or "",
            "is_active": loc.is_active,
        },
    })


@admin_required
@require_http_methods(["POST"])
def admin_location_update_ajax(request, pk):
    """Update a training location from the admin modal."""
    loc = get_object_or_404(TrainingLocation, pk=pk)
    biz = loc.business
    form = TrainingLocationForm(request.POST, instance=loc)
    if not form.is_valid():
        return JsonResponse({"ok": False, "errors": form.errors}, status=400)

    loc = form.save(commit=False)
    loc.business = biz
    loc.save()

    return JsonResponse({
        "ok": True,
        "location": {
            "id": str(loc.id),
            "name": loc.name or "",
            "property_name": loc.property_name or "",
            "business_id": str(biz.id),
            "contact_name": loc.contact_name or "",
            "telephone": loc.telephone or "",
            "email": loc.email or "",
            "address_line": loc.address_line or "",
            "town": loc.town or "",
            "postcode": loc.postcode or "",
            "is_active": loc.is_active,
        },
    })


@admin_required
@require_http_methods(["POST"])
def admin_location_delete_ajax(request, pk):
    """Delete or archive a training location from the admin modal."""
    loc = get_object_or_404(TrainingLocation, pk=pk)
    name = loc.name

    if Booking.objects.filter(training_location=loc).exists():
        if loc.is_active:
            loc.is_active = False
            loc.save(update_fields=["is_active"])
        return JsonResponse({
            "ok": True,
            "archived": True,
            "deleted": False,
            "location": {
                "id": str(loc.id),
                "name": loc.name or "",
                "property_name": loc.property_name or "",
                "business_id": str(loc.business_id),
                "contact_name": loc.contact_name or "",
                "telephone": loc.telephone or "",
                "email": loc.email or "",
                "address_line": loc.address_line or "",
                "town": loc.town or "",
                "postcode": loc.postcode or "",
                "is_active": loc.is_active,
            },
            "message": f'Location "{name}" archived (it has existing bookings).',
        })

    loc.delete()
    return JsonResponse({
        "ok": True,
        "archived": False,
        "deleted": True,
        "location_id": str(pk),
        "message": f'Location "{name}" deleted.',
    })


@admin_required
@require_http_methods(["POST"])
def admin_location_unarchive_ajax(request, pk):
    """Restore an archived training location from the admin modal."""
    loc = get_object_or_404(TrainingLocation, pk=pk)
    name = loc.name
    if not loc.is_active:
        loc.is_active = True
        loc.save(update_fields=["is_active"])

    return JsonResponse({
        "ok": True,
        "location": {
            "id": str(loc.id),
            "name": loc.name or "",
            "property_name": loc.property_name or "",
            "business_id": str(loc.business_id),
            "contact_name": loc.contact_name or "",
            "telephone": loc.telephone or "",
            "email": loc.email or "",
            "address_line": loc.address_line or "",
            "town": loc.town or "",
            "postcode": loc.postcode or "",
            "is_active": loc.is_active,
        },
        "message": f'Location "{name}" restored.',
    })


@admin_required
def location_edit(request, pk):
    loc = get_object_or_404(TrainingLocation, pk=pk)
    biz = loc.business
    other_locations = (
        TrainingLocation.objects
        .filter(business=biz, is_active=True)
        .exclude(pk=loc.pk)
        .order_by("name")
    )

    if request.method == "POST":
        form = TrainingLocationForm(request.POST, instance=loc)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.business = biz
            obj.save()
            messages.success(request, "Location saved.")
            if "save_return" in request.POST:
                return redirect("admin_business_edit", pk=biz.id)
            return redirect("admin_location_edit", pk=obj.id)
        else:
            messages.error(request, "Please fix the errors below.")
    else:
        form = TrainingLocationForm(instance=loc)

    return render(request, "admin/form_location.html", {
        "title": f"Edit Location – {biz.name}",
        "form": form,
        "business": biz,
        "location": loc,
        "other_locations": other_locations,
        "back_url": reverse("admin_business_edit", args=[biz.id]),
        "GOOGLE_MAPS_API_KEY": settings.GOOGLE_MAPS_API_KEY,
    })


@admin_required
@require_http_methods(["POST"])
def location_unarchive(request, pk):
    loc = get_object_or_404(TrainingLocation, pk=pk)
    business_pk = loc.business_id
    name = loc.name
    if not loc.is_active:
        loc.is_active = True
        loc.save(update_fields=["is_active"])
        messages.success(request, f'Location "{name}" restored.')
    if business_pk:
        return redirect("admin_business_edit", pk=business_pk)
    return redirect("admin_business_list")


@admin_required
@require_http_methods(["POST"])
def location_delete(request, pk):
    loc = get_object_or_404(TrainingLocation, pk=pk)  # <-- TrainingLocation
    # keep the parent to return to its edit page after delete
    business_pk = getattr(loc, "business_id", None)
    name = loc.name

    # If this location has ever been used for a booking, don't hard-delete.
    # Archive it instead so historical bookings remain intact.
    if Booking.objects.filter(training_location=loc).exists():
        if loc.is_active:
            loc.is_active = False
            loc.save(update_fields=["is_active"])
        messages.success(request, f'Location “{name}” archived (it has existing bookings).')
        if business_pk:
            return redirect("admin_business_edit", pk=business_pk)
        return redirect("admin_business_list")

    # Unused -> safe to delete.
    loc.delete()
    messages.success(request, f'Location “{name}” deleted.')
    if business_pk:
        return redirect("admin_business_edit", pk=business_pk)
    return redirect("admin_business_list")

# =========================
# Course Types
# =========================
@admin_required
def course_list(request):
    rows = []
    for c in CourseType.objects.order_by("name"):
        status = "Suspended" if c.is_suspended else "Active"
        rows.append({
            "cells": [c.name, c.code, c.duration_days, status],
            "edit_url": reverse("admin_course_edit", args=[c.id]),
        })
    ctx = {
        "title": "Course Types",
        "headers": ["Name", "Code", "Duration (days)", "Status"],
        "rows": rows,
        "create_url": reverse("admin_course_new"),
    }
    return render(request, "admin/list.html", ctx)


CourseCompetencyFormSet = inlineformset_factory(
    parent_model=CourseType,
    model=CourseCompetency,
    form=CourseCompetencyForm,
    extra=0,           # <-- No automatic blank row
    can_delete=True,
)

@admin_required
def course_form(request, pk=None):
    if pk:
        ct = get_object_or_404(CourseType.objects.prefetch_related("online_exercises"), pk=pk)
        title = "Edit course type"
    else:
        ct = CourseType()
        title = "New course type"

    if request.method == "POST":
        form = CourseTypeForm(request.POST, instance=ct)
        formset = CourseCompetencyFormSet(request.POST, instance=ct, prefix="comps")

        if form.is_valid() and formset.is_valid():
            is_new = not pk
            ct = form.save(commit=False)
            if is_new or not (ct.code or "").strip():
                from .utils.course_type_code import unique_course_code
                ct.code = unique_course_code(ct.name, exclude_pk=ct.pk if ct.pk else None)
            ct.save()
            formset.instance = ct

            from .models import CourseTypeOnlineExercise

            selected_exercises = set(form.cleaned_data.get("online_exercises") or [])
            if ct.has_online_exercises:
                existing = set(ct.online_exercises.values_list("exercise_key", flat=True))
                for key in selected_exercises - existing:
                    CourseTypeOnlineExercise.objects.create(
                        course_type=ct,
                        exercise_key=key,
                    )
                ct.online_exercises.exclude(exercise_key__in=selected_exercises).delete()
            else:
                ct.online_exercises.all().delete()

            # Save competencies safely: keep any competency that is still referenced
            # by assessments instead of raising ProtectedError.
            competency_forms = formset.save(commit=False)
            blocked_deletes = []

            for obj in formset.deleted_objects:
                try:
                    obj.delete()
                except ProtectedError:
                    blocked_deletes.append(obj)

            for obj in competency_forms:
                obj.course_type = ct
                obj.save()

            if hasattr(formset, "save_m2m"):
                formset.save_m2m()

            if blocked_deletes:
                names = ", ".join([getattr(o, "name", "(unnamed)") for o in blocked_deletes])
                messages.warning(
                    request,
                    f"Some competencies were not deleted because existing assessments depend on them: {names}."
                )

            # ⬇️ REPLACED BLOCK: auto-sync exams (create missing, delete extras, clear if unticked)
            try:
                from .models import Exam
            except Exception:
                Exam = None

            if Exam and hasattr(ct, "exams"):
                if getattr(ct, "has_exam", False) and getattr(ct, "number_of_exams", None):
                    desired = int(ct.number_of_exams)
                    deleted = ct.exams.filter(sequence__gt=desired).delete()[0]
                    existing = set(ct.exams.values_list("sequence", flat=True))
                    created = 0
                    for seq in range(1, desired + 1):
                        if seq not in existing:
                            Exam.objects.create(course_type=ct, sequence=seq)  # model.save() will set title + code
                            created += 1
                    if created or deleted:
                        bits = []
                        if created: bits.append(f"created {created}")
                        if deleted: bits.append(f"removed {deleted}")
                        messages.info(request, f"Exam rows synced: {', '.join(bits)}.")
                else:
                    removed_all = ct.exams.count()
                    if removed_all:
                        ct.exams.all().delete()
                        messages.info(request, f"Removed {removed_all} exam(s) because 'Has exam?' is not ticked.")
            # ⬆️ END REPLACED BLOCK

            if is_new:
                messages.success(request, f"Course type saved. Code: {ct.code}")
            else:
                messages.success(request, "Course type saved.")

            if "save_return" in request.POST:
                return redirect("admin_course_list")
            return redirect("admin_course_edit", pk=ct.pk)
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        form = CourseTypeForm(instance=ct)
        formset = CourseCompetencyFormSet(instance=ct, prefix="comps")

    register_links = None
    feedback_links = None
    accident_report_links = None
    exam_links = []
    delegate_qr_count = 0
    if pk and (ct.code or "").strip():
        from .models import CourseTypeOnlineExercise
        from .utils.register_links import (
            course_register_full_url,
            course_register_short_url,
        )
        from .utils.feedback_links import (
            course_feedback_full_url,
            course_feedback_short_url,
        )
        from .utils.accident_report_links import (
            course_accident_report_full_url,
            course_accident_report_short_url,
        )
        from .utils.exam_links import exam_full_url, exam_short_url
        from .utils.online_exercises import course_type_has_exercise

        register_links = {
            "short_url": course_register_short_url(request, ct.code),
            "full_url": course_register_full_url(request, ct.code),
            "qr_url": reverse("public_register_short_qr", kwargs={"code": ct.code}),
            "download_name": f"register-{ct.code}.png",
        }
        feedback_links = {
            "short_url": course_feedback_short_url(request, ct.code),
            "full_url": course_feedback_full_url(request, ct.code),
            "qr_url": reverse("public_feedback_short_qr", kwargs={"code": ct.code}),
            "download_name": f"feedback-{ct.code}.png",
        }
        if course_type_has_exercise(ct, CourseTypeOnlineExercise.EXERCISE_ACCIDENT_REPORTS):
            accident_report_links = {
                "short_url": course_accident_report_short_url(request, ct.code),
                "full_url": course_accident_report_full_url(request, ct.code),
                "qr_url": reverse("public_accident_report_short_qr", kwargs={"code": ct.code}),
                "download_name": f"accident-report-{ct.code}.png",
            }
        if ct.has_exam:
            for ex in ct.exams.order_by("sequence"):
                code = (ex.exam_code or "").strip()
                if not code:
                    continue
                exam_links.append({
                    "sequence": ex.sequence,
                    "exam_code": code,
                    "title": ex.title,
                    "short_url": exam_short_url(request, code),
                    "full_url": exam_full_url(request, code),
                    "qr_url": reverse("public_exam_short_qr", kwargs={"code": code}),
                    "download_name": f"exam-{code}.png",
                })
        delegate_qr_count = (
            (1 if register_links else 0)
            + (1 if feedback_links else 0)
            + (1 if accident_report_links else 0)
            + len(exam_links)
        )

    course_code_preview_url = ""
    if not pk:
        try:
            course_code_preview_url = reverse("admin_course_code_preview")
        except NoReverseMatch:
            course_code_preview_url = "/app/admin/courses/preview-code/"

    return render(request, "admin/course_form.html", {
        "title": title,
        "form": form,
        "formset": formset,
        "object": ct,
        "register_links": register_links,
        "feedback_links": feedback_links,
        "accident_report_links": accident_report_links,
        "exam_links": exam_links,
        "delegate_qr_count": delegate_qr_count,
        "cancel_url": reverse("admin_course_list"),
        "is_new_course": pk is None,
        "course_code_preview_url": course_code_preview_url,
        "can_delete": pk is not None and ct.can_be_deleted(),
        "has_bookings": pk is not None and ct.has_ever_been_booked(),
    })


@admin_required
def course_code_preview(request):
    """Return the unique code that would be assigned for a course name (new types only)."""
    from .utils.course_type_code import unique_course_code

    name = (request.GET.get("name") or "").strip()
    if not name:
        return JsonResponse({"code": ""})
    return JsonResponse({"code": unique_course_code(name)})


@admin_required
@require_http_methods(["GET", "POST"])
def course_delete(request, pk):
    obj = get_object_or_404(CourseType, pk=pk)
    if not obj.can_be_deleted():
        messages.error(
            request,
            "This course type cannot be deleted because bookings have used it.",
        )
        return redirect("admin_course_edit", pk=obj.pk)
    if request.method == "POST":
        try:
            name = obj.name
            obj.delete()
        except ProtectedError:
            messages.error(
                request,
                "This course type cannot be deleted because it is still referenced elsewhere.",
            )
            return redirect("admin_course_edit", pk=pk)
        messages.success(request, f"Course type “{name}” deleted.")
        return redirect("admin_course_list")
    return render(request, "admin/confirm_delete.html", {
        "title": "Delete course type",
        "object": obj,
        "confirm_lead": "Are you sure you want to permanently delete this course type?",
        "back_url": reverse("admin_course_edit", args=[obj.id]),
    })

@admin_required
def admin_attempt_review(request, attempt_id: int):
    att = get_object_or_404(
        ExamAttempt.objects.select_related("exam", "exam__course_type"),
        pk=attempt_id
    )
    questions = list(att.exam.questions.order_by("order", "id").prefetch_related("answers"))
    chosen = {aa.question_id: aa for aa in att.answers.select_related("answer", "question")}
    return render(request, "admin/exam_attempt_review.html", {
        "attempt": att,
        "exam": att.exam,
        "course_type": att.exam.course_type,
        "questions": questions,
        "chosen": chosen,
    })

# =========================
# Bookings — LIST (filters + sort + pagination)
# =========================
@admin_required
def booking_list(request):
    """
    Bookings index with search + filters + sorting + pagination (25/page).
    Columns: Date, Course, Business, Status, Instructor, Ref
    Sort keys: date | course | business | status | instructor | ref
    """
    # Run the automatic status updater (off by default in settings; never on list load)
    if getattr(settings, "BOOKING_AUTO_UPDATE_ON_PAGE", False) and not request.GET.get("noauto"):
        auto_update_booking_statuses()

    qs = Booking.objects.select_related(
        "business", "training_location", "course_type", "instructor"
    )

    # ----- filters
    q           = (request.GET.get("q") or "").strip()
    statuses    = [s.strip() for s in request.GET.getlist("status") if s and s.strip()]
    business_id = (request.GET.get("business") or "").strip()
    course_id   = (request.GET.get("course_type") or "").strip()
    inst_id     = (request.GET.get("instructor") or "").strip()
    date_from   = (request.GET.get("date_from") or "").strip()
    date_to     = (request.GET.get("date_to") or "").strip()

    allowed_statuses = {
        "scheduled", "in_progress", "awaiting_closure", "completed", "cancelled",
    }
    statuses = [s for s in statuses if s in allowed_statuses]

    if q:
        qs = qs.filter(
            Q(course_reference__icontains=q) |
            Q(business__name__icontains=q)   |
            Q(course_type__name__icontains=q)|
            Q(training_location__name__icontains=q)
        )
    if statuses:
        qs = qs.filter(status__in=statuses)
    if business_id:
        qs = qs.filter(business_id=business_id)
    if course_id:
        qs = qs.filter(course_type_id=course_id)
    if inst_id:
        qs = qs.filter(instructor_id=inst_id)
    if date_from:
        qs = qs.filter(course_date__gte=date_from)
    if date_to:
        qs = qs.filter(course_date__lte=date_to)

    qs = filter_bookings_visible_to_admin(request.user, qs)

    # ----- sorting
    sort_key = (request.GET.get("o") or "date").strip()
    sort_dir = (request.GET.get("dir") or "desc").strip()  # asc|desc
    allowed = {
        "date":       "course_date",
        "course":     "course_type__name",
        "business":   "business__name",
        "status":     "status",
        "instructor": "instructor__name",
        "ref":        "course_reference",
        "invoice":    "lead_invoice_status",
    }
    order_field = allowed.get(sort_key, "course_date")

    if sort_key == "invoice":
        qs = qs.annotate(
            lead_invoice_status=Subquery(
                Invoice.objects.filter(
                    booking_id=OuterRef("pk"),
                    instructor_id=OuterRef("instructor_id"),
                ).values("status")[:1]
            )
        )
    if sort_dir == "desc":
        order_field = "-" + order_field

    # Add a stable tie-breaker so new items don't get buried on equal dates
    order_by_args = [order_field]
    if "created_at" not in order_field:
        order_by_args.append("-created_at")

    qs = qs.order_by(*order_by_args)

    # ----- pagination (25/page)
    paginator  = Paginator(qs, 25)
    page_param = request.GET.get("page")
    page_obj   = paginator.get_page(page_param)

    invoice_status_by_booking = (
        {
            b.pk: (getattr(b, "lead_invoice_status", None) or "").lower()
            for b in page_obj.object_list
        }
        if sort_key == "invoice"
        else lead_invoice_status_map(page_obj.object_list)
    )

    # ----- status pill styles
    status_style = {
        "scheduled":        {"cls": "badge bg-info text-dark"},
        "cancelled":        {"cls": "badge bg-warning text-dark"},
        "completed":        {"cls": "badge bg-success"},
        "in_progress":      {"cls": "badge bg-dark"},
        "awaiting_closure": {"cls": "badge", "style": "background-color:#6f42c1;color:#fff"},  # purple
    }

    invoice_status_style = {
        "draft":            {"cls": "badge bg-secondary"},
        "sent":             {"cls": "badge bg-primary"},
        "viewed":           {"cls": "badge bg-info text-dark"},
        "paid":             {"cls": "badge bg-success"},
        "awaiting_review":  {"cls": "badge bg-warning text-dark"},
        "rejected":         {"cls": "badge bg-danger"},
    }

    # ----- rows for the table
    from .utils.booking_details import attach_booking_schedule_labels

    page_bookings = attach_booking_schedule_labels(page_obj.object_list)
    rows = []
    for b in page_bookings:
        # Booking status pill
        st = (b.status or "")
        pill = status_style.get(st, {"cls": "badge bg-secondary"})
        label_fn = getattr(b, "get_status_display", lambda: st.replace("_", " ").title())

        # Invoice status pill (one batch query for the page, not per row)
        inv_st = invoice_status_by_booking.get(b.pk, "")
        inv_ctx = None
        if inv_st and not b.is_dummy_business:
            inv_pill = invoice_status_style.get(inv_st, {"cls": "badge bg-secondary"})
            inv_label_fn = lambda s=inv_st: s.replace("_", " ").title()
            inv_ctx = {
                "label": inv_label_fn(),
                "cls":   inv_pill.get("cls", "badge bg-secondary"),
                "style": inv_pill.get("style", ""),
            }

        rows.append({
            "date":       b.course_date,
            "dates_display": b.dates_display,
            "dates_nonconsecutive": b.dates_nonconsecutive,
            "course":     (b.course_type.name if b.course_type else ""),
            "business":   (f"{b.business.name} (Dummy)" if b.is_dummy_business else (b.business.name if b.business else "")),
            "status":     {
                "label": label_fn(),
                "cls":   pill.get("cls", "badge bg-secondary"),
                "style": pill.get("style", ""),
            },
            "invoice_status": inv_ctx,   # <<< NEW
            "instructor": (b.instructor.name if b.instructor else ""),
            "ref":        (b.course_reference or ""),
            "edit_url":   reverse("admin_booking_edit", args=[b.id]),
        })


    # ----- helper to build URLs preserving current filters
    def url_with(**overrides):
        from urllib.parse import urlencode
        params = {
            "q": q, "business": business_id, "course_type": course_id,
            "instructor": inst_id, "date_from": date_from, "date_to": date_to,
            "o": sort_key, "dir": sort_dir, "page": page_obj.number if page_obj.number else 1,
        }
        status_list = list(statuses)
        if "status" in overrides:
            status_override = overrides.pop("status")
            if isinstance(status_override, (list, tuple)):
                status_list = list(status_override)
            elif status_override:
                status_list = [status_override]
            else:
                status_list = []
        params.update(overrides)
        # drop page when changing sort to avoid empty pages
        if "o" in overrides or "dir" in overrides:
            params.pop("page", None)
        pairs = [(k, v) for k, v in params.items() if v]
        for s in status_list:
            pairs.append(("status", s))
        return f"{reverse('admin_booking_list')}?{urlencode(pairs)}"

    # ----- build sort header links
    headers = []
    for key, title in [
        ("date","Date"), ("course","Course type"), ("business","Business"),
        ("instructor","Instructor"), ("status","Course status"),  ("ref","Ref"),
        ("invoice","Invoice status"),
        
    ]:

        active = (sort_key == key)
        next_dir = "asc" if (active and sort_dir == "desc") else "desc"
        headers.append({
            "title": title,
            "url": url_with(o=key, dir=next_dir),
            "active": active,
            "dir": sort_dir if active else "",
        })

    # ----- pagination links
    prev_url = url_with(page=page_obj.previous_page_number()) if page_obj.has_previous() else None
    next_url = url_with(page=page_obj.next_page_number()) if page_obj.has_next() else None
    page_info = f"Page {page_obj.number} of {page_obj.paginator.num_pages}" if page_obj.paginator.num_pages > 1 else ""

    ctx = {
        "title": "Bookings",
        "headers": headers,
        "rows": rows,
        "create_url": reverse("admin_booking_new"),

        "filter_initial": {
            "q": q, "statuses": statuses, "business": business_id, "course_type": course_id,
            "instructor": inst_id, "date_from": date_from, "date_to": date_to
        },
        "status_choices": [
            ("scheduled", "Scheduled"),
            ("in_progress", "In progress"),
            ("awaiting_closure", "Awaiting closure"),
            ("completed", "Completed"),
            ("cancelled", "Cancelled"),
        ],
        "businesses":  Business.objects.order_by("name"),
        "course_types": CourseType.objects.order_by("name"),
        "instructors": delivery_personnel_queryset(),

        "page_obj": page_obj,
        "page_prev_url": prev_url,
        "page_next_url": next_url,
        "page_info": page_info,

        "sort_key": sort_key,
        "sort_dir": sort_dir,
    }
    return render(request, "admin/bookings_list.html", ctx)


def _admin_invoice_context(booking):
    if booking.is_dummy_business:
        return {
            "admin_invoice": None,
            "admin_invoice_base_fee": None,
            "admin_invoice_items": [],
            "admin_invoice_total": None,
            "admin_invoice_status": None,
            "admin_invoice_receipts": [],
        }

    inv = getattr(booking, "invoice", None)

    if inv is None:
        return {
            "admin_invoice": None,
            "admin_invoice_base_fee": None,
            "admin_invoice_items": [],
            "admin_invoice_total": None,
            "admin_invoice_status": "draft",
            "admin_invoice_receipts": [],
        }

    status = (inv.status or "draft").lower()

    base_fee = Decimal(str(booking.instructor_fee or 0))

    items_qs = inv.items.all().order_by("id")
    extra_total = sum((it.amount or Decimal("0")) for it in items_qs)
    total = base_fee + extra_total

    # extras only – no base row here
    items = [
        {
            "description": it.description or "",
            "amount": f"{(it.amount or Decimal('0')):.2f}",
        }
        for it in items_qs
    ]

    receipts = []
    try:
        if getattr(settings, "GOOGLE_DRIVE_ROOT_RECEIPTS", None):
            svc = get_drive_service(
                settings.GOOGLE_OAUTH_CLIENT_SECRET,
                settings.GOOGLE_OAUTH_TOKEN,
            )
            files = list_course_receipts_drive(
                svc,
                settings.GOOGLE_DRIVE_ROOT_RECEIPTS,
                getattr(booking, "course_reference", "") or "",
            )
            receipts = [
                {
                    "name": f.get("name") or "receipt",
                    "url": f.get("webViewLink") or "",
                }
                for f in files
            ]
    except Exception:
        receipts = []

    return {
        "admin_invoice": inv,
        "admin_invoice_base_fee": f"{base_fee:.2f}",
        "admin_invoice_items": items,
        "admin_invoice_total": f"{total:.2f}",
        "admin_invoice_status": status,
        "admin_invoice_receipts": receipts,
    }

@admin_required
@transaction.atomic
def booking_form(request, pk=None):
    """
    Create/edit a booking.
    - On POST: save and replace BookingDay rows from hidden JSON ('booking_days' or 'days_json').
    - On GET: send booking_days_initial_json so the days table repopulates.
    """
    obj = (
        get_admin_booking_or_404(
            request.user,
            pk,
            Booking.objects.select_related(
                "business", "course_type", "training_location", "instructor"
            ).prefetch_related(
                "days",
                "telegram_notifications",
                "telegram_notifications__personnel",
                "email_notifications",
                "email_notifications__personnel",
                "course_type__online_exercises",
            ),
        )
        if pk
        else None
    )

    if request.method == "POST":
        from .utils.booking_change_detection import (
            booking_notification_snapshot,
            detect_notifiable_booking_changes,
        )

        before_snapshot = booking_notification_snapshot(obj) if obj else None
        form = BookingForm(request.POST, instance=obj)
        if form.is_valid():
            was_new = obj is None
            booking = form.save(commit=False)

            # SAFETY: ensure DB NOT NULL field is always satisfied
            if hasattr(booking, "last_notification_type") and not booking.last_notification_type:
                booking.last_notification_type = "system"

            booking.save()

            if booking.precise_lat is not None and booking.precise_lng is not None:
                booking.admin_precise_lat = booking.precise_lat
                booking.admin_precise_lng = booking.precise_lng
                booking.save(update_fields=["admin_precise_lat", "admin_precise_lng"])


            inv_status = (request.POST.get("invoice_status_admin") or "").strip().lower()
            admin_comment = (request.POST.get("invoice_admin_comment") or "").strip()

            inv = getattr(booking, "invoice", None)
            if inv:
                fields_to_update = []

                # status – only allow these admin options
                if inv_status in {"paid", "awaiting_review", "rejected"}:
                    if (inv.status or "").lower() != inv_status:
                        inv.status = inv_status
                        fields_to_update.append("status")

                # admin comment
                if admin_comment != (inv.admin_comment or ""):
                    inv.admin_comment = admin_comment
                    fields_to_update.append("admin_comment")

                if fields_to_update:
                    inv.save(update_fields=fields_to_update)

            # Accept either key; template writes to both
            raw_days = request.POST.get("booking_days") or request.POST.get("days_json") or "[]"
            try:
                days_payload = json.loads(raw_days)
                if not isinstance(days_payload, list):
                    days_payload = []
            except json.JSONDecodeError:
                days_payload = []

            valid_delivery_ids = {
                str(person_id)
                for person_id in delivery_personnel_queryset().values_list("pk", flat=True)
            }

            invalid_day_instructor = next(
                (
                    row.get("instructor")
                    for row in days_payload
                    if (row.get("instructor") or "").strip()
                    and (row.get("instructor") or "").strip() not in valid_delivery_ids
                ),
                None,
            )

            if invalid_day_instructor:
                messages.error(
                    request,
                    "Only personnel with the instructor role can be assigned to deliver course days.",
                )
                return redirect("admin_booking_edit", pk=booking.pk)

            if days_payload:
                has_delegates = DelegateRegister.objects.filter(
                    booking_day__booking=booking
                ).exists()

                if has_delegates:
                    # Keep existing BookingDay rows (and their registers), but allow
                    # admin to update dates/times/instructor/note in place.
                    existing_days = list(
                        booking.days.all().order_by("date", "id")
                    )
                    payload_rows = [
                        r for r in days_payload
                        if (r.get("day_date") or "").strip()
                    ]

                    if len(payload_rows) != len(existing_days):
                        messages.error(
                            request,
                            "Cannot add or remove course days because delegates are already registered. "
                            "You can still change the start date (which shifts later days) and the times."
                        )
                        if "save_return" in request.POST:
                            return redirect("admin_booking_list")
                        return redirect("admin_booking_edit", pk=booking.pk)

                    for i, (d, row) in enumerate(zip(existing_days, payload_rows), start=1):
                        day_date_str = (row.get("day_date") or "").strip()
                        try:
                            new_date = datetime.fromisoformat(day_date_str).date()
                        except ValueError:
                            messages.error(request, f"Invalid date on day {i}.")
                            return redirect("admin_booking_edit", pk=booking.pk)

                        start_t = _parse_time_or_none(row.get("start_time")) or booking.start_time
                        end_t = _parse_time_or_none(row.get("end_time"))
                        if not end_t and start_t:
                            total_days = float(getattr(booking.course_type, "duration_days", 1.0) or 1.0)
                            rows = max(1, math.ceil(total_days))
                            end_t = _add_hours_to_time(
                                start_t,
                                _hours_for_day_index(i, total_days, rows),
                            )

                        inst_id = (row.get("instructor") or "").strip() or None
                        note = (row.get("note") or "").strip()

                        update_fields = []
                        if d.date != new_date:
                            d.date = new_date
                            update_fields.append("date")
                        if d.start_time != start_t:
                            d.start_time = start_t
                            update_fields.append("start_time")
                        if d.end_time != end_t:
                            d.end_time = end_t
                            update_fields.append("end_time")
                        if d.instructor_id != inst_id:
                            d.instructor_id = inst_id
                            update_fields.append("instructor")
                        if hasattr(d, "note") and d.note != note:
                            d.note = note
                            update_fields.append("note")

                        if update_fields:
                            d.save(update_fields=update_fields)

                else:
                    # No delegates -> safe to replace all BookingDay rows
                    booking.days.all().delete()

                    total_days = float(getattr(booking.course_type, "duration_days", 1.0) or 1.0)
                    rows = max(1, math.ceil(total_days))

                    for i, row in enumerate(days_payload, start=1):
                        day_date_str = (row.get("day_date") or "").strip()
                        if not day_date_str:
                            continue

                        start_t = _parse_time_or_none(row.get("start_time")) or booking.start_time
                        end_t = _parse_time_or_none(row.get("end_time"))

                        if not end_t and start_t:
                            end_t = _add_hours_to_time(
                                start_t,
                                _hours_for_day_index(i, total_days, rows),
                            )

                        inst_id = (row.get("instructor") or "").strip() or None

                        BookingDay.objects.create(
                            booking=booking,
                            date=datetime.fromisoformat(day_date_str).date(),
                            start_time=start_t,
                            end_time=end_t,
                            instructor_id=inst_id,
                        )

            from .utils.booking_notifications import notify_new_booking, notify_booking_changes

            if was_new:
                notify_new_booking(booking)
            else:
                booking_for_notify = (
                    Booking.objects.prefetch_related("days")
                    .select_related(
                        "instructor",
                        "course_type",
                        "business",
                        "training_location",
                    )
                    .get(pk=booking.pk)
                )
                changed_areas = detect_notifiable_booking_changes(
                    before_snapshot,
                    booking_for_notify,
                )
                if changed_areas:
                    notify_booking_changes(booking_for_notify, changed_areas=changed_areas)

            from .utils.travel_time import update_booking_travel_duration

            update_booking_travel_duration(booking)

            messages.success(request, "Booking saved.")
            if "save_return" in request.POST:
                return redirect("admin_booking_list")
            return redirect("admin_booking_edit", pk=booking.pk)
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        # Auto-advance statuses before showing the form
        if getattr(settings, "BOOKING_AUTO_UPDATE_ON_PAGE", False) and not request.GET.get("noauto"):
            auto_update_booking_statuses()

        form = BookingForm(instance=obj)

    # maps for client-side behaviour
    course_type_map = {
        str(ct.id): {
            "code": ct.code or "",
            "default_course_fee": str(ct.default_course_fee or ""),
            "default_instructor_fee": str(ct.default_instructor_fee or ""),
        }
        for ct in CourseType.objects.order_by("name")
    }

    location_map = {
        str(loc.id): {
            "name": loc.name or "",
            "property_name": loc.property_name or "",
            "contact_name": loc.contact_name or "",
            "telephone": loc.telephone or "",
            "email": loc.email or "",
            "address_line": loc.address_line or "",
            "town": loc.town or "",
            "postcode": loc.postcode or "",
            "business_id": str(loc.business_id),
        }
        for loc in TrainingLocation.objects.filter(is_active=True).select_related("business").order_by("name")
    }

    if obj and obj.pk:
        booking_days_initial = [
        {
            "day_date": d.date.isoformat(),
            "start_time": d.start_time.strftime("%H:%M") if d.start_time else "",
            "end_time": d.end_time.strftime("%H:%M") if d.end_time else "",
            "instructor": str(d.instructor_id or ""),   # <-- NEW FIELD
            "note": getattr(d, "note", "") or "",       # optional
        }
        for d in obj.days.all().order_by("date")
    ]

    else:
        booking_days_initial = []

    # --- Feedback data for Feedback tab (read only) ---
    fb_qs = FeedbackResponse.objects.none()
    fb_count = 0
    fb_avg = None

    if obj and obj.pk:
        # reuse the same logic as the instructor view
        from .views_instructor import _feedback_queryset_for_booking

        fb_qs = _feedback_queryset_for_booking(obj)
        fb_count = fb_qs.count()
        fb_avg = fb_qs.aggregate(avg=Avg("overall_rating"))["avg"]

    # ---------- tab + register detail support ----------
    active_tab = request.GET.get("tab", "course-info")
    if active_tab == "registers":
        active_tab = "days"
    if obj and obj.is_dummy_business and active_tab == "invoice":
        active_tab = "course-info"

    regs_day = None
    regs = None
    if obj and obj.pk:
        regs_day_id = request.GET.get("regs_day")
        if regs_day_id:
            try:
                regs_day = obj.days.get(pk=regs_day_id)
                regs = (
                    DelegateRegister.objects
                    .filter(booking_day=regs_day)
                    .select_related("instructor")
                    .order_by("id")
                )
            except BookingDay.DoesNotExist:
                pass

    # ---------- Read-only assessment matrix for admin ----------
    assessment_delegates = []
    assessment_competencies = []
    assessment_optional_slots = []
    assessment_existing = {}

    if obj and obj.pk and obj.course_type_id:
        # All outcomes so admin sees live competency ticks before Pass
        assessment_delegates = _unique_assessment_delegates_for_booking(obj)
        from .services.prior_completion import annotate_registers_prior_pass
        annotate_registers_prior_pass(obj, assessment_delegates)
        selection_ctx = _assessment_selection_context(obj, assessment_delegates)
        assessment_competencies = selection_ctx["mandatory_competencies"]
        assessment_optional_slots = selection_ctx["optional_slots"]
        required_competencies = selection_ctx["required_competencies"]

        if assessment_delegates and required_competencies:
            assessments_qs = (
                CompetencyAssessment.objects
                .filter(
                    register__in=assessment_delegates,
                    course_competency__in=required_competencies,
                )
                .select_related("register", "course_competency")
            )

            existing_map = {}
            for a in assessments_qs:
                key = (a.register_id, a.course_competency_id)
                if key not in existing_map:
                    existing_map[key] = a
            assessment_existing = existing_map

    # --- Exams / attempts for the Exams tab (read-only)
    # --- Certificates tab delegates (read-only list for admin) ---

    # Always define these so they are safe even when creating a new booking
    cert_delegates = []
    course_exams = []
    attempts_by_exam = {}
    has_exam = False

    if obj and obj.pk:
        # Certificates: Pass-only (same filter used for certificate PDF generation)
        cert_delegates = _unique_pass_delegates_for_booking(obj)

    if obj and obj.course_type:
        # All exams for this course type
        course_exams = list(
            Exam.objects.filter(course_type=obj.course_type).order_by("sequence", "id")
        )

        # We consider the course to "have exams" if either the flag is set
        # or there are actual Exam rows.
        has_exam = bool(course_exams) or getattr(obj.course_type, "has_exam", False)

        if course_exams:
            # All attempts that belong to this booking's dates + instructor
            booking_dates = list(
                BookingDay.objects.filter(booking=obj).values_list("date", flat=True)
            )

            if booking_dates:
                tmp = defaultdict(list)
                attempts_qs = (
                    ExamAttempt.objects
                    .select_related("exam")
                    .filter(
                        exam__course_type=obj.course_type,
                        instructor=obj.instructor,
                        exam_date__in=booking_dates,
                    )
                    .order_by("exam_date", "id")
                )
                for att in attempts_qs:
                    seq = getattr(att.exam, "sequence", None) or getattr(att.exam, "id")
                    tmp[seq].append(att)
                attempts_by_exam = dict(tmp)

    # If user somehow has ?tab=exams but this course has no exams,
    # force the tab back to 'registers' so template doesn't get confused.
    if active_tab == "exams" and not has_exam:
        active_tab = "course-info"

    from .utils.online_exercises import booking_accident_reports_enabled
    from .utils.accident_reports import booking_accident_reports_context

    has_accident_reports = bool(obj and obj.pk and booking_accident_reports_enabled(obj))
    if active_tab == "accident-reports" and not has_accident_reports:
        active_tab = "course-info"

    accident_reports_ctx = {}
    if has_accident_reports:
        accident_reports_ctx = booking_accident_reports_context(request, obj, portal="admin")

    # ----------------- BUILD CONTEXT -----------------
    ctx = {
        "title": ("Edit Booking" if obj else "New Booking"),
        "form": form,
        "booking": obj,
        "back_url": reverse("admin_booking_list"),
        "course_type_map_json": json.dumps(course_type_map, cls=DjangoJSONEncoder),
        "location_map_json": json.dumps(location_map, cls=DjangoJSONEncoder),
        "booking_days_initial_json": json.dumps(booking_days_initial, cls=DjangoJSONEncoder),
        "delete_url": reverse("admin_booking_delete", args=[obj.id]) if obj else None,
        "unlock_url": (
            reverse("admin_booking_unlock", args=[obj.id])
            if obj and getattr(obj, "status", "") == "completed"
            else None
        ),
        "active_tab": active_tab,
        "regs_day": regs_day,
        "regs": regs,
        # feedback (used by instructor/booking_feedback.html)
        "fb_qs": fb_qs,
        "fb_count": fb_count,
        "fb_avg": fb_avg,
        # assessments (read-only matrix)
        "assessment_delegates": assessment_delegates,
        "assessment_competencies": assessment_competencies,
        "assessment_optional_slots": assessment_optional_slots,
        "assessment_existing": assessment_existing,
        # exams (read-only list of attempts)
        "course_exams": course_exams,
        "attempts_by_exam": attempts_by_exam,
        "has_exam": has_exam,
        "has_accident_reports": has_accident_reports,
        # certificates
        "cert_delegates": cert_delegates,
        "mileage_rate": get_meta("mileage_rate", "0"),
        "show_invoice_tab": bool(obj and obj.pk and not obj.is_dummy_business),
    }

    # ---- NEW: attach admin invoice context if there is a booking ----
    if obj and obj.pk:
        ctx.update(_admin_invoice_context(obj))
        from .utils.communication_log import communication_log_entries

        try:
            ctx["communication_log_entries"] = communication_log_entries(obj)
        except Exception:
            ctx["communication_log_entries"] = []

    if accident_reports_ctx:
        ctx.update(accident_reports_ctx)

    if obj and obj.pk:
        from .utils.quick_test_links import attach_quick_test_links_context
        attach_quick_test_links_context(ctx, obj)

    return render(request, "admin/form_booking.html", ctx)


@login_required
@user_passes_test(lambda u: u.is_staff)
def admin_invoice_pdf(request, pk):
    """
    Thin wrapper: send admin users to the existing instructor invoice
    preview so we reuse the exact same PDF builder.
    """
    # Optional: you can still check an invoice exists and bounce back nicely
    booking = get_admin_booking_or_404(request.user, pk)
    if booking.is_dummy_business:
        messages.info(request, "Invoices are hidden for dummy businesses in admin screens.")
        return redirect("admin_booking_edit", pk=booking.pk)
    if not getattr(booking, "invoice", None):
        messages.error(request, "No invoice exists for this booking.")
        return redirect("admin_booking_edit", pk=booking.pk)

    # Reuse the instructor PDF view (this already generates the nice invoice)
    return redirect("instructor_invoice_preview", pk=pk)



@admin_required
def admin_booking_certificates_selected(request, pk):
    """
    Export certificates PDF for a subset of delegates on this booking.
    Called via GET with one or more ?delegate_id=<id> params.
    If none are provided, behaves like 'all certificates'.
    """
    booking = get_admin_booking_or_404(request.user, pk)

    ids = request.GET.getlist("delegate_id")
    registers = None

    if ids:
        registers = list(
            DelegateRegister.objects
            .filter(booking_day__booking=booking, id__in=ids)
            .order_by("name", "date_of_birth", "id")
        )
        if not registers:
            messages.error(request, "No matching delegates selected for certificates.")
            return redirect(f"{reverse('admin_booking_edit', args=[booking.pk])}?tab=certificates")

    result = build_certificates_pdf_for_booking(booking, registers=registers)
    if not result:
        messages.error(request, "No certificates could be generated for this booking.")
        return redirect(f"{reverse('admin_booking_edit', args=[booking.pk])}?tab=certificates")

    filename, pdf_bytes = result
    resp = HttpResponse(pdf_bytes, content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename=\"{filename}\"'
    return resp


@admin_required
@require_http_methods(["GET", "POST"])
def booking_delete(request, pk):
    booking = get_admin_booking_or_404(
        request.user,
        pk,
        Booking.objects.select_related("business", "course_type", "training_location"),
    )

    if request.method == "POST":
        ref = booking.course_reference or ""
        if booking.is_dummy_business:
            delete_dummy_booking_tree(booking)
        else:
            booking.delete()
        messages.success(request, f"Booking {ref} deleted.")
        return redirect("admin_booking_list")

    return render(request, "admin/confirm_delete_booking.html", {
        "title": "Delete booking",
        "booking": booking,
        "back_url": reverse("admin_booking_edit", args=[booking.pk]),
        # a handy link to cancel instead of delete
        "cancel_url": reverse("admin_booking_cancel", args=[booking.pk]),
    })



@admin_required
@require_http_methods(["GET", "POST"])
def booking_cancel(request, pk):
    booking = get_admin_booking_or_404(
        request.user,
        pk,
        Booking.objects.select_related("business", "course_type", "training_location"),
    )

    if request.method == "POST":
        reason = request.POST.get("reason", "").strip()
        booking.status = "cancelled"
        booking.cancel_reason = reason
        booking.cancelled_at = timezone.now()
        booking.save(update_fields=["status", "cancel_reason", "cancelled_at"])
        from .utils.booking_notifications import notify_booking_cancellation

        notify_booking_cancellation(booking)
        messages.success(request, "Booking cancelled.")
        return redirect("admin_booking_edit", pk=booking.pk)

    # GET – show confirm cancel
    return render(request, "admin/confirm_cancel.html", {
        "title": "Cancel booking",
        "booking": booking,
        "back_url": reverse("admin_booking_edit", args=[booking.pk]),
    })

# --- Reinstate a cancelled booking ---
@admin_required
@require_http_methods(["GET", "POST"])
def booking_reinstate(request, pk):
    booking = get_admin_booking_or_404(
        request.user,
        pk,
        Booking.objects.select_related("business", "course_type", "training_location"),
    )

    if request.method == "POST":
        booking.status = "scheduled"
        booking.cancel_reason = ""
        booking.cancelled_at = None
        booking.save(update_fields=["status", "cancel_reason", "cancelled_at"])
        from .utils.booking_change_detection import CHANGE_REINSTATEMENT
        from .utils.booking_notifications import notify_booking_changes

        notify_booking_changes(
            booking,
            intro="A cancelled booking assigned to you has been reinstated.",
            changed_areas=[CHANGE_REINSTATEMENT],
        )
        messages.success(request, "Booking reinstated.")
        return redirect("admin_booking_edit", pk=booking.pk)

    return render(request, "admin/confirm_reinstate.html", {
        "title": "Reinstate booking",
        "booking": booking,
        "back_url": reverse("admin_booking_edit", args=[booking.pk]),
    })


@admin_required
@require_http_methods(["POST"])
def admin_booking_telegram_send(request, pk):
    booking = get_admin_booking_or_404(
        request.user,
        pk,
        Booking.objects.select_related(
            "instructor", "course_type", "business", "training_location"
        ).prefetch_related("days"),
    )
    from .utils.booking_notifications import notify_resend_telegram

    if notify_resend_telegram(booking):
        messages.success(request, "Booking details resent via Telegram.")
    else:
        messages.warning(
            request,
            "Could not send via Telegram (instructor not linked or bot not configured).",
        )
    return redirect("admin_booking_edit", pk=booking.pk)


@admin_required
@require_http_methods(["POST"])
def admin_booking_email_send(request, pk):
    booking = get_admin_booking_or_404(
        request.user,
        pk,
        Booking.objects.select_related(
            "instructor",
            "instructor__user",
            "course_type",
            "business",
            "training_location",
        ).prefetch_related("days"),
    )
    from .utils.booking_notifications import notify_resend_email

    if notify_resend_email(booking):
        messages.success(request, "Booking details resent via email.")
    else:
        messages.warning(
            request,
            "Could not send via email (check the instructor has an email address).",
        )
    return redirect("admin_booking_edit", pk=booking.pk)


@admin_required
@require_http_methods(["GET"])
def admin_booking_course_pack_pdf(request, pk):
    booking = get_admin_booking_or_404(
        request.user,
        pk,
        Booking.objects.select_related(
            "instructor",
            "instructor__user",
            "course_type",
            "business",
            "training_location",
        ).prefetch_related("days", "course_type__exams"),
    )
    from .utils.booking_notification_pdf import booking_course_pack_pdf_response

    return booking_course_pack_pdf_response(booking)


# --- Unlock a completed booking so instructor can edit again ---
@admin_required
@require_http_methods(["GET", "POST"])
def booking_unlock(request, pk):
    """
    Admin action: reopen a completed booking for instructor editing.
    - Sets status back to 'awaiting_closure'
    - Optionally clears the two 'manual submission to follow' flags
    """
    booking = get_admin_booking_or_404(
        request.user,
        pk,
        Booking.objects.select_related("business", "course_type", "training_location"),
    )

    if request.method == "POST":
        reset_flags = request.POST.get("reset_flags") == "1"

        fields = ["status"]
        booking.status = "awaiting_closure"

        # If your Booking has these fields (you created them earlier), allow clearing them:
        if reset_flags and hasattr(booking, "closure_register_manual"):
            booking.closure_register_manual = False
            fields.append("closure_register_manual")
        if reset_flags and hasattr(booking, "closure_assess_manual"):
            booking.closure_assess_manual = False
            fields.append("closure_assess_manual")

        booking.save(update_fields=fields)
        messages.success(request, "Booking unlocked — instructor can edit again.")
        return redirect("admin_booking_edit", pk=booking.pk)

    # GET – confirm page
    return render(request, "admin/confirm_unlock.html", {
        "title": "Unlock booking",
        "booking": booking,
        "back_url": reverse("admin_booking_edit", args=[booking.pk]),
    })


# =========================
# Users (admin creates/edits)
# =========================
class AdminUserCreateForm(forms.ModelForm):
    password = forms.CharField(
        label="Initial password",
        widget=forms.PasswordInput,
        required=True,
    )
    groups = forms.ModelMultipleChoiceField(
        queryset=Group.objects.all(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Tick one or more roles (e.g., admin, instructor).",
    )
    must_change_password = forms.BooleanField(
        required=False,
        initial=True,
        help_text="Force this user to change their password at next login."
    )

    class Meta:
        model = User
        fields = ["username", "email", "password", "is_active", "groups", "must_change_password"]


class AdminUserEditForm(forms.ModelForm):
    new_password = forms.CharField(
        label="Reset password",
        widget=forms.PasswordInput,
        required=False,
        help_text="Leave blank to keep the current password.",
    )
    groups = forms.ModelMultipleChoiceField(
        queryset=Group.objects.all(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Tick one or more roles (e.g., admin, instructor).",
    )
    must_change_password = forms.BooleanField(
        required=False,
        initial=False,
        help_text="If ticked, user will be forced to change password next login."
    )

    class Meta:
        model = User
        fields = ["username", "email", "is_active", "groups", "must_change_password"]


@admin_required
def admin_user_list(request):
    _ensure_core_groups()
    users = User.objects.order_by("username")
    return render(request, "admin/users/list.html", {
        "title": "Users",
        "users": users,
    })


@admin_required
def admin_user_new(request):
    _ensure_core_groups()
    if request.method == "POST":
        form = AdminUserCreateForm(request.POST)
        if form.is_valid():
            u = User(
                username=form.cleaned_data["username"],
                email=form.cleaned_data["email"],
                is_active=form.cleaned_data["is_active"],
            )
            u.set_password(form.cleaned_data["password"])
            u.save()

            u.groups.set(form.cleaned_data["groups"])
            u.is_staff = False  # keep out of Django admin unless you want otherwise
            u.save()

            profile, _ = StaffProfile.objects.get_or_create(user=u)
            profile.must_change_password = form.cleaned_data.get("must_change_password", True)
            profile.save()

            messages.success(request, "User created.")
            if "save_return" in request.POST:
                return redirect("admin_user_list")
            return redirect("admin_user_edit", pk=u.pk)
        else:
            messages.error(request, "Please fix the errors below.")
    else:
        form = AdminUserCreateForm()

    return render(request, "admin/users/form_user.html", {
        "title": "Create user",
        "form": form,
    })


@admin_required
def admin_user_edit(request, pk: int):
    _ensure_core_groups()
    user = get_object_or_404(User, pk=pk)

    if user.is_superuser and not request.user.is_superuser:
        return HttpResponseForbidden("You cannot edit a superuser.")

    profile, _ = StaffProfile.objects.get_or_create(user=user)

    if request.method == "POST":
        form = AdminUserEditForm(request.POST, instance=user)
        if form.is_valid():
            u = form.save(commit=False)
            if form.cleaned_data.get("new_password"):
                u.set_password(form.cleaned_data["new_password"])
                profile.must_change_password = True
            u.save()
            u.groups.set(form.cleaned_data["groups"])
            u.is_staff = False
            u.save()

            if "must_change_password" in form.cleaned_data:
                profile.must_change_password = form.cleaned_data["must_change_password"]
            profile.save()

            messages.success(request, "Changes saved.")
            if "save_return" in request.POST:
                return redirect("admin_user_list")
            return redirect("admin_user_edit", pk=user.pk)
        else:
            messages.error(request, "Please fix the errors below.")
    else:
        form = AdminUserEditForm(
            instance=user,
            initial={
                "must_change_password": profile.must_change_password,
            },
        )

    return render(request, "admin/users/form_user.html", {
        "title": f"Edit user: {user.username}",
        "form": form,
        "user_obj": user,
    })


# =========================
# Instructors (personnel)
# =========================
@admin_required
def admin_personnel_list(request):
    people = Personnel.objects.select_related("user").order_by("name")

    return render(
        request,
        "admin/personnel/list.html",
        {
            "instructors": people,
            "missing_own_profile": not getattr(request.user, "personnel", None),
        },
    )

@admin_required
def admin_delegate_search(request):
    query = (request.GET.get("q") or "").strip()

    # Base queryset: all registers, with related booking info preloaded
    qs = (
        DelegateRegister.objects
        .select_related(
            "booking_day",
            "booking_day__booking",
            "booking_day__booking__course_type",
            "booking_day__booking__business",
            "booking_day__booking__training_location",
        )
    )

    # Apply text search if user typed something
    if query:
        qs = qs.filter(
            Q(name__icontains=query) |
            Q(job_title__icontains=query) |
            Q(employee_id__icontains=query) |
            Q(notes__icontains=query)
        )

    qs = filter_delegate_registers_visible_to_admin(request.user, qs)

    # Order so earlier days for a course come first
    qs = qs.order_by(
        "name",
        "date_of_birth",
        "booking_day__booking__course_reference",
        "booking_day__date",
    )

    # --- De-duplicate: one row per (delegate, course) ---
    dedup = {}
    for reg in qs:
        key = (
            (reg.name or "").strip().lower(),
            reg.date_of_birth,
            reg.booking_day.booking_id,
        )
        # keep the first (earliest date) we see for that delegate+course
        if key not in dedup:
            dedup[key] = reg

    results = list(dedup.values())

    # Sort final list by booking date DESC then name
    results.sort(
        key=lambda r: (r.booking_day.date, (r.name or "").lower()),
        reverse=True,
    )

    return render(
        request,
        "admin/delegate_search.html",
        {
            "title": "Delegate search",
            "query": query,
            "results": results,
        },
    )

@admin_required
def admin_personnel_new(request):
    if request.method == "POST":
        form = PersonnelForm(request.POST)

        if form.is_valid():
            inst = form.save(commit=False)

            # Active toggle logic
            is_active = form.cleaned_data.get("is_active", True)
            can_login = form.cleaned_data.get("can_login", False)

            inst.is_active = is_active
            inst.can_login = can_login if is_active else False

            inst.save()
            form.save_m2m()

            # --- CREATE DJANGO USER IF LOGIN ALLOWED ---
            if inst.can_login and inst.user is None:

                # Generate secure temporary password
                temp_password = get_random_string(12)

                # Create Django user
                user = User.objects.create_user(
                    username=inst.email,
                    email=inst.email,
                    password=temp_password,   # <-- correct for first login
                    is_active=True,
                    is_staff=False,
                )

                inst.user = user
                inst.must_change_password = True
                inst.save()

                # Email the password (dev or prod behaviour)
                send_initial_password_email(inst, temp_password)

                # DEV ONLY: show password on screen
                if settings.DEBUG:
                    messages.success(
                        request,
                        f"User created. Temporary password: {temp_password} "
                        f"(Email sent to {settings.DEV_CATCH_ALL_EMAIL})"
                    )
                else:
                    messages.success(request, "User created and password emailed.")

            # --- Apply groups to linked user ---
            groups = form.cleaned_data.get("groups")
            if inst.user and groups is not None:
                inst.user.groups.set(groups)
                inst.user.save()

            if "save_return" in request.POST:
                return redirect("admin_personnel_list")
            return redirect("admin_personnel_edit", pk=inst.pk)

        else:
            messages.error(request, "Please fix the errors below.")

    else:
        form = PersonnelForm()

    return render(request, "admin/personnel/form.html", {
        "title": "Add Staff Member",
        "form": form,
        "back_url": "admin_personnel_list",
        "instructor": None,  # <-- IMPORTANT so template buttons don't error
    })


@admin_required
def admin_personnel_edit(request, pk):
    inst = get_object_or_404(Personnel.objects.select_related("user"), pk=pk)
    roles_locked = bool(inst.user and inst.user.is_superuser)

    if request.method == "POST":
        # Handle 2FA operations
        if "disable_2fa" in request.POST:
            if inst.totp_secret:
                inst.totp_secret = None
                inst.save(update_fields=['totp_secret'])
                messages.success(request, f"2FA disabled for {inst.name}.")
            return redirect("admin_personnel_edit", pk=inst.pk)
        
        if "reset_password" in request.POST:
            if inst.user:
                temp_password = get_random_string(12)
                inst.user.set_password(temp_password)
                inst.user.is_active = True
                inst.user.save(update_fields=['password', 'is_active'])
                inst.must_change_password = True
                inst.save(update_fields=['must_change_password'])
                send_initial_password_email(inst, temp_password)
                messages.success(request, f"Password reset for {inst.name}. Email sent.")
            return redirect("admin_personnel_edit", pk=inst.pk)
        
        form = PersonnelForm(request.POST, instance=inst, lock_groups=roles_locked)

        if form.is_valid():
            inst = form.save(commit=False)

            # Consistent toggle behaviour
            is_active = form.cleaned_data.get("is_active", True)
            can_login = form.cleaned_data.get("can_login", False)

            inst.is_active = is_active
            inst.can_login = can_login if is_active else False

            inst.save()
            form.save_m2m()

            groups = form.cleaned_data.get("groups")

            # --- USER LOGIC --------------------------------------------------
            if inst.can_login:

                if inst.user is None:
                    # CREATE USER (same as new view)
                    temp_password = get_random_string(12)

                    user = User.create_user(
                        username=inst.email,
                        email=inst.email,
                        password=temp_password,
                        is_active=True,
                        is_staff=False,
                    )

                    inst.user = user
                    inst.must_change_password = True
                    inst.save()

                    # Email temp password
                    send_initial_password_email(inst, temp_password)

                    if settings.DEBUG:
                        messages.success(
                            request,
                            f"Login enabled. Temporary password: {temp_password} "
                            f"(Email sent to DEV catch-all {settings.DEV_CATCH_ALL_EMAIL})"
                        )

                else:
                    # UPDATE EXISTING USER DETAILS
                    inst.user.is_active = True
                    inst.user.email = inst.email
                    inst.user.username = inst.email
                    inst.user.save()

                # SYNC GROUPS
                if groups is not None and not roles_locked:
                    inst.user.groups.set(groups)
                    inst.user.save()

            else:
                # Cannot login → deactivate user
                if inst.user:
                    inst.user.is_active = False
                    inst.user.save()

            messages.success(request, "Changes saved.")

            if "save_return" in request.POST:
                return redirect("admin_personnel_list")

            return redirect("admin_personnel_edit", pk=inst.pk)

        else:
            messages.error(request, "Please fix the errors below.")

    else:
        form = PersonnelForm(instance=inst, lock_groups=roles_locked)

        if inst.user:
            form.fields["groups"].initial = inst.user.groups.all()

    return render(request, "admin/personnel/form.html", {
        "title": f"Edit Staff Member — {inst.name}",
        "form": form,
        "back_url": "admin_personnel_list",
        "instructor": inst,
        "roles_locked": roles_locked,
    })

@admin_required
@require_http_methods(["GET", "POST"])
def admin_certificate_name_edit(request, reg_pk: int):
    """
    Edit the certificate_name for a single DelegateRegister.
    - Does NOT change DelegateRegister.name.
    - Every change is logged with a reason.
    """
    register = get_object_or_404(
        DelegateRegister.objects.select_related("booking_day__booking"),
        pk=reg_pk,
    )
    booking = register.booking_day.booking
    if not admin_may_view_booking(request.user, booking):
        raise Http404("Register not found.")

    if request.method == "POST":
        # Checkbox: revert to original delegate name?
        revert = request.POST.get("revert_to_original") in ("1", "on", "true", "True")

        # If reverting, ignore the custom field and use the original delegate name
        if revert:
            new_name = (register.name or "").strip()
        else:
            new_name = (request.POST.get("certificate_name") or "").strip()

        reason = (request.POST.get("reason") or "").strip()

        if not new_name:
            messages.error(request, "Please enter the name to show on the certificate.")
        elif not reason:
            messages.error(request, "Please provide a reason for this change.")
        else:
            old_display = register.certificate_display_name()

            if new_name == old_display:
                messages.info(request, "The certificate name is unchanged.")
            else:
                # Log every change with timestamp (changed_at) and user
                CertificateNameChange.objects.create(
                    register=register,
                    old_name=old_display,
                    new_name=new_name,
                    reason=reason,
                    changed_by=request.user if request.user.is_authenticated else None,
                )

                # If reverting, clear the override so we fall back to the original name
                if revert:
                    register.certificate_name = ""
                else:
                    register.certificate_name = new_name

                register.save(update_fields=["certificate_name"])

                messages.success(request, "Certificate name updated.")
                return redirect(
                    f"{reverse('admin_booking_edit', args=[booking.pk])}?tab=certificates"
                )


    # GET (or POST with errors) – show form + history
    changes = list(register.certificate_name_changes.all())

    return render(request, "admin/certificate_name_edit.html", {
        "title": "Edit certificate name",
        "register": register,
        "booking": booking,
        "current_display_name": register.certificate_display_name(),
        "changes": changes,
        "back_url": f"{reverse('admin_booking_edit', args=[booking.pk])}?tab=certificates",
    })

@admin_required
@require_http_methods(["GET", "POST"])
def admin_personnel_delete(request, pk):
    inst = get_object_or_404(Instructor, pk=pk)
    if request.method == "POST":
        inst.delete()
        messages.success(request, "Instructor deleted.")
        return redirect("admin_personnel_list")
    return render(request, "admin/confirm_delete.html", {
        "title": "Delete instructor",
        "object": inst,
        "back_url": reverse("admin_instructor_edit", args=[inst.pk]),
    })


# =========================
# Business / Booking delete (business)
# =========================
@admin_required
@require_http_methods(["GET", "POST"])
def business_delete(request, pk):
    obj = get_object_or_404(Business, pk=pk)

    if request.method == "POST":
        try:
            name = obj.name
            obj.delete()
        except ProtectedError:
            messages.error(
                request,
                f"“{obj.name}” can’t be deleted because it has related records."
            )
            return redirect("admin_business_edit", pk=obj.pk)

        messages.success(request, f"Business “{name}” deleted.")
        return redirect("admin_business_list")

    # GET -> confirm page
    return render(request, "admin/confirm_delete.html", {
        "title": "Delete business",
        "object": obj,
        "back_url": reverse("admin_business_edit", args=[obj.id]),  # uuid ok
        "post_url": reverse("admin_business_delete", args=[obj.id]),
        "warning": "This action cannot be undone.",
    })

@admin_required
@require_http_methods(["GET", "POST"])
def booking_day_registers(request, pk):
    """
    Admin page to view/edit delegates for a specific BookingDay.
    """
    day = get_admin_booking_day_or_404(
        request.user,
        pk,
        BookingDay.objects.select_related("booking", "booking__course_type", "booking__business"),
    )

    # Formset to edit multiple delegates quickly
    DelegateFormSet = modelformset_factory(
        model=DelegateRegister,
        fields=["name", "date_of_birth", "job_title", "employee_id", "date"],
        extra=0,
        can_delete=True,
        widgets={
            "date_of_birth": forms.DateInput(attrs={"type": "date", "class": "form-control form-control-sm"}),
            "date": forms.DateInput(attrs={"type": "date", "class": "form-control form-control-sm"}),
        },
    )

    if request.method == "POST":
        formset = DelegateFormSet(request.POST, queryset=day.delegateregister_set.order_by("name"))
        if formset.is_valid():
            # Apply deletions and updates
            instances = formset.save(commit=False)
            for obj in instances:
                obj.booking_day = day
                obj.save()
            # Delete any rows flagged for deletion
            for obj in formset.deleted_objects:
                obj.delete()

            messages.success(request, "Registers saved.")
            if "save_return" in request.POST:
                return redirect("admin_booking_edit", pk=day.booking_id)
            return redirect("admin_booking_day_registers", pk=day.pk)
        else:
            messages.error(request, "Please fix the errors below.")
    else:
        formset = DelegateFormSet(queryset=day.delegateregister_set.order_by("name"))

    return render(request, "admin/booking_day_registers.html", {
        "title": "Registers",
        "day": day,
        "booking": day.booking,
        "formset": formset,
        "back_url": reverse("admin_booking_edit", args=[day.booking_id]),
    })

@admin_required
def booking_day_registers(request, pk: int):
    """
    Admin: read-only view of all delegate registers for a given BookingDay.

    If requested with ?inline=1 or via AJAX (X-Requested-With: XMLHttpRequest),
    return just the delegates table so it can be injected into the Registers tab
    without reloading the whole page.
    """
    day = get_admin_booking_day_or_404(
        request.user,
        pk,
        BookingDay.objects.select_related(
            "booking__course_type",
            "booking__business",
            "booking__instructor",
        ),
    )

    registers = list(
        DelegateRegister.objects.filter(booking_day=day)
        .select_related("instructor")
        .order_by("id")
    )
    from .services.prior_completion import annotate_registers_prior_pass
    annotate_registers_prior_pass(day.booking, registers)

    # Inline / AJAX request → return only the table fragment
    if (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or request.GET.get("inline") == "1"
    ):
        return render(
            request,
            "admin/partials/booking_day_registers_table.html",
            {
                "day": day,
                "registers": registers,
            },
        )

    # Full-page fallback (e.g. if you ever browse to the URL directly)
    heading_bits = []
    if day.booking and day.booking.course_type:
        heading_bits.append(day.booking.course_type.name)
    if day.date:
        heading_bits.append(date_format(day.date, "j M Y"))
    if day.booking and day.booking.business:
        heading_bits.append(day.booking.business.name)
    if day.booking and day.booking.instructor:
        heading_bits.append(f"Instructor: {day.booking.instructor.name}")
    heading = " – ".join(heading_bits) if heading_bits else "Registers"

    return render(
        request,
        "admin/booking_day_registers.html",
        {
            "title": f"Registers — {heading}",
            "day": day,
            "registers": registers,
            "back_url": (
                reverse("admin_booking_edit", args=[day.booking_id])
                if day.booking_id
                else reverse("admin_booking_list")
            ),
        },
    )

@admin_required
@require_http_methods(["GET", "POST"])
def delegate_register_delete(request, pk: int):
    reg = get_object_or_404(DelegateRegister.objects.select_related("booking_day__booking"), pk=pk)
    if not admin_may_view_booking(request.user, reg.booking_day.booking):
        raise Http404("Register not found.")
    back = reverse("admin_booking_day_registers", args=[reg.booking_day_id]) if reg.booking_day_id else reverse("admin_booking_list")

    if request.method == "POST":
        reg.delete()
        messages.success(request, "Delegate removed from register.")
        return redirect(back)

    # GET — confirm screen
    return render(request, "admin/confirm_delete.html", {
        "title": "Remove delegate from register",
        "confirm_lead": "Are you sure you want to remove this delegate from the register:",
        "object": reg.name or "Unnamed delegate",
        "back_url": back,
    })

@admin_required
def exam_form(request, pk):
    exam = get_object_or_404(Exam.objects.select_related("course_type"), pk=pk)
    course = exam.course_type

    if request.method == "POST":
        # We render "correct answer" as a radio group per question (more obvious that only one can be selected).
        # Convert that radio selection back into the boolean is_correct fields expected by the AnswerFormSet.
        post = request.POST.copy()

        # For each posted answer formset prefix (a-0, a-1, ...), map prefix-correct -> prefix-<n>-is_correct
        # Radio input name is "{prefix}-correct" with value being the answer form index.
        for key in list(post.keys()):
            if key.endswith("-correct") and key.startswith("a-"):
                prefix = key[:-len("-correct")]
                selected = post.get(key)
                try:
                    selected_i = int(selected)
                except (TypeError, ValueError):
                    selected_i = None

                try:
                    total = int(post.get(f"{prefix}-TOTAL_FORMS") or 0)
                except (TypeError, ValueError):
                    total = 0

                for j in range(total):
                    field_name = f"{prefix}-{j}-is_correct"
                    if selected_i is not None and j == selected_i:
                        post[field_name] = "on"
                    else:
                        # ensure unchecked boxes aren't lingering in POST
                        if field_name in post:
                            del post[field_name]

        eform = ExamForm(post, instance=exam)
        qset  = QuestionFormSet(post, request.FILES, instance=exam, prefix="q")

        # Build answer formsets; bind only if management keys exist for that prefix
        answer_sets = []
        for i, qf in enumerate(qset.forms):
            prefix = f"a-{i}"
            if f"{prefix}-TOTAL_FORMS" in post:
                afs = AnswerFormSet(post, instance=qf.instance, prefix=prefix)
            else:
                afs = AnswerFormSet(instance=qf.instance, prefix=prefix)  # unbound (no answers posted)
            answer_sets.append(afs)

        # Validate: only bound answer sets participate in validation
        is_valid = eform.is_valid() and qset.is_valid()
        for afs in answer_sets:
            if afs.is_bound:
                is_valid = is_valid and afs.is_valid()

        if is_valid:
            # Save exam fields (including our new raw inputs)
            exam_obj = eform.save(commit=False)

            # NEW: collect pass/viva inputs regardless of whether ExamForm has them
            try:
                exam_obj.pass_mark_percent = int(post.get("pass_mark_percent") or 80)
            except (TypeError, ValueError):
                exam_obj.pass_mark_percent = 80

            allow_viva = (post.get("allow_viva") in ("on", "true", "1", "True"))
            exam_obj.allow_viva = allow_viva

            v_raw = post.get("viva_pass_percent")
            if allow_viva and v_raw not in (None, "",):
                try:
                    exam_obj.viva_pass_percent = int(v_raw)
                except (TypeError, ValueError):
                    exam_obj.viva_pass_percent = None
            else:
                exam_obj.viva_pass_percent = None

            # model.save() will run clean() and enforce ranges + compute title/code
            exam_obj.save()

            # Save questions
            qset.instance = exam_obj
            qset.save()   # ensure new questions get PKs

            # Save answers for any indices that were posted
            for i, qf in enumerate(qset.forms):
                prefix = f"a-{i}"
                if f"{prefix}-TOTAL_FORMS" in post:
                    if qf.instance.pk:
                        qf.instance.refresh_from_db()
                    afs = AnswerFormSet(post, instance=qf.instance, prefix=prefix)
                    if afs.is_valid():
                        afs.save()

            messages.success(request, "Exam updated.")
            if "save_return" in post:
                return redirect("admin_course_edit", pk=course.pk)
            return redirect("admin_exam_edit", pk=exam_obj.pk)
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        eform = ExamForm(instance=exam)
        qset  = QuestionFormSet(instance=exam, prefix="q")
        answer_sets = [
            AnswerFormSet(instance=qf.instance, prefix=f"a-{i}")
            for i, qf in enumerate(qset.forms)
        ]

    qa_pairs = [{"qform": qf, "aformset": afs} for qf, afs in zip(qset.forms, answer_sets)]

    return render(request, "admin/exam_form.html", {
        "title": f"Edit exam — {course.name} ({exam.exam_code or ''})",
        "exam": exam,
        "course": course,
        "exam_form": eform,
        "q_formset": qset,
        "qa_pairs": qa_pairs,
        "cancel_url": reverse("admin_course_edit", args=[course.pk]),
    })


def _provision_exams_for_course(ct):
    """
    Create missing Exam(sequence=1..N) rows for this CourseType.
    Returns the number of exams created. Safe to call repeatedly.
    """
    try:
        from .models import Exam
    except Exception:
        return 0

    if not getattr(ct, "has_exam", False) or not getattr(ct, "number_of_exams", None):
        return 0

    if not hasattr(ct, "exams"):
        return 0

    existing = set(ct.exams.values_list("sequence", flat=True))
    created = 0
    for seq in range(1, int(ct.number_of_exams) + 1):
        if seq not in existing:
            Exam.objects.create(course_type=ct, sequence=seq)  # let model compute title/code
            created += 1
    return created

@login_required
def admin_dashboard(request):
    # Start with username as a fallback
    display_name = request.user.username

    # Prefer first_name if you start using it
    if request.user.first_name:
        display_name = request.user.first_name

    # If there is a linked Personnel with a name, prefer that
    try:
        person = Personnel.objects.get(user=request.user)
        if person.name:
            display_name = person.name.split()[0]  # first word only
    except Personnel.DoesNotExist:
        pass

    return render(request, "admin/admin_dashboard.html", {
        "display_name": display_name,
    })




@login_required
def api_courses_today(request):
    today = timezone.localdate()

    bookings = filter_bookings_visible_to_admin(
        request.user,
        Booking.objects.filter(
            days__date=today,
            status__in=["scheduled", "in_progress", "awaiting_closure"],
        )
        .select_related("instructor", "business", "training_location", "course_type")
        .distinct(),
    )
    from .utils.booking_details import attach_booking_schedule_labels
    bookings = attach_booking_schedule_labels(bookings)

    results = []
    for b in bookings:
        results.append(
            {
                "id": b.id,
                "reference": getattr(b, "course_reference", str(b.id)),
                "course": str(b.course_type) if b.course_type else "",
                "instructor": str(b.instructor) if b.instructor else "",
                "business": str(b.business) if b.business else "",
                "location": str(b.training_location) if b.training_location else "",
                "date": b.course_date.isoformat() if b.course_date else "",
                "dates_label": getattr(b, "dates_display", "") or "",
                "dates_nonconsecutive": bool(getattr(b, "dates_nonconsecutive", False)),
                # 🔽 use the booking detail route, not course type
                "url": reverse("admin_booking_edit", kwargs={"pk": b.id}),
            }
        )

    return JsonResponse({"data": results})

@login_required
def api_courses_awaiting_closure(request):
    today = timezone.localdate()

    bookings = filter_bookings_visible_to_admin(
        request.user,
        Booking.objects.filter(
            status="awaiting_closure",
            days__date__lt=today,
        )
        .select_related("instructor", "business", "training_location", "course_type")
        .prefetch_related("days")
        .distinct(),
    )
    from .utils.booking_details import attach_booking_schedule_labels
    bookings = attach_booking_schedule_labels(bookings)

    results = []
    for b in bookings:
        # Determine completed date = last course day
        dates = getattr(b, "schedule_dates", None) or []
        last_day = dates[-1] if dates else None

        results.append(
            {
                "id": b.id,
                "reference": getattr(b, "course_reference", str(b.id)),
                "course": str(b.course_type) if b.course_type else "",
                "instructor": str(b.instructor) if b.instructor else "",
                "business": str(b.business) if b.business else "",
                "location": str(b.training_location) if b.training_location else "",
                "completed": last_day.isoformat() if last_day else "",
                "dates_label": getattr(b, "dates_display", "") or "",
                "dates_nonconsecutive": bool(getattr(b, "dates_nonconsecutive", False)),
                "url": reverse("admin_booking_edit", kwargs={"pk": b.id}),
            }
        )

    return JsonResponse({"data": results})

@login_required
def api_courses_in_7_days(request):
    today = timezone.localdate()
    target = today + timedelta(days=7)

    bookings = filter_bookings_visible_to_admin(
        request.user,
        Booking.objects.filter(
            days__date__gt=today,
            days__date__lte=target,
            status="scheduled",
        )
        .select_related("instructor", "business", "training_location", "course_type")
        .prefetch_related("days")
        .distinct(),
    )
    from .utils.booking_details import attach_booking_schedule_labels
    bookings = attach_booking_schedule_labels(bookings)

    results = []
    for b in bookings:
        next_day = min(
            (day for day in (getattr(b, "schedule_dates", None) or []) if today < day <= target),
            default=b.course_date,
        )

        results.append(
            {
                "id": b.id,
                "reference": getattr(b, "course_reference", str(b.id)),
                "course": str(b.course_type) if b.course_type else "",
                "instructor": str(b.instructor) if b.instructor else "",
                "business": str(b.business) if b.business else "",
                "location": str(b.training_location) if b.training_location else "",
                "date": next_day.isoformat() if next_day else "",
                "dates_label": getattr(b, "dates_display", "") or "",
                "dates_nonconsecutive": bool(getattr(b, "dates_nonconsecutive", False)),
                "url": reverse("admin_booking_edit", kwargs={"pk": b.id}),
            }
        )

    results.sort(key=lambda item: item["date"] or "")

    return JsonResponse({"data": results})

STATUS_BADGES = {
    "sent": '<span class="badge bg-success">Sent</span>',
    "awaiting_review": '<span class="badge bg-warning text-dark">Awaiting review</span>',
    "draft": '<span class="badge bg-secondary">Draft</span>',
    "viewed": '<span class="badge bg-info text-dark">Viewed</span>',
    "paid": '<span class="badge bg-primary">Paid</span>',
    "rejected": '<span class="badge bg-danger">Rejected</span>',
}

@login_required
def api_outstanding_invoices(request):
    invoices = (
        Invoice.objects
        .filter(status__in=["sent", "awaiting_review"], booking__business__is_dummy=False)
        .select_related(
            "booking",
            "booking__course_type",
            "booking__business",
            "booking__training_location",
            "booking__instructor",
        )
    )

    data = []

    for inv in invoices:
        data.append({
            "course": getattr(inv.booking.course_type, "name", "–"),
            "business": getattr(inv.booking.business, "name", "–"),
            "instructor": inv.instructor.name,
            "status": inv.status,
            "status_badge": STATUS_BADGES.get(inv.status, inv.status),
            "sent_date": inv.invoice_date.strftime("%Y-%m-%d") if inv.invoice_date else "–",

            # 🔥 FIXED URL — Copy this line exactly
            "url": f"/app/admin/bookings/{inv.booking.id}/?tab=invoice",
        })

    return JsonResponse({"data": data})

@admin_required
def admin_personnel_resend_password(request, pk):
    inst = get_object_or_404(Personnel, pk=pk)

    if not inst.user:
        messages.error(request, "This staff member does not have a login account.")
        return redirect("admin_personnel_edit", pk=pk)

    temp_password = get_random_string(12)

    inst.user.set_password(temp_password)
    inst.user.is_active = True
    inst.user.save()

    inst.must_change_password = True
    inst.save()

    send_initial_password_email(inst, temp_password)

    if settings.DEBUG:
        messages.success(
            request,
            f"New temporary password: {temp_password} "
            f"(Email sent to DEV catch-all {settings.DEV_CATCH_ALL_EMAIL})"
        )
    else:
        messages.success(request, "A new temporary password email has been sent.")

    return redirect("admin_personnel_edit", pk=pk)

@admin_required
def admin_personnel_delete(request, pk):
    inst = get_object_or_404(Personnel, pk=pk)

    if inst.user and inst.user.is_superuser:
        messages.error(request, "You cannot delete a superuser-linked staff record.")
        return redirect("admin_personnel_edit", pk=pk)

    user = inst.user  # store reference

    # Delete Personnel first
    inst.delete()

    # Now safe to delete/deactivate the User
    if user:
        try:
            user.delete()
        except ProtectedError:
            # If some other model protects it, fall back to deactivation
            user.is_active = False
            user.save()

    messages.success(request, "Staff member deleted permanently.")
    return redirect("admin_personnel_list")

@login_required
def password_change_done_and_clear(request):
    profile = getattr(request.user, "personnel", None)

    if profile:
        profile.must_change_password = False
        profile.save()

    messages.success(request, "Your password has been changed successfully.")
    return redirect("home")

@admin_required
def api_instructor_postcode(request, pk):
    inst = get_object_or_404(Personnel, pk=pk)
    return JsonResponse({"postcode": inst.postcode or ""})

@admin_required
def site_settings(request):
    from .services.logos import (
        available_logo_filenames,
        build_logo_catalog,
        get_current_logo,
        load_schedule_spec,
        save_schedule_spec,
    )

    if request.method == "POST" and request.POST.get("action") == "save_logo_schedule":
        schedule_raw = (request.POST.get("schedule_json") or "").strip()
        try:
            spec = json.loads(schedule_raw) if schedule_raw else {"rules": [], "default": "logo.png"}
        except json.JSONDecodeError:
            messages.error(request, "Logo schedule JSON is not valid.")
            return redirect("admin_site_settings")

        if not isinstance(spec, dict):
            messages.error(request, "Logo schedule must be a JSON object.")
            return redirect("admin_site_settings")
        if "rules" not in spec or not isinstance(spec["rules"], list):
            messages.error(request, 'Logo schedule must include a "rules" array.')
            return redirect("admin_site_settings")
        default_logo = (request.POST.get("default_logo") or spec.get("default") or "logo.png").strip()
        spec["default"] = default_logo
        save_schedule_spec(spec)
        messages.success(request, "Logo schedule saved.")
        return redirect("admin_site_settings")

    schedule_spec = load_schedule_spec()
    schedule_json = json.dumps(schedule_spec, indent=2)
    current_logo = get_current_logo()

    return render(
        request,
        "admin/site_settings.html",
        {
            "title": "Site settings",
            "settings": MetaSetting.objects.order_by("key"),
            "logo_overrides": LogoOverride.objects.order_by("priority", "-id"),
            "current_logo": current_logo,
            "schedule_json": schedule_json,
            "schedule_default": schedule_spec.get("default", "logo.png"),
            "logo_files": available_logo_filenames(),
            "logo_catalog": build_logo_catalog(schedule_spec, current_logo=current_logo),
        },
    )


@admin_required
@transaction.atomic
def meta_settings_edit(request, pk):
    # pk == 0 means “add new”
    if pk == 0:
        setting = None
    else:
        setting = get_object_or_404(MetaSetting, pk=pk)

    if request.method == "POST":
        form = MetaSettingForm(request.POST, instance=setting)
        if form.is_valid():
            form.save()
            messages.success(request, "Setting saved.")
            return redirect("admin_site_settings")
    else:
        form = MetaSettingForm(instance=setting)

    return render(request, "admin/meta_settings_edit.html", {
        "form": form,
        "setting": setting,
    })


@admin_required
@transaction.atomic
def logo_override_edit(request, pk):
    if pk == 0:
        override = None
    else:
        override = get_object_or_404(LogoOverride, pk=pk)

    if request.method == "POST":
        form = LogoOverrideForm(request.POST, instance=override)
        if form.is_valid():
            form.save()
            messages.success(request, "Logo override saved.")
            return redirect("admin_site_settings")
    else:
        form = LogoOverrideForm(instance=override)

    return render(
        request,
        "admin/logo_override_edit.html",
        {
            "title": "Add logo override" if override is None else "Edit logo override",
            "form": form,
            "override": override,
        },
    )


@admin_required
@require_http_methods(["POST"])
def logo_override_delete(request, pk):
    override = get_object_or_404(LogoOverride, pk=pk)
    override.delete()
    messages.success(request, "Logo override deleted.")
    return redirect("admin_site_settings")
