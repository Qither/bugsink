import datetime
import math
from collections import defaultdict

from django.db.models import Count
from django.db.models.functions import TruncDay, TruncHour, TruncMinute
from rest_framework import serializers

from issues.api_query import MAX_WINDOW_DAYS, RFC3339DateTimeField

from .models import Event


MAX_STATS_BUCKETS = 200
INTERVAL_SECONDS = {
    "5m": 5 * 60,
    "15m": 15 * 60,
    "1h": 60 * 60,
    "1d": 24 * 60 * 60,
}


class EventStatsQuerySerializer(serializers.Serializer):
    project = serializers.IntegerField(required=True)
    start = RFC3339DateTimeField(required=True)
    end = RFC3339DateTimeField(required=True)
    interval = serializers.ChoiceField(choices=tuple(INTERVAL_SECONDS), required=True)

    def validate(self, attrs):
        if attrs["end"] <= attrs["start"]:
            raise serializers.ValidationError({"end": ["Must be after start."]})
        if attrs["end"] - attrs["start"] > datetime.timedelta(days=MAX_WINDOW_DAYS):
            raise serializers.ValidationError({"end": [f"Window must be at most {MAX_WINDOW_DAYS} days."]})

        seconds = INTERVAL_SECONDS[attrs["interval"]]
        first_bucket = math.floor(attrs["start"].timestamp() / seconds) * seconds
        bucket_count = math.ceil((attrs["end"].timestamp() - first_bucket) / seconds)
        if bucket_count > MAX_STATS_BUCKETS:
            raise serializers.ValidationError(
                {"interval": [f"Query would return more than {MAX_STATS_BUCKETS} buckets."]}
            )
        return attrs


class EventStatsPointSerializer(serializers.Serializer):
    timestamp = serializers.DateTimeField(default_timezone=datetime.timezone.utc)
    value = serializers.IntegerField(min_value=0)


class EventStatsResponseSerializer(serializers.Serializer):
    time_basis = serializers.CharField()
    interval = serializers.ChoiceField(choices=tuple(INTERVAL_SECONDS))
    crash_volume = EventStatsPointSerializer(many=True)
    issue_count = serializers.IntegerField(min_value=0)


def get_event_stats(params):
    serializer = EventStatsQuerySerializer(data=params)
    serializer.is_valid(raise_exception=True)
    query = serializer.validated_data
    interval = query["interval"]
    interval_seconds = INTERVAL_SECONDS[interval]

    events = Event.objects.filter(
        project_id=query["project"],
        issue__is_deleted=False,
        timestamp__gte=query["start"],
        timestamp__lt=query["end"],
    )
    truncation = {
        "5m": TruncMinute,
        "15m": TruncMinute,
        "1h": TruncHour,
        "1d": TruncDay,
    }[interval]("timestamp", tzinfo=datetime.timezone.utc)
    rows = events.annotate(raw_bucket=truncation).values("raw_bucket").annotate(value=Count("id"))

    counts = defaultdict(int)
    for row in rows:
        counts[_floor_bucket(row["raw_bucket"], interval_seconds)] += row["value"]

    first_bucket = _floor_bucket(query["start"], interval_seconds)
    bucket_step = datetime.timedelta(seconds=interval_seconds)
    crash_volume = []
    bucket = first_bucket
    while bucket < query["end"]:
        crash_volume.append({"timestamp": bucket, "value": counts[bucket]})
        bucket += bucket_step

    issue_count = events.filter(issue__is_resolved=False).values("issue_id").distinct().count()
    return {
        "time_basis": "event.timestamp",
        "interval": interval,
        "crash_volume": crash_volume,
        "issue_count": issue_count,
    }


def _floor_bucket(value, interval_seconds):
    timestamp = math.floor(value.timestamp() / interval_seconds) * interval_seconds
    return datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)
