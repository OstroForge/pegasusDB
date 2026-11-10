from django import forms
from django.contrib.auth.models import User, Group
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator
from django.db.models import Q
from django.forms import inlineformset_factory, BaseInlineFormSet
from django.utils import timezone
from datetime import date
from . import models as m
from .utils.names import title_case_person_name

from .models import (
    AccidentReport,
    Business,
    CourseType,
    Personnel,
    TrainingLocation,
    Booking,
    BookingDay,
    Attendance,
    DelegateRegister,
    CourseCompetency,
    FeedbackResponse,
    Exam,
    ExamQuestion,
    ExamAnswer,
    MetaSetting,
    LogoOverride,
)

import string, secrets


def delivery_personnel_queryset(extra_personnel_ids=None):
    qs = Personnel.objects.filter(
        is_active=True,
        user__groups__name__iexact="instructor",
    )

    if extra_personnel_ids:
        qs = Personnel.objects.filter(
            Q(pk__in=extra_personnel_ids) | Q(pk__in=qs.values("pk"))
        )

    return qs.distinct().order_by("name")

# ---------------- Attendance ----------------
#class AttendanceForm(forms.ModelForm):
#    class Meta:
#        model = Attendance
#         fields = ["delegate_name", "delegate_email", "result", "notes"]


# ---------------- Booking ----------------

class BookingForm(forms.ModelForm):
    class Meta:
        model = Booking
        fields = [
            "business",
            "training_location",
            "precise_lat",
            "precise_lng",
            "course_type",
            "instructor",
            "course_date",
            "start_time",
            "course_fee",
            "instructor_fee",
            "allow_mileage_claim",
            "allow_accommodation",
            "mileage_fee",
            "contact_name",
            "telephone",
            "email",
            "course_reference",
            "booking_notes",
        ]
        # BookingForm.Meta.widgets
        widgets = {
            "course_date": forms.DateInput(attrs={"type": "date"}),
            "start_time": forms.TimeInput(attrs={"type": "time"}),
            "course_reference": forms.TextInput(attrs={"readonly": "readonly"}),
            "booking_notes": forms.Textarea(attrs={"rows": 6, "class": "form-control booking-notes-input"}),
            'precise_lat': forms.HiddenInput(),
            'precise_lng': forms.HiddenInput(), 
            "allow_mileage_claim": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "mileage_fee": forms.HiddenInput(),
            "allow_accommodation": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        current_instructor_id = getattr(self.instance, "instructor_id", None)
        extra_ids = [current_instructor_id] if current_instructor_id else None
        self.fields["instructor"].queryset = delivery_personnel_queryset(extra_ids)

        from .utils.course_types import bookable_course_types

        current_ct_id = getattr(self.instance, "course_type_id", None)
        self.fields["course_type"].queryset = bookable_course_types(include_pk=current_ct_id)

        if self.instance and self.instance.pk and not self.is_bound:
            admin_lat = self.instance.admin_precise_lat
            admin_lng = self.instance.admin_precise_lng
            if admin_lat is not None and admin_lng is not None:
                self.instance.precise_lat = float(admin_lat)
                self.instance.precise_lng = float(admin_lng)

        # Start with none until we know the business
        self.fields["training_location"].queryset = TrainingLocation.objects.none()

        # --- Always display a blank option at the top ---
        # CHANGED: force an explicit choice and keep a visible blank row
        self.fields["training_location"].empty_label = "— Select a training location —"  # NEW
        self.fields["training_location"].required = True                                  # NEW
        # Make sure we don't carry any implicit initial on create
        if not (self.instance and self.instance.pk):                                      # NEW
            self.fields["training_location"].initial = None                               # NEW

        # Determine selected business (POST > instance)
        data = self.data if self.is_bound else None
        biz_id = None
        if data and data.get("business"):
            biz_id = data.get("business")
        elif self.instance and self.instance.pk:
            biz_id = self.instance.business_id

        qs = TrainingLocation.objects.none()
        if biz_id:
            qs = TrainingLocation.objects.filter(business_id=biz_id).order_by("name")
            self.fields["training_location"].queryset = qs

            # HARD-GUARANTEE the blank row shows up even if Django would hide it
            # (e.g., when an initial sneaks in or browser autofill happens)
            choices = [("", "— Select a training location —")]                            # NEW
            choices += [(str(o.pk), str(o)) for o in qs]                                  # NEW
            self.fields["training_location"].widget.choices = choices                     # NEW

        # Prefill fees & contacts on CREATE (don’t overwrite user POST values)
        creating = not (self.instance and self.instance.pk)

        if creating:
            # Fees from course type
            ct_id = (data.get("course_type") if data else self.initial.get("course_type"))
            if ct_id:
                try:
                    ct = CourseType.objects.get(pk=ct_id)
                    if not (data and data.get("course_fee")):
                        self.initial["course_fee"] = ct.default_course_fee
                    if not (data and data.get("instructor_fee")):
                        self.initial["instructor_fee"] = ct.default_instructor_fee
                except CourseType.DoesNotExist:
                    pass

            # Contacts from location (two behaviours):
            #   A) if user already selected a location in POST -> use that
            #   B) if there is EXACTLY ONE location for the business and user hasn’t picked yet,
            #      prefill contacts from that single location BUT keep the select blank.
            loc_id = data.get("training_location") if data else None
            try:
                loc = None
                if loc_id:
                    loc = TrainingLocation.objects.get(pk=loc_id)
                elif biz_id and qs.count() == 1:                                          # NEW
                    loc = qs.first()                                                      # NEW

                if loc:
                    if not (data and data.get("contact_name")):
                        self.initial["contact_name"] = loc.contact_name
                    if not (data and data.get("telephone")):
                        self.initial["telephone"] = loc.telephone
                    if not (data and data.get("email")):
                        self.initial["email"] = loc.email
            except TrainingLocation.DoesNotExist:
                pass

    # --- helpers for course reference ---
    @staticmethod
    def _rand_code(n=6):
        alphabet = string.ascii_uppercase + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(n))

    def clean(self):
        cleaned = super().clean()

        # Ensure location belongs to selected business
        biz = cleaned.get("business")
        loc = cleaned.get("training_location")
        if biz and loc and loc.business_id != biz.id:
            self.add_error("training_location", "Selected location does not belong to the chosen business.")

        ct = cleaned.get("course_type")
        if ct and ct.is_suspended:
            creating = not (self.instance and self.instance.pk)
            changing = (
                self.instance
                and self.instance.pk
                and self.instance.course_type_id != ct.pk
            )
            if creating or changing:
                self.add_error(
                    "course_type",
                    "This course type is suspended and cannot be used for new bookings.",
                )

        # Generate a unique course reference if empty
        if not cleaned.get("course_reference"):
            ct = cleaned.get("course_type")
            if ct:
                base = (ct.code or "COURSE").upper()
                for _ in range(50):
                    candidate = f"{base}-{self._rand_code(6)}"
                    qs = Booking.objects.filter(course_reference=candidate)
                    if self.instance and self.instance.pk:
                        qs = qs.exclude(pk=self.instance.pk)
                    if not qs.exists():
                        cleaned["course_reference"] = candidate
                        break
                else:
                    raise ValidationError("Could not generate a unique course reference; please try again.")

        return cleaned

    def clean_instructor(self):
        instructor = self.cleaned_data.get("instructor")
        if instructor and not delivery_personnel_queryset().filter(pk=instructor.pk).exists():
            raise ValidationError("Only personnel with the instructor role can be assigned to deliver courses.")
        return instructor





