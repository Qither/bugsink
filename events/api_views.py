from django.db.models import Prefetch
from rest_framework import viewsets
from rest_framework.generics import get_object_or_404
from rest_framework.exceptions import ValidationError
from rest_framework.decorators import action
from rest_framework.response import Response
from drf_spectacular.utils import extend_schema, OpenApiExample, OpenApiParameter, OpenApiTypes, OpenApiResponse


from bugsink.utils import assert_
from bugsink.api_pagination import AscDescCursorPagination
from bugsink.api_mixins import AtomicRequestMixin
from issues.api_query import filter_events_for_event_query, parse_issue_event_query
from issues.models import issue_lookup_kwargs
from tags.models import EventTag

from .models import Event
from .api_stats import EventStatsResponseSerializer, get_event_stats
from .serializers import EventListSerializer, EventDetailSerializer
from .markdown_stacktrace import render_stacktrace_md
from .renderers import MarkdownRenderer


class EventPagination(AscDescCursorPagination):
    # Cursor pagination requires an indexed, mostly-stable ordering field. We use `digest_order`: we require
    # ?issue=<uuid> and have a composite (issue_id, digest_order) index, so ORDER BY digest_order after filtering by
    # issue is fast and cursor-stable. (also note that digest_order comes in in-order).
    base_ordering = ("digest_order",)
    page_size = 250
    page_size_query_param = "limit"
    max_page_size = 100
    default_direction = "desc"  # newest first by default, aligned with UI

    def get_page_size(self, request):
        if "issue" not in request.query_params and "limit" not in request.query_params:
            return self.max_page_size
        return super().get_page_size(request)

    def get_ordering(self, request, queryset, view):
        order_param = request.query_params.get("order")
        if order_param and order_param not in ("asc", "desc"):
            raise ValidationError({"order": ["Must be 'asc' or 'desc'."]})

        direction = order_param or self.default_direction
        fields = ("digest_order",) if "issue" in request.query_params else ("timestamp", "id")
        return [f"-{field}" if direction == "desc" else field for field in fields]


class EventViewSet(AtomicRequestMixin, viewsets.ReadOnlyModelViewSet):
    queryset = Event.objects.all()  # router requirement for basename inference
    serializer_class = EventListSerializer
    pagination_class = EventPagination

    def filter_queryset(self, queryset):
        query_params = self.request.query_params
        identity_tags = EventTag.objects.filter(
            value__key__key__in=("player_id", "user.id", "session_id", "build")
        ).select_related("value__key")

        if "issue" in query_params:
            lookup_kwargs = {"issue__" + k: v for k, v in issue_lookup_kwargs(query_params["issue"]).items()}
            events = queryset.filter(issue__is_deleted=False, **lookup_kwargs)
        else:
            query = parse_issue_event_query(query_params)
            events = filter_events_for_event_query(queryset.filter(issue__is_deleted=False), query)

        return events.prefetch_related(
            Prefetch("tags", queryset=identity_tags, to_attr="matched_identity_tags")
        )

    @extend_schema(
        summary="List events",
        description=(
            "List events for one issue, or query exact events across issues with project/start/end. "
            "Cross-issue queries use event.timestamp with a [start,end) interval and support exact identity filters. "
            "The list response omits the full event `data` payload."
        ),
        parameters=[
            OpenApiParameter(
                name="issue",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Filter events by issue UUID or friendly ID. Mutually exclusive with cross-issue scope.",
            ),
            OpenApiParameter(
                name="project",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Project ID. Required with start/end when issue is omitted.",
            ),
            OpenApiParameter(
                name="start",
                type=OpenApiTypes.DATETIME,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Inclusive event timestamp boundary for a cross-issue query.",
            ),
            OpenApiParameter(
                name="end",
                type=OpenApiTypes.DATETIME,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Exclusive event timestamp boundary for a cross-issue query.",
            ),
            OpenApiParameter(
                name="player_id",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Exact player_id, falling back to user.id only when player_id is absent on the event.",
            ),
            OpenApiParameter(
                name="session_id",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Exact session_id filter on the same event.",
            ),
            OpenApiParameter(
                name="release",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Exact event release filter.",
            ),
            OpenApiParameter(
                name="build",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Exact build tag filter on the same event.",
            ),
            OpenApiParameter(
                name="limit",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Page size for cross-issue queries (1-100, default 100).",
            ),
            OpenApiParameter(
                name="order",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                enum=["asc", "desc"],
                description="Sort order of digest_order (issue scope) or timestamp/id (cross-issue scope).",
            ),
        ]
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        summary="Aggregate crash statistics",
        description=(
            "Return exact event.timestamp crash-volume buckets and the number of unresolved issues observed in the "
            "same [start,end) range. Queries are project-scoped, limited to 31 days and at most 200 buckets."
        ),
        parameters=[
            OpenApiParameter("project", OpenApiTypes.INT, OpenApiParameter.QUERY, required=True),
            OpenApiParameter("start", OpenApiTypes.DATETIME, OpenApiParameter.QUERY, required=True),
            OpenApiParameter("end", OpenApiTypes.DATETIME, OpenApiParameter.QUERY, required=True),
            OpenApiParameter(
                "interval",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                required=True,
                enum=["5m", "15m", "1h", "1d"],
            ),
        ],
        responses=EventStatsResponseSerializer,
    )
    @action(detail=False, methods=["get"], url_path="stats")
    def stats(self, request):
        return Response(EventStatsResponseSerializer(get_event_stats(request.query_params)).data)

    @extend_schema(
        summary="Retrieve an event",
        description=(
            "Retrieve an event by internal Bugsink event UUID. "
            "The detail response includes the full `data` payload."
        ),
        responses=EventDetailSerializer,
    )
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    def get_object(self):
        """
        DRF's get_object(), but we intentionally bypass filter_queryset for detail routes to keep PK lookups
        db-index-friendly (no WHERE filters other than the PK which is already indexed).
        # NOTE: alternatively, we just complain hard when a filter is applied to a detail view.
        """
        queryset = self.get_queryset()  # no filter_queryset() here

        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        assert_(lookup_url_kwarg in self.kwargs, (
            'Expected view %s to be called with a URL keyword argument '
            'named "%s". Fix your URL conf, or set the `.lookup_field` '
            'attribute on the view correctly.' %
            (self.__class__.__name__, lookup_url_kwarg)
        ))

        filter_kwargs = {self.lookup_field: self.kwargs[lookup_url_kwarg]}
        obj = get_object_or_404(queryset, **filter_kwargs)

        # May raise a permission denied
        self.check_object_permissions(self.request, obj)

        return obj

    def get_serializer_class(self):
        return EventDetailSerializer if self.action == "retrieve" else EventListSerializer

    @extend_schema(
        summary="Render an event stacktrace",
        description="Render the event's stacktrace (frames, source, locals) as Markdown-like text.",
        responses={
            200: OpenApiResponse(
                response=str,
                description="Stacktrace as Markdown",
                examples=[
                    OpenApiExample(
                        "Stacktrace",
                        value="Traceback (most rece...",
                        response_only=True,
                    ),
                ],
            )
        },
    )
    @action(
        detail=True,
        methods=["get"],
        url_path="stacktrace",
        renderer_classes=[MarkdownRenderer],
    )
    def stacktrace(self, request, pk=None):
        event = self.get_object()
        text = render_stacktrace_md(event, in_app_only=False, include_locals=True)
        return Response(text)
