from pathlib import Path
import io, contextlib, os, re, mimetypes, logging
from urllib.parse import urlencode
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from datetime import time as clock_time, timedelta, datetime
from django.contrib import messages
from django.conf import settings
from django.core.mail import EmailMessage
from django.core.paginator import Paginator
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import transaction
from django.db.models import Count, Max, Avg, Q
from django.forms import modelformset_factory
from django.http import JsonResponse, HttpResponseForbidden, FileResponse, HttpResponseNotAllowed, HttpResponse, Http404, HttpResponseServerError, HttpRequest, HttpResponseBadRequest
from django.shortcuts import redirect, render, get_object_or_404
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.urls import reverse, resolve
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.formats import date_format
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.timezone import now, localtime
from django.views.decorators.http import require_POST, require_http_methods
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from googleapiclient.errors import HttpError
from .google_oauth import get_drive_service
from .drive_paths import ensure_path
from itertools import zip_longest
from .utils.emailing import send_admin_email
from .utils.invoice_html import render_invoice_pdf_from_html, resolve_admin_email
from .utils.locks import guard_unlocked
from .utils.course_docs import email_all_course_docs_to_admin
from io import BytesIO
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib import colors
from statistics import mean
from django.template.loader import render_to_string
import subprocess, tempfile, os
from .models import Business, Personnel, Booking, BookingDay, CompetencyAssessment, DelegateRegister, CourseType, CourseCompetency, FeedbackResponse, Invoice, InvoiceItem, Exam, ExamAttempt, ExamAttemptAnswer, CourseOutcome, Resource, InstructorAvailability, InstructorAvailabilityPattern
from .forms import DelegateRegisterInstructorForm, BookingNotesForm, DummyBookingQuickCreateForm
from .utils.invoice import (
    get_invoice_template_path,
    render_invoice_pdf,      # returns (bytes, filename); falls back to DOCX if PDF conversion not available
    render_invoice_file,     # if you want to choose prefer_pdf=False somewhere
    send_invoice_email,      # email helper with dev/admin routing
)
from .utils.certificates import (
    build_certificates_pdf_for_booking,
    _unique_delegates_for_booking as cert_pass_delegates_for_booking,
)
from .services.dummy_bookings import delete_dummy_booking_tree
from .services.assessment_exams import (
    build_assessment_exam_matrix,
    recompute_delegate_outcome,
    recompute_outcomes_for_exam_attempt,
)
from .utils.instructor_access import (
    booking_has_invoice_for,
    instructor_bookings_queryset,
    instructor_can_access_booking,
    instructor_can_access_day,
    instructor_visible_booking_ids,
    lead_invoice_status_map,
)

SAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9 _\-\(\)\.&]")

def _get_instructor(user):
    """Return the Personnel record linked to this user (if any)."""
    if not user.is_authenticated:
        return None
    return getattr(user, "personnel", None)


def _safe_next_url(request, fallback_url: str) -> str:
    candidate = request.POST.get("next") or request.GET.get("next")
    if candidate and url_has_allowed_host_and_scheme(
        url=candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return fallback_url


def _parse_posted_clock(raw):
    """Read an <input type="time"> value. Browsers send HH:MM or HH:MM:SS."""
    text = (raw or "").strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def _open_exam_sequence_for_booking(days, course_exams):
    """
    Which exam accordion to open while the instructor waits for submissions.
    Course day 1 opens exam 1, day 2 opens exam 2, and so on.
    Before the course, exam 1. After the last day, the exam for that last day.
    """
    sequences = sorted({
        int(ex.sequence)
        for ex in (course_exams or [])
        if getattr(ex, "sequence", None) is not None
    })
    if not sequences:
        return None

    today = timezone.localdate()
    day_index = None
    for i, day in enumerate(days, start=1):
        if day.date < today:
            day_index = i
        elif day.date == today:
            day_index = i
            break
        else:
            break
    if day_index is None:
        day_index = 1

    chosen = sequences[0]
    for seq in sequences:
        if seq <= day_index:
            chosen = seq
        else:
            break
    return chosen


def _previous_booking_day(day):
    """The course day immediately before this one, by date then id."""
    if not day or not getattr(day, "booking_id", None) or not day.date:
        return None
    return (
        BookingDay.objects.filter(booking_id=day.booking_id)
        .select_related("booking", "instructor")
        .filter(Q(date__lt=day.date) | Q(date=day.date, pk__lt=day.pk))
        .order_by("-date", "-pk")
        .first()
    )


def _can_copy_delegates_onto(personnel, day, previous):
    """
    Copy onto a day you deliver, or onto the next day after one you deliver.
    On a split course the person who had yesterday's register can seed the
    next day without taking that day over.
    """
    if not personnel or not day or not previous:
        return False
    if instructor_can_access_day(personnel, day):
        return True
    return instructor_can_access_day(personnel, previous)


def _copy_delegates_from_day(source_day, target_day, instructor):
    """
    Copy identity fields onto target_day. Skip anyone already there
    (same name and date of birth). Health, notes and outcome stay fresh
    for the new day.
    """
    existing = {
        ((name or "").strip().lower(), dob)
        for name, dob in DelegateRegister.objects.filter(
            booking_day=target_day
        ).values_list("name", "date_of_birth")
    }
    copied = 0
    skipped = 0
    target_date = target_day.date or timezone.localdate()
    for src in DelegateRegister.objects.filter(booking_day=source_day).order_by("name", "id"):
        key = ((src.name or "").strip().lower(), src.date_of_birth)
        if key in existing:
            skipped += 1
            continue
        DelegateRegister.objects.create(
            name=src.name,
            date_of_birth=src.date_of_birth,
            job_title=src.job_title or "",
            employee_id=src.employee_id or "",
            certificate_name=src.certificate_name or "",
            date=target_date,
            instructor=instructor,
            health_status=DelegateRegister.HealthStatus.FIT,
            outcome=CourseOutcome.PENDING,
            notes="",
            booking_day=target_day,
        )
        existing.add(key)
        copied += 1
    return copied, skipped



def safe_folder_name(s: str) -> str:
    """Make a Google Drive-safe folder name."""
    s = (s or "").strip()
    if not s:
        return "No-Ref"
    s = SAFE_CHARS_RE.sub("_", s)
    return s[:128]  # keep it reasonable

MAX_ATTACH_TOTAL = 20 * 1024 * 1024   # 20 MB total across all receipts
MAX_ATTACH_EACH  = 8  * 1024 * 1024   # 8 MB per receipt

def list_course_receipts_drive(svc, root_id: str, course_ref: str) -> list[dict]:
    """Return [{id,name,webViewLink,mimeType,size}] in Receipts/<course_ref>, or [] if folder missing."""
    folder_name = safe_folder_name(course_ref or "")
    parent_id = find_folder(svc, folder_name, root_id)
    if not parent_id:
        return []
    res = svc.files().list(
        q=f"'{parent_id}' in parents and trashed=false",
        fields="files(id,name,webViewLink,mimeType,size)",
        pageSize=100
    ).execute()
    return res.get("files", []) or []

def download_small_file_bytes(svc, file_id: str, *, max_bytes: int) -> bytes | None:
    """Download file; abort if exceeds max_bytes."""
    req = svc.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
        if buf.tell() > max_bytes:
            return None
    return buf.getvalue()

def gather_receipt_attachments_and_links(booking) -> tuple[list[tuple[str, bytes, str]], list[str]]:
    """Returns (attachments, link_lines). Attachments obey size caps."""
    svc = get_drive_service(settings.GOOGLE_OAUTH_CLIENT_SECRET, settings.GOOGLE_OAUTH_TOKEN)
    files = list_course_receipts_drive(
        svc,
        settings.GOOGLE_DRIVE_ROOT_RECEIPTS,
        getattr(booking, "course_reference", "") or ""
    )

    links = [f"{f.get('name','(file)')} → {f.get('webViewLink','')}" for f in files]

    attachments, total = [], 0
    for f in files:
        name = f.get("name") or "receipt"
        mime = f.get("mimeType") or (mimetypes.guess_type(name)[0] or "application/octet-stream")
        size_str = f.get("size")
        try:
            size = int(size_str) if size_str is not None else 0
        except:
            size = 0

        # quick skip if clearly too big
        if size and (size > MAX_ATTACH_EACH or total + size > MAX_ATTACH_TOTAL):
            continue

        content = download_small_file_bytes(
            svc, f["id"], max_bytes=min(MAX_ATTACH_EACH, MAX_ATTACH_TOTAL - total)
        )
        if content is None:
            continue

        attachments.append((name, content, mime))
        total += len(content)
        if total >= MAX_ATTACH_TOTAL:
            break

    return attachments, links

def render_invoice_pdf_via_preview(request, booking) -> tuple[bytes, str]:
    """
    Call the existing invoice preview route to get the exact same PDF bytes,
    without importing the view directly (no circular import; no WeasyPrint here).
    """
    path = reverse('instructor_invoice_preview', kwargs={'pk': booking.pk})
    match = resolve(path)

    rf = RequestFactory()

    # Set scheme via 'secure=' and host via HTTP_HOST; don't touch request.scheme
    is_https = request.is_secure()
    fake = rf.get(path, secure=is_https)
    fake.user = request.user
    fake.META['HTTP_HOST'] = request.META.get('HTTP_HOST', 'localhost:8000')
    fake.META['wsgi.url_scheme'] = 'https' if is_https else 'http'

    # Call the resolved view callable (works for FBV or CBV via as_view())
    response = match.func(fake, *match.args, **match.kwargs)

    pdf_bytes = response.content
    filename = f"invoice-{booking.course_reference or booking.pk}.pdf"
    return pdf_bytes, filename

def _assessments_complete(booking):
    """
    True iff there are *some* register rows on this booking AND
    none of them are pending/blank/NULL.
    """
    regs = DelegateRegister.objects.filter(booking_day__booking=booking)
    if not regs.exists():
        return False
    return not regs.filter(
        Q(outcome__iexact='pending') | Q(outcome__isnull=True) | Q(outcome__exact='')
    ).exists()


def _get_register_pdf_bytes_via_existing_view(request, pk: int) -> tuple[bytes, str]:
    """
    Calls the existing instructor_day_registers_pdf view to obtain the PDF HttpResponse,
    then returns (raw_bytes, filename). No duplication of PDF drawing code.
    """
    # Import locally to avoid circulars if file is reorganised
    from .views_instructor import instructor_day_registers_pdf  # this is the existing download view

    resp: HttpResponse = instructor_day_registers_pdf(request, pk)
    data = resp.content or b""

    # Try to parse filename from Content-Disposition; fall back to a generic one
    filename = "registers.pdf"
    cd = resp.get("Content-Disposition") or resp.get("content-disposition")
    if cd:
        m = re.search(r'filename="?([^";]+)"?', cd)
        if m:
            filename = m.group(1)

    return data, filename

def _extract_line_items(post):
    keys = set(post.keys())
    desc_keys = [k for k in keys if k.startswith("item_desc")]
    amt_keys  = [k for k in keys if k.startswith("item_amount")]

    straight = list(zip(post.getlist("item_desc"), post.getlist("item_amount")))
    indexed = []
    for pattern in [r"item_desc\[(\d+)\]", r"item_amount\[(\d+)\]"]:
        pass

    tuples = []
    for d, a in straight:
        d = (d or "").strip()
        if not d:
            continue
        try:
            amt = Decimal(a or "0")
        except Exception:
            amt = Decimal("0")
        tuples.append((d, amt))
    return tuples

def _can_view_attempt(user, attempt) -> bool:
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return True
    instr = getattr(user, "instructor", None)
    if not instr:
        return False
    a_instr_id = getattr(attempt, "instructor_id", None)
    return (a_instr_id is None) or (a_instr_id == instr.id)

def _display_name_for_attempt(attempt) -> str:
    for f in ("name", "delegate_name"):
        val = getattr(attempt, f, None)
        if val:
            return str(val)
    first = getattr(attempt, "first_name", "") or ""
    last  = getattr(attempt, "last_name", "") or ""
    full = f"{first} {last}".strip()
    return full or "—"

def _attempt_result_badge(attempt):
    """
    Returns (label, bootstrap_color):
      ('Pass'|'Viva'|'Fail', 'success'|'warning'|'danger')
    """
    if getattr(attempt, "passed", False):
        return ("Pass", "success")
    if getattr(attempt, "viva_eligible", False):
        return ("Viva", "warning")
    return ("Fail", "danger")

def _counts_for_attempt(attempt):
    from .models import ExamAttemptAnswer
    correct = ExamAttemptAnswer.objects.filter(attempt=attempt, is_correct=True).count()
    # prefer exam.questions.count(); fall back to distinct questions answered
    try:
        total = attempt.exam.questions.count()
    except Exception:
        from .models import ExamAttemptAnswer as AAA
        total = AAA.objects.filter(attempt=attempt).values("question_id").distinct().count()
    return correct, total

def _back_url_for_attempt(request, attempt) -> str:
    """
    Prefer an explicit ?back=<booking_uuid> if provided.
    Else, best-effort link back to the relevant booking’s Exams tab.
    Else, instructor bookings list.
    """
    back = request.GET.get("back") or request.POST.get("back")
    if back:
        return f"{reverse('instructor_booking_detail', kwargs={'pk': back})}?tab=exams"

    try:
        day = (
            BookingDay.objects.select_related("booking")
            .filter(
                booking__course_type=attempt.exam.course_type,
                booking__instructor=attempt.instructor,
                date=attempt.exam_date,
            )
            .order_by("date")
            .first()
        )
        if day and day.booking_id:
            return f"{reverse('instructor_booking_detail', args=[day.booking_id])}?tab=exams"
    except Exception:
        pass

    return reverse("instructor_bookings")

def _can_authorise_retest(user, attempt) -> bool:
    """Allow re-test authorisation for attempt #1 only, by the instructor who can view it."""
    if not _can_view_attempt(user, attempt):
        return False
    number = getattr(attempt, "attempt_no", None) or getattr(attempt, "attempt_number", None) or 1
    try:
        number = int(number)
    except Exception:
        number = 1
    return number <= 1

def _attempt_header_stats(attempt):
    """Returns header dict preferring recorded viva decision."""
    correct = ExamAttemptAnswer.objects.filter(attempt=attempt, is_correct=True).count()
    total   = ExamAttemptAnswer.objects.filter(attempt=attempt).aggregate(n=Count("id"))["n"] or 0

    # If viva already decided, honour recorded outcome
    if hasattr(attempt, "viva_eligible") and not getattr(attempt, "viva_eligible", False):
        decided_pass = bool(getattr(attempt, "passed", False))
        return {
            "display_name": _display_name_for_attempt(attempt),
            "correct_count": correct,
            "total_questions": total,
            "result_label": "Pass" if decided_pass else "Fail",
            "result_class": "success" if decided_pass else "danger",
        }

    exam = getattr(attempt, "exam", None)
    passp = getattr(exam, "pass_mark_percent", 70) or 70
    viva_p = getattr(exam, "viva_threshold_percent", None)
    pct = (correct * 100.0 / total) if total else 0.0

    if pct >= passp:
        label, klass = "Pass", "success"
    elif viva_p and pct >= viva_p:
        label, klass = "Viva", "warning"
    else:
        label, klass = "Fail", "danger"

    return {
        "display_name": _display_name_for_attempt(attempt),
        "correct_count": correct,
        "total_questions": total,
        "result_label": label,
        "result_class": klass,
    }

def _back_to_booking_url(attempt: ExamAttempt) -> str:
    """
    Best-effort link back to the relevant booking page for this attempt.
    If we can't find a booking in ±7 days for the same course type/instructor, we
    fall back to instructor bookings list.
    """
    instr_id = getattr(attempt, "instructor_id", None)
    ct_id = getattr(attempt.exam, "course_type_id", None)
    exam_day = getattr(attempt, "exam_date", None)

    try:
        bd = (
            BookingDay.objects
            .select_related("booking")
            .filter(
                booking__instructor_id=instr_id,
                booking__course_type_id=ct_id,
                date__gte=exam_day - timedelta(days=7),
                date__lte=exam_day + timedelta(days=7),
            )
            .order_by("date")
            .first()
        )
        if bd:
            return reverse("instructor_booking_detail", kwargs={"pk": bd.booking_id})
    except Exception:
        pass
    return reverse("instructor_bookings")

def get_invoice_template_path():
    """
    Find Invoice.docx in common locations (project-level templates or app templates).
    """
    candidates = [
        Path(settings.BASE_DIR) / "templates" / "invoicing" / "Invoice.docx",
        Path(settings.BASE_DIR) / "unicorn_project" / "training" / "templates" / "invoicing" / "Invoice.docx",
        Path(__file__).resolve().parent / "templates" / "invoicing" / "Invoice.docx",
    ]
    for p in candidates:
        try:
            if p.exists():
                return str(p)
        except Exception:
            pass
    raise FileNotFoundError("Invoice.docx template not found. Tried: " + " | ".join(str(p) for p in candidates))

# Adjust if your templates dir differs
DOCX_TEMPLATE_PATH = os.path.join(settings.BASE_DIR, "templates", "invoicing", "Invoice.docx")

buffer = io.BytesIO()
c = canvas.Canvas(buffer, pagesize=landscape(A4))
W, H = landscape(A4)   # use these for your coordinates

HEALTH_BADGE = {
    "fit":                 ("✔", "bg-success", "Fit to take part"),
    "agreed_adjustments":  ("■", "bg-warning text-dark", "Impairment – agreed adjustments"),
    "will_discuss":        ("▲", "bg-warning", "Impairment – will discuss"),
    "not_fit":             ("✖", "bg-danger", "Not fit to take part"),
}
# Note: Bootstrap has no orange by default; we re-use warning (yellow).

def _time_greeting():
    hour = localtime().hour
    if hour < 12:
        return "Good morning"
    if hour < 18:
        return "Good afternoon"
    return "Good evening"

def _business_name(booking):
    return (
        getattr(getattr(booking, "business", None), "name", None)
        or getattr(getattr(booking, "company", None), "name", None)
        or getattr(getattr(booking, "customer", None), "name", None)
        or getattr(booking, "business_name", None)
        or "N/A"
    )

def _course_dates(booking):
    dt_fmt = "%d %b %Y"
    start = getattr(booking, "start_date", None) or getattr(booking, "date", None) or getattr(booking, "course_date", None)
    end = getattr(booking, "end_date", None)
    try:
        if start and end and start != end:
            return f"{start.strftime(dt_fmt)} – {end.strftime(dt_fmt)}"
        if start:
            return start.strftime(dt_fmt)
    except Exception:
        pass
    # Optional: derive from BookingDay dates if you have that model
    try:
        from .models import BookingDay
        ds = list(BookingDay.objects.filter(booking=booking).order_by("date").values_list("date", flat=True))
        if ds:
            if len(ds) == 1:
                return ds[0].strftime(dt_fmt)
            return f"{ds[0].strftime(dt_fmt)} – {ds[-1].strftime(dt_fmt)}"
    except Exception:
        pass
    return "N/A"

def _health_badge_tuple(code: str):
    return HEALTH_BADGE.get(code or "", ("–", "bg-secondary", "Not provided"))

def _closed_guard(request, booking):
    """
    If the booking is already completed, block further edits and bounce
    back to the booking detail page (Invoicing tab stays usable).
    """
    if getattr(booking, "status", "") == "completed":
        messages.error(request, "Course is closed — only Invoicing is editable.")
        return redirect(
            f"{reverse('instructor_booking_detail', kwargs={'pk': booking.pk})}?tab=invoicing"
        )
    return None

_DASHBOARD_BOOKING_RELATED = ("course_type", "business", "training_location")


def _attach_first_day_date(bookings):
    """Set schedule labels (including first_day_date) for dashboard/list templates."""
    from .utils.booking_details import attach_booking_schedule_labels

    return attach_booking_schedule_labels(bookings)


def _dashboard_action_bookings(personnel, **filters):
    qs = (
        Booking.objects.filter(instructor=personnel, **filters)
        .select_related(*_DASHBOARD_BOOKING_RELATED)
        .order_by("course_date", "id")
    )
    return _attach_first_day_date(qs)


def _delegate_outcome_pending(outcome) -> bool:
    if outcome is None:
        return True
    text = str(outcome).strip()
    return text == "" or text.lower() == "pending"


def _bookings_with_pending_assessments(personnel):
    booking_ids = list(
        Booking.objects.filter(
            instructor=personnel,
            status__in=["in_progress", "awaiting_closure", "completed"],
        ).values_list("pk", flat=True)
    )
    if not booking_ids:
        return []

    seen_by_booking = defaultdict(set)
    pending_ids = set()
    regs = (
        DelegateRegister.objects.filter(booking_day__booking_id__in=booking_ids)
        .order_by("booking_day__booking_id", "name", "date_of_birth", "id")
        .values("id", "name", "date_of_birth", "outcome", "booking_day__booking_id")
    )
    for reg in regs:
        booking_id = reg["booking_day__booking_id"]
        name = (reg["name"] or "").strip().lower()
        dob = reg["date_of_birth"]
        key = (name, dob) if dob else ("__nodedob__", reg["id"])
        seen = seen_by_booking[booking_id]
        if key in seen:
            continue
        seen.add(key)
        if _delegate_outcome_pending(reg["outcome"]):
            pending_ids.add(booking_id)

    if not pending_ids:
        return []

    return _attach_first_day_date(
        Booking.objects.filter(pk__in=pending_ids)
        .select_related(*_DASHBOARD_BOOKING_RELATED)
        .order_by("course_date", "id")
    )


@login_required
def instructor_dashboard(request):
    user = request.user
    personnel = getattr(user, "personnel", None)

    if not personnel:
        return redirect("instructor_bookings")

    today = timezone.localdate()

    # ------------------------------------------------------
    # TODAY'S COURSES (BookingDay)
    # ------------------------------------------------------
    todays_days = list(
        BookingDay.objects
        .filter(instructor=personnel, date=today)
        .select_related(
            "booking",
            "booking__training_location",
            "booking__business",
            "booking__course_type",
        )
        .order_by("start_time")
    )

    # ------------------------------------------------------
    # UPCOMING COURSES (next 14 days)
    # ------------------------------------------------------
    upcoming = list(
        BookingDay.objects
        .filter(
            instructor=personnel,
            date__gt=today,
            date__lte=today + timedelta(days=14)
        )
        .select_related(
            "booking__course_type",
            "booking__business",
            "booking__training_location",
        )
        .order_by("date", "start_time")
    )

    # ------------------------------------------------------
    # RECENT COURSES (past 30 days)
    # ------------------------------------------------------
    recent = list(
        BookingDay.objects
        .filter(
            instructor=personnel,
            date__lt=today,
            date__gte=today - timedelta(days=30)
        )
        .select_related("booking__course_type", "booking__business")
        .order_by("-date")
    )

    # ------------------------------------------------------
    # ACTIONS REQUIRED
    # ------------------------------------------------------
    awaiting_closure = _dashboard_action_bookings(personnel, status="awaiting_closure")

    invoice_attention = _attach_first_day_date(
        Booking.objects.filter(
            pk__in=instructor_visible_booking_ids(personnel),
            status="completed",
        )
        .filter(booking_has_invoice_for(personnel, statuses=["draft", "awaiting_review"]))
        .select_related(*_DASHBOARD_BOOKING_RELATED)
        .order_by("-course_date", "id")
    )

    incomplete_registers = []
    missing_assessments = _bookings_with_pending_assessments(personnel)
    missing_feedback = []

    return render(request, "instructor/dashboard.html", {
        "personnel": personnel,
        "todays_days": todays_days,
        "upcoming": upcoming,
        "recent": recent,

        # Actions required
        "awaiting_closure": awaiting_closure,
        "invoice_attention": invoice_attention,
        "missing_assessments": missing_assessments,
        "missing_feedback": missing_feedback,
        "incomplete_registers": incomplete_registers,
    })

@login_required
def post_login(request):
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
def booking_fee(request, pk):
    booking = get_object_or_404(Booking, pk=pk)

    # Permission check
    is_instructor_for_booking = (
        hasattr(request.user, "personnel")
        and request.user.personnel
        and instructor_can_access_booking(request.user.personnel, booking)
    )
    is_admin_or_staff = request.user.is_staff or request.user.is_superuser

    if not (is_instructor_for_booking or is_admin_or_staff):
        return HttpResponseForbidden("Not allowed")

    # Instructor fee
    amount = booking.instructor_fee or 0

    # Mileage allowance (total lump sum — NOT per mile)
    mileage = (
        f"£{booking.mileage_fee:,.2f}"
        if booking.allow_mileage_claim and booking.mileage_fee
        else None
    )

    return JsonResponse({
        "amount": f"£{amount:,.2f}",
        "mileage": mileage,
        "accommodation": booking.allow_accommodation,
    })

def _invoicing_tab_context(booking, personnel=None):
    """Build context keys that _invoicing_tab.html expects."""
    personnel = personnel or booking.instructor
    inv = _get_or_create_invoice(booking, personnel=personnel)
    instr = personnel or booking.instructor

    inv_date = inv.invoice_date or now().date()

    try:
        base_fee = inv.base_amount if inv else 0
        if base_fee in (None, ""):
            base_fee = getattr(booking.course_type, "default_instructor_fee", 0)
    except Exception:
        base_fee = 0

    bank_name = (inv.account_name or (getattr(instr, "name_on_account", "") if instr else "") or "")
    bank_sort = (inv.sort_code or (getattr(instr, "bank_sort_code", "") if instr else "") or "")
    bank_num  = (inv.account_number or (getattr(instr, "bank_account_number", "") if instr else "") or "")

    addr_parts = []
    if instr:
        for f in ["address_line", "town", "postcode"]:
            addr_parts.append(getattr(instr, f, "") or "")
    instructor_address = ", ".join([x for x in addr_parts if x.strip()])

    return {
        "invoice": inv,
        "invoice_date": inv_date,
        "base_instructor_fee": base_fee or 0,
        "invoice_items": list(getattr(inv, "items", []).all()) if hasattr(inv, "items") else [],
        "bank_account_name": bank_name,
        "bank_sort_code": bank_sort,
        "bank_account_number": bank_num,
        "instructor_address": instructor_address or "—",
    }

@login_required
def instructor_bookings(request):
    """
    Instructor’s bookings dashboard.
    """
    inst = getattr(request.user, "personnel", None)
    if not inst:
        messages.error(request, "Your user account isn’t linked to an instructor record.")
        return redirect("app_admin_dashboard" if request.user.is_staff else "home")

    today = timezone.localdate()

    status_filter_choices = [
        ("in_progress", "In progress"),
        ("awaiting_closure", "Awaiting closure"),
        ("scheduled", "Scheduled"),
        ("completed", "Closed"),
    ]
    allowed_status_filters = {c[0] for c in status_filter_choices}
    selected_statuses = [
        s.strip() for s in request.GET.getlist("status")
        if s and s.strip() in allowed_status_filters
    ]
    # Empty selection = show all sections (default)
    show_all_statuses = not selected_statuses

    base_qs = (
        instructor_bookings_queryset(inst)
        .select_related("course_type", "business", "training_location")
        .exclude(status="cancelled")
        .exclude(business__is_dummy=True)
    )

    practice_bookings = list(
        Booking.objects
        .select_related("course_type", "business", "training_location")
        .filter(instructor=inst, business__is_dummy=True)
        .exclude(status="cancelled")
        .order_by("-course_date", "-id")
    )

    in_progress = []
    if show_all_statuses or "in_progress" in selected_statuses:
        in_progress = list(
            base_qs.filter(status="in_progress").order_by("course_date", "start_time")
        )

    awaiting = []
    if show_all_statuses or "awaiting_closure" in selected_statuses:
        awaiting = list(
            base_qs.filter(status="awaiting_closure").order_by("course_date", "start_time")
        )

    scheduled = []
    if show_all_statuses or "scheduled" in selected_statuses:
        scheduled = list(
            base_qs.filter(status="scheduled", course_date__gte=today).order_by(
                "course_date", "start_time"
            )[:10]
        )

    # --- CLOSED (completed/closed) ---
    closed_rows = []
    closed_page = None
    closed_total = 0
    if show_all_statuses or "completed" in selected_statuses:
        closed_qs = (
            base_qs.filter(status__in=["completed", "closed"])
            .order_by("-course_date", "-id")
        )
        closed_total = closed_qs.count()

        p = Paginator(closed_qs, 10)
        closed_page = p.get_page(request.GET.get("closed_page") or 1)

        # Copy the page’s rows so we can annotate them
        closed_rows = list(closed_page.object_list)

        inv_map = lead_invoice_status_map(closed_rows, instructor_id=inst.pk)

        # Attach invoice_status directly to each booking row
        for b in closed_rows:
            b.invoice_status = inv_map.get(b.id, "")

    # Add first register-day id and schedule labels for list rows.
    visible_bookings = in_progress + awaiting + scheduled + closed_rows + practice_bookings
    visible_booking_ids = [b.id for b in visible_bookings]

    first_day_by_booking = {}
    if visible_booking_ids:
        for day in (
            BookingDay.objects
            .filter(booking_id__in=visible_booking_ids)
            .order_by('booking_id', 'date', 'id')
        ):
            if day.booking_id not in first_day_by_booking:
                first_day_by_booking[day.booking_id] = day.id

    for b in visible_bookings:
        b.first_day_id = first_day_by_booking.get(b.id)

    from .utils.booking_details import attach_booking_schedule_labels
    attach_booking_schedule_labels(visible_bookings)

    dummy_businesses = list(
        Business.objects
        .filter(is_dummy=True, training_locations__is_active=True)
        .select_related("dummy_course_type")
        .distinct()
        .order_by("name")
    )

    from urllib.parse import urlencode
    status_query = urlencode([("status", s) for s in selected_statuses])

    return render(request, "instructor/bookings.html", {
        "title": "My bookings",
        "instructor": inst,
        "in_progress": in_progress,
        "awaiting": awaiting,
        "scheduled": scheduled,
        "closed_page": closed_page,
        "closed_rows": closed_rows,
        "closed_total": closed_total,
        "dummy_businesses": dummy_businesses,
        "practice_bookings": practice_bookings,
        "status_filter_choices": status_filter_choices,
        "selected_statuses": selected_statuses,
        "show_in_progress": show_all_statuses or "in_progress" in selected_statuses,
        "show_awaiting": show_all_statuses or "awaiting_closure" in selected_statuses,
        "show_scheduled": show_all_statuses or "scheduled" in selected_statuses,
        "show_closed": show_all_statuses or "completed" in selected_statuses,
        "status_query": status_query,
    })


@login_required
def instructor_calendar(request):
    inst = _get_instructor(request.user)
    if not inst:
        messages.error(request, "Your user account isn't linked to an instructor record.")
        return redirect("home")

    return render(
        request,
        "instructor/calendar.html",
        {
            "title": "Instructor calendar",
            "instructor": inst,
        },
    )


@login_required
def instructor_calendar_events(request):
    inst = _get_instructor(request.user)
    if not inst:
        return JsonResponse({"detail": "Forbidden"}, status=403)

    start_raw = (request.GET.get("start") or "")[:10]
    end_raw = (request.GET.get("end") or "")[:10]
    start_date = parse_date(start_raw) if start_raw else None
    end_date = parse_date(end_raw) if end_raw else None

    days_qs = (
        BookingDay.objects
        .filter(instructor=inst)
        .select_related("booking__course_type", "booking__business")
    )
    if start_date:
        days_qs = days_qs.filter(date__gte=start_date)
    if end_date:
        # FullCalendar uses an exclusive end date for event ranges.
        days_qs = days_qs.filter(date__lt=end_date)

    avail_qs = InstructorAvailability.objects.filter(instructor=inst)
    if start_date:
        avail_qs = avail_qs.filter(date__gte=start_date)
    if end_date:
        avail_qs = avail_qs.filter(date__lt=end_date)

    events = []

    # Build set of dates with booking assignments (bookings take absolute priority)
    booking_dates = set(d.date for d in days_qs)

    for d in days_qs:
        ct_name = d.booking.course_type.name if d.booking and d.booking.course_type_id else "Course"
        business_name = d.booking.business.name if d.booking and d.booking.business_id else "Business"
        events.append({
            "id": f"booking-day-{d.id}",
            "title": f"{ct_name} - {business_name}",
            "start": d.date.isoformat(),
            "allDay": True,
            "color": "#2563eb",
            "textColor": "#ffffff",
            "url": reverse("instructor_booking_detail", kwargs={"pk": d.booking_id}),
            "extendedProps": {
                "eventType": "booking",
                "bookingId": str(d.booking_id),
            },
        })

    for a in avail_qs:
        # Skip if this date has a course booking (bookings override all availability)
        if a.date in booking_dates:
            continue

        is_available = a.status == InstructorAvailability.STATUS_AVAILABLE
        events.append({
            "id": f"availability-{a.id}",
            "title": "Available" if is_available else "Not available",
            "start": a.date.isoformat(),
            "allDay": True,
            "color": "#16a34a" if is_available else "#dc2626",
            "textColor": "#ffffff",
            "extendedProps": {
                "eventType": "availability",
                "status": a.status,
                "note": a.note or "",
                "date": a.date.isoformat(),
            },
        })

    # Expand patterns and add as events, but skip dates with explicit InstructorAvailability entries or bookings
    patterns = InstructorAvailabilityPattern.objects.filter(instructor=inst)
    if start_date:
        patterns = patterns.filter(end_date__gte=start_date)
    if end_date:
        patterns = patterns.filter(start_date__lt=end_date)

    # Build set of dates with explicit availability entries (for override logic)
    explicit_dates = set(a.date for a in avail_qs)

    for p in patterns:
        # Expand pattern to individual dates
        expanded_dates = p.expand_to_dates()

        for d in expanded_dates:
            # Skip if there's a course booking on this date (absolute priority)
            if d in booking_dates:
                continue

            # Skip if there's an explicit InstructorAvailability entry for this date
            if d in explicit_dates:
                continue

            # Also skip if outside the requested date range
            if start_date and d < start_date:
                continue
            if end_date and d >= end_date:
                continue

            is_available = p.status == InstructorAvailabilityPattern.STATUS_AVAILABLE
            events.append({
                "id": f"pattern-{p.id}-{d.isoformat()}",
                "title": "Available" if is_available else "Not available",
                "start": d.isoformat(),
                "allDay": True,
                "color": "#16a34a" if is_available else "#dc2626",
                "textColor": "#ffffff",
                "extendedProps": {
                    "eventType": "availability",
                    "status": p.status,
                    "note": p.note or "",
                    "date": d.isoformat(),
                    "isPattern": True,
                    "patternId": p.id,
                },
            })

    return JsonResponse(events, safe=False)


@login_required
@require_POST
def instructor_set_availability(request):
    inst = _get_instructor(request.user)
    if not inst:
        return JsonResponse({"ok": False, "error": "Forbidden"}, status=403)

    date_str = (request.POST.get("date") or "").strip()
    status = (request.POST.get("status") or "").strip().lower()
    note = (request.POST.get("note") or "").strip()

    day = parse_date(date_str)
    if not day:
        return JsonResponse({"ok": False, "error": "Invalid date"}, status=400)

    if status == "clear":
        InstructorAvailability.objects.filter(instructor=inst, date=day).delete()
        return JsonResponse({"ok": True, "status": "clear", "date": day.isoformat()})

    valid_status = {
        InstructorAvailability.STATUS_AVAILABLE,
        InstructorAvailability.STATUS_UNAVAILABLE,
    }
    if status not in valid_status:
        return JsonResponse({"ok": False, "error": "Invalid status"}, status=400)

    obj, created = InstructorAvailability.objects.get_or_create(
        instructor=inst,
        date=day,
        defaults={"status": status, "note": note[:255]},
    )
    if not created:
        obj.status = status
        obj.note = note[:255]
        obj.save(update_fields=["status", "note", "updated_at"])

    return JsonResponse({
        "ok": True,
        "status": obj.status,
        "date": obj.date.isoformat(),
        "note": obj.note,
    })


@login_required
def instructor_list_patterns(request):
    """GET: List all recurring availability patterns for the instructor."""
    inst = _get_instructor(request.user)
    if not inst:
        return JsonResponse({"ok": False, "error": "Forbidden"}, status=403)

    patterns = InstructorAvailabilityPattern.objects.filter(instructor=inst)
    data = []
    for p in patterns:
        data.append({
            "id": p.id,
            "start_date": p.start_date.isoformat(),
            "end_date": p.end_date.isoformat(),
            "status": p.status,
            "days_of_week": p.days_of_week,
            "days_labels": p.get_days_of_week_labels(),
            "note": p.note,
            "created_at": p.created_at.isoformat(),
        })
    return JsonResponse({"ok": True, "patterns": data})


@require_POST
@login_required
def instructor_create_pattern(request):
    """POST: Create a new recurring availability pattern."""
    inst = _get_instructor(request.user)
    if not inst:
        return JsonResponse({"ok": False, "error": "Forbidden"}, status=403)

    start_str = (request.POST.get("start_date") or "").strip()
    end_str = (request.POST.get("end_date") or "").strip()
    status = (request.POST.get("status") or "").strip().lower()
    days_str = (request.POST.get("days_of_week") or "").strip()
    note = (request.POST.get("note") or "").strip()

    start_date = parse_date(start_str)
    end_date = parse_date(end_str)

    if not start_date or not end_date:
        return JsonResponse({"ok": False, "error": "Invalid date range"}, status=400)

    if start_date > end_date:
        return JsonResponse({"ok": False, "error": "Start date must be before end date"}, status=400)

    valid_status = {
        InstructorAvailabilityPattern.STATUS_AVAILABLE,
        InstructorAvailabilityPattern.STATUS_UNAVAILABLE,
    }
    if status not in valid_status:
        return JsonResponse({"ok": False, "error": "Invalid status"}, status=400)

    # Validate days_of_week (comma-separated integers 0-6)
    if not days_str:
        return JsonResponse({"ok": False, "error": "No days selected"}, status=400)

    try:
        days_list = [int(d.strip()) for d in days_str.split(",") if d.strip()]
        if not all(0 <= d <= 6 for d in days_list):
            raise ValueError("Invalid day number")
        days_str = ",".join(str(d) for d in sorted(set(days_list)))
    except (ValueError, TypeError):
        return JsonResponse({"ok": False, "error": "Invalid day format"}, status=400)

    pattern = InstructorAvailabilityPattern.objects.create(
        instructor=inst,
        start_date=start_date,
        end_date=end_date,
        status=status,
        days_of_week=days_str,
        note=note[:255] if note else "",
    )

    return JsonResponse({
        "ok": True,
        "pattern": {
            "id": pattern.id,
            "start_date": pattern.start_date.isoformat(),
            "end_date": pattern.end_date.isoformat(),
            "status": pattern.status,
            "days_of_week": pattern.days_of_week,
            "days_labels": pattern.get_days_of_week_labels(),
            "note": pattern.note,
        },
    })


@require_POST
@login_required
def instructor_update_pattern(request):
    """POST: Update an existing pattern."""
    inst = _get_instructor(request.user)
    if not inst:
        return JsonResponse({"ok": False, "error": "Forbidden"}, status=403)

    pattern_id = request.POST.get("pattern_id")
    if not pattern_id:
        return JsonResponse({"ok": False, "error": "Pattern ID required"}, status=400)

    pattern = get_object_or_404(InstructorAvailabilityPattern, id=pattern_id, instructor=inst)

    start_str = (request.POST.get("start_date") or "").strip()
    end_str = (request.POST.get("end_date") or "").strip()
    status = (request.POST.get("status") or "").strip().lower()
    days_str = (request.POST.get("days_of_week") or "").strip()
    note = (request.POST.get("note") or "").strip()

    if start_str:
        start_date = parse_date(start_str)
        if not start_date:
            return JsonResponse({"ok": False, "error": "Invalid start date"}, status=400)
        pattern.start_date = start_date

    if end_str:
        end_date = parse_date(end_str)
        if not end_date:
            return JsonResponse({"ok": False, "error": "Invalid end date"}, status=400)
        pattern.end_date = end_date

    if pattern.start_date > pattern.end_date:
        return JsonResponse({"ok": False, "error": "Start date must be before end date"}, status=400)

    if status:
        valid_status = {
            InstructorAvailabilityPattern.STATUS_AVAILABLE,
            InstructorAvailabilityPattern.STATUS_UNAVAILABLE,
        }
        if status not in valid_status:
            return JsonResponse({"ok": False, "error": "Invalid status"}, status=400)
        pattern.status = status

    if days_str:
        try:
            days_list = [int(d.strip()) for d in days_str.split(",") if d.strip()]
            if not all(0 <= d <= 6 for d in days_list):
                raise ValueError("Invalid day number")
            pattern.days_of_week = ",".join(str(d) for d in sorted(set(days_list)))
        except (ValueError, TypeError):
            return JsonResponse({"ok": False, "error": "Invalid day format"}, status=400)

    if note is not None:
        pattern.note = note[:255]

    pattern.save()

    return JsonResponse({
        "ok": True,
        "pattern": {
            "id": pattern.id,
            "start_date": pattern.start_date.isoformat(),
            "end_date": pattern.end_date.isoformat(),
            "status": pattern.status,
            "days_of_week": pattern.days_of_week,
            "days_labels": pattern.get_days_of_week_labels(),
            "note": pattern.note,
        },
    })


@require_POST
@login_required
def instructor_delete_pattern(request):
    """POST: Delete a pattern."""
    inst = _get_instructor(request.user)
    if not inst:
        return JsonResponse({"ok": False, "error": "Forbidden"}, status=403)

    pattern_id = request.POST.get("pattern_id")
    if not pattern_id:
        return JsonResponse({"ok": False, "error": "Pattern ID required"}, status=400)

    pattern = get_object_or_404(InstructorAvailabilityPattern, id=pattern_id, instructor=inst)
    pattern.delete()
    return JsonResponse({"ok": True, "message": "Pattern deleted"})



@login_required
def instructor_dummy_booking_new(request, business_id):
    inst = getattr(request.user, "personnel", None)
    if not inst:
        messages.error(request, "Your user account isn’t linked to an instructor record.")
        return redirect("home")

    business = get_object_or_404(
        Business.objects.select_related("dummy_course_type"),
        pk=business_id,
        is_dummy=True,
    )
    training_location = business.training_locations.filter(is_active=True).order_by("name").first()

    if not training_location:
        messages.error(
            request,
            "This dummy business is not ready yet. It needs at least one active training location.",
        )
        return redirect("instructor_bookings")

    if request.method == "POST":
        form = DummyBookingQuickCreateForm(
            request.POST,
            default_course_type=business.dummy_course_type,
        )
        if form.is_valid():
            course_date = form.cleaned_data["course_date"]
            start_time = form.cleaned_data["start_time"]
            course_type = form.cleaned_data["course_type"]

            booking = Booking.objects.create(
                business=business,
                training_location=training_location,
                course_type=course_type,
                instructor=inst,
                course_date=course_date,
                start_time=start_time,
                booking_notes="Dummy / familiarisation booking.",
                course_fee=course_type.default_course_fee,
                instructor_fee=course_type.default_instructor_fee,
                contact_name=training_location.contact_name or business.contact_name or "",
                telephone=training_location.telephone or business.telephone or "",
                email=training_location.email or business.email or "",
            )

            duration_days = float(course_type.duration_days or 1.0)
            rows = max(1, int(duration_days) if duration_days.is_integer() else int(duration_days) + 1)

            def _hours_for_day(index, total, total_rows):
                if total_rows == 1:
                    return 3.5 if 0 < total < 1 else 7.0
                whole = int(total)
                frac = total - whole
                if index <= whole:
                    return 7.0
                if frac > 0 and index == whole + 1:
                    return 3.5
                return 7.0

            def _add_hours(base_time, hours_float):
                base_dt = datetime.combine(datetime(2000, 1, 1).date(), base_time)
                result = base_dt + timedelta(seconds=round(hours_float * 3600))
                return result.time()

            for i in range(rows):
                day_no = i + 1
                BookingDay.objects.create(
                    booking=booking,
                    date=course_date + timedelta(days=i),
                    start_time=start_time,
                    end_time=_add_hours(start_time, _hours_for_day(day_no, duration_days, rows)),
                    instructor=inst,
                    note="Dummy / familiarisation day",
                )

            from .utils.booking_notifications import notify_new_booking
            from .utils.travel_time import update_booking_travel_duration

            update_booking_travel_duration(booking)
            notify_new_booking(booking)

            messages.success(request, f"Practice booking created for {business.name}.")
            return redirect("instructor_booking_detail", pk=booking.pk)
    else:
        form = DummyBookingQuickCreateForm(
            initial={"course_date": timezone.localdate()},
            default_course_type=business.dummy_course_type,
        )

    return render(request, "instructor/dummy_booking_new.html", {
        "title": "Create practice booking",
        "form": form,
        "business": business,
        "training_location": training_location,
        "default_course_type": business.dummy_course_type,
    })


@login_required
@require_POST
def instructor_delete_dummy_booking(request, pk):
    booking = get_object_or_404(
        Booking.objects.select_related("business", "instructor"),
        pk=pk,
        business__is_dummy=True,
    )
    instr = getattr(request.user, "personnel", None)
    is_admin = request.user.is_superuser or request.user.groups.filter(name__iexact="admin").exists()

    if not is_admin and (not instr or not instructor_can_access_booking(instr, booking)):
        messages.error(request, "You do not have permission to delete this dummy booking.")
        return redirect("instructor_bookings")

    ref = booking.course_reference or ""
    delete_dummy_booking_tree(booking)
    messages.success(request, f"Dummy booking {ref} deleted.")
    return redirect(request.POST.get("next") or reverse("instructor_bookings"))


@login_required
@require_POST
def instructor_send_telegram_booking(request, pk):
    booking = get_object_or_404(
        Booking.objects.select_related(
            "instructor", "course_type", "business", "training_location"
        ).prefetch_related("days"),
        pk=pk,
    )
    instr = getattr(request.user, "personnel", None)
    if not instr or not instructor_can_access_booking(instr, booking):
        messages.error(request, "You do not have access to this booking.")
        return redirect("instructor_bookings")

    from .utils.booking_notifications import notify_resend_telegram

    if notify_resend_telegram(booking):
        messages.success(request, "Booking details resent via Telegram.")
    else:
        messages.warning(request, "Could not send via Telegram. Link Telegram on Profile and try again.")
    return redirect("instructor_booking_detail", pk=booking.pk)


@login_required
@require_POST
def instructor_send_email_booking(request, pk):
    booking = get_object_or_404(
        Booking.objects.select_related(
            "instructor",
            "instructor__user",
            "course_type",
            "business",
            "training_location",
        ).prefetch_related("days"),
        pk=pk,
    )
    instr = getattr(request.user, "personnel", None)
    if not instr or not instructor_can_access_booking(instr, booking):
        messages.error(request, "You do not have access to this booking.")
        return redirect("instructor_bookings")

    from .utils.booking_notifications import notify_resend_email

    if notify_resend_email(booking):
        messages.success(request, "Booking details resent via email.")
    else:
        messages.warning(request, "Could not send via email. Check your email address on Profile.")
    return redirect("instructor_booking_detail", pk=booking.pk)


@login_required
def instructor_booking_course_pack_pdf(request, pk):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])

    booking = get_object_or_404(
        Booking.objects.select_related(
            "instructor",
            "instructor__user",
            "course_type",
            "business",
            "training_location",
        ).prefetch_related("days", "course_type__exams"),
        pk=pk,
    )
    instr = getattr(request.user, "personnel", None)
    if not (request.user.is_staff or (instr and instructor_can_access_booking(instr, booking))):
        return HttpResponseForbidden("You do not have access to this booking.")

    from .utils.booking_notification_pdf import booking_course_pack_pdf_response

    return booking_course_pack_pdf_response(booking)


