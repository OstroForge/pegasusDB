from datetime import timedelta, datetime, date
from threading import active_count
from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.db.models import Prefetch
from django.http import HttpResponseForbidden, HttpRequest, HttpResponse, HttpResponseBadRequest, Http404
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.timezone import now
from django.db.models import Q
from .models import Booking, Exam, DelegateRegister, ExamAnswer, ExamAttempt, ExamAttemptAnswer, ExamQuestion, Personnel, CourseType
from .services.assessment_exams import recompute_outcomes_for_exam_attempt
from .forms_exam import DelegateExamStartForm
from django.views.decorators.csrf import ensure_csrf_cookie, csrf_protect
from django.views.decorators.http import require_http_methods

from math import ceil
from urllib.parse import urlencode, unquote_plus

import re, math, random

def _norm_name(s: str) -> str:
    """Name normalisation used everywhere (case/space-insensitive)."""
    s = (s or "").strip()
    # title-case while keeping internal spaces normalised
    return " ".join(part.capitalize() for part in s.split())

def _title_case_name(s: str) -> str:
    """Pretty storage form (Title Case) while keeping comparison robust."""
    return " ".join(p.capitalize() for p in _norm_name(s).split())

def _candidate_attempts(exam, name: str, dob, exam_date):
    """
    Attempts for the *same exam* on the *same day* by the *same person*,
    matching on delegate_name (case-insensitive) and DOB (if provided).
    """
    qs = ExamAttempt.objects.filter(
        exam=exam,
        exam_date=exam_date,
        delegate_name__iexact=name,
    )
    if dob:
        qs = qs.filter(date_of_birth=dob)
    return qs.order_by("started_at", "id")

def _result_value(att) -> str:
    """
    Defensive getter for result — your model has changed a few times;
    default to '' if the field doesn’t exist.
    """
    return (getattr(att, "result", "") or "").lower()

def _show_exam_date_picker():
    return getattr(settings, "REGISTER_SHOW_DATE", settings.DEBUG)


def _resolve_exam_date_from_request(request, *, default):
    """Resolve exam date from GET/POST (ISO hidden field or DD/MM/YYYY partials)."""
    from .views import _parse_partial_date_from_post

    if request.method == "POST":
        raw = (request.POST.get("exam_date") or "").strip()
        if raw:
            try:
                return date.fromisoformat(raw)
            except ValueError:
                pass
        parsed = _parse_partial_date_from_post(request.POST, "exam_date")
        if parsed:
            return parsed
    else:
        raw = (request.GET.get("exam_date") or request.GET.get("date") or "").strip()
        if raw:
            try:
                return date.fromisoformat(raw)
            except ValueError:
                pass
    return default