# ---------------- Business ----------------
class BusinessForm(forms.ModelForm):
    add_as_training_location = forms.BooleanField(
        required=False,
        label="Also add/update a training location with this address"
    )

    class Meta:
        model = Business
        fields = [
            'name', 'address_line', 'town', 'postcode',
            'contact_name', 'telephone', 'email',
            'is_dummy', 'dummy_course_type',
            'add_as_training_location',
        ]
        widgets = {
            'name': forms.TextInput(attrs={'class':'form-control'}),
            'address_line': forms.TextInput(attrs={
                'class':'form-control',
                'id':'id_business_address',
                'placeholder': 'Start typing to search…',
            }),
            'town': forms.TextInput(attrs={'class':'form-control'}),
            'postcode': forms.TextInput(attrs={'class':'form-control'}),
            'contact_name': forms.TextInput(attrs={'class':'form-control'}),
            'telephone': forms.TextInput(attrs={'class':'form-control'}),
            'email': forms.EmailInput(attrs={'class':'form-control'}),
            'is_dummy': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'dummy_course_type': forms.Select(attrs={'class': 'form-select'}),
        }

    def __init__(self, *args, **kwargs):
        self.allow_dummy_configuration = kwargs.pop('allow_dummy_configuration', False)
        super().__init__(*args, **kwargs)
        # Pre-tick if an identical “default” location appears to exist
        if self.instance and self.instance.pk:
            exists = TrainingLocation.objects.filter(
                business=self.instance,
                name=self.instance.name
            ).exists()
            self.fields['add_as_training_location'].initial = exists

        if self.allow_dummy_configuration:
            self.fields['dummy_course_type'].queryset = CourseType.objects.order_by('name')
            self.fields['is_dummy'].help_text = "Allows this business to be used for practice / familiarisation bookings."
            self.fields['dummy_course_type'].help_text = "Only used for dummy businesses. Instructors use this as the automatic course type when creating a practice booking."
        else:
            self.fields.pop('is_dummy', None)
            self.fields.pop('dummy_course_type', None)

    def clean(self):
        cleaned = super().clean()

        if not self.allow_dummy_configuration:
            return cleaned

        if cleaned.get('is_dummy'):
            if not cleaned.get('dummy_course_type'):
                self.add_error('dummy_course_type', 'Choose the default course type for this dummy business.')

            has_existing_location = bool(
                self.instance and self.instance.pk and TrainingLocation.objects.filter(
                    business=self.instance,
                    is_active=True,
                ).exists()
            )
            if not cleaned.get('add_as_training_location') and not has_existing_location:
                self.add_error(
                    'add_as_training_location',
                    'Dummy businesses need at least one active training location for quick familiarisation bookings.',
                )

        return cleaned

    def save(self, commit=True):
        biz = super().save(commit=commit)
        make = self.cleaned_data.get('add_as_training_location')

        if make:
            # create or update a location matching the business name
            loc, created = TrainingLocation.objects.get_or_create(
                business=biz,
                name=biz.name,
                defaults={
                    'address_line': biz.address_line,
                    'town': biz.town,
                    'postcode': biz.postcode,
                    'contact_name': biz.contact_name,
                    'telephone': biz.telephone,
                    'email': biz.email,
                }
            )
            if not created:
                loc.address_line = biz.address_line
                loc.town = biz.town
                loc.postcode = biz.postcode
                loc.contact_name = biz.contact_name
                loc.telephone = biz.telephone
                loc.email = biz.email
                loc.save()
        else:
            # remove the “default” location if it exists
            TrainingLocation.objects.filter(
                business=biz,
                name=biz.name
            ).delete()

        return biz


class DummyBookingQuickCreateForm(forms.Form):
    course_type = forms.ModelChoiceField(
        queryset=CourseType.objects.none(),
        widget=forms.Select(attrs={'class': 'form-select'}),
        empty_label='- Select a course type -',
    )
    course_date = forms.DateField(
        widget=forms.DateInput(attrs={'class': 'form-control', 'type': 'date'})
    )
    start_time = forms.TimeField(
        widget=forms.TimeInput(attrs={'class': 'form-control', 'type': 'time'})
    )

    def __init__(self, *args, **kwargs):
        default_course_type = kwargs.pop('default_course_type', None)
        super().__init__(*args, **kwargs)

        from .utils.course_types import bookable_course_types

        self.fields['course_type'].queryset = bookable_course_types()
        if default_course_type and not self.is_bound:
            self.fields['course_type'].initial = default_course_type


