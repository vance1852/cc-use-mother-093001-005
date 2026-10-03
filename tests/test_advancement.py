import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from creative_program_foundation.advancement import AdvancementService
from creative_program_foundation.clock import MutableClock
from creative_program_foundation.errors import (ConflictError, PermissionDenied,
                                                ValidationError)
from creative_program_foundation.service import DomainService
from creative_program_foundation.storage import Database

START = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
DEADLINE = "2026-10-02T00:00:00Z"


class AdvancementTestBase(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = MutableClock(START)
        self.domain = DomainService(self.database, self.clock)
        self.service = AdvancementService(self.database, self.clock, domain=self.domain)
        self.domain.register_organization(request_id="org", actor_id="bootstrap",
                                          organization_id="o1", name="组委会")
        self.domain.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                   display_name="管理员", role="admin", organization_id="o1")
        for actor_id, role in (("op1", "operator"), ("r1", "reviewer"), ("r2", "reviewer"),
                               ("pub1", "publisher"), ("j1", "judge"), ("j2", "judge"),
                               ("j3", "judge"), ("p1", "participant"), ("p2", "participant"),
                               ("p3", "participant"), ("p4", "participant"), ("au1", "auditor")):
            self.domain.register_actor(request_id=f"actor-{actor_id}", actor_id="a1",
                                       new_actor_id=actor_id, display_name=actor_id,
                                       role=role, organization_id="o1")
        self.service.create_stage(request_id="stage", actor_id="op1", stage_id="st1",
                                  name="初赛", sequence=1, reviewer_quorum=2,
                                  countersign_ttl_hours=72, appeal_window_hours=48)
        for track_id in ("ta", "tb"):
            self.service.create_track(request_id=f"track-{track_id}", actor_id="op1",
                                      track_id=track_id, name=f"赛道{track_id}")

    def tearDown(self):
        self.database.close()

    def _rule(self, tag, **overrides):
        options = {"weights": {"创意": 0.5, "执行": 0.5}, "tie_policy": "tie_break",
                   "tie_break_dimensions": ["创意"], "missing_score_policy": "exclude_judge",
                   "late_score_policy": "reject", "score_deadline": DEADLINE}
        options.update(overrides)
        receipt = self.service.create_rule_version(request_id=f"rule-{tag}", actor_id="op1",
                                                   stage_id="st1", **options)
        self.service.freeze_rule_version(request_id=f"freeze-{tag}", actor_id="op1",
                                         rule_version_id=receipt["rule_version_id"])
        return receipt["rule_version_id"]

    def _entry(self, entry_id, track_id, participant_id):
        self.service.register_entry(request_id=f"entry-{entry_id}", actor_id="op1",
                                    entry_id=entry_id, stage_id="st1", track_id=track_id,
                                    participant_id=participant_id, title=f"作品{entry_id}")

    def _score(self, tag, judge_id, entry_id, scores, **kwargs):
        return self.service.submit_score(request_id=f"score-{tag}", actor_id=judge_id,
                                         entry_id=entry_id, scores=scores, **kwargs)

    def _publish(self, run_id, tag=""):
        self.service.countersign(request_id=f"sign-{tag}1", actor_id="r1",
                                 run_id=run_id, decision="approved")
        self.service.countersign(request_id=f"sign-{tag}2", actor_id="r2",
                                 run_id=run_id, decision="approved")
        return self.service.publish_run(request_id=f"publish-{tag}", actor_id="pub1", run_id=run_id)

    def _items(self, run_id):
        detail = self.service.get_run_detail(actor_id="a1", run_id=run_id)
        return {item["entry_id"]: item for item in detail["items"]}