@ensure_csrf_cookie   # set csrftoken on GET
@csrf_protect         # validate on POST
def delegate_exam_start(request):
    """
    Start page for delegates. Uses Django messages for banners and exposes
    'can_continue' (bool) to show the green 'Continue to exam rules' button
    when we have a positive register match.
    """
    code = (request.GET.get("examcode") or request.POST.get("examcode") or "").upper()
    if not code:
        return render(request, "exam/delegate_start_missing_code.html", status=400)

    exam = get_object_or_404(Exam, exam_code=code)
    course_type = exam.course_type
    show_exam_date = _show_exam_date_picker()

    can_continue = False
    initial_exam_date = _resolve_exam_date_from_request(
        request, default=timezone.localdate()
    )

    if request.method == "POST":
        exam_date_for_instructors = _resolve_exam_date_from_request(
            request, default=timezone.localdate()
        )
        from .views import _resolve_public_register_instructors

        instructors, instructor_fallback = _resolve_public_register_instructors(
            course_type, exam_date_for_instructors
        )
        form = DelegateExamStartForm(
            request.POST,
            initial={"exam_code": exam.exam_code},
            instructors=instructors,
            show_exam_date=show_exam_date,
        )
        if form.is_valid():
            # Normalise inputs
            raw_name = form.cleaned_data["name"]
            name = " ".join(raw_name.split()).strip()
            dob = form.cleaned_data["date_of_birth"]
            ex_date = form.cleaned_data["exam_date"]
            instructor = form.cleaned_data["instructor"]  # retained for flow

            # Exact match: name + dob AND (booking_day with course/date OR legacy direct date)
            qs = (
                DelegateRegister.objects.filter(
                    name__iexact=name,
                    date_of_birth=dob,
                )
                .filter(
                    Q(
                        booking_day__booking__course_type=course_type,
                        booking_day__date=ex_date,
                    )
                    | Q(booking_day__isnull=True, date=ex_date)
                )
            )

            if qs.exists():
                can_continue = True
                messages.success(
                    request,
                    f"We found you on the register for {course_type.name} on {ex_date:%d %b %Y}."
                )
            else:
                # Provide helpful near matches for the same DOB + course + date
                near = (
                    DelegateRegister.objects.filter(date_of_birth=dob)
                    .filter(
                        Q(
                            booking_day__booking__course_type=course_type,
                            booking_day__date=ex_date,
                        )
                        | Q(booking_day__isnull=True, date=ex_date)
                    )
                    .values_list("name", flat=True)
                )
                if near:
                    unique_names = ", ".join(sorted(set(near)))
                    messages.warning(
                        request,
                        "We couldn’t find an exact match for your name. "
                        f"Names on the register for this course/date: {unique_names}. "
                        "Please check the spelling or speak to your instructor."
                    )
                else:
                    messages.warning(
                        request,
                        "We couldn’t find a matching register entry. "
                        "Please double-check your name and date of birth, or ask your instructor."
                    )
        # if form invalid, the form will show its field errors; no banner needed
    else:
        from .views import _resolve_public_register_instructors

        instructors, instructor_fallback = _resolve_public_register_instructors(
            course_type, initial_exam_date
        )
        form = DelegateExamStartForm(
            initial={
                "exam_code": exam.exam_code,
                "exam_date": initial_exam_date,
            },
            instructors=instructors,
            show_exam_date=show_exam_date,
        )

    exam_date_for_filter = None
    if request.method == "POST" and form.is_bound:
        if form.is_valid():
            exam_date_for_filter = form.cleaned_data.get("exam_date")
        else:
            exam_date_for_filter = _resolve_exam_date_from_request(
                request, default=timezone.localdate()
            )
    else:
        exam_date_for_filter = initial_exam_date

    from .views import _resolve_public_register_instructors, _dev_instructor_register_hint

    instructors, instructor_fallback = _resolve_public_register_instructors(
        course_type, exam_date_for_filter
    )
    single_instructor = instructors[0] if len(instructors) == 1 else None
    instructor_dev_hint = (
        _dev_instructor_register_hint(course_type, exam_date_for_filter)
        if show_exam_date and not instructor_fallback and not instructors
        else ""
    )

    ctx = {
        "exam": exam,
        "course_type": course_type,
        "form": form,
        "can_continue": can_continue,
        "show_exam_date": show_exam_date,
        "exam_date": exam_date_for_filter,
        "instructors": instructors,
        "single_instructor": single_instructor,
        "instructor_fallback": instructor_fallback,
        "instructor_dev_hint": instructor_dev_hint,
    }
    return render(request, "exam/delegate_start.html", ctx)

from math import ceil
# ...