# ---------------- CourseType ----------------
class CourseTypeForm(forms.ModelForm):
    online_exercises = forms.MultipleChoiceField(
        choices=[],
        required=False,
        label="Delegate submitted exercises",
        widget=forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"}),
    )

    class Meta:
        model = CourseType
        fields = [
            "name", "duration_days",
            "certificate_duration", "onedrive_folder_link",
            "default_course_fee", "default_instructor_fee",
            "has_exam", "number_of_exams", "optional_modules_required",
            "has_online_exercises",
            "is_suspended",
        ]
        widgets = {
            "optional_modules_required": forms.Select(
                choices=[(i, str(i)) for i in range(0, 6)],
                attrs={"class": "form-select"},
            ),
        }

    def __init__(self, *args, **kwargs):
        from .models import CourseTypeOnlineExercise

        super().__init__(*args, **kwargs)
        self.fields["online_exercises"].choices = CourseTypeOnlineExercise.EXERCISE_CHOICES
        if self.instance and self.instance.pk:
            self.fields["online_exercises"].initial = list(
                self.instance.online_exercises.values_list("exercise_key", flat=True)
            )
        if self.instance._state.adding:
            self.fields.pop("is_suspended", None)
        for name, field in self.fields.items():
            if name in {"has_exam", "has_online_exercises"}:
                field.widget.attrs.setdefault("class", "form-check-input")
            elif name == "is_suspended":
                field.widget.attrs.setdefault("class", "form-check-input suspend-course-toggle")
                field.label = "Suspend course"
                field.help_text = "Existing bookings and records are unaffected."
            elif name == "optional_modules_required":
                field.widget.attrs.setdefault("class", "form-select")
            elif isinstance(field.widget, forms.CheckboxSelectMultiple):
                pass
            elif not isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "form-control")

class CourseCompetencyForm(forms.ModelForm):
    do_not_use = forms.BooleanField(required=False)

    class Meta:
        model = CourseCompetency
        fields = ["name", "sort_order", "is_optional"]

        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control", "placeholder": "Competency"}),
            "sort_order": forms.NumberInput(attrs={"class": "form-control", "style": "max-width:7rem"}),
            "is_optional": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].label = "Competency"
        self.fields["sort_order"].label = "Order"
        self.fields["is_optional"].label = "Optional competency"
        self.fields["do_not_use"].label = "Do not use this competency"
        self.fields["do_not_use"].widget.attrs.update({"class": "form-check-input"})
        self.fields["do_not_use"].initial = not getattr(self.instance, "is_active", True)

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.is_active = not bool(self.cleaned_data.get("do_not_use"))
        if commit:
            obj.save()
        return obj


CourseCompetencyFormSet = inlineformset_factory(
    parent_model=CourseType,
    model=CourseCompetency,
    form=CourseCompetencyForm,
    extra=2,            # two blank rows for quick add
    can_delete=True,
)

# ---------------- Instructor (Admin view) ----------------
class PersonnelAdminForm(forms.ModelForm):
    """
    Admin form for Personnel with a 'user' selector that:
      - lists non-superusers
      - lists only users not already assigned to another Personnel
      - keeps the current linked user when editing
    """

    user = forms.ModelChoiceField(
        queryset=User.objects.none(),
        required=False,
        help_text="Optional login for this person. Only unassigned (non-superuser) users are listed.",
        widget=forms.Select(attrs={"class": "form-select"})
    )

    class Meta:
        model = Personnel
        fields = [
            "name",
            "address_line",
            "town",
            "postcode",
            "telephone",
            "email",
            "bank_sort_code",
            "bank_account_number",
            "name_on_account",
            "user",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "address_line": forms.TextInput(attrs={"class": "form-control"}),
            "town": forms.TextInput(attrs={"class": "form-control"}),
            "postcode": forms.TextInput(attrs={"class": "form-control"}),
            "telephone": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "bank_sort_code": forms.TextInput(attrs={"class": "form-control"}),
            "bank_account_number": forms.TextInput(attrs={"class": "form-control"}),
            "name_on_account": forms.TextInput(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        base_qs = User.objects.filter(is_superuser=False)

        if self.instance and self.instance.pk and self.instance.user_id:
            qs = base_qs.filter(
                Q(personnel__isnull=True) | Q(pk=self.instance.user_id)
            )
        else:
            qs = base_qs.filter(personnel__isnull=True)

        self.fields["user"].queryset = qs.order_by("email", "username")

    def clean_user(self):
        user = self.cleaned_data.get("user")
        if not user:
            return user

        if user.is_superuser:
            raise forms.ValidationError("You cannot assign a superuser account.")

        existing = Personnel.objects.filter(user=user)
        if self.instance and self.instance.pk:
            existing = existing.exclude(pk=self.instance.pk)

        if existing.exists():
            raise forms.ValidationError("That user is already linked to another Personnel record.")

        return user


# ---------------- TrainingLocation ----------------
class TrainingLocationForm(forms.ModelForm):
    class Meta:
        model = TrainingLocation
        fields = [
            "name",
            "property_name",
            "address_line",
            "town",
            "postcode",
            "contact_name",
            "telephone",
            "email",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "property_name": forms.TextInput(attrs={"class": "form-control"}),
            "address_line": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Start typing to search…",
            }),
            "town": forms.TextInput(attrs={"class": "form-control"}),
            "postcode": forms.TextInput(attrs={"class": "form-control"}),
            "contact_name": forms.TextInput(attrs={"class": "form-control"}),
            "telephone": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.EmailInput(attrs={"class": "form-control"}),
        }
        # (business is set in the view; it’s not an editable form field here)


