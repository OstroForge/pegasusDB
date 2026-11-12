from datetime import date

from django import forms
from django.contrib import messages
from django.db import transaction
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from .models import Booking, Business, CourseCompetency, CourseType, Personnel, TrainingLocation
from .services.matrix_import import ensure_business, ensure_location, import_assessment_matrix, reference_with_course_code
from .utils.assessment_matrix_sheet import course_code_from_title, read_assessment_matrix
from .views_admin import admin_required

SESSION_KEY = "assessment_matrix_import"
NEW = "__new__"


def _sheet_payload(sheet) -> dict:
    return {
        "title": sheet.title,
        "course_date": sheet.course_date.date().isoformat() if sheet.course_date else "",
        "location": sheet.location,
        "instructor": sheet.instructor,
        "skills": sheet.skills,
        "delegates": [
            {
                "name": person.name,
                "did_not_finish": person.did_not_finish,
                "passed": person.passed,
                "ticks": person.ticks,
            }
            for person in sheet.delegates
        ],
    }


def _result_label(person: dict) -> str:
    if person.get("did_not_finish"):
        return "DNF"
    if person.get("passed") is True:
        return "Pass"
    if person.get("passed") is False:
        return "Not passed"
    return "No pass mark"


def _norm(value: str) -> str:
    return " ".join((value or "").split()).casefold()


