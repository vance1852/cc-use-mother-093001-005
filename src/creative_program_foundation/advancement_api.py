"""晋级评议与申诉系统的 HTTP/JSON 路由。"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

from .advancement import AdvancementService
from .errors import DomainError, ValidationError


def _created(receipt: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return (200 if receipt["replayed"] else 201), receipt


def route(service: AdvancementService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把 /advancement 前缀的 HTTP 语义请求分派到晋级评议服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "POST" and parsed.path == "/advancement/stages":
            return _created(service.create_stage(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/advancement/tracks":
            return _created(service.create_track(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/advancement/rule-versions":
            return _created(service.create_rule_version(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/advancement/rule-versions/freeze":
            return _created(service.freeze_rule_version(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/advancement/track-rules":
            return _created(service.assign_track_rule(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/advancement/quotas":
            return _created(service.set_track_quota(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/advancement/award-constraints":
            return _created(service.set_award_constraint(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/advancement/judges":
            return _created(service.assign_judge(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/advancement/entries":
            return _created(service.register_entry(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/advancement/entries/disqualify":
            return _created(service.disqualify_entry(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/advancement/entries/reinstate":
            return _created(service.reinstate_entry(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/advancement/scores":
            return _created(service.submit_score(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/advancement/scores/revoke":
            return _created(service.revoke_score(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/advancement/scores/seal":
            return _created(service.seal_scores(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/advancement/runs":
            return _created(service.compute_run(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/advancement/runs/publish":
            return _created(service.publish_run(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/advancement/runs/replay":
            run_id = body.get("run_id", "")
            return 200, service.replay_run(actor_id=actor_id, run_id=run_id)
        if method == "POST" and parsed.path == "/advancement/countersigns":
            return _created(service.countersign(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/advancement/appeals":
            return _created(service.file_appeal(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/advancement/appeals/adjudicate":
            return _created(service.adjudicate_appeal(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/advancement/tick":
            return 200, service.tick(actor_id=actor_id)
        if method == "GET" and parsed.path == "/advancement/stages":
            return 200, {"items": service.list_stages(actor_id=actor_id)}
        if method == "GET" and parsed.path == "/advancement/stages/detail":
            stage_id = query.get("stage_id", [""])[0]
            if not stage_id:
                raise ValidationError("stage_id 不能为空")
            return 200, service.get_stage_detail(actor_id=actor_id, stage_id=stage_id)
        if method == "GET" and parsed.path == "/advancement/scores":
            stage_id = query.get("stage_id", [""])[0]
            if not stage_id:
                raise ValidationError("stage_id 不能为空")
            return 200, {"items": service.list_scores(
                actor_id=actor_id, stage_id=stage_id,
                entry_id=query.get("entry_id", [None])[0],
                judge_id=query.get("judge_id", [None])[0])}
        if method == "GET" and parsed.path == "/advancement/runs":
            return 200, {"items": service.list_runs(
                actor_id=actor_id, stage_id=query.get("stage_id", [None])[0])}
        if method == "GET" and parsed.path == "/advancement/runs/detail":
            run_id = query.get("run_id", [""])[0]
            if not run_id:
                raise ValidationError("run_id 不能为空")
            return 200, service.get_run_detail(actor_id=actor_id, run_id=run_id)
        if method == "GET" and parsed.path == "/advancement/runs/explain":
            run_id = query.get("run_id", [""])[0]
            entry_id = query.get("entry_id", [""])[0]
            if not run_id or not entry_id:
                raise ValidationError("run_id 与 entry_id 不能为空")
            return 200, service.explain_item(actor_id=actor_id, run_id=run_id, entry_id=entry_id)
        if method == "GET" and parsed.path == "/advancement/appeals":
            return 200, {"items": service.list_appeals(
                actor_id=actor_id, stage_id=query.get("stage_id", [None])[0])}
        if method == "GET" and parsed.path == "/advancement/public/results":
            stage_id = query.get("stage_id", [""])[0]
            if not stage_id:
                raise ValidationError("stage_id 不能为空")
            return 200, service.public_results(actor_id=actor_id, stage_id=stage_id)
        if method == "GET" and parsed.path == "/advancement/my/entries":
            return 200, service.my_entries(actor_id=actor_id)
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}
