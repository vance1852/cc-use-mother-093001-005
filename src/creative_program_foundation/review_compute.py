"""晋级榜单的确定性计算：规则校验、排名、约束检查与版本差异。

本模块只包含纯函数：输入快照（冻结规则 + 评分事实 + 配额约束）唯一
确定输出榜单，不访问时钟、数据库或随机源，因此任意时刻都可以重放
并逐条解释每个晋级或落选原因。
"""

from __future__ import annotations

from functools import cmp_to_key
from typing import Any

from .audit import digest
from .clock import parse_instant
from .errors import ConflictError, ValidationError


OUTCOME_ADVANCE = "advance"
OUTCOME_ELIMINATE = "eliminate"
OUTCOME_DISQUALIFIED = "disqualified"
OUTCOME_EXCLUDED = "excluded_missing_scores"

LATE_SCORE_POLICIES = ("reject", "accept")
MISSING_SCORE_POLICIES = ("average", "zero", "disqualify")
REVOKED_SCORE_POLICIES = ("treat_as_missing", "treat_as_zero")
BOUNDARY_POLICIES = ("strict", "shared")
DEFAULT_APPEAL_WINDOW_SECONDS = 72 * 3600


def _choice(config: dict[str, Any], key: str, allowed: tuple[str, ...], default: str) -> str:
    value = config.get(key, default)
    if value not in allowed:
        raise ValidationError(f"{key} 必须是 {'/'.join(allowed)} 之一")
    return value


def validate_rule_config(config: Any) -> dict[str, Any]:
    """校验并规范化阶段规则配置，返回可冻结的完整形态。"""

    if not isinstance(config, dict):
        raise ValidationError("规则配置必须是对象")
    dimensions = config.get("dimensions")
    if not isinstance(dimensions, list) or not dimensions:
        raise ValidationError("dimensions 必须是非空列表")
    seen: set[str] = set()
    normalized_dimensions: list[dict[str, Any]] = []
    total_weight = 0.0
    for item in dimensions:
        if not isinstance(item, dict):
            raise ValidationError("维度定义必须是对象")
        name = str(item.get("name", "")).strip()
        if not name or len(name) > 40:
            raise ValidationError("维度名称不能为空且不能超过 40 个字符")
        if name in seen:
            raise ValidationError(f"维度 {name} 重复")
        seen.add(name)
        weight = item.get("weight")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or float(weight) <= 0:
            raise ValidationError("维度权重必须是正数")
        total_weight += float(weight)
        normalized_dimensions.append({"name": name, "weight": float(weight)})
    if abs(total_weight - 1.0) > 1e-6:
        raise ValidationError("维度权重之和必须等于 1")

    score_due_at = str(config.get("score_due_at", "")).strip()
    if not score_due_at:
        raise ValidationError("score_due_at 不能为空")
    try:
        parse_instant(score_due_at)
    except ValueError as exc:
        raise ValidationError("score_due_at 必须是带时区的 ISO 时间") from exc

    tie_break = config.get("tie_break_dimensions")
    if tie_break is None:
        tie_break = [item["name"] for item in normalized_dimensions]
    if not isinstance(tie_break, list) or any(not isinstance(name, str) or name not in seen
                                              for name in tie_break):
        raise ValidationError("tie_break_dimensions 必须属于已定义维度")

    award_levels = config.get("award_levels", [])
    if not isinstance(award_levels, list):
        raise ValidationError("award_levels 必须是列表")
    normalized_levels: list[dict[str, Any]] = []
    level_names: set[str] = set()
    for band in award_levels:
        if not isinstance(band, dict):
            raise ValidationError("奖项档位必须是对象")
        level = str(band.get("level", "")).strip()
        count = band.get("count")
        if not level or len(level) > 40:
            raise ValidationError("奖项档位名称无效")
        if level in level_names:
            raise ValidationError(f"奖项档位 {level} 重复")
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValidationError("奖项档位数量必须是正整数")
        level_names.add(level)
        normalized_levels.append({"level": level, "count": count})

    window = config.get("appeal_window_seconds", DEFAULT_APPEAL_WINDOW_SECONDS)
    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        raise ValidationError("appeal_window_seconds 必须是正整数")

    return {
        "dimensions": normalized_dimensions,
        "score_due_at": score_due_at,
        "late_score_policy": _choice(config, "late_score_policy", LATE_SCORE_POLICIES, "reject"),
        "missing_score_policy": _choice(config, "missing_score_policy", MISSING_SCORE_POLICIES,
                                        "average"),
        "revoked_score_policy": _choice(config, "revoked_score_policy", REVOKED_SCORE_POLICIES,
                                        "treat_as_missing"),
        "boundary_policy": _choice(config, "boundary_policy", BOUNDARY_POLICIES, "strict"),
        "tie_break_dimensions": list(tie_break),
        "award_levels": normalized_levels,
        "appeal_window_seconds": window,
    }