@login_required
def whoami(request):
    user = request.user
    is_instructor = False
    try:
        # If user ↔ Instructor is OneToOne with related_name='instructor'
        is_instructor = hasattr(user, "instructor") and user.instructor is not None
    except Instructor.DoesNotExist:
        is_instructor = False

    return JsonResponse({
        "username": user.username,
        "is_staff": user.is_staff,
        "is_superuser": user.is_superuser,
        "has_instructor_link": is_instructor,
        "session_role": request.session.get("role"),
    })

def _get_user_instructor(user):
    """Return Instructor linked to this user (or None)."""
    if not user.is_authenticated:
        return None
    return Personnel.objects.filter(user=user).first()

def _unique_delegates_for_booking(booking):
    """
    Return DelegateRegister rows for the booking, deduped by (normalized name, DOB).
    Keep the first occurrence (lowest id). If DOB is missing, do NOT dedupe that row.
    """
    qs = (
        DelegateRegister.objects
        .filter(booking_day__booking=booking)
        .order_by("name", "date_of_birth", "id")
        # IMPORTANT: no select_related here; and avoid deferring fields needed by FK traversal
        # (we don't actually traverse booking_day in this helper)
        # If you want to keep 'only', include booking_day_id to be safe:
        # .only("id", "name", "date_of_birth", "outcome", "booking_day_id")
    )

    unique = []
    seen = set()
    for r in qs:
        nm = (r.name or "").strip().lower()
        dob = getattr(r, "date_of_birth", None)
        key = (nm, dob) if dob else ("__nodedob__", r.id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    return unique


def _assessment_selection_context(booking, delegates=None):
    """
    Build competency selection state for a booking matrix.

    Rules:
    - Mandatory competencies: non-optional competencies that are active,
      plus any already assessed for this booking (historic preservation).
    - Optional competencies: selected via Booking.optional_modules.
    - Required optional count comes from CourseType.optional_modules_required.
    """
    if delegates is None:
        delegates = _unique_delegates_for_booking(booking)

    existing_comp_ids = set()
    if delegates:
        existing_comp_ids = set(
            CompetencyAssessment.objects
            .filter(register__in=delegates)
            .values_list("course_competency_id", flat=True)
        )

    required_optional_count = int(getattr(booking.course_type, "optional_modules_required", 0) or 0)

    mandatory_competencies = list(
        CourseCompetency.objects
        .filter(course_type=booking.course_type, is_optional=False)
        .filter(Q(is_active=True) | Q(id__in=existing_comp_ids))
        .order_by("sort_order", "name", "id")
    )

    selected_optional = list(
        booking.optional_modules
        .filter(course_type=booking.course_type, is_optional=True)
        .filter(Q(is_active=True) | Q(id__in=existing_comp_ids))
        .order_by("sort_order", "name", "id")
    )
    if required_optional_count >= 0:
        selected_optional = selected_optional[:required_optional_count]

    selected_ids = {c.id for c in selected_optional}
    optional_pool = list(
        CourseCompetency.objects
        .filter(course_type=booking.course_type, is_optional=True, is_active=True)
        .order_by("sort_order", "name", "id")
    )
    for comp in selected_optional:
        if comp.id not in {c.id for c in optional_pool}:
            optional_pool.append(comp)

    optional_slots = []
    for idx in range(required_optional_count):
        optional_slots.append({
            "index": idx,
            "selected": selected_optional[idx] if idx < len(selected_optional) else None,
        })

    required_competencies = mandatory_competencies + selected_optional
    required_competency_ids = {c.id for c in required_competencies}

    return {
        "mandatory_competencies": mandatory_competencies,
        "selected_optional": selected_optional,
        "optional_slots": optional_slots,
        "optional_pool": optional_pool,
        "required_optional_count": required_optional_count,
        "selected_optional_count": len(selected_optional),
        "required_competencies": required_competencies,
        "required_competency_ids": required_competency_ids,
        "existing_comp_ids": existing_comp_ids,
        "selected_optional_ids": selected_ids,
    }



def _assessment_context(booking, user):
    # permissions: staff or assigned instructor (match the guard used in the view)
    instr = getattr(user, "personnel", None)  # <-- changed from "instructor"

    if not (user.is_staff or (instr and instructor_can_access_booking(instr, booking))):
        raise PermissionError("Not your booking.")

    # --- Delegates: unique by (name + DOB) for the whole booking ---
    delegates = _unique_delegates_for_booking(booking)
    from .services.prior_completion import annotate_registers_prior_pass
    annotate_registers_prior_pass(booking, delegates)

    selection_ctx = _assessment_selection_context(booking, delegates)
    competencies = selection_ctx["mandatory_competencies"]
    selected_optional = selection_ctx["selected_optional"]
    required_competencies = selection_ctx["required_competencies"]

    existing = {}
    if delegates and required_competencies:
        qs = (
            CompetencyAssessment.objects
            .filter(register__in=delegates, course_competency__in=required_competencies)
            .select_related("register", "course_competency")
        )
        for a in qs:
            existing[(a.register_id, a.course_competency_id)] = a

    try:
        from .models import AssessmentLevel
        levels = AssessmentLevel.choices
    except Exception:
        levels = (("na", "Not assessed"), ("c", "Competent"))

    opt_slots = selection_ctx["optional_slots"]

    # For completed/closed courses: suppress rows that have no ticks,
    # but only when at least some ticks exist (avoids blanking out courses
    # where assessments were never recorded electronically).
    if booking.status == 'completed':
        ticked_comp_ids = {
            cid for (_, cid), a in existing.items() if a.level in ('c', 'e')
        }
        if ticked_comp_ids:
            competencies = [c for c in competencies if c.id in ticked_comp_ids]
            opt_slots = [s for s in opt_slots if s.get("selected") and s["selected"].id in ticked_comp_ids]

    # Exam rows / auto-pass exam rules apply only to open courses. Completed
    # bookings keep the legacy matrix exactly as recorded at closure.
    if booking.status == 'completed':
        course_exams_matrix, exam_matrix_rows = [], []
    else:
        course_exams_matrix, exam_matrix_rows = build_assessment_exam_matrix(booking, delegates)

    return {
        "delegates": delegates,
        "competencies": competencies,
        "selected_optional_competencies": selected_optional,
        "optional_slots": opt_slots,
        "optional_pool": selection_ctx["optional_pool"],
        "selected_optional_ids": list(selection_ctx["selected_optional_ids"]),
        "required_optional_count": selection_ctx["required_optional_count"],
        "selected_optional_count": selection_ctx["selected_optional_count"],
        "existing": existing,
        "levels": levels,
        "course_exams_matrix": course_exams_matrix,
        "exam_matrix_rows": exam_matrix_rows,
        "has_course_exams": bool(course_exams_matrix),
    }


def _get_or_create_invoice(booking, personnel=None):
    personnel = personnel or booking.instructor
    if not personnel:
        return None
    inv = booking.invoice_for(personnel)
    if inv:
        return inv
    return Invoice.objects.create(
        booking=booking,
        instructor=personnel,
        invoice_date=now().date(),
        account_name=getattr(personnel, "name_on_account", "") or "",
        sort_code=getattr(personnel, "bank_sort_code", "") or "",
        account_number=getattr(personnel, "bank_account_number", "") or "",
    )

def _attempt_back_url(attempt):
    """
    Best-effort link back to the booking that matches this attempt.
    We match on (course_type, instructor, exam_date in booking days).
    Fallback to instructor_bookings if nothing matches.
    """
    try:
        from .models import BookingDay  # or the correct import path for BookingDay
        day = (
            BookingDay.objects
            .select_related("booking")
            .filter(
                booking__course_type=attempt.exam.course_type,
                booking__instructor=attempt.instructor,
                date=attempt.exam_date or localdate(),
            )
            .order_by("date")
            .first()
        )
        if day and day.booking_id:
            return reverse("instructor_booking_detail", args=[day.booking_id])
    except Exception:
        pass
    return reverse("instructor_bookings")

@login_required
def instructor_booking_detail(request, pk):

    instr = getattr(request.user, "personnel", None)

    booking = get_object_or_404(
        Booking.objects.select_related(
            "course_type", "business", "instructor", "instructor__user", "training_location"
        ).prefetch_related(
            "telegram_notifications",
            "telegram_notifications__personnel",
            "email_notifications",
            "email_notifications__personnel",
            "course_type__online_exercises",
            "days",
        ),
        pk=pk,
    )

    if not instr or not instructor_can_access_booking(instr, booking):
        messages.error(request, "You do not have access to this booking.")
        return redirect("instructor_bookings")

    if request.method == "GET" and request.GET.get("exams_poll"):
        ref = getattr(booking, "course_reference", None) or booking.pk
        print(f"*** [Exams] poll — {ref} ***", flush=True)

    booking.sync_precise_from_admin(save=True)

    is_locked = booking.status == "completed"

    # ------------------------------------------------------------------
    # Ensure invoice exists
    # ------------------------------------------------------------------
    inv = _get_or_create_invoice(booking, personnel=instr)

    # Prefill missing bank details
    prefilled = False
    if not inv.account_name:
        inv.account_name = (booking.instructor.name_on_account or "").strip()
        prefilled = True
    if not inv.sort_code:
        inv.sort_code = (booking.instructor.bank_sort_code or "").strip()
        prefilled = True
    if not inv.account_number:
        inv.account_number = (booking.instructor.bank_account_number or "").strip()
        prefilled = True
    if prefilled:
        inv.save(update_fields=["account_name", "sort_code", "account_number"])

    # ------------------------------------------------------------------
    #                           POST
    # ------------------------------------------------------------------
    if request.method == "POST":

        # ✅ Dummy-only: unset final closure so instructor can continue practising
        if request.POST.get("action") == "reopen_dummy_course":
            if not booking.is_dummy_business:
                messages.error(request, "Only dummy bookings can be reopened from course closure.")
                return redirect(f"{request.path}#closure-pane")

            if booking.status != "completed":
                messages.info(request, "This dummy booking is already open.")
                return redirect(f"{request.path}#closure-pane")

            booking.status = "awaiting_closure"
            booking.date_completed = None
            booking.course_registers_status = None
            booking.assessment_matrix_status = None
            booking.save(update_fields=[
                "status",
                "date_completed",
                "course_registers_status",
                "assessment_matrix_status",
            ])

            messages.success(request, "Dummy booking reopened. Course closure has been unset so you can continue training.")
            return redirect(f"{request.path}#closure-pane")

        # ✅ AUTOSAVE course-closure dropdowns (no locking / no emails yet)
        if request.POST.get("action") == "autosave_closure":

            booking.course_registers_status = request.POST.get("course_registers_status") or None
            booking.assessment_matrix_status = request.POST.get("assessment_matrix_status") or None

            booking.save(update_fields=[
                "course_registers_status",
                "assessment_matrix_status",
            ])

            return redirect(f"{request.path}#closure-pane")


        # ✅ FINAL CLOSE — STEP 3 + STEP 4 (BUILD PDFs — NO EMAILS YET)
        if request.POST.get("action") == "final_close_course":

            print("FINAL CLOSE CLICKED")

            reg_manual = request.POST.get("registers_send_separately") == "on"
            counts_confirm = request.POST.get("delegate_counts_confirm") == "on"
            ass_manual = request.POST.get("assessment_send_separately") == "on"
            feedback_manual = request.POST.get("feedback_send_separately") == "on"

            day_counts = list(
                BookingDay.objects
                .filter(booking=booking)
                .annotate(delegate_count=Count("registers"))
                .order_by("date")
            )
            missing_delegate_days = [d for d in day_counts if (d.delegate_count or 0) <= 0]
            counts_vary = len({d.delegate_count or 0 for d in day_counts}) > 1 if len(day_counts) > 1 else False
            # Keep closure checks aligned with the assessments matrix delegate set
            # (deduped by name + DOB), otherwise duplicates across days appear as
            # false extra pending outcomes.
            pending_outcome_regs = [
                r for r in _unique_delegates_for_booking(booking)
                if (r.outcome is None) or (str(r.outcome).strip() == "") or (str(r.outcome).strip().lower() == "pending")
            ]

            if missing_delegate_days and not reg_manual:
                day_labels = ", ".join(d.date.strftime("%d %b %Y") for d in missing_delegate_days)
                messages.error(
                    request,
                    f"Cannot close course: no delegates recorded for {day_labels}. "
                    "Tick 'registers will be submitted separately' if paper records will follow."
                )
                return redirect(f"{request.path}#closure-pane")

            if counts_vary and not (counts_confirm or reg_manual):
                messages.error(
                    request,
                    "Cannot close course: delegate counts vary across days. "
                    "Please confirm this is accurate (or mark registers as separate submission)."
                )
                return redirect(f"{request.path}#closure-pane")

            if pending_outcome_regs and not ass_manual:
                pending_labels = ", ".join(
                    f"{r.name}" for r in pending_outcome_regs[:8]
                )
                extra = "" if len(pending_outcome_regs) <= 8 else f" and {len(pending_outcome_regs)-8} more"
                messages.error(
                    request,
                    "Cannot close course: assessment outcomes still pending for "
                    f"{pending_labels}{extra}. "
                    "Set outcomes to Pass/Fail/DNF or tick 'assessment matrix will be submitted separately'."
                )
                return redirect(f"{request.path}#closure-pane")

            feedback_count = FeedbackResponse.objects.filter(booking=booking).count()
            if feedback_count <= 0 and not feedback_manual:
                messages.error(
                    request,
                    "Cannot close course: no feedback has been submitted. "
                    "Submit at least one feedback form or tick 'feedback will be submitted separately'."
                )
                return redirect(f"{request.path}#closure-pane")

            reg = "send_later" if reg_manual else "completed"
            ass = "send_later" if ass_manual else "completed"
            booking.course_registers_status = reg
            booking.assessment_matrix_status = ass
            booking.save(update_fields=["course_registers_status", "assessment_matrix_status"])

            print("REGISTER:", reg)
            print("ASSESSMENT:", ass)

            # ✅ Safety check
            if not (reg in ["completed", "send_later"] and ass in ["completed", "send_later"] and booking.status != "completed"):
                print("BLOCKED: Closure conditions not met")
                messages.error(request, "Course cannot be closed yet.")
                return redirect(f"{request.path}#closure-pane")

            # ✅ STEP 4 — BUILD PDFs IN MEMORY (NO EMAILS, NO SAVING)
            print("Generating closure PDFs...")
            pdf_files = []

            # 1️⃣ Assessment Matrix PDF
            try:
                from django.test import RequestFactory
                rf = RequestFactory()
                fake_request = rf.get("/")
                fake_request.user = request.user

                assessment_response = instructor_assessment_pdf(
                    fake_request, booking.id
                )

                assessment_pdf = b"".join(assessment_response)
                assessment_filename = f"assessment-matrix-{booking.course_reference}.pdf"

                pdf_files.append((assessment_filename, assessment_pdf))
                print("Assessment Matrix PDF generated")

            except Exception as e:
                print("Assessment Matrix PDF skipped:", e)


            # 2️⃣ Registers PDFs
            try:
                for d in BookingDay.objects.filter(booking=booking):
                    file_bytes, filename = _get_register_pdf_bytes_via_existing_view(request, d.pk)
                    pdf_files.append((filename, file_bytes))
                print("Registers PDFs generated")
            except Exception as e:
                print("Registers PDF skipped:", e)

            # 3️⃣ Certificates PDF ✅ SAFE FOR EMAIL
            try:
                cert_result = build_certificates_pdf_for_booking(booking)

                # ✅ FORCE CORRECT ORDER NO MATTER WHAT THE FUNCTION RETURNS
                if isinstance(cert_result, tuple) and len(cert_result) == 2:

                    # If returned as (bytes, filename)
                    if isinstance(cert_result[0], (bytes, bytearray)):
                        cert_bytes = cert_result[0]
                        cert_filename = cert_result[1]

                    # If returned as (filename, bytes)
                    else:
                        cert_filename = cert_result[0]
                        cert_bytes = cert_result[1]

                else:
                    raise ValueError("Certificate generator returned invalid format")

                pdf_files.append((cert_filename, cert_bytes))
                print("Certificates PDF generated")

            except Exception as e:
                print("Certificates PDF skipped:", e)

            # 4️⃣ Feedback Summary PDF (STEP 4B)
            try:
                from django.test import RequestFactory
                rf = RequestFactory()
                fake_request = rf.get("/")
                fake_request.user = request.user

                feedback_response = instructor_feedback_pdf_summary(
                    fake_request, booking.id
                )

                feedback_pdf = b"".join(feedback_response)
                feedback_filename = f"feedback-summary-{booking.course_reference}.pdf"

                pdf_files.append((feedback_filename, feedback_pdf))
                print("Feedback Summary PDF generated")

            except Exception as e:
                print(f"Feedback Summary PDF skipped: {e}")

            # -----------------------------------------------------------
            # STEP 5 — SEND COURSE CLOSURE EMAIL (NO INVOICE ATTACHMENTS)
            # -----------------------------------------------------------
            # ✅ ENSURE COMPLETION TIMESTAMP EXISTS BEFORE EMAIL
            if not booking.date_completed:
                booking.date_completed = now()
                booking.save(update_fields=["date_completed"])
            
            dummy_closure_email_sent = False
            if booking.is_dummy_business:
                try:
                    import mimetypes

                    instructor_email = (booking.instructor.email or "").strip()
                    catch_all = (getattr(settings, "DEV_CATCH_ALL_EMAIL", "") or "").strip()

                    subject = f"[DUMMY] Course closure documents — {booking.course_type.name} ({booking.course_reference})"
                    if settings.DEBUG and catch_all:
                        to_recipients = [catch_all]
                        effective_subject = (
                            f"[DEV] {subject} "
                            f"(Would send to instructor: {instructor_email or '(none configured)'})"
                        )
                    else:
                        if not instructor_email:
                            raise ValueError("Instructor email is not configured for this dummy booking.")
                        to_recipients = [instructor_email]
                        effective_subject = subject

                    body = (
                        "Please find attached documents for the dummy / familiarisation course closure.\n\n"
                        f"Course: {booking.course_type.name}\n"
                        f"Business: {booking.business.name}\n"
                        f"Reference: {booking.course_reference}\n"
                        f"Closed on: {booking.date_completed.strftime('%Y-%m-%d %H:%M')}\n\n"
                        "Attached documents:\n"
                        " - Assessment Matrix\n"
                        " - Registers\n"
                        " - Certificates\n"
                        " - Feedback Summary\n\n"
                        "This is a dummy booking test email only. Admin closure email has been skipped.\n\n"
                        "----------------------------------------\n"
                        f"Instructor: {booking.instructor.name}\n"
                        f"Email: {booking.instructor.email or '(none)'}\n"
                    )

                    manual_lines = []
                    if booking.course_registers_status in ("send_later", "separate"):
                        manual_lines.append("Course registers will be submitted manually.")
                    if booking.assessment_matrix_status in ("send_later", "separate"):
                        manual_lines.append("Assessment matrix will be submitted manually.")
                    if feedback_manual:
                        manual_lines.append("Feedback will be submitted manually.")
                    if manual_lines:
                        body += "\nManual submissions to follow:\n" + "\n".join(f" - {ln}" for ln in manual_lines)

                    email = EmailMessage(
                        subject=effective_subject,
                        body=body,
                        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@example.com"),
                        to=to_recipients,
                    )

                    for filename, content in pdf_files:
                        if isinstance(content, tuple):
                            content = content[0]
                        safe_filename = str(filename)
                        mime = mimetypes.guess_type(safe_filename)[0] or "application/pdf"
                        email.attach(safe_filename, content, mime)

                    email.send(fail_silently=False)
                    dummy_closure_email_sent = True
                    print("Dummy closure email sent to instructor route")

                except Exception as e:
                    print("Dummy closure email failed:", e)
                    messages.error(request, f"Dummy course closed, but instructor email failed: {e}")
            else:
                try:
                    import mimetypes

                    admin_email = getattr(settings, "ADMIN_INBOX_EMAIL", "")
                    instructor_email = booking.instructor.email or ""
                    catch_all = getattr(settings, "DEV_CATCH_ALL_EMAIL", "")

                    subject = f"Course closure documents — {booking.course_type.name} ({booking.course_reference})"

                    if settings.DEBUG:
                        to_recipients = [catch_all]
                        effective_subject = (
                            f"[DEV] {subject}  "
                            f"(Would send to: {admin_email}, {instructor_email})"
                        )
                        cc_recipients = []
                    else:
                        to_recipients = [admin_email] if admin_email else []
                        cc_recipients = [instructor_email] if instructor_email else []
                        effective_subject = subject

                    body = (
                        "Please find attached to this email documents relating to the below course.\n\n"
                        f"Course: {booking.course_type.name}\n"
                        f"Business: {booking.business.name}\n"
                        f"Reference: {booking.course_reference}\n"
                        f"Closed on: {booking.date_completed.strftime('%Y-%m-%d %H:%M')}\n\n"
                        "Attached documents:\n"
                        " • Assessment Matrix\n"
                        " • Registers\n"
                        " • Certificates\n"
                        " • Feedback Summary\n\n"
                        "----------------------------------------\n"
                        f"Instructor: {booking.instructor.name}\n"
                        f"Email: {booking.instructor.email}\n"
                    )

                    manual_lines = []
                    if booking.course_registers_status in ("send_later", "separate"):
                        manual_lines.append("Course registers will be submitted manually.")
                    if booking.assessment_matrix_status in ("send_later", "separate"):
                        manual_lines.append("Assessment matrix will be submitted manually.")
                    if feedback_manual:
                        manual_lines.append("Feedback will be submitted manually.")
                    if manual_lines:
                        body += "\nManual submissions to follow:\n" + "\n".join(f" - {ln}" for ln in manual_lines)

                    if isinstance(effective_subject, str):
                        effective_subject = effective_subject.encode("utf-8", "ignore").decode("utf-8")

                    if isinstance(body, str):
                        body = body.encode("utf-8", "ignore").decode("utf-8")

                    email = EmailMessage(
                        subject=effective_subject,
                        body=body,
                        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@example.com"),
                        to=to_recipients,
                        cc=cc_recipients,
                    )
                    email.encoding = "utf-8"

                    for filename, content in pdf_files:
                        if isinstance(content, tuple):
                            content = content[0]

                        if isinstance(filename, bytes):
                            safe_filename = filename.decode("utf-8", "ignore")
                        else:
                            safe_filename = str(filename).encode("utf-8", "ignore").decode("utf-8")

                        mime = mimetypes.guess_type(safe_filename)[0] or "application/pdf"
                        email.attach(safe_filename, content, mime)

                    email.send(fail_silently=False)
                    print("Closure email sent successfully")

                except Exception as e:
                    print("Closure email failed:", e)
                    messages.error(request, f"Course closed, but email failed: {e}")

            print(f"TOTAL PDFs GENERATED: {len(pdf_files)}")

            # ✅ LOCK THE COURSE
            booking.status = "completed"
            booking.save(update_fields=["status"])

            print("COURSE SUCCESSFULLY CLOSED")

            if booking.is_dummy_business:
                if dummy_closure_email_sent:
                    messages.success(request, "✅ Dummy course closed. Documents emailed to instructor only (admin skipped).")
                else:
                    messages.success(request, "✅ Dummy course closed. Admin email was skipped.")
            else:
                messages.success(request, "✅ Course closed and documents emailed to admin and instructor.")
            return redirect(f"{request.path}#closure-pane")

        # -------------------------------
        # existing logic continues here
        # -------------------------------

        action = (request.POST.get("action") or "").strip().lower()
        print("DEBUG ACTION RECEIVED:", action)

        # ------------------------------------------------------
        # UPDATE DAY START/END TIMES (instructor — dates stay admin-only)
        # ------------------------------------------------------
        if action == "update_day_times":
            if booking.status == "cancelled":
                messages.error(request, "Cannot change times on a cancelled booking.")
                return redirect(f"{request.path}#days-pane")
            if is_locked:
                return HttpResponseForbidden("Course is locked.")

            days = list(BookingDay.objects.filter(booking=booking).order_by("date", "id"))
            changed = False
            first_day = days[0] if days else None

            for d in days:
                start_raw = (request.POST.get(f"start_{d.pk}") or "").strip()
                end_raw = (request.POST.get(f"end_{d.pk}") or "").strip()

                new_start = d.start_time
                new_end = d.end_time
                if start_raw:
                    new_start = _parse_posted_clock(start_raw)
                    if new_start is None:
                        messages.error(request, f"Invalid start time for {d.date}.")
                        return redirect(f"{request.path}#days-pane")
                if end_raw:
                    new_end = _parse_posted_clock(end_raw)
                    if new_end is None:
                        messages.error(request, f"Invalid end time for {d.date}.")
                        return redirect(f"{request.path}#days-pane")

                update_fields = []
                if new_start != d.start_time:
                    d.start_time = new_start
                    update_fields.append("start_time")
                if new_end != d.end_time:
                    d.end_time = new_end
                    update_fields.append("end_time")
                if update_fields:
                    d.save(update_fields=update_fields)
                    changed = True

            if changed and first_day and first_day.start_time and booking.start_time != first_day.start_time:
                booking.start_time = first_day.start_time
                booking.save(update_fields=["start_time"])

            if changed:
                messages.success(request, "Course times updated.")
                try:
                    from .utils.booking_change_detection import CHANGE_DATES_TIMES
                    from .utils.booking_notifications import notify_booking_changes

                    notify_booking_changes(
                        booking,
                        intro="Course times for a booking assigned to you have been updated.",
                        changed_areas=[CHANGE_DATES_TIMES],
                    )
                except Exception:
                    pass
            else:
                messages.info(request, "No time changes to save.")

            return redirect(f"{request.path}#days-pane")


        # ------------------------------------------------------
        # PRECISE MAP LOCATION UPDATE (Instructor) ✅ WITH BASELINE
        # ------------------------------------------------------
        if action == "update_precise_location":
            if is_locked:
                return HttpResponseForbidden("Course is locked.")

            lat = request.POST.get("precise_lat")
            lng = request.POST.get("precise_lng")

            if lat and lng:
                booking.precise_lat = float(lat)
                booking.precise_lng = float(lng)
                booking.save(update_fields=["precise_lat", "precise_lng"])

                messages.success(request, "Precise location updated.")
            else:
                messages.error(request, "Invalid map coordinates received.")

            return redirect("instructor_booking_detail", pk=pk)


        # ------------------------------------------------------
        # RESET PRECISE LOCATION BACK TO ADMIN VALUE ✅ FIXED
        # ------------------------------------------------------
        elif action == "reset_precise_location":
            if is_locked:
                return HttpResponseForbidden("Course is locked.")

            if booking.admin_precise_lat and booking.admin_precise_lng:
                booking.precise_lat = float(booking.admin_precise_lat)
                booking.precise_lng = float(booking.admin_precise_lng)
                booking.save(update_fields=["precise_lat", "precise_lng"])
                messages.success(request, "Precise location reset to admin-defined point.")
            else:
                messages.warning(request, "No admin baseline location exists to reset to.")

            return redirect("instructor_booking_detail", pk=pk)


        # ------------------------------------------------------
        # NOTES SAVE
        # ------------------------------------------------------
        elif action == "save_notes":
            form = BookingNotesForm(request.POST, instance=booking)
            if form.is_valid():
                form.save()
                messages.success(request, "Notes saved.")
            else:
                messages.error(request, "Could not save notes.")

            return redirect(reverse("instructor_booking_detail", kwargs={"pk": pk}))


        # ------------------------------------------------------------------
        # MARK INVOICE AS PAID (Instructor)
        # ------------------------------------------------------------------
        elif action == "mark_paid":
            # One-way action: once marked as Paid it cannot be undone via the UI.
            if inv.status == "paid":
                messages.info(request, "Invoice is already marked as Paid.")
                return redirect(f"{request.path}#invoicing-pane")

            # Only allow once the invoice has been sent/viewed.
            if inv.status not in ("sent", "viewed"):
                messages.error(request, "Invoice can only be marked as Paid after it has been sent.")
                return redirect(f"{request.path}#invoicing-pane")

            inv.status = "paid"
            inv.save(update_fields=["status"])
            messages.success(request, "Invoice marked as Paid.")
            return redirect(f"{request.path}#invoicing-pane")


        # ------------------------------------------------------------------
        # SAVE DRAFT OR SEND ADMIN
        # ------------------------------------------------------------------
        if action in ("save_draft", "send_admin"):

            # Update invoice fields every time
            inv.instructor_ref = request.POST.get("instructor_ref", "") or ""
            inv.account_name   = request.POST.get("account_name", "") or ""
            inv.sort_code      = request.POST.get("sort_code", "") or ""
            inv.account_number = request.POST.get("account_number", "") or ""

            # Save line items
            inv.items.exclude(description="Mileage").delete()
            from decimal import Decimal
            for desc, amt in zip(
                request.POST.getlist("item_desc"),
                request.POST.getlist("item_amount")
            ):
                desc = (desc or "").strip()
                if desc:
                    try:
                        amt = Decimal(amt)
                    except:
                        amt = Decimal("0.00")
                    InvoiceItem.objects.create(
                        invoice=inv,
                        description=desc,
                        amount=amt
                    )

            inv.save()

            # AUTOSAVE
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({"ok": True, "saved_at": now().strftime("%H:%M:%S")})

            # ============= 2) SEND ADMIN =============
            if action == "send_admin":

                # Require receipt confirmation
                # confirm = request.POST.get("confirm_receipts")
                # if confirm != "1":
                    # display modal-required page (this only shows in old fallback flows)
                    # return render(
                        # request,
                        # "instructor/confirm_receipts_required.html",
                        # {"booking": booking},
                    # )

                # --- Persist latest invoice values before sending ---
                inv.instructor_ref = request.POST.get("instructor_ref", "") or ""
                inv.account_name   = request.POST.get("account_name", "") or ""
                inv.sort_code      = request.POST.get("sort_code", "") or ""
                inv.account_number = request.POST.get("account_number", "") or ""
                inv.invoice_date   = now().date()   # always today until locked
                inv.status = "draft"

                # Save extra line items
                descs = request.POST.getlist("item_desc")
                amts  = request.POST.getlist("item_amount")

                inv.items.exclude(description="Mileage").delete()
                from decimal import Decimal
                for desc, amt in zip(descs, amts):
                    desc = (desc or "").strip()
                    if not desc:
                        continue
                    try:
                        amt = Decimal(amt)
                    except:
                        amt = Decimal("0.00")
                    InvoiceItem.objects.create(invoice=inv, description=desc, amount=amt)

                inv.save()

                if booking.is_dummy_business:
                    try:
                        file_bytes, filename = render_invoice_pdf_via_preview(request, booking)
                        attachments, link_lines = gather_receipt_attachments_and_links(booking)

                        instr_real = getattr(booking.instructor, "email", "") or ""
                        catch_all = getattr(settings, "DEV_CATCH_ALL_EMAIL", "")
                        if catch_all:
                            to_recipients = [catch_all]
                            effective_subject = (
                                f"[DEV] [DUMMY] Instructor invoice — {booking.course_type.name} "
                                f"({booking.course_reference or booking.pk}) "
                                f"(Would send to instructor: {instr_real or '(none configured)'})"
                            )
                        else:
                            if not instr_real:
                                raise ValueError("Instructor email is not configured for this dummy booking.")
                            to_recipients = [instr_real]
                            effective_subject = (
                                f"[DUMMY] Instructor invoice — {booking.course_type.name} "
                                f"({booking.course_reference or booking.pk})"
                            )

                        body = (
                            f"{_time_greeting()},\n\n"
                            f"Please find attached the invoice for the dummy / familiarisation booking "
                            f"for {booking.course_type.name} completed by {booking.instructor.name} "
                            f"for {booking.business.name}.\n\n"
                            "This is a dummy booking test email only. It has NOT been sent to admin "
                            "and will not enter the normal admin invoice workflow.\n\n"
                            "You can review the booking in the portal if needed.\n\n"
                            "Many thanks\n\n"
                            "Unicorn Admin System\n\n"
                            "https://unicorn.adminforge.co.uk"
                        )

                        if link_lines:
                            body += "\n\nReceipts:\n" + "\n".join(f" - {ln}" for ln in link_lines)

                        email = EmailMessage(
                            subject=effective_subject,
                            body=body,
                            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@example.com"),
                            to=to_recipients,
                        )
                        email.attach(
                            filename=filename,
                            content=file_bytes,
                            mimetype="application/pdf",
                        )
                        for fname, content, mime in attachments:
                            email.attach(filename=fname, content=content, mimetype=mime)

                        email.send(fail_silently=False)

                        inv.status = "sent"
                        inv.date_sent = now()
                        inv.save(update_fields=["status", "date_sent", "invoice_date", "instructor_ref", "account_name", "sort_code", "account_number"])
                        messages.success(request, "Dummy booking invoice emailed to instructor only. Admin email skipped.")
                    except Exception as e:
                        messages.error(request, f"Failed to send dummy invoice email: {e}")

                    return redirect(
                        f"{reverse('instructor_booking_detail', kwargs={'pk': booking.pk})}?tab=invoicing"
                    )

                # ----------------------------------------------------------------------
                #                BUILD PDF + GATHER RECEIPTS + SEND EMAIL
                # ----------------------------------------------------------------------
                try:
                    import mimetypes

                    # Generate invoice PDF identical to preview
                    file_bytes, filename = render_invoice_pdf_via_preview(request, booking)

                    # Gather receipts from Drive
                    attachments, link_lines = gather_receipt_attachments_and_links(booking)

                    # Subject + body
                    subj = f"[Unicorn] Instructor invoice — {booking.course_type.name} ({booking.course_reference or booking.pk})"
                    body = (
                        f"{_time_greeting()},\n\n"
                        f"Please find attached invoice for the {booking.course_type.name} course "
                        f"completed by {booking.instructor.name} for {booking.business.name}.\n\n"
                        "You can login to the portal to view this receipt and update its status.\n\n"
                        "Many thanks\n\n"
                        "Unicorn Admin System\n\n"
                        "https://unicorn.adminforge.co.uk"
                    )

                    if link_lines:
                        body += "Receipts:\n" + "\n".join(f" - {ln}" for ln in link_lines)
                    else:
                        body += "Receipts: (none found)"

                    # Determine recipients
                    admin_real = getattr(settings, "ADMIN_INBOX_EMAIL", "")
                    instr_real = getattr(booking.instructor, "email", "") or ""
                    catch_all  = getattr(settings, "DEV_CATCH_ALL_EMAIL", "")

                    if settings.DEBUG:
                        to_recipients = [catch_all]
                        effective_subject = f"[DEV] {subj}  (Would send to: {admin_real}, {instr_real})"
                        cc_recipients = []
                    else:
                        to_recipients = [admin_real] if admin_real else []
                        cc_recipients = [instr_real] if instr_real else []
                        effective_subject = subj

                    # Build email
                    email = EmailMessage(
                        subject=effective_subject,
                        body=body,
                        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@example.com"),
                        to=to_recipients,
                        cc=cc_recipients,
                    )

                    # Attach invoice PDF
                    email.attach(
                        filename=filename,
                        content=file_bytes,
                        mimetype=mimetypes.guess_type(filename)[0] or "application/pdf"
                    )

                    # Attach receipts
                    for fname, content, mime in attachments:
                        email.attach(filename=fname, content=content, mimetype=mime)

                    # Send email
                    email.send(fail_silently=False)

                    # Lock invoice
                    inv.status = "sent"
                    inv.date_sent = now()
                    inv.save(update_fields=["status", "date_sent"])

                    # optional admin workflow
                    try:
                        inv.admin_status = "pending"
                        inv.save(update_fields=["admin_status"])
                    except:
                        pass

                    messages.success(request, "Invoice emailed to admin successfully.")

                except Exception as e:
                    messages.error(request, f"Failed to send invoice: {e}")
                    return redirect(
                        f"{reverse('instructor_booking_detail', kwargs={'pk': booking.pk})}?tab=invoicing"
                    )

                return redirect(
                    f"{reverse('instructor_booking_detail', kwargs={'pk': booking.pk})}?tab=invoicing"
                )


            # NORMAL SAVE
            return redirect(
                f"{reverse('instructor_booking_detail', kwargs={'pk': pk})}?tab=invoicing"
            )

    # ------------------------------------------------------------------
    # Build context (your unchanged blocks)
    # ------------------------------------------------------------------
    from .views_instructor import _invoicing_tab_context
    from .views_instructor import _assessment_context

    active_tab = request.POST.get("active_tab") or request.GET.get("tab", "")
    if active_tab.endswith("-pane"):
        active_tab = active_tab[:-5]
    if not active_tab:
        active_tab = "days" if request.GET.get("day") else "course-info"

    ctx = {
        "title": booking.course_type.name,
        "booking": booking,
        "invoice": inv,
        "is_locked": is_locked,
        "active_tab": active_tab,
    }

    # ✅ Closure prerequisites and readiness
    reg = booking.course_registers_status
    ass = booking.assessment_matrix_status
    status = booking.status

    closure_day_counts_qs = list(
        BookingDay.objects
        .filter(booking=booking)
        .annotate(delegate_count=Count("registers"))
        .order_by("date")
    )
    closure_day_delegate_counts = [
        {"date": d.date, "count": d.delegate_count or 0}
        for d in closure_day_counts_qs
    ]
    closure_missing_delegate_days = [d for d in closure_day_counts_qs if (d.delegate_count or 0) <= 0]
    closure_counts_vary = (
        len({d.delegate_count or 0 for d in closure_day_counts_qs}) > 1
        if len(closure_day_counts_qs) > 1 else False
    )
    closure_pending_outcomes = [
        r for r in _unique_delegates_for_booking(booking)
        if (r.outcome is None) or (str(r.outcome).strip() == "") or (str(r.outcome).strip().lower() == "pending")
    ]
    closure_feedback_count = FeedbackResponse.objects.filter(booking=booking).count()
    closure_missing_feedback = closure_feedback_count <= 0

    can_close_course = (
        reg in ["completed", "send_later"]
        and ass in ["completed", "send_later"]
        and status != "completed"
        and not closure_missing_delegate_days
        and not closure_pending_outcomes
        and not closure_missing_feedback
        and not closure_counts_vary
    )

    # Skip noisy debug on background pane refreshes (exams/days poll the full page)
    if request.method == "GET" and not (
        request.GET.get("exams_poll")
        or request.headers.get("X-Requested-With") in ("fetch", "XMLHttpRequest")
    ):
        print("CLOSURE CHECK:")
        print("REGISTER:", repr(reg))
        print("ASSESSMENT:", repr(ass))
        print("BOOKING STATUS:", repr(status))
        print("CAN CLOSE:", can_close_course)

    ctx["can_close_course"] = can_close_course
    ctx["closure_day_delegate_counts"] = closure_day_delegate_counts
    ctx["closure_missing_delegate_days"] = closure_missing_delegate_days
    ctx["closure_counts_vary"] = closure_counts_vary
    ctx["closure_pending_outcomes"] = closure_pending_outcomes
    ctx["closure_feedback_count"] = closure_feedback_count
    ctx["closure_missing_feedback"] = closure_missing_feedback



    # -----------------------------------------
    # FEEDBACK CONTEXT (FIX)
    # -----------------------------------------
    from django.db.models import Avg

    fb_qs = FeedbackResponse.objects.filter(booking=booking)

    ctx["fb_qs"] = fb_qs
    ctx["fb_count"] = fb_qs.count()
    ctx["fb_avg"] = fb_qs.aggregate(avg=Avg("overall_rating"))["avg"]

    try:
        ctx.update(_invoicing_tab_context(booking, personnel=instr))
    except:
        pass

    try:
        ctx.update(_assessment_context(booking, request.user))
    except:
        pass

    # ---------------------------------------------------------
    # EXAMS TAB CONTEXT (match the admin booking logic)
    # ---------------------------------------------------------
    course_exams = []
    attempts_by_exam = {}
    exam_wrong_summary_by_seq = {}
    has_exam = False

    # All exams for this course type
    course_exams = list(
        Exam.objects.filter(course_type=booking.course_type).order_by("sequence", "id")
    )

    # Course "has exams" if the flag is set OR there are Exam rows
    has_exam = bool(course_exams) or getattr(booking.course_type, "has_exam", False)

    if course_exams:
        booking_dates = list(
            BookingDay.objects.filter(booking=booking).values_list("date", flat=True)
        )

        if booking_dates:
            tmp = defaultdict(list)
            attempts_qs = (
                ExamAttempt.objects
                .select_related("exam")
                .filter(
                    exam__course_type=booking.course_type,
                    instructor=booking.instructor,
                    exam_date__in=booking_dates,
                )
                .order_by("exam_date", "id")
            )

            for att in attempts_qs:
                seq = getattr(att.exam, "sequence", None) or getattr(att.exam, "id")
                tmp[seq].append(att)

            attempts_by_exam = dict(tmp)
            completed_attempts = [a for a in attempts_qs if getattr(a, "finished_at", None)]
            exam_wrong_summary_by_seq = _booking_exam_wrong_summary(course_exams, completed_attempts)

    ctx["course_exams"] = course_exams
    ctx["attempts_by_exam"] = attempts_by_exam
    ctx["exam_wrong_summary_by_seq"] = exam_wrong_summary_by_seq
    ctx["has_exam"] = has_exam

    from .utils.online_exercises import booking_accident_reports_enabled
    from .utils.accident_reports import booking_accident_reports_context

    has_accident_reports = booking_accident_reports_enabled(booking)
    ctx["has_accident_reports"] = has_accident_reports
    if has_accident_reports:
        ctx.update(booking_accident_reports_context(request, booking, portal="instructor"))

    if ctx.get("active_tab") == "exams" and not has_exam:
        ctx["active_tab"] = "course-info"
    if ctx.get("active_tab") == "accident-reports" and not has_accident_reports:
        ctx["active_tab"] = "course-info"
    
    # -------------------------------------------------------
    # Build day_rows for template
    # -------------------------------------------------------
    days = (
        BookingDay.objects
        .filter(booking=booking)
        .select_related("instructor", "booking")
        .order_by("date")
    )

    day_rows = []
    prior_day = None
    for d in days:
        n = DelegateRegister.objects.filter(booking_day=d).count()

        warn_count = DelegateRegister.objects.filter(
            booking_day=d,
            date_of_birth__isnull=True
        ).count()

        day_rows.append({
            "id": d.pk,
            "date": d.date,
            "start_time": d.start_time,
            "end_time": d.end_time,
            "n": n,
            "warn": warn_count > 0,
            "warn_count": warn_count,
            "previous_id": prior_day.pk if prior_day else None,
            "previous_date": prior_day.date if prior_day else None,
            "can_copy": _can_copy_delegates_onto(instr, d, prior_day),
            "edit_url": (
                reverse("instructor_booking_detail", kwargs={"pk": booking.pk})
                + f"?day={d.pk}#days-pane"
            ),
        })
        prior_day = d

    ctx["day_rows"] = day_rows
    ctx["days"] = days
    ctx["last_day"] = days.last()
    ctx["open_exam_sequence"] = _open_exam_sequence_for_booking(days, course_exams)

    selected_day = None
    selected_day_rows = []
    selected_day_id = request.GET.get("day")
    if selected_day_id:
        try:
            selected_day = days.filter(pk=int(selected_day_id)).first()
        except (TypeError, ValueError):
            selected_day = None

    if selected_day:
        selected_day_back_url = (
            reverse("instructor_booking_detail", kwargs={"pk": booking.pk})
            + f"?day={selected_day.pk}#days-pane"
        )
        selected_regs = (
            DelegateRegister.objects.filter(booking_day=selected_day)
            .order_by("name")
            .only("id", "name", "date_of_birth", "job_title", "employee_id", "health_status", "notes")
        )
        for reg in selected_regs:
            symbol, cls, title = _health_badge_tuple(reg.health_status)
            selected_day_rows.append({
                "obj": reg,
                "health_symbol": symbol,
                "health_class": cls,
                "health_title": title,
                "edit_url": reverse("instructor_delegate_edit", args=[reg.pk]) + "?" + urlencode({"next": selected_day_back_url}),
            })
        from .services.prior_completion import annotate_registers_prior_pass
        annotate_registers_prior_pass(booking, selected_day_rows)

        ctx["selected_day"] = selected_day
        ctx["selected_day_rows"] = selected_day_rows
        ctx["selected_day_back_url"] = selected_day_back_url
        ctx["selected_day_add_url"] = reverse("instructor_delegate_new", args=[selected_day.pk]) + "?" + urlencode({"next": selected_day_back_url})
        ctx["selected_day_full_url"] = reverse("instructor_day_registers", args=[selected_day.pk])
        ctx["selected_day_pdf_url"] = reverse("instructor_day_registers_pdf", args=[selected_day.pk])
        ctx["previous_day"] = _previous_booking_day(selected_day)
        ctx["can_copy_selected"] = _can_copy_delegates_onto(
            instr, selected_day, ctx["previous_day"]
        )
    else:
        ctx["selected_day"] = None
        ctx["selected_day_rows"] = []
        ctx["previous_day"] = None
        ctx["can_copy_selected"] = False

    # For Quick test links (dummy bookings always; all bookings when DEBUG)
    from .utils.quick_test_links import attach_quick_test_links_context
    attach_quick_test_links_context(ctx, booking, days=days)

    # -------------------------------------------------------
    # Build unified event description for Google & ICS
    # -------------------------------------------------------
    lines = []

    # Notes
    if booking.booking_notes:
        lines.append(f"Notes: {booking.booking_notes}")

    # Location contact details
    if booking.contact_name or booking.telephone or booking.email:
        lines.append("Contact details:")
        if booking.contact_name:
            lines.append(f"  - Name: {booking.contact_name}")
        if booking.telephone:
            lines.append(f"  - Phone: {booking.telephone}")
        if booking.email:
            lines.append(f"  - Email: {booking.email}")

    # Instructor fees
    fee_lines = []
    if booking.instructor_fee:
        fee_lines.append(f"Instructor fee: £{booking.instructor_fee}")

    if booking.allow_mileage_claim:
        if booking.mileage_fee:
            fee_lines.append(f"Mileage allowance: £{booking.mileage_fee}")
        else:
            fee_lines.append("Mileage: allowed")

    if booking.allow_accommodation:
        fee_lines.append("Accommodation: allowed")

    if fee_lines:
        lines.append("Instructor claims:")
        for ln in fee_lines:
            lines.append(f"  - {ln}")

    event_description = "\n".join(lines).strip()

    ctx["event_description"] = event_description

    # -----------------------------------------
    # Build Google Calendar links per BookingDay
    # -----------------------------------------
    import urllib.parse

    gcal_links = []

    for d in days:
        date_str = d.date.strftime("%Y%m%d")

        start_t = d.start_time or clock_time(9, 0)
        end_t   = d.end_time   or clock_time(17, 0)

        start_str = start_t.strftime("%H%M%S")
        end_str   = end_t.strftime("%H%M%S")

        # Booking title
        title = f"{booking.course_type.name}"

        # Location
        if booking.training_location:
            loc = (
                f"{booking.training_location.address_line}, "
                f"{booking.training_location.town} "
                f"{booking.training_location.postcode}"
            )
        else:
            loc = ""

        # TEMPLATE URL opens the Google Calendar app on Android when installed;
        # the /u/0/r/eventedit path forces the desktop website on mobile.
        gcal_url = (
            "https://calendar.google.com/calendar/render?action=TEMPLATE"
            + "&text=" + urllib.parse.quote(title)
            + "&dates=" + date_str + "T" + start_str + "/" + date_str + "T" + end_str
            + "&ctz=Europe/London"
            + "&details=" + urllib.parse.quote(event_description)
            + "&location=" + urllib.parse.quote(loc)
        )

        gcal_links.append({
            "date": d.date,
            "url": gcal_url,
        })

    ctx["gcal_links"] = gcal_links
    from .utils.communication_log import communication_log_entries, user_editable_booking_notes

    ctx["notes_form"] = BookingNotesForm(
        initial={"booking_notes": user_editable_booking_notes(booking)},
    )
    try:
        ctx["communication_log_entries"] = communication_log_entries(booking)
    except Exception:
        ctx["communication_log_entries"] = []

    ctx["cert_delegates"] = (
        cert_pass_delegates_for_booking(booking) if is_locked else []
    )

    return render(request, "instructor/booking_detail.html", ctx)

@login_required
@require_POST
def instructor_assessment_autosave(request, pk):
    """
    AJAX: toggle a single competency checkbox for a delegate register row.
    Creates/updates CompetencyAssessment with the correct foreign keys.
    """
    if request.method != "POST" or not request.headers.get("x-requested-with"):
        return JsonResponse({"ok": False, "error": "Bad request"}, status=400)

    # --- lookups
    reg_id  = request.POST.get("register_id")
    comp_id = request.POST.get("competency_id")
    checked = (request.POST.get("checked") or "").lower() in ("1", "true", "yes", "on")

    try:
        reg = DelegateRegister.objects.select_related("booking_day__booking").get(id=reg_id)
    except DelegateRegister.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Register not found"}, status=404)

    # safety: ensure this row belongs to the booking in the URL
    if str(reg.booking_day.booking_id) != str(pk):
        return JsonResponse({"ok": False, "error": "Mismatched booking"}, status=400)

    try:
        comp = CourseCompetency.objects.get(id=comp_id)
    except CourseCompetency.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Competency not found"}, status=404)

    allowed_ids = set(
        _assessment_selection_context(reg.booking_day.booking)["required_competency_ids"]
    )
    allowed_ids.update(
        CompetencyAssessment.objects.filter(register=reg).values_list(
            "course_competency_id", flat=True
        )
    )
    if comp.id not in allowed_ids:
        return JsonResponse(
            {"ok": False, "error": "This competency is not enabled for this course assessment."},
            status=400,
        )

    # we must store an Instructor instance, not a User
    instr = Personnel.objects.filter(user=request.user).first()
    if not instr:
        return JsonResponse({"ok": False, "error": "No instructor profile found"}, status=400)

    # Block changes if the course is closed
    if getattr(reg.booking_day.booking, "status", "") == "completed":
        return JsonResponse(
            {"ok": False, "error": "Course is closed — only Invoicing is editable."},
            status=400,
        )

    # 🔒 NEW: lock whole column when outcome is DNF/Fail
    # This mirrors your prod behaviour: once DNF/Fail is chosen for the delegate,
    # all checkboxes in that column are locked in their current state until set back to Pending.
    if (reg.outcome or "").lower() in ("dnf", "fail"):
        return JsonResponse(
            {"ok": False, "error": "Assessments are locked for this delegate (Outcome set to DNF/Fail)."},
            status=400
        )

    # --- create / update competency assessment
    ca, created = CompetencyAssessment.objects.get_or_create(
        register=reg,
        course_competency=comp,
        defaults={"assessed_by": instr, "level": "c" if checked else "na"},
    )

    if not created:
        # 🚫 prevent unchecking a carried-forward competency (from DNF carry-forward)
        if ca.is_locked and not checked:
            return JsonResponse(
                {"ok": False, "error": "This competency is locked (carried forward)."},
                status=400
            )

        # normal update when not locked (or when checking it)
        ca.level = "c" if checked else "na"
        ca.assessed_by = instr
        ca.save(update_fields=["level", "assessed_by"])

    else:
        # record was just created; defaults already set
        pass

    try:
        recompute_delegate_outcome(reg.booking_day.booking, reg)
    except Exception:
        pass

    return JsonResponse({"ok": True})


@login_required
@require_POST
def instructor_assessment_optional_modules_save(request, pk):
    instr = _get_instructor(request.user)
    booking = get_object_or_404(
        Booking.objects.select_related("course_type", "instructor"),
        pk=pk,
    )

    if not instr or not instructor_can_access_booking(instr, booking):
        return JsonResponse({"ok": False, "error": "Forbidden"}, status=403)

    if getattr(booking, "status", "") == "completed":
        return JsonResponse(
            {"ok": False, "error": "Course is closed — only Invoicing is editable."},
            status=400,
        )

    required = int(getattr(booking.course_type, "optional_modules_required", 0) or 0)
    raw_ids = request.POST.getlist("selected_ids[]") or request.POST.getlist("selected_ids")

    cleaned_ids = []
    seen = set()
    for raw in raw_ids:
        val = (raw or "").strip()
        if not val or val in seen:
            continue
        seen.add(val)
        cleaned_ids.append(val)

    if len(cleaned_ids) > required:
        return JsonResponse(
            {"ok": False, "error": f"Only {required} optional module(s) can be selected."},
            status=400,
        )

    if required == 0 and cleaned_ids:
        return JsonResponse(
            {"ok": False, "error": "This course does not allow optional module selections."},
            status=400,
        )

    valid_qs = CourseCompetency.objects.filter(
        id__in=cleaned_ids,
        course_type=booking.course_type,
        is_optional=True,
        is_active=True,
    )
    valid_ids = {str(c.id) for c in valid_qs}
    if len(valid_ids) != len(cleaned_ids):
        return JsonResponse(
            {"ok": False, "error": "One or more selected optional modules are invalid."},
            status=400,
        )

    booking.optional_modules.set(valid_qs)

    return JsonResponse(
        {
            "ok": True,
            "required": required,
            "selected_count": len(cleaned_ids),
            "selected_ids": cleaned_ids,
        }
    )

@login_required
def instructor_day_registers_poll(request, pk: int):
    """
    Lightweight GET endpoint used by client-side code to refresh the register
    rows for a given day without a full page reload.
    """
    if request.method != "GET":
        return JsonResponse({"ok": False, "error": "GET only"}, status=405)

    instr = getattr(request.user, "personnel", None)
    day = get_object_or_404(
        BookingDay.objects.select_related(
            "booking__course_type", "booking__business", "booking__instructor"
        ),
        pk=pk,
    )
    if not instr or not instructor_can_access_day(instr, day):
        return JsonResponse({"ok": False, "error": "Forbidden"}, status=403)

    qs = (
        DelegateRegister.objects.filter(booking_day=day)
        .order_by("name")
        .only("name", "date_of_birth", "job_title", "employee_id", "health_status", "notes")
    )

    rows = []
    for r in qs:
        symbol, cls, title = _health_badge_tuple(r.health_status)
        rows.append({
            "obj": r,
            "health_symbol": symbol,
            "health_class": cls,
            "health_title": title,
            # Optional DOB-mismatch UI (safe defaults)
            "dob_mismatch": False,
            "dob_expected": None,
        })
    from .services.prior_completion import annotate_registers_prior_pass
    annotate_registers_prior_pass(day.booking, rows)

    html = render_to_string("instructor/_day_register_rows.html", {"rows": rows}, request=request)
    return JsonResponse({"ok": True, "day_id": pk, "html": html, "rows": len(rows)})

@login_required
@require_POST
def instructor_assessment_outcome_autosave(request, pk):
    """
    Persist a column outcome for a delegate.
    POST: register_id, outcome ("pending"|"dnf"|"fail"|"pass")

    Propagates the outcome across *all days* of the same booking for the same
    person (normalized name + DOB when present).
    """
    # Guard: instructor & booking match
    instr = _get_instructor(request.user)
    booking = get_object_or_404(
        Booking.objects.select_related("course_type", "instructor"),
        pk=pk
    )
    if not instr or not instructor_can_access_booking(instr, booking):
        return JsonResponse({"ok": False, "error": "Forbidden"}, status=403)
    
        # Block outcome changes if the course is closed
    if getattr(booking, "status", "") == "completed":
        return JsonResponse(
            {"ok": False, "error": "Course is closed — only Invoicing is editable."},
            status=400,
        )

    reg_id  = request.POST.get("register_id")
    outcome = (request.POST.get("outcome") or "").lower().strip()
    if outcome not in {"pending", "dnf", "fail", "pass"}:
        return JsonResponse({"ok": False, "error": "Invalid outcome"}, status=400)

    # The specific row that was changed (must belong to this booking)
    reg = get_object_or_404(
        DelegateRegister.objects.select_related("booking_day__booking"),
        pk=reg_id, booking_day__booking=booking,
    )

    # same person (case-insensitive name) on the same booking; constrain by DOB if present
    q = DelegateRegister.objects.filter(
        booking_day__booking=booking,
        name__iexact=(reg.name or "").strip(),
    )
    if reg.date_of_birth:
        q = q.filter(date_of_birth=reg.date_of_birth)

    updated = q.update(outcome=outcome)
    return JsonResponse({"ok": True, "outcome": outcome, "updated": int(updated)})


@login_required
@require_POST
@transaction.atomic
def instructor_copy_previous_day_registers(request, pk: int):
    """Copy delegate names from the previous course day onto this day."""
    instr = getattr(request.user, "personnel", None)
    day = get_object_or_404(
        BookingDay.objects.select_related(
            "booking__instructor", "booking__course_type", "instructor"
        ),
        pk=pk,
    )
    if not instr or not instructor_can_access_booking(instr, day.booking):
        messages.error(request, "You do not have access to this booking.")
        return redirect("instructor_bookings")

    fallback_url = (
        reverse("instructor_booking_detail", kwargs={"pk": day.booking_id})
        + f"?day={day.pk}#days-pane"
    )
    next_url = _safe_next_url(request, fallback_url)

    previous = _previous_booking_day(day)
    if not previous:
        messages.error(
            request,
            "This is the first day of the course, so there is no earlier register to copy.",
        )
        return redirect(next_url)
    if not _can_copy_delegates_onto(instr, day, previous):
        messages.error(
            request,
            "You can copy delegates onto a day you are delivering, or onto the next day after one you are delivering.",
        )
        return redirect(next_url)

    guard = _closed_guard(request, day.booking)
    if guard:
        return guard

    prev_label = date_format(previous.date, "j M Y")
    if not DelegateRegister.objects.filter(booking_day=previous).exists():
        messages.warning(request, f"There are no delegates on {prev_label} to copy.")
        return redirect(next_url)

    # When you only taught the day before, the new rows stay with the
    # instructor who is delivering the day you are copying onto.
    if instructor_can_access_day(instr, day):
        owner = instr
    else:
        owner = day.instructor or day.booking.instructor or instr
    copied, skipped = _copy_delegates_from_day(previous, day, owner)
    if copied == 0:
        messages.info(request, f"Everyone from {prev_label} is already on this register.")
    elif skipped:
        messages.success(
            request,
            f"Copied {copied} delegate{'' if copied == 1 else 's'} from {prev_label}. "
            f"{skipped} {'was' if skipped == 1 else 'were'} already on this day.",
        )
    else:
        messages.success(
            request,
            f"Copied {copied} delegate{'' if copied == 1 else 's'} from {prev_label}.",
        )
    return redirect(next_url)


@login_required
def instructor_day_registers(request, pk: int):
    """
    Read-only list of delegates for a day, with coloured health indicator and Edit/Delete buttons.
    """
    instr = getattr(request.user, "personnel", None)
    day = get_object_or_404(
        BookingDay.objects.select_related(
            "booking__course_type", "booking__business", "booking__instructor"
        ),
        pk=pk,
    )
    if not instr or not instructor_can_access_day(instr, day):
        messages.error(request, "You do not have access to this register.")
        return redirect("instructor_bookings")

    # Pull only what we need, sorted alphabetically by name
    qs = (
        DelegateRegister.objects.filter(booking_day=day)
        .order_by("name")  # alphabetical
        .only("name", "date_of_birth", "job_title", "employee_id", "health_status", "notes")
    )

    rows = []
    for r in qs:
        symbol, cls, title = _health_badge_tuple(r.health_status)
        rows.append({
            "obj": r,
            "health_symbol": symbol,
            "health_class": cls,
            "health_title": title,
        })
    from .services.prior_completion import annotate_registers_prior_pass
    annotate_registers_prior_pass(day.booking, rows)

    return render(request, "instructor/day_registers.html", {
        "title": f"Registers — {day.booking.course_type.name} — {date_format(day.date, 'j M Y')}",
        "day": day,
        "previous_day": _previous_booking_day(day),
        "rows": rows,
        "back_url": redirect("instructor_booking_detail", pk=day.booking_id).url,
        "legend": [
            ("✔", "bg-success", "Fit to take part"),
            ("■", "bg-warning text-dark", "Impairment – agreed adjustments"),
            ("▲", "bg-warning", "Impairment – will discuss with instructor"),
            ("✖", "bg-danger", "Not fit to take part today"),
        ],
    })


@login_required
@transaction.atomic
def instructor_delegate_edit(request, pk: int):
    """
    Edit a single delegate row. Instructor FK is left unchanged on save
    (access is gated by instructor_can_access_day).
    """
    instr = getattr(request.user, "personnel", None)
    reg = get_object_or_404(
        DelegateRegister.objects.select_related("booking_day__booking__instructor"),
        pk=pk
    )
    if not instr or not instructor_can_access_day(instr, reg.booking_day):
        messages.error(request, "You do not have access to edit this delegate.")
        return redirect("instructor_bookings")
    
    guard = _closed_guard(request, reg.booking_day.booking)
    if guard:
        return guard

    fallback_url = redirect("instructor_day_registers", pk=reg.booking_day_id).url
    next_url = _safe_next_url(request, fallback_url)

    if request.method == "POST" and "delete" in request.POST:
        reg.delete()
        messages.success(request, "Delegate removed.")
        return redirect(next_url)

    if request.method == "POST":
        form = DelegateRegisterInstructorForm(
            request.POST, instance=reg, current_instructor=instr
        )
        if form.is_valid():
            old_name = (reg.name or "").strip()
            old_dob = reg.date_of_birth
            obj = form.save()

            # Keep multi-day identity in sync (same person on other days of this booking)
            if old_name:
                booking = reg.booking_day.booking
                siblings = DelegateRegister.objects.filter(
                    booking_day__booking=booking,
                    name__iexact=old_name,
                ).exclude(pk=obj.pk)
                if old_dob:
                    siblings = siblings.filter(date_of_birth=old_dob)
                siblings.update(
                    name=obj.name,
                    date_of_birth=obj.date_of_birth,
                    job_title=obj.job_title,
                    employee_id=obj.employee_id,
                )

            messages.success(request, "Delegate updated.")
            return redirect(next_url)
        messages.error(request, "Please correct the errors below.")
    else:
        form = DelegateRegisterInstructorForm(
            instance=reg, current_instructor=instr
        )

    return render(request, "instructor/delegate_form.html", {
        "title": f"Edit delegate — {reg.name}",
        "form": form,
        "reg": reg,
        "day": reg.booking_day,
        "back_url": next_url,
    })


# -----------------------------
# 1️⃣ HELPER FUNCTION
# -----------------------------
def _carry_competencies_from_prior_dnf(new_reg):
    """
    If the same delegate (name + dob) had a DNF for the same course type
    in the last 2 years, copy any competencies already achieved as locked ticks.
    """
    from .models import DelegateRegister, CompetencyAssessment

    if not (new_reg and new_reg.booking_day_id):
        return 0

    bd = new_reg.booking_day
    course_type = bd.booking.course_type
    dob = new_reg.date_of_birth
    name = (new_reg.name or "").strip()

    if not (name and dob and course_type):
        return 0

    two_years_ago = timezone.localdate() - timedelta(days=730)

    prior_regs = (
        DelegateRegister.objects
        .filter(
            name__iexact=name,
            date_of_birth=dob,
            outcome='dnf',
            booking_day__booking__course_type=course_type,
            booking_day__date__gte=two_years_ago,
            booking_day__date__lt=bd.date,
        )
        .order_by('-booking_day__date', '-id')
    )

    prior = prior_regs.first()
    if not prior:
        return 0

    prev_assessments = (
        CompetencyAssessment.objects
        .filter(register=prior, level__in=['c', 'e'])
        .select_related('course_competency')
    )

    created_count = 0
    for pa in prev_assessments:
        ca, created = CompetencyAssessment.objects.get_or_create(
            register=new_reg,
            course_competency=pa.course_competency,
            defaults={
                "level": pa.level,
                "assessed_by": new_reg.instructor,
                "is_locked": True,
                "source_note": f"carried from DNF on {prior.booking_day.date:%Y-%m-%d}",
            },
        )
        if not created:
            if ca.level in ('na', 'p'):
                ca.level = pa.level
            ca.is_locked = True
            ca.source_note = f"carried from DNF on {prior.booking_day.date:%Y-%m-%d}"
            ca.save(update_fields=['level', 'is_locked', 'source_note'])
        created_count += 1

    return created_count


# -----------------------------
# 2️⃣ EXISTING VIEW WITH ONE EXTRA LINE
# -----------------------------
@login_required
@transaction.atomic
def instructor_delegate_new(request, day_pk: int):
    instr = getattr(request.user, "personnel", None)
    day = get_object_or_404(
        BookingDay.objects.select_related("booking__instructor", "booking__course_type"),
        pk=day_pk
    )
    if not instr or not instructor_can_access_day(instr, day):
        messages.error(request, "You do not have access to this register.")
        return redirect("instructor_bookings")
    
    guard = _closed_guard(request, day.booking)
    if guard:
        return guard

    fallback_url = redirect("instructor_day_registers", pk=day.pk).url
    next_url = _safe_next_url(request, fallback_url)

    if request.method == "POST":
        form = DelegateRegisterInstructorForm(request.POST, current_instructor=instr)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.booking_day = day
            obj.instructor = instr
            if not obj.date:
                obj.date = day.date or timezone.localdate()
            obj.save()
            messages.success(request, "Delegate added.")
            return redirect(next_url)
        messages.error(request, "Please correct the errors below.")
    else:
        form = DelegateRegisterInstructorForm(current_instructor=instr)

    return render(request, "instructor/delegate_form.html", {
        "title": "Add delegate",
        "form": form,
        "day": day,
        "back_url": next_url,
    })

@login_required
def instructor_register_edit(request, pk: int):
    instr = getattr(request.user, "personnel", None)
    reg = get_object_or_404(
        DelegateRegister.objects.select_related("booking_day__booking__course_type"),
        pk=pk
    )
    if not instr or not instructor_can_access_day(instr, reg.booking_day):
        messages.error(request, "You do not have access to edit this delegate.")
        return redirect("instructor_bookings")
    
    guard = _closed_guard(request, reg.booking_day.booking)
    if guard:
        return guard

    fallback_url = redirect("instructor_day_registers", pk=reg.booking_day_id).url
    next_url = _safe_next_url(request, fallback_url)

    if request.method == "POST":
        form = DelegateRegisterInstructorForm(request.POST, instance=reg, current_instructor=instr)
        if form.is_valid():
            form.save()
            messages.success(request, "Delegate updated.")
            return redirect(next_url)
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        form = DelegateRegisterInstructorForm(instance=reg, current_instructor=instr)

    day = reg.booking_day
    return render(request, "instructor/register_edit.html", {
        "title": f"Edit delegate — {reg.name}",
        "form": form,
        "reg": reg,
        "day": day,
        "back_url": next_url,
    })

@login_required
def instructor_delegate_delete(request, pk: int):
    """Delete a single DelegateRegister row (POST only, with confirm)."""
    instr = getattr(request.user, "personnel", None)
    reg = get_object_or_404(
        DelegateRegister.objects.select_related("booking_day__booking"),
        pk=pk
    )
    if not instr or not instructor_can_access_day(instr, reg.booking_day):
        messages.error(request, "You do not have permission to delete this delegate.")
        return redirect("instructor_bookings")
    
    guard = _closed_guard(request, reg.booking_day.booking)
    if guard:
        return guard

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    day_pk = reg.booking_day_id
    fallback_url = redirect("instructor_day_registers", pk=day_pk).url
    next_url = _safe_next_url(request, fallback_url)
    reg.delete()
    messages.success(request, "Delegate deleted.")
    return redirect(next_url)


@login_required
def instructor_day_registers_pdf(request, pk: int):
    """
    PDF: Delegate register (A4 landscape).
    Main row: Full name | Job title | Emp. ID | Health declaration
    If a delegate has notes, a second sub-row is drawn immediately underneath:
        [ Notes ] | <wrapped notes spanning remaining width>
    """
    # Lazy imports
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    from reportlab.lib import colors

    from .models import BookingDay, DelegateRegister

    # --- Fetch data ---
    day = get_object_or_404(
        BookingDay.objects.select_related(
            "booking",
            "booking__course_type",
            "booking__business",
            "booking__training_location",
            "booking__instructor",
        ),
        pk=pk,
    )
    booking = day.booking
    regs = list(
        DelegateRegister.objects
        .filter(booking_day=day)
        .order_by("name", "id")
    )

    # --- Page geometry ---
    page_w, page_h = landscape(A4)
    left, right = 12 * mm, page_w - 12 * mm
    top, bottom = page_h - 12 * mm, 12 * mm

    # Main table columns (DOB removed)
    col_name   = 70 * mm
    col_job    = 50 * mm
    col_emp    = 24 * mm
    col_health = (right - left) - (col_name + col_job + col_emp)

    # Notes sub-row widths
    notes_label_w   = 20 * mm
    notes_content_w = (right - left) - notes_label_w

    # Sizing
    row_min_h  = 12 * mm
    line_gap   = 4
    pad_x      = 2 * mm
    pad_y      = 3
    header_pad = 6 * mm  # extra padding below header line before table

    # (Adjust these to match your matrix/assessment PDFs as needed)
    FOOTER_LINE_1 = "Unicorn Training — Delegate Register"
    FOOTER_LINE_2 = "This form may contain personal data. Handle and store appropriately."

    # --- Wrapping helpers ---
    def wrap_lines(c, text, font, size, max_w):
        if not text:
            return []
        words = text.split()
        if not words:
            return []
        lines, cur = [], words[0]
        for w in words[1:]:
            t = f"{cur} {w}"
            if c.stringWidth(t, font, size) <= max_w:
                cur = t
            else:
                lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        return lines

    def cell_height(c, text, font="Helvetica", size=9, max_w=1000, min_h=row_min_h):
        lines = wrap_lines(c, text or "", font, size, max_w)
        return max(min_h, (len(lines) * (size + line_gap)) + pad_y * 2) if lines else min_h

    # --- Header/footer ---
    def draw_header_block(c):
        """
        Draws the header and returns the y position of the horizontal line
        under the header. The table should start below that line.
        """
        c.saveState()
        try:
            y = top
            c.setFont("Helvetica-Bold", 14)
            c.drawString(left, y, "Delegate Register")
            c.setFont("Helvetica", 9)

            y -= 7 * mm
            if booking and booking.course_type:
                c.drawString(left, y, f"Course: {booking.course_type.name}")
                y -= 5 * mm

            c.drawString(left, y, f"Date: {day.date.strftime('%d %b %Y')}")
            y -= 5 * mm

            if booking and booking.instructor:
                c.drawString(left, y, f"Instructor: {booking.instructor.name}")
                y -= 5 * mm

            if booking and booking.training_location:
                loc = booking.training_location
                addr = ", ".join(filter(None, [loc.name, loc.address_line, loc.town, loc.postcode]))
                c.drawString(left, y, f"Location: {addr}")
                y -= 5 * mm

            if booking and booking.course_reference:
                c.drawString(left, y, f"Reference: {booking.course_reference}")
                y -= 5 * mm

            c.setStrokeColor(colors.lightgrey)
            c.line(left, y, right, y)
            return y
        finally:
            c.restoreState()

    def draw_footer(c):
        """
        Draw a two-line footer and page number.
        Draw this *after* finishing page content and *before* showPage().
        """
        c.saveState()
        try:
            # fine line above footer
            c.setStrokeColor(colors.lightgrey)
            c.line(left, bottom + 12, right, bottom + 12)

            c.setFont("Helvetica", 8)
            c.setFillGray(0.35)
            c.drawString(left, bottom + 5 * mm, FOOTER_LINE_1)
            c.drawString(left, bottom + 3 * mm, FOOTER_LINE_2)
            c.drawRightString(right, bottom + 3 * mm, f"Page {c.getPageNumber()}")
            c.setFillGray(0)
            c.setStrokeColor(colors.black)
        finally:
            c.restoreState()

    def draw_header_row(c, y):
        c.saveState()
        try:
            c.setFont("Helvetica-Bold", 9)
            x = left
            headers = [
                ("Full name",          col_name),
                ("Job title",          col_job),
                ("Emp. ID",            col_emp),
                ("Health declaration", col_health),
            ]
            for title, w in headers:
                c.rect(x, y - row_min_h, w, row_min_h, stroke=1, fill=0)
                c.drawString(x + pad_x, y - row_min_h + pad_y, title)
                x += w
        finally:
            c.restoreState()

    # --- Draw one delegate (with optional notes sub-row) ---
    def draw_one_row(c, r, y_top):
        """
        Returns new y (next row top) or None if a page break is needed.
        """
        name  = (getattr(r, "name", "") or "").strip()
        job   = (getattr(r, "job_title", "") or "").strip()
        emp   = (getattr(r, "employee_id", "") or "").strip()
        notes = (getattr(r, "notes", "") or "").strip()

        c.setFont("Helvetica", 9)
        h_name = cell_height(c, name, max_w=col_name - 2 * pad_x)
        h_job  = cell_height(c, job,  max_w=col_job  - 2 * pad_x)
        base_h = max(row_min_h, h_name, h_job)

        notes_h = cell_height(c, notes, max_w=notes_content_w - 2 * pad_x, min_h=10 * mm) if notes else 0
        total_h = base_h + notes_h

        # Keep a buffer above the footer
        if y_top - total_h < (bottom + 24 * mm):
            return None

        # --- base row ---
        x = left

        # Name
        c.rect(x, y_top - base_h, col_name, base_h, stroke=1, fill=0)
        lines = wrap_lines(c, name, "Helvetica", 9, col_name - 2 * pad_x) or [""]
        yy = y_top - pad_y - 9
        for ln in lines:
            c.drawString(x + pad_x, yy, ln)
            yy -= (9 + line_gap)
            if yy < y_top - base_h + pad_y:
                break
        x += col_name

        # Job
        c.rect(x, y_top - base_h, col_job, base_h, stroke=1, fill=0)
        lines = wrap_lines(c, job, "Helvetica", 9, col_job - 2 * pad_x)
        if lines:
            yy = y_top - pad_y - 9
            for ln in lines:
                c.drawString(x + pad_x, yy, ln)
                yy -= (9 + line_gap)
                if yy < y_top - base_h + pad_y:
                    break
        x += col_job

        # Emp ID
        c.rect(x, y_top - base_h, col_emp, base_h, stroke=1, fill=0)
        c.drawString(x + pad_x, y_top - base_h + pad_y, emp or "—")
        x += col_emp

        # Health declaration: show stored value if present, otherwise a faint signature line
        c.rect(x, y_top - base_h, col_health, base_h, stroke=1, fill=0)
        if hasattr(r, "get_health_status_display"):
            health_txt = (r.get_health_status_display() or "").strip()
        else:
            health_txt = (getattr(r, "health_status", "") or "").strip()

        if health_txt:
            lines = wrap_lines(c, health_txt, "Helvetica", 9, col_health - 2 * pad_x)
            yy = y_top - pad_y - 9
            for ln in lines:
                c.drawString(x + pad_x, yy, ln)
                yy -= (9 + line_gap)
                if yy < y_top - base_h + pad_y:
                    break
        else:
            sig_y = y_top - base_h + base_h / 2
            c.setStrokeColor(colors.lightgrey)
            c.line(x + pad_x, sig_y, x + col_health - pad_x, sig_y)
            c.setStrokeColor(colors.black)

        y = y_top - base_h

        # --- optional notes sub-row ---
        if notes:
            # label
            c.rect(left, y - notes_h, notes_label_w, notes_h, stroke=1, fill=0)
            c.setFont("Helvetica-Bold", 9)
            c.drawString(left + pad_x, y - notes_h + pad_y, "Notes")
            c.setFont("Helvetica", 9)

            # content
            nx = left + notes_label_w
            c.rect(nx, y - notes_h, notes_content_w, notes_h, stroke=1, fill=0)
            lines = wrap_lines(c, notes, "Helvetica", 9, notes_content_w - 2 * pad_x)
            if lines:
                yy = y - pad_y - 9
                for ln in lines:
                    c.drawString(nx + pad_x, yy, ln)
                    yy -= (9 + line_gap)
                    if yy < y - notes_h + pad_y:
                        break
            y -= notes_h

        return y

    # --- Build PDF (force download) ---

    # Course code from CourseType.code, else derive a short token from name
    ct = getattr(day.booking, "course_type", None)
    name = (getattr(ct, "name", "") or "").strip()
    code = (getattr(ct, "code", "") or "").strip()
    course_code = code or (name.split()[0][:6].upper() if name else "COURSE")

    # Booking reference (fallback to booking PK)
    ref_raw = str(getattr(day.booking, "course_reference", "") or day.booking.pk)

    # If the reference already starts with the course code (e.g. "FAAW-7Z78LF"),
    # strip that leading code so we don’t duplicate it in the filename.
    ref_clean = ref_raw
    if course_code:
        up = course_code.upper()
        rup = ref_raw.upper()
        if rup.startswith(up):
            ref_clean = ref_raw[len(course_code):].lstrip("-_ ")

    # Day number within this booking (robust to related_name differences)
    ordered_ids = list(
        BookingDay.objects
        .filter(booking=day.booking)
        .order_by("date", "id")
        .values_list("id", flat=True)
    )
    try:
        day_number = ordered_ids.index(day.id) + 1
    except ValueError:
        day_number = 1

    # Build final filename
    filename = f"register_day_{day_number}_{course_code}"
    if ref_clean:
        filename += f"_{ref_clean}"
    filename += ".pdf"

    resp = HttpResponse(content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename=\"{filename}\"'

    c = canvas.Canvas(resp, pagesize=landscape(A4))
    c.setTitle(f"Delegate Register — {booking.course_reference if booking else day.pk}")

    def start_new_page():
        header_line_y = draw_header_block(c)
        y0 = header_line_y - header_pad
        draw_header_row(c, y0)
        return y0 - row_min_h - 2

    y = start_new_page()

    if not regs:
        c.setFont("Helvetica-Oblique", 10)
        c.drawString(left, y - 8 * mm, "No delegates recorded.")
        # footer + page
        draw_footer(c)
        c.showPage()
        c.save()
        return resp

    for r in regs:
        new_y = draw_one_row(c, r, y)
        if new_y is None:
            # finish this page
            draw_footer(c)
            c.showPage()
            y = start_new_page()
            new_y = draw_one_row(c, r, y) or (y - (row_min_h * 2))
        y = new_y - 1  # small gap

    # finish last page
    draw_footer(c)
    c.showPage()
    c.save()
    return resp

@login_required
@transaction.atomic
def instructor_assessment_save(request, pk):
    booking = get_object_or_404(Booking, pk=pk)
    instr = getattr(request.user, "personnel", None)
    if not (request.user.is_staff or (instr and instructor_can_access_booking(instr, booking))):
        return HttpResponseForbidden("You are not assigned to this booking.")

    if request.method != "POST":
        return redirect(f"{reverse('instructor_booking_detail', kwargs={'pk': booking.id})}#assessments-tab")

    # guard: only registers for this booking; only competencies for this course type
    # Deduplicate delegates by (name + DOB)
    delegates = _unique_delegates_for_booking(booking)

    selection_ctx = _assessment_selection_context(booking, delegates)
    comps = list(selection_ctx["required_competencies"])
    required_optional_count = selection_ctx["required_optional_count"]
    selected_optional_count = selection_ctx["selected_optional_count"]

    if required_optional_count and selected_optional_count < required_optional_count:
        messages.error(
            request,
            f"Please select all required optional modules ({selected_optional_count}/{required_optional_count}) before saving outcomes.",
        )
        return redirect(f"{reverse('instructor_booking_detail', kwargs={'pk': booking.id})}#assessments-tab")

    reg_map = {str(r.id): r for r in delegates}
    comp_ids = [str(c.id) for c in comps]

    # models
    from .models import CompetencyAssessment, AssessmentLevel, CourseOutcome
    valid_levels = {c[0] for c in AssessmentLevel.choices}
    valid_outcomes = {c[0] for c in CourseOutcome.choices}

    created = updated = 0

    # --- Save per-cell levels ---
    for key, val in request.POST.items():
        if not key.startswith("level_"):
            continue
        try:
            _, rid, cid = key.split("_", 2)
        except ValueError:
            continue
        if rid not in reg_map or cid not in comp_ids:
            continue

        level = val if val in valid_levels else "na"
        obj, was_created = CompetencyAssessment.objects.get_or_create(
            register_id=reg_map[rid].id,
            course_competency_id=cid,
            defaults={"level": level, "assessed_by_id": booking.instructor_id},
        )
        if was_created:
            created += 1
        else:
            if obj.level != level or obj.assessed_by_id != booking.instructor_id:
                obj.level = level
                obj.assessed_by_id = booking.instructor_id
                obj.save(update_fields=["level", "assessed_by", "assessed_at"])
                updated += 1

    # --- Save per-delegate outcome, enforcing PASS if all comps competent ---
    for rid, reg in reg_map.items():
        posted_outcome = request.POST.get(f"outcome_{rid}", "pending")
        if posted_outcome not in valid_outcomes:
            posted_outcome = "pending"

        all_competent = selected_optional_count >= required_optional_count
        for cid in comp_ids:
            if request.POST.get(f"level_{rid}_{cid}", "na") not in {"c", "e"}:
                all_competent = False
                break

        if all_competent and booking.status != "completed":
            from .services.assessment_exams import delegate_exams_all_passed
            all_competent = delegate_exams_all_passed(booking, reg)

        final_outcome = "pass" if all_competent else posted_outcome

        if getattr(reg, "outcome", None) != final_outcome:
            reg.outcome = final_outcome
            reg.save(update_fields=["outcome"])

    messages.success(request, f"Saved {created} new and {updated} updated assessment entr{'y' if (created+updated)==1 else 'ies'}.")
    return redirect(f"{reverse('instructor_booking_detail', kwargs={'pk': booking.id})}#assessments-tab")

def _chunked(iterable, size):
    for i in range(0, len(iterable), size):
        yield iterable[i:i + size]

@login_required
def instructor_assessment_pdf(request, pk):
    """
    Export the assessment matrix (landscape PDF) with:
    - Max 12 delegates per page
    - Repeated competencies on each page
    - Rotated delegate names
    - Zebra striping
    - Footer on every page
    """
    from datetime import date
    import io
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm

    instr = getattr(request.user, "personnel", None)
    booking = get_object_or_404(
        Booking.objects.select_related("course_type", "business", "instructor"),
        pk=pk
    )

    if not (request.user.is_staff or (instr and instructor_can_access_booking(instr, booking))):
        messages.error(request, "You do not have access to this booking.")
        return redirect("instructor_bookings")

    delegates = _unique_delegates_for_booking(booking)

    # ❌ Block if any pending
    if any((d.outcome or "pending") == "pending" for d in delegates):
        messages.error(request, "All delegates must have an outcome before exporting.")
        return redirect(f"{reverse('instructor_booking_detail', kwargs={'pk': booking.id})}#assessments-tab")

    competencies = _assessment_selection_context(booking, delegates)["required_competencies"]

    assess_map = {
        (rid, cid): lvl
        for rid, cid, lvl in CompetencyAssessment.objects
        .filter(register__in=delegates, course_competency__in=competencies)
        .values_list("register_id", "course_competency_id", "level")
    }

    from .models import BookingDay
    day_dates = list(
        BookingDay.objects.filter(booking=booking)
        .order_by("date")
        .values_list("date", flat=True)
    )

    def chunked(seq, size):
        for i in range(0, len(seq), size):
            yield seq[i:i + size]

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=landscape(A4))
    W, H = landscape(A4)

    left = 15 * mm
    right = W - 15 * mm
    top = H - 15 * mm
    bottom = 15 * mm

    FOOTER = "Unicorn Fire & Safety Solutions, Unicorn House, 6 Salendine, Shrewsbury, SY1 3XJ | info@unicornsafety.co.uk | 01743 360211"

    def footer():
        c.setFont("Helvetica", 8)
        c.setFillGray(0.4)
        c.drawString(left, bottom - 6 * mm, FOOTER)
        c.setFillGray(0)

    def draw_page_header():
        y = top
        c.setFont("Helvetica-Bold", 14)
        c.drawString(left, y, f"{booking.course_type.name} — Assessment Matrix")
        y -= 6 * mm
        c.setFont("Helvetica", 10)
        c.drawString(left, y, f"Business: {booking.business.name}")
        y -= 5 * mm
        c.drawString(left, y, f"Course reference: {booking.course_reference}")
        y -= 5 * mm
        c.drawString(left, y, f"Instructor: {booking.instructor.name}")
        y -= 5 * mm
        if day_dates:
            d1, d2 = day_dates[0], day_dates[-1]
            c.drawString(left, y, f"Course dates: {d1.strftime('%d %b %Y')} – {d2.strftime('%d %b %Y')}")
        return y - 7 * mm

    for page_no, page_delegates in enumerate(chunked(delegates, 12), start=1):

        if page_no > 1:
            footer()
            c.showPage()

        y = draw_page_header()

        comp_w = 85 * mm
        ncols = len(page_delegates)
        del_w = min(26 * mm, max(16 * mm, (right - left - comp_w) / ncols))
        header_h = 18 * mm
        row_h = 7 * mm

        # Header row
        c.setFillGray(0.95)
        c.rect(left, y - header_h, comp_w + del_w * ncols, header_h, stroke=0, fill=1)
        c.setFillGray(0)

        c.setFont("Helvetica-Bold", 9)
        c.rect(left, y - header_h, comp_w, header_h)
        c.drawCentredString(left + comp_w / 2, y - header_h + 4 * mm, "Competency")

        x = left + comp_w
        for d in page_delegates:
            c.rect(x, y - header_h, del_w, header_h)
            c.saveState()
            c.translate(x + del_w / 2, y - header_h / 2)
            c.rotate(90)
            c.setFont("Helvetica-Bold", 7)
            parts = d.name.split()
            c.drawCentredString(0, -3 * mm, parts[0][:18])
            if len(parts) > 1:
                c.drawCentredString(0, 3 * mm, " ".join(parts[1:])[:18])
            c.restoreState()
            x += del_w

        y -= header_h

        for idx, comp in enumerate(competencies):
            if y - row_h < bottom + 20 * mm:
                footer()
                c.showPage()
                y = draw_page_header()

            if idx % 2 == 0:
                c.setFillGray(0.86)
                c.rect(left, y - row_h, comp_w + del_w * ncols, row_h, stroke=0, fill=1)
                c.setFillGray(0)

            c.setFont("Helvetica", 9)
            c.rect(left, y - row_h, comp_w, row_h)
            c.drawString(left + 2 * mm, y - row_h + 2 * mm, comp.name[:80])

            x = left + comp_w
            for d in page_delegates:
                c.rect(x, y - row_h, del_w, row_h)
                lvl = assess_map.get((d.id, comp.id))
                c.setFont("Helvetica-Bold", 10 if lvl in ("c", "e") else 9)
                c.drawCentredString(x + del_w / 2, y - row_h + 2 * mm, "✔" if lvl in ("c", "e") else "—")
                x += del_w

            y -= row_h

        if booking.status != "completed":
            from .services.assessment_exams import build_assessment_exam_matrix
            _, exam_matrix_rows = build_assessment_exam_matrix(booking, page_delegates)
            for row in exam_matrix_rows:
                if y - row_h < bottom + 20 * mm:
                    footer()
                    c.showPage()
                    y = draw_page_header()

                c.setFont("Helvetica", 9)
                c.rect(left, y - row_h, comp_w, row_h)
                c.drawString(left + 2 * mm, y - row_h + 2 * mm, f"Exam {row['exam'].sequence}"[:80])

                x = left + comp_w
                for cell in row["cells"]:
                    c.rect(x, y - row_h, del_w, row_h)
                    c.setFont("Helvetica-Bold", 9)
                    c.drawCentredString(x + del_w / 2, y - row_h + 2 * mm, cell["label"][:12])
                    x += del_w
                y -= row_h

        # Outcome row
        c.setFillGray(0.94)
        c.rect(left, y - 9 * mm, comp_w + del_w * ncols, 9 * mm, stroke=0, fill=1)
        c.setFillGray(0)

        c.setFont("Helvetica-Bold", 9)
        c.rect(left, y - 9 * mm, comp_w, 9 * mm)
        c.drawString(left + 2 * mm, y - 7 * mm, "Outcome")

        x = left + comp_w
        for d in page_delegates:
            c.rect(x, y - 9 * mm, del_w, 9 * mm)
            status = (d.outcome or "").upper()
            c.drawCentredString(x + del_w / 2, y - 7 * mm, status)
            x += del_w

    footer()
    c.save()
    buf.seek(0)

    return FileResponse(
        buf,
        as_attachment=True,
        filename=f"assessment-matrix-{booking.course_reference}.pdf",
    )

# views_instructor.py
from django.db.models import Avg, Count
# ...existing imports...
from .models import FeedbackResponse

def instructor_feedback_tab(request, booking_id):
    """
    Feedback tab for a booking:
    - lists individual forms (date, instructor, overall)
    - shows quick summary stats
    """
    booking = get_object_or_404(Booking, pk=booking_id)

    # Full date range for this booking (min..max across all days)
    day_qs = booking.days.order_by("date").values_list("date", flat=True)
    if day_qs:
        start_date = day_qs.first()
        end_date   = day_qs.last()
        date_filter = {"date__range": (start_date, end_date)}
    else:
        # fall back to the booking start date if no days are present
        start_date = end_date = booking.course_date
        date_filter = {"date": booking.course_date}

    # Pull responses for this booking's course_type and date range
    qs = (
        FeedbackResponse.objects
        .filter(course_type=booking.course_type, **date_filter)
        .select_related("instructor")
        .order_by("-date", "-created_at")
    )

    summary = qs.aggregate(n=Count("id"), avg_overall=Avg("overall_rating"))

    context = {
        "title": "Feedback",
        "booking": booking,
        "responses": qs,
        "summary": summary,
    }
    return render(request, "instructor/booking_feedback.html", context)


@login_required
def instructor_feedback_poll(request, booking_id):
    """
    Lightweight GET endpoint used by client-side code to refresh the feedback
    rows for a given booking without a full page reload.
    Returns JSON: { ok: true, html: "<tr>...</tr>..." }
    """
    if request.method != "GET":
        return JsonResponse({"ok": False, "error": "GET only"}, status=405)

    booking = get_object_or_404(Booking, pk=booking_id)

    # Ensure instructor can only see their own booking feedback
    instr = getattr(request.user, "personnel", None)
    if not instr or not instructor_can_access_booking(instr, booking):
        return JsonResponse({"ok": False, "error": "Forbidden"}, status=403)

    # Full date range for this booking (min..max across all days)
    day_qs = booking.days.order_by("date").values_list("date", flat=True)
    if day_qs:
        start_date = day_qs.first()
        end_date   = day_qs.last()
        date_filter = {"date__range": (start_date, end_date)}
    else:
        start_date = end_date = booking.course_date
        date_filter = {"date": booking.course_date}

    fb_qs = (
        FeedbackResponse.objects
        .filter(course_type=booking.course_type, **date_filter)
        .select_related("instructor")
        .order_by("-date", "-created_at")
    )
    rows = fb_qs.count()
    ref = getattr(booking, "course_reference", None) or booking.pk
    print(f"*** [Feedback] poll — {ref} — {rows} response(s) ***", flush=True)

    html = render_to_string("instructor/_booking_feedback_rows.html", {"fb_qs": fb_qs}, request=request)
    return JsonResponse({"ok": True, "booking_id": str(booking_id), "html": html, "rows": rows})


def instructor_feedback_view(request, pk):
    """
    Read-only view of a single feedback response.
    """
    fb = get_object_or_404(FeedbackResponse.objects.select_related("course_type", "instructor"), pk=pk)
    return render(request, "instructor/feedback_detail.html", {"fb": fb})

@login_required
def instructor_feedback_all_pdf(request, pk):
    # simple redirect to a public/export endpoint if you already have one
    return redirect("public_feedback_pdf", pk=pk)  # or build your own combined PDF

@login_required
def instructor_feedback_summary_pdf(request, pk):
    # generate a 1-page summary; can reuse ReportLab similar to your matrix/export
    # (left as a stub so we don’t collide with existing code)
    return HttpResponseNotAllowed(["GET"])

def _feedback_queryset_for_booking(booking):
    day_qs = booking.days.order_by("date").values_list("date", flat=True)
    if day_qs:
        start_date, end_date = day_qs.first(), day_qs.last()
        date_filter = {"date__range": (start_date, end_date)}
    else:
        start_date = end_date = booking.course_date
        date_filter = {"date": booking.course_date}

    return (
        FeedbackResponse.objects
        .filter(course_type=booking.course_type, **date_filter)
        .select_related("instructor")
        .order_by("date", "created_at")
    )

@login_required
def instructor_course_summary_pdf(request, pk):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])

    booking = get_object_or_404(Booking, pk=pk)

    # All registers for any day of this booking
    regs = (
        DelegateRegister.objects
        .filter(booking_day__booking=booking)
        .only("name", "outcome")
        .order_by("name")
    )

    by_outcome = defaultdict(list)
    for r in regs:
        by_outcome[r.outcome].append(r.name)

    # Figure course end date from Booking.days if present
    day_qs = booking.days.order_by("date").values_list("date", flat=True)
    end_date = day_qs.last() if day_qs else booking.course_date

    # ---------- DEV: lightweight ReportLab PDF, no WeasyPrint ----------
    if settings.DEBUG:
        buf = BytesIO()
        c = canvas.Canvas(buf, pagesize=A4)

        y = 800
        c.setFont("Helvetica-Bold", 14)
        c.drawString(72, y, "Course summary")
        y -= 24

        c.setFont("Helvetica", 11)
        c.drawString(72, y, f"Course: {booking.course_type.name}")
        y -= 16
        c.drawString(72, y, f"Reference: {booking.course_reference or '-'}")
        y -= 16
        c.drawString(72, y, f"Location: {getattr(booking.training_location, 'name', '-')}")
        y -= 16
        c.drawString(72, y, f"Instructor: {getattr(booking.instructor, 'name', '-')}")
        y -= 16
        c.drawString(72, y, f"End date: {end_date:%d/%m/%Y}")
        y -= 24

        def _write_block(title, names):
            nonlocal y
            if not names:
                return
            c.setFont("Helvetica-Bold", 11)
            c.drawString(72, y, title)
            y -= 16
            c.setFont("Helvetica", 10)
            for n in sorted(names):
                c.drawString(90, y, f"- {n}")
                y -= 14
                if y < 72:
                    c.showPage()
                    y = 800

        _write_block("Passed",   by_outcome.get(CourseOutcome.PASS, []))
        _write_block("Failed",   by_outcome.get(CourseOutcome.FAIL, []))
        _write_block("Did not finish", by_outcome.get(CourseOutcome.DNF, []))

        c.showPage()
        c.save()
        buf.seek(0)

        filename = f"course-summary-{booking.course_reference or booking.pk}.pdf"
        return FileResponse(buf, as_attachment=True, filename=filename)

    # ---------- PROD: full HTML → WeasyPrint pipeline ----------
    context = {
        "booking": booking,
        "end_date": end_date,
        "passed": by_outcome.get(CourseOutcome.PASS, []),
        "failed": by_outcome.get(CourseOutcome.FAIL, []),
        "dnf":    by_outcome.get(CourseOutcome.DNF,  []),
        "filename": f"course-summary-{booking.course_reference}.pdf",
    }

    pdf_bytes, filename, mimetype = render_invoice_pdf_from_html(
        "training/course_summary.html", context
    )
    resp = HttpResponse(pdf_bytes, content_type=mimetype)
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp

