from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("training", "0106_examquestion_stored_image"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="is_imported",
            field=models.BooleanField(
                default=False,
                help_text="Set for courses brought in from an old assessment matrix.",
            ),
        ),
    ]