def validate_constraint(kind: Any, params: Any) -> dict[str, Any]:
    """校验跨赛道奖项约束。"""

    if not isinstance(params, dict):
        raise ValidationError("约束参数必须是对象")
    if kind == "total_advance_exact":
        value = params.get("value")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValidationError("total_advance_exact 需要非负整数 value")
        return {"kind": kind, "params": {"value": value}}
    if kind == "award_level_exact":
        level = str(params.get("level", "")).strip()
        value = params.get("value")
        if not level:
            raise ValidationError("award_level_exact 需要 level")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValidationError("award_level_exact 需要非负整数 value")
        return {"kind": kind, "params": {"level": level, "value": value}}
    raise ValidationError("未知的约束类型")


def compute_ranking(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """根据冻结规则与评分事实计算榜单行，同一快照必然得到同一结果。"""

    rule = snapshot["rule"]
    quotas = snapshot["quotas"]
    valid_judges = snapshot["valid_judges"]
    scores_by_entry: dict[str, list[dict[str, Any]]] = {}
    for score in snapshot["scores"]:
        scores_by_entry.setdefault(score["entry_id"], []).append(score)
    deductions_by_entry: dict[str, list[dict[str, Any]]] = {}
    for deduction in snapshot["deductions"]:
        deductions_by_entry.setdefault(deduction["entry_id"], []).append(deduction)
    disqualified = {item["entry_id"]: item for item in snapshot["disqualified"]}
    rule_version = snapshot["rule_version"]

    rows: list[dict[str, Any]] = []
    per_track: dict[str, list[dict[str, Any]]] = {}
    for enrolled in snapshot["enrolled"]:
        entry_id = enrolled["entry_id"]
        track_id = enrolled["track_id"]
        if entry_id in disqualified:
            fact = disqualified[entry_id]
            rows.append({
                "entry_id": entry_id, "track_id": track_id, "rank": None, "total_score": None,
                "outcome": OUTCOME_DISQUALIFIED, "award_level": None,
                "explanation": {
                    "entry_id": entry_id, "track_id": track_id, "rule_version": rule_version,
                    "outcome": OUTCOME_DISQUALIFIED, "rank": None, "quota": quotas.get(track_id, 0),
                    "outcome_reason": f"资格取消：{fact['reason']}",
                    "disqualification": {"disqualification_id": fact["disqualification_id"],
                                         "reason": fact["reason"]},
                },
            })
            continue
        detail, dim_averages, missing_total, excluded = _entry_dimensions(
            rule, valid_judges.get(track_id, []), scores_by_entry.get(entry_id, []))
        if excluded:
            rows.append({
                "entry_id": entry_id, "track_id": track_id, "rank": None, "total_score": None,
                "outcome": OUTCOME_EXCLUDED, "award_level": None,
                "explanation": {
                    "entry_id": entry_id, "track_id": track_id, "rule_version": rule_version,
                    "outcome": OUTCOME_EXCLUDED, "rank": None, "quota": quotas.get(track_id, 0),
                    "outcome_reason": "存在缺评维度，按冻结规则取消排名资格",
                    "dimensions": detail,
                },
            })
            continue
        deductions = deductions_by_entry.get(entry_id, [])
        deduction_total = round(sum(item["points"] for item in deductions), 6)
        raw_score = round(sum(item["weighted"] for item in detail), 6)
        final_score = round(raw_score - deduction_total, 6)
        clamped = final_score < 0
        if clamped:
            final_score = 0.0
        row = {
            "entry_id": entry_id, "track_id": track_id, "rank": None, "total_score": final_score,
            "outcome": None, "award_level": None,
            "_dim_averages": dim_averages, "_missing": missing_total,
            "_enrolled_at": enrolled["enrolled_at"],
            "explanation": {
                "entry_id": entry_id, "track_id": track_id, "rule_version": rule_version,
                "dimensions": detail,
                "raw_score": raw_score,
                "deductions": [
                    {"deduction_id": item["deduction_id"], "points": item["points"],
                     "reason": item["reason"], "basis": item["basis"]}
                    for item in deductions
                ],
                "deduction_total": deduction_total,
                "final_score": final_score,
                "clamped": clamped,
                "quota": quotas.get(track_id, 0),
            },
        }
        rows.append(row)
        per_track.setdefault(track_id, []).append(row)

    tie_dimensions = rule["tie_break_dimensions"]
    boundary = rule["boundary_policy"]
    for track_id in sorted(per_track):
        group = per_track[track_id]
        group.sort(key=cmp_to_key(lambda left, right: _compare(left, right, tie_dimensions)))
        quota = quotas.get(track_id, 0)
        if boundary == "shared":
            rank = 0
            previous_score: float | None = None
            for index, row in enumerate(group):
                if previous_score is None or row["total_score"] < previous_score:
                    rank = index + 1
                    previous_score = row["total_score"]
                row["rank"] = rank
        else:
            for index, row in enumerate(group):
                row["rank"] = index + 1
        _annotate_ties(group, rule)
        advancing = [row for row in group if row["rank"] <= quota]
        position = 0
        for band in rule["award_levels"]:
            for _ in range(band["count"]):
                if position < len(advancing):
                    advancing[position]["award_level"] = band["level"]
                    position += 1
        for row in group:
            row["outcome"] = OUTCOME_ADVANCE if row["rank"] <= quota else OUTCOME_ELIMINATE
            explanation = row["explanation"]
            explanation["rank"] = row["rank"]
            explanation["outcome"] = row["outcome"]
            explanation["award_level"] = row["award_level"]
            reason = f"赛道内排名第 {row['rank']}，名额 {quota}"
            if row["outcome"] == OUTCOME_ADVANCE:
                reason += "，晋级"
                if row["award_level"]:
                    reason += f"并获 {row['award_level']}"
            else:
                reason += "，落选"
            tie = explanation.get("tie_break")
            if tie and tie.get("decided_by"):
                reason += f"；同分决胜依据 {tie['decided_by']}"
            explanation["outcome_reason"] = reason

    _check_constraints(snapshot["constraints"], rows)
    for row in rows:
        row.pop("_dim_averages", None)
        row.pop("_missing", None)
        row.pop("_enrolled_at", None)
    rows.sort(key=lambda item: (item["track_id"], item["rank"] is None, item["rank"] or 0,
                                item["entry_id"]))
    return rows


def _entry_dimensions(rule: dict[str, Any], valid_judge_ids: list[str],
                      scores: list[dict[str, Any]]):
    """汇总一个作品各维度得分，返回 (维度明细, 维度均值, 缺评总数, 是否排除)。"""

    dimension_names = {item["name"] for item in rule["dimensions"]}
    judge_set = set(valid_judge_ids)
    slots: dict[tuple[str, str], dict[str, Any]] = {}
    for score in scores:
        if score["judge_id"] not in judge_set or score["dimension"] not in dimension_names:
            continue
        slot = slots.setdefault((score["dimension"], score["judge_id"]), {})
        status = score["status"]
        if status == "sealed":
            slot["sealed"] = score
        elif status == "revoked":
            slot["revoked"] = score
        elif status == "rejected_late":
            slot["late"] = score
    detail: list[dict[str, Any]] = []
    dim_averages: dict[str, float | None] = {}
    missing_total = 0
    excluded = False
    for dimension in rule["dimensions"]:
        name = dimension["name"]
        weight = dimension["weight"]
        judge_rows: list[dict[str, Any]] = []
        values: list[float] = []
        missing: list[str] = []
        for judge_id in sorted(judge_set):
            slot = slots.get((name, judge_id), {})
            if "sealed" in slot:
                values.append(slot["sealed"]["value"])
                judge_rows.append({"judge_id": judge_id, "value": slot["sealed"]["value"],
                                   "status": "sealed"})
            elif "revoked" in slot:
                if rule["revoked_score_policy"] == "treat_as_zero":
                    values.append(0.0)
                    judge_rows.append({"judge_id": judge_id, "value": 0.0,
                                       "status": "revoked_zero"})
                else:
                    missing.append(judge_id)
                    judge_rows.append({"judge_id": judge_id, "value": None, "status": "revoked"})
            elif "late" in slot:
                missing.append(judge_id)
                judge_rows.append({"judge_id": judge_id, "value": None,
                                   "status": "rejected_late"})
            else:
                missing.append(judge_id)
                judge_rows.append({"judge_id": judge_id, "value": None, "status": "missing"})
        if missing and rule["missing_score_policy"] == "disqualify":
            excluded = True
            detail.append({"name": name, "weight": weight, "judges": judge_rows,
                           "missing_judges": missing, "average": None, "weighted": None})
            break
        if missing and rule["missing_score_policy"] == "zero":
            values.extend([0.0] * len(missing))
        average = round(sum(values) / len(values), 6) if values else 0.0
        dim_averages[name] = average if values else None
        missing_total += len(missing)
        detail.append({"name": name, "weight": weight, "judges": judge_rows,
                       "missing_judges": missing, "average": average,
                       "weighted": round(average * weight, 6)})
    return detail, dim_averages, missing_total, excluded


def _dimension_value(row: dict[str, Any], dimension: str) -> float:
    value = row["_dim_averages"].get(dimension)
    return -1.0 if value is None else value


def _compare(left: dict[str, Any], right: dict[str, Any], tie_dimensions: list[str]) -> int:
    """严格的确定性名次比较：总分、决胜维度、缺评数、纳入时间、作品编号。"""

    if left["total_score"] != right["total_score"]:
        return -1 if left["total_score"] > right["total_score"] else 1
    for dimension in tie_dimensions:
        left_value = _dimension_value(left, dimension)
        right_value = _dimension_value(right, dimension)
        if left_value != right_value:
            return -1 if left_value > right_value else 1
    if left["_missing"] != right["_missing"]:
        return -1 if left["_missing"] < right["_missing"] else 1
    if left["_enrolled_at"] != right["_enrolled_at"]:
        return -1 if left["_enrolled_at"] < right["_enrolled_at"] else 1
    if left["entry_id"] != right["entry_id"]:
        return -1 if left["entry_id"] < right["entry_id"] else 1
    return 0


def _decider(left: dict[str, Any], right: dict[str, Any], tie_dimensions: list[str]) -> str:
    for dimension in tie_dimensions:
        if _dimension_value(left, dimension) != _dimension_value(right, dimension):
            return f"dimension:{dimension}"
    if left["_missing"] != right["_missing"]:
        return "missing_count"
    if left["_enrolled_at"] != right["_enrolled_at"]:
        return "enrolled_at"
    return "entry_id"


def _annotate_ties(group: list[dict[str, Any]], rule: dict[str, Any]) -> None:
    by_score: dict[float, list[dict[str, Any]]] = {}
    for row in group:
        by_score.setdefault(row["total_score"], []).append(row)
    for members in by_score.values():
        if len(members) < 2:
            continue
        for index, row in enumerate(members):
            decided_by = None
            if rule["boundary_policy"] == "strict":
                if index + 1 < len(members):
                    decided_by = _decider(row, members[index + 1], rule["tie_break_dimensions"])
                elif index > 0:
                    decided_by = _decider(members[index - 1], row, rule["tie_break_dimensions"])
            row["explanation"]["tie_break"] = {
                "policy": rule["boundary_policy"], "group_size": len(members),
                "decided_by": decided_by,
            }


def _check_constraints(constraints: list[dict[str, Any]], rows: list[dict[str, Any]]) -> None:
    """晋级名额与跨赛道奖项总量必须同时满足，否则整榜不可生成。"""

    advancing = [row for row in rows if row["outcome"] == OUTCOME_ADVANCE]
    for constraint in constraints:
        kind = constraint["kind"]
        params = constraint["params"]
        if kind == "total_advance_exact":
            required = params["value"]
            if len(advancing) != required:
                raise ConflictError(f"晋级总数约束不满足：要求 {required}，实际 {len(advancing)}")
        elif kind == "award_level_exact":
            required = params["value"]
            level = params["level"]
            actual = sum(1 for row in advancing if row["award_level"] == level)
            if actual != required:
                raise ConflictError(f"奖项 {level} 总量约束不满足：要求 {required}，实际 {actual}")


def _canonical_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"entry_id": row["entry_id"], "track_id": row["track_id"], "rank": row["rank"],
         "total_score": row["total_score"], "outcome": row["outcome"],
         "award_level": row["award_level"], "explanation": row["explanation"]}
        for row in sorted(rows, key=lambda item: (item["track_id"], item["entry_id"]))
    ]