class RuleAndSetupTest(AdvancementTestBase):
    def test_compute_requires_frozen_rule(self):
        receipt = self.service.create_rule_version(
            request_id="rule-draft", actor_id="op1", stage_id="st1",
            weights={"创意": 1.0}, tie_policy="share", missing_score_policy="zero",
            late_score_policy="accept")
        self.service.assign_track_rule(request_id="tr", actor_id="op1", stage_id="st1",
                                       track_id="ta", rule_version_id=receipt["rule_version_id"])
        self._entry("e1", "ta", "p1")
        with self.assertRaises(ValidationError):
            self.service.compute_run(request_id="run", actor_id="op1", stage_id="st1")

    def test_track_without_rule_rejected(self):
        self._entry("e1", "ta", "p1")
        with self.assertRaises(ValidationError):
            self.service.compute_run(request_id="run", actor_id="op1", stage_id="st1")

    def test_rule_versions_are_kept_per_stage(self):
        first = self._rule("v1")
        second = self._rule("v2", weights={"创意": 0.6, "执行": 0.4})
        detail = self.service.get_stage_detail(actor_id="a1", stage_id="st1")
        versions = [rule["rule_version_id"] for rule in detail["rule_versions"]]
        self.assertEqual([first, second], versions)
        self.assertEqual([1, 2], [rule["version"] for rule in detail["rule_versions"]])

    def test_refreeze_conflicts(self):
        rule_id = self._rule("v1")
        with self.assertRaises(ConflictError):
            self.service.freeze_rule_version(request_id="again", actor_id="op1",
                                             rule_version_id=rule_id)

    def test_invalid_weights_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.create_rule_version(
                request_id="bad", actor_id="op1", stage_id="st1",
                weights={"创意": -1.0}, tie_policy="share", missing_score_policy="zero",
                late_score_policy="accept")

    def test_participant_cannot_configure(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_track(request_id="blocked", actor_id="p1",
                                      track_id="tx", name="越权赛道")


class ScoringTest(AdvancementTestBase):
    def setUp(self):
        super().setUp()
        self.rule_id = self._rule("v1")
        self.service.assign_track_rule(request_id="tr", actor_id="op1", stage_id="st1",
                                       track_id="ta", rule_version_id=self.rule_id)
        self.service.assign_judge(request_id="ja1", actor_id="op1", stage_id="st1",
                                  track_id="ta", judge_id="j1")
        self.service.assign_judge(request_id="ja2", actor_id="op1", stage_id="st1",
                                  track_id="ta", judge_id="j2")
        self._entry("e1", "ta", "p1")

    def test_score_dimension_must_match_rule(self):
        with self.assertRaises(ValidationError):
            self._score("bad", "j1", "e1", {"创意": 90})

    def test_deduction_requires_basis(self):
        with self.assertRaises(ValidationError):
            self._score("bad", "j1", "e1", {"创意": 90, "执行": 80}, deduction=3)

    def test_unassigned_judge_rejected(self):
        with self.assertRaises(PermissionDenied):
            self._score("bad", "j3", "e1", {"创意": 90, "执行": 80})

    def test_sealed_score_cannot_change(self):
        self._score("s1", "j1", "e1", {"创意": 90, "执行": 80})
        self.service.seal_scores(request_id="seal", actor_id="op1", stage_id="st1")
        with self.assertRaises(ConflictError):
            self._score("s2", "j1", "e1", {"创意": 10, "执行": 10})

    def test_revoked_score_cannot_change(self):
        receipt = self._score("s1", "j1", "e1", {"创意": 90, "执行": 80})
        self.service.revoke_score(request_id="revoke", actor_id="a1",
                                  score_id=receipt["score_id"], reason="评委申报利益冲突")
        with self.assertRaises(ConflictError):
            self._score("s2", "j1", "e1", {"创意": 10, "执行": 10})

    def test_judge_cannot_see_unsealed_scores_of_others(self):
        self._score("s1", "j1", "e1", {"创意": 90, "执行": 80})
        visible = self.service.list_scores(actor_id="j2", stage_id="st1")
        self.assertEqual([], visible)
        own = self.service.list_scores(actor_id="j1", stage_id="st1")
        self.assertEqual(1, len(own))
        self.service.seal_scores(request_id="seal", actor_id="op1", stage_id="st1")
        visible = self.service.list_scores(actor_id="j2", stage_id="st1")
        self.assertEqual(1, len(visible))

    def test_score_submit_is_idempotent(self):
        first = self._score("s1", "j1", "e1", {"创意": 90, "执行": 80})
        replay = self._score("s1", "j1", "e1", {"创意": 90, "执行": 80})
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["score_id"], replay["score_id"])