@login_required
def instructor_course_summary_by_ref_pdf(request, ref):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])

    booking = get_object_or_404(
        Booking.objects.select_related("course_type", "business", "instructor", "training_location"),
        course_reference__iexact=ref,
    )

    instr = getattr(request.user, "personnel", None)
    if not (request.user.is_staff or (instr and instructor_can_access_booking(instr, booking))):
        return HttpResponseForbidden("You do not have access to this booking.")

    # IMPORTANT: do not render here. Call the UUID view so the DEBUG→HTML logic is used.
    return instructor_course_summary_pdf(request, booking.pk)

@login_required
def download_booking_ics(request, booking_id):
    import datetime
    from django.utils.timezone import now

    booking = get_object_or_404(Booking, pk=booking_id)

    # Collect BookingDay entries
    days = booking.days.order_by("date")

    if not days:
        # Fallback: single-day course (rare, but safe)
        class FakeDay:
            date = booking.course_date
            start_time = booking.start_time or datetime.time(9, 0)
            end_time   = datetime.time(17, 0)

        days = [FakeDay()]

    # -----------------------------------------
    # Build unified event description (same as view)
    # -----------------------------------------
    desc_lines = []

    # Notes
    if booking.booking_notes:
        desc_lines.append(f"Notes: {booking.booking_notes}")

    # Contact details
    if booking.contact_name or booking.telephone or booking.email:
        desc_lines.append("Contact details:")
        if booking.contact_name:
            desc_lines.append(f" - Name: {booking.contact_name}")
        if booking.telephone:
            desc_lines.append(f" - Phone: {booking.telephone}")
        if booking.email:
            desc_lines.append(f" - Email: {booking.email}")

    # Instructor fee info
    fee_lines = []
    if booking.instructor_fee:
        fee_lines.append(f"Instructor fee: £{booking.instructor_fee}")

    if booking.allow_mileage_claim:
        if booking.mileage_fee:
            fee_lines.append(f"Mileage allowance: £{booking.mileage_fee}")
        else:
            fee_lines.append("Mileage allowed")

    if booking.allow_accommodation:
        fee_lines.append("Accommodation allowed")

    if fee_lines:
        desc_lines.append("Instructor claim info:")
        for ln in fee_lines:
            desc_lines.append(f" - {ln}")

    event_description = "\\n".join(desc_lines) or "Training course"

    # -----------------------------------------
    # Build ICS file
    # -----------------------------------------
    ics_lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Unicorn Training//Booking Calendar//EN"
    ]

    for d in days:
        start_dt = datetime.datetime.combine(
            d.date,
            d.start_time or datetime.time(9, 0)
        )

        end_dt = datetime.datetime.combine(
            d.date,
            d.end_time or datetime.time(17, 0)
        )

        location = (
            f"{booking.training_location.address_line}, "
            f"{booking.training_location.town}, "
            f"{booking.training_location.postcode}"
        )

        ics_lines += [
            "BEGIN:VEVENT",
            f"UID:{booking.pk}-{d.date}",
            f"DTSTAMP:{now().strftime('%Y%m%dT%H%M%SZ')}",
            f"DTSTART:{start_dt.strftime('%Y%m%dT%H%M%S')}",
            f"DTEND:{end_dt.strftime('%Y%m%dT%H%M%S')}",
            f"SUMMARY:{booking.course_type.name}",
            f"DESCRIPTION:{event_description}",
            f"LOCATION:{location}",
            "END:VEVENT"
        ]

    ics_lines.append("END:VCALENDAR")

    ics_data = "\r\n".join(ics_lines)

    response = HttpResponse(ics_data, content_type="text/calendar; charset=utf-8")
    # inline so mobile browsers hand off to a calendar app instead of only downloading
    response["Content-Disposition"] = (
        f'inline; filename="{booking.course_reference}.ics"'
    )
    return response