def exam_instructors_api(request):
    """JSON: instructors for an exam's course on a given date (same rules as register)."""
    from django.http import JsonResponse
    from .views import _dev_instructor_register_hint, _resolve_public_register_instructors

    code = (request.GET.get("examcode") or "").upper()
    raw_date = (request.GET.get("date") or "").strip()
    if not code or not raw_date:
        return JsonResponse({"instructors": []})
    try:
        d = date.fromisoformat(raw_date)
    except ValueError:
        return JsonResponse({"instructors": []})
    exam = Exam.objects.filter(exam_code=code).select_related("course_type").first()
    if not exam:
        return JsonResponse({"instructors": []})
    instructors, instructor_fallback = _resolve_public_register_instructors(
        exam.course_type, d
    )
    payload = {
        "instructors": [{"id": i.id, "name": i.name} for i in instructors],
        "fallback": instructor_fallback,
    }
    if not instructor_fallback:
        hint = _dev_instructor_register_hint(exam.course_type, d)
        if hint:
            payload["dev_hint"] = hint
    return JsonResponse(payload)


@ensure_csrf_cookie
def delegate_exam_rules(request):
    code = (request.GET.get("examcode") or "").upper()
    if not code:
        return HttpResponseBadRequest("Missing exam code")

    exam = get_object_or_404(Exam, exam_code=code)
    course_type = exam.course_type

    # values carried from the start page
    name        = (request.GET.get("name") or "").strip()
    date_of_birth = request.GET.get("date_of_birth") or ""
    instructor  = request.GET.get("instructor") or ""
    exam_date   = request.GET.get("exam_date") or ""

    # Timer: 90 seconds per question
    active_count = exam.questions.filter(is_active=True).count()
    num_questions = min(active_count or exam.questions.count(), 15)
    total_seconds   = num_questions * 90
    minutes, seconds = divmod(total_seconds, 60)

    # Pass/viva thresholds expressed as question counts
    pass_mark_percent = exam.pass_mark_percent
    required_correct  = ceil((pass_mark_percent / 100) * num_questions)

    viva_required = None
    viva_pass_percent = getattr(exam, "viva_pass_percent", None)
    if viva_pass_percent:
        viva_required = ceil((viva_pass_percent / 100) * num_questions)

    # Build the querystring for the “Start exam” link
    qs = urlencode({
        "examcode":    exam.exam_code,
        "name":        name,
        "date_of_birth": date_of_birth,
        "instructor":  instructor,
        "exam_date":   exam_date,
    })

    context = {
        "exam": exam,
        "course_type": course_type,
        "num_questions": num_questions,
        "minutes": minutes,
        "seconds": seconds,
        "pass_mark_percent": pass_mark_percent,
        "required_correct": required_correct,
        "viva_pass_percent": viva_pass_percent,
        "viva_required": viva_required,   # None if not applicable
        "qs": qs,
    }
    return render(request, "exam/rules.html", context)

def _remaining_seconds(attempt) -> int:
    """
    Remaining time = (attempt.seconds_total or questions*90) minus time used.
    If expires_at is present, we trust it.
    """
    # 1) exact if you stored an expiry
    if getattr(attempt, "expires_at", None):
        delta = attempt.expires_at - datetime.now(attempt.expires_at.tzinfo)
        return max(0, int(delta.total_seconds()))

    # 2) compute from started_at + configured seconds_total (or questions*90)
    started = getattr(attempt, "started_at", None)
    if not started:
        return 0

    q_count = attempt.total_questions or getattr(getattr(attempt, "exam", None), "questions", None)
    q_count = q_count.count() if hasattr(q_count, "count") else int(q_count or 0)

    seconds_total = int(getattr(attempt, "seconds_total", 0) or q_count * 90 or 0)
    used = (datetime.now(started.tzinfo) - started).total_seconds()
    return max(0, int(seconds_total - used))

def _normalise_name(raw: str) -> str:
    """
    Turn '  jANE   dOE ' into 'Jane Doe'. Accepts URL-escaped values.
    """
    s = unquote_plus(raw or "").strip()
    if not s:
        return ""
    parts = [p for p in re.split(r"\s+", s) if p]
    return " ".join(p.capitalize() for p in parts)

