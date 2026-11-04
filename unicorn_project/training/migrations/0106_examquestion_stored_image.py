from pathlib import Path

from django.db import migrations, models


BUNDLED_IMAGES = {
    "exam_faaw_02_12.png": "https://i.postimg.cc/ZRv9z8zj/laceration.png",
    "exam_faaw_02_16.png": "https://i.postimg.cc/NfNkYwyQ/embedded-object.png",
}


def import_bundled_exam_images(apps, schema_editor):
    ExamQuestion = apps.get_model("training", "ExamQuestion")
    root = Path(__file__).resolve().parent.parent / "exam_images"
    for filename, url in BUNDLED_IMAGES.items():
        path = root / filename
        if not path.is_file():
            continue
        ExamQuestion.objects.filter(image_url=url).update(
            image_filename=filename,
            image_content_type="image/png",
            image_data=path.read_bytes(),
        )


class Migration(migrations.Migration):

    dependencies = [
        ("training", "0105_booking_certificates_released"),
    ]

    operations = [
        migrations.AddField(
            model_name="examquestion",
            name="image_content_type",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="examquestion",
            name="image_data",
            field=models.BinaryField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="examquestion",
            name="image_filename",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.RunPython(import_bundled_exam_images, migrations.RunPython.noop),
    ]
