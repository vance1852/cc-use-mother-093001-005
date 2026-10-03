import unittest
from datetime import datetime, timezone

from creative_program_foundation.advancement import AdvancementService
from creative_program_foundation.api import route
from creative_program_foundation.clock import MutableClock
from creative_program_foundation.service import DomainService
from creative_program_foundation.storage import Database

START = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)


class AdvancementApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = MutableClock(START)
        self.domain = DomainService(self.database, self.clock)
        self.advancement = AdvancementService(self.database, self.clock, domain=self.domain)
        self.domain.register_organization(request_id="org", actor_id="bootstrap",
                                          organization_id="o1", name="组委会")
        self.domain.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                   display_name="管理员", role="admin", organization_id="o1")
        self.domain.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                                   display_name="运营", role="operator", organization_id="o1")

    def tearDown(self):
        self.database.close()

    def _route(self, method, path, body=None, actor="op1"):
        return route(self.domain, method, path, body, {"X-Actor-Id": actor},
                     advancement=self.advancement)

    def test_unknown_advancement_route_returns_404(self):
        status, payload = self._route("GET", "/advancement/missing")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_advancement_route_requires_mounted_service(self):
        status, payload = route(self.domain, "GET", "/advancement/stages", None,
                                {"X-Actor-Id": "a1"})
        self.assertEqual(404, status)

    def test_create_stage_and_list(self):
        status, payload = self._route("POST", "/advancement/stages", {
            "request_id": "st", "stage_id": "st1", "name": "初赛", "sequence": 1,
            "reviewer_quorum": 2, "countersign_ttl_hours": 72, "appeal_window_hours": 48})
        self.assertEqual(201, status)
        self.assertEqual("st1", payload["stage_id"])
        status, payload = self._route("GET", "/advancement/stages")
        self.assertEqual(200, status)
        self.assertEqual("st1", payload["items"][0]["stage_id"])

    def test_replayed_request_returns_200(self):
        body = {"request_id": "tr", "track_id": "ta", "name": "赛道"}
        first, _ = self._route("POST", "/advancement/tracks", dict(body))
        second, payload = self._route("POST", "/advancement/tracks", dict(body))
        self.assertEqual(201, first)
        self.assertEqual(200, second)
        self.assertTrue(payload["replayed"])

    def test_invalid_body_returns_400(self):
        status, payload = self._route("POST", "/advancement/stages", {"request_id": "x"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_permission_denied_maps_to_403(self):
        status, payload = self._route("POST", "/advancement/tracks",
                                      {"request_id": "tr", "track_id": "ta", "name": "赛道"},
                                      actor="nobody")
        self.assertEqual(404, status)
        self.domain.register_actor(request_id="judge", actor_id="a1", new_actor_id="j1",
                                   display_name="评委", role="judge", organization_id="o1")
        status, payload = self._route("POST", "/advancement/tracks",
                                      {"request_id": "tr", "track_id": "ta", "name": "赛道"},
                                      actor="j1")
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_tick_endpoint(self):
        status, payload = self._route("POST", "/advancement/tick", {})
        self.assertEqual(200, status)
        self.assertEqual(0, payload["expired_runs"])


if __name__ == "__main__":
    unittest.main()
