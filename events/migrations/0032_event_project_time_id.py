from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0031_event_issue_time_id"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="event",
            index=models.Index(fields=["project", "timestamp", "id"], name="event_project_time_id"),
        ),
    ]
