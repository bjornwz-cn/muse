-- NAS Curator 统一元数据 schema（SQLite，WAL 模式）。
-- 幂等：CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS，可反复执行。
-- 文档与媒体共用 files 表（kind 区分）；分类定物理位置（单归属），标签只影响检索（多归属）。

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS files (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    path       TEXT NOT NULL UNIQUE,
    sha256     TEXT,
    size       INTEGER,
    mtime      REAL,
    kind       TEXT NOT NULL CHECK (kind IN ('document', 'media', 'unit')) DEFAULT 'document',
    ext        TEXT,
    status     TEXT NOT NULL CHECK (status IN ('active', 'review', 'quarantine', 'gone')) DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files (sha256);
CREATE INDEX IF NOT EXISTS idx_files_kind_status ON files (kind, status);

CREATE TABLE IF NOT EXISTS file_text (
    file_id      INTEGER PRIMARY KEY REFERENCES files (id) ON DELETE CASCADE,
    text         TEXT,
    source       TEXT,
    extracted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

CREATE TABLE IF NOT EXISTS file_media (
    file_id     INTEGER PRIMARY KEY REFERENCES files (id) ON DELETE CASCADE,
    dhash       INTEGER,
    phash       INTEGER,
    width       INTEGER,
    height      INTEGER,
    duration    REAL,
    blur        REAL,
    clip_scores TEXT,
    captions    TEXT
);

CREATE TABLE IF NOT EXISTS file_emb (
    file_id    INTEGER NOT NULL REFERENCES files (id) ON DELETE CASCADE,
    model      TEXT NOT NULL,
    vector     BLOB,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    PRIMARY KEY (file_id, model)
);

CREATE TABLE IF NOT EXISTS categories (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    path     TEXT NOT NULL UNIQUE,
    kind     TEXT,
    keywords TEXT,
    desc     TEXT,
    samples  TEXT,
    exts     TEXT,
    parent_id INTEGER REFERENCES categories (id)
);
CREATE INDEX IF NOT EXISTS idx_categories_kind ON categories (kind);

CREATE TABLE IF NOT EXISTS entities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    category_id INTEGER REFERENCES categories (id),
    count       INTEGER NOT NULL DEFAULT 0,
    ratio       REAL NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

CREATE TABLE IF NOT EXISTS tags (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT NOT NULL UNIQUE,
    kind  TEXT,
    color TEXT
);

CREATE TABLE IF NOT EXISTS file_tags (
    file_id    INTEGER NOT NULL REFERENCES files (id) ON DELETE CASCADE,
    tag_id     INTEGER NOT NULL REFERENCES tags (id) ON DELETE CASCADE,
    source     TEXT NOT NULL CHECK (source IN ('auto', 'manual')) DEFAULT 'auto',
    confidence REAL,
    PRIMARY KEY (file_id, tag_id, source)
);
CREATE INDEX IF NOT EXISTS idx_file_tags_tag ON file_tags (tag_id);

CREATE TABLE IF NOT EXISTS rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    version    INTEGER NOT NULL,
    payload    TEXT NOT NULL,
    is_active  INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    UNIQUE (name, version)
);

CREATE TABLE IF NOT EXISTS plans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    library       TEXT,
    rules_version INTEGER,
    status        TEXT NOT NULL CHECK (status IN ('draft', 'pending_review', 'approved', 'applying', 'done', 'rolled_back', 'failed')) DEFAULT 'draft',
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    summary       TEXT
);
CREATE INDEX IF NOT EXISTS idx_plans_status ON plans (status);

CREATE TABLE IF NOT EXISTS plan_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id     INTEGER NOT NULL REFERENCES plans (id) ON DELETE CASCADE,
    file_id     INTEGER REFERENCES files (id),
    src_path    TEXT NOT NULL,
    dst_path    TEXT,
    new_name    TEXT,
    category_id INTEGER REFERENCES categories (id),
    category    TEXT,
    confidence  REAL,
    reason      TEXT,
    evidence    TEXT,
    action      TEXT NOT NULL CHECK (action IN ('move', 'rename', 'tag', 'skip', 'review')) DEFAULT 'move',
    kind        TEXT,
    size        INTEGER,
    ext         TEXT,
    sha256      TEXT,
    tags        TEXT,
    review_kind TEXT,
    status      TEXT NOT NULL CHECK (status IN ('pending', 'done', 'skipped', 'failed')) DEFAULT 'pending',
    auto_exempt INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_plan_items_plan ON plan_items (plan_id);

CREATE TABLE IF NOT EXISTS operations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id       INTEGER REFERENCES plans (id),
    src_path      TEXT NOT NULL,
    dst_path      TEXT NOT NULL,
    sha256        TEXT,
    executed_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    rolled_back_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_operations_plan ON operations (plan_id);

CREATE TABLE IF NOT EXISTS review_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id    INTEGER REFERENCES files (id),
    plan_id    INTEGER REFERENCES plans (id),
    kind       TEXT NOT NULL CHECK (kind IN ('low_conf', 'dup', 'blur', 'ambiguous', 'shell', 'sensitive')),
    payload    TEXT,
    status     TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')) DEFAULT 'pending',
    decided_by TEXT,
    decided_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_review_status ON review_queue (status);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('running', 'done', 'failed')) DEFAULT 'running',
    started_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    finished_at TEXT,
    log_path    TEXT,
    summary     TEXT
);

-- 自学习反馈回路（v0.2.2，见 docs/SELF-LEARNING-DESIGN.md；与 common/learn.py 的
-- ensure_tables() 建表口径一致，IF NOT EXISTS，可反复执行）。
CREATE TABLE IF NOT EXISTS decision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_path TEXT NOT NULL, ext TEXT, kind TEXT,
    predicted_category TEXT, predicted_confidence REAL, predicted_reason TEXT,
    final_category TEXT, final_dst TEXT,
    outcome TEXT NOT NULL CHECK (outcome IN ('auto_applied','review_approved','review_rejected','user_corrected','manual_move')),
    corrected INTEGER NOT NULL DEFAULT 0,
    rule_ids_applied TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    processed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_decision_processed ON decision_log (processed);

CREATE TABLE IF NOT EXISTS learned_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('filename_pattern','content_keyword','source_path','format_heuristic','policy','media_filename')),
    pattern TEXT NOT NULL, target TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.55,
    confirmations INTEGER NOT NULL DEFAULT 0,
    corrections INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ('active','disabled')) DEFAULT 'active',
    source TEXT, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    UNIQUE (kind, pattern, target)
);
CREATE INDEX IF NOT EXISTS idx_learned_status ON learned_rules (status);

CREATE TABLE IF NOT EXISTS learn_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER, event TEXT NOT NULL,
    detail TEXT, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);