# ---------------- Instructor (self-service) ----------------
class PersonnelProfileForm(forms.ModelForm):
    """
    Form used in your custom front-end for instructors to edit their own profile.
    Does NOT allow changing the linked User account.
    """
    class Meta:
        model = Personnel
        fields = [
            "name",
            "address_line",
            "town",
            "postcode",
            "telephone",
            "email",
            "bank_sort_code",
            "bank_account_number",
            "name_on_account",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "address_line": forms.TextInput(attrs={"class": "form-control"}),
            "town": forms.TextInput(attrs={"class": "form-control"}),
            "postcode": forms.TextInput(attrs={"class": "form-control"}),
            "telephone": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "bank_sort_code": forms.TextInput(attrs={"class": "form-control"}),
            "bank_account_number": forms.TextInput(attrs={"class": "form-control"}),
            "name_on_account": forms.TextInput(attrs={"class": "form-control"}),
        }



# ---------------- Admin Instructor (explicit) ----------------
class AdminInstructorForm(forms.ModelForm):
    user = forms.ModelChoiceField(
        queryset=User.objects.order_by("username"),
        required=False,
        help_text="Optional: link to an existing Django user account (for login).",
    )

    class Meta:
        model = Personnel
        fields = [
            "user",
            "name",
            "address_line",
            "town",
            "postcode",
            "telephone",
            "email",
            "bank_sort_code",
            "bank_account_number",
            "name_on_account",
        ]

from django import forms
from django.utils import timezone

class DelegateRegisterForm(forms.ModelForm):
    health_status = forms.ChoiceField(
        choices=DelegateRegister.HealthStatus.choices,
        widget=forms.RadioSelect,
        required=True,
    )

    class Meta:
        model = DelegateRegister
        fields = [
            "name",
            "date_of_birth",
            "job_title",
            "employee_id",
            "date",
            "instructor",
            "health_status",
        ]
        # Force HTML5 date format so initial shows up
        widgets = {
            "date_of_birth": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "date":          forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # default health status
        if not self.is_bound and not self.initial.get("health_status"):
            self.initial["health_status"] = DelegateRegister.HealthStatus.FIT

        # default date to today if not provided by the view
        if not self.is_bound and not self.initial.get("date"):
            self.initial["date"] = timezone.localdate()

        # Make sure parsing accepts the browser's YYYY-MM-DD
        self.fields["date"].input_formats = ["%Y-%m-%d"]
        self.fields["date_of_birth"].input_formats = ["%Y-%m-%d"]

        # (nice-to-have) avoid autofill weirdness
        self.fields["date"].widget.attrs.setdefault("autocomplete", "off")
        self.fields["date_of_birth"].widget.attrs.setdefault("autocomplete", "off")


class PublicDelegateRegisterForm(DelegateRegisterForm):
    """Public register form: hidden course date (always today), split DOB entry."""

    first_name = forms.CharField(
        label="First name",
        max_length=50,
        error_messages={
            "required": "Please enter your first name.",
        },
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
        error_messages={
            "required": "Please enter your surname.",
        },
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "autocomplete": "family-name",
                "id": "id_surname",
            }
        ),
    )

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
    course_date_day = forms.CharField(
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
                "id": "course-date-day",
            }
        ),
    )
    course_date_month = forms.CharField(
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
                "id": "course-date-month",
            }
        ),
    )
    course_date_year = forms.CharField(
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
                "id": "course-date-year",
            }
        ),
    )

    def __init__(self, *args, instructors=None, show_register_date=False, **kwargs):
        self.show_register_date = show_register_date
        super().__init__(*args, **kwargs)

        self.fields["date"].widget = forms.HiddenInput()
        self.fields["date"].required = False

        self.fields["date_of_birth"].widget = forms.HiddenInput()
        self.fields["date_of_birth"].required = False

        # Combined name is built from first_name + surname in clean()
        self.fields["name"].widget = forms.HiddenInput()
        self.fields["name"].required = False

        for field_name in ("first_name", "surname", "job_title", "employee_id"):
            self.fields[field_name].widget.attrs.setdefault("class", "form-control")

        today = timezone.localdate()
        if not self.is_bound:
            self.initial.setdefault("date", today)
            if show_register_date:
                self._set_date_partials("course_date", self.initial.get("date"))
            dob = self.initial.get("date_of_birth")
            if dob:
                self._set_date_partials("dob", dob)
            full_name = (self.initial.get("name") or "").strip()
            if full_name and "first_name" not in self.initial:
                parts = full_name.split(None, 1)
                self.fields["first_name"].initial = parts[0]
                if len(parts) > 1:
                    self.fields["surname"].initial = parts[1]

        instructors = instructors or []
        if len(instructors) == 1:
            self.fields["instructor"].widget = forms.HiddenInput()
            if not self.is_bound:
                self.fields["instructor"].initial = instructors[0].pk
        elif len(instructors) > 1:
            self.fields["instructor"].widget = forms.RadioSelect(
                attrs={"class": "public-instructor-radio"}
            )
            self.fields["instructor"].queryset = Personnel.objects.filter(
                pk__in=[i.pk for i in instructors]
            ).order_by("name")
            self.fields["instructor"].empty_label = None
            self.fields["instructor"].error_messages = {
                "required": "Please select your instructor.",
            }
        else:
            self.fields["instructor"].queryset = Personnel.objects.none()

    def _set_date_partials(self, prefix, value):
        if not value:
            return
        self.fields[f"{prefix}_day"].initial = f"{value.day:02d}"
        self.fields[f"{prefix}_month"].initial = f"{value.month:02d}"
        self.fields[f"{prefix}_year"].initial = str(value.year)

    def _combine_partial_date(self, cleaned_data, prefix, target_field, *, required, label):
        existing = cleaned_data.get(target_field)
        if existing:
            return existing

        day_raw = (cleaned_data.get(f"{prefix}_day") or "").strip()
        month_raw = (cleaned_data.get(f"{prefix}_month") or "").strip()
        year_raw = (cleaned_data.get(f"{prefix}_year") or "").strip()

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
        missing_name_parts = []
        if not first:
            missing_name_parts.append("first name")
            if "first_name" not in self.errors:
                self.add_error("first_name", "Please enter your first name.")
        if not surname:
            missing_name_parts.append("surname")
            if "surname" not in self.errors:
                self.add_error("surname", "Please enter your surname.")
        if missing_name_parts:
            if len(missing_name_parts) == 2:
                self.add_error(
                    None,
                    "Registration could not be submitted because both first name and surname are required.",
                )
            else:
                self.add_error(
                    None,
                    f"Registration could not be submitted because your {missing_name_parts[0]} is required.",
                )
        else:
            cleaned_data["name"] = title_case_person_name(f"{first} {surname}")

        if self.show_register_date:
            self._combine_partial_date(
                cleaned_data, "course_date", "date", required=True, label="course date"
            )
        else:
            cleaned_data["date"] = timezone.localdate()

        self._combine_partial_date(
            cleaned_data, "dob", "date_of_birth", required=True, label="date of birth"
        )

        dob = cleaned_data.get("date_of_birth")
        if dob and not self.errors.get("date_of_birth"):
            today = timezone.localdate()
            if dob >= today:
                self.add_error("date_of_birth", "Date of birth must be before today.")
            elif dob.year < 1900:
                self.add_error("date_of_birth", "Please check the year entered.")

        return cleaned_data

   