class RankingTest(AdvancementTestBase):
    def _prepare(self, rule_options=None, quota=2, constraint=None):
        rule_id = self._rule("v1", **(rule_options or {}))
        self.service.assign_track_rule(request_id="tr", actor_id="op1", stage_id="st1",
                                       track_id="ta", rule_version_id=rule_id)
        self.service.set_track_quota(request_id="quota", actor_id="op1", stage_id="st1",
                                     track_id="ta", quota=quota)
        if constraint:
            self.service.set_award_constraint(request_id="award", actor_id="op1",
                                              stage_id="st1", **constraint)
        self.service.assign_judge(request_id="ja1", actor_id="op1", stage_id="st1",
                                  track_id="ta", judge_id="j1")
        self.service.assign_judge(request_id="ja2", actor_id="op1", stage_id="st1",
                                  track_id="ta", judge_id="j2")
        for entry_id, participant_id in (("e1", "p1"), ("e2", "p2"), ("e3", "p3")):
            self._entry(entry_id, "ta", participant_id)
        return rule_id

    def _compute(self):
        receipt = self.service.compute_run(request_id="run", actor_id="op1", stage_id="st1")
        return receipt["run_id"]

    def test_tie_break_dimension_decides_quota_boundary(self):
        self._prepare()
        self._score("s1", "j1", "e1", {"创意": 90, "执行": 80})
        self._score("s2", "j2", "e1", {"创意": 80, "执行": 80})
        self._score("s3", "j1", "e2", {"创意": 80, "执行": 90})
        self._score("s4", "j2", "e2", {"创意": 80, "执行": 80})
        self._score("s5", "j1", "e3", {"创意": 60, "执行": 60})
        self._score("s6", "j2", "e3", {"创意": 60, "执行": 60})
        items = self._items(self._compute())
        self.assertEqual(82.5, items["e1"]["total_score"])
        self.assertEqual(82.5, items["e2"]["total_score"])
        self.assertEqual(1, items["e1"]["rank"])
        self.assertEqual(2, items["e2"]["rank"])
        self.assertIn("创意", items["e1"]["explanation"]["summary"])
        self.assertTrue(items["e1"]["advanced"])
        self.assertTrue(items["e2"]["advanced"])
        self.assertFalse(items["e3"]["advanced"])

    def test_share_policy_lets_tied_entries_advance_together(self):
        self._prepare(rule_options={"tie_policy": "share"}, quota=1)
        self._score("s1", "j1", "e1", {"创意": 80, "执行": 80})
        self._score("s2", "j2", "e1", {"创意": 80, "执行": 80})
        self._score("s3", "j1", "e2", {"创意": 80, "执行": 80})
        self._score("s4", "j2", "e2", {"创意": 80, "执行": 80})
        self._score("s5", "j1", "e3", {"创意": 60, "执行": 60})
        self._score("s6", "j2", "e3", {"创意": 60, "执行": 60})
        items = self._items(self._compute())
        self.assertEqual(1, items["e1"]["rank"])
        self.assertEqual(1, items["e2"]["rank"])
        self.assertTrue(items["e1"]["advanced"])
        self.assertTrue(items["e2"]["advanced"])
        self.assertEqual(3, items["e3"]["rank"])

    def test_late_scores_follow_frozen_rule(self):
        self._prepare()
        self._score("s1", "j1", "e1", {"创意": 80, "执行": 80})
        self.clock.advance(hours=20)  # 越过 2026-10-02T00:00:00Z
        self._score("s2", "j2", "e1", {"创意": 100, "执行": 100})
        items = self._items(self._compute())
        self.assertEqual(80.0, items["e1"]["total_score"])
        excluded = items["e1"]["explanation"]["excluded_judges"]
        self.assertEqual("late", excluded[0]["reason"])

    def test_late_scores_accepted_when_rule_allows(self):
        self._prepare(rule_options={"late_score_policy": "accept"})
        self._score("s1", "j1", "e1", {"创意": 80, "执行": 80})
        self.clock.advance(hours=20)
        self._score("s2", "j2", "e1", {"创意": 100, "执行": 100})
        items = self._items(self._compute())
        self.assertEqual(90.0, items["e1"]["total_score"])

    def test_missing_score_exclude_judge(self):
        self._prepare()
        self._score("s1", "j1", "e1", {"创意": 80, "执行": 80})
        items = self._items(self._compute())
        self.assertEqual(80.0, items["e1"]["total_score"])
        self.assertEqual(1, items["e1"]["explanation"]["judge_denominator"])

    def test_missing_score_zero_fill(self):
        self._prepare(rule_options={"missing_score_policy": "zero"})
        self._score("s1", "j1", "e1", {"创意": 80, "执行": 80})
        items = self._items(self._compute())
        self.assertEqual(40.0, items["e1"]["total_score"])
        self.assertEqual(1, items["e1"]["explanation"]["missing_zero_filled"])

    def test_revoked_score_excluded(self):
        self._prepare()
        self._score("s1", "j1", "e1", {"创意": 80, "执行": 80})
        receipt = self._score("s2", "j2", "e1", {"创意": 40, "执行": 40})
        self.service.revoke_score(request_id="revoke", actor_id="a1",
                                  score_id=receipt["score_id"], reason="评分无效")
        items = self._items(self._compute())
        self.assertEqual(80.0, items["e1"]["total_score"])
        reasons = [item["reason"] for item in items["e1"]["explanation"]["excluded_judges"]]
        self.assertEqual(["revoked"], reasons)

    def test_invalid_judge_excluded_from_valid_set(self):
        self._prepare()
        self.service.assign_judge(request_id="ja3", actor_id="op1", stage_id="st1",
                                  track_id="ta", judge_id="j3", valid=False)
        self._score("s1", "j1", "e1", {"创意": 80, "执行": 80})
        items = self._items(self._compute())
        self.assertEqual(80.0, items["e1"]["total_score"])

    def test_disqualified_entry_unranked(self):
        self._prepare()
        self.service.disqualify_entry(request_id="dq", actor_id="op1",
                                      entry_id="e1", reason="材料造假")
        items = self._items(self._compute())
        self.assertIsNone(items["e1"]["rank"])
        self.assertIn("资格已取消", items["e1"]["explanation"]["summary"])

    def test_pass_score_blocks_advancement(self):
        self._prepare(rule_options={"pass_score": 85.0})
        self._score("s1", "j1", "e1", {"创意": 80, "执行": 80})
        self._score("s2", "j2", "e1", {"创意": 80, "执行": 80})
        items = self._items(self._compute())
        self.assertFalse(items["e1"]["advanced"])
        self.assertIn("及格线", items["e1"]["explanation"]["summary"])

    def test_award_total_and_track_cap(self):
        self._prepare(quota=3, constraint={"total_awards": 2, "per_track_cap": 2})
        self._score("s1", "j1", "e1", {"创意": 90, "执行": 90})
        self._score("s2", "j2", "e1", {"创意": 90, "执行": 90})
        self._score("s3", "j1", "e2", {"创意": 80, "执行": 80})
        self._score("s4", "j2", "e2", {"创意": 80, "执行": 80})
        self._score("s5", "j1", "e3", {"创意": 70, "执行": 70})
        self._score("s6", "j2", "e3", {"创意": 70, "执行": 70})
        items = self._items(self._compute())
        self.assertTrue(items["e1"]["awarded"])
        self.assertTrue(items["e2"]["awarded"])
        self.assertFalse(items["e3"]["awarded"])
        self.assertIn("总量", items["e3"]["explanation"]["award"]["reason"])

    def test_award_per_track_cap(self):
        self._prepare(quota=3, constraint={"total_awards": 3, "per_track_cap": 1})
        self._score("s1", "j1", "e1", {"创意": 90, "执行": 90})
        self._score("s2", "j2", "e1", {"创意": 90, "执行": 90})
        self._score("s3", "j1", "e2", {"创意": 80, "执行": 80})
        self._score("s4", "j2", "e2", {"创意": 80, "执行": 80})
        items = self._items(self._compute())
        self.assertTrue(items["e1"]["awarded"])
        self.assertFalse(items["e2"]["awarded"])
        self.assertIn("上限", items["e2"]["explanation"]["award"]["reason"])

    def test_missing_dimension_counts_zero_with_note(self):
        rule_id = self._rule("v1", weights={"创意": 0.5, "执行": 0.5})
        self.service.assign_track_rule(request_id="tr", actor_id="op1", stage_id="st1",
                                       track_id="ta", rule_version_id=rule_id)
        self.service.assign_judge(request_id="ja1", actor_id="op1", stage_id="st1",
                                  track_id="ta", judge_id="j1")
        self._entry("e1", "ta", "p1")
        self._score("s1", "j1", "e1", {"创意": 80, "执行": 80})
        # 换成三维规则后，旧评分缺少维度
        new_rule = self._rule("v2", weights={"创意": 0.4, "执行": 0.3, "表现": 0.3})
        self.service.assign_track_rule(request_id="tr2", actor_id="op1", stage_id="st1",
                                       track_id="ta", rule_version_id=new_rule)
        items = self._items(self._compute())
        self.assertEqual(["表现"], items["e1"]["explanation"]["missing_dimensions"])
        self.assertEqual(56.0, items["e1"]["total_score"])


