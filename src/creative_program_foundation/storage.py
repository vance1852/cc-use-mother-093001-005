"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_tracks (
    track_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_stages (
    stage_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence >= 0),
    status TEXT NOT NULL CHECK(status IN ('open', 'frozen', 'published')),
    frozen_rule_version_id TEXT,
    frozen_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_rule_versions (
    rule_version_id TEXT PRIMARY KEY,
    stage_id TEXT NOT NULL REFERENCES review_stages(stage_id),
    version INTEGER NOT NULL,
    config_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft', 'frozen')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    frozen_at TEXT,
    UNIQUE(stage_id, version)
);
CREATE TABLE IF NOT EXISTS review_entries (
    entry_id TEXT PRIMARY KEY,
    track_id TEXT NOT NULL REFERENCES review_tracks(track_id),
    participant_id TEXT NOT NULL REFERENCES actors(actor_id),
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'withdrawn')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_stage_entries (
    stage_id TEXT NOT NULL REFERENCES review_stages(stage_id),
    entry_id TEXT NOT NULL REFERENCES review_entries(entry_id),
    status TEXT NOT NULL CHECK(status IN ('enrolled', 'removed')),
    enrolled_at TEXT NOT NULL,
    PRIMARY KEY(stage_id, entry_id)
);
CREATE TABLE IF NOT EXISTS review_stage_judges (
    stage_id TEXT NOT NULL REFERENCES review_stages(stage_id),
    track_id TEXT NOT NULL REFERENCES review_tracks(track_id),
    judge_id TEXT NOT NULL REFERENCES actors(actor_id),
    valid INTEGER NOT NULL CHECK(valid IN (0, 1)),
    reason TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(stage_id, track_id, judge_id)
);
CREATE TABLE IF NOT EXISTS review_scores (
    score_id TEXT PRIMARY KEY,
    stage_id TEXT NOT NULL REFERENCES review_stages(stage_id),
    track_id TEXT NOT NULL REFERENCES review_tracks(track_id),
    entry_id TEXT NOT NULL REFERENCES review_entries(entry_id),
    judge_id TEXT NOT NULL REFERENCES actors(actor_id),
    dimension TEXT NOT NULL,
    value REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('submitted', 'sealed', 'rejected_late', 'revoked')),
    submitted_at TEXT NOT NULL,
    sealed_at TEXT,
    revoked_at TEXT,
    revoke_reason TEXT,
    corrected_from TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_review_scores_active
    ON review_scores(stage_id, entry_id, judge_id, dimension) WHERE status IN ('submitted', 'sealed');
CREATE INDEX IF NOT EXISTS idx_review_scores_stage ON review_scores(stage_id, entry_id);
CREATE TABLE IF NOT EXISTS review_deductions (
    deduction_id TEXT PRIMARY KEY,
    stage_id TEXT NOT NULL REFERENCES review_stages(stage_id),
    entry_id TEXT NOT NULL REFERENCES review_entries(entry_id),
    points REAL NOT NULL CHECK(points >= 0),
    reason TEXT NOT NULL,
    basis TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'withdrawn')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    withdrawn_at TEXT,
    withdrawn_by TEXT,
    withdraw_reason TEXT
);
CREATE TABLE IF NOT EXISTS review_disqualifications (
    disqualification_id TEXT PRIMARY KEY,
    stage_id TEXT NOT NULL REFERENCES review_stages(stage_id),
    entry_id TEXT NOT NULL REFERENCES review_entries(entry_id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'lifted')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    lifted_at TEXT,
    lifted_by TEXT,
    lift_reason TEXT
);
CREATE TABLE IF NOT EXISTS review_track_quotas (
    stage_id TEXT NOT NULL REFERENCES review_stages(stage_id),
    track_id TEXT NOT NULL REFERENCES review_tracks(track_id),
    advance_count INTEGER NOT NULL CHECK(advance_count >= 0),
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(stage_id, track_id)
);
CREATE TABLE IF NOT EXISTS review_award_constraints (
    constraint_id TEXT PRIMARY KEY,
    stage_id TEXT NOT NULL REFERENCES review_stages(stage_id),
    kind TEXT NOT NULL,
    params_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_ranking_versions (
    ranking_version_id TEXT PRIMARY KEY,
    stage_id TEXT NOT NULL REFERENCES review_stages(stage_id),
    version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('candidate', 'published', 'superseded')),
    rule_version_id TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    inputs_json TEXT NOT NULL,
    entries_hash TEXT NOT NULL,
    delta_json TEXT,
    trigger_type TEXT,
    trigger_id TEXT,
    generated_by TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    review_signed_by TEXT,
    review_signed_at TEXT,
    publish_signed_by TEXT,
    publish_signed_at TEXT,
    published_by TEXT,
    published_at TEXT,
    appeal_deadline TEXT,
    UNIQUE(stage_id, version)
);
CREATE TABLE IF NOT EXISTS review_ranking_entries (
    ranking_version_id TEXT NOT NULL REFERENCES review_ranking_versions(ranking_version_id),
    entry_id TEXT NOT NULL,
    track_id TEXT NOT NULL,
    rank INTEGER,
    total_score REAL,
    outcome TEXT NOT NULL,
    award_level TEXT,
    explanation_json TEXT NOT NULL,
    PRIMARY KEY(ranking_version_id, entry_id)
);
CREATE TABLE IF NOT EXISTS review_appeals (
    appeal_id TEXT PRIMARY KEY,
    ranking_version_id TEXT NOT NULL REFERENCES review_ranking_versions(ranking_version_id),
    stage_id TEXT NOT NULL REFERENCES review_stages(stage_id),
    entry_id TEXT NOT NULL REFERENCES review_entries(entry_id),
    appellant_id TEXT NOT NULL REFERENCES actors(actor_id),
    fact_type TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('filed', 'adjudicated_upheld', 'adjudicated_rejected')),
    filed_at TEXT NOT NULL,
    adjudicated_by TEXT,
    adjudicated_at TEXT,
    decision_note TEXT,
    corrections_json TEXT,
    resulting_version_id TEXT
);
CREATE TABLE IF NOT EXISTS review_notifications (
    notification_id TEXT PRIMARY KEY,
    ranking_version_id TEXT NOT NULL REFERENCES review_ranking_versions(ranking_version_id),
    stage_id TEXT NOT NULL REFERENCES review_stages(stage_id),
    entry_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    outcome_snapshot TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('sent', 'affected')),
    sent_by TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    affected_by_version_id TEXT
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