class DelegateRegisterAdminForm(forms.ModelForm):
    class Meta:
        model = DelegateRegister
        # No "date" field here (date comes from BookingDay)
        fields = ["name", "date_of_birth", "job_title", "employee_id", "instructor", "health_status"]
        widgets = {
            # Force ISO so <input type="date"> shows the saved value
            "date_of_birth": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }


    def clean_name(self):
        # Title-case the name to tidy common variations
        name = (self.cleaned_data.get("name") or "").strip()
        parts = [p for p in name.split() if p]
        if len(parts) < 2:
            raise forms.ValidationError(
                "Please enter both a first name and a surname."
            )
        return title_case_person_name(" ".join(parts))

class DelegateRegisterInstructorForm(forms.ModelForm):
    # Keep radios – this guarantees the widget even if Meta changes
    health_status = forms.ChoiceField(
        choices=DelegateRegister.HealthStatus.choices,
        widget=forms.RadioSelect,
        required=True,
    )

    class Meta:
        model = DelegateRegister
        # instructor is set in the view — including it as HiddenInput broke edits when
        # the register belonged to a different instructor (day lead / pre-swap).
        fields = ["name", "date_of_birth", "job_title", "employee_id", "health_status", "notes"]
        widgets = {
            # IMPORTANT: include format so HTML5 date shows the saved value
            "date_of_birth": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "notes": forms.Textarea(attrs={"rows": 1, "placeholder": "Optional notes…"}),
        }

    def __init__(self, *args, **kwargs):
        kwargs.pop("current_instructor", None)  # accepted for call-site compatibility
        super().__init__(*args, **kwargs)

        # sensible default for new rows
        if not self.is_bound and not self.initial.get("health_status"):
            self.initial["health_status"] = DelegateRegister.HealthStatus.FIT

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        parts = [p for p in name.split() if p]
        if len(parts) < 2:
            raise forms.ValidationError(
                "Please enter both a first name and a surname."
            )
        return title_case_person_name(" ".join(parts))

class BookingNotesForm(forms.ModelForm):
    class Meta:
        model = Booking
        fields = ["booking_notes"]
        widgets = {
            "booking_notes": forms.Textarea(
                attrs={
                    "rows": 6,
                    "class": "form-control booking-notes-input",
                    "placeholder": "Notes about this course (visible to instructor and admin).",
                }
            ),
        }

from django import forms
from .models import FeedbackResponse, Personnel as Instructor, CourseType

RATING_CHOICES = [(i, str(i)) for i in range(1, 6)]

class SmileyRadioSelect(forms.RadioSelect):
    template_name = "widgets/smiley_radio.html"


class RatingScaleSelect(forms.RadioSelect):
    template_name = "widgets/rating_scale.html"
    option_template_name = "django/forms/widgets/radio_option.html"

EMOJI_1_TO_5 = (
    (1, "😟 1"),
    (2, "🙁 2"),
    (3, "😐 3"),
    (4, "🙂 4"),
    (5, "😀 5"),
)

# forms.py
from django import forms
from .models import FeedbackResponse, CourseType, Personnel as Instructor

RATING_CHOICES = (
    (1, "1"),
    (2, "2"),
    (3, "3"),
    (4, "4"),
    (5, "5"),
)