class CountersignAndPublishTest(AdvancementTestBase):
    def setUp(self):
        super().setUp()
        rule_id = self._rule("v1")
        self.service.assign_track_rule(request_id="tr", actor_id="op1", stage_id="st1",
                                       track_id="ta", rule_version_id=rule_id)
        self.service.set_track_quota(request_id="quota", actor_id="op1", stage_id="st1",
                                     track_id="ta", quota=1)
        self.service.assign_judge(request_id="ja1", actor_id="op1", stage_id="st1",
                                  track_id="ta", judge_id="j1")
        self._entry("e1", "ta", "p1")
        self._entry("e2", "ta", "p2")
        self._score("s1", "j1", "e1", {"创意": 90, "执行": 90})
        self._score("s2", "j1", "e2", {"创意": 70, "执行": 70})
        self.run_id = self.service.compute_run(
            request_id="run", actor_id="op1", stage_id="st1")["run_id"]

    def test_quorum_required_before_publish(self):
        self.service.countersign(request_id="sign1", actor_id="r1",
                                 run_id=self.run_id, decision="approved")
        with self.assertRaises(ConflictError):
            self.service.publish_run(request_id="publish", actor_id="pub1", run_id=self.run_id)

    def test_publisher_must_be_independent_from_reviewers(self):
        self.service.countersign(request_id="sign1", actor_id="r1",
                                 run_id=self.run_id, decision="approved")
        self.service.countersign(request_id="sign2", actor_id="r2",
                                 run_id=self.run_id, decision="approved")
        with self.assertRaises(PermissionDenied):
            self.service.countersign(request_id="sign3", actor_id="pub1",
                                     run_id=self.run_id, decision="approved")

    def test_rejection_terminates_candidate(self):
        self.service.countersign(request_id="sign1", actor_id="r1",
                                 run_id=self.run_id, decision="rejected", comment="数据有误")
        with self.assertRaises(ConflictError):
            self.service.countersign(request_id="sign2", actor_id="r2",
                                     run_id=self.run_id, decision="approved")

    def test_double_sign_rejected(self):
        self.service.countersign(request_id="sign1", actor_id="r1",
                                 run_id=self.run_id, decision="approved")
        with self.assertRaises(ConflictError):
            self.service.countersign(request_id="sign2", actor_id="r1",
                                     run_id=self.run_id, decision="approved")

    def test_publish_assigns_version_and_notifications(self):
        receipt = self._publish(self.run_id)
        self.assertEqual(1, receipt["version"])
        mine = self.service.my_entries(actor_id="p1")
        kinds = [note["kind"] for note in mine["notifications"]]
        self.assertEqual(["advancement"], kinds)
        mine = self.service.my_entries(actor_id="p2")
        self.assertEqual(["elimination"], [note["kind"] for note in mine["notifications"]])

    def test_published_run_cannot_be_countersigned_again(self):
        self._publish(self.run_id)
        with self.assertRaises(ConflictError):
            self.service.countersign(request_id="late", actor_id="r1",
                                     run_id=self.run_id, decision="approved")

    def test_second_publish_supersedes_first(self):
        first = self._publish(self.run_id, tag="a")
        run2 = self.service.compute_run(request_id="run2", actor_id="op1", stage_id="st1")["run_id"]
        second = self._publish(run2, tag="b")
        self.assertEqual(2, second["version"])
        runs = {run["run_id"]: run for run in self.service.list_runs(actor_id="a1")}
        self.assertEqual("superseded", runs[self.run_id]["status"])
        self.assertEqual("published", runs[run2]["status"])
        public = self.service.public_results(stage_id="st1")
        self.assertEqual(2, public["version"])
        self.assertEqual(first["run_id"], runs[self.run_id]["run_id"])

    def test_candidate_expires_after_ttl(self):
        self.clock.advance(hours=73)
        swept = self.service.tick(actor_id="op1")
        self.assertEqual(1, swept["expired_runs"])
        with self.assertRaises(ConflictError):
            self.service.countersign(request_id="late", actor_id="r1",
                                     run_id=self.run_id, decision="approved")