from django.core.exceptions import PermissionDenied
from django.contrib.auth.decorators import login_required
@login_required
def invoice_preview(request, pk):
    booking = get_object_or_404(
        Booking.objects.select_related("course_type", "business", "instructor", "training_location"),
        pk=pk,
    )

    instr = getattr(request.user, "personnel", None)

    # Admins (superuser or in "admin" group) are allowed regardless of instructor
    user_is_admin = (
        getattr(request.user, "is_superuser", False)
        or request.user.groups.filter(name__iexact="admin").exists()
    )

    if not user_is_admin:
        # Non-admins must be the booking instructor
        user_is_admin = (
            request.user.is_staff
            or request.user.is_superuser
            or request.user.groups.filter(name__iexact="admin").exists()
        )

        if not user_is_admin:
            if not instr or not instructor_can_access_booking(instr, booking):
                raise PermissionDenied("Not allowed.")


    # ---- Build context for the HTML invoice template ----
    inv = getattr(booking, "invoice", None) or Invoice.objects.create(
        booking=booking, instructor=booking.instructor, invoice_date=now().date()
    )

    addr_parts = [
        getattr(booking.instructor, "address_line", ""),
        getattr(booking.instructor, "town", ""),
        getattr(booking.instructor, "postcode", ""),
    ]
    from_address = "\n".join([p for p in addr_parts if p.strip()])

    base_fee = Decimal(str(booking.instructor_fee or 0))
    items = [{"description": f"{booking.course_type.name} – {booking.business.name} – {booking.course_date:%d/%m/%Y}",
              "amount": f"{base_fee:.2f}"}]
    for it in inv.items.all().order_by("id"):
        items.append({"description": it.description or "", "amount": f"{(it.amount or 0):.2f}"})

    ctx = {
        "instructor_name": booking.instructor.name if booking.instructor else "",
        "from_address": from_address,
        "invoice_date": (inv.invoice_date or now().date()).strftime("%d/%m/%Y"),
        "course_ref": booking.course_reference or "",
        "instructor_ref": inv.instructor_ref or "",
        "items": items,
        "invoice_total": f"{(base_fee + sum((x.amount or 0) for x in inv.items.all())):.2f}",
        "account_name":   inv.account_name   or getattr(booking.instructor, "name_on_account", "") or "",
        "sort_code":      inv.sort_code      or getattr(booking.instructor, "bank_sort_code", "") or "",
        "account_number": inv.account_number or getattr(booking.instructor, "bank_account_number", "") or "",
    }

    html = render_to_string("invoicing/invoice.html", ctx)  # your HTML template

    # ---- Run wkhtmltopdf ----
    wk = getattr(settings, "WKHTMLTOPDF_CMD", None)
    if not wk or not os.path.exists(wk):
        return HttpResponseServerError("wkhtmltopdf not configured.")

    # Use a temp HTML file so asset paths resolve correctly
    with tempfile.TemporaryDirectory() as tmp:
        html_path = os.path.join(tmp, "invoice.html")
        pdf_path  = os.path.join(tmp, "invoice.pdf")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)

        # Common flags: quiet, enable-local-file-access for Windows
        cmd = [wk, "--quiet", "--enable-local-file-access", html_path, pdf_path]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0 or not os.path.exists(pdf_path):
            # Surface wkhtmltopdf errors to help debugging
            return HttpResponseServerError(
                "wkhtmltopdf failed:\n" + (proc.stderr.decode("utf-8", errors="ignore") or "Unknown error")
            )

        with open(pdf_path, "rb") as f:
            pdf = f.read()

    response = HttpResponse(pdf, content_type="application/pdf")
    filename = f"invoice-{booking.course_reference or booking.pk}.pdf"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _pdf_header_footer(c, booking, title):
    w, h = A4
    c.setTitle(title)

    # Header
    c.setFont("Helvetica-Bold", 14)
    c.drawString(20*mm, h - 20*mm, f"{booking.course_type.name} — {title}")
    c.setFont("Helvetica", 10)
    c.drawString(20*mm, h - 26*mm, f"Course reference: {booking.course_reference}")
    c.drawString(20*mm, h - 31*mm, f"Business: {booking.business.name}")
    # Dates line
    day_qs = booking.days.order_by("date").values_list("date", flat=True)
    if day_qs:
        ds = day_qs.first().strftime("%d %b %Y")
        de = day_qs.last().strftime("%d %b %Y")
        date_str = ds if ds == de else f"{ds} – {de}"
    else:
        date_str = booking.course_date.strftime("%d %b %Y")
    c.drawString(20*mm, h - 36*mm, f"Course dates: {date_str}")

    # Footer
    c.setFont("Helvetica", 9)
    c.setFillColor(colors.grey)
    c.drawString(20*mm, 10*mm, "Unicorn Fire & Safety Solutions, Unicorn House, 6 Salendine, Shrewsbury, SY1 3XJ · info@unicornsafety.co.uk · 01743 360211")
    c.setFillColor(colors.black)

