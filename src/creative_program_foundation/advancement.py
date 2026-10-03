"""晋级评议与申诉系统：规则版本、评分事实、候选榜会签、发布与申诉。

设计要点：
- 每个阶段的规则版本、维度权重、有效评委集合、评分事实、扣分依据、
  赛道配额和跨赛道奖项约束全部落库，计算时生成输入快照并随榜单保存；
- 榜单先生成候选状态，由复核人会签达到法定人数后，再由相互独立的
  发布人发布；发布后的榜单不可原地改写，只能被新版本取代；
- 并列、缺评、评分撤销与资格取消按计算时冻结的规则版本处理；
- 申诉必须在时限内引用具体评分或资格事实，裁决成立后以新版本说明
  受影响的名次、名额与后续通知；
- 会签截止与申诉时限持久化在数据库中，服务重启后由惰性时钟推进继续生效。
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta, timezone
from typing import Any, Callable

from .advancement_engine import (build_change_summary, compute_ranking,
                                 output_projection, parse_instant)
from .advancement_schema import ensure_advancement_schema
from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .service import DomainService
from .storage import Database

TIE_POLICIES = frozenset({"tie_break", "share"})
MISSING_SCORE_POLICIES = frozenset({"exclude_judge", "zero"})
LATE_SCORE_POLICIES = frozenset({"accept", "reject"})
PRIVILEGED_ROLES = frozenset({"admin", "operator", "reviewer", "publisher", "auditor"})
SCORE_MIN = 0.0
SCORE_MAX = 100.0


class AdvancementService:
    """协调晋级评议的规则、评分、会签、发布与申诉。"""

    def __init__(self, database: Database, clock: Clock | None = None,
                 domain: DomainService | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        self.domain = domain or DomainService(database, self.clock)
        ensure_advancement_schema(database.connection)

    # ---------- 基础工具 ----------

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _after_hours(self, hours: int) -> str:
        moment = self.clock.now() + timedelta(hours=hours)
        return moment.isoformat().replace("+00:00", "Z")

    def _actor(self, connection, actor_id: str):
        return self.domain._actor(connection, actor_id)

    def _optional_actor(self, connection, actor_id: str):
        if not actor_id:
            return None
        return self._actor(connection, actor_id)

    def _require(self, actor, *roles: str) -> None:
        self.domain._require(actor, *roles)

    def _receipt(self, connection, *, request_id: str, action: str,
                 payload: dict[str, Any],
                 create: Callable[[], tuple[str, str, dict[str, Any]]]) -> dict[str, Any]:
        request_id = self.domain._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            stored = json.loads(row["response_json"])
            return {"request_id": request_id, "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"], "replayed": True, **stored}
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()))
        return {"request_id": request_id, "resource_type": resource_type,
                "resource_id": resource_id, "replayed": False, **response}

    def _positive_int(self, value: Any, field: str, allow_zero: bool = False) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{field} 必须是整数")
        if allow_zero:
            if value < 0:
                raise ValidationError(f"{field} 不能为负数")
        elif value < 1:
            raise ValidationError(f"{field} 必须为正整数")
        return value

    def _weights(self, value: Any) -> dict[str, float]:
        if not isinstance(value, dict) or not value:
            raise ValidationError("weights 必须是非空对象")
        weights: dict[str, float] = {}
        for dimension, weight in value.items():
            dimension = str(dimension).strip()
            if not dimension or len(dimension) > 40:
                raise ValidationError("维度名称不能为空且不能超过 40 个字符")
            if isinstance(weight, bool) or not isinstance(weight, (int, float)) or float(weight) <= 0:
                raise ValidationError("维度权重必须是正数")
            weights[dimension] = float(weight)
        return weights

    def _scores_map(self, value: Any, weights: dict[str, float]) -> dict[str, float]:
        if not isinstance(value, dict) or not value:
            raise ValidationError("scores 必须是非空对象")
        if set(value) != set(weights):
            raise ValidationError("scores 维度必须与规则权重一致")
        scores: dict[str, float] = {}
        for dimension, score in value.items():
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise ValidationError("评分必须是数字")
            if not SCORE_MIN <= float(score) <= SCORE_MAX:
                raise ValidationError(f"评分必须在 {SCORE_MIN} 到 {SCORE_MAX} 之间")
            scores[str(dimension)] = float(score)
        return scores

    def _deduction(self, deduction: Any, basis: Any) -> tuple[float, str | None]:
        if isinstance(deduction, bool) or not isinstance(deduction, (int, float)) or float(deduction) < 0:
            raise ValidationError("deduction 必须是非负数字")
        deduction = float(deduction)
        basis = str(basis).strip() if basis else ""
        if deduction > 0 and not basis:
            raise ValidationError("扣分必须填写扣分依据 deduction_basis")
        if len(basis) > 200:
            raise ValidationError("deduction_basis 不能超过 200 个字符")
        return deduction, basis or None

    def _deadline(self, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        try:
            parsed = parse_instant(text)
        except ValueError:
            raise ValidationError("score_deadline 必须是 ISO 时间") from None
        if parsed.tzinfo is None:
            raise ValidationError("score_deadline 必须包含时区")
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    # ---------- 行读取 ----------

    def _stage_row(self, connection, stage_id: str):
        row = connection.execute(
            "SELECT * FROM advancement_stages WHERE stage_id=?", (stage_id,)).fetchone()
        if row is None:
            raise NotFoundError("阶段不存在")
        return row

    def _track_row(self, connection, track_id: str):
        row = connection.execute(
            "SELECT * FROM advancement_tracks WHERE track_id=?", (track_id,)).fetchone()
        if row is None:
            raise NotFoundError("赛道不存在")
        return row

    def _rule_row(self, connection, rule_version_id: str):
        row = connection.execute(
            "SELECT * FROM advancement_rule_versions WHERE rule_version_id=?", (rule_version_id,)).fetchone()
        if row is None:
            raise NotFoundError("规则版本不存在")
        return row

    def _entry_row(self, connection, entry_id: str):
        row = connection.execute(
            "SELECT * FROM advancement_entries WHERE entry_id=?", (entry_id,)).fetchone()
        if row is None:
            raise NotFoundError("作品不存在")
        return row

    def _score_row(self, connection, score_id: str):
        row = connection.execute(
            "SELECT * FROM advancement_scores WHERE score_id=?", (score_id,)).fetchone()
        if row is None:
            raise NotFoundError("评分不存在")
        return row

    def _run_row(self, connection, run_id: str):
        row = connection.execute(
            "SELECT * FROM advancement_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise NotFoundError("榜单不存在")
        return row

    def _appeal_row(self, connection, appeal_id: str):
        row = connection.execute(
            "SELECT * FROM advancement_appeals WHERE appeal_id=?", (appeal_id,)).fetchone()
        if row is None:
            raise NotFoundError("申诉不存在")
        return row

    # ---------- 阶段与规则配置 ----------

    def create_stage(self, *, request_id: str, actor_id: str, stage_id: str, name: str,
                     sequence: int, reviewer_quorum: int = 1, countersign_ttl_hours: int = 72,
                     appeal_window_hours: int = 72) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "stage_id": stage_id, "name": name, "sequence": sequence,
                   "reviewer_quorum": reviewer_quorum, "countersign_ttl_hours": countersign_ttl_hours,
                   "appeal_window_hours": appeal_window_hours}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            stage_id = self.domain._identifier(stage_id, "stage_id")
            name = self.domain._text(name, "name")
            sequence = self._positive_int(sequence, "sequence")
            reviewer_quorum = self._positive_int(reviewer_quorum, "reviewer_quorum")
            countersign_ttl_hours = self._positive_int(countersign_ttl_hours, "countersign_ttl_hours")
            appeal_window_hours = self._positive_int(appeal_window_hours, "appeal_window_hours")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO advancement_stages(stage_id,name,sequence,reviewer_quorum,"
                        "countersign_ttl_hours,appeal_window_hours,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (stage_id, name, sequence, reviewer_quorum, countersign_ttl_hours,
                         appeal_window_hours, actor_id, self._now()))
                except Exception as exc:
                    raise ConflictError("阶段编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="advancement.stage.created",
                             resource_type="advancement_stage", resource_id=stage_id,
                             detail={"name": name, "sequence": sequence,
                                     "reviewer_quorum": reviewer_quorum},
                             occurred_at=self._now())
                return "advancement_stage", stage_id, {"stage_id": stage_id}

            return self._receipt(connection, request_id=request_id,
                                 action="advancement.stage.create", payload=payload, create=create)

    def create_track(self, *, request_id: str, actor_id: str, track_id: str, name: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "track_id": track_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            track_id = self.domain._identifier(track_id, "track_id")
            name = self.domain._text(name, "name")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO advancement_tracks(track_id,name,created_by,created_at) VALUES(?,?,?,?)",
                        (track_id, name, actor_id, self._now()))
                except Exception as exc:
                    raise ConflictError("赛道编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="advancement.track.created",
                             resource_type="advancement_track", resource_id=track_id,
                             detail={"name": name}, occurred_at=self._now())
                return "advancement_track", track_id, {"track_id": track_id}

            return self._receipt(connection, request_id=request_id,
                                 action="advancement.track.create", payload=payload, create=create)

    def create_rule_version(self, *, request_id: str, actor_id: str, stage_id: str,
                            weights: dict[str, float], tie_policy: str, missing_score_policy: str,
                            late_score_policy: str, tie_break_dimensions: list[str] | None = None,
                            score_deadline: str | None = None,
                            pass_score: float | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "stage_id": stage_id, "weights": weights,
                   "tie_policy": tie_policy, "missing_score_policy": missing_score_policy,
                   "late_score_policy": late_score_policy, "tie_break_dimensions": tie_break_dimensions,
                   "score_deadline": score_deadline, "pass_score": pass_score}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._stage_row(connection, stage_id)
            weights = self._weights(weights)
            if tie_policy not in TIE_POLICIES:
                raise ValidationError("tie_policy 不在允许范围内")
            if missing_score_policy not in MISSING_SCORE_POLICIES:
                raise ValidationError("missing_score_policy 不在允许范围内")
            if late_score_policy not in LATE_SCORE_POLICIES:
                raise ValidationError("late_score_policy 不在允许范围内")
            tie_break_dimensions = tie_break_dimensions or []
            if not isinstance(tie_break_dimensions, list) or \
                    any(not isinstance(dimension, str) for dimension in tie_break_dimensions):
                raise ValidationError("tie_break_dimensions 必须是字符串数组")
            unknown = [dimension for dimension in tie_break_dimensions if dimension not in weights]
            if unknown:
                raise ValidationError("tie_break_dimensions 必须属于评分维度")
            score_deadline = self._deadline(score_deadline)
            if pass_score is not None:
                if isinstance(pass_score, bool) or not isinstance(pass_score, (int, float)) \
                        or not SCORE_MIN <= float(pass_score) <= SCORE_MAX:
                    raise ValidationError(f"pass_score 必须在 {SCORE_MIN} 到 {SCORE_MAX} 之间")
                pass_score = float(pass_score)

            def create() -> tuple[str, str, dict[str, Any]]:
                version = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM advancement_rule_versions "
                    "WHERE stage_id=?", (stage_id,)).fetchone()["next_version"]
                rule_version_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO advancement_rule_versions(rule_version_id,stage_id,version,status,"
                    "weights_json,tie_policy,tie_break_json,missing_score_policy,late_score_policy,"
                    "score_deadline,pass_score,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rule_version_id, stage_id, version, "draft", canonical_json(weights), tie_policy,
                     canonical_json(tie_break_dimensions), missing_score_policy, late_score_policy,
                     score_deadline, pass_score, actor_id, self._now()))
                append_event(connection, actor_id=actor_id, action="advancement.rule_version.created",
                             resource_type="advancement_rule_version", resource_id=rule_version_id,
                             detail={"stage_id": stage_id, "version": version, "weights": weights,
                                     "tie_policy": tie_policy, "missing_score_policy": missing_score_policy,
                                     "late_score_policy": late_score_policy},
                             occurred_at=self._now())
                return "advancement_rule_version", rule_version_id, {
                    "rule_version_id": rule_version_id, "version": version, "status": "draft"}

            return self._receipt(connection, request_id=request_id,
                                 action="advancement.rule_version.create", payload=payload, create=create)

    def freeze_rule_version(self, *, request_id: str, actor_id: str,
                            rule_version_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "rule_version_id": rule_version_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            rule = self._rule_row(connection, rule_version_id)
            if rule["status"] == "frozen":
                raise ConflictError("规则版本已冻结")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE advancement_rule_versions SET status='frozen', frozen_at=? "
                    "WHERE rule_version_id=?", (self._now(), rule_version_id))
                append_event(connection, actor_id=actor_id, action="advancement.rule_version.frozen",
                             resource_type="advancement_rule_version", resource_id=rule_version_id,
                             detail={"stage_id": rule["stage_id"], "version": rule["version"]},
                             occurred_at=self._now())
                return "advancement_rule_version", rule_version_id, {
                    "rule_version_id": rule_version_id, "status": "frozen"}

            return self._receipt(connection, request_id=request_id,
                                 action="advancement.rule_version.freeze", payload=payload, create=create)

    def assign_track_rule(self, *, request_id: str, actor_id: str, stage_id: str, track_id: str,
                          rule_version_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "stage_id": stage_id, "track_id": track_id,
                   "rule_version_id": rule_version_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._stage_row(connection, stage_id)
            self._track_row(connection, track_id)
            rule = self._rule_row(connection, rule_version_id)
            if rule["stage_id"] != stage_id:
                raise ValidationError("规则版本不属于该阶段")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO advancement_track_rules(stage_id,track_id,rule_version_id,set_by,set_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(stage_id,track_id) DO UPDATE SET "
                    "rule_version_id=excluded.rule_version_id,set_by=excluded.set_by,set_at=excluded.set_at",
                    (stage_id, track_id, rule_version_id, actor_id, self._now()))
                append_event(connection, actor_id=actor_id, action="advancement.track_rule.assigned",
                             resource_type="advancement_track_rule", resource_id=f"{stage_id}:{track_id}",
                             detail={"track_id": track_id, "rule_version_id": rule_version_id,
                                     "rule_version": rule["version"]},
                             occurred_at=self._now())
                return "advancement_track_rule", f"{stage_id}:{track_id}", {
                    "stage_id": stage_id, "track_id": track_id, "rule_version_id": rule_version_id}

            return self._receipt(connection, request_id=request_id,
                                 action="advancement.track_rule.assign", payload=payload, create=create)

    def set_track_quota(self, *, request_id: str, actor_id: str, stage_id: str, track_id: str,
                        quota: int) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "stage_id": stage_id, "track_id": track_id, "quota": quota}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._stage_row(connection, stage_id)
            self._track_row(connection, track_id)
            quota = self._positive_int(quota, "quota", allow_zero=True)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO advancement_track_quotas(stage_id,track_id,quota,set_by,set_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(stage_id,track_id) DO UPDATE SET "
                    "quota=excluded.quota,set_by=excluded.set_by,set_at=excluded.set_at",
                    (stage_id, track_id, quota, actor_id, self._now()))
                append_event(connection, actor_id=actor_id, action="advancement.quota.set",
                             resource_type="advancement_track_quota", resource_id=f"{stage_id}:{track_id}",
                             detail={"track_id": track_id, "quota": quota}, occurred_at=self._now())
                return "advancement_track_quota", f"{stage_id}:{track_id}", {
                    "stage_id": stage_id, "track_id": track_id, "quota": quota}

            return self._receipt(connection, request_id=request_id,
                                 action="advancement.quota.set", payload=payload, create=create)

    def set_award_constraint(self, *, request_id: str, actor_id: str, stage_id: str,
                             total_awards: int, per_track_cap: int) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "stage_id": stage_id,
                   "total_awards": total_awards, "per_track_cap": per_track_cap}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._stage_row(connection, stage_id)
            total_awards = self._positive_int(total_awards, "total_awards", allow_zero=True)
            per_track_cap = self._positive_int(per_track_cap, "per_track_cap", allow_zero=True)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO advancement_award_constraints(stage_id,total_awards,per_track_cap,set_by,set_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(stage_id) DO UPDATE SET "
                    "total_awards=excluded.total_awards,per_track_cap=excluded.per_track_cap,"
                    "set_by=excluded.set_by,set_at=excluded.set_at",
                    (stage_id, total_awards, per_track_cap, actor_id, self._now()))
                append_event(connection, actor_id=actor_id, action="advancement.award_constraint.set",
                             resource_type="advancement_award_constraint", resource_id=stage_id,
                             detail={"total_awards": total_awards, "per_track_cap": per_track_cap},
                             occurred_at=self._now())
                return "advancement_award_constraint", stage_id, {
                    "stage_id": stage_id, "total_awards": total_awards, "per_track_cap": per_track_cap}

            return self._receipt(connection, request_id=request_id,
                                 action="advancement.award_constraint.set", payload=payload, create=create)

    def assign_judge(self, *, request_id: str, actor_id: str, stage_id: str, track_id: str,
                     judge_id: str, valid: bool = True) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "stage_id": stage_id, "track_id": track_id,
                   "judge_id": judge_id, "valid": valid}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._stage_row(connection, stage_id)
            self._track_row(connection, track_id)
            judge = self._actor(connection, judge_id)
            if judge.role != "judge":
                raise ValidationError("被指派者不是评委角色")
            if not isinstance(valid, bool):
                raise ValidationError("valid 必须是布尔值")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO advancement_judge_assignments(stage_id,track_id,judge_id,valid,set_by,set_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(stage_id,track_id,judge_id) DO UPDATE SET "
                    "valid=excluded.valid,set_by=excluded.set_by,set_at=excluded.set_at",
                    (stage_id, track_id, judge_id, 1 if valid else 0, actor_id, self._now()))
                append_event(connection, actor_id=actor_id, action="advancement.judge.assigned",
                             resource_type="advancement_judge_assignment",
                             resource_id=f"{stage_id}:{track_id}:{judge_id}",
                             detail={"track_id": track_id, "judge_id": judge_id, "valid": valid},
                             occurred_at=self._now())
                return "advancement_judge_assignment", f"{stage_id}:{track_id}:{judge_id}", {
                    "stage_id": stage_id, "track_id": track_id, "judge_id": judge_id, "valid": valid}

            return self._receipt(connection, request_id=request_id,
                                 action="advancement.judge.assign", payload=payload, create=create)

    # ---------- 作品与资格 ----------

    def register_entry(self, *, request_id: str, actor_id: str, entry_id: str, stage_id: str,
                       track_id: str, participant_id: str, title: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "entry_id": entry_id, "stage_id": stage_id,
                   "track_id": track_id, "participant_id": participant_id, "title": title}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._stage_row(connection, stage_id)
            self._track_row(connection, track_id)
            participant = self._actor(connection, participant_id)
            if participant.role != "participant":
                raise ValidationError("参赛人不是 participant 角色")
            entry_id = self.domain._identifier(entry_id, "entry_id")
            title = self.domain._text(title, "title")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO advancement_entries(entry_id,stage_id,track_id,participant_id,title,"
                        "status,created_by,created_at) VALUES(?,?,?,?,?,'active',?,?)",
                        (entry_id, stage_id, track_id, participant_id, title, actor_id, self._now()))
                except Exception as exc:
                    raise ConflictError("作品编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="advancement.entry.registered",
                             resource_type="advancement_entry", resource_id=entry_id,
                             detail={"stage_id": stage_id, "track_id": track_id,
                                     "participant_id": participant_id, "title": title},
                             occurred_at=self._now())
                return "advancement_entry", entry_id, {"entry_id": entry_id}

            return self._receipt(connection, request_id=request_id,
                                 action="advancement.entry.register", payload=payload, create=create)

    def disqualify_entry(self, *, request_id: str, actor_id: str, entry_id: str,
                         reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "entry_id": entry_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            entry = self._entry_row(connection, entry_id)
            if entry["status"] != "active":
                raise ConflictError("作品当前不是有效状态")
            reason = self.domain._text(reason, "reason", 400)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE advancement_entries SET status='disqualified', disqualified_reason=?, "
                    "disqualified_at=? WHERE entry_id=?", (reason, self._now(), entry_id))
                append_event(connection, actor_id=actor_id, action="advancement.entry.disqualified",
                             resource_type="advancement_entry", resource_id=entry_id,
                             detail={"reason": reason}, occurred_at=self._now())
                return "advancement_entry", entry_id, {"entry_id": entry_id, "status": "disqualified"}

            return self._receipt(connection, request_id=request_id,
                                 action="advancement.entry.disqualify", payload=payload, create=create)

    def reinstate_entry(self, *, request_id: str, actor_id: str, entry_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "entry_id": entry_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            entry = self._entry_row(connection, entry_id)
            if entry["status"] != "disqualified":
                raise ConflictError("作品当前不是资格取消状态")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE advancement_entries SET status='active', disqualified_reason=NULL, "
                    "disqualified_at=NULL WHERE entry_id=?", (entry_id,))
                append_event(connection, actor_id=actor_id, action="advancement.entry.reinstated",
                             resource_type="advancement_entry", resource_id=entry_id,
                             detail={}, occurred_at=self._now())
                return "advancement_entry", entry_id, {"entry_id": entry_id, "status": "active"}

            return self._receipt(connection, request_id=request_id,
                                 action="advancement.entry.reinstate", payload=payload, create=create)

    # ---------- 评分事实 ----------

    def submit_score(self, *, request_id: str, actor_id: str, entry_id: str,
                     scores: dict[str, float], deduction: float = 0.0,
                     deduction_basis: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "entry_id": entry_id, "scores": scores,
                   "deduction": deduction, "deduction_basis": deduction_basis}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "judge")
            entry = self._entry_row(connection, entry_id)
            if entry["status"] != "active":
                raise ValidationError("作品资格已取消，不能提交评分")
            assignment = connection.execute(
                "SELECT valid FROM advancement_judge_assignments WHERE stage_id=? AND track_id=? AND judge_id=?",
                (entry["stage_id"], entry["track_id"], actor_id)).fetchone()
            if assignment is None or not assignment["valid"]:
                raise PermissionDenied("评委不在该赛道的有效评委集合内")
            rule_link = connection.execute(
                "SELECT r.weights_json FROM advancement_track_rules t "
                "JOIN advancement_rule_versions r ON t.rule_version_id=r.rule_version_id "
                "WHERE t.stage_id=? AND t.track_id=?",
                (entry["stage_id"], entry["track_id"])).fetchone()
            if rule_link is None:
                raise ValidationError("赛道尚未指定评分规则版本")
            scores = self._scores_map(scores, json.loads(rule_link["weights_json"]))
            deduction, deduction_basis = self._deduction(deduction, deduction_basis)
            existing = connection.execute(
                "SELECT * FROM advancement_scores WHERE entry_id=? AND judge_id=?",
                (entry_id, actor_id)).fetchone()
            if existing is not None and existing["status"] == "sealed":
                raise ConflictError("评分已封存，不能修改")
            if existing is not None and existing["status"] == "revoked":
                raise ConflictError("评分已撤销，不能修改")

            def create() -> tuple[str, str, dict[str, Any]]:
                score_id = existing["score_id"] if existing is not None else uuid.uuid4().hex
                if existing is not None:
                    connection.execute(
                        "UPDATE advancement_scores SET scores_json=?, deduction=?, deduction_basis=?, "
                        "submitted_at=? WHERE score_id=?",
                        (canonical_json(scores), deduction, deduction_basis, self._now(), score_id))
                else:
                    connection.execute(
                        "INSERT INTO advancement_scores(score_id,stage_id,track_id,entry_id,judge_id,"
                        "scores_json,deduction,deduction_basis,status,submitted_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (score_id, entry["stage_id"], entry["track_id"], entry_id, actor_id,
                         canonical_json(scores), deduction, deduction_basis, "submitted", self._now()))
                append_event(connection, actor_id=actor_id, action="advancement.score.submitted",
                             resource_type="advancement_score", resource_id=score_id,
                             detail={"entry_id": entry_id, "judge_id": actor_id, "deduction": deduction},
                             occurred_at=self._now())
                return "advancement_score", score_id, {"score_id": score_id}

            return self._receipt(connection, request_id=request_id,
                                 action="advancement.score.submit", payload=payload, create=create)

    def revoke_score(self, *, request_id: str, actor_id: str, score_id: str,
                     reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "score_id": score_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            score = self._score_row(connection, score_id)
            if score["status"] == "revoked":
                raise ConflictError("评分已处于撤销状态")
            reason = self.domain._text(reason, "reason", 400)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE advancement_scores SET status='revoked', revoked_at=?, revoke_reason=? "
                    "WHERE score_id=?", (self._now(), reason, score_id))
                append_event(connection, actor_id=actor_id, action="advancement.score.revoked",
                             resource_type="advancement_score", resource_id=score_id,
                             detail={"reason": reason}, occurred_at=self._now())
                return "advancement_score", score_id, {"score_id": score_id, "status": "revoked"}

            return self._receipt(connection, request_id=request_id,
                                 action="advancement.score.revoke", payload=payload, create=create)

    def seal_scores(self, *, request_id: str, actor_id: str, stage_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "stage_id": stage_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._stage_row(connection, stage_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                sealed = connection.execute(
                    "UPDATE advancement_scores SET status='sealed', sealed_at=? "
                    "WHERE stage_id=? AND status='submitted'", (self._now(), stage_id)).rowcount
                append_event(connection, actor_id=actor_id, action="advancement.scores.sealed",
                             resource_type="advancement_stage", resource_id=stage_id,
                             detail={"sealed": sealed}, occurred_at=self._now())
                return "advancement_stage", stage_id, {"stage_id": stage_id, "sealed": sealed}

            return self._receipt(connection, request_id=request_id,
                                 action="advancement.scores.seal", payload=payload, create=create)

    # ---------- 榜单计算、会签与发布 ----------

    def compute_run(self, *, request_id: str, actor_id: str, stage_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "stage_id": stage_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._advance_clocks(connection)
            stage = self._stage_row(connection, stage_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                run_id, response = self._compute_run(connection, stage, created_by=actor_id,
                                                     supersedes_run_id=None)
                return "advancement_run", run_id, response

            return self._receipt(connection, request_id=request_id,
                                 action="advancement.run.compute", payload=payload, create=create)

    def _compute_run(self, connection, stage, *, created_by: str,
                     supersedes_run_id: str | None) -> tuple[str, dict[str, Any]]:
        stage_id = stage["stage_id"]
        now = self._now()
        sealed = connection.execute(
            "UPDATE advancement_scores SET status='sealed', sealed_at=? "
            "WHERE stage_id=? AND status='submitted'", (now, stage_id)).rowcount
        rules: dict[str, Any] = {}
        for row in connection.execute(
                "SELECT t.track_id, r.* FROM advancement_track_rules t "
                "JOIN advancement_rule_versions r ON t.rule_version_id=r.rule_version_id "
                "WHERE t.stage_id=?", (stage_id,)):
            if row["status"] != "frozen":
                raise ValidationError(f"赛道 {row['track_id']} 使用的规则版本尚未冻结")
            rules[row["track_id"]] = {
                "rule_version_id": row["rule_version_id"],
                "version": row["version"],
                "weights": json.loads(row["weights_json"]),
                "tie_policy": row["tie_policy"],
                "tie_break_dimensions": json.loads(row["tie_break_json"]),
                "missing_score_policy": row["missing_score_policy"],
                "late_score_policy": row["late_score_policy"],
                "score_deadline": row["score_deadline"],
                "pass_score": row["pass_score"],
            }
        entries = [dict(row) for row in connection.execute(
            "SELECT entry_id, track_id, participant_id, title, status, disqualified_reason "
            "FROM advancement_entries WHERE stage_id=? ORDER BY entry_id", (stage_id,))]
        if not entries:
            raise ValidationError("阶段内没有参赛作品")
        for track_id in {entry["track_id"] for entry in entries}:
            if track_id not in rules:
                raise ValidationError(f"赛道 {track_id} 尚未指定评分规则版本")
        quotas = {row["track_id"]: row["quota"] for row in connection.execute(
            "SELECT track_id, quota FROM advancement_track_quotas WHERE stage_id=?", (stage_id,))}
        constraint_row = connection.execute(
            "SELECT total_awards, per_track_cap FROM advancement_award_constraints WHERE stage_id=?",
            (stage_id,)).fetchone()
        constraint = dict(constraint_row) if constraint_row else None
        judges: dict[str, list[str]] = {}
        for row in connection.execute(
                "SELECT track_id, judge_id FROM advancement_judge_assignments "
                "WHERE stage_id=? AND valid=1", (stage_id,)):
            judges.setdefault(row["track_id"], []).append(row["judge_id"])
        scores = []
        for row in connection.execute(
                "SELECT score_id, entry_id, judge_id, scores_json, deduction, deduction_basis, status, "
                "submitted_at, sealed_at, revoked_at, revoke_reason FROM advancement_scores "
                "WHERE stage_id=? ORDER BY score_id", (stage_id,)):
            scores.append({
                "score_id": row["score_id"], "entry_id": row["entry_id"], "judge_id": row["judge_id"],
                "scores": json.loads(row["scores_json"]), "deduction": row["deduction"],
                "deduction_basis": row["deduction_basis"], "status": row["status"],
                "submitted_at": row["submitted_at"], "sealed_at": row["sealed_at"],
                "revoked_at": row["revoked_at"], "revoke_reason": row["revoke_reason"],
            })
        snapshot = {
            "stage_id": stage_id,
            "rules": rules,
            "quotas": quotas,
            "award_constraint": constraint,
            "entries": entries,
            "judges": judges,
            "scores": scores,
        }
        items = compute_ranking(snapshot)
        projection = output_projection(items)
        input_hash = digest(snapshot)
        output_hash = digest(projection)
        reference_run_id = supersedes_run_id
        if reference_run_id is None:
            row = connection.execute(
                "SELECT run_id FROM advancement_runs WHERE stage_id=? AND status='published' "
                "ORDER BY version DESC LIMIT 1", (stage_id,)).fetchone()
            reference_run_id = row["run_id"] if row else None
        change_summary = None
        if reference_run_id is not None:
            previous_items = self._run_items(connection, reference_run_id)
            change_summary = build_change_summary(previous_items, items, reference_run_id)
        run_id = uuid.uuid4().hex
        deadline = self._after_hours(stage["countersign_ttl_hours"])
        connection.execute(
            "INSERT INTO advancement_runs(run_id,stage_id,status,version,snapshot_json,input_hash,"
            "output_hash,change_summary_json,supersedes_run_id,countersign_deadline,appeal_deadline,"
            "created_by,created_at,published_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, stage_id, "candidate", None, canonical_json(snapshot), input_hash, output_hash,
             canonical_json(change_summary) if change_summary else None, reference_run_id,
             deadline, None, created_by, now, None))
        for item in items:
            connection.execute(
                "INSERT INTO advancement_run_items(run_id,entry_id,track_id,participant_id,total_score,"
                "rank,advanced,awarded,explanation_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, item["entry_id"], item["track_id"], item["participant_id"], item["total_score"],
                 item["rank"], 1 if item["advanced"] else 0, 1 if item["awarded"] else 0,
                 canonical_json(item["explanation"])))
        append_event(connection, actor_id=created_by, action="advancement.run.computed",
                     resource_type="advancement_run", resource_id=run_id,
                     detail={"stage_id": stage_id, "input_hash": input_hash, "output_hash": output_hash,
                             "item_count": len(items), "sealed_scores": sealed,
                             "supersedes_run_id": reference_run_id},
                     occurred_at=now)
        return run_id, {"run_id": run_id, "status": "candidate",
                        "countersign_deadline": deadline,
                        "input_hash": input_hash, "output_hash": output_hash}

    def countersign(self, *, request_id: str, actor_id: str, run_id: str, decision: str,
                    comment: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "run_id": run_id, "decision": decision, "comment": comment}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            self._advance_clocks(connection)
            run = self._run_row(connection, run_id)
            if run["status"] != "candidate":
                raise ConflictError("候选榜当前不在会签状态")
            if decision not in ("approved", "rejected"):
                raise ValidationError("decision 必须是 approved 或 rejected")
            if connection.execute(
                    "SELECT 1 FROM advancement_countersigns WHERE run_id=? AND actor_id=?",
                    (run_id, actor_id)).fetchone():
                raise ConflictError("该会签人已签署过")
            comment = self.domain._text(comment, "comment", 200) if comment else None

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO advancement_countersigns(run_id,actor_id,duty,decision,comment,decided_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (run_id, actor_id, "reviewer", decision, comment, self._now()))
                run_status = "candidate"
                if decision == "rejected":
                    connection.execute(
                        "UPDATE advancement_runs SET status='rejected' WHERE run_id=?", (run_id,))
                    run_status = "rejected"
                    append_event(connection, actor_id=actor_id, action="advancement.run.rejected",
                                 resource_type="advancement_run", resource_id=run_id,
                                 detail={"comment": comment}, occurred_at=self._now())
                append_event(connection, actor_id=actor_id, action="advancement.run.countersigned",
                             resource_type="advancement_run", resource_id=run_id,
                             detail={"duty": "reviewer", "decision": decision, "comment": comment},
                             occurred_at=self._now())
                approvals = connection.execute(
                    "SELECT COUNT(*) AS count FROM advancement_countersigns "
                    "WHERE run_id=? AND duty='reviewer' AND decision='approved'",
                    (run_id,)).fetchone()["count"]
                return "advancement_run", run_id, {
                    "run_id": run_id, "decision": decision, "approvals": approvals,
                    "run_status": run_status}

            return self._receipt(connection, request_id=request_id,
                                 action="advancement.run.countersign", payload=payload, create=create)

    def publish_run(self, *, request_id: str, actor_id: str, run_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "run_id": run_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "publisher")
            self._advance_clocks(connection)
            run = self._run_row(connection, run_id)
            if run["status"] != "candidate":
                raise ConflictError("候选榜当前不可发布")
            stage = self._stage_row(connection, run["stage_id"])
            approvals = connection.execute(
                "SELECT COUNT(*) AS count FROM advancement_countersigns "
                "WHERE run_id=? AND duty='reviewer' AND decision='approved'",
                (run_id,)).fetchone()["count"]
            if approvals < stage["reviewer_quorum"]:
                raise ConflictError("复核会签人数不足，不能发布")
            if connection.execute(
                    "SELECT 1 FROM advancement_countersigns WHERE run_id=? AND actor_id=?",
                    (run_id, actor_id)).fetchone():
                raise PermissionDenied("发布人必须与复核人相互独立")

            def create() -> tuple[str, str, dict[str, Any]]:
                version = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM advancement_runs "
                    "WHERE stage_id=? AND version IS NOT NULL", (run["stage_id"],)).fetchone()["next_version"]
                now = self._now()
                appeal_deadline = self._after_hours(stage["appeal_window_hours"])
                superseded = [row["run_id"] for row in connection.execute(
                    "SELECT run_id FROM advancement_runs WHERE stage_id=? AND status='published'",
                    (run["stage_id"],))]
                connection.execute(
                    "UPDATE advancement_runs SET status='superseded' "
                    "WHERE stage_id=? AND status='published'", (run["stage_id"],))
                for superseded_id in superseded:
                    append_event(connection, actor_id=actor_id, action="advancement.run.superseded",
                                 resource_type="advancement_run", resource_id=superseded_id,
                                 detail={"by_run_id": run_id, "version": version}, occurred_at=now)
                connection.execute(
                    "UPDATE advancement_runs SET status='published', version=?, published_at=?, "
                    "appeal_deadline=? WHERE run_id=?", (version, now, appeal_deadline, run_id))
                connection.execute(
                    "INSERT INTO advancement_countersigns(run_id,actor_id,duty,decision,comment,decided_at) "
                    "VALUES(?,?,?,?,?,?)", (run_id, actor_id, "publisher", "approved", None, now))
                items = self._run_items(connection, run_id)
                for item in items:
                    kinds = []
                    if item["advanced"]:
                        kinds.append("advancement")
                    if item["awarded"]:
                        kinds.append("award")
                    if not kinds:
                        kinds.append("elimination")
                    for kind in kinds:
                        connection.execute(
                            "INSERT INTO advancement_notifications(notification_id,run_id,entry_id,"
                            "participant_id,kind,version,created_at) VALUES(?,?,?,?,?,?,?)",
                            (uuid.uuid4().hex, run_id, item["entry_id"], item["participant_id"],
                             kind, version, now))
                append_event(connection, actor_id=actor_id, action="advancement.run.published",
                             resource_type="advancement_run", resource_id=run_id,
                             detail={"stage_id": run["stage_id"], "version": version,
                                     "approvals": approvals, "appeal_deadline": appeal_deadline},
                             occurred_at=now)
                return "advancement_run", run_id, {
                    "run_id": run_id, "status": "published", "version": version,
                    "appeal_deadline": appeal_deadline}

            return self._receipt(connection, request_id=request_id,
                                 action="advancement.run.publish", payload=payload, create=create)

    def replay_run(self, *, actor_id: str, run_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, "admin", "operator", "reviewer", "publisher", "auditor")
        run = self._run_row(connection, run_id)
        snapshot = json.loads(run["snapshot_json"])
        replayed = output_projection(compute_ranking(snapshot))
        stored = output_projection(self._run_items(connection, run_id))
        stored_by_entry = {item["entry_id"]: item for item in stored}
        replayed_by_entry = {item["entry_id"]: item for item in replayed}
        differences = []
        for entry_id in sorted(set(stored_by_entry) | set(replayed_by_entry)):
            before = stored_by_entry.get(entry_id)
            after = replayed_by_entry.get(entry_id)
            if before != after:
                differences.append({"entry_id": entry_id, "stored": before, "replayed": after})
        return {"run_id": run_id, "match": not differences, "differences": differences,
                "input_hash": run["input_hash"], "stored_output_hash": run["output_hash"],
                "replayed_output_hash": digest(replayed)}

    def explain_item(self, *, actor_id: str, run_id: str, entry_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        run = self._run_row(connection, run_id)
        item = connection.execute(
            "SELECT * FROM advancement_run_items WHERE run_id=? AND entry_id=?",
            (run_id, entry_id)).fetchone()
        if item is None:
            raise NotFoundError("榜单中不存在该作品")
        if actor.role in PRIVILEGED_ROLES:
            pass
        elif actor.role == "participant":
            if run["status"] not in ("published", "superseded"):
                raise PermissionDenied("榜单尚未发布")
            if item["participant_id"] != actor_id:
                raise PermissionDenied("只能查看本人作品的解释")
        else:
            raise PermissionDenied("当前角色不能查看解释")
        return {"run_id": run_id, "entry_id": entry_id, "run_status": run["status"],
                "version": run["version"],
                "item": {"entry_id": item["entry_id"], "track_id": item["track_id"],
                         "participant_id": item["participant_id"], "total_score": item["total_score"],
                         "rank": item["rank"], "advanced": bool(item["advanced"]),
                         "awarded": bool(item["awarded"]),
                         "explanation": json.loads(item["explanation_json"])}}

    # ---------- 申诉 ----------

    def file_appeal(self, *, request_id: str, actor_id: str, run_id: str, entry_id: str,
                    target_type: str, target_id: str, reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "run_id": run_id, "entry_id": entry_id,
                   "target_type": target_type, "target_id": target_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "participant")
            self._advance_clocks(connection)
            run = self._run_row(connection, run_id)
            if run["status"] != "published":
                raise ValidationError("只能针对已发布的榜单版本提出申诉")
            entry = self._entry_row(connection, entry_id)
            if entry["participant_id"] != actor_id:
                raise PermissionDenied("只能为本人作品提出申诉")
            if connection.execute(
                    "SELECT 1 FROM advancement_run_items WHERE run_id=? AND entry_id=?",
                    (run_id, entry_id)).fetchone() is None:
                raise ValidationError("作品不在该榜单内")
            if parse_instant(run["appeal_deadline"]) < self.clock.now():
                raise ValidationError("申诉时限已过")
            if target_type not in ("score", "qualification"):
                raise ValidationError("target_type 必须是 score 或 qualification")
            if target_type == "score":
                score = self._score_row(connection, target_id)
                if score["entry_id"] != entry_id:
                    raise ValidationError("申诉引用的评分不属于该作品")
            elif target_id != entry_id:
                raise ValidationError("资格申诉必须引用本人作品编号")
            reason = self.domain._text(reason, "reason", 400)
            if connection.execute(
                    "SELECT 1 FROM advancement_appeals WHERE run_id=? AND target_type=? AND target_id=? "
                    "AND status='filed'", (run_id, target_type, target_id)).fetchone():
                raise ConflictError("同一事实已有未决申诉")

            def create() -> tuple[str, str, dict[str, Any]]:
                appeal_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO advancement_appeals(appeal_id,run_id,stage_id,entry_id,participant_id,"
                    "target_type,target_id,reason,status,filed_at,deadline_at,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (appeal_id, run_id, run["stage_id"], entry_id, actor_id, target_type, target_id,
                     reason, "filed", self._now(), run["appeal_deadline"], self._now()))
                append_event(connection, actor_id=actor_id, action="advancement.appeal.filed",
                             resource_type="advancement_appeal", resource_id=appeal_id,
                             detail={"run_id": run_id, "entry_id": entry_id,
                                     "target_type": target_type, "target_id": target_id},
                             occurred_at=self._now())
                return "advancement_appeal", appeal_id, {
                    "appeal_id": appeal_id, "deadline_at": run["appeal_deadline"]}

            return self._receipt(connection, request_id=request_id,
                                 action="advancement.appeal.file", payload=payload, create=create)

    def adjudicate_appeal(self, *, request_id: str, actor_id: str, appeal_id: str, decision: str,
                          note: str, remedy: str | None = None, deduction: float | None = None,
                          deduction_basis: str | None = None, reason: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "appeal_id": appeal_id, "decision": decision, "note": note,
                   "remedy": remedy, "deduction": deduction, "deduction_basis": deduction_basis,
                   "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            self._advance_clocks(connection)
            appeal = self._appeal_row(connection, appeal_id)
            if appeal["status"] != "filed":
                raise ConflictError("申诉不在待裁决状态")
            if decision not in ("upheld", "rejected"):
                raise ValidationError("decision 必须是 upheld 或 rejected")
            note = self.domain._text(note, "note", 400)

            def create() -> tuple[str, str, dict[str, Any]]:
                result_run_id = None
                if decision == "upheld":
                    if remedy is None:
                        raise ValidationError("裁决成立必须给出 remedy")
                    self._apply_remedy(connection, actor_id, appeal, remedy,
                                       deduction=deduction, deduction_basis=deduction_basis,
                                       reason=reason)
                    stage = self._stage_row(connection, appeal["stage_id"])
                    result_run_id, _ = self._compute_run(connection, stage, created_by=actor_id,
                                                         supersedes_run_id=None)
                status = "adjudicated_upheld" if decision == "upheld" else "adjudicated_rejected"
                connection.execute(
                    "UPDATE advancement_appeals SET status=?, adjudicated_by=?, adjudicated_at=?, "
                    "decision_note=?, remedy=?, result_run_id=? WHERE appeal_id=?",
                    (status, actor_id, self._now(), note,
                     remedy if decision == "upheld" else None, result_run_id, appeal_id))
                append_event(connection, actor_id=actor_id, action="advancement.appeal.adjudicated",
                             resource_type="advancement_appeal", resource_id=appeal_id,
                             detail={"decision": decision, "remedy": remedy,
                                     "result_run_id": result_run_id},
                             occurred_at=self._now())
                response: dict[str, Any] = {"appeal_id": appeal_id, "status": status}
                if result_run_id:
                    response["result_run_id"] = result_run_id
                return "advancement_appeal", appeal_id, response

            return self._receipt(connection, request_id=request_id,
                                 action="advancement.appeal.adjudicate", payload=payload, create=create)

    def _apply_remedy(self, connection, actor_id: str, appeal, remedy: str, *,
                      deduction: float | None, deduction_basis: str | None,
                      reason: str | None) -> None:
        if appeal["target_type"] == "score":
            score = self._score_row(connection, appeal["target_id"])
            if remedy == "revoke":
                if score["status"] == "revoked":
                    raise ConflictError("评分已处于撤销状态")
                connection.execute(
                    "UPDATE advancement_scores SET status='revoked', revoked_at=?, revoke_reason=? "
                    "WHERE score_id=?",
                    (self._now(), f"申诉 {appeal['appeal_id']} 裁决撤销", score["score_id"]))
                action = "advancement.score.revoked"
            elif remedy == "restore":
                if score["status"] != "revoked":
                    raise ConflictError("评分不在撤销状态")
                connection.execute(
                    "UPDATE advancement_scores SET status='sealed', revoked_at=NULL, revoke_reason=NULL "
                    "WHERE score_id=?", (score["score_id"],))
                action = "advancement.score.restored"
            elif remedy == "adjust_deduction":
                if score["status"] == "revoked":
                    raise ConflictError("评分已撤销，不能调整扣分")
                new_deduction, new_basis = self._deduction(
                    deduction if deduction is not None else 0.0, deduction_basis)
                connection.execute(
                    "UPDATE advancement_scores SET deduction=?, deduction_basis=? WHERE score_id=?",
                    (new_deduction, new_basis, score["score_id"]))
                action = "advancement.score.deduction_adjusted"
            else:
                raise ValidationError("remedy 不在允许范围内")
            append_event(connection, actor_id=actor_id, action=action,
                         resource_type="advancement_score", resource_id=score["score_id"],
                         detail={"appeal_id": appeal["appeal_id"], "remedy": remedy},
                         occurred_at=self._now())
            return
        entry = self._entry_row(connection, appeal["target_id"])
        if remedy == "reinstate":
            if entry["status"] != "disqualified":
                raise ConflictError("作品当前不是资格取消状态")
            connection.execute(
                "UPDATE advancement_entries SET status='active', disqualified_reason=NULL, "
                "disqualified_at=NULL WHERE entry_id=?", (entry["entry_id"],))
            action = "advancement.entry.reinstated"
        elif remedy == "disqualify":
            if entry["status"] != "active":
                raise ConflictError("作品当前不是有效状态")
            if not reason or not str(reason).strip():
                raise ValidationError("取消资格必须填写原因")
            connection.execute(
                "UPDATE advancement_entries SET status='disqualified', disqualified_reason=?, "
                "disqualified_at=? WHERE entry_id=?",
                (self.domain._text(reason, "reason", 400), self._now(), entry["entry_id"]))
            action = "advancement.entry.disqualified"
        else:
            raise ValidationError("remedy 不在允许范围内")
        append_event(connection, actor_id=actor_id, action=action,
                     resource_type="advancement_entry", resource_id=entry["entry_id"],
                     detail={"appeal_id": appeal["appeal_id"], "remedy": remedy},
                     occurred_at=self._now())

    # ---------- 时钟推进 ----------

    def tick(self, *, actor_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            expired_runs, expired_appeals = self._advance_clocks(connection)
            return {"expired_runs": expired_runs, "expired_appeals": expired_appeals}

    def _advance_clocks(self, connection) -> tuple[int, int]:
        now = self.clock.now()
        expired_runs = 0
        for row in connection.execute(
                "SELECT run_id, countersign_deadline FROM advancement_runs WHERE status='candidate'"):
            if parse_instant(row["countersign_deadline"]) < now:
                connection.execute(
                    "UPDATE advancement_runs SET status='expired' WHERE run_id=?", (row["run_id"],))
                append_event(connection, actor_id="system", action="advancement.run.expired",
                             resource_type="advancement_run", resource_id=row["run_id"],
                             detail={"countersign_deadline": row["countersign_deadline"]},
                             occurred_at=self._now())
                expired_runs += 1
        expired_appeals = 0
        for row in connection.execute(
                "SELECT appeal_id, deadline_at FROM advancement_appeals WHERE status='filed'"):
            if parse_instant(row["deadline_at"]) < now:
                connection.execute(
                    "UPDATE advancement_appeals SET status='expired' WHERE appeal_id=?",
                    (row["appeal_id"],))
                append_event(connection, actor_id="system", action="advancement.appeal.expired",
                             resource_type="advancement_appeal", resource_id=row["appeal_id"],
                             detail={"deadline_at": row["deadline_at"]}, occurred_at=self._now())
                expired_appeals += 1
        return expired_runs, expired_appeals

    # ---------- 查询 ----------

    def _run_items(self, connection, run_id: str) -> list[dict[str, Any]]:
        items = []
        for row in connection.execute(
                "SELECT * FROM advancement_run_items WHERE run_id=? "
                "ORDER BY track_id, rank IS NULL, rank, entry_id", (run_id,)):
            items.append({
                "entry_id": row["entry_id"], "track_id": row["track_id"],
                "participant_id": row["participant_id"], "total_score": row["total_score"],
                "rank": row["rank"], "advanced": bool(row["advanced"]),
                "awarded": bool(row["awarded"]),
                "explanation": json.loads(row["explanation_json"]),
            })
        return items

    def _run_summary(self, row) -> dict[str, Any]:
        return {"run_id": row["run_id"], "stage_id": row["stage_id"], "status": row["status"],
                "version": row["version"], "input_hash": row["input_hash"],
                "output_hash": row["output_hash"], "supersedes_run_id": row["supersedes_run_id"],
                "countersign_deadline": row["countersign_deadline"],
                "appeal_deadline": row["appeal_deadline"], "created_by": row["created_by"],
                "created_at": row["created_at"], "published_at": row["published_at"]}

    def _score_summary(self, row) -> dict[str, Any]:
        return {"score_id": row["score_id"], "entry_id": row["entry_id"],
                "judge_id": row["judge_id"], "scores": json.loads(row["scores_json"]),
                "deduction": row["deduction"], "deduction_basis": row["deduction_basis"],
                "status": row["status"], "submitted_at": row["submitted_at"],
                "sealed_at": row["sealed_at"], "revoked_at": row["revoked_at"],
                "revoke_reason": row["revoke_reason"]}

    def _appeal_summary(self, row) -> dict[str, Any]:
        return {"appeal_id": row["appeal_id"], "run_id": row["run_id"], "stage_id": row["stage_id"],
                "entry_id": row["entry_id"], "participant_id": row["participant_id"],
                "target_type": row["target_type"], "target_id": row["target_id"],
                "reason": row["reason"], "status": row["status"], "filed_at": row["filed_at"],
                "deadline_at": row["deadline_at"], "adjudicated_by": row["adjudicated_by"],
                "adjudicated_at": row["adjudicated_at"], "decision_note": row["decision_note"],
                "remedy": row["remedy"], "result_run_id": row["result_run_id"]}

    def list_stages(self, *, actor_id: str = "") -> list[dict[str, Any]]:
        connection = self.database.connection
        self._optional_actor(connection, actor_id)
        stages = []
        for row in connection.execute("SELECT * FROM advancement_stages ORDER BY sequence, stage_id"):
            published = connection.execute(
                "SELECT MAX(version) AS version FROM advancement_runs "
                "WHERE stage_id=? AND status='published'", (row["stage_id"],)).fetchone()["version"]
            stages.append({"stage_id": row["stage_id"], "name": row["name"],
                           "sequence": row["sequence"], "reviewer_quorum": row["reviewer_quorum"],
                           "countersign_ttl_hours": row["countersign_ttl_hours"],
                           "appeal_window_hours": row["appeal_window_hours"],
                           "published_version": published})
        return stages

    def get_stage_detail(self, *, actor_id: str, stage_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, *PRIVILEGED_ROLES)
        stage = self._stage_row(connection, stage_id)
        rules = []
        for row in connection.execute(
                "SELECT * FROM advancement_rule_versions WHERE stage_id=? ORDER BY version", (stage_id,)):
            rules.append({"rule_version_id": row["rule_version_id"], "version": row["version"],
                          "status": row["status"], "weights": json.loads(row["weights_json"]),
                          "tie_policy": row["tie_policy"],
                          "tie_break_dimensions": json.loads(row["tie_break_json"]),
                          "missing_score_policy": row["missing_score_policy"],
                          "late_score_policy": row["late_score_policy"],
                          "score_deadline": row["score_deadline"], "pass_score": row["pass_score"],
                          "frozen_at": row["frozen_at"]})
        tracks = []
        for row in connection.execute(
                "SELECT t.track_id, t.name, r.rule_version_id, q.quota FROM advancement_tracks t "
                "LEFT JOIN advancement_track_rules r ON t.track_id=r.track_id AND r.stage_id=? "
                "LEFT JOIN advancement_track_quotas q ON t.track_id=q.track_id AND q.stage_id=? "
                "ORDER BY t.track_id", (stage_id, stage_id)):
            judges = [j["judge_id"] for j in connection.execute(
                "SELECT judge_id FROM advancement_judge_assignments "
                "WHERE stage_id=? AND track_id=? AND valid=1 ORDER BY judge_id",
                (stage_id, row["track_id"]))]
            tracks.append({"track_id": row["track_id"], "name": row["name"],
                           "rule_version_id": row["rule_version_id"], "quota": row["quota"],
                           "valid_judges": judges})
        constraint = connection.execute(
            "SELECT total_awards, per_track_cap FROM advancement_award_constraints WHERE stage_id=?",
            (stage_id,)).fetchone()
        return {"stage_id": stage_id, "name": stage["name"], "sequence": stage["sequence"],
                "reviewer_quorum": stage["reviewer_quorum"],
                "countersign_ttl_hours": stage["countersign_ttl_hours"],
                "appeal_window_hours": stage["appeal_window_hours"],
                "rule_versions": rules, "tracks": tracks,
                "award_constraint": dict(constraint) if constraint else None}

    def list_scores(self, *, actor_id: str, stage_id: str, entry_id: str | None = None,
                    judge_id: str | None = None) -> list[dict[str, Any]]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._stage_row(connection, stage_id)
        query = ("SELECT s.*, e.participant_id FROM advancement_scores s "
                 "JOIN advancement_entries e ON s.entry_id=e.entry_id WHERE s.stage_id=?")
        parameters: list[Any] = [stage_id]
        if entry_id:
            query += " AND s.entry_id=?"
            parameters.append(entry_id)
        if judge_id:
            query += " AND s.judge_id=?"
            parameters.append(judge_id)
        query += " ORDER BY s.entry_id, s.judge_id"
        visible = []
        for row in connection.execute(query, parameters):
            if actor.role in PRIVILEGED_ROLES:
                visible.append(self._score_summary(row))
            elif actor.role == "judge" and (row["judge_id"] == actor_id or row["sealed_at"] is not None):
                visible.append(self._score_summary(row))
            elif actor.role == "participant" and row["participant_id"] == actor_id:
                visible.append(self._score_summary(row))
        return visible

    def list_runs(self, *, actor_id: str, stage_id: str | None = None) -> list[dict[str, Any]]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        query = "SELECT * FROM advancement_runs"
        parameters: list[Any] = []
        if stage_id:
            query += " WHERE stage_id=?"
            parameters.append(stage_id)
        query += " ORDER BY created_at, run_id"
        runs = []
        for row in connection.execute(query, parameters):
            if actor.role in PRIVILEGED_ROLES or row["status"] in ("published", "superseded"):
                runs.append(self._run_summary(row))
        return runs

    def get_run_detail(self, *, actor_id: str, run_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._optional_actor(connection, actor_id)
        run = self._run_row(connection, run_id)
        items = self._run_items(connection, run_id)
        detail = self._run_summary(run)
        detail["change_summary"] = (
            json.loads(run["change_summary_json"]) if run["change_summary_json"] else None)
        if actor is not None and actor.role in PRIVILEGED_ROLES:
            detail["items"] = items
            detail["countersigns"] = [
                {"actor_id": row["actor_id"], "duty": row["duty"], "decision": row["decision"],
                 "comment": row["comment"], "decided_at": row["decided_at"]}
                for row in connection.execute(
                    "SELECT * FROM advancement_countersigns WHERE run_id=? ORDER BY decided_at, actor_id",
                    (run_id,))]
            detail["snapshot"] = json.loads(run["snapshot_json"])
            return detail
        if run["status"] not in ("published", "superseded"):
            raise PermissionDenied("榜单尚未发布")
        titles = {row["entry_id"]: row["title"] for row in connection.execute(
            "SELECT entry_id, title FROM advancement_entries WHERE stage_id=?", (run["stage_id"],))}
        public_items = []
        for item in items:
            public_item = {"entry_id": item["entry_id"], "title": titles.get(item["entry_id"]),
                           "track_id": item["track_id"], "rank": item["rank"],
                           "total_score": item["total_score"], "advanced": item["advanced"],
                           "awarded": item["awarded"]}
            if actor is not None and actor.role == "participant" \
                    and item["participant_id"] == actor.actor_id:
                public_item["explanation"] = item["explanation"]
            public_items.append(public_item)
        detail["items"] = public_items
        return detail

    def public_results(self, *, stage_id: str, actor_id: str = "") -> dict[str, Any]:
        connection = self.database.connection
        self._optional_actor(connection, actor_id)
        self._stage_row(connection, stage_id)
        run = connection.execute(
            "SELECT * FROM advancement_runs WHERE stage_id=? AND status='published' "
            "ORDER BY version DESC LIMIT 1", (stage_id,)).fetchone()
        if run is None:
            return {"stage_id": stage_id, "published": False, "version": None, "items": []}
        items = self._run_items(connection, run["run_id"])
        titles = {row["entry_id"]: row["title"] for row in connection.execute(
            "SELECT entry_id, title FROM advancement_entries WHERE stage_id=?", (stage_id,))}
        return {
            "stage_id": stage_id,
            "published": True,
            "run_id": run["run_id"],
            "version": run["version"],
            "published_at": run["published_at"],
            "appeal_deadline": run["appeal_deadline"],
            "items": [
                {"entry_id": item["entry_id"], "title": titles.get(item["entry_id"]),
                 "track_id": item["track_id"], "rank": item["rank"],
                 "total_score": item["total_score"], "advanced": item["advanced"],
                 "awarded": item["awarded"]}
                for item in items
            ],
        }

    def my_entries(self, *, actor_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, "participant")
        entries = []
        for row in connection.execute(
                "SELECT * FROM advancement_entries WHERE participant_id=? ORDER BY entry_id", (actor_id,)):
            scores = [self._score_summary(score) for score in connection.execute(
                "SELECT * FROM advancement_scores WHERE entry_id=? ORDER BY judge_id", (row["entry_id"],))]
            results = []
            for item in connection.execute(
                    "SELECT i.*, r.version, r.published_at FROM advancement_run_items i "
                    "JOIN advancement_runs r ON i.run_id=r.run_id "
                    "WHERE i.entry_id=? AND r.status IN ('published','superseded') "
                    "ORDER BY r.version", (row["entry_id"],)):
                results.append({"run_id": item["run_id"], "version": item["version"],
                                "published_at": item["published_at"],
                                "total_score": item["total_score"], "rank": item["rank"],
                                "advanced": bool(item["advanced"]), "awarded": bool(item["awarded"]),
                                "explanation": json.loads(item["explanation_json"])})
            entries.append({"entry_id": row["entry_id"], "stage_id": row["stage_id"],
                            "track_id": row["track_id"], "title": row["title"],
                            "status": row["status"],
                            "disqualified_reason": row["disqualified_reason"],
                            "scores": scores, "results": results})
        notifications = [
            {"notification_id": row["notification_id"], "run_id": row["run_id"],
             "entry_id": row["entry_id"], "kind": row["kind"], "version": row["version"],
             "created_at": row["created_at"]}
            for row in connection.execute(
                "SELECT * FROM advancement_notifications WHERE participant_id=? "
                "ORDER BY created_at, notification_id", (actor_id,))]
        return {"entries": entries, "notifications": notifications}

    def list_appeals(self, *, actor_id: str, stage_id: str | None = None) -> list[dict[str, Any]]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        query = "SELECT * FROM advancement_appeals"
        parameters: list[Any] = []
        if stage_id:
            query += " WHERE stage_id=?"
            parameters.append(stage_id)
        query += " ORDER BY filed_at, appeal_id"
        appeals = []
        for row in connection.execute(query, parameters):
            if actor.role in PRIVILEGED_ROLES or row["participant_id"] == actor_id:
                appeals.append(self._appeal_summary(row))
        return appeals