class VisibilityTest(AdvancementTestBase):
    def setUp(self):
        super().setUp()
        rule_id = self._rule("v1")
        self.service.assign_track_rule(request_id="tr", actor_id="op1", stage_id="st1",
                                       track_id="ta", rule_version_id=rule_id)
        self.service.set_track_quota(request_id="quota", actor_id="op1", stage_id="st1",
                                     track_id="ta", quota=1)
        self.service.assign_judge(request_id="ja1", actor_id="op1", stage_id="st1",
                                  track_id="ta", judge_id="j1")
        self._entry("e1", "ta", "p1")
        self._entry("e2", "ta", "p2")
        self._score("s1", "j1", "e1", {"创意": 90, "执行": 90})
        self._score("s2", "j1", "e2", {"创意": 70, "执行": 70})
        self.run_id = self.service.compute_run(
            request_id="run", actor_id="op1", stage_id="st1")["run_id"]

    def test_participant_cannot_see_candidate(self):
        with self.assertRaises(PermissionDenied):
            self.service.get_run_detail(actor_id="p1", run_id=self.run_id)

    def test_participant_sees_own_detail_after_publish(self):
        self._publish(self.run_id)
        detail = self.service.get_run_detail(actor_id="p1", run_id=self.run_id)
        mine = [item for item in detail["items"] if item["entry_id"] == "e1"]
        other = [item for item in detail["items"] if item["entry_id"] == "e2"]
        self.assertIn("explanation", mine[0])
        self.assertNotIn("explanation", other[0])

    def test_participant_scores_are_scoped_to_own_entries(self):
        visible = self.service.list_scores(actor_id="p1", stage_id="st1")
        self.assertEqual(["e1"], [item["entry_id"] for item in visible])
        visible = self.service.list_scores(actor_id="p2", stage_id="st1")
        self.assertEqual(["e2"], [item["entry_id"] for item in visible])

    def test_explain_requires_privilege_or_ownership(self):
        self._publish(self.run_id)
        with self.assertRaises(PermissionDenied):
            self.service.explain_item(actor_id="p1", run_id=self.run_id, entry_id="e2")
        own = self.service.explain_item(actor_id="p1", run_id=self.run_id, entry_id="e1")
        self.assertIn("summary", own["item"]["explanation"])
        admin_view = self.service.explain_item(actor_id="a1", run_id=self.run_id, entry_id="e2")
        self.assertIn("summary", admin_view["item"]["explanation"])
        with self.assertRaises(PermissionDenied):
            self.service.explain_item(actor_id="j1", run_id=self.run_id, entry_id="e1")

    def test_public_results_hide_candidate(self):
        public = self.service.public_results(stage_id="st1")
        self.assertFalse(public["published"])
        self._publish(self.run_id)
        public = self.service.public_results(stage_id="st1")
        self.assertTrue(public["published"])
        self.assertEqual(2, len(public["items"]))