@login_required
def instructor_feedback_pdf_all(request, booking_id):
    """
    Export all individual feedback forms for THIS booking (portrait).
    Filters by:
      - booking.course_type
      - dates within the booking's days (or booking.course_date)
      - instructor == booking.instructor
    """
    instr = getattr(request.user, "personnel", None)
    booking = get_object_or_404(
        Booking.objects.select_related("course_type", "business", "instructor"),
        pk=booking_id,
    )
    # Allow: assigned instructor OR any staff user (admin)
    if not (request.user.is_staff or (instr and instructor_can_access_booking(instr, booking))):
        return HttpResponseForbidden("You do not have access to this booking.")


    # Date window = all booking days (fallback to booking.course_date)
    day_dates = list(
        BookingDay.objects.filter(booking=booking)
        .order_by("date").values_list("date", flat=True)
    )
    if day_dates:
        date_min, date_max = min(day_dates), max(day_dates)
    else:
        date_min = date_max = booking.course_date

    # *** KEY FILTER: restrict to this booking's instructor ***
    responses = list(
        FeedbackResponse.objects.filter(
            course_type=booking.course_type,
            instructor_id=booking.instructor_id,
            date__gte=date_min,
            date__lte=date_max,
        ).select_related("instructor")
         .order_by("created_at", "id")
    )

    # ---------- PDF ----------
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)   # portrait
    W, H = A4

    M_L = 18*mm; M_R = 18*mm; M_T = 16*mm; M_B = 16*mm
    left, right, top, bottom = M_L, W - M_R, H - M_T, M_B
    content_w = right - left

    from reportlab.pdfbase import pdfmetrics
    def sw(txt, font="Helvetica", size=10):
        return pdfmetrics.stringWidth(str(txt), font, size)

    def wrap_lines(text, font="Helvetica", size=9.5, max_width=120*mm):
        text = (text or "").replace("\r", " ").strip()
        if not text:
            return []
        words, lines, cur = text.split(), [], ""
        for w in words:
            probe = (cur + " " + w).strip()
            if sw(probe, font, size) <= max_width:
                cur = probe
            else:
                if cur: lines.append(cur)
                cur = w
        if cur: lines.append(cur)
        return lines

    def header():
        y = top
        c.setFont("Helvetica-Bold", 14)
        c.drawString(left, y, f"{booking.course_type.name} — Feedback — All Forms")
        y -= 7*mm
        c.setFont("Helvetica", 10)
        meta = [
            ("Course reference", booking.course_reference or "—"),
            ("Business", booking.business.name),
            ("Course dates", _format_course_dates_for_booking(booking)),
            ("Instructor", booking.instructor.name if booking.instructor else "—"),
        ]
        for k, v in meta:
            c.setFont("Helvetica-Bold", 10);  c.drawString(left, y, f"{k}:")
            c.setFont("Helvetica", 10);       c.drawString(left+35*mm, y, str(v))
            y -= 4.8*mm
        c.setStrokeColorRGB(.8,.8,.85)
        c.line(left, y-1.5*mm, right, y-1.5*mm)
        c.setStrokeColor(colors.black)
        return y - 5*mm

    def footer():
        c.setFont("Helvetica", 8.5)
        c.setFillColorRGB(0.35, 0.35, 0.4)
        c.drawCentredString(
            W/2, 8*mm,
            "Unicorn Fire & Safety Solutions, Unicorn House, 6 Salendine, Shrewsbury, SY1 3XJ  ·  info@unicornsafety.co.uk  ·  01743 360211"
        )
        c.setFillColor(colors.black)

    def draw_card(y, idx, r):
        pad = 5*mm
        col_gap = 6*mm
        col1_x = left + pad + 1*mm
        col2_x = left + pad + 70*mm
        usable_w = content_w - 2*pad

        short_rows = 5
        line_h = 4.9*mm
        comment_lines = wrap_lines(r.comments, "Helvetica-Oblique", 9.5, max_width=usable_w-6*mm)
        comment_h = (len(comment_lines) or 1) * line_h
        cb_needed = 1 if getattr(r, "wants_callback", False) else 0
        cb_h = cb_needed * line_h
        title_h = 7*mm

        need = title_h + 2*mm + short_rows*line_h + 2*mm + line_h*2 + 2*mm + comment_h + cb_h + 4*mm
        if y - need < bottom + 20*mm:
            c.showPage()
            y = header()

        c.setFillColorRGB(0.97, 0.98, 1.0)
        c.setStrokeColorRGB(0.85, 0.88, 0.95)
        c.roundRect(left, y-need, content_w, need, 3*mm, fill=1, stroke=1)
        c.setFillColor(colors.black); c.setStrokeColor(colors.black)

        cy = y - pad
        c.setFont("Helvetica-Bold", 11)
        c.drawString(left+pad, cy-4*mm, f"{idx}. {r.date.strftime('%d %b %Y')}")
        c.setFont("Helvetica", 10)
        c.drawString(left+pad+40*mm, cy-4*mm, f"Instructor: {r.instructor.name if r.instructor else '—'}")
        c.setFont("Helvetica-Bold", 10)
        ov = getattr(r, "overall_rating", None)
        c.drawRightString(right-pad, cy-4*mm, f"Overall: {ov}/5" if ov else "Overall: —")
        cy -= (title_h + 2*mm)

        c.setFont("Helvetica", 9.5)
        col1 = [
            ("Prior knowledge", r.prior_knowledge),
            ("Post", r.post_knowledge),
            ("Structure", r.q_structure),
            ("Pace", r.q_pace),
            ("Materials", r.q_materials_quality),
        ]
        col2 = [
            ("Purpose clear", r.q_purpose_clear),
            ("Met needs", r.q_personal_needs),
            ("Content clear", r.q_content_clear),
            ("Instructor knowledge", r.q_instructor_knowledge),
            ("Books/handouts", r.q_books_quality),
        ]
        for i in range(short_rows):
            c.drawString(col1_x, cy, f"{col1[i][0]}: {col1[i][1] if col1[i][1] else '—'}")
            c.drawString(col2_x, cy, f"{col2[i][0]}: {col2[i][1] if col2[i][1] else '—'}")
            cy -= line_h

        c.drawString(col1_x, cy, f"Venue: {r.q_venue_suitable if r.q_venue_suitable else '—'}")
        c.drawString(col2_x, cy, f"Work benefit: {r.q_benefit_at_work if r.q_benefit_at_work else '—'}")
        cy -= line_h
        c.drawString(col1_x, cy, f"Outside-work benefit: {r.q_benefit_outside if r.q_benefit_outside else '—'}")
        cy -= (line_h + 1*mm)

        c.setFont("Helvetica-Bold", 10)
        c.drawString(col1_x, cy, "Comments:")
        cy -= line_h
        c.setFont("Helvetica-Oblique", 9.5)
        if comment_lines:
            for ln in comment_lines:
                c.drawString(col1_x+6*mm, cy, ln)
                cy -= line_h
        else:
            c.drawString(col1_x+6*mm, cy, "—")
            cy -= line_h

        if cb_needed:
            c.setFont("Helvetica-Bold", 9.5)
            c.setFillColorRGB(0.8, 0.1, 0.1)
            c.drawString(col1_x, cy, "Requested a callback")
            c.setFillColor(colors.black)
            c.setFont("Helvetica", 9.5)
            c.drawString(col1_x + 42*mm, cy, f"Name: {(r.contact_name or '—').strip()}")
            c.drawString(col1_x + 95*mm, cy, f"Email: {(r.contact_email or '—').strip()}")
            c.drawString(col1_x + 150*mm, cy, f"Tel: {(r.contact_phone or '—').strip()}")
            cy -= line_h

        return (y - need) - 6*mm

    y = header()
    if not responses:
        c.setFont("Helvetica-Oblique", 10)
        c.drawString(left, y, "No feedback responses yet for this booking.")
    else:
        for i, r in enumerate(responses, start=1):
            y = draw_card(y, i, r)

    footer(); c.showPage(); footer(); c.save()
    buf.seek(0)
    filename = f"feedback_all_{booking.course_reference or booking.pk}.pdf"
    return FileResponse(buf, as_attachment=True, filename=filename)