class FeedbackForm(forms.ModelForm):
    class Meta:
        model = FeedbackResponse
        fields = [
            "course_type", "date", "instructor",
            "prior_knowledge", "post_knowledge",
            "q_purpose_clear", "q_personal_needs", "q_exercises_useful",
            "q_structure", "q_pace", "q_content_clear", "q_instructor_knowledge",
            "q_materials_quality", "q_books_quality",
            "q_venue_suitable",
            "q_benefit_at_work", "q_benefit_outside",  # <-- ensure included
            "overall_rating",
            "comments", "wants_callback",
            "contact_name", "contact_email", "contact_phone",
        ]
        widgets = {
            # 1–5 radio widgets for all rating questions, including the two “Summary” ones:
            "prior_knowledge":        RatingScaleSelect(choices=RATING_CHOICES),
            "post_knowledge":         RatingScaleSelect(choices=RATING_CHOICES),
            "q_purpose_clear":        RatingScaleSelect(choices=RATING_CHOICES),
            "q_personal_needs":       RatingScaleSelect(choices=RATING_CHOICES),
            "q_exercises_useful":     RatingScaleSelect(choices=RATING_CHOICES),
            "q_structure":            RatingScaleSelect(choices=RATING_CHOICES),
            "q_pace":                 RatingScaleSelect(choices=RATING_CHOICES),
            "q_content_clear":        RatingScaleSelect(choices=RATING_CHOICES),
            "q_instructor_knowledge": RatingScaleSelect(choices=RATING_CHOICES),
            "q_materials_quality":    RatingScaleSelect(choices=RATING_CHOICES),
            "q_books_quality":        RatingScaleSelect(choices=RATING_CHOICES),
            "q_venue_suitable":       RatingScaleSelect(choices=RATING_CHOICES),
            "q_benefit_at_work":      RatingScaleSelect(choices=RATING_CHOICES),
            "q_benefit_outside":      RatingScaleSelect(choices=RATING_CHOICES),
            "overall_rating":         RatingScaleSelect(choices=RATING_CHOICES),

            # the rest are normal inputs/textarea/checkbox:
            "comments":       forms.Textarea(attrs={"rows": 4, "class": "form-control"}),
            "wants_callback": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "date":           forms.DateInput(attrs={"type": "date", "class": "form-control"}, format="%Y-%m-%d"),
            "course_type":    forms.Select(attrs={"class": "form-select"}),
            "instructor":     forms.Select(attrs={"class": "form-select"}),
            "contact_name":   forms.TextInput(attrs={"class": "form-control"}),
            "contact_email":  forms.EmailInput(attrs={"class": "form-control"}),
            "contact_phone":  forms.TextInput(attrs={"class": "form-control"}),
        }


class PublicFeedbackForm(FeedbackForm):
    """Public feedback: hidden course date (today in production), instructor like register."""

    def __init__(self, *args, instructors=None, show_feedback_date=False, **kwargs):
        self.show_feedback_date = show_feedback_date
        super().__init__(*args, **kwargs)

        if not show_feedback_date:
            self.fields["date"].widget = forms.HiddenInput()

        if not self.is_bound:
            self.initial.setdefault("date", timezone.localdate())

        instructors = instructors or []
        if len(instructors) == 1:
            self.fields["instructor"].widget = forms.HiddenInput()
            if not self.is_bound:
                self.initial["instructor"] = instructors[0].pk
            self.fields["instructor"].required = True
        elif len(instructors) > 1:
            self.fields["instructor"].widget = forms.RadioSelect()
            self.fields["instructor"].queryset = Personnel.objects.filter(
                pk__in=[i.pk for i in instructors]
            ).order_by("name")
            self.fields["instructor"].empty_label = None
            self.fields["instructor"].required = True
            self.fields["instructor"].error_messages = {
                "required": "Please select your instructor.",
            }
        else:
            self.fields["instructor"].queryset = Personnel.objects.none()
            self.fields["instructor"].required = False


class ExamForm(forms.ModelForm):
    class Meta:
        model = Exam
        fields = ["sequence", "title"]
        widgets = {
            "sequence": forms.NumberInput(attrs={"class": "form-control form-control-sm", "style": "max-width:6rem"}),
            "title": forms.TextInput(attrs={"class": "form-control"}),
        }

# ...imports above...