def _find_latest_attempt(exam, name, dob, instructor, exam_date):
    """Find the most recent attempt for this delegate on this exam+date."""
    name_norm = _norm_name(name)
    return (
        ExamAttempt.objects
        .filter(
            exam=exam,
            delegate_name=name_norm,
            date_of_birth=dob,
            instructor=instructor,
            exam_date=exam_date,
        )
        .order_by("-pk")
        .first()
    )


def _resolve_booking_for_attempt(exam, delegate_name, dob, instructor, exam_date):
    if not instructor or not exam_date:
        return None

    register = (
        DelegateRegister.objects
        .select_related("booking_day__booking")
        .filter(
            booking_day__booking__course_type=exam.course_type,
            booking_day__booking__instructor=instructor,
            booking_day__date=exam_date,
            name__iexact=delegate_name,
            date_of_birth=dob,
        )
        .order_by("-created_at", "-id")
        .first()
    )

    if not register or not getattr(register, "booking_day", None):
        return None
    return register.booking_day.booking

def _start_new_attempt(exam, name, dob, instructor, exam_date):
    """Create a fresh attempt and consume any pending retake authorisation."""
    name_norm = _norm_name(name)
    total_q = exam.questions.count()
    # time limit from exam, store on the row (helps future calculations)
    secs_total = int(getattr(exam, "time_limit_seconds", 0) or 0)

    attempt = ExamAttempt.objects.create(
        exam=exam,
        booking=_resolve_booking_for_attempt(exam, name_norm, dob, instructor, exam_date),
        total_questions=total_q,
        score_correct=0,
        delegate_name=name_norm,
        date_of_birth=dob,
        instructor=instructor,
        exam_date=exam_date,
        started_at=timezone.now(),
        seconds_total=secs_total,
        # consume/clear any existing authorisation at the start of a new attempt
        retake_authorised=False,
        viva_eligible=False,
    )
    # derive expires_at if you keep it non-nullable
    if secs_total:
        attempt.expires_at = attempt.started_at + timedelta(seconds=secs_total)
        attempt.save(update_fields=["expires_at"])
    return attempt

def _enforce_single_attempt_or_authorised(exam, name, dob, instructor, exam_date):
    """
    Return either:
      - an existing in-progress attempt to continue, or
      - None when we should create a new one (allowed), or
      - raise PermissionDenied when blocked (already finished and no authorisation).
    """
    latest = _find_latest_attempt(exam, name, dob, instructor, exam_date)
    if not latest:
        return None  # allowed to start first attempt

    # still in progress? continue it
    if not latest.finished_at and _remaining_seconds(latest) > 0:
        return latest

    # finished — only allow new one if authorised
    if latest.retake_authorised:
        return None  # allowed to start fresh (we will consume the flag on create)

    # otherwise blocked
    raise PermissionDenied("A re-test has not been authorised for this delegate.")