# --- ADD these two helpers (near your other helpers) ---
def _format_course_dates_for_booking(booking):
    """Return a nice dates string across all BookingDay rows."""
    days = list(BookingDay.objects.filter(booking=booking).order_by("date").values_list("date", flat=True))
    if not days:
        return booking.course_date.strftime("%d %b %Y")
    if len(days) == 1:
        return days[0].strftime("%d %b %Y")
    return f"{days[0].strftime('%d %b %Y')} – {days[-1].strftime('%d %b %Y')}"

def _draw_zebra_row(c, x, y, w, h, idx, light=0.96, dark=0.90):
    """Subtle zebra background behind a row rectangle at (x,y)."""
    col = colors.Color(dark, dark, dark) if (idx % 2) else colors.Color(light, light, light)
    c.setFillColor(col)
    c.setStrokeColor(col)
    c.rect(x, y, w, h, stroke=0, fill=1)
    c.setFillColor(colors.black)
    c.setStrokeColor(colors.black)

# --- REPLACE your current instructor_feedback_pdf_summary with this one ---
@login_required
def instructor_feedback_pdf_summary(request, booking_id):
    """
    Pretty PDF summary (landscape) for THIS booking.
    Filters by course_type, booking day range, and booking.instructor.
    """
    instr = getattr(request.user, "personnel", None)
    booking = get_object_or_404(
        Booking.objects.select_related("course_type", "business", "instructor"),
        pk=booking_id,
    )
    # Allow: assigned instructor OR any staff user (admin)
    if not (request.user.is_staff or (instr and instructor_can_access_booking(instr, booking))):
        return HttpResponseForbidden("You do not have access to this booking.")


    # Date window
    day_dates = list(
        BookingDay.objects.filter(booking=booking).order_by("date").values_list("date", flat=True)
    )
    if day_dates:
        date_min, date_max = min(day_dates), max(day_dates)
    else:
        date_min = date_max = booking.course_date

    # *** KEY FILTER: restrict to this booking's instructor ***
    responses = list(
        FeedbackResponse.objects.filter(
            course_type=booking.course_type,
            instructor_id=booking.instructor_id,
            date__gte=date_min,
            date__lte=date_max,
        ).order_by("created_at", "id")
    )

    # Aggregates
    count = len(responses)
    vals = [int(r.overall_rating) for r in responses if getattr(r, "overall_rating", None)]
    overall_avg = round(mean(vals), 2) if vals else None
    dist = {i: 0 for i in range(1, 6)}
    for v in vals:
        if 1 <= v <= 5:
            dist[v] += 1

    score_fields = [
        ("Prior knowledge", "prior_knowledge"),
        ("Post knowledge", "post_knowledge"),
        ("Purpose & objectives clear", "q_purpose_clear"),
        ("Met my training needs", "q_personal_needs"),
        ("Exercises were useful", "q_exercises_useful"),
        ("Structure / logical", "q_structure"),
        ("Pace suitable", "q_pace"),
        ("Content & language clear", "q_content_clear"),
        ("Instructor knowledgeable", "q_instructor_knowledge"),
        ("Materials / equipment quality", "q_materials_quality"),
        ("Books / handouts quality", "q_books_quality"),
        ("Venue suitable & comfortable", "q_venue_suitable"),
        ("Beneficial at work", "q_benefit_at_work"),
        ("Beneficial outside work", "q_benefit_outside"),
    ]
    def avg_of(field):
        nums = []
        for r in responses:
            v = getattr(r, field, None)
            try:
                v = int(v)
            except Exception:
                v = None
            if v and 1 <= v <= 5:
                nums.append(v)
        return round(mean(nums), 2) if nums else None
    averages = [(label, avg_of(attr)) for (label, attr) in score_fields]

    comments = [r.comments.strip() for r in responses if (r.comments or "").strip()]
    callbacks = [
        {
            "name": (r.contact_name or "").strip() or "—",
            "email": (r.contact_email or "").strip(),
            "phone": (r.contact_phone or "").strip(),
            "submitted": r.created_at,
        }
        for r in responses if getattr(r, "wants_callback", False)
    ]

    # ---------- PDF ----------
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=landscape(A4))
    W, H = landscape(A4)

    left = 18*mm; right = W - 18*mm; top = H - 16*mm; bottom = 14*mm
    width = right - left

    def draw_header():
        y = top
        c.setFont("Helvetica-Bold", 14)
        c.drawString(left, y, "Course Feedback — Summary")
        y -= 7*mm
        c.setFont("Helvetica", 10)
        meta = [
            ("Business", booking.business.name),
            ("Course", booking.course_type.name),
            ("Reference", booking.course_reference or "—"),
            ("Dates", _format_course_dates_for_booking(booking)),
            ("Instructor", booking.instructor.name if booking.instructor else "—"),
            ("Responses", str(count)),
        ]
        col_gap = 8*mm
        col_w = (width - col_gap) / 2
        x1, x2 = left, left + col_w + col_gap
        y1 = y2 = y
        for i, (k, v) in enumerate(meta[:3]):
            c.setFont("Helvetica-Bold", 10); c.drawString(x1, y1, f"{k}:")
            c.setFont("Helvetica", 10);      c.drawString(x1+28*mm, y1, str(v)); y1 -= 5.2*mm
        for i, (k, v) in enumerate(meta[3:]):
            c.setFont("Helvetica-Bold", 10); c.drawString(x2, y2, f"{k}:")
            c.setFont("Helvetica", 10);      c.drawString(x2+28*mm, y2, str(v)); y2 -= 5.2*mm
        yy = min(y1, y2)
        c.setStrokeColorRGB(.8,.8,.85); c.line(left, yy-2*mm, right, yy-2*mm); c.setStrokeColor(colors.black)
        return yy - 6*mm

    def draw_overall(y):
        c.setFont("Helvetica-Bold", 12); c.drawString(left, y, "Overall rating")
        if overall_avg is None:
            c.setFont("Helvetica", 10); c.drawString(left + 45*mm, y, "No responses")
            return y - 10*mm
        # avg badge
        badge_h, badge_w = 12*mm, 28*mm
        x_badge, y_badge = left + 45*mm, y + 3*mm - badge_h
        c.setFillColorRGB(0.89, 0.96, 0.90); c.setStrokeColorRGB(0.75, 0.90, 0.80)
        c.roundRect(x_badge, y_badge, badge_w, badge_h, 2.5*mm, stroke=1, fill=1)
        c.setFillColor(colors.black); c.setFont("Helvetica-Bold", 16)
        c.drawCentredString(x_badge + badge_w/2, y_badge + 3.2*mm, f"{overall_avg:.2f}")
        # distribution
        c.setFont("Helvetica", 10); x = x_badge + badge_w + 10*mm; c.drawString(x, y, "Distribution:")
        x += 24*mm
        for i in range(1, 6):
            c.drawString(x, y, f"{i}: {dist.get(i,0)}"); x += 18*mm
        return y - 10*mm

    def zebra(y, idx, h):  # subtle background
        col = 0.96 if idx % 2 else 0.90
        c.setFillColorRGB(col, col, col); c.rect(left, y-h+2, width, h-2, stroke=0, fill=1); c.setFillColor(colors.black)

    def draw_averages(y):
        c.setFont("Helvetica-Bold", 12); c.drawString(left, y, "Averages by question"); y -= 5*mm
        row_h = 7*mm
        for i, (label, avgv) in enumerate(averages, start=1):
            if y - row_h < bottom + 40*mm:
                c.showPage(); y = draw_header()
                c.setFont("Helvetica-Bold", 12); c.drawString(left, y, "Averages by question (cont.)"); y -= 7*mm
            zebra(y, i, row_h)
            c.setFont("Helvetica", 9.5); c.drawString(left + 1.5*mm, y - 4.8*mm, label)
            c.setFont("Helvetica-Bold", 10)
            c.drawRightString(right - 1.5*mm, y - 4.8*mm, ("—" if avgv is None else f"{avgv:.2f}"))
            y -= row_h
        return y - 6*mm

    def draw_comments(y):
        if not comments: return y
        c.setFont("Helvetica-Bold", 12); c.drawString(left, y, "Comments"); y -= 6*mm
        row_h = 12*mm
        for i, txt in enumerate(comments, start=1):
            s = " ".join(txt.replace("\r", "").splitlines()).strip()
            chunks = []
            while len(s) > 110:
                cut = s.rfind(" ", 0, 110); cut = 110 if cut <= 0 else cut
                chunks.append(s[:cut].strip()); s = s[cut:].strip()
            if s: chunks.append(s)
            need = max(row_h, 5*mm + len(chunks)*5.2*mm)
            if y - need < bottom + 16*mm:
                c.showPage(); y = draw_header()
                c.setFont("Helvetica-Bold", 12); c.drawString(left, y, "Comments (cont.)"); y -= 6*mm
            zebra(y, i, need)
            c.setFont("Helvetica-Oblique", 9.5)
            yy = y - 6*mm
            for line in chunks:
                c.drawString(left + 2*mm, yy, f"“{line}”"); yy -= 5.2*mm
            y -= need
        return y - 4*mm

    def draw_callbacks(y):
        if not callbacks: return y
        c.setFont("Helvetica-Bold", 12); c.drawString(left, y, "Call-back requested"); y -= 6*mm
        row_h = 7.2*mm
        widths = [50*mm, 60*mm, 40*mm, width - (50+60+40)*mm]
        xs = [left, left+widths[0], left+widths[0]+widths[1], left+widths[0]+widths[1]+widths[2]]
        # header
        zebra(y, 0, row_h)
        c.setFont("Helvetica-Bold", 10)
        for i, htxt in enumerate(["Name", "Email", "Telephone", "Submitted"]):
            c.drawString(xs[i] + 1.5*mm, y - 4.5*mm, htxt)
        y -= row_h; c.setFont("Helvetica", 9.5)
        for i, cb in enumerate(callbacks, start=1):
            if y - row_h < bottom + 16*mm:
                c.showPage(); y = draw_header()
                c.setFont("Helvetica-Bold", 12); c.drawString(left, y, "Call-back requested (cont.)"); y -= 6*mm
                zebra(y, 0, row_h); c.setFont("Helvetica-Bold", 10)
                for j, htxt in enumerate(["Name", "Email", "Telephone", "Submitted"]):
                    c.drawString(xs[j] + 1.5*mm, y - 4.5*mm, htxt)
                y -= row_h; c.setFont("Helvetica", 9.5)
            zebra(y, i, row_h)
            c.drawString(xs[0] + 1.5*mm, y - 4.5*mm, cb["name"] or "—")
            c.drawString(xs[1] + 1.5*mm, y - 4.5*mm, cb["email"] or "—")
            c.drawString(xs[2] + 1.5*mm, y - 4.5*mm, cb["phone"] or "—")
            submitted = cb["submitted"].strftime("%d %b %Y %H:%M") if cb.get("submitted") else "—"
            c.drawString(xs[3] + 1.5*mm, y - 4.5*mm, submitted)
            y -= row_h
        return y - 4*mm

    def footer():
        c.setFont("Helvetica", 8.5)
        c.setFillColorRGB(0.35, 0.35, 0.4)
        c.drawCentredString(W/2, 8*mm,
            "Unicorn Fire & Safety Solutions, Unicorn House, 6 Salendine, Shrewsbury, SY1 3XJ  ·  info@unicornsafety.co.uk  ·  01743 360211")
        c.setFillColor(colors.black)

    y = draw_header()
    y = draw_overall(y)
    y = draw_averages(y)
    y = draw_comments(y)
    y = draw_callbacks(y)
    footer(); c.showPage(); footer(); c.save()

    buf.seek(0)
    filename = f"feedback_summary_{booking.course_reference or booking.pk}.pdf"
    return FileResponse(buf, as_attachment=True, filename=filename)

