from datetime import date

from django import forms
from django.contrib import messages
from django.db import transaction
from django.db.models import Q
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from .models import Booking, Business, CourseCompetency, CourseType, DelegateRegister, Personnel, TrainingLocation
from .services.matrix_import import (
    add_delegates_to_booking,
    delegate_name_key,
    ensure_business,
    ensure_location,
    import_assessment_matrix,
    reference_with_course_code,
)
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

    def __init__(self, payload, matching_bookings=None, *args, **kwargs):
        self.payload = payload
        self.matching_bookings = list(matching_bookings or [])
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
        self.fields["existing_booking"] = forms.ChoiceField(
            required=False,
            choices=[("", "")] + [
                (str(booking.pk), f"{booking.course_reference} — {booking.business.name}")
                for booking in self.matching_bookings
            ],
        )
        if not self.is_bound and self.matching_bookings:
            self.fields["existing_booking"].initial = str(self.matching_bookings[0].pk)

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
        match_ids = {str(booking.pk) for booking in self.matching_bookings}
        target = (self.data.get("import_target") or "").strip() if self.is_bound else ""
        if target not in {"add", "new"}:
            target = "add" if match_ids else "new"
        if target == "add" and not match_ids:
            target = "new"
        chosen = (cleaned.get("existing_booking") or "").strip()
        if target == "add" and not chosen and len(self.matching_bookings) == 1:
            chosen = str(self.matching_bookings[0].pk)
        if target == "add" and chosen not in match_ids:
            self.add_error("existing_booking", "Choose the booking to add these names to.")
        cleaned["import_target"] = target
        cleaned["existing_booking"] = chosen

        existing_names = set()
        if target == "add" and chosen in match_ids:
            existing_names = {
                delegate_name_key(name)
                for name in DelegateRegister.objects.filter(
                    booking_day__booking_id=chosen
                ).values_list("name", flat=True)
            }

        if target == "new":
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
            if target == "add" and delegate_name_key(person["name"]) in existing_names:
                continue
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

        if target == "new":
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
        Booking.objects.filter(course_type_id=course_type_id, instructor_id=instructor_id)
        .filter(Q(course_date=course_date) | Q(days__date=course_date))
        .exclude(status="cancelled")
        .distinct()
        .select_related("business", "course_type")
        .prefetch_related("days__registers")
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

    if request.method == "POST":
        matches = _matching_bookings(
            request.POST.get("course_type"),
            request.POST.get("course_date"),
            request.POST.get("instructor"),
        )
        form_initial = None
    else:
        form_initial = _initial_from_payload(payload)
        matches = _matching_bookings(
            form_initial.get("course_type"),
            form_initial.get("course_date"),
            form_initial.get("instructor"),
        )
    form = MatrixImportConfirmForm(
        payload,
        matching_bookings=matches,
        data=request.POST if request.method == "POST" else None,
        initial=form_initial,
    )
    if request.method == "POST" and form.is_valid():
        course_type = form.cleaned_data["course_type"]
        course_date = form.cleaned_data["course_date"]
        instructor = form.cleaned_data["instructor"]
        people = []
        for index, person in enumerate(payload["delegates"]):
            people.append({
                **person,
                "name": person["name"][:100],
                "date_of_birth": form.cleaned_data.get(f"dob_{index}"),
                "job_title": form.cleaned_data.get(f"job_{index}") or "",
                "employee_id": (form.cleaned_data.get(f"emp_{index}") or "").strip(),
            })
        if form.cleaned_data.get("import_target") == "add":
            booking = Booking.objects.get(pk=form.cleaned_data["existing_booking"])
            with transaction.atomic():
                added = add_delegates_to_booking(
                    booking=booking,
                    instructor=instructor,
                    course_date=course_date,
                    delegates=people,
                    skills=payload["skills"],
                )
            request.session.pop(SESSION_KEY, None)
            if added:
                messages.success(
                    request,
                    f"Added {len(added)} new delegates to {booking.course_reference}.",
                )
            else:
                messages.info(
                    request,
                    f"Everyone in this spreadsheet is already on {booking.course_reference}.",
                )
            return redirect("admin_booking_edit", pk=booking.pk)

        business_choice = form.cleaned_data["business"]
        location_choice = form.cleaned_data.get("location") or NEW
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

    similar = list(matches)
    selected_id = (request.POST.get("existing_booking") or "").strip() if form.is_bound else ""
    if not selected_id and similar:
        selected_id = str(similar[0].pk)
    names_by_booking = {}
    for booking in similar:
        names_by_booking[str(booking.pk)] = sorted({
            delegate_name_key(reg.name)
            for day in booking.days.all()
            for reg in day.registers.all()
        })
    selected_names = set(names_by_booking.get(selected_id, []))

    rows = []
    for index, person in enumerate(payload["delegates"]):
        name_key = delegate_name_key(person["name"])
        rows.append({
            "index": index,
            "name": person["name"],
            "name_key": name_key,
            "already": name_key in selected_names,
            "result": _result_label(person),
            "ticked": sum(1 for value in person["ticks"].values() if value),
            "skills": len(person["ticks"]),
            "dob": form[f"dob_{index}"],
            "job": form[f"job_{index}"],
            "emp": form[f"emp_{index}"],
        })

    course_type_id = None
    if form.is_bound:
        course_type_id = request.POST.get("course_type")
    else:
        course_type_id = form.initial.get("course_type")

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
        "names_by_booking": names_by_booking,
        "retired": retired,
        "locations": _location_map(),
        "new_value": NEW,
    })
