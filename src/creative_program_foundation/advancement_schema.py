"""晋级评议与申诉模块的 SQLite 表结构。"""

from __future__ import annotations


ADVANCEMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS advancement_stages (
    stage_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence >= 1),
    reviewer_quorum INTEGER NOT NULL CHECK(reviewer_quorum >= 1),
    countersign_ttl_hours INTEGER NOT NULL CHECK(countersign_ttl_hours >= 1),
    appeal_window_hours INTEGER NOT NULL CHECK(appeal_window_hours >= 1),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS advancement_tracks (
    track_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS advancement_rule_versions (
    rule_version_id TEXT PRIMARY KEY,
    stage_id TEXT NOT NULL REFERENCES advancement_stages(stage_id),
    version INTEGER NOT NULL CHECK(version >= 1),
    status TEXT NOT NULL CHECK(status IN ('draft', 'frozen')),
    weights_json TEXT NOT NULL,
    tie_policy TEXT NOT NULL CHECK(tie_policy IN ('tie_break', 'share')),
    tie_break_json TEXT NOT NULL,
    missing_score_policy TEXT NOT NULL CHECK(missing_score_policy IN ('exclude_judge', 'zero')),
    late_score_policy TEXT NOT NULL CHECK(late_score_policy IN ('accept', 'reject')),
    score_deadline TEXT,
    pass_score REAL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    frozen_at TEXT,
    UNIQUE(stage_id, version)
);
CREATE TABLE IF NOT EXISTS advancement_track_rules (
    stage_id TEXT NOT NULL REFERENCES advancement_stages(stage_id),
    track_id TEXT NOT NULL REFERENCES advancement_tracks(track_id),
    rule_version_id TEXT NOT NULL REFERENCES advancement_rule_versions(rule_version_id),
    set_by TEXT NOT NULL,
    set_at TEXT NOT NULL,
    PRIMARY KEY(stage_id, track_id)
);
CREATE TABLE IF NOT EXISTS advancement_track_quotas (
    stage_id TEXT NOT NULL REFERENCES advancement_stages(stage_id),
    track_id TEXT NOT NULL REFERENCES advancement_tracks(track_id),
    quota INTEGER NOT NULL CHECK(quota >= 0),
    set_by TEXT NOT NULL,
    set_at TEXT NOT NULL,
    PRIMARY KEY(stage_id, track_id)
);
CREATE TABLE IF NOT EXISTS advancement_award_constraints (
    stage_id TEXT PRIMARY KEY REFERENCES advancement_stages(stage_id),
    total_awards INTEGER NOT NULL CHECK(total_awards >= 0),
    per_track_cap INTEGER NOT NULL CHECK(per_track_cap >= 0),
    set_by TEXT NOT NULL,
    set_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS advancement_judge_assignments (
    stage_id TEXT NOT NULL REFERENCES advancement_stages(stage_id),
    track_id TEXT NOT NULL REFERENCES advancement_tracks(track_id),
    judge_id TEXT NOT NULL REFERENCES actors(actor_id),
    valid INTEGER NOT NULL CHECK(valid IN (0, 1)),
    set_by TEXT NOT NULL,
    set_at TEXT NOT NULL,
    PRIMARY KEY(stage_id, track_id, judge_id)
);
CREATE TABLE IF NOT EXISTS advancement_entries (
    entry_id TEXT PRIMARY KEY,
    stage_id TEXT NOT NULL REFERENCES advancement_stages(stage_id),
    track_id TEXT NOT NULL REFERENCES advancement_tracks(track_id),
    participant_id TEXT NOT NULL REFERENCES actors(actor_id),
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'disqualified')),
    disqualified_reason TEXT,
    disqualified_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS advancement_scores (
    score_id TEXT PRIMARY KEY,
    stage_id TEXT NOT NULL,
    track_id TEXT NOT NULL,
    entry_id TEXT NOT NULL REFERENCES advancement_entries(entry_id),
    judge_id TEXT NOT NULL REFERENCES actors(actor_id),
    scores_json TEXT NOT NULL,
    deduction REAL NOT NULL CHECK(deduction >= 0),
    deduction_basis TEXT,
    status TEXT NOT NULL CHECK(status IN ('submitted', 'sealed', 'revoked')),
    submitted_at TEXT NOT NULL,
    sealed_at TEXT,
    revoked_at TEXT,
    revoke_reason TEXT,
    UNIQUE(entry_id, judge_id)
);
CREATE TABLE IF NOT EXISTS advancement_runs (
    run_id TEXT PRIMARY KEY,
    stage_id TEXT NOT NULL REFERENCES advancement_stages(stage_id),
    status TEXT NOT NULL CHECK(status IN ('candidate', 'published', 'superseded', 'rejected', 'expired')),
    version INTEGER,
    snapshot_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    output_hash TEXT NOT NULL,
    change_summary_json TEXT,
    supersedes_run_id TEXT,
    countersign_deadline TEXT NOT NULL,
    appeal_deadline TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT
);
CREATE TABLE IF NOT EXISTS advancement_run_items (
    run_id TEXT NOT NULL REFERENCES advancement_runs(run_id),
    entry_id TEXT NOT NULL REFERENCES advancement_entries(entry_id),
    track_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    total_score REAL,
    rank INTEGER,
    advanced INTEGER NOT NULL CHECK(advanced IN (0, 1)),
    awarded INTEGER NOT NULL CHECK(awarded IN (0, 1)),
    explanation_json TEXT NOT NULL,
    PRIMARY KEY(run_id, entry_id)
);
CREATE TABLE IF NOT EXISTS advancement_countersigns (
    run_id TEXT NOT NULL REFERENCES advancement_runs(run_id),
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    duty TEXT NOT NULL CHECK(duty IN ('reviewer', 'publisher')),
    decision TEXT NOT NULL CHECK(decision IN ('approved', 'rejected')),
    comment TEXT,
    decided_at TEXT NOT NULL,
    PRIMARY KEY(run_id, actor_id)
);
CREATE TABLE IF NOT EXISTS advancement_appeals (
    appeal_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES advancement_runs(run_id),
    stage_id TEXT NOT NULL,
    entry_id TEXT NOT NULL REFERENCES advancement_entries(entry_id),
    participant_id TEXT NOT NULL REFERENCES actors(actor_id),
    target_type TEXT NOT NULL CHECK(target_type IN ('score', 'qualification')),
    target_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('filed', 'adjudicated_upheld', 'adjudicated_rejected', 'expired')),
    filed_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    adjudicated_by TEXT,
    adjudicated_at TEXT,
    decision_note TEXT,
    remedy TEXT,
    result_run_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS advancement_notifications (
    notification_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES advancement_runs(run_id),
    entry_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('advancement', 'award', 'elimination')),
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""


def ensure_advancement_schema(connection) -> None:
    """在共享连接上幂等地创建模块表。"""

    connection.executescript(ADVANCEMENT_SCHEMA)