class ExamQuestionForm(forms.ModelForm):
    image_upload = forms.ImageField(
        required=False,
        label="Upload from this computer",
        widget=forms.FileInput(attrs={"class": "form-control", "accept": "image/jpeg,image/png,image/webp,image/gif"}),
    )
    server_image = forms.ChoiceField(
        required=False,
        label="Image on the server",
        choices=[("", "Leave unchanged")],
        widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )
    clear_stored_image = forms.BooleanField(
        required=False,
        label="Remove the stored image",
    )

    def __init__(self, *args, library_images=None, **kwargs):
        super().__init__(*args, **kwargs)
        names = list(library_images or [])
        self.fields["server_image"].choices = [("", "Leave unchanged")] + [(name, name) for name in names]
        current = (getattr(self.instance, "image_filename", "") or "")
        if not self.is_bound and current in names:
            self.initial["server_image"] = current

    class Meta:
        model = ExamQuestion
        fields = ["order", "is_active", "text", "image_url"]
        widgets = {
            "order": forms.NumberInput(attrs={"class": "form-control form-control-sm", "style": "max-width:6rem"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "text": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "image_url": forms.URLInput(attrs={"class": "form-control"}),
        }

    def clean_image_upload(self):
        upload = self.cleaned_data.get("image_upload")
        if upload and upload.size > 4 * 1024 * 1024:
            raise forms.ValidationError("Image must be 4 MB or smaller.")
        return upload

    def clean_server_image(self):
        from .exam_library import library_image_path

        name = (self.cleaned_data.get("server_image") or "").strip()
        if name and library_image_path(name) is None:
            raise forms.ValidationError("That image is not on the server.")
        return name

    def save(self, commit=True):
        from .exam_library import read_library_image

        obj = super().save(commit=False)
        upload = self.cleaned_data.get("image_upload")
        chosen = (self.cleaned_data.get("server_image") or "").strip()
        if upload:
            upload.seek(0)
            obj.image_data = upload.read()
            obj.image_content_type = getattr(upload, "content_type", "") or "image/png"
            obj.image_filename = (getattr(upload, "name", "") or "question-image").rsplit("/", 1)[-1][:255]
        elif self.cleaned_data.get("clear_stored_image"):
            obj.image_data = None
            obj.image_content_type = ""
            obj.image_filename = ""
        elif chosen:
            loaded = read_library_image(chosen)
            if loaded:
                data, content_type = loaded
                obj.image_data = data
                obj.image_content_type = content_type
                obj.image_filename = chosen
        if commit:
            obj.save()
            self.save_m2m()
        return obj

class ExamAnswerForm(forms.ModelForm):
    class Meta:
        model = ExamAnswer
        fields = ["order", "text", "is_correct"]
        widgets = {
            "order": forms.NumberInput(attrs={"class": "form-control form-control-sm", "style": "max-width:6rem"}),
            "text": forms.TextInput(attrs={"class": "form-control form-control-sm"}),
            # is_correct rendered as radios in template; keep widget for fallback
            "is_correct": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

class BaseAnswerFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        alive = 0
        correct = 0
        for f in self.forms:
            if not getattr(f, "cleaned_data", None) or f.cleaned_data.get("DELETE"):
                continue
            if f.cleaned_data.get("text"):
                alive += 1
                if f.cleaned_data.get("is_correct"):
                    correct += 1
        if alive:
            if alive < 2:
                raise forms.ValidationError("Each question must have at least two answers.")
            if correct != 1:
                raise forms.ValidationError("Exactly one answer must be marked correct.")

AnswerFormSet = inlineformset_factory(
    parent_model=ExamQuestion,
    model=ExamAnswer,
    form=ExamAnswerForm,
    fields=["order", "text", "is_correct"],
    extra=0,           # <<< important: JS adds the rows
    can_delete=True,
    formset=BaseAnswerFormSet,
)


QuestionFormSet = inlineformset_factory(
    parent_model=Exam,
    model=ExamQuestion,
    form=ExamQuestionForm,
    fields=["order", "is_active", "text", "image_url"],
    extra=0,              # add via “Add question” button (empty_form)
    can_delete=True,
)

class AccidentReportForm(forms.ModelForm):
    class Meta:
        model = AccidentReport
        fields = [
            "date", "time", "location",
            "injured_name", "injured_address",
            "what_happened", "injuries_sustained",
            "actions_carried_out", "actions_prevent_recurrence",
            "first_aider_name", "reporter_name",
        ]
        widgets = {
            "date":  forms.DateInput(attrs={"type": "date", "class": "form-control", "id": "ar-date"}),
            "time":  forms.TimeInput(attrs={"type": "time", "class": "form-control", "id": "ar-time"}),
            "location": forms.TextInput(attrs={"class": "form-control"}),
            "injured_name": forms.TextInput(attrs={"class": "form-control"}),

            # TextInput so Places can attach
            "injured_address": forms.TextInput(attrs={
                "class": "form-control", "id": "ar-injured-address", "autocomplete": "off"
            }),

            "what_happened": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "injuries_sustained": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "actions_carried_out": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "actions_prevent_recurrence": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "first_aider_name": forms.TextInput(attrs={"class": "form-control", "id": "ar-first-aider"}),
            "reporter_name":    forms.TextInput(attrs={"class": "form-control", "id": "ar-reporter"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # belt-and-braces: ensure IDs exist even if a widget gets swapped
        self.fields["date"].widget.attrs.setdefault("id", "ar-date")
        self.fields["time"].widget.attrs.setdefault("id", "ar-time")
        self.fields["injured_address"].widget.attrs.setdefault("id", "ar-injured-address")
        self.fields["first_aider_name"].widget.attrs.setdefault("id", "ar-first-aider")
        self.fields["reporter_name"].widget.attrs.setdefault("id", "ar-reporter")


class PublicAccidentReportForm(AccidentReportForm):
    """Public accident report with course context and instructor routing."""

    course_type = forms.ModelChoiceField(
        queryset=CourseType.objects.none(),
        required=False,
        label="Course",
        widget=forms.Select(attrs={"class": "form-select", "id": "ar-course-type"}),
    )
    reported_to = forms.ModelChoiceField(
        queryset=Personnel.objects.none(),
        required=True,
        label="Reported to",
        widget=forms.Select(attrs={"class": "form-select", "id": "ar-reported-to"}),
    )

    class Meta(AccidentReportForm.Meta):
        fields = AccidentReportForm.Meta.fields

    def __init__(
        self,
        *args,
        instructors=None,
        show_accident_date=False,
        prefilled_course=None,
        lock_course=False,
        **kwargs,
    ):
        from .utils.online_exercises import course_types_with_accident_reports

        super().__init__(*args, **kwargs)
        self.show_accident_date = show_accident_date
        self.lock_course = lock_course
        self.prefilled_course = prefilled_course

        self.fields["course_type"].queryset = course_types_with_accident_reports()
        # Always hidden — delegates never choose course (date + instructor resolve it).
        self.fields["course_type"].widget = forms.HiddenInput(attrs={"id": "ar-course-type"})
        self.fields["course_type"].required = False

        if not show_accident_date:
            self.fields["date"].widget = forms.HiddenInput(attrs={"id": "ar-date"})
            if not self.is_bound:
                self.initial.setdefault("date", timezone.localdate())
        else:
            self.fields["date"].label = "Incident date"
            self.fields["date"].help_text = "Change this when testing on a different course date."

        if prefilled_course and not self.is_bound:
            self.initial["course_type"] = prefilled_course.pk

        instructors = list(instructors or [])
        self.single_instructor = None
        if len(instructors) == 1:
            self.single_instructor = instructors[0]
            self.fields["reported_to"].queryset = Personnel.objects.filter(pk=instructors[0].pk)
            self.fields["reported_to"].widget = forms.HiddenInput(attrs={"id": "ar-reported-to"})
            if not self.is_bound:
                self.initial["reported_to"] = instructors[0].pk
            self.fields["reported_to"].required = True
        elif len(instructors) > 1:
            self.fields["reported_to"].queryset = Personnel.objects.filter(
                pk__in=[i.pk for i in instructors]
            ).order_by("name")
            self.fields["reported_to"].empty_label = "Select instructor…"
            self.fields["reported_to"].error_messages = {
                "required": "Please select the instructor you are reporting to.",
            }
        else:
            self.fields["reported_to"].queryset = Personnel.objects.none()
            self.fields["reported_to"].required = True
            self.single_instructor = None

    def clean(self):
        from .utils.accident_reports import accident_report_day_options
        from .utils.online_exercises import course_types_with_accident_reports

        cleaned = super().clean()
        course = self.prefilled_course or cleaned.get("course_type")
        reported_to = cleaned.get("reported_to") or self.single_instructor
        if reported_to and not cleaned.get("reported_to"):
            cleaned["reported_to"] = reported_to

        the_date = cleaned.get("date")
        if not course and reported_to and the_date:
            for row in accident_report_day_options(the_date).get("instructors") or []:
                if str(row["id"]) == str(reported_to.pk):
                    course = course_types_with_accident_reports().filter(pk=row["course_id"]).first()
                    if course:
                        cleaned["course_type"] = course
                    break

        if not course:
            self.add_error(
                None,
                "We could not match this report to a course. Check the incident date and instructor.",
            )
        elif not course_types_with_accident_reports().filter(pk=course.pk).exists():
            self.add_error(None, "Accident reports are not enabled for this course type.")

        if not self.single_instructor and not reported_to:
            self.add_error(
                "reported_to",
                "No instructor is scheduled for this course on the selected date. "
                "Try a different incident date or check with your trainer.",
            )
        return cleaned


class PersonnelForm(forms.ModelForm):

    groups = forms.ModelMultipleChoiceField(
        queryset=Group.objects.all().order_by("name"),
        required=False,
        widget=forms.CheckboxSelectMultiple(
            attrs={"class": "form-check-input"}
        ),
    )
    deliverable_course_types = forms.ModelMultipleChoiceField(
        queryset=CourseType.objects.filter(is_suspended=False).order_by("name"),
        required=False,
        widget=forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"}),
        label="Courses allowed to deliver",
        help_text="Instructors only receive updates for courses selected here.",
    )

    class Meta:
        model = Personnel
        fields = [
            "name",
            "email",
            "telephone",
            "address_line",
            "town",
            "postcode",
            "bank_sort_code",
            "bank_account_number",
            "name_on_account",
            "can_login",
            "is_active",
            "deliverable_course_types",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "telephone": forms.TextInput(attrs={"class": "form-control"}),
            "address_line": forms.TextInput(attrs={"class": "form-control"}),
            "town": forms.TextInput(attrs={"class": "form-control"}),
            "postcode": forms.TextInput(attrs={"class": "form-control"}),
            "bank_sort_code": forms.TextInput(attrs={"class": "form-control"}),
            "bank_account_number": forms.TextInput(attrs={"class": "form-control"}),
            "name_on_account": forms.TextInput(attrs={"class": "form-control"}),
            "can_login": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    # --------------------------
    # PRE-POPULATE GROUPS
    # --------------------------
    def __init__(self, *args, lock_groups=False, **kwargs):
        self.lock_groups = lock_groups
        super().__init__(*args, **kwargs)

        if self.instance and self.instance.user:
            # Load user's groups into the form
            self.fields["groups"].initial = self.instance.user.groups.all()

        if self.lock_groups:
            self.fields["groups"].disabled = True
            self.fields["groups"].help_text = (
                "Roles cannot be changed for superuser accounts."
            )

        if self.instance and self.instance.pk:
            self.fields["deliverable_course_types"].initial = (
                self.instance.deliverable_course_types.all()
            )

        # Disable the "can_login" checkbox if inactive
        if self.instance and not self.instance.is_active:
            self.fields["can_login"].widget.attrs["disabled"] = True

    # --------------------------
    # SAVE LOGIC
    # --------------------------
    def save(self, commit=True):
        inst = super().save(commit=False)

        # If inactive → force disable login
        if not inst.is_active:
            inst.can_login = False

        if commit:
            inst.save()
            self.save_m2m()

        # APPLY GROUPS + LOGIN SETTINGS TO USER
        if inst.user:
            user = inst.user

            # --- 1. Split Personnel.name into first + last ---
            full_name = self.cleaned_data["name"].strip()
            parts = full_name.split(" ", 1)
            user.first_name = parts[0]
            user.last_name = parts[1] if len(parts) > 1 else ""

            # --- 2. Combine first + last to keep Personnel.name clean ---
            inst.name = f"{user.first_name} {user.last_name}".strip()

            # --- 3. Apply groups ---
            if not getattr(self, "lock_groups", False):
                groups = self.cleaned_data.get("groups", [])
                user.groups.set(groups)

            # --- 4. Activate/deactivate login ---
            user.is_active = inst.can_login and inst.is_active
            user.save()

        return inst

class MetaSettingForm(forms.ModelForm):
    class Meta:
        model = MetaSetting
        fields = ["key", "value"]
        widgets = {
            "key": forms.TextInput(attrs={"class": "form-control"}),
            "value": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }


class LogoOverrideForm(forms.ModelForm):
    class Meta:
        model = LogoOverride
        fields = [
            "file_name",
            "active",
            "reason",
            "starts_at",
            "ends_at",
            "priority",
        ]
        widgets = {
            "file_name": forms.Select(attrs={"class": "form-select"}),
            "active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "reason": forms.TextInput(attrs={"class": "form-control"}),
            "starts_at": forms.DateTimeInput(
                attrs={"class": "form-control", "type": "datetime-local"},
                format="%Y-%m-%dT%H:%M",
            ),
            "ends_at": forms.DateTimeInput(
                attrs={"class": "form-control", "type": "datetime-local"},
                format="%Y-%m-%dT%H:%M",
            ),
            "priority": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
        }

    def __init__(self, *args, **kwargs):
        from .services.logos import available_logo_filenames

        super().__init__(*args, **kwargs)
        choices = [(name, name) for name in available_logo_filenames()]
        self.fields["file_name"].widget = forms.Select(
            choices=choices,
            attrs={"class": "form-select"},
        )
        self.fields["starts_at"].required = False
        self.fields["ends_at"].required = False
        self.fields["priority"].help_text = "Lower numbers take priority over higher ones."
        if self.instance and self.instance.pk:
            for field_name in ("starts_at", "ends_at"):
                dt = getattr(self.instance, field_name, None)
                if dt:
                    local_dt = timezone.localtime(dt)
                    self.initial[field_name] = local_dt.strftime("%Y-%m-%dT%H:%M")