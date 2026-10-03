"""晋级评议与申诉领域服务。

在基础服务（操作者、幂等、审计链、事务）之上实现：

- 每个阶段的规则版本、维度权重、有效评委集合、评分事实、扣分依据、
  赛道配额与跨赛道奖项约束的登记与冻结；
- 可复核候选榜的生成、相互独立的复核人/发布人会签与发布；
- 并列、缺评、评分撤销与资格取消按冻结规则处理；
- 发布后榜单不可原地改写，申诉在时限内引用具体事实，裁决产生新版本；
- 参赛人/评委/管理侧的可见性边界，以及榜单重放、原因解释和
  重启后未完成的会签与申诉时钟。
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clock import parse_instant
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .review_compute import (
    OUTCOME_ADVANCE,
    compute_delta,
    compute_ranking,
    entries_digest,
    rows_diff,
    validate_constraint,
    validate_rule_config,
)
from .service import DomainService


PRIVILEGED_ROLES = ("admin", "operator", "reviewer", "auditor", "publisher")
APPEAL_FACT_TYPES = ("score", "deduction", "disqualification", "judge_exclusion", "quota")


class ReviewService(DomainService):
    """协调晋级评议与申诉的权限、幂等、事务和审计规则。"""

    # ------------------------------------------------------------------
    # 通用工具
    # ------------------------------------------------------------------

    def _idempotent_result(self, connection, *, request_id: str, action: str,
                           payload: dict[str, Any],
                           create: Callable[[], tuple[str, str, dict[str, Any]]]) -> dict[str, Any]:
        """与基础服务相同的幂等语义，但返回完整响应体。"""

        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            response = json.loads(row["response_json"])
            response["replayed"] = True
            return response
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,response_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id, canonical_json(response), self._now()),
        )
        response["replayed"] = False
        return response

    def _stage_row(self, connection, stage_id: str):
        row = connection.execute("SELECT * FROM review_stages WHERE stage_id=?", (stage_id,)).fetchone()
        if row is None:
            raise NotFoundError("阶段不存在")
        return row

    def _track_row(self, connection, track_id: str):
        row = connection.execute("SELECT * FROM review_tracks WHERE track_id=?", (track_id,)).fetchone()
        if row is None:
            raise NotFoundError("赛道不存在")
        return row

    def _entry_row(self, connection, entry_id: str):
        row = connection.execute("SELECT * FROM review_entries WHERE entry_id=?", (entry_id,)).fetchone()
        if row is None:
            raise NotFoundError("作品不存在")
        return row

    def _version_row(self, connection, ranking_version_id: str):
        row = connection.execute(
            "SELECT * FROM review_ranking_versions WHERE ranking_version_id=?", (ranking_version_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("榜单版本不存在")
        return row

    def _appeal_row(self, connection, appeal_id: str):
        row = connection.execute("SELECT * FROM review_appeals WHERE appeal_id=?", (appeal_id,)).fetchone()
        if row is None:
            raise NotFoundError("申诉不存在")
        return row

    def _score_value(self, value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError("分数必须是数字")
        value = float(value)
        if not 0.0 <= value <= 100.0:
            raise ValidationError("分数必须在 0 到 100 之间")
        return value

    def _non_negative_int(self, value: Any, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValidationError(f"{field} 必须是非负整数")
        return value

    # ------------------------------------------------------------------
    # 基础登记：赛道、阶段、规则版本
    # ------------------------------------------------------------------

    def create_track(self, *, request_id: str, actor_id: str, track_id: str, name: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "track_id": track_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            track_id = self._identifier(track_id, "track_id")
            name = self._text(name, "name")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO review_tracks(track_id,name,created_at) VALUES(?,?,?)",
                        (track_id, name, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("赛道编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="review.track.created",
                             resource_type="review_track", resource_id=track_id,
                             detail={"name": name}, occurred_at=self._now())
                return "review_track", track_id, {"track_id": track_id, "name": name}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="review.create_track", payload=payload, create=create)

    def create_stage(self, *, request_id: str, actor_id: str, stage_id: str, name: str,
                     sequence: int) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "stage_id": stage_id, "name": name, "sequence": sequence}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            stage_id = self._identifier(stage_id, "stage_id")
            name = self._text(name, "name")
            sequence = self._non_negative_int(sequence, "sequence")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO review_stages(stage_id,name,sequence,status,created_at) VALUES(?,?,?,'open',?)",
                        (stage_id, name, sequence, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("阶段编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="review.stage.created",
                             resource_type="review_stage", resource_id=stage_id,
                             detail={"name": name, "sequence": sequence}, occurred_at=self._now())
                return "review_stage", stage_id, {"stage_id": stage_id, "name": name,
                                                  "sequence": sequence, "status": "open"}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="review.create_stage", payload=payload, create=create)

    def create_rule_version(self, *, request_id: str, actor_id: str, stage_id: str,
                            config: dict[str, Any]) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "stage_id": stage_id, "config": config}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            self._stage_row(connection, stage_id)
            normalized = validate_rule_config(config)

            def create() -> tuple[str, str, dict[str, Any]]:
                version = connection.execute(
                    "SELECT COALESCE(MAX(version),0)+1 AS v FROM review_rule_versions WHERE stage_id=?",
                    (stage_id,),
                ).fetchone()["v"]
                rule_version_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO review_rule_versions(rule_version_id,stage_id,version,config_json,status,created_by,created_at) "
                    "VALUES(?,?,?,?, 'draft', ?, ?)",
                    (rule_version_id, stage_id, version, canonical_json(normalized), actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="review.rule.created",
                             resource_type="review_rule_version", resource_id=rule_version_id,
                             detail={"stage_id": stage_id, "version": version, "config_hash": digest(normalized)},
                             occurred_at=self._now())
                return "review_rule_version", rule_version_id, {
                    "rule_version_id": rule_version_id, "stage_id": stage_id, "version": version,
                    "status": "draft", "config": normalized,
                }

            return self._idempotent_result(connection, request_id=request_id,
                                           action="review.create_rule_version", payload=payload, create=create)

    def freeze_stage(self, *, request_id: str, actor_id: str, stage_id: str,
                     rule_version_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "stage_id": stage_id, "rule_version_id": rule_version_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            stage = self._stage_row(connection, stage_id)
            if stage["status"] != "open":
                raise ConflictError("阶段已冻结，不能重复冻结")
            rule = connection.execute(
                "SELECT * FROM review_rule_versions WHERE rule_version_id=?", (rule_version_id,)
            ).fetchone()
            if rule is None or rule["stage_id"] != stage_id:
                raise NotFoundError("规则版本不存在")
            if rule["status"] != "draft":
                raise ConflictError("只能冻结草稿状态的规则版本")
            config = json.loads(rule["config_json"])
            due_at = parse_instant(config["score_due_at"])
            accept_late = config["late_score_policy"] == "accept"

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                connection.execute(
                    "UPDATE review_rule_versions SET status='frozen', frozen_at=? WHERE rule_version_id=?",
                    (now, rule_version_id),
                )
                connection.execute(
                    "UPDATE review_stages SET status='frozen', frozen_rule_version_id=?, frozen_at=? WHERE stage_id=?",
                    (rule_version_id, now, stage_id),
                )
                sealed = 0
                rejected = 0
                pending = connection.execute(
                    "SELECT score_id, submitted_at FROM review_scores WHERE stage_id=? AND status='submitted'",
                    (stage_id,),
                ).fetchall()
                for score in pending:
                    if accept_late or parse_instant(score["submitted_at"]) <= due_at:
                        connection.execute(
                            "UPDATE review_scores SET status='sealed', sealed_at=? WHERE score_id=?",
                            (now, score["score_id"]),
                        )
                        sealed += 1
                    else:
                        connection.execute(
                            "UPDATE review_scores SET status='rejected_late' WHERE score_id=?",
                            (score["score_id"],),
                        )
                        rejected += 1
                append_event(connection, actor_id=actor_id, action="review.stage.frozen",
                             resource_type="review_stage", resource_id=stage_id,
                             detail={"rule_version_id": rule_version_id, "rule_version": rule["version"],
                                     "sealed_scores": sealed, "rejected_late_scores": rejected},
                             occurred_at=now)
                return "review_stage", stage_id, {
                    "stage_id": stage_id, "status": "frozen", "rule_version_id": rule_version_id,
                    "rule_version": rule["version"], "sealed_scores": sealed, "rejected_late_scores": rejected,
                }

            return self._idempotent_result(connection, request_id=request_id,
                                           action="review.freeze_stage", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 作品、评委、评分事实
    # ------------------------------------------------------------------

    def register_entry(self, *, request_id: str, actor_id: str, entry_id: str, track_id: str,
                       participant_id: str, title: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "entry_id": entry_id, "track_id": track_id,
                   "participant_id": participant_id, "title": title}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if actor.role == "participant":
                if participant_id != actor_id:
                    raise PermissionDenied("参赛人只能登记自己的作品")
            else:
                self._require(actor, "admin", "operator")
            self._track_row(connection, track_id)
            participant = self._actor(connection, participant_id)
            if participant.role != "participant":
                raise ValidationError("participant_id 必须是参赛人角色")
            entry_id = self._identifier(entry_id, "entry_id")
            title = self._text(title, "title", 120)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO review_entries(entry_id,track_id,participant_id,title,status,created_at) "
                        "VALUES(?,?,?,?, 'active', ?)",
                        (entry_id, track_id, participant_id, title, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("作品编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="review.entry.registered",
                             resource_type="review_entry", resource_id=entry_id,
                             detail={"track_id": track_id, "participant_id": participant_id, "title": title},
                             occurred_at=self._now())
                return "review_entry", entry_id, {"entry_id": entry_id, "track_id": track_id,
                                                  "participant_id": participant_id, "title": title}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="review.register_entry", payload=payload, create=create)

    def enroll_entries(self, *, request_id: str, actor_id: str, stage_id: str,
                       entry_ids: list[str]) -> dict[str, Any]:
        if not isinstance(entry_ids, list) or not entry_ids:
            raise ValidationError("entry_ids 必须是非空列表")
        payload = {"actor_id": actor_id, "stage_id": stage_id, "entry_ids": entry_ids}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._stage_row(connection, stage_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                enrolled = []
                for entry_id in entry_ids:
                    entry = self._entry_row(connection, entry_id)
                    if entry["status"] != "active":
                        raise ValidationError(f"作品 {entry_id} 已退出，不能纳入阶段")
                    connection.execute(
                        "INSERT INTO review_stage_entries(stage_id,entry_id,status,enrolled_at) VALUES(?,?, 'enrolled', ?) "
                        "ON CONFLICT(stage_id,entry_id) DO UPDATE SET status='enrolled'",
                        (stage_id, entry_id, self._now()),
                    )
                    enrolled.append(entry_id)
                append_event(connection, actor_id=actor_id, action="review.stage.enrolled",
                             resource_type="review_stage", resource_id=stage_id,
                             detail={"entry_ids": enrolled}, occurred_at=self._now())
                return "review_stage", stage_id, {"stage_id": stage_id, "enrolled": enrolled}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="review.enroll_entries", payload=payload, create=create)

    def assign_judge(self, *, request_id: str, actor_id: str, stage_id: str, track_id: str,
                     judge_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "stage_id": stage_id, "track_id": track_id, "judge_id": judge_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            self._stage_row(connection, stage_id)
            self._track_row(connection, track_id)
            judge = self._actor(connection, judge_id)
            if judge.role != "judge":
                raise ValidationError("judge_id 必须是评委角色")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO review_stage_judges(stage_id,track_id,judge_id,valid,reason,updated_at) "
                    "VALUES(?,?,?,1,NULL,?) "
                    "ON CONFLICT(stage_id,track_id,judge_id) DO UPDATE SET valid=1, reason=NULL, updated_at=excluded.updated_at",
                    (stage_id, track_id, judge_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="review.judge.assigned",
                             resource_type="review_stage", resource_id=stage_id,
                             detail={"track_id": track_id, "judge_id": judge_id}, occurred_at=self._now())
                return "review_stage", stage_id, {"stage_id": stage_id, "track_id": track_id,
                                                  "judge_id": judge_id, "valid": True}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="review.assign_judge", payload=payload, create=create)

    def exclude_judge(self, *, request_id: str, actor_id: str, stage_id: str, track_id: str,
                      judge_id: str, reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "stage_id": stage_id, "track_id": track_id,
                   "judge_id": judge_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            self._stage_row(connection, stage_id)
            reason = self._text(reason, "reason", 300)

            def create() -> tuple[str, str, dict[str, Any]]:
                updated = self._set_judge_valid_tx(connection, actor_id=actor_id, stage_id=stage_id,
                                                   track_id=track_id, judge_id=judge_id, valid=False,
                                                   reason=reason)
                if not updated:
                    raise NotFoundError("评委指派不存在")
                return "review_stage", stage_id, {"stage_id": stage_id, "track_id": track_id,
                                                  "judge_id": judge_id, "valid": False}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="review.exclude_judge", payload=payload, create=create)

    def _set_judge_valid_tx(self, connection, *, actor_id: str, stage_id: str, track_id: str,
                            judge_id: str, valid: bool, reason: str | None) -> bool:
        cursor = connection.execute(
            "UPDATE review_stage_judges SET valid=?, reason=?, updated_at=? "
            "WHERE stage_id=? AND track_id=? AND judge_id=?",
            (1 if valid else 0, reason, self._now(), stage_id, track_id, judge_id),
        )
        if cursor.rowcount:
            append_event(connection, actor_id=actor_id,
                         action="review.judge.restored" if valid else "review.judge.excluded",
                         resource_type="review_stage", resource_id=stage_id,
                         detail={"track_id": track_id, "judge_id": judge_id, "reason": reason},
                         occurred_at=self._now())
        return bool(cursor.rowcount)

    def submit_score(self, *, request_id: str, actor_id: str, stage_id: str, entry_id: str,
                     dimension: str, value: float) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "stage_id": stage_id, "entry_id": entry_id,
                   "dimension": dimension, "value": value}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "judge")
            stage = self._stage_row(connection, stage_id)
            if stage["status"] != "open":
                raise ConflictError("阶段已冻结，不能提交分数")
            entry = self._entry_row(connection, entry_id)
            if entry["status"] != "active":
                raise ValidationError("作品已退出，不能评分")
            enrolled = connection.execute(
                "SELECT 1 FROM review_stage_entries WHERE stage_id=? AND entry_id=? AND status='enrolled'",
                (stage_id, entry_id),
            ).fetchone()
            if enrolled is None:
                raise ValidationError("作品未纳入该阶段")
            assignment = connection.execute(
                "SELECT valid FROM review_stage_judges WHERE stage_id=? AND track_id=? AND judge_id=?",
                (stage_id, entry["track_id"], actor_id),
            ).fetchone()
            if assignment is None or not assignment["valid"]:
                raise PermissionDenied("评委未获该赛道授权")
            dimension = self._text(dimension, "dimension", 40)
            value = self._score_value(value)

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT score_id FROM review_scores WHERE stage_id=? AND entry_id=? AND judge_id=? "
                    "AND dimension=? AND status='submitted'",
                    (stage_id, entry_id, actor_id, dimension),
                ).fetchone()
                if existing:
                    score_id = existing["score_id"]
                    connection.execute(
                        "UPDATE review_scores SET value=?, submitted_at=? WHERE score_id=?",
                        (value, self._now(), score_id),
                    )
                    action = "review.score.updated"
                else:
                    score_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO review_scores(score_id,stage_id,track_id,entry_id,judge_id,dimension,value,status,submitted_at) "
                        "VALUES(?,?,?,?,?,?,?, 'submitted', ?)",
                        (score_id, stage_id, entry["track_id"], entry_id, actor_id, dimension, value, self._now()),
                    )
                    action = "review.score.submitted"
                append_event(connection, actor_id=actor_id, action=action,
                             resource_type="review_score", resource_id=score_id,
                             detail={"stage_id": stage_id, "entry_id": entry_id,
                                     "dimension": dimension, "value": value},
                             occurred_at=self._now())
                return "review_score", score_id, {"score_id": score_id, "entry_id": entry_id,
                                                  "dimension": dimension, "value": value,
                                                  "status": "submitted"}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="review.submit_score", payload=payload, create=create)

    def revoke_score(self, *, request_id: str, actor_id: str, score_id: str,
                     reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "score_id": score_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            reason = self._text(reason, "reason", 300)

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute("SELECT * FROM review_scores WHERE score_id=?", (score_id,)).fetchone()
                if row is None:
                    raise NotFoundError("评分不存在")
                self._revoke_score_tx(connection, actor_id=actor_id, row=row, reason=reason)
                return "review_score", score_id, {"score_id": score_id, "status": "revoked"}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="review.revoke_score", payload=payload, create=create)

    def _revoke_score_tx(self, connection, *, actor_id: str, row, reason: str) -> None:
        if row["status"] != "sealed":
            raise ConflictError("只有已封存的分数可以撤销")
        connection.execute(
            "UPDATE review_scores SET status='revoked', revoked_at=?, revoke_reason=? WHERE score_id=?",
            (self._now(), reason, row["score_id"]),
        )
        append_event(connection, actor_id=actor_id, action="review.score.revoked",
                     resource_type="review_score", resource_id=row["score_id"],
                     detail={"stage_id": row["stage_id"], "entry_id": row["entry_id"], "reason": reason},
                     occurred_at=self._now())

    # ------------------------------------------------------------------
    # 扣分、资格取消、配额与约束
    # ------------------------------------------------------------------

    def add_deduction(self, *, request_id: str, actor_id: str, stage_id: str, entry_id: str,
                      points: float, reason: str, basis: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "stage_id": stage_id, "entry_id": entry_id,
                   "points": points, "reason": reason, "basis": basis}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._stage_row(connection, stage_id)
            self._entry_row(connection, entry_id)
            if isinstance(points, bool) or not isinstance(points, (int, float)) or float(points) < 0:
                raise ValidationError("扣分必须是非负数字")
            points = float(points)
            reason = self._text(reason, "reason", 300)
            basis = self._text(basis, "basis", 300)

            def create() -> tuple[str, str, dict[str, Any]]:
                deduction_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO review_deductions(deduction_id,stage_id,entry_id,points,reason,basis,status,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?, 'active', ?, ?)",
                    (deduction_id, stage_id, entry_id, points, reason, basis, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="review.deduction.added",
                             resource_type="review_deduction", resource_id=deduction_id,
                             detail={"stage_id": stage_id, "entry_id": entry_id, "points": points,
                                     "reason": reason, "basis": basis},
                             occurred_at=self._now())
                return "review_deduction", deduction_id, {"deduction_id": deduction_id, "entry_id": entry_id,
                                                          "points": points, "status": "active"}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="review.add_deduction", payload=payload, create=create)

    def withdraw_deduction(self, *, request_id: str, actor_id: str, deduction_id: str,
                           reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "deduction_id": deduction_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            reason = self._text(reason, "reason", 300)

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT * FROM review_deductions WHERE deduction_id=?", (deduction_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError("扣分记录不存在")
                self._withdraw_deduction_tx(connection, actor_id=actor_id, row=row, reason=reason)
                return "review_deduction", deduction_id, {"deduction_id": deduction_id, "status": "withdrawn"}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="review.withdraw_deduction", payload=payload, create=create)

    def _withdraw_deduction_tx(self, connection, *, actor_id: str, row, reason: str) -> None:
        if row["status"] != "active":
            raise ConflictError("扣分记录已撤回")
        connection.execute(
            "UPDATE review_deductions SET status='withdrawn', withdrawn_at=?, withdrawn_by=?, withdraw_reason=? "
            "WHERE deduction_id=?",
            (self._now(), actor_id, reason, row["deduction_id"]),
        )
        append_event(connection, actor_id=actor_id, action="review.deduction.withdrawn",
                     resource_type="review_deduction", resource_id=row["deduction_id"],
                     detail={"stage_id": row["stage_id"], "entry_id": row["entry_id"], "reason": reason},
                     occurred_at=self._now())

    def disqualify_entry(self, *, request_id: str, actor_id: str, stage_id: str, entry_id: str,
                         reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "stage_id": stage_id, "entry_id": entry_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            self._stage_row(connection, stage_id)
            self._entry_row(connection, entry_id)
            reason = self._text(reason, "reason", 300)

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT 1 FROM review_disqualifications WHERE stage_id=? AND entry_id=? AND status='active'",
                    (stage_id, entry_id),
                ).fetchone()
                if existing:
                    raise ConflictError("该作品已存在有效的资格取消记录")
                disqualification_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO review_disqualifications(disqualification_id,stage_id,entry_id,reason,status,created_by,created_at) "
                    "VALUES(?,?,?,?, 'active', ?, ?)",
                    (disqualification_id, stage_id, entry_id, reason, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="review.entry.disqualified",
                             resource_type="review_disqualification", resource_id=disqualification_id,
                             detail={"stage_id": stage_id, "entry_id": entry_id, "reason": reason},
                             occurred_at=self._now())
                return "review_disqualification", disqualification_id, {
                    "disqualification_id": disqualification_id, "entry_id": entry_id, "status": "active",
                }

            return self._idempotent_result(connection, request_id=request_id,
                                           action="review.disqualify_entry", payload=payload, create=create)

    def lift_disqualification(self, *, request_id: str, actor_id: str, disqualification_id: str,
                              reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "disqualification_id": disqualification_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            reason = self._text(reason, "reason", 300)

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT * FROM review_disqualifications WHERE disqualification_id=?", (disqualification_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError("资格取消记录不存在")
                self._lift_disqualification_tx(connection, actor_id=actor_id, row=row, reason=reason)
                return "review_disqualification", disqualification_id, {
                    "disqualification_id": disqualification_id, "status": "lifted",
                }

            return self._idempotent_result(connection, request_id=request_id,
                                           action="review.lift_disqualification", payload=payload, create=create)

    def _lift_disqualification_tx(self, connection, *, actor_id: str, row, reason: str) -> None:
        if row["status"] != "active":
            raise ConflictError("资格取消记录已解除")
        connection.execute(
            "UPDATE review_disqualifications SET status='lifted', lifted_at=?, lifted_by=?, lift_reason=? "
            "WHERE disqualification_id=?",
            (self._now(), actor_id, reason, row["disqualification_id"]),
        )
        append_event(connection, actor_id=actor_id, action="review.disqualification.lifted",
                     resource_type="review_disqualification", resource_id=row["disqualification_id"],
                     detail={"stage_id": row["stage_id"], "entry_id": row["entry_id"], "reason": reason},
                     occurred_at=self._now())

    def set_quota(self, *, request_id: str, actor_id: str, stage_id: str, track_id: str,
                  advance_count: int) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "stage_id": stage_id, "track_id": track_id,
                   "advance_count": advance_count}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            self._stage_row(connection, stage_id)
            self._track_row(connection, track_id)
            advance_count = self._non_negative_int(advance_count, "advance_count")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO review_track_quotas(stage_id,track_id,advance_count,updated_by,updated_at) "
                    "VALUES(?,?,?,?,?) "
                    "ON CONFLICT(stage_id,track_id) DO UPDATE SET advance_count=excluded.advance_count, "
                    "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                    (stage_id, track_id, advance_count, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="review.quota.set",
                             resource_type="review_stage", resource_id=stage_id,
                             detail={"track_id": track_id, "advance_count": advance_count},
                             occurred_at=self._now())
                return "review_stage", stage_id, {"stage_id": stage_id, "track_id": track_id,
                                                  "advance_count": advance_count}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="review.set_quota", payload=payload, create=create)

    def add_award_constraint(self, *, request_id: str, actor_id: str, stage_id: str, kind: str,
                             params: dict[str, Any]) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "stage_id": stage_id, "kind": kind, "params": params}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            self._stage_row(connection, stage_id)
            normalized = validate_constraint(kind, params)

            def create() -> tuple[str, str, dict[str, Any]]:
                constraint_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO review_award_constraints(constraint_id,stage_id,kind,params_json,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (constraint_id, stage_id, normalized["kind"], canonical_json(normalized["params"]),
                     actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="review.constraint.added",
                             resource_type="review_award_constraint", resource_id=constraint_id,
                             detail={"stage_id": stage_id, "kind": normalized["kind"],
                                     "params": normalized["params"]},
                             occurred_at=self._now())
                return "review_award_constraint", constraint_id, {
                    "constraint_id": constraint_id, "stage_id": stage_id,
                    "kind": normalized["kind"], "params": normalized["params"],
                }

            return self._idempotent_result(connection, request_id=request_id,
                                           action="review.add_award_constraint", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 榜单生成、会签、发布
    # ------------------------------------------------------------------

    def _build_snapshot(self, connection, stage_id: str) -> dict[str, Any]:
        stage = self._stage_row(connection, stage_id)
        if stage["status"] == "open":
            raise ConflictError("阶段尚未冻结，不能生成榜单")
        rule = connection.execute(
            "SELECT * FROM review_rule_versions WHERE rule_version_id=?", (stage["frozen_rule_version_id"],)
        ).fetchone()
        if rule is None:
            raise ConflictError("阶段缺少冻结的规则版本")
        config = json.loads(rule["config_json"])
        enrolled = [
            {"entry_id": row["entry_id"], "track_id": row["track_id"],
             "participant_id": row["participant_id"], "title": row["title"],
             "enrolled_at": row["enrolled_at"]}
            for row in connection.execute(
                "SELECT se.entry_id, se.enrolled_at, e.track_id, e.participant_id, e.title "
                "FROM review_stage_entries se JOIN review_entries e ON e.entry_id=se.entry_id "
                "WHERE se.stage_id=? AND se.status='enrolled' AND e.status='active' ORDER BY se.entry_id",
                (stage_id,),
            )
        ]
        entry_ids = [item["entry_id"] for item in enrolled]
        valid_judges: dict[str, list[str]] = {}
        for row in connection.execute(
            "SELECT track_id, judge_id FROM review_stage_judges WHERE stage_id=? AND valid=1 "
            "ORDER BY track_id, judge_id",
            (stage_id,),
        ):
            valid_judges.setdefault(row["track_id"], []).append(row["judge_id"])
        quotas = {
            row["track_id"]: row["advance_count"]
            for row in connection.execute("SELECT track_id, advance_count FROM review_track_quotas WHERE stage_id=?",
                                          (stage_id,))
        }
        tracks = sorted({item["track_id"] for item in enrolled})
        for track_id in tracks:
            if not valid_judges.get(track_id):
                raise ConflictError(f"赛道 {track_id} 没有有效评委")
            if track_id not in quotas:
                raise ConflictError(f"赛道 {track_id} 缺少晋级名额配置")

        def for_entries(query: str) -> list[dict[str, Any]]:
            if not entry_ids:
                return []
            marks = ",".join("?" for _ in entry_ids)
            return [dict(row) for row in connection.execute(
                query.format(marks=marks), (stage_id, *entry_ids)
            )]

        scores = for_entries(
            "SELECT score_id, entry_id, track_id, judge_id, dimension, value, status, submitted_at "
            "FROM review_scores WHERE stage_id=? AND entry_id IN ({marks}) ORDER BY score_id"
        )
        deductions = for_entries(
            "SELECT deduction_id, entry_id, points, reason, basis FROM review_deductions "
            "WHERE stage_id=? AND status='active' AND entry_id IN ({marks}) ORDER BY deduction_id"
        )
        disqualified = for_entries(
            "SELECT disqualification_id, entry_id, reason FROM review_disqualifications "
            "WHERE stage_id=? AND status='active' AND entry_id IN ({marks}) ORDER BY disqualification_id"
        )
        constraints = [
            {"kind": row["kind"], "params": json.loads(row["params_json"])}
            for row in connection.execute(
                "SELECT kind, params_json FROM review_award_constraints WHERE stage_id=? ORDER BY constraint_id",
                (stage_id,),
            )
        ]
        return {
            "stage_id": stage_id,
            "rule_version_id": rule["rule_version_id"],
            "rule_version": rule["version"],
            "rule": config,
            "enrolled": enrolled,
            "valid_judges": valid_judges,
            "scores": scores,
            "deductions": deductions,
            "disqualified": disqualified,
            "quotas": quotas,
            "constraints": constraints,
        }

    def _entries_of(self, connection, ranking_version_id: str) -> list[dict[str, Any]]:
        return [
            {"entry_id": row["entry_id"], "track_id": row["track_id"], "rank": row["rank"],
             "total_score": row["total_score"], "outcome": row["outcome"],
             "award_level": row["award_level"], "explanation": json.loads(row["explanation_json"])}
            for row in connection.execute(
                "SELECT * FROM review_ranking_entries WHERE ranking_version_id=? ORDER BY track_id, entry_id",
                (ranking_version_id,),
            )
        ]

    def _generate_tx(self, connection, *, actor_id: str, stage_id: str,
                     trigger_type: str | None, trigger_id: str | None) -> str:
        snapshot = self._build_snapshot(connection, stage_id)
        rows = compute_ranking(snapshot)
        version_no = connection.execute(
            "SELECT COALESCE(MAX(version),0)+1 AS v FROM review_ranking_versions WHERE stage_id=?",
            (stage_id,),
        ).fetchone()["v"]
        connection.execute(
            "UPDATE review_ranking_versions SET status='superseded' WHERE stage_id=? AND status='candidate'",
            (stage_id,),
        )
        published = connection.execute(
            "SELECT * FROM review_ranking_versions WHERE stage_id=? AND status='published'", (stage_id,)
        ).fetchone()
        delta = None
        if published is not None:
            old_rows = self._entries_of(connection, published["ranking_version_id"])
            delta = compute_delta(published["version"], version_no, old_rows, rows)
        ranking_version_id = uuid.uuid4().hex
        now = self._now()
        connection.execute(
            "INSERT INTO review_ranking_versions(ranking_version_id,stage_id,version,status,rule_version_id,"
            "input_hash,inputs_json,entries_hash,delta_json,trigger_type,trigger_id,generated_by,generated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ranking_version_id, stage_id, version_no, "candidate", snapshot["rule_version_id"],
             digest(snapshot), canonical_json(snapshot), entries_digest(rows),
             canonical_json(delta) if delta else None, trigger_type, trigger_id, actor_id, now),
        )
        for row in rows:
            connection.execute(
                "INSERT INTO review_ranking_entries(ranking_version_id,entry_id,track_id,rank,total_score,"
                "outcome,award_level,explanation_json) VALUES(?,?,?,?,?,?,?,?)",
                (ranking_version_id, row["entry_id"], row["track_id"], row["rank"], row["total_score"],
                 row["outcome"], row["award_level"], canonical_json(row["explanation"])),
            )
        append_event(connection, actor_id=actor_id, action="review.ranking.generated",
                     resource_type="review_ranking", resource_id=ranking_version_id,
                     detail={"stage_id": stage_id, "version": version_no, "input_hash": digest(snapshot),
                             "entries_hash": entries_digest(rows),
                             "trigger_type": trigger_type, "trigger_id": trigger_id},
                     occurred_at=now)
        return ranking_version_id

    def generate_ranking(self, *, request_id: str, actor_id: str, stage_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "stage_id": stage_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            self._stage_row(connection, stage_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                ranking_version_id = self._generate_tx(connection, actor_id=actor_id, stage_id=stage_id,
                                                       trigger_type=None, trigger_id=None)
                return "review_ranking", ranking_version_id, self._version_payload(connection, ranking_version_id)

            return self._idempotent_result(connection, request_id=request_id,
                                           action="review.generate_ranking", payload=payload, create=create)

    def countersign_ranking(self, *, request_id: str, actor_id: str, ranking_version_id: str,
                            sign_role: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "ranking_version_id": ranking_version_id, "sign_role": sign_role}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            row = self._version_row(connection, ranking_version_id)
            if sign_role == "review":
                self._require(actor, "reviewer", "admin")
            elif sign_role == "publish":
                self._require(actor, "publisher", "admin")
            else:
                raise ValidationError("sign_role 必须是 review 或 publish")
            if row["status"] != "candidate":
                raise ConflictError("只有候选榜可以会签")
            if actor_id == row["generated_by"]:
                raise PermissionDenied("榜单生成人不能参与会签")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                if sign_role == "review":
                    if row["review_signed_by"]:
                        raise ConflictError("复核会签已完成")
                    if row["publish_signed_by"] == actor_id:
                        raise PermissionDenied("复核人与发布人必须相互独立")
                    connection.execute(
                        "UPDATE review_ranking_versions SET review_signed_by=?, review_signed_at=? "
                        "WHERE ranking_version_id=?",
                        (actor_id, now, ranking_version_id),
                    )
                else:
                    if row["publish_signed_by"]:
                        raise ConflictError("发布会签已完成")
                    if row["review_signed_by"] == actor_id:
                        raise PermissionDenied("复核人与发布人必须相互独立")
                    connection.execute(
                        "UPDATE review_ranking_versions SET publish_signed_by=?, publish_signed_at=? "
                        "WHERE ranking_version_id=?",
                        (actor_id, now, ranking_version_id),
                    )
                append_event(connection, actor_id=actor_id, action="review.ranking.countersigned",
                             resource_type="review_ranking", resource_id=ranking_version_id,
                             detail={"stage_id": row["stage_id"], "version": row["version"],
                                     "sign_role": sign_role},
                             occurred_at=now)
                return "review_ranking", ranking_version_id, self._version_payload(connection, ranking_version_id)

            return self._idempotent_result(connection, request_id=request_id,
                                           action="review.countersign_ranking", payload=payload, create=create)

    def publish_ranking(self, *, request_id: str, actor_id: str,
                        ranking_version_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "ranking_version_id": ranking_version_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            row = self._version_row(connection, ranking_version_id)
            if row["status"] != "candidate":
                raise ConflictError("只有候选榜可以发布")
            if not row["review_signed_by"] or not row["publish_signed_by"]:
                raise ConflictError("会签未完成，不能发布")
            if actor_id != row["publish_signed_by"]:
                raise PermissionDenied("必须由发布会签人执行发布")
            rule = connection.execute(
                "SELECT config_json FROM review_rule_versions WHERE rule_version_id=?", (row["rule_version_id"],)
            ).fetchone()
            window = json.loads(rule["config_json"])["appeal_window_seconds"]

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                deadline = (self.clock.now() + timedelta(seconds=window)).isoformat().replace("+00:00", "Z")
                previous = connection.execute(
                    "SELECT * FROM review_ranking_versions WHERE stage_id=? AND status='published'",
                    (row["stage_id"],),
                ).fetchone()
                affected: list[dict[str, Any]] = []
                if previous is not None:
                    connection.execute(
                        "UPDATE review_ranking_versions SET status='superseded' WHERE ranking_version_id=?",
                        (previous["ranking_version_id"],),
                    )
                    delta = json.loads(row["delta_json"]) if row["delta_json"] else compute_delta(
                        previous["version"], row["version"],
                        self._entries_of(connection, previous["ranking_version_id"]),
                        self._entries_of(connection, ranking_version_id),
                    )
                    changed = {item["entry_id"] for item in delta["rank_changes"]}
                    for notification in connection.execute(
                        "SELECT * FROM review_notifications WHERE ranking_version_id=? AND status='sent'",
                        (previous["ranking_version_id"],),
                    ):
                        if notification["entry_id"] in changed:
                            connection.execute(
                                "UPDATE review_notifications SET status='affected', affected_by_version_id=? "
                                "WHERE notification_id=?",
                                (ranking_version_id, notification["notification_id"]),
                            )
                            affected.append({"notification_id": notification["notification_id"],
                                             "entry_id": notification["entry_id"]})
                    delta["affected_notifications"] = affected
                    connection.execute(
                        "UPDATE review_ranking_versions SET delta_json=? WHERE ranking_version_id=?",
                        (canonical_json(delta), ranking_version_id),
                    )
                connection.execute(
                    "UPDATE review_ranking_versions SET status='superseded' "
                    "WHERE stage_id=? AND status='candidate' AND ranking_version_id<>?",
                    (row["stage_id"], ranking_version_id),
                )
                connection.execute(
                    "UPDATE review_ranking_versions SET status='published', published_by=?, published_at=?, "
                    "appeal_deadline=? WHERE ranking_version_id=?",
                    (actor_id, now, deadline, ranking_version_id),
                )
                connection.execute(
                    "UPDATE review_stages SET status='published' WHERE stage_id=?", (row["stage_id"],)
                )
                append_event(connection, actor_id=actor_id, action="review.ranking.published",
                             resource_type="review_ranking", resource_id=ranking_version_id,
                             detail={"stage_id": row["stage_id"], "version": row["version"],
                                     "appeal_deadline": deadline, "affected_notifications": affected},
                             occurred_at=now)
                return "review_ranking", ranking_version_id, self._version_payload(connection, ranking_version_id)

            return self._idempotent_result(connection, request_id=request_id,
                                           action="review.publish_ranking", payload=payload, create=create)

    def issue_notifications(self, *, request_id: str, actor_id: str,
                            ranking_version_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "ranking_version_id": ranking_version_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            row = self._version_row(connection, ranking_version_id)
            if row["status"] != "published":
                raise ConflictError("只有已发布榜单可以发送通知")

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = [
                    self._notification_dict(item) for item in connection.execute(
                        "SELECT * FROM review_notifications WHERE ranking_version_id=? ORDER BY notification_id",
                        (ranking_version_id,),
                    )
                ]
                if existing:
                    return "review_ranking", ranking_version_id, {
                        "ranking_version_id": ranking_version_id, "issued": False, "notifications": existing,
                    }
                now = self._now()
                items = []
                for entry in self._entries_of(connection, ranking_version_id):
                    kind = "award" if entry["award_level"] else (
                        "advance" if entry["outcome"] == OUTCOME_ADVANCE else entry["outcome"])
                    notification_id = uuid.uuid4().hex
                    snapshot = canonical_json({"outcome": entry["outcome"], "rank": entry["rank"],
                                               "award_level": entry["award_level"],
                                               "total_score": entry["total_score"]})
                    connection.execute(
                        "INSERT INTO review_notifications(notification_id,ranking_version_id,stage_id,entry_id,"
                        "kind,outcome_snapshot,status,sent_by,sent_at) VALUES(?,?,?,?,?,?, 'sent', ?, ?)",
                        (notification_id, ranking_version_id, row["stage_id"], entry["entry_id"],
                         kind, snapshot, actor_id, now),
                    )
                    items.append({"notification_id": notification_id, "entry_id": entry["entry_id"],
                                  "kind": kind, "status": "sent"})
                append_event(connection, actor_id=actor_id, action="review.notifications.issued",
                             resource_type="review_ranking", resource_id=ranking_version_id,
                             detail={"stage_id": row["stage_id"], "count": len(items)}, occurred_at=now)
                return "review_ranking", ranking_version_id, {
                    "ranking_version_id": ranking_version_id, "issued": True, "notifications": items,
                }

            return self._idempotent_result(connection, request_id=request_id,
                                           action="review.issue_notifications", payload=payload, create=create)

    def _notification_dict(self, row) -> dict[str, Any]:
        return {"notification_id": row["notification_id"], "entry_id": row["entry_id"],
                "kind": row["kind"], "status": row["status"], "sent_at": row["sent_at"],
                "affected_by_version_id": row["affected_by_version_id"]}

    # ------------------------------------------------------------------
    # 申诉与裁决
    # ------------------------------------------------------------------

    def _validate_appeal_fact(self, connection, *, stage_id: str, entry,
                              fact_type: str, fact_id: str) -> dict[str, Any]:
        entry_id = entry["entry_id"]
        if fact_type == "score":
            row = connection.execute("SELECT * FROM review_scores WHERE score_id=?", (fact_id,)).fetchone()
            if row is None or row["entry_id"] != entry_id or row["stage_id"] != stage_id:
                raise ValidationError("申诉引用的评分事实无效")
            return {"fact_type": fact_type, "fact_id": fact_id, "score_status": row["status"]}
        if fact_type == "deduction":
            row = connection.execute(
                "SELECT * FROM review_deductions WHERE deduction_id=?", (fact_id,)).fetchone()
            if row is None or row["entry_id"] != entry_id or row["stage_id"] != stage_id:
                raise ValidationError("申诉引用的扣分事实无效")
            return {"fact_type": fact_type, "fact_id": fact_id, "deduction_status": row["status"]}
        if fact_type == "disqualification":
            row = connection.execute(
                "SELECT * FROM review_disqualifications WHERE disqualification_id=?", (fact_id,)).fetchone()
            if row is None or row["entry_id"] != entry_id or row["stage_id"] != stage_id:
                raise ValidationError("申诉引用的资格事实无效")
            return {"fact_type": fact_type, "fact_id": fact_id, "disqualification_status": row["status"]}
        if fact_type == "judge_exclusion":
            parts = str(fact_id).split(":")
            if len(parts) != 2:
                raise ValidationError("评委事实编号格式为 track_id:judge_id")
            track_id, judge_id = parts
            if track_id != entry["track_id"]:
                raise ValidationError("申诉引用的评委事实不属于该作品赛道")
            row = connection.execute(
                "SELECT * FROM review_stage_judges WHERE stage_id=? AND track_id=? AND judge_id=?",
                (stage_id, track_id, judge_id),
            ).fetchone()
            if row is None:
                raise ValidationError("申诉引用的评委事实不存在")
            return {"fact_type": fact_type, "fact_id": fact_id, "judge_valid": bool(row["valid"])}
        if fact_type == "quota":
            if fact_id != entry["track_id"]:
                raise ValidationError("申诉引用的配额事实不属于该作品赛道")
            row = connection.execute(
                "SELECT 1 FROM review_track_quotas WHERE stage_id=? AND track_id=?",
                (stage_id, fact_id),
            ).fetchone()
            if row is None:
                raise ValidationError("申诉引用的配额事实不存在")
            return {"fact_type": fact_type, "fact_id": fact_id}
        raise ValidationError("未知的申诉事实类型")

    def file_appeal(self, *, request_id: str, actor_id: str, ranking_version_id: str, entry_id: str,
                    fact_type: str, fact_id: str, reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "ranking_version_id": ranking_version_id, "entry_id": entry_id,
                   "fact_type": fact_type, "fact_id": fact_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "participant")
            version = self._version_row(connection, ranking_version_id)
            if version["status"] != "published":
                raise ConflictError("只能针对当前已发布的榜单提出申诉")
            if self.clock.now() > parse_instant(version["appeal_deadline"]):
                raise ValidationError("申诉时限已过")
            entry = self._entry_row(connection, entry_id)
            if entry["participant_id"] != actor_id:
                raise PermissionDenied("只能为自己的作品申诉")
            ranked = connection.execute(
                "SELECT 1 FROM review_ranking_entries WHERE ranking_version_id=? AND entry_id=?",
                (ranking_version_id, entry_id),
            ).fetchone()
            if ranked is None:
                raise ValidationError("该作品不在此榜单中")
            reason = self._text(reason, "reason", 500)
            fact = self._validate_appeal_fact(connection, stage_id=version["stage_id"], entry=entry,
                                              fact_type=fact_type, fact_id=fact_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                appeal_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO review_appeals(appeal_id,ranking_version_id,stage_id,entry_id,appellant_id,"
                    "fact_type,fact_id,reason,status,filed_at) VALUES(?,?,?,?,?,?,?,?, 'filed', ?)",
                    (appeal_id, ranking_version_id, version["stage_id"], entry_id, actor_id,
                     fact_type, fact_id, reason, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="review.appeal.filed",
                             resource_type="review_appeal", resource_id=appeal_id,
                             detail={"ranking_version_id": ranking_version_id, "entry_id": entry_id,
                                     "fact": fact, "reason": reason},
                             occurred_at=self._now())
                return "review_appeal", appeal_id, {
                    "appeal_id": appeal_id, "ranking_version_id": ranking_version_id, "entry_id": entry_id,
                    "fact_type": fact_type, "fact_id": fact_id, "status": "filed",
                }

            return self._idempotent_result(connection, request_id=request_id,
                                           action="review.file_appeal", payload=payload, create=create)

    def adjudicate_appeal(self, *, request_id: str, actor_id: str, appeal_id: str, decision: str,
                          note: str, corrections: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        corrections = corrections or []
        payload = {"actor_id": actor_id, "appeal_id": appeal_id, "decision": decision,
                   "note": note, "corrections": corrections}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            appeal = self._appeal_row(connection, appeal_id)
            if appeal["status"] != "filed":
                raise ConflictError("申诉已裁决")
            if appeal["appellant_id"] == actor_id:
                raise PermissionDenied("不能裁决自己的申诉")
            if decision not in ("upheld", "rejected"):
                raise ValidationError("decision 必须是 upheld 或 rejected")
            note = self._text(note, "note", 500)
            if decision == "upheld" and not corrections:
                raise ValidationError("维持申诉必须给出纠正措施")

            def create() -> tuple[str, str, dict[str, Any]]:
                applied: list[dict[str, Any]] = []
                resulting_version_id = None
                if decision == "upheld":
                    for correction in corrections:
                        applied.append(self._apply_correction(connection, actor_id=actor_id,
                                                              stage_id=appeal["stage_id"],
                                                              correction=correction))
                    resulting_version_id = self._generate_tx(connection, actor_id=actor_id,
                                                             stage_id=appeal["stage_id"],
                                                             trigger_type="appeal", trigger_id=appeal_id)
                status = "adjudicated_upheld" if decision == "upheld" else "adjudicated_rejected"
                connection.execute(
                    "UPDATE review_appeals SET status=?, adjudicated_by=?, adjudicated_at=?, decision_note=?, "
                    "corrections_json=?, resulting_version_id=? WHERE appeal_id=?",
                    (status, actor_id, self._now(), note, canonical_json(applied),
                     resulting_version_id, appeal_id),
                )
                append_event(connection, actor_id=actor_id, action="review.appeal.adjudicated",
                             resource_type="review_appeal", resource_id=appeal_id,
                             detail={"decision": decision, "note": note, "corrections": applied,
                                     "resulting_version_id": resulting_version_id},
                             occurred_at=self._now())
                response: dict[str, Any] = {
                    "appeal_id": appeal_id, "status": status, "decision_note": note,
                    "corrections": applied, "resulting_version_id": resulting_version_id,
                }
                if resulting_version_id:
                    response["ranking"] = self._version_payload(connection, resulting_version_id)
                return "review_appeal", appeal_id, response

            return self._idempotent_result(connection, request_id=request_id,
                                           action="review.adjudicate_appeal", payload=payload, create=create)

    def _apply_correction(self, connection, *, actor_id: str, stage_id: str,
                          correction: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(correction, dict):
            raise ValidationError("纠正措施必须是对象")
        action = correction.get("action")
        reason = str(correction.get("reason", "")).strip() or "申诉裁决纠正"
        now = self._now()
        if action == "revoke_score":
            row = connection.execute("SELECT * FROM review_scores WHERE score_id=?",
                                     (correction.get("score_id"),)).fetchone()
            if row is None or row["stage_id"] != stage_id:
                raise ValidationError("纠正措施引用的评分不存在")
            self._revoke_score_tx(connection, actor_id=actor_id, row=row, reason=reason)
            return {"action": action, "score_id": row["score_id"]}
        if action == "accept_late_score":
            row = connection.execute("SELECT * FROM review_scores WHERE score_id=?",
                                     (correction.get("score_id"),)).fetchone()
            if row is None or row["stage_id"] != stage_id:
                raise ValidationError("纠正措施引用的评分不存在")
            if row["status"] != "rejected_late":
                raise ConflictError("该分数不是迟交被拒状态")
            connection.execute(
                "UPDATE review_scores SET status='sealed', sealed_at=? WHERE score_id=?",
                (now, row["score_id"]),
            )
            append_event(connection, actor_id=actor_id, action="review.score.late_accepted",
                         resource_type="review_score", resource_id=row["score_id"],
                         detail={"stage_id": stage_id, "entry_id": row["entry_id"], "reason": reason},
                         occurred_at=now)
            return {"action": action, "score_id": row["score_id"]}
        if action == "correct_score":
            new_value = self._score_value(correction.get("new_value"))
            row = connection.execute("SELECT * FROM review_scores WHERE score_id=?",
                                     (correction.get("score_id"),)).fetchone()
            if row is None or row["stage_id"] != stage_id:
                raise ValidationError("纠正措施引用的评分不存在")
            if row["status"] not in ("sealed", "rejected_late"):
                raise ConflictError("只有已封存或迟交被拒的分数可以更正")
            connection.execute(
                "UPDATE review_scores SET status='revoked', revoked_at=?, revoke_reason=? WHERE score_id=?",
                (now, f"更正：{reason}", row["score_id"]),
            )
            new_score_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO review_scores(score_id,stage_id,track_id,entry_id,judge_id,dimension,value,status,"
                "submitted_at,sealed_at,corrected_from) VALUES(?,?,?,?,?,?,?, 'sealed', ?, ?, ?)",
                (new_score_id, row["stage_id"], row["track_id"], row["entry_id"], row["judge_id"],
                 row["dimension"], new_value, now, now, row["score_id"]),
            )
            append_event(connection, actor_id=actor_id, action="review.score.corrected",
                         resource_type="review_score", resource_id=new_score_id,
                         detail={"stage_id": stage_id, "entry_id": row["entry_id"],
                                 "corrected_from": row["score_id"], "value": new_value, "reason": reason},
                         occurred_at=now)
            return {"action": action, "score_id": row["score_id"], "new_score_id": new_score_id}
        if action == "withdraw_deduction":
            row = connection.execute("SELECT * FROM review_deductions WHERE deduction_id=?",
                                     (correction.get("deduction_id"),)).fetchone()
            if row is None or row["stage_id"] != stage_id:
                raise ValidationError("纠正措施引用的扣分记录不存在")
            self._withdraw_deduction_tx(connection, actor_id=actor_id, row=row, reason=reason)
            return {"action": action, "deduction_id": row["deduction_id"]}
        if action == "lift_disqualification":
            row = connection.execute("SELECT * FROM review_disqualifications WHERE disqualification_id=?",
                                     (correction.get("disqualification_id"),)).fetchone()
            if row is None or row["stage_id"] != stage_id:
                raise ValidationError("纠正措施引用的资格记录不存在")
            self._lift_disqualification_tx(connection, actor_id=actor_id, row=row, reason=reason)
            return {"action": action, "disqualification_id": row["disqualification_id"]}
        if action in ("restore_judge", "exclude_judge"):
            track_id = self._identifier(str(correction.get("track_id", "")), "track_id")
            judge_id = self._identifier(str(correction.get("judge_id", "")), "judge_id")
            updated = self._set_judge_valid_tx(connection, actor_id=actor_id, stage_id=stage_id,
                                               track_id=track_id, judge_id=judge_id,
                                               valid=action == "restore_judge", reason=reason)
            if not updated:
                raise ValidationError("纠正措施引用的评委指派不存在")
            return {"action": action, "track_id": track_id, "judge_id": judge_id}
        raise ValidationError("未知的纠正措施")

    # ------------------------------------------------------------------
    # 查询、重放与解释
    # ------------------------------------------------------------------

    def _version_payload(self, connection, ranking_version_id: str,
                         public_only: bool = False) -> dict[str, Any]:
        row = self._version_row(connection, ranking_version_id)
        entries = []
        for item in connection.execute(
            "SELECT * FROM review_ranking_entries WHERE ranking_version_id=? "
            "ORDER BY track_id, (rank IS NULL), rank, entry_id",
            (ranking_version_id,),
        ):
            entry = {"entry_id": item["entry_id"], "track_id": item["track_id"], "rank": item["rank"],
                     "total_score": item["total_score"], "outcome": item["outcome"],
                     "award_level": item["award_level"]}
            if not public_only:
                entry["explanation"] = json.loads(item["explanation_json"])
            entries.append(entry)
        return {
            "ranking_version_id": row["ranking_version_id"], "stage_id": row["stage_id"],
            "version": row["version"], "status": row["status"],
            "rule_version_id": row["rule_version_id"], "input_hash": row["input_hash"],
            "entries_hash": row["entries_hash"],
            "trigger_type": row["trigger_type"], "trigger_id": row["trigger_id"],
            "generated_by": row["generated_by"], "generated_at": row["generated_at"],
            "review_signed_by": row["review_signed_by"], "review_signed_at": row["review_signed_at"],
            "publish_signed_by": row["publish_signed_by"], "publish_signed_at": row["publish_signed_at"],
            "published_by": row["published_by"], "published_at": row["published_at"],
            "appeal_deadline": row["appeal_deadline"],
            "delta": json.loads(row["delta_json"]) if row["delta_json"] else None,
            "entries": entries,
        }

    def get_ranking(self, *, actor_id: str, ranking_version_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        row = self._version_row(connection, ranking_version_id)
        if actor.role in PRIVILEGED_ROLES:
            return self._version_payload(connection, ranking_version_id)
        if row["status"] != "published":
            raise PermissionDenied("榜单尚未发布")
        return self._version_payload(connection, ranking_version_id, public_only=True)

    def list_rankings(self, *, actor_id: str, stage_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._stage_row(connection, stage_id)
        rows = connection.execute(
            "SELECT * FROM review_ranking_versions WHERE stage_id=? ORDER BY version", (stage_id,)
        ).fetchall()
        items = []
        for row in rows:
            if actor.role not in PRIVILEGED_ROLES and row["status"] != "published":
                continue
            items.append({"ranking_version_id": row["ranking_version_id"], "stage_id": row["stage_id"],
                          "version": row["version"], "status": row["status"],
                          "generated_at": row["generated_at"], "published_at": row["published_at"],
                          "review_signed_by": row["review_signed_by"],
                          "publish_signed_by": row["publish_signed_by"]})
        return items

    def public_results(self, *, actor_id: str, stage_id: str) -> dict[str, Any]:
        connection = self.database.connection
        self._actor(connection, actor_id)
        self._stage_row(connection, stage_id)
        row = connection.execute(
            "SELECT ranking_version_id FROM review_ranking_versions WHERE stage_id=? AND status='published'",
            (stage_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("阶段尚未发布榜单")
        return self._version_payload(connection, row["ranking_version_id"], public_only=True)

    def explain_entry(self, *, actor_id: str, ranking_version_id: str, entry_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        version = self._version_row(connection, ranking_version_id)
        row = connection.execute(
            "SELECT * FROM review_ranking_entries WHERE ranking_version_id=? AND entry_id=?",
            (ranking_version_id, entry_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("榜单中没有该作品")
        if actor.role in PRIVILEGED_ROLES:
            pass
        elif actor.role == "participant":
            if version["status"] != "published":
                raise PermissionDenied("只能查看已发布榜单")
            entry = self._entry_row(connection, entry_id)
            if entry["participant_id"] != actor_id:
                raise PermissionDenied("只能查看自己的作品明细")
        else:
            raise PermissionDenied("当前角色不能查看晋级解释")
        return {"ranking_version_id": ranking_version_id, "stage_id": version["stage_id"],
                "version": version["version"], "entry_id": entry_id, "outcome": row["outcome"],
                "rank": row["rank"], "explanation": json.loads(row["explanation_json"])}

    def replay_ranking(self, *, actor_id: str, ranking_version_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, "admin", "operator", "auditor")
        row = self._version_row(connection, ranking_version_id)
        inputs = json.loads(row["inputs_json"])
        stored_rows = self._entries_of(connection, ranking_version_id)
        recomputed = compute_ranking(inputs)
        input_hash_valid = digest(inputs) == row["input_hash"]
        entries_hash_valid = (entries_digest(recomputed) == row["entries_hash"]
                              and entries_digest(stored_rows) == row["entries_hash"])
        result: dict[str, Any] = {
            "ranking_version_id": ranking_version_id, "stage_id": row["stage_id"],
            "version": row["version"], "status": row["status"],
            "input_hash_valid": input_hash_valid, "entries_hash_valid": entries_hash_valid,
            "snapshot_replay": "match" if input_hash_valid and entries_hash_valid else "mismatch",
        }
        try:
            current_rows = compute_ranking(self._build_snapshot(connection, row["stage_id"]))
            if entries_digest(current_rows) == row["entries_hash"]:
                result["current_facts"] = "match"
            else:
                result["current_facts"] = "drifted"
                result["current_facts_diff"] = rows_diff(stored_rows, current_rows)
        except (ConflictError, ValidationError) as exc:
            result["current_facts"] = "unavailable"
            result["current_facts_error"] = str(exc)
        return result

    def my_entries(self, *, actor_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, "participant")
        items = []
        for entry in connection.execute(
            "SELECT * FROM review_entries WHERE participant_id=? ORDER BY created_at, entry_id", (actor_id,)
        ):
            entry_id = entry["entry_id"]
            scores = [dict(row) for row in connection.execute(
                "SELECT score_id, stage_id, judge_id, dimension, value, status, submitted_at "
                "FROM review_scores WHERE entry_id=? ORDER BY submitted_at, score_id", (entry_id,))]
            deductions = [dict(row) for row in connection.execute(
                "SELECT deduction_id, stage_id, points, reason, basis, status, created_at "
                "FROM review_deductions WHERE entry_id=? ORDER BY created_at", (entry_id,))]
            disqualifications = [dict(row) for row in connection.execute(
                "SELECT disqualification_id, stage_id, reason, status, created_at "
                "FROM review_disqualifications WHERE entry_id=? ORDER BY created_at", (entry_id,))]
            outcomes = [dict(row) for row in connection.execute(
                "SELECT rv.stage_id, rv.version, rv.published_at, re.rank, re.total_score, re.outcome, "
                "re.award_level FROM review_ranking_entries re "
                "JOIN review_ranking_versions rv ON rv.ranking_version_id=re.ranking_version_id "
                "WHERE re.entry_id=? AND rv.status='published' ORDER BY rv.stage_id, rv.version", (entry_id,))]
            appeals = [dict(row) for row in connection.execute(
                "SELECT appeal_id, ranking_version_id, fact_type, fact_id, status, filed_at, "
                "adjudicated_at, decision_note FROM review_appeals "
                "WHERE entry_id=? AND appellant_id=? ORDER BY filed_at", (entry_id, actor_id))]
            items.append({"entry_id": entry_id, "track_id": entry["track_id"], "title": entry["title"],
                          "status": entry["status"], "scores": scores, "deductions": deductions,
                          "disqualifications": disqualifications, "published_outcomes": outcomes,
                          "appeals": appeals})
        return items

    def list_scores(self, *, actor_id: str, stage_id: str,
                    entry_id: str | None = None) -> list[dict[str, Any]]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._stage_row(connection, stage_id)
        query = "SELECT * FROM review_scores WHERE stage_id=?"
        parameters: list[Any] = [stage_id]
        if entry_id:
            query += " AND entry_id=?"
            parameters.append(entry_id)
        query += " ORDER BY submitted_at, score_id"
        owners: dict[str, str] = {}
        visible = []
        for row in connection.execute(query, parameters):
            if actor.role in PRIVILEGED_ROLES:
                allowed = True
            elif actor.role == "judge":
                allowed = row["judge_id"] == actor_id or row["status"] != "submitted"
            elif actor.role == "participant":
                if row["entry_id"] not in owners:
                    entry = connection.execute("SELECT participant_id FROM review_entries WHERE entry_id=?",
                                               (row["entry_id"],)).fetchone()
                    owners[row["entry_id"]] = entry["participant_id"] if entry else ""
                allowed = owners[row["entry_id"]] == actor_id
            else:
                allowed = False
            if allowed:
                visible.append({"score_id": row["score_id"], "stage_id": row["stage_id"],
                                "track_id": row["track_id"], "entry_id": row["entry_id"],
                                "judge_id": row["judge_id"], "dimension": row["dimension"],
                                "value": row["value"], "status": row["status"],
                                "submitted_at": row["submitted_at"]})
        return visible

    def get_appeal(self, *, actor_id: str, appeal_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        row = self._appeal_row(connection, appeal_id)
        if actor.role not in PRIVILEGED_ROLES and row["appellant_id"] != actor_id:
            raise PermissionDenied("只能查看自己的申诉")
        return self._appeal_dict(row)

    def list_appeals(self, *, actor_id: str,
                     ranking_version_id: str | None = None) -> list[dict[str, Any]]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        query = "SELECT * FROM review_appeals"
        parameters: list[Any] = []
        conditions = []
        if actor.role not in PRIVILEGED_ROLES:
            conditions.append("appellant_id=?")
            parameters.append(actor_id)
        if ranking_version_id:
            conditions.append("ranking_version_id=?")
            parameters.append(ranking_version_id)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY filed_at, appeal_id"
        return [self._appeal_dict(row) for row in connection.execute(query, parameters)]

    def _appeal_dict(self, row) -> dict[str, Any]:
        return {"appeal_id": row["appeal_id"], "ranking_version_id": row["ranking_version_id"],
                "stage_id": row["stage_id"], "entry_id": row["entry_id"],
                "appellant_id": row["appellant_id"], "fact_type": row["fact_type"],
                "fact_id": row["fact_id"], "reason": row["reason"], "status": row["status"],
                "filed_at": row["filed_at"], "adjudicated_by": row["adjudicated_by"],
                "adjudicated_at": row["adjudicated_at"], "decision_note": row["decision_note"],
                "resulting_version_id": row["resulting_version_id"]}

    def pending_tasks(self, *, actor_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, "admin", "operator", "reviewer", "publisher", "auditor")
        now = self.clock.now()
        countersign_pending = [
            {"ranking_version_id": row["ranking_version_id"], "stage_id": row["stage_id"],
             "version": row["version"], "generated_at": row["generated_at"],
             "review_signed": bool(row["review_signed_by"]),
             "publish_signed": bool(row["publish_signed_by"])}
            for row in connection.execute(
                "SELECT * FROM review_ranking_versions WHERE status='candidate' ORDER BY generated_at"
            )
        ]
        appeal_windows = []
        for row in connection.execute(
            "SELECT * FROM review_ranking_versions WHERE status='published' ORDER BY published_at"
        ):
            deadline = parse_instant(row["appeal_deadline"])
            remaining = int((deadline - now).total_seconds())
            appeal_windows.append({
                "ranking_version_id": row["ranking_version_id"], "stage_id": row["stage_id"],
                "version": row["version"], "published_at": row["published_at"],
                "appeal_deadline": row["appeal_deadline"],
                "remaining_seconds": max(0, remaining), "open": remaining > 0,
            })
        appeals_pending = [
            {"appeal_id": row["appeal_id"], "stage_id": row["stage_id"], "entry_id": row["entry_id"],
             "fact_type": row["fact_type"], "fact_id": row["fact_id"], "filed_at": row["filed_at"]}
            for row in connection.execute("SELECT * FROM review_appeals WHERE status='filed' ORDER BY filed_at")
        ]
        return {"countersign_pending": countersign_pending, "appeal_windows": appeal_windows,
                "appeals_pending": appeals_pending}

    # ------------------------------------------------------------------
    # 元数据查询
    # ------------------------------------------------------------------

    def list_tracks(self, *, actor_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        self._actor(connection, actor_id)
        return [dict(row) for row in connection.execute("SELECT * FROM review_tracks ORDER BY track_id")]

    def list_stages(self, *, actor_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        self._actor(connection, actor_id)
        return [self._stage_dict(connection, row) for row in connection.execute(
            "SELECT * FROM review_stages ORDER BY sequence, stage_id")]

    def get_stage(self, *, actor_id: str, stage_id: str) -> dict[str, Any]:
        connection = self.database.connection
        self._actor(connection, actor_id)
        return self._stage_dict(connection, self._stage_row(connection, stage_id))

    def _stage_dict(self, connection, row) -> dict[str, Any]:
        stage_id = row["stage_id"]
        quotas = [dict(item) for item in connection.execute(
            "SELECT track_id, advance_count, updated_at FROM review_track_quotas WHERE stage_id=? "
            "ORDER BY track_id", (stage_id,))]
        constraints = [
            {"constraint_id": item["constraint_id"], "kind": item["kind"],
             "params": json.loads(item["params_json"])}
            for item in connection.execute(
                "SELECT * FROM review_award_constraints WHERE stage_id=? ORDER BY created_at", (stage_id,))]
        judges = [dict(item) for item in connection.execute(
            "SELECT track_id, judge_id, valid, reason, updated_at FROM review_stage_judges "
            "WHERE stage_id=? ORDER BY track_id, judge_id", (stage_id,))]
        rule_versions = [dict(item) for item in connection.execute(
            "SELECT rule_version_id, version, status, created_at, frozen_at FROM review_rule_versions "
            "WHERE stage_id=? ORDER BY version", (stage_id,))]
        return {"stage_id": stage_id, "name": row["name"], "sequence": row["sequence"],
                "status": row["status"], "frozen_rule_version_id": row["frozen_rule_version_id"],
                "frozen_at": row["frozen_at"], "created_at": row["created_at"],
                "quotas": quotas, "constraints": constraints, "judges": judges,
                "rule_versions": rule_versions}
