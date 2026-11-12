# training/forms_exam.py
from datetime import date

from django import forms
from django.utils import timezone

from .models import Personnel
from .utils.names import title_case_person_name


class DelegateExamStartForm(forms.Form):
    exam_code = forms.CharField(required=False, widget=forms.HiddenInput())
    first_name = forms.CharField(
        label="First name",
        max_length=50,
        error_messages={"required": "Please enter your first name."},
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "autocomplete": "given-name",
                "id": "id_first_name",
            }
        ),
    )
    surname = forms.CharField(
        label="Surname",
        max_length=50,
        error_messages={"required": "Please enter your surname."},
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "autocomplete": "family-name",
                "id": "id_surname",
            }
        ),
    )
    date_of_birth = forms.DateField(required=False, widget=forms.HiddenInput())
    dob_day = forms.CharField(
        label="Day",
        max_length=2,
        required=False,
        widget=forms.TextInput(
            attrs={
                "inputmode": "numeric",
                "pattern": "[0-9]*",
                "maxlength": "2",
                "placeholder": "DD",
                "autocomplete": "bday-day",
                "class": "form-control text-center",
                "id": "dob-day",
            }
        ),
    )
    dob_month = forms.CharField(
        label="Month",
        max_length=2,
        required=False,
        widget=forms.TextInput(
            attrs={
                "inputmode": "numeric",
                "pattern": "[0-9]*",
                "maxlength": "2",
                "placeholder": "MM",
                "autocomplete": "bday-month",
                "class": "form-control text-center",
                "id": "dob-month",
            }
        ),
    )
    dob_year = forms.CharField(
        label="Year",
        max_length=4,
        required=False,
        widget=forms.TextInput(
            attrs={
                "inputmode": "numeric",
                "pattern": "[0-9]*",
                "maxlength": "4",
                "placeholder": "YYYY",
                "autocomplete": "bday-year",
                "class": "form-control text-center",
                "id": "dob-year",
            }
        ),
    )
    exam_date = forms.DateField(required=False, widget=forms.HiddenInput())
    exam_date_day = forms.CharField(
        label="Day",
        max_length=2,
        required=False,
        widget=forms.TextInput(
            attrs={
                "inputmode": "numeric",
                "pattern": "[0-9]*",
                "maxlength": "2",
                "placeholder": "DD",
                "autocomplete": "off",
                "class": "form-control text-center",
                "id": "exam-date-day",
            }
        ),
    )
    exam_date_month = forms.CharField(
        label="Month",
        max_length=2,
        required=False,
        widget=forms.TextInput(
            attrs={
                "inputmode": "numeric",
                "pattern": "[0-9]*",
                "maxlength": "2",
                "placeholder": "MM",
                "autocomplete": "off",
                "class": "form-control text-center",
                "id": "exam-date-month",
            }
        ),
    )
    exam_date_year = forms.CharField(
        label="Year",
        max_length=4,
        required=False,
        widget=forms.TextInput(
            attrs={
                "inputmode": "numeric",
                "pattern": "[0-9]*",
                "maxlength": "4",
                "placeholder": "YYYY",
                "autocomplete": "off",
                "class": "form-control text-center",
                "id": "exam-date-year",
            }
        ),
    )
    instructor = forms.ModelChoiceField(
        label="Instructor",
        queryset=Personnel.objects.none(),
        required=True,
        error_messages={"required": "Please select your instructor."},
    )

    def __init__(self, *args, instructors=None, show_exam_date=False, **kwargs):
        self.show_exam_date = show_exam_date
        super().__init__(*args, **kwargs)

        if not self.is_bound:
            self.initial.setdefault("exam_date", timezone.localdate())
            if show_exam_date:
                self._set_date_partials("exam_date", self.initial.get("exam_date"))
            dob = self.initial.get("date_of_birth")
            if dob:
                self._set_date_partials("dob", dob)

        instructors = instructors or []
        if len(instructors) == 1:
            self.fields["instructor"].widget = forms.HiddenInput()
            if not self.is_bound:
                self.fields["instructor"].initial = instructors[0].pk
            self.fields["instructor"].queryset = Personnel.objects.filter(pk=instructors[0].pk)
        elif len(instructors) > 1:
            self.fields["instructor"].widget = forms.RadioSelect()
            self.fields["instructor"].queryset = Personnel.objects.filter(
                pk__in=[i.pk for i in instructors]
            ).order_by("name")
            self.fields["instructor"].empty_label = None
        else:
            self.fields["instructor"].queryset = Personnel.objects.none()
            self.fields["instructor"].required = False

    def _set_date_partials(self, prefix, value):
        if not value:
            return
        if prefix == "dob":
            day_field, month_field, year_field = "dob_day", "dob_month", "dob_year"
        else:
            day_field, month_field, year_field = "exam_date_day", "exam_date_month", "exam_date_year"
        self.fields[day_field].initial = f"{value.day:02d}"
        self.fields[month_field].initial = f"{value.month:02d}"
        self.fields[year_field].initial = str(value.year)

    def _combine_partial_date(self, cleaned_data, prefix, target_field, *, required, label):
        existing = cleaned_data.get(target_field)
        if existing:
            return existing

        if prefix == "dob":
            day_field, month_field, year_field = "dob_day", "dob_month", "dob_year"
        else:
            day_field, month_field, year_field = "exam_date_day", "exam_date_month", "exam_date_year"

        day_raw = (cleaned_data.get(day_field) or "").strip()
        month_raw = (cleaned_data.get(month_field) or "").strip()
        year_raw = (cleaned_data.get(year_field) or "").strip()

        if day_raw and month_raw and year_raw:
            try:
                parsed = date(int(year_raw), int(month_raw), int(day_raw))
                cleaned_data[target_field] = parsed
                return parsed
            except (TypeError, ValueError):
                self.add_error(target_field, f"Please enter a valid {label} (DD MM YYYY).")
                return None

        if day_raw or month_raw or year_raw:
            self.add_error(target_field, f"Please enter the full {label} (day, month and year).")
        elif required:
            self.add_error(target_field, f"Please enter the {label}.")
        return None

    def clean(self):
        cleaned_data = super().clean()
        first = (cleaned_data.get("first_name") or "").strip()
        surname = (cleaned_data.get("surname") or "").strip()
        if first and surname:
            cleaned_data["name"] = title_case_person_name(f"{first} {surname}")

        self._combine_partial_date(
            cleaned_data, "dob", "date_of_birth", required=True, label="date of birth"
        )

        if self.show_exam_date:
            self._combine_partial_date(
                cleaned_data, "exam_date", "exam_date", required=True, label="exam date"
            )
        else:
            cleaned_data["exam_date"] = timezone.localdate()

        dob = cleaned_data.get("date_of_birth")
        if dob and not self.errors.get("date_of_birth"):
            today = timezone.localdate()
            if dob >= today:
                self.add_error("date_of_birth", "Date of birth must be before today.")
            elif dob.year < 1900:
                self.add_error("date_of_birth", "Please check the year entered.")

        return cleaned_data