class AppealTest(AdvancementTestBase):
    def setUp(self):
        super().setUp()
        rule_id = self._rule("v1")
        self.service.assign_track_rule(request_id="tr", actor_id="op1", stage_id="st1",
                                       track_id="ta", rule_version_id=rule_id)
        self.service.set_track_quota(request_id="quota", actor_id="op1", stage_id="st1",
                                     track_id="ta", quota=1)
        self.service.assign_judge(request_id="ja1", actor_id="op1", stage_id="st1",
                                  track_id="ta", judge_id="j1")
        self.service.assign_judge(request_id="ja2", actor_id="op1", stage_id="st1",
                                  track_id="ta", judge_id="j2")
        self._entry("e1", "ta", "p1")
        self._entry("e2", "ta", "p2")
        self._score("s1", "j1", "e1", {"创意": 90, "执行": 90})
        self.deducted = self._score("s2", "j2", "e1", {"创意": 90, "执行": 90},
                                    deduction=10, deduction_basis="疑似抄袭待查")
        self._score("s3", "j1", "e2", {"创意": 88, "执行": 88})
        self._score("s4", "j2", "e2", {"创意": 86, "执行": 86})
        self.run_id = self.service.compute_run(
            request_id="run", actor_id="op1", stage_id="st1")["run_id"]
        self._publish(self.run_id)

    def _appeal(self, tag="a1", **overrides):
        options = {"request_id": f"appeal-{tag}", "actor_id": "p1", "run_id": self.run_id,
                   "entry_id": "e1", "target_type": "score",
                   "target_id": self.deducted["score_id"], "reason": "扣分依据不成立"}
        options.update(overrides)
        return self.service.file_appeal(**options)

    def test_appeal_requires_published_run(self):
        run2 = self.service.compute_run(request_id="run2", actor_id="op1", stage_id="st1")["run_id"]
        with self.assertRaises(ValidationError):
            self._appeal(run_id=run2)

    def test_appeal_must_reference_own_entry(self):
        with self.assertRaises(PermissionDenied):
            self._appeal(actor_id="p2")

    def test_appeal_target_must_match_entry(self):
        other = self.service.list_scores(actor_id="a1", stage_id="st1", entry_id="e2")[0]
        with self.assertRaises(ValidationError):
            self._appeal(target_id=other["score_id"])

    def test_appeal_window_enforced(self):
        self.clock.advance(hours=49)
        with self.assertRaises(ValidationError):
            self._appeal()

    def test_duplicate_open_appeal_rejected(self):
        self._appeal()
        with self.assertRaises(ConflictError):
            self._appeal(tag="a2")

    def test_rejected_appeal_keeps_ranking(self):
        appeal = self._appeal()
        receipt = self.service.adjudicate_appeal(
            request_id="adj", actor_id="a1", appeal_id=appeal["appeal_id"],
            decision="rejected", note="证据不足")
        self.assertEqual("adjudicated_rejected", receipt["status"])
        self.assertNotIn("result_run_id", receipt)

    def test_upheld_appeal_creates_new_version_with_change_summary(self):
        appeal = self._appeal()
        receipt = self.service.adjudicate_appeal(
            request_id="adj", actor_id="a1", appeal_id=appeal["appeal_id"],
            decision="upheld", note="扣分依据不成立", remedy="adjust_deduction",
            deduction=0, deduction_basis=None)
        run2 = receipt["result_run_id"]
        detail = self.service.get_run_detail(actor_id="a1", run_id=run2)
        summary = detail["change_summary"]
        self.assertEqual(self.run_id, summary["compared_to_run_id"])
        changed = {change["entry_id"] for change in summary["rank_changes"]}
        self.assertEqual({"e1", "e2"}, changed)
        self.assertEqual({"e1", "e2"}, set(summary["affected_notifications"]))
        items = self._items(run2)
        self.assertEqual(90.0, items["e1"]["total_score"])
        self.assertEqual(1, items["e1"]["rank"])
        published = self._publish(run2, tag="v2")
        self.assertEqual(2, published["version"])
        old = self.service.get_run_detail(actor_id="a1", run_id=self.run_id)
        self.assertEqual("superseded", old["status"])
        old_items = {item["entry_id"]: item for item in old["items"]}
        self.assertEqual(85.0, old_items["e1"]["total_score"])  # 旧版本不被改写

    def test_qualification_appeal_reinstates_entry(self):
        self.service.disqualify_entry(request_id="dq", actor_id="op1",
                                      entry_id="e2", reason="资格存疑")
        run2 = self.service.compute_run(request_id="run2", actor_id="op1", stage_id="st1")["run_id"]
        self._publish(run2, tag="v2")
        appeal = self.service.file_appeal(request_id="appeal-q", actor_id="p2", run_id=run2,
                                          entry_id="e2", target_type="qualification",
                                          target_id="e2", reason="资格材料已补齐")
        receipt = self.service.adjudicate_appeal(
            request_id="adj-q", actor_id="a1", appeal_id=appeal["appeal_id"],
            decision="upheld", note="资格有效", remedy="reinstate")
        items = self._items(receipt["result_run_id"])
        self.assertIsNotNone(items["e2"]["rank"])

    def test_appeal_expires_after_deadline(self):
        appeal = self._appeal()
        self.clock.advance(hours=49)
        swept = self.service.tick(actor_id="op1")
        self.assertEqual(1, swept["expired_appeals"])
        with self.assertRaises(ConflictError):
            self.service.adjudicate_appeal(
                request_id="adj", actor_id="a1", appeal_id=appeal["appeal_id"],
                decision="rejected", note="已过期")


