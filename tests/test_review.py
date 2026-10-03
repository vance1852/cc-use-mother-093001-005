import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from creative_program_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from creative_program_foundation.review_service import ReviewService
from creative_program_foundation.storage import Database


class ManualClock:
    def __init__(self, value):
        self._value = value

    def now(self):
        return self._value

    def set(self, value):
        self._value = value

    def advance(self, **kwargs):
        self._value = self._value + timedelta(**kwargs)


DUE = "2026-09-10T00:00:00Z"


def rule_config(**overrides):
    config = {
        "dimensions": [{"name": "创意", "weight": 0.6}, {"name": "表现", "weight": 0.4}],
        "score_due_at": DUE,
        "late_score_policy": "reject",
        "missing_score_policy": "average",
        "revoked_score_policy": "treat_as_missing",
        "boundary_policy": "strict",
        "appeal_window_seconds": 72 * 3600,
    }
    config.update(overrides)
    return config


class ReviewServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = ManualClock(datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc))
        self.service = ReviewService(self.database, self.clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="赛事机构")
        self.service.register_actor(request_id="actor-a1", actor_id="bootstrap",
                                    new_actor_id="a1", display_name="管理员",
                                    role="admin", organization_id="o1")
        actors = [("op1", "运营", "operator"),
                  ("rv1", "复核人", "reviewer"), ("pb1", "发布人", "publisher"),
                  ("au1", "审计员", "auditor"),
                  ("j1", "评委一", "judge"), ("j2", "评委二", "judge"), ("j3", "评委三", "judge")]
        for actor_id, name, role in actors:
            self.service.register_actor(request_id=f"actor-{actor_id}", actor_id="a1",
                                        new_actor_id=actor_id, display_name=name,
                                        role=role, organization_id="o1")
        for index in range(1, 7):
            self.service.register_actor(request_id=f"actor-p{index}", actor_id="a1",
                                        new_actor_id=f"p{index}", display_name=f"参赛人{index}",
                                        role="participant", organization_id="o1")
        for track_id in ("t1", "t2", "t3"):
            self.service.create_track(request_id=f"track-{track_id}", actor_id="a1",
                                      track_id=track_id, name=f"赛道{track_id}")
        for index, track_id in enumerate(("t1", "t2", "t3"), start=1):
            for offset in (0, 1):
                number = 2 * (index - 1) + offset + 1
                self.service.register_entry(request_id=f"entry-e{number}", actor_id="op1",
                                            entry_id=f"e{number}", track_id=track_id,
                                            participant_id=f"p{number}", title=f"作品{number}")

    def tearDown(self):
        self.database.close()

    # ------------------------------------------------------------------
    # 场景搭建辅助
    # ------------------------------------------------------------------

    def make_stage(self, stage_id="s1", config=None, judges=("j1", "j2"), quotas=None,
                   constraint=3, entries=("e1", "e2", "e3", "e4", "e5", "e6")):
        self.service.create_stage(request_id=f"stage-{stage_id}", actor_id="a1",
                                  stage_id=stage_id, name="初赛", sequence=1)
        rule = self.service.create_rule_version(request_id=f"rule-{stage_id}", actor_id="a1",
                                                stage_id=stage_id, config=config or rule_config())
        self.service.enroll_entries(request_id=f"enroll-{stage_id}", actor_id="op1",
                                    stage_id=stage_id, entry_ids=list(entries))
        entry_track = {"e1": "t1", "e2": "t1", "e3": "t2", "e4": "t2", "e5": "t3", "e6": "t3"}
        tracks = sorted({entry_track[entry] for entry in entries})
        for track_id in tracks:
            for judge_id in judges:
                self.service.assign_judge(request_id=f"assign-{stage_id}-{track_id}-{judge_id}",
                                          actor_id="a1", stage_id=stage_id, track_id=track_id,
                                          judge_id=judge_id)
        for track_id, count in (quotas or {track: 1 for track in tracks}).items():
            self.service.set_quota(request_id=f"quota-{stage_id}-{track_id}", actor_id="a1",
                                   stage_id=stage_id, track_id=track_id, advance_count=count)
        if constraint is not None:
            self.service.add_award_constraint(request_id=f"constraint-{stage_id}", actor_id="a1",
                                              stage_id=stage_id, kind="total_advance_exact",
                                              params={"value": constraint})
        return rule["rule_version_id"]

    def score(self, judge, entry, creativity, presentation, tag=""):
        first = self.service.submit_score(request_id=f"score{tag}-{judge}-{entry}-c", actor_id=judge,
                                          stage_id="s1", entry_id=entry, dimension="创意",
                                          value=creativity)
        second = self.service.submit_score(request_id=f"score{tag}-{judge}-{entry}-p", actor_id=judge,
                                           stage_id="s1", entry_id=entry, dimension="表现",
                                           value=presentation)
        return first["score_id"], second["score_id"]

    def score_all(self):
        self.score("j1", "e1", 90, 80)
        self.score("j2", "e1", 80, 70)
        self.score("j1", "e2", 70, 60)
        self.score("j2", "e2", 60, 60)
        self.score("j1", "e3", 80, 80)
        self.score("j2", "e3", 80, 80)
        self.score("j1", "e4", 70, 70)
        self.score("j2", "e4", 70, 70)
        self.score("j1", "e5", 88, 88)
        self.score("j2", "e5", 88, 88)
        self.score("j1", "e6", 66, 66)
        self.score("j2", "e6", 66, 66)

    def freeze(self, rule_version_id, stage_id="s1"):
        return self.service.freeze_stage(request_id=f"freeze-{stage_id}", actor_id="a1",
                                         stage_id=stage_id, rule_version_id=rule_version_id)

    def generate(self, stage_id="s1", tag="g1"):
        return self.service.generate_ranking(request_id=f"gen-{stage_id}-{tag}", actor_id="a1",
                                             stage_id=stage_id)

    def publish_flow(self, ranking_version_id, tag=""):
        self.service.countersign_ranking(request_id=f"sign-r{tag}", actor_id="rv1",
                                         ranking_version_id=ranking_version_id, sign_role="review")
        self.service.countersign_ranking(request_id=f"sign-p{tag}", actor_id="pb1",
                                         ranking_version_id=ranking_version_id, sign_role="publish")
        return self.service.publish_ranking(request_id=f"publish{tag}", actor_id="pb1",
                                            ranking_version_id=ranking_version_id)

    def published_v1(self):
        rule_id = self.make_stage()
        self.score_all()
        self.freeze(rule_id)
        candidate = self.generate()
        published = self.publish_flow(candidate["ranking_version_id"], tag="-v1")
        return published

    def outcomes(self, payload):
        return {item["entry_id"]: item for item in payload["entries"]}

    # ------------------------------------------------------------------
    # 规则与冻结
    # ------------------------------------------------------------------

    def test_rule_config_requires_weights_sum_to_one(self):
        self.service.create_stage(request_id="stage-x", actor_id="a1", stage_id="sx",
                                  name="初赛", sequence=1)
        with self.assertRaises(ValidationError):
            self.service.create_rule_version(request_id="rule-bad", actor_id="a1", stage_id="sx",
                                             config=rule_config(dimensions=[
                                                 {"name": "创意", "weight": 0.7},
                                                 {"name": "表现", "weight": 0.7}]))
        with self.assertRaises(ValidationError):
            self.service.create_rule_version(request_id="rule-bad2", actor_id="a1", stage_id="sx",
                                             config=rule_config(late_score_policy="sometimes"))

    def test_freeze_seals_on_time_and_rejects_late_scores(self):
        rule_id = self.make_stage(judges=("j1",), constraint=None, quotas={"t1": 1},
                                  entries=("e1",))
        self.service.submit_score(request_id="on-time", actor_id="j1", stage_id="s1",
                                  entry_id="e1", dimension="创意", value=80)
        self.clock.set(datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc))
        self.service.submit_score(request_id="late", actor_id="j1", stage_id="s1",
                                  entry_id="e1", dimension="表现", value=90)
        result = self.freeze(rule_id)
        self.assertEqual(1, result["sealed_scores"])
        self.assertEqual(1, result["rejected_late_scores"])
        scores = {item["dimension"]: item["status"]
                  for item in self.service.list_scores(actor_id="a1", stage_id="s1")}
        self.assertEqual("sealed", scores["创意"])
        self.assertEqual("rejected_late", scores["表现"])
        with self.assertRaises(ConflictError):
            self.service.submit_score(request_id="after-freeze", actor_id="j1", stage_id="s1",
                                      entry_id="e1", dimension="创意", value=10)

    def test_freeze_with_accept_policy_seals_late_scores(self):
        rule_id = self.make_stage(config=rule_config(late_score_policy="accept"),
                                  judges=("j1",), constraint=None, quotas={"t1": 1},
                                  entries=("e1",))
        self.clock.set(datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc))
        self.service.submit_score(request_id="late", actor_id="j1", stage_id="s1",
                                  entry_id="e1", dimension="创意", value=80)
        result = self.freeze(rule_id)
        self.assertEqual(1, result["sealed_scores"])
        self.assertEqual(0, result["rejected_late_scores"])

    def test_freeze_is_one_time(self):
        rule_id = self.make_stage(judges=("j1",), constraint=None, quotas={"t1": 1}, entries=("e1",))
        self.freeze(rule_id)
        with self.assertRaises(ConflictError):
            self.freeze(rule_id)

    # ------------------------------------------------------------------
    # 评委可见性
    # ------------------------------------------------------------------

    def test_judge_cannot_see_others_unsealed_scores(self):
        self.make_stage(constraint=None)
        self.score("j1", "e1", 90, 80)
        own = self.service.list_scores(actor_id="j1", stage_id="s1")
        self.assertEqual(2, len(own))
        others = self.service.list_scores(actor_id="j2", stage_id="s1")
        self.assertEqual([], others)

    def test_judge_sees_others_scores_after_sealing(self):
        rule_id = self.make_stage(constraint=None)
        self.score("j1", "e1", 90, 80)
        self.freeze(rule_id)
        visible = self.service.list_scores(actor_id="j2", stage_id="s1")
        self.assertEqual(2, len(visible))
        self.assertTrue(all(item["status"] == "sealed" for item in visible))

    def test_unassigned_judge_cannot_submit(self):
        self.make_stage(constraint=None)
        with self.assertRaises(PermissionDenied):
            self.score("j3", "e1", 90, 90)

    # ------------------------------------------------------------------
    # 候选榜、会签与发布
    # ------------------------------------------------------------------

    def test_candidate_ranking_and_publish_flow(self):
        published = self.published_v1()
        self.assertEqual("published", published["status"])
        outcomes = self.outcomes(published)
        self.assertEqual("advance", outcomes["e1"]["outcome"])
        self.assertEqual("eliminate", outcomes["e2"]["outcome"])
        self.assertEqual(81.0, outcomes["e1"]["total_score"])
        self.assertEqual(63.0, outcomes["e2"]["total_score"])
        explanation = outcomes["e2"]["explanation"]
        self.assertIn("名额 1", explanation["outcome_reason"])
        self.assertEqual(1, explanation["rule_version"])

    def test_countersign_requires_independent_people(self):
        rule_id = self.make_stage()
        self.score_all()
        self.freeze(rule_id)
        candidate = self.generate()
        ranking_id = candidate["ranking_version_id"]
        with self.assertRaises(PermissionDenied):
            self.service.countersign_ranking(request_id="self-sign", actor_id="a1",
                                             ranking_version_id=ranking_id, sign_role="review")
        with self.assertRaises(PermissionDenied):
            self.service.countersign_ranking(request_id="wrong-role", actor_id="j1",
                                             ranking_version_id=ranking_id, sign_role="review")
        self.service.countersign_ranking(request_id="sign-r", actor_id="rv1",
                                         ranking_version_id=ranking_id, sign_role="review")
        with self.assertRaises(PermissionDenied):
            self.service.countersign_ranking(request_id="sign-both", actor_id="rv1",
                                             ranking_version_id=ranking_id, sign_role="publish")
        with self.assertRaises(ConflictError):
            self.service.publish_ranking(request_id="early-publish", actor_id="pb1",
                                         ranking_version_id=ranking_id)
        self.service.countersign_ranking(request_id="sign-p", actor_id="pb1",
                                         ranking_version_id=ranking_id, sign_role="publish")
        with self.assertRaises(PermissionDenied):
            self.service.publish_ranking(request_id="wrong-publisher", actor_id="rv1",
                                         ranking_version_id=ranking_id)
        published = self.service.publish_ranking(request_id="publish", actor_id="pb1",
                                                 ranking_version_id=ranking_id)
        self.assertEqual("published", published["status"])

    def test_published_ranking_is_immutable_and_versions_supersede(self):
        published = self.published_v1()
        ranking_v1 = published["ranking_version_id"]
        self.service.add_deduction(request_id="ded-e1", actor_id="a1", stage_id="s1",
                                   entry_id="e1", points=5, reason="迟交作品", basis="规程第 3 条")
        second = self.generate(tag="g2")
        self.assertEqual(2, second["version"])
        self.assertEqual("candidate", second["status"])
        old = self.service.get_ranking(actor_id="a1", ranking_version_id=ranking_v1)
        self.assertEqual("published", old["status"])
        self.assertEqual(81.0, self.outcomes(old)["e1"]["total_score"])
        published_v2 = self.publish_flow(second["ranking_version_id"], tag="-v2")
        self.assertEqual(76.0, self.outcomes(published_v2)["e1"]["total_score"])
        old_after = self.service.get_ranking(actor_id="a1", ranking_version_id=ranking_v1)
        self.assertEqual("superseded", old_after["status"])
        self.assertEqual(81.0, self.outcomes(old_after)["e1"]["total_score"])

    def test_generate_requires_frozen_stage_and_quota(self):
        rule_id = self.make_stage()
        with self.assertRaises(ConflictError):
            self.generate()
        self.score_all()
        self.freeze(rule_id)
        self.service.create_stage(request_id="stage-s2", actor_id="a1", stage_id="s2",
                                  name="加赛", sequence=2)
        rule2 = self.service.create_rule_version(request_id="rule-s2", actor_id="a1",
                                                 stage_id="s2", config=rule_config())
        self.service.enroll_entries(request_id="enroll-s2", actor_id="op1", stage_id="s2",
                                    entry_ids=["e1"])
        self.service.assign_judge(request_id="assign-s2", actor_id="a1", stage_id="s2",
                                  track_id="t1", judge_id="j1")
        self.service.freeze_stage(request_id="freeze-s2", actor_id="a1", stage_id="s2",
                                  rule_version_id=rule2["rule_version_id"])
        with self.assertRaises(ConflictError):
            self.service.generate_ranking(request_id="gen-s2", actor_id="a1", stage_id="s2")

    def test_total_advance_constraint_is_enforced(self):
        rule_id = self.make_stage(constraint=4)
        self.score_all()
        self.freeze(rule_id)
        with self.assertRaises(ConflictError):
            self.generate()

    # ------------------------------------------------------------------
    # 并列、缺评、撤销、资格取消
    # ------------------------------------------------------------------

    def test_strict_tie_break_at_boundary(self):
        rule_id = self.make_stage(judges=("j1",), constraint=None, quotas={"t1": 1},
                                  entries=("e1", "e2"))
        self.score("j1", "e1", 90, 60)   # 78
        self.score("j1", "e2", 80, 75)   # 78，创意较低
        self.freeze(rule_id)
        candidate = self.generate()
        outcomes = self.outcomes(candidate)
        self.assertEqual(78.0, outcomes["e1"]["total_score"])
        self.assertEqual(78.0, outcomes["e2"]["total_score"])
        self.assertEqual(1, outcomes["e1"]["rank"])
        self.assertEqual(2, outcomes["e2"]["rank"])
        tie = outcomes["e2"]["explanation"]["tie_break"]
        self.assertEqual(2, tie["group_size"])
        self.assertEqual("dimension:创意", tie["decided_by"])

    def test_shared_boundary_advances_all_tied_when_constraint_allows(self):
        rule_id = self.make_stage(config=rule_config(boundary_policy="shared"),
                                  judges=("j1",), constraint=2, quotas={"t1": 1},
                                  entries=("e1", "e2"))
        self.score("j1", "e1", 90, 60)
        self.score("j1", "e2", 80, 75)
        self.freeze(rule_id)
        candidate = self.generate()
        outcomes = self.outcomes(candidate)
        self.assertEqual(1, outcomes["e1"]["rank"])
        self.assertEqual(1, outcomes["e2"]["rank"])
        self.assertEqual("advance", outcomes["e1"]["outcome"])
        self.assertEqual("advance", outcomes["e2"]["outcome"])

    def test_shared_boundary_overflow_fails_total_constraint(self):
        rule_id = self.make_stage(config=rule_config(boundary_policy="shared"),
                                  judges=("j1",), constraint=1, quotas={"t1": 1},
                                  entries=("e1", "e2"))
        self.score("j1", "e1", 90, 60)
        self.score("j1", "e2", 80, 75)
        self.freeze(rule_id)
        with self.assertRaises(ConflictError):
            self.generate()

    def test_missing_score_policies(self):
        # average：缺评取剩余评委平均
        rule_id = self.make_stage(judges=("j1", "j2"), constraint=None, quotas={"t1": 2},
                                  entries=("e1", "e2"))
        self.score("j1", "e1", 90, 90)
        self.score("j2", "e1", 90, 90)
        self.score("j1", "e2", 60, 60)
        self.freeze(rule_id)
        candidate = self.generate()
        outcomes = self.outcomes(candidate)
        self.assertEqual(60.0, outcomes["e2"]["total_score"])
        self.assertEqual(["j2"], outcomes["e2"]["explanation"]["dimensions"][0]["missing_judges"])

    def test_missing_score_zero_policy(self):
        rule_id = self.make_stage(config=rule_config(missing_score_policy="zero"),
                                  judges=("j1", "j2"), constraint=None, quotas={"t1": 2},
                                  entries=("e1", "e2"))
        self.score("j1", "e1", 90, 90)
        self.score("j2", "e1", 90, 90)
        self.score("j1", "e2", 60, 60)
        self.freeze(rule_id)
        candidate = self.generate()
        self.assertEqual(30.0, self.outcomes(candidate)["e2"]["total_score"])

    def test_missing_score_disqualify_policy(self):
        rule_id = self.make_stage(config=rule_config(missing_score_policy="disqualify"),
                                  judges=("j1", "j2"), constraint=None, quotas={"t1": 2},
                                  entries=("e1", "e2"))
        self.score("j1", "e1", 90, 90)
        self.score("j2", "e1", 90, 90)
        self.score("j1", "e2", 60, 60)
        self.freeze(rule_id)
        candidate = self.generate()
        outcomes = self.outcomes(candidate)
        self.assertEqual("excluded_missing_scores", outcomes["e2"]["outcome"])
        self.assertIsNone(outcomes["e2"]["rank"])

    def test_revoked_score_follows_frozen_rule(self):
        rule_id = self.make_stage(judges=("j1", "j2"), constraint=None, quotas={"t1": 2},
                                  entries=("e1", "e2"))
        self.score("j1", "e1", 90, 90)
        score_ids = self.score("j2", "e1", 80, 80)
        self.score("j1", "e2", 60, 60)
        self.score("j2", "e2", 60, 60)
        self.freeze(rule_id)
        self.service.revoke_score(request_id="revoke-1", actor_id="a1", score_id=score_ids[0],
                                  reason="评委声明误填")
        candidate = self.generate()
        # 撤销按缺评处理：e1 创意只剩 j1 的 90，表现仍为两评委平均 85。
        outcomes = self.outcomes(candidate)
        self.assertEqual(88.0, outcomes["e1"]["total_score"])
        judges = {item["judge_id"]: item["status"]
                  for item in outcomes["e1"]["explanation"]["dimensions"][0]["judges"]}
        self.assertEqual("revoked", judges["j2"])

    def test_disqualification_excludes_and_lift_restores(self):
        rule_id = self.make_stage(judges=("j1",), constraint=None, quotas={"t1": 1},
                                  entries=("e1", "e2"))
        self.score("j1", "e1", 90, 90)
        self.score("j1", "e2", 80, 80)
        record = self.service.disqualify_entry(request_id="disq-e1", actor_id="a1", stage_id="s1",
                                               entry_id="e1", reason="抄袭查实")
        self.freeze(rule_id)
        candidate = self.generate()
        outcomes = self.outcomes(candidate)
        self.assertEqual("disqualified", outcomes["e1"]["outcome"])
        self.assertIsNone(outcomes["e1"]["rank"])
        self.assertEqual("advance", outcomes["e2"]["outcome"])
        self.service.lift_disqualification(request_id="lift-e1", actor_id="a1",
                                           disqualification_id=record["disqualification_id"],
                                           reason="复核撤销")
        regenerated = self.generate(tag="g2")
        self.assertEqual("advance", self.outcomes(regenerated)["e1"]["outcome"])

    def test_deduction_applied_and_withdrawn(self):
        rule_id = self.make_stage(judges=("j1",), constraint=None, quotas={"t1": 2},
                                  entries=("e1", "e2"))
        self.score("j1", "e1", 90, 90)
        self.score("j1", "e2", 80, 80)
        deduction = self.service.add_deduction(request_id="ded-1", actor_id="op1", stage_id="s1",
                                               entry_id="e1", points=15, reason="迟交",
                                               basis="规程第 3 条")
        self.freeze(rule_id)
        candidate = self.generate()
        outcomes = self.outcomes(candidate)
        self.assertEqual(75.0, outcomes["e1"]["total_score"])
        self.assertEqual(15.0, outcomes["e1"]["explanation"]["deduction_total"])
        self.service.withdraw_deduction(request_id="ded-1-w", actor_id="a1",
                                        deduction_id=deduction["deduction_id"], reason="申诉成立")
        regenerated = self.generate(tag="g2")
        self.assertEqual(90.0, self.outcomes(regenerated)["e1"]["total_score"])

    def test_excluded_judge_scores_are_ignored(self):
        rule_id = self.make_stage(judges=("j1", "j2"), constraint=None, quotas={"t1": 2},
                                  entries=("e1", "e2"))
        self.score("j1", "e1", 90, 90)
        self.score("j2", "e1", 10, 10)
        self.score("j1", "e2", 60, 60)
        self.score("j2", "e2", 60, 60)
        self.freeze(rule_id)
        self.service.exclude_judge(request_id="exclude-j2", actor_id="a1", stage_id="s1",
                                   track_id="t1", judge_id="j2", reason="利益冲突")
        candidate = self.generate()
        self.assertEqual(90.0, self.outcomes(candidate)["e1"]["total_score"])

    # ------------------------------------------------------------------
    # 申诉与裁决
    # ------------------------------------------------------------------

    def test_appeal_requires_published_version_fact_and_owner(self):
        published = self.published_v1()
        ranking_id = published["ranking_version_id"]
        score_id = self.service.my_entries(actor_id="p2")[0]["scores"][0]["score_id"]
        with self.assertRaises(PermissionDenied):
            self.service.file_appeal(request_id="appeal-other", actor_id="p3",
                                     ranking_version_id=ranking_id, entry_id="e2",
                                     fact_type="score", fact_id=score_id, reason="不是我的")
        with self.assertRaises(ValidationError):
            self.service.file_appeal(request_id="appeal-bad-fact", actor_id="p2",
                                     ranking_version_id=ranking_id, entry_id="e2",
                                     fact_type="score", fact_id="missing", reason="事实不存在")
        with self.assertRaises(ValidationError):
            self.service.file_appeal(request_id="appeal-bad-type", actor_id="p2",
                                     ranking_version_id=ranking_id, entry_id="e2",
                                     fact_type="unknown", fact_id=score_id, reason="类型错误")
        appeal = self.service.file_appeal(request_id="appeal-ok", actor_id="p2",
                                          ranking_version_id=ranking_id, entry_id="e2",
                                          fact_type="score", fact_id=score_id, reason="分数有误")
        self.assertEqual("filed", appeal["status"])

    def test_appeal_window_is_enforced(self):
        published = self.published_v1()
        ranking_id = published["ranking_version_id"]
        score_id = self.service.my_entries(actor_id="p2")[0]["scores"][0]["score_id"]
        self.clock.set(datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc))
        with self.assertRaises(ValidationError):
            self.service.file_appeal(request_id="appeal-late", actor_id="p2",
                                     ranking_version_id=ranking_id, entry_id="e2",
                                     fact_type="score", fact_id=score_id, reason="超时申诉")

    def test_appeal_upheld_generates_new_version_with_delta(self):
        published = self.published_v1()
        ranking_v1 = published["ranking_version_id"]
        self.service.issue_notifications(request_id="notify-1", actor_id="op1",
                                         ranking_version_id=ranking_v1)
        scores = self.service.list_scores(actor_id="a1", stage_id="s1", entry_id="e2")
        by_key = {(item["judge_id"], item["dimension"]): item["score_id"] for item in scores}
        appeal = self.service.file_appeal(request_id="appeal-1", actor_id="p2",
                                          ranking_version_id=ranking_v1, entry_id="e2",
                                          fact_type="score", fact_id=by_key[("j1", "创意")],
                                          reason="两名评委均确认误填")
        adjudicated = self.service.adjudicate_appeal(
            request_id="adj-1", actor_id="a1", appeal_id=appeal["appeal_id"], decision="upheld",
            note="更正分数",
            corrections=[
                {"action": "correct_score", "score_id": by_key[("j1", "创意")],
                 "new_value": 100, "reason": "评委确认误填"},
                {"action": "correct_score", "score_id": by_key[("j2", "创意")],
                 "new_value": 100, "reason": "评委确认误填"},
            ])
        ranking_v2 = adjudicated["resulting_version_id"]
        self.assertEqual("appeal", adjudicated["ranking"]["trigger_type"])
        published_v2 = self.publish_flow(ranking_v2, tag="-v2")
        outcomes_v2 = self.outcomes(published_v2)
        self.assertEqual(84.0, outcomes_v2["e2"]["total_score"])
        self.assertEqual("advance", outcomes_v2["e2"]["outcome"])
        self.assertEqual("eliminate", outcomes_v2["e1"]["outcome"])
        delta = published_v2["delta"]
        self.assertEqual(1, delta["from_version"])
        self.assertEqual(2, delta["to_version"])
        changed = {item["entry_id"] for item in delta["rank_changes"]}
        self.assertEqual({"e1", "e2"}, changed)
        affected = {item["entry_id"] for item in delta["affected_notifications"]}
        self.assertEqual({"e1", "e2"}, affected)
        quota_changes = {item["track_id"] for item in delta["quota_changes"]}
        self.assertEqual({"t1"}, quota_changes)
        old = self.service.get_ranking(actor_id="a1", ranking_version_id=ranking_v1)
        self.assertEqual("superseded", old["status"])
        self.assertEqual(63.0, self.outcomes(old)["e2"]["total_score"])

    def test_appeal_rejected_keeps_ranking(self):
        published = self.published_v1()
        ranking_id = published["ranking_version_id"]
        score_id = self.service.my_entries(actor_id="p2")[0]["scores"][0]["score_id"]
        appeal = self.service.file_appeal(request_id="appeal-2", actor_id="p2",
                                          ranking_version_id=ranking_id, entry_id="e2",
                                          fact_type="score", fact_id=score_id, reason="不成立")
        result = self.service.adjudicate_appeal(request_id="adj-2", actor_id="a1",
                                                appeal_id=appeal["appeal_id"], decision="rejected",
                                                note="查无实据")
        self.assertEqual("adjudicated_rejected", result["status"])
        self.assertIsNone(result["resulting_version_id"])
        with self.assertRaises(ConflictError):
            self.service.adjudicate_appeal(request_id="adj-again", actor_id="a1",
                                           appeal_id=appeal["appeal_id"], decision="rejected",
                                           note="重复裁决")

    def test_upheld_appeal_requires_corrections(self):
        published = self.published_v1()
        score_id = self.service.my_entries(actor_id="p2")[0]["scores"][0]["score_id"]
        appeal = self.service.file_appeal(request_id="appeal-3", actor_id="p2",
                                          ranking_version_id=published["ranking_version_id"],
                                          entry_id="e2", fact_type="score", fact_id=score_id,
                                          reason="需要纠正")
        with self.assertRaises(ValidationError):
            self.service.adjudicate_appeal(request_id="adj-3", actor_id="a1",
                                           appeal_id=appeal["appeal_id"], decision="upheld",
                                           note="没有措施")

    # ------------------------------------------------------------------
    # 可见性边界
    # ------------------------------------------------------------------

    def test_participant_sees_only_own_details_and_public_results(self):
        published = self.published_v1()
        ranking_id = published["ranking_version_id"]
        mine = self.service.my_entries(actor_id="p1")
        self.assertEqual(["e1"], [item["entry_id"] for item in mine])
        self.assertEqual("advance", mine[0]["published_outcomes"][0]["outcome"])
        with self.assertRaises(PermissionDenied):
            self.service.explain_entry(actor_id="p1", ranking_version_id=ranking_id, entry_id="e2")
        own = self.service.explain_entry(actor_id="p1", ranking_version_id=ranking_id, entry_id="e1")
        self.assertEqual("advance", own["outcome"])
        public = self.service.public_results(actor_id="p1", stage_id="s1")
        self.assertNotIn("explanation", public["entries"][0])
        with self.assertRaises(NotFoundError):
            self.service.public_results(actor_id="p1", stage_id="s2")

    def test_participant_cannot_see_unpublished_candidate(self):
        rule_id = self.make_stage()
        self.score_all()
        self.freeze(rule_id)
        candidate = self.generate()
        with self.assertRaises(PermissionDenied):
            self.service.get_ranking(actor_id="p1",
                                     ranking_version_id=candidate["ranking_version_id"])
        listed = self.service.list_rankings(actor_id="p1", stage_id="s1")
        self.assertEqual([], listed)

    # ------------------------------------------------------------------
    # 重放、解释与重启续跑
    # ------------------------------------------------------------------

    def test_replay_matches_snapshot_and_detects_drift(self):
        published = self.published_v1()
        ranking_id = published["ranking_version_id"]
        replay = self.service.replay_ranking(actor_id="au1", ranking_version_id=ranking_id)
        self.assertEqual("match", replay["snapshot_replay"])
        self.assertEqual("match", replay["current_facts"])
        score_id = self.service.my_entries(actor_id="p1")[0]["scores"][0]["score_id"]
        self.service.revoke_score(request_id="revoke-drift", actor_id="a1", score_id=score_id,
                                  reason="复核发现异常")
        replay_after = self.service.replay_ranking(actor_id="au1", ranking_version_id=ranking_id)
        self.assertEqual("match", replay_after["snapshot_replay"])
        self.assertEqual("drifted", replay_after["current_facts"])
        changed = {item["entry_id"] for item in replay_after["current_facts_diff"]}
        self.assertIn("e1", changed)
        with self.assertRaises(PermissionDenied):
            self.service.replay_ranking(actor_id="p1", ranking_version_id=ranking_id)

    def test_explain_covers_advance_and_eliminate(self):
        published = self.published_v1()
        ranking_id = published["ranking_version_id"]
        advance = self.service.explain_entry(actor_id="op1", ranking_version_id=ranking_id,
                                             entry_id="e1")
        self.assertIn("晋级", advance["explanation"]["outcome_reason"])
        eliminate = self.service.explain_entry(actor_id="op1", ranking_version_id=ranking_id,
                                               entry_id="e2")
        self.assertIn("落选", eliminate["explanation"]["outcome_reason"])
        self.assertEqual(1, eliminate["explanation"]["quota"])

    def test_restart_resumes_countersign_and_appeal_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review.sqlite3"
            database = Database(path)
            clock = ManualClock(datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc))
            service = ReviewService(database, clock)
            service.register_organization(request_id="org", actor_id="bootstrap",
                                          organization_id="o1", name="赛事机构")
            service.register_actor(request_id="actor-a1", actor_id="bootstrap", new_actor_id="a1",
                                   display_name="管理员", role="admin", organization_id="o1")
            for actor_id, role in (("rv1", "reviewer"), ("pb1", "publisher"),
                                   ("j1", "judge"), ("p1", "participant")):
                service.register_actor(request_id=f"actor-{actor_id}", actor_id="a1",
                                       new_actor_id=actor_id, display_name=actor_id, role=role,
                                       organization_id="o1")
            service.create_track(request_id="track", actor_id="a1", track_id="t1", name="赛道")
            service.register_entry(request_id="entry", actor_id="a1", entry_id="e1",
                                   track_id="t1", participant_id="p1", title="作品")
            service.create_stage(request_id="stage", actor_id="a1", stage_id="s1",
                                 name="初赛", sequence=1)
            rule = service.create_rule_version(request_id="rule", actor_id="a1", stage_id="s1",
                                               config=rule_config())
            service.enroll_entries(request_id="enroll", actor_id="a1", stage_id="s1",
                                   entry_ids=["e1"])
            service.assign_judge(request_id="assign", actor_id="a1", stage_id="s1",
                                 track_id="t1", judge_id="j1")
            service.set_quota(request_id="quota", actor_id="a1", stage_id="s1", track_id="t1",
                              advance_count=1)
            service.submit_score(request_id="score", actor_id="j1", stage_id="s1", entry_id="e1",
                                 dimension="创意", value=80)
            service.submit_score(request_id="score-2", actor_id="j1", stage_id="s1", entry_id="e1",
                                 dimension="表现", value=80)
            service.freeze_stage(request_id="freeze", actor_id="a1", stage_id="s1",
                                 rule_version_id=rule["rule_version_id"])
            candidate = service.generate_ranking(request_id="gen", actor_id="a1", stage_id="s1")
            ranking_id = candidate["ranking_version_id"]
            service.countersign_ranking(request_id="sign-r", actor_id="rv1",
                                        ranking_version_id=ranking_id, sign_role="review")
            database.close()

            # 重启：会签进度保留，发布人继续会签并发布。
            database2 = Database(path)
            clock2 = ManualClock(datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc))
            service2 = ReviewService(database2, clock2)
            pending = service2.pending_tasks(actor_id="a1")
            self.assertEqual(1, len(pending["countersign_pending"]))
            self.assertTrue(pending["countersign_pending"][0]["review_signed"])
            self.assertFalse(pending["countersign_pending"][0]["publish_signed"])
            service2.countersign_ranking(request_id="sign-p", actor_id="pb1",
                                         ranking_version_id=ranking_id, sign_role="publish")
            published = service2.publish_ranking(request_id="publish", actor_id="pb1",
                                                 ranking_version_id=ranking_id)
            self.assertEqual("published", published["status"])
            windows = service2.pending_tasks(actor_id="a1")["appeal_windows"]
            self.assertTrue(windows[0]["open"])
            self.assertEqual(72 * 3600, windows[0]["remaining_seconds"])

            # 再次重启并越过申诉时限：时钟继续走，申诉被拒绝。
            database2.close()
            database3 = Database(path)
            clock3 = ManualClock(datetime(2026, 9, 6, 9, 0, tzinfo=timezone.utc))
            service3 = ReviewService(database3, clock3)
            self.assertFalse(service3.pending_tasks(actor_id="a1")["appeal_windows"][0]["open"])
            score_id = service3.my_entries(actor_id="p1")[0]["scores"][0]["score_id"]
            with self.assertRaises(ValidationError):
                service3.file_appeal(request_id="appeal-expired", actor_id="p1",
                                     ranking_version_id=ranking_id, entry_id="e1",
                                     fact_type="score", fact_id=score_id, reason="超时")
            database3.close()

    # ------------------------------------------------------------------
    # 幂等
    # ------------------------------------------------------------------

    def test_writes_are_idempotent(self):
        first = self.service.create_track(request_id="idem-track", actor_id="a1",
                                          track_id="t9", name="幂等赛道")
        second = self.service.create_track(request_id="idem-track", actor_id="a1",
                                           track_id="t9", name="幂等赛道")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        rule_id = self.make_stage()
        self.score_all()
        self.freeze(rule_id)
        one = self.service.generate_ranking(request_id="idem-gen", actor_id="a1", stage_id="s1")
        two = self.service.generate_ranking(request_id="idem-gen", actor_id="a1", stage_id="s1")
        self.assertEqual(one["ranking_version_id"], two["ranking_version_id"])
        self.assertTrue(two["replayed"])
        versions = self.service.list_rankings(actor_id="a1", stage_id="s1")
        self.assertEqual(1, len(versions))
        with self.assertRaises(ConflictError):
            self.service.create_track(request_id="idem-track", actor_id="a1",
                                      track_id="t10", name="不同内容")

    def test_audit_chain_stays_valid(self):
        self.published_v1()
        valid, count = self.service.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 0)


if __name__ == "__main__":
    unittest.main()
