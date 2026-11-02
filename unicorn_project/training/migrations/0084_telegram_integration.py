from django.db import migrations, models
import django.db.models.deletion


def drop_legacy_telegram_tables(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        if schema_editor.connection.vendor == "sqlite":
            cursor.execute("DROP TABLE IF EXISTS training_telegramnotification")
            cursor.execute("DROP TABLE IF EXISTS training_telegramaccount")
        elif schema_editor.connection.vendor == "postgresql":
            cursor.execute("DROP TABLE IF EXISTS training_telegramnotification CASCADE")
            cursor.execute("DROP TABLE IF EXISTS training_telegramaccount CASCADE")


def _personnel_columns(connection):
    with connection.cursor() as cursor:
        if connection.vendor == "postgresql":
            cursor.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'training_personnel'
                """
            )
            return {row[0] for row in cursor.fetchall()}
        if connection.vendor == "sqlite":
            cursor.execute("PRAGMA table_info(training_personnel)")
            return {row[1] for row in cursor.fetchall()}
    return set()


def _table_exists(connection, table_name):
    with connection.cursor() as cursor:
        if connection.vendor == "postgresql":
            cursor.execute("SELECT to_regclass(%s)", [table_name])
            return cursor.fetchone()[0] is not None
        if connection.vendor == "sqlite":
            cursor.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=%s",
                [table_name],
            )
            return cursor.fetchone() is not None
    return False


def _notification_columns(connection):
    with connection.cursor() as cursor:
        if connection.vendor == "postgresql":
            cursor.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'training_telegramnotification'
                """
            )
            return {row[0] for row in cursor.fetchall()}
        if connection.vendor == "sqlite":
            cursor.execute("PRAGMA table_info(training_telegramnotification)")
            return {row[1] for row in cursor.fetchall()}
    return set()


def apply_telegram_integration(apps, schema_editor):
    """
    Idempotent: production may already have these columns from the old
    0083_telegram_integration migration before it was renumbered to 0084.
    """
    connection = schema_editor.connection
    existing = _personnel_columns(connection)

    column_sql = {
        "notify_booking_changes_telegram": (
            "ALTER TABLE training_personnel "
            "ADD COLUMN IF NOT EXISTS notify_booking_changes_telegram "
            "boolean NOT NULL DEFAULT false",
            "ALTER TABLE training_personnel "
            "ADD COLUMN notify_booking_changes_telegram bool NOT NULL DEFAULT 0",
        ),
        "notify_new_bookings_telegram": (
            "ALTER TABLE training_personnel "
            "ADD COLUMN IF NOT EXISTS notify_new_bookings_telegram "
            "boolean NOT NULL DEFAULT false",
            "ALTER TABLE training_personnel "
            "ADD COLUMN notify_new_bookings_telegram bool NOT NULL DEFAULT 0",
        ),
        "notify_reminders_telegram": (
            "ALTER TABLE training_personnel "
            "ADD COLUMN IF NOT EXISTS notify_reminders_telegram "
            "boolean NOT NULL DEFAULT false",
            "ALTER TABLE training_personnel "
            "ADD COLUMN notify_reminders_telegram bool NOT NULL DEFAULT 0",
        ),
        "telegram_chat_id": (
            "ALTER TABLE training_personnel "
            "ADD COLUMN IF NOT EXISTS telegram_chat_id "
            "varchar(32) NOT NULL DEFAULT ''",
            "ALTER TABLE training_personnel "
            "ADD COLUMN telegram_chat_id varchar(32) NOT NULL DEFAULT ''",
        ),
        "telegram_linked_at": (
            "ALTER TABLE training_personnel "
            "ADD COLUMN IF NOT EXISTS telegram_linked_at "
            "timestamp with time zone NULL",
            "ALTER TABLE training_personnel "
            "ADD COLUMN telegram_linked_at datetime NULL",
        ),
    }

    for column, (pg_sql, sqlite_sql) in column_sql.items():
        if column in existing:
            continue
        if connection.vendor == "postgresql":
            schema_editor.execute(pg_sql)
        elif connection.vendor == "sqlite":
            schema_editor.execute(sqlite_sql)

    # This runs before TelegramNotification is in the migration state, so the
    # table is created or rebuilt later by 0090_fix_telegramnotification_schema.
    if not _table_exists(connection, "training_telegramnotification"):
        return

    columns = _notification_columns(connection)
    if "personnel_id" not in columns:
        return


class Migration(migrations.Migration):

    dependencies = [
        ("training", "0083_coursetype_is_suspended"),
    ]

    operations = [
        migrations.RunPython(drop_legacy_telegram_tables, migrations.RunPython.noop),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="personnel",
                    name="notify_booking_changes_telegram",
                    field=models.BooleanField(
                        default=False,
                        help_text="Telegram alert when a booking is updated or cancelled.",
                    ),
                ),
                migrations.AddField(
                    model_name="personnel",
                    name="notify_new_bookings_telegram",
                    field=models.BooleanField(
                        default=False,
                        help_text="Telegram alert when a new booking is assigned to you.",
                    ),
                ),
                migrations.AddField(
                    model_name="personnel",
                    name="notify_reminders_telegram",
                    field=models.BooleanField(
                        default=False,
                        help_text="Telegram reminder before upcoming bookings.",
                    ),
                ),
                migrations.AddField(
                    model_name="personnel",
                    name="telegram_chat_id",
                    field=models.CharField(blank=True, default="", max_length=32),
                ),
                migrations.AddField(
                    model_name="personnel",
                    name="telegram_linked_at",
                    field=models.DateTimeField(blank=True, null=True),
                ),
                migrations.CreateModel(
                    name="TelegramNotification",
                    fields=[
                        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                        ("notification_type", models.CharField(max_length=32)),
                        ("message_id", models.CharField(blank=True, default="", max_length=64)),
                        ("sent_at", models.DateTimeField(auto_now_add=True)),
                        ("success", models.BooleanField(default=True)),
                        ("error_text", models.TextField(blank=True, default="")),
                        (
                            "booking",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.CASCADE,
                                related_name="telegram_notifications",
                                to="training.booking",
                            ),
                        ),
                        (
                            "personnel",
                            models.ForeignKey(
                                blank=True,
                                null=True,
                                on_delete=django.db.models.deletion.SET_NULL,
                                related_name="telegram_notifications",
                                to="training.personnel",
                            ),
                        ),
                    ],
                    options={
                        "ordering": ["-sent_at"],
                    },
                ),
            ],
            database_operations=[
                migrations.RunPython(apply_telegram_integration, migrations.RunPython.noop),
            ],
        ),
    ]