class ReplayTest(AdvancementTestBase):
    def setUp(self):
        super().setUp()
        rule_id = self._rule("v1")
        self.service.assign_track_rule(request_id="tr", actor_id="op1", stage_id="st1",
                                       track_id="ta", rule_version_id=rule_id)
        self.service.assign_judge(request_id="ja1", actor_id="op1", stage_id="st1",
                                  track_id="ta", judge_id="j1")
        self._entry("e1", "ta", "p1")
        self._score("s1", "j1", "e1", {"创意": 90, "执行": 80})
        self.run_id = self.service.compute_run(
            request_id="run", actor_id="op1", stage_id="st1")["run_id"]

    def test_replay_matches_stored_output(self):
        result = self.service.replay_run(actor_id="a1", run_id=self.run_id)
        self.assertTrue(result["match"])
        self.assertEqual(result["stored_output_hash"], result["replayed_output_hash"])

    def test_replay_detects_tampered_items(self):
        self.database.connection.execute(
            "UPDATE advancement_run_items SET total_score=1.0 WHERE run_id=? AND entry_id='e1'",
            (self.run_id,))
        result = self.service.replay_run(actor_id="a1", run_id=self.run_id)
        self.assertFalse(result["match"])
        self.assertEqual("e1", result["differences"][0]["entry_id"])

    def test_replay_uses_frozen_snapshot_not_live_facts(self):
        self.service.disqualify_entry(request_id="dq", actor_id="op1",
                                      entry_id="e1", reason="事后取消")
        result = self.service.replay_run(actor_id="a1", run_id=self.run_id)
        self.assertTrue(result["match"])

    def test_participant_cannot_replay(self):
        with self.assertRaises(PermissionDenied):
            self.service.replay_run(actor_id="p1", run_id=self.run_id)


