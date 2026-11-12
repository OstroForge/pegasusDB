# unicorn_project/training/models.py
import uuid
from datetime import date
from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.db.models import Q
from django.db.models.signals import post_save
from django.contrib.auth.models import User
from django.dispatch import receiver
from django.utils import timezone


# =========================
# Core / Reference Models
# =========================

class Business(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    is_dummy = models.BooleanField(
        default=False,
        help_text="Marks this as a dummy / familiarisation business.",
    )
    dummy_course_type = models.ForeignKey(
        "CourseType",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="dummy_businesses",
        help_text="Default course type used when instructors create quick dummy bookings.",
    )

    address_line = models.CharField(max_length=255, blank=True, null=True)
    town         = models.CharField(max_length=255, blank=True, null=True)
    postcode     = models.CharField(max_length=32,  blank=True, null=True)

    contact_name = models.CharField(max_length=255, blank=True, null=True)
    telephone    = models.CharField(max_length=64,  blank=True, null=True)
    email        = models.EmailField(blank=True, null=True)

    # used to keep a matching default TrainingLocation in sync
    add_as_training_location = models.BooleanField(default=False)

    def __str__(self):
        return self.name


class TrainingLocation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="training_locations")
    name = models.CharField(max_length=255)

    # Soft-delete / archive flag.
    # We don't hard-delete a location if it has been used for bookings.
    is_active = models.BooleanField(default=True, db_index=True)

    property_name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Building or site name (e.g. Progress House), if different from the street address.",
    )
    address_line = models.CharField(max_length=255, blank=True, null=True)
    town         = models.CharField(max_length=255, blank=True, null=True)
    postcode     = models.CharField(max_length=32,  blank=True, null=True)

    contact_name = models.CharField(max_length=255, blank=True, null=True)
    telephone    = models.CharField(max_length=64,  blank=True, null=True)
    email        = models.EmailField(blank=True, null=True)

    def __str__(self):
        return f"{self.name} ({self.business.name})"


