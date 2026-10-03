"""榜单计算引擎：对冻结快照做纯函数式计算，输出名次、晋级、奖项与解释。

快照之外没有任何输入，因此同一快照无论何时重放都会得到同一结果，
候选榜复核、发布后重放和申诉产生的新版本都依赖这一性质。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def parse_instant(value: str) -> datetime:
    """把存储的 ISO 时间文本解析为可比较的感知时间。"""

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _round6(value: float) -> float:
    return round(float(value), 6)


def compute_ranking(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """根据快照计算全部作品的名次、晋级与奖项标记。"""

    rules: dict[str, dict[str, Any]] = snapshot["rules"]
    quotas: dict[str, int] = snapshot["quotas"]
    entries: list[dict[str, Any]] = snapshot["entries"]
    judges: dict[str, list[str]] = snapshot["judges"]
    scores_by_entry: dict[str, list[dict[str, Any]]] = {}
    for fact in snapshot["scores"]:
        scores_by_entry.setdefault(fact["entry_id"], []).append(fact)

    items: list[dict[str, Any]] = []
    for track_id in sorted({entry["track_id"] for entry in entries}):
        rule = rules[track_id]
        quota = int(quotas.get(track_id, 0))
        valid_judges = sorted(judges.get(track_id, []))
        track_entries = [entry for entry in entries if entry["track_id"] == track_id]
        items.extend(_compute_track(track_id, rule, quota, track_entries, valid_judges, scores_by_entry))
    _allocate_awards(items, snapshot.get("award_constraint"))
    items.sort(key=lambda item: (item["track_id"], item["rank"] is None, item["rank"] or 0, item["entry_id"]))
    return items


def output_projection(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """提取用于一致性校验与重放比对的稳定投影。"""

    return [
        {"entry_id": item["entry_id"], "total_score": item["total_score"], "rank": item["rank"],
         "advanced": bool(item["advanced"]), "awarded": bool(item["awarded"])}
        for item in sorted(items, key=lambda value: value["entry_id"])
    ]


def build_change_summary(previous_items: list[dict[str, Any]], new_items: list[dict[str, Any]],
                         compared_to_run_id: str) -> dict[str, Any]:
    """对比两个榜单版本，说明名次、名额与后续通知受到的影响。"""

    previous = {item["entry_id"]: item for item in previous_items}
    current = {item["entry_id"]: item for item in new_items}
    rank_changes: list[dict[str, Any]] = []
    advancement_changes: list[dict[str, Any]] = []
    award_changes: list[dict[str, Any]] = []
    for entry_id in sorted(set(previous) | set(current)):
        before = previous.get(entry_id)
        after = current.get(entry_id)
        before_rank = before["rank"] if before else None
        after_rank = after["rank"] if after else None
        if before_rank != after_rank:
            rank_changes.append({"entry_id": entry_id, "from": before_rank, "to": after_rank})
        before_advanced = bool(before["advanced"]) if before else False
        after_advanced = bool(after["advanced"]) if after else False
        if before_advanced != after_advanced:
            advancement_changes.append({"entry_id": entry_id, "from": before_advanced, "to": after_advanced})
        before_awarded = bool(before["awarded"]) if before else False
        after_awarded = bool(after["awarded"]) if after else False
        if before_awarded != after_awarded:
            award_changes.append({"entry_id": entry_id, "from": before_awarded, "to": after_awarded})
    quota_effects: list[dict[str, Any]] = []
    track_ids = {item["track_id"] for item in previous_items} | {item["track_id"] for item in new_items}
    for track_id in sorted(track_ids):
        before_count = sum(1 for item in previous_items if item["track_id"] == track_id and item["advanced"])
        after_count = sum(1 for item in new_items if item["track_id"] == track_id and item["advanced"])
        if before_count != after_count:
            quota_effects.append({"track_id": track_id, "advanced_before": before_count,
                                  "advanced_after": after_count})
    affected = sorted({change["entry_id"] for change in advancement_changes + award_changes})
    return {
        "compared_to_run_id": compared_to_run_id,
        "rank_changes": rank_changes,
        "advancement_changes": advancement_changes,
        "award_changes": award_changes,
        "quota_effects": quota_effects,
        "affected_notifications": affected,
    }


def _item(entry: dict[str, Any], track_id: str, total: float | None, rank: int | None,
          advanced: bool, explanation: dict[str, Any]) -> dict[str, Any]:
    return {
        "entry_id": entry["entry_id"],
        "track_id": track_id,
        "participant_id": entry["participant_id"],
        "total_score": total,
        "rank": rank,
        "advanced": advanced,
        "awarded": False,
        "explanation": explanation,
    }


def _compute_track(track_id: str, rule: dict[str, Any], quota: int,
                   entries: list[dict[str, Any]], valid_judges: list[str],
                   scores_by_entry: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    weights: dict[str, float] = rule["weights"]
    deadline = rule.get("score_deadline")
    reject_late = rule["late_score_policy"] == "reject"
    fill_zero = rule["missing_score_policy"] == "zero"
    items: list[dict[str, Any]] = []
    rankable: list[dict[str, Any]] = []
    for entry in sorted(entries, key=lambda value: value["entry_id"]):
        explanation: dict[str, Any] = {
            "rule_version_id": rule["rule_version_id"],
            "rule_version": rule["version"],
            "weights": dict(weights),
        }
        if entry["status"] == "disqualified":
            explanation["status_note"] = "disqualified"
            explanation["summary"] = f"资格已取消（{entry.get('disqualified_reason') or '未说明原因'}），不参与排名"
            items.append(_item(entry, track_id, None, None, False, explanation))
            continue
        facts = scores_by_entry.get(entry["entry_id"], [])
        by_judge = {fact["judge_id"]: fact for fact in facts}
        included: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for judge_id in valid_judges:
            fact = by_judge.pop(judge_id, None)
            if fact is None:
                excluded.append({"judge_id": judge_id, "reason": "missing", "note": "评委未提交评分"})
            elif fact["status"] == "revoked":
                excluded.append({"judge_id": judge_id, "reason": "revoked",
                                 "note": f"评分已撤销（{fact.get('revoke_reason') or '未说明原因'}）"})
            elif reject_late and deadline and parse_instant(fact["submitted_at"]) > parse_instant(deadline):
                excluded.append({"judge_id": judge_id, "reason": "late", "note": "迟交评分按冻结规则不计入"})
            else:
                included.append(fact)
        for judge_id in sorted(by_judge):
            excluded.append({"judge_id": judge_id, "reason": "not_assigned", "note": "评委不在有效评委集合内"})
        explanation["included_judges"] = sorted(fact["judge_id"] for fact in included)
        explanation["excluded_judges"] = excluded
        explanation["missing_score_policy"] = rule["missing_score_policy"]
        denominator = len(valid_judges) if fill_zero else len(included)
        if denominator == 0:
            explanation["status_note"] = "no_valid_scores"
            explanation["summary"] = "无有效评分，不参与排名"
            items.append(_item(entry, track_id, None, None, False, explanation))
            continue
        judge_totals: list[float] = []
        deduction_total = 0.0
        dimension_values: dict[str, list[float]] = {dimension: [] for dimension in weights}
        missing_dimensions: set[str] = set()
        for fact in included:
            scores_map: dict[str, Any] = fact["scores"]
            subtotal = 0.0
            for dimension, weight in weights.items():
                if dimension in scores_map:
                    value = float(scores_map[dimension])
                    dimension_values[dimension].append(value)
                    subtotal += float(weight) * value
                else:
                    missing_dimensions.add(dimension)
            deduction = float(fact.get("deduction", 0.0))
            deduction_total += deduction
            judge_totals.append(subtotal - deduction)
        total = _round6(sum(judge_totals) / denominator)
        if fill_zero:
            zero_filled = denominator - len(included)
            if zero_filled:
                explanation["missing_zero_filled"] = zero_filled
        dimension_averages = {
            dimension: _round6(sum(values) / len(values))
            for dimension, values in dimension_values.items() if values
        }
        explanation["dimension_averages"] = dimension_averages
        if missing_dimensions:
            explanation["missing_dimensions"] = sorted(missing_dimensions)
        explanation["deduction_total"] = _round6(deduction_total)
        explanation["judge_denominator"] = denominator
        rankable.append({
            "entry": entry,
            "total": total,
            "dimension_averages": dimension_averages,
            "explanation": explanation,
        })

    rankable.sort(key=lambda member: (-member["total"], member["entry"]["entry_id"]))
    position = 1
    index = 0
    while index < len(rankable):
        end = index
        while end + 1 < len(rankable) and rankable[end + 1]["total"] == rankable[index]["total"]:
            end += 1
        group = rankable[index:end + 1]
        if len(group) > 1 and rule["tie_policy"] == "tie_break":
            ordered = _tie_break_order(group, rule["tie_break_dimensions"])
            for offset, member in enumerate(ordered):
                member["rank"] = position + offset
            _annotate_tie_break(ordered, rule["tie_break_dimensions"])
        else:
            for member in group:
                member["rank"] = position
                if len(group) > 1:
                    member["tie_note"] = f"总分并列第 {position} 名"
        position += len(group)
        index = end + 1

    pass_score = rule.get("pass_score")
    for member in rankable:
        rank = member["rank"]
        total = member["total"]
        within_quota = rank <= quota
        pass_met = pass_score is None or total >= float(pass_score)
        advanced = within_quota and pass_met
        explanation = member["explanation"]
        explanation["quota"] = {"track_quota": quota, "rank": rank, "within_quota": within_quota}
        if pass_score is not None:
            explanation["pass_score"] = {"required": float(pass_score), "met": pass_met}
        parts = [f"总分 {total:.2f}"]
        if member.get("tie_note"):
            parts.append(member["tie_note"])
        parts.append(f"赛道内第 {rank} 名")
        if not pass_met:
            parts.append(f"低于及格线 {float(pass_score):.2f}，落选")
        elif within_quota:
            parts.append(f"在赛道名额 {quota} 内，晋级")
        else:
            parts.append(f"超出赛道名额 {quota}，落选")
        explanation["summary"] = "，".join(parts)
        items.append(_item(member["entry"], track_id, total, rank, advanced, explanation))
    return items


def _tie_break_order(group: list[dict[str, Any]], dimensions: list[str]) -> list[dict[str, Any]]:
    def key(member: dict[str, Any]) -> tuple:
        return tuple([-member["dimension_averages"].get(dimension, 0.0)
                      for dimension in dimensions]) + (member["entry"]["entry_id"],)

    return sorted(group, key=key)


def _annotate_tie_break(ordered: list[dict[str, Any]], dimensions: list[str]) -> None:
    for index, member in enumerate(ordered):
        neighbor = None
        if index + 1 < len(ordered):
            neighbor = ordered[index + 1]
        elif index > 0:
            neighbor = ordered[index - 1]
        if neighbor is None:
            continue
        note = "总分与决胜维度均相同，按作品编号排序"
        for dimension in dimensions:
            mine = member["dimension_averages"].get(dimension, 0.0)
            theirs = neighbor["dimension_averages"].get(dimension, 0.0)
            if mine != theirs:
                note = f"总分并列，按维度「{dimension}」决胜"
                break
        member["tie_note"] = note


def _allocate_awards(items: list[dict[str, Any]], constraint: dict[str, Any] | None) -> None:
    pending: list[dict[str, Any]] = []
    for item in items:
        explanation = item["explanation"]
        if constraint is None:
            explanation["award"] = {"awarded": False, "reason": "未设置跨赛道奖项约束"}
        elif item["rank"] is None:
            explanation["award"] = {"awarded": False, "reason": "无排名，不参与奖项评定"}
        elif not item["advanced"]:
            explanation["award"] = {"awarded": False, "reason": "未晋级，不参与奖项评定"}
        else:
            pending.append(item)
    if constraint is not None:
        pending.sort(key=lambda item: (-item["total_score"], item["entry_id"]))
        total_cap = int(constraint["total_awards"])
        per_track_cap = int(constraint["per_track_cap"])
        counts: dict[str, int] = {}
        granted = 0
        for item in pending:
            track_id = item["track_id"]
            if granted >= total_cap:
                item["explanation"]["award"] = {"awarded": False, "reason": f"跨赛道奖项总量 {total_cap} 已满"}
            elif counts.get(track_id, 0) >= per_track_cap:
                item["explanation"]["award"] = {"awarded": False, "reason": f"赛道奖项上限 {per_track_cap} 已满"}
            else:
                counts[track_id] = counts.get(track_id, 0) + 1
                granted += 1
                item["awarded"] = True
                item["explanation"]["award"] = {
                    "awarded": True,
                    "reason": f"在跨赛道奖项总量 {total_cap} 与赛道奖项上限 {per_track_cap} 内",
                }
    for item in items:
        award = item["explanation"]["award"]
        suffix = "获奖" if award["awarded"] else f"未获奖（{award['reason']}）"
        item["explanation"]["summary"] = f"{item['explanation']['summary']}；{suffix}"
