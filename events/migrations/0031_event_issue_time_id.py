from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0030_backfill_event_counts_per_hour"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="event",
            index=models.Index(fields=["issue", "timestamp", "id"], name="event_issue_time_id"),
        ),
    ]
