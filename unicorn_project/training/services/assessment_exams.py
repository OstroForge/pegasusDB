"""
Exam rows on the instructor assessment matrix and outcome auto-pass rules.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from django.db.models import Q

from ..models import (
    Booking,
    BookingDay,
    CompetencyAssessment,
    CourseCompetency,
    DelegateRegister,
    Exam,
    ExamAttempt,
)


def register_matches_attempt(register, attempt) -> bool:
    reg_name = (getattr(register, "name", "") or "").strip().lower()
    att_name = (getattr(attempt, "delegate_name", "") or "").strip().lower()
    if not reg_name or reg_name != att_name:
        return False
    reg_dob = getattr(register, "date_of_birth", None)
    att_dob = getattr(attempt, "date_of_birth", None)
    if reg_dob and att_dob:
        return reg_dob == att_dob
    return True


def exam_attempt_result_code(attempt: Optional[ExamAttempt]) -> str:
    """
    Normalised result for matrix / auto-pass logic.
    Returns one of: pass, viva, fail, in_progress, pending.
    """
    if not attempt:
        return "pending"
    if not attempt.finished_at:
        return "in_progress"
    if getattr(attempt, "viva_decided_at", None):
        return "pass" if attempt.passed else "fail"
    if attempt.passed:
        return "pass"
    if attempt.viva_eligible:
        return "viva"
    return "fail"


def exam_result_display(code: str) -> Tuple[str, str]:
    return {
        "pass": ("Pass", "success"),
        "viva": ("Viva", "warning"),
        "fail": ("Fail", "danger"),
        "in_progress": ("In progress", "info"),
        "pending": ("—", "secondary"),
    }.get(code, ("—", "secondary"))


def attempts_for_booking(booking: Booking) -> List[ExamAttempt]:
    """Attempts for this booking, including those taken with the day's instructor."""
    dates = list(
        BookingDay.objects.filter(booking=booking).values_list("date", flat=True)
    )
    instructor_ids = set(
        BookingDay.objects.filter(booking=booking)
        .exclude(instructor_id__isnull=True)
        .values_list("instructor_id", flat=True)
    )
    if booking.instructor_id:
        instructor_ids.add(booking.instructor_id)

    scope = Q(booking=booking)
    if dates and instructor_ids:
        register_match = Q()
        seen = set()
        for name, dob in (
            DelegateRegister.objects.filter(booking_day__booking=booking)
            .values_list("name", "date_of_birth")
            .distinct()
        ):
            key = ((name or "").casefold(), dob)
            if key in seen:
                continue
            seen.add(key)
            register_match |= Q(delegate_name__iexact=name, date_of_birth=dob)
        if register_match:
            scope |= Q(exam_date__in=dates, instructor_id__in=instructor_ids) & register_match

    return list(
        ExamAttempt.objects.select_related("exam", "instructor")
        .filter(exam__course_type=booking.course_type)
        .filter(scope)
        .order_by("exam__sequence", "exam_date", "started_at", "pk")
    )


def pick_best_attempt(attempts: List[ExamAttempt]) -> Optional[ExamAttempt]:
    if not attempts:
        return None
    finished = [a for a in attempts if a.finished_at]
    if finished:
        return max(finished, key=lambda a: (a.finished_at, a.pk))
    return max(attempts, key=lambda a: (a.started_at, a.pk))


def course_exams_for_booking(booking: Booking) -> List[Exam]:
    return list(
        Exam.objects.filter(course_type=booking.course_type).order_by("sequence", "id")
    )


def build_assessment_exam_matrix(booking: Booking, delegates) -> Tuple[List[Exam], List[dict]]:
    """
    Build exam rows for the assessment matrix template.

    Returns (course_exams, rows) where each row is:
      {"exam": Exam, "cells": [{"code", "label", "badge_class", "attempt_id"}, ...]}
    """
    course_exams = course_exams_for_booking(booking)
    if not course_exams:
        return [], []

    by_exam: Dict[int, List[ExamAttempt]] = {}
    for att in attempts_for_booking(booking):
        by_exam.setdefault(att.exam_id, []).append(att)

    rows = []
    for exam in course_exams:
        cells = []
        for reg in delegates:
            candidates = [
                a for a in by_exam.get(exam.id, [])
                if register_matches_attempt(reg, a)
            ]
            best = pick_best_attempt(candidates)
            code = exam_attempt_result_code(best)
            label, badge = exam_result_display(code)
            cells.append({
                "code": code,
                "label": label,
                "badge_class": badge,
                "attempt_id": best.pk if best else None,
            })
        rows.append({"exam": exam, "cells": cells})
    return course_exams, rows