def _get_or_create_attempt(request, exam: Exam):
    """
    Enforce one attempt per delegate per day unless an instructor has authorised a re-test.
    - Name match is case-insensitive and whitespace-trimmed.
    - If an unfinished attempt exists, resume it.
    - If a finished attempt exists:
        * If passed => refuse a new attempt.
        * If failed and retake_authorised == True => allow ONE more attempt and clear the flag.
        * Otherwise => refuse.
    Returns (attempt, error_message) where error_message is None on success.
    """
    # Extract details from querystring / form (you already do this earlier in the view)
    exam_code   = getattr(exam, "exam_code", None)
    name_input = _normalise_name(request.GET.get("name") or request.POST.get("name") or "")
    dob_str     = request.GET.get("date_of_birth") or request.POST.get("date_of_birth") or ""
    instructor_id = request.GET.get("instructor") or request.POST.get("instructor") or ""
    exam_date_str = request.GET.get("exam_date") or request.POST.get("exam_date") or ""

    # Parse / normalise
    delegate_name = _normalise_name(name_input)
    try:
        from datetime import datetime
        date_of_birth = datetime.strptime(dob_str, "%Y-%m-%d").date()
    except Exception:
        return None, "Your date of birth couldn’t be read. Please check the format (YYYY-MM-DD)."

    try:
        exam_day = datetime.strptime(exam_date_str, "%Y-%m-%d").date()
    except Exception:
        exam_day = now().date()

    from unicorn_project.training.models import Instructor  # adjust import path if needed
    try:
        instructor = Personnel.objects.get(pk=instructor_id)
    except Personnel.DoesNotExist:
        return None, "Instructor not found."

    # Look for attempts for THIS delegate on THIS day for THIS exam & instructor
    qs = ExamAttempt.objects.filter(
        exam=exam,
        instructor=instructor,
        exam_date=exam_day,
        date_of_birth=date_of_birth,
        # Case-insensitive match on name
        delegate_name__iexact=delegate_name,
    ).order_by("-id")

    # If there is an unfinished attempt, just resume it
    unfinished = qs.filter(started_at__isnull=False, finished_at__isnull=True).first()
    if unfinished:
        return unfinished, None

    latest = qs.first()

    # No previous attempt today -> create new
    if not latest:
        duration = exam.time_limit_seconds if hasattr(exam, "time_limit_seconds") else 1800  # default 30min

        new_attempt = ExamAttempt.objects.create(
            exam=exam,
            booking=_resolve_booking_for_attempt(exam, delegate_name, date_of_birth, instructor, exam_day),
            instructor=instructor,
            exam_date=exam_day,
            delegate_name=delegate_name,
            date_of_birth=date_of_birth,
            total_questions=exam.questions.count() if hasattr(exam, "questions") else 0,
            expires_at=now() + timedelta(seconds=duration),
        )
        return new_attempt, None


    # There is a finished attempt
    if latest.passed:
        return None, "You have already passed this exam today. A re-test is not required."

    # Failed attempt: allow ONE re-test if an instructor authorised it
    if latest.retake_authorised:
        # create the new attempt and immediately consume the authorisation
        duration = exam.time_limit_seconds if hasattr(exam, "time_limit_seconds") else 1800

        new_attempt = ExamAttempt.objects.create(
            exam=exam,
            booking=_resolve_booking_for_attempt(exam, delegate_name, date_of_birth, instructor, exam_day),
            instructor=instructor,
            exam_date=exam_day,
            delegate_name=delegate_name,
            date_of_birth=date_of_birth,
            total_questions=exam.questions.count() if hasattr(exam, "questions") else 0,
            expires_at=now() + timedelta(seconds=duration),
        )

        # consume authorisation
        latest.retake_authorised = False
        latest.save(update_fields=["retake_authorised"])

        return new_attempt, None

    # Otherwise: not authorised — do not create a new attempt
    return None, "A re-test must be authorised by the instructor before you can try again today."

def parse_user_date(date_str: str):
    """
    Accept dd/mm/yyyy or yyyy-mm-dd
    """
    if not date_str:
        return None

    date_str = date_str.strip()

    # Try DD/MM/YYYY
    try:
        if "/" in date_str:
            return datetime.strptime(date_str, "%d/%m/%Y").date()
    except Exception:
        pass

    # Try YYYY-MM-DD
    try:
        return datetime.fromisoformat(date_str).date()
    except Exception:
        pass

    return None

def _parse_dob(value: str) -> date | None:
    """
    Accept YYYY-MM-DD or DD/MM/YYYY. Return a date or None.
    """
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None

def _parse_exam_date(value: str) -> date | None:
    """
    Same parsing as _parse_dob, used for the exam_date query param.
    """
    raw = unquote_plus(value or "").strip()
    if not raw:
        return None

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    return None