@login_required
def send_course_docs(request, pk):
    booking = get_object_or_404(
        Booking.objects.select_related("course_type", "business", "instructor", "training_location"),
        pk=pk,
    )
    instr = getattr(request.user, "personnel", None)
    if not instr or not instructor_can_access_booking(instr, booking):
        messages.error(request, "You do not have access to this booking.")
        return redirect("instructor_bookings")

    if request.method != "POST":
        return redirect("instructor_booking_detail", pk=booking.pk)

    if booking.is_dummy_business:
        messages.info(request, "Dummy familiarisation bookings do not email course documents to admin.")
        return redirect("instructor_booking_detail", pk=booking.pk)

    n = email_all_course_docs_to_admin(request, booking)
    if n:
        messages.success(request, f"Sent {n} PDF document(s) to admin.")
    else:
        messages.warning(request, "No documents were generated to send.")
    return redirect("instructor_booking_detail", pk=booking.pk)




@login_required
@user_passes_test(lambda u: u.is_superuser)
def email_diagnostics(request):
    """
    TEMP endpoint to debug SMTP on Render. Plain-text output.
    Only accessible to logged-in superusers.
    """
    mask = lambda s: (s[:3] + "…" + s[-3:]) if s else ""
    lines = []
    lines.append("== EMAIL SETTINGS SEEN BY DJANGO ==")
    lines.append(f"EMAIL_BACKEND        = {settings.EMAIL_BACKEND}")
    lines.append(f"EMAIL_HOST           = {getattr(settings, 'EMAIL_HOST', '')}")
    lines.append(f"EMAIL_PORT           = {getattr(settings, 'EMAIL_PORT', '')}")
    lines.append(f"EMAIL_USE_TLS        = {getattr(settings, 'EMAIL_USE_TLS', '')}")
    lines.append(f"EMAIL_HOST_USER      = {getattr(settings, 'EMAIL_HOST_USER', '')}")
    pw = getattr(settings, 'EMAIL_HOST_PASSWORD', '')
    lines.append(f"EMAIL_HOST_PASSWORD  = {mask(pw)}")
    lines.append(f"DEFAULT_FROM_EMAIL   = {getattr(settings, 'DEFAULT_FROM_EMAIL', '')}")
    lines.append(f"ADMIN_INBOX_EMAIL    = {getattr(settings, 'ADMIN_INBOX_EMAIL', '')}")
    lines.append(f"DEV_CATCH_ALL_EMAIL  = {getattr(settings, 'DEV_CATCH_ALL_EMAIL', '')}")
    lines.append("")

    # Try a real SMTP handshake
    lines.append("== SMTP HANDSHAKE ==")
    host = getattr(settings, 'EMAIL_HOST', 'smtp.gmail.com')
    port = int(getattr(settings, 'EMAIL_PORT', 587) or 587)
    user = getattr(settings, 'EMAIL_HOST_USER', '')
    pwd  = getattr(settings, 'EMAIL_HOST_PASSWORD', '')

    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            s = smtplib.SMTP(host, port, timeout=30)
            s.set_debuglevel(1)  # print SMTP conversation
            s.ehlo()
            if getattr(settings, 'EMAIL_USE_TLS', True):
                s.starttls(context=ssl.create_default_context())
                s.ehlo()
            if user:
                s.login(user, pwd)
            s.quit()
        lines.append("SMTP handshake: OK")
        lines.append("")
        lines.append("Raw SMTP dialogue:")
        lines.append(buf.getvalue())
    except Exception as e:
        lines.append(f"SMTP handshake: FAILED -> {type(e).__name__}: {e}")

    return HttpResponse("\n".join(lines), content_type="text/plain; charset=utf-8")

@require_POST
def send_booking_email(request, booking_id):
    booking = get_object_or_404(Booking, pk=booking_id)
    subject = f"Docs for {booking.course_name} — {booking.ref_code} ({now().strftime('%Y-%m-%d %H:%M')})"
    body = "Please find the documents attached."
    attachments = []  # e.g., [booking.generated_pdf.path] or [("notes.txt","hello","text/plain")]

    try:
        send_admin_email(subject, body, attachments=attachments, reply_to=["unicorn@adminforge.co.uk"])
        messages.success(request, "Email sent ✅")
    except Exception as e:
        messages.error(request, f"Email failed: {e}")
    return redirect("booking_detail", booking_id=booking.id)

