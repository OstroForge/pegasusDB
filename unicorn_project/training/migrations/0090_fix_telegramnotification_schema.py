from django.db import migrations


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


def fix_telegramnotification_schema(apps, schema_editor):
    """
    Production may still have a legacy training_telegramnotification table from an
    earlier telegram migration. Migration 0084 only creates the table when it is
    missing, so the old schema (without personnel_id) was left in place.
    """
    connection = schema_editor.connection
    table = "training_telegramnotification"

    if not _table_exists(connection, table):
        TelegramNotification = apps.get_model("training", "TelegramNotification")
        schema_editor.create_model(TelegramNotification)
        return

    if "personnel_id" in _notification_columns(connection):
        return

    with connection.cursor() as cursor:
        if connection.vendor == "postgresql":
            cursor.execute(f"DROP TABLE {table} CASCADE")
        elif connection.vendor == "sqlite":
            cursor.execute(f"DROP TABLE {table}")

    TelegramNotification = apps.get_model("training", "TelegramNotification")
    schema_editor.create_model(TelegramNotification)


class Migration(migrations.Migration):

    dependencies = [
        ("training", "0089_remove_booking_what3words"),
    ]

    operations = [
        migrations.RunPython(
            fix_telegramnotification_schema,
            migrations.RunPython.noop,
        ),
    ]
