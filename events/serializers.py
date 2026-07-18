from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field
from bugsink.api_serializers import UTCModelSerializer
from issues.api_query import matched_event_identity
from issues.serializers import MatchedIdentitySerializer

from .markdown_stacktrace import render_stacktrace_md
from .models import Event


class EventListSerializer(UTCModelSerializer):
    """Lightweight list view: excludes the (potentially large) `data` field."""

    identity = serializers.SerializerMethodField()

    class Meta:
        model = Event
        fields = [
            "id",
            "ingested_at",
            "digested_at",
            "issue",
            "grouping",
            "event_id",
            "project",
            "timestamp",
            "digest_order",
            "identity",
        ]

    @extend_schema_field(MatchedIdentitySerializer)
    def get_identity(self, obj):
        return MatchedIdentitySerializer(matched_event_identity(obj)).data


class EventDetailSerializer(EventListSerializer):
    """Detail view: includes full `data` payload."""
    # NOTE as with Issue.grouping_keys: check viewset for prefetching
    # grouping_key = serializers.CharField(source="grouping.grouping_key", read_only=True)

    data = serializers.SerializerMethodField()
    stacktrace_md = serializers.SerializerMethodField()

    class Meta:
        model = Event
        fields = EventListSerializer.Meta.fields + [
            "data",
            "stacktrace_md",
            # "grouping_key"  # TODO (likely) once we have the "expand" idea implemented
        ]

    @extend_schema_field(serializers.JSONField)
    def get_data(self, obj):
        # we override `data` to return the parsed version (which may come from the file store rather than the DB)
        return obj.get_parsed_data()

    @extend_schema_field(serializers.CharField)
    def get_stacktrace_md(self, obj):
        return render_stacktrace_md(obj, in_app_only=False, include_locals=True)
