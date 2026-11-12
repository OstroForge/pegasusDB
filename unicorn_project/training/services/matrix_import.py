"""Create a completed booking from a checked assessment-matrix spreadsheet."""
from __future__ import annotations

from datetime import datetime, time

from django.db import transaction
from django.utils import timezone

from ..models import (
    AssessmentLevel,
    Booking,
    BookingDay,
    Business,
    CompetencyAssessment,
    CourseCompetency,
    CourseOutcome,
    DelegateRegister,
    Invoice,
    TrainingLocation,
)


def _norm(value: str) -> str:
    return " ".join((value or "").replace("\n", " ").split()).casefold()


def reference_with_course_code(course_type, reference: str) -> str:
    """Put the course short code at the start of a typed reference."""
    code = ((getattr(course_type, "code", None) or "COURSE").strip() or "COURSE").upper()
    reference = " ".join((reference or "").split())
    if not reference:
        return ""
    prefix = f"{code}-"
    if reference.upper() == code:
        return code
    if reference.upper().startswith(prefix):
        return prefix + reference[len(prefix):]
    return f"{prefix}{reference}"


def ensure_business(name: str) -> Business:
    cleaned = " ".join((name or "").split())
    existing = Business.objects.filter(name__iexact=cleaned).first()
    if existing:
        return existing
    return Business.objects.create(name=cleaned)


def ensure_location(business: Business, name: str) -> TrainingLocation:
    cleaned = " ".join((name or "").split())
    existing = business.training_locations.filter(name__iexact=cleaned).first()
    if existing:
        if not existing.is_active:
            existing.is_active = True
            existing.save(update_fields=["is_active"])
        return existing
    return TrainingLocation.objects.create(business=business, name=cleaned, is_active=True)


def competency_for_skill(course_type, skill: str, *, cache: dict) -> CourseCompetency:
    key = _norm(skill)
    if key in cache:
        return cache[key]
    for item in CourseCompetency.objects.filter(course_type=course_type):
        cache[_norm(item.name)] = item
    found = cache.get(key)
    if found:
        return found
    order = CourseCompetency.objects.filter(course_type=course_type).order_by("-sort_order").values_list("sort_order", flat=True).first() or 0
    created = CourseCompetency.objects.create(
        course_type=course_type,
        name=" ".join(skill.replace("\n", " ").split()),
        sort_order=order + 10,
        is_optional=False,
        is_active=False,
        description="Retired. Present on older assessment matrices and no longer assessed.",
    )
    cache[key] = created
    return created


def _outcome(person: dict) -> str:
    if person.get("did_not_finish"):
        return CourseOutcome.DNF
    if person.get("passed") is True:
        return CourseOutcome.PASS
    if person.get("passed") is False:
        return CourseOutcome.FAIL
    return CourseOutcome.PENDING


@transaction.atomic
def import_assessment_matrix(*, course_type, course_date, instructor, business, location, delegates, skills, invoice_amount=None, mark_invoice_paid=False, course_reference=""):
    """
    Create one completed booking, a single course day, the registers, and the ticks.
    Does not send booking notifications.
    """
    booking = Booking(
        business=business,
        training_location=location,
        course_type=course_type,
        instructor=instructor,
        course_date=course_date,
        course_fee=course_type.default_course_fee,
        instructor_fee=invoice_amount if invoice_amount is not None else course_type.default_instructor_fee,
        allow_mileage_claim=False,
        allow_accommodation=False,
        status="completed",
        course_registers_status="complete",
        assessment_matrix_status="completed",
        booking_notes="Imported from an assessment matrix spreadsheet.",
        is_imported=True,
        course_reference=reference_with_course_code(course_type, course_reference),
        contact_name=((location.contact_name or "") if location else "")[:200],
        telephone=((location.telephone or "") if location else "")[:50],
        email=(location.email or "") if location else "",
    )
    completed_at = timezone.make_aware(datetime.combine(course_date, time(17, 0)))
    booking.date_completed = completed_at
    booking.save()

    day = BookingDay.objects.create(
        booking=booking,
        date=course_date,
        instructor=instructor,
    )

    cache: dict = {}
    for skill in skills:
        competency_for_skill(course_type, skill, cache=cache)

    for person in delegates:
        register = DelegateRegister(
            name=person["name"],
            date_of_birth=person["date_of_birth"],
            job_title=person["job_title"],
            employee_id=person.get("employee_id") or "",
            date=course_date,
            instructor=instructor,
            health_status=DelegateRegister.HealthStatus.FIT,
            outcome=_outcome(person),
            notes="Imported from an assessment matrix spreadsheet.",
            booking_day=day,
        )
        register.save()
        for skill, ticked in (person.get("ticks") or {}).items():
            competency = competency_for_skill(course_type, skill, cache=cache)
            CompetencyAssessment.objects.create(
                register=register,
                course_competency=competency,
                level=AssessmentLevel.COMPETENT if ticked else AssessmentLevel.NOT_ASSESSED,
                is_locked=False,
                source_note="Imported from assessment matrix",
                assessed_by=instructor,
                assessed_at=completed_at,
            )

    if mark_invoice_paid:
        Invoice.objects.create(
            booking=booking,
            instructor=instructor,
            status="paid",
            base_fee_override=booking.instructor_fee,
            invoice_date=course_date,
            date_sent=completed_at,
            account_name=(instructor.name_on_account or "")[:200],
            sort_code=(instructor.bank_sort_code or "")[:20],
            account_number=(instructor.bank_account_number or "")[:30],
            admin_comment="Imported from an assessment matrix spreadsheet.",
        )

    return booking
