from bugsink.test_utils import TransactionTestCase25251 as TransactionTestCase
from django.db import connection
from django.urls import reverse
from django.utils import timezone
from django.test.utils import CaptureQueriesContext

from rest_framework.test import APIClient

from bsmain.models import AuthToken
from projects.models import Project
from releases.models import create_release_if_needed
from issues.models import Issue, TurningPoint, TurningPointKind
from issues.factories import get_or_create_issue
from events.factories import create_event, create_event_data
from tags.models import store_tags

from issues.api_views import IssueViewSet


class IssueApiTests(TransactionTestCase):
    def setUp(self):
        self.client = APIClient()
        token = AuthToken.objects.create()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.token}")

        self.project = Project.objects.create(name="Test Project")

        # create two distinct issues for ordering tests (different grouping keys)
        data0 = create_event_data(exception_type="E0")
        data1 = create_event_data(exception_type="E1")

        self.issue0, _ = get_or_create_issue(project=self.project, event_data=data0)
        self.issue1, _ = get_or_create_issue(project=self.project, event_data=data1)

        # ensure deterministic last_seen ordering
        now = timezone.now()
        Issue.objects.filter(id=self.issue0.id).update(last_seen=now)
        Issue.objects.filter(id=self.issue1.id).update(last_seen=now + timezone.timedelta(seconds=1))
        self.issue0.refresh_from_db()
        self.issue1.refresh_from_db()

    def test_list_requires_project(self):
        response = self.client.get(reverse("api:issue-list"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual({"project": ["This field is required."]}, response.json())

    def test_list_by_project_default_asc(self):
        response = self.client.get(reverse("api:issue-list"), {"project": str(self.project.id)})
        self.assertEqual(response.status_code, 200)
        ids = [row["id"] for row in response.json()["results"]]
        self.assertEqual(ids[0], str(self.issue0.id))
        self.assertEqual(ids[1], str(self.issue1.id))

    def test_list_by_project_order_desc(self):
        response = self.client.get(reverse("api:issue-list"), {"project": str(self.project.id), "order": "desc"})
        self.assertEqual(response.status_code, 200)
        ids = [row["id"] for row in response.json()["results"]]
        self.assertEqual(ids[0], str(self.issue1.id))
        self.assertEqual(ids[1], str(self.issue0.id))

    def test_list_rejects_bad_order(self):
        response = self.client.get(reverse("api:issue-list"), {"project": str(self.project.id), "order": "sideways"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual({"order": ["Must be 'asc' or 'desc'."]}, response.json())

    def test_detail_by_id(self):
        url = reverse("api:issue-detail", args=[self.issue0.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], str(self.issue0.id))

    def test_friendly_id_alias(self):
        event = create_event(issue=self.issue0)
        friendly_id = self.issue0.friendly_id().lower()

        response = self.client.get(reverse("api:issue-detail", args=[friendly_id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], str(self.issue0.id))
        self.assertEqual(response.json()["friendly_id"], self.issue0.friendly_id())

        response = self.client.post(reverse("api:issue-mute", args=[friendly_id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["is_muted"])

        response = self.client.get(reverse("api:event-list"), {"issue": friendly_id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["id"], str(event.id))

    def test_detail_ignores_query_filters(self):
        url = reverse("api:issue-detail", args=[self.issue0.id])
        response = self.client.get(url, {"project": "00000000-0000-0000-0000-000000000000", "order": "asc"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], str(self.issue0.id))

    def test_detail_404_on_is_deleted(self):
        Issue.objects.filter(id=self.issue0.id).update(is_deleted=True)
        url = reverse("api:issue-detail", args=[self.issue0.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_list_rejects_bad_sort(self):
        r = self.client.get(
            reverse("api:issue-list"),
            {"project": str(self.project.id), "sort": "nope"},
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json(), {"sort": ["Must be 'digest_order', 'last_seen' or 'matched_at'."]})

    def test_query_capabilities(self):
        response = self.client.get(reverse("api:issue-query-capabilities"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "extension": "exact-event-issue-query",
            "version": 1,
            "time_basis": "event.timestamp",
            "interval": "[start,end)",
            "max_window_days": 31,
            "same_event_conjunction": True,
            "identity_fields": ["player_id", "session_id", "release", "build"],
            "matched_event_evidence": True,
            "cross_issue_event_timeline": True,
        })

    def _create_tagged_event(self, issue, timestamp, tags=None, release=""):
        event = create_event(issue=issue, timestamp=timestamp, release=release)
        if tags:
            store_tags(event, issue, tags)
        return event

    def _event_query_params(self, start, end, **extra):
        params = {
            "project": str(self.project.id),
            "start": start.isoformat(),
            "end": end.isoformat(),
        }
        params.update(extra)
        return params

    def test_event_time_filter_uses_start_inclusive_end_exclusive(self):
        start = timezone.now().replace(microsecond=0)
        end = start + timezone.timedelta(hours=1)
        before = self._create_tagged_event(self.issue0, start - timezone.timedelta(seconds=1))
        at_start = self._create_tagged_event(self.issue0, start)
        at_end = self._create_tagged_event(self.issue1, end)
        self.assertIsNotNone(before)
        self.assertIsNotNone(at_end)

        response = self.client.get(reverse("api:issue-list"), self._event_query_params(start, end))

        self.assertEqual(response.status_code, 200)
        rows = response.json()["results"]
        self.assertEqual([row["id"] for row in rows], [str(self.issue0.id)])
        self.assertEqual(rows[0]["matched_event"]["id"], str(at_start.id))

    def test_event_identity_filters_must_match_one_event(self):
        start = timezone.now().replace(microsecond=0)
        end = start + timezone.timedelta(hours=1)
        self._create_tagged_event(self.issue0, start, tags={"player_id": "p1"})
        self._create_tagged_event(
            self.issue0,
            start + timezone.timedelta(minutes=1),
            tags={"session_id": "s1"},
        )
        matched = self._create_tagged_event(
            self.issue1,
            start + timezone.timedelta(minutes=2),
            tags={"player_id": "p1", "session_id": "s1", "build": "123"},
            release="1.0.0",
        )

        response = self.client.get(
            reverse("api:issue-list"),
            self._event_query_params(
                start,
                end,
                player_id="p1",
                session_id="s1",
                release="1.0.0",
                build="123",
            ),
        )

        self.assertEqual(response.status_code, 200)
        rows = response.json()["results"]
        self.assertEqual([row["id"] for row in rows], [str(self.issue1.id)])
        self.assertEqual(rows[0]["matched_event"]["id"], str(matched.id))
        self.assertEqual(rows[0]["matched_event"]["identity"], {
            "player_id": "p1",
            "player_id_source": "player_id",
            "session_id": "s1",
            "release": "1.0.0",
            "build": "123",
        })

    def test_player_id_falls_back_to_user_id_when_player_id_tag_missing(self):
        start = timezone.now().replace(microsecond=0)
        end = start + timezone.timedelta(hours=1)
        matched = self._create_tagged_event(self.issue0, start, tags={"user.id": "fallback-player"})
        self._create_tagged_event(
            self.issue1,
            start,
            tags={"player_id": "other-player", "user.id": "fallback-player"},
        )

        response = self.client.get(
            reverse("api:issue-list"),
            self._event_query_params(start, end, player_id="fallback-player"),
        )

        self.assertEqual(response.status_code, 200)
        rows = response.json()["results"]
        self.assertEqual([row["id"] for row in rows], [str(self.issue0.id)])
        self.assertEqual(rows[0]["matched_event"]["id"], str(matched.id))
        self.assertEqual(rows[0]["matched_event"]["identity"]["player_id_source"], "user.id")

    def test_matched_at_sort_orders_by_matched_event_timestamp(self):
        start = timezone.now().replace(microsecond=0)
        end = start + timezone.timedelta(hours=1)
        early = self._create_tagged_event(self.issue0, start + timezone.timedelta(minutes=1))
        late = self._create_tagged_event(self.issue1, start + timezone.timedelta(minutes=2))
        self.assertIsNotNone(early)
        self.assertIsNotNone(late)

        response = self.client.get(
            reverse("api:issue-list"),
            self._event_query_params(start, end, sort="matched_at", order="desc", limit="1"),
        )

        self.assertEqual(response.status_code, 200)
        rows = response.json()["results"]
        self.assertEqual([row["id"] for row in rows], [str(self.issue1.id)])
        self.assertIsNotNone(response.json()["next"])

    def test_event_query_defaults_to_latest_matched_event(self):
        start = timezone.now().replace(microsecond=0)
        end = start + timezone.timedelta(hours=1)
        self._create_tagged_event(self.issue0, start + timezone.timedelta(minutes=1))
        latest = self._create_tagged_event(self.issue1, start + timezone.timedelta(minutes=2))

        response = self.client.get(
            reverse("api:issue-list"),
            self._event_query_params(start, end, limit="1"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["id"], str(self.issue1.id))
        self.assertEqual(response.json()["results"][0]["matched_event"]["id"], str(latest.id))

    def test_event_identity_query_has_bounded_database_round_trips(self):
        start = timezone.now().replace(microsecond=0)
        end = start + timezone.timedelta(hours=1)
        self._create_tagged_event(
            self.issue0,
            start,
            tags={"player_id": "p1", "session_id": "s1", "build": "123"},
            release="1.0.0",
        )

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(
                reverse("api:issue-list"),
                self._event_query_params(
                    start,
                    end,
                    player_id="p1",
                    session_id="s1",
                    release="1.0.0",
                    build="123",
                    limit="100",
                ),
            )

        self.assertEqual(response.status_code, 200)
        select_queries = [
            query for query in queries.captured_queries if query["sql"].lstrip().upper().startswith("SELECT")
        ]
        self.assertLessEqual(
            len(select_queries),
            5,
            "Unexpected query plan:\n" + "\n".join(query["sql"] for query in queries.captured_queries),
        )
        # AtomicRequestMixin adds BEGIN/COMMIT around the five bounded reads.
        self.assertLessEqual(len(queries), 7)

    def test_event_query_validation(self):
        start = timezone.now().replace(microsecond=0)
        response = self.client.get(reverse("api:issue-list"), {"project": str(self.project.id), "player_id": "p1"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"start": ["start and end are required for event filters."]})

        response = self.client.get(
            reverse("api:issue-list"),
            {"project": str(self.project.id), "sort": "matched_at"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"sort": ["matched_at requires event filters."]})

        response = self.client.get(
            reverse("api:issue-list"),
            self._event_query_params(start, start, limit="101"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("limit", response.json())

    def test_resolve(self):
        response = self.client.post(reverse("api:issue-resolve", args=[self.issue0.id]))
        self.assertEqual(response.status_code, 200)

        self.issue0.refresh_from_db()
        self.assertTrue(self.issue0.is_resolved)
        self.assertTrue(self.issue0.is_resolved_unconditionally)
        self.assertEqual(self.issue0.fixed_at, "")
        self.assertEqual(response.json()["is_resolved"], True)
        self.assertEqual(response.json()["is_resolved_unconditionally"], True)

        turningpoint = TurningPoint.objects.get(issue=self.issue0)
        self.assertEqual(turningpoint.kind, TurningPointKind.RESOLVED)
        self.assertIsNone(turningpoint.user)

    def test_resolve_next(self):
        response = self.client.post(reverse("api:issue-resolve-next", args=[self.issue0.id]))
        self.assertEqual(response.status_code, 200)

        self.issue0.refresh_from_db()
        self.assertTrue(self.issue0.is_resolved)
        self.assertTrue(self.issue0.is_resolved_by_next_release)

    def test_resolve_latest(self):
        create_release_if_needed(self.project, "1.0.0", timezone.now())

        response = self.client.post(reverse("api:issue-resolve-latest", args=[self.issue0.id]))
        self.assertEqual(response.status_code, 200)

        self.issue0.refresh_from_db()
        self.assertTrue(self.issue0.is_resolved)
        self.assertFalse(self.issue0.is_resolved_unconditionally)
        self.assertEqual(self.issue0.fixed_at, "1.0.0\n")

    def test_resolve_latest_allows_existing_occurrence(self):
        create_release_if_needed(self.project, "1.0.0", timezone.now())
        self.issue0.events_at = "1.0.0\n"
        self.issue0.save()

        response = self.client.post(reverse("api:issue-resolve-latest", args=[self.issue0.id]))
        self.assertEqual(response.status_code, 200)

        self.issue0.refresh_from_db()
        self.assertTrue(self.issue0.is_resolved)
        self.assertFalse(self.issue0.is_resolved_unconditionally)
        self.assertEqual(self.issue0.fixed_at, "1.0.0\n")

    def test_resolve_latest_requires_releases(self):
        response = self.client.post(reverse("api:issue-resolve-latest", args=[self.issue0.id]))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "Project has no releases."})

    def test_reopen(self):
        Issue.objects.filter(id=self.issue0.id).update(is_resolved=True, is_resolved_by_next_release=True)

        response = self.client.post(reverse("api:issue-reopen", args=[self.issue0.id]))
        self.assertEqual(response.status_code, 200)

        self.issue0.refresh_from_db()
        self.assertFalse(self.issue0.is_resolved)
        self.assertFalse(self.issue0.is_resolved_unconditionally)
        self.assertFalse(self.issue0.is_resolved_by_next_release)
        self.assertEqual(response.json()["is_resolved"], False)
        self.assertEqual(response.json()["is_resolved_unconditionally"], False)
        self.assertEqual(response.json()["is_resolved_by_next_release"], False)

        turningpoint = TurningPoint.objects.get(issue=self.issue0)
        self.assertEqual(turningpoint.kind, TurningPointKind.REOPENED)

    def test_reopen_requires_resolved(self):
        response = self.client.post(reverse("api:issue-reopen", args=[self.issue0.id]))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "Issue is not resolved."})

    def test_mute(self):
        response = self.client.post(reverse("api:issue-mute", args=[self.issue0.id]))
        self.assertEqual(response.status_code, 200)

        self.issue0.refresh_from_db()
        self.assertTrue(self.issue0.is_muted)

        turningpoint = TurningPoint.objects.get(issue=self.issue0)
        self.assertEqual(turningpoint.kind, TurningPointKind.MUTED)

    def test_mute_for(self):
        response = self.client.post(
            reverse("api:issue-mute-for", args=[self.issue0.id]),
            {"period_name": "day", "nr_of_periods": 1},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

        self.issue0.refresh_from_db()
        self.assertTrue(self.issue0.is_muted)
        self.assertIsNotNone(self.issue0.unmute_after)

    def test_mute_until(self):
        response = self.client.post(
            reverse("api:issue-mute-until", args=[self.issue0.id]),
            {"period_name": "hour", "nr_of_periods": 1, "gte_threshold": 5},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

        self.issue0.refresh_from_db()
        self.assertTrue(self.issue0.is_muted)
        self.assertEqual(
            self.issue0.unmute_on_volume_based_conditions,
            '[{"period": "hour", "nr_of_periods": 1, "volume": 5}]',
        )

    def test_mute_for_accepts_non_ui_period(self):
        response = self.client.post(
            reverse("api:issue-mute-for", args=[self.issue0.id]),
            {"period_name": "minute", "nr_of_periods": 30},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

        self.issue0.refresh_from_db()
        self.assertTrue(self.issue0.is_muted)
        self.assertIsNotNone(self.issue0.unmute_after)

    def test_mute_for_rejects_bad_period(self):
        response = self.client.post(
            reverse("api:issue-mute-for", args=[self.issue0.id]),
            {"period_name": "decade", "nr_of_periods": 1},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("period_name", response.json())

    def test_mute_for_rejects_bad_period_count(self):
        response = self.client.post(
            reverse("api:issue-mute-for", args=[self.issue0.id]),
            {"period_name": "day", "nr_of_periods": -1},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("nr_of_periods", response.json())

    def test_mute_until_rejects_bad_threshold(self):
        response = self.client.post(
            reverse("api:issue-mute-until", args=[self.issue0.id]),
            {"period_name": "day", "nr_of_periods": 1, "gte_threshold": 0},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("gte_threshold", response.json())

    def test_unmute(self):
        Issue.objects.filter(id=self.issue0.id).update(is_muted=True)

        response = self.client.post(reverse("api:issue-unmute", args=[self.issue0.id]))
        self.assertEqual(response.status_code, 200)

        self.issue0.refresh_from_db()
        self.assertFalse(self.issue0.is_muted)

    def test_invalid_action(self):
        Issue.objects.filter(id=self.issue0.id).update(is_resolved=True)

        response = self.client.post(reverse("api:issue-mute", args=[self.issue0.id]))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "Issue is already resolved."})

    def test_delete(self):
        self.project.issue_count = 2
        self.project.save(update_fields=["issue_count"])

        response = self.client.delete(reverse("api:issue-detail", args=[self.issue0.id]))
        self.assertEqual(response.status_code, 204)

        self.project.refresh_from_db()
        # Snappea runs eagerly in tests, so delete_deferred() has completed by the time the response returns.
        self.assertFalse(Issue.objects.filter(id=self.issue0.id).exists())
        self.assertEqual(self.project.issue_count, 1)

    def test_unresolve_does_not_exist(self):
        response = self.client.post("/api/canonical/0/issues/%s/unresolve/" % self.issue0.id)
        self.assertEqual(response.status_code, 404)

    def test_create_comment(self):
        response = self.client.post(
            reverse("api:issue-comment-list"),
            {"issue": self.issue0.friendly_id(), "comment": "Needs a closer look."},
            format="json",
        )
        self.assertEqual(response.status_code, 201)

        turningpoint = TurningPoint.objects.get(issue=self.issue0)
        self.assertEqual(turningpoint.kind, TurningPointKind.MANUAL_ANNOTATION)
        self.assertEqual(turningpoint.project, self.project)
        self.assertEqual(turningpoint.comment, "Needs a closer look.")
        self.assertIsNone(turningpoint.user)

        self.assertEqual(response.json()["id"], turningpoint.id)
        self.assertEqual(response.json()["issue"], str(self.issue0.id))
        self.assertEqual(response.json()["project"], self.project.id)
        self.assertEqual(response.json()["user"], None)

    def test_create_comment_rejects_bad_issue_identifier(self):
        response = self.client.post(
            reverse("api:issue-comment-list"),
            {"issue": "not-an-issue", "comment": "Needs a closer look."},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"issue": ["Invalid issue identifier."]})

    def test_create_comment_rejects_missing_issue(self):
        response = self.client.post(
            reverse("api:issue-comment-list"),
            {"issue": "00000000-0000-0000-0000-000000000000", "comment": "Needs a closer look."},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"issue": ["Issue not found."]})


class IssuePaginationTests(TransactionTestCase):
    last_seen_deltas = [3, 1, 4, 0, 2]

    def setUp(self):
        self.client = APIClient()
        token = AuthToken.objects.create()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.token}")
        self.old_size = IssueViewSet.pagination_class.page_size
        IssueViewSet.pagination_class.page_size = 2

    def tearDown(self):
        IssueViewSet.pagination_class.page_size = self.old_size

    def _make_issues(self):
        proj = Project.objects.create(name="P")
        base = timezone.now().replace(microsecond=0)
        issues = []
        for i, delta in enumerate(self.last_seen_deltas):
            data = create_event_data(exception_type=f"E{i}")
            iss = get_or_create_issue(project=proj, event_data=data)[0]
            iss.digest_order = i + 1
            iss.last_seen = base + timezone.timedelta(minutes=delta)
            iss.save(update_fields=["digest_order", "last_seen"])
            issues.append(iss)
        return proj, issues

    def _ids(self, resp):
        return [row["id"] for row in resp.json()["results"]]

    def _idx_by_last_seen(self, issues, minutes):
        return issues[self.last_seen_deltas.index(minutes)].id

    def _idx_by_digest(self, issues, n):
        return issues[n - 1].id  # digest_order = n

    def test_digest_order_asc(self):
        proj, issues = self._make_issues()
        r1 = self.client.get(
            reverse("api:issue-list"),
            {"project": str(proj.id), "sort": "digest_order", "order": "asc"})

        self.assertEqual(self._ids(r1), [str(self._idx_by_digest(issues, 1)), str(self._idx_by_digest(issues, 2))])

        r2 = self.client.get(r1.json()["next"])
        self.assertEqual(self._ids(r2), [str(self._idx_by_digest(issues, 3)), str(self._idx_by_digest(issues, 4))])

    def test_digest_order_desc(self):
        proj, issues = self._make_issues()
        r1 = self.client.get(
            reverse("api:issue-list"), {"project": str(proj.id), "sort": "digest_order", "order": "desc"})

        self.assertEqual(self._ids(r1), [str(self._idx_by_digest(issues, 5)), str(self._idx_by_digest(issues, 4))])

        r2 = self.client.get(r1.json()["next"])
        self.assertEqual(self._ids(r2), [str(self._idx_by_digest(issues, 3)), str(self._idx_by_digest(issues, 2))])

    def test_last_seen_asc(self):
        proj, issues = self._make_issues()
        r1 = self.client.get(
            reverse("api:issue-list"), {"project": str(proj.id), "sort": "last_seen", "order": "asc"})

        self.assertEqual(
            self._ids(r1), [str(self._idx_by_last_seen(issues, 0)), str(self._idx_by_last_seen(issues, 1))])

        r2 = self.client.get(r1.json()["next"])
        self.assertEqual(self._ids(r2),
                         [str(self._idx_by_last_seen(issues, 2)), str(self._idx_by_last_seen(issues, 3))])

    def test_last_seen_desc(self):
        proj, issues = self._make_issues()

        r1 = self.client.get(
            reverse("api:issue-list"), {"project": str(proj.id), "sort": "last_seen", "order": "desc"})

        self.assertEqual(
            self._ids(r1), [str(self._idx_by_last_seen(issues, 4)), str(self._idx_by_last_seen(issues, 3))])

        r2 = self.client.get(r1.json()["next"])
        self.assertEqual(
            self._ids(r2), [str(self._idx_by_last_seen(issues, 2)), str(self._idx_by_last_seen(issues, 1))])
