import datetime
import re

from django.db.models import Exists, OuterRef, Prefetch, Subquery
from rest_framework import serializers

from events.models import Event
from tags.models import EventTag, TagValue


MAX_WINDOW_DAYS = 31
IDENTITY_FIELDS = ("player_id", "session_id", "release", "build")
EVENT_QUERY_PARAMS = ("start", "end", *IDENTITY_FIELDS)
TIMEZONE_RE = re.compile(r"(Z|[+-]\d\d:\d\d)$")


class RFC3339DateTimeField(serializers.DateTimeField):
    def to_internal_value(self, value):
        if isinstance(value, str) and not TIMEZONE_RE.search(value):
            raise serializers.ValidationError("Must include a timezone offset or Z.")
        return super().to_internal_value(value)


class IssueEventQuerySerializer(serializers.Serializer):
    project = serializers.IntegerField(required=True)
    start = RFC3339DateTimeField(required=False)
    end = RFC3339DateTimeField(required=False)
    player_id = serializers.CharField(required=False, allow_blank=False, max_length=200)
    session_id = serializers.CharField(required=False, allow_blank=False, max_length=200)
    release = serializers.CharField(required=False, allow_blank=False, max_length=250)
    build = serializers.CharField(required=False, allow_blank=False, max_length=200)
    sort = serializers.CharField(required=False)
    order = serializers.CharField(required=False)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=100)

    def validate(self, attrs):
        params = self.initial_data
        has_event_filter = any(params.get(name) not in (None, "") for name in EVENT_QUERY_PARAMS)

        if attrs.get("sort") not in (None, "digest_order", "last_seen", "matched_at"):
            raise serializers.ValidationError({"sort": ["Must be 'digest_order', 'last_seen' or 'matched_at'."]})

        if attrs.get("order") not in (None, "asc", "desc"):
            raise serializers.ValidationError({"order": ["Must be 'asc' or 'desc'."]})

        if has_event_filter and ("start" not in attrs or "end" not in attrs):
            raise serializers.ValidationError({"start": ["start and end are required for event filters."]})

        if "start" in attrs or "end" in attrs:
            if "start" not in attrs or "end" not in attrs:
                raise serializers.ValidationError({"start": ["start and end must be provided together."]})

            if attrs["end"] <= attrs["start"]:
                raise serializers.ValidationError({"end": ["Must be after start."]})

            if attrs["end"] - attrs["start"] > datetime.timedelta(days=MAX_WINDOW_DAYS):
                raise serializers.ValidationError({"end": [f"Window must be at most {MAX_WINDOW_DAYS} days."]})

        if attrs.get("sort") == "matched_at" and not has_event_filter:
            raise serializers.ValidationError({"sort": ["matched_at requires event filters."]})

        return attrs


class IssueEventQuery:
    def __init__(self, data):
        serializer = IssueEventQuerySerializer(data=data)
        serializer.is_valid(raise_exception=True)
        self.data = serializer.validated_data
        self.uses_events = "start" in self.data


def parse_issue_event_query(params):
    return IssueEventQuery(params)


def _tag_value_ids(project_id, query_data):
    requested = {
        (key, query_data[key])
        for key in ("session_id", "build", "player_id")
        if key in query_data
    }
    if "player_id" in query_data:
        requested.add(("user.id", query_data["player_id"]))
    if not requested:
        return {}

    rows = TagValue.objects.filter(
        key__project_id=project_id,
        key__key__in={key for key, _ in requested},
        value__in={value for _, value in requested},
    ).values_list("key__key", "value", "id")
    return {(key, value): value_id for key, value, value_id in rows if (key, value) in requested}


def _tag_exists(value_id):
    return Exists(EventTag.objects.filter(event_id=OuterRef("pk"), value_id=value_id))


def _any_tag_exists(key):
    return Exists(EventTag.objects.filter(event_id=OuterRef("pk"), value__key__key=key))