def entries_digest(rows: list[dict[str, Any]]) -> str:
    """榜单行的稳定摘要，用于重放核对。"""

    return digest(_canonical_rows(rows))


def rows_diff(old_rows: list[dict[str, Any]], new_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """比较两份榜单行，列出名次、分数、结果和奖项的变化。"""

    old_by_id = {row["entry_id"]: row for row in old_rows}
    new_by_id = {row["entry_id"]: row for row in new_rows}
    diff: list[dict[str, Any]] = []
    for entry_id in sorted(set(old_by_id) | set(new_by_id)):
        old = old_by_id.get(entry_id)
        new = new_by_id.get(entry_id)
        if old is None:
            diff.append({"entry_id": entry_id, "change": "added",
                         "new_outcome": new["outcome"], "new_rank": new["rank"]})
            continue
        if new is None:
            diff.append({"entry_id": entry_id, "change": "removed",
                         "old_outcome": old["outcome"], "old_rank": old["rank"]})
            continue
        fields = {}
        for field in ("rank", "total_score", "outcome", "award_level"):
            if old[field] != new[field]:
                fields[field] = {"old": old[field], "new": new[field]}
        if fields:
            diff.append({"entry_id": entry_id, "change": "updated", "fields": fields})
    return diff


def compute_delta(from_version: int, to_version: int, old_rows: list[dict[str, Any]],
                  new_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """生成新版本相对已发布版本的名次、名额与通知影响说明。"""

    old_by_id = {row["entry_id"]: row for row in old_rows}
    new_by_id = {row["entry_id"]: row for row in new_rows}
    rank_changes: list[dict[str, Any]] = []
    for entry_id in sorted(new_by_id):
        new = new_by_id[entry_id]
        old = old_by_id.get(entry_id)
        if old is None:
            rank_changes.append({"entry_id": entry_id, "change": "entered",
                                 "new_outcome": new["outcome"], "new_rank": new["rank"]})
            continue
        if ((old["rank"], old["outcome"], old["award_level"])
                != (new["rank"], new["outcome"], new["award_level"])):
            rank_changes.append({
                "entry_id": entry_id, "change": "updated",
                "old_rank": old["rank"], "new_rank": new["rank"],
                "old_outcome": old["outcome"], "new_outcome": new["outcome"],
                "old_award_level": old["award_level"], "new_award_level": new["award_level"],
            })
    for entry_id in sorted(set(old_by_id) - set(new_by_id)):
        old = old_by_id[entry_id]
        rank_changes.append({"entry_id": entry_id, "change": "removed",
                             "old_rank": old["rank"], "old_outcome": old["outcome"]})
    quota_changes: list[dict[str, Any]] = []
    tracks = sorted({row["track_id"] for row in old_rows} | {row["track_id"] for row in new_rows})
    for track_id in tracks:
        old_advance = sorted(row["entry_id"] for row in old_rows
                             if row["track_id"] == track_id and row["outcome"] == OUTCOME_ADVANCE)
        new_advance = sorted(row["entry_id"] for row in new_rows
                             if row["track_id"] == track_id and row["outcome"] == OUTCOME_ADVANCE)
        if old_advance != new_advance:
            quota_changes.append({"track_id": track_id, "old_advance": old_advance,
                                  "new_advance": new_advance})
    return {"from_version": from_version, "to_version": to_version,
            "rank_changes": rank_changes, "quota_changes": quota_changes,
            "affected_notifications": []}