# ---- helpers for attempt header bits (robust to missing model helpers) ----
def _attempt_header_bits(attempt):
    """
    Returns a dict with:
      display_name, correct_count, total_questions, result_label, result_class
    Robust even if the model lacks helpers/fields; will fall back to the register.
    """
    # ----- Name candidates on the attempt row -----
    display_name = ""
    # callables first
    for cand in ("display_name",):
        v = getattr(attempt, cand, None)
        if callable(v):
            try:
                display_name = (v() or "").strip()
            except Exception:
                pass
        if display_name:
            break
    # plain attributes next
    if not display_name:
        for cand in ("display_name", "name", "full_name", "delegate_name", "candidate_name"):
            v = getattr(attempt, cand, "") or ""
            if v:
                display_name = str(v).strip()
                break
    # first/last
    if not display_name:
        fn = (getattr(attempt, "first_name", "") or "").strip()
        ln = (getattr(attempt, "last_name", "") or "").strip()
        display_name = (fn + " " + ln).strip()

    # ----- Fallback: look up from the booking-day register for the same course/instructor/date -----
    if not display_name:
        try:
            from .models import BookingDay, DelegateRegister  # local import to avoid cycles

            day_ids = list(
                BookingDay.objects.filter(
                    booking__course_type_id=getattr(attempt.exam, "course_type_id", None),
                    booking__instructor_id=getattr(attempt, "instructor_id", None),
                    date=getattr(attempt, "exam_date", None),
                ).values_list("id", flat=True)
            )
            if day_ids:
                reg = (
                    DelegateRegister.objects.filter(
                        booking_day_id__in=day_ids,
                        date_of_birth=getattr(attempt, "date_of_birth", None),
                    )
                    .order_by("id")
                    .first()
                )
                if reg:
                    # try 'name', else first+last
                    rn = (getattr(reg, "name", "") or "").strip()
                    if not rn:
                        rfn = (getattr(reg, "first_name", "") or "").strip()
                        rln = (getattr(reg, "last_name", "") or "").strip()
                        rn = (rfn + " " + rln).strip()
                    display_name = rn or display_name
        except Exception:
            # ignore lookup failures; we’ll just show a dash
            pass

    # ----- Counts (safe) -----
    from .models import ExamAttemptAnswer
    cc = getattr(attempt, "correct_count", None)
    if callable(cc):
        try:
            correct_count = cc()
        except Exception:
            correct_count = None
    else:
        correct_count = cc
    if correct_count is None:
        correct_count = ExamAttemptAnswer.objects.filter(attempt=attempt, is_correct=True).count()

    tq = getattr(attempt, "total_questions", None)
    if callable(tq):
        try:
            total_questions = tq()
        except Exception:
            total_questions = None
    else:
        total_questions = tq
    if total_questions is None:
        total_questions = attempt.exam.questions.count()

    # ----- Result (derive if needed) -----
    rl = getattr(attempt, "result_label", None)
    if callable(rl):
        try:
            result_label = rl()
        except Exception:
            result_label = None
    else:
        result_label = rl

    rc = getattr(attempt, "result_class", None)
    if callable(rc):
        try:
            result_class = rc()
        except Exception:
            result_class = None
    else:
        result_class = rc

    if not result_label or not result_class:
        pct = 0 if total_questions == 0 else round(correct_count * 100 / total_questions)
        passp = getattr(attempt.exam, "pass_mark_percent", 70) or 70
        viva  = getattr(attempt.exam, "viva_threshold_percent", None)
        if pct >= passp:
            result_label, result_class = "Pass", "success"
        elif viva and pct >= viva:
            result_label, result_class = "Viva", "warning"
        else:
            result_label, result_class = "Fail", "danger"

    return {
        "display_name": (display_name or "—"),
        "correct_count": correct_count,
        "total_questions": total_questions,
        "result_label": result_label,
        "result_class": result_class,
    }


def _booking_exam_wrong_summary(course_exams, attempts_qs):
    """Return wrong-answer summary keyed by exam sequence for display/export."""
    exam_by_id = {ex.id: ex for ex in course_exams}
    attempts_by_exam_id = defaultdict(int)
    for att in attempts_qs:
        attempts_by_exam_id[getattr(att, "exam_id", None)] += 1

    wrong_choices_rows = (
        ExamAttemptAnswer.objects
        .filter(attempt__in=attempts_qs, is_correct=False, answer__isnull=False)
        .values("attempt__exam_id", "question_id", "answer__text")
        .annotate(choice_count=Count("id"))
        .order_by("attempt__exam_id", "question_id", "-choice_count", "answer__text")
    )

    no_answer_rows = (
        ExamAttemptAnswer.objects
        .filter(attempt__in=attempts_qs, is_correct=False, answer__isnull=True)
        .values("attempt__exam_id", "question_id")
        .annotate(choice_count=Count("id"))
    )

    wrong_choices_by_question = defaultdict(list)
    for row in wrong_choices_rows:
        key = (row["attempt__exam_id"], row["question_id"])
        wrong_choices_by_question[key].append({
            "answer_text": row.get("answer__text") or "",
            "count": int(row.get("choice_count") or 0),
        })

    for row in no_answer_rows:
        key = (row["attempt__exam_id"], row["question_id"])
        wrong_choices_by_question[key].append({
            "answer_text": "No answer selected",
            "count": int(row.get("choice_count") or 0),
        })

    wrong_rows = (
        ExamAttemptAnswer.objects
        .filter(attempt__in=attempts_qs, is_correct=False)
        .values("attempt__exam_id", "question_id", "question__order", "question__text")
        .annotate(wrong_count=Count("id"), wrong_attempts=Count("attempt", distinct=True))
        .order_by("attempt__exam_id", "question__order", "question_id")
    )

    summary_by_seq = defaultdict(list)
    for row in wrong_rows:
        exam_id = row["attempt__exam_id"]
        exam = exam_by_id.get(exam_id)
        if not exam:
            continue
        seq = getattr(exam, "sequence", None) or exam.id
        total_attempts = max(attempts_by_exam_id.get(exam_id, 0), 1)
        wrong_attempts = int(row.get("wrong_attempts") or 0)
        choice_key = (exam_id, row["question_id"])
        wrong_choices = wrong_choices_by_question.get(choice_key, [])
        wrong_choices = sorted(wrong_choices, key=lambda x: x["count"], reverse=True)
        summary_by_seq[seq].append({
            "question_id": row["question_id"],
            "question_order": row.get("question__order"),
            "question_text": row.get("question__text") or "",
            "wrong_count": int(row.get("wrong_count") or 0),
            "wrong_attempts": wrong_attempts,
            "wrong_pct": round((wrong_attempts * 100.0) / total_attempts, 1),
            "total_attempts": total_attempts,
            "wrong_choices": wrong_choices,
        })

    # Order each exam summary by most-frequently-wrong first.
    for seq in list(summary_by_seq.keys()):
        summary_by_seq[seq] = sorted(
            summary_by_seq[seq],
            key=lambda r: (r["wrong_attempts"], r["wrong_count"], -(r["question_order"] or 0)),
            reverse=True,
        )

    return dict(summary_by_seq)

@login_required
def instructor_attempt_review(request, attempt_id: int):
    """
    Full review: show all answers in original order.
    """
    attempt = get_object_or_404(
        ExamAttempt.objects.select_related("exam", "exam__course_type", "instructor"),
        pk=attempt_id,
    )
    if not _can_view_attempt(request.user, attempt):
        return HttpResponseForbidden("Not allowed.")

    answers = (
        ExamAttemptAnswer.objects
        .select_related("question", "answer")
        .filter(attempt=attempt)
        .order_by("question__order", "question_id")
    )

    # IMPORTANT: _attempt_header_stats returns a dict; do NOT unpack into 4 vars
    stats = _attempt_header_stats(attempt)

    ctx = {
        "attempt": attempt,
        "exam": attempt.exam,
        "course_type": attempt.exam.course_type,
        "answers": answers,
        "display_name": _display_name_for_attempt(attempt) or "—",

        # pull values from the dict
        "correct_count": stats.get("correct_count"),
        "total_questions": stats.get("total_questions"),
        "result_label": stats.get("result_label") or stats.get("result_text"),
        "result_class": stats.get("result_class"),

        "back_url": _back_url_for_attempt(request, attempt),
    }
    return render(request, "instructor/exams/attempt_review.html", ctx)


def _build_attempt_incorrect_context(request, attempt):
    wrong = (
        ExamAttemptAnswer.objects
        .select_related("question", "answer")
        .filter(attempt=attempt, is_correct=False)
        .order_by("question__order", "question_id")
    )

    stats = _attempt_header_stats(attempt)

    viva_eligible = bool(getattr(attempt, "viva_eligible", False))
    viva_decided_at = getattr(attempt, "viva_decided_at", None)
    viva_decided_by = getattr(attempt, "viva_decided_by", None)
    viva_notes = getattr(attempt, "viva_notes", "")

    edit_mode = (request.GET.get("edit_viva") == "1") and bool(viva_decided_at)
    show_viva_form = (viva_eligible and not viva_decided_at) or edit_mode

    viva_selected = None
    if show_viva_form:
        existing = (getattr(attempt, "viva_result", "") or "").lower()
        if existing in ("pass", "fail"):
            viva_selected = existing

    viva_decided_summary = None
    if getattr(attempt, "viva_result", None) or viva_decided_at:
        viva_decided_summary = {
            "outcome": (
                getattr(attempt, "viva_result", None)
                or ("pass" if bool(getattr(attempt, "passed", False)) else "fail")
            ),
            "when": viva_decided_at,
            "by": getattr(viva_decided_by, "name", None) or getattr(viva_decided_by, "username", None),
            "notes": viva_notes,
        }

    return {
        "attempt": attempt,
        "exam": attempt.exam,
        "course_type": attempt.exam.course_type,
        "back_id": request.GET.get("back") or request.GET.get("booking") or request.POST.get("back") or "",
        "display_name": stats["display_name"],
        "correct_count": stats["correct_count"],
        "total_questions": stats["total_questions"],
        "result_label": stats["result_label"],
        "result_class": stats["result_class"],
        "wrong": wrong,
        "show_viva_form": show_viva_form,
        "viva_eligible": viva_eligible,
        "viva_decided_at": viva_decided_at,
        "viva_selected": viva_selected,
        "viva_notes_prefill": viva_notes,
        "viva_decided_summary": viva_decided_summary,
        "can_authorise_retest": not bool(getattr(attempt, "passed", False)),
        "flash_success": "",
        "flash_error": "",
    }


def _attempt_incorrect_embed_mode(request) -> bool:
    return (
        request.GET.get("embed") == "1"
        or request.POST.get("embed") == "1"
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
    )


def _render_attempt_incorrect(request, attempt, *, embed: bool, extra=None):
    ctx = _build_attempt_incorrect_context(request, attempt)
    ctx["embed"] = embed
    if extra:
        ctx.update(extra)
    template = (
        "instructor/exams/_attempt_incorrect_panel.html"
        if embed
        else "instructor/exams/attempt_incorrect.html"
    )
    return render(request, template, ctx)


@login_required
def instructor_attempt_incorrect(request, attempt_id: int):
    """
    Show incorrect answers, allow viva decision (with edit), and authorise re-test.
    When authorising a re-test, set a 60-minute expiry window.
    """
    attempt = get_object_or_404(
        ExamAttempt.objects.select_related("exam", "exam__course_type", "instructor"),
        pk=attempt_id
    )
    if not _can_view_attempt(request.user, attempt):
        return HttpResponseForbidden("Not allowed.")

    embed = _attempt_incorrect_embed_mode(request)

    # --- POST: save viva decision (unconditional save when posted) ---
    if request.method == "POST" and request.POST.get("save_viva") == "1":
        outcome = (request.POST.get("viva_outcome") or "").strip().lower()
        notes   = (request.POST.get("viva_notes") or "").strip()

        if outcome in ("pass", "fail"):
            attempt.passed = (outcome == "pass")
            attempt.viva_result = outcome
            attempt.viva_notes = notes
            attempt.viva_eligible = False
            if not getattr(attempt, "finished_at", None):
                attempt.finished_at = now()
            attempt.viva_decided_at = now()
            attempt.viva_decided_by = getattr(request.user, "personnel", None)

            attempt.save()
            recompute_outcomes_for_exam_attempt(attempt)
            if embed:
                attempt.refresh_from_db()
                return _render_attempt_incorrect(
                    request, attempt, embed=True,
                    extra={"flash_success": "Viva decision saved."},
                )
            messages.success(request, "Viva decision saved.")
            return redirect(f"{reverse('instructor_attempt_incorrect', args=[attempt.pk])}"
                            f"{'?' + request.GET.urlencode() if request.GET else ''}")

        if embed:
            return _render_attempt_incorrect(
                request, attempt, embed=True,
                extra={"flash_error": "Viva not saved: invalid outcome posted."},
            )
        messages.error(request, "Viva not saved: invalid outcome posted.")
        return redirect(f"{reverse('instructor_attempt_incorrect', args=[attempt.pk])}"
                        f"{'?' + request.GET.urlencode() if request.GET else ''}")

    # --- POST: authorise a re-test (60-minute window) ---
    if request.method == "POST" and "authorise" in request.POST:
        if getattr(attempt, "passed", False):
            msg = "This attempt is a pass. You can only authorise a re-test for failed attempts."
            if embed:
                return _render_attempt_incorrect(request, attempt, embed=True, extra={"flash_error": msg})
            messages.error(request, msg)
            return redirect(request.get_full_path())

        attempt.retake_authorised = True
        attempt.retake_authorised_until = now() + timedelta(minutes=60)
        attempt.save(update_fields=["retake_authorised", "retake_authorised_until"])

        msg = "Re-test authorised for this delegate for the next 60 minutes."
        if embed:
            attempt.refresh_from_db()
            return _render_attempt_incorrect(request, attempt, embed=True, extra={"flash_success": msg})
        messages.success(request, msg)
        return redirect(request.get_full_path())

    return _render_attempt_incorrect(request, attempt, embed=embed)

@login_required
@require_http_methods(["POST"])
@transaction.atomic
def instructor_attempt_authorize_retake(request, attempt_id: int):
    """
    Allow one retake (set a flag on the attempt or on the candidate keyed by name+dob).
    For now we store on the attempt so the UI can enable a 'Retake' button.
    """
    attempt = get_object_or_404(
        ExamAttempt.objects.select_related("exam", "exam__course_type", "instructor"),
        pk=attempt_id,
    )
    if not _can_view_attempt(request.user, attempt):
        return HttpResponseForbidden("Not allowed.")

    # Mark retake allowed unless this is already the second attempt
    # (If you maintain attempt_number, treat >=2 as final)
    if getattr(attempt, "attempt_number", 1) >= 2:
        # no-op: second (or more) attempts cannot get re-authorised
        pass
    else:
        setattr(attempt, "retake_allowed", True)
        attempt.save(update_fields=["retake_allowed"])

    # bounce back to booking page (you already build those links there)
    return redirect(
        f"{reverse('instructor_booking_detail', kwargs={'pk': attempt.booking_id})}#exams-tab"
        if hasattr(attempt, "booking_id") and attempt.booking_id
        else reverse("instructor_bookings")
    )


@login_required
def instructor_attempt_response_pdf(request, attempt_id: int):
    """Export one delegate's full exam responses to PDF."""
    attempt = get_object_or_404(
        ExamAttempt.objects.select_related("exam", "exam__course_type", "instructor", "booking"),
        pk=attempt_id,
    )
    if not _can_view_attempt(request.user, attempt):
        return HttpResponseForbidden("Not allowed.")

    answers = (
        ExamAttemptAnswer.objects
        .select_related("question", "answer")
        .filter(attempt=attempt)
        .order_by("question__order", "question_id")
    )

    stats = _attempt_header_stats(attempt)
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    left = 18 * mm
    y = height - 18 * mm

    def _line(text, size=10, gap=5):
        nonlocal y
        if y < 18 * mm:
            pdf.showPage()
            y = height - 18 * mm
        pdf.setFont("Helvetica", size)
        pdf.drawString(left, y, str(text))
        y -= gap * mm

    _line("Delegate Exam Response", size=14, gap=7)
    _line(f"Delegate: {_display_name_for_attempt(attempt)}")
    _line(f"Exam: {attempt.exam.course_type.name} / {attempt.exam.exam_code}")
    _line(f"Date of birth: {attempt.date_of_birth:%d %b %Y}")
    _line(f"Score: {stats['correct_count']}/{stats['total_questions']} ({stats['result_label']})", gap=7)

    for idx, aa in enumerate(answers, start=1):
        question_text = (aa.question.text or "").strip().replace("\n", " ")
        chosen_text = (aa.answer.text if aa.answer else "No answer selected")
        correctness = "Correct" if aa.is_correct else "Incorrect"
        _line(f"Q{idx}: {question_text}", size=9, gap=4)
        _line(f"Chosen: {chosen_text}", size=9, gap=4)
        _line(f"Result: {correctness}", size=9, gap=6)

    pdf.showPage()
    pdf.save()
    pdf_bytes = buffer.getvalue()
    buffer.close()

    filename = f"exam-response-attempt-{attempt.pk}.pdf"
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required
def instructor_exams_summary_pdf(request, booking_id):
    """Export exam summary PDF: delegate scores + commonly missed questions."""
    instr = getattr(request.user, "personnel", None)
    booking = get_object_or_404(
        Booking.objects.select_related("course_type", "instructor"),
        pk=booking_id,
    )
    if not instr or not instructor_can_access_booking(instr, booking):
        return HttpResponseForbidden("Not allowed.")

    booking_dates = list(BookingDay.objects.filter(booking=booking).values_list("date", flat=True))
    course_exams = list(Exam.objects.filter(course_type=booking.course_type).order_by("sequence", "id"))
    attempts_qs = list(
        ExamAttempt.objects
        .select_related("exam")
        .filter(
            exam__course_type=booking.course_type,
            instructor=booking.instructor,
            exam_date__in=booking_dates,
            finished_at__isnull=False,
        )
        .order_by("exam__sequence", "exam_date", "delegate_name")
    )
    summary_by_seq = _booking_exam_wrong_summary(course_exams, attempts_qs)

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    left = 18 * mm
    y = height - 18 * mm

    def _line(text, size=10, gap=5):
        nonlocal y
        if y < 18 * mm:
            pdf.showPage()
            y = height - 18 * mm
        pdf.setFont("Helvetica", size)
        pdf.drawString(left, y, str(text))
        y -= gap * mm

    _line("Exam Summary", size=14, gap=7)
    _line(f"Course: {booking.course_type.name}")
    _line(f"Booking ref: {booking.course_reference}", gap=7)

    _line("Delegate scores", size=12, gap=6)
    if not attempts_qs:
        _line("No completed attempts yet.")
    else:
        for att in attempts_qs:
            total = max(getattr(att, "total_questions", 0) or 0, 1)
            pct = round((float(getattr(att, "score_correct", 0) or 0) * 100.0) / total, 1)
            _line(
                f"{_display_name_for_attempt(att)} | Exam {getattr(att.exam, 'sequence', '-')}: "
                f"{att.score_correct}/{att.total_questions} ({pct}%)"
            )

    _line("", gap=2)
    _line("Most missed questions", size=12, gap=6)
    if not summary_by_seq:
        _line("No incorrect answers recorded yet.")
    else:
        for ex in course_exams:
            seq = getattr(ex, "sequence", None) or ex.id
            rows = summary_by_seq.get(seq, [])
            if not rows:
                continue
            _line(f"Exam {seq} — {ex.title}", size=11, gap=5)
            for row in rows[:12]:
                qtxt = (row["question_text"] or "").strip().replace("\n", " ")
                qtxt = qtxt[:95] + ("…" if len(qtxt) > 95 else "")
                _line(
                    f"Q{row['question_order'] or '?'}: {row['wrong_attempts']}/{row['total_attempts']} "
                    f"({row['wrong_pct']}%) wrong — {qtxt}",
                    size=9,
                    gap=4,
                )
            _line("", gap=2)

    pdf.showPage()
    pdf.save()
    pdf_bytes = buffer.getvalue()
    buffer.close()

    filename = f"exam-summary-{booking.course_reference}.pdf"
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response

@login_required
@require_POST
def instructor_upload_receipt(request, pk):
    """
    AJAX: upload one receipt file to Google Drive into:
      Receipts/<COURSE_REF>/
    Returns JSON on success/failure. Never redirects/returns HTML.
    """
    try:
        # 1) Guard: instructor must own this booking
        booking = get_object_or_404(Booking.objects.select_related("instructor"), pk=pk)
        instr = getattr(request.user, "personnel", None)
        if not instr or not instructor_can_access_booking(instr, booking):
            return HttpResponseForbidden("You do not have access to this booking.")

        # 2) File present?
        f = request.FILES.get("file")
        if not f:
            return JsonResponse({"ok": False, "error": "No file uploaded"}, status=400)

        # 3) Build Drive service
        try:
            svc = get_drive_service(settings.GOOGLE_OAUTH_CLIENT_SECRET, settings.GOOGLE_OAUTH_TOKEN)
        except Exception as e:
            logging.exception("OAuth/Drive init failed")
            return JsonResponse({"ok": False, "error": f"Drive auth failed: {e}"}, status=500)

        # 4) Ensure folder path: Receipts/<COURSE_REF>
        course_ref = getattr(booking, "course_reference", "") or ""
        folder_name = safe_folder_name(course_ref)  # e.g. FAAW-3HGHX9 or "No-Ref"

        root = settings.GOOGLE_DRIVE_ROOT_RECEIPTS
        try:
            # Single-level folder directly under root, named after course ref
            parent = ensure_path(svc, [folder_name], root)
        except HttpError as he:
            logging.exception("ensure_path HttpError")
            return JsonResponse({"ok": False, "error": f"Drive folder error: {he}"}, status=500)

        # 5) Upload
        mime = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        meta = {
            "name": f.name,
            "parents": [parent],
            "appProperties": {
                "purpose": "receipt",
                "booking_id": str(booking.id),
                "instructor_id": str(instr.id),
                "course_ref": folder_name,
            },
        }
        media = MediaIoBaseUpload(io.BytesIO(f.read()), mimetype=mime, resumable=True)

        created = svc.files().create(
            body=meta, media_body=media, fields="id,name,mimeType,webViewLink"
        ).execute()

        return JsonResponse({"ok": True, **created})

    except HttpError as he:
        logging.exception("Google API HttpError")
        return JsonResponse({"ok": False, "error": f"Google API error: {he}"}, status=500)
    except Exception as e:
        logging.exception("Unexpected error in instructor_upload_receipt")
        return JsonResponse({"ok": False, "error": f"Server error: {e}"}, status=500)
    
from .drive_paths import ensure_path, find_folder  # make sure find_folder is imported

@login_required
def instructor_list_receipts(request, pk):
    """
    JSON: list existing receipts for a booking.
    Looks under Receipts/<COURSE_REF>/ (no creation if missing).
    """
    try:
      booking = get_object_or_404(Booking.objects.select_related("instructor"), pk=pk)
      instr = getattr(request.user, "personnel", None)
      if not instr or not instructor_can_access_booking(instr, booking):
          return HttpResponseForbidden("Forbidden")

      svc = get_drive_service(settings.GOOGLE_OAUTH_CLIENT_SECRET, settings.GOOGLE_OAUTH_TOKEN)

      course_ref = getattr(booking, "course_reference", "") or ""
      folder_name = safe_folder_name(course_ref)  # from earlier helper
      root = settings.GOOGLE_DRIVE_ROOT_RECEIPTS

      # find the course folder; if missing, return empty list (don't create here)
      parent_id = find_folder(svc, folder_name, root)
      if not parent_id:
          return JsonResponse({"ok": True, "files": []})

      res = svc.files().list(
          q=f"'{parent_id}' in parents and trashed=false",
          fields="files(id,name,webViewLink,mimeType)",
          pageSize=100,
      ).execute()
      files = res.get("files", [])
      return JsonResponse({"ok": True, "files": files})
    except Exception as e:
      logging.exception("list receipts failed")
      return JsonResponse({"ok": False, "error": str(e)}, status=500)


@login_required
@require_POST
def instructor_delete_receipt(request, pk):
    """
    JSON: delete one receipt (by Drive fileId) for a booking you own.
    """
    try:
      booking = get_object_or_404(Booking.objects.select_related("instructor"), pk=pk)
      instr = getattr(request.user, "personnel", None)
      if not instr or not instructor_can_access_booking(instr, booking):
          return HttpResponseForbidden("Forbidden")

      file_id = request.POST.get("file_id")
      if not file_id:
          return JsonResponse({"ok": False, "error": "Missing file_id"}, status=400)

      svc = get_drive_service(settings.GOOGLE_OAUTH_CLIENT_SECRET, settings.GOOGLE_OAUTH_TOKEN)
      svc.files().delete(fileId=file_id).execute()
      return JsonResponse({"ok": True})
    except HttpError as he:
      logging.exception("delete HttpError")
      return JsonResponse({"ok": False, "error": f"Google API error: {he}"}, status=500)
    except Exception as e:
      logging.exception("delete failed")
      return JsonResponse({"ok": False, "error": str(e)}, status=500)


@login_required
def whoami(request):
    u = request.user
    has_instructor = bool(getattr(u, "instructor", None))
    return JsonResponse({
        "username": u.get_username(),
        "is_staff": u.is_staff,
        "has_instructor": has_instructor,
    })

@login_required
def instructor_booking_certificates_pdf(request, pk):
    """
    Return the certificates PDF for this booking so instructors/admin
    can view/download it from the UI.

    Permissions:
      - The instructor assigned to this booking, OR
      - Staff / superuser.
    """
    booking = get_object_or_404(
        Booking.objects.select_related("instructor"),
        pk=pk,
    )

    # Permission guard
    instr = getattr(request.user, "personnel", None)
    if not (
        request.user.is_staff
        or request.user.is_superuser
        or (instr and instructor_can_access_booking(instr, booking))
    ):
        return HttpResponseForbidden("You do not have access to this booking.")

    # Build certificates PDF (ReportLab via utils.certificates)
    result = build_certificates_pdf_for_booking(booking)
    if not result:
        return HttpResponse(
            "No certificates available for this booking.",
            content_type="text/plain",
        )

    filename, pdf_bytes = result

    # inline = open in browser tab; change to attachment to force download
    resp = HttpResponse(pdf_bytes, content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{filename}"'
    return resp

@login_required
def instructor_resources(request):
    resources = Resource.objects.filter(is_active=True)

    course_folders = (
        CourseType.objects
        .filter(~Q(onedrive_folder_link=""), onedrive_folder_link__isnull=False)
        .order_by("name")
    )

    return render(request, "instructor/resources.html", {
        "resources": resources,
        "course_folders": course_folders,
    })