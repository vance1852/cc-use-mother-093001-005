"""晋级评议与申诉模块的 HTTP/JSON 路由。"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


def _created(result: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return (200 if result.get("replayed") else 201), result


def _query(query: dict[str, list[str]], key: str) -> str | None:
    return query.get(key, [None])[0]


def route_review(service, method: str, path: str, body: dict[str, Any] | None,
                 headers: dict[str, str] | None) -> tuple[int, dict[str, Any]] | None:
    """把 /review 前缀的请求分派到评议服务，未命中返回 None。"""

    parsed = urlparse(path)
    segments = [unquote(item) for item in parsed.path.split("/") if item]
    if not segments or segments[0] != "review":
        return None
    query = parse_qs(parsed.query)
    actor_id = (headers or {}).get("X-Actor-Id", "")
    body = body or {}
    rest = segments[1:]

    if method == "POST":
        if rest == ["tracks"]:
            return _created(service.create_track(actor_id=actor_id, **body))
        if rest == ["stages"]:
            return _created(service.create_stage(actor_id=actor_id, **body))
        if rest == ["rule-versions"]:
            return _created(service.create_rule_version(actor_id=actor_id, **body))
        if rest == ["entries"]:
            return _created(service.register_entry(actor_id=actor_id, **body))
        if rest == ["judges"]:
            return _created(service.assign_judge(actor_id=actor_id, **body))
        if rest == ["judges", "exclude"]:
            return _created(service.exclude_judge(actor_id=actor_id, **body))
        if rest == ["scores"]:
            return _created(service.submit_score(actor_id=actor_id, **body))
        if len(rest) == 3 and rest[0] == "scores" and rest[2] == "revoke":
            return _created(service.revoke_score(actor_id=actor_id, score_id=rest[1], **body))
        if rest == ["deductions"]:
            return _created(service.add_deduction(actor_id=actor_id, **body))
        if len(rest) == 3 and rest[0] == "deductions" and rest[2] == "withdraw":
            return _created(service.withdraw_deduction(actor_id=actor_id, deduction_id=rest[1], **body))
        if rest == ["disqualifications"]:
            return _created(service.disqualify_entry(actor_id=actor_id, **body))
        if len(rest) == 3 and rest[0] == "disqualifications" and rest[2] == "lift":
            return _created(service.lift_disqualification(actor_id=actor_id,
                                                          disqualification_id=rest[1], **body))
        if rest == ["quotas"]:
            return _created(service.set_quota(actor_id=actor_id, **body))
        if rest == ["award-constraints"]:
            return _created(service.add_award_constraint(actor_id=actor_id, **body))
        if rest == ["appeals"]:
            return _created(service.file_appeal(actor_id=actor_id, **body))
        if len(rest) == 3 and rest[0] == "appeals" and rest[2] == "adjudicate":
            return _created(service.adjudicate_appeal(actor_id=actor_id, appeal_id=rest[1], **body))
        if len(rest) == 3 and rest[0] == "stages" and rest[2] == "freeze":
            return _created(service.freeze_stage(actor_id=actor_id, stage_id=rest[1], **body))
        if len(rest) == 3 and rest[0] == "stages" and rest[2] == "enroll":
            return _created(service.enroll_entries(actor_id=actor_id, stage_id=rest[1], **body))
        if len(rest) == 3 and rest[0] == "stages" and rest[2] == "rankings":
            return _created(service.generate_ranking(actor_id=actor_id, stage_id=rest[1], **body))
        if len(rest) == 3 and rest[0] == "rankings" and rest[2] == "countersign":
            return _created(service.countersign_ranking(actor_id=actor_id,
                                                        ranking_version_id=rest[1], **body))
        if len(rest) == 3 and rest[0] == "rankings" and rest[2] == "publish":
            return _created(service.publish_ranking(actor_id=actor_id, ranking_version_id=rest[1], **body))
        if len(rest) == 3 and rest[0] == "rankings" and rest[2] == "notifications":
            return _created(service.issue_notifications(actor_id=actor_id,
                                                        ranking_version_id=rest[1], **body))
        return None

    if method == "GET":
        if rest == ["tracks"]:
            return 200, {"items": service.list_tracks(actor_id=actor_id)}
        if rest == ["stages"]:
            return 200, {"items": service.list_stages(actor_id=actor_id)}
        if rest == ["pending"]:
            return 200, service.pending_tasks(actor_id=actor_id)
        if rest == ["scores"]:
            stage_id = _query(query, "stage_id")
            if not stage_id:
                from .errors import ValidationError
                raise ValidationError("stage_id 不能为空")
            return 200, {"items": service.list_scores(actor_id=actor_id, stage_id=stage_id,
                                                      entry_id=_query(query, "entry_id"))}
        if rest == ["appeals"]:
            return 200, {"items": service.list_appeals(
                actor_id=actor_id, ranking_version_id=_query(query, "ranking_version_id"))}
        if rest == ["me", "entries"]:
            return 200, {"items": service.my_entries(actor_id=actor_id)}
        if len(rest) == 2 and rest[0] == "stages":
            return 200, service.get_stage(actor_id=actor_id, stage_id=rest[1])
        if len(rest) == 3 and rest[0] == "stages" and rest[2] == "results":
            return 200, service.public_results(actor_id=actor_id, stage_id=rest[1])
        if len(rest) == 3 and rest[0] == "stages" and rest[2] == "rankings":
            return 200, {"items": service.list_rankings(actor_id=actor_id, stage_id=rest[1])}
        if len(rest) == 2 and rest[0] == "rankings":
            return 200, service.get_ranking(actor_id=actor_id, ranking_version_id=rest[1])
        if len(rest) == 3 and rest[0] == "rankings" and rest[2] == "replay":
            return 200, service.replay_ranking(actor_id=actor_id, ranking_version_id=rest[1])
        if (len(rest) == 5 and rest[0] == "rankings" and rest[2] == "entries"
                and rest[4] == "explain"):
            return 200, service.explain_entry(actor_id=actor_id, ranking_version_id=rest[1],
                                              entry_id=rest[3])
        if len(rest) == 2 and rest[0] == "appeals":
            return 200, service.get_appeal(actor_id=actor_id, appeal_id=rest[1])
        return None

    return None