@require_http_methods(["GET"])
def exam_question_image(request, pk: int):
    """Serve a question image stored in the database. Used by the public exam."""
    question = get_object_or_404(
        ExamQuestion.objects.only("id", "image_filename", "image_content_type", "image_data"),
        pk=pk,
    )
    if not question.image_filename or not question.image_data:
        raise Http404("Question image not found.")
    content_type = question.image_content_type or "application/octet-stream"
    if not content_type.startswith("image/"):
        content_type = "application/octet-stream"
    response = HttpResponse(bytes(question.image_data), content_type=content_type)
    response["Cache-Control"] = "public, max-age=86400"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@ensure_csrf_cookie
@csrf_protect
@require_http_methods(["GET", "POST"])
def delegate_exam_run(request: HttpRequest) -> HttpResponse:
    """
    Create (or reuse) an ExamAttempt and present the current question.
    If ?attempt=<id> is provided, we *reuse that attempt* and do not try to
    create a new row. Otherwise, we enforce one attempt per exam-day unless
    a failed attempt has retake_authorised=True.
    """
    # --- common params ---
    examcode = (request.GET.get("examcode") or request.POST.get("examcode") or "").strip()
    if not examcode:
        return HttpResponseBadRequest("Missing examcode.")

    # from .models import Exam, ExamAttempt, ExamAnswer, ExamAttemptAnswer, ExamQuestion, Instructor

    exam = get_object_or_404(Exam, exam_code__iexact=examcode)

    # If we're navigating an existing run, use that attempt immediately
    att_id = (request.GET.get("attempt") or request.POST.get("attempt") or "").strip()
    if att_id:
        attempt = get_object_or_404(ExamAttempt, pk=att_id, exam=exam)
        # short-circuit to the navigation/answering flow below
    else:
        # ---- starting a new run (arrived from rules page) ----
        raw_name = (request.GET.get("name") or request.POST.get("name") or "").strip()
        raw_dob  = (request.GET.get("date_of_birth") or request.POST.get("date_of_birth") or "").strip()
        instructor_id = (request.GET.get("instructor") or request.POST.get("instructor") or "").strip()
        exam_date_str = (request.GET.get("exam_date") or request.POST.get("exam_date") or "").strip()

        delegate_name = _normalise_name(raw_name)
        dob = _parse_dob(raw_dob)
        if dob is None:
            messages.error(request, "Your date of birth couldn’t be read. Please check the format (YYYY-MM-DD or DD/MM/YYYY).")
            return redirect(f"{reverse('delegate_exam_start')}?examcode={exam.exam_code}")

        try:
            exam_date = _parse_dob(exam_date_str) or date.today()
        except Exception:
            exam_date = date.today()

        instructor = None
        if instructor_id:
            try:
                instructor = Personnel.objects.get(pk=instructor_id)
            except Personnel.DoesNotExist:
                instructor = None

        # If the user POSTed “accept rules”, normalise to a clean GET URL
        if request.method == "POST" and "accept_rules" in request.POST:
            return redirect(
                f"{reverse('delegate_exam_run')}?examcode={exam.exam_code}"
                f"&name={delegate_name}&date_of_birth={dob.isoformat()}"
                f"&instructor={instructor.pk if instructor else ''}"
                f"&exam_date={exam_date.isoformat()}"
            )

        # ---- Enforce single attempt per day (unless retake authorised) ----
        existing_qs = ExamAttempt.objects.filter(
            exam=exam,
            exam_date=exam_date,
            instructor=instructor,
            delegate_name__iexact=delegate_name,
            date_of_birth=dob,
        ).order_by("-started_at", "-id")

        latest = existing_qs.first()

        if latest and latest.finished_at:
            passed = bool(getattr(latest, "passed", False))
            if passed:
                messages.success(
                    request,
                    "You have already passed this exam today."
                )
                return redirect(f"{reverse('delegate_exam_start')}?examcode={exam.exam_code}")

            if not getattr(latest, "retake_authorised", False):
                messages.warning(   # ← was messages.error; warning gives amber banner
                    request,
                    "You have already completed this exam today. "
                    "Your instructor must authorise a re-test before you can try again."
                )
                return redirect(f"{reverse('delegate_exam_start')}?examcode={exam.exam_code}")


        if latest and not latest.finished_at:
            attempt = latest
        else:
            # ✅ pick active questions, random order, max 15, and store it on the attempt
            active_ids = list(exam.questions.filter(is_active=True).values_list("id", flat=True))
            if not active_ids:
                active_ids = list(exam.questions.values_list("id", flat=True))  # fallback

            random.shuffle(active_ids)
            question_ids = active_ids[:15]  # ✅ cap

            q_total = len(question_ids)
            seconds_total = q_total * 90  # 90 seconds per question

            started = now()
            expires = started + timedelta(seconds=seconds_total)

            attempt = ExamAttempt.objects.create(
                exam=exam,
                booking=_resolve_booking_for_attempt(exam, delegate_name, dob, instructor, exam_date),
                exam_date=exam_date,
                instructor=instructor,
                delegate_name=delegate_name,
                date_of_birth=dob,
                total_questions=q_total,
                question_order=question_ids,   # ✅ NEW
                started_at=started,
                expires_at=expires,
            )

            # consume retake authorisation if it existed
            if latest and latest.retake_authorised:
                latest.retake_authorised = False
                latest.save(update_fields=["retake_authorised"])

    # ----- From here on we have a valid `attempt` -----

    # If time is up, send them to the finish endpoint
    if _remaining_seconds(attempt) <= 0:
        return redirect(f"{reverse('delegate_exam_finish')}?examcode={exam.exam_code}&attempt={attempt.pk}")

    q_total = attempt.total_questions or exam.questions.count()
    try:
        q_index = int(request.GET.get("q") or request.POST.get("q") or "1")
    except ValueError:
        q_index = 1
    q_index = max(1, min(q_index, q_total if q_total else 1))

    # Persist answer if posted
    if request.method == "POST":
        answer_id = request.POST.get("answer")
        if answer_id:
            try:
                answer = ExamAnswer.objects.select_related("question").get(pk=answer_id)

                # ✅ Guard: prevent saving answers for questions not in this attempt’s set
                if attempt.question_order and answer.question_id not in attempt.question_order:
                    return redirect(
                        f"{reverse('delegate_exam_run')}?examcode={exam.exam_code}&attempt={attempt.pk}&q={q_index}"
                    )

                ExamAttemptAnswer.objects.update_or_create(
                    attempt=attempt,
                    question=answer.question,
                    defaults={"answer": answer, "is_correct": bool(answer.is_correct)},
                )
            except ExamAnswer.DoesNotExist:
                pass

        if "prev" in request.POST and q_index > 1:
            return redirect(f"{reverse('delegate_exam_run')}?examcode={exam.exam_code}&attempt={attempt.pk}&q={q_index-1}")
        if "next" in request.POST and q_index < q_total:
            return redirect(f"{reverse('delegate_exam_run')}?examcode={exam.exam_code}&attempt={attempt.pk}&q={q_index+1}")
        if "finish" in request.POST:
            return redirect(f"{reverse('delegate_exam_review')}?examcode={exam.exam_code}&attempt={attempt.pk}")

    # Fetch question + answers for the current index
    ids = attempt.question_order or list(
        exam.questions.filter(is_active=True)
        .order_by("order", "id")
        .values_list("id", flat=True)[:15]
    )

    if not ids:
        messages.error(request, "This exam has no available questions. Please ask the instructor/admin.")
        return redirect(f"{reverse('delegate_exam_start')}?examcode={exam.exam_code}")

    q_total = len(ids)
    q_index = max(1, min(q_index, q_total))

    question_id = ids[q_index - 1]
    question = ExamQuestion.objects.prefetch_related("answers").get(pk=question_id)
    answers = question.answers.order_by("order", "id")

    prev = ExamAttemptAnswer.objects.filter(attempt=attempt, question=question).first()
    selected_pk = getattr(getattr(prev, "answer", None), "pk", None)

    ctx = {
        "course_type": exam.course_type,
        "exam": exam,
        "attempt": attempt,
        "question": question,
        "answers": answers,
        "q_index": q_index,
        "q_total": q_total,
        "selected_pk": selected_pk,
        "remaining": _remaining_seconds(attempt),
    }
    return render(request, "exam/run.html", ctx)


