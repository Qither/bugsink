from bugsink.test_utils import TransactionTestCase25251 as TransactionTestCase
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from projects.models import Project
from bsmain.models import AuthToken
from events.factories import create_event
from events.api_views import EventViewSet

from issues.factories import get_or_create_issue
from events.factories import create_event_data
from tags.models import store_tags


class EventApiTests(TransactionTestCase):
    def setUp(self):
        self.client = APIClient()
        token = AuthToken.objects.create()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.token}")

        self.project = Project.objects.create(name="Test Project")

        self.issue, _ = get_or_create_issue(project=self.project)
        self.event = create_event(issue=self.issue)

    def test_cross_issue_list_requires_project(self):
        response = self.client.get(reverse("api:event-list"))

        self.assertEqual(response.status_code, 400)
        self.assertEqual({'project': ['This field is required.']}, response.json())

    def test_detail_by_id(self):
        url = reverse("api:event-detail", args=[self.event.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        detail = response.json()
        self.assertEqual(detail["id"], str(self.event.id))
        self.assertIn("data", detail)
        self.assertTrue("event_id" in detail["data"])

    def test_detail_includes_stacktrace_md_field(self):
        url = reverse("api:event-detail", args=[self.event.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        detail = response.json()

        self.assertIn("stacktrace_md", detail)
        self.assertIsInstance(detail["stacktrace_md"], str)
        self.assertTrue(len(detail["stacktrace_md"]) > 0)

        self.assertEqual("_No stacktrace available._", detail["stacktrace_md"])

    def test_stacktrace_action_returns_markdown(self):
        url = reverse("api:event-stacktrace", args=[self.event.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        self.assertTrue(response["Content-Type"].startswith("text/markdown"))
        body = response.content.decode("utf-8")
        self.assertTrue(len(body) > 0)

        self.assertEqual("_No stacktrace available._", body)

    def test_list_by_issue_is_light_payload(self):
        response = self.client.get(reverse("api:event-list"), {"issue": str(self.issue.id)})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("data", response.json()["results"][0])

    def test_detail_not_found_is_404(self):
        url = reverse("api:event-detail", args=["00000000-0000-0000-0000-000000000000"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_detail_malformed_id_is_404(self):
        response = self.client.get(reverse("api:event-detail", args=["not-a-uuid"]))
        self.assertEqual(response.status_code, 404)

    def test_stacktrace_malformed_id_is_404(self):
        response = self.client.get(reverse("api:event-stacktrace", args=["not-a-uuid"]))
        self.assertEqual(response.status_code, 404)
        self.assertIn("404 Not Found", response.content.decode("utf-8"))

    def test_list_rejects_bad_order(self):
        response = self.client.get(reverse("api:event-list"), {"issue": str(self.issue.id), "order": "sideways"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual({'order': ["Must be 'asc' or 'desc'."]}, response.json())

    def test_list_order_default_desc(self):
        e0 = self.event
        e1 = create_event(issue=self.issue)
        response = self.client.get(reverse("api:event-list"), {"issue": str(self.issue.id)})
        self.assertEqual(response.status_code, 200)
        ids = [item["id"] for item in response.json()["results"]]
        self.assertEqual(ids[0], str(e1.id))
        self.assertEqual(ids[1], str(e0.id))

    def test_list_order_asc(self):
        e0 = self.event
        e1 = create_event(issue=self.issue)
        response = self.client.get(reverse("api:event-list"), {"issue": str(self.issue.id), "order": "asc"})
        self.assertEqual(response.status_code, 200)
        ids = [item["id"] for item in response.json()["results"]]
        self.assertEqual(ids[0], str(e0.id))
        self.assertEqual(ids[1], str(e1.id))

    def test_cross_issue_query_is_exact_and_returns_identity_evidence(self):
        start = timezone.now().replace(microsecond=0)
        end = start + timezone.timedelta(hours=1)
        second_issue = get_or_create_issue(
            project=self.project,
            event_data=create_event_data(exception_type="second"),
        )[0]
        split_player = create_event(issue=self.issue, timestamp=start + timezone.timedelta(minutes=1))
        store_tags(split_player, self.issue, {"player_id": "player-1"})
        split_session = create_event(issue=self.issue, timestamp=start + timezone.timedelta(minutes=2))
        store_tags(split_session, self.issue, {"session_id": "session-1", "build": "100"})
        matched = create_event(
            issue=second_issue,
            timestamp=start + timezone.timedelta(minutes=3),
            release="1.0.0",
        )
        store_tags(
            matched,
            second_issue,
            {"player_id": "player-1", "session_id": "session-1", "build": "100"},
        )
        at_end = create_event(issue=second_issue, timestamp=end, release="1.0.0")
        store_tags(
            at_end,
            second_issue,
            {"player_id": "player-1", "session_id": "session-1", "build": "100"},
        )

        response = self.client.get(
            reverse("api:event-list"),
            {
                "project": str(self.project.id),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "player_id": "player-1",
                "session_id": "session-1",
                "release": "1.0.0",
                "build": "100",
            },
        )

        self.assertEqual(response.status_code, 200)
        rows = response.json()["results"]
        self.assertEqual([row["id"] for row in rows], [str(matched.id)])
        self.assertNotIn("data", rows[0])
        self.assertEqual(rows[0]["identity"], {
            "player_id": "player-1",
            "player_id_source": "player_id",
            "session_id": "session-1",
            "release": "1.0.0",
            "build": "100",
        })

    def test_cross_issue_identity_serialization_has_bounded_queries(self):
        start = timezone.now().replace(microsecond=0)
        end = start + timezone.timedelta(hours=1)
        for offset in range(20):
            event = create_event(issue=self.issue, timestamp=start + timezone.timedelta(seconds=offset))
            store_tags(event, self.issue, {"player_id": f"player-{offset}", "session_id": "session"})

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(
                reverse("api:event-list"),
                {
                    "project": str(self.project.id),
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["results"]), 21)
        self.assertLessEqual(len(queries), 8)


class EventPaginationTests(TransactionTestCase):
    def setUp(self):
        self.client = APIClient()
        token = AuthToken.objects.create()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.token}")
        self.old_size = EventViewSet.pagination_class.page_size
        EventViewSet.pagination_class.page_size = 2

    def tearDown(self):
        EventViewSet.pagination_class.page_size = self.old_size

    def _make_events(self, issue, n=5):
        events = []
        for i in range(n):
            ev = create_event(issue=issue)
            events.append(ev)
        return events

    def _ids(self, resp):
        return [row["id"] for row in resp.json()["results"]]

    def test_digest_order_desc_two_pages(self):
        proj = Project.objects.create(name="P")
        issue = get_or_create_issue(project=proj, event_data=create_event_data(exception_type="root"))[0]
        events = self._make_events(issue, 5)

        # default (desc) → events 5,4 on page 1; 3,2 on page 2
        r1 = self.client.get(reverse("api:event-list"), {"issue": str(issue.id)})
        self.assertEqual(self._ids(r1), [str(events[4].id), str(events[3].id)])

        r2 = self.client.get(r1.json()["next"])
        self.assertEqual(self._ids(r2), [str(events[2].id), str(events[1].id)])

    def test_digest_order_asc_two_pages(self):
        proj = Project.objects.create(name="P2")
        issue = get_or_create_issue(project=proj, event_data=create_event_data(exception_type="root2"))[0]
        events = self._make_events(issue, 5)

        # asc → events 1,2 on page 1; 3,4 on page 2
        r1 = self.client.get(reverse("api:event-list"),
                             {"issue": str(issue.id), "order": "asc"})
        self.assertEqual(self._ids(r1), [str(events[0].id), str(events[1].id)])

        r2 = self.client.get(r1.json()["next"])
        self.assertEqual(self._ids(r2), [str(events[2].id), str(events[3].id)])

    def test_cross_issue_timestamp_desc_two_pages(self):
        project = Project.objects.create(name="Timeline")
        issue0 = get_or_create_issue(
            project=project,
            event_data=create_event_data(exception_type="timeline-0"),
        )[0]
        issue1 = get_or_create_issue(
            project=project,
            event_data=create_event_data(exception_type="timeline-1"),
        )[0]
        start = timezone.now().replace(microsecond=0)
        events = [
            create_event(
                issue=issue0 if offset % 2 == 0 else issue1,
                timestamp=start + timezone.timedelta(seconds=offset),
            )
            for offset in range(5)
        ]

        r1 = self.client.get(
            reverse("api:event-list"),
            {
                "project": str(project.id),
                "start": start.isoformat(),
                "end": (start + timezone.timedelta(hours=1)).isoformat(),
                "limit": 2,
            },
        )
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(self._ids(r1), [str(events[4].id), str(events[3].id)])

        r2 = self.client.get(r1.json()["next"])
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(self._ids(r2), [str(events[2].id), str(events[1].id)])