class CourseType(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=20, unique=True)
    duration_days = models.DecimalField(
        max_digits=3, decimal_places=1,
        validators=[MinValueValidator(Decimal("0.1")), MaxValueValidator(Decimal("99.0"))],
        default=Decimal("1.0"),
        help_text="Enter the number of days the course runs over (e.g. 3.0 = 3 days).",
    )
    certificate_duration = models.DecimalField(
        max_digits=3, decimal_places=1, 
        validators=[MinValueValidator(Decimal("0.1")), MaxValueValidator(Decimal("5.0"))],  
        default=Decimal("3.0"),
        help_text="Certificate validity in years (e.g. 3.0 = 3 years).",
    )

    # defaults that prefill a Booking (but remain editable there)
    default_course_fee = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    default_instructor_fee = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    # exams
    has_exam = models.BooleanField(
        default=False,
        verbose_name="This course contains theoretical exam(s)",
    )
    number_of_exams = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Required if 'This course contains theoretical exam(s)' is ticked.",
        validators=[MinValueValidator(1)],
    )
    optional_modules_required = models.PositiveSmallIntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(5)],
        help_text="How many optional competencies must be selected in the assessment matrix (0-5).",
    )

    has_online_exercises = models.BooleanField(
        default=False,
        verbose_name="Delegates need to submit online exercises (e.g. accident reports)",
    )

    onedrive_folder_link = models.URLField(
    blank=True,
    help_text="Optional OneDrive folder link for instructors"
)

    is_suspended = models.BooleanField(
        default=False,
        help_text="When suspended, this course type cannot be used for new bookings.",
    )

    def has_ever_been_booked(self) -> bool:
        return self.bookings.exists()

    def can_be_deleted(self) -> bool:
        return not self.has_ever_been_booked()

    def clean(self):
        # Enforce conditional requirement
        if self.has_exam and not self.number_of_exams:
            raise ValidationError({"number_of_exams": "Please enter how many exams this course has (whole number ≥ 1)."})
        # Keep field tidy when exams are disabled
        if not self.has_exam:
            self.number_of_exams = None

    class Meta:
        constraints = [
            # DB-level safety: either no exams -> number_of_exams must be NULL,
            # or has_exam -> number_of_exams >= 1
            models.CheckConstraint(
                name="course_type_exam_consistency",
                check=Q(has_exam=False, number_of_exams__isnull=True) | Q(has_exam=True, number_of_exams__gte=1),
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"


class CourseTypeOnlineExercise(models.Model):
    """Online delegate exercises enabled for a course type (e.g. accident reports)."""

    EXERCISE_ACCIDENT_REPORTS = "accident_reports"

    EXERCISE_CHOICES = [
        (EXERCISE_ACCIDENT_REPORTS, "Accident reports"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    course_type = models.ForeignKey(
        CourseType,
        on_delete=models.CASCADE,
        related_name="online_exercises",
    )
    exercise_key = models.CharField(max_length=40, choices=EXERCISE_CHOICES)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["course_type", "exercise_key"],
                name="course_type_online_exercise_unique",
            ),
        ]

    def __str__(self):
        return f"{self.course_type.code}: {self.get_exercise_key_display()}"


class Personnel(models.Model):
    SIDEBAR_THEME_CHOICES = [
        ("default", "Default Burgundy"),
        ("ocean", "Ocean Blue"),
        ("forest", "Forest Green"),
        ("plum", "Plum"),
        ("charcoal", "Charcoal"),
    ]

    AVATAR_ICON_CHOICES = [
        ("initials", "Initials"),
        ("person", "Person"),
        ("person_badge", "Person Badge"),
        ("teacher", "Teacher"),
        ("star", "Star"),
        ("graduation", "Graduation Cap"),
        ("tools", "Tools"),
        ("unicorn", "Sparkles"),
        ("briefcase", "Briefcase"),
        ("books", "Books"),
        ("note", "Note"),
        ("cog", "Cog"),
        ("heart", "Heart"),
        ("palette", "Palette"),
        ("lightning", "Lightning"),
        ("music", "Music"),
        ("camera", "Camera"),
        ("bug", "Bug"),
        ("rocket", "Rocket"),
        ("ambulance", "Ambulance"),
        ("fire_engine", "Fire Engine"),
        ("first_aid", "First Aid Kit"),
        ("extinguisher", "Fire Extinguisher"),
        ("firefighter", "Firefighter"),
        ("paramedic", "Ambulance Crew"),
        ("hospital", "Hospital"),
        ("lifebuoy", "Lifebuoy"),
        ("shield_medical", "Medical Shield"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # The important fix is here ↓↓↓
    user = models.OneToOneField(
        User,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="personnel",
    )

    name = models.CharField(max_length=255)
    address_line = models.CharField(max_length=255, blank=True, default="")
    town = models.CharField(max_length=120, blank=True, default="")
    postcode = models.CharField(max_length=12, blank=True, default="")

    telephone = models.CharField(max_length=32, blank=True, default="")
    email = models.EmailField(unique=True)

    bank_sort_code = models.CharField(max_length=16, blank=True, default="")
    bank_account_number = models.CharField(max_length=20, blank=True, default="")
    name_on_account = models.CharField(max_length=255, blank=True, default="")

    dyslexia_mode = models.BooleanField(default=False)
    PASTEL_CHOICES = [
        ('none', 'Default (off-white)'),
        ('cream', 'Cream'),
        ('yellow', 'Soft Yellow'),
        ('blue', 'Soft Blue'),
        ('green', 'Soft Green'),
    ]

    pastel_background = models.CharField(
        max_length=20,
        choices=PASTEL_CHOICES,
        default='none'
    )

    night_mode = models.BooleanField(default=False)

    sidebar_theme = models.CharField(
        max_length=20,
        choices=SIDEBAR_THEME_CHOICES,
        default="default",
    )

    sidebar_custom_color = models.CharField(
        max_length=7,
        blank=True,
        default="",
    )

    avatar_icon = models.CharField(
        max_length=30,
        choices=AVATAR_ICON_CHOICES,
        default="initials",
    )

    default_dashboard_role = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="Preferred dashboard when logging in (for users with multiple roles).",
    )

    dismissed_release_notes_id = models.CharField(
        max_length=40,
        blank=True,
        default="",
        help_text="Release-notes modal ID the user chose not to see again.",
    )
  
    is_active = models.BooleanField(default=True)
    can_login = models.BooleanField(default=True)
    must_change_password = models.BooleanField(default=True)

    # Two-factor authentication
    totp_secret = models.CharField(max_length=32, blank=True, null=True, help_text="TOTP secret for 2FA")
    totp_backup_codes = models.TextField(blank=True, null=True, help_text="JSON list of hashed one-time backup codes")

    telegram_chat_id = models.CharField(max_length=32, blank=True, default="")
    telegram_linked_at = models.DateTimeField(null=True, blank=True)
    notify_new_bookings_telegram = models.BooleanField(
        default=False,
        help_text="Telegram alert when a new booking is assigned to you.",
    )
    notify_booking_changes_telegram = models.BooleanField(
        default=False,
        help_text="Telegram alert when a booking is updated or cancelled.",
    )
    notify_reminders_telegram = models.BooleanField(
        default=False,
        help_text="Telegram departure reminder on the course day.",
    )
    notify_upcoming_bookings_telegram = models.BooleanField(
        default=False,
        help_text="Telegram reminders before upcoming bookings (days chosen on profile).",
    )
    notify_cover_requests_telegram = models.BooleanField(
        default=True,
        help_text="Telegram alerts for course cover requests and responses.",
    )
    notify_new_bookings_email = models.BooleanField(
        default=False,
        help_text="Email alert when a new booking is assigned to you.",
    )
    notify_booking_changes_email = models.BooleanField(
        default=False,
        help_text="Email alert when a booking is updated or cancelled.",
    )
    notify_reminders_email = models.BooleanField(
        default=False,
        help_text="Email departure reminder on the course day.",
    )
    notify_upcoming_bookings_email = models.BooleanField(
        default=False,
        help_text="Email reminders before upcoming bookings (days chosen on profile).",
    )
    deliverable_course_types = models.ManyToManyField(
        "CourseType",
        blank=True,
        related_name="qualified_instructors",
        help_text="Course types this instructor is allowed to deliver.",
    )
    upcoming_reminder_days_1 = models.PositiveSmallIntegerField(null=True, blank=True)
    upcoming_reminder_days_2 = models.PositiveSmallIntegerField(null=True, blank=True)
    upcoming_reminder_days_3 = models.PositiveSmallIntegerField(null=True, blank=True)

    def upcoming_reminder_offsets(self):
        offsets = []
        for value in (
            self.upcoming_reminder_days_1,
            self.upcoming_reminder_days_2,
            self.upcoming_reminder_days_3,
        ):
            if value and value > 0 and value not in offsets:
                offsets.append(int(value))
        return sorted(offsets, reverse=True)

    @property
    def avatar_initial(self):
        source = (self.name or self.email or "?").strip()
        return (source[:1] or "?").upper()

    @property
    def avatar_icon_class(self):
        return {
            "person": "bi-person-fill",
            "person_badge": "bi-person-badge-fill",
            "teacher": "bi-easel2-fill",
            "star": "bi-star-fill",
            "graduation": "bi-mortarboard-fill",
            "tools": "bi-tools",
            "unicorn": "bi-stars",
            "briefcase": "bi-briefcase-fill",
            "books": "bi-journal-bookmark-fill",
            "note": "bi-journal-text",
            "cog": "bi-gear-fill",
            "heart": "bi-heart-fill",
            "palette": "bi-palette-fill",
            "lightning": "bi-lightning-charge-fill",
            "music": "bi-music-note-beamed",
            "camera": "bi-camera-fill",
            "bug": "bi-bug-fill",
            "rocket": "bi-rocket-takeoff-fill",
            "ambulance": "bi-truck-front-fill",
            "fire_engine": "bi-fire",
            "first_aid": "bi-heart-pulse-fill",
            "extinguisher": "bi-shield-fill-plus",
            "firefighter": "bi-person-badge-fill",
            "paramedic": "bi-person-vcard-fill",
            "hospital": "bi-hospital-fill",
            "lifebuoy": "bi-life-preserver",
            "shield_medical": "bi-shield-plus",
        }.get(self.avatar_icon, "")

    def __str__(self):
        return self.name


class InstructorAvailability(models.Model):
    STATUS_AVAILABLE = "available"
    STATUS_UNAVAILABLE = "unavailable"

    STATUS_CHOICES = [
        (STATUS_AVAILABLE, "Available"),
        (STATUS_UNAVAILABLE, "Not available"),
    ]

    instructor = models.ForeignKey(
        "Personnel",
        on_delete=models.CASCADE,
        related_name="availability_entries",
    )
    date = models.DateField(db_index=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES)
    note = models.CharField(max_length=255, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["instructor", "date"],
                name="uniq_instructor_availability_per_day",
            )
        ]
        ordering = ["date"]

    def __str__(self):
        return f"{self.instructor.name} - {self.date} ({self.get_status_display()})"


class InstructorAvailabilityPattern(models.Model):
    """
    Recurring availability pattern for an instructor.
    Expands to individual InstructorAvailability dates.
    Individual dates override patterns.
    """
    STATUS_AVAILABLE = "available"
    STATUS_UNAVAILABLE = "unavailable"

    STATUS_CHOICES = [
        (STATUS_AVAILABLE, "Available"),
        (STATUS_UNAVAILABLE, "Not available"),
    ]

    # Days of week: 0=Monday, 6=Sunday (Django convention)
    # Stored as comma-separated string like "1,2,3" for Tue,Wed,Thu
    DOW_LABELS = {
        0: "Monday",
        1: "Tuesday",
        2: "Wednesday",
        3: "Thursday",
        4: "Friday",
        5: "Saturday",
        6: "Sunday",
    }

    instructor = models.ForeignKey(
        "Personnel",
        on_delete=models.CASCADE,
        related_name="availability_patterns",
    )
    start_date = models.DateField(db_index=True)
    end_date = models.DateField(db_index=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES)
    days_of_week = models.CharField(
        max_length=13,  # "0,1,2,3,4,5,6" is 13 chars
        help_text="Comma-separated day numbers: 0=Mon, 6=Sun",
    )
    note = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        days_list = [self.DOW_LABELS[int(d)] for d in self.days_of_week.split(",") if d]
        return f"{self.instructor.name} - {', '.join(days_list)} ({self.get_status_display()})"

    def get_days_of_week_list(self):
        """Return list of day numbers as integers."""
        if not self.days_of_week:
            return []
        return [int(d) for d in self.days_of_week.split(",") if d.isdigit()]

    def get_days_of_week_labels(self):
        """Return list of day names."""
        return [self.DOW_LABELS[int(d)] for d in self.days_of_week.split(",") if d]

    def expand_to_dates(self):
        """
        Expand this pattern to a list of dates matching the recurrence.
        Returns list of date objects that match the pattern.
        """
        from datetime import timedelta
        days = self.get_days_of_week_list()
        if not days:
            return []

        result = []
        current = self.start_date
        while current <= self.end_date:
            if current.weekday() in days:
                result.append(current)
            current += timedelta(days=1)
        return result

# =========================
# Operational Models
# =========================

class Booking(models.Model):
    """
    A booking of a course for a business/location.
    course_reference is generated as: <course_type.code>-<6 char A/Z/0-9>, unique.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    business          = models.ForeignKey(Business,         on_delete=models.CASCADE, related_name="bookings")
    # Prevent deleting a location that has bookings (we archive instead).
    training_location = models.ForeignKey(TrainingLocation, on_delete=models.PROTECT, related_name="bookings")
    course_type       = models.ForeignKey(CourseType,       on_delete=models.PROTECT, related_name="bookings")
    instructor        = models.ForeignKey(
        "Personnel", on_delete=models.SET_NULL, null=True, blank=True, related_name="bookings"
    )

    course_date = models.DateField()
    start_time  = models.TimeField(null=True, blank=True, default=None)
    booking_notes = models.TextField(blank=True)

    # Optional modules selected for this specific booking.
    # Required count is controlled by CourseType.optional_modules_required.
    optional_modules = models.ManyToManyField(
        "CourseCompetency",
        blank=True,
        related_name="bookings_as_optional_modules",
        limit_choices_to={"is_optional": True},
    )

    # generated reference, unless an imported course supplies its own
    course_reference = models.CharField(max_length=40, unique=True, blank=True)
    is_imported = models.BooleanField(
        default=False,
        help_text="Set for courses brought in from an old assessment matrix.",
    )

    precise_lat = models.FloatField(null=True, blank=True)
    precise_lng = models.FloatField(null=True, blank=True)
    admin_precise_lat = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    admin_precise_lng = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    # fees (prefilled from CourseType, but editable)
    course_fee     = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    instructor_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    allow_mileage_claim = models.BooleanField(default=True)
    mileage_fee = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    allow_accommodation = models.BooleanField(default=False)
    travel_duration_seconds = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="One-way driving time (seconds) from instructor to venue via Google Maps.",
    )

    # snapshot of location’s contact (prefilled, editable)
    contact_name   = models.CharField(max_length=200, blank=True)
    telephone      = models.CharField(max_length=50, blank=True)
    email          = models.EmailField(blank=True)

    # -------------------------------
    # STATUS
    # -------------------------------
    STATUS_CHOICES = [
        ("scheduled", "Scheduled"),
        ("in_progress", "In progress"),
        ("awaiting_closure", "Awaiting instructor closure"),
        ("completed", "Completed"),
        ("cancelled", "Cancelled"),
    ]

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="scheduled",
        db_index=True
    )

    certificates_released = models.BooleanField(
        default=False,
        help_text="When enabled, delegates can view certificates (and course details) via the public portal.",
    )

    last_notification_type = models.CharField(
    max_length=32,
    default="system",
    blank=True
)

    cancel_reason = models.TextField(blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    # -------------------------------
    # ✅ NEW COURSE CLOSURE DROPDOWNS
    # -------------------------------
    COURSE_REG_CHOICES = [
        ("complete", "Complete"),
        ("separate", "Will send separately"),
    ]

    MATRIX_CHOICES = [
        ("completed", "Completed"),
        ("separate", "Will send separately"),
    ]

    course_registers_status = models.CharField(
        max_length=20,
        choices=COURSE_REG_CHOICES,
        null=True,
        blank=True,
    )

    assessment_matrix_status = models.CharField(
        max_length=20,
        choices=MATRIX_CHOICES,
        null=True,
        blank=True,
    )

    # ✅ Final closure timestamp
    date_completed = models.DateTimeField(null=True, blank=True)

    # -------------------------------
    # MISC
    # -------------------------------
    comments   = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    # -------------------------------
    # HELPERS
    # -------------------------------
    def is_cancelled(self):
        return self.status == "cancelled"

    @property
    def is_dummy_business(self):
        return bool(self.business_id and getattr(self.business, "is_dummy", False))

    _MAP_PLACEHOLDER_POINTS = (
        (52.5, -2.1),
        (52.4895, -1.8986),
    )

    @classmethod
    def is_placeholder_coords(cls, lat, lng):
        if lat is None or lng is None:
            return True
        lat = float(lat)
        lng = float(lng)
        return any(
            abs(lat - plat) < 0.01 and abs(lng - plng) < 0.01
            for plat, plng in cls._MAP_PLACEHOLDER_POINTS
        )

    def effective_precise_lat(self):
        if self.admin_precise_lat is not None and self.admin_precise_lng is not None:
            return float(self.admin_precise_lat)
        if (
            self.precise_lat is not None
            and self.precise_lng is not None
            and not self.is_placeholder_coords(self.precise_lat, self.precise_lng)
        ):
            return self.precise_lat
        return None

    def effective_precise_lng(self):
        if self.admin_precise_lat is not None and self.admin_precise_lng is not None:
            return float(self.admin_precise_lng)
        if (
            self.precise_lat is not None
            and self.precise_lng is not None
            and not self.is_placeholder_coords(self.precise_lat, self.precise_lng)
        ):
            return self.precise_lng
        return None

    def has_effective_precise_location(self):
        return (
            self.effective_precise_lat() is not None
            and self.effective_precise_lng() is not None
        )

    def effective_plus_code(self):
        lat = self.effective_precise_lat()
        lng = self.effective_precise_lng()
        if lat is None or lng is None:
            return ""
        from .utils.plus_codes import coordinates_to_plus_code

        return coordinates_to_plus_code(lat, lng)

    def plus_code_url(self):
        from .utils.plus_codes import plus_code_url

        return plus_code_url(self.effective_plus_code())

    def plus_code_maps_url(self):
        from .utils.plus_codes import plus_code_maps_url

        return plus_code_maps_url(self.effective_plus_code())

    def training_address_for_map(self):
        loc = self.training_location
        if not loc:
            return ""
        parts = [
            loc.property_name or "",
            loc.address_line or "",
            loc.town or "",
            loc.postcode or "",
        ]
        return ", ".join(p for p in parts if p and str(p).strip())

    def sync_precise_from_admin(self, save=False, force=False):
        """Copy admin baseline coordinates into precise_*."""
        admin_lat = self.admin_precise_lat
        admin_lng = self.admin_precise_lng
        precise_lat = self.precise_lat
        precise_lng = self.precise_lng

        updates = {}

        if admin_lat is None and admin_lng is None:
            if precise_lat is not None and precise_lng is not None:
                if not self.is_placeholder_coords(precise_lat, precise_lng):
                    updates["admin_precise_lat"] = precise_lat
                    updates["admin_precise_lng"] = precise_lng
                    admin_lat = precise_lat
                    admin_lng = precise_lng

        if admin_lat is not None and admin_lng is not None:
            new_lat = float(admin_lat)
            new_lng = float(admin_lng)
            should_sync = force or (
                precise_lat is None
                or precise_lng is None
                or self.is_placeholder_coords(precise_lat, precise_lng)
            )
            if should_sync and (precise_lat != new_lat or precise_lng != new_lng):
                updates["precise_lat"] = new_lat
                updates["precise_lng"] = new_lng

        if not updates:
            return False

        for field, value in updates.items():
            setattr(self, field, value)
        if save:
            self.save(update_fields=list(updates.keys()))
        return True

    @property
    def invoice(self):
        """Primary invoice for the lead instructor (backwards compatible)."""
        if self.instructor_id:
            inv = self.invoices.filter(instructor_id=self.instructor_id).first()
            if inv:
                return inv
        return self.invoices.first()

    def invoice_for(self, personnel):
        """Invoice belonging to a specific instructor on this booking."""
        if not personnel:
            return self.invoice
        return self.invoices.filter(instructor_id=personnel.pk).first()

    def __str__(self):
        return self.course_reference or "(pending)"

    def _generate_unique_reference(self):
        import random, string
        base = (self.course_type.code if self.course_type_id else "COURSE").upper()
        for _ in range(80):
            suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
            ref = f"{base}-{suffix}"
            if not Booking.objects.filter(course_reference=ref).exists():
                return ref
        raise ValueError("Could not generate unique course reference")

    def save(self, *args, **kwargs):
        if not self.course_reference:
            self.course_reference = self._generate_unique_reference()
        super().save(*args, **kwargs)


class BookingDay(models.Model):
    booking = models.ForeignKey('Booking', on_delete=models.CASCADE, related_name='days')
    date = models.DateField()  # (you already renamed from day_date)
    start_time = models.TimeField(blank=True, null=True)
    end_time   = models.TimeField(null=True, blank=True)
    # Make it safe to migrate without a one-off default:
    day_code = models.CharField(max_length=64, blank=True, default='', db_index=True)

    instructor = models.ForeignKey(
        'Personnel',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='booking_days'
    )
    
    note = models.CharField(max_length=255, blank=True, default="")
    departure_reminder_sent_at = models.DateTimeField(null=True, blank=True)

    def save(self, *args, **kwargs):
        # auto-generate if missing
        if not self.day_code and self.booking_id and self.date:
            # assumes Booking has course_reference
            self.day_code = f"{self.date.strftime('%Y%m%d')}-{self.booking.course_reference}"
        super().save(*args, **kwargs)


class CourseSwapStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    DECLINED = "declined", "Declined"
    CANCELLED = "cancelled", "Cancelled"


class CourseSwap(models.Model):
    """
    Instructor-initiated offer to transfer a booking to another instructor.
    The booking only moves when the recipient accepts.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking = models.ForeignKey(
        "Booking",
        on_delete=models.CASCADE,
        related_name="swap_requests",
    )
    from_instructor = models.ForeignKey(
        "Personnel",
        on_delete=models.CASCADE,
        related_name="course_swaps_offered",
    )
    to_instructor = models.ForeignKey(
        "Personnel",
        on_delete=models.CASCADE,
        related_name="course_swaps_received",
    )
    status = models.CharField(
        max_length=16,
        choices=CourseSwapStatus.choices,
        default=CourseSwapStatus.PENDING,
        db_index=True,
    )
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    responded_at = models.DateTimeField(null=True, blank=True)
    from_instructor_seen_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the offering instructor viewed the accept/decline outcome.",
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["booking"],
                condition=Q(status="pending"),
                name="uniq_pending_course_swap_per_booking",
            ),
        ]

    def __str__(self):
        ref = getattr(self.booking, "course_reference", "") or str(self.booking_id)
        return f"{ref}: {self.from_instructor} → {self.to_instructor} ({self.status})"