def _apply_event_filters(events, query):
    project_id = query.data["project"]
    tag_value_ids = _tag_value_ids(project_id, query.data)
    events = events.filter(
        project_id=project_id,
        timestamp__gte=query.data["start"],
        timestamp__lt=query.data["end"],
    )

    if "release" in query.data:
        events = events.filter(release=query.data["release"])

    for key in ("session_id", "build"):
        if key not in query.data:
            continue

        value_id = tag_value_ids.get((key, query.data[key]))
        if value_id is None:
            return Event.objects.none()
        events = events.annotate(**{f"has_{key}": _tag_exists(value_id)}).filter(**{f"has_{key}": True})

    if "player_id" in query.data:
        player_id = tag_value_ids.get(("player_id", query.data["player_id"]))
        user_id = tag_value_ids.get(("user.id", query.data["player_id"]))
        if player_id is None and user_id is None:
            return Event.objects.none()

        events = events.annotate(has_any_player_id=_any_tag_exists("player_id"))
        if player_id is not None:
            events = events.annotate(has_player_id=_tag_exists(player_id))
        if user_id is not None:
            events = events.annotate(has_user_id=_tag_exists(user_id))

        if player_id is not None and user_id is not None:
            events = events.filter(has_player_id=True) | events.filter(has_any_player_id=False, has_user_id=True)
        elif player_id is not None:
            events = events.filter(has_player_id=True)
        else:
            events = events.filter(has_any_player_id=False, has_user_id=True)

    return events.order_by("-timestamp", "-id")


def _matching_events(query):
    return _apply_event_filters(Event.objects.filter(issue_id=OuterRef("pk")), query)


def filter_events_for_event_query(queryset, query):
    if not query.uses_events:
        raise serializers.ValidationError({"start": ["start and end are required for cross-issue event queries."]})
    return _apply_event_filters(queryset, query)


def filter_issues_for_event_query(queryset, query):
    queryset = queryset.filter(project_id=query.data["project"])
    if not query.uses_events:
        return queryset

    matching_events = _matching_events(query)
    return queryset.annotate(
        matched_event_id=Subquery(matching_events.values("id")[:1]),
        matched_at=Subquery(matching_events.values("timestamp")[:1]),
    ).filter(matched_event_id__isnull=False)


def get_matched_events(issues):
    event_ids = [issue.matched_event_id for issue in issues if getattr(issue, "matched_event_id", None)]
    if not event_ids:
        return {}

    identity_tags = EventTag.objects.filter(
        value__key__key__in=("player_id", "user.id", "session_id", "build")
    ).select_related("value__key")
    events = Event.objects.filter(id__in=event_ids).prefetch_related(
        Prefetch("tags", queryset=identity_tags, to_attr="matched_identity_tags")
    )
    return {event.id: event for event in events}


def matched_event_identity(event):
    event_tags = getattr(event, "matched_identity_tags", None)
    if event_tags is None:
        event_tags = event.tags.select_related("value__key").all()
    tags = {event_tag.value.key.key: event_tag.value.value for event_tag in event_tags}
    player_id_source = None
    player_id = None
    if tags.get("player_id"):
        player_id_source = "player_id"
        player_id = tags["player_id"]
    elif tags.get("user.id"):
        player_id_source = "user.id"
        player_id = tags["user.id"]

    return {
        "player_id": player_id,
        "player_id_source": player_id_source,
        "session_id": tags.get("session_id"),
        "release": event.release or None,
        "build": tags.get("build"),
    }


def query_capabilities():
    return {
        "extension": "exact-event-issue-query",
        "version": 1,
        "time_basis": "event.timestamp",
        "interval": "[start,end)",
        "max_window_days": MAX_WINDOW_DAYS,
        "same_event_conjunction": True,
        "identity_fields": list(IDENTITY_FIELDS),
        "matched_event_evidence": True,
        "cross_issue_event_timeline": True,
        "aggregate_crash_stats": True,
    }
