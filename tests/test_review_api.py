import unittest
from datetime import datetime, timezone

from creative_program_foundation.api import route
from creative_program_foundation.review_service import ReviewService
from creative_program_foundation.storage import Database


class ManualClock:
    def __init__(self, value):
        self._value = value

    def now(self):
        return self._value


RULE_CONFIG = {
    "dimensions": [{"name": "创意", "weight": 0.6}, {"name": "表现", "weight": 0.4}],
    "score_due_at": "2026-09-10T00:00:00Z",
    "late_score_policy": "reject",
    "missing_score_policy": "average",
    "revoked_score_policy": "treat_as_missing",
    "boundary_policy": "strict",
    "appeal_window_seconds": 259200,
}


class ReviewApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = ReviewService(self.database, ManualClock(datetime(2026, 9, 1, tzinfo=timezone.utc)))
        self.post("/organizations", {"request_id": "org", "organization_id": "o1", "name": "机构"},
                  actor="bootstrap")
        self.post("/actors", {"request_id": "admin", "new_actor_id": "a1", "display_name": "管理员",
                              "role": "admin", "organization_id": "o1"}, actor="bootstrap")
        for actor_id, role in (("rv1", "reviewer"), ("pb1", "publisher"), ("j1", "judge"),
                               ("p1", "participant")):
            self.post("/actors", {"request_id": f"a-{actor_id}", "new_actor_id": actor_id,
                                  "display_name": actor_id, "role": role, "organization_id": "o1"},
                      actor="a1")

    def tearDown(self):
        self.database.close()

    def post(self, path, body, actor):
        return route(self.service, "POST", path, body, {"X-Actor-Id": actor})

    def get(self, path, actor):
        return route(self.service, "GET", path, None, {"X-Actor-Id": actor})

    def test_track_stage_and_rule_flow(self):
        status, payload = self.post("/review/tracks", {"request_id": "t1", "track_id": "t1",
                                                       "name": "视觉"}, actor="a1")
        self.assertEqual(201, status)
        self.assertFalse(payload["replayed"])
        status, payload = self.post("/review/tracks", {"request_id": "t1", "track_id": "t1",
                                                       "name": "视觉"}, actor="a1")
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])
        status, _ = self.post("/review/stages", {"request_id": "s1", "stage_id": "s1",
                                                 "name": "初赛", "sequence": 1}, actor="a1")
        self.assertEqual(201, status)
        status, payload = self.post("/review/rule-versions",
                                    {"request_id": "r1", "stage_id": "s1", "config": RULE_CONFIG},
                                    actor="a1")
        self.assertEqual(201, status)
        self.assertEqual(1, payload["version"])
        status, payload = self.get("/review/stages/s1", actor="a1")
        self.assertEqual(200, status)
        self.assertEqual("open", payload["status"])
        self.assertEqual(1, len(payload["rule_versions"]))

    def test_permission_denied_maps_to_403(self):
        status, payload = self.post("/review/tracks", {"request_id": "tx", "track_id": "tx",
                                                       "name": "越权"}, actor="j1")
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])
        status, payload = self.get("/review/pending", actor="p1")
        self.assertEqual(403, status)

    def test_unknown_review_route_returns_404(self):
        status, payload = self.get("/review/unknown", actor="a1")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_public_results_404_before_publish(self):
        self.post("/review/stages", {"request_id": "s1", "stage_id": "s1", "name": "初赛",
                                     "sequence": 1}, actor="a1")
        status, payload = self.get("/review/stages/s1/results", actor="p1")
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])

    def test_scores_requires_stage_id(self):
        status, payload = self.get("/review/scores", actor="a1")
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_missing_body_fields_return_400(self):
        status, payload = self.post("/review/tracks", {"request_id": "bad"}, actor="a1")
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])


if __name__ == "__main__":
    unittest.main()