class EmergencyTakeover(models.Model):
    """
    Audit record when an instructor takes over another instructor's course day(s)
    at short notice (sickness etc.).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking = models.ForeignKey(
        "Booking",
        on_delete=models.CASCADE,
        related_name="emergency_takeovers",
    )
    taken_by = models.ForeignKey(
        "Personnel",
        on_delete=models.CASCADE,
        related_name="emergency_takeovers_performed",
    )
    replaced_instructor = models.ForeignKey(
        "Personnel",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="emergency_takeovers_replaced",
    )
    days = models.ManyToManyField("BookingDay", related_name="emergency_takeovers")
    fee_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    mileage_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    is_full_takeover = models.BooleanField(default=False)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        ref = getattr(self.booking, "course_reference", "") or str(self.booking_id)
        return f"{ref}: emergency cover by {self.taken_by}"


class Attendance(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking_day   = models.ForeignKey(BookingDay, on_delete=models.CASCADE, related_name="attendances")
    delegate_name = models.CharField(max_length=200)
    delegate_email = models.EmailField(blank=True)
    result = models.CharField(max_length=50, blank=True)
    notes  = models.TextField(blank=True)
    signed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.delegate_name} @ {self.booking_day}"

@receiver(post_save, sender=Business)
def sync_business_training_location(sender, instance: Business, created, **kwargs):
    """
    Keep a default TrainingLocation in sync with Business when the
    'Add as training location' toggle is changed.
    """
    name = instance.name
    if instance.add_as_training_location:
        loc = TrainingLocation.objects.filter(business=instance, name=name).order_by("id").first()
        if not loc:
            loc = TrainingLocation(business=instance, name=name)
        # copy fields
        loc.address_line = instance.address_line
        loc.town         = instance.town
        loc.postcode     = instance.postcode
        loc.contact_name = instance.contact_name
        loc.telephone    = instance.telephone
        loc.email        = instance.email
        loc.save()
    else:
        TrainingLocation.objects.filter(business=instance, name=name).delete()

class CourseOutcome(models.TextChoices):
    PENDING = "pending", "Pending"
    PASS    = "pass",    "Pass"
    FAIL    = "fail",    "Fail"
    DNF     = "dnf",     "Did not finish"

class DelegateRegister(models.Model):
    class HealthStatus(models.TextChoices):
        FIT            = "fit", "I am fit to take part in all the mental and physical aspects of today's course."
        AGREED         = "agreed_adjustments", "I have a mental or physical impairment that I have discussed with the instructor already and am happy to continue with agreed alterations."
        WILL_DISCUSS   = "will_discuss", "I have a mental or physical impairment and will discuss this with the instructor."
        NOT_FIT        = "not_fit", "I do not feel mentally and/or physically well enough to take part in training today and will speak with my own manager about this."

    name = models.CharField(max_length=100)
    date_of_birth = models.DateField()
    job_title = models.CharField(max_length=100)
    employee_id = models.CharField(max_length=50, blank=True)
    date = models.DateField(default=timezone.localdate)  # forced to 'today' server-side
    instructor = models.ForeignKey("Personnel", on_delete=models.PROTECT)
    health_status = models.CharField(
        max_length=24,
        choices=HealthStatus.choices,
        default=HealthStatus.FIT,
    )
    outcome = models.CharField(
        max_length=8,
        choices=CourseOutcome.choices,
        default=CourseOutcome.PENDING,
        null=False,
        blank=False,    
    )
    notes = models.TextField(blank=True)
    booking_day = models.ForeignKey("BookingDay", on_delete=models.PROTECT, related_name="registers")
    created_at = models.DateTimeField(auto_now_add=True)
    certificate_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="If set, this name will be used on certificates instead of the original delegate name.",
    )

    def certificate_display_name(self) -> str:
        """
        Name that should appear on certificates.
        Falls back to the original register name if no override set.
        """
        return (self.certificate_name or self.name or "").strip()

    def save(self, *args, **kwargs):
        # Proper case the name, keeping the capital after a hyphen or apostrophe.
        if self.name:
            from .utils.names import title_case_person_name

            self.name = title_case_person_name(self.name)
        super().save(*args, **kwargs)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.date})"

    def health_badge_class(self) -> str:
        return {
            self.HealthStatus.FIT: "bg-success",
            self.HealthStatus.AGREED: "bg-warning text-dark",
            self.HealthStatus.WILL_DISCUSS: "",  # we'll style inline orange
            self.HealthStatus.NOT_FIT: "bg-danger",
        }.get(self.health_status, "bg-secondary")

class CertificateNameChange(models.Model):
    """
    Permanent audit log of any certificate-name overrides.
    Does NOT change the underlying DelegateRegister.name, only the
    certificate_name field and keeps each change + reason.
    """
    register = models.ForeignKey(
        "DelegateRegister",
        on_delete=models.CASCADE,
        related_name="certificate_name_changes",
    )
    old_name = models.CharField(max_length=255)
    new_name = models.CharField(max_length=255)
    reason   = models.TextField()
    changed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="certificate_name_changes",
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-changed_at"]

    def __str__(self):
        return f"{self.old_name} → {self.new_name} ({self.changed_at:%Y-%m-%d})"

class CourseCompetency(models.Model):
    course_type = models.ForeignKey(
        CourseType,
        on_delete=models.CASCADE,
        related_name="competencies",
    )
    code = models.CharField(
        max_length=32, blank=True,
        help_text="Optional short code (e.g. C1, ABC-01)."
    )
    name = models.CharField(
        max_length=200,
        help_text="What the delegate must be able to do (visible to instructors)."
    )
    description = models.TextField(blank=True)
    sort_order = models.PositiveIntegerField(default=0, help_text="Controls display order.")
    is_active = models.BooleanField(default=True)
    is_optional = models.BooleanField(default=False)

    class Meta:
        ordering = ["sort_order", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["course_type", "name"],
                name="uniq_competency_name_per_course_type",
            ),
        ]

    def __str__(self):
        return f"{self.course_type.code or self.course_type.name}: {self.name}"

class AssessmentLevel(models.TextChoices):
    NOT_ASSESSED = "na", "Not assessed"
    NEEDS_IMPROVEMENT = "ni", "Needs improvement"
    COMPETENT = "c", "Competent"
    EXCEEDED = "e", "Exceeded"

class CompetencyAssessment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    register = models.ForeignKey("DelegateRegister", on_delete=models.CASCADE, related_name="assessments")
    course_competency = models.ForeignKey("CourseCompetency", on_delete=models.PROTECT, related_name="assessments")
    level = models.CharField(max_length=2, choices=AssessmentLevel.choices, default=AssessmentLevel.NOT_ASSESSED)
    notes = models.CharField(max_length=255, blank=True)
    is_locked = models.BooleanField(
        default=False,
        help_text="If true, instructors cannot unset this tick."
    )
    source_note = models.CharField(
        max_length=255,
        blank=True,
        help_text="Optional note (e.g. 'carried from DNF on 2025-03-12')."
    )
    assessed_by = models.ForeignKey("Personnel", on_delete=models.PROTECT)
    assessed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ("register", "course_competency")
        ordering = ["register_id", "course_competency_id"]

# --- Feedback ---------------------------------------------------------------
import uuid
from django.db import models
from django.utils import timezone

# ✅ Make sure these imports exist at the top of the same file
from .models import Booking, CourseType, Personnel


class FeedbackResponse(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # ✅ THIS IS THE MISSING LINK THAT FIXES EVERYTHING
    booking = models.ForeignKey(
        Booking,
        on_delete=models.CASCADE,
        related_name="feedback_responses",
        null=True,
        blank=True
    )

    course_type = models.ForeignKey(
        CourseType,
        on_delete=models.PROTECT,
        related_name="feedback"
    )

    date = models.DateField(default=timezone.localdate)  # auto "today"

    # still called "instructor" but now links to Personnel
    instructor = models.ForeignKey(
        Personnel,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="feedback",
    )

    # -------------------------
    # 1–5 ratings
    # -------------------------
    prior_knowledge = models.PositiveSmallIntegerField(null=True, blank=True)
    post_knowledge  = models.PositiveSmallIntegerField(null=True, blank=True)

    q_purpose_clear     = models.PositiveSmallIntegerField(null=True, blank=True)
    q_personal_needs    = models.PositiveSmallIntegerField(null=True, blank=True)
    q_exercises_useful  = models.PositiveSmallIntegerField(null=True, blank=True)

    q_structure         = models.PositiveSmallIntegerField(null=True, blank=True)
    q_pace              = models.PositiveSmallIntegerField(null=True, blank=True)
    q_content_clear     = models.PositiveSmallIntegerField(null=True, blank=True)
    q_instructor_knowledge = models.PositiveSmallIntegerField(null=True, blank=True)
    q_materials_quality = models.PositiveSmallIntegerField(null=True, blank=True)
    q_books_quality     = models.PositiveSmallIntegerField(null=True, blank=True)

    q_venue_suitable    = models.PositiveSmallIntegerField(null=True, blank=True)

    q_benefit_at_work   = models.PositiveSmallIntegerField(null=True, blank=True)
    q_benefit_outside   = models.PositiveSmallIntegerField(null=True, blank=True)

    overall_rating = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Delegate’s overall 1–5 rating"
    )

    # -------------------------
    # Free text + contact
    # -------------------------
    comments = models.TextField(blank=True)

    wants_callback = models.BooleanField(default=False)
    contact_name   = models.CharField(max_length=200, blank=True)
    contact_email  = models.EmailField(blank=True)
    contact_phone  = models.CharField(max_length=50, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    # -------------------------
    # Helpers
    # -------------------------
    def overall_average(self):
        vals = [
            v for v in [
                self.prior_knowledge, self.post_knowledge,
                self.q_purpose_clear, self.q_personal_needs, self.q_exercises_useful,
                self.q_structure, self.q_pace, self.q_content_clear,
                self.q_instructor_knowledge,
                self.q_materials_quality, self.q_books_quality,
                self.q_venue_suitable,
                self.q_benefit_at_work, self.q_benefit_outside
            ] if v is not None
        ]
        return round(sum(vals) / len(vals), 2) if vals else None

    def __str__(self):
        ref = self.course_type.code if self.course_type_id else "Course"
        return f"Feedback {ref} {self.date} ({self.id})"



class Invoice(models.Model):
    STATUS_CHOICES = [
        ("draft", "Awaiting completion and sending"),
        ("sent", "Sent"),
        ("viewed", "Viewed"),
        ("paid", "Paid"),
        ("awaiting_review", "Awaiting instructor review"),
        ("rejected", "Rejected"),
    ]
    booking = models.ForeignKey("Booking", on_delete=models.CASCADE, related_name="invoices")
    instructor = models.ForeignKey("Personnel", on_delete=models.CASCADE)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft")
    base_fee_override = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="When set, used instead of booking.instructor_fee for this instructor's invoice.",
    )

    invoice_date = models.DateField(null=True, blank=True)
    instructor_ref = models.CharField(max_length=100, blank=True)

    # Bank details (pre-filled from Instructor; editable on draft)
    account_name = models.CharField(max_length=200, blank=True)
    sort_code = models.CharField(max_length=20, blank=True)
    account_number = models.CharField(max_length=30, blank=True)

    admin_comment = models.TextField(blank=True, null=True)
    staff_comment = models.TextField(blank=True, null=True)

    date_sent = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def is_locked(self):
        # cannot edit after sent unless admin sets awaiting_review
        return self.status not in ("draft", "awaiting_review")

    @property
    def base_amount(self):
        if self.base_fee_override is not None:
            return self.base_fee_override
        if self.instructor_id and self.instructor_id == self.booking.instructor_id:
            return self.booking.instructor_fee or 0
        return 0

    @property
    def total(self):
        add = sum(x.amount for x in self.items.all())
        return (self.base_amount or 0) + add


class InvoiceItem(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="items")
    description = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)

# models.py

class Exam(models.Model):
    course_type = models.ForeignKey("CourseType", on_delete=models.CASCADE, related_name="exams")
    sequence = models.PositiveIntegerField(default=1)
    title = models.CharField(max_length=200, blank=True)

    exam_code = models.CharField(
        max_length=40,
        unique=True,
        editable=False,
        db_index=True,
        blank=True,
    )

    # --- NEW: pass mark + viva options ---
    pass_mark_percent = models.PositiveSmallIntegerField(
        default=80,  # was 70; now default 80%
        validators=[MinValueValidator(1), MaxValueValidator(100)],
        help_text="Passing threshold as a percentage (1–100).",
    )
    allow_viva = models.BooleanField(default=False)
    viva_pass_percent = models.PositiveSmallIntegerField(
        null=True, blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
        help_text="If enabled, viva threshold (must be between pass−10 and pass−1).",
    )
    # -------------------------------------

    class Meta:
        unique_together = ("course_type", "sequence")
        ordering = ["sequence"]

    def _computed_exam_code(self) -> str:
        base = (self.course_type.code or "").upper()
        return f"{base}{int(self.sequence):02d}"

    def _computed_title(self) -> str:
        return f"{self.course_type.name}: Exam {int(self.sequence):02d}"

    # Validation for viva rules
    def clean(self):
        super().clean()

        p = int(self.pass_mark_percent or 0)

        if not self.allow_viva:
            # If viva disabled, ensure field is cleared
            self.viva_pass_percent = None
            return

        # Viva enabled → enforce range [pass−10, pass−1]
        min_viva = max(1, p - 10)
        max_viva = max(1, p - 1)

        # If not provided, default to pass−10 (clamped just in case)
        if self.viva_pass_percent is None:
            self.viva_pass_percent = min(max_viva, max(min_viva, p - 10))
            return

        v = int(self.viva_pass_percent)
        if not (min_viva <= v <= max_viva):
            raise ValidationError({
                "viva_pass_percent": (
                    f"Viva must be between {min_viva}% and {max_viva}% "
                    f"(given {v}%, pass mark is {p}%)."
                )
            })

    def save(self, *args, **kwargs):
        if self.course_type_id and self.sequence:
            self.exam_code = self._computed_exam_code()

            auto_title = self._computed_title()
            simple_default = f"Exam {int(self.sequence)}"

            if not self.pk:
                if not (self.title or "").strip() or (self.title or "").strip() == simple_default:
                    self.title = auto_title
            else:
                try:
                    old = type(self).objects.get(pk=self.pk)
                    old_auto = f"{old.course_type.name}: Exam {int(old.sequence):02d}"
                    if (self.title or "").strip() in ("", simple_default, old_auto):
                        self.title = auto_title
                except type(self).DoesNotExist:
                    if not (self.title or "").strip() or (self.title or "").strip() == simple_default:
                        self.title = auto_title

        # Run validators (incl. viva rules) before saving
        self.full_clean()
        super().save(*args, **kwargs)

class ExamQuestion(models.Model):
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="questions")
    order = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    text = models.TextField()
    image_url = models.URLField(
        blank=True,
        null=True,
        max_length=500,
        help_text="Optional external image URL. A file stored on the question is used instead when one has been uploaded.",
    )
    image_filename = models.CharField(max_length=255, blank=True, default="")
    image_content_type = models.CharField(max_length=100, blank=True, default="")
    image_data = models.BinaryField(null=True, blank=True)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return f"Q{self.order}: {self.text[:60]}"

    def display_image_url(self) -> str:
        """Address the exam page should use. Stored files are served by this app."""
        if self.pk and self.image_filename:
            from django.urls import reverse
            return reverse("exam_question_image", args=[self.pk])
        return (self.image_url or "").strip()


class ExamAnswer(models.Model):
    question = models.ForeignKey(ExamQuestion, on_delete=models.CASCADE, related_name="answers")
    order = models.PositiveIntegerField(default=1)
    text = models.CharField(max_length=500)
    is_correct = models.BooleanField(default=False)

    class Meta:
        ordering = ["order", "id"]
        constraints = [
            # at most one correct answer per question (DB-level)
            models.UniqueConstraint(
                fields=["question"],
                condition=Q(is_correct=True),
                name="unique_correct_answer_per_question",
            )
        ]

    def __str__(self):
        return f"{self.text}{' (correct)' if self.is_correct else ''}"
    
class ExamAttempt(models.Model):
    exam = models.ForeignKey("Exam", on_delete=models.CASCADE, related_name="attempts")
    booking = models.ForeignKey(
        "Booking",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="exam_attempts",
    )

    # Snapshot of who started (from the start page)
    delegate_name = models.CharField(max_length=120)
    date_of_birth = models.DateField()
    instructor = models.ForeignKey("Personnel", null=True, blank=True, on_delete=models.SET_NULL)
    exam_date = models.DateField()

    # Timing
    started_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)

    # Scoring
    score_correct = models.PositiveIntegerField(default=0)
    total_questions = models.PositiveIntegerField(default=0)
    question_order = models.JSONField(default=list, blank=True)
    passed = models.BooleanField(default=False)
    viva_eligible = models.BooleanField(default=False)

    # Viva / oral follow-up
    viva_notes = models.TextField(blank=True)
    viva_decided_at = models.DateTimeField(null=True, blank=True)
    viva_decided_by = models.ForeignKey(
        "Personnel",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="viva_decisions",
    )

    # Retake management
    retake_authorised = models.BooleanField(default=False)
    retake_authorised_until = models.DateTimeField(null=True, blank=True)

    # ----- Methods / helpers -------------------------------------

    def remaining_seconds(self) -> int:
        """
        Number of seconds remaining before the attempt expires.
        Returns 0 if already finished.
        """
        if self.finished_at:
            return 0
        now = timezone.now()
        return max(0, int((self.expires_at - now).total_seconds()))

    def is_viva_pending(self) -> bool:
        """
        True if this attempt is eligible for viva and a decision
        has not yet been recorded.
        """
        return self.viva_eligible and not self.viva_decided_at

    @property
    def attempt_number(self) -> int:
        """
        1-based index of this attempt for the same delegate / exam / DOB,
        ordered by started_at then primary key.
        """
        qs = (
            ExamAttempt.objects
            .filter(
                exam=self.exam,
                delegate_name__iexact=self.delegate_name,
                date_of_birth=self.date_of_birth,
            )
            .order_by("started_at", "pk")
            .values_list("pk", flat=True)
        )
        try:
            return list(qs).index(self.pk) + 1
        except ValueError:
            return 1  # fallback if not in the list


class ExamAttemptAnswer(models.Model):
    attempt = models.ForeignKey("ExamAttempt", on_delete=models.CASCADE, related_name="answers")
    question = models.ForeignKey("ExamQuestion", on_delete=models.CASCADE)
    answer = models.ForeignKey("ExamAnswer", null=True, blank=True, on_delete=models.SET_NULL)
    is_correct = models.BooleanField(default=False)

    class Meta:
        unique_together = ("attempt", "question")


class AccidentReport(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    date = models.DateField()
    time = models.TimeField(null=True, blank=True)

    location = models.CharField(max_length=255)
    injured_name = models.CharField(max_length=255)
    injured_address = models.TextField(blank=True, null=True)

    anonymized_at = models.DateTimeField(null=True, blank=True)

    what_happened = models.TextField()
    injuries_sustained = models.TextField()
    actions_carried_out = models.TextField()
    actions_prevent_recurrence = models.TextField()

    first_aider_name = models.CharField(max_length=255)
    reporter_name = models.CharField(max_length=255)

    booking = models.ForeignKey(
        "Booking",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="accident_reports",
    )

    reported_to = models.ForeignKey(
        "Personnel",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="accident_reports_received",
        verbose_name="Reported to",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Incident at {self.location} on {self.date}"
    
    def anonymize(self):
        if self.anonymized_at:
            return
        self.injured_name = "Anonymised"
        self.injured_address = "Anonymised"
        self.first_aider_name = "Anonymised"
        self.reporter_name = "Anonymised"
        self.anonymized_at = timezone.now()
        self.save(update_fields=[
            "injured_name",
            "injured_address",
            "first_aider_name",
            "reporter_name",
            "anonymized_at",
        ])

class RegisterEmailLog(models.Model):
    day = models.ForeignKey("BookingDay", on_delete=models.CASCADE, related_name="register_email_logs")
    recipients = models.TextField()  # comma-separated list
    subject = models.TextField(blank=True)
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} -> {self.recipients}"

class LogoOverride(models.Model):
    file_name = models.CharField(
        max_length=200,
        help_text="File in static/training/img/, e.g. logo_crown.png",
    )
    active = models.BooleanField(default=False)
    reason = models.CharField(max_length=200, blank=True)
    starts_at = models.DateTimeField(blank=True, null=True)
    ends_at = models.DateTimeField(blank=True, null=True)
    priority = models.PositiveSmallIntegerField(default=10, help_text="Lower = higher priority")

    class Meta:
        ordering = ["priority", "-id"]

    def is_active_now(self):
        now = timezone.now()
        if not self.active:
            return False
        if self.starts_at and now < self.starts_at:
            return False
        if self.ends_at and now > self.ends_at:
            return False
        return True

    def __str__(self):
        return f"{self.file_name} ({'ON' if self.active else 'off'})"

class MetaSetting(models.Model):
    key = models.CharField(max_length=100, unique=True)
    value = models.TextField(blank=True)

    class Meta:
        verbose_name = "Meta setting"
        verbose_name_plural = "Meta settings"

    def __str__(self):
        return f"{self.key}: {self.value}"

class Resource(models.Model):

    CATEGORY_CHOICES = [
        ("slides", "Slides / Presentations"),
        ("handout", "Handouts"),
        ("document", "Documents"),
        ("policy", "Policies"),
        ("template", "Templates"),
        ("other", "Other"),
    ]

    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)

    course_type = models.ForeignKey(
        CourseType,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="resources"
    )

    category = models.CharField(
        max_length=20,
        choices=CATEGORY_CHOICES,
        default="document"
    )

    url = models.URLField(help_text="OneDrive share link")

    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order", "title"]

    def __str__(self):
        return self.title


class TelegramNotification(models.Model):
    """Audit log of Telegram messages sent for a booking."""

    booking = models.ForeignKey(
        Booking,
        on_delete=models.CASCADE,
        related_name="telegram_notifications",
    )
    personnel = models.ForeignKey(
        Personnel,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="telegram_notifications",
    )
    notification_type = models.CharField(max_length=32)
    message_id = models.CharField(max_length=64, blank=True, default="")
    sent_at = models.DateTimeField(auto_now_add=True)
    success = models.BooleanField(default=True)
    error_text = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-sent_at"]

    def __str__(self):
        return f"{self.notification_type} for booking {self.booking_id}"


class EmailNotification(models.Model):
    """Audit log of booking notification emails sent to instructors."""

    booking = models.ForeignKey(
        Booking,
        on_delete=models.CASCADE,
        related_name="email_notifications",
    )
    personnel = models.ForeignKey(
        Personnel,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="email_notifications",
    )
    notification_type = models.CharField(max_length=32)
    provider_message_id = models.CharField(max_length=128, blank=True, default="")
    sent_at = models.DateTimeField(auto_now_add=True)
    success = models.BooleanField(default=True)
    error_text = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-sent_at"]

    def __str__(self):
        return f"{self.notification_type} email for booking {self.booking_id}"


class InstructorCourseRequestStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    DECLINED = "declined", "Declined"


class InstructorCourseRequest(models.Model):
    """Instructor request to be allowed to deliver a course type."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    instructor = models.ForeignKey(
        Personnel,
        on_delete=models.CASCADE,
        related_name="course_delivery_requests",
    )
    course_type = models.ForeignKey(
        CourseType,
        on_delete=models.CASCADE,
        related_name="delivery_requests",
    )
    status = models.CharField(
        max_length=16,
        choices=InstructorCourseRequestStatus.choices,
        default=InstructorCourseRequestStatus.PENDING,
        db_index=True,
    )
    message = models.TextField(blank=True, default="")
    admin_note = models.TextField(blank=True, default="")
    requested_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_course_delivery_requests",
    )

    class Meta:
        ordering = ["-requested_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["instructor", "course_type"],
                condition=models.Q(status=InstructorCourseRequestStatus.PENDING),
                name="uniq_pending_instructor_course_request",
            )
        ]

    def __str__(self):
        return f"{self.instructor} → {self.course_type} ({self.status})"


