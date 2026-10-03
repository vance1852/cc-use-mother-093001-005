"""晋级评议与申诉系统的离线端到端验收。

场景对应评议组遇到的实际问题：三个赛道使用过两版评分细则，一位评委
迟交的分数只影响部分作品，末位出现同分，奖项总量与各赛道晋级名额
必须同时满足。验收覆盖候选榜会签、发布、申诉裁决产生新版本、榜单
重放以及服务重启后时钟与状态的延续。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .advancement import AdvancementService
from .clock import MutableClock
from .errors import PermissionDenied
from .service import DomainService
from .storage import Database

START = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
DEADLINE = "2026-10-02T00:00:00Z"


def _setup_actors(domain: DomainService) -> None:
    domain.register_organization(request_id="acc-org", actor_id="bootstrap",
                                 organization_id="org-001", name="文化创意赛事组委会")
    domain.register_actor(request_id="acc-admin", actor_id="bootstrap", new_actor_id="admin-001",
                          display_name="系统管理员", role="admin", organization_id="org-001")
    domain.register_actor(request_id="acc-operator", actor_id="admin-001", new_actor_id="op-001",
                          display_name="赛事运营", role="operator", organization_id="org-001")
    for index in (1, 2):
        domain.register_actor(request_id=f"acc-reviewer-{index}", actor_id="admin-001",
                              new_actor_id=f"reviewer-00{index}",
                              display_name=f"复核人{index}", role="reviewer", organization_id="org-001")
    domain.register_actor(request_id="acc-publisher", actor_id="admin-001", new_actor_id="publisher-001",
                          display_name="发布人", role="publisher", organization_id="org-001")
    for index in (1, 2, 3):
        domain.register_actor(request_id=f"acc-judge-{index}", actor_id="admin-001",
                              new_actor_id=f"judge-00{index}",
                              display_name=f"评委{index}", role="judge", organization_id="org-001")
    for index in range(1, 10):
        domain.register_actor(request_id=f"acc-participant-{index}", actor_id="admin-001",
                              new_actor_id=f"participant-00{index}",
                              display_name=f"参赛人{index}", role="participant", organization_id="org-001")


def _setup_competition(service: AdvancementService) -> dict[str, str]:
    service.create_stage(request_id="acc-stage", actor_id="op-001", stage_id="stage-pre",
                         name="初赛", sequence=1, reviewer_quorum=2,
                         countersign_ttl_hours=72, appeal_window_hours=48)
    for track_id, name in (("track-a", "视觉设计"), ("track-b", "文创产品"), ("track-c", "数字媒体")):
        service.create_track(request_id=f"acc-track-{track_id}", actor_id="op-001",
                             track_id=track_id, name=name)
    rule_v1 = service.create_rule_version(
        request_id="acc-rule-v1", actor_id="op-001", stage_id="stage-pre",
        weights={"创意": 0.5, "执行": 0.5}, tie_policy="tie_break",
        tie_break_dimensions=["创意"], missing_score_policy="exclude_judge",
        late_score_policy="reject", score_deadline=DEADLINE)
    rule_v2 = service.create_rule_version(
        request_id="acc-rule-v2", actor_id="op-001", stage_id="stage-pre",
        weights={"创意": 0.5, "执行": 0.3, "表现": 0.2}, tie_policy="share",
        missing_score_policy="zero", late_score_policy="accept", pass_score=60)
    for rule in (rule_v1, rule_v2):
        service.freeze_rule_version(request_id=f"acc-freeze-{rule['rule_version_id'][:8]}",
                                    actor_id="op-001", rule_version_id=rule["rule_version_id"])
    service.assign_track_rule(request_id="acc-tr-a", actor_id="op-001", stage_id="stage-pre",
                              track_id="track-a", rule_version_id=rule_v1["rule_version_id"])
    service.assign_track_rule(request_id="acc-tr-b", actor_id="op-001", stage_id="stage-pre",
                              track_id="track-b", rule_version_id=rule_v1["rule_version_id"])
    service.assign_track_rule(request_id="acc-tr-c", actor_id="op-001", stage_id="stage-pre",
                              track_id="track-c", rule_version_id=rule_v2["rule_version_id"])
    for track_id, quota in (("track-a", 2), ("track-b", 2), ("track-c", 1)):
        service.set_track_quota(request_id=f"acc-quota-{track_id}", actor_id="op-001",
                                stage_id="stage-pre", track_id=track_id, quota=quota)
    service.set_award_constraint(request_id="acc-award", actor_id="op-001", stage_id="stage-pre",
                                 total_awards=3, per_track_cap=2)
    for track_id, judges in (("track-a", ("judge-001", "judge-002")),
                             ("track-b", ("judge-001", "judge-003")),
                             ("track-c", ("judge-002", "judge-003"))):
        for judge_id in judges:
            service.assign_judge(request_id=f"acc-judge-{track_id}-{judge_id}", actor_id="op-001",
                                 stage_id="stage-pre", track_id=track_id, judge_id=judge_id)
    entries = (
        ("entry-01", "track-a", "participant-001", "山海海报"),
        ("entry-02", "track-a", "participant-002", "古城插画"),
        ("entry-03", "track-a", "participant-003", "民俗纹样"),
        ("entry-04", "track-b", "participant-004", "榫卯文具"),
        ("entry-05", "track-b", "participant-005", "漆器茶礼"),
        ("entry-06", "track-b", "participant-006", "竹编灯具"),
        ("entry-07", "track-c", "participant-007", "沉浸戏曲"),
        ("entry-08", "track-c", "participant-008", "数字壁画"),
        ("entry-09", "track-c", "participant-009", "虚拟展厅"),
    )
    for entry_id, track_id, participant_id, title in entries:
        service.register_entry(request_id=f"acc-entry-{entry_id}", actor_id="op-001",
                               entry_id=entry_id, stage_id="stage-pre", track_id=track_id,
                               participant_id=participant_id, title=title)
    service.disqualify_entry(request_id="acc-dq", actor_id="op-001",
                             entry_id="entry-09", reason="报名材料造假")
    return {"rule_v1": rule_v1["rule_version_id"], "rule_v2": rule_v2["rule_version_id"]}


def _submit_scores(service: AdvancementService, clock: MutableClock) -> None:
    # 截止前的评分：track-a 末位同分，entry-01 带一笔有依据的扣分
    service.submit_score(request_id="acc-s-01a", actor_id="judge-001", entry_id="entry-01",
                         scores={"创意": 90, "执行": 80})
    service.submit_score(request_id="acc-s-01b", actor_id="judge-002", entry_id="entry-01",
                         scores={"创意": 80, "执行": 80}, deduction=5,
                         deduction_basis="迟交作品材料")
    service.submit_score(request_id="acc-s-02a", actor_id="judge-001", entry_id="entry-02",
                         scores={"创意": 80, "执行": 90})
    service.submit_score(request_id="acc-s-02b", actor_id="judge-002", entry_id="entry-02",
                         scores={"创意": 80, "执行": 80})
    service.submit_score(request_id="acc-s-03a", actor_id="judge-001", entry_id="entry-03",
                         scores={"创意": 70, "执行": 70})
    service.submit_score(request_id="acc-s-03b", actor_id="judge-002", entry_id="entry-03",
                         scores={"创意": 60, "执行": 80})
    service.submit_score(request_id="acc-s-04a", actor_id="judge-001", entry_id="entry-04",
                         scores={"创意": 88, "执行": 88})
    service.submit_score(request_id="acc-s-05a", actor_id="judge-001", entry_id="entry-05",
                         scores={"创意": 80, "执行": 80})
    service.submit_score(request_id="acc-s-05b", actor_id="judge-003", entry_id="entry-05",
                         scores={"创意": 80, "执行": 80})
    service.submit_score(request_id="acc-s-06a", actor_id="judge-001", entry_id="entry-06",
                         scores={"创意": 60, "执行": 60})
    service.submit_score(request_id="acc-s-07a", actor_id="judge-002", entry_id="entry-07",
                         scores={"创意": 90, "执行": 90, "表现": 90})
    service.submit_score(request_id="acc-s-07b", actor_id="judge-003", entry_id="entry-07",
                         scores={"创意": 80, "执行": 80, "表现": 80})
    service.submit_score(request_id="acc-s-08a", actor_id="judge-002", entry_id="entry-08",
                         scores={"创意": 70, "执行": 70, "表现": 70})
    # judge-003 在截止后迟交：只影响 track-b 的 entry-04（规则拒绝迟交）
    clock.advance(hours=20)
    service.submit_score(request_id="acc-s-04b", actor_id="judge-003", entry_id="entry-04",
                         scores={"创意": 90, "执行": 90})


def _countersign_and_publish(service: AdvancementService, run_id: str, tag: str) -> dict:
    service.countersign(request_id=f"acc-sign-{tag}-1", actor_id="reviewer-001",
                        run_id=run_id, decision="approved")
    service.countersign(request_id=f"acc-sign-{tag}-2", actor_id="reviewer-002",
                        run_id=run_id, decision="approved")
    return service.publish_run(request_id=f"acc-publish-{tag}", actor_id="publisher-001",
                               run_id=run_id)


def run() -> dict[str, object]:
    """执行完整评议与申诉链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "advancement.sqlite3"
        clock = MutableClock(START)
        database = Database(path)
        domain = DomainService(database, clock)
        service = AdvancementService(database, clock, domain=domain)

        _setup_actors(domain)
        _setup_competition(service)
        _submit_scores(service, clock)

        # 评委在封存前看不到其他人的分数
        visible = service.list_scores(actor_id="judge-001", stage_id="stage-pre")
        assert visible and all(item["judge_id"] == "judge-001" for item in visible)

        # 生成候选榜：两版规则分别作用于不同赛道
        candidate = service.compute_run(request_id="acc-run-1", actor_id="op-001",
                                        stage_id="stage-pre")
        run_v1 = candidate["run_id"]
        detail = service.get_run_detail(actor_id="op-001", run_id=run_v1)
        items = {item["entry_id"]: item for item in detail["items"]}

        # 迟交评分被规则拒绝，entry-04 只按 judge-001 一人计分
        assert items["entry-04"]["total_score"] == 88.0
        assert items["entry-04"]["explanation"]["excluded_judges"][0]["reason"] == "late"
        # 末位同分按冻结规则的决胜维度排位：entry-01 与 entry-02 原本同为 82.5
        assert items["entry-02"]["total_score"] == 82.5
        assert items["entry-01"]["total_score"] == 80.0  # 含 5 分扣分
        assert items["entry-01"]["rank"] == 2 and items["entry-02"]["rank"] == 1
        # 缺评按规则处理：track-b 剔除缺评评委，track-c 以 0 分计入
        assert items["entry-06"]["total_score"] == 60.0
        assert items["entry-08"]["total_score"] == 35.0
        assert items["entry-08"]["advanced"] is False  # 低于及格线
        # 资格取消不参与排名
        assert items["entry-09"]["rank"] is None
        # 奖项总量约束：晋级者中只发 3 个奖
        awarded = sorted(item["entry_id"] for item in detail["items"] if item["awarded"])
        assert awarded == ["entry-02", "entry-04", "entry-07"], awarded

        # 参赛人看不到候选榜，发布人独立完成发布
        try:
            service.get_run_detail(actor_id="participant-001", run_id=run_v1)
            raise AssertionError("参赛人不应看到候选榜")
        except PermissionDenied:
            pass
        published_v1 = _countersign_and_publish(service, run_v1, "v1")
        assert published_v1["version"] == 1

        # 参赛人申诉自己作品的扣分事实，裁决成立后产生新版本
        target_score = next(item for item in service.list_scores(
            actor_id="participant-001", stage_id="stage-pre", entry_id="entry-01")
            if item["judge_id"] == "judge-002")
        appeal = service.file_appeal(request_id="acc-appeal-1", actor_id="participant-001",
                                     run_id=run_v1, entry_id="entry-01", target_type="score",
                                     target_id=target_score["score_id"],
                                     reason="扣分依据与事实不符，材料按时提交")
        adjudicated = service.adjudicate_appeal(
            request_id="acc-adjudicate-1", actor_id="admin-001",
            appeal_id=appeal["appeal_id"], decision="upheld",
            note="经查证扣分依据不成立", remedy="adjust_deduction",
            deduction=0, deduction_basis=None)
        run_v2 = adjudicated["result_run_id"]
        detail_v2 = service.get_run_detail(actor_id="op-001", run_id=run_v2)
        summary = detail_v2["change_summary"]
        assert summary["compared_to_run_id"] == run_v1
        assert {change["entry_id"] for change in summary["rank_changes"]} == {"entry-01", "entry-02"}
        assert set(summary["affected_notifications"]) == {"entry-01", "entry-02"}
        published_v2 = _countersign_and_publish(service, run_v2, "v2")
        assert published_v2["version"] == 2

        # 发布后不可原地改写：旧版本保持原样，公开结果只显示新版本
        public = service.public_results(stage_id="stage-pre")
        assert public["version"] == 2
        items_v2 = {item["entry_id"]: item for item in public["items"]}
        assert items_v2["entry-01"]["rank"] == 1 and items_v2["entry-01"]["awarded"] is True
        old = service.get_run_detail(actor_id="op-001", run_id=run_v1)
        assert old["status"] == "superseded"
        assert old["items"][1]["total_score"] == 80.0  # entry-01 在旧版本中不变

        # 服务重启：状态、会签与申诉时钟从 SQLite 继续
        database.close()
        database = Database(path)
        domain = DomainService(database, clock)
        service = AdvancementService(database, clock, domain=domain)
        replay_v1 = service.replay_run(actor_id="admin-001", run_id=run_v1)
        replay_v2 = service.replay_run(actor_id="admin-001", run_id=run_v2)
        assert replay_v1["match"] and replay_v2["match"]
        clock.advance(hours=24 * 30)
        swept = service.tick(actor_id="op-001")
        assert swept["expired_appeals"] == 0  # 唯一申诉已裁决，无悬挂时钟
        valid, event_count = domain.verify_audit()
        result = {
            "status": "ok",
            "audit_events": event_count,
            "audit_valid": valid,
            "published_versions": [published_v1["version"], published_v2["version"]],
            "awarded_v1": awarded,
            "rank_changes_v2": len(summary["rank_changes"]),
            "replay_match": replay_v1["match"] and replay_v2["match"],
            "restart_continued": True,
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] and result["replay_match"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