class RestartContinuationTest(unittest.TestCase):
    def test_countersign_and_appeal_clocks_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "advancement.sqlite3"
            clock = MutableClock(START)
            database = Database(path)
            domain = DomainService(database, clock)
            service = AdvancementService(database, clock, domain=domain)
            domain.register_organization(request_id="org", actor_id="bootstrap",
                                         organization_id="o1", name="组委会")
            domain.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                  display_name="管理员", role="admin", organization_id="o1")
            for actor_id, role in (("op1", "operator"), ("r1", "reviewer"), ("r2", "reviewer"),
                                   ("pub1", "publisher"), ("j1", "judge"), ("p1", "participant")):
                domain.register_actor(request_id=f"actor-{actor_id}", actor_id="a1",
                                      new_actor_id=actor_id, display_name=actor_id,
                                      role=role, organization_id="o1")
            service.create_stage(request_id="stage", actor_id="op1", stage_id="st1",
                                 name="初赛", sequence=1, reviewer_quorum=2,
                                 countersign_ttl_hours=72, appeal_window_hours=48)
            service.create_track(request_id="track", actor_id="op1", track_id="ta", name="赛道")
            rule = service.create_rule_version(
                request_id="rule", actor_id="op1", stage_id="st1",
                weights={"创意": 1.0}, tie_policy="share", missing_score_policy="zero",
                late_score_policy="accept")
            service.freeze_rule_version(request_id="freeze", actor_id="op1",
                                        rule_version_id=rule["rule_version_id"])
            service.assign_track_rule(request_id="tr", actor_id="op1", stage_id="st1",
                                      track_id="ta", rule_version_id=rule["rule_version_id"])
            service.assign_judge(request_id="ja", actor_id="op1", stage_id="st1",
                                 track_id="ta", judge_id="j1")
            service.register_entry(request_id="entry", actor_id="op1", entry_id="e1",
                                   stage_id="st1", track_id="ta", participant_id="p1", title="作品")
            service.submit_score(request_id="score", actor_id="j1", entry_id="e1",
                                 scores={"创意": 90})
            run_id = service.compute_run(request_id="run", actor_id="op1",
                                         stage_id="st1")["run_id"]
            service.countersign(request_id="sign1", actor_id="r1",
                                run_id=run_id, decision="approved")
            database.close()

            # 重启：新服务实例从 SQLite 恢复，会签进度保留
            database = Database(path)
            domain = DomainService(database, clock)
            service = AdvancementService(database, clock, domain=domain)
            service.countersign(request_id="sign2", actor_id="r2",
                                run_id=run_id, decision="approved")
            published = service.publish_run(request_id="publish", actor_id="pub1", run_id=run_id)
            self.assertEqual(1, published["version"])
            score_id = service.list_scores(actor_id="a1", stage_id="st1")[0]["score_id"]
            appeal = service.file_appeal(request_id="appeal", actor_id="p1", run_id=run_id,
                                         entry_id="e1", target_type="score",
                                         target_id=score_id, reason="分数有误")
            database.close()

            # 再次重启并越过申诉时限：时钟从持久化的截止时间继续
            clock.advance(hours=49)
            database = Database(path)
            domain = DomainService(database, clock)
            service = AdvancementService(database, clock, domain=domain)
            swept = service.tick(actor_id="op1")
            self.assertEqual(1, swept["expired_appeals"])
            appeals = service.list_appeals(actor_id="a1")
            self.assertEqual("expired", appeals[0]["status"])
            valid, _ = domain.verify_audit()
            self.assertTrue(valid)
            database.close()


if __name__ == "__main__":
    unittest.main()