def delegate_exam_review(request):
    code = (request.GET.get("examcode") or "").upper()
    exam = get_object_or_404(Exam, exam_code=code)
    att_id = request.GET.get("attempt")
    attempt = get_object_or_404(ExamAttempt, pk=att_id, exam=exam)

    # If time is up, jump directly to results
    if _remaining_seconds(attempt) <= 0:
        return redirect(f'{reverse("delegate_exam_finish")}?examcode={exam.exam_code}&attempt={attempt.pk}')

    # ✅ Only show the questions used in this attempt (max 15), in the same order
    ids = attempt.question_order or list(
        exam.questions.filter(is_active=True)
        .order_by("order", "id")
        .values_list("id", flat=True)[:15]
    )

    questions_qs = ExamQuestion.objects.filter(id__in=ids).prefetch_related("answers")
    q_by_id = {q.id: q for q in questions_qs}
    questions = [q_by_id[i] for i in ids if i in q_by_id]
    chosen = {
        aa.question_id: aa for aa in attempt.answers.select_related("answer", "question")
    }

    # Submit from the review page goes to final scoring
    if request.method == "POST":
        return redirect(f'{reverse("delegate_exam_finish")}?examcode={exam.exam_code}&attempt={attempt.pk}')

    ctx = {
        "exam": exam,
        "course_type": exam.course_type,
        "attempt": attempt,
        "questions": questions,
        "chosen": chosen,
        "remaining": _remaining_seconds(attempt),
    }
    return render(request, "exam/review.html", ctx)


