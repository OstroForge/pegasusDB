from django.core.management.base import BaseCommand, CommandError

from unicorn_project.training.models import CourseCompetency, CourseType, Personnel
from unicorn_project.training.utils.assessment_matrix_sheet import (
    course_code_from_title,
    read_assessment_matrix,
)


def _norm(value: str) -> str:
    return " ".join((value or "").replace("\n", " ").split()).casefold()


class Command(BaseCommand):
    help = "Summarise an old assessment-matrix spreadsheet without importing delegates."

    def add_arguments(self, parser):
        parser.add_argument("path", help="Path to an assessment matrix .xlsx file")
        parser.add_argument(
            "--add-retired",
            action="store_true",
            help="Add sheet skills that are not on this course type, marked as no longer used.",
        )

    def handle(self, *args, **options):
        try:
            sheet = read_assessment_matrix(options["path"])
        except (OSError, ValueError, KeyError) as exc:
            raise CommandError(str(exc)) from exc

        code = course_code_from_title(sheet.title)
        course_type = CourseType.objects.filter(code__iexact=code).first() if code else None
        instructor = Personnel.objects.filter(name__iexact=sheet.instructor).first() if sheet.instructor else None

        self.stdout.write(sheet.title or "(no title)")
        if course_type:
            self.stdout.write(f"Course type: {course_type.name} ({course_type.code})")
        else:
            self.stdout.write(self.style.WARNING(
                f"Course type: not recognised from the title ({code or 'no code'})."
            ))
        when = sheet.course_date.strftime("%d %b %Y") if sheet.course_date else "not found"
        self.stdout.write(f"Date: {when}")
        self.stdout.write(f"Location: {sheet.location or 'not found'}")
        if instructor:
            self.stdout.write(f"Instructor: {instructor.name}")
        else:
            self.stdout.write(self.style.WARNING(
                f"Instructor: {sheet.instructor or 'not found'} (no matching personnel record)"
            ))

        passed = sum(1 for person in sheet.delegates if person.passed)
        dnf = sum(1 for person in sheet.delegates if person.did_not_finish)
        self.stdout.write(
            f"Delegates: {len(sheet.delegates)} ({passed} pass, {dnf} DNF)"
        )
        self.stdout.write("")

        existing = []
        if course_type:
            existing = list(CourseCompetency.objects.filter(course_type=course_type))
        by_name = {_norm(item.name): item for item in existing}

        matched = []
        retired = []
        for skill in sheet.skills:
            current = by_name.get(_norm(skill))
            if current:
                state = "in use" if current.is_active else "already marked do not use"
                matched.append(f"  {skill} — {state}")
            else:
                retired.append(skill)

        self.stdout.write("Skills on this sheet:")
        for line in matched:
            self.stdout.write(line)
        for skill in retired:
            self.stdout.write(self.style.WARNING(f"  {skill} — not on the current matrix"))

        self.stdout.write("")
        self.stdout.write("Delegates:")
        for person in sheet.delegates:
            if person.did_not_finish:
                result = "DNF"
            elif person.passed:
                result = "Pass"
            elif person.passed is False:
                result = "Not passed"
            else:
                result = "No pass mark"
            ticked = sum(1 for value in person.ticks.values() if value)
            self.stdout.write(f"  {person.name} — {result} ({ticked}/{len(person.ticks)} ticked)")

        self.stdout.write("")
        if retired and course_type and options["add_retired"]:
            order = max((item.sort_order for item in existing), default=0)
            for skill in retired:
                order += 10
                CourseCompetency.objects.create(
                    course_type=course_type,
                    name=skill,
                    sort_order=order,
                    is_optional=False,
                    is_active=False,
                    description="Retired. Present on older assessment matrices and no longer assessed.",
                )
                self.stdout.write(self.style.SUCCESS(
                    f"Added {skill} to {course_type.code}, marked do not use."
                ))
        elif retired:
            self.stdout.write(
                "Nothing was added. Run again with --add-retired to keep those skills "
                "on the course type without showing them on new matrices."
            )
        else:
            self.stdout.write("Every skill on this sheet is already on the course type.")

        self.stdout.write("No delegates, bookings, or assessment ticks were imported.")