class StaffInboxItemKind(models.TextChoices):
    COURSE_DELIVERY_REQUEST = "course_delivery_request", "Course delivery request"
    COURSE_DELIVERY_OUTCOME = "course_delivery_outcome", "Course delivery update"
    COURSE_SWAP_INCOMING = "course_swap_incoming", "Course cover request"
    COURSE_SWAP_OUTCOME = "course_swap_outcome", "Course cover update"


class StaffInboxItemStatus(models.TextChoices):
    OPEN = "open", "Open"
    RESOLVED = "resolved", "Resolved"


class StaffInboxItem(models.Model):
    """Per-personnel inbox for admin and instructor notifications."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recipient = models.ForeignKey(
        Personnel,
        on_delete=models.CASCADE,
        related_name="inbox_items",
        null=True,
        blank=True,
    )
    kind = models.CharField(max_length=40, choices=StaffInboxItemKind.choices, db_index=True)
    status = models.CharField(
        max_length=16,
        choices=StaffInboxItemStatus.choices,
        default=StaffInboxItemStatus.OPEN,
        db_index=True,
    )
    summary = models.CharField(max_length=500)
    course_delivery_request = models.ForeignKey(
        InstructorCourseRequest,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="inbox_items",
    )
    course_swap = models.ForeignKey(
        CourseSwap,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="inbox_items",
    )
    read_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_inbox_items",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "status", "read_at"]),
        ]

    def __str__(self):
        return self.summary

    @property
    def is_unread(self):
        return self.read_at is None