def _required_competency_ids_for_booking(booking: Booking) -> Tuple[List[int], int, int]:
    mandatory_ids = list(
        CourseCompetency.objects.filter(
            course_type=booking.course_type,
            is_optional=False,
            is_active=True,
        ).order_by("sort_order", "name", "id").values_list("id", flat=True)
    )
    required_optional_count = int(
        getattr(booking.course_type, "optional_modules_required", 0) or 0
    )
    selected_optional = list(
        booking.optional_modules.filter(
            course_type=booking.course_type,
            is_optional=True,
            is_active=True,
        ).order_by("sort_order", "name", "id")[: max(required_optional_count, 0)]
    )
    comp_ids = mandatory_ids + [c.id for c in selected_optional]
    return comp_ids, required_optional_count, len(selected_optional)


def delegate_competencies_complete(booking: Booking, register) -> bool:
    comp_ids, required_optional_count, selected_optional_count = (
        _required_competency_ids_for_booking(booking)
    )
    if required_optional_count and selected_optional_count < required_optional_count:
        return False
    if not comp_ids:
        return True
    for cid in comp_ids:
        ca = CompetencyAssessment.objects.filter(
            register=register,
            course_competency_id=cid,
        ).first()
        if not ca or ca.level not in ("c", "e"):
            return False
    return True


def delegate_exams_all_passed(booking: Booking, register) -> bool:
    course_exams = course_exams_for_booking(booking)
    if not course_exams:
        return True

    _, rows = build_assessment_exam_matrix(booking, [register])
    for row in rows:
        if not row["cells"]:
            return False
        if row["cells"][0]["code"] != "pass":
            return False
    return True


def _registers_for_same_person(booking: Booking, register):
    q = DelegateRegister.objects.filter(
        booking_day__booking=booking,
        name__iexact=(register.name or "").strip(),
    )
    if register.date_of_birth:
        q = q.filter(date_of_birth=register.date_of_birth)
    return q


def recompute_delegate_outcome(booking: Booking, register) -> str:
    """
    Set outcome to pass when all competencies are achieved and every exam is pass.
    Preserves instructor-set DNF / Fail. Otherwise sets pending or pass.
    Completed bookings are frozen — never recompute.
    """
    if booking.status == "completed":
        return (register.outcome or "pending").lower()

    current = (register.outcome or "pending").lower()
    if current in ("dnf", "fail"):
        return current

    if delegate_competencies_complete(booking, register) and delegate_exams_all_passed(
        booking, register
    ):
        new_outcome = "pass"
    else:
        new_outcome = "pending"

    if current != new_outcome:
        _registers_for_same_person(booking, register).update(outcome=new_outcome)
    return new_outcome


def recompute_outcomes_for_exam_attempt(attempt: ExamAttempt) -> None:
    """After an exam is scored or a viva decision is saved, refresh delegate outcomes."""
    booking = attempt.booking
    if not booking:
        day = (
            BookingDay.objects.select_related("booking")
            .filter(
                booking__course_type=attempt.exam.course_type,
                date=attempt.exam_date,
            )
            .filter(
                Q(instructor=attempt.instructor)
                | Q(instructor__isnull=True, booking__instructor=attempt.instructor)
            )
            .order_by("date")
            .first()
        )
        booking = day.booking if day else None
    if not booking:
        return

    if booking.status == "completed":
        return

    regs = DelegateRegister.objects.filter(
        booking_day__booking=booking,
        name__iexact=(attempt.delegate_name or "").strip(),
    )
    if attempt.date_of_birth:
        regs = regs.filter(date_of_birth=attempt.date_of_birth)

    seen = set()
    for reg in regs.order_by("id"):
        key = (
            (reg.name or "").strip().lower(),
            reg.date_of_birth,
        )
        if key in seen:
            continue
        seen.add(key)
        recompute_delegate_outcome(booking, reg)