class MatrixImportConfirmForm(forms.Form):
    course_type = forms.ModelChoiceField(
        queryset=CourseType.objects.none(),
        label="Course",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    course_date = forms.DateField(
        label="Course date",
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
    )
    instructor = forms.ModelChoiceField(
        queryset=Personnel.objects.none(),
        label="Instructor",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    business = forms.ChoiceField(
        label="Business",
        widget=forms.Select(attrs={"class": "form-select", "id": "id_business"}),
    )
    new_business_name = forms.CharField(
        required=False,
        label="New business name",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    location = forms.ChoiceField(
        required=False,
        label="Training location",
        widget=forms.Select(attrs={"class": "form-select", "id": "id_location"}),
    )
    new_location_name = forms.CharField(
        required=False,
        label="New location name",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    default_job_title = forms.CharField(
        required=False,
        label="Job title for anyone left blank",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Optional"}),
    )
    unknown_dob = forms.BooleanField(
        required=False,
        label="Use 01/01/1970 for anyone without a date of birth",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    invoice_amount = forms.DecimalField(
        required=False,
        min_value=0,
        max_digits=12,
        decimal_places=2,
        label="Invoice amount",
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "step": "0.01",
            "min": "0",
            "id": "id_invoice_amount",
        }),
    )
    invoice_paid = forms.BooleanField(
        required=False,
        label="Mark the invoice as paid",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    course_reference = forms.CharField(
        required=False,
        max_length=40,
        label="Course reference",
        widget=forms.TextInput(attrs={"class": "form-control", "maxlength": "40"}),
    )

    def __init__(self, payload, *args, **kwargs):
        self.payload = payload
        super().__init__(*args, **kwargs)
        self.fields["course_type"].queryset = CourseType.objects.order_by("name")
        self.fields["instructor"].queryset = Personnel.objects.order_by("name")
        business_choices = [(NEW, "Create a new business")]
        business_choices += [
            (str(item.pk), item.name)
            for item in Business.objects.order_by("name")
        ]
        self.fields["business"].choices = business_choices
        location_choices = [(NEW, "Create a new location")]
        location_choices += [
            (str(item.pk), f"{item.name} — {item.business.name}")
            for item in TrainingLocation.objects.select_related("business").order_by("business__name", "name")
        ]
        self.fields["location"].choices = location_choices

        for index, person in enumerate(payload["delegates"]):
            self.fields[f"dob_{index}"] = forms.DateField(
                required=False,
                label="Date of birth",
                widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
            )
            self.fields[f"job_{index}"] = forms.CharField(
                required=False,
                label="Job title",
                max_length=100,
                widget=forms.TextInput(attrs={"class": "form-control"}),
            )
            self.fields[f"emp_{index}"] = forms.CharField(
                required=False,
                label="Employee ID",
                max_length=50,
                widget=forms.TextInput(attrs={"class": "form-control"}),
            )

    def clean(self):
        cleaned = super().clean()
        business_choice = cleaned.get("business")
        if business_choice == NEW and not (cleaned.get("new_business_name") or "").strip():
            self.add_error("new_business_name", "Enter the business name.")
        location_choice = cleaned.get("location") or NEW
        if business_choice == NEW:
            location_choice = NEW
            cleaned["location"] = NEW
        if location_choice == NEW and not (cleaned.get("new_location_name") or "").strip():
            self.add_error("new_location_name", "Enter the location name.")
        if business_choice and business_choice != NEW and location_choice not in ("", NEW):
            location = TrainingLocation.objects.filter(pk=location_choice, business_id=business_choice).first()
            if not location:
                self.add_error("location", "Choose a location that belongs to this business, or create a new one.")

        default_job = (cleaned.get("default_job_title") or "").strip()
        use_unknown_dob = bool(cleaned.get("unknown_dob"))
        today = date.today()
        for index, person in enumerate(self.payload["delegates"]):
            field_name = f"dob_{index}"
            dob = cleaned.get(field_name)
            raw_dob = (self.data.get(field_name) or "").strip() if self.is_bound else ""
            if not dob and not raw_dob and use_unknown_dob:
                dob = date(1970, 1, 1)
                cleaned[field_name] = dob
            if not dob and not raw_dob:
                self.add_error(
                    field_name,
                    f"Enter a date of birth for {person['name']}, or tick the box to use 01/01/1970.",
                )
            elif dob and (dob >= today or dob.year < 1900):
                self.add_error(field_name, "Enter a real date of birth.")
            job = (cleaned.get(f"job_{index}") or "").strip() or default_job
            if not job:
                self.add_error(f"job_{index}", f"Enter a job title for {person['name']}, or a job title for anyone left blank.")
            cleaned[f"job_{index}"] = job

        reference = reference_with_course_code(cleaned.get("course_type"), cleaned.get("course_reference") or "")
        cleaned["course_reference"] = reference
        if len(reference) > 40:
            self.add_error("course_reference", "That reference is too long once the course code is added.")
        elif reference and Booking.objects.filter(course_reference__iexact=reference).exists():
            self.add_error("course_reference", "That course reference is already in use.")

        if cleaned.get("invoice_paid") and cleaned.get("invoice_amount") is None:
            course_type = cleaned.get("course_type")
            default_fee = getattr(course_type, "default_instructor_fee", None) if course_type else None
            if default_fee is None:
                self.add_error("invoice_amount", "Enter the invoice amount to mark it as paid.")
            else:
                cleaned["invoice_amount"] = default_fee
        return cleaned


def _matching_bookings(course_type_id, course_date, instructor_id):
    if not (course_type_id and course_date and instructor_id):
        return Booking.objects.none()
    return (
        Booking.objects.filter(
            course_type_id=course_type_id,
            course_date=course_date,
            instructor_id=instructor_id,
        )
        .exclude(status="cancelled")
        .select_related("business", "course_type")
        .order_by("course_reference")
    )


def _location_map():
    grouped: dict[str, list] = {}
    for item in TrainingLocation.objects.filter(is_active=True).order_by("name"):
        grouped.setdefault(str(item.business_id), []).append({"id": str(item.pk), "name": item.name})
    return grouped


def _initial_from_payload(payload):
    code = course_code_from_title(payload.get("title") or "")
    course_type = CourseType.objects.filter(code__iexact=code).first() if code else None
    instructor = Personnel.objects.filter(name__iexact=payload.get("instructor") or "").first()
    location_name = payload.get("location") or ""
    business = Business.objects.filter(name__iexact=location_name).first() if location_name else None
    initial = {
        "course_type": course_type.pk if course_type else None,
        "course_date": payload.get("course_date") or None,
        "instructor": instructor.pk if instructor else None,
        "business": str(business.pk) if business else NEW,
        "new_business_name": "" if business else location_name,
        "location": NEW,
        "new_location_name": location_name,
        "invoice_amount": course_type.default_instructor_fee if course_type else None,
    }
    if business:
        location = business.training_locations.filter(name__iexact=location_name, is_active=True).first()
        if location:
            initial["location"] = str(location.pk)
            initial["new_location_name"] = ""
    return initial


@admin_required
@require_http_methods(["GET", "POST"])
def matrix_import_upload(request):
    error = ""
    if request.method == "POST":
        upload = request.FILES.get("spreadsheet")
        if not upload:
            error = "Choose an assessment matrix spreadsheet."
        elif not upload.name.lower().endswith(".xlsx"):
            error = "The file needs to be an Excel .xlsx workbook."
        elif upload.size > 5 * 1024 * 1024:
            error = "That file is larger than 5 MB."
        else:
            try:
                sheet = read_assessment_matrix(upload)
            except (OSError, ValueError, KeyError):
                error = "That workbook could not be read as an assessment matrix."
            else:
                if not sheet.delegates:
                    error = "No delegate names were found in that spreadsheet."
                else:
                    request.session[SESSION_KEY] = _sheet_payload(sheet)
                    request.session.modified = True
                    return redirect("admin_matrix_import_confirm")
    return render(request, "admin/matrix_import_upload.html", {"title": "Import assessment matrix", "error": error})


@admin_required
@require_http_methods(["GET", "POST"])
def matrix_import_confirm(request):
    payload = request.session.get(SESSION_KEY)
    if not payload:
        messages.warning(request, "Upload an assessment matrix to import.")
        return redirect("admin_matrix_import")

    if request.method == "POST" and request.POST.get("cancel") == "1":
        request.session.pop(SESSION_KEY, None)
        return redirect("admin_matrix_import")

    form = MatrixImportConfirmForm(
        payload,
        data=request.POST if request.method == "POST" else None,
        initial=None if request.method == "POST" else _initial_from_payload(payload),
    )
    if request.method == "POST" and form.is_valid():
        course_type = form.cleaned_data["course_type"]
        course_date = form.cleaned_data["course_date"]
        instructor = form.cleaned_data["instructor"]
        already = list(_matching_bookings(course_type.pk, course_date, instructor.pk))
        if already and request.POST.get("confirm_duplicate") != "1":
            form.add_error(
                None,
                "A booking already exists for this course, date and instructor. Tick the confirmation box to import another one.",
            )
        else:
            business_choice = form.cleaned_data["business"]
            location_choice = form.cleaned_data.get("location") or NEW
            people = []
            for index, person in enumerate(payload["delegates"]):
                people.append({
                    **person,
                    "name": person["name"][:100],
                    "date_of_birth": form.cleaned_data[f"dob_{index}"],
                    "job_title": form.cleaned_data[f"job_{index}"],
                    "employee_id": (form.cleaned_data.get(f"emp_{index}") or "").strip(),
                })
            with transaction.atomic():
                if business_choice == NEW:
                    business = ensure_business(form.cleaned_data["new_business_name"])
                else:
                    business = Business.objects.get(pk=business_choice)
                if location_choice == NEW:
                    location = ensure_location(business, form.cleaned_data["new_location_name"])
                else:
                    location = TrainingLocation.objects.get(pk=location_choice, business=business)
                booking = import_assessment_matrix(
                    course_type=course_type,
                    course_date=course_date,
                    instructor=instructor,
                    business=business,
                    location=location,
                    delegates=people,
                    skills=payload["skills"],
                    invoice_amount=form.cleaned_data.get("invoice_amount"),
                    mark_invoice_paid=bool(form.cleaned_data.get("invoice_paid")),
                    course_reference=form.cleaned_data.get("course_reference") or "",
                )
            request.session.pop(SESSION_KEY, None)
            messages.success(
                request,
                f"Imported {len(people)} delegates onto {booking.course_reference}.",
            )
            return redirect("admin_booking_edit", pk=booking.pk)

    rows = []
    for index, person in enumerate(payload["delegates"]):
        rows.append({
            "index": index,
            "name": person["name"],
            "result": _result_label(person),
            "ticked": sum(1 for value in person["ticks"].values() if value),
            "skills": len(person["ticks"]),
            "dob": form[f"dob_{index}"],
            "job": form[f"job_{index}"],
            "emp": form[f"emp_{index}"],
        })

    course_type_id = None
    course_date = payload.get("course_date")
    instructor_id = None
    if form.is_bound:
        course_type_id = request.POST.get("course_type")
        course_date = request.POST.get("course_date") or course_date
        instructor_id = request.POST.get("instructor")
    else:
        course_type_id = form.initial.get("course_type")
        instructor_id = form.initial.get("instructor")
    similar = list(_matching_bookings(course_type_id, course_date, instructor_id)[:8])

    known = {_norm(name) for name in CourseCompetency.objects.filter(
        course_type_id=course_type_id
    ).values_list("name", flat=True)} if course_type_id else set()
    retired = [skill for skill in payload["skills"] if _norm(skill) not in known]

    return render(request, "admin/matrix_import_confirm.html", {
        "title": "Check assessment matrix",
        "form": form,
        "payload": payload,
        "rows": rows,
        "similar": similar,
        "retired": retired,
        "locations": _location_map(),
        "new_value": NEW,
    })