def _score_attempt(attempt: ExamAttempt):
    ans = attempt.answers.filter(is_correct=True)
    if attempt.question_order:
        ans = ans.filter(question_id__in=attempt.question_order)
    correct = ans.count()

    attempt.score_correct = correct
    attempt.total_questions = len(attempt.question_order) if attempt.question_order else attempt.exam.questions.count()

    pct = attempt.exam.pass_mark_percent or 80
    required = ceil(attempt.total_questions * pct / 100.0)

    viva_pct = attempt.exam.viva_pass_percent if getattr(attempt.exam, "allow_viva", False) else None
    viva_required = ceil(attempt.total_questions * viva_pct / 100.0) if viva_pct else None

    attempt.passed = (correct >= required)
    attempt.viva_eligible = (not attempt.passed) and (viva_required is not None) and (correct >= viva_required)

    if not attempt.finished_at:
        attempt.finished_at = timezone.now()

    attempt.save()
    recompute_outcomes_for_exam_attempt(attempt)
    return required, viva_required

def delegate_exam_finish(request):
    code = (request.GET.get("examcode") or "").upper()
    exam = get_object_or_404(Exam, exam_code=code)
    att_id = request.GET.get("attempt")
    attempt = get_object_or_404(ExamAttempt, pk=att_id, exam=exam)

    # If time’s up, ensure it’s marked finished and scored
    required, viva_required = _score_attempt(attempt)

    ctx = {
        "exam": exam,
        "course_type": exam.course_type,
        "attempt": attempt,
        "required_correct": required,
        "viva_required": viva_required,
    }
    return render(request, "exam/finish.html", ctx)

def privacy_notices(request):
    """
    Public privacy notices covering Registers, Exams and Feedback.
    """
    return render(request, "legal/privacy.html")