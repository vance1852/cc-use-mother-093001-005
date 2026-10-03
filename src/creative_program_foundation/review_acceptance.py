"""运行晋级评议与申诉模块的离线端到端验收。

场景对应初赛发布暴露的问题：三个赛道、两版评分细则、一位评委迟交、
末位同分、名额与奖项总量约束、会签发布、申诉裁决产生新版本、
榜单重放以及服务重启后的会签与申诉时钟续跑。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import parse_instant
from .review_service import ReviewService
from .storage import Database


class ManualClock:
    """可以推进的测试时钟。"""

    def __init__(self, value: datetime) -> None:
        self._value = value

    def now(self) -> datetime:
        return self._value

    def set(self, value: datetime) -> None:
        self._value = value


def _rule_config(**overrides) -> dict:
    config = {
        "dimensions": [{"name": "创意", "weight": 0.6}, {"name": "表现", "weight": 0.4}],
        "score_due_at": "2026-09-10T00:00:00Z",
        "late_score_policy": "reject",
        "missing_score_policy": "average",
        "revoked_score_policy": "treat_as_missing",
        "boundary_policy": "strict",
        "appeal_window_seconds": 72 * 3600,
    }
    config.update(overrides)
    return config


def _bootstrap(service: ReviewService) -> None:
    service.register_organization(request_id="acc-org", actor_id="bootstrap",
                                  organization_id="org-001", name="文化创意赛事机构")
    service.register_actor(request_id="acc-a-admin", actor_id="bootstrap", new_actor_id="admin-001",
                           display_name="管理员", role="admin", organization_id="org-001")
    actors = [
        ("acc-a-operator", "operator-001", "运营", "operator"),
        ("acc-a-reviewer", "reviewer-001", "复核人", "reviewer"),
        ("acc-a-publisher", "publisher-001", "发布人", "publisher"),
        ("acc-a-j1", "judge-001", "评委一", "judge"),
        ("acc-a-j2", "judge-002", "评委二", "judge"),
        ("acc-a-j3", "judge-003", "迟交评委", "judge"),
    ]
    for request_id, actor_id, name, role in actors:
        service.register_actor(request_id=request_id, actor_id="admin-001", new_actor_id=actor_id,
                               display_name=name, role=role, organization_id="org-001")
    for index in range(1, 7):
        service.register_actor(request_id=f"acc-a-p{index}", actor_id="admin-001",
                               new_actor_id=f"participant-00{index}",
                               display_name=f"参赛人{index}", role="participant",
                               organization_id="org-001")


def _build_stage(service: ReviewService, clock: ManualClock) -> str:
    for track_id, name in (("track-a", "视觉创意"), ("track-b", "数字叙事"), ("track-c", "沉浸体验")):
        service.create_track(request_id=f"acc-track-{track_id}", actor_id="admin-001",
                             track_id=track_id, name=name)
    service.create_stage(request_id="acc-stage", actor_id="admin-001",
                         stage_id="stage-1", name="初赛", sequence=1)
    # 两版评分细则：v1 接受迟交，v2 拒绝迟交；冻结时采用 v2。
    service.create_rule_version(request_id="acc-rule-v1", actor_id="admin-001",
                                stage_id="stage-1", config=_rule_config(late_score_policy="accept"))
    rule_v2 = service.create_rule_version(request_id="acc-rule-v2", actor_id="admin-001",
                                          stage_id="stage-1", config=_rule_config())
    for index, track_id in enumerate(("track-a", "track-b", "track-c"), start=1):
        for offset in (0, 1):
            number = 2 * (index - 1) + offset + 1
            service.register_entry(request_id=f"acc-entry-{number}", actor_id="operator-001",
                                   entry_id=f"entry-{number:03d}", track_id=track_id,
                                   participant_id=f"participant-00{number}",
                                   title=f"作品{number}")
    service.enroll_entries(request_id="acc-enroll", actor_id="operator-001", stage_id="stage-1",
                           entry_ids=[f"entry-{number:03d}" for number in range(1, 7)])
    for track_id in ("track-a", "track-b", "track-c"):
        for judge_id in ("judge-001", "judge-002"):
            service.assign_judge(request_id=f"acc-assign-{track_id}-{judge_id}", actor_id="admin-001",
                                 stage_id="stage-1", track_id=track_id, judge_id=judge_id)
    service.assign_judge(request_id="acc-assign-track-c-j3", actor_id="admin-001",
                         stage_id="stage-1", track_id="track-c", judge_id="judge-003")
    for track_id in ("track-a", "track-b", "track-c"):
        service.set_quota(request_id=f"acc-quota-{track_id}", actor_id="admin-001",
                          stage_id="stage-1", track_id=track_id, advance_count=1)
    service.add_award_constraint(request_id="acc-constraint", actor_id="admin-001",
                                 stage_id="stage-1", kind="total_advance_exact", params={"value": 3})

    def score(request_id: str, judge: str, entry: str, creativity: float, presentation: float) -> None:
        service.submit_score(request_id=f"{request_id}-c", actor_id=judge, stage_id="stage-1",
                             entry_id=entry, dimension="创意", value=creativity)
        service.submit_score(request_id=f"{request_id}-p", actor_id=judge, stage_id="stage-1",
                             entry_id=entry, dimension="表现", value=presentation)

    score("acc-s1", "judge-001", "entry-001", 90, 80)
    score("acc-s2", "judge-002", "entry-001", 80, 70)
    score("acc-s3", "judge-001", "entry-002", 70, 60)
    score("acc-s4", "judge-002", "entry-002", 60, 60)
    score("acc-s5", "judge-001", "entry-003", 80, 80)
    score("acc-s6", "judge-002", "entry-003", 80, 80)
    score("acc-s7", "judge-001", "entry-004", 70, 70)
    score("acc-s8", "judge-002", "entry-004", 70, 70)
    # 末位同分：entry-005 与 entry-006 在两名评委下总分相同，靠决胜维度分出先后。
    score("acc-s9", "judge-001", "entry-005", 90, 60)
    score("acc-s10", "judge-002", "entry-005", 90, 60)
    score("acc-s11", "judge-001", "entry-006", 80, 75)
    score("acc-s12", "judge-002", "entry-006", 80, 75)
    # 迟交评委：在截止之后才对 track-c 两部作品打分。
    clock.set(datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc))
    late_scores = {}
    for entry, creativity, presentation in (("entry-005", 30, 30), ("entry-006", 95, 95)):
        late_scores[entry] = [
            service.submit_score(request_id=f"acc-late-{entry}-c", actor_id="judge-003",
                                 stage_id="stage-1", entry_id=entry, dimension="创意",
                                 value=creativity)["score_id"],
            service.submit_score(request_id=f"acc-late-{entry}-p", actor_id="judge-003",
                                 stage_id="stage-1", entry_id=entry, dimension="表现",
                                 value=presentation)["score_id"],
        ]
    clock.set(datetime(2026, 9, 12, 9, 0, tzinfo=timezone.utc))
    frozen = service.freeze_stage(request_id="acc-freeze", actor_id="admin-001",
                                  stage_id="stage-1", rule_version_id=rule_v2["rule_version_id"])
    assert frozen["rejected_late_scores"] == 4, frozen
    return late_scores


def _countersign_and_publish(service: ReviewService, request_prefix: str,
                             ranking_version_id: str) -> dict:
    service.countersign_ranking(request_id=f"{request_prefix}-sign-r", actor_id="reviewer-001",
                                ranking_version_id=ranking_version_id, sign_role="review")
    service.countersign_ranking(request_id=f"{request_prefix}-sign-p", actor_id="publisher-001",
                                ranking_version_id=ranking_version_id, sign_role="publish")
    return service.publish_ranking(request_id=f"{request_prefix}-publish", actor_id="publisher-001",
                                   ranking_version_id=ranking_version_id)


def run() -> dict[str, object]:
    """执行完整评议与申诉链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "review.sqlite3"
        clock = ManualClock(datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc))
        database = Database(path)
        service = ReviewService(database, clock)
        _bootstrap(service)
        late_scores = _build_stage(service, clock)

        candidate = service.generate_ranking(request_id="acc-generate-1", actor_id="admin-001",
                                             stage_id="stage-1")
        ranking_v1 = candidate["ranking_version_id"]
        outcomes_v1 = {item["entry_id"]: item for item in candidate["entries"]}
        # 迟交被拒后 track-c 两部作品总分同为 78，末位同分按冻结规则的决胜维度分出先后。
        assert outcomes_v1["entry-005"]["outcome"] == "advance", outcomes_v1["entry-005"]
        assert outcomes_v1["entry-006"]["outcome"] == "eliminate", outcomes_v1["entry-006"]
        assert outcomes_v1["entry-001"]["outcome"] == "advance"
        tie = outcomes_v1["entry-006"]["explanation"]["tie_break"]
        assert tie["group_size"] == 2 and tie["decided_by"] == "dimension:创意", tie

        published_v1 = _countersign_and_publish(service, "acc-v1", ranking_v1)
        assert published_v1["status"] == "published"
        notifications = service.issue_notifications(request_id="acc-notify-1", actor_id="operator-001",
                                                    ranking_version_id=ranking_v1)
        assert len(notifications["notifications"]) == 6

        replay_v1 = service.replay_ranking(actor_id="admin-001", ranking_version_id=ranking_v1)
        assert replay_v1["snapshot_replay"] == "match" and replay_v1["current_facts"] == "match"

        # 参赛人只能看到公开结果与自己的明细。
        public = service.public_results(actor_id="participant-006", stage_id="stage-1")
        assert "explanation" not in public["entries"][0]
        mine = service.my_entries(actor_id="participant-006")
        assert len(mine) == 1 and mine[0]["entry_id"] == "entry-006"

        # 申诉：参赛人 6 引用迟交被拒的具体评分事实。
        appeal = service.file_appeal(request_id="acc-appeal", actor_id="participant-006",
                                     ranking_version_id=ranking_v1, entry_id="entry-006",
                                     fact_type="score", fact_id=late_scores["entry-006"][0],
                                     reason="评委迟交分数被规则 v2 拒绝，申请按裁决采纳")
        corrections = [
            {"action": "accept_late_score", "score_id": score_id, "reason": "评议会确认迟交有正当理由"}
            for score_id in (late_scores["entry-005"] + late_scores["entry-006"])
        ]
        adjudicated = service.adjudicate_appeal(request_id="acc-adjudicate", actor_id="admin-001",
                                                appeal_id=appeal["appeal_id"], decision="upheld",
                                                note="采纳迟交分数并重新计算", corrections=corrections)
        ranking_v2 = adjudicated["resulting_version_id"]
        assert adjudicated["ranking"]["trigger_type"] == "appeal"
        published_v2 = _countersign_and_publish(service, "acc-v2", ranking_v2)
        delta = published_v2["delta"]
        changed = {item["entry_id"] for item in delta["rank_changes"]}
        assert changed == {"entry-005", "entry-006"}, delta
        assert len(delta["affected_notifications"]) == 2
        outcomes_v2 = {item["entry_id"]: item for item in published_v2["entries"]}
        assert outcomes_v2["entry-006"]["outcome"] == "advance"
        assert outcomes_v2["entry-005"]["outcome"] == "eliminate"

        # 旧版本保持原样，可以重放核对；当前事实已与之一致地漂移。
        replay_old = service.replay_ranking(actor_id="admin-001", ranking_version_id=ranking_v1)
        assert replay_old["snapshot_replay"] == "match" and replay_old["current_facts"] == "drifted"
        replay_new = service.replay_ranking(actor_id="admin-001", ranking_version_id=ranking_v2)
        assert replay_new["snapshot_replay"] == "match" and replay_new["current_facts"] == "match"

        explanation = service.explain_entry(actor_id="admin-001", ranking_version_id=ranking_v2,
                                            entry_id="entry-006")
        assert explanation["explanation"]["outcome"] == "advance"

        database.close()

        # 服务重启：未完成的会签与申诉时钟从 SQLite 继续。
        database2 = Database(path)
        later = ManualClock(datetime(2026, 9, 13, 9, 0, tzinfo=timezone.utc))
        service2 = ReviewService(database2, later)
        pending = service2.pending_tasks(actor_id="admin-001")
        assert pending["appeal_windows"] and pending["appeal_windows"][0]["open"]
        remaining = pending["appeal_windows"][0]["remaining_seconds"]
        expected = int((parse_instant(published_v2["appeal_deadline"])
                        - later.now()).total_seconds())
        assert remaining == expected
        later.set(later.now() + timedelta(seconds=remaining + 1))
        expired = service2.pending_tasks(actor_id="admin-001")
        assert not expired["appeal_windows"][0]["open"]
        try:
            service2.file_appeal(request_id="acc-appeal-late", actor_id="participant-005",
                                 ranking_version_id=ranking_v2, entry_id="entry-005",
                                 fact_type="score", fact_id=late_scores["entry-005"][0],
                                 reason="超过时限的申诉")
            raise AssertionError("超过申诉时限不应受理")
        except Exception as exc:
            assert "时限" in str(exc)
        valid, event_count = service2.verify_audit()
        database2.close()
        return {"status": "ok", "audit_events": event_count, "audit_valid": valid,
                "versions": 2, "delta_rank_changes": len(delta["rank_changes"]),
                "affected_notifications": len(delta["affected_notifications"])}


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
